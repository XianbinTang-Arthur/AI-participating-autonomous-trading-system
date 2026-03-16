from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
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
from aats.services.operator.query_service import OperatorQueryService
from aats.services.operator.runtime_profiles import (
    RuntimeProfileControlService,
    RuntimeProfileError,
    describe_runtime_profile_diff,
)


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


def _runtime_profiles(request: Request) -> RuntimeProfileControlService:
    runtime = _runtime(request)
    return RuntimeProfileControlService(
        settings=runtime.settings,
        repo=runtime.runtime_profile_repo,
        execution_repo=runtime.execution_repo,
        event_store=runtime.event_store,
    )


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
    _query(request).record_operator_login(
        actor_identity=principal.identity or payload.username,
        actor_role=principal.role,
        auth_source="session",
    )
    token = issue_session_token(settings=settings, identity=principal.identity or payload.username, role=principal.role)
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
    settings = _runtime(request).settings
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
        "bootstrap_pending": (
            settings.operator_bootstrap_enabled
            and settings.operator_bootstrap_users_configured
            and stored_operator_user_count(runtime) == 0
        ),
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


@auth_router.post("/runtime-profiles/drafts")
async def create_runtime_profile_draft(
    request: Request,
    payload: CreateRuntimeProfileDraftRequest,
    principal: OperatorPrincipal = Depends(require_admin_access),
) -> dict[str, Any]:
    control = _runtime_profiles(request)
    try:
        revision, diff = control.create_draft(
            profile_label=payload.profile_label,
            actor_identity=principal.identity,
        )
    except RuntimeProfileError as exc:
        raise _runtime_profile_http_error(str(exc)) from exc
    _query(request).record_runtime_profile_action(
        action="runtime_profile_create",
        actor_role=principal.role,
        actor_identity=principal.identity,
        auth_source=principal.auth_source,
        status="draft_created",
        new_revision_id=revision.revision_id,
        details={"changed_fields": diff.changed_fields},
    )
    return {
        "revision": revision.model_dump(mode="json"),
        "diff": diff.model_dump(mode="json"),
        "diff_narrative": describe_runtime_profile_diff(diff),
    }


@auth_router.patch("/runtime-profiles/revisions/{revision_id}")
async def update_runtime_profile(
    request: Request,
    revision_id: str,
    payload: UpdateRuntimeProfileRequest,
    principal: OperatorPrincipal = Depends(require_admin_access),
) -> dict[str, Any]:
    control = _runtime_profiles(request)
    try:
        revision, diff = control.update_draft(
            revision_id=revision_id,
            profile_label=payload.profile_label,
            payload=payload.payload,
            activation_note=payload.activation_note,
            actor_identity=principal.identity,
        )
    except RuntimeProfileError as exc:
        raise _runtime_profile_http_error(str(exc)) from exc
    _query(request).record_runtime_profile_action(
        action="runtime_profile_update",
        actor_role=principal.role,
        actor_identity=principal.identity,
        auth_source=principal.auth_source,
        status="draft_validated",
        previous_revision_id=revision.supersedes_revision_id,
        new_revision_id=revision.revision_id,
        details={
            "changed_fields": diff.changed_fields,
            "change_classification": diff.classification,
        },
    )
    return {
        "revision": revision.model_dump(mode="json"),
        "diff": diff.model_dump(mode="json"),
        "diff_narrative": describe_runtime_profile_diff(diff),
    }


@auth_router.post("/runtime-profiles/revisions/{revision_id}/stage")
async def stage_runtime_profile(
    request: Request,
    revision_id: str,
    payload: StageRuntimeProfileRequest | None = None,
    principal: OperatorPrincipal = Depends(require_admin_access),
) -> dict[str, Any]:
    control = _runtime_profiles(request)
    try:
        if payload is not None and payload.activation_note is not None:
            control.update_draft(
                revision_id=revision_id,
                profile_label=None,
                payload={},
                activation_note=payload.activation_note,
                actor_identity=principal.identity,
            )
        revision, diff, preflight, activation = control.stage_revision(
            revision_id=revision_id,
            actor_identity=principal.identity,
        )
    except RuntimeProfileError as exc:
        code = str(exc)
        if code.startswith("runtime_profile_preflight_blocked:"):
            revision = control.repo.get_revision(revision_id)
            active = control.active_revision()
            prior_payload = active.payload if active is not None else control.settings.model_dump(mode="python")
            preflight = control.preflight("product_posture_change")
            _query(request).record_runtime_profile_action(
                action="runtime_profile_stage_rejected",
                actor_role=principal.role,
                actor_identity=principal.identity,
                auth_source=principal.auth_source,
                status="preflight_blocked",
                previous_revision_id=control.active_revision().revision_id if control.active_revision() is not None else None,
                new_revision_id=revision_id,
                details={
                    "preflight": preflight.model_dump(mode="json"),
                    "revision_found": revision is not None,
                    "active_payload_keys": sorted(prior_payload.keys()),
                },
            )
        raise _runtime_profile_http_error(code) from exc
    _query(request).record_runtime_profile_action(
        action="runtime_profile_stage",
        actor_role=principal.role,
        actor_identity=principal.identity,
        auth_source=principal.auth_source,
        status="pending_activation",
        previous_revision_id=activation.previous_active_revision_id,
        new_revision_id=revision.revision_id,
        details={
            "change_classification": diff.classification,
            "preflight": preflight.model_dump(mode="json"),
        },
    )
    return {
        "revision": revision.model_dump(mode="json"),
        "diff": diff.model_dump(mode="json"),
        "diff_narrative": describe_runtime_profile_diff(diff),
        "preflight": preflight.model_dump(mode="json"),
        "activation": activation.model_dump(mode="json"),
    }


@auth_router.post("/runtime-profiles/pending/cancel")
async def cancel_pending_runtime_profile(
    request: Request,
    principal: OperatorPrincipal = Depends(require_admin_access),
) -> dict[str, Any]:
    control = _runtime_profiles(request)
    previous = control.repo.activation_state()
    try:
        activation = control.cancel_pending()
    except RuntimeProfileError as exc:
        raise _runtime_profile_http_error(str(exc)) from exc
    _query(request).record_runtime_profile_action(
        action="runtime_profile_cancel_pending",
        actor_role=principal.role,
        actor_identity=principal.identity,
        auth_source=principal.auth_source,
        status="pending_cleared",
        previous_revision_id=previous.pending_revision_id,
        new_revision_id=activation.active_revision_id,
    )
    return {"activation": activation.model_dump(mode="json")}


@auth_router.post("/runtime-profiles/restart")
async def request_runtime_profile_restart(
    request: Request,
    principal: OperatorPrincipal = Depends(require_admin_access),
) -> dict[str, Any]:
    control = _runtime_profiles(request)
    try:
        activation = control.request_restart(actor_identity=principal.identity)
    except RuntimeProfileError as exc:
        raise _runtime_profile_http_error(str(exc)) from exc
    _query(request).record_runtime_profile_action(
        action="runtime_profile_restart_request",
        actor_role=principal.role,
        actor_identity=principal.identity,
        auth_source=principal.auth_source,
        status="restart_requested",
        previous_revision_id=activation.active_revision_id,
        new_revision_id=activation.pending_revision_id,
    )
    return {"activation": activation.model_dump(mode="json")}


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
