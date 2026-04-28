"""Unit tests for _CollectingBus subscription deduplication adapter.

Stage 7: 修复 hybrid 模式下 critical + observer 同 topic 重复 subscribe 触发
NATS "consumer is already bound" 的 bug。_CollectingBus 把所有 subscribe 调用
buffer 起来，flush 时按 topic 聚合 fan-out。

测试覆盖：
  - 单 handler topic 直通
  - 多 handler topic fan-out 顺序
  - 多 topic 互不串扰（closure capture 正确性）
  - flush() 幂等：空 buffer 不再 emit
  - publish 直通底层 bus
  - publish 在 _CollectingBus 上调用是合法的（壳层不丢消息）
  - fan_out 异常隔离（所有 handler 跑完再 raise first）
  - **fan_out 并行执行**（2026-04-23 P1-b：慢 handler 不拖累快 handler
    也不拖累 NATS ack 延迟）
"""

from __future__ import annotations

import asyncio
import time
import unittest

from aats.bootstrap.config import _CollectingBus
from aats.bus.base import EventBus, MessageHandler


class _RecordingBus(EventBus):
    """Test double：把 subscribe / publish 调用按顺序记下来便于断言。"""

    def __init__(self) -> None:
        self.subscriptions: list[tuple[str, MessageHandler]] = []
        self.publishes: list[tuple[str, str, dict]] = []

    async def publish(self, topic: str, key: str, payload: dict) -> None:
        self.publishes.append((topic, key, payload))

    async def subscribe(self, topic: str, handler: MessageHandler) -> None:
        self.subscriptions.append((topic, handler))


