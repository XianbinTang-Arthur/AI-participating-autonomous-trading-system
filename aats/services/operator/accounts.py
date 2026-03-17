from __future__ import annotations

from aats.schemas.operator import OperatorRole, OperatorUserRecord
from aats.schemas.common import utc_now
from aats.services.operator.passwords import hash_password


def create_operator_user(
    operator_repo,
    *,
    username: str,
    password: str,
    role: OperatorRole,
    enabled: bool = True,
) -> OperatorUserRecord:
    normalized_username = username.strip()
    if not normalized_username:
        raise ValueError("operator_username_required")
    if role not in {"viewer", "operator", "admin"}:
        raise ValueError("operator_role_invalid")
    if not password:
        raise ValueError("operator_password_required")
    if operator_repo.get_by_username(normalized_username) is not None:
        raise ValueError("operator_username_conflict")
    return operator_repo.save_user(
        OperatorUserRecord(
            username=normalized_username,
            password_hash=hash_password(password),
            role=role,
            enabled=enabled,
        )
    )


def update_operator_user(
    operator_repo,
    *,
    username: str,
    role: OperatorRole | None = None,
    enabled: bool | None = None,
    password: str | None = None,
    actor_identity: str | None = None,
) -> tuple[OperatorUserRecord, dict[str, object]]:
    existing = operator_repo.get_by_username(username)
    if existing is None:
        raise KeyError(username)
    if password is not None and not password:
        raise ValueError("operator_password_required")

    next_role = role if role is not None else existing.role
    next_enabled = enabled if enabled is not None else existing.enabled
    if next_role not in {"viewer", "operator", "admin"}:
        raise ValueError("operator_role_invalid")
    _ensure_admin_retained(
        operator_repo,
        existing=existing,
        next_role=next_role,
        next_enabled=next_enabled,
        actor_identity=actor_identity,
    )

    changes: dict[str, object] = {}
    if next_role != existing.role:
        changes["role"] = {"before": existing.role, "after": next_role}
    if next_enabled != existing.enabled:
        changes["enabled"] = {"before": existing.enabled, "after": next_enabled}
    if password is not None:
        changes["password_reset"] = True

    updated = operator_repo.save_user(
        existing.model_copy(
            update={
                "role": next_role,
                "enabled": next_enabled,
                "password_hash": hash_password(password) if password is not None else existing.password_hash,
                "updated_at": utc_now(),
            }
        )
    )
    return updated, changes


def delete_operator_user(
    operator_repo,
    *,
    username: str,
    actor_identity: str | None = None,
) -> OperatorUserRecord:
    existing = operator_repo.get_by_username(username)
    if existing is None:
        raise KeyError(username)
    _ensure_admin_retained(
        operator_repo,
        existing=existing,
        next_role=existing.role,
        next_enabled=False,
        actor_identity=actor_identity,
        deleting=True,
    )
    operator_repo.delete_user(username)
    return existing


def enabled_admin_count(operator_repo) -> int:
    return sum(1 for user in operator_repo.all_users() if user.enabled and user.role == "admin")


def _ensure_admin_retained(
    operator_repo,
    *,
    existing: OperatorUserRecord,
    next_role: OperatorRole,
    next_enabled: bool,
    actor_identity: str | None,
    deleting: bool = False,
) -> None:
    if actor_identity and existing.username == actor_identity:
        if deleting:
            raise ValueError("operator_self_delete_forbidden")
        if not next_enabled:
            raise ValueError("operator_self_disable_forbidden")
    if existing.role != "admin" or not existing.enabled:
        return
    if next_role == "admin" and next_enabled and not deleting:
        return
    if enabled_admin_count(operator_repo) <= 1:
        raise ValueError("operator_last_admin_required")
