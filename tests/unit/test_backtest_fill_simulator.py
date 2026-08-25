"""Unit tests for aats.data_platform.replay.backtest.fill_simulator.

验收清单（对应任务列出的 9 条）：
    1. test_ioc_buy_taker_fill              — cap 内 IOC buy 全量成交，fee = taker rate
    2. test_ioc_sell_slippage_direction     — IOC sell 滑点方向为负
    3. test_post_only_high_prob_fills       — 低 volume_ratio deterministic 种子成交
    4. test_post_only_over_10_pct_no_fill   — qty/bar_vol > 10% 必不成交
    5. test_post_only_fill_price_no_slippage— 成交时 avg_fill_price == bar_close
    6. test_bounded_limit_taker_fallback    — bounded_limit 保守按 taker 计价
    7. test_zero_bar_volume_post_only_no_fill
    8. test_zero_qty_no_fill
    9. test_fee_notional_is_decimal_precise — 无 float 舍入误差

附加冗余用例若干（不在必选清单，但加强回归面）：
    - test_post_only_unlucky_sample_no_fill
    - test_post_only_mid_band_fill_prob
    - test_unknown_order_type_no_fill
    - test_non_positive_close_no_fill
"""

from __future__ import annotations

import hashlib
import unittest
from decimal import Decimal

from aats.data_platform.replay.backtest.fill_simulator import (
    FillRequest,
    FillSimulator,
)


def _md5_uniform(seed: str) -> float:
    """Test helper: mirror the sampler in fill_simulator for picking seeds."""
    digest = hashlib.md5(seed.encode("utf-8")).digest()
    as_int = int.from_bytes(digest, "big")
    return float(Decimal(as_int) / (Decimal(2) ** 128))


class TestFillSimulatorIOC(unittest.TestCase):
    """IOC 分支：participation cap + taker fee + slippage 带方向。"""

    def setUp(self) -> None:
        self.sim = FillSimulator()  # 默认参数: maker 2, taker 5, slip 1

    def test_ioc_buy_taker_fill(self) -> None:
        """cap 内 IOC buy 全量成交，filled_qty = target_qty，fee 为 taker。"""
        req = FillRequest(
            order_id="ioc-buy-1",
            side="buy",
            order_type="ioc",
            target_qty=Decimal("2"),
            submitted_at_ts=1_000,
        )
        result = self.sim.simulate(req, Decimal("50000"), Decimal("1000"))

        self.assertEqual(result.fill_kind, "taker")
        self.assertEqual(result.filled_qty, Decimal("2"))
        self.assertEqual(result.fee_bps, 5.0)
        self.assertEqual(result.slippage_bps, 1.0)
        # buy slippage +1 bps: 50000 * (1 + 1/10000) = 50005
        self.assertEqual(result.avg_fill_price, Decimal("50005.0000"))
        # fee = 2 * 50005 * 5/10000 = 50.005
        self.assertEqual(result.fee_notional, Decimal("50.00500000"))

    def test_ioc_sell_slippage_direction(self) -> None:
        """IOC sell 的 avg_fill_price 应低于 bar_close（负向 slippage）。"""
        req = FillRequest(
            order_id="ioc-sell-1",
            side="sell",
            order_type="ioc",
            target_qty=Decimal("1"),
            submitted_at_ts=2_000,
        )
        bar_close = Decimal("50000")
        result = self.sim.simulate(req, bar_close, Decimal("100"))

        self.assertEqual(result.fill_kind, "taker")
        self.assertEqual(result.filled_qty, Decimal("1"))
        self.assertLess(result.avg_fill_price, bar_close)
        # sell slippage -1 bps: 50000 * (1 - 1/10000) = 49995
        self.assertEqual(result.avg_fill_price, Decimal("49995.0000"))


