"""Verify FS-008 declared PostgreSQL connection-budget contracts.

This verifier performs no database or network I/O.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from aats.storage.connection_budget import (
    MIN_OPERATIONAL_CONNECTION_RESERVE,
    POSTGRES_MAX_CONNECTIONS,
    POSTGRES_SUPERUSER_RESERVED_CONNECTIONS,
    declared_live_topology_components,
    declared_live_topology_connection_ceiling,
    declared_operational_connection_reserve,
)


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy" / "wsl2-dev" / "docker-compose.yml"
WORKFLOW = ROOT / ".github" / "workflows" / "quality.yml"
ENGINE_FACTORY_NAMES = {"create_engine", "create_async_engine"}

# Every application create_engine location must be classified. New locations
# make CI fail until their lifecycle and budget are reviewed.
EXPECTED_POOL_ROOTS = {
    "aats/api/_governance_db.py": {"GATEWAY_GOVERNANCE_API_POOL"},
    "aats/bootstrap/active_parameters.py": {"ACTIVE_PARAMETER_TRANSIENT_POOL"},
    "aats/data_platform/db.py": {"RDP_RESEARCH_POOL"},
    "aats/data_platform/governance/_db_util.py": {
        "GOVERNANCE_TRANSIENT_ENGINE_POOL",
        "RDP_GOVERNANCE_CACHE_POOL",
    },
    "aats/data_platform/live_facts/db.py": {"RDP_LIVE_FACTS_POOL"},
    "aats/data_platform/live_query_adapter.py": {"RDP_LIVE_QUERY_POOL"},
    "aats/data_platform/runtime/live_session.py": {
        "RDP_LIVE_SESSION_RO_POOL",
        "RDP_LIVE_SESSION_RW_POOL",
    },
    "aats/services/execution_engine/orderbook_snapshot_refs.py": {
        "ORDERBOOK_READ_POOL"
    },
    "aats/storage/session.py": {"pool_limits"},
}
MANAGED_ENGINE_FILES = set(EXPECTED_POOL_ROOTS)
NULL_POOL_ENGINE_FILES = {
    "aats/cli.py",
    "aats/services/operator/missed_market_replay.py",
}


def _create_engine_calls(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    imported_factories: set[str] = set()
    sqlalchemy_module_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("sqlalchemy"):
            imported_factories.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name in ENGINE_FACTORY_NAMES
            )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sqlalchemy" or alias.name.startswith("sqlalchemy."):
                    sqlalchemy_module_aliases.add(alias.asname or alias.name.split(".", 1)[0])

    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if isinstance(function, ast.Name) and function.id in imported_factories:
            calls.append(node)
        elif isinstance(function, ast.Attribute) and function.attr in ENGINE_FACTORY_NAMES:
            root = function.value
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name) and root.id in sqlalchemy_module_aliases:
                calls.append(node)
    return calls


def verify_engine_inventory() -> int:
    """Ensure all application engine creation sites have reviewed pool semantics."""

    discovered: dict[str, list[ast.Call]] = {}
    for path in sorted((ROOT / "aats").rglob("*.py")):
        calls = _create_engine_calls(path)
        if calls:
            discovered[path.relative_to(ROOT).as_posix()] = calls

    expected = MANAGED_ENGINE_FILES | NULL_POOL_ENGINE_FILES
    if set(discovered) != expected:
        raise ValueError(
            "create_engine inventory drifted: "
            f"missing={sorted(expected - set(discovered))}, "
            f"unclassified={sorted(set(discovered) - expected)}"
        )

    for relative, calls in discovered.items():
        path = ROOT / relative
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for call in calls:
            keywords = {keyword.arg: keyword.value for keyword in call.keywords if keyword.arg}
            if relative in NULL_POOL_ENGINE_FILES:
                poolclass = keywords.get("poolclass")
                if not (
                    isinstance(poolclass, ast.Name)
                    and poolclass.id == "NullPool"
                ):
                    raise ValueError(f"{relative}: short-lived engine must use NullPool")
                continue

            pool_roots: set[str] = set()
            for keyword in ("pool_size", "max_overflow"):
                value = keywords.get(keyword)
                if value is None:
                    raise ValueError(f"{relative}: create_engine misses {keyword}")
                if not (
                    isinstance(value, ast.Attribute)
                    and value.attr == keyword
                    and isinstance(value.value, ast.Name)
                ):
                    raise ValueError(
                        f"{relative}: {keyword} must use connection_budget single truth"
                    )
                pool_roots.add(value.value.id)
            if len(pool_roots) != 1 or not pool_roots <= EXPECTED_POOL_ROOTS[relative]:
                raise ValueError(
                    f"{relative}: unexpected pool roots {sorted(pool_roots)}; "
                    f"expected one of {sorted(EXPECTED_POOL_ROOTS[relative])}"
                )

        if relative in MANAGED_ENGINE_FILES:
            observed_pool_roots = {
                value.value.id
                for call in calls
                for keyword in call.keywords
                if keyword.arg in {"pool_size", "max_overflow"}
                and isinstance((value := keyword.value), ast.Attribute)
                and isinstance(value.value, ast.Name)
            }
            if observed_pool_roots != EXPECTED_POOL_ROOTS[relative]:
                raise ValueError(
                    f"{relative}: pool inventory drifted: "
                    f"observed={sorted(observed_pool_roots)}, "
                    f"expected={sorted(EXPECTED_POOL_ROOTS[relative])}"
                )

            required_imports = EXPECTED_POOL_ROOTS[relative] - {"pool_limits"}
            imported_budget_names = {
                alias.asname or alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
                and node.module == "aats.storage.connection_budget"
                for alias in node.names
            }
            if not required_imports <= imported_budget_names:
                raise ValueError(
                    f"{relative}: pool limits are not imported from connection_budget"
                )

        if relative == "aats/storage/session.py":
            has_role_resolver = any(
                isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == "pool_limits"
                    for target in node.targets
                )
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "primary_storage_pool_limit"
                and any(
                    isinstance(argument, ast.Name) and argument.id == "process_role"
                    for argument in node.value.args
                )
                for node in ast.walk(tree)
            )
            if not has_role_resolver:
                raise ValueError(
                    "aats/storage/session.py: primary pool is not resolved by process role"
                )
    return sum(len(calls) for calls in discovered.values())


def verify_compose_capacity() -> None:
    """Verify Compose makes the reviewed PostgreSQL capacity explicit."""

    text = COMPOSE.read_text(encoding="utf-8")
    max_match = re.search(r'"max_connections=(\d+)"', text)
    reserved_match = re.search(r'"superuser_reserved_connections=(\d+)"', text)
    if max_match is None or int(max_match.group(1)) != POSTGRES_MAX_CONNECTIONS:
        raise ValueError("Compose max_connections differs from connection budget")
    if (
        reserved_match is None
        or int(reserved_match.group(1)) != POSTGRES_SUPERUSER_RESERVED_CONNECTIONS
    ):
        raise ValueError(
            "Compose superuser_reserved_connections differs from connection budget"
        )


def verify_workflow() -> None:
    """Ensure the repository quality gate runs this verifier."""

    command = "python scripts/verify_database_connection_budget.py"
    if command not in WORKFLOW.read_text(encoding="utf-8"):
        raise ValueError(f"quality workflow misses connection budget verifier: {command}")


def main() -> int:
    """Run all static connection-budget checks."""

    ceiling = declared_live_topology_connection_ceiling()
    reserve = declared_operational_connection_reserve()
    if reserve < MIN_OPERATIONAL_CONNECTION_RESERVE:
        raise ValueError(
            f"declared operational reserve {reserve} is below "
            f"minimum {MIN_OPERATIONAL_CONNECTION_RESERVE}"
        )
    components = declared_live_topology_components()
    if len({component.name for component in components}) != len(components):
        raise ValueError("declared connection component names must be unique")
    verify_compose_capacity()
    verify_workflow()
    engine_calls = verify_engine_inventory()
    print(
        "database connection budget OK: "
        f"declared_ceiling={ceiling} operational_reserve={reserve} "
        f"components={len(components)} engine_calls={engine_calls}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
