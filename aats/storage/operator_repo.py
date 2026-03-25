from __future__ import annotations

from datetime import datetime, timedelta

from aats.schemas.operator import OperatorUserRecord


class InMemoryOperatorUserRepository:
    def __init__(self) -> None:
        self._users_by_username: dict[str, OperatorUserRecord] = {}

    def save_user(self, user: OperatorUserRecord) -> OperatorUserRecord:
        existing = self._users_by_username.get(user.username)
        if existing is not None:
            user = user.model_copy(
                update={
                    "user_id": existing.user_id,
                    "created_at": existing.created_at,
                }
            )
        self._users_by_username[user.username] = user
        return user

    def get_by_username(self, username: str) -> OperatorUserRecord | None:
        return self._users_by_username.get(username)

    def all_users(self) -> list[OperatorUserRecord]:
        return sorted(self._users_by_username.values(), key=lambda item: item.username)

    def count(self, *, enabled_only: bool = False) -> int:
        if not enabled_only:
            return len(self._users_by_username)
        return sum(1 for user in self._users_by_username.values() if user.enabled)

    def record_login(self, username: str, logged_in_at: datetime) -> None:
        user = self._users_by_username.get(username)
        if user is None:
            return
        self._users_by_username[username] = user.model_copy(
            update={
                "last_login_at": logged_in_at,
                "last_failed_login_at": None,
                "failed_login_attempts": 0,
                "locked_until": None,
                "updated_at": logged_in_at,
            }
        )

    def record_login_failure(
        self,
        username: str,
        attempted_at: datetime,
        *,
        max_failed_attempts: int,
        lockout_seconds: int,
    ) -> OperatorUserRecord | None:
        user = self._users_by_username.get(username)
        if user is None:
            return None
        attempts = max(0, int(user.failed_login_attempts)) + 1
        locked_until = None
        if attempts >= max(1, max_failed_attempts):
            locked_until = attempted_at + timedelta(seconds=max(0, lockout_seconds))
        updated = user.model_copy(
            update={
                "failed_login_attempts": attempts,
                "last_failed_login_at": attempted_at,
                "locked_until": locked_until,
                "updated_at": attempted_at,
            }
        )
        self._users_by_username[username] = updated
        return updated

    def delete_user(self, username: str) -> bool:
        return self._users_by_username.pop(username, None) is not None

    def bump_session_version(self, username: str, updated_at: datetime) -> OperatorUserRecord | None:
        user = self._users_by_username.get(username)
        if user is None:
            return None
        updated = user.model_copy(
            update={
                "session_version": user.session_version + 1,
                "updated_at": updated_at,
            }
        )
        self._users_by_username[username] = updated
        return updated
