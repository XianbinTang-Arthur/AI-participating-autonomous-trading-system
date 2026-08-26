from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine

from aats.data_platform.data_governance.coverage import (
    _column_is_required,
    _coverage_status,
    _database_enforces_unique_key,
    _gap_count,
    _natural_key_columns,
    _symbol_windows,
    _zero_status,
    audit_coverage,
    build_recovery_matrix,
    git_commit,
)
from aats.data_platform.rdp_models import RdpBase


def test_git_commit_prefers_valid_deployed_revision(monkeypatch) -> None:
    deployed = "a" * 40
    monkeypatch.setenv("AATS_DEPLOYED_GIT_COMMIT", deployed)
    monkeypatch.setattr(
        "aats.data_platform.data_governance.coverage.subprocess.check_output",
        lambda *args, **kwargs: "b" * 40,
    )

    assert git_commit("/app") == deployed


def test_git_commit_ignores_invalid_deployed_revision_and_fails_closed(
    monkeypatch,
) -> None:
    monkeypatch.setenv("AATS_DEPLOYED_GIT_COMMIT", "short-revision")
    monkeypatch.setattr(
        "aats.data_platform.data_governance.coverage.subprocess.check_output",
        lambda *args, **kwargs: "not-a-commit",
    )

    assert git_commit("/app") == "unknown"


def test_empty_database_audit_is_read_only_complete_and_deterministic() -> None:
    engine = create_engine("sqlite:///:memory:")
    end = datetime(2026, 8, 26, tzinfo=timezone.utc)

    first = audit_coverage(engine, window_end=end, window_days=30)
    second = audit_coverage(engine, window_end=end, window_days=30)

    assert first["read_only"] is True
    assert len(first["tables"]) == len(RdpBase.metadata.tables) == 98
    assert first["result_fingerprint_sha256"] == second["result_fingerprint_sha256"]
    assert all(table["status"] == "missing" for table in first["tables"])
    assert first["summary"]["missing"] == 98


def test_trade_duplicate_key_includes_trade_id() -> None:
    table = RdpBase.metadata.tables["bronze.market_trades"]

    assert _natural_key_columns(table, set(table.columns.keys()), "ts") == (
        "symbol",
        "ts",
        "trade_id",
    )


def test_constrained_natural_key_does_not_require_duplicate_scan() -> None:
    table = RdpBase.metadata.tables["bronze.market_orderbook_payloads"]
    key = _natural_key_columns(table, set(table.columns.keys()), "ts")

    assert key == ("snapshot_table", "symbol", "ts", "row_checksum")
    assert _database_enforces_unique_key(table, key) is True
    assert _database_enforces_unique_key(table, ("symbol", "ts")) is False


def test_metadata_uses_identity_key_and_nullable_symbol_is_not_a_quality_error() -> None:
    quality_reports = RdpBase.metadata.tables["meta.quality_reports"]

    assert _natural_key_columns(
        quality_reports,
        set(quality_reports.columns.keys()),
        "window_start_ts",
    ) == ("quality_report_id",)
    assert _database_enforces_unique_key(
        quality_reports,
        ("quality_report_id",),
    ) is True
    assert _column_is_required(
        {"symbol": {"name": "symbol", "nullable": True}},
        "symbol",
    ) is False
    assert _column_is_required(
        {"symbol": {"name": "symbol", "nullable": False}},
        "symbol",
    ) is True


def test_gap_count_partitions_each_symbol_independently() -> None:
    class _Scalar:
        def scalar_one(self) -> int:
            return 1

    class _Connection:
        sql = ""

        def execute(self, statement, _parameters):
            self.sql = str(statement)
            return _Scalar()

    connection = _Connection()
    gaps = _gap_count(
        connection,
        '"ticks"',
        '"ts"',
        "ticks_1m",
        {"symbol", "ts"},
        datetime(2026, 8, 26, tzinfo=timezone.utc),
        datetime(2026, 8, 27, tzinfo=timezone.utc),
    )

    assert gaps == 1
    assert 'PARTITION BY "symbol"' in connection.sql
    assert 'SELECT DISTINCT "symbol", "ts"' in connection.sql


