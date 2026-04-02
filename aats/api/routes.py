from __future__ import annotations

from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from aats.api.auth import OperatorPrincipal, require_admin_access, require_read_access, require_write_access
from aats.bootstrap.config import ApplicationRuntime
from aats.schemas.system import RuntimeModeState
from aats.services.operator.query_service import OperatorQueryService

router = APIRouter(dependencies=[Depends(require_read_access)])


class ModeUpdateRequest(BaseModel):
    mode: str


class HaltRequest(BaseModel):
    reason: str = "manual_halt"


class ResumeRequest(BaseModel):
    reason: str = "manual_resume"


class CancelOrderRequest(BaseModel):
    reason: str = "operator_cancel"


class ResolveStuckSubmissionRequest(BaseModel):
    reason: str = "operator_resolve_stuck_submission"


class ValidationRequest(BaseModel):
    reason: str = "operator_validate"


class RebaselineRequest(BaseModel):
    reason: str = "operator_rebaseline"


class BlockerActionRequest(BaseModel):
    panel_version: str | None = None
    blocker: str | None = None
    parent_intent_id: str | None = None
    reason: str | None = None


class ScalingReviewRequest(BaseModel):
    verdict: Literal[
        "approve_scale_up",
        "continue_small_capital",
        "shrink_trial",
        "pause_trial",
    ]
    reason: str = "ui_capital_scale_review"


class TrialReviewRecordRequest(BaseModel):
    reason: str = "ui_trial_review_snapshot"


class TrialReviewActionRequest(BaseModel):
    action_type: Literal[
        "review_snapshot",
        "reset_trial_guard",
        "continue_small_capital",
        "shrink_trial",
        "pause_trial",
        "approve_scale_up",
    ]
    reason: str = "ui_trial_review_action"


class ExitExecutionActionRequest(BaseModel):
    reason: str
    parent_intent_id: str | None = None


def _runtime(request: Request) -> ApplicationRuntime:
    return cast(ApplicationRuntime, request.app.state.runtime)


def _query(request: Request) -> OperatorQueryService:
    return OperatorQueryService(_runtime(request))


@router.get("/system/health")
async def system_health(request: Request) -> dict[str, Any]:
    query = _query(request)
    health = query.system_health()
    operator_metrics = query.metrics()
    health["execution_summary"] = {
        "order_count": len(query._scoped_order_states()),
        "fill_count": len(query._scoped_fills()),
        "open_order_count": len(query._scoped_open_order_states()),
        "order_intents_generated": _runtime(request).metrics.snapshot().get("order_intents_generated", 0),
        "fills_processed": _runtime(request).metrics.snapshot().get("fills_processed", 0),
        "processing_failures": operator_metrics.get("processing_failure_count", 0),
        "portfolio_snapshot_repairs": operator_metrics.get("portfolio_snapshot_repair_count", 0),
        "fills_without_snapshot": operator_metrics.get("fill_without_snapshot_count", 0),
        "snapshots_without_reconciliation": operator_metrics.get("snapshot_without_reconciliation_count", 0),
        "phase1_shadow_status": operator_metrics.get("phase1_shadow", {}).get("status"),
        "phase1_shadow_failure_count": operator_metrics.get("phase1_shadow_failure_count", 0),
        "phase1_shadow_alert_count": operator_metrics.get("phase1_shadow_alert_count", 0),
        "phase1_shadow_recovery_count": operator_metrics.get("phase1_shadow_recovery_count", 0),
        "phase1_shadow_order_backlog": operator_metrics.get("phase1_shadow_order_backlog"),
        "phase1_shadow_fill_backlog": operator_metrics.get("phase1_shadow_fill_backlog"),
        "phase1_shadow_obligation_backlog": operator_metrics.get("phase1_shadow_obligation_backlog"),
    }
    return health


@router.get("/system/mode")
async def system_mode(request: Request) -> dict[str, Any]:
    return RuntimeModeState(**_query(request).system_mode()).model_dump(mode="json")


@router.get("/system/runtime")
async def system_runtime(request: Request) -> dict[str, Any]:
    return _query(request).system_runtime()


