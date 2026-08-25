"""FS-009：schema apply 必须显式、可记账；应用启动只验证且失败关闭。"""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from apps.api_gateway import main as gateway_main
from aats.bootstrap.managed_profiles import MANAGED_PROFILE_DEFINITIONS
from aats.data_platform.migrations import _batch_b
from aats.storage.session import validate_current_migrations


class _RowsResult:
    def __init__(self, rows=None, scalar=None) -> None:
        self._rows = list(rows or [])
        self._scalar = scalar

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)

    def scalar_one_or_none(self):
        return self._scalar


class _FakeConnection:
    def __init__(self, engine: "_FakeEngine", transaction_id: int | None = None) -> None:
        self.engine = engine
        self.transaction_id = transaction_id

    def execute(self, statement, params=None):
        sql = str(statement)
        params = params or {}
        self.engine.executed_sql.append(sql)
        self.engine.executed_transactions.append((sql, self.transaction_id))
        if "SELECT version, checksum" in sql:
            return _RowsResult(
                [
                    {"version": version, "checksum": checksum}
                    for version, checksum in self.engine.ledger.items()
                ]
            )
        if "INSERT INTO governance.rdp_schema_migrations" in sql:
            self.engine.ledger[str(params["version"])] = str(params["checksum"])
            return _RowsResult()
        if "DELETE FROM governance.rdp_schema_migrations" in sql:
            self.engine.ledger.pop(str(params["version"]), None)
            return _RowsResult()
        for stage in self.engine.fail_stages:
            if stage in sql:
                raise RuntimeError(f"simulated stage failure: {stage}")
        return _RowsResult()


class _FakeEngine:
    dialect = SimpleNamespace(name="sqlite")

    def __init__(self) -> None:
        self.ledger: dict[str, str] = {}
        self.fail_stages: set[str] = set()
        self.executed_sql: list[str] = []
        self.executed_transactions: list[tuple[str, int | None]] = []
        self.transaction_count = 0

    @contextmanager
    def begin(self):
        self.transaction_count += 1
        yield _FakeConnection(self, self.transaction_count)

    @contextmanager
    def connect(self):
        yield _FakeConnection(self)


def test_batch_b_full_chain_is_ledgered_and_second_run_skips() -> None:
    engine = _FakeEngine()

    first = _batch_b.run_batch_b_migrations(engine)
    second = _batch_b.run_batch_b_migrations(engine)

    assert first.ok is True
    assert [stage.stage for stage in first.stages] == list(_batch_b.BATCH_B_STAGES)
    assert all(stage.applied for stage in first.stages)
    assert second.ok is True
    assert all(not stage.applied for stage in second.stages)
    assert set(engine.ledger) == set(_batch_b.BATCH_B_STAGES)
    assert _batch_b.validate_batch_b_migrations(engine) == _batch_b.BATCH_B_STAGES


def test_batch_b_schema_change_and_ledger_row_share_one_transaction() -> None:
    engine = _FakeEngine()
    stage = _batch_b.BATCH_B_STAGES[0]
    original_load_sql = _batch_b._load_sql

    def _marked_sql(name: str, *, rollback: bool = False) -> str:
        return f"-- transaction-marker:{name}\n" + original_load_sql(
            name,
            rollback=rollback,
        )

    with patch.object(_batch_b, "_load_sql", side_effect=_marked_sql):
        report = _batch_b.run_batch_b_migrations(engine, stages=[stage])

    assert report.ok is True
    stage_transaction = next(
        transaction_id
        for sql, transaction_id in engine.executed_transactions
        if f"transaction-marker:{stage}" in sql
    )
    ledger_transaction = next(
        transaction_id
        for sql, transaction_id in engine.executed_transactions
        if "INSERT INTO governance.rdp_schema_migrations" in sql
    )
    assert stage_transaction == ledger_transaction


def test_batch_b_rejects_sql_without_one_outer_transaction_wrapper() -> None:
    engine = _FakeEngine()
    stage = _batch_b.BATCH_B_STAGES[0]

    with patch.object(_batch_b, "_load_sql", return_value="SELECT 1;"):
        report = _batch_b.run_batch_b_migrations(engine, stages=[stage])

    assert report.ok is False
    assert "transaction_wrapper_invalid" in str(report.error_message)
    assert stage not in engine.ledger


