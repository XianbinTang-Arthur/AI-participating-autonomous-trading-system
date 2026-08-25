"""PostgreSQL integration coverage for Batch B stage 12 orderbook payloads.

Runs only when AATS_RUN_POSTGRES_INTEGRATION=1 and testcontainers is available.
"""

from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import create_engine, text

try:
    from testcontainers.postgres import PostgresContainer  # type: ignore[import-not-found]

    _TESTCONTAINERS_AVAILABLE = True
except ImportError:  # pragma: no cover
    PostgresContainer = None  # type: ignore[assignment,misc]
    _TESTCONTAINERS_AVAILABLE = False

try:
    import psycopg2  # type: ignore[import-not-found]  # noqa: F401

    _PSYCOPG2_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PSYCOPG2_AVAILABLE = False


_SHOULD_RUN = (
    os.getenv("AATS_RUN_POSTGRES_INTEGRATION") == "1"
    and _TESTCONTAINERS_AVAILABLE
    and _PSYCOPG2_AVAILABLE
)

_TS = datetime(2026, 4, 26, 2, 10, tzinfo=timezone.utc)
_SHA_A = "sha256:" + "a" * 64
_SHA_B = "sha256:" + "b" * 64
_SHA_C = "sha256:" + "c" * 64


@unittest.skipUnless(
    _SHOULD_RUN,
    "need docker + testcontainers + AATS_RUN_POSTGRES_INTEGRATION=1",
)
class OrderbookPayloadMigrationPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        assert PostgresContainer is not None
        cls.container = PostgresContainer("postgres:16-alpine")
        cls.container.start()
        cls.engine = create_engine(
            cls.container.get_connection_url(driver="psycopg2"),
            future=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()
        cls.container.stop()

    def test_stage_12_forward_constraints_and_rollback(self) -> None:
        from aats.data_platform.migrations._batch_b import (
            _load_sql,
            _without_outer_transaction,
        )

        # This is a standalone SQL contract test.  The production runner
        # intentionally rejects stage 12 without its complete predecessor
        # ledger, so execute the normalized SQL directly in this isolated DB.
        forward_sql = _without_outer_transaction(
            _load_sql("batch_b_12_orderbook_payloads"),
            stage="batch_b_12_orderbook_payloads",
        )
        with self.engine.begin() as conn:
            conn.execute(text(forward_sql))

        with self.engine.begin() as conn:
            table_exists = conn.execute(text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema='bronze' "
                "AND table_name='market_orderbook_payloads'"
            )).scalar()
            self.assertEqual(table_exists, 1)

            constraints = {
                row[0]
                for row in conn.execute(text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid = 'bronze.market_orderbook_payloads'::regclass"
                ))
            }
            self.assertIn("pk_brz_orderbook_payloads", constraints)
            self.assertIn("chk_brz_orderbook_payload_diff_required", constraints)

            indexes = {
                row[0]
                for row in conn.execute(text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE schemaname='bronze' "
                    "AND tablename='market_orderbook_payloads'"
                ))
            }
            self.assertIn("idx_brz_orderbook_payloads_snapshot", indexes)
            self.assertIn("ux_brz_orderbook_payloads_sequence_scope", indexes)

            ingest_run_id = str(uuid4())
            conn.execute(
                text(
                    "INSERT INTO bronze.market_orderbook_payloads ("
                    "snapshot_table, symbol, ts, source_ts, collector_sequence, "
                    "row_checksum, capture_status, payload_hash, "
                    "payload_schema_version, payload_kind, raw_payload, "
                    "ingest_run_id, channel"
                    ") VALUES ("
                    ":snapshot_table, :symbol, :ts, :source_ts, :sequence, "
                    ":row_checksum, 'diff_payload_persisted', :payload_hash, "
                    "'orderbook_diff_payload_v1', 'okx_books5_snapshot', "
                    "CAST(:raw_payload AS jsonb), :ingest_run_id, 'books5'"
                    ")"
                ),
                {
                    "snapshot_table": "bronze.market_orderbook_books5",
                    "symbol": "BTC-USDT-SWAP",
                    "ts": _TS,
                    "source_ts": _TS,
                    "sequence": 11,
                    "row_checksum": _SHA_A,
                    "payload_hash": _SHA_B,
                    "raw_payload": json.dumps({
                        "arg": {"channel": "books5", "instId": "BTC-USDT-SWAP"},
                        "data": [{"bids": [["64000", "1"]], "asks": [["64001", "2"]]}],
                    }),
                    "ingest_run_id": ingest_run_id,
                },
            )

            count = conn.execute(text(
                "SELECT COUNT(*) FROM bronze.market_orderbook_payloads"
            )).scalar_one()
            self.assertEqual(count, 1)

        with self.assertRaises(Exception):
            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO bronze.market_orderbook_payloads ("
                        "snapshot_table, symbol, ts, source_ts, collector_sequence, "
                        "row_checksum, capture_status, ingest_run_id"
                        ") VALUES ("
                        "'bronze.market_orderbook_books5', 'BTC-USDT-SWAP', "
                        ":ts, :ts, 0, :row_checksum, "
                        "'snapshot_only_diff_payload_missing', :ingest_run_id"
                        ")"
                    ),
                    {
                        "ts": _TS,
                        "row_checksum": _SHA_C,
                        "ingest_run_id": str(uuid4()),
                    },
                )

        rollback_sql = _without_outer_transaction(
            _load_sql("batch_b_12_orderbook_payloads", rollback=True),
            stage="batch_b_12_orderbook_payloads:rollback",
        )
        with self.engine.begin() as conn:
            conn.execute(text(rollback_sql))

        with self.engine.begin() as conn:
            table_exists = conn.execute(text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema='bronze' "
                "AND table_name='market_orderbook_payloads'"
            )).scalar()
            self.assertIsNone(table_exists)


if __name__ == "__main__":
    unittest.main()
