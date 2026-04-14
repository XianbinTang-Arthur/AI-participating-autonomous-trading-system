"""Unit tests for ``aats.services.operator.command_bridge``.

Coverage target（设计文档：docs/task/slice_4proc_operator_command_proxy_fix_design.md §6）：

Client:
    * bootstrap() 幂等 + subscribe response topic
    * invoke() 在未 bootstrap 时抛 OperatorCommandError
    * invoke() 在 stop 后抛 OperatorCommandError
    * invoke() 成功路径：返回 worker 的 result dict
    * invoke() 远端错误路径：抛 OperatorCommandRemoteError
    * invoke() 超时路径：抛 OperatorCommandTimeoutError
    * _handle_response() 未知 correlation_id 不抛不挂

Worker:
    * bootstrap() 幂等 + subscribe request topic
    * _handle_request() dispatch 已注册命令 → 成功响应
    * _handle_request() 未知命令 → success=False + UnknownCommandError
    * _handle_request() handler raise → success=False + 原 exc 类名
    * stop() 后 _handle_request 直接 no-op

End-to-end（client + worker 挂同一 InMemoryEventBus）:
    * rebaseline 成功路径
    * rebaseline 远端抛错

用 InMemoryEventBus 而不是 NATS 的原因：
    - 本层只测代理链路（序列化 + correlation_id + dispatch），不测 NATS
      本身。NATS 的 at-least-once / ack / replay 行为在
      test_nats_bus_skeleton 里覆盖。
    - InMemoryEventBus.publish 是同步 dispatch：发请求进去的同时就把
      handler call 完，配合 future resolve 可以让 invoke() 在 await
      publish 之后立即拿到响应，不需要 sleep / event loop 心跳。
"""
from __future__ import annotations

import asyncio
import logging
import unittest
from typing import Any

from aats.bus.memory_bus import InMemoryEventBus
from aats.events import topics
from aats.schemas.common import EventEnvelope, dump_payload_exact
from aats.schemas.operator_command import (
    OperatorCommandRequest,
    OperatorCommandResponse,
)
from aats.services.operator.command_bridge import (
    OperatorCommandClient,
    OperatorCommandError,
    OperatorCommandRemoteError,
    OperatorCommandTimeoutError,
    OperatorCommandWorker,
)


def _make_logger() -> logging.Logger:
    # 用标准 logging.Logger 而不是 aats.bootstrap.logging.get_logger，
    # 保持测试对 log_event 的行为断言最小化（log_event 会 accept 任意
    # logger 对象，只要有 log/info/error 等方法）。
    return logging.getLogger("test_operator_command_bridge")