@router.get("/system/blockers")
async def system_blockers(request: Request) -> dict[str, Any]:
    blockers = _query(request).blockers()
    return {
        "blocked": bool(blockers),
        "halted": _runtime(request).kill_switch.halted,
        "blockers": blockers,
        "recent_history": _query(request).blocker_history(limit=20, offset=0)["history"],
    }


@router.get("/system/blocker-control")
async def system_blocker_control(request: Request) -> dict[str, Any]:
    return _query(request).blocker_control()


@router.get("/system/blocker-history")
async def system_blocker_history(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=5000),
) -> dict[str, Any]:
    return _query(request).blocker_history(limit=limit, offset=offset)


@router.get("/system/metrics")
async def system_metrics(request: Request) -> dict[str, Any]:
    return _query(request).metrics()


@router.get("/system/shadow")
async def system_shadow(request: Request) -> dict[str, Any]:
    return _query(request).phase1_shadow()


@router.get("/system/shadow/history")
async def system_shadow_history(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=5000),
) -> dict[str, Any]:
    return _query(request).phase1_shadow_history(limit=limit, offset=offset)


@router.post("/system/mode")
async def set_system_mode(
    request: Request,
    payload: ModeUpdateRequest,
    _: OperatorPrincipal = Depends(require_write_access),
) -> dict[str, Any]:
    runtime = _runtime(request)
    if payload.mode == "autonomous_live":
        raise HTTPException(status_code=400, detail="autonomous_live is not supported in this prototype")
    if payload.mode not in {"backtest", "paper_live", "guarded_live"}:
        raise HTTPException(status_code=400, detail=f"unsupported mode={payload.mode}")
    if payload.mode != runtime.mode_controller.mode:
        raise HTTPException(
            status_code=409,
            detail="runtime_mode_hot_swap_not_supported_restart_required",
        )
    return RuntimeModeState(**_query(request).system_mode()).model_dump(mode="json")


@router.post("/system/halt")
@router.post("/halt")
async def halt(
    request: Request,
    payload: HaltRequest | None = None,
    reason: str | None = None,
    principal: OperatorPrincipal = Depends(require_admin_access),
) -> dict[str, Any]:
    halt_reason = reason or (payload.reason if payload is not None else "manual_halt")
    result = _query(request).halt(
        reason=halt_reason,
        actor_role=principal.role,
        actor_identity=principal.identity,
        auth_source=principal.auth_source,
    )
    result["mode"] = RuntimeModeState(**_query(request).system_mode()).model_dump(mode="json")
    result["blockers"] = _query(request).blockers()
    return result


@router.post("/system/resume")
@router.post("/resume")
async def resume(
    request: Request,
    payload: ResumeRequest | None = None,
    principal: OperatorPrincipal = Depends(require_admin_access),
) -> dict[str, Any]:
    resume_reason = payload.reason if payload is not None else "manual_resume"
    result = await _query(request).resume(
        reason=resume_reason,
        actor_role=principal.role,
        actor_identity=principal.identity,
        auth_source=principal.auth_source,
    )
    result["mode"] = RuntimeModeState(**_query(request).system_mode()).model_dump(mode="json")
    return result


@router.get("/system/recovery")
async def system_recovery(request: Request) -> dict[str, Any]:
    return _query(request).system_recovery()


