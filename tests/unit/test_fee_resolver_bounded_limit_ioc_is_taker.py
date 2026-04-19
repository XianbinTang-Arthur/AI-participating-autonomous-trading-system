"""P1-B step 2 cost 审计修复 regression test.

锁定契约：OKX `ordType=ioc` (Immediate-Or-Cancel) 官方定义永远付 taker fee
——订单要么立刻与簿内订单匹配 (taker)，要么取消，不会停留成为 maker。

`EffectiveFeeResolver.estimated_execution_fee_bps_decimal` 必须把
`execution_style="bounded_limit_ioc"` 归类为 taker，不能按 passive_bias /
maker_taker_bias 给 fee 打折（之前的 bug 让 expected_cost_bps 低估 ~1.4 bps，
导致 live net_edge 预估虚高）。

详见 docs/review/independent_cost_model_audit_2026_04_19.md
"""

from __future__ import annotations

import unittest
from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.services.fee_resolver import EffectiveFeeResolver


def _resolver(*, taker_bps: float = 5.0, maker_bps: float = 1.0) -> EffectiveFeeResolver:
    """构造 BTC-USDT-SWAP 衍生品永续的 fee resolver，taker 5 / maker 1 bps。"""
    settings = AATSSettings.model_validate(
        {
            "trade_cost_derivatives_taker_fee_bps": taker_bps,
            "trade_cost_derivatives_maker_fee_bps": maker_bps,
        }
    )
    return EffectiveFeeResolver(settings=settings)


class BoundedLimitIocIsTakerTests(unittest.TestCase):
    def test_bounded_limit_ioc_returns_full_taker_fee_ignoring_passive_bias(self) -> None:
        """passive_bias=1.0（最激进 passive）也不应让 bounded_limit_ioc 打折."""
        r = _resolver(taker_bps=5.0, maker_bps=1.0)
        fee = r.estimated_execution_fee_bps_decimal(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            execution_style="bounded_limit_ioc",
            passive_bias=1.0,
            maker_taker_bias=-1.0,
        )
        self.assertEqual(
            fee, Decimal("5.0"),
            "bounded_limit_ioc 必须归 taker；OKX IOC 官方永远付 taker fee",
        )

    def test_bounded_limit_ioc_with_zero_bias_still_taker(self) -> None:
        """passive_bias=0 的 bounded_limit_ioc 同样应返回 taker (不走 maker-blend)."""
        r = _resolver(taker_bps=5.0, maker_bps=1.0)
        fee = r.estimated_execution_fee_bps_decimal(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            execution_style="bounded_limit_ioc",
            passive_bias=0.0,
            maker_taker_bias=0.0,
        )
        self.assertEqual(fee, Decimal("5.0"))

    def test_real_limit_style_still_blends_maker_taker(self) -> None:
        """对照组：普通 limit (非 IOC) 仍走 maker-blend 路径."""
        r = _resolver(taker_bps=5.0, maker_bps=1.0)
        fee = r.estimated_execution_fee_bps_decimal(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            execution_style="maker",
            passive_bias=0.7,
            maker_taker_bias=-0.5,
        )
        # maker_weight = clamp(0.15 + 0.7×0.45 + 0.5×0.20, 0, 0.80) = clamp(0.565, ..) = 0.565
        # expected = 5.0 × (1 - 0.565) + 1.0 × 0.565 = 2.175 + 0.565 = 2.74
        self.assertLess(
            fee, Decimal("5.0"),
            "maker/passive style 仍然应打折",
        )
        self.assertGreater(
            fee, Decimal("1.0"),
            "不应该完全变成 maker fee (需要有 taker 混合)",
        )

    def test_market_order_remains_taker(self) -> None:
        """对照组：market 订单仍是 taker."""
        r = _resolver(taker_bps=5.0, maker_bps=1.0)
        fee = r.estimated_execution_fee_bps_decimal(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            order_type="market",
            execution_style="",
        )
        self.assertEqual(fee, Decimal("5.0"))

    def test_bounded_taker_cap_remains_taker(self) -> None:
        """对照组：bounded_taker_cap 仍是 taker（未受本次修复影响）."""
        r = _resolver(taker_bps=5.0, maker_bps=1.0)
        fee = r.estimated_execution_fee_bps_decimal(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            execution_style="bounded_taker_cap",
        )
        self.assertEqual(fee, Decimal("5.0"))


if __name__ == "__main__":
    unittest.main()
