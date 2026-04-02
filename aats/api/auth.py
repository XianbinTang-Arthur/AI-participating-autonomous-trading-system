from __future__ import annotations

from dataclasses import dataclass

from fastapi import Header, HTTPException, Request
from pydantic import BaseModel

from aats.api.session_auth import SessionIdentity, verify_session_token
from aats.bootstrap.config import ApplicationRuntime
from aats.schemas.common import utc_now
from aats.schemas.operator import AuthSource, OperatorRole
from aats.services.operator.passwords import verify_password


class OperatorPrincipal(BaseModel):
    identity: str | None = None
    role: OperatorRole
    auth_enabled: bool
    auth_source: AuthSource = "anonymous"

    @property
    def can_write(self) -> bool:
        return self.role in {"operator", "admin"}


@dataclass(frozen=True, slots=True)
class OperatorLoginResult:
    principal: OperatorPrincipal | None
    failure_code: str | None = None


def _runtime(request: Request) -> ApplicationRuntime:
    return request.app.state.runtime

def authenticate_operator_user(runtime: ApplicationRuntime, *, username: str, password: str) -> OperatorLoginResult:
    if not hasattr(runtime, "operator_repo"):
        return OperatorLoginResult(principal=None, failure_code="operator_login_failed")
    user = runtime.operator_repo.get_by_username(username)
    if user is None or not user.enabled:
        return OperatorLoginResult(principal=None, failure_code="operator_login_failed")
    now = utc_now()
    if user.locked_until is not None and user.locked_until > now:
        return OperatorLoginResult(principal=None, failure_code="operator_login_locked")
    if not verify_password(password, user.password_hash):
        record_failure = getattr(runtime.operator_repo, "record_login_failure", None)
        if callable(record_failure):
            record_failure(
                username,
                now,
                max_failed_attempts=runtime.settings.operator_login_max_failed_attempts,
                lockout_seconds=runtime.settings.operator_login_lockout_seconds,
            )
        return OperatorLoginResult(principal=None, failure_code="operator_login_failed")
    runtime.operator_repo.record_login(username, now)
    return OperatorLoginResult(
        principal=OperatorPrincipal(
            identity=user.username,
            role=user.role,
            auth_enabled=True,
            auth_source="session",
        )
    )


def configured_operator_roles(runtime: ApplicationRuntime) -> list[OperatorRole]:
    roles = [user.role for user in runtime.operator_repo.all_users() if user.enabled] if hasattr(runtime, "operator_repo") else []
    if roles:
        return sorted(set(roles), key=lambda role: {"viewer": 0, "operator": 1, "admin": 2}.get(role, 99))
    return []


def stored_operator_user_count(runtime: ApplicationRuntime) -> int:
    if not hasattr(runtime, "operator_repo"):
        return 0
    return runtime.operator_repo.count()


def session_principal(request: Request) -> OperatorPrincipal | None:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        return None
    settings = runtime.settings
    identity = _validated_session_identity(
        runtime,
        request.cookies.get(settings.operator_session_cookie_name),
    )
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
    return _validated_session_identity(
        runtime,
        request.cookies.get(runtime.settings.operator_session_cookie_name),
    )


def _validated_session_identity(runtime: ApplicationRuntime, token: str | None) -> SessionIdentity | None:
    identity = verify_session_token(settings=runtime.settings, token=token)
    if identity is None:
        return None
    user = runtime.operator_repo.get_by_username(identity.identity) if hasattr(runtime, "operator_repo") else None
    if user is None or not user.enabled:
        return None
    if identity.session_version != getattr(user, "session_version", 1):
        return None
    if user.role == identity.role and user.username == identity.identity:
        return identity
    return SessionIdentity(
        identity=user.username,
        role=user.role,
        issued_at=identity.issued_at,
        expires_at=identity.expires_at,
        session_version=getattr(user, "session_version", 1),
    )


def require_read_access(
    request: Request,
    x_aats_api_key: str | None = Header(default=None, alias="X-AATS-API-Key"),
) -> OperatorPrincipal:
    runtime = _runtime(request)
    settings = runtime.settings
    if not settings.operator_auth_enabled:
        return OperatorPrincipal(role="anonymous", auth_enabled=False, auth_source="anonymous")

    session_user = session_principal(request)
    if session_user is not None:
        return session_user

    if _write_api_key_compatibility_enabled(runtime) and settings.operator_write_api_key and x_aats_api_key == settings.operator_write_api_key:
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

    enabled_user_count = runtime.operator_repo.count(enabled_only=True) if hasattr(runtime, "operator_repo") else 0
    if enabled_user_count == 0 and not settings.operator_read_api_key and not settings.operator_write_api_key:
        raise HTTPException(status_code=503, detail="operator_auth_misconfigured")
    raise HTTPException(status_code=401, detail="operator_auth_required")


def require_write_access(
    request: Request,
    x_aats_api_key: str | None = Header(default=None, alias="X-AATS-API-Key"),
) -> OperatorPrincipal:
    principal = require_read_access(request, x_aats_api_key)
    if not principal.auth_enabled:
        runtime = _runtime(request)
        settings = runtime.settings
        if settings.operator_control_plane_execution_ledger_enabled:
            raise HTTPException(status_code=403, detail="operator_write_auth_required")
        if settings.operator_unsafe_write_without_auth and (
            runtime.environment_capabilities.local_only or settings.storage_mode == "memory"
        ):
            return principal
        raise HTTPException(status_code=403, detail="operator_write_auth_required")
    if not principal.can_write:
        raise HTTPException(status_code=403, detail="operator_write_access_required")
    return principal


def require_admin_access(
    request: Request,
    x_aats_api_key: str | None = Header(default=None, alias="X-AATS-API-Key"),
) -> OperatorPrincipal:
    principal = require_write_access(request, x_aats_api_key)
    runtime = _runtime(request)
    settings = runtime.settings
    if settings.operator_control_plane_execution_ledger_enabled and not principal.auth_enabled:
        raise HTTPException(status_code=403, detail="operator_admin_access_required")
    if not principal.auth_enabled and settings.operator_unsafe_write_without_auth and (
        runtime.environment_capabilities.local_only or settings.storage_mode == "memory"
    ):
        return principal
    if principal.role != "admin":
        raise HTTPException(status_code=403, detail="operator_admin_access_required")
    return principal


def _write_api_key_compatibility_enabled(runtime: ApplicationRuntime) -> bool:
    if not runtime.settings.operator_write_api_key:
        return False
    return bool(
        runtime.environment_capabilities.local_only
        or runtime.settings.storage_mode == "memory"
    )
