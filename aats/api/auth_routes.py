from __future__ import annotations

import asyncio
from time import perf_counter
from types import SimpleNamespace
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
from aats.bootstrap.logging import get_logger, log_event
from aats.schemas.common import utc_now
from aats.schemas.system import RuntimeModeState
from aats.services.operator.command_bridge import (
    OperatorCommandError,
    OperatorCommandRemoteError,
    OperatorCommandTimeoutError,
)
from aats.services.operator.dashboard_snapshot import (
    DASHBOARD_SNAPSHOT_PANEL_KEYS,
    DASHBOARD_SNAPSHOT_POLICIES,
    DashboardSnapshotPlane,
)
from aats.services.operator.query_service import OperatorQueryService
from aats.services.operator.ui_capabilities import ui_operating_mode_override_enabled


auth_router = APIRouter(include_in_schema=False)
_logger = get_logger("aats.api.auth")


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


def _request_scheme(request: Request) -> str:
    scheme = (request.url.scheme or "").strip().lower()
    return scheme or "http"


def _session_transport_payload(request: Request) -> dict[str, Any]:
    settings = _runtime(request).settings
    request_scheme = _request_scheme(request)
    secure_cookie_required = bool(settings.operator_session_cookie_secure)
    transport_compatible = (not secure_cookie_required) or request_scheme == "https"
    required_transport = "https" if secure_cookie_required else "http_or_https"
    auth_blocked_reason = None if transport_compatible else "operator_https_required_for_secure_session"
    return {
        "request_scheme": request_scheme,
        "secure_cookie_required": secure_cookie_required,
        "transport_compatible": transport_compatible,
        "required_transport": required_transport,
        "auth_blocked_reason": auth_blocked_reason,
    }


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
        **_session_transport_payload(request),
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
        **_session_transport_payload(request),
    }


_DASHBOARD_AUTH_ERROR_CODES = {
    "operator_auth_required",
    "operator_write_auth_required",
    "operator_write_access_required",
    "operator_admin_access_required",
    "operator_https_required_for_secure_session",
}


def _dashboard_bundle_auth_summary(
    request: Request,
    *,
    panel_keys: tuple[str, ...],
    panels: dict[str, dict[str, Any]],
    read_error: HTTPException | None,
) -> dict[str, Any]:
    session_payload = _session_payload(request)
    providers_payload = _auth_providers_payload(request)
    protected_panel_keys = [key for key in panel_keys if key not in {"session", "authProviders"}]
    blocked_panel_keys: list[str] = []
    primary_error: str | None = providers_payload.get("auth_blocked_reason")

    for panel_key in protected_panel_keys:
        panel_error = panels.get(panel_key, {}).get("error")
        if not isinstance(panel_error, str) or panel_error not in _DASHBOARD_AUTH_ERROR_CODES:
            continue
        blocked_panel_keys.append(panel_key)
        if primary_error is None:
            primary_error = panel_error

    read_error_code = _dashboard_panel_error(read_error) if read_error is not None else None
    if primary_error is None and read_error_code in _DASHBOARD_AUTH_ERROR_CODES:
        primary_error = read_error_code

    if not providers_payload["auth_enabled"]:
        access_state = "disabled"
    elif not providers_payload["transport_compatible"]:
        access_state = "transport_blocked"
    elif read_error_code in {"operator_auth_required", "operator_write_auth_required"}:
        access_state = "auth_required"
    elif blocked_panel_keys:
        access_state = "granted"
    else:
        access_state = "granted" if read_error is None else "auth_required"

    return {
        "auth_enabled": providers_payload["auth_enabled"],
        "authenticated": session_payload["authenticated"],
        "request_scheme": providers_payload["request_scheme"],
        "secure_cookie_required": providers_payload["secure_cookie_required"],
        "transport_compatible": providers_payload["transport_compatible"],
        "required_transport": providers_payload["required_transport"],
        "auth_blocked_reason": providers_payload["auth_blocked_reason"],
        "protected_panel_keys": protected_panel_keys,
        "blocked_panel_keys": blocked_panel_keys,
        "primary_error": primary_error,
        "access_state": access_state,
    }


def _system_health_payload_for_runtime(runtime: ApplicationRuntime, query: OperatorQueryService) -> dict[str, Any]:
    health = query.system_health()
    operator_metrics = query.metrics()
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


