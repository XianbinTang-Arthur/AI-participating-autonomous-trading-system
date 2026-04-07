from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from aats.api.auth_routes import auth_router, invalidate_bundle_cache
from aats.api.rdp_routes import rdp_router
from aats.api.routes import router
from aats.api.ui import ui_router
from aats.bootstrap.config import build_runtime, load_settings
from aats.bootstrap.logging import configure_logging_for_settings

# 任何 mutation 请求（POST/PATCH/PUT/DELETE）成功后都要把 dashboard bundle
# 缓存清空一次。否则用户切 mode / 触发 halt / 激活 profile 后，紧接着的
# refreshDashboard 仍可能在 2 秒 TTL 窗口里命中上一个快照，让 UI 看不到
# 刚刚发生的状态变化。只在 2xx/3xx 响应上清缓存——失败请求不应该污染缓存
# （也不产生状态变化，所以原样保留缓存即可）。
_MUTATING_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    configure_logging_for_settings(settings)
    runtime = await build_runtime(settings)
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


app.include_router(auth_router)
app.include_router(ui_router)
app.include_router(router)
app.include_router(rdp_router)