class TestFillSimulatorPostOnly(unittest.TestCase):
    """post_only 分支：按 volume_ratio 分段概率 + deterministic 抽样。"""

    def setUp(self) -> None:
        self.sim = FillSimulator()

    def test_post_only_high_prob_fills(self) -> None:
        """低 volume_ratio + deterministic 低抽样值应能成交（@bar_close, maker fee）。"""
        # 选 "order-4" 作为 seed，md5 抽样 ≈ 0.0003 → 远低于任何 fill_prob。
        seed = "order-4"
        self.assertLess(_md5_uniform(seed), 0.001)  # 置信该 seed 确实低

        req = FillRequest(
            order_id=seed,
            side="buy",
            order_type="post_only",
            target_qty=Decimal("0.5"),   # qty/vol = 0.5/100 = 0.5% < 1% → high prob 0.9
            submitted_at_ts=1,
        )
        result = self.sim.simulate(req, Decimal("50000"), Decimal("100"))

        self.assertEqual(result.fill_kind, "maker")
        self.assertEqual(result.filled_qty, Decimal("0.5"))
        self.assertEqual(result.avg_fill_price, Decimal("50000"))
        self.assertEqual(result.fee_bps, 2.0)

    def test_post_only_fill_price_no_slippage(self) -> None:
        """post_only 成交时 avg_fill_price 必须精确等于 bar_close（无 slippage）。"""
        req = FillRequest(
            order_id="order-4",
            side="sell",
            order_type="post_only",
            target_qty=Decimal("0.5"),
            submitted_at_ts=1,
        )
        bar_close = Decimal("12345.6789")
        result = self.sim.simulate(req, bar_close, Decimal("100"))

        self.assertEqual(result.fill_kind, "maker")
        self.assertEqual(result.avg_fill_price, bar_close)

    def test_post_only_over_10_pct_no_fill(self) -> None:
        """qty/bar_vol > 10% 必定 no_fill，与种子无关。"""
        # target 11 / volume 100 = 11% → 高于最宽松的 LOW 阈值 10%
        for seed in ("order-4", "lucky", "x", "y"):
            req = FillRequest(
                order_id=seed,
                side="buy",
                order_type="post_only",
                target_qty=Decimal("11"),
                submitted_at_ts=1,
            )
            result = self.sim.simulate(req, Decimal("50000"), Decimal("100"))
            self.assertEqual(result.fill_kind, "no_fill", msg=f"seed={seed}")
            self.assertEqual(result.filled_qty, Decimal(0))
            self.assertEqual(result.avg_fill_price, Decimal(0))
            self.assertEqual(result.fee_bps, 0.0)
            self.assertEqual(result.fee_notional, Decimal(0))

    def test_post_only_unlucky_sample_no_fill(self) -> None:
        """即使 volume_ratio 很低，若抽样值 >= fill_prob 仍不成交。"""
        # "order-3" 的 md5 抽样 ≈ 0.97 → 高于 high(0.90)
        seed = "order-3"
        self.assertGreater(_md5_uniform(seed), 0.90)

        req = FillRequest(
            order_id=seed,
            side="buy",
            order_type="post_only",
            target_qty=Decimal("0.5"),
            submitted_at_ts=1,
        )
        result = self.sim.simulate(req, Decimal("50000"), Decimal("100"))
        self.assertEqual(result.fill_kind, "no_fill")
        self.assertIn("post_only missed", result.notes)

    def test_post_only_mid_band_fill_prob(self) -> None:
        """1%~5% 带 → 使用 mid prob（0.60）；"ord-A" 抽样 ≈ 0.87 应不成交。"""
        seed = "ord-A"
        self.assertGreater(_md5_uniform(seed), 0.60)
        self.assertLess(_md5_uniform(seed), 0.90)

        # qty 3 / volume 100 = 3% → mid band
        req = FillRequest(
            order_id=seed,
            side="buy",
            order_type="post_only",
            target_qty=Decimal("3"),
            submitted_at_ts=1,
        )
        mid_result = self.sim.simulate(req, Decimal("50000"), Decimal("100"))
        self.assertEqual(mid_result.fill_kind, "no_fill")

        # 对比：若是 high band（0.5% + 同 seed），应成交
        high_req = FillRequest(
            order_id=seed,
            side="buy",
            order_type="post_only",
            target_qty=Decimal("0.5"),
            submitted_at_ts=1,
        )
        high_result = self.sim.simulate(high_req, Decimal("50000"), Decimal("100"))
        self.assertEqual(high_result.fill_kind, "maker")

    def test_zero_bar_volume_post_only_no_fill(self) -> None:
        """bar_volume <= 0 时 post_only 立刻 no_fill，notes 标注缺失流动性。"""
        req = FillRequest(
            order_id="order-4",
            side="buy",
            order_type="post_only",
            target_qty=Decimal("1"),
            submitted_at_ts=1,
        )
        result = self.sim.simulate(req, Decimal("50000"), Decimal("0"))
        self.assertEqual(result.fill_kind, "no_fill")
        self.assertEqual(result.filled_qty, Decimal(0))
        self.assertIn("non-positive bar_volume", result.notes)


