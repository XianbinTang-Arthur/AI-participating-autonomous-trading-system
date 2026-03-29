from __future__ import annotations

import unittest
from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.services.fee_resolver import EffectiveFeeResolver
from aats.services.trade_costs import TradeCostService


class TestTradeCostService(unittest.TestCase):
    def test_single_leg_spot_entry_uses_spot_fee_spread_and_slippage_defaults(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trade_cost_spot_taker_fee_bps": 10.0,
                "trade_cost_spot_spread_bps": 1.5,
                "trade_cost_spot_slippage_bps": 2.0,
            }
        )
        service = TradeCostService(settings=settings)

        estimate = service.estimate_single_leg_entry(
            model_name="spot_dca",
            symbol="BTC-USDT",
            product_type="spot",
            margin_mode="cash",
            include_spread=True,
        )

        self.assertEqual(estimate.ideal_total_cost_bps, Decimal("10"))
        self.assertEqual(estimate.executable_total_drag_bps, Decimal("13.5"))

    def test_single_leg_derivatives_entry_uses_derivatives_fee_and_funding_proxy(self) -> None:
        class _FundingAwareAccount:
            @staticmethod
            def funding_fee_bps_proxy(symbol: str | None = None) -> Decimal:
                _ = symbol
                return Decimal("7")

        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "trade_cost_derivatives_taker_fee_bps": 5.0,
                "trade_cost_derivatives_slippage_bps": 1.5,
            }
        )
        service = TradeCostService(settings=settings, account_service=_FundingAwareAccount())

        estimate = service.estimate_single_leg_entry(
            model_name="derivatives_directional",
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            include_funding=True,
        )

        self.assertEqual(estimate.ideal_total_cost_bps, Decimal("12"))
        self.assertEqual(estimate.executable_total_drag_bps, Decimal("13.5"))

    def test_fee_resolver_distinguishes_spot_and_derivatives_defaults(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trade_cost_spot_maker_fee_bps": 8.0,
                "trade_cost_spot_taker_fee_bps": 10.0,
                "trade_cost_derivatives_maker_fee_bps": 2.0,
                "trade_cost_derivatives_taker_fee_bps": 5.0,
            }
        )
        resolver = EffectiveFeeResolver(settings=settings)

        self.assertEqual(resolver.maker_fee_bps_decimal(symbol="BTC-USDT", product_type="spot"), Decimal("8"))
        self.assertEqual(resolver.taker_fee_bps_decimal(symbol="BTC-USDT", product_type="spot"), Decimal("10"))
        self.assertEqual(
            resolver.maker_fee_bps_decimal(symbol="BTC-USDT-SWAP", product_type="derivatives"),
            Decimal("2"),
        )
        self.assertEqual(
            resolver.taker_fee_bps_decimal(symbol="BTC-USDT-SWAP", product_type="derivatives"),
            Decimal("5"),
        )

    def test_fee_resolver_preserves_negative_maker_rebate_from_account_schedule(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trade_cost_derivatives_maker_fee_bps": 2.0,
                "trade_cost_derivatives_taker_fee_bps": 5.0,
            }
        )

        class _RebateAccount:
            @staticmethod
            def effective_maker_fee_bps(*, symbol: str | None = None) -> Decimal:
                _ = symbol
                return Decimal("-8")

            @staticmethod
            def effective_taker_fee_bps(*, symbol: str | None = None) -> Decimal:
                _ = symbol
                return Decimal("5")

        resolver = EffectiveFeeResolver(settings=settings, account_service=_RebateAccount())

        self.assertEqual(
            resolver.maker_fee_bps_decimal(symbol="BTC-USDT-SWAP", product_type="derivatives"),
            Decimal("-8"),
        )
        self.assertEqual(
            resolver.estimated_execution_fee_bps_decimal(
                symbol="BTC-USDT-SWAP",
                product_type="derivatives",
                margin_mode="cross",
                execution_style="maker",
                order_type="limit",
                passive_bias=Decimal("1"),
                maker_taker_bias=Decimal("-1"),
            ),
            Decimal("-5.4"),
        )


if __name__ == "__main__":
    unittest.main()
