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

    def mark_published_batch(self, event_ids: list[str]) -> None:
        if not event_ids:
            return
        now = utc_now()
        with self.session_factory() as session:
            for event_id in event_ids:
                row = session.get(OutboxEventModel, event_id)
                if row is None:
                    continue
                row.status = "PUBLISHED"
                row.attempt_count += 1
                row.published_at = now
                row.last_error = None
            session.commit()

    def record_failure(self, event_id: str, error: str) -> None:
        self.record_failure_with_threshold(event_id, error)

    def record_failure_with_threshold(
        self,
        event_id: str,
        error: str,
        *,
        max_attempts: int | None = None,
    ) -> str | None:
        with self.session_factory() as session:
            row = session.get(OutboxEventModel, event_id)
            if row is None:
                return None
            row.attempt_count += 1
            row.last_error = error[:512]
            if max_attempts is not None and row.attempt_count >= max_attempts:
                row.status = "FAILED"
            session.commit()
            return row.status

    def counts(self) -> dict[str, int]:
        with self.session_factory() as session:
            rows = session.scalars(select(OutboxEventModel)).all()
        pending = sum(1 for row in rows if row.status == "PENDING")
        published = sum(1 for row in rows if row.status == "PUBLISHED")
        failed = sum(1 for row in rows if row.status == "FAILED")
        return {"pending": pending, "published": published, "failed": failed}
