from __future__ import annotations

from datetime import datetime

from sqlalchemy import asc, select
from sqlalchemy.orm import Session, sessionmaker

from aats.schemas.common import EventEnvelope, dump_payload_exact
from aats.storage.sqlalchemy_models import CommandOutboxModel


class PostgresCommandOutboxRepositoryV2:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def enqueue(self, *, envelope: EventEnvelope, aggregate_type: str, aggregate_id: str) -> None:
        with self.session_factory() as session:
            row = session.get(CommandOutboxModel, envelope.event_id)
            if row is not None:
                return
            session.add(
                CommandOutboxModel(
                    event_id=envelope.event_id,
                    aggregate_type=aggregate_type,
                    aggregate_id=aggregate_id,
                    topic=envelope.topic,
                    payload=dump_payload_exact(envelope),
                    status="PENDING",
                    attempt_count=0,
                    last_error=None,
                    created_at=envelope.created_at,
                    published_at=None,
                )
            )
            session.commit()

    def pending(self, *, limit: int) -> list[dict]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(CommandOutboxModel)
                .where(CommandOutboxModel.status == "PENDING")
                .order_by(asc(CommandOutboxModel.created_at), asc(CommandOutboxModel.event_id))
                .limit(limit)
            ).all()
        return [_command_outbox_row_to_dict(row) for row in rows]

    def mark_published(self, event_id: str, published_at: datetime) -> None:
        with self.session_factory() as session:
            row = session.get(CommandOutboxModel, event_id)
            if row is None:
                return
            row.status = "PUBLISHED"
            row.attempt_count += 1
            row.published_at = published_at
            row.last_error = None
            session.commit()

    def mark_failed(self, event_id: str, error: str) -> None:
        with self.session_factory() as session:
            row = session.get(CommandOutboxModel, event_id)
            if row is None:
                return
            row.status = "FAILED"
            row.attempt_count += 1
            row.last_error = error[:1024]
            session.commit()


def _command_outbox_row_to_dict(row: CommandOutboxModel) -> dict:
    return {
        "event_id": row.event_id,
        "aggregate_type": row.aggregate_type,
        "aggregate_id": row.aggregate_id,
        "topic": row.topic,
        "payload": dict(row.payload),
        "status": row.status,
        "attempt_count": row.attempt_count,
        "last_error": row.last_error,
        "created_at": row.created_at,
        "published_at": row.published_at,
    }
