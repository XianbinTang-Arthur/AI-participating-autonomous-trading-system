from __future__ import annotations

from datetime import datetime, timezone
import unittest
from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.schemas.market import MarketSnapshot
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

    def test_derivatives_lifecycle_entry_can_include_expected_close_fee(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "trade_cost_derivatives_taker_fee_bps": 5.0,
                "trade_cost_derivatives_spread_bps": 0.5,
                "trade_cost_derivatives_slippage_bps": 1.0,
            }
        )
        service = TradeCostService(settings=settings)

        estimate = service.estimate_single_leg_entry(
            model_name="directional_lifecycle_entry",
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            include_spread=True,
            include_close_fee=True,
        )

        self.assertEqual(estimate.ideal_open_fee_bps, Decimal("5"))
        self.assertEqual(estimate.ideal_close_fee_bps, Decimal("5"))
        self.assertEqual(estimate.ideal_total_cost_bps, Decimal("10"))
        self.assertEqual(estimate.executable_total_drag_bps, Decimal("11.5"))
        self.assertIn("close_fee_trade_cost_service", estimate.cost_source_flags)

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

    def test_size_aware_cost_model_increases_drag_for_larger_orders(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trade_cost_derivatives_taker_fee_bps": 5.0,
                "trade_cost_derivatives_slippage_bps": 1.5,
            }
        )
        snapshot = MarketSnapshot(
            symbol="BTC-USDT-SWAP",
            exchange="OKX",
            snapshot_ts=datetime.now(timezone.utc),
            best_bid=Decimal("100"),
            best_ask=Decimal("101"),
            last_price=Decimal("100.5"),
            bid_size=Decimal("0.6"),
            ask_size=Decimal("0.5"),
            volume_24h=Decimal("1000000"),
            kline_15m={"open": Decimal("99"), "high": Decimal("102"), "low": Decimal("98"), "close": Decimal("100.5")},
            kline_1h={"open": Decimal("97"), "high": Decimal("103"), "low": Decimal("96"), "close": Decimal("100.5")},
            orderbook_depth={
                "bids": [
                    {"price": Decimal("100"), "size": Decimal("0.6")},
                    {"price": Decimal("99.5"), "size": Decimal("0.8")},
                ],
                "asks": [
                    {"price": Decimal("101"), "size": Decimal("0.5")},
                    {"price": Decimal("101.5"), "size": Decimal("0.6")},
                    {"price": Decimal("102"), "size": Decimal("0.9")},
                ],
            },
        )
        service = TradeCostService(settings=settings)

        small = service.estimate_single_leg_entry(
            model_name="independent_short_book",
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            execution_style="taker",
            order_type="market",
            side="buy",
            quantity=Decimal("0.10"),
            market_snapshot=snapshot,
            expected_slippage_bps=Decimal("1.5"),
        )
        large = service.estimate_single_leg_entry(
            model_name="independent_short_book",
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            execution_style="taker",
            order_type="market",
            side="buy",
            quantity=Decimal("1.20"),
            market_snapshot=snapshot,
            expected_slippage_bps=Decimal("1.5"),
        )

        self.assertIn("size_impact_bps", small.execution_drag_components_bps)
        self.assertIn("size_impact_bps", large.execution_drag_components_bps)
        self.assertIn("quoted_depth_notional", large.execution_context)
        self.assertIn("depth_consumption_ratio", large.execution_context)
        self.assertGreater(
            large.execution_context["depth_consumption_ratio"],
            small.execution_context["depth_consumption_ratio"],
        )
        self.assertGreater(
            large.execution_drag_components_bps["size_impact_bps"],
            small.execution_drag_components_bps["size_impact_bps"],
        )
        self.assertGreater(large.executable_total_drag_bps, small.executable_total_drag_bps)

    def test_size_aware_cost_model_requires_explicit_side_for_depth_consumption(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trade_cost_derivatives_taker_fee_bps": 5.0,
                "trade_cost_derivatives_slippage_bps": 1.5,
            }
        )
        snapshot = MarketSnapshot(
            symbol="BTC-USDT-SWAP",
            exchange="OKX",
            snapshot_ts=datetime.now(timezone.utc),
            best_bid=Decimal("100"),
            best_ask=Decimal("101"),
            last_price=Decimal("100.5"),
            bid_size=Decimal("0.6"),
            ask_size=Decimal("0.5"),
            volume_24h=Decimal("1000000"),
            kline_15m={"open": Decimal("99"), "high": Decimal("102"), "low": Decimal("98"), "close": Decimal("100.5")},
            kline_1h={"open": Decimal("97"), "high": Decimal("103"), "low": Decimal("96"), "close": Decimal("100.5")},
            orderbook_depth={
                "bids": [{"price": Decimal("100"), "size": Decimal("0.6")}],
                "asks": [{"price": Decimal("101"), "size": Decimal("0.5")}],
            },
        )
        service = TradeCostService(settings=settings)

        estimate = service.estimate_single_leg_entry(
            model_name="independent_unknown_leg",
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            execution_style="taker",
            order_type="market",
            quantity=Decimal("1.20"),
            market_snapshot=snapshot,
            expected_slippage_bps=Decimal("1.5"),
        )

        self.assertNotIn("size_impact_bps", estimate.execution_drag_components_bps)
        self.assertNotIn("depth_consumption_ratio", estimate.execution_context)
        self.assertIn("projected_notional", estimate.execution_context)


if __name__ == "__main__":
    unittest.main()