def _system_health_payload(request: Request, query: OperatorQueryService) -> dict[str, Any]:
    return _system_health_payload_for_runtime(_runtime(request), query)


def _blockers_panel_payload_from_blocker_control(
    *,
    request: Request,
    query: OperatorQueryService,
    blocker_control: dict[str, Any],
) -> dict[str, Any]:
    return _blockers_panel_payload_from_blocker_control_for_runtime(
        runtime=_runtime(request),
        query=query,
        blocker_control=blocker_control,
    )


def _blockers_panel_payload_from_blocker_control_for_runtime(
    *,
    runtime: ApplicationRuntime,
    query: OperatorQueryService,
    blocker_control: dict[str, Any],
) -> dict[str, Any]:
    blocker_items = blocker_control.get("blockers")
    blockers = []
    if isinstance(blocker_items, list):
        blockers = [
            _legacy_blocker_payload(item)
            for item in blocker_items
            if isinstance(item, dict)
        ]
    return {
        "blocked": bool(blockers),
        "halted": runtime.kill_switch.halted,
        "blockers": blockers,
        "recent_history": query.blocker_history(limit=20, offset=0)["history"],
    }


def _legacy_blocker_payload(item: dict[str, Any]) -> dict[str, Any]:
    subsystem = item.get("subsystem")
    return {
        "blocker": item.get("blocker"),
        "subsystem": subsystem,
        "affects_execution": item.get("affects_execution", True),
        "affects_account_synchronization": subsystem == "account_state",
        "submit_only": item.get("submit_only", False),
        "recommended_action": item.get("recommended_next_step"),
        "recommended_next_step": item.get("recommended_next_step"),
        "title": item.get("title"),
        "description": item.get("description"),
        "impact": item.get("impact"),
        "priority": item.get("priority"),
        "root_cause": item.get("root_cause", False),
        "derived_from": list(item.get("derived_from") or []),
        "resolution_mode": item.get("resolution_mode"),
        "actions": list(item.get("actions") or []),
    }


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


def _dashboard_snapshot_default_payload(panel_key: str) -> dict[str, Any]:
    if panel_key == "health":
        return {
            "runtime_state": "unknown",
            "execution_summary": {},
        }
    if panel_key == "mode":
        return {}
    if panel_key == "runtime":
        return {}
    if panel_key == "systemRecovery":
        return {"recovery": {}}
    if panel_key == "blockerControl":
        return {
            "blocked": False,
            "blockers": [],
            "actions": [],
        }
    if panel_key == "blockers":
        return {
            "blocked": False,
            "halted": False,
            "blockers": [],
            "recent_history": [],
        }
    if panel_key == "aiRuntime":
        return {}
    if panel_key == "metrics":
        return {}
    if panel_key == "accountState":
        return {}
    if panel_key == "latestDecision":
        return {}
    if panel_key == "strategyRuntime":
        return {}
    if panel_key == "executionLatest":
        return {}
    if panel_key == "portfolio":
        return {"portfolio": None}
    if panel_key == "positions":
        return {}
    if panel_key == "reconciliationLatest":
        return {"reconciliation": None}
    if panel_key == "trialGuard":
        return {}
    if panel_key == "guardedLivePreflight":
        return {}
    if panel_key == "guardedLiveRunPacket":
        return {}
    if panel_key == "replayStatus":
        return {}
    if panel_key == "aiOverview":
        return {}
    if panel_key == "aiLatest":
        return {}
    if panel_key == "aiShadowLatest":
        return {}
    if panel_key == "profileControlSummary":
        return {}
    if panel_key == "aiConfigModel":
        return {"ai": {}}
    if panel_key == "rdpControl":
        return {}
    if panel_key == "rdpWorkbenchOverview":
        return {}
    if panel_key == "rdpWorkbenchItems":
        return {}
    if panel_key == "rdpWorkbenchAlerts":
        return {}
    if panel_key == "rdpTuningOverview":
        return {}
    if panel_key == "rdpTuningProposals":
        return {}
    return {}


def _dashboard_snapshot_rdp_request(runtime: ApplicationRuntime) -> Any:
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(runtime=runtime)),
        state=SimpleNamespace(),
    )


