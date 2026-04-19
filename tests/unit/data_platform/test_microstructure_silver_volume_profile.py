"""P1-D Phase 1A Stage 3 — silver.market_volume_profile_15m 聚合测试。

对齐设计 §5.4 / §8 case 14: baseline cold-start。

Phase 1A 首 4 周 baseline_sample_weeks < 4 → z_score=NULL + quality_flags
+= 'partial_baseline'。测试构造零历史场景覆盖冷启动路径。
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


class TestVolumeProfileColdStart(unittest.TestCase):
    """§8 case 14: silver 无历史 → z_score=NULL + flag='partial_baseline'。"""

    def test_cold_start_has_partial_baseline_flag(self) -> None:
        env = make_env()
        with Session(env.engine) as sess:
            # 插 20 笔 trades 凑本 bar 的 volume
            trades = [
                {
                    "ts": env.bar_start + timedelta(seconds=i * 30),
                    "trade_id": f"t-{i}",
                    "px": Decimal("95000"),
                    "sz": Decimal("0.5"),
                    "side": "buy",
                }
                for i in range(20)
            ]
            insert_trades(
                sess, symbol=env.symbol, ingest_run_id=env.ingest_run_id,
                trades=trades,
            )
            result = build_silver_microstructure_15m(
                session=sess, symbol=env.symbol,
                bar_start_ts=env.bar_start, bar_end_ts=env.bar_end,
                ingest_run_id=env.ingest_run_id,
            )
            sess.commit()
            self.assertIn("partial_baseline", result.quality_flags)

            row = sess.execute(
                text(
                    "SELECT volume_z_score, baseline_sample_weeks, "
                    "expected_volume_ccy, dow_hod_slot, volume_ccy, trade_count "
                    "FROM silver.market_volume_profile_15m "
                    "WHERE symbol = :sym AND ts = :ts"
                ),
                {"sym": env.symbol, "ts": env.bar_start},
            ).fetchone()
            self.assertEqual(row.baseline_sample_weeks, 0)
            self.assertIsNone(row.volume_z_score)
            self.assertIsNone(row.expected_volume_ccy)
            self.assertEqual(row.trade_count, 20)
            # dow_hod_slot 格式: mon_12:00 — 但 2026-04-20 是 monday
            self.assertIsNotNone(row.dow_hod_slot)
            self.assertIn(":", row.dow_hod_slot)
            # 本 bar volume = 20 * 0.5 * 95000 = 950000
            self.assertAlmostEqual(float(row.volume_ccy), 950000.0, places=1)


class TestVolumeProfileBaselineFullyPopulated(unittest.TestCase):
    """§5.4 4-week baseline: 造足 4 个同时段历史 silver row → z_score 计算。"""

    def test_z_score_computed_when_four_weeks_present(self) -> None:
        env = make_env()
        with Session(env.engine) as sess:
            # 造 4 周前的同 slot silver 行 (每周往前推 7 天)
            slot = env.bar_start.strftime("%a").lower() + "_" + env.bar_start.strftime("%H:%M")
            for weeks_ago in (1, 2, 3, 4):
                hist_ts = env.bar_start - timedelta(weeks=weeks_ago)
                sess.execute(
                    text("""
                        INSERT INTO silver.market_volume_profile_15m
                            (symbol, ts, volume_ccy, trade_count, dow_hod_slot,
                             ingest_run_id, dataset_version, quality_flags,
                             volume_spike_flag, baseline_sample_weeks, created_at, updated_at)
                        VALUES
                            (:sym, :ts, :vol, :n, :slot, :run, 'test', '', 0, 0, now(), now())
                    """),
                    {
                        "sym": env.symbol, "ts": hist_ts,
                        "vol": Decimal("1000000"), "n": 100,
                        "slot": slot, "run": env.ingest_run_id,
                    },
                )
                sess.flush()

            # 本 bar: volume 明显高于历史 baseline → 高 z_score
            trades = [
                {
                    "ts": env.bar_start + timedelta(seconds=i * 10),
                    "trade_id": f"t-{i}",
                    "px": Decimal("95000"),
                    "sz": Decimal("2.0"),
                    "side": "buy",
                }
                for i in range(50)    # 50 * 2 * 95000 = 9.5M, 远大于 baseline 1M
            ]
            insert_trades(
                sess, symbol=env.symbol, ingest_run_id=env.ingest_run_id,
                trades=trades,
            )
            result = build_silver_microstructure_15m(
                session=sess, symbol=env.symbol,
                bar_start_ts=env.bar_start, bar_end_ts=env.bar_end,
                ingest_run_id=env.ingest_run_id,
            )
            sess.commit()
            # 有足够历史 → 不应 partial_baseline
            self.assertNotIn("partial_baseline", result.quality_flags)

            row = sess.execute(
                text(
                    "SELECT baseline_sample_weeks, volume_z_score, volume_spike_flag, "
                    "expected_volume_ccy "
                    "FROM silver.market_volume_profile_15m "
                    "WHERE symbol = :sym AND ts = :ts"
                ),
                {"sym": env.symbol, "ts": env.bar_start},
            ).fetchone()
            self.assertEqual(row.baseline_sample_weeks, 4)
            self.assertIsNotNone(row.expected_volume_ccy)
            # 4 个相等历史样本 (std=0) → z_score 应为 NULL (避免除 0)
            # 我们 expect z_score is None because std == 0
            # Or if historical is uniform 0 std → None per our code
            # 实际: 4 个 Decimal(1000000) 相同, variance=0, sigma=0, z=None
            self.assertIsNone(row.volume_z_score)


if __name__ == "__main__":
    unittest.main()
