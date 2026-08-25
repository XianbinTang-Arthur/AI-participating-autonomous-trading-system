from __future__ import annotations

from datetime import timedelta
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from aats.schemas.common import EventEnvelope, utc_now
from aats.schemas.reconciliation import ReplayProjectionOffset
from aats.services.runtime_scope import RuntimeStateScope
from aats.storage.event_store import InMemoryEventStore
from aats.storage.event_store_postgres import PostgresEventStore
from aats.storage.sqlalchemy_models import Base


def _session_factory(owner: unittest.TestCase) -> sessionmaker[Session]:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    owner.addCleanup(engine.dispose)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def _event(*, age_hours: int) -> EventEnvelope:
    return EventEnvelope(
        event_type="test.event",
        event_timestamp=utc_now() - timedelta(hours=age_hours),
        source_component="test",
        topic="test.topic",
        key="BTC-USDT",
        payload={"symbol": "BTC-USDT", "product_type": "spot", "margin_mode": "cash"},
    )


class TestEventStoreArchive(unittest.TestCase):
    def test_archive_moves_old_events_and_preserves_lookup(self) -> None:
        store = InMemoryEventStore()
        now = utc_now()
        old_event = EventEnvelope(
            event_type="test.event",
            event_timestamp=now - timedelta(hours=2),
            source_component="test",
            topic="test.topic",
            key="BTC-USDT",
            payload={"symbol": "BTC-USDT", "product_type": "spot", "margin_mode": "cash"},
        )
        new_event = EventEnvelope(
            event_type="test.event",
            event_timestamp=now,
            source_component="test",
            topic="test.topic",
            key="BTC-USDT",
            payload={"symbol": "BTC-USDT", "product_type": "spot", "margin_mode": "cash"},
        )
        store.append(old_event)
        store.append(new_event)

        result = store.archive_before(before_ts=now - timedelta(hours=1))

        self.assertEqual(result["archived_event_count"], 1)
        self.assertEqual(store.count(topic="test.topic"), 2)
        self.assertEqual(store.archive_summary()["archive_event_count"], 1)
        self.assertEqual(store.latest("test.topic").event_id, new_event.event_id)
        self.assertIsNotNone(store.get(old_event.event_id))

    def test_replay_offset_round_trip_is_scope_aware(self) -> None:
        store = InMemoryEventStore()
        now = utc_now()
        offset = ReplayProjectionOffset(
            projection_key="portfolio_replay",
            product_type="spot",
            margin_mode="cash",
            allowed_symbols=("BTC-USDT",),
            last_event_id="evt_last",
            last_event_timestamp=now,
            baseline_generation_id="base_1",
            exchange_ack_watermark_id="watermark_1",
            updated_at=now,
        )
        scope = RuntimeStateScope(
            product_type="spot",
            margin_mode="cash",
            default_symbol="BTC-USDT",
            allowed_symbols=("BTC-USDT",),
        )

        store.save_replay_offset(offset)
        loaded = store.latest_replay_offset(projection_key="portfolio_replay", scope=scope)

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.offset_id, offset.offset_id)
        self.assertEqual(loaded.baseline_generation_id, "base_1")
        self.assertEqual(loaded.exchange_ack_watermark_id, "watermark_1")


class TestEventStoreBatchLookup(unittest.TestCase):
    def test_inmemory_get_many_returns_hot_and_archived_rows(self) -> None:
        store = InMemoryEventStore()
        old_event = _event(age_hours=2)
        new_event = _event(age_hours=0)
        store.append(old_event)
        store.append(new_event)
        store.archive_before(before_ts=utc_now() - timedelta(hours=1))

        rows = store.get_many([
            old_event.event_id,
            "",
            new_event.event_id,
            old_event.event_id,
            "missing_event",
        ])

        self.assertEqual(set(rows), {old_event.event_id, new_event.event_id})
        self.assertEqual(rows[old_event.event_id].event_id, old_event.event_id)
        self.assertEqual(rows[new_event.event_id].event_id, new_event.event_id)

    def test_postgres_get_many_returns_hot_and_archived_rows(self) -> None:
        store = PostgresEventStore(_session_factory(self))
        old_event = _event(age_hours=2)
        new_event = _event(age_hours=0)
        store.append(old_event)
        store.append(new_event)
        store.archive_before(before_ts=utc_now() - timedelta(hours=1))

        rows = store.get_many([
            old_event.event_id,
            "",
            new_event.event_id,
            old_event.event_id,
            "missing_event",
        ])

        self.assertEqual(set(rows), {old_event.event_id, new_event.event_id})
        self.assertEqual(rows[old_event.event_id].payload["symbol"], "BTC-USDT")
        self.assertEqual(rows[new_event.event_id].payload["symbol"], "BTC-USDT")


if __name__ == "__main__":
    unittest.main()