def test_recovery_matrix_never_claims_prospective_data_is_backfillable() -> None:
    matrix = build_recovery_matrix(
        [
            {"table": "staging.raw_liquidations", "status": "collector_unknown"},
            {"table": "silver.market_trade_flow_15m", "status": "missing"},
            {"table": "research.experiments", "status": "missing"},
            {"table": "governance.parameter_releases", "status": "missing"},
            {
                "table": "silver.market_liquidation_metrics_15m",
                "status": "zero_event_with_healthy_collector",
            },
        ]
    )

    by_dataset = {item["dataset"]: item for item in matrix}
    assert by_dataset["staging.raw_liquidations"]["classification"] == "prospective_only"
    assert by_dataset["silver.market_trade_flow_15m"]["classification"] == (
        "deterministic_rebuild"
    )
    assert by_dataset["research.experiments"]["classification"] == "cannot_recover"
    assert by_dataset["governance.parameter_releases"]["classification"] == (
        "prospective_only"
    )
    assert "silver.market_liquidation_metrics_15m" not in by_dataset


def test_observed_rows_with_quality_deficits_are_not_hidden_as_healthy() -> None:
    status = _coverage_status(
        count=100,
        duplicates=0,
        missing_intervals=1,
        null_symbol_rows=0,
        unconfirmed_rows=0,
        quality_flagged_rows=0,
        zero_status=None,
    )

    assert status == "observed_with_quality_issues"
    matrix = build_recovery_matrix(
        [{"table": "silver.market_trade_flow_15m", "status": status}]
    )
    assert matrix[0]["classification"] == "deterministic_rebuild"


def test_symbol_windows_are_bounded_and_use_utc_days() -> None:
    class _Rows:
        def mappings(self):
            return self

        def all(self):
            return [
                {
                    "symbol": "BTC-USDT-SWAP",
                    "row_count": 3,
                    "earliest_ts": datetime(2026, 8, 26, tzinfo=timezone.utc),
                    "latest_ts": datetime(2026, 8, 26, 1, tzinfo=timezone.utc),
                    "utc_days_observed": 1,
                },
                {
                    "symbol": "ETH-USDT-SWAP",
                    "row_count": 2,
                    "earliest_ts": datetime(2026, 8, 26, tzinfo=timezone.utc),
                    "latest_ts": datetime(2026, 8, 26, 1, tzinfo=timezone.utc),
                    "utc_days_observed": 1,
                },
            ]

    class _Dialect:
        name = "postgresql"

    class _Connection:
        dialect = _Dialect()
        sql = ""
        params = None

        def execute(self, statement, params):
            self.sql = str(statement)
            self.params = params
            return _Rows()

    connection = _Connection()
    rows, truncated = _symbol_windows(
        connection,
        '"bronze"."market_trades"',
        '"ts"',
        {"symbol", "ts"},
        datetime(2026, 8, 26, tzinfo=timezone.utc),
        datetime(2026, 8, 27, tzinfo=timezone.utc),
        limit=1,
    )

    assert len(rows) == 1
    assert rows[0]["symbol"] == "BTC-USDT-SWAP"
    assert truncated is True
    assert "AT TIME ZONE 'UTC'" in connection.sql
    assert connection.params["limit"] == 2


def test_liquidation_zero_requires_continuity_at_both_window_boundaries() -> None:
    start = datetime(2026, 8, 26, tzinfo=timezone.utc)
    end = datetime(2026, 8, 26, 1, tzinfo=timezone.utc)

    class _Exists:
        def scalar_one_or_none(self):
            return "meta.collector_continuity_events"

    class _Rows:
        def __init__(self, rows):
            self._rows = rows

        def mappings(self):
            return self

        def all(self):
            return self._rows

    class _Connection:
        def __init__(self, rows):
            self._rows = rows
            self._calls = 0

        def execute(self, _statement, _params=None):
            self._calls += 1
            return _Exists() if self._calls == 1 else _Rows(self._rows)

    base = {
        "event_type": "MESSAGE",
        "event_count": 100,
        "earliest_event_ts": start + timedelta(seconds=15),
        "latest_event_ts": end - timedelta(seconds=15),
        "generations": 1,
        "ingest_runs": 1,
    }
    assert _zero_status(
        _Connection([base]), "staging.raw_liquidations", start, end
    ) == "zero_event_with_healthy_collector"
    assert _zero_status(
        _Connection([{**base, "earliest_event_ts": start + timedelta(minutes=5)}]),
        "staging.raw_liquidations",
        start,
        end,
    ) == "collector_unknown"
