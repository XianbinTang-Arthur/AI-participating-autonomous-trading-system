"""P1-D Phase 1A Stage 3 — silver.market_liquidation_metrics_15m 聚合测试。

对齐设计 §5.5 / §8 case 15:
  - staging.raw_liquidations 的 side → long/short 清算映射
  - notional USD 按 bk_px * sz 算
  - cascade_flag 由 count 阈值触发
  - 空 bar → silver 行仍写入 + liquidation_no_data
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
    insert_liquidations,
    make_env,
)


class TestLiquidationSplitBySide(unittest.TestCase):
    """OKX side='sell'→长仓清算, side='buy'→短仓清算。"""

    def test_long_vs_short_counts_and_notional(self) -> None:
        env = make_env()
        with Session(env.engine) as sess:
            rows = [
                # 3 长仓清算 (side='sell')
                {"ts": env.bar_start + timedelta(seconds=30), "side": "sell",
                 "bk_px": Decimal("95000"), "sz": Decimal("1.0")},
                {"ts": env.bar_start + timedelta(seconds=120), "side": "sell",
                 "bk_px": Decimal("94900"), "sz": Decimal("0.5")},
                {"ts": env.bar_start + timedelta(seconds=300), "side": "sell",
                 "bk_px": Decimal("94800"), "sz": Decimal("2.0")},
                # 2 短仓清算 (side='buy')
                {"ts": env.bar_start + timedelta(seconds=500), "side": "buy",
                 "bk_px": Decimal("95200"), "sz": Decimal("0.5")},
                {"ts": env.bar_start + timedelta(seconds=800), "side": "buy",
                 "bk_px": Decimal("95300"), "sz": Decimal("1.5")},
            ]
            insert_liquidations(sess, symbol=env.symbol, rows=rows)

            result = build_silver_microstructure_15m(
                session=sess, symbol=env.symbol,
                bar_start_ts=env.bar_start, bar_end_ts=env.bar_end,
                ingest_run_id=env.ingest_run_id,
            )
            sess.commit()
            self.assertNotIn("liquidation_no_data", result.quality_flags)

            row = sess.execute(
                text(
                    "SELECT long_liq_count, short_liq_count, "
                    "long_liq_notional_usd, short_liq_notional_usd, "
                    "liq_imbalance, cascade_flag, max_single_liq_usd "
                    "FROM silver.market_liquidation_metrics_15m "
                    "WHERE symbol = :sym AND ts = :ts"
                ),
                {"sym": env.symbol, "ts": env.bar_start},
            ).fetchone()
            self.assertEqual(row.long_liq_count, 3)
            self.assertEqual(row.short_liq_count, 2)
            # long notional = 95000 + 94900*0.5 + 94800*2.0 = 95000 + 47450 + 189600 = 332050
            self.assertAlmostEqual(float(row.long_liq_notional_usd), 332050.0, places=1)
            # short notional = 95200*0.5 + 95300*1.5 = 47600 + 142950 = 190550
            self.assertAlmostEqual(float(row.short_liq_notional_usd), 190550.0, places=1)
            # imbalance = (332050 - 190550) / (332050 + 190550) = 141500 / 522600 ≈ 0.2707
            # 方言无关只断言 > 0 (long 主导)
            self.assertGreater(float(row.liq_imbalance), 0.0)
            # 5 counts < 30 threshold → cascade_flag=False
            self.assertFalse(bool(row.cascade_flag))
            # max_single = 94800 * 2.0 = 189600
            self.assertAlmostEqual(float(row.max_single_liq_usd), 189600.0, places=1)


class TestLiquidationCascadeFlag(unittest.TestCase):
    """count >= 30 触发 cascade_flag。"""

    def test_cascade_triggers_when_count_exceeds_threshold(self) -> None:
        env = make_env()
        with Session(env.engine) as sess:
            # 35 笔清算 → > 30 阈值
            rows = [
                {
                    "ts": env.bar_start + timedelta(seconds=i * 20),
                    "side": "sell",
                    "bk_px": Decimal("95000"),
                    "sz": Decimal("0.1"),
                }
                for i in range(35)
            ]
            insert_liquidations(sess, symbol=env.symbol, rows=rows)

            build_silver_microstructure_15m(
                session=sess, symbol=env.symbol,
                bar_start_ts=env.bar_start, bar_end_ts=env.bar_end,
                ingest_run_id=env.ingest_run_id,
            )
            sess.commit()

            row = sess.execute(
                text(
                    "SELECT long_liq_count, cascade_flag, cascade_threshold_used "
                    "FROM silver.market_liquidation_metrics_15m "
                    "WHERE symbol = :sym AND ts = :ts"
                ),
                {"sym": env.symbol, "ts": env.bar_start},
            ).fetchone()
            self.assertEqual(row.long_liq_count, 35)
            self.assertTrue(bool(row.cascade_flag))
            self.assertEqual(row.cascade_threshold_used, 30)


class TestLiquidationEmptyBar(unittest.TestCase):
    def test_empty_bar_flag_and_zero_counts(self) -> None:
        env = make_env()
        with Session(env.engine) as sess:
            result = build_silver_microstructure_15m(
                session=sess, symbol=env.symbol,
                bar_start_ts=env.bar_start, bar_end_ts=env.bar_end,
                ingest_run_id=env.ingest_run_id,
            )
            sess.commit()
            self.assertIn("liquidation_no_data", result.quality_flags)

            row = sess.execute(
                text(
                    "SELECT long_liq_count, short_liq_count, cascade_flag "
                    "FROM silver.market_liquidation_metrics_15m "
                    "WHERE symbol = :sym AND ts = :ts"
                ),
                {"sym": env.symbol, "ts": env.bar_start},
            ).fetchone()
            self.assertEqual(row.long_liq_count, 0)
            self.assertEqual(row.short_liq_count, 0)
            self.assertFalse(bool(row.cascade_flag))


if __name__ == "__main__":
    unittest.main()
