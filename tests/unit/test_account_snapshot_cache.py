"""跨进程 account snapshot 缓存边车单元测试。

覆盖范围
========
- bootstrap：空 / Redis hydrate / Redis 失败 / parse 失败
- publish：本地 + Redis + NATS 广播；idempotent fetched_at 规则
- _handle_remote_event：apply 新事件 / skip 旧事件 / parse 失败
- get_sync / latest：未 bootstrap -> None；bootstrap 后正常
- snapshot() 自省
- register_remote_subscription deferred 路径
- set_on_snapshot_updated listener 回调
- best-effort Redis/NATS 失败不阻塞

不在本测试范围：
- 真实 Redis / NATS 后端 -> 集成测试
- 4 进程跨容器广播 -> 部署验证
"""
from __future__ import annotations

import logging
import unittest
from datetime import datetime, timezone
from typing import Any

from aats.bus.memory_bus import InMemoryEventBus
from aats.events import topics
from aats.schemas.common import EventEnvelope
from aats.schemas.exchange import ExchangeAccountSnapshot
from aats.services.execution_engine.account_snapshot_cache import (
    ACCOUNT_SNAPSHOT_EVENT_TYPE,
    ACCOUNT_SNAPSHOT_SOURCE_COMPONENT,
    AccountSnapshotCache,
    _redis_key,
)
from aats.storage.hot_state_store import InMemoryHotStateStore


# ─────────────────────────────────────────────────────────────────────
# Test doubles
# ─────────────────────────────────────────────────────────────────────


class _ExplodingHotStateStore(InMemoryHotStateStore):
    """所有 set 都抛异常的 HotStateStore。"""

    def __init__(
        self,
        *,
        raise_on_set: bool = True,
        raise_on_get: bool = False,
    ) -> None:
        super().__init__()
        self._raise_on_set = raise_on_set
        self._raise_on_get = raise_on_get

    async def get(self, key: str) -> Any | None:
        if self._raise_on_get:
            raise RuntimeError("redis_get_boom")
        return await super().get(key)

    async def set(
        self,
        key: str,
        value: Any,
        *,
        ttl_seconds: float | None = None,
    ) -> None:
        if self._raise_on_set:
            raise RuntimeError("redis_set_boom")
        await super().set(key, value, ttl_seconds=ttl_seconds)


class _ExplodingBus(InMemoryEventBus):
    """publish 抛异常的 bus。"""

    async def publish(self, *, topic: str, key: str, payload: Any) -> None:  # type: ignore[override]
        raise RuntimeError("nats_publish_boom")


def _make_logger() -> logging.Logger:
    logger = logging.getLogger("test.account_snapshot_cache")
    logger.setLevel(logging.DEBUG)
    return logger


def _make_snapshot(
    *,
    fetched_at: datetime | None = None,
    account_source: str = "okx",
) -> ExchangeAccountSnapshot:
    if fetched_at is None:
        fetched_at = datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)
    return ExchangeAccountSnapshot(
        account_source=account_source,
        fetched_at=fetched_at,
        balances=[],
        positions=[],
        open_orders=[],
        fills=[],
        instruments=[],
        raw={
            "balance": {"code": "0", "data": [{"totalEq": "10000"}]},
            "funding_rate_by_symbol": {"BTC-USDT-SWAP": {"fundingRate": "0.0001"}},
        },
    )


def _make_cache() -> AccountSnapshotCache:
    return AccountSnapshotCache(logger=_make_logger())


async def _boot(
    cache: AccountSnapshotCache,
    *,
    store: InMemoryHotStateStore | None = None,
    bus: InMemoryEventBus | None = None,
    process_role: str = "execution",
    subscribe: bool = True,
) -> tuple[InMemoryHotStateStore, InMemoryEventBus]:
    store_obj = store if store is not None else InMemoryHotStateStore()
    bus_obj = bus if bus is not None else InMemoryEventBus()
    # InMemoryEventBus 不需要 start()
    await cache.bootstrap(
        hot_state_store=store_obj,
        bus=bus_obj,
        process_role=process_role,
        subscribe=subscribe,
    )
    return store_obj, bus_obj


