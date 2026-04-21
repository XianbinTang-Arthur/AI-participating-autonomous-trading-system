"""Integration test: OKX liquidation-orders collector → real PostgreSQL.

Validates what the unit suite can't prove with a mock session:

  1. ``create_rdp_schema`` actually builds ``staging.raw_liquidations`` on a
     fresh DB, with the UNIQUE constraint + CHECK constraint + indexes as
     declared in ``RawLiquidationsModel``.
  2. ``write_liquidation_batch`` round-trips Decimal / JSONB / TIMESTAMPTZ
     columns correctly via CAST(:raw_payload AS JSONB).
  3. The natural-key UNIQUE + ``ON CONFLICT DO NOTHING`` silences OKX
     retransmissions (re-inserting identical rows is a no-op, not an error).
  4. Full-path ``LiquidationsCollector._handle_message`` → buffer → flush
     hits the DB under a patched ``get_session`` without any mocks on the
     parser or writer.

Run conditions:

  - ``docker`` daemon reachable
  - ``testcontainers`` + ``psycopg2`` installed (``pip install -e .[postgres-integration]``)
  - ``AATS_RUN_POSTGRES_INTEGRATION=1`` in env

WSL2 invocation::

    AATS_RUN_POSTGRES_INTEGRATION=1 pytest \
        tests/integration/data_platform/test_liquidations_ws_postgres.py -x -q
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import unittest
from datetime import datetime, timezone
from decimal import Decimal

try:
    from testcontainers.postgres import PostgresContainer  # type: ignore[import-not-found]

    _TESTCONTAINERS_AVAILABLE = True
except ImportError:  # pragma: no cover - skip when not installed
    PostgresContainer = None  # type: ignore[assignment,misc]
    _TESTCONTAINERS_AVAILABLE = False

try:
    import psycopg2  # type: ignore[import-not-found]  # noqa: F401

    _PSYCOPG2_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PSYCOPG2_AVAILABLE = False


_INTEGRATION_ENV_FLAG = "AATS_RUN_POSTGRES_INTEGRATION"
_SHOULD_RUN = (
    os.getenv(_INTEGRATION_ENV_FLAG) == "1"
    and _TESTCONTAINERS_AVAILABLE
    and _PSYCOPG2_AVAILABLE
)


_SAMPLE_PUSH = {
    "arg": {"channel": "liquidation-orders", "instType": "SWAP"},
    "data": [
        {
            "instType": "SWAP",
            "instFamily": "BTC-USDT",
            "instId": "BTC-USDT-SWAP",
            "details": [
                {
                    "side": "sell",
                    "bkPx": "95000",
                    "sz": "1.5",
                    "bkLoss": "0",
                    "ccy": "USDT",
                    "ts": "1745000000000",
                }
            ],
        }
    ],
}


def _ws_settings():
    from aats.bootstrap.settings import AATSSettings

    return AATSSettings.model_validate(
        {
            "okx_ws_read_timeout_seconds": 0.5,
            "okx_ws_market_data_timeout_seconds": 1.5,
            "okx_market_reconnect_delay_seconds": 0.1,
            "okx_market_reconnect_max_delay_seconds": 0.2,
            "okx_ws_open_timeout_seconds": 5.0,
            "okx_private_ws_idle_ping_interval_seconds": 0.5,
        }
    )


@unittest.skipUnless(_SHOULD_RUN, f"need docker + testcontainers + {_INTEGRATION_ENV_FLAG}=1")
class LiquidationsPostgresIntegrationTests(unittest.TestCase):
    container: "PostgresContainer | None" = None
    engine = None

    @classmethod
    def setUpClass(cls) -> None:
        # postgres:16-alpine matches the WSL2 dev stack (aats-postgres).
        cls.container = PostgresContainer("postgres:16-alpine")
        cls.container.start()

        from sqlalchemy import create_engine

        from aats.data_platform.rdp_models import create_rdp_schema

        url = cls.container.get_connection_url()
        cls.engine = create_engine(url, future=True)
        create_rdp_schema(cls.engine)

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.engine is not None:
            cls.engine.dispose()
        if cls.container is not None:
            cls.container.stop()

    def setUp(self) -> None:
        from sqlalchemy import text

        with self.engine.begin() as conn:  # type: ignore[union-attr]
            conn.execute(text("TRUNCATE TABLE staging.raw_liquidations RESTART IDENTITY"))

    def test_schema_has_table_and_unique_constraint(self) -> None:
        from sqlalchemy import text

        with self.engine.begin() as conn:  # type: ignore[union-attr]
            tbl = conn.execute(
                text(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema='staging' AND table_name='raw_liquidations'"
                )
            ).fetchone()
            self.assertIsNotNone(tbl)

            uq = conn.execute(
                text(
                    "SELECT 1 FROM pg_constraint "
                    "WHERE conname = 'uq_raw_liquidations_natural_key'"
                )
            ).fetchone()
            self.assertIsNotNone(uq)

            chk = conn.execute(
                text(
                    "SELECT 1 FROM pg_constraint WHERE conname = 'chk_raw_liq_side'"
                )
            ).fetchone()
            self.assertIsNotNone(chk)

    def test_write_batch_roundtrip(self) -> None:
        from sqlalchemy import text
        from sqlalchemy.orm import Session

        from aats.data_platform.collectors.liquidations_ws_collector import (
            LiquidationRow,
            write_liquidation_batch,
        )

        row = LiquidationRow(
            ts=datetime.fromtimestamp(1745000000, tz=timezone.utc),
            inst_id="BTC-USDT-SWAP",
            inst_type="SWAP",
            inst_family="BTC-USDT",
            side="sell",
            bk_px=Decimal("95000"),
            sz=Decimal("1.5"),
            bk_loss=Decimal("0"),
            ccy="USDT",
            raw_payload={"source": "integration_test"},
        )
        with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
            written = write_liquidation_batch(session, [row])
            self.assertEqual(written, 1)

        with self.engine.begin() as conn:  # type: ignore[union-attr]
            result = conn.execute(
                text("SELECT inst_id, side, bk_px, sz, raw_payload FROM staging.raw_liquidations")
            ).mappings().all()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["inst_id"], "BTC-USDT-SWAP")
        self.assertEqual(result[0]["side"], "sell")
        self.assertEqual(Decimal(result[0]["bk_px"]), Decimal("95000"))
        self.assertEqual(Decimal(result[0]["sz"]), Decimal("1.5"))
        self.assertEqual(result[0]["raw_payload"], {"source": "integration_test"})

    def test_duplicate_insert_is_noop(self) -> None:
        """OKX retransmits the same event after reconnect; unique + ON CONFLICT suppresses it."""
        from sqlalchemy import text
        from sqlalchemy.orm import Session

        from aats.data_platform.collectors.liquidations_ws_collector import (
            LiquidationRow,
            write_liquidation_batch,
        )

        row = LiquidationRow(
            ts=datetime.fromtimestamp(1745000000, tz=timezone.utc),
            inst_id="BTC-USDT-SWAP",
            inst_type="SWAP",
            inst_family="BTC-USDT",
            side="buy",
            bk_px=Decimal("3000"),
            sz=Decimal("0.5"),
            bk_loss=None,
            ccy=None,
            raw_payload={"attempt": 1},
        )
        # Identical natural key, different raw_payload — the second insert
        # should be silently dropped (ON CONFLICT DO NOTHING).
        duplicate = LiquidationRow(
            ts=row.ts,
            inst_id=row.inst_id,
            inst_type=row.inst_type,
            inst_family=row.inst_family,
            side=row.side,
            bk_px=row.bk_px,
            sz=row.sz,
            bk_loss=row.bk_loss,
            ccy=row.ccy,
            raw_payload={"attempt": 2},
        )
        with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
            write_liquidation_batch(session, [row])
        with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
            write_liquidation_batch(session, [duplicate])

        with self.engine.begin() as conn:  # type: ignore[union-attr]
            count = conn.execute(text("SELECT COUNT(*) FROM staging.raw_liquidations")).scalar()
            kept_payload = conn.execute(
                text("SELECT raw_payload FROM staging.raw_liquidations")
            ).scalar()
        self.assertEqual(count, 1)
        # First attempt's payload is retained — DO NOTHING preserves the
        # original row instead of updating it.
        self.assertEqual(kept_payload, {"attempt": 1})

    def test_end_to_end_collector_flush(self) -> None:
        """Parse a real OKX-shaped message, buffer it, flush it, confirm the row."""
        from sqlalchemy import text
        from sqlalchemy.orm import sessionmaker
        from unittest.mock import patch

        factory = sessionmaker(bind=self.engine, expire_on_commit=False)  # type: ignore[arg-type]

        @contextlib.contextmanager
        def _engine_session():
            session = factory()
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

        from aats.data_platform.collectors.liquidations_ws_collector import (
            LiquidationsCollector,
        )

        async def _drive() -> None:
            with patch(
                "aats.data_platform.collectors.liquidations_ws_collector.get_session",
                _engine_session,
            ):
                collector = LiquidationsCollector(
                    settings=_ws_settings(),
                    flush_max_rows=1,
                    flush_max_seconds=60.0,
                )
                await collector._handle_message(_SAMPLE_PUSH)

        asyncio.run(_drive())

        with self.engine.begin() as conn:  # type: ignore[union-attr]
            rows = conn.execute(
                text(
                    "SELECT inst_id, inst_family, side, bk_px, sz, bk_loss, ccy, raw_payload "
                    "FROM staging.raw_liquidations"
                )
            ).mappings().all()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["inst_id"], "BTC-USDT-SWAP")
        self.assertEqual(r["inst_family"], "BTC-USDT")
        self.assertEqual(r["side"], "sell")
        self.assertEqual(Decimal(r["bk_px"]), Decimal("95000"))
        self.assertEqual(Decimal(r["sz"]), Decimal("1.5"))
        self.assertEqual(Decimal(r["bk_loss"]), Decimal("0"))
        self.assertEqual(r["ccy"], "USDT")
        # raw_payload persists the OKX detail dict unchanged for future
        # silver-layer schema evolution.
        self.assertEqual(r["raw_payload"]["side"], "sell")
        self.assertEqual(r["raw_payload"]["bkPx"], "95000")
        self.assertEqual(r["raw_payload"]["ts"], "1745000000000")


if __name__ == "__main__":
    unittest.main()
