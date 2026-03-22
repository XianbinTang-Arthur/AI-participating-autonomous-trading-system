from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi import Query
from pydantic import BaseModel

from aats.api.auth import (
    OperatorPrincipal,
    authenticate_operator_user,
    configured_local_principal,
    configured_operator_roles,
    require_admin_access,
    require_read_access,
    session_principal,
    stored_operator_user_count,
)
from aats.api.session_auth import issue_session_token
from aats.bootstrap.config import ApplicationRuntime
from aats.schemas.common import utc_now
from aats.services.operator.query_service import OperatorQueryService


auth_router = APIRouter(include_in_schema=False)


class LoginRequest(BaseModel):
    username: str
    password: str


class CreateOperatorUserRequest(BaseModel):
    username: str
    password: str
    role: str
    enabled: bool = True


class UpdateOperatorUserRequest(BaseModel):
    role: str | None = None
    password: str | None = None
    enabled: bool | None = None


class CreateRuntimeProfileDraftRequest(BaseModel):
    profile_label: str


class UpdateRuntimeProfileRequest(BaseModel):
    profile_label: str | None = None
    activation_note: str | None = None
    payload: dict[str, Any]


class StageRuntimeProfileRequest(BaseModel):
    activation_note: str | None = None


class StrategyProfileManualActivateRequest(BaseModel):
    reason: str = "manual_activate_strategy_profile"


class StrategyProfileManualRestoreRequest(BaseModel):
    reason: str = "manual_restore_auto_strategy_profile_control"


class AIManualOperatingModeOverrideRequest(BaseModel):
    mode: str
    reason: str = "manual_override_ai_operating_mode"


class AIManualOperatingModeRestoreRequest(BaseModel):
    reason: str = "manual_restore_auto_ai_operating_mode"


def _runtime(request: Request) -> ApplicationRuntime:
    return request.app.state.runtime


def _query(request: Request) -> OperatorQueryService:
    return OperatorQueryService(_runtime(request))