def _make_remote_message(
    snapshot: ExchangeAccountSnapshot,
    *,
    recent_bills: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """构造模拟 NATS 收到的 message payload（新格式包含 bills）。

    与 ``_build_broadcast_payload`` 保持一致：白名单裁剪 raw，
    仅保留 ``funding_rate_by_symbol``。
    """
    snapshot_data = snapshot.model_dump(mode="json")
    raw = snapshot_data.pop("raw", None)
    if isinstance(raw, dict):
        whitelisted_raw: dict[str, Any] = {}
        funding_rate = raw.get("funding_rate_by_symbol")
        if funding_rate is not None:
            whitelisted_raw["funding_rate_by_symbol"] = funding_rate
        if whitelisted_raw:
            snapshot_data["raw"] = whitelisted_raw
    payload_data = {
        "snapshot": snapshot_data,
        "recent_bills": recent_bills or [],
    }
    envelope = EventEnvelope(
        event_type=ACCOUNT_SNAPSHOT_EVENT_TYPE,
        source_component=ACCOUNT_SNAPSHOT_SOURCE_COMPONENT,
        topic=topics.ACCOUNT_SNAPSHOTS,
        key="latest",
        payload=payload_data,
    )
    return {"payload": envelope.model_dump(mode="json")}


# ─────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────


class TestBootstrapEmpty(unittest.IsolatedAsyncioTestCase):
    async def test_empty_redis_bootstrap(self) -> None:
        cache = _make_cache()
        store, bus = await _boot(cache, process_role="gateway")
        self.assertIsNone(cache.get_sync())
        self.assertIsNone(cache.latest)
        snap = cache.snapshot()
        self.assertTrue(snap["bootstrapped"])
        self.assertFalse(snap["has_snapshot"])

    async def test_redis_failure_bootstrap_does_not_raise(self) -> None:
        cache = _make_cache()
        store = _ExplodingHotStateStore(raise_on_get=True)
        await _boot(cache, store=store, process_role="gateway")
        self.assertIsNone(cache.get_sync())
        # bootstrapped 仍为 True（fail-soft）
        self.assertTrue(cache.snapshot()["bootstrapped"])


class TestBootstrapHydrate(unittest.IsolatedAsyncioTestCase):
    async def test_redis_hydrate(self) -> None:
        snapshot = _make_snapshot()
        store = InMemoryHotStateStore()
        # 预先写入 Redis（新格式：白名单裁剪后的 snapshot + recent_bills）
        snapshot_data = snapshot.model_dump(mode="json")
        raw = snapshot_data.pop("raw", None)
        if isinstance(raw, dict):
            funding = raw.get("funding_rate_by_symbol")
            if funding is not None:
                snapshot_data["raw"] = {"funding_rate_by_symbol": funding}
        bills = [{"billId": "1001", "type": "8", "instId": "BTC-USDT-SWAP"}]
        await store.set(_redis_key(), {"snapshot": snapshot_data, "recent_bills": bills})

        cache = _make_cache()
        await _boot(cache, store=store, process_role="decision")

        result = cache.get_sync()
        self.assertIsNotNone(result)
        self.assertEqual(result.fetched_at, snapshot.fetched_at)
        self.assertEqual(result.account_source, "okx")
        self.assertEqual(len(cache.recent_bills), 1)
        self.assertEqual(cache.recent_bills[0]["billId"], "1001")

    async def test_redis_hydrate_backward_compat_old_format(self) -> None:
        """兼容旧格式：Redis 中只有 bare snapshot dict（无 wrapper / 完整剥离 raw）。"""
        snapshot = _make_snapshot()
        store = InMemoryHotStateStore()
        payload = snapshot.model_dump(mode="json")
        payload.pop("raw", None)  # 旧版完整剥离
        await store.set(_redis_key(), payload)

        cache = _make_cache()
        await _boot(cache, store=store, process_role="decision")

        result = cache.get_sync()
        self.assertIsNotNone(result)
        self.assertEqual(result.fetched_at, snapshot.fetched_at)
        self.assertEqual(len(cache.recent_bills), 0)

    async def test_redis_parse_failure_does_not_raise(self) -> None:
        store = InMemoryHotStateStore()
        await store.set(_redis_key(), {"invalid": "data"})

        cache = _make_cache()
        await _boot(cache, store=store, process_role="gateway")
        # parse 失败 -> 仍然 bootstrapped, 但没有 snapshot
        self.assertIsNone(cache.get_sync())
        self.assertTrue(cache.snapshot()["bootstrapped"])


class TestPublish(unittest.IsolatedAsyncioTestCase):
    async def test_publish_updates_local_and_redis(self) -> None:
        cache = _make_cache()
        store, bus = await _boot(cache, process_role="execution")

        snapshot = _make_snapshot()
        bills = [{"billId": "2001", "type": "8"}]
        await cache.publish(snapshot, recent_bills=bills)

        # 本地更新
        self.assertIs(cache.get_sync(), snapshot)
        self.assertEqual(len(cache.recent_bills), 1)
        # Redis 写入（wrapper 格式，raw 白名单裁剪）
        stored = await store.get(_redis_key())
        self.assertIsNotNone(stored)
        self.assertIn("snapshot", stored)
        self.assertIn("raw", stored["snapshot"])
        self.assertIn("funding_rate_by_symbol", stored["snapshot"]["raw"])
        self.assertNotIn("balance", stored["snapshot"]["raw"])
        self.assertEqual(stored["snapshot"]["account_source"], "okx")
        self.assertEqual(len(stored["recent_bills"]), 1)

    async def test_publish_broadcasts_nats(self) -> None:
        cache = _make_cache()
        store, bus = await _boot(cache, process_role="execution")

        received: list[dict] = []

        async def handler(msg: dict) -> None:
            received.append(msg)

        await bus.subscribe(topics.ACCOUNT_SNAPSHOTS, handler)

        snapshot = _make_snapshot()
        await cache.publish(snapshot)

        self.assertEqual(len(received), 1)

    async def test_publish_idempotent_stale(self) -> None:
        cache = _make_cache()
        store, bus = await _boot(cache, process_role="execution")

        newer = _make_snapshot(
            fetched_at=datetime(2026, 4, 10, 13, 0, 0, tzinfo=timezone.utc)
        )
        older = _make_snapshot(
            fetched_at=datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)
        )

        await cache.publish(newer)
        await cache.publish(older)  # should be noop

        self.assertEqual(cache.get_sync().fetched_at, newer.fetched_at)

    async def test_publish_idempotent_equal_ts(self) -> None:
        cache = _make_cache()
        store, bus = await _boot(cache, process_role="execution")

        ts = datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)
        snap1 = _make_snapshot(fetched_at=ts)
        snap2 = _make_snapshot(fetched_at=ts)

        await cache.publish(snap1)
        await cache.publish(snap2)  # equal ts -> noop

        self.assertIs(cache.get_sync(), snap1)

    async def test_publish_redis_failure_best_effort(self) -> None:
        cache = _make_cache()
        store = _ExplodingHotStateStore(raise_on_set=True)
        await _boot(cache, store=store, process_role="execution")

        snapshot = _make_snapshot()
        # 不抛异常
        await cache.publish(snapshot)
        # 本地仍然更新了
        self.assertEqual(cache.get_sync().fetched_at, snapshot.fetched_at)

    async def test_publish_nats_failure_best_effort(self) -> None:
        cache = _make_cache()
        bus = _ExplodingBus()
        await _boot(cache, bus=bus, process_role="execution")

        snapshot = _make_snapshot()
        # 不抛异常
        await cache.publish(snapshot)
        # 本地仍然更新了
        self.assertEqual(cache.get_sync().fetched_at, snapshot.fetched_at)


