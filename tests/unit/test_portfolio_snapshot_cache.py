"""Stage 6 Slice 6.3 单元测试：PortfolioSnapshotCache。

设计文档
========
docs/task/stage_6_slice_6_3_portfolio_snapshot_design.md §7.1

覆盖范围
========
- bootstrap：从 Redis 空 / 命中 / 失败 / parse 失败 + 订阅 NATS topic
- publish：本地 dict + Redis 双写、Redis 失败 best-effort、新 ts 覆盖、旧 ts noop
- get_sync：命中 / 未命中 / 多 scope 隔离
- _handle_remote_event：apply 新事件 / skip 旧事件 / parse 失败不抛
- snapshot() 自省

不在本测试范围：
- 真实 Redis 后端 → 集成测试 (test_portfolio_snapshot_cache_cross_process.py)
- 4 进程跨 NATS 跨容器 → 真跑验证（runbook §11）
"""
from __future__ import annotations

import logging
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from aats.bus.memory_bus import InMemoryEventBus
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.common import EventEnvelope
from aats.schemas.portfolio import PortfolioSnapshot
from aats.services.portfolio_service.snapshot_cache import (
    PORTFOLIO_SNAPSHOT_EVENT_TYPE,
    PortfolioSnapshotCache,
)
from aats.services.runtime_scope import RuntimeStateScope
from aats.storage.hot_state_store import InMemoryHotStateStore


# ─────────────────────────────────────────────────────────────────────
# Test 双件
# ─────────────────────────────────────────────────────────────────────


class _ExplodingHotStateStore(InMemoryHotStateStore):
    """所有 set 都抛异常的 HotStateStore，用于测试 best-effort 写。"""

    def __init__(self, *, raise_on_set: bool = True, raise_on_get: bool = False) -> None:
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


def _make_logger() -> logging.Logger:
    logger = logging.getLogger("test.portfolio_snapshot_cache")
    logger.setLevel(logging.DEBUG)
    return logger


def _make_scope(
    *,
    product_type: str = "spot",
    margin_mode: str = "cash",
    default_symbol: str = "BTC-USDT",
) -> RuntimeStateScope:
    return RuntimeStateScope(
        product_type=product_type,  # type: ignore[arg-type]
        margin_mode=margin_mode,  # type: ignore[arg-type]
        allowed_symbols=(default_symbol,),
        default_symbol=default_symbol,
    )


def _make_snapshot(
    *,
    snapshot_ts: datetime | None = None,
    product_type: str = "spot",
    margin_mode: str = "cash",
    decision_id: str | None = "decision-test-001",
    total_equity: str = "12345.67",
) -> PortfolioSnapshot:
    if snapshot_ts is None:
        snapshot_ts = datetime(2026, 4, 8, 12, 0, 0, tzinfo=timezone.utc)
    return PortfolioSnapshot(
        decision_id=decision_id,
        snapshot_ts=snapshot_ts,
        balances={"USDT": Decimal(total_equity)},
        positions=[],
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        total_equity=Decimal(total_equity),
        gross_exposure=Decimal("0"),
        net_exposure=Decimal("0"),
        product_type=product_type,  # type: ignore[arg-type]
        margin_mode=margin_mode,  # type: ignore[arg-type]
    )


def _make_cache(
    *,
    process_role: str = "gateway",
    hot_state_store: InMemoryHotStateStore | None = None,
    bus: InMemoryEventBus | None = None,
) -> tuple[PortfolioSnapshotCache, InMemoryHotStateStore, InMemoryEventBus]:
    store = hot_state_store or InMemoryHotStateStore()
    bus_obj = bus if bus is not None else InMemoryEventBus()
    cache = PortfolioSnapshotCache(
        hot_state_store=store,
        bus=bus_obj,
        process_role=process_role,
        logger=_make_logger(),
    )
    return cache, store, bus_obj


