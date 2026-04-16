from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from typing import Any

from aats.schemas.execution import FillEvent
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, to_decimal
from aats.services.strategy_engines.independent.payload_normalization import (
    normalize_independent_runtime_state_payloads,
)

_FLOOR_PROMOTED_REASON_CODE_TEMPLATE = "independent_{leg}_book_de_risk_floor_promoted_to_close"


def decision_ids_for_guard_exclusions(*, fills: Sequence[FillEvent]) -> set[str]:
    return {
        decision_id
        for fill in fills
        if (decision_id := _fill_decision_id(fill)) is not None
    }


def guard_excluded_fill_ids_for_independent_residual_exits(
    *,
    fills: Sequence[FillEvent],
    audits: Sequence[Any],
    payload_by_ref: Callable[[str | None], Mapping[str, Any] | None],
) -> set[str]:
    decision_ids = decision_ids_for_guard_exclusions(fills=fills)
    if not decision_ids or not audits:
        return set()

    excluded_decision_keys: set[tuple[str, str]] = set()
    excluded_decision_ids: set[str] = set()
    excluded_chain_ids: set[str] = set()
    for audit in audits:
        decision_id = _audit_field(audit, "decision_id")
        if not decision_id or decision_id not in decision_ids:
            continue
        for state in _audit_runtime_states(audit=audit, payload_by_ref=payload_by_ref):
            leg = str(state.get("leg") or "").strip().lower()
            if leg not in {"long", "short"}:
                continue
            if not _runtime_state_marks_independent_residual_cleanup(state=state, leg=leg):
                continue
            excluded_decision_ids.add(decision_id)
            excluded_decision_keys.add((decision_id, leg))
            chain_id = str(state.get("execution_chain_id") or "").strip()
            if chain_id:
                excluded_chain_ids.add(chain_id)

    excluded_fill_ids: set[str] = set()
    for fill in fills:
        fill_id = str(getattr(fill, "fill_id", "") or "").strip()
        if not fill_id:
            continue
        chain_id = str(getattr(fill, "execution_chain_id", "") or "").strip()
        if chain_id and chain_id in excluded_chain_ids:
            excluded_fill_ids.add(fill_id)
            continue
        decision_id = _fill_decision_id(fill) or ""
        leg = _fill_leg(fill)
        if leg is not None and (decision_id, leg) in excluded_decision_keys:
            excluded_fill_ids.add(fill_id)
            continue
        if leg is None and decision_id in excluded_decision_ids:
            excluded_fill_ids.add(fill_id)
    return excluded_fill_ids


def _runtime_state_marks_independent_residual_cleanup(*, state: Mapping[str, Any], leg: str) -> bool:
    normalized_leg = str(leg or "").strip().lower()
    if normalized_leg not in {"long", "short"}:
        return False
    normalized_reason_codes = {
        str(code or "").strip().lower()
        for code in list(state.get("reason_codes") or [])
        if str(code or "").strip()
    }
    expected_reason_code = _FLOOR_PROMOTED_REASON_CODE_TEMPLATE.format(leg=normalized_leg)
    if expected_reason_code not in normalized_reason_codes:
        return False
    target_qty = to_decimal(state.get("target_qty")) or Decimal("0")
    if target_qty > EPSILON_DECIMAL_12:
        return False
    book_action = str(state.get("book_action") or "").strip().lower()
    return book_action == "de_risk"


def _audit_runtime_states(
    *,
    audit: Any,
    payload_by_ref: Callable[[str | None], Mapping[str, Any] | None],
) -> list[dict[str, Any]]:
    states_by_key: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for ref_field in (
        "position_target_ref",
        "decision_outcome_ref",
        "policy_decision_ref",
        "risk_decision_ref",
    ):
        payload = payload_by_ref(_audit_field(audit, ref_field))
        for state in _runtime_states_from_payload(payload):
            key = (
                str(state.get("execution_chain_id") or "").strip(),
                str(state.get("leg") or "").strip().lower(),
                str(state.get("book_action") or "").strip().lower(),
                str(state.get("close_reason") or "").strip().lower(),
            )
            existing = states_by_key.get(key)
            if existing is None:
                states_by_key[key] = dict(state)
                continue
            merged_reason_codes = list(
                dict.fromkeys(
                    [
                        *list(existing.get("reason_codes") or []),
                        *list(state.get("reason_codes") or []),
                    ]
                )
            )
            if merged_reason_codes:
                existing["reason_codes"] = merged_reason_codes
            existing_target_qty = to_decimal(existing.get("target_qty"))
            incoming_target_qty = to_decimal(state.get("target_qty"))
            if incoming_target_qty is not None and (
                existing_target_qty is None or incoming_target_qty < existing_target_qty - EPSILON_DECIMAL_12
            ):
                existing["target_qty"] = state.get("target_qty")
            if existing.get("execution_chain_id") in {None, ""} and state.get("execution_chain_id"):
                existing["execution_chain_id"] = state.get("execution_chain_id")
    return list(states_by_key.values())


def _runtime_states_from_payload(payload: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(payload, Mapping):
        return []
    direct = payload.get("book_runtime_states")
    if isinstance(direct, list) and direct:
        return normalize_independent_runtime_state_payloads(runtime_states=direct)
    family_summary = payload.get("family_execution_summary")
    if not isinstance(family_summary, Mapping):
        return []
    nested = family_summary.get("book_runtime_states")
    if isinstance(nested, list):
        return normalize_independent_runtime_state_payloads(runtime_states=nested)
    return []


def _audit_field(audit: Any, field: str) -> str | None:
    value = getattr(audit, field, None)
    if value is None and isinstance(audit, Mapping):
        value = audit.get(field)
    normalized = str(value or "").strip()
    return normalized or None


def _fill_leg(fill: FillEvent) -> str | None:
    pos_side = str(getattr(fill, "pos_side", "") or "").strip().lower()
    if pos_side in {"long", "short"}:
        return pos_side
    position_intent = str(getattr(fill, "position_intent", "") or "").strip().lower()
    if position_intent.endswith("_long"):
        return "long"
    if position_intent.endswith("_short"):
        return "short"
    return None


def _fill_decision_id(fill: FillEvent) -> str | None:
    explicit_decision_id = str(getattr(fill, "decision_id", "") or "").strip()
    if explicit_decision_id:
        return explicit_decision_id
    execution_chain_id = str(getattr(fill, "execution_chain_id", "") or "").strip()
    return _decision_id_from_execution_chain_id(execution_chain_id)


def _decision_id_from_execution_chain_id(execution_chain_id: str | None) -> str | None:
    normalized_chain_id = str(execution_chain_id or "").strip()
    if not normalized_chain_id.startswith("independent:"):
        return None
    parts = normalized_chain_id.split(":")
    if len(parts) < 4:
        return None
    decision_id = str(parts[1] or "").strip()
    return decision_id or None
