"""Unit tests for the AI command cross-process proxy.

Covers:
  * OperatorCommandClient/Worker with custom ``request_topic`` /
    ``response_topic`` (AI_COMMAND_* instead of OPERATOR_COMMAND_*) keeps
    the existing execution bridge isolated.
  * End-to-end dispatch for the 3 AI commands via InMemoryEventBus:
      - ai_operating_mode_select → set_ai_operating_mode
      - ai_review_restore        → resolve_outcome_review_restore_ai
      - ai_review_degrade_to_baseline
  * query_service.py gateway fallback: when runtime.ai_service is None and
    runtime.ai_command_client is set, the 3 AI mutate methods invoke the
    client instead of raising.
  * When both ai_service AND ai_command_client are missing, raising
    ``ai_service_not_loaded_in_this_process_role`` preserves the pre-refactor
    error semantics (covers monolith/market runtime roles that should not
    accept these mutates at all).
"""
from __future__ import annotations

import logging
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from aats.bus.memory_bus import InMemoryEventBus
from aats.events import topics
from aats.services.operator.command_bridge import (
    OperatorCommandClient,
    OperatorCommandWorker,
)
from aats.services.operator.query_service import OperatorQueryService
from aats.services.operator.runtime_queries import RuntimeQueryFacade


def _make_logger() -> logging.Logger:
    return logging.getLogger("test_ai_command_bridge")


class TestAICommandBridgeTopicIsolation(unittest.IsolatedAsyncioTestCase):
    async def test_ai_bridge_uses_ai_command_topics_not_operator_topics(self) -> None:
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")
        received: list[str] = []

        async def _record_any(_msg: dict[str, Any]) -> None:
            received.append("should_not_fire")

        await bus.subscribe(topics.OPERATOR_COMMAND_REQUESTS, _record_any)

        dispatched_payloads: list[dict[str, Any]] = []

        async def _handle(payload: dict[str, Any]) -> dict[str, Any]:
            dispatched_payloads.append(payload)
            return {"status": "ok"}

        worker = OperatorCommandWorker(
            bus=bus,
            process_role="decision",
            logger=_make_logger(),
            command_handlers={"ai_review_restore": _handle},
            request_topic=topics.AI_COMMAND_REQUESTS,
            response_topic=topics.AI_COMMAND_RESPONSES,
        )
        await worker.bootstrap()

        client = OperatorCommandClient(
            bus=bus,
            process_role="gateway",
            logger=_make_logger(),
            timeout_seconds=5.0,
            request_topic=topics.AI_COMMAND_REQUESTS,
            response_topic=topics.AI_COMMAND_RESPONSES,
        )
        await client.bootstrap()

        result = await client.invoke(
            command="ai_review_restore",
            payload={"reason": "x", "actor_role": "admin"},
        )

        self.assertEqual(result, {"status": "ok"})
        self.assertEqual(len(dispatched_payloads), 1)
        self.assertEqual(dispatched_payloads[0]["reason"], "x")
        # OPERATOR_COMMAND_REQUESTS subscribers must not see AI traffic
        self.assertEqual(received, [])


