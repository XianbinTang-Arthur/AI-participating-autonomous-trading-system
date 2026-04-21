"""Finding 3: GuardSignalHotStateCache 单元测试。

测试跨进程 guard signal 缓存的：
  - bootstrap 从 Redis 恢复
  - publish 写 local + Redis + NATS
  - snapshot 同步读取 + stale fail-closed
  - NATS 回调更新 + 幂等性（旧消息丢弃）
  - __call__ callable 接口（recovery_status_provider）
  - E2E: execution publish → NATS → decision read
"""
from __future__ import annotations

import asyncio
import unittest
from typing import Any

from aats.bus.memory_bus import InMemoryEventBus
from aats.services.governance_engine.guard_signal_cache import (
    GuardSignalHotStateCache,
    _FAIL_CLOSED_SENTINEL,
)
from aats.storage.hot_state_store import InMemoryHotStateStore

import logging


def _make_logger() -> logging.Logger:
    return logging.getLogger("test_guard_signal_cache")


class TestBootstrap(unittest.IsolatedAsyncioTestCase):
    """bootstrap() 生命周期测试。"""

    async def test_bootstrap_without_store(self) -> None:
        """无 hot_state_store 也不报错（退化为纯内存缓存）；快照返回 fail-closed。"""
        cache = GuardSignalHotStateCache(
            signal_name="derivatives_live",
            logger=_make_logger(),
        )
        await cache.bootstrap()
        self.assertTrue(cache.bootstrapped)
        snapshot = cache.snapshot()
        self.assertTrue(snapshot["only_reduce_required"])
        self.assertFalse(snapshot["safe_to_trade"])
        self.assertTrue(snapshot.get("_stale"))

    async def test_bootstrap_restores_from_redis(self) -> None:
        """bootstrap 从 Redis 恢复之前发布的快照。"""
        store = InMemoryHotStateStore()
        cache_writer = GuardSignalHotStateCache(
            signal_name="trial",
            logger=_make_logger(),
        )
        await cache_writer.bootstrap(hot_state_store=store, process_role="execution")
        await cache_writer.publish({"status": "clean", "breaches": 0})

        # 新实例 bootstrap 应该恢复
        cache_reader = GuardSignalHotStateCache(
            signal_name="trial",
            logger=_make_logger(),
        )
        await cache_reader.bootstrap(hot_state_store=store, process_role="decision")
        snapshot = cache_reader.snapshot()
        self.assertEqual(snapshot["status"], "clean")
        self.assertEqual(snapshot["breaches"], 0)

    async def test_bootstrap_with_empty_redis(self) -> None:
        """Redis 无数据时 bootstrap 成功，snapshot 返回 fail-closed sentinel。"""
        store = InMemoryHotStateStore()
        cache = GuardSignalHotStateCache(
            signal_name="recovery",
            logger=_make_logger(),
        )
        await cache.bootstrap(hot_state_store=store, process_role="decision")
        self.assertTrue(cache.bootstrapped)
        snapshot = cache.snapshot()
        self.assertTrue(snapshot["only_reduce_required"])
        self.assertFalse(snapshot["safe_to_trade"])


