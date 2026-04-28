from __future__ import annotations

from decimal import Decimal
from typing import Any

from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.execution import OrderIntent, OrderState, pos_side_from_position_intent
from aats.schemas.exit_execution import (
    ChildExitOrderCategory,
    ChildExitOrderRef,
    ExitExecutionIntent,
)
from aats.schemas.reconciliation import ReconciliationFinding, ReconciliationReport
from aats.services.execution_engine.order_truth import (
    is_risk_reducing_order_intent,
    is_risk_reducing_order_state,
    requires_unknown_write_review,
    unknown_write_state,
)
from aats.services.execution_engine.bundle_recovery import _ordered_unique
from aats.services.execution_engine.exit_execution_writer import ExitExecutionWriter
from aats.storage.base import ExecutionRepository, ExitExecutionRepository


_ZERO = Decimal("0")
_EPSILON = Decimal("1e-12")
_TERMINAL_PARENT_STATUSES = {"COMPLETED", "CANCELED", "FAILED_SAFE"}

# ── Task 142：exit_execution review kind 常量集中化 ────────────────
# 这些字符串同时在三处 consume（aggregator、recovery_posture、startup_recovery
# overlay），早期以字面量散落在各处，一旦改动易错位。提到 module level 后
# recovery_posture._PERSISTENT_STATUS_BLOCKERS 通过 EXIT_EXECUTION_BLOCKER_KINDS
# 直接引用，保证"加一条 blocker kind"只需改本文件。字符串**不得**变更——
# 下游 RecoveryStatus.resume_blocked_reasons、reconciliation finding_type、
# Grafana 告警、operator UI 都按字面量匹配。
EXIT_EXECUTION_PARENT_REVIEW_REQUIRED_KIND = "exit_execution_parent_review_required"
EXIT_EXECUTION_TRUTH_PENDING_KIND = "exit_execution_truth_pending"
EXIT_EXECUTION_MISSING_CHILD_REFS_KIND = "exit_execution_missing_child_refs_for_parent"
EXIT_EXECUTION_RESUME_TEMPLATE_MISSING_KIND = "exit_execution_resume_template_missing"
EXIT_EXECUTION_RESUME_LIMIT_LOOKUP_FAILED_KIND = "exit_execution_resume_limit_lookup_failed"

EXIT_EXECUTION_BLOCKER_KINDS: frozenset[str] = frozenset(
    {
        EXIT_EXECUTION_PARENT_REVIEW_REQUIRED_KIND,
        EXIT_EXECUTION_TRUTH_PENDING_KIND,
        EXIT_EXECUTION_MISSING_CHILD_REFS_KIND,
        EXIT_EXECUTION_RESUME_TEMPLATE_MISSING_KIND,
        EXIT_EXECUTION_RESUME_LIMIT_LOOKUP_FAILED_KIND,
    }
)

# ── resume_issue kind 常量（写入 parent.metadata["resume_issue"]["kind"]）──
# 与上面的 review kind 不同：这些 kind 是 parent 级别的 resume issue 分类，
# 不带 "exit_execution_" 前缀。review item kind 与之语义映射：
#   MISSING_CHILD_REFS_RESUME_ISSUE_KIND    ↔ EXIT_EXECUTION_MISSING_CHILD_REFS_KIND
#   RESUME_LIMIT_LOOKUP_FAILED_RESUME_ISSUE_KIND ↔ EXIT_EXECUTION_RESUME_LIMIT_LOOKUP_FAILED_KIND
# 保留两套常量是因为在 metadata 里的 kind 不应该 leak review-specific 前缀，
# 但它们必须成对变更。字符串**不得**变更——持久化在 DB metadata，改名会破坏
# 已写入的 resume_issue 记录。
MISSING_CHILD_REFS_RESUME_ISSUE_KIND = "missing_child_refs_for_parent"
RESUME_LIMIT_LOOKUP_FAILED_RESUME_ISSUE_KIND = "resume_limit_lookup_failed"


def parent_exit_intent_id_from_order_intent(intent: OrderIntent) -> str:
    base = str(intent.execution_chain_id or intent.intent_id).strip()
    return f"exit_parent:{base}"


def parent_exit_intent_id_from_order_state(order_state: OrderState) -> str:
    base = str(order_state.execution_chain_id or order_state.intent_id).strip()
    return f"exit_parent:{base}"


def create_exit_execution_intent_from_order_intent(intent: OrderIntent) -> ExitExecutionIntent:
    if not is_risk_reducing_order_intent(intent):
        raise ValueError("exit_execution_intent_requires_risk_reducing_intent")
    intent_kind = _intent_kind(
        position_intent=intent.position_intent,
        execution_action=intent.execution_action,
        close_only=bool(intent.close_only),
    )
    position_side = _position_side(
        pos_side=intent.pos_side,
        position_intent=intent.position_intent,
        exposure_side=intent.exposure_side,
    )
    target_exit_quantity = max(Decimal(intent.quantity), _ZERO)
    return ExitExecutionIntent(
        parent_intent_id=parent_exit_intent_id_from_order_intent(intent),
        execution_chain_id=str(intent.execution_chain_id or intent.intent_id),
        symbol=intent.symbol,
        market=intent.instrument_family,
        instrument_type=str(intent.product_type or ""),
        side=intent.side,
        position_side=position_side,
        intent_kind=intent_kind,
        target_exit_quantity=target_exit_quantity,
        target_exit_notional=intent.projected_notional,
        remaining_dispatchable_quantity=target_exit_quantity,
        remaining_unresolved_quantity=target_exit_quantity,
        metadata={
            "source_intent_id": intent.intent_id,
            "position_intent": intent.position_intent,
            "execution_action": intent.execution_action,
            "leg_action": intent.leg_action,
        },
    )