def _make_remote_message(snapshot: PortfolioSnapshot) -> dict[str, Any]:
    """模拟 outbox publisher 发到 NATS 后的 envelope dict。"""
    envelope = build_envelope(
        topic=topics.PORTFOLIO_SNAPSHOTS,
        key=snapshot.decision_id or "test",
        payload_model=snapshot,
        source_component="aats.portfolio.outbox",
    )
    return {
        "topic": topics.PORTFOLIO_SNAPSHOTS,
        "key": snapshot.decision_id or "test",
        "payload": envelope.model_dump(mode="json"),
    }


# ─────────────────────────────────────────────────────────────────────
# bootstrap (4 用例)
# ─────────────────────────────────────────────────────────────────────


class TestPortfolioSnapshotCacheBootstrap(unittest.IsolatedAsyncioTestCase):
    async def test_bootstrap_from_empty_redis_keeps_local_dict_empty(self) -> None:
        cache, _store, _bus = _make_cache()
        await cache.bootstrap(scope_fingerprint="spot:cash")
        snap = cache.snapshot()
        self.assertTrue(snap["bootstrapped"])
        self.assertTrue(snap["subscribed"])
        self.assertEqual(snap["cached_scopes"], [])
        self.assertEqual(snap["bootstrapped_scopes"], ["spot:cash"])

    async def test_bootstrap_hydrates_snapshot_from_redis(self) -> None:
        cache, store, _bus = _make_cache()
        seeded = _make_snapshot(
            snapshot_ts=datetime(2026, 4, 8, 11, 30, 0, tzinfo=timezone.utc),
            decision_id="hydrated-001",
        )
        await store.set(
            cache._key_for("spot:cash"),
            seeded.model_dump(mode="json"),
        )
        await cache.bootstrap(scope_fingerprint="spot:cash")
        cached = cache.get_sync(_make_scope())
        self.assertIsNotNone(cached)
        assert cached is not None
        self.assertEqual(cached.decision_id, "hydrated-001")
        self.assertEqual(cached.snapshot_ts, seeded.snapshot_ts)

    async def test_bootstrap_redis_get_failure_does_not_raise(self) -> None:
        store = _ExplodingHotStateStore(raise_on_set=False, raise_on_get=True)
        cache, _, _bus = _make_cache(hot_state_store=store)
        # 不能抛
        await cache.bootstrap(scope_fingerprint="spot:cash")
        # 仍订阅成功
        snap = cache.snapshot()
        self.assertTrue(snap["bootstrapped"])
        self.assertTrue(snap["subscribed"])
        # 本地 dict 仍然空
        self.assertEqual(snap["cached_scopes"], [])

    async def test_bootstrap_subscribes_to_portfolio_snapshots_topic(self) -> None:
        cache, _store, bus = _make_cache()
        await cache.bootstrap(scope_fingerprint="spot:cash")
        # InMemoryEventBus._subs[topic] 应该有 1 个 handler
        self.assertEqual(len(bus._subs[topics.PORTFOLIO_SNAPSHOTS]), 1)


# ─────────────────────────────────────────────────────────────────────
# publish (4 用例)
# ─────────────────────────────────────────────────────────────────────