class TestPublish(unittest.IsolatedAsyncioTestCase):
    """publish() 写路径测试。"""

    async def test_publish_updates_local_and_redis(self) -> None:
        """publish 同时写 local dict 和 Redis。"""
        store = InMemoryHotStateStore()
        cache = GuardSignalHotStateCache(
            signal_name="derivatives_live",
            logger=_make_logger(),
        )
        await cache.bootstrap(hot_state_store=store, process_role="execution")

        await cache.publish({
            "status": "active",
            "only_reduce_required": False,
            "auto_halt_required": False,
            "risk_snapshot_stage": "ok",
        })

        # 本地读
        snapshot = cache.snapshot()
        self.assertEqual(snapshot["status"], "active")
        self.assertFalse(snapshot["only_reduce_required"])

        # Redis 读
        raw = await store.get(cache.redis_key)
        self.assertIsInstance(raw, dict)
        self.assertEqual(raw["status"], "active")
        self.assertIn("_cached_at", raw)

    async def test_publish_with_nats_broadcast(self) -> None:
        """publish 也 best-effort 广播到 NATS。"""
        store = InMemoryHotStateStore()
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")
        captured: list[dict] = []

        async def _collector(message: dict[str, Any]) -> None:
            captured.append(message)

        from aats.events import topics
        await bus.subscribe(topics.GUARD_SIGNAL_UPDATES, _collector)

        cache = GuardSignalHotStateCache(
            signal_name="trial",
            logger=_make_logger(),
        )
        await cache.bootstrap(
            hot_state_store=store, bus=bus, process_role="execution",
        )
        await cache.publish({"status": "breached", "breaches": 3})

        self.assertEqual(len(captured), 1)

    async def test_publish_without_store_still_works_locally(self) -> None:
        """无 Redis 时 publish 仍然更新本地 dict。"""
        cache = GuardSignalHotStateCache(
            signal_name="recovery",
            logger=_make_logger(),
        )
        await cache.bootstrap()
        await cache.publish({"safe_to_trade": True, "review_required": False})

        snapshot = cache.snapshot()
        self.assertTrue(snapshot["safe_to_trade"])


class TestSnapshot(unittest.IsolatedAsyncioTestCase):
    """snapshot() 读路径测试。"""

    async def test_snapshot_returns_copy_without_metadata(self) -> None:
        """snapshot 剥离 _ 开头的内部字段。"""
        cache = GuardSignalHotStateCache(
            signal_name="derivatives_live",
            logger=_make_logger(),
        )
        await cache.bootstrap()
        await cache.publish({"status": "active", "margin_usage": 0.35})

        snapshot = cache.snapshot()
        self.assertEqual(snapshot["status"], "active")
        self.assertNotIn("_cached_at", snapshot)
        self.assertNotIn("_signal_name", snapshot)
        self.assertNotIn("_writer_role", snapshot)

    async def test_snapshot_fail_closed_on_stale(self) -> None:
        """快照过期时返回 fail-closed sentinel（only_reduce=True, safe_to_trade=False）。"""
        cache = GuardSignalHotStateCache(
            signal_name="trial",
            logger=_make_logger(),
            stale_threshold_seconds=0.05,  # 50ms
        )
        await cache.bootstrap()
        await cache.publish({"status": "clean"})

        # 立刻读 → 有数据
        self.assertEqual(cache.snapshot()["status"], "clean")

        # 等超过 stale threshold → fail-closed
        await asyncio.sleep(0.1)
        stale = cache.snapshot()
        self.assertTrue(stale["only_reduce_required"])
        self.assertFalse(stale["safe_to_trade"])
        self.assertEqual(stale["status"], "stale")
        self.assertTrue(stale.get("_stale"))

    async def test_snapshot_empty_before_any_publish(self) -> None:
        """未 publish 过时 snapshot 返回 fail-closed sentinel。"""
        cache = GuardSignalHotStateCache(
            signal_name="recovery",
            logger=_make_logger(),
        )
        await cache.bootstrap()
        snapshot = cache.snapshot()
        self.assertTrue(snapshot["only_reduce_required"])
        self.assertFalse(snapshot["safe_to_trade"])
        self.assertTrue(snapshot.get("_stale"))

    async def test_callable_interface(self) -> None:
        """__call__ 与 snapshot 返回相同结果。"""
        cache = GuardSignalHotStateCache(
            signal_name="recovery",
            logger=_make_logger(),
        )
        await cache.bootstrap()
        await cache.publish({"safe_to_trade": True})

        self.assertEqual(cache(), cache.snapshot())
        self.assertTrue(cache()["safe_to_trade"])


