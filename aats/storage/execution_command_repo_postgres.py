from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, asc, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from aats.schemas.common import dump_payload_exact
from aats.storage.sqlalchemy_models import ExecutionCommandModel


class PostgresExecutionCommandRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def enqueue_command(
        self,
        *,
        command_id: str,
        order_id: str,
        command_type: str,
        idempotency_key: str,
        payload: dict,
        created_at: datetime,
    ) -> None:
        with self.session_factory() as session:
            self.enqueue_command_in_session(
                session,
                command_id=command_id,
                order_id=order_id,
                command_type=command_type,
                idempotency_key=idempotency_key,
                payload=payload,
                created_at=created_at,
            )
            session.commit()

    def enqueue_command_in_session(
        self,
        session: Session,
        *,
        command_id: str,
        order_id: str,
        command_type: str,
        idempotency_key: str,
        payload: dict,
        created_at: datetime,
    ) -> None:
        inserted_command_id = session.scalar(
            insert(ExecutionCommandModel)
            .values(
                command_id=command_id,
                order_id=order_id,
                command_type=command_type,
                idempotency_key=idempotency_key,
                state="PENDING",
                attempt_count=0,
                last_error=None,
                command_payload=dump_payload_exact(payload),
                created_at=created_at,
                updated_at=created_at,
            )
            .returning(ExecutionCommandModel.command_id)
            .on_conflict_do_nothing()
        )
        if inserted_command_id is not None:
            return
        row = session.get(ExecutionCommandModel, command_id)
        if row is None:
            row = self._get_by_idempotency_key_in_session(session, idempotency_key)
        if row is not None:
            return
        raise RuntimeError("execution_command_insert_conflict_without_existing_row")

    def get_command(self, command_id: str) -> dict | None:
        with self.session_factory() as session:
            row = session.get(ExecutionCommandModel, command_id)
        return _command_row_to_dict(row) if row is not None else None

    def get_by_idempotency_key(self, idempotency_key: str) -> dict | None:
        with self.session_factory() as session:
            row = self._get_by_idempotency_key_in_session(session, idempotency_key)
        return _command_row_to_dict(row) if row is not None else None

    def get_by_idempotency_key_in_session(self, session: Session, idempotency_key: str) -> dict | None:
        row = self._get_by_idempotency_key_in_session(session, idempotency_key)
        return _command_row_to_dict(row) if row is not None else None

    def pending_commands(self, *, limit: int, sent_stale_before: datetime | None = None) -> list[dict]:
        with self.session_factory() as session:
            sent_clause = ExecutionCommandModel.state == "PENDING"
            if sent_stale_before is None:
                sent_clause = ExecutionCommandModel.state.in_(("PENDING", "SENT"))
            else:
                sent_clause = or_(
                    ExecutionCommandModel.state == "PENDING",
                    and_(
                        ExecutionCommandModel.state == "SENT",
                        ExecutionCommandModel.updated_at <= sent_stale_before,
                    ),
                )
            rows = session.scalars(
                select(ExecutionCommandModel)
                .where(sent_clause)
                .order_by(asc(ExecutionCommandModel.created_at), asc(ExecutionCommandModel.command_id))
                .limit(limit)
            ).all()
        return [_command_row_to_dict(row) for row in rows]

    def claim_command(
        self,
        *,
        command_id: str,
        expected_state: str,
        expected_updated_at: datetime,
        updated_at: datetime,
    ) -> bool:
        with self.session_factory() as session:
            row = session.scalar(
                select(ExecutionCommandModel)
                .where(ExecutionCommandModel.command_id == command_id)
                .with_for_update()
                .limit(1)
            )
            if row is None:
                return False
            if row.state != expected_state:
                return False
            if row.updated_at != expected_updated_at:
                return False
            row.state = "SENT"
            row.attempt_count += 1
            row.updated_at = updated_at
            row.last_error = None
            session.commit()
            return True

    def mark_sent(self, command_id: str, updated_at: datetime) -> None:
        self._update_state(command_id=command_id, state="SENT", updated_at=updated_at, last_error=None)

    def mark_acked(self, command_id: str, updated_at: datetime) -> None:
        self._update_state(command_id=command_id, state="ACKED", updated_at=updated_at, last_error=None)

    def mark_failed(self, command_id: str, error: str, updated_at: datetime) -> None:
        self._update_state(command_id=command_id, state="FAILED", updated_at=updated_at, last_error=error)

    def mark_abandoned(self, command_id: str, reason: str, updated_at: datetime) -> None:
        self._update_state(command_id=command_id, state="ABANDONED", updated_at=updated_at, last_error=reason)

    def _update_state(
        self,
        *,
        command_id: str,
        state: str,
        updated_at: datetime,
        last_error: str | None,
    ) -> None:
        with self.session_factory() as session:
            row = session.get(ExecutionCommandModel, command_id)
            if row is None:
                return
            row.state = state
            row.attempt_count += 1
            row.last_error = None if last_error is None else last_error[:1024]
            row.updated_at = updated_at
            session.commit()

    @staticmethod
    def _get_by_idempotency_key_in_session(session: Session, idempotency_key: str) -> ExecutionCommandModel | None:
        return session.scalar(
            select(ExecutionCommandModel).where(ExecutionCommandModel.idempotency_key == idempotency_key).limit(1)
        )


def _command_row_to_dict(row: ExecutionCommandModel) -> dict:
    return {
        "command_id": row.command_id,
        "order_id": row.order_id,
        "command_type": row.command_type,
        "idempotency_key": row.idempotency_key,
        "state": row.state,
        "attempt_count": row.attempt_count,
        "last_error": row.last_error,
        "command_payload": dict(row.command_payload),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