class TestOperatorCommandClient(unittest.IsolatedAsyncioTestCase):
    async def test_invoke_before_bootstrap_raises(self) -> None:
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")
        client = OperatorCommandClient(
            bus=bus,
            process_role="gateway",
            logger=_make_logger(),
        )
        with self.assertRaises(OperatorCommandError) as ctx:
            await client.invoke(command="rebaseline", payload={})
        self.assertIn("not_bootstrapped", str(ctx.exception))

    async def test_bootstrap_is_idempotent(self) -> None:
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")
        client = OperatorCommandClient(
            bus=bus,
            process_role="gateway",
            logger=_make_logger(),
        )
        await client.bootstrap()
        await client.bootstrap()
        # 两次 bootstrap 不应导致 double subscribe（InMemoryEventBus 没去重，
        # 但 client 内部 _subscribed flag 会拦下第二次）
        self.assertEqual(len(bus._subs[topics.OPERATOR_COMMAND_RESPONSES]), 1)

    async def test_invoke_after_stop_raises(self) -> None:
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")
        client = OperatorCommandClient(
            bus=bus,
            process_role="gateway",
            logger=_make_logger(),
        )
        await client.bootstrap()
        await client.stop()
        with self.assertRaises(OperatorCommandError) as ctx:
            await client.invoke(command="rebaseline", payload={})
        self.assertIn("stopped", str(ctx.exception))

    async def test_invoke_timeout_raises_timeout_error(self) -> None:
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")
        client = OperatorCommandClient(
            bus=bus,
            process_role="gateway",
            logger=_make_logger(),
            timeout_seconds=0.05,
        )
        await client.bootstrap()
        # 没有 worker 挂在这条 bus 上，publish 请求后不会有响应。
        with self.assertRaises(OperatorCommandTimeoutError) as ctx:
            await client.invoke(command="rebaseline", payload={"reason": "test"})
        self.assertEqual(ctx.exception.timeout_seconds, 0.05)
        self.assertTrue(ctx.exception.correlation_id.startswith("opcmd_"))

    async def test_handle_response_unknown_correlation_id_is_dropped(self) -> None:
        """陈旧响应或 client 侧已超时 pop 的 entry 不应让 handler 抛异常。"""
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")
        client = OperatorCommandClient(
            bus=bus,
            process_role="gateway",
            logger=_make_logger(),
        )
        await client.bootstrap()

        # 构造一条 response，correlation_id 不在 _pending
        response = OperatorCommandResponse(
            correlation_id="opcmd_stale_not_in_pending",
            success=True,
            result={"ok": True},
            responder_role="execution",
        )
        envelope = EventEnvelope(
            event_type="OperatorCommandResponse",
            source_component="test",
            topic=topics.OPERATOR_COMMAND_RESPONSES,
            key=response.correlation_id,
            payload=dump_payload_exact(response),
        )
        message = {
            "topic": envelope.topic,
            "key": envelope.key,
            "payload": envelope.model_dump(mode="json"),
        }
        # 不应抛异常（handler 会 log warning 后 return）
        await client._handle_response(message)

    async def test_handle_response_parse_error_is_dropped(self) -> None:
        """畸形 payload 不应让 handler 抛异常污染订阅流。"""
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")
        client = OperatorCommandClient(
            bus=bus,
            process_role="gateway",
            logger=_make_logger(),
        )
        await client.bootstrap()

        # 直接传畸形 message（缺 payload key）
        await client._handle_response({"topic": "x", "key": "y"})

    async def test_stop_cancels_pending_futures(self) -> None:
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")
        client = OperatorCommandClient(
            bus=bus,
            process_role="gateway",
            logger=_make_logger(),
            timeout_seconds=5.0,
        )
        await client.bootstrap()

        # 手动塞一个 pending future（模拟已发出但未收到响应）
        loop = asyncio.get_running_loop()
        future: asyncio.Future[OperatorCommandResponse] = loop.create_future()
        client._pending["opcmd_test"] = future

        await client.stop()

        self.assertTrue(future.done())
        with self.assertRaises(OperatorCommandError):
            future.result()