def _session_payload(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    settings = runtime.settings
    principal = session_principal(request)
    if principal is None and not settings.operator_auth_enabled:
        principal = configured_local_principal(runtime)
    return {
        "auth_enabled": settings.operator_auth_enabled,
        "session_enabled": settings.operator_session_configured,
        "api_key_compatibility_enabled": bool(settings.operator_read_api_key or settings.operator_write_api_key),
        "database_backed": runtime.database_runtime is not None,
        "stored_user_count": stored_operator_user_count(runtime),
        "authenticated": principal is not None and principal.auth_enabled,
        "identity": principal.identity if principal is not None else None,
        "role": principal.role if principal is not None else "anonymous",
        "auth_source": principal.auth_source if principal is not None else "anonymous",
    }


def _runtime_profile_control_disabled(
    request: Request,
    *,
    principal: OperatorPrincipal,
    action: str,
    details: dict[str, Any] | None = None,
) -> None:
    _query(request).record_runtime_profile_action(
        action=action,
        actor_role=principal.role,
        actor_identity=principal.identity,
        auth_source=principal.auth_source,
        status="control_disabled",
        details={
            "control_plane_status": "deprecated_readonly",
            **(details or {}),
        },
    )
    raise _runtime_profile_http_error("runtime_profile_control_disabled")


@auth_router.get("/auth/session")
async def auth_session(request: Request) -> dict[str, Any]:
    return _session_payload(request)


@auth_router.post("/auth/login")
async def auth_login(request: Request, payload: LoginRequest, response: Response) -> dict[str, Any]:
    runtime = _runtime(request)
    settings = runtime.settings
    if not settings.operator_auth_enabled:
        raise HTTPException(status_code=400, detail="operator_auth_disabled")
    if not settings.operator_session_configured:
        raise HTTPException(status_code=503, detail="operator_session_auth_not_configured")
    principal = authenticate_operator_user(runtime, username=payload.username, password=payload.password)
    if principal is None:
        raise HTTPException(status_code=401, detail="operator_login_failed")
    user = runtime.operator_repo.get_by_username(payload.username)
    if user is None:
        raise HTTPException(status_code=401, detail="operator_login_failed")
    _query(request).record_operator_login(
        actor_identity=principal.identity or payload.username,
        actor_role=principal.role,
        auth_source="session",
    )
    token = issue_session_token(
        settings=settings,
        identity=principal.identity or payload.username,
        role=principal.role,
        session_version=user.session_version,
    )
    response.set_cookie(
        key=settings.operator_session_cookie_name,
        value=token,
        max_age=settings.operator_session_max_age_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.operator_session_cookie_secure,
        path="/",
    )
    return {
        "authenticated": True,
        "identity": principal.identity,
        "role": principal.role,
        "auth_source": "session",
    }


@auth_router.post("/auth/logout")
async def auth_logout(request: Request, response: Response) -> dict[str, Any]:
    runtime = _runtime(request)
    settings = _runtime(request).settings
    principal = session_principal(request)
    if principal is not None and hasattr(runtime, "operator_repo"):
        bump = getattr(runtime.operator_repo, "bump_session_version", None)
        if callable(bump):
            bump(principal.identity or "", utc_now())
    response.delete_cookie(settings.operator_session_cookie_name, path="/")
    return {"authenticated": False, "status": "logged_out"}


@auth_router.get("/auth/whoami")
async def auth_whoami(
    _: Request,
    principal: OperatorPrincipal = Depends(require_read_access),
) -> dict[str, Any]:
    return {
        "identity": principal.identity,
        "role": principal.role,
        "auth_source": principal.auth_source,
        "auth_enabled": principal.auth_enabled,
    }


@auth_router.get("/auth/providers")
async def auth_providers(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    settings = runtime.settings
    return {
        "auth_enabled": settings.operator_auth_enabled,
        "session_enabled": settings.operator_session_configured,
        "database_backed": runtime.database_runtime is not None,
        "configured_roles": configured_operator_roles(runtime),
        "stored_user_count": stored_operator_user_count(runtime),
        "runtime_profile_control_enabled": False,
        "api_key_compatibility_enabled": bool(settings.operator_read_api_key or settings.operator_write_api_key),
    }


@auth_router.get("/auth/users")
async def auth_users(
    request: Request,
    principal: OperatorPrincipal = Depends(require_admin_access),
) -> dict[str, Any]:
    return _query(request).operator_users(actor_identity=principal.identity)


@auth_router.get("/runtime-profiles")
async def runtime_profiles(
    request: Request,
    principal: OperatorPrincipal = Depends(require_admin_access),
) -> dict[str, Any]:
    _ = principal
    return _query(request).runtime_profile_snapshot()


@auth_router.get("/runtime-profiles/summary")
async def runtime_profiles_summary(
    request: Request,
    principal: OperatorPrincipal = Depends(require_admin_access),
) -> dict[str, Any]:
    _ = principal
    return _query(request).runtime_profile_ai_config_snapshot()


@auth_router.post("/runtime-profiles/drafts")
async def create_runtime_profile_draft(
    request: Request,
    payload: CreateRuntimeProfileDraftRequest,
    principal: OperatorPrincipal = Depends(require_admin_access),
) -> dict[str, Any]:
    _runtime_profile_control_disabled(
        request,
        principal=principal,
        action="runtime_profile_create",
        details={"profile_label": payload.profile_label},
    )


@auth_router.patch("/runtime-profiles/revisions/{revision_id}")
async def update_runtime_profile(
    request: Request,
    revision_id: str,
    payload: UpdateRuntimeProfileRequest,
    principal: OperatorPrincipal = Depends(require_admin_access),
) -> dict[str, Any]:
    _runtime_profile_control_disabled(
        request,
        principal=principal,
        action="runtime_profile_update",
        details={
            "revision_id": revision_id,
            "profile_label": payload.profile_label,
            "payload_keys": sorted(payload.payload.keys()),
            "activation_note_present": payload.activation_note is not None,
        },
    )


@auth_router.post("/runtime-profiles/revisions/{revision_id}/stage")
async def stage_runtime_profile(
    request: Request,
    revision_id: str,
    payload: StageRuntimeProfileRequest | None = None,
    principal: OperatorPrincipal = Depends(require_admin_access),
) -> dict[str, Any]:
    _runtime_profile_control_disabled(
        request,
        principal=principal,
        action="runtime_profile_stage",
        details={
            "revision_id": revision_id,
            "activation_note_present": payload is not None and payload.activation_note is not None,
        },
    )


@auth_router.post("/runtime-profiles/pending/cancel")
async def cancel_pending_runtime_profile(
    request: Request,
    principal: OperatorPrincipal = Depends(require_admin_access),
) -> dict[str, Any]:
    _runtime_profile_control_disabled(
        request,
        principal=principal,
        action="runtime_profile_cancel_pending",
    )


@auth_router.post("/runtime-profiles/restart")
async def request_runtime_profile_restart(
    request: Request,
    principal: OperatorPrincipal = Depends(require_admin_access),
) -> dict[str, Any]:
    _runtime_profile_control_disabled(
        request,
        principal=principal,
        action="runtime_profile_restart_request",
    )


@auth_router.get("/strategy-profiles")
async def strategy_profiles(
    request: Request,
    principal: OperatorPrincipal = Depends(require_admin_access),
) -> dict[str, Any]:
    _ = principal
    return _query(request).strategy_profile_snapshot()


@auth_router.get("/strategy-profiles/summary")
async def strategy_profiles_summary(
    request: Request,
    principal: OperatorPrincipal = Depends(require_admin_access),
) -> dict[str, Any]:
    _ = principal
    return _query(request).strategy_profile_ai_config_snapshot()


@auth_router.get("/strategy-profiles/activation-history")
async def strategy_profile_activation_history(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=5000),
    principal: OperatorPrincipal = Depends(require_admin_access),
) -> dict[str, Any]:
    _ = principal
    return _query(request).strategy_profile_activation_history(limit=limit, offset=offset)


@auth_router.get("/strategy-profiles/optimization/reports")
async def strategy_profile_optimization_reports(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=5000),
    principal: OperatorPrincipal = Depends(require_admin_access),
) -> dict[str, Any]:
    _ = principal
    return _query(request).strategy_profile_optimization_reports(limit=limit, offset=offset)