class TestRemoteEvent(unittest.IsolatedAsyncioTestCase):
    async def test_handle_remote_event_apply(self) -> None:
        cache = _make_cache()
        await _boot(cache, process_role="gateway")

        snapshot = _make_snapshot()
        bills = [{"billId": "3001", "type": "8"}]
        msg = _make_remote_message(snapshot, recent_bills=bills)
        await cache._handle_remote_event(msg)

        result = cache.get_sync()
        self.assertIsNotNone(result)
        self.assertEqual(result.fetched_at, snapshot.fetched_at)
        # raw 白名单裁剪：仅保留 funding_rate_by_symbol，balance 等被剥离
        self.assertIn("funding_rate_by_symbol", result.raw)
        self.assertNotIn("balance", result.raw)
        # bills 也同步了
        self.assertEqual(len(cache.recent_bills), 1)
        self.assertEqual(cache.recent_bills[0]["billId"], "3001")

    async def test_handle_remote_event_stale_skip(self) -> None:
        cache = _make_cache()
        await _boot(cache, process_role="gateway")

        newer = _make_snapshot(
            fetched_at=datetime(2026, 4, 10, 13, 0, 0, tzinfo=timezone.utc)
        )
        older = _make_snapshot(
            fetched_at=datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)
        )

        await cache._handle_remote_event(_make_remote_message(newer))
        await cache._handle_remote_event(_make_remote_message(older))

        self.assertEqual(cache.get_sync().fetched_at, newer.fetched_at)

    async def test_handle_remote_event_parse_failure_no_raise(self) -> None:
        cache = _make_cache()
        await _boot(cache, process_role="gateway")

        # 传入损坏的 message
        await cache._handle_remote_event({"payload": {"bad": "data"}})
        self.assertIsNone(cache.get_sync())

    async def test_handle_remote_event_triggers_listener(self) -> None:
        cache = _make_cache()
        await _boot(cache, process_role="gateway")

        received: list[tuple[ExchangeAccountSnapshot, list]] = []
        cache.set_on_state_updated(lambda snap, bills: received.append((snap, bills)))

        snapshot = _make_snapshot()
        bills = [{"billId": "4001"}]
        await cache._handle_remote_event(_make_remote_message(snapshot, recent_bills=bills))

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][0].fetched_at, snapshot.fetched_at)
        self.assertEqual(len(received[0][1]), 1)

    async def test_backward_compat_set_on_snapshot_updated(self) -> None:
        """旧接口 set_on_snapshot_updated 仍然可用。"""
        cache = _make_cache()
        await _boot(cache, process_role="gateway")

        received: list[ExchangeAccountSnapshot] = []
        cache.set_on_snapshot_updated(lambda snap: received.append(snap))

        snapshot = _make_snapshot()
        await cache._handle_remote_event(_make_remote_message(snapshot))

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].fetched_at, snapshot.fetched_at)

    async def test_listener_failure_does_not_propagate(self) -> None:
        cache = _make_cache()
        await _boot(cache, process_role="gateway")

        def exploding_listener(snap: ExchangeAccountSnapshot, bills: list) -> None:
            raise RuntimeError("listener_boom")

        cache.set_on_state_updated(exploding_listener)

        snapshot = _make_snapshot()
        # 不抛异常
        await cache._handle_remote_event(_make_remote_message(snapshot))
        # snapshot 仍然更新了
        self.assertEqual(cache.get_sync().fetched_at, snapshot.fetched_at)


