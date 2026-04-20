"""B0 契约测试：persist-only critical topics 的 publish 短路语义。

docs/task/nats_retention_global_architecture_sow.md §B0 引入：
`publish_envelope` 对 `DEFAULT_PERSIST_ONLY_CRITICAL_TOPICS` 里的 topic 做短路
——只走 `event_store.append`（PG 持久化），**跳过** `js.publish`（NATS stream）。

为什么：这类 topic（当前是 AUDIT_RECORDS）有 **0 live NATS consumer** 订阅
（已通过 runtime 全扫 66 durable consumer 确认 + 代码层面 grep），NATS stream
里的消息等 TTL 到期自然 discard，纯占字节。唯一消费路径是 `event_store.by_decision()`
类 PG 查询。

本测试不允许"意外回归到走 NATS 路径"，那会让 40% stream 字节再次被浪费。
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel

from aats.bus.nats_bus import (
    DEFAULT_PERSIST_ONLY_CRITICAL_TOPICS,
    NatsBusConfig,
    NatsEventBus,
)
from aats.events import topics as _topics
from aats.events.envelopes import build_envelope
from aats.schemas.common import EventEnvelope


class _FakePayloadModel(BaseModel):
    """最小 Pydantic payload —— build_envelope 要求 BaseModel + dict-dumpable。"""

    marker: str = "test"


def _make_envelope(topic: str) -> EventEnvelope:
    return build_envelope(
        topic=topic,
        key="BTC-USDT-SWAP",
        payload_model=_FakePayloadModel(),
        source_component="test",
    )


class TestPersistOnlyShortCircuit(unittest.IsolatedAsyncioTestCase):
    """AUDIT_RECORDS 走 persist-only 路径：event_store.append 调用；
    js.publish **不** 被调用。"""

    async def test_audit_records_persist_only_skips_js_publish(self) -> None:
        pytest.importorskip(
            "nats",
            reason="nats-py 未安装；本测试走 NatsEventBus 真实 publish_envelope",
        )

        bus = NatsEventBus(
            config=NatsBusConfig(),
            consumer_role="test",
        )
        fake_event_store = MagicMock()
        fake_event_store.append = MagicMock()
        fake_js = MagicMock()
        fake_js.publish = AsyncMock()
        bus._event_store = fake_event_store  # type: ignore[attr-defined]
        bus._js = fake_js  # type: ignore[attr-defined]
        bus._persistence_mode = "strict"  # type: ignore[attr-defined]

        envelope = _make_envelope(_topics.AUDIT_RECORDS)
        await bus.publish_envelope(envelope)

        fake_event_store.append.assert_called_once_with(envelope)
        fake_js.publish.assert_not_awaited()

    async def test_non_persist_only_topic_still_goes_to_js_publish(self) -> None:
        """对照组：非 persist-only topic（DECISION_CONTEXTS）应同时 append
        event_store **和** js.publish。确保短路没误伤其他 topic。"""
        pytest.importorskip("nats")

        bus = NatsEventBus(
            config=NatsBusConfig(),
            consumer_role="test",
        )
        fake_event_store = MagicMock()
        fake_event_store.append = MagicMock()
        fake_js = MagicMock()
        fake_js.publish = AsyncMock()
        bus._event_store = fake_event_store  # type: ignore[attr-defined]
        bus._js = fake_js  # type: ignore[attr-defined]
        bus._persistence_mode = "strict"  # type: ignore[attr-defined]

        envelope = _make_envelope(_topics.DECISION_CONTEXTS)
        await bus.publish_envelope(envelope)

        fake_event_store.append.assert_called_once_with(envelope)
        fake_js.publish.assert_awaited_once()

    async def test_all_persist_only_topics_are_shortcircuited(self) -> None:
        """白名单锚点：遍历 DEFAULT_PERSIST_ONLY_CRITICAL_TOPICS，每个都走
        短路。未来往集合里加新 persist-only topic 时本测试自动覆盖。"""
        pytest.importorskip("nats")

        for topic in DEFAULT_PERSIST_ONLY_CRITICAL_TOPICS:
            with self.subTest(topic=topic):
                bus = NatsEventBus(
                    config=NatsBusConfig(),
                    consumer_role="test",
                )
                fake_event_store = MagicMock()
                fake_event_store.append = MagicMock()
                fake_js = MagicMock()
                fake_js.publish = AsyncMock()
                bus._event_store = fake_event_store  # type: ignore[attr-defined]
                bus._js = fake_js  # type: ignore[attr-defined]
                bus._persistence_mode = "strict"  # type: ignore[attr-defined]

                envelope = _make_envelope(topic)
                await bus.publish_envelope(envelope)

                fake_js.publish.assert_not_awaited()
                fake_event_store.append.assert_called_once()


if __name__ == "__main__":
    unittest.main()
