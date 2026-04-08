from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from aats.api.auth_routes import auth_router, invalidate_bundle_cache
from aats.api.rdp_routes import rdp_router
from aats.api.routes import router
from aats.api.ui import ui_router
from aats.bootstrap.config import build_runtime, load_settings
from aats.bootstrap.logging import configure_logging_for_settings
from aats.bootstrap.settings import (
    ALLOWED_PROCESS_ROLES,
    PROCESS_ROLE_GATEWAY,
    PROCESS_ROLE_MONOLITH,
)
from aats.bootstrap.telemetry import start_span

# 任何 mutation 请求（POST/PATCH/PUT/DELETE）成功后都要把 dashboard bundle
# 缓存清空一次。否则用户切 mode / 触发 halt / 激活 profile 后，紧接着的
# refreshDashboard 仍可能在 2 秒 TTL 窗口里命中上一个快照，让 UI 看不到
# 刚刚发生的状态变化。只在 2xx/3xx 响应上清缓存——失败请求不应该污染缓存
# （也不产生状态变化，所以原样保留缓存即可）。
_MUTATING_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})


def _resolved_process_role() -> str:
    """Stage 5d：FastAPI gateway 进程默认 role=gateway，但允许 monolith 兼容旧路径。

    通过 AATS_PROCESS_ROLE=monolith 让 api_gateway 同时承担 4 个 slice（开发机
    与单机部署的零依赖路径）。生产 4 进程拓扑下应当置 AATS_PROCESS_ROLE=gateway。
    """
    raw = os.environ.get("AATS_PROCESS_ROLE", PROCESS_ROLE_GATEWAY).strip().lower()
    if raw not in ALLOWED_PROCESS_ROLES:
        raw = PROCESS_ROLE_GATEWAY
    # gateway / monolith 之外的 role 不应该跑 FastAPI gateway
    if raw not in {PROCESS_ROLE_GATEWAY, PROCESS_ROLE_MONOLITH}:
        raw = PROCESS_ROLE_GATEWAY
    return raw


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    configure_logging_for_settings(settings)
    runtime = await build_runtime(settings, process_role=_resolved_process_role())
    await runtime.start_background_tasks()
    app.state.runtime = runtime
    try:
        yield
    finally:
        await runtime.stop_background_tasks()


app = FastAPI(title="AATS API Gateway", lifespan=lifespan)


@app.middleware("http")
async def _invalidate_bundle_cache_on_mutation(request: Request, call_next):
    response = await call_next(request)
    if request.method in _MUTATING_METHODS and 200 <= response.status_code < 400:
        invalidate_bundle_cache()
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


app.include_router(auth_router)
app.include_router(ui_router)
app.include_router(router)
app.include_router(rdp_router)
