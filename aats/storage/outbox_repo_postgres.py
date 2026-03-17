from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aats.schemas.common import EventEnvelope, utc_now
from aats.storage.sqlalchemy_models import OutboxEventModel


class PostgresOutboxRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def enqueue_in_session(self, session: Session, envelope: EventEnvelope) -> None:
        row = session.get(OutboxEventModel, envelope.event_id)
        if row is not None:
            return
        session.add(
            OutboxEventModel(
                event_id=envelope.event_id,
                topic=envelope.topic,
                event_key=envelope.key,
                source_component=envelope.source_component,
                status="PENDING",
                attempt_count=0,
                created_at=envelope.created_at,
                published_at=None,
                last_error=None,
                payload=envelope.model_dump(mode="json"),
            )
        )

    def pending(self, *, limit: int = 100) -> list[EventEnvelope]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(OutboxEventModel)
                .where(OutboxEventModel.status == "PENDING")
                .order_by(OutboxEventModel.created_at, OutboxEventModel.event_id)
                .limit(limit)
            ).all()
        return [EventEnvelope.model_validate(row.payload) for row in rows]

    def mark_published(self, event_id: str) -> None:
        with self.session_factory() as session:
            row = session.get(OutboxEventModel, event_id)
            if row is None:
                return
            row.status = "PUBLISHED"
            row.attempt_count += 1
            row.published_at = utc_now()
            row.last_error = None
            session.commit()

    def record_failure(self, event_id: str, error: str) -> None:
        with self.session_factory() as session:
            row = session.get(OutboxEventModel, event_id)
            if row is None:
                return
            row.attempt_count += 1
            row.last_error = error[:512]
            session.commit()

    def counts(self) -> dict[str, int]:
        with self.session_factory() as session:
            rows = session.scalars(select(OutboxEventModel)).all()
        pending = sum(1 for row in rows if row.status == "PENDING")
        published = sum(1 for row in rows if row.status == "PUBLISHED")
        return {"pending": pending, "published": published}