async def _load_dashboard_snapshot_panel(runtime: ApplicationRuntime, panel_key: str) -> dict[str, Any]:
    query = OperatorQueryService(runtime)
    if panel_key == "aiRuntime":
        return dict(await query.ai_runtime_authoritative())
    if panel_key == "aiOverview":
        runtime_payload = dict(await query.ai_runtime_authoritative())
        return query.ai_overview_with_runtime(runtime_payload)
    if panel_key == "aiConfigModel":
        runtime_payload = dict(await query.ai_runtime_authoritative())
        return query.ai_config_summary_with_runtime(runtime_payload)

    def _load_sync_panel() -> dict[str, Any]:
        if panel_key == "health":
            return _system_health_payload_for_runtime(runtime, query)
        if panel_key == "mode":
            return RuntimeModeState(**query.system_mode()).model_dump(mode="json")
        if panel_key == "runtime":
            return query.system_runtime()
        if panel_key == "systemRecovery":
            return query.system_recovery()
        if panel_key == "blockerControl":
            return query.blocker_control()
        if panel_key == "blockers":
            return _blockers_panel_payload_from_blocker_control_for_runtime(
                runtime=runtime,
                query=query,
                blocker_control=query.blocker_control(),
            )
        if panel_key == "metrics":
            return query.metrics()
        if panel_key == "accountState":
            return query.account_state()
        if panel_key == "latestDecision":
            return query.latest_decision()
        if panel_key == "strategyRuntime":
            return query.strategy_runtime()
        if panel_key == "executionLatest":
            return query.execution_latest()
        if panel_key == "portfolio":
            return query.portfolio_latest()
        if panel_key == "positions":
            return query.positions()
        if panel_key == "reconciliationLatest":
            return query.reconciliation_latest()
        if panel_key == "trialGuard":
            return query.trial_guard()
        if panel_key == "guardedLivePreflight":
            return query.guarded_live_preflight()
        if panel_key == "guardedLiveRunPacket":
            return query.guarded_live_run_packet()
        if panel_key == "replayStatus":
            return query.replay_status()
        if panel_key == "aiLatest":
            return query.ai_latest()
        if panel_key == "aiShadowLatest":
            return query.ai_shadow_latest()
        if panel_key == "profileControlSummary":
            return query.profile_control_summary_report()
        if panel_key.startswith("rdp"):
            request = _dashboard_snapshot_rdp_request(runtime)
            if panel_key == "rdpControl":
                from aats.api.rdp_control_summary import build_rdp_control_summary

                return build_rdp_control_summary(request)
            if panel_key == "rdpWorkbenchOverview":
                from aats.api.rdp_control_summary import build_rdp_workbench_overview

                return build_rdp_workbench_overview(request)
            if panel_key == "rdpWorkbenchItems":
                from aats.api.rdp_control_summary import build_rdp_workbench_items

                return build_rdp_workbench_items(request)
            if panel_key == "rdpWorkbenchAlerts":
                from aats.api.rdp_control_summary import build_rdp_workbench_alerts

                return build_rdp_workbench_alerts(request)
            if panel_key == "rdpTuningOverview":
                from aats.api.rdp_control_summary import build_rdp_tuning_overview

                return build_rdp_tuning_overview(request)
            if panel_key == "rdpTuningProposals":
                from aats.api.rdp_control_summary import build_rdp_tuning_proposals

                return build_rdp_tuning_proposals(request)
        raise KeyError(f"dashboard_snapshot_panel_not_found:{panel_key}")

    return await asyncio.to_thread(_load_sync_panel)


def install_dashboard_snapshot_plane(runtime: ApplicationRuntime) -> DashboardSnapshotPlane:
    return DashboardSnapshotPlane(
        loader=lambda panel_key: _load_dashboard_snapshot_panel(runtime, panel_key),
        default_factory=_dashboard_snapshot_default_payload,
        policies=DASHBOARD_SNAPSHOT_POLICIES,
    )


async def start_dashboard_snapshot_plane(app: Any, runtime: ApplicationRuntime) -> DashboardSnapshotPlane:
    plane = install_dashboard_snapshot_plane(runtime)
    app.state.dashboard_snapshot_plane = plane
    await plane.start()
    return plane


async def stop_dashboard_snapshot_plane(app: Any) -> None:
    plane = getattr(app.state, "dashboard_snapshot_plane", None)
    if isinstance(plane, DashboardSnapshotPlane):
        await plane.stop()
    if hasattr(app.state, "dashboard_snapshot_plane"):
        delattr(app.state, "dashboard_snapshot_plane")


