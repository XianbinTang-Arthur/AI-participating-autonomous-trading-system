"""P1-D Phase 1A Stage 3 — silver.market_oi_funding_metrics_15m 聚合测试。

对齐设计 §5.3 / §8 case 13:
  - OI open/close/high/low 由 tick_type='oi' 聚合
  - EMA 冷启动: 无历史 silver 行 → EMA seeded from current + flag='ema_seed_from_sma'
  - funding/mark 各取 bar 内最后一个 tick
"""
from __future__ import annotations

import unittest
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from aats.data_platform.merge.microstructure_silver_merger import (
    build_silver_microstructure_15m,
)
from tests.unit.data_platform._silver_test_helpers import (
    insert_oi_funding_ticks,
    make_env,
)


class TestOiAggregation(unittest.TestCase):
    def test_oi_open_close_high_low_from_ticks(self) -> None:
        env = make_env()
        with Session(env.engine) as sess:
            ticks = [
                {"ts": env.bar_start + timedelta(seconds=10), "tick_type": "oi",
                 "oi": Decimal("1000000.0")},
                {"ts": env.bar_start + timedelta(seconds=100), "tick_type": "oi",
                 "oi": Decimal("1000500.0")},   # high
                {"ts": env.bar_start + timedelta(seconds=200), "tick_type": "oi",
                 "oi": Decimal("999800.0")},    # low
                {"ts": env.bar_start + timedelta(seconds=800), "tick_type": "oi",
                 "oi": Decimal("1000100.0")},   # close
            ]
            insert_oi_funding_ticks(sess, symbol=env.symbol, rows=ticks)

            build_silver_microstructure_15m(
                session=sess, symbol=env.symbol,
                bar_start_ts=env.bar_start, bar_end_ts=env.bar_end,
                ingest_run_id=env.ingest_run_id,
            )
            sess.commit()
            row = sess.execute(
                text(
                    "SELECT oi_open, oi_close, oi_high, oi_low, oi_samples_n "
                    "FROM silver.market_oi_funding_metrics_15m "
                    "WHERE symbol = :sym AND ts = :ts"
                ),
                {"sym": env.symbol, "ts": env.bar_start},
            ).fetchone()
            self.assertEqual(row.oi_samples_n, 4)
            self.assertAlmostEqual(float(row.oi_open), 1000000.0, places=2)
            self.assertAlmostEqual(float(row.oi_close), 1000100.0, places=2)
            self.assertAlmostEqual(float(row.oi_high), 1000500.0, places=2)
            self.assertAlmostEqual(float(row.oi_low), 999800.0, places=2)


class TestEmaColdStart(unittest.TestCase):
    """§7.4 EMA 递归 cold-start: silver 无上一行时 seed + flag='ema_seed_from_sma'。"""

    def test_first_bar_seeds_ema_with_sma_flag(self) -> None:
        env = make_env()
        with Session(env.engine) as sess:
            ticks = [
                {"ts": env.bar_start + timedelta(seconds=i * 60), "tick_type": "oi",
                 "oi": Decimal("1000000") + Decimal(str(i * 100))}
                for i in range(5)
            ]
            insert_oi_funding_ticks(sess, symbol=env.symbol, rows=ticks)

            result = build_silver_microstructure_15m(
                session=sess, symbol=env.symbol,
                bar_start_ts=env.bar_start, bar_end_ts=env.bar_end,
                ingest_run_id=env.ingest_run_id,
            )
            sess.commit()
            self.assertIn("ema_seed_from_sma", result.quality_flags)

            row = sess.execute(
                text(
                    "SELECT oi_ema_20, oi_close FROM silver.market_oi_funding_metrics_15m "
                    "WHERE symbol = :sym AND ts = :ts"
                ),
                {"sym": env.symbol, "ts": env.bar_start},
            ).fetchone()
            self.assertIsNotNone(row.oi_ema_20)


class TestFundingAndMarkLastValue(unittest.TestCase):
    """每 bar 内 tick_type='funding'/'mark' 只取最后一个 tick."""

    def test_last_funding_rate_kept(self) -> None:
        env = make_env()
        with Session(env.engine) as sess:
            ticks = [
                {"ts": env.bar_start + timedelta(seconds=30), "tick_type": "funding",
                 "funding_rate": Decimal("0.00010")},
                {"ts": env.bar_start + timedelta(seconds=600), "tick_type": "funding",
                 "funding_rate": Decimal("0.00020")},
                {"ts": env.bar_start + timedelta(seconds=850), "tick_type": "funding",
                 "funding_rate": Decimal("0.00015")},  # latest in bar
                # mark ticks
                {"ts": env.bar_start + timedelta(seconds=100), "tick_type": "mark",
                 "mark_px": Decimal("94999")},
                {"ts": env.bar_start + timedelta(seconds=870), "tick_type": "mark",
                 "mark_px": Decimal("95003")},         # latest in bar
            ]
            insert_oi_funding_ticks(sess, symbol=env.symbol, rows=ticks)

            build_silver_microstructure_15m(
                session=sess, symbol=env.symbol,
                bar_start_ts=env.bar_start, bar_end_ts=env.bar_end,
                ingest_run_id=env.ingest_run_id,
            )
            sess.commit()
            row = sess.execute(
                text(
                    "SELECT funding_rate_current, mark_price "
                    "FROM silver.market_oi_funding_metrics_15m "
                    "WHERE symbol = :sym AND ts = :ts"
                ),
                {"sym": env.symbol, "ts": env.bar_start},
            ).fetchone()
            self.assertAlmostEqual(
                float(row.funding_rate_current), 0.00015, places=6,
            )
            self.assertAlmostEqual(float(row.mark_price), 95003.0, places=1)


if __name__ == "__main__":
    unittest.main()
