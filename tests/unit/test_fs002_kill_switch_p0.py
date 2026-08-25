from __future__ import annotations

import asyncio
import logging
import time
import unittest
from types import SimpleNamespace
from typing import Any

from aats.bus.memory_bus import InMemoryEventBus
from aats.services.execution_engine.okx_adapter import OKXExecutionAdapter
from aats.services.governance_engine.kill_switch import (
    KILL_SWITCH_REDIS_KEY,
    KillSwitch,
    KillSwitchAuthorityError,
    kill_switch_permission_key,
)
from aats.services.governance_engine.mode import RuntimeModeController
from aats.services.operator.command_bridge import OperatorCommandError
from aats.services.operator.reconciliation_system_queries import ReconciliationSystemQueryFacade
from aats.storage.hot_state_store import InMemoryHotStateStore
from tests.unit.test_guarded_simulated import (
    FakeAccountService,
    FakeHealthService,
    FakeOKXClient,
    make_derivatives_account_service,
    make_intent,
    make_risk_reducing_derivatives_intent,
    make_settings,
)


def _running_record(generation: str = "ksgen_fs002_running") -> dict[str, Any]:
    return {
        "halted": False,
        "reason": None,
        "state": "RUNNING",
        "generation": generation,
        "set_at_ts": 1.0,
        "source_role": "execution",
        "resume_authorized": True,
    }