def create_exit_execution_intent_from_order_state(order_state: OrderState) -> ExitExecutionIntent:
    if not is_risk_reducing_order_state(order_state):
        raise ValueError("exit_execution_intent_requires_risk_reducing_order_state")
    intent_kind = _intent_kind(
        position_intent=order_state.position_intent,
        execution_action=order_state.execution_action,
        close_only=bool(order_state.close_only),
    )
    position_side = _position_side(
        pos_side=order_state.pos_side,
        position_intent=order_state.position_intent,
        exposure_side=order_state.exposure_side,
    )
    target_exit_quantity = max(Decimal(order_state.requested_qty), _ZERO)
    return ExitExecutionIntent(
        parent_intent_id=parent_exit_intent_id_from_order_state(order_state),
        execution_chain_id=str(order_state.execution_chain_id or order_state.intent_id),
        symbol=order_state.symbol,
        market=order_state.instrument_family,
        instrument_type=str(order_state.product_type or ""),
        side=_exit_side(
            position_intent=order_state.position_intent,
            position_side=position_side,
        ),
        position_side=position_side,
        intent_kind=intent_kind,
        target_exit_quantity=target_exit_quantity,
        remaining_dispatchable_quantity=target_exit_quantity,
        remaining_unresolved_quantity=target_exit_quantity,
        metadata={
            "source_intent_id": order_state.intent_id,
            "position_intent": order_state.position_intent,
            "execution_action": order_state.execution_action,
            "leg_action": order_state.leg_action,
        },
    )


def map_child_order_state_to_aggregate_category(order_state: OrderState) -> ChildExitOrderCategory:
    if unknown_write_state(order_state) is not None:
        return "UNKNOWN_TRUTH"
    if order_state.status in {"CREATED", "SUBMITTING"}:
        return "PENDING_DISPATCH"
    if order_state.status in {"SUBMITTED", "PARTIALLY_FILLED", "CANCEL_PENDING"}:
        return "WORKING"
    if Decimal(order_state.filled_qty) > _EPSILON and order_state.status in {
        "FILLED",
        "CANCELED",
        "REJECTED",
        "FAILED",
        "EXPIRED",
    }:
        return "TERMINAL_FILLED"
    return "TERMINAL_NONFILLED"


def child_exit_order_ref_from_order_state(
    *,
    parent_intent_id: str,
    order_state: OrderState,
    settings: AATSSettings | None = None,
) -> ChildExitOrderRef:
    aggregate_category = map_child_order_state_to_aggregate_category(order_state)
    remaining_quantity_estimate = _remaining_quantity_estimate(order_state=order_state)
    unresolved = unknown_write_state(order_state, settings=settings)
    return ChildExitOrderRef(
        parent_intent_id=parent_intent_id,
        child_order_id=order_state.client_order_id,
        client_order_id=order_state.client_order_id,
        exchange_order_id=order_state.exchange_order_id,
        execution_chain_id=order_state.execution_chain_id,
        intent_id=order_state.intent_id,
        symbol=order_state.symbol,
        planned_quantity=Decimal(order_state.requested_qty),
        known_filled_quantity=Decimal(order_state.filled_qty),
        remaining_quantity_estimate=remaining_quantity_estimate,
        child_status=order_state.status,
        aggregate_category=aggregate_category,
        write_confirmation_state="unknown" if unresolved is not None else "confirmed",
        exchange_truth_pending=unresolved is not None,
        operator_review_required=requires_unknown_write_review(order_state, settings=settings),
        risk_reducing_invariant=is_risk_reducing_order_state(order_state),
        updated_at=order_state.last_update_ts or utc_now(),
    )


