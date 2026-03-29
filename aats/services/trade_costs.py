from __future__ import annotations

from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.services.fee_resolver import EffectiveFeeResolver
from aats.services.portfolio_service.decimals import to_decimal
from aats.services.trade_drag import TradeDragCalculator, TradeDragEstimate, TradeDragProfile


class TradeCostService:
    def __init__(
        self,
        *,
        settings: AATSSettings,
        fee_resolver: EffectiveFeeResolver | None = None,
        account_service=None,
    ) -> None:
        self.settings = settings
        self.fee_resolver = fee_resolver or EffectiveFeeResolver(
            settings=settings,
            account_service=account_service,
        )
        self.drag_calculator = TradeDragCalculator()

    def estimated_execution_fee_bps_decimal(
        self,
        *,
        symbol: str | None = None,
        product_type: str | None = None,
        margin_mode: str | None = None,
        execution_style: str | None = None,
        order_type: str | None = None,
        passive_bias: Decimal | float | int | str | None = None,
        maker_taker_bias: Decimal | float | int | str | None = None,
    ) -> Decimal:
        decimal_method = getattr(self.fee_resolver, "estimated_execution_fee_bps_decimal", None)
        if callable(decimal_method):
            return decimal_method(
                symbol=symbol,
                product_type=product_type,
                margin_mode=margin_mode,
                execution_style=execution_style,
                order_type=order_type,
                passive_bias=passive_bias,
                maker_taker_bias=maker_taker_bias,
            )
        float_method = getattr(self.fee_resolver, "estimated_execution_fee_bps", None)
        if callable(float_method):
            return to_decimal(
                float_method(
                    symbol=symbol,
                    execution_style=execution_style,
                    order_type=order_type,
                    passive_bias=passive_bias,
                    maker_taker_bias=maker_taker_bias,
                )
            )
        return Decimal("0")

    def default_spread_bps(
        self,
        *,
        symbol: str | None = None,
        product_type: str | None = None,
        margin_mode: str | None = None,
    ) -> Decimal:
        kind = self.fee_resolver.instrument_kind(
            symbol=symbol,
            product_type=product_type,
            margin_mode=margin_mode,
        )
        if kind == "spot":
            return max(to_decimal(self.settings.trade_cost_spot_spread_bps), Decimal("0"))
        if kind == "margin_spot":
            return max(to_decimal(self.settings.trade_cost_margin_spread_bps), Decimal("0"))
        return max(to_decimal(self.settings.trade_cost_derivatives_spread_bps), Decimal("0"))

    def default_slippage_bps(
        self,
        *,
        symbol: str | None = None,
        product_type: str | None = None,
        margin_mode: str | None = None,
    ) -> Decimal:
        kind = self.fee_resolver.instrument_kind(
            symbol=symbol,
            product_type=product_type,
            margin_mode=margin_mode,
        )
        if kind == "spot":
            return max(to_decimal(self.settings.trade_cost_spot_slippage_bps), Decimal("0"))
        if kind == "margin_spot":
            return max(to_decimal(self.settings.trade_cost_margin_slippage_bps), Decimal("0"))
        return max(to_decimal(self.settings.trade_cost_derivatives_slippage_bps), Decimal("0"))

    def settlement_fee_bps_decimal(
        self,
        *,
        symbol: str | None = None,
        product_type: str | None = None,
    ) -> Decimal:
        return self.fee_resolver.settlement_fee_bps_decimal(
            symbol=symbol,
            product_type=product_type,
        )

    def estimate_single_leg_entry(
        self,
        *,
        model_name: str,
        symbol: str | None = None,
        product_type: str = "spot",
        margin_mode: str = "cash",
        execution_style: str = "taker",
        order_type: str = "market",
        passive_bias: Decimal | float | int | str | None = None,
        maker_taker_bias: Decimal | float | int | str | None = None,
        expected_slippage_bps: Decimal | float | int | str | None = None,
        include_spread: bool = False,
        expected_spread_bps: Decimal | float | int | str | None = None,
        include_funding: bool = False,
        include_settlement: bool = False,
        additional_explicit_cost_components_bps: dict[str, Decimal] | None = None,
        additional_execution_drag_components_bps: dict[str, Decimal] | None = None,
    ) -> TradeDragEstimate:
        source_flags = ["trade_cost_service_single_leg"]
        fee_bps = self.estimated_execution_fee_bps_decimal(
            symbol=symbol,
            product_type=product_type,
            margin_mode=margin_mode,
            execution_style=execution_style,
            order_type=order_type,
            passive_bias=passive_bias,
            maker_taker_bias=maker_taker_bias,
        )
        if fee_bps > Decimal("0"):
            source_flags.append("fee_trade_cost_service")

        spread_bps = Decimal("0")
        if include_spread:
            spread_override = None if expected_spread_bps is None else max(to_decimal(expected_spread_bps), Decimal("0"))
            spread_bps = self.default_spread_bps(
                symbol=symbol,
                product_type=product_type,
                margin_mode=margin_mode,
            ) if spread_override is None else spread_override
            if spread_bps > Decimal("0"):
                source_flags.append("spread_trade_cost_service")

        slippage_override = None if expected_slippage_bps is None else max(to_decimal(expected_slippage_bps), Decimal("0"))
        slippage_bps = self.default_slippage_bps(
            symbol=symbol,
            product_type=product_type,
            margin_mode=margin_mode,
        ) if slippage_override is None else slippage_override
        if slippage_bps > Decimal("0"):
            source_flags.append("slippage_trade_cost_service")

        funding_bps = Decimal("0")
        if include_funding and str(product_type).lower() == "derivatives":
            decimal_method = getattr(self.fee_resolver, "funding_fee_bps_decimal", None)
            if callable(decimal_method):
                funding_bps = max(decimal_method(symbol=symbol), Decimal("0"))
            else:
                float_method = getattr(self.fee_resolver, "funding_fee_bps", None)
                if callable(float_method):
                    funding_bps = max(to_decimal(float_method(symbol=symbol)), Decimal("0"))
            if funding_bps > Decimal("0"):
                source_flags.append("funding_account_proxy_total")

        explicit_components = dict(additional_explicit_cost_components_bps or {})
        if include_settlement:
            settlement_bps = self.settlement_fee_bps_decimal(symbol=symbol, product_type=product_type)
            if settlement_bps > Decimal("0"):
                explicit_components["delivery_settlement_fee_bps"] = settlement_bps
                source_flags.append("settlement_fee_trade_cost_service")

        return self.drag_calculator.estimate(
            profile=TradeDragProfile(
                model_name=model_name,
                cost_model_enabled=True,
                edge_reference_bps=Decimal("0"),
                ideal_open_fee_bps=fee_bps,
                ideal_close_fee_bps=Decimal("0"),
                executable_spread_bps=spread_bps,
                executable_slippage_bps=slippage_bps,
                funding_cost_bps=funding_bps,
                explicit_cost_components_bps=explicit_components,
                execution_drag_components_bps=dict(additional_execution_drag_components_bps or {}),
                cost_source_flags=source_flags,
            )
        )