def _dashboard_snapshot_plane(request: Request) -> DashboardSnapshotPlane | None:
    plane = getattr(request.app.state, "dashboard_snapshot_plane", None)
    return plane if isinstance(plane, DashboardSnapshotPlane) else None


# -----------------------------------------------------------------------------
# Dashboard bundle in-memory cache (Plan E)
# -----------------------------------------------------------------------------
# 场景：dashboard 首屏会在 1-2 秒内爆发多次 /dashboard/bundle 请求：
#   - refreshDashboard() 同时请求 primary + deferred bundle（2 次）
#   - 多个 tab 同时打开同一控制台（每个 tab × 2 次）
#   - 背景 AUTO_REFRESH_MS 窗口内和手动刷新并发（2 次）
# 每次 bundle 都会在线程池里跑几十个 OperatorQueryService 同步 DB 查询，
# 大部分 panel 的源数据在 2 秒内根本不会变（决策周期本身 ~15s 跑一次）。
# 短 TTL 内存缓存 + 同 key in-flight 去重能把这类重复请求直接收敛成一次
# 后端计算，明显缓解 event loop 的 DB I/O 压力。
#
# 安全性注意事项：
#   1. 缓存 KEY 必须包含 (role, identity)，否则 admin 的 operatorUsers panel
#      可能被匿名用户的缓存命中而返回空数据或越权数据。
#   2. TTL 必须远小于用户"感知实时"的阈值。2 秒足够让同帧内并发请求命中，
#      又小于 AUTO_REFRESH_MS=30s 的一个数量级，用户无法察觉陈旧。
#   3. 只缓存成功响应。失败路径 (raise) 不写缓存；inflight 的 future 异常
#      也会从 dict 里清理，下次请求重新跑。
#   4. 缓存 dict 是 module-level，单进程单 event loop 下天然线程安全
#      （asyncio 协程之间没有抢占，只有 await 点才让出控制权，而 cache
#      check / inflight set 这段路径里没有 await）。
_BUNDLE_CACHE_TTL_SECONDS = 2.0
_DASHBOARD_BUNDLE_SLOW_MS = 2_000.0
_bundle_cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
_bundle_cache_inflight: dict[tuple[Any, ...], tuple[int, "asyncio.Future[dict[str, Any]]"]] = {}
# 代际计数器：每次 invalidate 递增。inflight compute 完成后只有代际一致
# 才写缓存，防止 mutation 之前启动的计算把旧结果写回已清空的缓存。
_bundle_cache_generation: int = 0


def invalidate_bundle_cache() -> None:
    """Drop the entire dashboard bundle cache and bump the generation.

    Called from the FastAPI app-level middleware after any successful
    mutating request (POST/PATCH/PUT/DELETE). Bumping the generation
    ensures that any in-flight compute that started BEFORE this
    invalidation will NOT write its (now-stale) result back into the
    cache when it finishes.
    """
    global _bundle_cache_generation
    _bundle_cache_generation += 1
    _bundle_cache.clear()


def _bundle_cache_key(
    *,
    principal: OperatorPrincipal | None,
    panel_keys: tuple[str, ...],
    view: str | None,
    recent_decisions: int,
    recent_orders: int,
    recent_fills: int,
    recent_replay_validations: int,
    recent_ai_assessments: int,
    recent_ai_shadow_decisions: int,
    recent_ai_shadow_evaluations: int,
) -> tuple[Any, ...]:
    identity = principal.identity if principal is not None else None
    role = principal.role if principal is not None else "anonymous"
    return (
        identity or "anonymous",
        role,
        # 把 panel_keys 排序后再入 key，否则 `panel=foo&panel=bar` 和
        # `panel=bar&panel=foo` 会被当成两个 key 重复 compute。
        tuple(sorted(panel_keys)),
        view or "",
        recent_decisions,
        recent_orders,
        recent_fills,
        recent_replay_validations,
        recent_ai_assessments,
        recent_ai_shadow_decisions,
        recent_ai_shadow_evaluations,
    )