class TestOperatorCommandWorker(unittest.IsolatedAsyncioTestCase):
    async def test_bootstrap_subscribes_to_request_topic(self) -> None:
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")

        async def _noop_handler(payload: dict[str, Any]) -> dict[str, Any]:
            return {}

        worker = OperatorCommandWorker(
            bus=bus,
            process_role="execution",
            logger=_make_logger(),
            command_handlers={"rebaseline": _noop_handler},
        )
        await worker.bootstrap()
        await worker.bootstrap()  # 幂等
        self.assertEqual(len(bus._subs[topics.OPERATOR_COMMAND_REQUESTS]), 1)

    async def test_dispatch_success_publishes_response_envelope(self) -> None:
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")
        captured_payloads: list[dict[str, Any]] = []

        async def _rebaseline_handler(payload: dict[str, Any]) -> dict[str, Any]:
            captured_payloads.append(payload)
            return {"baseline_status": "baseline_imported", "event_id": "evt_123"}

        worker = OperatorCommandWorker(
            bus=bus,
            process_role="execution",
            logger=_make_logger(),
            command_handlers={"rebaseline": _rebaseline_handler},
        )
        await worker.bootstrap()

        # 订阅响应 topic 抓响应
        responses: list[dict[str, Any]] = []

        async def _response_collector(message: dict[str, Any]) -> None:
            envelope = EventEnvelope.model_validate(message["payload"])
            responses.append(envelope.payload)

        await bus.subscribe(topics.OPERATOR_COMMAND_RESPONSES, _response_collector)

        # 构造一条请求并投递
        request = OperatorCommandRequest(
            correlation_id="opcmd_test_success",
            command="rebaseline",
            payload={"reason": "unit_test", "actor_role": "admin"},
            requested_by_role="gateway",
        )
        request_envelope = EventEnvelope(
            event_type="OperatorCommandRequest",
            source_component="test",
            topic=topics.OPERATOR_COMMAND_REQUESTS,
            key=request.correlation_id,
            payload=dump_payload_exact(request),
        )
        await bus.publish(
            topic=topics.OPERATOR_COMMAND_REQUESTS,
            key=request.correlation_id,
            payload=request_envelope.model_dump(mode="json"),
        )

        # Handler 被调用，payload 与请求匹配
        self.assertEqual(len(captured_payloads), 1)
        self.assertEqual(captured_payloads[0]["reason"], "unit_test")
        self.assertEqual(captured_payloads[0]["actor_role"], "admin")

        # 响应被发布，成功且 correlation_id 一致
        self.assertEqual(len(responses), 1)
        response = OperatorCommandResponse.model_validate(responses[0])
        self.assertEqual(response.correlation_id, "opcmd_test_success")
        self.assertTrue(response.success)
        self.assertEqual(response.result, {"baseline_status": "baseline_imported", "event_id": "evt_123"})
        self.assertEqual(response.responder_role, "execution")
        self.assertIsNone(response.error_type)

    async def test_unknown_command_returns_error_response(self) -> None:
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")

        async def _rebaseline_handler(payload: dict[str, Any]) -> dict[str, Any]:
            return {}

        worker = OperatorCommandWorker(
            bus=bus,
            process_role="execution",
            logger=_make_logger(),
            command_handlers={"rebaseline": _rebaseline_handler},
        )
        await worker.bootstrap()

        responses: list[dict[str, Any]] = []

        async def _response_collector(message: dict[str, Any]) -> None:
            envelope = EventEnvelope.model_validate(message["payload"])
            responses.append(envelope.payload)

        await bus.subscribe(topics.OPERATOR_COMMAND_RESPONSES, _response_collector)

        request = OperatorCommandRequest(
            correlation_id="opcmd_unknown",
            command="resume",  # 未注册
            payload={},
            requested_by_role="gateway",
        )
        request_envelope = EventEnvelope(
            event_type="OperatorCommandRequest",
            source_component="test",
            topic=topics.OPERATOR_COMMAND_REQUESTS,
            key=request.correlation_id,
            payload=dump_payload_exact(request),
        )
        await bus.publish(
            topic=topics.OPERATOR_COMMAND_REQUESTS,
            key=request.correlation_id,
            payload=request_envelope.model_dump(mode="json"),
        )

        self.assertEqual(len(responses), 1)
        response = OperatorCommandResponse.model_validate(responses[0])
        self.assertFalse(response.success)
        self.assertEqual(response.error_type, "UnknownCommandError")
        self.assertIn("resume", response.error_message or "")

    async def test_handler_exception_becomes_error_response(self) -> None:
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")

        async def _failing_handler(payload: dict[str, Any]) -> dict[str, Any]:
            raise ValueError("rebaseline_requires_okx_account_read")

        worker = OperatorCommandWorker(
            bus=bus,
            process_role="execution",
            logger=_make_logger(),
            command_handlers={"rebaseline": _failing_handler},
        )
        await worker.bootstrap()

        responses: list[dict[str, Any]] = []

        async def _response_collector(message: dict[str, Any]) -> None:
            envelope = EventEnvelope.model_validate(message["payload"])
            responses.append(envelope.payload)

        await bus.subscribe(topics.OPERATOR_COMMAND_RESPONSES, _response_collector)

        request = OperatorCommandRequest(
            correlation_id="opcmd_failing",
            command="rebaseline",
            payload={"reason": "test"},
            requested_by_role="gateway",
        )
        request_envelope = EventEnvelope(
            event_type="OperatorCommandRequest",
            source_component="test",
            topic=topics.OPERATOR_COMMAND_REQUESTS,
            key=request.correlation_id,
            payload=dump_payload_exact(request),
        )
        await bus.publish(
            topic=topics.OPERATOR_COMMAND_REQUESTS,
            key=request.correlation_id,
            payload=request_envelope.model_dump(mode="json"),
        )

        self.assertEqual(len(responses), 1)
        response = OperatorCommandResponse.model_validate(responses[0])
        self.assertFalse(response.success)
        self.assertEqual(response.error_type, "ValueError")
        self.assertEqual(response.error_message, "rebaseline_requires_okx_account_read")

    async def test_stop_makes_handler_noop(self) -> None:
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")
        handler_called = []

        async def _rebaseline_handler(payload: dict[str, Any]) -> dict[str, Any]:
            handler_called.append(payload)
            return {}

        worker = OperatorCommandWorker(
            bus=bus,
            process_role="execution",
            logger=_make_logger(),
            command_handlers={"rebaseline": _rebaseline_handler},
        )
        await worker.bootstrap()
        await worker.stop()

        # 停机后再来一条请求，handler 不应被调用
        request = OperatorCommandRequest(
            correlation_id="opcmd_after_stop",
            command="rebaseline",
            payload={},
            requested_by_role="gateway",
        )
        request_envelope = EventEnvelope(
            event_type="OperatorCommandRequest",
            source_component="test",
            topic=topics.OPERATOR_COMMAND_REQUESTS,
            key=request.correlation_id,
            payload=dump_payload_exact(request),
        )
        await bus.publish(
            topic=topics.OPERATOR_COMMAND_REQUESTS,
            key=request.correlation_id,
            payload=request_envelope.model_dump(mode="json"),
        )

        self.assertEqual(handler_called, [])