class TestNATSSubscription(unittest.IsolatedAsyncioTestCase):
    """NATS 订阅回调测试。"""

    async def test_nats_update_refreshes_local_snapshot(self) -> None:
        """decision 侧通过 NATS 收到 execution 侧的更新。"""
        store = InMemoryHotStateStore()
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")

        # Execution 侧 publisher
        pub_cache = GuardSignalHotStateCache(
            signal_name="derivatives_live",
            logger=_make_logger(),
        )
        await pub_cache.bootstrap(
            hot_state_store=store, bus=bus, process_role="execution",
        )

        # Decision 侧 subscriber
        sub_cache = GuardSignalHotStateCache(
            signal_name="derivatives_live",
            logger=_make_logger(),
        )
        await sub_cache.bootstrap(
            hot_state_store=store, bus=bus,
            process_role="decision", subscribe=True,
        )

        # Execution 发布
        await pub_cache.publish({
            "status": "active",
            "only_reduce_required": True,
            "only_reduce_reasons": ["margin_critical"],
        })

        # Decision 侧应该通过 NATS 收到更新
        snapshot = sub_cache.snapshot()
        self.assertEqual(snapshot["status"], "active")
        self.assertTrue(snapshot["only_reduce_required"])

    async def test_nats_ignores_different_signal_name(self) -> None:
        """不同 signal_name 的消息被忽略。"""
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")

        # trial publisher
        trial_pub = GuardSignalHotStateCache(
            signal_name="trial",
            logger=_make_logger(),
        )
        await trial_pub.bootstrap(bus=bus, process_role="execution")

        # derivatives_live subscriber
        live_sub = GuardSignalHotStateCache(
            signal_name="derivatives_live",
            logger=_make_logger(),
        )
        await live_sub.bootstrap(bus=bus, process_role="decision", subscribe=True)

        await trial_pub.publish({"status": "breached"})

        # derivatives_live subscriber 不应收到 trial 的更新 → 仍是 fail-closed
        stale = live_sub.snapshot()
        self.assertTrue(stale["only_reduce_required"])
        self.assertTrue(stale.get("_stale"))

    async def test_nats_idempotent_stale_message_dropped(self) -> None:
        """旧时间戳的 NATS 消息被丢弃（幂等）。"""
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")

        pub_cache = GuardSignalHotStateCache(
            signal_name="recovery",
            logger=_make_logger(),
        )
        await pub_cache.bootstrap(bus=bus, process_role="execution")

        sub_cache = GuardSignalHotStateCache(
            signal_name="recovery",
            logger=_make_logger(),
        )
        await sub_cache.bootstrap(bus=bus, process_role="decision", subscribe=True)

        # 发布两条，第二条比第一条新
        await pub_cache.publish({"safe_to_trade": False, "version": 1})
        await asyncio.sleep(0.01)
        await pub_cache.publish({"safe_to_trade": True, "version": 2})

        # subscriber 应该持有最新版本
        self.assertTrue(sub_cache.snapshot()["safe_to_trade"])
        self.assertEqual(sub_cache.snapshot()["version"], 2)


