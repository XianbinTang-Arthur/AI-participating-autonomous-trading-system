from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from aats.api.auth import (
    OperatorPrincipal,
    authenticate_operator_user,
    configured_operator_users,
    require_read_access,
    session_principal,
)
from aats.api.session_auth import issue_session_token
from aats.bootstrap.config import ApplicationRuntime


auth_router = APIRouter(include_in_schema=False)


class LoginRequest(BaseModel):
    username: str
    password: str


def _runtime(request: Request) -> ApplicationRuntime:
    return request.app.state.runtime


def _session_payload(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    settings = runtime.settings
    principal = session_principal(request)
    return {
        "auth_enabled": settings.operator_auth_enabled,
        "session_enabled": settings.operator_session_configured,
        "api_key_compatibility_enabled": bool(settings.operator_read_api_key or settings.operator_write_api_key),
        "authenticated": principal is not None,
        "identity": principal.identity if principal is not None else None,
        "role": principal.role if principal is not None else "anonymous",
        "auth_source": principal.auth_source if principal is not None else "anonymous",
    }


@auth_router.get("/auth/session")
async def auth_session(request: Request) -> dict[str, Any]:
    return _session_payload(request)


@auth_router.post("/auth/login")
async def auth_login(request: Request, payload: LoginRequest, response: Response) -> dict[str, Any]:
    runtime = _runtime(request)
    settings = runtime.settings
    if not settings.operator_auth_enabled:
        raise HTTPException(status_code=400, detail="operator_auth_disabled")
    if not settings.operator_session_configured:
        raise HTTPException(status_code=503, detail="operator_session_auth_not_configured")
    principal = authenticate_operator_user(settings, username=payload.username, password=payload.password)
    if principal is None:
        raise HTTPException(status_code=401, detail="operator_login_failed")
    token = issue_session_token(settings=settings, identity=principal.identity or payload.username, role=principal.role)
    response.set_cookie(
        key=settings.operator_session_cookie_name,
        value=token,
        max_age=settings.operator_session_max_age_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.operator_session_cookie_secure,
        path="/",
    )
    return {
        "authenticated": True,
        "identity": principal.identity,
        "role": principal.role,
        "auth_source": "session",
    }


@auth_router.post("/auth/logout")
async def auth_logout(request: Request, response: Response) -> dict[str, Any]:
    settings = _runtime(request).settings
    response.delete_cookie(settings.operator_session_cookie_name, path="/")
    return {"authenticated": False, "status": "logged_out"}


@auth_router.get("/auth/whoami")
async def auth_whoami(
    _: Request,
    principal: OperatorPrincipal = Depends(require_read_access),
) -> dict[str, Any]:
    return {
        "identity": principal.identity,
        "role": principal.role,
        "auth_source": principal.auth_source,
        "auth_enabled": principal.auth_enabled,
    }


@auth_router.get("/auth/providers")
async def auth_providers(request: Request) -> dict[str, Any]:
    settings = _runtime(request).settings
    return {
        "auth_enabled": settings.operator_auth_enabled,
        "session_enabled": settings.operator_session_configured,
        "configured_roles": [user.role for user in configured_operator_users(settings)],
        "api_key_compatibility_enabled": bool(settings.operator_read_api_key or settings.operator_write_api_key),
    }
