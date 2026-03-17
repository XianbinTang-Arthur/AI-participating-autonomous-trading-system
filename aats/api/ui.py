from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, RedirectResponse

from aats.api.auth import session_principal


UI_DIR = Path(__file__).resolve().parent / "static"

ui_router = APIRouter(include_in_schema=False)

NO_STORE_HEADERS = {"Cache-Control": "no-store"}


def _auth_enabled(request: Request) -> bool:
    runtime = getattr(request.app.state, "runtime", None)
    return bool(runtime is not None and runtime.settings.operator_auth_enabled)


def _dashboard_allowed(request: Request) -> bool:
    if not _auth_enabled(request):
        return True
    return session_principal(request) is not None


@ui_router.get("/")
@ui_router.get("/ui")
async def dashboard_index(request: Request):
    if not _dashboard_allowed(request):
        return RedirectResponse(url="/login", status_code=303)
    return FileResponse(UI_DIR / "dashboard.html", media_type="text/html; charset=utf-8", headers=NO_STORE_HEADERS)


@ui_router.get("/login")
async def login_index(request: Request):
    if _dashboard_allowed(request):
        return RedirectResponse(url="/ui", status_code=303)
    return FileResponse(UI_DIR / "login.html", media_type="text/html; charset=utf-8", headers=NO_STORE_HEADERS)


@ui_router.get("/ui/app.css")
async def dashboard_css() -> FileResponse:
    return FileResponse(UI_DIR / "app.css", media_type="text/css; charset=utf-8", headers=NO_STORE_HEADERS)


@ui_router.get("/ui/app.js")
async def dashboard_js() -> FileResponse:
    return FileResponse(UI_DIR / "app.js", media_type="application/javascript; charset=utf-8", headers=NO_STORE_HEADERS)


@ui_router.get("/ui/login.js")
async def login_js() -> FileResponse:
    return FileResponse(UI_DIR / "login.js", media_type="application/javascript; charset=utf-8", headers=NO_STORE_HEADERS)
