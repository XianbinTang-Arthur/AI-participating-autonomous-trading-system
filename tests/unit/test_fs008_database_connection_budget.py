"""FS-008: PostgreSQL connection-budget and engine-inventory contracts."""

from __future__ import annotations

from unittest.mock import patch, sentinel

import pytest

from aats.storage.connection_budget import (
    MIN_OPERATIONAL_CONNECTION_RESERVE,
    POSTGRES_MAX_CONNECTIONS,
    POSTGRES_SUPERUSER_RESERVED_CONNECTIONS,
    PRIMARY_STORAGE_POOL_LIMITS,
    declared_live_topology_components,
    declared_live_topology_connection_ceiling,
    declared_operational_connection_reserve,
    primary_storage_pool_limit,
)
from aats.storage.session import create_database_runtime
from scripts import verify_database_connection_budget as budget_verifier


def test_primary_pool_limits_are_role_specific_and_bounded() -> None:
    assert {role: limits.ceiling for role, limits in PRIMARY_STORAGE_POOL_LIMITS.items()} == {
        "gateway": 32,
        "market": 8,
        "decision": 10,
        "execution": 16,
        "monolith": 32,
    }
    assert primary_storage_pool_limit(None) == PRIMARY_STORAGE_POOL_LIMITS["monolith"]
    with pytest.raises(ValueError, match="unsupported database pool process role"):
        primary_storage_pool_limit("unknown")


def test_declared_full_live_topology_keeps_reviewed_operational_reserve() -> None:
    assert declared_live_topology_connection_ceiling() == 150
    assert declared_operational_connection_reserve() == 47
    assert POSTGRES_MAX_CONNECTIONS == 200
    assert POSTGRES_SUPERUSER_RESERVED_CONNECTIONS == 3
    assert declared_operational_connection_reserve() >= MIN_OPERATIONAL_CONNECTION_RESERVE
    assert len(declared_live_topology_components()) == 14


@pytest.mark.parametrize("role", ["gateway", "market", "decision", "execution", None])
def test_primary_database_runtime_consumes_role_budget(role: str | None) -> None:
    database_url = "postgresql+psycopg://user:pass@localhost:5432/aats"
    expected = primary_storage_pool_limit(role)
    with (
        patch("aats.storage.session.create_engine", return_value=sentinel.engine) as create_engine_mock,
        patch(
            "aats.storage.session.sessionmaker",
            return_value=sentinel.session_factory,
        ),
    ):
        create_database_runtime(database_url, process_role=role)

    kwargs = create_engine_mock.call_args.kwargs
    assert kwargs["pool_size"] == expected.pool_size
    assert kwargs["max_overflow"] == expected.max_overflow
    assert kwargs["pool_timeout"] == 30


def test_compose_capacity_and_all_application_engine_sites_are_classified() -> None:
    budget_verifier.verify_compose_capacity()
    budget_verifier.verify_workflow()
    assert budget_verifier.verify_engine_inventory() == 13


def test_connection_budget_verifier_entrypoint() -> None:
    assert budget_verifier.main() == 0


@pytest.mark.parametrize(
    "source",
    [
        "from sqlalchemy import create_engine as make_engine\nmake_engine('postgresql://')\n",
        "import sqlalchemy as sa\nsa.create_engine('postgresql://')\n",
        (
            "from sqlalchemy.ext.asyncio import create_async_engine as make_async\n"
            "make_async('postgresql+asyncpg://')\n"
        ),
    ],
)
def test_engine_inventory_scanner_detects_sqlalchemy_factory_aliases(
    tmp_path,
    source: str,
) -> None:
    path = tmp_path / "engine_alias.py"
    path.write_text(source, encoding="utf-8-sig")
    assert len(budget_verifier._create_engine_calls(path)) == 1
