from __future__ import annotations

from sqlalchemy import Select, desc, select
from sqlalchemy.orm import Session, sessionmaker

from aats.schemas.common import EventEnvelope
from aats.storage.sqlalchemy_models import EventEnvelopeModel


class PostgresEventStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def append(self, envelope: EventEnvelope) -> None:
        with self.session_factory() as session:
            existing = session.scalar(
                select(EventEnvelopeModel.sequence_id).where(EventEnvelopeModel.event_id == envelope.event_id)
            )
            if existing is not None:
                return

            session.add(
                EventEnvelopeModel(
                    event_id=envelope.event_id,
                    schema_version=envelope.schema_version,
                    created_at=envelope.created_at,
                    event_type=envelope.event_type,
                    event_timestamp=envelope.event_timestamp,
                    source_component=envelope.source_component,
                    topic=envelope.topic,
                    event_key=envelope.key,
                    decision_id=self._decision_id(envelope),
                    payload=envelope.payload,
                )
            )
            session.commit()

    def all(self) -> list[EventEnvelope]:
        with self.session_factory() as session:
            rows = session.scalars(select(EventEnvelopeModel).order_by(EventEnvelopeModel.sequence_id)).all()
        return [self._to_schema(row) for row in rows]

    def get(self, event_id: str) -> EventEnvelope | None:
        with self.session_factory() as session:
            row = session.scalar(select(EventEnvelopeModel).where(EventEnvelopeModel.event_id == event_id))
        return self._to_schema(row) if row is not None else None

    def latest(self, topic: str, key: str | None = None) -> EventEnvelope | None:
        query: Select[tuple[EventEnvelopeModel]] = select(EventEnvelopeModel).where(EventEnvelopeModel.topic == topic)
        if key is not None:
            query = query.where(EventEnvelopeModel.event_key == key)
        query = query.order_by(desc(EventEnvelopeModel.sequence_id)).limit(1)
        with self.session_factory() as session:
            row = session.scalar(query)
        return self._to_schema(row) if row is not None else None

    def by_topic(self, topic: str) -> list[EventEnvelope]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(EventEnvelopeModel)
                .where(EventEnvelopeModel.topic == topic)
                .order_by(EventEnvelopeModel.sequence_id)
            ).all()
        return [self._to_schema(row) for row in rows]

    def by_decision(self, decision_id: str) -> list[EventEnvelope]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(EventEnvelopeModel)
                .where(EventEnvelopeModel.decision_id == decision_id)
                .order_by(EventEnvelopeModel.sequence_id)
            ).all()
        return [self._to_schema(row) for row in rows]

    @staticmethod
    def _decision_id(envelope: EventEnvelope) -> str | None:
        decision_id = envelope.payload.get("decision_id")
        return decision_id if isinstance(decision_id, str) else None

    @staticmethod
    def _to_schema(row: EventEnvelopeModel) -> EventEnvelope:
        return EventEnvelope(
            schema_version=row.schema_version,
            created_at=row.created_at,
            event_id=row.event_id,
            event_type=row.event_type,
            event_timestamp=row.event_timestamp,
            source_component=row.source_component,
            topic=row.topic,
            key=row.event_key,
            payload=row.payload,
        )