class TestFillSimulatorBoundedLimit(unittest.TestCase):
    """bounded_limit 分支：participation cap 下保守按 taker fallback。"""

    def setUp(self) -> None:
        self.sim = FillSimulator()

    def test_bounded_limit_taker_fallback(self) -> None:
        """bounded_limit 应施加 taker fee 与固定不利滑点。"""
        req = FillRequest(
            order_id="bl-1",
            side="buy",
            order_type="bounded_limit",
            target_qty=Decimal("1"),
            submitted_at_ts=1,
        )
        result = self.sim.simulate(req, Decimal("50000"), Decimal("100"))

        self.assertEqual(result.fill_kind, "taker")
        self.assertEqual(result.filled_qty, Decimal("1"))
        self.assertEqual(result.avg_fill_price, Decimal("50005.0000"))
        self.assertEqual(result.fee_bps, 5.0)
        self.assertEqual(result.slippage_bps, 1.0)
        # fee = 1 * 50005 * 5/10000 = 25.0025
        self.assertEqual(result.fee_notional, Decimal("25.00250000"))

    def test_bounded_limit_sell_unfavourable_slippage(self) -> None:
        """bounded_limit sell 采用 taker fallback，价格承受负向滑点。"""
        req = FillRequest(
            order_id="bl-sell",
            side="sell",
            order_type="bounded_limit",
            target_qty=Decimal("2"),
            submitted_at_ts=1,
        )
        result = self.sim.simulate(req, Decimal("50000"), Decimal("1000"))
        self.assertEqual(result.avg_fill_price, Decimal("49995.0000"))


class TestFillSimulatorGuards(unittest.TestCase):
    """入参健壮性：target_qty<=0、bar_close<=0、未知 order_type。"""

    def setUp(self) -> None:
        self.sim = FillSimulator()

    def test_zero_qty_no_fill(self) -> None:
        """target_qty == 0 → no_fill，notes 说明 non-positive qty。"""
        req = FillRequest(
            order_id="zero-qty",
            side="buy",
            order_type="ioc",
            target_qty=Decimal("0"),
            submitted_at_ts=1,
        )
        result = self.sim.simulate(req, Decimal("50000"), Decimal("100"))
        self.assertEqual(result.fill_kind, "no_fill")
        self.assertEqual(result.filled_qty, Decimal(0))
        self.assertEqual(result.avg_fill_price, Decimal(0))
        self.assertEqual(result.fee_bps, 0.0)
        self.assertEqual(result.fee_notional, Decimal(0))
        self.assertEqual(result.notes, "non-positive qty")

    def test_negative_qty_no_fill(self) -> None:
        """负的 target_qty → no_fill（绝不抛异常）。"""
        req = FillRequest(
            order_id="neg-qty",
            side="buy",
            order_type="ioc",
            target_qty=Decimal("-1"),
            submitted_at_ts=1,
        )
        result = self.sim.simulate(req, Decimal("50000"), Decimal("100"))
        self.assertEqual(result.fill_kind, "no_fill")

    def test_non_positive_close_no_fill(self) -> None:
        """bar_close <= 0 → no_fill。"""
        req = FillRequest(
            order_id="bad-close",
            side="buy",
            order_type="ioc",
            target_qty=Decimal("1"),
            submitted_at_ts=1,
        )
        result = self.sim.simulate(req, Decimal("0"), Decimal("100"))
        self.assertEqual(result.fill_kind, "no_fill")
        self.assertIn("non-positive bar_close_price", result.notes)

    def test_unknown_order_type_no_fill(self) -> None:
        """传入不识别的 order_type 也不应抛异常。"""
        # 绕过 Literal 类型检查构造一个 "非法" 请求
        req = FillRequest(
            order_id="unknown",
            side="buy",
            order_type="market",  # type: ignore[arg-type]
            target_qty=Decimal("1"),
            submitted_at_ts=1,
        )
        result = self.sim.simulate(req, Decimal("50000"), Decimal("100"))
        self.assertEqual(result.fill_kind, "no_fill")
        self.assertIn("unknown order_type", result.notes)

    def test_non_finite_market_inputs_return_no_fill_without_decimal_error(self) -> None:
        req = FillRequest(
            order_id="non-finite-input",
            side="buy",
            order_type="ioc",
            target_qty=Decimal("1"),
            submitted_at_ts=1,
        )
        for price, volume in (
            (Decimal("NaN"), Decimal("1000")),
            (Decimal("Infinity"), Decimal("1000")),
            (Decimal("50000"), Decimal("NaN")),
            (Decimal("50000"), Decimal("Infinity")),
        ):
            with self.subTest(price=price, volume=volume):
                result = self.sim.simulate(req, price, volume)
                self.assertEqual(result.fill_kind, "no_fill")
                self.assertEqual(result.filled_qty, Decimal("0"))

    def test_non_finite_or_out_of_range_model_config_is_rejected(self) -> None:
        invalid_configs = (
            {"max_volume_participation": Decimal("NaN")},
            {"max_volume_participation": Decimal("Infinity")},
            {"ioc_slippage_bps": float("nan")},
            {"taker_fee_bps": float("inf")},
            {"post_only_fill_prob_high": 1.01},
        )
        for config in invalid_configs:
            with self.subTest(config=config):
                with self.assertRaises(ValueError):
                    FillSimulator(**config)


