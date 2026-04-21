"""2026-04-21 A2 · ApplicationRuntime._jittered_sleep_seconds 测试。

## 背景

固定间隔 background loop（`_flush_stream_cache_loop` 5s、`_refresh_account_loop`
60s 等）在 4 进程同一秒 deploy 启动后，会永远在同一时间窗触发，造成协同
DB 峰值。helper 加 [0, base * 10%) 的随机 jitter 让几次迭代内自然
decorrelate。

## 本文件保障

- helper 返回值始终 >= base_seconds（不会比基准更短；只会"推后一点"）
- helper 返回值始终 < base_seconds * (1 + jitter_fraction)
- jitter=0 时退化为 identity（给希望"精确间隔"的调用方提供 escape hatch）
- base_seconds <= 0 原样返回（防御性，不应该被调用）

未来若有人"简化"把 jitter 去掉或改 constant，本测试立刻红，防静默
regression。
"""
from __future__ import annotations

import statistics
import unittest

from aats.bootstrap.config import ApplicationRuntime


class TestJitteredSleepSeconds(unittest.TestCase):
    def test_returns_base_seconds_as_minimum(self) -> None:
        """1000 次采样的最小值应接近 base（允许浮点误差）。"""
        samples = [
            ApplicationRuntime._jittered_sleep_seconds(10.0)
            for _ in range(1000)
        ]
        self.assertGreaterEqual(min(samples), 10.0 - 1e-9)

    def test_does_not_exceed_base_plus_jitter_fraction(self) -> None:
        """max < base * (1 + jitter_fraction)。"""
        samples = [
            ApplicationRuntime._jittered_sleep_seconds(10.0, 0.1)
            for _ in range(10000)
        ]
        self.assertLess(max(samples), 10.0 * 1.1 + 1e-6)

    def test_jitter_actually_varies(self) -> None:
        """1000 次采样的标准差应该显著 > 0，证明 jitter 真的在起作用。"""
        samples = [
            ApplicationRuntime._jittered_sleep_seconds(10.0)
            for _ in range(1000)
        ]
        stdev = statistics.stdev(samples)
        # 10% uniform jitter on 10s 的理论 stdev ≈ 0.289s
        self.assertGreater(
            stdev,
            0.1,
            f"stdev={stdev} 太低 —— jitter 没起作用 / 被某处 optimize 没了",
        )

    def test_jitter_zero_is_identity(self) -> None:
        """jitter_fraction=0 时永远返回 base。"""
        for base in (5.0, 10.0, 60.0, 0.5):
            self.assertEqual(
                ApplicationRuntime._jittered_sleep_seconds(base, 0.0),
                base,
            )

    def test_custom_jitter_fraction(self) -> None:
        """jitter_fraction=0.5 允许最大 50% 超出。"""
        samples = [
            ApplicationRuntime._jittered_sleep_seconds(10.0, 0.5)
            for _ in range(10000)
        ]
        self.assertLess(max(samples), 10.0 * 1.5 + 1e-6)
        # 大 jitter 更容易命中较高值
        high_samples = [s for s in samples if s > 12.0]
        self.assertGreater(
            len(high_samples),
            500,
            "50% jitter 应该有相当比例 > 12s，否则 jitter 没正确 scale",
        )

    def test_nonpositive_base_returns_unchanged(self) -> None:
        """base <= 0 时原样返回，防御性（正常调用方应确保正值）。"""
        self.assertEqual(ApplicationRuntime._jittered_sleep_seconds(0.0), 0.0)
        self.assertEqual(ApplicationRuntime._jittered_sleep_seconds(-1.0), -1.0)


if __name__ == "__main__":
    unittest.main()
