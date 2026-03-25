from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from aats.schemas.execution import OrderObligation, OrderState
from aats.schemas.system import RecoveryBundleLegStatus, RecoveryBundleSummary
from aats.services.runtime_scope import RuntimeStateScope, order_state_matches_scope


_TERMINAL_ORDER_STATES = {"FILLED", "CANCELED", "REJECTED", "FAILED", "BLOCKED", "DRY_RUN", "EXPIRED"}


@dataclass(frozen=True, slots=True)
class BundleRecoveryAssessment:
    open_order_count: int
    open_bundle_count: int
    recoverable_bundle_count: int
    unbundled_open_order_count: int
    bundle_recovery_required: bool
    recovery_blocking: bool
    bundle_summaries: tuple[RecoveryBundleSummary, ...]


def scoped_bundle_recovery_assessment(
    *,
    scope: RuntimeStateScope,
    order_states: list[OrderState],
    obligations: list[OrderObligation],
) -> BundleRecoveryAssessment:
    scoped_orders = [order for order in order_states if order_state_matches_scope(order, scope)]
    scoped_obligations = [obligation for obligation in obligations if obligation_matches_scope(obligation, scope)]
    open_orders = [order for order in scoped_orders if _is_open_order(order)]
    bundle_ids = {
        str(order.strategy_bundle_id)
        for order in open_orders
        if str(order.strategy_bundle_id or "").strip()
    } | {
        str(obligation.strategy_bundle_id)
        for obligation in scoped_obligations
        if str(obligation.strategy_bundle_id or "").strip()
    }
    summaries = [
        _build_bundle_summary(
            bundle_id=bundle_id,
            orders=[order for order in scoped_orders if str(order.strategy_bundle_id or "") == bundle_id],
            obligations=[
                obligation
                for obligation in scoped_obligations
                if str(obligation.strategy_bundle_id or "") == bundle_id
            ],
        )
        for bundle_id in sorted(bundle_ids)
    ]
    unbundled_open_order_count = sum(
        1
        for order in open_orders
        if not str(order.strategy_bundle_id or "").strip()
    )
    recovery_blocking = bool(
        unbundled_open_order_count
        or any(not summary.recoverable for summary in summaries)
    )
    return BundleRecoveryAssessment(
        open_order_count=len(open_orders),
        open_bundle_count=len(summaries),
        recoverable_bundle_count=sum(1 for summary in summaries if summary.recoverable),
        unbundled_open_order_count=unbundled_open_order_count,
        bundle_recovery_required=bool(summaries),
        recovery_blocking=recovery_blocking,
        bundle_summaries=tuple(summaries),
    )


def obligation_matches_scope(obligation: OrderObligation, scope: RuntimeStateScope) -> bool:
    if scope.product_type == "derivatives" and obligation.strategy_family == "smart_arbitrage":
        if obligation.product_type == "spot":
            if obligation.margin_mode != "cash":
                return False
        else:
            if obligation.product_type != scope.product_type:
                return False
            if obligation.margin_mode != scope.margin_mode:
                return False
    else:
        if obligation.product_type != scope.product_type:
            return False
        if obligation.margin_mode != scope.margin_mode:
            return False
    return scope.symbol_allowed(obligation.symbol)


