from __future__ import annotations

from datetime import timedelta
import unittest

from aats.schemas.common import EventEnvelope, utc_now
from aats.schemas.reconciliation import ReplayProjectionOffset
from aats.services.runtime_scope import RuntimeStateScope
from aats.storage.event_store import InMemoryEventStore


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


if __name__ == "__main__":
    unittest.main()