@auth_router.get("/strategy-profiles/selection-decisions")
async def strategy_profile_selection_decisions(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=5000),
    principal: OperatorPrincipal = Depends(require_admin_access),
) -> dict[str, Any]:
    _ = principal
    return _query(request).strategy_profile_selection_decisions(limit=limit, offset=offset)


@auth_router.post("/strategy-profiles/profiles/{profile_id}/activate")
async def activate_strategy_profile(
    request: Request,
    profile_id: str,
    payload: StrategyProfileManualActivateRequest,
    principal: OperatorPrincipal = Depends(require_admin_access),
) -> dict[str, Any]:
    try:
        return _query(request).activate_strategy_profile(
            profile_id=profile_id,
            reason=payload.reason,
            actor_role=principal.role,
            actor_identity=principal.identity,
            auth_source=principal.auth_source,
        )
    except ValueError as exc:
        detail = str(exc)
        if detail == "strategy_profile_profile_not_found":
            raise HTTPException(status_code=404, detail=detail) from exc
        raise HTTPException(status_code=409, detail=detail) from exc


@auth_router.post("/strategy-profiles/restore-auto")
async def restore_strategy_profile_auto(
    request: Request,
    payload: StrategyProfileManualRestoreRequest | None = None,
    principal: OperatorPrincipal = Depends(require_admin_access),
) -> dict[str, Any]:
    try:
        return _query(request).restore_strategy_profile_auto(
            reason=payload.reason if payload is not None else "manual_restore_auto_strategy_profile_control",
            actor_role=principal.role,
            actor_identity=principal.identity,
            auth_source=principal.auth_source,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@auth_router.post("/ai/operating-mode/override")
async def override_ai_operating_mode(
    request: Request,
    payload: AIManualOperatingModeOverrideRequest,
    principal: OperatorPrincipal = Depends(require_admin_access),
) -> dict[str, Any]:
    try:
        return _query(request).set_ai_operating_mode_override(
            mode=payload.mode,
            reason=payload.reason,
            actor_role=principal.role,
            actor_identity=principal.identity,
            auth_source=principal.auth_source,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@auth_router.post("/ai/operating-mode/restore-auto")
async def restore_ai_operating_mode_auto(
    request: Request,
    payload: AIManualOperatingModeRestoreRequest | None = None,
    principal: OperatorPrincipal = Depends(require_admin_access),
) -> dict[str, Any]:
    return _query(request).clear_ai_operating_mode_override(
        reason=payload.reason if payload is not None else "manual_restore_auto_ai_operating_mode",
        actor_role=principal.role,
        actor_identity=principal.identity,
        auth_source=principal.auth_source,
    )


@auth_router.post("/auth/users")
async def create_auth_user(
    request: Request,
    payload: CreateOperatorUserRequest,
    principal: OperatorPrincipal = Depends(require_admin_access),
) -> dict[str, Any]:
    try:
        return _query(request).create_operator_user(
            username=payload.username,
            password=payload.password,
            role=payload.role,
            enabled=payload.enabled,
            actor_role=principal.role,
            actor_identity=principal.identity,
            auth_source=principal.auth_source,
        )
    except ValueError as exc:
        raise _operator_user_http_error(str(exc)) from exc


@auth_router.patch("/auth/users/{username}")
async def update_auth_user(
    request: Request,
    username: str,
    payload: UpdateOperatorUserRequest,
    principal: OperatorPrincipal = Depends(require_admin_access),
) -> dict[str, Any]:
    if payload.role is None and payload.password is None and payload.enabled is None:
        raise HTTPException(status_code=400, detail="operator_user_update_empty")
    try:
        return _query(request).update_operator_user(
            username=username,
            role=payload.role,
            password=payload.password,
            enabled=payload.enabled,
            actor_role=principal.role,
            actor_identity=principal.identity,
            auth_source=principal.auth_source,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="operator_user_not_found") from exc
    except ValueError as exc:
        raise _operator_user_http_error(str(exc)) from exc


@auth_router.delete("/auth/users/{username}")
async def delete_auth_user(
    request: Request,
    username: str,
    principal: OperatorPrincipal = Depends(require_admin_access),
) -> dict[str, Any]:
    try:
        return _query(request).delete_operator_user(
            username=username,
            actor_role=principal.role,
            actor_identity=principal.identity,
            auth_source=principal.auth_source,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="operator_user_not_found") from exc
    except ValueError as exc:
        raise _operator_user_http_error(str(exc)) from exc


def _operator_user_http_error(code: str) -> HTTPException:
    if code in {"operator_role_invalid", "operator_password_required", "operator_username_required"}:
        return HTTPException(status_code=400, detail=code)
    if code == "operator_username_conflict":
        return HTTPException(status_code=409, detail=code)
    if code in {"operator_last_admin_required", "operator_self_delete_forbidden", "operator_self_disable_forbidden"}:
        return HTTPException(status_code=409, detail=code)
    return HTTPException(status_code=400, detail=code)


def _runtime_profile_http_error(code: str) -> HTTPException:
    if code == "runtime_profile_control_disabled":
        return HTTPException(status_code=409, detail=code)
    if code == "runtime_profile_revision_not_found":
        return HTTPException(status_code=404, detail=code)
    if code in {
        "runtime_profile_label_required",
        "runtime_profile_revision_locked",
        "runtime_profile_payload_invalid",
        "runtime_profile_fields_unsupported",
        "runtime_profile_already_active",
    }:
        return HTTPException(status_code=400, detail=code)
    if code == "runtime_profile_pending_activation_exists":
        return HTTPException(status_code=409, detail=code)
    if code.startswith("runtime_profile_fields_unsupported:") or code.startswith("runtime_profile_payload_invalid:"):
        return HTTPException(status_code=400, detail=code)
    if code.startswith("runtime_profile_preflight_blocked:"):
        return HTTPException(status_code=409, detail=code)
    return HTTPException(status_code=400, detail=code)