def _build_bundle_summary(
    *,
    bundle_id: str,
    orders: list[OrderState],
    obligations: list[OrderObligation],
) -> RecoveryBundleSummary:
    sorted_orders = sorted(
        orders,
        key=lambda item: (
            _sort_ts(item.submitted_ts, item.last_update_ts),
        ),
    )
    open_orders = [order for order in sorted_orders if _is_open_order(order)]
    allocation_ids = {
        str(order.allocation_id)
        for order in sorted_orders
        if str(order.allocation_id or "").strip()
    } | {
        str(obligation.allocation_id)
        for obligation in obligations
        if str(obligation.allocation_id or "").strip()
    }
    participating_families = _ordered_unique(
        [
            str(item)
            for item in [
                *(order.strategy_family for order in sorted_orders),
                *(obligation.strategy_family for obligation in obligations),
            ]
            if str(item or "").strip()
        ]
    )
    sleeve_refs = _ordered_unique(
        [
            str(item)
            for item in [
                *(order.strategy_sleeve_id for order in sorted_orders),
                *(obligation.strategy_sleeve_id for obligation in obligations),
            ]
            if str(item or "").strip()
        ]
    )
    symbols = _ordered_unique([order.symbol for order in sorted_orders if str(order.symbol or "").strip()])
    product_types = _ordered_unique(
        [str(order.product_type) for order in sorted_orders if str(order.product_type or "").strip()]
    )
    margin_modes = _ordered_unique(
        [str(order.margin_mode) for order in sorted_orders if str(order.margin_mode or "").strip()]
    )
    reason_codes: list[str] = []
    if not open_orders:
        reason_codes.append("bundle_no_open_orders_visible")
    if any(not str(order.strategy_sleeve_id or "").strip() for order in open_orders):
        reason_codes.append("bundle_missing_strategy_sleeve_id")
    if any(not str(order.allocation_id or "").strip() for order in open_orders):
        reason_codes.append("bundle_missing_allocation_id")
    if len(allocation_ids) > 1:
        reason_codes.append("bundle_inconsistent_allocation_id")
    if len(open_orders) < len(sorted_orders):
        reason_codes.append("bundle_partial_fill_recovery")
    if obligations:
        reason_codes.append("bundle_active_obligations_present")
    if any(order.status in {"FAILED", "REJECTED", "BLOCKED"} for order in sorted_orders):
        reason_codes.append("bundle_leg_failure_present")
    recoverable = not any(
        code
        in {
            "bundle_missing_strategy_sleeve_id",
            "bundle_missing_allocation_id",
            "bundle_inconsistent_allocation_id",
            "bundle_leg_failure_present",
        }
        for code in reason_codes
    )
    if not recoverable:
        recovery_state = "unknown_bundle_state"
    elif "bundle_partial_fill_recovery" in reason_codes:
        recovery_state = "partial_fill_recovery"
    else:
        recovery_state = "structured_open_orders"
    return RecoveryBundleSummary(
        bundle_id=bundle_id,
        allocation_id=next(iter(allocation_ids), None),
        participating_families=participating_families,
        strategy_sleeve_refs=sleeve_refs,
        symbol_scope=symbols,
        product_types=product_types,
        margin_modes=margin_modes,
        open_order_count=len(open_orders),
        total_order_count=len(sorted_orders),
        terminal_order_count=len(sorted_orders) - len(open_orders),
        active_obligation_count=len(obligations),
        recovery_state=recovery_state,
        recoverable=recoverable,
        reason_codes=reason_codes,
        operator_summary=_bundle_operator_summary(
            recovery_state=recovery_state,
            recoverable=recoverable,
            open_order_count=len(open_orders),
            family_count=len(participating_families),
            sleeve_count=len(sleeve_refs),
        ),
        legs=[
            RecoveryBundleLegStatus(
                client_order_id=order.client_order_id,
                exchange_order_id=order.exchange_order_id,
                symbol=order.symbol,
                product_type=order.product_type,
                margin_mode=order.margin_mode,
                side=_order_side(order),
                status=order.status,
                strategy_family=order.strategy_family,
                strategy_sleeve_id=order.strategy_sleeve_id,
                strategy_leg_role=order.strategy_leg_role,
                requested_qty=order.requested_qty,
                filled_qty=order.filled_qty,
                remaining_qty=order.remaining_qty,
                submitted_ts=order.submitted_ts,
                last_update_ts=order.last_update_ts,
            )
            for order in sorted_orders
        ],
    )


def _bundle_operator_summary(
    *,
    recovery_state: str,
    recoverable: bool,
    open_order_count: int,
    family_count: int,
    sleeve_count: int,
) -> str:
    if not recoverable:
        return "当前 bundle 的未完成腿缺少一致的 sleeve / allocation 身份，恢复链无法自动确认，需要先人工检查。"
    if recovery_state == "partial_fill_recovery":
        return (
            f"当前 bundle 处于部分成交恢复中，仍有 {open_order_count} 条未完成腿待收敛，"
            f"涉及 {family_count} 个策略 family / {sleeve_count} 个 sleeve。"
        )
    return (
        f"当前 bundle 仍有 {open_order_count} 条未完成腿，"
        f"系统正在按 bundle / sleeve 维度跟踪恢复，涉及 {family_count} 个 family / {sleeve_count} 个 sleeve。"
    )


def _sort_ts(primary: datetime | None, fallback: datetime | None) -> float:
    ts = primary or fallback
    if ts is None:
        return 0.0
    return ts.timestamp()


def _is_open_order(order: OrderState) -> bool:
    return str(order.status or "").upper() not in _TERMINAL_ORDER_STATES


def _ordered_unique(values: list[str]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _order_side(order: OrderState) -> str:
    payload_side = str((order.submission_payload or {}).get("side") or "").strip().lower()
    if payload_side in {"buy", "sell"}:
        return payload_side
    position_intent = str(order.position_intent or "").strip().lower()
    if position_intent in {"open_long", "reduce_short", "close_short", "reverse_to_long"}:
        return "buy"
    if position_intent in {"open_short", "reduce_long", "close_long", "reverse_to_short"}:
        return "sell"
    return "buy"
