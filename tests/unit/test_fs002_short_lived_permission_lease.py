from __future__ import annotations

import asyncio
import inspect
import logging
import time
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from aats.bootstrap.config import ApplicationRuntime
from aats.bus.memory_bus import InMemoryEventBus
from aats.services.governance_engine import kill_switch as kill_switch_module
from aats.services.governance_engine.kill_switch import (
    KILL_SWITCH_REDIS_KEY,
    KillSwitch,
    KillSwitchAuthorityError,
    KillSwitchSubmissionBlocked,
    kill_switch_permission_key,
)
from aats.services.operator.command_bridge import OperatorCommandError
from aats.services.operator.reconciliation_system_queries import (
    ReconciliationSystemQueryFacade,
)
from aats.storage.hot_state_store import InMemoryHotStateStore


def _running_record(generation: str = "ksgen_lease_running") -> dict[str, Any]:
    return {
        "halted": False,
        "reason": None,
        "state": "RUNNING",
        "generation": generation,
        "set_at_ts": time.time(),
        "source_role": "execution",
        "resume_authorized": True,
    }


def _halted_record(generation: str = "ksgen_lease_halted") -> dict[str, Any]:
    return {
        "halted": True,
        "reason": "seed_halt",
        "state": "HALTED",
        "generation": generation,
        "set_at_ts": time.time(),
        "source_role": "execution",
        "resume_authorized": False,
    }


