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
        if callable(getter):
            try:
                resolved = getter(symbol=symbol)
            except TypeError:
                resolved = getter(symbol) if symbol is not None else getter()
            if resolved is not None:
                fee_bps = to_decimal(resolved)
                return max(fee_bps, Decimal("0"))
        summary_getter = getattr(self.account_service, "recent_funding_fee_summary", None)
        if not callable(summary_getter):
            return Decimal("0")
        try:
            summary = summary_getter(symbol=symbol)
        except TypeError:
            summary = summary_getter(symbol) if symbol is not None else summary_getter()
        if not isinstance(summary, dict):
            return Decimal("0")
        resolved = summary.get("funding_fee_bps_proxy")
        if resolved in {None, ""}:
            return Decimal("0")
        fee_bps = to_decimal(resolved)
        return max(fee_bps, Decimal("0"))

    def funding_fee_bps_per_event_decimal(self, *, symbol: str | None = None) -> Decimal | None:
        getter = getattr(self.account_service, "funding_fee_bps_proxy_per_event", None)
        if callable(getter):
            try:
                resolved = getter(symbol=symbol)
            except TypeError:
                resolved = getter(symbol) if symbol is not None else getter()
            if resolved is not None:
                return max(to_decimal(resolved), Decimal("0"))

        summary_getter = getattr(self.account_service, "recent_funding_fee_summary", None)
        if not callable(summary_getter):
            return None
        try:
            summary = summary_getter(symbol=symbol)
        except TypeError:
            summary = summary_getter(symbol) if symbol is not None else summary_getter()
        if not isinstance(summary, dict):
            return None
        resolved = summary.get("funding_fee_bps_proxy_per_event")
        if resolved in {None, ""}:
            return None
        return max(to_decimal(resolved), Decimal("0"))

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
        # P1-B step 2 cost 审计修复 (2026-04-19): OKX 官方 ordType=ioc
        # (Immediate-Or-Cancel) 永远付 taker fee —— 订单要么立刻和簿内单匹配 (taker),
        # 要么取消 (不成交), 绝无停留成为 maker 的可能. 之前 bounded_limit_ioc 被归
        # 到 maker-blend 分支, 按 passive_bias=0.7 给 fee 打折, 让 expected_cost_bps
        # 低估 ~1.4 bps, 实盘永远看到 net_edge 过线但实际 fill 后亏费.
        # 详见 docs/review/independent_cost_model_audit_2026_04_19.md
        if normalized_order_type == "market" or normalized_style in {
            "taker", "bounded_taker_cap", "bounded_limit_ioc", "exchange",
        }:
            return taker
        # post_only_with_timeout_fallback (2026-04-21): OKX ordType=post_only 是真 maker,
        # 若跨价被 OKX 即时拒绝; 超时未成交时 fallback 为 taker. 期望成本按配置的
        # expected_fill_rate 做 maker/taker 加权 —— **不是** H2 回退 (bounded_limit_ioc
        # 仍归 taker), 这是新增分支. 详见 docs/design/post_only_maker_exit_mode_2026_04_21.md §3.6
        if normalized_order_type == "post_only" or normalized_style == "post_only":
            fill_rate_raw = getattr(
                self.settings,
                "strategy_hedge_independent_post_only_expected_fill_rate",
                None,
            )
            fill_rate = min(
                max(to_decimal(fill_rate_raw if fill_rate_raw is not None else 0), Decimal("0")),
                Decimal("1"),
            )
            return (maker * fill_rate) + (taker * (Decimal("1") - fill_rate))
        if normalized_order_type == "limit" or normalized_style in {"maker", "passive"}:
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