class TestOperatorCommandBridgeEndToEnd(unittest.IsolatedAsyncioTestCase):
    """Client + Worker 挂同一 InMemoryEventBus，走完整 roundtrip。"""

    async def test_rebaseline_success_e2e(self) -> None:
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")

        received_payloads: list[dict[str, Any]] = []

        async def _rebaseline_handler(payload: dict[str, Any]) -> dict[str, Any]:
            received_payloads.append(payload)
            return {
                "baseline_status": "baseline_imported",
                "baseline_event_id": "evt_xxx",
                "recovery_state": "rebaseline_pending",
            }

        worker = OperatorCommandWorker(
            bus=bus,
            process_role="execution",
            logger=_make_logger(),
            command_handlers={"rebaseline": _rebaseline_handler},
        )
        await worker.bootstrap()

        client = OperatorCommandClient(
            bus=bus,
            process_role="gateway",
            logger=_make_logger(),
            timeout_seconds=5.0,
        )
        await client.bootstrap()

        result = await client.invoke(
            command="rebaseline",
            payload={
                "reason": "e2e_test",
                "actor_role": "admin",
                "actor_identity": "tester",
                "auth_source": "password",
            },
        )

        # Handler 收到预期的 payload
        self.assertEqual(len(received_payloads), 1)
        self.assertEqual(received_payloads[0]["reason"], "e2e_test")
        self.assertEqual(received_payloads[0]["actor_role"], "admin")
        self.assertEqual(received_payloads[0]["actor_identity"], "tester")

        # Client 拿到 handler 返回的 result dict
        self.assertEqual(result["baseline_status"], "baseline_imported")
        self.assertEqual(result["baseline_event_id"], "evt_xxx")
        self.assertEqual(result["recovery_state"], "rebaseline_pending")

        # Client _pending 已清空
        self.assertEqual(client._pending, {})

        await client.stop()
        await worker.stop()

    async def test_rebaseline_handler_error_raises_remote_error(self) -> None:
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")

        async def _failing_handler(payload: dict[str, Any]) -> dict[str, Any]:
            raise ValueError("rebaseline_requires_okx_account_read")

        worker = OperatorCommandWorker(
            bus=bus,
            process_role="execution",
            logger=_make_logger(),
            command_handlers={"rebaseline": _failing_handler},
        )
        await worker.bootstrap()

        client = OperatorCommandClient(
            bus=bus,
            process_role="gateway",
            logger=_make_logger(),
            timeout_seconds=5.0,
        )
        await client.bootstrap()

        with self.assertRaises(OperatorCommandRemoteError) as ctx:
            await client.invoke(
                command="rebaseline",
                payload={"reason": "test"},
            )
        self.assertEqual(ctx.exception.error_type, "ValueError")
        self.assertEqual(
            ctx.exception.error_message,
            "rebaseline_requires_okx_account_read",
        )

        # 即使出错 _pending 也要被清空（finally 块）
        self.assertEqual(client._pending, {})

        await client.stop()
        await worker.stop()

    async def test_e2e_two_concurrent_rebaselines_are_serialized_by_worker(self) -> None:
        """Worker 的 asyncio.Lock 保证并发请求 serial 执行。"""
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")

        call_order: list[str] = []

        async def _slow_handler(payload: dict[str, Any]) -> dict[str, Any]:
            tag = payload["tag"]
            call_order.append(f"{tag}:start")
            await asyncio.sleep(0.01)
            call_order.append(f"{tag}:end")
            return {"tag": tag}

        worker = OperatorCommandWorker(
            bus=bus,
            process_role="execution",
            logger=_make_logger(),
            command_handlers={"rebaseline": _slow_handler},
        )
        await worker.bootstrap()

        client = OperatorCommandClient(
            bus=bus,
            process_role="gateway",
            logger=_make_logger(),
            timeout_seconds=5.0,
        )
        await client.bootstrap()

        # InMemoryEventBus.publish 是同步 dispatch，两个 invoke() 必须
        # 用 asyncio.gather 并发起，才可能让 worker 观察到 lock 竞争。
        # 注：同步 dispatch 意味着第一条 publish 会深度同步到 handler
        # 里的 await sleep，此时第二条 invoke 会抢到主 event loop；
        # 但 worker 的 lock 会让第二条的 _dispatch 等第一条释放。
        results = await asyncio.gather(
            client.invoke(command="rebaseline", payload={"tag": "A"}),
            client.invoke(command="rebaseline", payload={"tag": "B"}),
        )

        self.assertEqual({r["tag"] for r in results}, {"A", "B"})
        # Serial 序：不能出现 A:start → B:start → A:end → B:end（交错）。
        # 合法序只有 A→B 或 B→A。
        self.assertIn(
            call_order,
            [
                ["A:start", "A:end", "B:start", "B:end"],
                ["B:start", "B:end", "A:start", "A:end"],
            ],
        )

        await client.stop()
        await worker.stop()


