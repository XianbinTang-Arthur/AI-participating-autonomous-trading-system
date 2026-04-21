"""DecisionCycleTrigger 的 queue + dispatcher 骨架契约测试（SOW §3.S1.6）。

验证 decision_features_handler_queue_decoupling_sow.md §3.S1 引入的
queue + dispatcher infrastructure：

- start() 初始化 queue + 起 dispatcher task；幂等
- stop() 通知 shutdown + cancel task + drain
- _dispatcher_loop 消费 queue 串行跑 run_cycle
- dispatcher task 遇 run_cycle 异常不退出、记 consecutive_failures
- _enqueue_trigger 走 latest-wins（queue 满时覆盖旧 pending）
- flag=False 时 handle_feature_snapshot 仍走 legacy 路径

测试不构造真实 FeatureSnapshot / MarketSnapshot（Pydantic 字段多），
用 SimpleNamespace / dataclasses 伪造同形对象——dispatcher / queue
逻辑只读 .symbol / .timeframe 这些属性，不关心类型。
"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from aats.services.decision_engine.trigger import DecisionCycleTrigger, _PendingTrigger


def _fake_trigger(*, orchestrator, market_gateway=None, policy=None):
    """用 __new__ 绕过 __init__ 里对 logger 构造 + 各种 service 依赖的
    验证，装配出最小 DecisionCycleTrigger 实例供 dispatcher 单元测试用。"""
    t = DecisionCycleTrigger.__new__(DecisionCycleTrigger)
    t.orchestrator = orchestrator
    t.market_gateway = market_gateway or SimpleNamespace()
    t.policy = policy or SimpleNamespace()
    t.can_trigger = None
    # 最小 logger mock：只需支持 log_event 里用到的 log 方法。用标准 stdlib
    # logger 即可，不污染 test output。
    import logging
    t.logger = logging.getLogger("test.decision_trigger")
    t._timeframe_locks = {}
    t._consecutive_failures = {}
    t._trigger_queue = None
    t._dispatcher_task = None
    t._dispatcher_shutdown = asyncio.Event()
    t._use_queue_dispatcher = False
    return t


def _make_pending(*, symbol="BTC-USDT-SWAP", timeframe="15m", tag=None) -> _PendingTrigger:
    """构造一个 _PendingTrigger。测试里用 SimpleNamespace 冒充 envelope /
    snapshot / market_snapshot，因为 dispatcher 只传对象不内省字段——
    FeatureSnapshot / MarketSnapshot / EventEnvelope 的实际字段与 dispatcher
    逻辑正交。"""
    return _PendingTrigger(
        feature_envelope=SimpleNamespace(event_id=f"evt_{tag or symbol}"),
        snapshot=SimpleNamespace(symbol=symbol),
        timeframe=timeframe,
        market_snapshot=SimpleNamespace(symbol=symbol, snapshot_ts=None, last_price=None),
    )


class TestDispatcherLifecycle(unittest.IsolatedAsyncioTestCase):
    """start / stop 的基本契约。"""

    async def test_start_initializes_queue_and_task(self) -> None:
        orch = SimpleNamespace(run_cycle=self._noop_run_cycle)
        trig = _fake_trigger(orchestrator=orch)

        self.assertIsNone(trig._trigger_queue)
        self.assertIsNone(trig._dispatcher_task)

        await trig.start()

        self.assertIsNotNone(trig._trigger_queue)
        self.assertIsNotNone(trig._dispatcher_task)
        self.assertFalse(trig._dispatcher_task.done())

        await trig.stop()

    async def test_start_is_idempotent(self) -> None:
        orch = SimpleNamespace(run_cycle=self._noop_run_cycle)
        trig = _fake_trigger(orchestrator=orch)

        await trig.start()
        task_first = trig._dispatcher_task
        await trig.start()  # 第二次 start 应该 no-op

        self.assertIs(trig._dispatcher_task, task_first)

        await trig.stop()

    async def test_stop_cancels_task_and_clears_queue(self) -> None:
        orch = SimpleNamespace(run_cycle=self._noop_run_cycle)
        trig = _fake_trigger(orchestrator=orch)
        await trig.start()

        task = trig._dispatcher_task
        await trig.stop()

        self.assertTrue(task.cancelled() or task.done())
        self.assertIsNone(trig._dispatcher_task)
        self.assertIsNone(trig._trigger_queue)

    @staticmethod
    async def _noop_run_cycle(*, symbol, timeframe, feature_snapshot_hint=None):
        return None


class TestDispatcherLoopConsumesQueue(unittest.IsolatedAsyncioTestCase):
    """dispatcher 从 queue 消费 trigger 并调 run_cycle。"""

    async def test_dispatcher_runs_cycle_for_enqueued_trigger(self) -> None:
        invocations: list[tuple[str, str]] = []

        async def spy_run_cycle(*, symbol, timeframe, feature_snapshot_hint=None):
            invocations.append((symbol, timeframe))

        orch = SimpleNamespace(run_cycle=spy_run_cycle)
        # policy 的 record_trigger 也得能 call（spy record_trigger 不抛即可）
        policy = SimpleNamespace(record_trigger=lambda **kwargs: None)
        trig = _fake_trigger(orchestrator=orch, policy=policy)
        await trig.start()

        try:
            pending = _make_pending(tag="a")
            await trig._enqueue_trigger(pending)

            # 等待 dispatcher 消费（短轮询避免 race）
            for _ in range(50):
                if invocations:
                    break
                await asyncio.sleep(0.02)

            self.assertEqual(invocations, [("BTC-USDT-SWAP", "15m")])
        finally:
            await trig.stop()


class TestDispatcherLoopSurviveException(unittest.IsolatedAsyncioTestCase):
    """run_cycle 抛异常时 dispatcher 继续活着处理下一个 trigger。"""

    async def test_exception_does_not_kill_loop(self) -> None:
        call_counts = {"n": 0}

        async def mock_run_cycle(*, symbol, timeframe, feature_snapshot_hint=None):
            call_counts["n"] += 1
            if call_counts["n"] == 1:
                raise RuntimeError("intentional boom")
            # 第二次成功

        orch = SimpleNamespace(run_cycle=mock_run_cycle)
        policy = SimpleNamespace(record_trigger=lambda **kwargs: None)
        trig = _fake_trigger(orchestrator=orch, policy=policy)
        # 把 backoff 压到最小加速测试
        trig._BACKOFF_INITIAL_S = 0.01
        trig._BACKOFF_MAX_S = 0.01

        await trig.start()
        try:
            await trig._enqueue_trigger(_make_pending(tag="bad"))
            # 等第一次 run_cycle 消费完 + backoff
            await asyncio.sleep(0.1)
            # 此时 consecutive_failures 应该记 1
            self.assertEqual(
                trig._consecutive_failures.get(("BTC-USDT-SWAP", "15m")), 1
            )

            # 入第二条：dispatcher 应该没死，继续消费
            await trig._enqueue_trigger(_make_pending(tag="good"))
            for _ in range(50):
                if call_counts["n"] >= 2:
                    break
                await asyncio.sleep(0.02)

            self.assertEqual(call_counts["n"], 2)
            # 成功后 consecutive_failures 应被清空
            self.assertNotIn(
                ("BTC-USDT-SWAP", "15m"), trig._consecutive_failures
            )
        finally:
            await trig.stop()


class TestEnqueueLatestWins(unittest.IsolatedAsyncioTestCase):
    """_enqueue_trigger 覆盖式入队语义。"""

    async def test_latest_wins_when_queue_full(self) -> None:
        # 阻塞 run_cycle，确保 dispatcher 卡在第一个 trigger 上不消费 queue
        release = asyncio.Event()
        invocations: list[str] = []

        async def stuck_run_cycle(*, symbol, timeframe, feature_snapshot_hint=None):
            invocations.append(feature_snapshot_hint.event_id)
            await release.wait()

        orch = SimpleNamespace(run_cycle=stuck_run_cycle)
        policy = SimpleNamespace(record_trigger=lambda **kwargs: None)
        trig = _fake_trigger(orchestrator=orch, policy=policy)
        await trig.start()

        try:
            # 第一个 enqueue：dispatcher 拿去跑 stuck_run_cycle
            await trig._enqueue_trigger(_make_pending(tag="first"))
            # 等 dispatcher 真的拿到第一个（进入 stuck_run_cycle）
            for _ in range(50):
                if invocations == ["evt_first"]:
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(invocations, ["evt_first"])

            # dispatcher 现在卡着。继续 enqueue 三个——都会依次堆到
            # maxsize=1 queue 里，后者覆盖前者（latest-wins）
            await trig._enqueue_trigger(_make_pending(tag="stale_a"))
            await trig._enqueue_trigger(_make_pending(tag="stale_b"))
            await trig._enqueue_trigger(_make_pending(tag="winner"))

            # queue 此时应该 exactly 有 1 条，且是 "winner"
            assert trig._trigger_queue is not None
            self.assertEqual(trig._trigger_queue.qsize(), 1)
            queued = trig._trigger_queue.get_nowait()
            trig._trigger_queue.task_done()
            self.assertEqual(queued.feature_envelope.event_id, "evt_winner")
        finally:
            # 放行 stuck 的第一个 run_cycle，让 dispatcher 能退
            release.set()
            await trig.stop()


class TestLegacyPathStillWorksWhenFlagFalse(unittest.IsolatedAsyncioTestCase):
    """S1 的底线：flag 默认 False 时，handle_feature_snapshot 走 legacy。

    验证机制：spy _handle_feature_snapshot_legacy / _handle_feature_snapshot_via_queue，
    确认 flag 正确分流。不跑 parse_envelope（需要完整 message 结构）——把两个
    内部方法替成 spy 就够了。
    """

    async def test_flag_false_routes_to_legacy(self) -> None:
        orch = SimpleNamespace(run_cycle=self._noop)
        trig = _fake_trigger(orchestrator=orch)

        legacy_calls: list[dict] = []
        queue_calls: list[dict] = []

        async def legacy_spy(msg):
            legacy_calls.append(msg)

        async def queue_spy(msg):
            queue_calls.append(msg)

        trig._handle_feature_snapshot_legacy = legacy_spy  # type: ignore[method-assign]
        trig._handle_feature_snapshot_via_queue = queue_spy  # type: ignore[method-assign]

        # flag 保持默认 False
        self.assertFalse(trig._use_queue_dispatcher)
        await trig.handle_feature_snapshot({"any": "msg"})

        self.assertEqual(legacy_calls, [{"any": "msg"}])
        self.assertEqual(queue_calls, [])

    async def test_flag_true_routes_to_queue(self) -> None:
        orch = SimpleNamespace(run_cycle=self._noop)
        trig = _fake_trigger(orchestrator=orch)

        legacy_calls: list[dict] = []
        queue_calls: list[dict] = []

        async def legacy_spy(msg):
            legacy_calls.append(msg)

        async def queue_spy(msg):
            queue_calls.append(msg)

        trig._handle_feature_snapshot_legacy = legacy_spy  # type: ignore[method-assign]
        trig._handle_feature_snapshot_via_queue = queue_spy  # type: ignore[method-assign]

        trig._use_queue_dispatcher = True
        await trig.handle_feature_snapshot({"any": "msg"})

        self.assertEqual(queue_calls, [{"any": "msg"}])
        self.assertEqual(legacy_calls, [])

    @staticmethod
    async def _noop(*args, **kwargs):
        return None


class TestHandleFeatureSnapshotViaQueue(unittest.IsolatedAsyncioTestCase):
    """S2 切 flag 后 _handle_feature_snapshot_via_queue 的行为。

    3 条核心契约：
    1. handler 快速返回（不等 run_cycle）
    2. should_trigger=True 命中后 dispatcher 最终跑 run_cycle
    3. 多条连续命中的消息走 latest-wins 去重（dispatcher 跑的次数 < handler 调用次数）
    """

    async def test_handler_fast_returns_without_awaiting_run_cycle(self) -> None:
        """快路径契约：即便 run_cycle 装慢 2s，handler 10 次调用 < 100ms。"""
        release_run_cycle = asyncio.Event()
        run_cycle_calls: list[str] = []

        async def slow_run_cycle(*, symbol, timeframe, feature_snapshot_hint=None):
            run_cycle_calls.append(feature_snapshot_hint.event_id)
            # 卡住不返回，模拟 22s 毛刺
            await release_run_cycle.wait()

        # spy should_trigger 让它总返回 True（模拟 cadence_elapsed）
        policy = SimpleNamespace(
            enabled_timeframes=lambda: ("15m",),
            should_trigger=lambda **kwargs: (True, "test"),
            record_trigger=lambda **kwargs: None,
        )
        market_gw = SimpleNamespace(
            latest_snapshot=lambda sym: SimpleNamespace(symbol=sym, snapshot_ts=None, last_price=None),
        )
        orch = SimpleNamespace(run_cycle=slow_run_cycle)

        trig = _fake_trigger(orchestrator=orch, market_gateway=market_gw, policy=policy)
        trig._use_queue_dispatcher = True
        await trig.start()

        try:
            # mock parse_envelope + FeatureSnapshot.model_validate：S2 handler
            # 需要这两个函数可用。我们在 trigger 模块的 symbol 空间里 patch。
            import aats.services.decision_engine.trigger as trigger_module

            call_log: list[int] = []
            original_parse = trigger_module.parse_envelope
            original_validate = trigger_module.FeatureSnapshot.model_validate

            def fake_parse(message):
                tag = message["tag"]
                call_log.append(tag)
                return SimpleNamespace(event_id=f"evt_{tag}", payload={"tag": tag})

            def fake_validate(payload):
                return SimpleNamespace(symbol="BTC-USDT-SWAP")

            trigger_module.parse_envelope = fake_parse
            trigger_module.FeatureSnapshot.model_validate = fake_validate
            try:
                import time as _time
                t0 = _time.monotonic()
                for i in range(10):
                    await trig.handle_feature_snapshot({"tag": i})
                elapsed_ms = (_time.monotonic() - t0) * 1000

                # 10 次 handler call 总耗时 < 100ms（不等 slow_run_cycle 的 "forever"）
                self.assertLess(
                    elapsed_ms, 100.0,
                    f"handler 应快速返回不等 run_cycle，实测 {elapsed_ms:.1f}ms",
                )
                # run_cycle 已经被 dispatcher 调用至少 1 次（第一次拿去跑卡住了）
                await asyncio.sleep(0.05)
                self.assertGreaterEqual(len(run_cycle_calls), 1)
            finally:
                trigger_module.parse_envelope = original_parse
                trigger_module.FeatureSnapshot.model_validate = original_validate
        finally:
            release_run_cycle.set()
            await trig.stop()

    async def test_handler_eventually_triggers_run_cycle_when_should_trigger_true(self) -> None:
        """命中 should_trigger=True 后 dispatcher 最终跑 run_cycle。"""
        run_cycle_calls: list[str] = []

        async def run_cycle(*, symbol, timeframe, feature_snapshot_hint=None):
            run_cycle_calls.append(feature_snapshot_hint.event_id)

        policy = SimpleNamespace(
            enabled_timeframes=lambda: ("15m",),
            should_trigger=lambda **kwargs: (True, "cadence_elapsed"),
            record_trigger=lambda **kwargs: None,
        )
        market_gw = SimpleNamespace(
            latest_snapshot=lambda sym: SimpleNamespace(symbol=sym, snapshot_ts=None, last_price=None),
        )
        orch = SimpleNamespace(run_cycle=run_cycle)

        trig = _fake_trigger(orchestrator=orch, market_gateway=market_gw, policy=policy)
        trig._use_queue_dispatcher = True
        await trig.start()

        try:
            import aats.services.decision_engine.trigger as trigger_module
            original_parse = trigger_module.parse_envelope
            original_validate = trigger_module.FeatureSnapshot.model_validate
            trigger_module.parse_envelope = lambda m: SimpleNamespace(event_id="evt_single", payload={})
            trigger_module.FeatureSnapshot.model_validate = lambda p: SimpleNamespace(symbol="BTC-USDT-SWAP")
            try:
                await trig.handle_feature_snapshot({"tag": "single"})
                # 等 dispatcher 消费
                for _ in range(50):
                    if run_cycle_calls:
                        break
                    await asyncio.sleep(0.02)
                self.assertEqual(run_cycle_calls, ["evt_single"])
            finally:
                trigger_module.parse_envelope = original_parse
                trigger_module.FeatureSnapshot.model_validate = original_validate
        finally:
            await trig.stop()

    async def test_handler_skips_enqueue_when_should_trigger_false(self) -> None:
        """should_trigger=False 时不 enqueue，dispatcher 不跑 run_cycle。

        这是确认快路径的"should_trigger 门控"仍然生效——不是所有 feature_snapshot
        都入队（那样 queue 一直满、dispatcher 没用）。
        """
        run_cycle_calls: list[str] = []

        async def run_cycle(*, symbol, timeframe, feature_snapshot_hint=None):
            run_cycle_calls.append(feature_snapshot_hint.event_id)

        policy = SimpleNamespace(
            enabled_timeframes=lambda: ("15m",),
            should_trigger=lambda **kwargs: (False, "suppressed_duplicate"),
            record_trigger=lambda **kwargs: None,
        )
        market_gw = SimpleNamespace(
            latest_snapshot=lambda sym: SimpleNamespace(symbol=sym, snapshot_ts=None, last_price=None),
        )
        orch = SimpleNamespace(run_cycle=run_cycle)

        trig = _fake_trigger(orchestrator=orch, market_gateway=market_gw, policy=policy)
        trig._use_queue_dispatcher = True
        await trig.start()

        try:
            import aats.services.decision_engine.trigger as trigger_module
            original_parse = trigger_module.parse_envelope
            original_validate = trigger_module.FeatureSnapshot.model_validate
            trigger_module.parse_envelope = lambda m: SimpleNamespace(event_id="evt_x", payload={})
            trigger_module.FeatureSnapshot.model_validate = lambda p: SimpleNamespace(symbol="BTC-USDT-SWAP")
            try:
                for _ in range(5):
                    await trig.handle_feature_snapshot({"any": "msg"})
                # 给 dispatcher 时间（应该什么都没做）
                await asyncio.sleep(0.1)
                # queue 应该空，run_cycle 没被调
                self.assertEqual(run_cycle_calls, [])
                assert trig._trigger_queue is not None
                self.assertEqual(trig._trigger_queue.qsize(), 0)
            finally:
                trigger_module.parse_envelope = original_parse
                trigger_module.FeatureSnapshot.model_validate = original_validate
        finally:
            await trig.stop()


if __name__ == "__main__":
    unittest.main()
