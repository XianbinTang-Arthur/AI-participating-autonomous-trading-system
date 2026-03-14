from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from aats.api.routes import router
from aats.bootstrap.config import build_runtime, load_settings
from aats.bootstrap.logging import configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    configure_logging(settings.log_level)
    runtime = await build_runtime(settings)
    await runtime.start_background_tasks()
    app.state.runtime = runtime
    try:
        yield
    finally:
        await runtime.stop_background_tasks()


app = FastAPI(title="AATS API Gateway", lifespan=lifespan)
app.include_router(router)