class TestOperatorCommandClientCancelledError(unittest.IsolatedAsyncioTestCase):
    async def test_invoke_cancelled_cleans_pending_and_reraises(self) -> None:
        """invoke() 被 asyncio.cancel → CancelledError 上抛 + _pending 清空。"""
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")
        client = OperatorCommandClient(
            bus=bus,
            process_role="gateway",
            logger=_make_logger(),
            timeout_seconds=5.0,
        )
        await client.bootstrap()

        async def _invoke() -> dict:
            return await client.invoke(command="rebaseline", payload={"reason": "cancel_test"})

        task = asyncio.create_task(_invoke())
        await asyncio.sleep(0.01)  # 让 invoke 进入 wait_for
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(client._pending, {})


class TestOperatorCommandClientPublishFailure(unittest.IsolatedAsyncioTestCase):
    async def test_publish_failure_cleans_pending_and_reraises(self) -> None:
        """bus.publish 抛异常 → _pending 清空 + 原异常上抛。"""
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")
        client = OperatorCommandClient(
            bus=bus,
            process_role="gateway",
            logger=_make_logger(),
        )
        await client.bootstrap()

        # 让 bus.publish 抛异常
        original_publish = bus.publish

        async def _failing_publish(**kwargs: Any) -> None:
            raise ConnectionError("nats_disconnected")

        bus.publish = _failing_publish  # type: ignore[assignment]
        try:
            with self.assertRaises(ConnectionError):
                await client.invoke(command="rebaseline", payload={})
            self.assertEqual(client._pending, {})
        finally:
            bus.publish = original_publish  # type: ignore[assignment]


