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


class CancelOrderRequest(BaseModel):
    reason: str = "operator_cancel"


def _runtime(request: Request) -> ApplicationRuntime:
    return cast(ApplicationRuntime, request.app.state.runtime)


def _envelope_payload(envelope: EventEnvelope | None) -> dict[str, Any] | None:
    if envelope is None:
        return None
    payload = dict(envelope.payload)
    payload["_event_id"] = envelope.event_id
    payload["_topic"] = envelope.topic
    return payload


def _resolve_linked_event_payloads(runtime: ApplicationRuntime, refs: list[str]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for ref in refs:
        envelope = runtime.event_store.get(ref)
        if envelope is None:
            continue
        payload = dict(envelope.payload)
        payload["_event_id"] = envelope.event_id
        payload["_topic"] = envelope.topic
        payloads.append(payload)
    return payloads


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


def _decision_view(runtime: ApplicationRuntime, decision_id: str) -> dict[str, Any]:
    latest_by_topic: dict[str, EventEnvelope] = {}
    for envelope in runtime.event_store.by_decision(decision_id):
        latest_by_topic[envelope.topic] = envelope

    audit_record = runtime.audit_repo.get(decision_id)
    if audit_record is not None:
        context_envelope = runtime.event_store.get(audit_record.decision_context_ref)
        if context_envelope is not None:
            latest_by_topic[topics.DECISION_CONTEXTS] = context_envelope
            health_snapshot_ref = context_envelope.payload.get("health_snapshot_ref")
            if isinstance(health_snapshot_ref, str):
                health_event = runtime.event_store.get(health_snapshot_ref)
                if health_event is not None:
                    latest_by_topic[topics.HEALTH_SNAPSHOTS] = health_event

    return {
        "decision_id": decision_id,
        "health_snapshot": _envelope_payload(latest_by_topic.get(topics.HEALTH_SNAPSHOTS)),
        "decision_context": _envelope_payload(latest_by_topic.get(topics.DECISION_CONTEXTS)),
        "baseline_assessment": _envelope_payload(latest_by_topic.get(topics.BASELINE_ASSESSMENTS)),
        "ai_assessment": _envelope_payload(latest_by_topic.get(topics.AI_ASSESSMENTS)),
        "position_target": _envelope_payload(latest_by_topic.get(topics.POSITION_TARGETS)),
        "policy_decision": _envelope_payload(latest_by_topic.get(topics.POLICY_DECISIONS)),
        "risk_decision": _envelope_payload(latest_by_topic.get(topics.RISK_DECISIONS)),
        "execution_plan": _envelope_payload(latest_by_topic.get(topics.EXECUTION_PLANS)),
        "audit": audit_record.model_dump(mode="json") if audit_record is not None else None,
        "latest_order_intent": _envelope_payload(latest_by_topic.get(topics.ORDER_INTENTS)),
        "latest_order_update": _envelope_payload(latest_by_topic.get(topics.ORDER_UPDATES)),
        "latest_fill_event": _envelope_payload(latest_by_topic.get(topics.FILL_EVENTS)),
        "latest_reconciliation": _envelope_payload(latest_by_topic.get(topics.RECONCILIATION_REPORTS)),
        "execution_summary": {
            "order_intents": _resolve_linked_event_payloads(runtime, audit_record.order_intent_refs)
            if audit_record is not None
            else [],
            "order_updates": _resolve_linked_event_payloads(runtime, audit_record.order_state_refs)
            if audit_record is not None
            else [],
            "fills": _resolve_linked_event_payloads(runtime, audit_record.fill_event_refs)
            if audit_record is not None
            else [],
        },
    }


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
        "recovery": runtime.recovery_status.model_dump(mode="json"),
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
    return {
        "reconciliation": report.model_dump(mode="json") if report is not None else None,
        "mismatch_summary": None
        if report is None
        else {
            "severity": report.severity,
            "halt_required": report.halt_required,
            "order_diff": report.order_diff,
            "fill_diff": report.fill_diff,
            "balance_diff": report.balance_diff,
            "position_diff": report.position_diff,
        },
    }


@router.get("/decision/latest")
async def latest_decision(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    decision_id, latest_by_topic = _latest_decision_events(runtime)
    if decision_id is None:
        return {
            "decision_id": None,
            "health_snapshot": None,
            "decision_context": None,
            "baseline_assessment": None,
            "ai_assessment": None,
            "position_target": None,
            "policy_decision": None,
            "risk_decision": None,
            "execution_plan": None,
            "audit": None,
            "latest_order_intent": None,
            "latest_order_update": None,
            "latest_fill_event": None,
            "latest_reconciliation": None,
            "execution_summary": {"order_intents": [], "order_updates": [], "fills": []},
        }
    _ = latest_by_topic
    return _decision_view(runtime, decision_id)


@router.get("/risk/latest")
async def latest_risk_decision(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    latest_risk = runtime.event_store.latest(topics.RISK_DECISIONS)
    if latest_risk is None:
        return {"decision_id": None, "risk_decision": None, "audit": None}
    decision_id = latest_risk.payload.get("decision_id")
    if not isinstance(decision_id, str):
        return {"decision_id": None, "risk_decision": latest_risk.payload, "audit": None}
    decision_view = _decision_view(runtime, decision_id)
    return {
        "decision_id": decision_id,
        "risk_decision": decision_view["risk_decision"],
        "policy_decision": decision_view["policy_decision"],
        "execution_plan": decision_view["execution_plan"],
        "audit": decision_view["audit"],
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
    history = runtime.audit_repo.history(decision_id)
    return {
        "audit": record.model_dump(mode="json"),
        "history_length": len(history),
        "linked_events": {
            "decision_context": _envelope_payload(runtime.event_store.get(record.decision_context_ref)),
            "baseline_assessment": _envelope_payload(
                runtime.event_store.get(record.baseline_assessment_ref) if record.baseline_assessment_ref else None
            ),
            "ai_assessment": _envelope_payload(
                runtime.event_store.get(record.ai_market_assessment_ref)
                if record.ai_market_assessment_ref
                else None
            ),
            "position_target": _envelope_payload(
                runtime.event_store.get(record.position_target_ref) if record.position_target_ref else None
            ),
            "policy_decision": _envelope_payload(
                runtime.event_store.get(record.policy_decision_ref) if record.policy_decision_ref else None
            ),
            "risk_decision": _envelope_payload(
                runtime.event_store.get(record.risk_decision_ref) if record.risk_decision_ref else None
            ),
            "execution_plan": _envelope_payload(
                runtime.event_store.get(record.execution_plan_ref) if record.execution_plan_ref else None
            ),
            "order_intents": _resolve_linked_event_payloads(runtime, record.order_intent_refs),
            "order_updates": _resolve_linked_event_payloads(runtime, record.order_state_refs),
            "fills": _resolve_linked_event_payloads(runtime, record.fill_event_refs),
            "portfolio_snapshot": _envelope_payload(
                runtime.event_store.get(record.portfolio_delta_ref) if record.portfolio_delta_ref else None
            ),
            "reconciliations": _resolve_linked_event_payloads(runtime, record.reconciliation_refs),
        },
    }


@router.get("/orders/latest")
async def latest_order(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    latest_order = _latest_order_state(runtime)
    return {"order": latest_order.model_dump(mode="json") if latest_order is not None else None}


@router.get("/orders/partial")
async def partial_orders(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    return {
        "orders": [
            order.model_dump(mode="json")
            for order in runtime.execution_repo.recent_order_states(
                limit=50,
                statuses=("PARTIALLY_FILLED", "CANCEL_PENDING"),
            )
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


@router.post("/orders/{client_order_id}/cancel")
async def cancel_order(
    request: Request,
    client_order_id: str,
    payload: CancelOrderRequest | None = None,
) -> dict[str, Any]:
    runtime = _runtime(request)
    _ = payload
    try:
        state = await runtime.order_manager.cancel_order(client_order_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"order": state.model_dump(mode="json")}


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


@router.get("/fills/recent")
async def recent_fills(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    fills = sorted(
        runtime.execution_repo.fills(),
        key=lambda item: (item.ingestion_timestamp, item.fill_id),
        reverse=True,
    )[:50]
    return {"fills": [fill.model_dump(mode="json") for fill in fills]}


@router.get("/execution/latest")
async def latest_execution(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    readiness = runtime.execution_adapter.readiness()
    latest_order_state = _latest_order_state(runtime)
    latest_fill_state = _latest_fill(runtime)
    decision_id = None
    if latest_fill_state is not None:
        decision_id = latest_fill_state.decision_id
    elif latest_order_state is not None:
        decision_id = latest_order_state.decision_id
    latest_order = latest_order_state.model_dump(mode="json") if latest_order_state is not None else None
    latest_local_fill = latest_fill_state.model_dump(mode="json") if latest_fill_state is not None else None
    latest_reconciliation = runtime.reconciliation_repo.latest()
    return {
        "decision_id": decision_id,
        "mode": RuntimeModeState(**runtime.mode_controller.snapshot()).model_dump(mode="json"),
        "execution": readiness,
        "latest_order": latest_order,
        "latest_fill": latest_local_fill,
        "latest_reconciliation": (
            latest_reconciliation.model_dump(mode="json") if latest_reconciliation is not None else None
        ),
        "recent_failures": [
            order.model_dump(mode="json")
            for order in runtime.execution_repo.recent_order_states(
                limit=20,
                statuses=("FAILED", "REJECTED", "BLOCKED"),
            )
        ],
        "recovery": runtime.recovery_status.model_dump(mode="json"),
    }


@router.get("/execution/result/latest")
async def latest_execution_result(request: Request) -> dict[str, Any]:
    return await latest_execution(request)


@router.get("/system/recovery")
async def system_recovery(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    return {"recovery": runtime.recovery_status.model_dump(mode="json")}
