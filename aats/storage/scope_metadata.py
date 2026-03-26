from __future__ import annotations

from collections.abc import Sequence

from aats.schemas.common import EventEnvelope
from aats.schemas.execution import FillEvent, OrderState
from aats.schemas.portfolio import PortfolioSnapshot
from aats.schemas.reconciliation import ReconciliationReport
from aats.services.runtime_scope import infer_product_type_from_symbol


def envelope_scope_metadata(envelope: EventEnvelope) -> dict[str, str | None]:
    payload = envelope.payload
    details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
    symbol = _string(payload.get("symbol")) or _string(details.get("symbol"))
    timeframe = _string(payload.get("timeframe"))
    product_type = _product_type(payload.get("product_type")) or _product_type(details.get("product_type"))
    margin_mode = _margin_mode(payload.get("margin_mode")) or _margin_mode(details.get("margin_mode"))

    if symbol is None:
        symbol = _first_string(payload.get("allowed_symbols")) or _first_string(details.get("allowed_symbols"))

    if product_type is None and symbol is not None:
        product_type = infer_product_type_from_symbol(symbol)
    if margin_mode is None and product_type == "spot":
        margin_mode = "cash"
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "product_type": product_type,
        "margin_mode": margin_mode,
    }


def portfolio_scope_metadata(snapshot: PortfolioSnapshot) -> dict[str, str | None]:
    return {
        "product_type": snapshot.product_type,
        "margin_mode": snapshot.margin_mode,
        "primary_symbol": _primary_symbol_from_positions(snapshot.positions),
    }


def order_scope_metadata(state: OrderState) -> dict[str, str | None]:
    return {
        "symbol": state.symbol,
        "product_type": state.product_type,
        "margin_mode": state.margin_mode,
        "position_intent": state.position_intent,
    }


def fill_scope_metadata(fill: FillEvent) -> dict[str, str | None]:
    return {
        "symbol": fill.symbol,
        "product_type": fill.product_type,
        "margin_mode": fill.margin_mode,
        "position_intent": fill.position_intent,
    }


def reconciliation_scope_metadata(report: ReconciliationReport) -> dict[str, str | None]:
    return {
        "decision_id": report.decision_id,
        "product_type": report.product_type,
        "margin_mode": report.margin_mode,
        "primary_symbol": _first_string(report.allowed_symbols),
    }


def _primary_symbol_from_positions(positions) -> str | None:
    if not positions:
        return None
    ranked = sorted(positions, key=lambda item: abs(item.position_qty), reverse=True)
    return ranked[0].symbol if ranked else None


def _first_string(value: object) -> str | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return None
    for item in value:
        resolved = _string(item)
        if resolved is not None:
            return resolved
    return None


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _product_type(value: object) -> str | None:
    if value in {"spot", "derivatives"}:
        return str(value)
    return None


def _margin_mode(value: object) -> str | None:
    if value in {"cash", "cross", "isolated"}:
        return str(value)
    return None