class TestE2EPublishSubscribe(unittest.IsolatedAsyncioTestCase):
    """端到端测试：3 个信号的完整 publish → subscribe 流程。"""

    async def test_three_signals_e2e(self) -> None:
        """模拟 4-proc: execution 发布 3 个信号，decision 全部接收。"""
        store = InMemoryHotStateStore()
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")

        # Execution 侧：3 个 publisher
        exec_caches: dict[str, GuardSignalHotStateCache] = {}
        for name in ("derivatives_live", "trial", "recovery"):
            c = GuardSignalHotStateCache(
                signal_name=name,
                logger=_make_logger(),
            )
            await c.bootstrap(
                hot_state_store=store, bus=bus, process_role="execution",
            )
            exec_caches[name] = c

        # Decision 侧：3 个 subscriber
        dec_caches: dict[str, GuardSignalHotStateCache] = {}
        for name in ("derivatives_live", "trial", "recovery"):
            c = GuardSignalHotStateCache(
                signal_name=name,
                logger=_make_logger(),
            )
            await c.bootstrap(
                hot_state_store=store, bus=bus,
                process_role="decision", subscribe=True,
            )
            dec_caches[name] = c

        # Execution 发布
        await exec_caches["derivatives_live"].publish({
            "status": "active",
            "only_reduce_required": False,
            "auto_halt_required": False,
            "risk_snapshot_stage": "ok",
        })
        await exec_caches["trial"].publish({
            "status": "clean",
            "breaches": 0,
        })
        await exec_caches["recovery"].publish({
            "safe_to_trade": True,
            "review_required": False,
            "only_reduce_required": False,
        })

        # Decision 侧验证
        live = dec_caches["derivatives_live"].snapshot()
        self.assertEqual(live["status"], "active")
        self.assertFalse(live["auto_halt_required"])

        trial = dec_caches["trial"].snapshot()
        self.assertEqual(trial["status"], "clean")

        recovery = dec_caches["recovery"]()  # callable 接口
        self.assertTrue(recovery["safe_to_trade"])


class TestFailClosedSentinel(unittest.IsolatedAsyncioTestCase):
    """Fail-closed sentinel 回归测试。

    验证 guard 快照缺失/过期时 RiskEngine 读取到的字段值都是保守的，
    不会意外放行开仓。这是 P1 安全缺陷的回归防护。
    """

    def test_sentinel_has_conservative_fields(self) -> None:
        """_FAIL_CLOSED_SENTINEL 的字段值必须让 RiskEngine 拒绝开仓。"""
        s = _FAIL_CLOSED_SENTINEL
        # ── 路径 B：_adaptive_control_states 软约束（multiplier 压缩）──
        # bool(runtime_guard.get("only_reduce_required")) → True
        self.assertTrue(bool(s.get("only_reduce_required")))
        # bool(runtime_guard.get("auto_halt_required")) → False = 不暴力停车
        self.assertFalse(bool(s.get("auto_halt_required")))

        # ── 路径 A：_runtime_guard_only_reduce_reasons 硬拒绝 ──
        # payload.get("only_reduce_reasons") 必须非空，否则
        # _evaluate_derivatives_pretrade 的 if provider_only_reduce_reasons: 不成立
        # → 开仓不会被 reject → fail-open。
        reasons = [
            str(item)
            for item in (s.get("only_reduce_reasons") or [])
            if str(item).strip()
        ]
        self.assertTrue(
            len(reasons) > 0,
            "CRITICAL: sentinel must have only_reduce_reasons to trigger "
            "RiskEngine hard rejection path (_evaluate_derivatives_pretrade "
            "line 1526). Without reasons, only soft multiplier compression "
            "applies and opening intents can still be approved.",
        )

        # ── recovery provider 路径 ──
        # recovery_status.get("safe_to_trade", True) → False
        self.assertFalse(s.get("safe_to_trade", True))
        # recovery_status.get("review_required", False) → True
        self.assertTrue(s.get("review_required", False))

        # trial: status != "breached"
        self.assertNotEqual(str(s.get("status", "")).lower(), "breached")
        # 标识这是 stale sentinel
        self.assertTrue(s.get("_stale"))

    async def test_stale_cache_blocks_opening_via_only_reduce(self) -> None:
        """过期缓存 → sentinel 的 only_reduce_reasons 非空 → RiskEngine 硬拒绝开仓。"""
        cache = GuardSignalHotStateCache(
            signal_name="derivatives_live",
            logger=_make_logger(),
            stale_threshold_seconds=0.05,
        )
        await cache.bootstrap()
        await cache.publish({"status": "active", "only_reduce_required": False})

        # 正常时 → 允许开仓
        fresh = cache.snapshot()
        self.assertFalse(fresh.get("only_reduce_required"))

        # 过期后 → 必须只减仓 + 有 reasons 触发硬拒绝
        await asyncio.sleep(0.1)
        stale = cache.snapshot()
        self.assertTrue(stale["only_reduce_required"],
                        "FAIL-OPEN BUG: stale guard must set only_reduce_required=True")
        self.assertFalse(stale["safe_to_trade"],
                         "FAIL-OPEN BUG: stale guard must set safe_to_trade=False")
        stale_reasons = stale.get("only_reduce_reasons", [])
        self.assertTrue(len(stale_reasons) > 0,
                        "FAIL-OPEN BUG: stale guard must have only_reduce_reasons "
                        "for RiskEngine hard rejection path")

    async def test_missing_cache_blocks_opening(self) -> None:
        """从未收到过数据 → sentinel → 开仓被硬拒绝。"""
        cache = GuardSignalHotStateCache(
            signal_name="trial",
            logger=_make_logger(),
        )
        await cache.bootstrap()
        # 从未 publish 任何数据
        snapshot = cache.snapshot()
        self.assertTrue(snapshot["only_reduce_required"],
                        "FAIL-OPEN BUG: missing guard must set only_reduce_required=True")
        reasons = snapshot.get("only_reduce_reasons", [])
        self.assertTrue(len(reasons) > 0,
                        "FAIL-OPEN BUG: missing guard must have only_reduce_reasons")


    def test_sentinel_reasons_survive_risk_engine_filter(self) -> None:
        """模拟 RiskEngine._runtime_guard_only_reduce_reasons 的完整过滤逻辑。

        RiskEngine 第 1784-1786 行：
            if not isinstance(payload, dict) or not bool(payload.get("only_reduce_required")):
                return []
            return [str(item) for item in (payload.get("only_reduce_reasons") or []) if str(item).strip()]

        sentinel 必须在经过这套过滤后仍返回非空列表。
        """
        payload = dict(_FAIL_CLOSED_SENTINEL)

        # 模拟 RiskEngine._runtime_guard_only_reduce_reasons
        if not isinstance(payload, dict) or not bool(payload.get("only_reduce_required")):
            reasons: list[str] = []
        else:
            reasons = [
                str(item)
                for item in (payload.get("only_reduce_reasons") or [])
                if str(item).strip()
            ]

        self.assertTrue(
            len(reasons) > 0,
            f"Sentinel reasons must survive RiskEngine filter, got: {reasons}. "
            f"Without non-empty reasons, _evaluate_derivatives_pretrade line 1526 "
            f"'if provider_only_reduce_reasons:' evaluates to False → opening "
            f"intents are NOT rejected → fail-open.",
        )
        self.assertIn("guard_signal_missing_or_stale", reasons)