class TestOperatorCommandWorkerDedup(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_correlation_id_is_dropped(self) -> None:
        """同一 correlation_id 第二次投递 → worker 不再 dispatch。"""
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")
        call_count = 0

        async def _counting_handler(payload: dict[str, Any]) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            return {"count": call_count}

        worker = OperatorCommandWorker(
            bus=bus,
            process_role="execution",
            logger=_make_logger(),
            command_handlers={"rebaseline": _counting_handler},
        )
        await worker.bootstrap()

        request = OperatorCommandRequest(
            correlation_id="opcmd_dedup_test",
            command="rebaseline",
            payload={"reason": "dedup_test"},
            requested_by_role="gateway",
        )
        request_envelope = EventEnvelope(
            event_type="OperatorCommandRequest",
            source_component="test",
            topic=topics.OPERATOR_COMMAND_REQUESTS,
            key=request.correlation_id,
            payload=dump_payload_exact(request),
        )
        msg = request_envelope.model_dump(mode="json")

        # 第一次投递
        await bus.publish(
            topic=topics.OPERATOR_COMMAND_REQUESTS,
            key=request.correlation_id,
            payload=msg,
        )
        self.assertEqual(call_count, 1)

        # 第二次投递（模拟 NATS ack_wait 重投）
        await bus.publish(
            topic=topics.OPERATOR_COMMAND_REQUESTS,
            key=request.correlation_id,
            payload=msg,
        )
        # Handler 不应被再次调用
        self.assertEqual(call_count, 1)

    async def test_processed_ids_bounded_by_maxlen(self) -> None:
        """_processed_ids 超过 maxlen 后自动淘汰最旧的 entry。"""
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")

        async def _noop_handler(payload: dict[str, Any]) -> dict[str, Any]:
            return {}

        worker = OperatorCommandWorker(
            bus=bus,
            process_role="execution",
            logger=_make_logger(),
            command_handlers={"rebaseline": _noop_handler},
        )
        worker._processed_ids_maxlen = 3  # 缩小 maxlen 便于测试
        await worker.bootstrap()

        for i in range(5):
            cid = f"opcmd_bound_{i}"
            request = OperatorCommandRequest(
                correlation_id=cid,
                command="rebaseline",
                payload={},
                requested_by_role="gateway",
            )
            envelope = EventEnvelope(
                event_type="OperatorCommandRequest",
                source_component="test",
                topic=topics.OPERATOR_COMMAND_REQUESTS,
                key=cid,
                payload=dump_payload_exact(request),
            )
            await bus.publish(
                topic=topics.OPERATOR_COMMAND_REQUESTS,
                key=cid,
                payload=envelope.model_dump(mode="json"),
            )

        # maxlen=3，5 条后只保留最近 3 条
        self.assertEqual(len(worker._processed_ids), 3)
        self.assertNotIn("opcmd_bound_0", worker._processed_ids)
        self.assertNotIn("opcmd_bound_1", worker._processed_ids)
        self.assertIn("opcmd_bound_2", worker._processed_ids)
        self.assertIn("opcmd_bound_3", worker._processed_ids)
        self.assertIn("opcmd_bound_4", worker._processed_ids)


class TestOperatorCommandWorkerDedupConcurrent(unittest.IsolatedAsyncioTestCase):
    """测试 lock 内 double-check dedup 能防止并发重投。

    场景：handler 较慢（sleep），两条同 correlation_id 的消息通过
    asyncio.create_task 并发投递。第一条拿到 lock 执行 handler，第二条
    在 lock 外等待；第一条释放 lock 后，第二条进入 lock 但发现 correlation_id
    已在 _processed_ids → 跳过。最终 handler 只被调用一次。
    """

    async def test_concurrent_duplicate_only_dispatches_once(self) -> None:
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")
        call_count = 0

        async def _slow_handler(payload: dict[str, Any]) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.05)  # 足够让第二条消息排队
            return {"count": call_count}

        worker = OperatorCommandWorker(
            bus=bus,
            process_role="execution",
            logger=_make_logger(),
            command_handlers={"rebaseline": _slow_handler},
        )
        await worker.bootstrap()

        request = OperatorCommandRequest(
            correlation_id="opcmd_concurrent_dedup",
            command="rebaseline",
            payload={"reason": "concurrent_test"},
            requested_by_role="gateway",
        )
        envelope = EventEnvelope(
            event_type="OperatorCommandRequest",
            source_component="test",
            topic=topics.OPERATOR_COMMAND_REQUESTS,
            key=request.correlation_id,
            payload=dump_payload_exact(request),
        )
        msg = {
            "topic": envelope.topic,
            "key": envelope.key,
            "payload": envelope.model_dump(mode="json"),
        }

        # 用 create_task 并发投递同一条消息，模拟 NATS ack_wait 重投
        t1 = asyncio.create_task(worker._handle_request(msg))
        t2 = asyncio.create_task(worker._handle_request(msg))
        await asyncio.gather(t1, t2)

        # handler 只执行一次
        self.assertEqual(call_count, 1)
        # correlation_id 在已处理集合中
        self.assertIn("opcmd_concurrent_dedup", worker._processed_ids)