class _FaultStore(InMemoryHotStateStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail_get = False
        self.fail_set = False
        self.fail_delete = False

    async def get(self, key: str) -> Any | None:
        if self.fail_get:
            raise RuntimeError("sensitive-redis-get-body")
        return await super().get(key)

    async def set(
        self,
        key: str,
        value: Any,
        *,
        ttl_seconds: float | None = None,
    ) -> None:
        if self.fail_set:
            raise RuntimeError("sensitive-redis-set-body")
        await super().set(key, value, ttl_seconds=ttl_seconds)

    async def delete(self, key: str) -> None:
        if self.fail_delete:
            raise RuntimeError("sensitive-redis-delete-body")
        await super().delete(key)


class _FaultBus(InMemoryEventBus):
    def __init__(self) -> None:
        super().__init__()
        self.fail_publish = False

    async def publish(self, topic: str, key: str, payload: dict) -> None:
        if self.fail_publish:
            raise RuntimeError("sensitive-nats-publish-body")
        await super().publish(topic=topic, key=key, payload=payload)


class _ResumeCommandClient:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result

    async def invoke(self, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("command") != "resume":
            raise AssertionError("unexpected command")
        return dict(self.result)


class _GatewayOwner:
    def __init__(self, kill_switch: KillSwitch, result: dict[str, Any]) -> None:
        self.runtime = SimpleNamespace(
            reconciliation_service=None,
            operator_command_client=_ResumeCommandClient(result),
            kill_switch=kill_switch,
        )
        self.cache_invalidated = False

    def _invalidate_cache(self) -> None:
        self.cache_invalidated = True


async def _bootstrap(
    *,
    role: str,
    store: _FaultStore,
    bus: _FaultBus,
    fail_closed: bool = True,
) -> KillSwitch:
    switch = KillSwitch()
    await switch.bootstrap(
        hot_state_store=store,
        bus=bus,
        process_role=role,
        logger=logging.getLogger(f"test.fs002.lease.{role}"),
        fail_closed_on_authority_loss=fail_closed,
    )
    return switch


async def _seed_permission(
    store: _FaultStore,
    generation: str,
    *,
    ttl_seconds: float = 15.0,
    payload_generation: str | None = None,
) -> None:
    await store.set(
        kill_switch_permission_key(generation),
        {
            "generation": payload_generation or generation,
            "issued_by": "gateway",
            "issued_at": time.time(),
        },
        ttl_seconds=ttl_seconds,
    )


class TestFS002ShortLivedPermissionLease(unittest.IsolatedAsyncioTestCase):
    async def test_gateway_renews_and_execution_accepts_same_generation(self) -> None:
        store = _FaultStore()
        bus = _FaultBus()
        await store.set(KILL_SWITCH_REDIS_KEY, _running_record())
        gateway = await _bootstrap(role="gateway", store=store, bus=bus)
        execution = await _bootstrap(role="execution", store=store, bus=bus)

        task = await gateway.start_trading_permission_lease()
        self.assertIsNotNone(task)
        lease = await store.get(kill_switch_permission_key(gateway.generation))
        self.assertIsInstance(lease, dict)
        async with execution.risk_increasing_submission_guard(
            expected_generation=execution.generation,
        ):
            admitted = True
        self.assertTrue(admitted)
        await gateway.stop()
        await execution.stop()

    async def test_execution_cannot_mint_permission(self) -> None:
        store = _FaultStore()
        bus = _FaultBus()
        await store.set(KILL_SWITCH_REDIS_KEY, _running_record())
        execution = await _bootstrap(role="execution", store=store, bus=bus)

        task = await execution.start_trading_permission_lease()

        self.assertIsNone(task)
        self.assertIsNone(
            await store.get(kill_switch_permission_key(execution.generation))
        )
        await execution.stop()

    async def test_missing_permission_fails_closed_and_latches_degraded(self) -> None:
        store = _FaultStore()
        bus = _FaultBus()
        await store.set(KILL_SWITCH_REDIS_KEY, _running_record())
        execution = await _bootstrap(role="execution", store=store, bus=bus)

        with self.assertRaisesRegex(
            KillSwitchSubmissionBlocked,
            "kill_switch_permission_missing",
        ):
            async with execution.risk_increasing_submission_guard(
                expected_generation=execution.generation,
            ):
                self.fail("missing lease must never enter the submission boundary")

        self.assertTrue(execution.halted)
        self.assertEqual(execution.phase, "DEGRADED")

    async def test_wrong_payload_generation_fails_closed(self) -> None:
        store = _FaultStore()
        bus = _FaultBus()
        await store.set(KILL_SWITCH_REDIS_KEY, _running_record())
        execution = await _bootstrap(role="execution", store=store, bus=bus)
        await _seed_permission(
            store,
            execution.generation,
            payload_generation="ksgen_wrong",
        )

        with self.assertRaisesRegex(
            KillSwitchSubmissionBlocked,
            "kill_switch_permission_generation_mismatch",
        ):
            async with execution.risk_increasing_submission_guard(
                expected_generation=execution.generation,
            ):
                self.fail("mismatched lease must never enter the submission boundary")

    async def test_non_finite_permission_timestamp_fails_closed(self) -> None:
        store = _FaultStore()
        bus = _FaultBus()
        await store.set(KILL_SWITCH_REDIS_KEY, _running_record())
        execution = await _bootstrap(role="execution", store=store, bus=bus)
        await store.set(
            kill_switch_permission_key(execution.generation),
            {
                "generation": execution.generation,
                "issued_by": "gateway",
                "issued_at": float("nan"),
            },
            ttl_seconds=15.0,
        )

        with self.assertRaisesRegex(
            KillSwitchSubmissionBlocked,
            "kill_switch_permission_invalid",
        ):
            async with execution.risk_increasing_submission_guard(
                expected_generation=execution.generation,
            ):
                self.fail("non-finite lease timestamp must fail closed")

        self.assertTrue(execution.halted)
        self.assertEqual(execution.phase, "DEGRADED")

    async def test_non_finite_running_authority_cannot_mint_permission(self) -> None:
        store = _FaultStore()
        bus = _FaultBus()
        record = _running_record()
        record["set_at_ts"] = float("inf")
        await store.set(KILL_SWITCH_REDIS_KEY, record)
        gateway = await _bootstrap(role="gateway", store=store, bus=bus)

        self.assertTrue(gateway.halted)
        self.assertEqual(gateway.phase, "DEGRADED")
        lease_task = await gateway.start_trading_permission_lease()
        self.assertIsNotNone(lease_task)
        self.assertIsNone(
            await store.get(kill_switch_permission_key(gateway.generation))
        )
        await gateway.stop()

    async def test_explicit_non_finite_halt_timestamp_is_rejected_before_mutation(self) -> None:
        store = _FaultStore()
        bus = _FaultBus()
        await store.set(KILL_SWITCH_REDIS_KEY, _running_record())
        execution = await _bootstrap(role="execution", store=store, bus=bus)
        initial = execution.transition_status()

        with self.assertRaisesRegex(ValueError, "finite_and_positive"):
            await execution.halt_async(
                reason="invalid-clock",
                generation="ksgen_invalid_clock",
                set_at_ts=float("nan"),
            )

        self.assertEqual(execution.transition_status(), initial)

    async def test_idempotent_halt_still_rejects_non_finite_explicit_timestamp(self) -> None:
        store = _FaultStore()
        bus = _FaultBus()
        await store.set(KILL_SWITCH_REDIS_KEY, _halted_record())
        execution = await _bootstrap(role="execution", store=store, bus=bus)
        initial = execution.transition_status()

        with self.assertRaisesRegex(ValueError, "finite_and_positive"):
            await execution.halt_async(
                reason="invalid-idempotent-clock",
                generation=execution.generation,
                set_at_ts=float("inf"),
            )

        self.assertEqual(execution.transition_status(), initial)

    async def test_expired_permission_cannot_be_replaced_by_old_running_state(self) -> None:
        store = _FaultStore()
        bus = _FaultBus()
        await store.set(KILL_SWITCH_REDIS_KEY, _running_record())
        execution = await _bootstrap(role="execution", store=store, bus=bus)
        await _seed_permission(store, execution.generation, ttl_seconds=0.01)
        await asyncio.sleep(0.03)

        self.assertIsInstance(await store.get(KILL_SWITCH_REDIS_KEY), dict)
        with self.assertRaisesRegex(
            KillSwitchSubmissionBlocked,
            "kill_switch_permission_missing",
        ):
            async with execution.risk_increasing_submission_guard(
                expected_generation=execution.generation,
            ):
                self.fail("expired lease must fail closed")

    async def test_halt_revokes_preceding_running_generation(self) -> None:
        store = _FaultStore()
        bus = _FaultBus()
        await store.set(KILL_SWITCH_REDIS_KEY, _running_record())
        gateway = await _bootstrap(role="gateway", store=store, bus=bus)
        await gateway.start_trading_permission_lease()
        running_generation = gateway.generation
        self.assertIsNotNone(
            await store.get(kill_switch_permission_key(running_generation))
        )

        await gateway.halt_async(reason="lease_revoke_test")

        self.assertNotEqual(gateway.generation, running_generation)
        self.assertIsNone(
            await store.get(kill_switch_permission_key(running_generation))
        )
        await gateway.stop()

    async def test_dual_transport_failure_still_converges_at_lease_ttl(self) -> None:
        store = _FaultStore()
        bus = _FaultBus()
        await store.set(KILL_SWITCH_REDIS_KEY, _running_record())
        gateway = await _bootstrap(role="gateway", store=store, bus=bus)
        execution = await _bootstrap(role="execution", store=store, bus=bus)
        old_generation = execution.generation
        await _seed_permission(store, old_generation, ttl_seconds=0.02)
        store.fail_set = True
        store.fail_delete = True
        bus.fail_publish = True

        result = await gateway.halt_async(reason="fully_partitioned_halt")
        self.assertFalse(result["enforced"])
        self.assertIsNotNone(
            await InMemoryHotStateStore.get(
                store,
                kill_switch_permission_key(old_generation),
            )
        )
        await asyncio.sleep(0.04)

        with self.assertRaisesRegex(
            KillSwitchSubmissionBlocked,
            "kill_switch_permission_missing",
        ):
            async with execution.risk_increasing_submission_guard(
                expected_generation=old_generation,
            ):
                self.fail("expired partition lease must fail closed")

    async def test_gateway_resume_establishes_new_permission_before_return(self) -> None:
        store = _FaultStore()
        bus = _FaultBus()
        await store.set(KILL_SWITCH_REDIS_KEY, _halted_record())
        gateway = await _bootstrap(role="gateway", store=store, bus=bus)

        resumed = await gateway.resume_async()
        permission = await store.get(
            kill_switch_permission_key(str(resumed["generation"]))
        )

        self.assertEqual(resumed["state"], "RUNNING")
        self.assertIsInstance(permission, dict)

    async def test_proxied_resume_activates_permission_before_gateway_returns(self) -> None:
        store = _FaultStore()
        bus = _FaultBus()
        await store.set(KILL_SWITCH_REDIS_KEY, _halted_record())
        gateway = await _bootstrap(role="gateway", store=store, bus=bus)
        resumed_generation = "ksgen_proxy_resumed"
        await store.set(
            KILL_SWITCH_REDIS_KEY,
            _running_record(resumed_generation),
        )
        owner = _GatewayOwner(
            gateway,
            {
                "status": "resumed",
                "state": "RUNNING",
                "generation": resumed_generation,
                "resume_authorized": True,
            },
        )
        facade = ReconciliationSystemQueryFacade(owner)  # type: ignore[arg-type]

        result = await facade.resume(reason="proxy_resume")

        self.assertEqual(
            result["trading_permission_generation"],
            resumed_generation,
        )
        self.assertFalse(gateway.halted)
        self.assertIsNotNone(
            await store.get(kill_switch_permission_key(resumed_generation))
        )
        self.assertTrue(owner.cache_invalidated)

    async def test_proxied_resume_fails_when_permission_cannot_be_activated(self) -> None:
        store = _FaultStore()
        bus = _FaultBus()
        await store.set(KILL_SWITCH_REDIS_KEY, _halted_record())
        gateway = await _bootstrap(role="gateway", store=store, bus=bus)
        owner = _GatewayOwner(
            gateway,
            {
                "status": "resumed",
                "state": "RUNNING",
                "generation": "ksgen_unpersisted_resume",
                "resume_authorized": True,
            },
        )
        facade = ReconciliationSystemQueryFacade(owner)  # type: ignore[arg-type]

        with self.assertRaisesRegex(
            OperatorCommandError,
            "kill_switch_permission_activation_failed",
        ):
            await facade.resume(reason="proxy_resume_mismatch")

        self.assertTrue(gateway.halted)
        self.assertEqual(gateway.phase, "DEGRADED")
        self.assertTrue(owner.cache_invalidated)

    async def test_authority_generation_mismatch_is_not_renewed(self) -> None:
        store = _FaultStore()
        bus = _FaultBus()
        await store.set(KILL_SWITCH_REDIS_KEY, _running_record("ksgen_old"))
        gateway = await _bootstrap(role="gateway", store=store, bus=bus)
        await _seed_permission(store, gateway.generation, ttl_seconds=0.03)
        await store.set(KILL_SWITCH_REDIS_KEY, _running_record("ksgen_new"))

        with self.assertRaisesRegex(
            KillSwitchAuthorityError,
            "authority_generation_mismatch",
        ):
            await gateway._renew_trading_permission_once()

        self.assertIsNone(
            await store.get(kill_switch_permission_key("ksgen_old"))
        )

    async def test_renewal_failure_surfaces_as_critical_task_without_error_body(self) -> None:
        store = _FaultStore()
        bus = _FaultBus()
        await store.set(KILL_SWITCH_REDIS_KEY, _running_record())
        gateway = await _bootstrap(role="gateway", store=store, bus=bus)

        with (
            patch.object(
                kill_switch_module,
                "_KILL_SWITCH_PERMISSION_TTL_SECONDS",
                0.04,
            ),
            patch.object(
                kill_switch_module,
                "_KILL_SWITCH_PERMISSION_RENEW_INTERVAL_SECONDS",
                0.01,
            ),
        ):
            task = await gateway.start_trading_permission_lease()
            assert task is not None
            store.fail_get = True
            with self.assertRaisesRegex(
                KillSwitchAuthorityError,
                "kill_switch_permission_lease_renewal_expired",
            ) as caught:
                await asyncio.wait_for(asyncio.shield(task), timeout=0.25)
            self.assertNotIn("sensitive-redis-get-body", str(caught.exception))
            self.assertEqual(gateway.phase, "DEGRADED")
            store.fail_get = False
            await gateway.stop()

    async def test_stop_cancels_renewal_and_revokes_permission(self) -> None:
        store = _FaultStore()
        bus = _FaultBus()
        await store.set(KILL_SWITCH_REDIS_KEY, _running_record())
        gateway = await _bootstrap(role="gateway", store=store, bus=bus)
        task = await gateway.start_trading_permission_lease()
        generation = gateway.generation
        assert task is not None

        await gateway.stop_trading_permission_lease()

        self.assertTrue(task.done())
        self.assertIsNone(
            await store.get(kill_switch_permission_key(generation))
        )
        self.assertIsNone(gateway.trading_permission_background_task)

    async def test_non_strict_research_runtime_does_not_require_permission(self) -> None:
        store = _FaultStore()
        bus = _FaultBus()
        await store.set(KILL_SWITCH_REDIS_KEY, _running_record())
        research = await _bootstrap(
            role="monolith",
            store=store,
            bus=bus,
            fail_closed=False,
        )

        self.assertIsNone(await research.start_trading_permission_lease())
        async with research.risk_increasing_submission_guard(
            expected_generation=research.generation,
        ):
            admitted = True
        self.assertTrue(admitted)

    def test_runtime_registers_lease_as_service_owned_critical_task(self) -> None:
        source = inspect.getsource(ApplicationRuntime.start_background_tasks)
        task_offset = source.index("start_trading_permission_lease")
        declaration = source[task_offset : task_offset + 600]

        self.assertIn("critical=True", declaration)
        self.assertIn("owned_by_runtime=False", declaration)

    def test_safety_intervals_are_fixed_code_constants(self) -> None:
        self.assertEqual(
            kill_switch_module._KILL_SWITCH_PERMISSION_TTL_SECONDS,
            15.0,
        )
        self.assertEqual(
            kill_switch_module._KILL_SWITCH_PERMISSION_RENEW_INTERVAL_SECONDS,
            5.0,
        )


if __name__ == "__main__":
    unittest.main()