class TestPortfolioSnapshotCachePublish(unittest.IsolatedAsyncioTestCase):
    async def test_publish_updates_local_dict_and_redis(self) -> None:
        cache, store, _bus = _make_cache()
        await cache.bootstrap(scope_fingerprint="spot:cash")
        snapshot = _make_snapshot(decision_id="publish-001")

        await cache.publish(snapshot)

        # 本地 dict 立即可见
        cached = cache.get_sync(_make_scope())
        self.assertIsNotNone(cached)
        assert cached is not None
        self.assertEqual(cached.decision_id, "publish-001")
        # Redis 也写入了
        stored = await store.get(cache._key_for("spot:cash"))
        self.assertIsNotNone(stored)
        assert isinstance(stored, dict)
        self.assertEqual(stored["decision_id"], "publish-001")

    async def test_publish_redis_failure_still_updates_local_dict(self) -> None:
        store = _ExplodingHotStateStore(raise_on_set=True)
        cache, _, _bus = _make_cache(hot_state_store=store)
        await cache.bootstrap(scope_fingerprint="spot:cash")
        snapshot = _make_snapshot(decision_id="publish-redis-fail")

        # 不抛
        await cache.publish(snapshot)

        # 本地 cache 仍然有
        cached = cache.get_sync(_make_scope())
        self.assertIsNotNone(cached)
        assert cached is not None
        self.assertEqual(cached.decision_id, "publish-redis-fail")

    async def test_publish_fresher_snapshot_overwrites_older(self) -> None:
        cache, _store, _bus = _make_cache()
        await cache.bootstrap(scope_fingerprint="spot:cash")
        old = _make_snapshot(
            snapshot_ts=datetime(2026, 4, 8, 11, 0, 0, tzinfo=timezone.utc),
            decision_id="old",
        )
        new = _make_snapshot(
            snapshot_ts=datetime(2026, 4, 8, 12, 0, 0, tzinfo=timezone.utc),
            decision_id="new",
        )
        await cache.publish(old)
        await cache.publish(new)

        cached = cache.get_sync(_make_scope())
        assert cached is not None
        self.assertEqual(cached.decision_id, "new")
        self.assertEqual(cached.snapshot_ts, new.snapshot_ts)

    async def test_publish_stale_snapshot_is_noop(self) -> None:
        """D6: 远端 / 重复 publish 一个比本地更老的 snapshot → noop。"""
        cache, _store, _bus = _make_cache()
        await cache.bootstrap(scope_fingerprint="spot:cash")
        new = _make_snapshot(
            snapshot_ts=datetime(2026, 4, 8, 12, 0, 0, tzinfo=timezone.utc),
            decision_id="newer",
        )
        old = _make_snapshot(
            snapshot_ts=datetime(2026, 4, 8, 11, 0, 0, tzinfo=timezone.utc),
            decision_id="older_should_not_overwrite",
        )
        await cache.publish(new)
        await cache.publish(old)

        cached = cache.get_sync(_make_scope())
        assert cached is not None
        self.assertEqual(cached.decision_id, "newer")


# ─────────────────────────────────────────────────────────────────────
# get_sync (3 用例)
# ─────────────────────────────────────────────────────────────────────


class TestPortfolioSnapshotCacheGetSync(unittest.IsolatedAsyncioTestCase):
    async def test_get_sync_returns_cached_snapshot(self) -> None:
        cache, _store, _bus = _make_cache()
        await cache.bootstrap(scope_fingerprint="spot:cash")
        snapshot = _make_snapshot(decision_id="get-sync-001")
        await cache.publish(snapshot)

        result = cache.get_sync(_make_scope())
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.decision_id, "get-sync-001")

    async def test_get_sync_returns_none_on_miss(self) -> None:
        cache, _store, _bus = _make_cache()
        await cache.bootstrap(scope_fingerprint="spot:cash")
        # 未 publish 任何 snapshot
        result = cache.get_sync(_make_scope())
        self.assertIsNone(result)

    async def test_get_sync_isolates_scopes(self) -> None:
        """I9：不同 product_type/margin_mode 的 snapshot 互不污染。"""
        cache, _store, _bus = _make_cache()
        # 两个不同 scope 都 bootstrap
        await cache.bootstrap(scope_fingerprint="spot:cash")
        await cache.bootstrap(scope_fingerprint="derivatives:isolated")

        spot_snapshot = _make_snapshot(
            decision_id="spot-decision",
            product_type="spot",
            margin_mode="cash",
        )
        deriv_snapshot = _make_snapshot(
            decision_id="deriv-decision",
            product_type="derivatives",
            margin_mode="isolated",
        )
        await cache.publish(spot_snapshot)
        await cache.publish(deriv_snapshot)

        spot_scope = _make_scope(product_type="spot", margin_mode="cash")
        deriv_scope = _make_scope(product_type="derivatives", margin_mode="isolated")
        spot_cached = cache.get_sync(spot_scope)
        deriv_cached = cache.get_sync(deriv_scope)
        assert spot_cached is not None and deriv_cached is not None
        self.assertEqual(spot_cached.decision_id, "spot-decision")
        self.assertEqual(deriv_cached.decision_id, "deriv-decision")