@router.post("/system/rebaseline")
async def system_rebaseline(
    request: Request,
    payload: RebaselineRequest | None = None,
    principal: OperatorPrincipal = Depends(require_admin_access),
) -> dict[str, Any]:
    reason = payload.reason if payload is not None else "operator_rebaseline"
    try:
        return await _query(request).rebaseline(
            reason=reason,
            actor_role=principal.role,
            actor_identity=principal.identity,
            auth_source=principal.auth_source,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/system/ai-review/restore")
async def system_ai_review_restore(
    request: Request,
    payload: ResumeRequest | None = None,
    principal: OperatorPrincipal = Depends(require_admin_access),
) -> dict[str, Any]:
    reason = payload.reason if payload is not None else "operator_restore_ai_review"
    return _query(request).ai_review_restore(
        reason=reason,
        actor_role=principal.role,
        actor_identity=principal.identity,
        auth_source=principal.auth_source,
    )


@router.post("/system/ai-review/degrade-to-baseline")
async def system_ai_review_degrade_to_baseline(
    request: Request,
    payload: ResumeRequest | None = None,
    principal: OperatorPrincipal = Depends(require_admin_access),
) -> dict[str, Any]:
    reason = payload.reason if payload is not None else "operator_degrade_ai_review_to_baseline"
    return _query(request).ai_review_degrade_to_baseline(
        reason=reason,
        actor_role=principal.role,
        actor_identity=principal.identity,
        auth_source=principal.auth_source,
    )


@router.post("/system/blocker-actions/{action_id}")
async def system_blocker_action(
    request: Request,
    action_id: str,
    payload: BlockerActionRequest | None = None,
    principal: OperatorPrincipal = Depends(require_admin_access),
) -> dict[str, Any]:
    body = payload or BlockerActionRequest()
    default_reason_map = {
        "reconcile-now": "blocker_reconcile_now",
        "accept-rebaseline": "blocker_accept_rebaseline",
        "resume-system": "blocker_resume_system",
        "halt-system": "blocker_keep_halted",
        "refresh-exchange-state": "blocker_refresh_exchange_state",
        "acknowledge-phase1-shadow": "blocker_phase1_shadow_review",
        "ai-review-restore": "blocker_ai_review_restore",
        "ai-review-degrade-to-baseline": "blocker_ai_review_degrade_to_baseline",
    }
    try:
        return await _query(request).perform_blocker_action(
            action_id=action_id,
            panel_version=body.panel_version,
            blocker=body.blocker,
            parent_intent_id=body.parent_intent_id,
            reason=body.reason or default_reason_map.get(action_id, f"blocker_action:{action_id}"),
            actor_role=principal.role,
            actor_identity=principal.identity,
            auth_source=principal.auth_source,
        )
    except ValueError as exc:
        detail = str(exc)
        status = 409 if detail in {"blocker_control_state_changed"} or detail.startswith("blocker_not_active:") else 400
        raise HTTPException(status_code=status, detail=detail) from exc


@router.post("/system/exit-execution/retry-limit-lookup")
async def retry_exit_execution_limit_lookup(
    request: Request,
    payload: ExitExecutionActionRequest | None = None,
    principal: OperatorPrincipal = Depends(require_admin_access),
) -> dict[str, Any]:
    body = payload or ExitExecutionActionRequest(reason="operator_retry_limit_lookup")
    try:
        return await _query(request).retry_limit_lookup(
            parent_intent_id=body.parent_intent_id,
            reason=body.reason,
            actor_role=principal.role,
            actor_identity=principal.identity,
            auth_source=principal.auth_source,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/system/exit-execution/refresh")
async def refresh_exit_execution_state(
    request: Request,
    payload: ExitExecutionActionRequest | None = None,
    principal: OperatorPrincipal = Depends(require_write_access),
) -> dict[str, Any]:
    body = payload or ExitExecutionActionRequest(reason="operator_refresh_exit_execution_state")
    try:
        return await _query(request).refresh_exchange_state(
            blocker=None,
            parent_intent_id=body.parent_intent_id,
            reason=body.reason,
            actor_role=principal.role,
            actor_identity=principal.identity,
            auth_source=principal.auth_source,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/system/exit-execution/safe-cancel")
async def safe_cancel_exit_execution(
    request: Request,
    payload: ExitExecutionActionRequest | None = None,
    principal: OperatorPrincipal = Depends(require_write_access),
) -> dict[str, Any]:
    body = payload or ExitExecutionActionRequest(reason="operator_safe_cancel_exit_execution")
    try:
        return await _query(request).safe_cancel_exit_execution(
            parent_intent_id=body.parent_intent_id,
            reason=body.reason,
            actor_role=principal.role,
            actor_identity=principal.identity,
            auth_source=principal.auth_source,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/system/exit-execution/action-history")
async def exit_execution_action_history(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=5000),
    parent_intent_id: str | None = Query(default=None),
    action: Literal["refresh_exchange_state", "retry_limit_lookup", "safe_cancel"] | None = Query(default=None),
    actor: str | None = Query(default=None),
    window_hours: int | None = Query(default=None, ge=1, le=24 * 30),
) -> dict[str, Any]:
    return _query(request).exit_execution_action_history(
        limit=limit,
        offset=offset,
        parent_intent_id=parent_intent_id,
        action=action,
        actor=actor,
        window_hours=window_hours,
    )


@router.get("/decision/latest")
async def latest_decision(request: Request) -> dict[str, Any]:
    return _query(request).latest_decision()


@router.get("/strategy/runtime")
async def strategy_runtime(request: Request) -> dict[str, Any]:
    return _query(request).strategy_runtime()


@router.get("/decision/recent")
async def recent_decisions(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=5000),
) -> dict[str, Any]:
    return _query(request).recent_decisions(limit=limit, offset=offset)


@router.get("/decision/{decision_id}")
async def decision_detail(request: Request, decision_id: str) -> dict[str, Any]:
    try:
        return _query(request).decision_view(decision_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/ai/runtime")
async def ai_runtime(request: Request) -> dict[str, Any]:
    return _query(request).ai_runtime()


@router.get("/ai/overview")
async def ai_overview(request: Request) -> dict[str, Any]:
    return _query(request).ai_overview()


@router.get("/ai-config/summary")
async def ai_config_summary(request: Request) -> dict[str, Any]:
    return _query(request).ai_config_summary()


@router.get("/ai/performance/overview")
async def ai_performance_overview(request: Request) -> dict[str, Any]:
    return _query(request).ai_performance_overview()


@router.get("/ai/latest")
async def ai_latest(request: Request) -> dict[str, Any]:
    return _query(request).ai_latest()


@router.get("/ai/recent")
async def ai_recent(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=5000),
) -> dict[str, Any]:
    return _query(request).ai_recent(limit=limit, offset=offset)


@router.get("/ai/shadow/latest")
async def ai_shadow_latest(request: Request) -> dict[str, Any]:
    return _query(request).ai_shadow_latest()


@router.get("/ai/shadow/recent")
async def ai_shadow_recent(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=5000),
) -> dict[str, Any]:
    return _query(request).ai_shadow_recent(limit=limit, offset=offset)


@router.get("/ai/shadow/evaluations")
async def ai_shadow_evaluations(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=5000),
) -> dict[str, Any]:
    return _query(request).ai_shadow_evaluations(limit=limit, offset=offset)