class TestOperatorCommandWorkerPublishFailureDedup(unittest.IsolatedAsyncioTestCase):
    """响应 publish 失败后，correlation_id 仍在 _processed_ids。

    场景：handler 成功执行，但 response publish 抛异常（NATS 断连）。
    此时业务已完成，dedup 标记必须保留，否则 NATS 重投会导致 handler
    被二次执行。
    """

    async def test_publish_failure_preserves_dedup_mark(self) -> None:
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")
        call_count = 0

        async def _counting_handler(payload: dict[str, Any]) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            return {"count": call_count}

        worker = OperatorCommandWorker(
            bus=bus,
            process_role="execution",
            logger=_make_logger(),
            command_handlers={"rebaseline": _counting_handler},
        )
        await worker.bootstrap()

        # 让 response publish 抛异常
        original_publish = bus.publish

        async def _failing_response_publish(*, topic: str, key: str, payload: Any) -> None:
            if topic == topics.OPERATOR_COMMAND_RESPONSES:
                raise ConnectionError("nats_disconnected")
            return await original_publish(topic=topic, key=key, payload=payload)

        bus.publish = _failing_response_publish  # type: ignore[assignment]

        request = OperatorCommandRequest(
            correlation_id="opcmd_publish_fail",
            command="rebaseline",
            payload={"reason": "publish_fail_test"},
            requested_by_role="gateway",
        )
        envelope = EventEnvelope(
            event_type="OperatorCommandRequest",
            source_component="test",
            topic=topics.OPERATOR_COMMAND_REQUESTS,
            key=request.correlation_id,
            payload=dump_payload_exact(request),
        )
        msg = {
            "topic": envelope.topic,
            "key": envelope.key,
            "payload": envelope.model_dump(mode="json"),
        }

        # 第一次投递：handler 成功，publish 失败（静默）
        await worker._handle_request(msg)
        self.assertEqual(call_count, 1)
        # dedup 标记仍在
        self.assertIn("opcmd_publish_fail", worker._processed_ids)

        # 第二次投递（模拟 NATS 重投）：dedup 命中，handler 不再执行
        await worker._handle_request(msg)
        self.assertEqual(call_count, 1)

        bus.publish = original_publish  # type: ignore[assignment]