class TestAICommandEndToEnd(unittest.IsolatedAsyncioTestCase):
    async def _build_pair(
        self,
        handlers: dict[str, Any],
    ) -> tuple[OperatorCommandClient, OperatorCommandWorker]:
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")
        worker = OperatorCommandWorker(
            bus=bus,
            process_role="decision",
            logger=_make_logger(),
            command_handlers=handlers,
            request_topic=topics.AI_COMMAND_REQUESTS,
            response_topic=topics.AI_COMMAND_RESPONSES,
        )
        await worker.bootstrap()
        client = OperatorCommandClient(
            bus=bus,
            process_role="gateway",
            logger=_make_logger(),
            timeout_seconds=5.0,
            request_topic=topics.AI_COMMAND_REQUESTS,
            response_topic=topics.AI_COMMAND_RESPONSES,
        )
        await client.bootstrap()
        return client, worker

    async def test_ai_operating_mode_select_dispatches_mode_and_reason(self) -> None:
        captured: dict[str, Any] = {}

        async def _handle(payload: dict[str, Any]) -> dict[str, Any]:
            captured.update(payload)
            return {"status": "completed", "effective_mode": payload["mode"]}

        client, _ = await self._build_pair({"ai_operating_mode_select": _handle})
        result = await client.invoke(
            command="ai_operating_mode_select",
            payload={
                "mode": "ai_decision_maker",
                "reason": "ui_toggle",
                "actor_role": "admin",
                "actor_identity": "tang",
                "auth_source": "session",
            },
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["effective_mode"], "ai_decision_maker")
        self.assertEqual(captured["mode"], "ai_decision_maker")
        self.assertEqual(captured["actor_identity"], "tang")

    async def test_ai_review_restore_and_degrade_both_dispatch(self) -> None:
        calls: list[str] = []

        async def _restore(payload: dict[str, Any]) -> dict[str, Any]:
            calls.append("restore")
            return {"status": "completed"}

        async def _degrade(payload: dict[str, Any]) -> dict[str, Any]:
            calls.append("degrade")
            return {"status": "completed"}

        client, _ = await self._build_pair(
            {
                "ai_review_restore": _restore,
                "ai_review_degrade_to_baseline": _degrade,
            }
        )
        await client.invoke(
            command="ai_review_restore",
            payload={"reason": "r", "actor_role": "admin"},
        )
        await client.invoke(
            command="ai_review_degrade_to_baseline",
            payload={"reason": "d", "actor_role": "admin"},
        )
        self.assertEqual(calls, ["restore", "degrade"])

    async def test_ai_runtime_status_dispatches_read_only_result(self) -> None:
        async def _runtime_status(_payload: dict[str, Any]) -> dict[str, Any]:
            return {
                "provider": "deepseek",
                "configured": True,
                "ai_service_loaded": True,
                "process_role": "decision",
            }

        client, _ = await self._build_pair({"ai_runtime_status": _runtime_status})

        result = await client.invoke(
            command="ai_runtime_status",
            payload={},
        )

        self.assertEqual(result["provider"], "deepseek")
        self.assertTrue(result["configured"])
        self.assertTrue(result["ai_service_loaded"])
        self.assertEqual(result["process_role"], "decision")


class TestQueryServiceGatewayFallback(unittest.IsolatedAsyncioTestCase):
    """When ai_service is None but ai_command_client is set, the 3 AI mutate
    methods must proxy through the client instead of raising."""

    def _make_runtime(self, *, with_client: bool) -> MagicMock:
        runtime = MagicMock()
        runtime.ai_service = None
        if with_client:
            client = MagicMock()
            client.invoke = AsyncMock(return_value={"status": "proxied"})
            runtime.ai_command_client = client
        else:
            runtime.ai_command_client = None
        return runtime

    async def test_ai_review_restore_proxies_through_client(self) -> None:
        runtime = self._make_runtime(with_client=True)
        service = OperatorQueryService.__new__(OperatorQueryService)
        service.runtime = runtime

        result = await service.ai_review_restore(
            reason="r",
            actor_role="admin",
            actor_identity="tang",
        )

        self.assertEqual(result, {"status": "proxied"})
        runtime.ai_command_client.invoke.assert_awaited_once()
        kwargs = runtime.ai_command_client.invoke.call_args.kwargs
        self.assertEqual(kwargs["command"], "ai_review_restore")
        self.assertEqual(kwargs["payload"]["reason"], "r")
        self.assertEqual(kwargs["payload"]["actor_role"], "admin")
        self.assertEqual(kwargs["payload"]["actor_identity"], "tang")

    async def test_set_ai_operating_mode_proxies_with_mode_in_payload(self) -> None:
        runtime = self._make_runtime(with_client=True)
        service = OperatorQueryService.__new__(OperatorQueryService)
        service.runtime = runtime

        await service.set_ai_operating_mode(
            mode="ai_decision_maker",
            reason="ui_toggle_decision_maker",
            actor_role="admin",
            actor_identity="tang",
            auth_source="session",
        )

        kwargs = runtime.ai_command_client.invoke.call_args.kwargs
        self.assertEqual(kwargs["command"], "ai_operating_mode_select")
        payload = kwargs["payload"]
        self.assertEqual(payload["mode"], "ai_decision_maker")
        self.assertEqual(payload["reason"], "ui_toggle_decision_maker")
        self.assertEqual(payload["actor_role"], "admin")
        self.assertEqual(payload["actor_identity"], "tang")
        self.assertEqual(payload["auth_source"], "session")

    async def test_ai_review_degrade_proxies_through_client(self) -> None:
        runtime = self._make_runtime(with_client=True)
        service = OperatorQueryService.__new__(OperatorQueryService)
        service.runtime = runtime

        await service.ai_review_degrade_to_baseline(
            reason="d",
            actor_role="admin",
        )

        kwargs = runtime.ai_command_client.invoke.call_args.kwargs
        self.assertEqual(kwargs["command"], "ai_review_degrade_to_baseline")
        self.assertEqual(kwargs["payload"]["reason"], "d")

    async def test_ai_runtime_authoritative_proxies_read_status_through_client(self) -> None:
        runtime = self._make_runtime(with_client=True)
        runtime.ai_command_client.invoke = AsyncMock(
            return_value={
                "provider": "deepseek",
                "configured": True,
                "ai_service_loaded": True,
                "process_role": "decision",
            }
        )
        service = OperatorQueryService.__new__(OperatorQueryService)
        service.runtime = runtime
        service.runtime_queries = RuntimeQueryFacade(service)

        result = await service.ai_runtime_authoritative()

        self.assertEqual(result["provider"], "deepseek")
        self.assertTrue(result["configured"])
        runtime.ai_command_client.invoke.assert_awaited_once_with(
            command="ai_runtime_status",
            payload={},
        )

    async def test_missing_ai_service_and_client_preserves_original_error(self) -> None:
        runtime = self._make_runtime(with_client=False)
        service = OperatorQueryService.__new__(OperatorQueryService)
        service.runtime = runtime

        for coro in (
            service.ai_review_restore(reason="r", actor_role="admin"),
            service.set_ai_operating_mode(mode="ai_assisted", reason="r", actor_role="admin"),
            service.ai_review_degrade_to_baseline(reason="r", actor_role="admin"),
        ):
            with self.assertRaises(ValueError) as ctx:
                await coro
            self.assertIn("ai_service_not_loaded_in_this_process_role", str(ctx.exception))