class TestFillSimulatorDecimalPrecision(unittest.TestCase):
    """fee 计算必须保持 Decimal 精度，无 float 舍入误差。"""

    def setUp(self) -> None:
        self.sim = FillSimulator()

    def test_fee_notional_is_decimal_precise(self) -> None:
        """校验 fee_notional 等于显式 Decimal 算的精确值，而非 float 近似。"""
        req = FillRequest(
            order_id="precise-1",
            side="buy",
            order_type="bounded_limit",
            target_qty=Decimal("0.3"),   # 0.3 float 有二进制舍入
            submitted_at_ts=1,
        )
        bar_close = Decimal("0.1")       # 0.1 float 亦有二进制舍入
        result = self.sim.simulate(req, bar_close, Decimal("100"))

        expected_avg = Decimal("0.1") * (
            Decimal(1) + Decimal("1") / Decimal("10000")
        )
        expected = (
            Decimal("0.3")
            * expected_avg
            * Decimal("5")
            / Decimal("10000")
        )
        self.assertEqual(result.fee_notional, expected)
        # 额外断言：返回类型是 Decimal，不是 float
        self.assertIsInstance(result.fee_notional, Decimal)
        self.assertIsInstance(result.avg_fill_price, Decimal)
        self.assertIsInstance(result.filled_qty, Decimal)

        # 反向证明：把中间量走 float 会产生不同的 (通常是近似) 值
        as_float = 0.3 * 0.1 * 3.5 / 10000
        # expected 由精确 Decimal 价格与费率计算；float 路径只用于反向说明。
        # 我们只断言 "Decimal 结果不等于 float 直算转 Decimal"（只要其一不成立，
        # 说明 Decimal 路径更精确；若相等也不是 bug，只是恰好对齐）。此处用
        # 绝对等价当成 sanity check：两者可能相等也可能不等，但 result 必须
        # 等于 Decimal 精确表达式。
        del as_float  # 只用于文档说明，不断言

    def test_ioc_fee_notional_decimal_precision(self) -> None:
        """IOC 分支 fee_notional 同样保持 Decimal 精度。"""
        req = FillRequest(
            order_id="precise-ioc",
            side="buy",
            order_type="ioc",
            target_qty=Decimal("0.3"),
            submitted_at_ts=1,
        )
        bar_close = Decimal("0.1")
        result = self.sim.simulate(req, bar_close, Decimal("100"))

        # avg = 0.1 * (1 + 1/10000) = 0.1 * 1.0001 = 0.10001
        expected_avg = Decimal("0.1") * (Decimal(1) + Decimal("1") / Decimal("10000"))
        self.assertEqual(result.avg_fill_price, expected_avg)

        # fee = qty * avg * 5/10000
        expected_fee = Decimal("0.3") * expected_avg * Decimal("5") / Decimal("10000")
        self.assertEqual(result.fee_notional, expected_fee)


class TestFillSimulatorReproducibility(unittest.TestCase):
    """同种子同 order_id → 同 outcome（reproducibility）。"""

    def setUp(self) -> None:
        self.sim = FillSimulator()

    def test_same_seed_same_outcome(self) -> None:
        """两次相同调用给出完全一致的 FillResult。"""
        req = FillRequest(
            order_id="repro-1",
            side="buy",
            order_type="post_only",
            target_qty=Decimal("0.5"),
            submitted_at_ts=1,
        )
        r1 = self.sim.simulate(req, Decimal("50000"), Decimal("100"))
        r2 = self.sim.simulate(req, Decimal("50000"), Decimal("100"))
        self.assertEqual(r1, r2)

    def test_explicit_seed_overrides_order_id(self) -> None:
        """显式 rng_seed 应覆盖 order_id 的抽样行为。"""
        req = FillRequest(
            order_id="order-3",   # 该 id md5 ≈ 0.97，默认会 no_fill
            side="buy",
            order_type="post_only",
            target_qty=Decimal("0.5"),
            submitted_at_ts=1,
        )
        default_result = self.sim.simulate(req, Decimal("50000"), Decimal("100"))
        self.assertEqual(default_result.fill_kind, "no_fill")

        # 用 "order-4" 种子 md5 ≈ 0.0003 → 必成交
        lucky_result = self.sim.simulate(
            req, Decimal("50000"), Decimal("100"), rng_seed="order-4"
        )
        self.assertEqual(lucky_result.fill_kind, "maker")


if __name__ == "__main__":
    unittest.main()
