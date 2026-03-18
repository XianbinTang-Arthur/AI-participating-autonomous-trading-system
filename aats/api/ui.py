from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

from aats.api.auth import session_principal


UI_DIR = Path(__file__).resolve().parent / "static"
MODULES_DIR = UI_DIR / "modules"
PAGE_FILES = {
    "home": UI_DIR / "home.html",
    "overview": UI_DIR / "dashboard.html",
    "strategy": UI_DIR / "strategy.html",
    "execution": UI_DIR / "execution.html",
    "risk": UI_DIR / "risk.html",
    "ai": UI_DIR / "ai.html",
    "settings": UI_DIR / "settings.html",
}

ui_router = APIRouter(include_in_schema=False)

NO_STORE_HEADERS = {"Cache-Control": "no-store"}


def _auth_enabled(request: Request) -> bool:
    runtime = getattr(request.app.state, "runtime", None)
    return bool(runtime is not None and runtime.settings.operator_auth_enabled)


def _dashboard_allowed(request: Request) -> bool:
    if not _auth_enabled(request):
        return True
    return session_principal(request) is not None


def _serve_dashboard_page(request: Request, page_name: str) -> FileResponse | RedirectResponse:
    if not _dashboard_allowed(request):
        return RedirectResponse(url="/login", status_code=303)
    page = PAGE_FILES.get(page_name)
    if page is None:
        raise HTTPException(status_code=404, detail="ui_page_not_found")
    return FileResponse(page, media_type="text/html; charset=utf-8", headers=NO_STORE_HEADERS)


@ui_router.get("/")
@ui_router.get("/ui")
async def dashboard_index(request: Request):
    return _serve_dashboard_page(request, "home")


@ui_router.get("/ui/home")
async def home_index(request: Request):
    return _serve_dashboard_page(request, "home")


@ui_router.get("/ui/overview")
async def overview_index(request: Request):
    return _serve_dashboard_page(request, "overview")


@ui_router.get("/ui/strategy")
async def strategy_index(request: Request):
    return _serve_dashboard_page(request, "strategy")


@ui_router.get("/ui/execution")
async def execution_index(request: Request):
    return _serve_dashboard_page(request, "execution")


@ui_router.get("/ui/risk")
async def risk_index(request: Request):
    return _serve_dashboard_page(request, "risk")


@ui_router.get("/ui/ai")
async def ai_index(request: Request):
    return _serve_dashboard_page(request, "ai")


@ui_router.get("/ui/settings")
async def settings_index(request: Request):
    return _serve_dashboard_page(request, "settings")


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


@ui_router.get("/ui/modules/{module_path:path}")
async def dashboard_module(module_path: str) -> FileResponse:
    resolved = (MODULES_DIR / module_path).resolve()
    if MODULES_DIR.resolve() not in resolved.parents or not resolved.is_file():
        raise HTTPException(status_code=404, detail="ui_module_not_found")
    return FileResponse(resolved, media_type="application/javascript; charset=utf-8", headers=NO_STORE_HEADERS)
