from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from time import monotonic

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from aats.api.auth_routes import (
    auth_router,
    invalidate_bundle_cache,
    start_dashboard_snapshot_plane,
    stop_dashboard_snapshot_plane,
)
from aats.api.rdp_profile_routes import profile_router as rdp_profile_router
from aats.api.rdp_routes import rdp_router
from aats.api.routes import router
from aats.api.ui import ui_router
from aats.bootstrap.config import build_runtime, load_settings
from aats.bootstrap.logging import get_logger as _get_lifecycle_logger
from aats.bootstrap.process_lifecycle import (
    _announce_runtime_ready,
    _wait_for_peer_roles_ready,
)
from aats.bootstrap.logging import configure_logging_for_settings
from aats.bootstrap.settings import (
    ALLOWED_PROCESS_ROLES,
    PROCESS_ROLE_GATEWAY,
    PROCESS_ROLE_MONOLITH,
)
from aats.bootstrap.telemetry import start_span
from aats.data_platform.governance._exceptions import (
    DBConstraintViolation,
    DBUnavailableError,
)

# 任何 mutation 请求（POST/PATCH/PUT/DELETE）成功后都要把 dashboard bundle
# 缓存清空一次。否则用户切 mode / 触发 halt / 激活 profile 后，紧接着的
# refreshDashboard 仍可能在 2 秒 TTL 窗口里命中上一个快照，让 UI 看不到
# 刚刚发生的状态变化。只在 2xx/3xx 响应上清缓存——失败请求不应该污染缓存
# （也不产生状态变化，所以原样保留缓存即可）。
_MUTATING_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})
_AUTH_SNAPSHOT_REFRESH_EXEMPT_PATH_PREFIXES = ("/auth/",)
_DASHBOARD_SNAPSHOT_MUTATION_REFRESH_COOLDOWN_SECONDS = 5.0
_DASHBOARD_SNAPSHOT_REFRESH_LOCK_ATTR = "_dashboard_snapshot_mutation_refresh_lock"
_DASHBOARD_SNAPSHOT_REFRESH_LAST_ATTR = "_dashboard_snapshot_mutation_refresh_last_at"
_DASHBOARD_SNAPSHOT_MUTATION_EAGER_PANEL_PREFIXES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("/ai/operating-mode/", ("aiConfigModel",)),
    ("/rdp/", ("rdpControl", "rdpWorkbenchOverview")),
    ("/strategy-profiles/", ("profileControlSummary",)),
)


_FASTAPI_ROLES: frozenset[str] = frozenset({PROCESS_ROLE_GATEWAY, PROCESS_ROLE_MONOLITH})


def _resolved_process_role() -> str:
    """Stage 5d：FastAPI gateway 进程默认 role=gateway，但允许 monolith 兼容旧路径。

    通过 AATS_PROCESS_ROLE=monolith 让 api_gateway 同时承担 4 个 slice（开发机
    与单机部署的零依赖路径）。生产 4 进程拓扑下应当置 AATS_PROCESS_ROLE=gateway。

    Fail-fast 校验（LF-017）：未设置环境变量 → 默认 gateway；如果**设置了**值
    但不在合法集合里（如 "gateways" 之类的 typo）就抛 ValueError，避免
    运维静默降级成 gateway 导致的排查困难。此外，合法但不是 gateway/monolith
    的 role（market/decision/execution）明显走错了二进制入口，也抛。
    """
    raw_env = os.environ.get("AATS_PROCESS_ROLE")
    if raw_env is None:
        return PROCESS_ROLE_GATEWAY
    raw = raw_env.strip().lower()
    if not raw:
        return PROCESS_ROLE_GATEWAY
    if raw not in ALLOWED_PROCESS_ROLES:
        raise ValueError(
            f"AATS_PROCESS_ROLE={raw_env!r} is not in allowed set "
            f"{sorted(ALLOWED_PROCESS_ROLES)}. Common typo: did you mean "
            f"'{PROCESS_ROLE_GATEWAY}'?"
        )
    if raw not in _FASTAPI_ROLES:
        raise ValueError(
            f"AATS_PROCESS_ROLE={raw_env!r} is valid but not eligible for the "
            f"api_gateway FastAPI process. Eligible roles: {sorted(_FASTAPI_ROLES)}. "
            f"For role={raw!r}, use apps/{raw}_* entrypoint instead."
        )
    return raw


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    configure_logging_for_settings(settings)
    resolved_role = _resolved_process_role()
    runtime = await build_runtime(settings, process_role=resolved_role)
    # Readiness barrier (B1) — gateway 用 FastAPI lifespan 而不是 process_lifecycle.run_process，
    # 所以要在这里手工挂钩。详见
    # docs/task/nats_retention_global_architecture_sow.md §B1。
    _lifespan_logger = _get_lifecycle_logger("apps.api_gateway.lifespan")
    _hot_state = getattr(runtime, "hot_state_store", None)
    await _announce_runtime_ready(
        role=resolved_role,
        hot_state_store=_hot_state,
        logger=_lifespan_logger,
    )
    await _wait_for_peer_roles_ready(
        role=resolved_role,
        hot_state_store=_hot_state,
        logger=_lifespan_logger,
    )
    await runtime.start_background_tasks()
    app.state.runtime = runtime
    await start_dashboard_snapshot_plane(app, runtime)
    try:
        # RDP schema 初始化：确保 governance.rdp_task_queue 等 47 张 RDP 表存在。
        # 放在 try 内部：即使建表失败也不阻断启动、不泄漏后台任务。
        # 复用 data_platform.db.run_migrations()，不另建 engine。
        try:
            from aats.data_platform.db import run_migrations
            run_migrations()
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "rdp_schema_ensure_failed: RDP tables may not exist, "
                "AI Config page will be unavailable until manually initialized"
            )
        yield
    finally:
        await stop_dashboard_snapshot_plane(app)
        await runtime.stop_background_tasks()