class TestDeferredSubscription(unittest.IsolatedAsyncioTestCase):
    async def test_subscribe_false_then_register(self) -> None:
        cache = _make_cache()
        store = InMemoryHotStateStore()
        bus = InMemoryEventBus()
        # InMemoryEventBus 不需要 start()

        await cache.bootstrap(
            hot_state_store=store,
            bus=bus,
            process_role="decision",
            subscribe=False,
        )
        self.assertFalse(cache.snapshot()["subscribed"])

        await cache.register_remote_subscription(bus)
        self.assertTrue(cache.snapshot()["subscribed"])


class TestEndToEndNats(unittest.IsolatedAsyncioTestCase):
    """端到端：publish -> NATS -> subscribe handler -> cache update。"""

    async def test_nats_round_trip(self) -> None:
        # execution 侧 cache
        exec_cache = _make_cache()
        store = InMemoryHotStateStore()
        bus = InMemoryEventBus()
        # InMemoryEventBus 不需要 start()
        await exec_cache.bootstrap(
            hot_state_store=store, bus=bus, process_role="execution"
        )

        # gateway 侧 cache
        gw_cache = _make_cache()
        await gw_cache.bootstrap(
            hot_state_store=store, bus=bus, process_role="gateway"
        )

        snapshot = _make_snapshot()
        bills = [{"billId": "5001", "type": "8"}]
        await exec_cache.publish(snapshot, recent_bills=bills)

        # InMemoryEventBus publish 是同步 fan-out，gateway cache 应该已经收到
        result = gw_cache.get_sync()
        self.assertIsNotNone(result)
        self.assertEqual(result.fetched_at, snapshot.fetched_at)
        self.assertEqual(len(gw_cache.recent_bills), 1)

    async def test_nats_round_trip_with_listener(self) -> None:
        """模拟完整流程：execution publish -> gateway cache -> account_service hydrate。"""
        store = InMemoryHotStateStore()
        bus = InMemoryEventBus()
        # InMemoryEventBus 不需要 start()

        # execution
        exec_cache = _make_cache()
        await exec_cache.bootstrap(
            hot_state_store=store, bus=bus, process_role="execution"
        )

        # gateway: 模拟 account_service
        class FakeAccountService:
            _latest_snapshot: ExchangeAccountSnapshot | None = None
            _latest_recent_bills: list[dict[str, Any]] = []

        fake_svc = FakeAccountService()
        gw_cache = _make_cache()
        await gw_cache.bootstrap(
            hot_state_store=store, bus=bus, process_role="gateway"
        )

        def _sync_listener(snap: ExchangeAccountSnapshot, bills: list) -> None:
            fake_svc._latest_snapshot = snap
            fake_svc._latest_recent_bills = list(bills)

        gw_cache.set_on_state_updated(_sync_listener)

        self.assertIsNone(fake_svc._latest_snapshot)
        self.assertEqual(len(fake_svc._latest_recent_bills), 0)

        snapshot = _make_snapshot()
        bills = [{"billId": "6001", "type": "8", "instId": "BTC-USDT-SWAP"}]
        await exec_cache.publish(snapshot, recent_bills=bills)

        # gateway 的 account_service 现在有 snapshot + bills 了
        self.assertIsNotNone(fake_svc._latest_snapshot)
        self.assertEqual(fake_svc._latest_snapshot.fetched_at, snapshot.fetched_at)
        self.assertEqual(len(fake_svc._latest_recent_bills), 1)
        self.assertEqual(fake_svc._latest_recent_bills[0]["billId"], "6001")


