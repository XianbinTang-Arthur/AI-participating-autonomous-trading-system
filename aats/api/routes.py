from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from aats.bootstrap.config import ApplicationRuntime
from aats.events import topics
from aats.schemas.system import RuntimeModeState

router = APIRouter()


class ModeUpdateRequest(BaseModel):
    mode: str


class HaltRequest(BaseModel):
    reason: str = "manual_halt"


def _runtime(request: Request) -> ApplicationRuntime:
    return cast(ApplicationRuntime, request.app.state.runtime)


@router.get("/system/health")
async def system_health(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    latest_reconciliation = runtime.reconciliation_repo.latest()
    snapshot = runtime.health_service.snapshot()
    return {
        "status": snapshot.status,
        "mode": snapshot.mode,
        "operating_state": snapshot.operating_state,
        "storage_mode": runtime.settings.storage_mode,
        "event_persistence_mode": runtime.settings.event_persistence_mode,
        "halted": runtime.kill_switch.halted,
        "event_count": runtime.event_store.count(),
        "audit_record_count": runtime.audit_repo.count(),
        "metrics": runtime.metrics.snapshot(),
        "components": [component.model_dump(mode="json") for component in snapshot.components],
        "blockers": snapshot.blockers,
        "market": runtime.market_gateway.status(),
        "account": runtime.account_service.status(),
        "execution": runtime.execution_adapter.readiness(),
        "latest_reconciliation": latest_reconciliation.model_dump(mode="json") if latest_reconciliation else None,
    }


@router.get("/system/mode")
async def system_mode(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    return RuntimeModeState(**runtime.mode_controller.snapshot()).model_dump(mode="json")


@router.post("/system/mode")
async def set_system_mode(request: Request, payload: ModeUpdateRequest) -> dict[str, Any]:
    runtime = _runtime(request)
    try:
        runtime.mode_controller.set_mode(payload.mode)  # type: ignore[arg-type]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RuntimeModeState(**runtime.mode_controller.snapshot()).model_dump(mode="json")


@router.post("/system/halt")
@router.post("/halt")
async def halt(request: Request, payload: HaltRequest | None = None, reason: str | None = None) -> dict[str, Any]:
    runtime = _runtime(request)
    halt_reason = reason or (payload.reason if payload is not None else "manual_halt")
    runtime.kill_switch.halt(reason=halt_reason)
    return {"status": "halt_triggered", **runtime.kill_switch.status()}


@router.post("/system/resume")
@router.post("/resume")
async def resume(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    runtime.kill_switch.resume()
    return {"status": "resume_triggered", **runtime.kill_switch.status()}


@router.get("/portfolio")
async def portfolio(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    snapshot = runtime.portfolio_repo.latest()
    return {
        "portfolio": snapshot.model_dump(mode="json") if snapshot is not None else None,
        "exchange_balances": (
            [item.model_dump(mode="json") for item in runtime.account_service.latest_snapshot().balances]
            if runtime.account_service.latest_snapshot() is not None
            else []
        ),
    }


@router.get("/positions")
async def positions(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    local = runtime.portfolio_repo.latest()
    exchange_snapshot = runtime.account_service.latest_snapshot()
    return {
        "local_positions": (
            [position.model_dump(mode="json") for position in local.positions]
            if local is not None
            else []
        ),
        "exchange_positions": (
            [position.model_dump(mode="json") for position in exchange_snapshot.positions]
            if exchange_snapshot is not None
            else []
        ),
    }


@router.get("/orders/open")
async def open_orders(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    exchange_snapshot = runtime.account_service.latest_snapshot()
    return {
        "local_open_orders": [
            order.model_dump(mode="json") for order in runtime.execution_repo.open_order_states()
        ],
        "exchange_open_orders": (
            [order.model_dump(mode="json") for order in exchange_snapshot.open_orders]
            if exchange_snapshot is not None
            else []
        ),
    }


@router.get("/reconciliation/latest")
async def latest_reconciliation(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    report = runtime.reconciliation_repo.latest()
    return {"reconciliation": report.model_dump(mode="json") if report is not None else None}


@router.get("/decision/latest")
async def latest_decision(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    latest_context = runtime.event_store.latest(topics.DECISION_CONTEXTS)
    latest_audit = runtime.event_store.latest(topics.AUDIT_RECORDS)
    return {
        "decision_context": latest_context.model_dump(mode="json") if latest_context is not None else None,
        "audit": latest_audit.model_dump(mode="json") if latest_audit is not None else None,
    }


@router.get("/audit/{decision_id}")
async def audit_record(request: Request, decision_id: str) -> dict[str, Any]:
    runtime = _runtime(request)
    record = runtime.audit_repo.get(decision_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"audit record not found for decision_id={decision_id}")
    return {"audit": record.model_dump(mode="json")}
