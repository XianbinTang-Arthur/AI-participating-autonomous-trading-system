from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from aats.api.auth_routes import auth_router
from aats.api.rdp_routes import rdp_router
from aats.api.routes import router
from aats.api.ui import ui_router
from aats.bootstrap.config import build_runtime, load_settings
from aats.bootstrap.logging import configure_logging_for_settings


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
app.include_router(auth_router)
app.include_router(ui_router)
app.include_router(router)
app.include_router(rdp_router)