def recompute_exit_execution_intent(
    *,
    parent_intent: ExitExecutionIntent,
    child_refs: list[ChildExitOrderRef],
) -> ExitExecutionIntent:
    now = utc_now()
    aggregated_filled_quantity = sum((Decimal(ref.known_filled_quantity) for ref in child_refs), _ZERO)
    aggregated_canceled_quantity = sum(
        (
            max(Decimal(ref.planned_quantity) - Decimal(ref.known_filled_quantity), _ZERO)
            for ref in child_refs
            if ref.aggregate_category == "TERMINAL_NONFILLED" and ref.child_status == "CANCELED"
        ),
        _ZERO,
    )
    aggregated_rejected_quantity = sum(
        (
            max(Decimal(ref.planned_quantity) - Decimal(ref.known_filled_quantity), _ZERO)
            for ref in child_refs
            if ref.aggregate_category == "TERMINAL_NONFILLED" and ref.child_status != "CANCELED"
        ),
        _ZERO,
    )
    open_child_working_quantity = sum(
        (
            Decimal(ref.remaining_quantity_estimate)
            for ref in child_refs
            if ref.aggregate_category in {"PENDING_DISPATCH", "WORKING"}
        ),
        _ZERO,
    )
    open_child_unknown_quantity = sum(
        (
            Decimal(ref.remaining_quantity_estimate)
            for ref in child_refs
            if ref.aggregate_category == "UNKNOWN_TRUTH"
        ),
        _ZERO,
    )
    remaining_unresolved_quantity = max(
        Decimal(parent_intent.target_exit_quantity) - aggregated_filled_quantity,
        _ZERO,
    )
    remaining_dispatchable_quantity = max(
        Decimal(parent_intent.target_exit_quantity)
        - aggregated_filled_quantity
        - open_child_working_quantity
        - open_child_unknown_quantity,
        _ZERO,
    )
    risk_reducing_invariant = all(ref.risk_reducing_invariant for ref in child_refs) if child_refs else True
    operator_review_required = any(ref.operator_review_required for ref in child_refs) or not risk_reducing_invariant
    operator_review_reason = (
        "child_risk_reducing_invariant_breached"
        if not risk_reducing_invariant
        else "child_unknown_truth_requires_review"
        if operator_review_required
        else None
    )
    reconciliation_state = (
        "review_required"
        if operator_review_required
        else "truth_pending"
        if any(ref.exchange_truth_pending for ref in child_refs)
        else "clean"
    )
    aggregate_status = derive_parent_status(
        parent_intent=parent_intent,
        child_refs=child_refs,
        aggregated_filled_quantity=aggregated_filled_quantity,
        operator_review_required=operator_review_required,
    )
    terminal_status = aggregate_status in {"COMPLETED", "CANCELED", "FAILED_SAFE"}
    completed_at = now if terminal_status else None
    if terminal_status and parent_intent.completed_at is not None:
        completed_at = parent_intent.completed_at
    return parent_intent.model_copy(
        update={
            "aggregated_filled_quantity": aggregated_filled_quantity,
            "aggregated_canceled_quantity": aggregated_canceled_quantity,
            "aggregated_rejected_quantity": aggregated_rejected_quantity,
            "open_child_working_quantity": open_child_working_quantity,
            "open_child_unknown_quantity": open_child_unknown_quantity,
            "remaining_dispatchable_quantity": remaining_dispatchable_quantity,
            "remaining_unresolved_quantity": remaining_unresolved_quantity,
            "aggregate_status": aggregate_status,
            "reconciliation_state": reconciliation_state,
            "risk_reducing_invariant": risk_reducing_invariant,
            "aggregate_version": int(parent_intent.aggregate_version) + 1,
            "child_order_ids": [ref.client_order_id for ref in child_refs],
            "operator_review_required": operator_review_required,
            "operator_review_reason": operator_review_reason,
            "updated_at": now,
            "completed_at": completed_at,
        }
    )


def derive_parent_status(
    *,
    parent_intent: ExitExecutionIntent,
    child_refs: list[ChildExitOrderRef],
    aggregated_filled_quantity: Decimal,
    operator_review_required: bool,
) -> str:
    if not child_refs:
        return "CREATED"
    if operator_review_required:
        return "REVIEW_REQUIRED"
    has_unknown = any(ref.aggregate_category == "UNKNOWN_TRUTH" for ref in child_refs)
    has_pending_dispatch = any(ref.aggregate_category == "PENDING_DISPATCH" for ref in child_refs)
    has_working = any(ref.aggregate_category == "WORKING" for ref in child_refs)
    if parent_intent.cancel_requested:
        if has_unknown or has_pending_dispatch or has_working:
            return "CANCEL_PENDING"
        if aggregated_filled_quantity >= Decimal(parent_intent.target_exit_quantity):
            return "COMPLETED"
        return "CANCELED"
    if has_unknown:
        return "PARTIALLY_FILLED" if aggregated_filled_quantity > _EPSILON else "WORKING"
    if has_pending_dispatch and not has_working:
        return "PARTIALLY_FILLED" if aggregated_filled_quantity > _EPSILON else "DISPATCHING"
    if has_working:
        return "PARTIALLY_FILLED" if aggregated_filled_quantity > _EPSILON else "WORKING"
    if aggregated_filled_quantity >= Decimal(parent_intent.target_exit_quantity):
        return "COMPLETED"
    if aggregated_filled_quantity > _EPSILON:
        return "PARTIALLY_FILLED"
    return "FAILED_SAFE"


def request_cancel_exit_execution_intent(parent_intent: ExitExecutionIntent) -> ExitExecutionIntent:
    return parent_intent.model_copy(
        update={
            "cancel_requested": True,
            "cancel_requested_ts": parent_intent.cancel_requested_ts or utc_now(),
            "updated_at": utc_now(),
        }
    )


def dispatch_template_from_parent(parent: ExitExecutionIntent) -> dict[str, Any] | None:
    template = parent.metadata.get("dispatch_template")
    return dict(template) if isinstance(template, dict) else None


def has_dispatch_template(parent: ExitExecutionIntent) -> bool:
    return dispatch_template_from_parent(parent) is not None


def resume_issue(parent: ExitExecutionIntent) -> dict[str, Any] | None:
    candidate = parent.metadata.get("resume_issue")
    return dict(candidate) if isinstance(candidate, dict) else None


def resume_issue_kind(parent: ExitExecutionIntent) -> str | None:
    issue = resume_issue(parent)
    return None if issue is None else str(issue.get("kind") or "").strip() or None


