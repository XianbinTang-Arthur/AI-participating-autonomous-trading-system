from __future__ import annotations

import logging
from decimal import Decimal
from math import sqrt

from aats.bootstrap.settings import AATSSettings
from aats.schemas.market import MarketSnapshot
from aats.services.fee_resolver import EffectiveFeeResolver
from aats.services.portfolio_service.decimals import to_decimal
from aats.services.trade_drag import TradeDragCalculator, TradeDragEstimate, TradeDragProfile

logger = logging.getLogger(__name__)


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
        side: str | None = None,
        quantity: Decimal | float | int | str | None = None,
        projected_notional: Decimal | float | int | str | None = None,
        reference_price: Decimal | float | int | str | None = None,
        market_snapshot: MarketSnapshot | None = None,
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

        execution_drag_components = dict(additional_execution_drag_components_bps or {})
        size_aware_context = _size_aware_execution_drag_context(
            market_snapshot=market_snapshot,
            side=side,
            expected_slippage_bps=slippage_bps,
            quantity=quantity,
            projected_notional=projected_notional,
            reference_price=reference_price,
        )
        if size_aware_context["size_impact_bps"] > Decimal("0"):
            execution_drag_components["size_impact_bps"] = size_aware_context["size_impact_bps"]
            source_flags.append("size_aware_market_impact")
        if size_aware_context["depth_source"] == "orderbook":
            source_flags.append("size_aware_market_depth")
        elif size_aware_context["depth_source"] == "top_of_book":
            source_flags.append("size_aware_top_of_book")
        if size_aware_context["reference_price_source"] == "market_snapshot":
            source_flags.append("size_aware_market_snapshot_price")
        elif size_aware_context["reference_price_source"] == "explicit":
            source_flags.append("size_aware_explicit_price")

        execution_context = {
            name: value
            for name, value in (
                ("size_impact_bps", size_aware_context["size_impact_bps"]),
                ("projected_notional", size_aware_context["projected_notional"]),
                ("reference_price", size_aware_context["reference_price"]),
                ("quoted_depth_notional", size_aware_context["quoted_depth_notional"]),
                ("depth_consumption_ratio", size_aware_context["depth_consumption_ratio"]),
            )
            if isinstance(value, Decimal)
        }

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
                execution_drag_components_bps=execution_drag_components,
                execution_context=execution_context,
                cost_source_flags=source_flags,
            )
        )


def _size_aware_execution_drag_context(
    *,
    market_snapshot: MarketSnapshot | None,
    side: str | None,
    expected_slippage_bps: Decimal,
    quantity: Decimal | float | int | str | None,
    projected_notional: Decimal | float | int | str | None,
    reference_price: Decimal | float | int | str | None,
) -> dict[str, Decimal | str | None]:
    empty = {
        "size_impact_bps": Decimal("0"),
        "projected_notional": None,
        "reference_price": None,
        "quoted_depth_notional": None,
        "depth_consumption_ratio": None,
        "reference_price_source": None,
        "depth_source": None,
    }
    if market_snapshot is None:
        return empty
    normalized_side = str(side or "").lower()

    resolved_price, price_source = _resolve_reference_price(
        market_snapshot=market_snapshot,
        reference_price=reference_price,
    )
    if resolved_price <= Decimal("0"):
        return empty

    resolved_notional = _resolve_projected_notional(
        quantity=quantity,
        projected_notional=projected_notional,
        reference_price=resolved_price,
    )
    if resolved_notional <= Decimal("0"):
        return {
            **empty,
            "projected_notional": resolved_notional,
            "reference_price": resolved_price,
            "reference_price_source": price_source,
        }
    if normalized_side not in {"buy", "sell"}:
        return {
            **empty,
            "projected_notional": resolved_notional,
            "reference_price": resolved_price,
            "reference_price_source": price_source,
        }

    quoted_depth_notional, depth_source = _resolve_quoted_depth_notional(
        market_snapshot=market_snapshot,
        side=normalized_side,
        reference_price=resolved_price,
    )
    if quoted_depth_notional is None or quoted_depth_notional <= Decimal("0"):
        return {
            **empty,
            "projected_notional": resolved_notional,
            "reference_price": resolved_price,
            "reference_price_source": price_source,
        }

    depth_consumption_ratio = max(float(resolved_notional / quoted_depth_notional), 0.0)
    if depth_consumption_ratio <= 0.0:
        return {
            **empty,
            "projected_notional": resolved_notional,
            "reference_price": resolved_price,
            "quoted_depth_notional": quoted_depth_notional,
            "reference_price_source": price_source,
            "depth_source": depth_source,
        }

    spread_bps = _market_spread_bps(market_snapshot=market_snapshot, reference_price=resolved_price)
    dynamic_slippage_bps = max(
        float(expected_slippage_bps),
        spread_bps * (0.55 + min(depth_consumption_ratio, 1.5)),
        float(expected_slippage_bps) * (1.0 + (sqrt(depth_consumption_ratio) * 0.75)),
    )
    size_impact_bps = max(dynamic_slippage_bps - float(expected_slippage_bps), 0.0)
    return {
        "size_impact_bps": to_decimal(round(size_impact_bps, 6)),
        "projected_notional": resolved_notional,
        "reference_price": resolved_price,
        "quoted_depth_notional": quoted_depth_notional,
        "depth_consumption_ratio": to_decimal(round(depth_consumption_ratio, 6)),
        "reference_price_source": price_source,
        "depth_source": depth_source,
    }