@router.get("/ai/performance/reports")
async def ai_performance_reports(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=5000),
) -> dict[str, Any]:
    return _query(request).ai_performance_reports(limit=limit, offset=offset)


@router.get("/risk/latest")
async def latest_risk(request: Request) -> dict[str, Any]:
    payload = _query(request).latest_risk()
    return {"decision_id": payload["decision_id"], "risk_decision": payload["payload"]}


@router.get("/risk/recent")
async def recent_risk(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=5000),
) -> dict[str, Any]:
    return _query(request).recent_risks(limit=limit, offset=offset)


@router.get("/risk/margin-buffer")
async def margin_buffer_risk(request: Request) -> dict[str, Any]:
    return _query(request).margin_buffer_risk()


@router.get("/system/guarded-live-preflight")
async def guarded_live_preflight(request: Request) -> dict[str, Any]:
    return _query(request).guarded_live_preflight()


@router.get("/policy/latest")
async def latest_policy(request: Request) -> dict[str, Any]:
    payload = _query(request).latest_policy()
    return {"decision_id": payload["decision_id"], "policy_decision": payload["payload"]}


@router.get("/policy/recent")
async def recent_policy(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=5000),
) -> dict[str, Any]:
    return _query(request).recent_policies(limit=limit, offset=offset)


@router.get("/portfolio")
@router.get("/portfolio/latest")
async def portfolio_latest(request: Request) -> dict[str, Any]:
    return _query(request).portfolio_latest()


@router.get("/portfolio/history")
async def portfolio_history(request: Request, limit: int = Query(default=20, ge=1, le=200)) -> dict[str, Any]:
    return _query(request).portfolio_history(limit=limit)


