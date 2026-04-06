"""Minimal mock backend for debugging the dashboard UI in a browser.

Serves the static files under aats/api/static and returns empty-but-valid
JSON for every API endpoint the dashboard may call. This is ONLY for
manual UI debugging; do NOT use in production.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = REPO_ROOT / "aats" / "api" / "static"

app = FastAPI(title="Dashboard UI Mock")

# Fake session claiming admin so action buttons are enabled.
FAKE_SESSION = {
    "authenticated": True,
    "identity": "debug_admin",
    "role": "admin",
}
FAKE_AUTH_PROVIDERS = {
    "auth_enabled": True,
}
FAKE_RUNTIME = {
    "operator_auth": {"unsafe_write_without_auth": False},
}
FAKE_HEALTH = {"runtime_state": "running", "overall_status": "running"}
FAKE_RECOVERY = {
    "halted": False,
    "resume_eligible": False,
    "safe_to_trade": True,
    "review_required": False,
}

CORE_PANELS = {
    "session": FAKE_SESSION,
    "authProviders": FAKE_AUTH_PROVIDERS,
    "health": FAKE_HEALTH,
    "mode": {"mode": "auto"},
    "runtime": FAKE_RUNTIME,
    "systemRecovery": {"recovery": FAKE_RECOVERY},
    "blockerControl": {"blockers": [], "primary_blocker": None},
}


@app.get("/auth/session")
async def auth_session():
    return FAKE_SESSION


@app.get("/auth/providers")
async def auth_providers():
    return FAKE_AUTH_PROVIDERS


@app.post("/auth/logout")
async def auth_logout():
    return {"ok": True}


@app.get("/dashboard/bundle")
async def dashboard_bundle(request: Request):
    view = request.query_params.get("view", "home")
    requested = request.query_params.getlist("panel")
    panels = {}
    # Always expose session/auth/health/mode/runtime/systemRecovery/blockerControl
    # so the shell renders with "running" state. For any other panel the client
    # asked for, return empty {data:{}, error:null} so panel renderers don't
    # explode with "loading" spinners forever.
    for key, data in CORE_PANELS.items():
        panels[key] = {"data": data, "error": None}
    for key in requested:
        if key not in panels:
            panels[key] = {"data": {}, "error": None}
    print(f">>> /dashboard/bundle view={view} panels={requested}")
    return {"panels": panels}


# Fallback for any panel path: empty data.
@app.api_route("/system/{rest:path}", methods=["GET", "POST"])
async def system_fallback(rest: str, request: Request):
    print(f">>> system/{rest} method={request.method}")
    if request.method == "POST":
        return {"ok": True, "message": f"mock {rest} ok"}
    return {}


@app.api_route("/reconciliation/{rest:path}", methods=["GET", "POST"])
async def reconciliation_fallback(rest: str, request: Request):
    print(f">>> reconciliation/{rest} method={request.method}")
    if request.method == "POST":
        return {"ok": True, "message": "mock reconciliation ok"}
    return {}


@app.api_route("/strategy-profiles/{rest:path}", methods=["GET", "POST"])
async def strategy_profiles_fallback(rest: str, request: Request):
    print(f">>> strategy-profiles/{rest} method={request.method}")
    if request.method == "POST":
        return {"ok": True, "message": "mock strategy ok", "active_revision": {"profile_id": "mock"}}
    return {}


@app.api_route("/ai/{rest:path}", methods=["GET", "POST"])
async def ai_fallback(rest: str, request: Request):
    print(f">>> ai/{rest} method={request.method}")
    if request.method == "POST":
        return {"ok": True, "message": "mock ai ok", "ai_runtime": {"effective_operating_mode": "full_ai"}}
    return {}


# IMPORTANT: route declaration order matters in FastAPI. Static asset paths
# must be declared BEFORE the catch-all /ui/{view} route, otherwise the
# catch-all swallows /ui/app.js and serves dashboard-shell.html for it.
@app.get("/ui/app.js")
async def ui_app_js():
    return Response((STATIC_DIR / "app.js").read_text(encoding="utf-8"),
                    media_type="application/javascript")


@app.get("/ui/app.css")
async def ui_app_css():
    return Response((STATIC_DIR / "app.css").read_text(encoding="utf-8"),
                    media_type="text/css")


@app.get("/ui/modules/{path:path}")
async def ui_modules(path: str):
    file = STATIC_DIR / "modules" / path
    if not file.exists():
        return Response(status_code=404)
    return Response(file.read_text(encoding="utf-8"),
                    media_type="application/javascript")


@app.get("/ui")
async def ui_root():
    html = (STATIC_DIR / "dashboard-shell.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/ui/{view}")
async def ui_view(view: str):
    html = (STATIC_DIR / "dashboard-shell.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=18765, log_level="info")
