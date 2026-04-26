from __future__ import annotations

from pathlib import Path

from aats.data_platform.migrations._batch_b import BATCH_B_STAGES


_ROOT = Path(__file__).resolve().parents[3]
_MIGRATIONS = _ROOT / "aats" / "data_platform" / "migrations"


def test_batch_b_13_collection_modeling_hygiene_is_registered_after_payload_sidecar() -> None:
    assert "batch_b_12_orderbook_payloads" in BATCH_B_STAGES
    assert "batch_b_13_rdp_collection_modeling_hygiene" in BATCH_B_STAGES
    assert BATCH_B_STAGES.index("batch_b_13_rdp_collection_modeling_hygiene") > BATCH_B_STAGES.index(
        "batch_b_12_orderbook_payloads"
    )


def test_batch_b_13_sql_covers_collection_scope_and_metadata_constraints() -> None:
    sql = (_MIGRATIONS / "batch_b_13_rdp_collection_modeling_hygiene.sql").read_text(encoding="utf-8")

    assert "ADD COLUMN IF NOT EXISTS source_scope" in sql
    assert "fixed_trading_scope" in sql
    assert "broad_market_context" in sql
    assert "chk_raw_liq_source_scope" in sql
    assert "fk_brz_orderbook_payloads_ingest_run" in sql
    assert "chk_dm_domain" in sql
    assert "microstructure" in sql
    assert "dormant" in sql


def test_batch_b_13_rollback_exists_and_reverses_new_liquidation_column() -> None:
    rollback = _MIGRATIONS / "batch_b_13_rdp_collection_modeling_hygiene_rollback.sql"
    assert rollback.exists()
    text = rollback.read_text(encoding="utf-8")
    assert "DROP COLUMN IF EXISTS source_scope" in text
    assert "DROP CONSTRAINT fk_brz_orderbook_payloads_ingest_run" in text
