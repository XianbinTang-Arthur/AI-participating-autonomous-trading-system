from __future__ import annotations

import datetime as _dt
import unittest
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import BigInteger, create_engine, event, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(_type, _compiler, **_kw):  # type: ignore[unused-argument]
    return "TEXT"


@compiles(UUID, "sqlite")
def _compile_uuid_sqlite(_type, _compiler, **_kw):  # type: ignore[unused-argument]
    return "TEXT"


@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(_type, _compiler, **_kw):  # type: ignore[unused-argument]
    return "INTEGER"


from aats.data_platform.rdp_models import (  # noqa: E402
    BronzeMarketOrderbookPayloadModel,
    RdpBase,
)


_TS = datetime(2026, 4, 26, 2, 0, tzinfo=timezone.utc)
_SHA_A = "sha256:" + "a" * 64
_SHA_B = "sha256:" + "b" * 64


def _make_engine():
    engine = create_engine("sqlite:///:memory:", future=True)

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _):  # type: ignore[unused-argument]
        dbapi_conn.create_function(
            "now",
            0,
            lambda: _dt.datetime.now(_dt.timezone.utc).isoformat(
                sep=" ",
                timespec="microseconds",
            ),
        )
        cur = dbapi_conn.cursor()
        cur.execute("ATTACH DATABASE ':memory:' AS bronze")
        cur.close()

    RdpBase.metadata.create_all(
        engine,
        tables=[BronzeMarketOrderbookPayloadModel.__table__],
    )
    return engine


def _payload_row(**overrides):
    row = {
        "snapshot_table": "bronze.market_orderbook_books5",
        "symbol": "BTC-USDT-SWAP",
        "ts": _TS,
        "source_ts": _TS,
        "collector_sequence": 7,
        "row_checksum": _SHA_A,
        "capture_status": "snapshot_only_diff_payload_missing",
        "ingest_run_id": str(uuid4()),
        "channel": "books5",
    }
    row.update(overrides)
    return row


class TestOrderbookDiffPayloadSchema(unittest.TestCase):
    def test_snapshot_only_payload_sidecar_roundtrip(self) -> None:
        engine = _make_engine()
        with Session(engine) as session:
            session.add(BronzeMarketOrderbookPayloadModel(**_payload_row()))
            session.commit()

            row = session.query(BronzeMarketOrderbookPayloadModel).one()
            self.assertEqual(row.storage_table, "bronze.market_orderbook_payloads")
            self.assertEqual(row.snapshot_table, "bronze.market_orderbook_books5")
            self.assertEqual(row.collector_sequence_scope, "per_ingest_run_symbol_channel")
            self.assertEqual(row.checksum_version, "orderbook_row_v1")
            self.assertIsNone(row.raw_payload)

    def test_diff_persisted_requires_payload_fields(self) -> None:
        engine = _make_engine()
        with Session(engine) as session:
            session.add(BronzeMarketOrderbookPayloadModel(**_payload_row(
                capture_status="diff_payload_persisted",
            )))
            with self.assertRaises(IntegrityError):
                session.commit()
            session.rollback()

    def test_diff_persisted_payload_roundtrip(self) -> None:
        engine = _make_engine()
        with Session(engine) as session:
            session.add(BronzeMarketOrderbookPayloadModel(**_payload_row(
                capture_status="diff_payload_persisted",
                payload_hash=_SHA_B,
                payload_schema_version="orderbook_diff_payload_v1",
                payload_kind="okx_books5_snapshot",
                raw_payload={
                    "arg": {"channel": "books5", "instId": "BTC-USDT-SWAP"},
                    "data": [{"bids": [["64000", "1"]], "asks": [["64001", "2"]]}],
                },
            )))
            session.commit()

            row = session.query(BronzeMarketOrderbookPayloadModel).one()
            self.assertEqual(row.capture_status, "diff_payload_persisted")
            self.assertEqual(row.payload_schema_version, "orderbook_diff_payload_v1")
            self.assertIsNotNone(row.raw_payload)

    def test_invalid_snapshot_table_sequence_and_checksum_rejected(self) -> None:
        engine = _make_engine()
        with Session(engine) as session:
            session.add(BronzeMarketOrderbookPayloadModel(**_payload_row(
                snapshot_table="execution.orderbook_payloads",
                collector_sequence=0,
                row_checksum="bad",
            )))
            with self.assertRaises(IntegrityError):
                session.commit()
            session.rollback()