class TestOperatorCommandWorkerParseError(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_request_is_dropped(self) -> None:
        """畸形请求不应让 worker handler 抛异常。"""
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")

        async def _noop_handler(payload: dict[str, Any]) -> dict[str, Any]:
            return {}

        worker = OperatorCommandWorker(
            bus=bus,
            process_role="execution",
            logger=_make_logger(),
            command_handlers={"rebaseline": _noop_handler},
        )
        await worker.bootstrap()

        # 直接传畸形 message
        await worker._handle_request({"topic": "x", "key": "y"})
        # 不抛即通过


class TestOperatorCommandClientFutureDone(unittest.IsolatedAsyncioTestCase):
    """覆盖 _handle_response 中 future.done() 已完成的分支。"""

    async def test_response_after_timeout_is_dropped(self) -> None:
        """invoke 超时后迟到的响应 → future.done()=True → log warning 后丢弃。"""
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")
        client = OperatorCommandClient(
            bus=bus,
            process_role="gateway",
            logger=_make_logger(),
            timeout_seconds=0.02,
        )
        await client.bootstrap()

        # invoke 会超时，但 correlation_id 在 finally 里被 pop
        with self.assertRaises(OperatorCommandTimeoutError):
            await client.invoke(command="rebaseline", payload={"reason": "timeout_test"})

        # 手动回注一个已 done 的 future 模拟迟到响应场景
        loop = asyncio.get_running_loop()
        done_future: asyncio.Future[OperatorCommandResponse] = loop.create_future()
        done_future.set_result(
            OperatorCommandResponse(
                correlation_id="opcmd_stale",
                success=True,
                result={},
                responder_role="execution",
            )
        )
        client._pending["opcmd_stale"] = done_future

        # 构造迟到响应
        late_response = OperatorCommandResponse(
            correlation_id="opcmd_stale",
            success=True,
            result={"late": True},
            responder_role="execution",
        )
        envelope = EventEnvelope(
            event_type="OperatorCommandResponse",
            source_component="test",
            topic=topics.OPERATOR_COMMAND_RESPONSES,
            key=late_response.correlation_id,
            payload=dump_payload_exact(late_response),
        )
        msg = {
            "topic": envelope.topic,
            "key": envelope.key,
            "payload": envelope.model_dump(mode="json"),
        }
        # 不应抛异常（走 future.done() early return）
        await client._handle_response(msg)


class TestOperatorCommandClientBootstrapFailure(unittest.IsolatedAsyncioTestCase):
    async def test_subscribe_failure_keeps_unsubscribed(self) -> None:
        """bus.subscribe 抛异常 → _subscribed 保持 False → 后续 invoke 仍报未启动。"""
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")
        client = OperatorCommandClient(
            bus=bus,
            process_role="gateway",
            logger=_make_logger(),
        )

        original_subscribe = bus.subscribe

        async def _failing_subscribe(topic: str, handler: Any) -> None:
            raise ConnectionError("nats_connect_failed")

        bus.subscribe = _failing_subscribe  # type: ignore[assignment]
        try:
            with self.assertRaises(ConnectionError):
                await client.bootstrap()
            self.assertFalse(client._subscribed)
            with self.assertRaises(OperatorCommandError):
                await client.invoke(command="rebaseline", payload={})
        finally:
            bus.subscribe = original_subscribe  # type: ignore[assignment]


class TestOperatorCommandWorkerBootstrapFailure(unittest.IsolatedAsyncioTestCase):
    async def test_subscribe_failure_keeps_unsubscribed(self) -> None:
        """bus.subscribe 抛异常 → _subscribed 保持 False。"""
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")

        async def _noop(payload: dict[str, Any]) -> dict[str, Any]:
            return {}

        worker = OperatorCommandWorker(
            bus=bus,
            process_role="execution",
            logger=_make_logger(),
            command_handlers={"rebaseline": _noop},
        )

        original_subscribe = bus.subscribe

        async def _failing_subscribe(topic: str, handler: Any) -> None:
            raise ConnectionError("nats_connect_failed")

        bus.subscribe = _failing_subscribe  # type: ignore[assignment]
        try:
            with self.assertRaises(ConnectionError):
                await worker.bootstrap()
            self.assertFalse(worker._subscribed)
        finally:
            bus.subscribe = original_subscribe  # type: ignore[assignment]


class TestOperatorCommandWorkerNonDictResult(unittest.IsolatedAsyncioTestCase):
    async def test_handler_returning_none_yields_empty_result(self) -> None:
        """handler 返回 None → response.result 降级为 {} + warning 日志。"""
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")

        async def _none_handler(payload: dict[str, Any]) -> dict[str, Any]:
            return None  # type: ignore[return-value]

        worker = OperatorCommandWorker(
            bus=bus,
            process_role="execution",
            logger=_make_logger(),
            command_handlers={"rebaseline": _none_handler},
        )
        await worker.bootstrap()

        responses: list[dict[str, Any]] = []

        async def _collector(message: dict[str, Any]) -> None:
            envelope = EventEnvelope.model_validate(message["payload"])
            responses.append(envelope.payload)

        await bus.subscribe(topics.OPERATOR_COMMAND_RESPONSES, _collector)

        request = OperatorCommandRequest(
            correlation_id="opcmd_none_result",
            command="rebaseline",
            payload={},
            requested_by_role="gateway",
        )
        envelope = EventEnvelope(
            event_type="OperatorCommandRequest",
            source_component="test",
            topic=topics.OPERATOR_COMMAND_REQUESTS,
            key=request.correlation_id,
            payload=dump_payload_exact(request),
        )
        await bus.publish(
            topic=topics.OPERATOR_COMMAND_REQUESTS,
            key=request.correlation_id,
            payload=envelope.model_dump(mode="json"),
        )

        self.assertEqual(len(responses), 1)
        response = OperatorCommandResponse.model_validate(responses[0])
        self.assertTrue(response.success)
        self.assertEqual(response.result, {})


if __name__ == "__main__":
    unittest.main()
