"""OpenInterestState 契约测试 (P1.6).

锁定:
  1. 同 ts update 幂等
  2. 旧 ts update 拒绝
  3. 负 OI 拒绝
  4. 样本 < ema_period → indicators.ready = False
  5. oi_delta = (now - ema) / ema 正确
  6. EMA 按 alpha=2/(N+1) 增量推进
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from aats.services.feature_engine.oi_state import OpenInterestState


def _ts(n: int) -> datetime:
    base = datetime(2026, 4, 19, 0, 0, tzinfo=timezone.utc)
    return base + timedelta(seconds=3 * n)  # OKX 每 3s 推一次


class OpenInterestStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = OpenInterestState(
            symbol="BTC-USDT-SWAP",
            max_snapshots=60,
            ema_period=20,
        )

    def test_idempotent_on_same_ts(self) -> None:
        self.state.update(1_000_000.0, ts=_ts(0))
        first_samples = self.state.samples_count()
        self.state.update(1_100_000.0, ts=_ts(0))  # 同 ts 覆盖
        self.state.update(1_200_000.0, ts=_ts(0))
        self.assertEqual(self.state.samples_count(), first_samples)

    def test_rejects_older_ts(self) -> None:
        self.state.update(1_000_000.0, ts=_ts(5))
        count_before = self.state.samples_count()
        self.state.update(900_000.0, ts=_ts(2))
        self.assertEqual(self.state.samples_count(), count_before)

    def test_rejects_negative_oi(self) -> None:
        self.state.update(-1.0, ts=_ts(0))
        self.assertEqual(self.state.samples_count(), 0)

    def test_not_ready_before_ema_period(self) -> None:
        for i in range(10):
            self.state.update(1_000_000.0 + i * 100, ts=_ts(i))
        self.assertFalse(self.state.indicators().ready)

    def test_ready_after_ema_period_samples(self) -> None:
        for i in range(25):
            self.state.update(1_000_000.0, ts=_ts(i))
        ind = self.state.indicators()
        self.assertTrue(ind.ready)
        # 平稳 OI → ema ≈ oi_now，delta ≈ 0
        assert ind.oi_delta is not None
        self.assertLess(abs(ind.oi_delta), 0.001)

    def test_delta_computed_correctly_against_ema(self) -> None:
        # 喂 20 根 1_000_000，EMA 稳定在 1_000_000
        for i in range(20):
            self.state.update(1_000_000.0, ts=_ts(i))
        # 第 21 根跳到 1_100_000 (+10%)
        self.state.update(1_100_000.0, ts=_ts(20))
        ind = self.state.indicators()
        assert ind.oi_delta is not None
        # EMA 新值 = 2/21 × 1.1M + 19/21 × 1M ≈ 1.00952M
        # delta = (1.1M - 1.00952M) / 1.00952M ≈ 0.0896 (~9%)
        self.assertGreater(ind.oi_delta, 0.05)
        self.assertLess(ind.oi_delta, 0.12)


if __name__ == "__main__":
    unittest.main()
