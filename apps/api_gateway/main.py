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
    app.state.runtime = await build_runtime(settings)
    yield


app = FastAPI(title="AATS API Gateway", lifespan=lifespan)
app.include_router(router)

