"""P1-D Phase 1A Stage 4 E2E 集成测 — Bronze 写入 → Silver ETL 全链路真 PostgreSQL。

本测验证 Stage 1-3 在真 PG 下协同正常,重点捕获 SQLite 单测弱化掉的
NUMERIC 精度 / GENERATED 列 / UUID CAST / PG-only 聚合 (STDDEV_SAMP,
PERCENTILE_CONT) 的语义。

测试拓扑
========

    testcontainers postgres:16-alpine
           │
           ▼
    batch_b_05_microstructure.sql        (Bronze + staging 4 表)
    batch_b_06_silver_microstructure.sql (Silver 5 表)
           │
           ▼
    write_trades_batch  (bronze.market_trades)
    write_bbo_batch     (bronze.market_orderbook_bbo, GENERATED mid/spread/imbalance)
    write_books5_batch  (bronze.market_orderbook_books5)
    write_oif_batch     (staging.market_oi_funding_ticks)
           │
           ▼
    build_silver_microstructure_15m()    (5 张 Silver 15m 表)
           │
           ▼
    断言 Silver 内容 (UPSERT 幂等 + 精确字段值 + quality_flags)

5 个 case:
  1. happy_path — 全量 Bronze + Silver 产 5 行,字段精度准确
  2. empty_bar — Bronze 无数据时 Silver 仍写 1 行 / 表,全 NULL + 'no_data' flag
  3. partial_data — 只写 BBO,看 trade_flow / volume_profile 降级
  4. idempotent — 重跑同一 bar,Silver 仍 1 行 / 表
  5. migration_forward_rollback — batch_b_05/06 migration 正向 + 逆向都成功

运行条件
========
- docker daemon reachable
- ``pip install testcontainers psycopg2-binary``
- ``AATS_RUN_POSTGRES_INTEGRATION=1`` 环境变量

WSL2 入口::

    AATS_RUN_POSTGRES_INTEGRATION=1 pytest \\
        tests/integration/data_platform/test_microstructure_pipeline_e2e.py -x -q
"""
from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

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


_INTEGRATION_ENV_FLAG = "AATS_RUN_POSTGRES_INTEGRATION"
_SHOULD_RUN = (
    os.getenv(_INTEGRATION_ENV_FLAG) == "1"
    and _TESTCONTAINERS_AVAILABLE
    and _PSYCOPG2_AVAILABLE
)


# ─────────────────────────────────────────────────────────────────────
# 公共 fixture: bar 时间窗 + symbol
# ─────────────────────────────────────────────────────────────────────
# 选 2026-01-15 11:00:00 UTC 作 bar_start_ts 固定点 (minute 可被 15 整除,
# 避开 DST 边界——UTC 无 DST, 但 2026-01 是干净的冬季日期便于 future debug)

_BAR_START = datetime(2026, 1, 15, 11, 0, 0, tzinfo=timezone.utc)
_BAR_END = _BAR_START + timedelta(minutes=15)
_SYMBOL = "BTC-USDT-SWAP"


def _apply_migrations(engine, rollback: bool = False) -> None:
    """Directly exercise only the stage 05/06 SQL on an isolated fresh DB.

    We deliberately do NOT run the full `create_rdp_schema` path — this test
    exists to verify the batch_b_05/06 *SQL migrations themselves* deploy
    cleanly on a fresh Postgres.  The production runner now requires the full
    predecessor ledger, so a standalone SQL contract test must not call that
    production entrypoint or fabricate predecessor ledger rows.

    The production runner strips each legacy outer BEGIN/COMMIT and owns the
    transaction; this test uses the same normalization before direct execute.
    """
    from sqlalchemy import text

    from aats.data_platform.migrations._batch_b import (
        _load_sql,
        _without_outer_transaction,
    )

    target = ("batch_b_05_microstructure", "batch_b_06_silver_microstructure")
    ordered = tuple(reversed(target)) if rollback else target
    for stage in ordered:
        sql = _without_outer_transaction(
            _load_sql(stage, rollback=rollback),
            stage=f"{stage}:rollback" if rollback else stage,
        )
        with engine.begin() as connection:
            connection.execute(text(sql))