def test_batch_b_checksum_mismatch_fails_closed_without_reapplying() -> None:
    engine = _FakeEngine()
    assert _batch_b.run_batch_b_migrations(engine).ok
    first_stage = _batch_b.BATCH_B_STAGES[0]
    engine.ledger[first_stage] = "tampered-checksum"

    report = _batch_b.run_batch_b_migrations(engine)

    assert report.ok is False
    assert report.stages[0].stage == first_stage
    assert report.stages[0].error_type == "RuntimeError"
    assert "checksum_mismatch" in str(report.stages[0].error_message)


def test_batch_b_stage_failure_stops_chain_and_does_not_write_failed_ledger() -> None:
    engine = _FakeEngine()
    failed_stage = _batch_b.BATCH_B_STAGES[2]
    engine.fail_stages.add(failed_stage)
    original_load_sql = _batch_b._load_sql

    def _load_sql_with_stage_marker(stage: str, *, rollback: bool = False) -> str:
        return f"-- {stage}\n" + original_load_sql(stage, rollback=rollback)

    with patch.object(
        _batch_b,
        "_load_sql",
        side_effect=_load_sql_with_stage_marker,
    ):
        report = _batch_b.run_batch_b_migrations(engine)

    assert report.ok is False
    assert [stage.stage for stage in report.stages] == list(
        _batch_b.BATCH_B_STAGES[:3]
    )
    assert failed_stage not in engine.ledger
    assert set(engine.ledger) == set(_batch_b.BATCH_B_STAGES[:2])


def test_batch_b_partial_forward_requires_all_canonical_predecessors() -> None:
    engine = _FakeEngine()
    target = _batch_b.BATCH_B_STAGES[3]

    report = _batch_b.run_batch_b_migrations(engine, stages=[target])

    assert report.ok is False
    assert "prerequisite_missing" in str(report.error_message)
    assert target not in engine.ledger


def test_batch_b_rollback_rejects_non_suffix_and_updates_ledger_for_suffix() -> None:
    engine = _FakeEngine()
    assert _batch_b.run_batch_b_migrations(engine).ok
    first_stage = _batch_b.BATCH_B_STAGES[0]
    last_stage = _batch_b.BATCH_B_STAGES[-1]

    rejected = _batch_b.run_batch_b_rollback(engine, stages=[first_stage])
    accepted = _batch_b.run_batch_b_rollback(engine, stages=[last_stage])

    assert rejected.ok is False
    assert "not_applied_suffix" in str(rejected.error_message)
    assert accepted.ok is True
    assert accepted.stages[0].applied is True
    assert last_stage not in engine.ledger
    assert first_stage in engine.ledger


class _RootLedgerConnection:
    def __init__(self, rows, *, ledger_exists: bool = True) -> None:
        self.rows = rows
        self.ledger_exists = ledger_exists

    def execute(self, statement, _params=None):
        sql = str(statement)
        if "to_regclass" in sql:
            return _RowsResult(
                scalar="schema_migrations" if self.ledger_exists else None
            )
        if "SELECT version, checksum" in sql:
            return _RowsResult(self.rows)
        raise AssertionError(sql)


class _RootLedgerEngine:
    dialect = SimpleNamespace(name="postgresql")

    def __init__(self, rows, *, ledger_exists: bool = True) -> None:
        self.connection = _RootLedgerConnection(
            rows,
            ledger_exists=ledger_exists,
        )

    @contextmanager
    def connect(self):
        yield self.connection


def _root_expected_rows() -> list[dict[str, str]]:
    migrations_dir = Path(__file__).resolve().parents[2] / "migrations"
    return [
        {
            "version": path.name,
            "checksum": hashlib.sha256(
                path.read_text(encoding="utf-8").encode("utf-8")
            ).hexdigest(),
        }
        for path in sorted(migrations_dir.glob("*.sql"))
    ]


@pytest.mark.parametrize("failure_mode", ["missing", "mismatched", "unknown"])
def test_root_migration_validator_fails_on_any_ledger_drift(
    failure_mode: str,
) -> None:
    rows = _root_expected_rows()
    if failure_mode == "missing":
        rows.pop()
    elif failure_mode == "mismatched":
        rows[0] = {**rows[0], "checksum": "tampered"}
    else:
        rows.append({"version": "999_future.sql", "checksum": "future"})
    runtime = SimpleNamespace(engine=_RootLedgerEngine(rows))

    with pytest.raises(RuntimeError, match="migration_contract_failed"):
        validate_current_migrations(runtime)


