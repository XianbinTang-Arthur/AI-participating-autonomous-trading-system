"""P1-D Phase 1A Stage 3 — silver.market_trade_flow_15m 聚合测试。

对齐设计 §5.2 / §8 case 12:
  - volume / count / taker_buy_ratio 算对
  - whale detection 触发 (size >= _WHALE_SIZE_FALLBACK)
  - 空 bar → silver 行仍写入 + trades_no_data flag
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
    insert_trades,
    make_env,
)


class TestTradeFlowHappyPath(unittest.TestCase):
    def test_buy_sell_volume_split(self) -> None:
        env = make_env()
        with Session(env.engine) as sess:
            trades = []
            for i in range(20):
                ts = env.bar_start + timedelta(seconds=i * 40)
                trades.append({
                    "ts": ts,
                    "trade_id": f"buy-{i}",
                    "px": Decimal("95000"),
                    "sz": Decimal("0.5"),
                    "side": "buy",    # taker buy
                })
            for i in range(10):
                ts = env.bar_start + timedelta(seconds=i * 80 + 5)
                trades.append({
                    "ts": ts,
                    "trade_id": f"sell-{i}",
                    "px": Decimal("95010"),
                    "sz": Decimal("0.3"),
                    "side": "sell",
                })
            insert_trades(
                sess, symbol=env.symbol, ingest_run_id=env.ingest_run_id,
                trades=trades,
            )
            build_silver_microstructure_15m(
                session=sess, symbol=env.symbol,
                bar_start_ts=env.bar_start, bar_end_ts=env.bar_end,
                ingest_run_id=env.ingest_run_id,
            )
            sess.commit()

            row = sess.execute(
                text(
                    "SELECT trade_count, buy_volume_ccy, sell_volume_ccy, "
                    "total_volume_ccy "
                    "FROM silver.market_trade_flow_15m "
                    "WHERE symbol = :sym AND ts = :ts"
                ),
                {"sym": env.symbol, "ts": env.bar_start},
            ).fetchone()
            self.assertEqual(row.trade_count, 30)
            # buy volume = 20 * 0.5 * 95000 = 950000
            self.assertAlmostEqual(float(row.buy_volume_ccy), 950000.0, places=1)
            # sell volume = 10 * 0.3 * 95010 = 285030
            self.assertAlmostEqual(float(row.sell_volume_ccy), 285030.0, places=1)
            self.assertAlmostEqual(
                float(row.total_volume_ccy),
                float(row.buy_volume_ccy) + float(row.sell_volume_ccy),
                places=1,
            )


class TestWhaleDetection(unittest.TestCase):
    """§8 case 12: size >= _WHALE_SIZE_FALLBACK (2.0) 的 trades 算 whale。"""

    def test_whale_count_triggers_above_threshold(self) -> None:
        env = make_env()
        with Session(env.engine) as sess:
            trades = []
            # 5 non-whale
            for i in range(5):
                trades.append({
                    "ts": env.bar_start + timedelta(seconds=i * 30),
                    "trade_id": f"s-{i}",
                    "px": Decimal("95000"),
                    "sz": Decimal("0.5"),       # below 2.0
                    "side": "buy",
                })
            # 3 whale (sz >= 2.0)
            for i in range(3):
                trades.append({
                    "ts": env.bar_start + timedelta(seconds=i * 30 + 100),
                    "trade_id": f"w-{i}",
                    "px": Decimal("95000"),
                    "sz": Decimal("5.0"),       # whale
                    "side": "buy",
                })
            insert_trades(
                sess, symbol=env.symbol, ingest_run_id=env.ingest_run_id,
                trades=trades,
            )
            build_silver_microstructure_15m(
                session=sess, symbol=env.symbol,
                bar_start_ts=env.bar_start, bar_end_ts=env.bar_end,
                ingest_run_id=env.ingest_run_id,
            )
            sess.commit()

            row = sess.execute(
                text(
                    "SELECT whale_count, whale_buy_volume_ccy, "
                    "whale_sell_volume_ccy, whale_threshold_applied "
                    "FROM silver.market_trade_flow_15m "
                    "WHERE symbol = :sym AND ts = :ts"
                ),
                {"sym": env.symbol, "ts": env.bar_start},
            ).fetchone()
            self.assertEqual(row.whale_count, 3)
            # whale buy volume = 3 * 5.0 * 95000 = 1425000
            self.assertAlmostEqual(
                float(row.whale_buy_volume_ccy), 1425000.0, places=1,
            )
            self.assertEqual(float(row.whale_sell_volume_ccy or 0), 0.0)
            self.assertIsNotNone(row.whale_threshold_applied)
            self.assertGreaterEqual(float(row.whale_threshold_applied), 2.0)


class TestTradeFlowEmptyBar(unittest.TestCase):
    """trades_no_data 且 trade_count=0 (不抛错, 不走 etl_failed)。"""

    def test_empty_trades_writes_null_row(self) -> None:
        env = make_env()
        with Session(env.engine) as sess:
            result = build_silver_microstructure_15m(
                session=sess, symbol=env.symbol,
                bar_start_ts=env.bar_start, bar_end_ts=env.bar_end,
                ingest_run_id=env.ingest_run_id,
            )
            sess.commit()
            self.assertEqual(result.tables_written["trade_flow_15m"], 1)
            self.assertIn("trades_no_data", result.quality_flags)

            row = sess.execute(
                text(
                    "SELECT trade_count, total_volume_ccy FROM silver.market_trade_flow_15m "
                    "WHERE symbol = :sym AND ts = :ts"
                ),
                {"sym": env.symbol, "ts": env.bar_start},
            ).fetchone()
            self.assertEqual(row.trade_count, 0)
            self.assertIsNone(row.total_volume_ccy)


if __name__ == "__main__":
    unittest.main()