class TestOrderbookDiffPayloadMigration(unittest.TestCase):
    def test_batch_b_12_registered_after_numeric_widen(self) -> None:
        from aats.data_platform.migrations._batch_b import BATCH_B_STAGES

        self.assertIn("batch_b_12_orderbook_payloads", BATCH_B_STAGES)
        self.assertLess(
            BATCH_B_STAGES.index("batch_b_11_silver_numeric_widen"),
            BATCH_B_STAGES.index("batch_b_12_orderbook_payloads"),
        )

    def test_batch_b_rollback_loader_supports_legacy_and_new_names(self) -> None:
        from aats.data_platform.migrations._batch_b import _load_sql

        legacy = _load_sql("batch_b_05_microstructure", rollback=True)
        current = _load_sql("batch_b_12_orderbook_payloads", rollback=True)

        self.assertIn("DROP TABLE IF EXISTS bronze.market_orderbook_bbo", legacy)
        self.assertIn("DROP TABLE IF EXISTS bronze.market_orderbook_payloads", current)

    def test_forward_and_rollback_sql_files_define_only_bronze_sidecar(self) -> None:
        from aats.data_platform.migrations import _batch_b

        migrate_dir = Path(_batch_b.__file__).parent
        forward = migrate_dir / "batch_b_12_orderbook_payloads.sql"
        rollback = migrate_dir / "batch_b_12_orderbook_payloads_rollback.sql"

        self.assertTrue(forward.is_file(), "missing batch_b_12 forward SQL")
        self.assertTrue(rollback.is_file(), "missing batch_b_12 rollback SQL")

        forward_text = forward.read_text(encoding="utf-8")
        rollback_text = rollback.read_text(encoding="utf-8")

        self.assertIn("CREATE TABLE IF NOT EXISTS bronze.market_orderbook_payloads", forward_text)
        self.assertIn("CONSTRAINT pk_brz_orderbook_payloads", forward_text)
        self.assertIn("PRIMARY KEY (snapshot_table, symbol, ts, row_checksum)", forward_text)
        self.assertIn("chk_brz_orderbook_payload_snapshot_table", forward_text)
        self.assertIn("chk_brz_orderbook_payload_diff_required", forward_text)
        self.assertIn("ux_brz_orderbook_payloads_sequence_scope", forward_text)
        self.assertIn("DROP TABLE IF EXISTS bronze.market_orderbook_payloads", rollback_text)
        self.assertNotIn("CREATE TABLE IF NOT EXISTS execution.", forward_text)
        self.assertNotIn("ON execution.", forward_text)

    def test_rollback_drops_only_sidecar_table(self) -> None:
        rollback_path = (
            Path(__file__).resolve().parents[3]
            / "aats"
            / "data_platform"
            / "migrations"
            / "batch_b_12_orderbook_payloads_rollback.sql"
        )
        sql_text = rollback_path.read_text(encoding="utf-8")

        engine = _make_engine()
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT name FROM bronze.sqlite_master "
                "WHERE type='table' AND name='market_orderbook_payloads'"
            )).fetchone()
            self.assertIsNotNone(row)

        statements: list[str] = []
        for line in sql_text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("--"):
                continue
            for raw in stripped.split(";"):
                statement = raw.strip()
                if statement and statement.upper() not in {"BEGIN", "COMMIT"}:
                    statements.append(statement)

        with engine.begin() as conn:
            for statement in statements:
                conn.execute(text(statement))

        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT name FROM bronze.sqlite_master "
                "WHERE type='table' AND name='market_orderbook_payloads'"
            )).fetchone()
            self.assertIsNone(row)


if __name__ == "__main__":
    unittest.main()
