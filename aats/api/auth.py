from __future__ import annotations

from dataclasses import dataclass

from fastapi import Cookie, Header, HTTPException, Request
from pydantic import BaseModel

from aats.api.session_auth import SessionIdentity, verify_session_token
from aats.bootstrap.config import ApplicationRuntime
from aats.bootstrap.settings import AATSSettings
from aats.schemas.operator import AuthSource, OperatorRole


class OperatorPrincipal(BaseModel):
    identity: str | None = None
    role: OperatorRole
    auth_enabled: bool
    auth_source: AuthSource = "anonymous"

    @property
    def can_write(self) -> bool:
        return self.role in {"operator", "admin"}


@dataclass(frozen=True, slots=True)
class ConfiguredOperatorUser:
    username: str
    password: str
    role: OperatorRole


def _runtime(request: Request) -> ApplicationRuntime:
    return request.app.state.runtime


def configured_operator_users(settings: AATSSettings) -> list[ConfiguredOperatorUser]:
    users: list[ConfiguredOperatorUser] = []
    if settings.operator_viewer_username and settings.operator_viewer_password:
        users.append(
            ConfiguredOperatorUser(
                username=settings.operator_viewer_username,
                password=settings.operator_viewer_password,
                role="viewer",
            )
        )
    if settings.operator_operator_username and settings.operator_operator_password:
        users.append(
            ConfiguredOperatorUser(
                username=settings.operator_operator_username,
                password=settings.operator_operator_password,
                role="operator",
            )
        )
    if settings.operator_admin_username and settings.operator_admin_password:
        users.append(
            ConfiguredOperatorUser(
                username=settings.operator_admin_username,
                password=settings.operator_admin_password,
                role="admin",
            )
        )
    return users


def authenticate_operator_user(settings: AATSSettings, *, username: str, password: str) -> OperatorPrincipal | None:
    for user in configured_operator_users(settings):
        if user.username == username and user.password == password:
            return OperatorPrincipal(
                identity=user.username,
                role=user.role,
                auth_enabled=True,
                auth_source="session",
            )
    return None


def session_principal(request: Request) -> OperatorPrincipal | None:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        return None
    settings = runtime.settings
    session_token = request.cookies.get(settings.operator_session_cookie_name)
    identity = verify_session_token(settings=settings, token=session_token)
    if identity is None:
        return None
    return OperatorPrincipal(
        identity=identity.identity,
        role=identity.role,
        auth_enabled=True,
        auth_source="session",
    )


def session_identity(request: Request) -> SessionIdentity | None:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        return None
    settings = runtime.settings
    return verify_session_token(
        settings=settings,
        token=request.cookies.get(settings.operator_session_cookie_name),
    )


def require_read_access(
    request: Request,
    x_aats_api_key: str | None = Header(default=None, alias="X-AATS-API-Key"),
) -> OperatorPrincipal:
    settings = _runtime(request).settings
    if not settings.operator_auth_enabled:
        return OperatorPrincipal(role="anonymous", auth_enabled=False, auth_source="anonymous")

    session_user = session_principal(request)
    if session_user is not None:
        return session_user

    if settings.operator_write_api_key and x_aats_api_key == settings.operator_write_api_key:
        return OperatorPrincipal(
            identity="api_key_write",
            role="admin",
            auth_enabled=True,
            auth_source="api_key",
        )
    if settings.operator_read_api_key and x_aats_api_key == settings.operator_read_api_key:
        return OperatorPrincipal(
            identity="api_key_read",
            role="viewer",
            auth_enabled=True,
            auth_source="api_key",
        )

    if not configured_operator_users(settings) and not settings.operator_read_api_key and not settings.operator_write_api_key:
        raise HTTPException(status_code=503, detail="operator_auth_misconfigured")
    raise HTTPException(status_code=401, detail="operator_auth_required")


def require_write_access(
    request: Request,
    x_aats_api_key: str | None = Header(default=None, alias="X-AATS-API-Key"),
) -> OperatorPrincipal:
    principal = require_read_access(request, x_aats_api_key)
    if not principal.auth_enabled:
        settings = _runtime(request).settings
        if settings.operator_unsafe_write_without_auth:
            return principal
        raise HTTPException(status_code=403, detail="operator_write_auth_required")
    if not principal.can_write:
        raise HTTPException(status_code=403, detail="operator_write_access_required")
    return principal