def record_resume_issue(
    parent: ExitExecutionIntent,
    *,
    kind: str,
    error: str | None = None,
) -> ExitExecutionIntent:
    metadata = dict(parent.metadata)
    # Task 142：若 parent 之前有**其他 kind** 的 resume_issue，保留旧 kind 到
    # 新 issue 的 prior_kind，防止 "childless 发现后静默覆盖
    # resume_limit_lookup_failed 原因" 这类无声丢失。下游 operator UI 可以
    # 按 prior_kind 展示链式诊断。
    prior_issue = metadata.get("resume_issue")
    prior_kind: str | None = None
    if isinstance(prior_issue, dict):
        prior_kind_raw = str(prior_issue.get("kind") or "").strip()
        if prior_kind_raw:
            prior_kind = prior_kind_raw
    issue: dict[str, Any] = {
        "kind": kind,
        "updated_at": utc_now().isoformat(),
        "operator_review_required": True,
    }
    if error:
        issue["error"] = error
    if prior_kind is not None and prior_kind != kind:
        issue["prior_kind"] = prior_kind
    metadata["resume_issue"] = issue
    return parent.model_copy(update={"metadata": metadata, "updated_at": utc_now()})


def clear_resume_issue(parent: ExitExecutionIntent, *, kind: str | None = None) -> ExitExecutionIntent:
    issue_kind = resume_issue_kind(parent)
    if "resume_issue" not in parent.metadata:
        return parent
    if kind is not None and issue_kind != kind:
        return parent
    metadata = dict(parent.metadata)
    metadata.pop("resume_issue", None)
    return parent.model_copy(update={"metadata": metadata, "updated_at": utc_now()})


def resume_block_reason(parent: ExitExecutionIntent) -> str | None:
    if parent.aggregate_status in _TERMINAL_PARENT_STATUSES:
        return "parent_terminal"
    if parent.cancel_requested or parent.aggregate_status == "CANCEL_PENDING":
        return "cancel_requested"
    if parent.operator_review_required or parent.aggregate_status == "REVIEW_REQUIRED":
        return "review_required"
    if resume_issue_kind(parent) == MISSING_CHILD_REFS_RESUME_ISSUE_KIND:
        return MISSING_CHILD_REFS_RESUME_ISSUE_KIND
    if Decimal(parent.open_child_unknown_quantity) > _EPSILON:
        return "unknown_child_truth_pending"
    if Decimal(parent.open_child_working_quantity) > _EPSILON:
        return "working_child_outstanding"
    if Decimal(parent.remaining_dispatchable_quantity) <= _EPSILON:
        return "no_remaining_dispatchable_quantity"
    if not has_dispatch_template(parent):
        return "dispatch_template_missing"
    return None


def resume_ready(parent: ExitExecutionIntent) -> bool:
    return resume_block_reason(parent) is None


