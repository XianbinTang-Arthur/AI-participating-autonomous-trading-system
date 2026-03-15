from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse


UI_DIR = Path(__file__).resolve().parent / "static"

ui_router = APIRouter(include_in_schema=False)


@ui_router.get("/")
@ui_router.get("/ui")
async def dashboard_index() -> FileResponse:
    return FileResponse(UI_DIR / "dashboard.html")


@ui_router.get("/ui/app.css")
async def dashboard_css() -> FileResponse:
    return FileResponse(UI_DIR / "app.css", media_type="text/css")


@ui_router.get("/ui/app.js")
async def dashboard_js() -> FileResponse:
    return FileResponse(UI_DIR / "app.js", media_type="application/javascript")
