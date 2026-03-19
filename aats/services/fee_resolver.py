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

    def taker_fee_bps_decimal(self, *, symbol: str | None = None) -> Decimal:
        fallback = max(to_decimal(self.settings.paper_taker_fee_bps), Decimal("0"))
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

    def taker_fee_bps(self, *, symbol: str | None = None) -> float:
        return float(self.taker_fee_bps_decimal(symbol=symbol))

    def maker_fee_bps_decimal(self, *, symbol: str | None = None) -> Decimal:
        fallback = self.taker_fee_bps_decimal(symbol=symbol)
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

    def maker_fee_bps(self, *, symbol: str | None = None) -> float:
        return float(self.maker_fee_bps_decimal(symbol=symbol))

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
        execution_style: str | None = None,
        order_type: str | None = None,
        passive_bias: Decimal | float | int | str | None = None,
        maker_taker_bias: Decimal | float | int | str | None = None,
    ) -> Decimal:
        taker = self.taker_fee_bps_decimal(symbol=symbol)
        maker = self.maker_fee_bps_decimal(symbol=symbol)
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