class TestSnapshot(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_fields(self) -> None:
        cache = _make_cache()
        await _boot(cache, process_role="market")

        snap = cache.snapshot()
        self.assertTrue(snap["bootstrapped"])
        self.assertFalse(snap["has_snapshot"])
        self.assertEqual(snap["process_role"], "market")
        self.assertIsNone(snap["fetched_at"])

        await cache.publish(_make_snapshot())
        snap = cache.snapshot()
        self.assertTrue(snap["has_snapshot"])
        self.assertIsNotNone(snap["fetched_at"])


class TestPublishRawWhitelist(unittest.IsolatedAsyncioTestCase):
    """验证 broadcast payload 的 raw 白名单裁剪行为 (D7)。

    - funding_rate_by_symbol 保留（非 execution 角色的 funding_schedule() 依赖）
    - balance 等大体积原始响应剥离
    """

    async def test_redis_payload_whitelists_funding_rate(self) -> None:
        cache = _make_cache()
        store, bus = await _boot(cache, process_role="execution")

        snapshot = _make_snapshot()
        self.assertIn("balance", snapshot.raw)
        self.assertIn("funding_rate_by_symbol", snapshot.raw)

        await cache.publish(snapshot)

        stored = await store.get(_redis_key())
        self.assertIn("snapshot", stored)
        snap_data = stored["snapshot"]
        # 白名单保留 funding_rate_by_symbol
        self.assertIn("raw", snap_data)
        self.assertIn("funding_rate_by_symbol", snap_data["raw"])
        self.assertEqual(
            snap_data["raw"]["funding_rate_by_symbol"],
            {"BTC-USDT-SWAP": {"fundingRate": "0.0001"}},
        )
        # 非白名单 key 被剥离
        self.assertNotIn("balance", snap_data["raw"])

    async def test_nats_broadcast_whitelists_funding_rate(self) -> None:
        cache = _make_cache()
        store, bus = await _boot(cache, process_role="execution")

        received: list[dict] = []

        async def handler(msg: dict) -> None:
            received.append(msg)

        await bus.subscribe(topics.ACCOUNT_SNAPSHOTS, handler)

        snapshot = _make_snapshot()
        await cache.publish(snapshot)

        self.assertEqual(len(received), 1)
        envelope_payload = received[0]["payload"]
        snap_data = envelope_payload["payload"]["snapshot"]
        # 白名单保留
        self.assertIn("raw", snap_data)
        self.assertIn("funding_rate_by_symbol", snap_data["raw"])
        # 非白名单剥离
        self.assertNotIn("balance", snap_data["raw"])

    async def test_broadcast_no_funding_rate_strips_raw_entirely(self) -> None:
        """raw 中没有 funding_rate_by_symbol 时整个 raw 被剥离。"""
        snapshot = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc),
            balances=[],
            positions=[],
            open_orders=[],
            fills=[],
            instruments=[],
            raw={"balance": {"code": "0"}},
        )
        cache = _make_cache()
        store, bus = await _boot(cache, process_role="execution")

        await cache.publish(snapshot)

        stored = await store.get(_redis_key())
        self.assertNotIn("raw", stored["snapshot"])

    async def test_end_to_end_funding_rate_preserved_cross_process(self) -> None:
        """publish -> NATS -> gateway hydrate，funding_rate_by_symbol 存活。"""
        store = InMemoryHotStateStore()
        bus = InMemoryEventBus()

        exec_cache = _make_cache()
        await exec_cache.bootstrap(
            hot_state_store=store, bus=bus, process_role="execution",
        )

        gw_cache = _make_cache()
        await gw_cache.bootstrap(
            hot_state_store=store, bus=bus, process_role="gateway",
        )

        snapshot = _make_snapshot()
        await exec_cache.publish(snapshot)

        result = gw_cache.get_sync()
        self.assertIsNotNone(result)
        # funding_rate_by_symbol 跨进程保留
        self.assertIn("funding_rate_by_symbol", result.raw)
        self.assertEqual(
            result.raw["funding_rate_by_symbol"],
            {"BTC-USDT-SWAP": {"fundingRate": "0.0001"}},
        )
        # balance 被剥离
        self.assertNotIn("balance", result.raw)


if __name__ == "__main__":
    unittest.main()