# ─────────────────────────────────────────────────────────────────────
# _handle_remote_event (3 用例)
# ─────────────────────────────────────────────────────────────────────


class TestPortfolioSnapshotCacheRemoteEvent(unittest.IsolatedAsyncioTestCase):
    async def test_remote_event_applies_new_snapshot(self) -> None:
        cache, _store, _bus = _make_cache(process_role="decision")
        await cache.bootstrap(scope_fingerprint="spot:cash")
        snapshot = _make_snapshot(
            snapshot_ts=datetime(2026, 4, 8, 12, 0, 0, tzinfo=timezone.utc),
            decision_id="remote-applied",
        )
        await cache._handle_remote_event(_make_remote_message(snapshot))

        cached = cache.get_sync(_make_scope())
        assert cached is not None
        self.assertEqual(cached.decision_id, "remote-applied")
        self.assertEqual(cached.snapshot_ts, snapshot.snapshot_ts)

    async def test_remote_event_skips_stale_snapshot(self) -> None:
        """D6/I8：远端 ts <= 本地 ts → noop。"""
        cache, _store, _bus = _make_cache(process_role="decision")
        await cache.bootstrap(scope_fingerprint="spot:cash")
        # 先 publish 一个 newer
        newer = _make_snapshot(
            snapshot_ts=datetime(2026, 4, 8, 12, 0, 0, tzinfo=timezone.utc),
            decision_id="newer",
        )
        await cache.publish(newer)

        # 收到一个更老的 remote event
        older = _make_snapshot(
            snapshot_ts=datetime(2026, 4, 8, 11, 0, 0, tzinfo=timezone.utc),
            decision_id="older_remote",
        )
        await cache._handle_remote_event(_make_remote_message(older))

        # 本地仍是 newer
        cached = cache.get_sync(_make_scope())
        assert cached is not None
        self.assertEqual(cached.decision_id, "newer")

    async def test_remote_event_with_invalid_payload_does_not_raise(self) -> None:
        cache, _store, _bus = _make_cache()
        await cache.bootstrap(scope_fingerprint="spot:cash")
        # 故意构造一个 missing required fields 的 envelope
        bad_envelope = EventEnvelope(
            event_type=PORTFOLIO_SNAPSHOT_EVENT_TYPE,
            source_component="aats.portfolio.outbox",
            topic=topics.PORTFOLIO_SNAPSHOTS,
            key="bad",
            payload={"this_is_not_a_portfolio_snapshot": True},
        )
        bad_message = {
            "topic": topics.PORTFOLIO_SNAPSHOTS,
            "key": "bad",
            "payload": bad_envelope.model_dump(mode="json"),
        }
        # 不抛
        await cache._handle_remote_event(bad_message)
        # 本地 dict 没有被污染
        self.assertIsNone(cache.get_sync(_make_scope()))


# ─────────────────────────────────────────────────────────────────────
# snapshot() introspection (1 用例)
# ─────────────────────────────────────────────────────────────────────


class TestPortfolioSnapshotCacheIntrospection(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_introspection_includes_state(self) -> None:
        cache, _store, _bus = _make_cache(process_role="execution")
        await cache.bootstrap(scope_fingerprint="spot:cash")
        await cache.publish(_make_snapshot(decision_id="introspect"))

        snap = cache.snapshot()
        self.assertEqual(snap["process_role"], "execution")
        self.assertTrue(snap["bootstrapped"])
        self.assertTrue(snap["subscribed"])
        self.assertEqual(snap["cached_scopes"], ["spot:cash"])
        self.assertEqual(snap["bootstrapped_scopes"], ["spot:cash"])


if __name__ == "__main__":
    unittest.main()