class TestDiagnostic(unittest.IsolatedAsyncioTestCase):
    """diagnostic() 运维信息测试。"""

    async def test_diagnostic_output(self) -> None:
        cache = GuardSignalHotStateCache(
            signal_name="trial",
            logger=_make_logger(),
            stale_threshold_seconds=60.0,
        )
        await cache.bootstrap(process_role="decision")
        diag = cache.diagnostic()
        self.assertEqual(diag["signal_name"], "trial")
        self.assertTrue(diag["bootstrapped"])
        self.assertFalse(diag["has_data"])
        self.assertEqual(diag["stale_threshold_seconds"], 60.0)
        self.assertEqual(diag["process_role"], "decision")


class _PersistTrackingBus:
    """Mock bus that records every publish_envelope call's persist flag.

    用于验证 guard_signal_cache.publish() 在 dedup 时传 persist=False、
    在内容变化时传 persist=True。
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._subs: list = []

    async def publish_envelope(self, envelope, *, persist: bool = True) -> None:
        # 记录 persist flag + envelope.key + business payload (排除 metadata)
        biz_payload = {
            k: v for k, v in envelope.payload.items()
            if not k.startswith("_")
        }
        self.calls.append({
            "persist": persist,
            "key": envelope.key,
            "business_payload": biz_payload,
            "full_payload": envelope.payload,
        })
        # 模拟 NATS 投递：仍 invoke 订阅的 handler（reader 心跳必须保留）
        message = {
            "topic": envelope.topic,
            "key": envelope.key,
            "payload": envelope.model_dump(mode="json"),
        }
        for handler in list(self._subs):
            await handler(message)

    async def publish(self, topic: str, key: str, payload: dict) -> None:
        # fallback path（老 bus 接口）—— 不应该在测试中走到
        raise AssertionError(
            "publish(...) fallback should not fire; "
            "guard_signal_cache should prefer publish_envelope"
        )

    async def subscribe(self, topic: str, handler) -> None:
        self._subs.append(handler)


class TestDedup(unittest.IsolatedAsyncioTestCase):
    """2026-04-21 TOCTOU 后续：`recovery` 信号 98.5% dedup 实施。

    核心属性：
      - 同 payload 连续 publish → 第 2 次起走 persist=False（不写 event_store）
      - payload 变化 → 恢复 persist=True（落盘）
      - 无论 persist=True/False，NATS 广播都要发出去（reader 心跳不丢）
      - 这保证 reader 侧 `_last_updated_at` 每次都更新，不会触发 120s fail-closed
    """

    async def test_duplicate_payload_uses_persist_false(self) -> None:
        """连续两次发布同一个 payload：第 2 次应 persist=False。"""
        bus = _PersistTrackingBus()
        cache = GuardSignalHotStateCache(
            signal_name="recovery",
            logger=_make_logger(),
        )
        await cache.bootstrap(bus=bus, process_role="execution")

        payload = {
            "safe_to_trade": True,
            "review_required": False,
            "independent_recovery_snapshots": [{"sleeve_id": "s1", "state": "ok"}],
        }
        await cache.publish(payload)
        await cache.publish(payload)  # identical

        self.assertEqual(len(bus.calls), 2)
        self.assertTrue(
            bus.calls[0]["persist"],
            "第一次 publish 没有前置 hash，必须走持久化",
        )
        self.assertFalse(
            bus.calls[1]["persist"],
            "第二次同 payload 应 dedup → persist=False 跳过 event_store.append",
        )

    async def test_duplicate_payload_sets_receive_side_skip_flag(self) -> None:
        """★ 架构关键 ★：publish 端 persist=False 只关一处 event_store.append
        （nats_bus.py publish_envelope 路径）；nats_bus.py 的 NATS receive
        handler 里还有一处 event_store.append (line 1377) 独立于 persist flag。
        所以必须在 envelope.payload 里放 `_dedup_skip_persist=True` 让 receive
        端也跳过，否则跨进程 subscriber 仍会把重复消息写 PG → 单边 dedup 失效。

        不加这条锚点测试的话，下一个读代码的人可能把 payload 字段当 "内部
        metadata 冗余" 删掉，静默 regress。
        """
        bus = _PersistTrackingBus()
        cache = GuardSignalHotStateCache(
            signal_name="recovery",
            logger=_make_logger(),
        )
        await cache.bootstrap(bus=bus, process_role="execution")

        payload = {"safe_to_trade": True, "x": 1}
        await cache.publish(payload)  # first: no flag
        await cache.publish(payload)  # dup: must have flag

        self.assertEqual(len(bus.calls), 2)
        first_payload = bus.calls[0]["full_payload"]
        second_payload = bus.calls[1]["full_payload"]
        self.assertFalse(
            first_payload.get("_dedup_skip_persist", False),
            "第一次 publish 不应有 _dedup_skip_persist（新 payload，需要持久化）",
        )
        self.assertTrue(
            second_payload.get("_dedup_skip_persist"),
            "CRITICAL：第二次同 payload 必须在 envelope.payload 里写 "
            "`_dedup_skip_persist=True`，否则 nats_bus receive 端会把重复消息 "
            "append 到 event_store，单边 dedup 失效。",
        )

    async def test_changed_payload_restores_persist_true(self) -> None:
        """payload 变化 → 恢复 persist=True。"""
        bus = _PersistTrackingBus()
        cache = GuardSignalHotStateCache(
            signal_name="recovery",
            logger=_make_logger(),
        )
        await cache.bootstrap(bus=bus, process_role="execution")

        await cache.publish({"safe_to_trade": True, "review_required": False})
        await cache.publish({"safe_to_trade": True, "review_required": False})  # dup
        await cache.publish({"safe_to_trade": False, "review_required": True})  # change!

        self.assertEqual(len(bus.calls), 3)
        self.assertTrue(bus.calls[0]["persist"])
        self.assertFalse(bus.calls[1]["persist"])
        self.assertTrue(
            bus.calls[2]["persist"],
            "payload 变了之后必须恢复持久化，否则 event_store 丢数据",
        )

    async def test_reader_heartbeat_preserved_under_dedup(self) -> None:
        """**关键安全验证**：dedup 不能破坏 reader 的 120s staleness 检测。

        reader 侧 `_last_updated_at` 只在收到 NATS 消息时更新；如果 dedup
        跳过 NATS publish，reader 超过 120s 会 fail-closed（RiskEngine 误入
        only-reduce 模式）。本测试保证即使 dedup 了，NATS 广播仍然发出、
        reader `_last_updated_at` 每次都更新。
        """
        bus = _PersistTrackingBus()

        writer = GuardSignalHotStateCache(
            signal_name="recovery",
            logger=_make_logger(),
        )
        await writer.bootstrap(bus=bus, process_role="execution")

        reader = GuardSignalHotStateCache(
            signal_name="recovery",
            logger=_make_logger(),
        )
        await reader.bootstrap(
            bus=bus,
            process_role="decision",
            subscribe=True,
        )

        # 第 1 次 publish 后，reader 有快照
        await writer.publish({"safe_to_trade": True, "sleeves": []})
        first_ts = reader._last_updated_at
        self.assertGreater(first_ts, 0, "reader 第一次应收到消息")

        # 等待一瞬（毫秒级）保证时间戳单调递增
        await asyncio.sleep(0.01)

        # 第 2 次 publish 同 payload（会被 dedup 成 persist=False）
        await writer.publish({"safe_to_trade": True, "sleeves": []})
        second_ts = reader._last_updated_at

        # ★ 关键断言 ★：即使 dedup，reader 的 _last_updated_at 必须更新
        self.assertGreater(
            second_ts,
            first_ts,
            "CRITICAL: dedup 不能跳过 NATS 广播 —— 否则 reader _last_updated_at "
            "冻结，120s 后 fail-closed 误入 only-reduce 模式。"
            "dedup 只能跳过 event_store.append，NATS 必须继续发。",
        )

        # 顺便验证第二次确实是 dedup（persist=False）
        self.assertFalse(bus.calls[1]["persist"])

    async def test_different_signals_do_not_cross_contaminate_hash(self) -> None:
        """`recovery` 和 `trial` 两个独立 cache，hash 互不影响。"""
        bus_recovery = _PersistTrackingBus()
        bus_trial = _PersistTrackingBus()

        cache_recovery = GuardSignalHotStateCache(
            signal_name="recovery",
            logger=_make_logger(),
        )
        cache_trial = GuardSignalHotStateCache(
            signal_name="trial",
            logger=_make_logger(),
        )
        await cache_recovery.bootstrap(bus=bus_recovery, process_role="execution")
        await cache_trial.bootstrap(bus=bus_trial, process_role="execution")

        payload = {"safe_to_trade": True}

        await cache_recovery.publish(payload)
        await cache_trial.publish(payload)  # 不同 cache 实例，应各自走 persist=True

        self.assertEqual(len(bus_recovery.calls), 1)
        self.assertEqual(len(bus_trial.calls), 1)
        self.assertTrue(bus_recovery.calls[0]["persist"])
        self.assertTrue(
            bus_trial.calls[0]["persist"],
            "不同 cache 实例 _last_published_hash 互相独立，trial 第一次必须持久化",
        )

    async def test_first_publish_failure_forces_next_persist_true(self) -> None:
        """★ 关键安全属性 ★：**第一次** publish 失败时 `_last_published_hash`
        不更新 —— 第二次必须 persist=True 保证数据落盘（event_store 无此条）。

        反例：如果实现先更新 hash 再 publish，call 1 会标记 hash=X 然后 raise；
        call 2 看到 hash=X 以为已经持久化过了，走 persist=False →
        **data loss**：event_store 从来没有这条业务 payload。
        """
        call_count = 0

        class _FailFirstBus:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            async def publish_envelope(self, envelope, *, persist: bool = True) -> None:
                nonlocal call_count
                call_count += 1
                self.calls.append({"persist": persist})
                if call_count == 1:
                    raise RuntimeError("simulated NATS failure on first publish")

            async def publish(self, topic: str, key: str, payload: dict) -> None:
                raise AssertionError("should not fall back to publish()")

            async def subscribe(self, topic: str, handler) -> None:
                pass

        bus = _FailFirstBus()
        cache = GuardSignalHotStateCache(
            signal_name="recovery",
            logger=_make_logger(),
        )
        await cache.bootstrap(bus=bus, process_role="execution")

        payload = {"safe_to_trade": True}
        await cache.publish(payload)  # 第 1 次 persist=True 但 raise → hash 不应更新
        await cache.publish(payload)  # 第 2 次必须再次 persist=True（event_store 还没这条）

        self.assertEqual(len(bus.calls), 2)
        self.assertTrue(
            bus.calls[0]["persist"],
            "第 1 次（新 cache, hash=None）应 persist=True",
        )
        self.assertTrue(
            bus.calls[1]["persist"],
            "FATAL IF FAIL: 第 1 次 publish raise → _last_published_hash 必须"
            "保持为 None → 第 2 次同 payload 应再次 persist=True。"
            "如果 hash 在 publish_envelope await 之前就被更新，第 2 次会误判为"
            "dup → persist=False → event_store 永远没这条 → data loss。",
        )

    async def test_successful_publish_then_failed_dedup_does_not_corrupt_hash(self) -> None:
        """call 1 成功写 event_store（hash=X），call 2 同 payload 走 persist=False
        但 NATS raise —— 这种场景下 data 已经在 event_store 里（call 1 存的），
        call 3 仍可继续 dedup（persist=False），因为真实状态未变。

        说明：publish_envelope raise 不等于 data loss 只要它是 dedup 路径（persist=False）。
        """
        call_count = 0

        class _FailSecondBus:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            async def publish_envelope(self, envelope, *, persist: bool = True) -> None:
                nonlocal call_count
                call_count += 1
                self.calls.append({"persist": persist})
                if call_count == 2:
                    raise RuntimeError("NATS hiccup mid-dedup")

            async def publish(self, topic: str, key: str, payload: dict) -> None:
                raise AssertionError("should not fall back to publish()")

            async def subscribe(self, topic: str, handler) -> None:
                pass

        bus = _FailSecondBus()
        cache = GuardSignalHotStateCache(
            signal_name="recovery",
            logger=_make_logger(),
        )
        await cache.bootstrap(bus=bus, process_role="execution")

        payload = {"safe_to_trade": True}
        await cache.publish(payload)  # call 1 success: persist=True, hash=X
        await cache.publish(payload)  # call 2: persist=False, NATS raise
        await cache.publish(payload)  # call 3: still persist=False (dedup ok,
                                       # data safe in event_store from call 1)

        self.assertEqual(len(bus.calls), 3)
        self.assertTrue(bus.calls[0]["persist"])
        self.assertFalse(bus.calls[1]["persist"])
        self.assertFalse(
            bus.calls[2]["persist"],
            "call 3: call 1 已经落盘同 payload → event_store 有这条 → dedup 安全",
        )
