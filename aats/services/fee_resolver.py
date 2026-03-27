from __future__ import annotations

from decimal import Decimal
from typing import Any

from aats.bootstrap.settings import AATSSettings
from aats.services.portfolio_service.decimals import to_decimal


class EffectiveFeeResolver:
    def __init__(
        self,
        *,
        settings: AATSSettings,
        account_service: Any | None = None,
    ) -> None:
        self.settings = settings
        self.account_service = account_service

    def taker_fee_bps_decimal(
        self,
        *,
        symbol: str | None = None,
        product_type: str | None = None,
        margin_mode: str | None = None,
    ) -> Decimal:
        fallback = self._default_taker_fee_bps(
            symbol=symbol,
            product_type=product_type,
            margin_mode=margin_mode,
        )
        getter = getattr(self.account_service, "effective_taker_fee_bps", None)
        if not callable(getter):
            return fallback
        try:
            resolved = getter(symbol=symbol)
        except TypeError:
            resolved = getter(symbol) if symbol is not None else getter()
        if resolved is None:
            return fallback
        fee_bps = to_decimal(resolved)
        if fee_bps < Decimal("0"):
            return fallback
        return fee_bps

    def taker_fee_bps(
        self,
        *,
        symbol: str | None = None,
        product_type: str | None = None,
        margin_mode: str | None = None,
    ) -> float:
        return float(
            self.taker_fee_bps_decimal(
                symbol=symbol,
                product_type=product_type,
                margin_mode=margin_mode,
            )
        )

    def maker_fee_bps_decimal(
        self,
        *,
        symbol: str | None = None,
        product_type: str | None = None,
        margin_mode: str | None = None,
    ) -> Decimal:
        fallback = self._default_maker_fee_bps(
            symbol=symbol,
            product_type=product_type,
            margin_mode=margin_mode,
        )
        getter = getattr(self.account_service, "effective_maker_fee_bps", None)
        if not callable(getter):
            return fallback
        try:
            resolved = getter(symbol=symbol)
        except TypeError:
            resolved = getter(symbol) if symbol is not None else getter()
        if resolved is None:
            return fallback
        fee_bps = to_decimal(resolved)
        if fee_bps < Decimal("0"):
            return fallback
        return fee_bps

    def maker_fee_bps(
        self,
        *,
        symbol: str | None = None,
        product_type: str | None = None,
        margin_mode: str | None = None,
    ) -> float:
        return float(
            self.maker_fee_bps_decimal(
                symbol=symbol,
                product_type=product_type,
                margin_mode=margin_mode,
            )
        )

    def funding_fee_bps_decimal(self, *, symbol: str | None = None) -> Decimal:
        getter = getattr(self.account_service, "funding_fee_bps_proxy", None)
        if not callable(getter):
            return Decimal("0")
        try:
            resolved = getter(symbol=symbol)
        except TypeError:
            resolved = getter(symbol) if symbol is not None else getter()
        if resolved is None:
            return Decimal("0")
        fee_bps = to_decimal(resolved)
        return max(fee_bps, Decimal("0"))

    def funding_fee_bps(self, *, symbol: str | None = None) -> float:
        return float(self.funding_fee_bps_decimal(symbol=symbol))

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
        taker = self.taker_fee_bps_decimal(
            symbol=symbol,
            product_type=product_type,
            margin_mode=margin_mode,
        )
        maker = self.maker_fee_bps_decimal(
            symbol=symbol,
            product_type=product_type,
            margin_mode=margin_mode,
        )
        normalized_style = str(execution_style or "").lower()
        normalized_order_type = str(order_type or "").lower()
        if normalized_order_type == "market" or normalized_style in {"taker", "bounded_taker_cap", "exchange"}:
            return taker
        if normalized_order_type == "limit" or normalized_style in {"bounded_limit_ioc", "maker", "passive"}:
            passive = min(max(to_decimal(passive_bias or 0), Decimal("0")), Decimal("1"))
            maker_bias = min(max(-to_decimal(maker_taker_bias or 0), Decimal("0")), Decimal("1"))
            maker_weight = min(
                max(Decimal("0.15") + (passive * Decimal("0.45")) + (maker_bias * Decimal("0.20")), Decimal("0")),
                Decimal("0.80"),
            )
            return (taker * (Decimal("1") - maker_weight)) + (maker * maker_weight)
        return taker

    def estimated_execution_fee_bps(self, **kwargs: object) -> float:
        return float(self.estimated_execution_fee_bps_decimal(**kwargs))

    def settlement_fee_bps_decimal(
        self,
        *,
        symbol: str | None = None,
        product_type: str | None = None,
    ) -> Decimal:
        kind = self.instrument_kind(symbol=symbol, product_type=product_type)
        if kind != "delivery":
            return Decimal("0")
        return max(to_decimal(self.settings.trade_cost_delivery_settlement_fee_bps), Decimal("0"))

    def instrument_kind(
        self,
        *,
        symbol: str | None = None,
        product_type: str | None = None,
        margin_mode: str | None = None,
    ) -> str:
        normalized_product = str(product_type or "").strip().lower()
        normalized_margin = str(margin_mode or "").strip().lower()
        normalized_symbol = str(symbol or "").strip().upper()
        if normalized_product == "spot":
            if normalized_margin in {"cross", "isolated"}:
                return "margin_spot"
            return "spot"
        if normalized_product == "derivatives":
            if normalized_symbol.endswith("-SWAP"):
                return "perpetual"
            tail = normalized_symbol.rsplit("-", 1)[-1] if normalized_symbol else ""
            if tail.isdigit():
                return "delivery"
            if normalized_symbol.count("-") >= 3:
                return "option"
            return "perpetual"
        if normalized_margin in {"cross", "isolated"}:
            return "margin_spot"
        return "spot"

    def _default_taker_fee_bps(
        self,
        *,
        symbol: str | None = None,
        product_type: str | None = None,
        margin_mode: str | None = None,
    ) -> Decimal:
        kind = self.instrument_kind(symbol=symbol, product_type=product_type, margin_mode=margin_mode)
        if kind == "spot":
            return max(to_decimal(self.settings.trade_cost_spot_taker_fee_bps), Decimal("0"))
        if kind == "margin_spot":
            return max(to_decimal(self.settings.trade_cost_margin_taker_fee_bps), Decimal("0"))
        if kind in {"perpetual", "delivery", "option"}:
            return max(to_decimal(self.settings.trade_cost_derivatives_taker_fee_bps), Decimal("0"))
        return max(to_decimal(self.settings.paper_taker_fee_bps), Decimal("0"))

    def _default_maker_fee_bps(
        self,
        *,
        symbol: str | None = None,
        product_type: str | None = None,
        margin_mode: str | None = None,
    ) -> Decimal:
        kind = self.instrument_kind(symbol=symbol, product_type=product_type, margin_mode=margin_mode)
        if kind == "spot":
            return max(to_decimal(self.settings.trade_cost_spot_maker_fee_bps), Decimal("0"))
        if kind == "margin_spot":
            return max(to_decimal(self.settings.trade_cost_margin_maker_fee_bps), Decimal("0"))
        if kind in {"perpetual", "delivery", "option"}:
            return max(to_decimal(self.settings.trade_cost_derivatives_maker_fee_bps), Decimal("0"))
        return self._default_taker_fee_bps(symbol=symbol, product_type=product_type, margin_mode=margin_mode)
