from __future__ import annotations

from pathlib import Path

from aats.data_platform.migrations._batch_b import BATCH_B_STAGES
from aats.data_platform.rdp_models import RdpBase


ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "aats/data_platform/migrations/batch_b_18_data_governance.sql"
ROLLBACK = ROOT / "aats/data_platform/migrations/batch_b_18_data_governance_rollback.sql"


def test_data_governance_migration_is_last_and_transaction_wrapped() -> None:
    assert BATCH_B_STAGES[-1] == "batch_b_18_data_governance"
    migration = MIGRATION.read_text(encoding="utf-8")
    rollback = ROLLBACK.read_text(encoding="utf-8")

    assert migration.count("BEGIN;") == migration.count("COMMIT;") == 1
    assert rollback.count("BEGIN;") == rollback.count("COMMIT;") == 1


def test_data_governance_schema_sql_and_orm_cover_same_new_tables() -> None:
    migration = MIGRATION.read_text(encoding="utf-8")
    expected = {
        "meta.data_source_registry",
        "meta.archive_partitions",
        "meta.data_gap_records",
        "meta.dataset_bundles",
        "meta.data_rebuild_runs",
        "meta.collector_continuity_events",
        "staging.official_trade_history",
        "staging.official_l2_history",
        "bronze.historical_orderbook_bbo_1hz",
        "bronze.historical_orderbook_books5_2hz",
        "bronze.market_mark_price_candles_15m",
        "bronze.market_mark_price_candles_1h",
        "silver.historical_orderbook_metrics_15m",
        "silver.historical_trade_flow_15m",
    }

    assert expected <= set(RdpBase.metadata.tables)
    for table in expected:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in migration

    raw_liquidations = RdpBase.metadata.tables["staging.raw_liquidations"]
    assert "raw_payload_hash" in raw_liquidations.c
    assert "ADD COLUMN IF NOT EXISTS raw_payload_hash" in migration


def test_data_governance_constraints_fail_closed() -> None:
    migration = MIGRATION.read_text(encoding="utf-8")

    assert "coverage_end > coverage_start" in migration
    assert "raw_partition_sha256 ~ '^[0-9a-f]{64}$'" in migration
    assert "fingerprint ~ '^[0-9a-f]{64}$'" in migration
    assert migration.count(
        "ingest_run_id UUID NOT NULL REFERENCES meta.ingest_runs(ingest_run_id)"
    ) == 5
    assert "source_state_ts <= ts" in migration
    assert "state IN ('DISCOVERED','ARCHIVING','VERIFIED','DELETE_ELIGIBLE','DELETED','FAILED')" in migration
    assert "eligibility_mode IN ('historical_research','live_capture')" in migration
    assert "event_type IN ('CONNECT','DISCONNECT','RECONNECT','MESSAGE','FLUSH','DROP','SHUTDOWN','CLOCK_SKEW')" in migration

    continuity = RdpBase.metadata.tables["meta.collector_continuity_events"]
    assert "terminal_at" not in continuity.c
    assert continuity.c.ingest_run_id.foreign_keys


def test_rollback_drops_consumers_before_source_registry() -> None:
    rollback = ROLLBACK.read_text(encoding="utf-8")

    assert rollback.index("DROP TABLE IF EXISTS staging.official_trade_history") < rollback.index(
        "DROP TABLE IF EXISTS meta.data_source_registry"
    )
    assert rollback.index("DROP TABLE IF EXISTS meta.archive_partitions") < rollback.index(
        "DROP TABLE IF EXISTS meta.data_source_registry"
    )
