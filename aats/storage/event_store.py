from __future__ import annotations

from aats.schemas.common import EventEnvelope


class InMemoryEventStore:
    def __init__(self) -> None:
        self._events: list[EventEnvelope] = []
        self._index: dict[str, EventEnvelope] = {}
        self._decision_index: dict[str, list[str]] = {}

    def append(self, envelope: EventEnvelope) -> None:
        if envelope.event_id in self._index:
            return
        self._events.append(envelope)
        self._index[envelope.event_id] = envelope
        decision_id = envelope.payload.get("decision_id")
        if isinstance(decision_id, str):
            self._decision_index.setdefault(decision_id, []).append(envelope.event_id)

    def all(self) -> list[EventEnvelope]:
        return list(self._events)

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
        return [event for event in self._events if event.topic == topic]

    def by_decision(self, decision_id: str) -> list[EventEnvelope]:
        event_ids = self._decision_index.get(decision_id, [])
        return [self._index[event_id] for event_id in event_ids if event_id in self._index]