def _protected_dashboard_panel_payload(
    *,
    request: Request,
    query: OperatorQueryService,
    view: str | None = None,
    panel_key: str,
    recent_decisions_limit: int,
    recent_orders_limit: int,
    recent_fills_limit: int,
    recent_replay_validations_limit: int,
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
        return _blockers_panel_payload_from_blocker_control(
            request=request,
            query=query,
            blocker_control=query.blocker_control(),
        )
    if panel_key == "metrics":
        return query.metrics()
    if panel_key == "portfolio":
        return query.portfolio_latest()
    if panel_key == "positions":
        return query.positions()
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
    if panel_key == "positionLifecycleAttribution":
        limit = 6 if view == "strategy" else 8
        return query.position_lifecycle_attribution(limit=limit)
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
    if panel_key == "replayRecentValidations":
        return query.replay_recent_validations(limit=recent_replay_validations_limit, offset=0)
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
    if panel_key == "rdpControl":
        from aats.api.rdp_control_summary import build_rdp_control_summary

        return build_rdp_control_summary(request)
    if panel_key == "rdpWorkbenchOverview":
        from aats.api.rdp_control_summary import build_rdp_workbench_overview

        return build_rdp_workbench_overview(request)
    if panel_key == "rdpWorkbenchItems":
        from aats.api.rdp_control_summary import build_rdp_workbench_items

        return build_rdp_workbench_items(request)
    if panel_key == "rdpWorkbenchAlerts":
        from aats.api.rdp_control_summary import build_rdp_workbench_alerts

        return build_rdp_workbench_alerts(request)
    if panel_key == "rdpTuningOverview":
        from aats.api.rdp_control_summary import build_rdp_tuning_overview

        return build_rdp_tuning_overview(request)
    if panel_key == "rdpTuningProposals":
        from aats.api.rdp_control_summary import build_rdp_tuning_proposals

        return build_rdp_tuning_proposals(request)
    raise KeyError(f"dashboard_bundle_panel_not_found:{panel_key}")


def _strategy_view_strategy_runtime_payload(payload: dict[str, Any]) -> dict[str, Any]:
    latest_snapshot = payload.get("latest_snapshot") if isinstance(payload, dict) else {}
    configured_parameters = payload.get("configured_parameters") if isinstance(payload, dict) else {}
    latest_snapshot = latest_snapshot if isinstance(latest_snapshot, dict) else {}
    configured_parameters = configured_parameters if isinstance(configured_parameters, dict) else {}
    return {
        "generated_at": payload.get("generated_at"),
        "summary": payload.get("summary") or {},
        "entry_execution_guard": payload.get("entry_execution_guard") or {},
        "family_enablement": payload.get("family_enablement") or {},
        "configured_parameters": {
            "strategy_family_active": configured_parameters.get("strategy_family_active"),
            "strategy_family_auto_selection_enabled": configured_parameters.get("strategy_family_auto_selection_enabled"),
            "strategy_sleeve_auto_execution_enabled": configured_parameters.get("strategy_sleeve_auto_execution_enabled"),
            "strategy_sleeve_auto_execution_config_source": configured_parameters.get("strategy_sleeve_auto_execution_config_source"),
            "strategy_sleeve_auto_execution_uses_deprecated_key": configured_parameters.get("strategy_sleeve_auto_execution_uses_deprecated_key"),
            "compatibility": {
                "deprecated_auto_execution_key": (
                    configured_parameters.get("compatibility", {}) or {}
                ).get("deprecated_auto_execution_key"),
                "deprecated_auto_execution_value": (
                    configured_parameters.get("compatibility", {}) or {}
                ).get("deprecated_auto_execution_value"),
            },
            "strategy_sleeve_auto_min_budget_multiplier": configured_parameters.get("strategy_sleeve_auto_min_budget_multiplier"),
            "strategy_sleeve_auto_reconciliation_contraction_multiplier": configured_parameters.get("strategy_sleeve_auto_reconciliation_contraction_multiplier"),
            "strategy_sleeve_auto_soft_loss_usdt": configured_parameters.get("strategy_sleeve_auto_soft_loss_usdt"),
            "strategy_sleeve_auto_hard_loss_usdt": configured_parameters.get("strategy_sleeve_auto_hard_loss_usdt"),
            "strategy_sleeve_auto_volatility_cap_enabled": configured_parameters.get("strategy_sleeve_auto_volatility_cap_enabled"),
            "env_template_profile": configured_parameters.get("env_template_profile"),
        },
        "latest_snapshot": {
            "automation_decisions": list(latest_snapshot.get("automation_decisions") or []),
        },
        "latest_bundle": payload.get("latest_bundle") or {},
        "latest_applied_target": payload.get("latest_applied_target") or {},
        "truth_source": payload.get("truth_source"),
    }


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
    transport = _session_transport_payload(request)
    if not transport["transport_compatible"]:
        raise HTTPException(status_code=400, detail=transport["auth_blocked_reason"])
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


def _bundle_response_with_cache_info(
    *,
    base_payload: dict[str, Any],
    request_started_at: float,
    cache_hit: bool,
    cache_age_ms: float,
    deduped: bool,
) -> dict[str, Any]:
    """Return a shallow copy of `base_payload` with this request's timing.

    Always hands back a NEW outer dict so the cached entry is never mutated by
    FastAPI's serializer or any upstream middleware. The `panels` mapping is
    shared by reference on purpose — it's treated as read-only downstream.
    """
    total_ms = round((perf_counter() - request_started_at) * 1000.0, 3)
    base_timing = base_payload.get("timing") if isinstance(base_payload.get("timing"), dict) else {}
    return {
        "view": base_payload.get("view"),
        "panels": base_payload.get("panels", {}),
        "auth": base_payload.get("auth"),
        "timing": {
            **base_timing,
            "total_ms": total_ms,
            "cache_hit": cache_hit,
            "cache_age_ms": cache_age_ms,
            "deduped": deduped,
        },
    }


@auth_router.get("/dashboard/bundle")
async def dashboard_bundle(
    request: Request,
    panel: list[str] = Query(default=[]),
    view: str | None = Query(default=None),
    recent_decisions: int = Query(default=8, alias="recentDecisions", ge=1, le=100),
    recent_orders: int = Query(default=8, alias="recentOrders", ge=1, le=200),
    recent_fills: int = Query(default=8, alias="recentFills", ge=1, le=200),
    recent_replay_validations: int = Query(default=8, alias="recentReplayValidations", ge=1, le=100),
    recent_ai_assessments: int = Query(default=8, alias="recentAIAssessments", ge=1, le=100),
    recent_ai_shadow_decisions: int = Query(default=8, alias="recentAIShadowDecisions", ge=1, le=100),
    recent_ai_shadow_evaluations: int = Query(default=8, alias="recentAIShadowEvaluations", ge=1, le=100),
) -> dict[str, Any]:
    request_started_at = perf_counter()
    query = _query(request)
    api_key = request.headers.get("X-AATS-API-Key")
    panel_keys = _normalize_dashboard_panel_keys(panel)
    if not panel_keys:
        raise HTTPException(status_code=400, detail="dashboard_bundle_panel_required")

    # Plan E 缓存查找 + 同 key 并发去重。必须在 require_read_access 之前构建
    # cache key，否则会多跑一次授权；session_principal 自己不抛异常，匿名用户
    # 只是返回 None 并以 "anonymous" 参与 key，与登录用户严格隔离。
    principal_for_key = session_principal(request)
    cache_key = _bundle_cache_key(
        principal=principal_for_key,
        panel_keys=panel_keys,
        view=view,
        recent_decisions=recent_decisions,
        recent_orders=recent_orders,
        recent_fills=recent_fills,
        recent_replay_validations=recent_replay_validations,
        recent_ai_assessments=recent_ai_assessments,
        recent_ai_shadow_decisions=recent_ai_shadow_decisions,
        recent_ai_shadow_evaluations=recent_ai_shadow_evaluations,
    )
    loop = asyncio.get_running_loop()
    monotonic_now = loop.time()
    compute_generation = _bundle_cache_generation

    cached_entry = _bundle_cache.get(cache_key)
    if cached_entry is not None:
        cached_at, cached_payload = cached_entry
        age = monotonic_now - cached_at
        if age < _BUNDLE_CACHE_TTL_SECONDS:
            return _bundle_response_with_cache_info(
                base_payload=cached_payload,
                request_started_at=request_started_at,
                cache_hit=True,
                cache_age_ms=round(age * 1000.0, 3),
                deduped=False,
            )
        # TTL 过期：顺手清掉陈旧条目，让重算路径拿到干净的 cache dict。
        _bundle_cache.pop(cache_key, None)

    # In-flight dedup：已经有同 key 请求在算，直接 await 它的 future。注意
    # 这个分支本身会 await，后面拿回的 payload 要走同一份 timing-overlay
    # helper 让前端能区分 "命中 cache" 还是 "等 peer 算完"。
    # 代际守卫：invalidate 后创建的 inflight 属于旧代际，新请求不能复用，
    # 否则 mutation 后的首次拉取仍会拿到旧 payload。
    inflight_entry = _bundle_cache_inflight.get(cache_key)
    if inflight_entry is not None:
        inflight_gen, inflight_future = inflight_entry
        if inflight_gen == compute_generation:
            try:
                shared_payload = await inflight_future
            except Exception:
                # Peer 的 compute 失败 = 这次请求也应该返回相同的错误。让上层
                # HTTPException/KeyError 顺着原路径抛出，不要在这里吞掉。
                raise
            return _bundle_response_with_cache_info(
                base_payload=shared_payload,
                request_started_at=request_started_at,
                cache_hit=True,
                cache_age_ms=0.0,
                deduped=True,
            )
        # 代际不匹配 → 旧 inflight 不可复用，fall through 重新计算。

    future: "asyncio.Future[dict[str, Any]]" = loop.create_future()
    _bundle_cache_inflight[cache_key] = (compute_generation, future)
    try:
        try:
            require_read_access(request, api_key)
            read_error: HTTPException | None = None
        except HTTPException as exc:
            read_error = exc

        authoritative_ai_runtime: dict[str, Any] | None = None
        authoritative_ai_runtime_lock = asyncio.Lock()

        async def _load_authoritative_ai_runtime() -> dict[str, Any]:
            nonlocal authoritative_ai_runtime
            if authoritative_ai_runtime is None:
                async with authoritative_ai_runtime_lock:
                    if authoritative_ai_runtime is None:
                        authoritative_ai_runtime = dict(await query.ai_runtime_authoritative())
            return dict(authoritative_ai_runtime)

        snapshot_plane = _dashboard_snapshot_plane(request)

        async def _load_snapshot_panel(panel_key: str) -> tuple[str, dict[str, Any], float] | None:
            if snapshot_plane is None or panel_key not in DASHBOARD_SNAPSHOT_PANEL_KEYS:
                return None
            if read_error is not None:
                return None
            read = await snapshot_plane.read_panel(panel_key)
            payload = read.data
            if panel_key == "strategyRuntime" and view == "strategy" and isinstance(payload, dict):
                payload = _strategy_view_strategy_runtime_payload(payload)
            return panel_key, {"data": payload, "error": read.error, "meta": read.meta}, read.duration_ms

        def _load_panel_sync(panel_key: str) -> tuple[str, dict[str, Any], float]:
            panel_started_at = perf_counter()
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
                        view=view,
                        panel_key=panel_key,
                        recent_decisions_limit=recent_decisions,
                        recent_orders_limit=recent_orders,
                        recent_fills_limit=recent_fills,
                        recent_replay_validations_limit=recent_replay_validations,
                        recent_ai_assessments_limit=recent_ai_assessments,
                        recent_ai_shadow_decisions_limit=recent_ai_shadow_decisions,
                        recent_ai_shadow_evaluations_limit=recent_ai_shadow_evaluations,
                    )
                    if panel_key == "strategyRuntime" and view == "strategy" and isinstance(payload, dict):
                        payload = _strategy_view_strategy_runtime_payload(payload)
                return panel_key, {"data": payload, "error": None}, round((perf_counter() - panel_started_at) * 1000.0, 3)
            except Exception as exc:
                return panel_key, {"data": None, "error": _dashboard_panel_error(exc)}, round((perf_counter() - panel_started_at) * 1000.0, 3)

        async def _load_panel(panel_key: str) -> tuple[str, dict[str, Any], float]:
            snapshot_result = await _load_snapshot_panel(panel_key)
            if snapshot_result is not None:
                return snapshot_result
            if panel_key not in {"aiRuntime", "aiOverview", "aiConfigModel"}:
                return await asyncio.to_thread(_load_panel_sync, panel_key)
            panel_started_at = perf_counter()
            try:
                if read_error is not None:
                    raise read_error
                runtime_payload = await _load_authoritative_ai_runtime()
                if panel_key == "aiRuntime":
                    payload = runtime_payload
                elif panel_key == "aiOverview":
                    payload = query.ai_overview_with_runtime(runtime_payload)
                else:
                    payload = query.ai_config_summary_with_runtime(runtime_payload)
                return panel_key, {"data": payload, "error": None}, round((perf_counter() - panel_started_at) * 1000.0, 3)
            except Exception as exc:
                return panel_key, {"data": None, "error": _dashboard_panel_error(exc)}, round((perf_counter() - panel_started_at) * 1000.0, 3)

        results = await asyncio.gather(
            *[_load_panel(key) for key in panel_keys]
        )
        panels: dict[str, dict[str, Any]] = {}
        panel_timings: dict[str, dict[str, float]] = {}
        for panel_key, panel_result, duration_ms in results:
            panels[panel_key] = panel_result
            panel_timings[panel_key] = {"duration_ms": duration_ms}

        payload_total_ms = round((perf_counter() - request_started_at) * 1000.0, 3)
        payload = {
            "view": view,
            "panels": panels,
            "auth": _dashboard_bundle_auth_summary(
                request,
                panel_keys=panel_keys,
                panels=panels,
                read_error=read_error,
            ),
            "timing": {
                "total_ms": payload_total_ms,
                "panels": panel_timings,
                "cache_hit": False,
                "cache_age_ms": 0.0,
                "deduped": False,
            },
        }
        if payload_total_ms >= _DASHBOARD_BUNDLE_SLOW_MS:
            log_event(
                _logger,
                "dashboard_bundle_slow",
                level="warning",
                view=view,
                total_ms=payload_total_ms,
                panel_keys=list(panel_keys),
                panel_timings_ms={
                    key: value["duration_ms"]
                    for key, value in panel_timings.items()
                },
            )
        # 只缓存成功响应（含各 panel 内部的 error 条目——panel-level error 是
        # OperatorQueryService 预期的 best-effort 输出，不是端点失败）。
        # 代际守卫：如果计算期间发生过 invalidate，不写缓存，防止旧数据污染。
        if _bundle_cache_generation == compute_generation:
            _bundle_cache[cache_key] = (loop.time(), payload)
        if not future.done():
            future.set_result(payload)
        return payload
    except Exception as exc:
        if not future.done():
            future.set_exception(exc)
        raise
    finally:
        # Inflight 登记必须在任何路径下都清理掉，否则下次同 key 会永远等一个
        # 不会有结果的 future。只清理自己的 entry——invalidate 后新请求可能
        # 已经用新代际覆盖了同 key 的 inflight，不能误删新 entry。
        stored = _bundle_cache_inflight.get(cache_key)
        if stored is not None and stored[1] is future:
            _bundle_cache_inflight.pop(cache_key, None)


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
    # 2026-04-20 code review C1 fix:
    #   UI /ai/operating-mode/select 允许 admin 对 ai_operating_mode 做临时 override
    #   (有 freeze_seconds + audit trail 约束), 但 governance 3 份 doc 默认假设
    #   "UI 不可切 mode". 为闭环此 doc-code 失步, 加一层 env-gated guard:
    #   AATS_ALLOW_UI_OPERATING_MODE_OVERRIDE=true 才放行, 否则 403 + hint 指向
    #   runtime_trading_mode_semantics.md §3.6.
    #
    # 设计意图:
    #   - 默认 false: 2026-04-27 alpha_evidence_gate 观察窗结束前 + 任何一条 Go
    #     决策之前, UI override 路径直接禁止, 只能走 §3.5 持久化流程
    #   - 运维场景需用 UI override 时, 显式在 .env.*.live 设 AATS_ALLOW_UI_
    #     OPERATING_MODE_OVERRIDE=true + deploy (同样留 audit trail)
    if not ui_operating_mode_override_enabled():
        raise HTTPException(
            status_code=403,
            detail=(
                "UI operating mode override is disabled by governance policy. "
                "Set AATS_ALLOW_UI_OPERATING_MODE_OVERRIDE=true in .env.*.live + "
                "deploy if this is a legitimate emergency override. See "
                "docs/governance/runtime_trading_mode_semantics.md §3.6 for "
                "the policy and §3.5 for the recommended persistent switch flow."
            ),
        )
    try:
        return await _query(request).set_ai_operating_mode(
            mode=payload.mode,
            reason=payload.reason,
            actor_role=principal.role,
            actor_identity=principal.identity,
            auth_source=principal.auth_source,
        )
    except OperatorCommandTimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except (ValueError, OperatorCommandRemoteError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OperatorCommandError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


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