class _ToggleStore(InMemoryHotStateStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail_set = False
        self.fail_get = False

    async def get(self, key: str) -> Any | None:
        if self.fail_get:
            raise RuntimeError("redis_get_failed")
        return await super().get(key)

    async def set(
        self,
        key: str,
        value: Any,
        *,
        ttl_seconds: float | None = None,
    ) -> None:
        if self.fail_set:
            raise RuntimeError("redis_set_failed")
        await super().set(key, value, ttl_seconds=ttl_seconds)


class _ToggleBus(InMemoryEventBus):
    def __init__(self) -> None:
        super().__init__()
        self.fail_publish = False

    async def publish(self, topic: str, key: str, payload: dict) -> None:
        if self.fail_publish:
            raise RuntimeError("nats_publish_failed")
        await super().publish(topic=topic, key=key, payload=payload)


class _BlockingPlaceOrderClient(FakeOKXClient):
    def __init__(self) -> None:
        super().__init__()
        self.place_order_started = asyncio.Event()
        self.release_place_order = asyncio.Event()
        self.place_order_started_at: list[float] = []

    async def place_order(self, payload):
        self.place_order_started_at.append(time.monotonic())
        self.place_order_started.set()
        await self.release_place_order.wait()
        return await super().place_order(payload)


class _FailingCommandClient:
    async def invoke(self, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("operator_command_transport_failed")


class _GatewayOwner:
    def __init__(self, kill_switch: KillSwitch) -> None:
        self.runtime = SimpleNamespace(
            reconciliation_service=None,
            kill_switch=kill_switch,
            operator_command_client=_FailingCommandClient(),
        )
        self.cache_invalidated = False

    def _invalidate_cache(self) -> None:
        self.cache_invalidated = True


class TestFS002KillSwitchP0(unittest.IsolatedAsyncioTestCase):
    async def _kill_switch(
        self,
        *,
        role: str = "execution",
        store: _ToggleStore | None = None,
        bus: _ToggleBus | None = None,
    ) -> tuple[KillSwitch, _ToggleStore, _ToggleBus]:
        resolved_store = store or _ToggleStore()
        if await resolved_store.get(KILL_SWITCH_REDIS_KEY) is None:
            await resolved_store.set(KILL_SWITCH_REDIS_KEY, _running_record())
        resolved_bus = bus or _ToggleBus()
        kill_switch = KillSwitch()
        await kill_switch.bootstrap(
            hot_state_store=resolved_store,
            bus=resolved_bus,
            process_role=role,
            logger=logging.getLogger(f"test.fs002.{role}"),
        )
        await resolved_store.set(
            kill_switch_permission_key(kill_switch.generation),
            {
                "generation": kill_switch.generation,
                "issued_by": "gateway",
                "issued_at": time.time(),
            },
            ttl_seconds=15.0,
        )
        return kill_switch, resolved_store, resolved_bus

    @staticmethod
    def _spot_adapter(
        kill_switch: KillSwitch,
        *,
        client: FakeOKXClient | None = None,
    ) -> tuple[OKXExecutionAdapter, FakeOKXClient]:
        settings = make_settings(
            {
                "live_submit_enabled": True,
                "guarded_execution_dry_run": False,
                "max_notional_per_symbol": 1_000.0,
            }
        )
        resolved_client = client or FakeOKXClient()
        adapter = OKXExecutionAdapter(
            settings=settings,
            client=resolved_client,  # type: ignore[arg-type]
            account_service=FakeAccountService(),  # type: ignore[arg-type]
            mode_controller=RuntimeModeController(
                settings=settings,
                kill_switch=kill_switch,
            ),
            health_service=FakeHealthService(),
            price_provider=lambda _symbol: 68_000.0,
        )
        return adapter, resolved_client

    async def test_01_normal_halt_blocks_order_before_it_begins(self) -> None:
        kill_switch, _, _ = await self._kill_switch()
        adapter, client = self._spot_adapter(kill_switch)

        halt = await kill_switch.halt_async(reason="fs002_normal_halt")
        state, _ = await adapter.submit(make_intent())

        self.assertEqual(halt["state"], "HALTED")
        self.assertTrue(halt["enforced"])
        self.assertEqual(state.status, "BLOCKED")
        self.assertEqual(client.place_order_calls, [])

    async def test_02_proven_final_submit_race_is_blocked(self) -> None:
        kill_switch, _, _ = await self._kill_switch()
        adapter, client = self._spot_adapter(kill_switch)
        reached_async_gap = asyncio.Event()
        release_async_gap = asyncio.Event()
        original_gate = adapter._max_size_gate_error

        async def paused_gate(*, intent, payload):
            reached_async_gap.set()
            await release_async_gap.wait()
            return await original_gate(intent=intent, payload=payload)

        adapter._max_size_gate_error = paused_gate  # type: ignore[method-assign]
        pending = asyncio.create_task(adapter.submit(make_intent()))
        await reached_async_gap.wait()
        await kill_switch.halt_async(reason="fs002_race_halt")
        release_async_gap.set()
        state, _ = await pending

        self.assertEqual(state.status, "BLOCKED")
        self.assertEqual(client.place_order_calls, [])

    async def test_03_redis_failure_cannot_bypass_execution_halt(self) -> None:
        store = _ToggleStore()
        await store.set(KILL_SWITCH_REDIS_KEY, _running_record())
        bus = _ToggleBus()
        gateway, _, _ = await self._kill_switch(role="gateway", store=store, bus=bus)
        execution, _, _ = await self._kill_switch(role="execution", store=store, bus=bus)
        adapter, client = self._spot_adapter(execution)
        store.fail_set = True

        result = await gateway.halt_async(reason="fs002_redis_failed")
        state, _ = await adapter.submit(make_intent())

        self.assertTrue(result["enforced"])
        self.assertEqual(result["acknowledged_by"], "execution")
        self.assertEqual(state.status, "BLOCKED")
        self.assertEqual(client.place_order_calls, [])

    async def test_04_nats_failure_uses_authoritative_final_read(self) -> None:
        store = _ToggleStore()
        await store.set(KILL_SWITCH_REDIS_KEY, _running_record())
        bus = _ToggleBus()
        gateway, _, _ = await self._kill_switch(role="gateway", store=store, bus=bus)
        execution, _, _ = await self._kill_switch(role="execution", store=store, bus=bus)
        adapter, client = self._spot_adapter(execution)
        bus.fail_publish = True

        result = await gateway.halt_async(reason="fs002_nats_failed")
        state, _ = await adapter.submit(make_intent())

        self.assertFalse(result["enforced"])
        self.assertEqual(result["state"], "HALTING")
        self.assertEqual(state.status, "BLOCKED")
        self.assertEqual(client.place_order_calls, [])

    async def test_05_both_transports_fail_without_false_gateway_ack(self) -> None:
        store = _ToggleStore()
        await store.set(KILL_SWITCH_REDIS_KEY, _running_record())
        bus = _ToggleBus()
        gateway, _, _ = await self._kill_switch(role="gateway", store=store, bus=bus)
        execution, _, _ = await self._kill_switch(role="execution", store=store, bus=bus)
        adapter, client = self._spot_adapter(execution)
        store.fail_set = True
        bus.fail_publish = True
        facade = ReconciliationSystemQueryFacade(_GatewayOwner(gateway))  # type: ignore[arg-type]

        with self.assertRaisesRegex(OperatorCommandError, "execution_ack_unavailable"):
            await facade.halt(reason="fs002_both_failed")
        self.assertEqual(gateway.phase, "HALTING")
        self.assertFalse(gateway.transition_status()["enforced"])

        effective = await execution.halt_async(reason="fs002_execution_local_halt")
        state, _ = await adapter.submit(make_intent())
        self.assertTrue(effective["enforced"])
        self.assertEqual(state.status, "BLOCKED")
        self.assertEqual(client.place_order_calls, [])

    async def test_06_stale_worker_is_blocked_by_authoritative_final_read(self) -> None:
        execution, store, _ = await self._kill_switch()
        adapter, client = self._spot_adapter(execution)
        await store.set(
            KILL_SWITCH_REDIS_KEY,
            {
                "halted": True,
                "reason": "fs002_remote_halt",
                "state": "HALTED",
                "generation": "ksgen_fs002_remote_halt",
                "set_at_ts": time.time(),
                "source_role": "execution",
                "resume_authorized": False,
            },
        )
        self.assertFalse(execution.halted)

        state, _ = await adapter.submit(make_intent())

        self.assertEqual(state.status, "BLOCKED")
        self.assertTrue(execution.halted)
        self.assertEqual(client.place_order_calls, [])

    async def test_07_halt_during_queued_order_blocks_before_exchange(self) -> None:
        kill_switch, _, _ = await self._kill_switch()
        adapter, client = self._spot_adapter(kill_switch)
        await kill_switch._submission_fence.acquire()
        pending = asyncio.create_task(adapter.submit(make_intent()))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        halt_task = asyncio.create_task(kill_switch.halt_async(reason="fs002_queued_halt"))
        await asyncio.sleep(0)
        self.assertTrue(kill_switch.halted)
        kill_switch._submission_fence.release()

        state, _ = await pending
        halt = await halt_task
        self.assertTrue(halt["enforced"])
        self.assertEqual(state.status, "BLOCKED")
        self.assertEqual(client.place_order_calls, [])

    async def test_08_concurrent_orders_respect_effective_boundary(self) -> None:
        kill_switch, _, _ = await self._kill_switch()
        client = _BlockingPlaceOrderClient()
        adapter, _ = self._spot_adapter(kill_switch, client=client)
        first = asyncio.create_task(adapter.submit(make_intent()))
        await client.place_order_started.wait()
        second_intent = make_intent().model_copy(
            update={"intent_id": "intent_2", "idempotency_key": "intent_2"}
        )
        second = asyncio.create_task(adapter.submit(second_intent))
        await asyncio.sleep(0)
        halt_task = asyncio.create_task(kill_switch.halt_async(reason="fs002_concurrent_halt"))
        await asyncio.sleep(0)
        client.release_place_order.set()

        await first
        second_state, _ = await second
        halt = await halt_task
        effective_at = time.monotonic()
        post_state, _ = await adapter.submit(
            make_intent().model_copy(
                update={"intent_id": "intent_3", "idempotency_key": "intent_3"}
            )
        )

        self.assertTrue(halt["enforced"])
        self.assertTrue(all(started <= effective_at for started in client.place_order_started_at))
        self.assertEqual(second_state.status, "BLOCKED")
        self.assertEqual(post_state.status, "BLOCKED")
        self.assertEqual(len(client.place_order_calls), 1)

    async def test_09_execution_restart_while_halted_stays_halted(self) -> None:
        first, store, bus = await self._kill_switch()
        await first.halt_async(reason="fs002_restart_halt")
        restarted = KillSwitch()
        await restarted.bootstrap(
            hot_state_store=store,
            bus=bus,
            process_role="execution",
            logger=logging.getLogger("test.fs002.restarted"),
        )
        adapter, client = self._spot_adapter(restarted)

        state, _ = await adapter.submit(make_intent())

        self.assertTrue(restarted.halted)
        self.assertEqual(restarted.phase, "HALTED")
        self.assertEqual(state.status, "BLOCKED")
        self.assertEqual(client.place_order_calls, [])

    async def test_10_duplicate_halt_calls_are_generation_idempotent(self) -> None:
        kill_switch, _, _ = await self._kill_switch()

        results = await asyncio.gather(
            kill_switch.halt_async(reason="fs002_duplicate"),
            kill_switch.halt_async(reason="fs002_duplicate"),
            kill_switch.halt_async(reason="fs002_duplicate"),
        )

        self.assertEqual({item["generation"] for item in results}, {kill_switch.generation})
        self.assertTrue(all(item["enforced"] for item in results))
        self.assertEqual(kill_switch.phase, "HALTED")

    async def test_11_resume_requires_authority_then_permits_trading(self) -> None:
        kill_switch, store, _ = await self._kill_switch()
        adapter, client = self._spot_adapter(kill_switch)
        await kill_switch.halt_async(reason="fs002_resume_halt")
        store.fail_set = True

        with self.assertRaises(KillSwitchAuthorityError):
            await kill_switch.resume_async()
        blocked, _ = await adapter.submit(make_intent())
        self.assertEqual(blocked.status, "BLOCKED")
        self.assertEqual(kill_switch.phase, "DEGRADED")

        store.fail_set = False
        resumed = await kill_switch.resume_async()
        await store.set(
            kill_switch_permission_key(kill_switch.generation),
            {
                "generation": kill_switch.generation,
                "issued_by": "gateway",
                "issued_at": time.time(),
            },
            ttl_seconds=15.0,
        )
        allowed, _ = await adapter.submit(make_intent())
        self.assertEqual(resumed["state"], "RUNNING")
        self.assertTrue(resumed["resume_authorized"])
        self.assertEqual(allowed.status, "FILLED")
        self.assertEqual(len(client.place_order_calls), 1)

    async def test_12_only_validated_reduce_only_can_bypass_halt(self) -> None:
        kill_switch, _, _ = await self._kill_switch()
        settings = make_settings(
            {
                "live_submit_enabled": True,
                "guarded_execution_dry_run": False,
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "max_notional_per_symbol": 10_000.0,
            }
        )
        client = FakeOKXClient()
        adapter = OKXExecutionAdapter(
            settings=settings,
            client=client,  # type: ignore[arg-type]
            account_service=make_derivatives_account_service(),  # type: ignore[arg-type]
            mode_controller=RuntimeModeController(settings=settings, kill_switch=kill_switch),
            health_service=FakeHealthService(),
            price_provider=lambda _symbol: 68_000.0,
        )
        await kill_switch.halt_async(reason="fs002_reduce_only_halt")
        valid = make_risk_reducing_derivatives_intent(
            "fs002_valid_close",
            position_intent="close_long",
            side="sell",
        )
        mislabeled = make_risk_reducing_derivatives_intent(
            "fs002_mislabeled_reduce",
            position_intent="close_long",
            side="buy",
        )

        valid_state, _ = await adapter.submit(valid)
        invalid_state, _ = await adapter.submit(mislabeled)

        self.assertEqual(valid_state.status, "FILLED")
        self.assertEqual(client.place_order_calls[0]["reduceOnly"], "true")
        self.assertEqual(invalid_state.status, "BLOCKED")
        self.assertEqual(len(client.place_order_calls), 1)


if __name__ == "__main__":
    unittest.main()