app = FastAPI(title="AATS API Gateway", lifespan=lifespan)


def _is_successful_mutation(method: str, status_code: int) -> bool:
    return method.upper() in _MUTATING_METHODS and 200 <= int(status_code) < 400


def _dashboard_snapshot_refresh_exempt_path(path: str) -> bool:
    normalized_path = "/" + str(path or "").lstrip("/")
    return any(normalized_path.startswith(prefix) for prefix in _AUTH_SNAPSHOT_REFRESH_EXEMPT_PATH_PREFIXES)


def _should_refresh_dashboard_snapshots_after_mutation(method: str, path: str, status_code: int) -> bool:
    if not _is_successful_mutation(method, status_code):
        return False
    return not _dashboard_snapshot_refresh_exempt_path(path)


def _eager_dashboard_snapshot_panels_for_mutation(path: str) -> tuple[str, ...]:
    normalized_path = "/" + str(path or "").lstrip("/")
    panels: list[str] = []
    for prefix, panel_keys in _DASHBOARD_SNAPSHOT_MUTATION_EAGER_PANEL_PREFIXES:
        if normalized_path.startswith(prefix):
            panels.extend(panel_keys)
    return tuple(dict.fromkeys(panels))


def _dashboard_snapshot_refresh_lock(app_state: object) -> asyncio.Lock:
    lock = getattr(app_state, _DASHBOARD_SNAPSHOT_REFRESH_LOCK_ATTR, None)
    if isinstance(lock, asyncio.Lock):
        return lock
    lock = asyncio.Lock()
    setattr(app_state, _DASHBOARD_SNAPSHOT_REFRESH_LOCK_ATTR, lock)
    return lock


async def _refresh_dashboard_snapshots_after_mutation(request: Request, *, reason: str) -> bool:
    plane = getattr(request.app.state, "dashboard_snapshot_plane", None)
    invalidate = getattr(plane, "invalidate_all", None)
    enqueue_scheduled = getattr(plane, "enqueue_scheduled", None)
    if not callable(invalidate) or not callable(enqueue_scheduled):
        return False

    await invalidate(reason=reason)

    lock = _dashboard_snapshot_refresh_lock(request.app.state)
    now = monotonic()
    async with lock:
        last_refresh_at = getattr(request.app.state, _DASHBOARD_SNAPSHOT_REFRESH_LAST_ATTR, None)
        if (
            isinstance(last_refresh_at, float)
            and now - last_refresh_at < _DASHBOARD_SNAPSHOT_MUTATION_REFRESH_COOLDOWN_SECONDS
        ):
            return False
        setattr(request.app.state, _DASHBOARD_SNAPSHOT_REFRESH_LAST_ATTR, now)

    await enqueue_scheduled(reason=reason)
    enqueue_panels = getattr(plane, "enqueue_panels", None)
    if callable(enqueue_panels):
        await enqueue_panels(
            _eager_dashboard_snapshot_panels_for_mutation(request.url.path),
            reason=reason,
        )
    return True


