from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aats.bootstrap.settings import AATSSettings
from aats.schemas.execution import FillEvent, OrderState
from aats.schemas.portfolio import PortfolioSnapshot
from aats.schemas.reconciliation import ReconciliationReport
from aats.schemas.system import MarginModelType, ProductType


@dataclass(frozen=True, slots=True)
class RuntimeStateScope:
    product_type: ProductType
    margin_mode: MarginModelType
    allowed_symbols: tuple[str, ...]
    default_symbol: str

    def symbol_allowed(self, symbol: str | None) -> bool:
        if not symbol:
            return False
        if self.allowed_symbols:
            return symbol in self.allowed_symbols
        return symbol == self.default_symbol


def runtime_state_scope(settings: AATSSettings) -> RuntimeStateScope:
    return RuntimeStateScope(
        product_type=settings.trading_product_type,
        margin_mode=settings.margin_mode,
        allowed_symbols=tuple(settings.allowed_symbols),
        default_symbol=settings.default_symbol,
    )


def filter_order_states(order_states: list[OrderState], scope: RuntimeStateScope) -> list[OrderState]:
    return [order for order in order_states if order_state_matches_scope(order, scope)]


def filter_fills(fills: list[FillEvent], scope: RuntimeStateScope) -> list[FillEvent]:
    return [fill for fill in fills if fill_event_matches_scope(fill, scope)]


def latest_matching_snapshot(
    snapshots: list[PortfolioSnapshot],
    scope: RuntimeStateScope,
) -> PortfolioSnapshot | None:
    for snapshot in reversed(snapshots):
        if portfolio_snapshot_matches_scope(snapshot, scope):
            return snapshot
    return None


def filter_snapshots(
    snapshots: list[PortfolioSnapshot],
    scope: RuntimeStateScope,
) -> list[PortfolioSnapshot]:
    return [snapshot for snapshot in snapshots if portfolio_snapshot_matches_scope(snapshot, scope)]


def latest_matching_reconciliation(
    reports: list[ReconciliationReport],
    scope: RuntimeStateScope,
) -> ReconciliationReport | None:
    for report in reversed(reports):
        if reconciliation_report_matches_scope(report, scope):
            return report
    return None


def portfolio_snapshot_matches_scope(
    snapshot: PortfolioSnapshot,
    scope: RuntimeStateScope,
) -> bool:
    if snapshot.product_type != scope.product_type:
        return False
    if snapshot.margin_mode != scope.margin_mode:
        return False
    symbols = {position.symbol for position in snapshot.positions if position.symbol}
    if not symbols:
        return True
    return all(scope.symbol_allowed(symbol) for symbol in symbols)


def reconciliation_report_matches_scope(
    report: ReconciliationReport,
    scope: RuntimeStateScope,
) -> bool:
    if report.product_type is None or report.margin_mode is None:
        return scope.product_type == "spot" and scope.margin_mode == "cash"
    if report.product_type != scope.product_type:
        return False
    if report.margin_mode != scope.margin_mode:
        return False
    if not report.allowed_symbols:
        return True
    return all(scope.symbol_allowed(symbol) for symbol in report.allowed_symbols)


def order_state_matches_scope(
    order: OrderState,
    scope: RuntimeStateScope,
) -> bool:
    if not scope.symbol_allowed(order.symbol):
        return False
    if inferred_order_state_product_type(order) != scope.product_type:
        return False
    return inferred_order_state_margin_mode(order) == scope.margin_mode


def fill_event_matches_scope(
    fill: FillEvent,
    scope: RuntimeStateScope,
) -> bool:
    if not scope.symbol_allowed(fill.symbol):
        return False
    if fill.product_type != scope.product_type:
        return False
    return fill.margin_mode == scope.margin_mode


def inferred_order_state_product_type(order: OrderState) -> ProductType:
    payload = order.submission_payload or {}
    payload_product_type = payload.get("productType")
    if payload_product_type in {"spot", "derivatives"}:
        return payload_product_type  # type: ignore[return-value]
    symbol = order.symbol or str(payload.get("instId", ""))
    inferred = infer_product_type_from_symbol(symbol)
    explicit = getattr(order, "product_type", None)
    if explicit == "derivatives":
        return "derivatives"
    if inferred == "derivatives":
        return "derivatives"
    return "spot"


def inferred_order_state_margin_mode(order: OrderState) -> MarginModelType:
    payload = order.submission_payload or {}
    payload_margin_mode = payload.get("marginMode")
    if payload_margin_mode in {"cash", "cross", "isolated"}:
        return payload_margin_mode  # type: ignore[return-value]
    td_mode = payload.get("tdMode")
    if td_mode in {"cash", "cross", "isolated"}:
        return td_mode  # type: ignore[return-value]
    explicit = getattr(order, "margin_mode", None)
    if explicit in {"cash", "cross", "isolated"}:
        if inferred_order_state_product_type(order) == "derivatives" and explicit == "cash":
            return "cross"
        return explicit
    if inferred_order_state_product_type(order) == "derivatives":
        return "cross"
    return "cash"


def infer_product_type_from_symbol(symbol: str | None) -> ProductType:
    if not symbol:
        return "spot"
    normalized = symbol.upper()
    if normalized.endswith("-SWAP"):
        return "derivatives"
    tail = normalized.rsplit("-", 1)[-1]
    if tail.isdigit():
        return "derivatives"
    return "spot"


def scoped_portfolio_event(events: list[Any], scope: RuntimeStateScope) -> Any | None:
    for event in reversed(events):
        payload = getattr(event, "payload", None)
        if not isinstance(payload, dict):
            continue
        try:
            snapshot = PortfolioSnapshot.model_validate(payload)
        except Exception:
            continue
        if portfolio_snapshot_matches_scope(snapshot, scope):
            return event
    return None