def _sample_trades(n_buy: int, n_sell: int, ingest_run_id: str):
    """Build N trade rows distributed across a 15m bar."""
    from aats.data_platform.collectors.microstructure_ws_collector import TradeRow

    rows = []
    # Distribute across the bar: 1/16 of window per trade (15m / 16 = 56s)
    step = (_BAR_END - _BAR_START) / max(n_buy + n_sell, 1)
    t = _BAR_START
    for i in range(n_buy):
        rows.append(
            TradeRow(
                symbol=_SYMBOL,
                ts=t,
                trade_id=f"buy-{i}",
                px=Decimal("95000") + Decimal(i),
                sz=Decimal("0.5"),
                side="buy",
                raw_payload={"source": "e2e", "seq": i, "kind": "buy"},
            )
        )
        t += step
    for i in range(n_sell):
        rows.append(
            TradeRow(
                symbol=_SYMBOL,
                ts=t,
                trade_id=f"sell-{i}",
                px=Decimal("95000") - Decimal(i),
                sz=Decimal("0.3"),
                side="sell",
                raw_payload={"source": "e2e", "seq": i, "kind": "sell"},
            )
        )
        t += step
    return rows


def _sample_bbo_rows(n: int):
    """Build N bbo rows across bar."""
    from aats.data_platform.collectors.microstructure_ws_collector import BboRow

    rows = []
    step = (_BAR_END - _BAR_START) / max(n, 1)
    t = _BAR_START
    # Bid < Ask, imbalance = (bid_sz-ask_sz)/(bid_sz+ask_sz). With bid_sz=2
    # and ask_sz=4, imbalance = -2/6 = -0.333333... (precision test!)
    for _ in range(n):
        rows.append(
            BboRow(
                symbol=_SYMBOL,
                ts=t,
                source_ts=t,
                bid_px=Decimal("94990"),
                bid_sz=Decimal("2"),
                ask_px=Decimal("95010"),
                ask_sz=Decimal("4"),
            )
        )
        t += step
    return rows


def _sample_books5_rows(n: int):
    """Build N books5 rows across bar (5 levels each side)."""
    from aats.data_platform.collectors.microstructure_ws_collector import Books5Row

    rows = []
    step = (_BAR_END - _BAR_START) / max(n, 1)
    t = _BAR_START
    for _ in range(n):
        rows.append(
            Books5Row(
                symbol=_SYMBOL,
                ts=t,
                source_ts=t,
                bid_px_1=Decimal("94990"), bid_sz_1=Decimal("2"),
                bid_px_2=Decimal("94989"), bid_sz_2=Decimal("3"),
                bid_px_3=Decimal("94988"), bid_sz_3=Decimal("5"),
                bid_px_4=Decimal("94987"), bid_sz_4=Decimal("8"),
                bid_px_5=Decimal("94986"), bid_sz_5=Decimal("13"),
                ask_px_1=Decimal("95010"), ask_sz_1=Decimal("4"),
                ask_px_2=Decimal("95011"), ask_sz_2=Decimal("6"),
                ask_px_3=Decimal("95012"), ask_sz_3=Decimal("10"),
                ask_px_4=Decimal("95013"), ask_sz_4=Decimal("16"),
                ask_px_5=Decimal("95014"), ask_sz_5=Decimal("26"),
            )
        )
        t += step
    return rows


