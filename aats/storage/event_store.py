from __future__ import annotations

import hashlib
from datetime import datetime

from aats.schemas.common import EventEnvelope
from aats.schemas.reconciliation import ReplayProjectionOffset
from aats.services.runtime_scope import RuntimeStateScope, infer_product_type_from_symbol


class InMemoryEventStore:
    def __init__(self) -> None:
        self._events: list[EventEnvelope] = []
        self._archive_events: list[EventEnvelope] = []
        self._index: dict[str, EventEnvelope] = {}
        self._decision_index: dict[str, list[str]] = {}
        self._topic_index: dict[str, list[str]] = {}
        self._archive_decision_index: dict[str, list[str]] = {}
        self._archive_topic_index: dict[str, list[str]] = {}
        self._replay_offsets: dict[tuple[str, str, str, str], ReplayProjectionOffset] = {}

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
        return [*self._archive_events, *self._events]

    def count(self, *, topic: str | None = None, decision_id: str | None = None) -> int:
        if topic is None and decision_id is None:
            return len(self._archive_events) + len(self._events)
        if topic is not None and decision_id is None:
            return len(self._archive_topic_index.get(topic, [])) + len(self._topic_index.get(topic, []))
        if topic is None and decision_id is not None:
            return len(self._archive_decision_index.get(decision_id, [])) + len(self._decision_index.get(decision_id, []))
        return sum(
            1
            for event in self.all()
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
        for event in reversed(self._archive_events):
            if event.topic != topic:
                continue
            if key is not None and event.key != key:
                continue
            return event
        return None

    def by_topic(self, topic: str) -> list[EventEnvelope]:
        archive_ids = self._archive_topic_index.get(topic, [])
        event_ids = self._topic_index.get(topic, [])
        ordered_ids = [*archive_ids, *event_ids]
        return [self._index[event_id] for event_id in ordered_ids if event_id in self._index]

    def recent_by_topic(self, topic: str, *, limit: int) -> list[EventEnvelope]:
        if limit <= 0:
            return []
        return self.by_topic(topic)[-limit:]

    def by_topic_scoped(
        self,
        topic: str,
        *,
        scope: RuntimeStateScope,
        limit: int | None = None,
    ) -> list[EventEnvelope]:
        rows = [event for event in self.by_topic(topic) if self._event_matches_scope(event, scope)]
        if limit is not None:
            rows = rows[-limit:]
        return rows

    def latest_by_topic_scoped(
        self,
        topic: str,
        *,
        scope: RuntimeStateScope,
        key: str | None = None,
    ) -> EventEnvelope | None:
        for event in reversed(self.by_topic(topic)):
            if key is not None and event.key != key:
                continue
            if self._event_matches_scope(event, scope):
                return event
        return None

    def by_decision(self, decision_id: str) -> list[EventEnvelope]:
        event_ids = [*self._archive_decision_index.get(decision_id, []), *self._decision_index.get(decision_id, [])]
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
            for event in self.all()
            if (start_at is None or event.event_timestamp >= start_at)
            and (end_at is None or event.event_timestamp <= end_at)
            and (topic is None or event.topic == topic)
            and (decision_id is None or event.payload.get("decision_id") == decision_id)
        ]

    def archive_before(self, *, before_ts: datetime) -> dict[str, int]:
        moved: list[EventEnvelope] = []
        retained: list[EventEnvelope] = []
        for event in self._events:
            if event.event_timestamp < before_ts:
                moved.append(event)
            else:
                retained.append(event)
        if not moved:
            return {"archived_event_count": 0, "hot_event_count": len(self._events), "archive_event_count": len(self._archive_events)}
        self._events = retained
        self._archive_events.extend(moved)
        self._topic_index = self._rebuild_topic_index(self._events)
        self._decision_index = self._rebuild_decision_index(self._events)
        self._archive_topic_index = self._rebuild_topic_index(self._archive_events)
        self._archive_decision_index = self._rebuild_decision_index(self._archive_events)
        return {
            "archived_event_count": len(moved),
            "hot_event_count": len(self._events),
            "archive_event_count": len(self._archive_events),
        }

    def archive_summary(self) -> dict[str, object]:
        hot_first = self._events[0].event_timestamp if self._events else None
        hot_last = self._events[-1].event_timestamp if self._events else None
        archive_first = self._archive_events[0].event_timestamp if self._archive_events else None
        archive_last = self._archive_events[-1].event_timestamp if self._archive_events else None
        return {
            "hot_event_count": len(self._events),
            "archive_event_count": len(self._archive_events),
            "total_event_count": len(self._archive_events) + len(self._events),
            "hot_window": {
                "start_at": hot_first,
                "end_at": hot_last,
            },
            "archive_window": {
                "start_at": archive_first,
                "end_at": archive_last,
            },
            "replay_offset_count": len(self._replay_offsets),
        }

    def save_replay_offset(self, offset: ReplayProjectionOffset) -> ReplayProjectionOffset:
        key = self._offset_key(
            projection_key=offset.projection_key,
            product_type=offset.product_type,
            margin_mode=offset.margin_mode,
            allowed_symbols=tuple(offset.allowed_symbols),
        )
        self._replay_offsets[key] = offset
        return offset

    def latest_replay_offset(
        self,
        *,
        projection_key: str,
        scope: RuntimeStateScope,
    ) -> ReplayProjectionOffset | None:
        return self._replay_offsets.get(
            self._offset_key(
                projection_key=projection_key,
                product_type=scope.product_type,
                margin_mode=scope.margin_mode,
                allowed_symbols=scope.allowed_symbols,
            )
        )

    @staticmethod
    def _rebuild_topic_index(events: list[EventEnvelope]) -> dict[str, list[str]]:
        index: dict[str, list[str]] = {}
        for event in events:
            index.setdefault(event.topic, []).append(event.event_id)
        return index

    @staticmethod
    def _rebuild_decision_index(events: list[EventEnvelope]) -> dict[str, list[str]]:
        index: dict[str, list[str]] = {}
        for event in events:
            decision_id = event.payload.get("decision_id")
            if isinstance(decision_id, str):
                index.setdefault(decision_id, []).append(event.event_id)
        return index

    @staticmethod
    def _offset_key(
        *,
        projection_key: str,
        product_type: str,
        margin_mode: str,
        allowed_symbols: tuple[str, ...],
    ) -> tuple[str, str, str, str]:
        normalized = ",".join(sorted(allowed_symbols))
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        return (projection_key, product_type, margin_mode, digest)

    @staticmethod
    def _event_matches_scope(event: EventEnvelope, scope: RuntimeStateScope) -> bool:
        payload = event.payload
        symbol = payload.get("symbol")
        allowed_symbols = payload.get("allowed_symbols")
        product_type = payload.get("product_type")
        margin_mode = payload.get("margin_mode")
        if product_type is None and isinstance(symbol, str):
            product_type = infer_product_type_from_symbol(symbol)
        if margin_mode is None and product_type == "spot":
            margin_mode = "cash"
        if product_type is not None and product_type != scope.product_type:
            return False
        if margin_mode is not None and margin_mode != scope.margin_mode:
            return False
        if isinstance(symbol, str):
            return scope.symbol_allowed(symbol)
        if isinstance(allowed_symbols, list) and allowed_symbols:
            return all(isinstance(item, str) and scope.symbol_allowed(item) for item in allowed_symbols)
        return True