def _resolve_reference_price(
    *,
    market_snapshot: MarketSnapshot,
    reference_price: Decimal | float | int | str | None,
) -> tuple[Decimal, str | None]:
    explicit = Decimal("0") if reference_price is None else max(to_decimal(reference_price), Decimal("0"))
    if explicit > Decimal("0"):
        return explicit, "explicit"
    bid = max(to_decimal(market_snapshot.best_bid), Decimal("0"))
    ask = max(to_decimal(market_snapshot.best_ask), Decimal("0"))
    if bid > Decimal("0") and ask > Decimal("0"):
        return (bid + ask) / Decimal("2"), "market_snapshot"
    last_price = max(to_decimal(market_snapshot.last_price), Decimal("0"))
    if last_price > Decimal("0"):
        return last_price, "market_snapshot"
    return Decimal("0"), None


def _resolve_projected_notional(
    *,
    quantity: Decimal | float | int | str | None,
    projected_notional: Decimal | float | int | str | None,
    reference_price: Decimal,
) -> Decimal:
    if projected_notional is not None:
        explicit = max(to_decimal(projected_notional), Decimal("0"))
        if explicit > Decimal("0"):
            return explicit
    if quantity is None:
        return Decimal("0")
    return max(abs(to_decimal(quantity)) * max(reference_price, Decimal("0")), Decimal("0"))


def _resolve_quoted_depth_notional(
    *,
    market_snapshot: MarketSnapshot,
    side: str | None,
    reference_price: Decimal,
) -> tuple[Decimal | None, str | None]:
    normalized_side = str(side or "").lower()
    depth_side = "asks" if normalized_side == "buy" else "bids"
    top_price = (
        max(to_decimal(market_snapshot.best_ask), Decimal("0"))
        if normalized_side == "buy"
        else max(to_decimal(market_snapshot.best_bid), Decimal("0"))
    )
    top_notional = (
        max(to_decimal(market_snapshot.ask_size), Decimal("0")) * max(top_price, Decimal("0"))
        if normalized_side == "buy"
        else max(to_decimal(market_snapshot.bid_size), Decimal("0")) * max(top_price, Decimal("0"))
    )
    depth_levels = market_snapshot.orderbook_depth.get(depth_side)
    depth_notional = _depth_notional_total(depth_levels, fallback_price=reference_price)
    if depth_notional > Decimal("0"):
        return depth_notional, "orderbook"
    if top_notional > Decimal("0"):
        return top_notional, "top_of_book"
    return None, None


def _depth_notional_total(levels: object, *, fallback_price: Decimal) -> Decimal:
    if not isinstance(levels, list):
        return Decimal("0")
    total = Decimal("0")
    for level in levels:
        level_notional = _level_notional(level, fallback_price=fallback_price)
        if level_notional is None:
            continue
        total += max(level_notional, Decimal("0"))
    return total


def _level_notional(level: object, *, fallback_price: Decimal) -> Decimal | None:
    if isinstance(level, dict):
        price = level.get("price") or level.get("px")
        size = level.get("size") or level.get("qty") or level.get("quantity")
    elif isinstance(level, (list, tuple)) and len(level) >= 2:
        price = level[0]
        size = level[1]
    else:
        return None
    try:
        resolved_size = max(to_decimal(size), Decimal("0"))
        resolved_price = max(to_decimal(price), Decimal("0")) if price is not None else Decimal("0")
    except Exception as exc:
        logger.warning("Failed to parse orderbook level price/size: %s", exc)
        return None
    if resolved_size <= Decimal("0"):
        return None
    if resolved_price <= Decimal("0"):
        resolved_price = max(fallback_price, Decimal("0"))
    if resolved_price <= Decimal("0"):
        return None
    return resolved_size * resolved_price


def _market_spread_bps(*, market_snapshot: MarketSnapshot, reference_price: Decimal) -> float:
    if reference_price <= Decimal("0"):
        return 0.0
    best_bid = max(to_decimal(market_snapshot.best_bid), Decimal("0"))
    best_ask = max(to_decimal(market_snapshot.best_ask), Decimal("0"))
    if best_bid <= Decimal("0") or best_ask <= Decimal("0"):
        return 0.0
    spread = max(best_ask - best_bid, Decimal("0"))
    return float((spread / reference_price) * Decimal("10000"))