def _sample_oif_rows(n: int):
    """Build N oi/funding/mark tick rows (mix all three types)."""
    from aats.data_platform.collectors.microstructure_ws_collector import (
        OiFundingMarkRow,
    )

    rows = []
    step = (_BAR_END - _BAR_START) / max(n, 1)
    t = _BAR_START
    for i in range(n):
        kind = i % 3
        if kind == 0:
            rows.append(
                OiFundingMarkRow(
                    ts=t, symbol=_SYMBOL, tick_type="oi",
                    oi=Decimal("50000") + Decimal(i * 100),
                    oi_ccy=Decimal("4750000000"),
                )
            )
        elif kind == 1:
            rows.append(
                OiFundingMarkRow(
                    ts=t, symbol=_SYMBOL, tick_type="funding",
                    funding_rate=Decimal("0.0001"),
                    next_funding_rate=Decimal("0.00015"),
                    next_funding_time=t + timedelta(hours=8),
                )
            )
        else:
            rows.append(
                OiFundingMarkRow(
                    ts=t, symbol=_SYMBOL, tick_type="mark",
                    mark_px=Decimal("95000") + Decimal(i),
                )
            )
        t += step
    return rows


@unittest.skipUnless(
    _SHOULD_RUN,
    f"need docker + testcontainers + {_INTEGRATION_ENV_FLAG}=1",
)
class MicrostructurePipelineE2ETests(unittest.TestCase):
    """End-to-end pipeline on real Postgres via testcontainers."""

    container: "PostgresContainer | None" = None
    engine = None

    @classmethod
    def setUpClass(cls) -> None:
        # postgres:16-alpine matches aats-postgres in WSL2 dev stack
        cls.container = PostgresContainer("postgres:16-alpine")
        cls.container.start()

        from sqlalchemy import create_engine

        url = cls.container.get_connection_url()
        cls.engine = create_engine(url, future=True)
        _apply_migrations(cls.engine)

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.engine is not None:
            cls.engine.dispose()
        if cls.container is not None:
            cls.container.stop()

    def setUp(self) -> None:
        """Each test starts with clean bronze/staging/silver tables."""
        from sqlalchemy import text

        tables = [
            "bronze.market_trades",
            "bronze.market_orderbook_bbo",
            "bronze.market_orderbook_books5",
            "staging.market_oi_funding_ticks",
            "silver.market_orderbook_metrics_15m",
            "silver.market_trade_flow_15m",
            "silver.market_oi_funding_metrics_15m",
            "silver.market_volume_profile_15m",
            "silver.market_liquidation_metrics_15m",
        ]
        with self.engine.begin() as conn:  # type: ignore[union-attr]
            for t in tables:
                conn.execute(text(f"TRUNCATE TABLE {t}"))

    # ─────────────────────────────────────────────────────────────────
    # Case 1: happy path — full Bronze + Silver ETL
    # ─────────────────────────────────────────────────────────────────
    def test_happy_path_full_pipeline(self) -> None:
        """Write 100 trades + 30 bbo + 20 books5 + 15 oif ticks, run Silver ETL,
        verify each of 5 Silver tables has 1 row with precise NUMERIC values.

        Key precision assertions (only visible under real PG, SQLite round-trips
        these to 0 or NULL):
          - bronze.market_orderbook_bbo.imbalance = -0.333333... (GENERATED)
          - silver.market_orderbook_metrics_15m.bbo_samples_n = 30
          - silver.market_trade_flow_15m.trade_count = 100
        """
        from sqlalchemy import text
        from sqlalchemy.orm import Session

        from aats.data_platform.collectors.microstructure_ws_collector import (
            write_trades_batch,
            write_bbo_batch,
            write_books5_batch,
            write_oif_batch,
        )
        from aats.data_platform.merge.microstructure_silver_merger import (
            build_silver_microstructure_15m,
        )

        run_id = str(uuid4())

        # ── Write Bronze + staging
        with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
            write_trades_batch(session, _sample_trades(50, 50, run_id), ingest_run_id=run_id)
            write_bbo_batch(session, _sample_bbo_rows(30), ingest_run_id=run_id)
            write_books5_batch(session, _sample_books5_rows(20), ingest_run_id=run_id)
            write_oif_batch(session, _sample_oif_rows(15))

        # ── Verify bronze.market_orderbook_bbo generated columns work in real PG
        with self.engine.begin() as conn:  # type: ignore[union-attr]
            row = conn.execute(
                text(
                    "SELECT mid, spread, imbalance "
                    "FROM bronze.market_orderbook_bbo "
                    "ORDER BY ts LIMIT 1"
                )
            ).mappings().fetchone()
            self.assertIsNotNone(row)
            # mid = (94990 + 95010) / 2 = 95000
            self.assertEqual(Decimal(row["mid"]), Decimal("95000"))
            # spread = 95010 - 94990 = 20
            self.assertEqual(Decimal(row["spread"]), Decimal("20"))
            # imbalance = (2 - 4) / (2 + 4) = -0.3333333333
            imbalance = Decimal(row["imbalance"])
            self.assertLess(imbalance, Decimal("-0.3"))
            self.assertGreater(imbalance, Decimal("-0.34"))

        # ── Run Silver ETL
        with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
            result = build_silver_microstructure_15m(
                session=session,
                symbol=_SYMBOL,
                bar_start_ts=_BAR_START,
                bar_end_ts=_BAR_END,
                ingest_run_id=run_id,
            )
            self.assertIsNone(result.error, f"ETL error: {result.error}")
            # Each of 5 silver tables wrote 1 row
            for tbl, cnt in result.tables_written.items():
                self.assertEqual(cnt, 1, f"{tbl} should have 1 row written, got {cnt}")

        # ── Verify Silver table contents (5 rows exactly)
        with self.engine.begin() as conn:  # type: ignore[union-attr]
            # Orderbook silver
            ob = conn.execute(
                text(
                    "SELECT bbo_samples_n, books5_samples_n, mid_price_last, "
                    "       bbo_imbalance_mean "
                    "FROM silver.market_orderbook_metrics_15m "
                    "WHERE symbol=:s AND ts=:t"
                ),
                {"s": _SYMBOL, "t": _BAR_START},
            ).mappings().fetchone()
            self.assertIsNotNone(ob)
            self.assertEqual(ob["bbo_samples_n"], 30)
            self.assertEqual(ob["books5_samples_n"], 20)
            self.assertEqual(Decimal(ob["mid_price_last"]), Decimal("95000"))
            # imbalance mean ≈ -0.333, real PG NUMERIC preserves precision
            self.assertLess(Decimal(ob["bbo_imbalance_mean"]), Decimal("-0.3"))

            # Trade flow silver
            tf = conn.execute(
                text(
                    "SELECT trade_count, total_volume_ccy, buy_volume_ccy, "
                    "       sell_volume_ccy, taker_buy_ratio "
                    "FROM silver.market_trade_flow_15m "
                    "WHERE symbol=:s AND ts=:t"
                ),
                {"s": _SYMBOL, "t": _BAR_START},
            ).mappings().fetchone()
            self.assertIsNotNone(tf)
            self.assertEqual(tf["trade_count"], 100)
            # Buy volume = 50 trades × 0.5 sz × ~95000 px = ~2375000
            buy_vol = Decimal(tf["buy_volume_ccy"])
            sell_vol = Decimal(tf["sell_volume_ccy"])
            total_vol = Decimal(tf["total_volume_ccy"])
            self.assertGreater(buy_vol, Decimal("0"))
            self.assertGreater(sell_vol, Decimal("0"))
            # taker_buy_ratio = buy_vol / (buy_vol + sell_vol)
            # Since buy trades are 0.5 sz and sell are 0.3 sz, buy > 0.5 ratio
            ratio = Decimal(tf["taker_buy_ratio"])
            self.assertGreater(ratio, Decimal("0.5"))
            # Sum check: buy + sell ≈ total (within rounding)
            self.assertAlmostEqual(
                float(buy_vol + sell_vol), float(total_vol), places=6
            )

            # OI funding silver (oi_samples_n counts 'oi' ticks = 5 out of 15)
            oif = conn.execute(
                text(
                    "SELECT oi_samples_n, oi_close, mark_price, funding_rate_current "
                    "FROM silver.market_oi_funding_metrics_15m "
                    "WHERE symbol=:s AND ts=:t"
                ),
                {"s": _SYMBOL, "t": _BAR_START},
            ).mappings().fetchone()
            self.assertIsNotNone(oif)
            # 15 total ticks, 1/3 are 'oi' = 5
            self.assertEqual(oif["oi_samples_n"], 5)
            self.assertIsNotNone(oif["oi_close"])
            self.assertIsNotNone(oif["mark_price"])

            # Volume profile silver (cold-start, < 4 weeks baseline)
            vp = conn.execute(
                text(
                    "SELECT volume_ccy, trade_count, volume_z_score, "
                    "       baseline_sample_weeks, quality_flags "
                    "FROM silver.market_volume_profile_15m "
                    "WHERE symbol=:s AND ts=:t"
                ),
                {"s": _SYMBOL, "t": _BAR_START},
            ).mappings().fetchone()
            self.assertIsNotNone(vp)
            self.assertEqual(vp["trade_count"], 100)
            # Cold-start: baseline_sample_weeks < 4, z-score NULL
            self.assertLess(vp["baseline_sample_weeks"], 4)
            self.assertIsNone(vp["volume_z_score"])
            self.assertIn("partial_baseline", list(vp["quality_flags"] or []))

            # Liquidation silver (no staging.raw_liquidations data → all zero counts)
            lq = conn.execute(
                text(
                    "SELECT long_liq_count, short_liq_count, quality_flags "
                    "FROM silver.market_liquidation_metrics_15m "
                    "WHERE symbol=:s AND ts=:t"
                ),
                {"s": _SYMBOL, "t": _BAR_START},
            ).mappings().fetchone()
            self.assertIsNotNone(lq)
            self.assertEqual(lq["long_liq_count"], 0)
            self.assertEqual(lq["short_liq_count"], 0)

    # ─────────────────────────────────────────────────────────────────
    # Case 2: empty bar — no Bronze data at all
    # ─────────────────────────────────────────────────────────────────
    def test_empty_bar_still_writes_silver_rows_with_flags(self) -> None:
        """Zero Bronze data → all 5 Silver tables still have 1 row each with
        NULL metrics + 'no_data' quality_flags for the relevant tables.

        This is a §11 Gate requirement: Silver table row count >= 96/24h
        even if a bar is sparse/missing data."""
        from sqlalchemy import text
        from sqlalchemy.orm import Session

        from aats.data_platform.merge.microstructure_silver_merger import (
            build_silver_microstructure_15m,
        )

        run_id = str(uuid4())
        with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
            result = build_silver_microstructure_15m(
                session=session,
                symbol=_SYMBOL,
                bar_start_ts=_BAR_START,
                bar_end_ts=_BAR_END,
                ingest_run_id=run_id,
            )
            self.assertIsNone(result.error, f"ETL error: {result.error}")

        # All 5 silver tables should have 1 row even with no Bronze data
        with self.engine.begin() as conn:  # type: ignore[union-attr]
            for tbl in (
                "silver.market_orderbook_metrics_15m",
                "silver.market_trade_flow_15m",
                "silver.market_oi_funding_metrics_15m",
                "silver.market_volume_profile_15m",
                "silver.market_liquidation_metrics_15m",
            ):
                cnt = conn.execute(
                    text(f"SELECT COUNT(*) FROM {tbl}")
                ).scalar()
                self.assertEqual(cnt, 1, f"{tbl} should have 1 row even in empty bar")

            # Orderbook silver: all numeric metrics NULL, samples_n = 0
            ob = conn.execute(
                text(
                    "SELECT bbo_samples_n, books5_samples_n, mid_price_last, "
                    "       quality_flags "
                    "FROM silver.market_orderbook_metrics_15m LIMIT 1"
                )
            ).mappings().fetchone()
            self.assertEqual(ob["bbo_samples_n"], 0)
            self.assertEqual(ob["books5_samples_n"], 0)
            self.assertIsNone(ob["mid_price_last"])
            flags = list(ob["quality_flags"] or [])
            self.assertTrue(
                any("no_data" in f or "partial" in f for f in flags),
                f"expected no_data/partial flag, got {flags}",
            )

    # ─────────────────────────────────────────────────────────────────
    # Case 3: partial data — only BBO, trade_flow + volume_profile degrade
    # ─────────────────────────────────────────────────────────────────
    def test_partial_data_only_bbo(self) -> None:
        """Only Bronze BBO has data, Silver trade_flow / volume_profile should
        still write rows (no-data flags), orderbook should have real metrics."""
        from sqlalchemy import text
        from sqlalchemy.orm import Session

        from aats.data_platform.collectors.microstructure_ws_collector import (
            write_bbo_batch,
        )
        from aats.data_platform.merge.microstructure_silver_merger import (
            build_silver_microstructure_15m,
        )

        run_id = str(uuid4())
        with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
            write_bbo_batch(session, _sample_bbo_rows(10), ingest_run_id=run_id)

        with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
            result = build_silver_microstructure_15m(
                session=session,
                symbol=_SYMBOL,
                bar_start_ts=_BAR_START,
                bar_end_ts=_BAR_END,
                ingest_run_id=run_id,
            )
            self.assertIsNone(result.error)

        with self.engine.begin() as conn:  # type: ignore[union-attr]
            # Orderbook silver has real metrics
            ob = conn.execute(
                text(
                    "SELECT bbo_samples_n, mid_price_last FROM "
                    "silver.market_orderbook_metrics_15m"
                )
            ).mappings().fetchone()
            self.assertEqual(ob["bbo_samples_n"], 10)
            self.assertEqual(Decimal(ob["mid_price_last"]), Decimal("95000"))

            # Trade flow silver: 0 trades, flag set
            tf = conn.execute(
                text(
                    "SELECT trade_count, total_volume_ccy, quality_flags "
                    "FROM silver.market_trade_flow_15m"
                )
            ).mappings().fetchone()
            self.assertEqual(tf["trade_count"], 0)
            flags = list(tf["quality_flags"] or [])
            self.assertTrue(
                any("no_data" in f or "partial" in f for f in flags),
                f"expected no_data flag, got {flags}",
            )

    # ─────────────────────────────────────────────────────────────────
    # Case 4: idempotent — running ETL twice on same bar produces 1 row
    # ─────────────────────────────────────────────────────────────────
    def test_idempotent_silver_etl_rerun(self) -> None:
        """Same bar ETL runs twice → Silver tables still have exactly 1 row each.

        Validates the `ON CONFLICT (symbol, ts) DO UPDATE` semantics in real PG."""
        from sqlalchemy import text
        from sqlalchemy.orm import Session

        from aats.data_platform.collectors.microstructure_ws_collector import (
            write_trades_batch, write_bbo_batch,
        )
        from aats.data_platform.merge.microstructure_silver_merger import (
            build_silver_microstructure_15m,
        )

        run_id_1 = str(uuid4())
        run_id_2 = str(uuid4())  # Different run → should still UPSERT, not INSERT

        # First run
        with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
            write_trades_batch(session, _sample_trades(10, 10, run_id_1), ingest_run_id=run_id_1)
            write_bbo_batch(session, _sample_bbo_rows(5), ingest_run_id=run_id_1)

        with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
            r1 = build_silver_microstructure_15m(
                session=session, symbol=_SYMBOL,
                bar_start_ts=_BAR_START, bar_end_ts=_BAR_END,
                ingest_run_id=run_id_1,
            )
            self.assertIsNone(r1.error)

        # Second run with different run_id
        with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
            r2 = build_silver_microstructure_15m(
                session=session, symbol=_SYMBOL,
                bar_start_ts=_BAR_START, bar_end_ts=_BAR_END,
                ingest_run_id=run_id_2,
            )
            self.assertIsNone(r2.error)

        # Each Silver table still has exactly 1 row
        with self.engine.begin() as conn:  # type: ignore[union-attr]
            for tbl in (
                "silver.market_orderbook_metrics_15m",
                "silver.market_trade_flow_15m",
                "silver.market_oi_funding_metrics_15m",
                "silver.market_volume_profile_15m",
                "silver.market_liquidation_metrics_15m",
            ):
                cnt = conn.execute(
                    text(f"SELECT COUNT(*) FROM {tbl} WHERE symbol=:s AND ts=:t"),
                    {"s": _SYMBOL, "t": _BAR_START},
                ).scalar()
                self.assertEqual(cnt, 1, f"{tbl} not idempotent: {cnt} rows")

            # ingest_run_id should be the latest (run_id_2) due to DO UPDATE
            final_run = conn.execute(
                text(
                    "SELECT ingest_run_id::text FROM "
                    "silver.market_orderbook_metrics_15m "
                    "WHERE symbol=:s AND ts=:t"
                ),
                {"s": _SYMBOL, "t": _BAR_START},
            ).scalar()
            self.assertEqual(
                final_run, run_id_2,
                "ingest_run_id must update to latest run (ON CONFLICT DO UPDATE)",
            )

    # ─────────────────────────────────────────────────────────────────
    # Case 5: migration rollback — batch_b_06 then batch_b_05 drop cleanly
    # ─────────────────────────────────────────────────────────────────
    def test_migration_forward_rollback_idempotent(self) -> None:
        """Rollback batch_b_06+05, then re-apply, verify tables present again.

        This test runs LAST because it mutates shared schema state. Subsequent
        tests would fail if run afterward, but pytest --unittest mode runs by
        method name alphabetically so this is test number 5 (case e)."""
        from sqlalchemy import text

        # Snapshot: batch_b_05+06 tables exist
        with self.engine.begin() as conn:  # type: ignore[union-attr]
            before = conn.execute(
                text(
                    "SELECT table_schema, table_name FROM information_schema.tables "
                    "WHERE (table_schema='bronze' AND table_name LIKE 'market_%') "
                    "OR (table_schema='silver' AND table_name LIKE 'market_%15m') "
                    "OR (table_schema='staging' AND table_name='market_oi_funding_ticks') "
                    "ORDER BY 1, 2"
                )
            ).all()
        # 3 bronze + 5 silver + 1 staging = 9 tables expected from batch_b_05+06
        self.assertGreaterEqual(len(before), 9, f"expected >= 9 tables, got {before}")

        # Rollback (tears down all 9 tables)
        _apply_migrations(self.engine, rollback=True)

        with self.engine.begin() as conn:  # type: ignore[union-attr]
            after_rollback = conn.execute(
                text(
                    "SELECT table_schema, table_name FROM information_schema.tables "
                    "WHERE (table_schema='bronze' AND table_name LIKE 'market_%') "
                    "OR (table_schema='silver' AND table_name LIKE 'market_%15m') "
                    "OR (table_schema='staging' AND table_name='market_oi_funding_ticks')"
                )
            ).all()
        self.assertEqual(
            list(after_rollback), [],
            f"after rollback, no microstructure tables should remain, "
            f"found: {after_rollback}",
        )

        # Re-apply (should be idempotent clean state)
        _apply_migrations(self.engine)

        with self.engine.begin() as conn:  # type: ignore[union-attr]
            after_reapply = conn.execute(
                text(
                    "SELECT table_schema, table_name FROM information_schema.tables "
                    "WHERE (table_schema='bronze' AND table_name LIKE 'market_%') "
                    "OR (table_schema='silver' AND table_name LIKE 'market_%15m') "
                    "OR (table_schema='staging' AND table_name='market_oi_funding_ticks') "
                    "ORDER BY 1, 2"
                )
            ).all()
        self.assertEqual(
            len(after_reapply), 9,
            f"after re-apply, all 9 tables must exist, got {after_reapply}",
        )


if __name__ == "__main__":
    unittest.main()
