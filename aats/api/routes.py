from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Request

from aats.bootstrap.config import ApplicationRuntime

router = APIRouter()


def _runtime(request: Request) -> ApplicationRuntime:
    return cast(ApplicationRuntime, request.app.state.runtime)


@router.get("/system/health")
async def system_health(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    latest_reconciliation = runtime.reconciliation_repo.latest()
    return {
        "status": "ok",
        "mode": runtime.settings.mode,
        "storage_mode": runtime.settings.storage_mode,
        "halted": runtime.kill_switch.halted,
        "event_count": len(runtime.event_store.all()),
        "audit_record_count": len(runtime.audit_repo.all()),
        "metrics": runtime.metrics.snapshot(),
        "latest_reconciliation": latest_reconciliation.severity if latest_reconciliation else None,
    }


@router.get("/portfolio")
async def portfolio(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    snapshot = runtime.portfolio_repo.latest()
    return {
        "portfolio": snapshot.model_dump(mode="json") if snapshot is not None else None,
    }


@router.post("/halt")
async def halt(request: Request, reason: str = "manual_halt") -> dict[str, Any]:
    runtime = _runtime(request)
    runtime.kill_switch.halt(reason=reason)
    return {"status": "halt_triggered", **runtime.kill_switch.status()}


@router.post("/resume")
async def resume(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    runtime.kill_switch.resume()
    return {"status": "resume_triggered", **runtime.kill_switch.status()}