class TestComponentNameLogPrefix(unittest.IsolatedAsyncioTestCase):
    """Regression guard for the ``component_name`` constructor param on
    Client/Worker. If someone accidentally reverts the f-string prefix back
    to the hardcoded ``"operator_command_"`` literal, the AI bridge's
    independent-grep property silently breaks — this test catches it.
    """

    async def test_client_default_component_emits_operator_command_prefix(self) -> None:
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")
        client = OperatorCommandClient(
            bus=bus,
            process_role="gateway",
            logger=_make_logger(),
        )
        with patch("aats.services.operator.command_bridge.log_event") as spy:
            await client.bootstrap()
        event_names = [call.args[1] for call in spy.call_args_list]
        self.assertIn("operator_command_client_subscribed", event_names)
        for name in event_names:
            self.assertFalse(name.startswith("ai_command_"), f"unexpected ai_command event: {name}")

    async def test_client_component_name_ai_emits_ai_command_prefix(self) -> None:
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")
        client = OperatorCommandClient(
            bus=bus,
            process_role="gateway",
            logger=_make_logger(),
            request_topic=topics.AI_COMMAND_REQUESTS,
            response_topic=topics.AI_COMMAND_RESPONSES,
            component_name="ai_command",
        )
        with patch("aats.services.operator.command_bridge.log_event") as spy:
            await client.bootstrap()
        event_names = [call.args[1] for call in spy.call_args_list]
        self.assertIn("ai_command_client_subscribed", event_names)
        for name in event_names:
            self.assertFalse(
                name.startswith("operator_command_"),
                f"AI bridge should not emit operator_command events: {name}",
            )

    async def test_worker_component_name_ai_emits_ai_command_prefix(self) -> None:
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")

        async def _noop(_: dict[str, Any]) -> dict[str, Any]:
            return {"status": "ok"}

        worker = OperatorCommandWorker(
            bus=bus,
            process_role="decision",
            logger=_make_logger(),
            command_handlers={"ai_review_restore": _noop},
            request_topic=topics.AI_COMMAND_REQUESTS,
            response_topic=topics.AI_COMMAND_RESPONSES,
            component_name="ai_command",
        )
        with patch("aats.services.operator.command_bridge.log_event") as spy:
            await worker.bootstrap()
        event_names = [call.args[1] for call in spy.call_args_list]
        self.assertIn("ai_command_worker_subscribed", event_names)


if __name__ == "__main__":
    unittest.main()