class TestCollectingBus(unittest.IsolatedAsyncioTestCase):
    async def test_single_handler_per_topic_passes_through(self) -> None:
        downstream = _RecordingBus()
        bus = _CollectingBus(downstream)

        async def h(_: dict) -> None: ...

        await bus.subscribe("topic.a", h)
        await bus.flush()

        self.assertEqual(len(downstream.subscriptions), 1)
        topic, handler = downstream.subscriptions[0]
        self.assertEqual(topic, "topic.a")
        self.assertIs(handler, h)

    async def test_multiple_handlers_same_topic_fan_out_in_order(self) -> None:
        downstream = _RecordingBus()
        bus = _CollectingBus(downstream)
        order: list[str] = []

        async def first(_: dict) -> None:
            order.append("first")

        async def second(_: dict) -> None:
            order.append("second")

        async def third(_: dict) -> None:
            order.append("third")

        await bus.subscribe("topic.a", first)
        await bus.subscribe("topic.a", second)
        await bus.subscribe("topic.a", third)
        await bus.flush()

        # 同 topic 应该只 emit 一次 subscribe 给底层 bus
        self.assertEqual(len(downstream.subscriptions), 1)
        topic, fan_out = downstream.subscriptions[0]
        self.assertEqual(topic, "topic.a")
        # fan-out 必须不是任何原 handler，而是新的包装
        self.assertIsNot(fan_out, first)
        self.assertIsNot(fan_out, second)
        self.assertIsNot(fan_out, third)

        await fan_out({"topic": "topic.a", "key": "k", "payload": {}})
        self.assertEqual(order, ["first", "second", "third"])

    async def test_multiple_topics_each_fan_out_independently(self) -> None:
        """Closure capture：循环里给每个 topic 构造的 fan_out 必须捕获各自的 handler 列表，
        而不是共享最后一次循环的引用。"""
        downstream = _RecordingBus()
        bus = _CollectingBus(downstream)
        a_calls: list[str] = []
        b_calls: list[str] = []

        async def a1(_: dict) -> None:
            a_calls.append("a1")

        async def a2(_: dict) -> None:
            a_calls.append("a2")

        async def b1(_: dict) -> None:
            b_calls.append("b1")

        async def b2(_: dict) -> None:
            b_calls.append("b2")

        await bus.subscribe("topic.a", a1)
        await bus.subscribe("topic.a", a2)
        await bus.subscribe("topic.b", b1)
        await bus.subscribe("topic.b", b2)
        await bus.flush()

        self.assertEqual(len(downstream.subscriptions), 2)
        topics_emitted = {t for t, _ in downstream.subscriptions}
        self.assertEqual(topics_emitted, {"topic.a", "topic.b"})

        # 找到 topic.a 的 fan_out 并调用
        fan_out_a = next(h for t, h in downstream.subscriptions if t == "topic.a")
        fan_out_b = next(h for t, h in downstream.subscriptions if t == "topic.b")

        await fan_out_a({"topic": "topic.a", "key": "k", "payload": {}})
        await fan_out_b({"topic": "topic.b", "key": "k", "payload": {}})

        self.assertEqual(a_calls, ["a1", "a2"])
        self.assertEqual(b_calls, ["b1", "b2"])

    async def test_flush_clears_pending(self) -> None:
        downstream = _RecordingBus()
        bus = _CollectingBus(downstream)

        async def h(_: dict) -> None: ...

        await bus.subscribe("topic.a", h)
        await bus.flush()
        self.assertEqual(len(downstream.subscriptions), 1)

        # 第二次 flush 不应该重复发任何东西
        await bus.flush()
        self.assertEqual(len(downstream.subscriptions), 1)

    async def test_publish_passes_through(self) -> None:
        downstream = _RecordingBus()
        bus = _CollectingBus(downstream)

        await bus.publish("topic.a", "k1", {"v": 1})

        self.assertEqual(downstream.publishes, [("topic.a", "k1", {"v": 1})])

    async def test_fan_out_isolates_handler_failures(self) -> None:
        """R3-P1-U-C：fan-out 链里任一 handler 抛异常时，**后续 handler 仍必须
        被调用**，然后再把首个异常 re-raise 以便 NATS NAK 重投。这样单个
        flaky observer 不会连带把顺序靠后的 critical handler 的首次执行窗口
        一起吞掉；重投时再走一遍，所有 handler 都至少被尝试执行一次。"""
        downstream = _RecordingBus()
        bus = _CollectingBus(downstream)
        called: list[str] = []

        async def failing(_: dict) -> None:
            called.append("failing")
            raise RuntimeError("boom")

        async def trailing(_: dict) -> None:
            called.append("trailing")

        async def third(_: dict) -> None:
            called.append("third")

        await bus.subscribe("topic.a", failing)
        await bus.subscribe("topic.a", trailing)
        await bus.subscribe("topic.a", third)
        await bus.flush()

        _, fan_out = downstream.subscriptions[0]
        # 首个 handler 的异常必须 re-raise 给 NATS NAK 重投
        with self.assertRaises(RuntimeError):
            await fan_out({"topic": "topic.a", "key": "k", "payload": {}})

        # 所有 handler 都应该被调用，不因 failing 抛错而跳过 trailing/third
        self.assertEqual(called, ["failing", "trailing", "third"])

    async def test_fan_out_reraises_first_exception_even_if_later_also_fails(self) -> None:
        """R3-P1-U-C：多个 handler 同时抛错时，re-raise 第一个，避免异常链混乱
        （否则 NATS 拿到最后一个异常，log 上下文不匹配真正第一个失败的 handler）。"""
        downstream = _RecordingBus()
        bus = _CollectingBus(downstream)

        async def first_fail(_: dict) -> None:
            raise RuntimeError("first_boom")

        async def second_fail(_: dict) -> None:
            raise ValueError("second_boom")

        await bus.subscribe("topic.a", first_fail)
        await bus.subscribe("topic.a", second_fail)
        await bus.flush()

        _, fan_out = downstream.subscriptions[0]
        with self.assertRaises(RuntimeError) as ctx:
            await fan_out({"topic": "topic.a", "key": "k", "payload": {}})
        self.assertEqual(str(ctx.exception), "first_boom")


    async def test_fan_out_runs_handlers_concurrently(self) -> None:
        """2026-04-23 P1-b 锚定：fan_out 必须并行调 handlers，而不是串行。

        串行实现下，3 个 sleep(0.1s) 的 handler 总耗时 ≥ 0.3s。
        并行（asyncio.gather）下，总耗时 ≈ 0.1s（所有 sleep 并发）。

        这保证了慢 observer 不会拖累 cache handler 的新鲜度，也不会把
        NATS ack 延迟累加——这正是 pre-fix 串行版本的核心痛点。
        """
        downstream = _RecordingBus()
        bus = _CollectingBus(downstream)

        release = asyncio.Event()
        ready_events = [asyncio.Event() for _ in range(3)]
        next_ready_index = 0

        async def gated(_: dict) -> None:
            nonlocal next_ready_index
            ready_event = ready_events[next_ready_index]
            next_ready_index += 1
            ready_event.set()
            await release.wait()

        await bus.subscribe("topic.a", gated)
        await bus.subscribe("topic.a", gated)
        await bus.subscribe("topic.a", gated)
        await bus.flush()

        _, fan_out = downstream.subscriptions[0]
        fan_out_task = asyncio.create_task(
            fan_out({"topic": "topic.a", "key": "k", "payload": {}})
        )
        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in ready_events)),
            timeout=1.0,
        )
        self.assertFalse(fan_out_task.done(), "fan_out 不应在 release 前完成")

        release.set()
        await asyncio.wait_for(fan_out_task, timeout=1.0)

    async def test_fan_out_one_slow_handler_does_not_block_others(self) -> None:
        """2026-04-23 P1-b 锚定：一个慢 handler 不应阻塞其他 handler 的开始执行。

        在串行版本里，handler[1] 必须等 handler[0] await 完才开始；并行版本
        里，所有 handler 几乎同时进入 body。用 timestamp 验证"几乎同时启动"
        而不是"顺序启动"。
        """
        downstream = _RecordingBus()
        bus = _CollectingBus(downstream)
        start_times: list[float] = []

        async def slow(_: dict) -> None:
            start_times.append(time.perf_counter())
            await asyncio.sleep(0.1)

        async def fast(_: dict) -> None:
            start_times.append(time.perf_counter())

        await bus.subscribe("topic.a", slow)
        await bus.subscribe("topic.a", fast)
        await bus.flush()

        _, fan_out = downstream.subscriptions[0]
        await fan_out({"topic": "topic.a", "key": "k", "payload": {}})

        # 两个 handler 都应该被调用
        self.assertEqual(len(start_times), 2)
        # 两者启动时间差应该 < 10ms（并行 scheduled），而非 ≥ 100ms（串行 await）
        time_diff = abs(start_times[1] - start_times[0])
        self.assertLess(
            time_diff,
            0.050,
            f"fast handler 启动被 slow handler 阻塞（差 {time_diff*1000:.1f}ms）",
        )


if __name__ == "__main__":
    unittest.main()
