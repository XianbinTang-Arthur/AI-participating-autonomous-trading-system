from __future__ import annotations

from fastapi import Header, HTTPException, Request
from pydantic import BaseModel

from aats.bootstrap.config import ApplicationRuntime
from aats.schemas.operator import OperatorRole


class OperatorPrincipal(BaseModel):
    role: OperatorRole
    auth_enabled: bool


def _runtime(request: Request) -> ApplicationRuntime:
    return request.app.state.runtime


def require_read_access(
    request: Request,
    x_aats_api_key: str | None = Header(default=None, alias="X-AATS-API-Key"),
) -> OperatorPrincipal:
    settings = _runtime(request).settings
    if not settings.operator_auth_enabled:
        return OperatorPrincipal(role="anonymous", auth_enabled=False)
    if not settings.operator_read_api_key and not settings.operator_write_api_key:
        raise HTTPException(status_code=503, detail="operator_auth_misconfigured")
    if settings.operator_write_api_key and x_aats_api_key == settings.operator_write_api_key:
        return OperatorPrincipal(role="write", auth_enabled=True)
    if settings.operator_read_api_key and x_aats_api_key == settings.operator_read_api_key:
        return OperatorPrincipal(role="read", auth_enabled=True)
    raise HTTPException(status_code=401, detail="operator_auth_required")


def require_write_access(
    request: Request,
    x_aats_api_key: str | None = Header(default=None, alias="X-AATS-API-Key"),
) -> OperatorPrincipal:
    principal = require_read_access(request, x_aats_api_key)
    if not principal.auth_enabled:
        return principal
    if principal.role != "write":
        raise HTTPException(status_code=403, detail="operator_write_access_required")
    return principal
