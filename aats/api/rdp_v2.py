"""RDP v2 logical-run API.

The legacy ``/rdp/tasks`` API remains compatible.  New clients should use this
resource-oriented API so retries, steps, cancellation, and idempotency have
explicit semantics.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import OperationalError

from aats.api._governance_db import governance_session
from aats.api.auth import OperatorPrincipal, require_read_access, require_write_access
from aats.data_platform.governance.rdp_runs_db import RUN_STATUSES
from aats.data_platform.governance.rdp_task_db import VALID_WORKFLOWS

log = logging.getLogger(__name__)

rdp_v2_router = APIRouter(prefix="/v2", tags=["RDP-v2"])


class CreateRunRequest(BaseModel):
    workflow: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=160)


def _actor(principal: OperatorPrincipal) -> str:
    identity = str(principal.identity or "").strip()
    if identity:
        return identity
    return "local-operator" if not principal.auth_enabled else "unknown-authenticated-operator"


def _project_root(request: Request) -> Path:
    try:
        from aats.data_platform.config import get_settings as get_rdp_settings

        root = Path(get_rdp_settings().project_root).resolve()
        if root.exists():
            return root
    except Exception:
        log.exception("RDP v2 无法从 settings 解析 project_root")
    return Path(".").resolve()


def _detail(session: Any, run_id: str) -> dict[str, Any]:
    from aats.data_platform.governance.rdp_runs_db import (
        db_get_run,
        db_get_run_attempts,
        db_get_run_events,
        db_get_run_steps,
    )

    run = db_get_run(session, run_id)
    if run is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "rdp_run_not_found", "run_id": run_id},
        )
    return {
        "run": run,
        "attempts": db_get_run_attempts(session, run_id),
        "steps": db_get_run_steps(session, run_id),
        "events": db_get_run_events(session, run_id),
    }


def _db_unavailable(exc: OperationalError, *, operation: str) -> HTTPException:
    """Map governance connectivity failures to the same fail-closed API shape."""
    log.exception("RDP v2 %s 时 governance DB 不可达", operation)
    return HTTPException(
        status_code=503,
        detail={"code": "governance_db_unavailable", "retryable": True},
    )


def build_rdp_runs_panel(*, limit: int = 20) -> dict[str, Any]:
    """Build the cheap, snapshot-friendly run list used by the RDP workspace."""
    from aats.data_platform.governance.rdp_runs_db import db_list_runs

    with governance_session() as session:
        return {"items": db_list_runs(session, limit=limit, offset=0), "limit": limit}


@rdp_v2_router.get("/runs", dependencies=[Depends(require_read_access)])
async def list_runs(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    status: str | None = Query(default=None),
    workflow: str | None = Query(default=None),
) -> dict[str, Any]:
    from aats.data_platform.governance.rdp_runs_db import db_list_runs

    if status is not None and status not in RUN_STATUSES:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_run_status", "status": status},
        )
    if workflow is not None and workflow not in VALID_WORKFLOWS:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_workflow", "workflow": workflow},
        )
    try:
        with governance_session() as session:
            runs = db_list_runs(
                session,
                limit=limit,
                offset=offset,
                status=status,
                workflow=workflow,
            )
    except OperationalError as exc:
        raise _db_unavailable(exc, operation="查询 run 列表") from exc
    return {"items": runs, "limit": limit, "offset": offset}


@rdp_v2_router.post("/runs", status_code=202)
async def create_run(
    request: Request,
    response: Response,
    body: CreateRunRequest,
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
    ),
    principal: OperatorPrincipal = Depends(require_write_access),
) -> dict[str, Any]:
    from aats.data_platform.governance.rdp_task_db import (
        WorkflowEnqueueBlockedError,
        db_create_task_if_idle,
    )
    from aats.data_platform.operations.workflow_dispatcher import (
        describe_manual_trigger_availability,
    )

    effective_idempotency_key = str(idempotency_key or body.idempotency_key or "").strip()
    if not 8 <= len(effective_idempotency_key) <= 160:
        raise HTTPException(
            status_code=422,
            detail={"code": "idempotency_key_required"},
        )

    if body.workflow not in VALID_WORKFLOWS:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_workflow", "workflow": body.workflow},
        )
    availability = describe_manual_trigger_availability(
        _project_root(request),
        body.workflow,
    )
    if not availability.get("enabled"):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "workflow_not_manually_available",
                "workflow": body.workflow,
                "message": availability.get("disabled_reason"),
            },
        )
    try:
        with governance_session() as session:
            task_id, existing = db_create_task_if_idle(
                session,
                workflow=body.workflow,
                requested_by=_actor(principal),
                trigger_kind="manual",
                idempotency_key=effective_idempotency_key,
                payload=body.payload,
            )
            if task_id is None and not (existing or {}).get("idempotent_replay"):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "workflow_busy",
                        "workflow": body.workflow,
                        "active_task": existing,
                        "message": "同一流程已有运行占用执行槽；本次未重复创建。",
                    },
                )
            run_id = str((existing or {}).get("run_id")) if task_id is None else None
            if run_id is None:
                from aats.data_platform.governance.rdp_task_db import (
                    db_get_latest_task_for_workflow,
                )

                created_attempt = db_get_latest_task_for_workflow(session, body.workflow)
                if created_attempt is None or created_attempt.get("task_id") != task_id:
                    raise RuntimeError("created_rdp_task_could_not_be_reloaded")
                run_id = str(created_attempt["run_id"])
            payload = _detail(session, run_id)
            is_replay = task_id is None
            if is_replay:
                replay_run = payload["run"]
                if (
                    replay_run.get("workflow") != body.workflow
                    or replay_run.get("payload", {}) != body.payload
                ):
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "idempotency_key_payload_mismatch",
                            "message": "该幂等键已经绑定到另一组 RDP 请求参数。",
                        },
                    )
            payload["idempotent_replay"] = is_replay
            if is_replay:
                response.status_code = 200
            return payload
    except WorkflowEnqueueBlockedError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "workflow_frozen", "workflow": body.workflow},
        ) from exc
    except HTTPException:
        raise
    except OperationalError as exc:
        raise _db_unavailable(exc, operation="创建 run") from exc
    except Exception as exc:
        log.exception("RDP v2 创建 run 失败 workflow=%s", body.workflow)
        raise HTTPException(
            status_code=500,
            detail={"code": "rdp_run_create_failed"},
        ) from exc


@rdp_v2_router.get("/runs/{run_id}", dependencies=[Depends(require_read_access)])
async def get_run(run_id: str) -> dict[str, Any]:
    try:
        with governance_session() as session:
            return _detail(session, run_id)
    except HTTPException:
        raise
    except OperationalError as exc:
        raise _db_unavailable(exc, operation="查询 run 详情") from exc


@rdp_v2_router.post("/runs/{run_id}/cancel")
async def cancel_run(
    run_id: str,
    principal: OperatorPrincipal = Depends(require_write_access),
) -> dict[str, Any]:
    from aats.data_platform.governance.rdp_runs_db import db_request_run_cancel

    try:
        with governance_session() as session:
            run = db_request_run_cancel(
                session,
                run_id=run_id,
                requested_by=_actor(principal),
            )
            if run is None:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "rdp_run_not_found", "run_id": run_id},
                )
            return _detail(session, run_id)
    except HTTPException:
        raise
    except OperationalError as exc:
        raise _db_unavailable(exc, operation="请求取消 run") from exc


@rdp_v2_router.post("/runs/{run_id}/retry", status_code=202)
async def retry_run(
    run_id: str,
    principal: OperatorPrincipal = Depends(require_write_access),
) -> dict[str, Any]:
    from aats.data_platform.governance.rdp_runs_db import db_get_run, db_get_run_attempts
    from aats.data_platform.governance.rdp_task_db import (
        WorkflowEnqueueBlockedError,
        db_create_task_if_idle,
    )

    workflow_name = "unknown"
    try:
        with governance_session() as session:
            run = db_get_run(session, run_id)
            if run is None:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "rdp_run_not_found", "run_id": run_id},
                )
            if run["status"] not in {"failed", "partially_succeeded"}:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "run_not_retryable",
                        "run_id": run_id,
                        "status": run["status"],
                        "message": "只有失败或部分成功的运行可以重试。",
                    },
                )
            attempts = db_get_run_attempts(session, run_id)
            if not attempts:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "run_has_no_attempt", "run_id": run_id},
                )
            parent = attempts[-1]
            workflow_name = str(run["workflow"])
            task_id, existing = db_create_task_if_idle(
                session,
                workflow=workflow_name,
                requested_by=_actor(principal),
                run_id=run_id,
                attempt_no=int(parent["attempt_no"]) + 1,
                parent_task_id=str(parent["task_id"]),
                trigger_kind="manual",
            )
            if task_id is None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "workflow_busy",
                        "active_task": existing,
                        "message": "同一流程已有运行占用执行槽；请等待或先取消该运行。",
                    },
                )
            return _detail(session, run_id)
    except HTTPException:
        raise
    except WorkflowEnqueueBlockedError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "workflow_frozen",
                "workflow": workflow_name,
            },
        ) from exc
    except OperationalError as exc:
        raise _db_unavailable(exc, operation="重试 run") from exc
