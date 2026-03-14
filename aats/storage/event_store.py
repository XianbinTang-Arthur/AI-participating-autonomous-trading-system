from __future__ import annotations

from datetime import datetime

from aats.schemas.common import EventEnvelope


class InMemoryEventStore:
    def __init__(self) -> None:
        self._events: list[EventEnvelope] = []
        self._index: dict[str, EventEnvelope] = {}
        self._decision_index: dict[str, list[str]] = {}
        self._topic_index: dict[str, list[str]] = {}

    def append(self, envelope: EventEnvelope) -> None:
        if envelope.event_id in self._index:
            return
        self._events.append(envelope)
        self._index[envelope.event_id] = envelope
        self._topic_index.setdefault(envelope.topic, []).append(envelope.event_id)
        decision_id = envelope.payload.get("decision_id")
        if isinstance(decision_id, str):
            self._decision_index.setdefault(decision_id, []).append(envelope.event_id)

    def all(self) -> list[EventEnvelope]:
        return list(self._events)

    def count(self, *, topic: str | None = None, decision_id: str | None = None) -> int:
        if topic is None and decision_id is None:
            return len(self._events)
        if topic is not None and decision_id is None:
            return len(self._topic_index.get(topic, []))
        if topic is None and decision_id is not None:
            return len(self._decision_index.get(decision_id, []))
        return sum(
            1
            for event in self._events
            if event.topic == topic and event.payload.get("decision_id") == decision_id
        )

    def get(self, event_id: str) -> EventEnvelope | None:
        return self._index.get(event_id)

    def latest(self, topic: str, key: str | None = None) -> EventEnvelope | None:
        for event in reversed(self._events):
            if event.topic != topic:
                continue
            if key is not None and event.key != key:
                continue
            return event
        return None

    def by_topic(self, topic: str) -> list[EventEnvelope]:
        event_ids = self._topic_index.get(topic, [])
        return [self._index[event_id] for event_id in event_ids if event_id in self._index]

    def by_decision(self, decision_id: str) -> list[EventEnvelope]:
        event_ids = self._decision_index.get(decision_id, [])
        return [self._index[event_id] for event_id in event_ids if event_id in self._index]

    def between(
        self,
        *,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        topic: str | None = None,
        decision_id: str | None = None,
    ) -> list[EventEnvelope]:
        return [
            event
            for event in self._events
            if (start_at is None or event.event_timestamp >= start_at)
            and (end_at is None or event.event_timestamp <= end_at)
            and (topic is None or event.topic == topic)
            and (decision_id is None or event.payload.get("decision_id") == decision_id)
        ]