@app.middleware("http")
async def _invalidate_bundle_cache_on_mutation(request: Request, call_next):
    response = await call_next(request)
    if _is_successful_mutation(request.method, response.status_code):
        invalidate_bundle_cache()
        if _should_refresh_dashboard_snapshots_after_mutation(
            request.method,
            request.url.path,
            response.status_code,
        ):
            await _refresh_dashboard_snapshots_after_mutation(
                request,
                reason=f"{request.method.lower()}_mutation",
            )
    return response


# Stage 8：把每一条 HTTP 请求作为 trace 的 root span。所有在 handler 内部
# 发起的 NatsEventBus.publish 都会自动挂在这条 span 下面（通过 OTel 的
# current context）。span name 用 "gateway.http.<METHOD> <url_path>"，
# Jaeger UI 默认按 service + span name 聚合统计 P50/P99。
# 不用 opentelemetry-instrumentation-fastapi 是为了：
#   1) 避免再引一个 optional extra
#   2) 保持 span 命名与 docs/task/stage_8_otel_integration_design.md §D5
#      的 "process_role.module.action" 规范完全对齐
#   3) /healthz / /metrics / /favicon.ico 这类 noise 路径可以在这里显式过滤
# 设计文档：docs/task/stage_8_otel_integration_design.md §D5
_TELEMETRY_IGNORED_PATHS = frozenset({
    "/healthz",
    "/metrics",
    "/favicon.ico",
})


@app.middleware("http")
async def _gateway_trace_root_span(request: Request, call_next):
    path = request.url.path
    if path in _TELEMETRY_IGNORED_PATHS:
        return await call_next(request)
    with start_span(
        f"gateway.http.{request.method} {path}",
        attributes={
            "http.method": request.method,
            "http.route": path,
            "http.scheme": request.url.scheme,
            "http.host": request.url.hostname or "",
            "aats.process_role": _resolved_process_role(),
        },
    ) as span:
        response = await call_next(request)
        try:
            span.set_attribute("http.status_code", response.status_code)
        except Exception:
            pass
        return response


# Stage 7 修复：lightweight liveness probe 给 docker compose healthcheck 专用。
# 与 /system/health 的区别：
#   * /system/health 是 operator/UI 用的诊断 endpoint，需要全量 portfolio /
#     reconciliation / market 状态，依赖 runtime 上多个 slice service。
#     在 gateway-only role 下 runtime.market_gateway / runtime.execution_adapter
#     都是 None（被 _SLICE_REQUIRED_ROLES 门控），调用会 NPE。
#   * /healthz 只需要返回"FastAPI lifespan 已就绪"这一个事实，不依赖任何 slice
#     service，所以在任何 process_role 下都能 200。
# 直接挂在 `app` 上而不是挂到 routes.py 的 router 上，是为了绕过 router 级
# require_read_access dependency —— docker healthcheck curl 不带 Bearer token。
@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "process_role": _resolved_process_role()}


# A-0.3：governance DB 写路径的统一错误→HTTP 映射。
# DBUnavailableError 是基础设施级不可达（e.g. 数据库宕机 / 连接耗尽），
# HTTP 契约上等价于 503 Service Unavailable，客户端应当重试；
# DBConstraintViolation 代表 FK / UQ / CHECK 被触发，通常是上游脏数据或业务
# 规则违反，映射成 422 Unprocessable Entity 让 operator / UI 看到原因。
# 这两条 handler 把写路径的异常收口到明确的 HTTP 语义，避免 FastAPI 默认
# 500 Internal Server Error 把基础设施故障与业务逻辑 bug 混成一锅。
@app.exception_handler(DBUnavailableError)
async def _handle_db_unavailable(_request: Request, exc: DBUnavailableError) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "ok": False,
            "error": "db_unavailable",
            "message": str(exc),
        },
    )


@app.exception_handler(DBConstraintViolation)
async def _handle_db_constraint_violation(
    _request: Request, exc: DBConstraintViolation,
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "ok": False,
            "error": "db_constraint_violation",
            "message": str(exc),
        },
    )


app.include_router(auth_router)
app.include_router(ui_router)
app.include_router(router)
app.include_router(rdp_router)
app.include_router(rdp_profile_router)
