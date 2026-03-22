from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from aats.schemas.operator import OperatorUserRecord
from aats.storage.sqlalchemy_models import OperatorUserModel


class PostgresOperatorUserRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def save_user(self, user: OperatorUserRecord) -> OperatorUserRecord:
        with self.session_factory() as session:
            row = session.scalar(
                select(OperatorUserModel).where(OperatorUserModel.username == user.username)
            )
            if row is None:
                row = OperatorUserModel(
                    user_id=user.user_id,
                    username=user.username,
                    password_hash=user.password_hash,
                    role=user.role,
                    enabled=user.enabled,
                    created_at=user.created_at,
                    updated_at=user.updated_at,
                    last_login_at=user.last_login_at,
                    payload=user.model_dump(mode="json"),
                )
                session.add(row)
            else:
                user = user.model_copy(
                    update={
                        "user_id": row.user_id,
                        "created_at": row.created_at,
                    }
                )
                row.password_hash = user.password_hash
                row.role = user.role
                row.enabled = user.enabled
                row.updated_at = user.updated_at
                row.last_login_at = user.last_login_at
                row.payload = user.model_dump(mode="json")
            session.commit()
        return user

    def get_by_username(self, username: str) -> OperatorUserRecord | None:
        with self.session_factory() as session:
            row = session.scalar(
                select(OperatorUserModel).where(OperatorUserModel.username == username)
            )
        return OperatorUserRecord.model_validate(row.payload) if row is not None else None

    def all_users(self) -> list[OperatorUserRecord]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(OperatorUserModel).order_by(OperatorUserModel.username)
            ).all()
        return [OperatorUserRecord.model_validate(row.payload) for row in rows]

    def count(self, *, enabled_only: bool = False) -> int:
        query = select(func.count()).select_from(OperatorUserModel)
        if enabled_only:
            query = query.where(OperatorUserModel.enabled.is_(True))
        with self.session_factory() as session:
            count = session.scalar(query)
        return int(count or 0)

    def record_login(self, username: str, logged_in_at: datetime) -> None:
        with self.session_factory() as session:
            row = session.scalar(
                select(OperatorUserModel).where(OperatorUserModel.username == username)
            )
            if row is None:
                return
            payload = dict(row.payload)
            payload["last_login_at"] = logged_in_at.isoformat().replace("+00:00", "Z")
            payload["updated_at"] = logged_in_at.isoformat().replace("+00:00", "Z")
            row.last_login_at = logged_in_at
            row.updated_at = logged_in_at
            row.payload = payload
            session.commit()

    def delete_user(self, username: str) -> bool:
        with self.session_factory() as session:
            row = session.scalar(
                select(OperatorUserModel).where(OperatorUserModel.username == username)
            )
            if row is None:
                return False
            session.delete(row)
            session.commit()
            return True

    def bump_session_version(self, username: str, updated_at: datetime) -> OperatorUserRecord | None:
        with self.session_factory() as session:
            row = session.scalar(
                select(OperatorUserModel).where(OperatorUserModel.username == username)
            )
            if row is None:
                return None
            user = OperatorUserRecord.model_validate(row.payload).model_copy(
                update={
                    "session_version": int(row.payload.get("session_version", 1) or 1) + 1,
                    "updated_at": updated_at,
                }
            )
            row.password_hash = user.password_hash
            row.role = user.role
            row.enabled = user.enabled
            row.updated_at = user.updated_at
            row.last_login_at = user.last_login_at
            row.payload = user.model_dump(mode="json")
            session.commit()
            return user
