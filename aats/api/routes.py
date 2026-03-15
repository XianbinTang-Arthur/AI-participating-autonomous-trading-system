from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from aats.api.auth import OperatorPrincipal, require_read_access, require_write_access
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


class ValidationRequest(BaseModel):
    reason: str = "operator_validate"


def _runtime(request: Request) -> ApplicationRuntime:
    return cast(ApplicationRuntime, request.app.state.runtime)


def _query(request: Request) -> OperatorQueryService:
    return OperatorQueryService(_runtime(request))


@router.get("/system/health")
async def system_health(request: Request) -> dict[str, Any]:
    query = _query(request)
    runtime = _runtime(request)
    health = query.system_health()
    health["execution_summary"] = {
        "order_count": len(runtime.execution_repo.order_states()),
        "fill_count": len(runtime.execution_repo.fills()),
        "open_order_count": len(runtime.execution_repo.open_order_states()),
        "order_intents_generated": runtime.metrics.snapshot().get("order_intents_generated", 0),
        "fills_processed": runtime.metrics.snapshot().get("fills_processed", 0),
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
        "recent_history": _query(request).blocker_history(limit=20)["history"],
    }


@router.get("/system/metrics")
async def system_metrics(request: Request) -> dict[str, Any]:
    return _query(request).metrics()


@router.post("/system/mode")
async def set_system_mode(
    request: Request,
    payload: ModeUpdateRequest,
    _: OperatorPrincipal = Depends(require_write_access),
) -> dict[str, Any]:
    runtime = _runtime(request)
    try:
        runtime.mode_controller.set_mode(payload.mode)  # type: ignore[arg-type]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RuntimeModeState(**_query(request).system_mode()).model_dump(mode="json")


@router.post("/system/halt")
@router.post("/halt")
async def halt(
    request: Request,
    payload: HaltRequest | None = None,
    reason: str | None = None,
    principal: OperatorPrincipal = Depends(require_write_access),
) -> dict[str, Any]:
    halt_reason = reason or (payload.reason if payload is not None else "manual_halt")
    result = _query(request).halt(reason=halt_reason, actor_role=principal.role)
    result["mode"] = RuntimeModeState(**_query(request).system_mode()).model_dump(mode="json")
    result["blockers"] = _query(request).blockers()
    return result


@router.post("/system/resume")
@router.post("/resume")
async def resume(
    request: Request,
    payload: ResumeRequest | None = None,
    _: OperatorPrincipal = Depends(require_write_access),
) -> dict[str, Any]:
    resume_reason = payload.reason if payload is not None else "manual_resume"
    result = _query(request).resume(reason=resume_reason, actor_role=_.role)
    result["mode"] = RuntimeModeState(**_query(request).system_mode()).model_dump(mode="json")
    return result


@router.get("/system/recovery")
async def system_recovery(request: Request) -> dict[str, Any]:
    return {"recovery": _runtime(request).recovery_status.model_dump(mode="json")}


@router.get("/decision/latest")
async def latest_decision(request: Request) -> dict[str, Any]:
    return _query(request).latest_decision()


@router.get("/decision/recent")
async def recent_decisions(request: Request, limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
    return {"decisions": _query(request).recent_decisions(limit=limit)}


@router.get("/decision/{decision_id}")
async def decision_detail(request: Request, decision_id: str) -> dict[str, Any]:
    try:
        return _query(request).decision_view(decision_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/risk/latest")
async def latest_risk(request: Request) -> dict[str, Any]:
    payload = _query(request).latest_risk()
    return {"decision_id": payload["decision_id"], "risk_decision": payload["payload"]}


@router.get("/risk/recent")
async def recent_risk(request: Request, limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
    return _query(request).recent_risks(limit=limit)


@router.get("/policy/latest")
async def latest_policy(request: Request) -> dict[str, Any]:
    payload = _query(request).latest_policy()
    return {"decision_id": payload["decision_id"], "policy_decision": payload["payload"]}


@router.get("/policy/recent")
async def recent_policy(request: Request, limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
    return _query(request).recent_policies(limit=limit)


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


@router.get("/account/open-orders")
async def account_open_orders(request: Request) -> dict[str, Any]:
    return _query(request).account_open_orders()


@router.get("/account/recent-fills")
async def account_recent_fills(request: Request) -> dict[str, Any]:
    return _query(request).account_recent_fills()


@router.get("/orders/open")
async def open_orders(request: Request) -> dict[str, Any]:
    return _query(request).orders_open()


@router.get("/orders/latest")
async def latest_order(request: Request) -> dict[str, Any]:
    order = _query(request).latest_order()
    return {"order": order.model_dump(mode="json") if order is not None else None}


@router.get("/orders/recent")
async def recent_orders(request: Request, limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    return _query(request).orders_recent(limit=limit)


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
    runtime = _runtime(request)
    _ = payload
    _ = principal
    try:
        state = await runtime.order_manager.cancel_order(client_order_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"order": state.model_dump(mode="json")}


@router.get("/fills/latest")
async def latest_fill(request: Request) -> dict[str, Any]:
    query = _query(request)
    local_fill = query.latest_fill()
    exchange = _runtime(request).account_service.latest_snapshot()
    exchange_fill = exchange.fills[0] if exchange is not None and exchange.fills else None
    return {
        "local_fill": local_fill.model_dump(mode="json") if local_fill is not None else None,
        "exchange_fill": exchange_fill.model_dump(mode="json") if exchange_fill is not None else None,
    }


@router.get("/fills/recent")
async def recent_fills(request: Request, limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    return _query(request).fills_recent(limit=limit)


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


@router.get("/reconciliation/latest")
async def latest_reconciliation(request: Request) -> dict[str, Any]:
    return _query(request).reconciliation_latest()


@router.get("/reconciliation/recent")
async def recent_reconciliation(request: Request, limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
    return _query(request).reconciliation_recent(limit=limit)


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
    return await _query(request).validate_reconciliation(reason=reason, actor_role=principal.role)


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
async def replay_recent_validations(request: Request) -> dict[str, Any]:
    return _query(request).replay_recent_validations()


@router.post("/replay/validate/{decision_id}")
async def replay_validate(
    request: Request,
    decision_id: str,
    _: OperatorPrincipal = Depends(require_write_access),
) -> dict[str, Any]:
    return _query(request).replay_validate(decision_id=decision_id)
