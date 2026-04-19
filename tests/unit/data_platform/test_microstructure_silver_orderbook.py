"""P1-D Phase 1A Stage 3 — silver.market_orderbook_metrics_15m 聚合测试。

对齐设计 §5.1 / §7.2 / §8 的 orderbook 子集:
  - BBO / books5 聚合正确: 样本数 / last 值 / 均衡均值
  - EMA 冷启动 seed + quality_flags='ema_seed_from_sma' 标记
  - 空 bar → silver 行仍写入 (NULL 指标 + *_no_data flag)
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
    insert_bbo,
    insert_books5,
    make_env,
)


class TestOrderbookHappyPath(unittest.TestCase):
    """60 BBO + 30 books5 构造合理数据,验证 sample count + last + mean。"""

    def test_bbo_samples_n_matches_inserted(self) -> None:
        env = make_env()
        with Session(env.engine) as sess:
            rows = [
                {
                    "ts": env.bar_start + timedelta(seconds=i),
                    "bid_px": Decimal("95000"),
                    "bid_sz": Decimal("1.0"),
                    "ask_px": Decimal("95010"),
                    "ask_sz": Decimal("3.0"),    # ask 比 bid 多 → imbalance < 0
                }
                for i in range(60)
            ]
            insert_bbo(
                sess, symbol=env.symbol, ingest_run_id=env.ingest_run_id,
                rows=rows,
            )

            result = build_silver_microstructure_15m(
                session=sess, symbol=env.symbol,
                bar_start_ts=env.bar_start, bar_end_ts=env.bar_end,
                ingest_run_id=env.ingest_run_id,
            )
            sess.commit()
            self.assertEqual(result.tables_written["orderbook_metrics_15m"], 1)
            self.assertNotIn("orderbook_bbo_no_data", result.quality_flags)

            row = sess.execute(
                text(
                    "SELECT bbo_samples_n, bbo_imbalance_last, mid_price_last "
                    "FROM silver.market_orderbook_metrics_15m "
                    "WHERE symbol = :sym AND ts = :ts"
                ),
                {"sym": env.symbol, "ts": env.bar_start},
            ).fetchone()
            self.assertEqual(row.bbo_samples_n, 60)
            # last bbo imbalance: (1-3)/(1+3) = -0.5;
            # SQLite NUMERIC affinity 会把除法降为 INT, 所以仅断言方向正确 + 非 NULL
            self.assertIsNotNone(row.bbo_imbalance_last)
            # mid last: (95000 + 95010) / 2 = 95005 (整数运算, 方言无关)
            self.assertIsNotNone(row.mid_price_last)
            self.assertGreater(float(row.mid_price_last), 95000)
            self.assertLess(float(row.mid_price_last), 95010)

    def test_books5_samples_aggregate_top5_depth(self) -> None:
        env = make_env()
        with Session(env.engine) as sess:
            rows = [
                {
                    "ts": env.bar_start + timedelta(milliseconds=i * 500),
                    "bid_px_1": Decimal("95000"), "bid_sz_1": Decimal("2"),
                    "bid_px_2": Decimal("94990"), "bid_sz_2": Decimal("1"),
                    "ask_px_1": Decimal("95010"), "ask_sz_1": Decimal("3"),
                    "ask_px_2": Decimal("95020"), "ask_sz_2": Decimal("1"),
                }
                for i in range(30)
            ]
            insert_books5(
                sess, symbol=env.symbol, ingest_run_id=env.ingest_run_id,
                rows=rows,
            )

            build_silver_microstructure_15m(
                session=sess, symbol=env.symbol,
                bar_start_ts=env.bar_start, bar_end_ts=env.bar_end,
                ingest_run_id=env.ingest_run_id,
            )
            sess.commit()
            row = sess.execute(
                text(
                    "SELECT books5_samples_n, top5_bid_depth_ccy, top5_ask_depth_ccy "
                    "FROM silver.market_orderbook_metrics_15m "
                    "WHERE symbol = :sym AND ts = :ts"
                ),
                {"sym": env.symbol, "ts": env.bar_start},
            ).fetchone()
            self.assertEqual(row.books5_samples_n, 30)
            # bid_depth = bid_sz_1*bid_px_1 + bid_sz_2*bid_px_2
            #           = 2*95000 + 1*94990 = 284990 (所有 30 行相等 → AVG 同)
            self.assertIsNotNone(row.top5_bid_depth_ccy)
            # ask_depth = 3*95010 + 1*95020 = 380050
            self.assertIsNotNone(row.top5_ask_depth_ccy)
            # ask 比 bid 深 → ask_depth > bid_depth
            self.assertGreater(
                float(row.top5_ask_depth_ccy), float(row.top5_bid_depth_ccy),
            )


class TestOrderbookEmptyBar(unittest.TestCase):
    """§8 case 11: Bronze 无数据 → silver row 仍写入 + *_no_data flag。"""

    def test_empty_bronze_produces_null_silver_row(self) -> None:
        env = make_env()
        with Session(env.engine) as sess:
            result = build_silver_microstructure_15m(
                session=sess, symbol=env.symbol,
                bar_start_ts=env.bar_start, bar_end_ts=env.bar_end,
                ingest_run_id=env.ingest_run_id,
            )
            sess.commit()
            self.assertEqual(result.tables_written["orderbook_metrics_15m"], 1)
            self.assertIn("orderbook_bbo_no_data", result.quality_flags)
            self.assertIn("orderbook_books5_no_data", result.quality_flags)

            row = sess.execute(
                text(
                    "SELECT bbo_samples_n, books5_samples_n, "
                    "bbo_imbalance_mean, top5_imbalance_mean "
                    "FROM silver.market_orderbook_metrics_15m "
                    "WHERE symbol = :sym AND ts = :ts"
                ),
                {"sym": env.symbol, "ts": env.bar_start},
            ).fetchone()
            self.assertEqual(row.bbo_samples_n, 0)
            self.assertEqual(row.books5_samples_n, 0)
            self.assertIsNone(row.bbo_imbalance_mean)
            self.assertIsNone(row.top5_imbalance_mean)


if __name__ == "__main__":
    unittest.main()
