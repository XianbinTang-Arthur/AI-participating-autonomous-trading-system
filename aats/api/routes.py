from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from aats.bootstrap.config import ApplicationRuntime
from aats.events import topics
from aats.schemas.common import EventEnvelope
from aats.schemas.system import RuntimeModeState

router = APIRouter()


class ModeUpdateRequest(BaseModel):
    mode: str


class HaltRequest(BaseModel):
    reason: str = "manual_halt"


def _runtime(request: Request) -> ApplicationRuntime:
    return cast(ApplicationRuntime, request.app.state.runtime)


def _envelope_payload(envelope: EventEnvelope | None) -> dict[str, Any] | None:
    return envelope.payload if envelope is not None else None


def _latest_order_state(runtime: ApplicationRuntime):
    orders = runtime.execution_repo.order_states()
    if not orders:
        return None
    return max(orders, key=lambda item: item.last_update_ts or item.created_at)


def _latest_fill(runtime: ApplicationRuntime):
    fills = runtime.execution_repo.fills()
    if not fills:
        return None
    return max(fills, key=lambda item: item.ingestion_timestamp)


def _latest_decision_events(runtime: ApplicationRuntime) -> tuple[str | None, dict[str, EventEnvelope]]:
    latest_audit = runtime.event_store.latest(topics.AUDIT_RECORDS)
    latest_context = runtime.event_store.latest(topics.DECISION_CONTEXTS)
    decision_id = None
    for envelope in (latest_audit, latest_context):
        if envelope is None:
            continue
        payload_decision_id = envelope.payload.get("decision_id")
        if isinstance(payload_decision_id, str):
            decision_id = payload_decision_id
            break

    if decision_id is None:
        return None, {}

    latest_by_topic: dict[str, EventEnvelope] = {}
    for envelope in runtime.event_store.by_decision(decision_id):
        latest_by_topic[envelope.topic] = envelope

    context_envelope = latest_by_topic.get(topics.DECISION_CONTEXTS)
    if context_envelope is not None:
        health_snapshot_ref = context_envelope.payload.get("health_snapshot_ref")
        if isinstance(health_snapshot_ref, str):
            health_event = runtime.event_store.get(health_snapshot_ref)
            if health_event is not None:
                latest_by_topic[topics.HEALTH_SNAPSHOTS] = health_event

    return decision_id, latest_by_topic


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
        "execution_summary": {
            "order_count": len(runtime.execution_repo.order_states()),
            "fill_count": len(runtime.execution_repo.fills()),
            "open_order_count": len(runtime.execution_repo.open_order_states()),
            "order_intents_generated": runtime.metrics.snapshot().get("order_intents_generated", 0),
            "fills_processed": runtime.metrics.snapshot().get("fills_processed", 0),
        },
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
    decision_id, latest_by_topic = _latest_decision_events(runtime)
    return {
        "decision_id": decision_id,
        "health_snapshot": _envelope_payload(latest_by_topic.get(topics.HEALTH_SNAPSHOTS)),
        "decision_context": _envelope_payload(latest_by_topic.get(topics.DECISION_CONTEXTS)),
        "policy_decision": _envelope_payload(latest_by_topic.get(topics.POLICY_DECISIONS)),
        "risk_decision": _envelope_payload(latest_by_topic.get(topics.RISK_DECISIONS)),
        "execution_plan": _envelope_payload(latest_by_topic.get(topics.EXECUTION_PLANS)),
        "audit": _envelope_payload(latest_by_topic.get(topics.AUDIT_RECORDS)),
        "latest_order_intent": _envelope_payload(latest_by_topic.get(topics.ORDER_INTENTS)),
        "latest_order_update": _envelope_payload(latest_by_topic.get(topics.ORDER_UPDATES)),
        "latest_fill_event": _envelope_payload(latest_by_topic.get(topics.FILL_EVENTS)),
        "execution_summary": {
            "order_count": len(runtime.execution_repo.order_states()),
            "fill_count": len(runtime.execution_repo.fills()),
            "open_order_count": len(runtime.execution_repo.open_order_states()),
        },
    }


@router.get("/audit/latest")
async def latest_audit_record(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    latest_audit = runtime.event_store.latest(topics.AUDIT_RECORDS)
    return {"audit": _envelope_payload(latest_audit)}


@router.get("/audit/{decision_id}")
async def audit_record(request: Request, decision_id: str) -> dict[str, Any]:
    runtime = _runtime(request)
    record = runtime.audit_repo.get(decision_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"audit record not found for decision_id={decision_id}")
    return {"audit": record.model_dump(mode="json")}


@router.get("/orders/latest")
async def latest_order(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    latest_order = _latest_order_state(runtime)
    return {"order": latest_order.model_dump(mode="json") if latest_order is not None else None}


@router.get("/fills/latest")
async def latest_fill(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    latest_local_fill = _latest_fill(runtime)
    exchange_snapshot = runtime.account_service.latest_snapshot()
    latest_exchange_fill = exchange_snapshot.fills[0] if exchange_snapshot and exchange_snapshot.fills else None
    return {
        "local_fill": latest_local_fill.model_dump(mode="json") if latest_local_fill is not None else None,
        "exchange_fill": latest_exchange_fill.model_dump(mode="json") if latest_exchange_fill is not None else None,
    }


@router.get("/execution/latest")
async def latest_execution(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    readiness = runtime.execution_adapter.readiness()
    latest_order = _latest_order_state(runtime)
    latest_local_fill = _latest_fill(runtime)
    latest_reconciliation = runtime.reconciliation_repo.latest()
    return {
        "mode": RuntimeModeState(**runtime.mode_controller.snapshot()).model_dump(mode="json"),
        "execution": readiness,
        "latest_order": latest_order.model_dump(mode="json") if latest_order is not None else None,
        "latest_fill": latest_local_fill.model_dump(mode="json") if latest_local_fill is not None else None,
        "latest_reconciliation": (
            latest_reconciliation.model_dump(mode="json") if latest_reconciliation is not None else None
        ),
    }
