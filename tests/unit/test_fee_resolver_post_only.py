"""post_only_with_timeout_fallback fee 分支 — 单元测试.

实施依据: docs/design/post_only_maker_exit_mode_2026_04_21.md §3.6

锁定契约:
1. post_only execution_style 或 order_type 走 maker × fill_rate + taker × (1 − fill_rate)
2. fill_rate 从 strategy_hedge_independent_post_only_expected_fill_rate 读取, clamp 到 [0, 1]
3. **H2 regression guard**: bounded_limit_ioc 仍归 taker，新增分支不得回退 H2

详见 docs/governance/frozen_parameters.md §2.3 和
docs/review/independent_cost_model_audit_2026_04_19.md
"""

from __future__ import annotations

import unittest
from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.services.fee_resolver import EffectiveFeeResolver


def _resolver(
    *,
    taker_bps: float = 5.0,
    maker_bps: float = 2.0,
    fill_rate: float = 0.3,
) -> EffectiveFeeResolver:
    """BTC-USDT-SWAP 永续 fee resolver；默认 taker 5 / maker 2 / fill_rate 0.3."""
    settings = AATSSettings.model_validate(
        {
            "trade_cost_derivatives_taker_fee_bps": taker_bps,
            "trade_cost_derivatives_maker_fee_bps": maker_bps,
            "strategy_hedge_independent_post_only_expected_fill_rate": fill_rate,
        }
    )
    return EffectiveFeeResolver(settings=settings)


class PostOnlyFeeBranchTests(unittest.TestCase):
    """新 post_only 分支的加权 fee 行为."""

    def test_post_only_style_blends_maker_and_taker_at_default_fill_rate(self) -> None:
        """execution_style=post_only + fill_rate=0.3 → 2.0×0.3 + 5.0×0.7 = 4.1 bps."""
        r = _resolver(taker_bps=5.0, maker_bps=2.0, fill_rate=0.3)
        fee = r.estimated_execution_fee_bps_decimal(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            execution_style="post_only",
        )
        self.assertEqual(fee, Decimal("4.1"))

    def test_post_only_order_type_also_triggers_branch(self) -> None:
        """order_type=post_only 和 execution_style=post_only 等价都走加权."""
        r = _resolver(taker_bps=5.0, maker_bps=2.0, fill_rate=0.3)
        fee = r.estimated_execution_fee_bps_decimal(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            order_type="post_only",
            execution_style="",
        )
        self.assertEqual(fee, Decimal("4.1"))

    def test_post_only_fill_rate_zero_degenerates_to_taker(self) -> None:
        """fill_rate=0 等价于"永远 fallback"，fee 应回到纯 taker."""
        r = _resolver(taker_bps=5.0, maker_bps=2.0, fill_rate=0.0)
        fee = r.estimated_execution_fee_bps_decimal(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            execution_style="post_only",
        )
        self.assertEqual(fee, Decimal("5.0"))

    def test_post_only_fill_rate_one_is_pure_maker(self) -> None:
        """fill_rate=1 等价于"永远成 maker"，fee 应为纯 maker."""
        r = _resolver(taker_bps=5.0, maker_bps=2.0, fill_rate=1.0)
        fee = r.estimated_execution_fee_bps_decimal(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            execution_style="post_only",
        )
        self.assertEqual(fee, Decimal("2.0"))

    def test_post_only_fill_rate_clamped_below_zero(self) -> None:
        """fill_rate 负数被 clamp 到 0 (防御错配置)."""
        r = _resolver(taker_bps=5.0, maker_bps=2.0, fill_rate=-0.5)
        fee = r.estimated_execution_fee_bps_decimal(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            execution_style="post_only",
        )
        self.assertEqual(fee, Decimal("5.0"))

    def test_post_only_fill_rate_clamped_above_one(self) -> None:
        """fill_rate > 1 被 clamp 到 1 (防御错配置)."""
        r = _resolver(taker_bps=5.0, maker_bps=2.0, fill_rate=2.5)
        fee = r.estimated_execution_fee_bps_decimal(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            execution_style="post_only",
        )
        self.assertEqual(fee, Decimal("2.0"))

    def test_post_only_with_conservative_fill_rate_still_above_zero_savings(self) -> None:
        """fill_rate=0.3 (§4.3 默认) 省 0.9 bps (远小于 3.0 bps 理论上限)."""
        r = _resolver(taker_bps=5.0, maker_bps=2.0, fill_rate=0.3)
        fee = r.estimated_execution_fee_bps_decimal(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            execution_style="post_only",
        )
        saving = Decimal("5.0") - fee
        self.assertEqual(saving, Decimal("0.9"))


class PostOnlyDoesNotRegressH2Tests(unittest.TestCase):
    """**核心 H2 regression guard**: 新增 post_only 分支不得影响 bounded_limit_ioc."""

    def test_bounded_limit_ioc_still_taker_when_post_only_config_present(self) -> None:
        """即使 post_only 配置在场，bounded_limit_ioc 必须仍归 taker."""
        r = _resolver(taker_bps=5.0, maker_bps=2.0, fill_rate=0.3)
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
            "H2 保护：bounded_limit_ioc 永远归 taker，不受 post_only 分支影响",
        )

    def test_market_order_still_taker(self) -> None:
        """对照组：market order 不受 post_only 分支影响."""
        r = _resolver(taker_bps=5.0, maker_bps=2.0, fill_rate=0.3)
        fee = r.estimated_execution_fee_bps_decimal(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            order_type="market",
        )
        self.assertEqual(fee, Decimal("5.0"))

    def test_maker_style_still_blends(self) -> None:
        """对照组：普通 maker style 仍走原 maker-blend 分支，不走 post_only 分支."""
        r = _resolver(taker_bps=5.0, maker_bps=2.0, fill_rate=0.3)
        fee_maker = r.estimated_execution_fee_bps_decimal(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            execution_style="maker",
            passive_bias=0.7,
        )
        fee_post_only = r.estimated_execution_fee_bps_decimal(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            execution_style="post_only",
        )
        # 两个分支必须产生不同结果（maker-blend 用 passive_bias 权重, post_only 用 fill_rate）
        self.assertNotEqual(fee_maker, fee_post_only)


class PostOnlyMissingConfigFallbackTests(unittest.TestCase):
    """边界: 如果 settings 缺 post_only_expected_fill_rate 配置，应安全退化."""

    def test_missing_fill_rate_attr_defaults_to_taker(self) -> None:
        """settings 没这个属性时 getattr 返回 None → fill_rate=0 → 纯 taker."""
        settings = AATSSettings.model_validate(
            {
                "trade_cost_derivatives_taker_fee_bps": 5.0,
                "trade_cost_derivatives_maker_fee_bps": 2.0,
            }
        )
        # 为了模拟 "没这个属性" 的情况, 创建一个子对象假装缺失
        class _StubSettings:
            def __init__(self, inner: AATSSettings) -> None:
                # 透传除 post_only_expected_fill_rate 以外的所有属性
                self._inner = inner

            def __getattr__(self, name: str) -> object:
                if name == "strategy_hedge_independent_post_only_expected_fill_rate":
                    raise AttributeError(name)
                return getattr(self._inner, name)

        stubbed = _StubSettings(settings)
        r = EffectiveFeeResolver(settings=stubbed)  # type: ignore[arg-type]
        fee = r.estimated_execution_fee_bps_decimal(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            execution_style="post_only",
        )
        # getattr 的 default=None → to_decimal(0) → fill_rate=0 → 纯 taker
        self.assertEqual(fee, Decimal("5.0"))


if __name__ == "__main__":
    unittest.main()