def test_root_migration_validator_accepts_exact_current_checkout() -> None:
    runtime = SimpleNamespace(engine=_RootLedgerEngine(_root_expected_rows()))
    validate_current_migrations(runtime)


class _GatewayRuntime:
    def __init__(self) -> None:
        self.hot_state_store = None
        self.started = False
        self.stopped = False

    async def start_background_tasks(self) -> None:
        self.started = True

    async def stop_background_tasks(self) -> None:
        self.stopped = True


async def _noop_readiness(**_kwargs) -> None:
    return None


def test_gateway_schema_validation_failure_blocks_lifespan_and_cleans_up() -> None:
    async def _run() -> tuple[_GatewayRuntime, AsyncMock, AsyncMock]:
        app = SimpleNamespace(state=SimpleNamespace())
        runtime = _GatewayRuntime()
        start_dashboard = AsyncMock()
        stop_dashboard = AsyncMock()
        with (
            patch.object(gateway_main, "load_settings", return_value=SimpleNamespace()),
            patch.object(gateway_main, "configure_logging_for_settings"),
            patch.object(gateway_main, "_resolved_process_role", return_value="gateway"),
            patch.object(
                gateway_main,
                "build_runtime",
                AsyncMock(return_value=runtime),
            ),
            patch.object(
                gateway_main,
                "_announce_runtime_ready",
                side_effect=_noop_readiness,
            ),
            patch.object(
                gateway_main,
                "_wait_for_peer_roles_ready",
                side_effect=_noop_readiness,
            ),
            patch.object(
                gateway_main,
                "start_dashboard_snapshot_plane",
                start_dashboard,
            ),
            patch.object(
                gateway_main,
                "stop_dashboard_snapshot_plane",
                stop_dashboard,
            ),
            patch(
                "aats.data_platform.db.validate_rdp_schema",
                side_effect=RuntimeError("simulated schema contract failure"),
            ),
        ):
            with pytest.raises(RuntimeError, match="schema contract failure"):
                async with gateway_main.lifespan(app):
                    pytest.fail("lifespan must not yield after schema failure")
        return runtime, start_dashboard, stop_dashboard

    runtime, start_dashboard, stop_dashboard = asyncio.run(_run())
    assert runtime.started is False
    assert runtime.stopped is False
    start_dashboard.assert_not_awaited()
    stop_dashboard.assert_not_awaited()


def test_all_managed_profiles_disable_runtime_schema_mutation() -> None:
    for definition in MANAGED_PROFILE_DEFINITIONS.values():
        assert definition.runtime_defaults["database_auto_create_schema"] is False


def test_deploy_builds_before_down_and_migrates_before_app_up() -> None:
    source = Path("scripts/deploy.sh").read_text(encoding="utf-8")
    main_body = source[source.index("main() {") :]
    assert main_body.index("step_build") < main_body.index("step_down")
    assert main_body.index("step_schema_migrate") < main_body.index("step_app_up")
    migration_body = source[
        source.index("step_schema_migrate()") : source.index("step_app_up()")
    ]
    assert "scripts/apply_schema_migrations.py" in migration_body
    assert "run --rm --no-deps aats-gateway" in migration_body
    assert "run --rm --no-deps aats-rdp-daemon" not in migration_body


def test_runtime_rdp_callers_use_validator_not_mutating_alias() -> None:
    runtime_files = [
        Path("apps/api_gateway/main.py"),
        Path("scripts/rdp_task_daemon.py"),
        Path("scripts/rdp_historical_daemon.py"),
        Path("scripts/rdp_run_daily_ingest.py"),
        Path("scripts/rdp_run_replay.py"),
    ]
    for path in runtime_files:
        source = path.read_text(encoding="utf-8")
        assert "validate_rdp_schema" in source, path
        assert "run_migrations" not in source, path


def test_compose_common_env_owns_single_rdp_database_url() -> None:
    source = Path("deploy/wsl2-dev/docker-compose.aats.yml").read_text(
        encoding="utf-8"
    )
    common_env = source[source.index("x-aats-common-env:") : source.index("x-aats-common-resources:")]
    assert "RDP_DATABASE_URL:" in common_env
    assert "@postgres:5432/aats_research" in common_env