@router.get("/balances")
async def balances(request: Request) -> dict[str, Any]:
    return _query(request).balances()


@router.get("/positions")
async def positions(request: Request) -> dict[str, Any]:
    return _query(request).positions()


@router.get("/account/state")
async def account_state(request: Request) -> dict[str, Any]:
    return _query(request).account_state()


@router.get("/system/trial-guard")
async def system_trial_guard(request: Request) -> dict[str, Any]:
    return _query(request).trial_guard()


@router.get("/account/open-orders")
async def account_open_orders(request: Request) -> dict[str, Any]:
    return _query(request).account_open_orders()


@router.get("/account/recent-fills")
async def account_recent_fills(request: Request) -> dict[str, Any]:
    return _query(request).account_recent_fills()


@router.get("/account/recent-bills")
async def account_recent_bills(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    return await _query(request).account_recent_bills(limit=limit)


@router.get("/account/recent-funding-fees")
async def account_recent_funding_fees(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    return _query(request).account_recent_funding_fees(limit=limit)


@router.get("/orders/open")
async def open_orders(request: Request) -> dict[str, Any]:
    return _query(request).orders_open()


@router.get("/orders/latest")
async def latest_order(request: Request) -> dict[str, Any]:
    order = _query(request).latest_order()
    if isinstance(order, dict):
        return {"order": order}
    return {"order": order.model_dump(mode="json") if order is not None else None}


@router.get("/orders/recent")
async def recent_orders(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=5000),
) -> dict[str, Any]:
    return _query(request).orders_recent(limit=limit, offset=offset)


@router.get("/orders/partial")
async def partial_orders(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    return {
        "orders": [
            order.model_dump(mode="json")
            for order in runtime.execution_repo.recent_order_states(limit=50, statuses=("PARTIALLY_FILLED", "CANCEL_PENDING"))
        ]
    }


@router.get("/orders/canceled")
async def canceled_orders(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    return {
        "orders": [
            order.model_dump(mode="json")
            for order in runtime.execution_repo.recent_order_states(limit=50, statuses=("CANCELED",))
        ]
    }


@router.get("/orders/{client_order_id}")
async def order_detail(request: Request, client_order_id: str) -> dict[str, Any]:
    try:
        return _query(request).order_detail(client_order_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/orders/{client_order_id}/cancel")
async def cancel_order(
    request: Request,
    client_order_id: str,
    payload: CancelOrderRequest | None = None,
    principal: OperatorPrincipal = Depends(require_write_access),
) -> dict[str, Any]:
    try:
        return await _query(request).cancel_order(
            client_order_id=client_order_id,
            reason=payload.reason if payload is not None else "operator_cancel",
            actor_role=principal.role,
            actor_identity=principal.identity,
            auth_source=principal.auth_source,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/orders/{client_order_id}/resolve-stuck-submission")
async def resolve_stuck_submission(
    request: Request,
    client_order_id: str,
    payload: ResolveStuckSubmissionRequest | None = None,
    principal: OperatorPrincipal = Depends(require_admin_access),
) -> dict[str, Any]:
    try:
        return await _query(request).resolve_stuck_submission(
            client_order_id=client_order_id,
            reason=payload.reason if payload is not None else "operator_resolve_stuck_submission",
            actor_role=principal.role,
            actor_identity=principal.identity,
            auth_source=principal.auth_source,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/fills/latest")
async def latest_fill(request: Request) -> dict[str, Any]:
    query = _query(request)
    local_fill = query.latest_fill()
    exchange = _runtime(request).account_service.latest_snapshot()
    exchange_fill = exchange.fills[0] if exchange is not None and exchange.fills else None
    return {
        "local_fill": local_fill if isinstance(local_fill, dict) else local_fill.model_dump(mode="json") if local_fill is not None else None,
        "exchange_fill": exchange_fill.model_dump(mode="json") if exchange_fill is not None else None,
    }


@router.get("/fills/recent")
async def recent_fills(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=5000),
) -> dict[str, Any]:
    return _query(request).fills_recent(limit=limit, offset=offset)


@router.get("/fills/{fill_id}")
async def fill_detail(request: Request, fill_id: str) -> dict[str, Any]:
    try:
        return _query(request).fill_detail(fill_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/execution/latest")
@router.get("/execution/result/latest")
async def latest_execution(request: Request) -> dict[str, Any]:
    return _query(request).execution_latest()


@router.get("/execution/errors")
async def execution_errors(request: Request) -> dict[str, Any]:
    return _query(request).execution_errors()


@router.get("/reports/execution-quality")
async def execution_quality_report(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=5000),
) -> dict[str, Any]:
    return _query(request).execution_quality_report(limit=limit, offset=offset)


@router.get("/reports/execution-attempts")
async def execution_attempt_report(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=5000),
) -> dict[str, Any]:
    return _query(request).execution_attempt_report(limit=limit, offset=offset)


@router.get("/reports/profitability-overview")
async def profitability_overview(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    return _query(request).profitability_overview(limit=limit)


@router.get("/reports/position-lifecycle-profitability")
async def position_lifecycle_profitability(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    return _query(request).position_lifecycle_profitability(limit=limit)


@router.get("/reports/strategy-segments")
async def strategy_segment_report(
    request: Request,
    limit: int = Query(default=200, ge=1, le=1000),
    group_by: str = Query(default="symbol,market_regime,side,execution_action"),
) -> dict[str, Any]:
    dimensions = tuple(item.strip() for item in group_by.split(",") if item.strip())
    return _query(request).strategy_segment_report(limit=limit, group_by=dimensions)


@router.get("/reports/execution-anomalies")
async def execution_anomaly_report(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    return _query(request).execution_anomaly_report(limit=limit)


@router.get("/reports/forward-validation")
async def forward_validation_report(
    request: Request,
    window_days: int = Query(default=7, ge=1, le=90),
    period_count: int = Query(default=4, ge=1, le=12),
) -> dict[str, Any]:
    return _query(request).forward_validation_report(window_days=window_days, period_count=period_count)


@router.get("/reports/scaling-readiness")
async def scaling_readiness_report(
    request: Request,
    window_days: int = Query(default=7, ge=1, le=90),
    period_count: int = Query(default=4, ge=1, le=12),
) -> dict[str, Any]:
    return _query(request).scaling_readiness_report(window_days=window_days, period_count=period_count)


@router.get("/reports/trial-review-packet")
async def trial_review_packet(
    request: Request,
    profitability_limit: int = Query(default=100, ge=1, le=500),
    anomaly_limit: int = Query(default=100, ge=1, le=500),
    segment_limit: int = Query(default=100, ge=1, le=500),
    window_days: int = Query(default=7, ge=1, le=90),
    period_count: int = Query(default=4, ge=1, le=12),
) -> dict[str, Any]:
    return _query(request).trial_review_packet(
        profitability_limit=profitability_limit,
        anomaly_limit=anomaly_limit,
        segment_limit=segment_limit,
        window_days=window_days,
        period_count=period_count,
    )


@router.get("/reports/trial-review-summary")
async def trial_review_summary(
    request: Request,
    segment_limit: int = Query(default=100, ge=1, le=500),
    window_days: int = Query(default=7, ge=1, le=90),
    period_count: int = Query(default=4, ge=1, le=12),
) -> dict[str, Any]:
    return _query(request).trial_review_summary(
        segment_limit=segment_limit,
        window_days=window_days,
        period_count=period_count,
    )


@router.get("/reports/trial-review-details")
async def trial_review_details(
    request: Request,
    profitability_limit: int = Query(default=100, ge=1, le=500),
    anomaly_limit: int = Query(default=100, ge=1, le=500),
    segment_limit: int = Query(default=100, ge=1, le=500),
    window_days: int = Query(default=7, ge=1, le=90),
    period_count: int = Query(default=4, ge=1, le=12),
) -> dict[str, Any]:
    return _query(request).trial_review_details(
        profitability_limit=profitability_limit,
        anomaly_limit=anomaly_limit,
        segment_limit=segment_limit,
        window_days=window_days,
        period_count=period_count,
    )


@router.get("/reports/guarded-live-run-packet")
async def guarded_live_run_packet(request: Request) -> dict[str, Any]:
    return _query(request).guarded_live_run_packet()


@router.get("/reports/strategy-attribution")
async def strategy_attribution_report(
    request: Request,
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict[str, Any]:
    return _query(request).strategy_attribution_report(limit=limit)


@router.get("/reports/trial-review-history")
async def trial_review_history(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=5000),
) -> dict[str, Any]:
    return _query(request).trial_review_history(limit=limit, offset=offset)


@router.get("/reports/profile-control-summary")
async def profile_control_summary_report(request: Request) -> dict[str, Any]:
    return _query(request).profile_control_summary_report()


@router.post("/system/scaling-review")
async def system_scaling_review(
    request: Request,
    payload: ScalingReviewRequest,
    principal: OperatorPrincipal = Depends(require_admin_access),
) -> dict[str, Any]:
    try:
        return _query(request).record_capital_scale_review(
            verdict=payload.verdict,
            reason=payload.reason,
            actor_role=principal.role,
            actor_identity=principal.identity,
            auth_source=principal.auth_source,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/system/trial-review/record")
async def system_trial_review_record(
    request: Request,
    payload: TrialReviewRecordRequest | None = None,
    principal: OperatorPrincipal = Depends(require_admin_access),
) -> dict[str, Any]:
    return _query(request).record_trial_review_snapshot(
        reason=payload.reason if payload is not None else "ui_trial_review_snapshot",
        actor_role=principal.role,
        actor_identity=principal.identity,
        auth_source=principal.auth_source,
    )


@router.post("/system/trial-review/action")
async def system_trial_review_action(
    request: Request,
    payload: TrialReviewActionRequest,
    principal: OperatorPrincipal = Depends(require_admin_access),
) -> dict[str, Any]:
    try:
        return _query(request).record_trial_review_action(
            action_type=payload.action_type,
            reason=payload.reason,
            actor_role=principal.role,
            actor_identity=principal.identity,
            auth_source=principal.auth_source,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/reconciliation/latest")
async def latest_reconciliation(request: Request) -> dict[str, Any]:
    return _query(request).reconciliation_latest()


@router.get("/reconciliation/recent")
async def recent_reconciliation(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=5000),
) -> dict[str, Any]:
    return _query(request).reconciliation_recent(limit=limit, offset=offset)


@router.get("/reconciliation/mismatches")
async def reconciliation_mismatches(request: Request, limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
    return _query(request).reconciliation_mismatches(limit=limit)


@router.get("/reconciliation/{reconciliation_id}")
async def reconciliation_detail(request: Request, reconciliation_id: str) -> dict[str, Any]:
    try:
        return _query(request).reconciliation_detail(reconciliation_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/reconciliation/validate")
async def reconciliation_validate(
    request: Request,
    payload: ValidationRequest | None = None,
    principal: OperatorPrincipal = Depends(require_write_access),
) -> dict[str, Any]:
    reason = payload.reason if payload is not None else "operator_validate"
    return await _query(request).validate_reconciliation(
        reason=reason,
        actor_role=principal.role,
        actor_identity=principal.identity,
        auth_source=principal.auth_source,
    )


@router.get("/audit/latest")
async def latest_audit(request: Request) -> dict[str, Any]:
    return _query(request).audit_latest()


@router.get("/audit/{decision_id}")
async def audit_detail(request: Request, decision_id: str) -> dict[str, Any]:
    try:
        return _query(request).audit_detail(decision_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/replay/status")
async def replay_status(request: Request) -> dict[str, Any]:
    return _query(request).replay_status()


@router.get("/replay/recent-validations")
async def replay_recent_validations(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=5000),
) -> dict[str, Any]:
    return _query(request).replay_recent_validations(limit=limit, offset=offset)


@router.post("/replay/validate/{decision_id}")
async def replay_validate(
    request: Request,
    decision_id: str,
    _: OperatorPrincipal = Depends(require_write_access),
) -> dict[str, Any]:
    return _query(request).replay_validate(decision_id=decision_id)