def exit_execution_review_items(parent_intents: list[ExitExecutionIntent]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for parent in parent_intents:
        if parent.aggregate_status in _TERMINAL_PARENT_STATUSES:
            continue
        block_reason = resume_block_reason(parent)
        if parent.operator_review_required or parent.aggregate_status == "REVIEW_REQUIRED":
            items.append(
                _parent_review_detail(
                    parent=parent,
                    kind=EXIT_EXECUTION_PARENT_REVIEW_REQUIRED_KIND,
                    resume_block_reason=block_reason or "review_required",
                    review_required=True,
                )
            )
            continue
        if block_reason == "unknown_child_truth_pending" or parent.reconciliation_state == "truth_pending":
            items.append(
                _parent_review_detail(
                    parent=parent,
                    kind=EXIT_EXECUTION_TRUTH_PENDING_KIND,
                    resume_block_reason=block_reason or "unknown_child_truth_pending",
                    review_required=False,
                )
            )
            continue
        if resume_issue_kind(parent) == MISSING_CHILD_REFS_RESUME_ISSUE_KIND:
            items.append(
                _parent_resume_issue_detail(
                    parent=parent,
                    kind=EXIT_EXECUTION_MISSING_CHILD_REFS_KIND,
                    resume_block_reason=MISSING_CHILD_REFS_RESUME_ISSUE_KIND,
                )
            )
            continue
        if block_reason == "dispatch_template_missing":
            items.append(
                _parent_review_detail(
                    parent=parent,
                    kind=EXIT_EXECUTION_RESUME_TEMPLATE_MISSING_KIND,
                    resume_block_reason=block_reason,
                    review_required=True,
                )
            )
            continue
        if resume_issue_kind(parent) == RESUME_LIMIT_LOOKUP_FAILED_RESUME_ISSUE_KIND and _parent_has_pending_resume(parent):
            items.append(
                _parent_resume_issue_detail(
                    parent=parent,
                    kind=EXIT_EXECUTION_RESUME_LIMIT_LOOKUP_FAILED_KIND,
                    resume_block_reason=RESUME_LIMIT_LOOKUP_FAILED_RESUME_ISSUE_KIND,
                )
            )
    return items


def augment_reconciliation_report_with_exit_execution(
    *,
    report: ReconciliationReport,
    parent_intents: list[ExitExecutionIntent],
) -> ReconciliationReport:
    overlay_findings: list[ReconciliationFinding] = []
    overlay_details: list[dict[str, Any]] = []
    overlay_mismatch_categories: list[str] = []
    overlay_mismatch_reasons: list[str] = []
    overlay_safety_impacts: list[str] = []
    recommended_operator_action = report.recommended_operator_action

    for parent in parent_intents:
        block_reason = resume_block_reason(parent)
        if parent.operator_review_required or parent.aggregate_status == "REVIEW_REQUIRED":
            detail = _parent_review_detail(
                parent=parent,
                kind=EXIT_EXECUTION_PARENT_REVIEW_REQUIRED_KIND,
                resume_block_reason=block_reason or "review_required",
                review_required=True,
            )
            overlay_details.append(detail)
            overlay_findings.append(
                ReconciliationFinding(
                    reconciliation_id=report.reconciliation_id,
                    scope_kind="order",
                    scope_ref=parent.parent_intent_id,
                    product_type=report.product_type,
                    margin_mode=report.margin_mode,
                    primary_symbol=parent.symbol,
                    layer="structural",
                    finding_type=EXIT_EXECUTION_PARENT_REVIEW_REQUIRED_KIND,
                    severity_class="review",
                    structural=True,
                    review_required=True,
                    blocks_resume=True,
                    reason_code=str(parent.operator_review_reason or EXIT_EXECUTION_PARENT_REVIEW_REQUIRED_KIND),
                    details_json=detail,
                )
            )
            overlay_mismatch_categories.append(EXIT_EXECUTION_PARENT_REVIEW_REQUIRED_KIND)
            overlay_mismatch_reasons.append(str(parent.operator_review_reason or EXIT_EXECUTION_PARENT_REVIEW_REQUIRED_KIND))
            overlay_safety_impacts.append("operator_review_required_before_resuming_exit_execution")
            if recommended_operator_action in {None, "", "review_unknown_write_and_refresh_exchange_state"}:
                recommended_operator_action = "review_exit_execution_parent_and_refresh_exchange_state"
            continue
        if block_reason == "unknown_child_truth_pending" or parent.reconciliation_state == "truth_pending":
            detail = _parent_review_detail(
                parent=parent,
                kind=EXIT_EXECUTION_TRUTH_PENDING_KIND,
                resume_block_reason=block_reason or "unknown_child_truth_pending",
                review_required=False,
            )
            overlay_details.append(detail)
            overlay_findings.append(
                ReconciliationFinding(
                    reconciliation_id=report.reconciliation_id,
                    scope_kind="order",
                    scope_ref=parent.parent_intent_id,
                    product_type=report.product_type,
                    margin_mode=report.margin_mode,
                    primary_symbol=parent.symbol,
                    layer="structural",
                    finding_type=EXIT_EXECUTION_TRUTH_PENDING_KIND,
                    severity_class="soft",
                    structural=True,
                    review_required=False,
                    blocks_resume=True,
                    reason_code=block_reason or "unknown_child_truth_pending",
                    details_json=detail,
                )
            )
            overlay_mismatch_categories.append(EXIT_EXECUTION_TRUTH_PENDING_KIND)
            overlay_mismatch_reasons.append(block_reason or "unknown_child_truth_pending")
            overlay_safety_impacts.append("resume_blocked_until_exit_execution_truth_converges")
            if recommended_operator_action in {None, "", "review_unknown_write_and_refresh_exchange_state"}:
                recommended_operator_action = "review_exit_execution_parent_and_refresh_exchange_state"
            continue
        if resume_issue_kind(parent) == MISSING_CHILD_REFS_RESUME_ISSUE_KIND:
            detail = _parent_resume_issue_detail(
                parent=parent,
                kind=EXIT_EXECUTION_MISSING_CHILD_REFS_KIND,
                resume_block_reason=MISSING_CHILD_REFS_RESUME_ISSUE_KIND,
            )
            overlay_details.append(detail)
            overlay_findings.append(
                ReconciliationFinding(
                    reconciliation_id=report.reconciliation_id,
                    scope_kind="order",
                    scope_ref=parent.parent_intent_id,
                    product_type=report.product_type,
                    margin_mode=report.margin_mode,
                    primary_symbol=parent.symbol,
                    layer="structural",
                    finding_type=EXIT_EXECUTION_MISSING_CHILD_REFS_KIND,
                    severity_class="review",
                    structural=True,
                    review_required=True,
                    blocks_resume=True,
                    reason_code=EXIT_EXECUTION_MISSING_CHILD_REFS_KIND,
                    details_json=detail,
                )
            )
            overlay_mismatch_categories.append(EXIT_EXECUTION_MISSING_CHILD_REFS_KIND)
            overlay_mismatch_reasons.append(EXIT_EXECUTION_MISSING_CHILD_REFS_KIND)
            overlay_safety_impacts.append("operator_review_required_before_resuming_exit_execution")
            if recommended_operator_action in {None, "", "review_unknown_write_and_refresh_exchange_state"}:
                recommended_operator_action = "review_exit_execution_parent_and_refresh_exchange_state"
            continue
        if block_reason == "dispatch_template_missing":
            detail = _parent_review_detail(
                parent=parent,
                kind=EXIT_EXECUTION_RESUME_TEMPLATE_MISSING_KIND,
                resume_block_reason=block_reason,
                review_required=True,
            )
            overlay_details.append(detail)
            overlay_findings.append(
                ReconciliationFinding(
                    reconciliation_id=report.reconciliation_id,
                    scope_kind="order",
                    scope_ref=parent.parent_intent_id,
                    product_type=report.product_type,
                    margin_mode=report.margin_mode,
                    primary_symbol=parent.symbol,
                    layer="structural",
                    finding_type=EXIT_EXECUTION_RESUME_TEMPLATE_MISSING_KIND,
                    severity_class="review",
                    structural=True,
                    review_required=True,
                    blocks_resume=True,
                    reason_code=EXIT_EXECUTION_RESUME_TEMPLATE_MISSING_KIND,
                    details_json=detail,
                )
            )
            overlay_mismatch_categories.append(EXIT_EXECUTION_RESUME_TEMPLATE_MISSING_KIND)
            overlay_mismatch_reasons.append(EXIT_EXECUTION_RESUME_TEMPLATE_MISSING_KIND)
            overlay_safety_impacts.append("operator_review_required_before_resuming_exit_execution")
            if recommended_operator_action in {None, "", "review_unknown_write_and_refresh_exchange_state"}:
                recommended_operator_action = "review_exit_execution_parent_and_prepare_manual_exit_completion"
            continue
        if resume_issue_kind(parent) == RESUME_LIMIT_LOOKUP_FAILED_RESUME_ISSUE_KIND and _parent_has_pending_resume(parent):
            detail = _parent_resume_issue_detail(
                parent=parent,
                kind=EXIT_EXECUTION_RESUME_LIMIT_LOOKUP_FAILED_KIND,
                resume_block_reason=RESUME_LIMIT_LOOKUP_FAILED_RESUME_ISSUE_KIND,
            )
            overlay_details.append(detail)
            overlay_findings.append(
                ReconciliationFinding(
                    reconciliation_id=report.reconciliation_id,
                    scope_kind="order",
                    scope_ref=parent.parent_intent_id,
                    product_type=report.product_type,
                    margin_mode=report.margin_mode,
                    primary_symbol=parent.symbol,
                    layer="structural",
                    finding_type=EXIT_EXECUTION_RESUME_LIMIT_LOOKUP_FAILED_KIND,
                    severity_class="review",
                    structural=True,
                    review_required=True,
                    blocks_resume=True,
                    reason_code=EXIT_EXECUTION_RESUME_LIMIT_LOOKUP_FAILED_KIND,
                    details_json=detail,
                )
            )
            overlay_mismatch_categories.append(EXIT_EXECUTION_RESUME_LIMIT_LOOKUP_FAILED_KIND)
            overlay_mismatch_reasons.append(EXIT_EXECUTION_RESUME_LIMIT_LOOKUP_FAILED_KIND)
            overlay_safety_impacts.append("operator_review_required_before_resuming_exit_execution")
            if recommended_operator_action in {None, "", "review_unknown_write_and_refresh_exchange_state"}:
                recommended_operator_action = "review_exit_execution_parent_and_retry_limit_lookup"

    if not overlay_findings and not overlay_details:
        return report

    findings = [*report.findings, *overlay_findings]
    review_required = bool(report.review_required or any(finding.review_required for finding in overlay_findings))
    halt_required = bool(report.halt_required or any(finding.halt_required for finding in overlay_findings))
    structural_review_required = bool(
        report.structural_review_required
        or any(
            finding.structural and (finding.review_required or finding.halt_required)
            for finding in overlay_findings
        )
    )
    financial_review_required = bool(
        report.financial_review_required
        or any(
            finding.financial and (finding.review_required or finding.halt_required)
            for finding in overlay_findings
        )
    )
    severity = report.severity
    if halt_required:
        severity = "HARD_MISMATCH"
    elif review_required:
        severity = "REVIEW_REQUIRED"
    elif severity == "CLEAN":
        severity = "SOFT_MISMATCH"
    return report.model_copy(
        update={
            "findings": findings,
            "finding_summary": _finding_summary(findings),
            "mismatch_categories": _ordered_unique([*report.mismatch_categories, *overlay_mismatch_categories]),
            "mismatch_reasons": _ordered_unique([*report.mismatch_reasons, *overlay_mismatch_reasons]),
            "unknown_state_details": [*report.unknown_state_details, *overlay_details],
            "safety_impacts": _ordered_unique([*report.safety_impacts, *overlay_safety_impacts]),
            "severity": severity,
            "review_required": review_required,
            "halt_required": halt_required,
            "structural_review_required": structural_review_required,
            "financial_review_required": financial_review_required,
            "recommended_operator_action": recommended_operator_action,
            "remediation_action": recommended_operator_action,
            "observational_only": False,
        }
    )


def _mark_parent_missing_child_refs(parent: ExitExecutionIntent) -> ExitExecutionIntent:
    """给无 child refs 的非终态 parent 打 missing_child_refs_for_parent
    structural resume issue。Task 142 收口：

    - 若 parent 已带同 kind 的 issue：直接返回，`record_resume_issue` 不被
      再次调用，避免 `updated_at` 无意义漂移但仍保留原 issue 语义。调用侧
      仍会再 save —— save 是幂等写路径，对应 DB 外键/去重已处理。
    - 若 parent 有别的 kind 的 resume_issue（如 resume_limit_lookup_failed）
      但当前 childless：`record_resume_issue` 会把旧 kind 写入 prior_kind，
      所以新 issue 不会静默吞掉原诊断信号。

    child refs 重建时由 caller 调 `clear_resume_issue(..., kind=<this>)`
    清除（clear 会整体 pop resume_issue，prior_kind 也一并清）。
    """
    if resume_issue_kind(parent) == MISSING_CHILD_REFS_RESUME_ISSUE_KIND:
        return parent
    return record_resume_issue(
        parent,
        kind=MISSING_CHILD_REFS_RESUME_ISSUE_KIND,
        error="child_refs_not_reconstructable",
    )


def refresh_exit_execution_intents(
    *,
    execution_repo: ExecutionRepository,
    exit_execution_repo: ExitExecutionRepository,
    settings: AATSSettings,
    scope: object | None = None,
    exit_execution_writer: ExitExecutionWriter | None = None,
) -> list[ExitExecutionIntent]:
    writer = exit_execution_writer or ExitExecutionWriter(exit_execution_repo)
    order_states = execution_repo.order_states()
    states_by_client_order_id = {
        state.client_order_id: state
        for state in order_states
        if str(state.client_order_id or "").strip()
    }
    states_by_execution_chain: dict[str, list[OrderState]] = {}
    for state in order_states:
        execution_chain_id = str(state.execution_chain_id or "").strip()
        if not execution_chain_id:
            continue
        states_by_execution_chain.setdefault(execution_chain_id, []).append(state)

    refreshed: list[ExitExecutionIntent] = []
    for parent in exit_execution_repo.list_exit_execution_intents():
        if not _parent_in_scope(parent=parent, scope=scope):
            continue
        if parent.aggregate_status in _TERMINAL_PARENT_STATUSES:
            # Terminal parents do not block resume and writer sticky semantics
            # already prevent them from being reopened by aggregate refresh.
            # Skipping them keeps recovery/rebaseline validation bounded to
            # live unresolved exit work instead of rewriting historical parents.
            continue
        child_refs = _refreshed_child_refs_for_parent(
            parent=parent,
            settings=settings,
            states_by_client_order_id=states_by_client_order_id,
            states_by_execution_chain=states_by_execution_chain,
            exit_execution_repo=exit_execution_repo,
        )
        if not child_refs:
            if parent.aggregate_status in _TERMINAL_PARENT_STATUSES:
                continue
            childless_parent = _mark_parent_missing_child_refs(parent)
            refreshed.append(
                writer.save_exit_execution_intent(
                    childless_parent,
                    source_component="exit_intent_aggregator",
                    reason_code="missing_child_refs_refresh",
                )
            )
            continue
        refreshed.append(
            writer.save_child_refs_and_recompute_parent(
                parent_intent=parent,
                child_refs=child_refs,
                transform_parent=lambda parent_intent: clear_resume_issue(
                    parent_intent,
                    kind=MISSING_CHILD_REFS_RESUME_ISSUE_KIND,
                ),
                recompute_parent=lambda parent_intent, persisted_child_refs: recompute_exit_execution_intent(
                    parent_intent=parent_intent,
                    child_refs=persisted_child_refs,
                ),
                source_component="exit_intent_aggregator",
                reason_code="refresh_parent_from_child_refs",
            )
        )
    return refreshed


def _position_side(
    *,
    pos_side: str | None,
    position_intent: str | None,
    exposure_side: str | None,
) -> str | None:
    normalized_pos_side = str(pos_side or "").strip().lower()
    if normalized_pos_side in {"long", "short", "net"}:
        return normalized_pos_side
    derived = pos_side_from_position_intent(
        position_intent=position_intent,
        position_mode="long_short_mode",
    )
    if derived in {"long", "short", "net"}:
        return derived
    normalized_exposure = str(exposure_side or "").strip().lower()
    if normalized_exposure in {"long", "short"}:
        return normalized_exposure
    return None


def _intent_kind(
    *,
    position_intent: str | None,
    execution_action: str | None,
    close_only: bool,
) -> str:
    normalized_position_intent = str(position_intent or "").strip().lower()
    normalized_execution_action = str(execution_action or "").strip().lower()
    if normalized_position_intent.startswith("close_") or close_only:
        return "close"
    if normalized_execution_action == "exit":
        return "flatten"
    return "reduce"


def _exit_side(
    *,
    position_intent: str | None,
    position_side: str | None,
) -> str:
    normalized_position_intent = str(position_intent or "").strip().lower()
    if normalized_position_intent.endswith("long"):
        return "sell"
    if normalized_position_intent.endswith("short"):
        return "buy"
    return "sell" if position_side == "long" else "buy"


def _remaining_quantity_estimate(*, order_state: OrderState) -> Decimal:
    remaining_qty = Decimal(order_state.remaining_qty)
    planned_remaining = max(Decimal(order_state.requested_qty) - Decimal(order_state.filled_qty), _ZERO)
    if remaining_qty > _EPSILON:
        return max(remaining_qty, planned_remaining)
    return planned_remaining


def _parent_in_scope(*, parent: ExitExecutionIntent, scope: object | None) -> bool:
    if scope is None:
        return True
    symbol_allowed = getattr(scope, "symbol_allowed", None)
    if callable(symbol_allowed):
        return bool(symbol_allowed(parent.symbol))
    return True


def _refreshed_child_refs_for_parent(
    *,
    parent: ExitExecutionIntent,
    settings: AATSSettings,
    states_by_client_order_id: dict[str, OrderState],
    states_by_execution_chain: dict[str, list[OrderState]],
    exit_execution_repo: ExitExecutionRepository,
) -> list[ChildExitOrderRef]:
    refreshed_by_child_id: dict[str, ChildExitOrderRef] = {}
    for existing_ref in exit_execution_repo.child_refs_for_parent(parent_intent_id=parent.parent_intent_id):
        state = states_by_client_order_id.get(existing_ref.client_order_id)
        if state is None:
            refreshed_by_child_id[existing_ref.client_order_id] = existing_ref
            continue
        refreshed_ref = child_exit_order_ref_from_order_state(
            parent_intent_id=parent.parent_intent_id,
            order_state=state,
            settings=settings,
        )
        refreshed_by_child_id[refreshed_ref.client_order_id] = refreshed_ref
    for state in states_by_execution_chain.get(parent.execution_chain_id, []):
        if not is_risk_reducing_order_state(state):
            continue
        if state.client_order_id in refreshed_by_child_id:
            continue
        refreshed_ref = child_exit_order_ref_from_order_state(
            parent_intent_id=parent.parent_intent_id,
            order_state=state,
            settings=settings,
        )
        refreshed_by_child_id[refreshed_ref.client_order_id] = refreshed_ref
    return list(refreshed_by_child_id.values())


def _parent_review_detail(
    *,
    parent: ExitExecutionIntent,
    kind: str,
    resume_block_reason: str,
    review_required: bool,
) -> dict[str, Any]:
    available_operator_actions = ["refresh_exchange_state", "safe_cancel"]
    if kind == EXIT_EXECUTION_RESUME_LIMIT_LOOKUP_FAILED_KIND:
        available_operator_actions.insert(1, "retry_limit_lookup")
    return {
        "kind": kind,
        "symbol": parent.symbol,
        "parent_intent_id": parent.parent_intent_id,
        "execution_chain_id": parent.execution_chain_id,
        "aggregate_status": parent.aggregate_status,
        "reconciliation_state": parent.reconciliation_state,
        "target_exit_quantity": parent.target_exit_quantity,
        "aggregated_filled_quantity": parent.aggregated_filled_quantity,
        "open_child_working_quantity": parent.open_child_working_quantity,
        "open_child_unknown_quantity": parent.open_child_unknown_quantity,
        "remaining_dispatchable_quantity": parent.remaining_dispatchable_quantity,
        "remaining_unresolved_quantity": parent.remaining_unresolved_quantity,
        "blocks_resume": True,
        "operator_review_required": review_required,
        "operator_review_reason": parent.operator_review_reason,
        "cancel_requested": parent.cancel_requested,
        "child_order_ids": list(parent.child_order_ids),
        "resume_block_reason": resume_block_reason,
        "dispatch_template_available": has_dispatch_template(parent),
        "resume_ready": resume_ready(parent),
        "resume_issue_kind": resume_issue_kind(parent),
        "available_operator_actions": available_operator_actions,
    }


def _parent_resume_issue_detail(
    *,
    parent: ExitExecutionIntent,
    kind: str,
    resume_block_reason: str,
) -> dict[str, Any]:
    detail = _parent_review_detail(
        parent=parent,
        kind=kind,
        resume_block_reason=resume_block_reason,
        review_required=True,
    )
    issue = resume_issue(parent) or {}
    detail["resume_issue"] = issue
    detail["operator_review_reason"] = str(issue.get("kind") or detail.get("operator_review_reason") or "")
    return detail


def _parent_has_pending_resume(parent: ExitExecutionIntent) -> bool:
    return (
        parent.aggregate_status not in _TERMINAL_PARENT_STATUSES
        and not parent.cancel_requested
        and Decimal(parent.remaining_dispatchable_quantity) > _EPSILON
        and Decimal(parent.open_child_working_quantity) <= _EPSILON
        and Decimal(parent.open_child_unknown_quantity) <= _EPSILON
    )




def _finding_summary(findings: list[ReconciliationFinding]) -> dict[str, object]:
    summary: dict[str, object] = {
        "total_count": len(findings),
        "structural_count": 0,
        "financial_count": 0,
        "observational_count": 0,
        "review_required_count": 0,
        "halt_required_count": 0,
        "blocks_resume_count": 0,
        "severity_counts": {"info": 0, "soft": 0, "review": 0, "halt": 0},
    }
    for finding in findings:
        if finding.structural:
            summary["structural_count"] = int(summary["structural_count"]) + 1
        if finding.financial:
            summary["financial_count"] = int(summary["financial_count"]) + 1
        if finding.observational:
            summary["observational_count"] = int(summary["observational_count"]) + 1
        if finding.review_required:
            summary["review_required_count"] = int(summary["review_required_count"]) + 1
        if finding.halt_required:
            summary["halt_required_count"] = int(summary["halt_required_count"]) + 1
        if finding.blocks_resume:
            summary["blocks_resume_count"] = int(summary["blocks_resume_count"]) + 1
        severity_counts = dict(summary["severity_counts"])
        severity_counts[finding.severity_class] = int(severity_counts.get(finding.severity_class, 0)) + 1
        summary["severity_counts"] = severity_counts
    return summary
