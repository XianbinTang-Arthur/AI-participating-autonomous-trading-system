from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi import Query
from pydantic import BaseModel

from aats.api.auth import (
    OperatorPrincipal,
    _write_api_key_compatibility_enabled,
    authenticate_operator_user,
    configured_operator_roles,
    require_admin_access,
    require_read_access,
    session_principal,
    stored_operator_user_count,
)
from aats.api.session_auth import issue_session_token
from aats.bootstrap.config import ApplicationRuntime
from aats.schemas.common import utc_now
from aats.schemas.system import RuntimeModeState
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


class StrategyProfileManualActivateRequest(BaseModel):
    reason: str = "manual_activate_strategy_profile"


class StrategyProfileManualRestoreRequest(BaseModel):
    reason: str = "manual_restore_auto_strategy_profile_control"


class StrategyProfileManualPauseRequest(BaseModel):
    reason: str = "manual_pause_auto_strategy_profile_control"


class AISelectOperatingModeRequest(BaseModel):
    mode: str
    reason: str = "manual_select_ai_operating_mode"


def _runtime(request: Request) -> ApplicationRuntime:
    return request.app.state.runtime


def _query(request: Request) -> OperatorQueryService:
    return OperatorQueryService(_runtime(request))


def _session_payload(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    settings = runtime.settings
    principal = session_principal(request)
    return {
        "auth_enabled": settings.operator_auth_enabled,
        "session_enabled": settings.operator_session_configured,
        "api_key_compatibility_enabled": bool(settings.operator_read_api_key or _write_api_key_compatibility_enabled(runtime)),
        "database_backed": runtime.database_runtime is not None,
        "stored_user_count": stored_operator_user_count(runtime),
        "authenticated": principal is not None and principal.auth_enabled,
        "identity": principal.identity if principal is not None else None,
        "role": principal.role if principal is not None else "anonymous",
        "auth_source": principal.auth_source if principal is not None else "anonymous",
    }


def _auth_providers_payload(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    settings = runtime.settings
    return {
        "auth_enabled": settings.operator_auth_enabled,
        "session_enabled": settings.operator_session_configured,
        "database_backed": runtime.database_runtime is not None,
        "configured_roles": configured_operator_roles(runtime),
        "stored_user_count": stored_operator_user_count(runtime),
        "runtime_profile_control_enabled": False,
        "api_key_compatibility_enabled": bool(settings.operator_read_api_key or _write_api_key_compatibility_enabled(runtime)),
    }


def _system_health_payload(request: Request, query: OperatorQueryService) -> dict[str, Any]:
    health = query.system_health()
    operator_metrics = query.metrics()
    runtime = _runtime(request)
    health["execution_summary"] = {
        "order_count": len(query._scoped_order_states()),
        "fill_count": len(query._scoped_fills()),
        "open_order_count": len(query._scoped_open_order_states()),
        "order_intents_generated": runtime.metrics.snapshot().get("order_intents_generated", 0),
        "fills_processed": runtime.metrics.snapshot().get("fills_processed", 0),
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


def _dashboard_panel_error(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, str):
            return detail
        return str(detail)
    return str(exc)


def _normalize_dashboard_panel_keys(panel_keys: list[str]) -> tuple[str, ...]:
    normalized = [str(panel_key or "").strip() for panel_key in panel_keys]
    filtered = [panel_key for panel_key in normalized if panel_key]
    return tuple(dict.fromkeys(filtered))


def _protected_dashboard_panel_payload(
    *,
    request: Request,
    query: OperatorQueryService,
    panel_key: str,
    recent_decisions_limit: int,
    recent_orders_limit: int,
    recent_fills_limit: int,
    recent_ai_assessments_limit: int,
    recent_ai_shadow_decisions_limit: int,
    recent_ai_shadow_evaluations_limit: int,
) -> dict[str, Any]:
    if panel_key == "health":
        return _system_health_payload(request, query)
    if panel_key == "mode":
        return RuntimeModeState(**query.system_mode()).model_dump(mode="json")
    if panel_key == "runtime":
        return query.system_runtime()
    if panel_key == "systemRecovery":
        return query.system_recovery()
    if panel_key == "blockerControl":
        return query.blocker_control()
    if panel_key == "blockers":
        blockers = query.blockers()
        return {
            "blocked": bool(blockers),
            "halted": _runtime(request).kill_switch.halted,
            "blockers": blockers,
            "recent_history": query.blocker_history(limit=20, offset=0)["history"],
        }
    if panel_key == "metrics":
        return query.metrics()
    if panel_key == "portfolio":
        return query.portfolio_latest()
    if panel_key == "latestDecision":
        return query.latest_decision()
    if panel_key == "executionLatest":
        return query.execution_latest()
    if panel_key == "reconciliationLatest":
        return query.reconciliation_latest()
    if panel_key == "accountState":
        return query.account_state()
    if panel_key == "strategyRuntime":
        return query.strategy_runtime()
    if panel_key == "strategyAttribution":
        return query.strategy_attribution_report(limit=200)
    if panel_key == "recentDecisions":
        return query.recent_decisions(limit=recent_decisions_limit, offset=0)
    if panel_key == "trialReviewSummary":
        return query.trial_review_summary(segment_limit=100, window_days=7, period_count=4)
    if panel_key == "trialReviewHistory":
        return query.trial_review_history(limit=5, offset=0)
    if panel_key == "recentOrders":
        return query.orders_recent(limit=recent_orders_limit, offset=0)
    if panel_key == "recentFills":
        return query.fills_recent(limit=recent_fills_limit, offset=0)
    if panel_key == "executionErrors":
        return query.execution_errors()
    if panel_key == "phase1Shadow":
        return query.phase1_shadow()
    if panel_key == "trialGuard":
        return query.trial_guard()
    if panel_key == "guardedLivePreflight":
        return query.guarded_live_preflight()
    if panel_key == "guardedLiveRunPacket":
        return query.guarded_live_run_packet()
    if panel_key == "replayStatus":
        return query.replay_status()
    if panel_key == "aiOverview":
        return query.ai_overview()
    if panel_key == "aiRuntime":
        return query.ai_runtime()
    if panel_key == "aiLatest":
        return query.ai_latest()
    if panel_key == "aiShadowLatest":
        return query.ai_shadow_latest()
    if panel_key == "profileControlSummary":
        return query.profile_control_summary_report()
    if panel_key == "aiConfigModel":
        return query.ai_config_summary()
    if panel_key == "aiRecent":
        return query.ai_recent(limit=recent_ai_assessments_limit, offset=0)
    if panel_key == "aiShadowRecent":
        return query.ai_shadow_recent(limit=recent_ai_shadow_decisions_limit, offset=0)
    if panel_key == "aiShadowEvaluations":
        return query.ai_shadow_evaluations(limit=recent_ai_shadow_evaluations_limit, offset=0)
    raise KeyError(f"dashboard_bundle_panel_not_found:{panel_key}")


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
    login_result = authenticate_operator_user(runtime, username=payload.username, password=payload.password)
    principal = login_result.principal
    if principal is None:
        _query(request).record_operator_login_failure(
            actor_identity=payload.username,
            auth_source="session",
            failure_code=login_result.failure_code or "operator_login_failed",
        )
        if login_result.failure_code == "operator_login_locked":
            raise HTTPException(status_code=429, detail="operator_login_locked")
        raise HTTPException(status_code=401, detail=login_result.failure_code or "operator_login_failed")
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
    return _auth_providers_payload(request)


@auth_router.get("/dashboard/bundle")
async def dashboard_bundle(
    request: Request,
    panel: list[str] = Query(default=[]),
    view: str | None = Query(default=None),
    recent_decisions: int = Query(default=8, alias="recentDecisions", ge=1, le=100),
    recent_orders: int = Query(default=8, alias="recentOrders", ge=1, le=200),
    recent_fills: int = Query(default=8, alias="recentFills", ge=1, le=200),
    recent_ai_assessments: int = Query(default=8, alias="recentAIAssessments", ge=1, le=100),
    recent_ai_shadow_decisions: int = Query(default=8, alias="recentAIShadowDecisions", ge=1, le=100),
    recent_ai_shadow_evaluations: int = Query(default=8, alias="recentAIShadowEvaluations", ge=1, le=100),
) -> dict[str, Any]:
    query = _query(request)
    api_key = request.headers.get("X-AATS-API-Key")
    panel_keys = _normalize_dashboard_panel_keys(panel)
    if not panel_keys:
        raise HTTPException(status_code=400, detail="dashboard_bundle_panel_required")
    try:
        require_read_access(request, api_key)
        read_error: HTTPException | None = None
    except HTTPException as exc:
        read_error = exc

    panels: dict[str, dict[str, Any]] = {}
    for panel_key in panel_keys:
        try:
            if panel_key == "session":
                payload = _session_payload(request)
            elif panel_key == "authProviders":
                payload = _auth_providers_payload(request)
            elif panel_key == "operatorUsers":
                principal = require_admin_access(request, api_key)
                payload = query.operator_users(actor_identity=principal.identity)
            else:
                if read_error is not None:
                    raise read_error
                payload = _protected_dashboard_panel_payload(
                    request=request,
                    query=query,
                    panel_key=panel_key,
                    recent_decisions_limit=recent_decisions,
                    recent_orders_limit=recent_orders,
                    recent_fills_limit=recent_fills,
                    recent_ai_assessments_limit=recent_ai_assessments,
                    recent_ai_shadow_decisions_limit=recent_ai_shadow_decisions,
                    recent_ai_shadow_evaluations_limit=recent_ai_shadow_evaluations,
                )
            panels[panel_key] = {"data": payload, "error": None}
        except Exception as exc:
            panels[panel_key] = {"data": None, "error": _dashboard_panel_error(exc)}

    return {"view": view, "panels": panels}


@auth_router.get("/auth/users")
async def auth_users(
    request: Request,
    principal: OperatorPrincipal = Depends(require_admin_access),
) -> dict[str, Any]:
    return _query(request).operator_users(actor_identity=principal.identity)


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


@auth_router.post("/strategy-profiles/pause-auto")
async def pause_strategy_profile_auto(
    request: Request,
    payload: StrategyProfileManualPauseRequest | None = None,
    principal: OperatorPrincipal = Depends(require_admin_access),
) -> dict[str, Any]:
    try:
        return _query(request).pause_strategy_profile_auto(
            reason=payload.reason if payload is not None else "manual_pause_auto_strategy_profile_control",
            actor_role=principal.role,
            actor_identity=principal.identity,
            auth_source=principal.auth_source,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@auth_router.post("/ai/operating-mode/select")
async def select_ai_operating_mode(
    request: Request,
    payload: AISelectOperatingModeRequest,
    principal: OperatorPrincipal = Depends(require_admin_access),
) -> dict[str, Any]:
    try:
        return _query(request).set_ai_operating_mode(
            mode=payload.mode,
            reason=payload.reason,
            actor_role=principal.role,
            actor_identity=principal.identity,
            auth_source=principal.auth_source,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
