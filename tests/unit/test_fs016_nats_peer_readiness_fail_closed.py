from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from aats.bootstrap import process_lifecycle, readiness_watchdog
from aats.bootstrap.process_lifecycle import (
    _RUNTIME_READY_LEASE_PROTOCOL,
    _RUNTIME_READY_PHASE_PROVISIONING,
    _RUNTIME_READY_PHASE_READY,
    _RUNTIME_READY_TTL_SECONDS,
    _announce_runtime_ready,
    _maintain_runtime_ready_lease,
    _promote_runtime_ready,
    _ready_key,
    _runtime_readiness_generation,
    _runtime_ready_clock,
    _strict_peer_readiness_required,
    _wait_for_peer_roles_ready,
    _withdraw_runtime_ready,
    run_process,
)
from aats.bootstrap.settings import AATSSettings
from aats.bus.nats_bus import NatsDeliveryGate
from apps.api_gateway import main as gateway_main


REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATION = "00b6df0f8a8d-20260824T120000Z-123-456"
_REAL_RUNTIME_READY_TAKEOVER_QUARANTINE = (
    process_lifecycle._wait_for_runtime_takeover_quarantine
)
_REAL_RUNTIME_READY_DEADLINE_WATCHDOG = (
    process_lifecycle._RuntimeReadyDeadlineWatchdog
)


class _FakeWatchdog:
    instances: list[_FakeWatchdog] = []

    def __init__(self, *, role: str, deadline_monotonic: float) -> None:
        self.role = role
        self.initial_deadline_monotonic = float(deadline_monotonic)
        self.deadline_monotonic = float(deadline_monotonic)
        self.fatal = False
        self.shutdown = False
        self.disarmed = False
        self.firing = False
        self.force_exit_count = 0
        self.rearm_deadlines: list[float] = []
        self.fatal_deadlines: list[float] = []
        self.__class__.instances.append(self)

    def rearm_success(self, *, deadline_monotonic: float) -> bool:
        if self.fatal or self.shutdown or self.disarmed or self.firing:
            return False
        self.deadline_monotonic = float(deadline_monotonic)
        self.rearm_deadlines.append(self.deadline_monotonic)
        return True

    def mark_fatal_and_tighten(self, *, deadline_monotonic: float) -> bool:
        if self.disarmed or self.firing:
            return False
        self.fatal = True
        self.deadline_monotonic = min(
            self.deadline_monotonic,
            float(deadline_monotonic),
        )
        self.fatal_deadlines.append(self.deadline_monotonic)
        return True

    def begin_shutdown(self, *, deadline_monotonic: float) -> bool:
        if self.fatal or self.disarmed or self.firing:
            return False
        self.shutdown = True
        self.deadline_monotonic = min(
            self.deadline_monotonic,
            float(deadline_monotonic),
        )
        return True

    def force_exit_now(self) -> None:
        if self.disarmed:
            return
        self.fatal = True
        self.firing = True
        self.force_exit_count += 1

    def disarm(self) -> bool:
        if self.fatal or self.firing:
            return False
        self.disarmed = True
        return True


@pytest.fixture(autouse=True)
def _prevent_real_process_exit(monkeypatch):
    """Lifecycle tests must never leave a real hard-exit child behind."""

    _FakeWatchdog.instances.clear()

    def _unexpected_hard_exit(exit_code: int) -> None:
        raise AssertionError(f"test attempted real hard exit: {exit_code}")

    monkeypatch.setattr(
        process_lifecycle,
        "_RuntimeReadyDeadlineWatchdog",
        _FakeWatchdog,
    )
    monkeypatch.setattr(
        gateway_main,
        "_RuntimeReadyDeadlineWatchdog",
        _FakeWatchdog,
    )
    monkeypatch.setattr(
        process_lifecycle,
        "_hard_exit_process",
        _unexpected_hard_exit,
    )
    monkeypatch.setattr(
        gateway_main,
        "_hard_exit_process",
        _unexpected_hard_exit,
    )

    async def _skip_takeover_quarantine(**_kwargs) -> None:
        return None

    monkeypatch.setattr(
        process_lifecycle,
        "_wait_for_runtime_takeover_quarantine",
        _skip_takeover_quarantine,
    )
    monkeypatch.setattr(
        gateway_main,
        "_wait_for_runtime_takeover_quarantine",
        _skip_takeover_quarantine,
    )


class _RecordingHotStateStore:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}
        self.deleted: list[str] = []
        self.replaced: list[tuple[str, object, object]] = []
        self.closed = False

    async def set(self, key: str, value: object, *, ttl_seconds: float | None = None) -> None:
        self.values[key] = value

    async def set_if_absent(
        self,
        key: str,
        value: object,
        *,
        ttl_seconds: float | None = None,
    ) -> bool:
        assert ttl_seconds == _RUNTIME_READY_TTL_SECONDS
        if key in self.values:
            return False
        self.values[key] = value
        return True

    async def compare_refresh(
        self,
        key: str,
        expected_value: object,
        *,
        ttl_seconds: float,
    ) -> bool:
        assert ttl_seconds > 0
        return self.values.get(key) == expected_value

    async def compare_replace(
        self,
        key: str,
        expected_value: object,
        replacement_value: object,
        *,
        ttl_seconds: float,
    ) -> bool:
        assert ttl_seconds > 0
        if self.values.get(key) != expected_value:
            return False
        self.replaced.append((key, expected_value, replacement_value))
        self.values[key] = replacement_value
        return True

    async def compare_delete(self, key: str, expected_value: object) -> bool:
        if self.values.get(key) != expected_value:
            return False
        self.deleted.append(key)
        self.values.pop(key, None)
        return True

    async def get_many(self, keys: list[str]) -> dict[str, object]:
        return {key: self.values[key] for key in keys if key in self.values}

    async def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.values.pop(key, None)

    async def close(self) -> None:
        self.closed = True


def _settings(**updates):
    values = {
        "event_bus_backend": "hybrid",
        "hot_state_backend": "redis",
        "runtime_readiness_generation": GENERATION,
    }
    values.update(updates)
    return AATSSettings.model_validate(values)


def _lease_payload(
    role: str,
    *,
    generation: str = GENERATION,
    phase: str = _RUNTIME_READY_PHASE_READY,
    protocol: int = _RUNTIME_READY_LEASE_PROTOCOL,
    instance_id: str = "a" * 32,
) -> dict[str, object]:
    return {
        "lease_protocol": protocol,
        "process_role": role,
        "generation": generation,
        "instance_id": instance_id,
        "announced_ts": "2026-08-28T00:00:00+00:00",
        "pid": 1,
        "phase": phase,
    }


async def _claim_ready_lease(
    *,
    role: str,
    store: _RecordingHotStateStore,
    logger,
    generation: str = GENERATION,
):
    provisioning = await _announce_runtime_ready(
        role=role,
        hot_state_store=store,
        logger=logger,
        generation=generation,
        required=True,
    )
    assert provisioning is not None
    watchdog = _FakeWatchdog(
        role=role,
        deadline_monotonic=_runtime_ready_clock() + _RUNTIME_READY_TTL_SECONDS,
    )
    ready = await _promote_runtime_ready(
        lease=provisioning,
        hot_state_store=store,
        watchdog=watchdog,
        logger=logger,
    )
    return ready, watchdog


async def _run_strict_build_hook(
    *,
    final_settings: AATSSettings,
    build_kwargs: dict[str, object],
    store: _RecordingHotStateStore,
) -> NatsDeliveryGate:
    hook = build_kwargs.get("before_event_bus_start")
    assert callable(hook)
    gate = build_kwargs.get("nats_delivery_gate")
    assert isinstance(gate, NatsDeliveryGate)
    assert not gate.activated
    assert not gate.aborted
    await hook(store, final_settings)
    assert not gate.activated
    assert not gate.aborted
    return gate


def test_readiness_generation_setting_normalizes_and_rejects_key_injection() -> None:
    settings = AATSSettings.model_validate(
        {"runtime_readiness_generation": f"  {GENERATION}  "}
    )
    assert settings.runtime_readiness_generation == GENERATION

    for invalid in ("bad generation", "bad/generation", "x" * 129, 123):
        with pytest.raises(ValueError, match="runtime_readiness_generation"):
            AATSSettings.model_validate({"runtime_readiness_generation": invalid})


def test_strict_requirement_and_missing_generation_fail_before_runtime_build() -> None:
    assert _strict_peer_readiness_required(role="market", settings=_settings()) is True
    assert (
        _strict_peer_readiness_required(
            role="market",
            settings=_settings(event_bus_backend="in_memory"),
        )
        is False
    )
    assert _strict_peer_readiness_required(role="monolith", settings=_settings()) is False

    with pytest.raises(RuntimeError, match="runtime_ready_gate_generation_required:market"):
        _runtime_readiness_generation(
            role="market",
            settings=_settings(runtime_readiness_generation=None),
            required=True,
        )


@pytest.mark.asyncio
async def test_strict_announce_claims_global_role_provisioning_and_redis_failure_is_fatal() -> None:
    store = _RecordingHotStateStore()
    logger = logging.getLogger("test.fs016.announce")
    lease = await _announce_runtime_ready(
        role="market",
        hot_state_store=store,
        logger=logger,
        generation=GENERATION,
        required=True,
    )
    key = _ready_key("market", generation=GENERATION)
    assert key == "aats:runtime:owner:market"
    assert store.values[key]["generation"] == GENERATION
    assert store.values[key]["process_role"] == "market"
    assert store.values[key]["lease_protocol"] == _RUNTIME_READY_LEASE_PROTOCOL
    assert store.values[key]["phase"] == _RUNTIME_READY_PHASE_PROVISIONING
    assert lease is not None
    assert lease.phase == _RUNTIME_READY_PHASE_PROVISIONING
    detached_payload = lease.payload
    detached_payload["instance_id"] = "b" * 32
    assert lease.payload["instance_id"] != "b" * 32
    assert store.values[key]["instance_id"] == lease.payload["instance_id"]

    class _BrokenStore:
        async def set_if_absent(self, *_args, **_kwargs) -> bool:
            raise ConnectionError("sensitive redis endpoint must not be forwarded")

    with pytest.raises(RuntimeError, match="runtime_ready_gate_announce_failed:market") as raised:
        await _announce_runtime_ready(
            role="market",
            hot_state_store=_BrokenStore(),
            logger=logger,
            generation=GENERATION,
            required=True,
        )
    assert "endpoint" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True

    with pytest.raises(RuntimeError, match="runtime_ready_gate_hot_state_required:market"):
        await _announce_runtime_ready(
            role="market",
            hot_state_store=None,
            logger=logger,
            generation=GENERATION,
            required=True,
        )

    with pytest.raises(RuntimeError, match="runtime_ready_gate_generation_required:market"):
        await _announce_runtime_ready(
            role="market",
            hot_state_store=store,
            logger=logger,
            required=True,
        )


@pytest.mark.asyncio
async def test_strict_announce_rejects_global_role_conflict_across_generations() -> None:
    store = _RecordingHotStateStore()
    logger = logging.getLogger("test.fs016.instance_conflict")
    first = await _announce_runtime_ready(
        role="market",
        hot_state_store=store,
        logger=logger,
        generation=GENERATION,
        required=True,
    )
    assert first is not None

    with pytest.raises(
        RuntimeError,
        match="runtime_ready_gate_instance_conflict:market",
    ):
        await _announce_runtime_ready(
            role="market",
            hot_state_store=store,
            logger=logger,
            generation="next-generation",
            required=True,
        )
    assert store.values[first.key] == first.payload


@pytest.mark.asyncio
async def test_runtime_ready_promotion_is_owner_aware_provisioning_to_ready_cas() -> None:
    store = _RecordingHotStateStore()
    logger = logging.getLogger("test.fs016.promotion")
    provisioning = await _announce_runtime_ready(
        role="decision",
        hot_state_store=store,
        logger=logger,
        generation=GENERATION,
        required=True,
    )
    assert provisioning is not None
    watchdog = _FakeWatchdog(
        role="decision",
        deadline_monotonic=_runtime_ready_clock() + _RUNTIME_READY_TTL_SECONDS,
    )

    ready = await _promote_runtime_ready(
        lease=provisioning,
        hot_state_store=store,
        watchdog=watchdog,
        logger=logger,
    )

    assert provisioning.phase == _RUNTIME_READY_PHASE_PROVISIONING
    assert ready.phase == _RUNTIME_READY_PHASE_READY
    assert ready.instance_id == provisioning.instance_id
    assert ready.generation == provisioning.generation
    assert store.replaced == [
        (provisioning.key, provisioning.payload, ready.payload)
    ]
    assert store.values[ready.key] == ready.payload
    assert watchdog.rearm_deadlines


@pytest.mark.asyncio
async def test_readiness_lease_renews_and_owner_loss_fails_closed() -> None:
    class _LeaseStore(_RecordingHotStateStore):
        def __init__(self) -> None:
            super().__init__()
            self.refresh_count = 0

        async def compare_refresh(
            self,
            key: str,
            expected_value: object,
            *,
            ttl_seconds: float,
        ) -> bool:
            self.refresh_count += 1
            return await super().compare_refresh(
                key,
                expected_value,
                ttl_seconds=ttl_seconds,
            )

    store = _LeaseStore()
    logger = logging.getLogger("test.fs016.lease")
    lease, _watchdog = await _claim_ready_lease(
        role="decision",
        store=store,
        logger=logger,
    )
    assert lease.phase == _RUNTIME_READY_PHASE_READY
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        _maintain_runtime_ready_lease(
            lease=lease,
            hot_state_store=store,
            logger=logger,
            stop_event=stop_event,
            ttl_seconds=0.06,
            renew_interval=0.02,
            shutdown_margin=0.01,
            required=True,
        )
    )
    for _attempt in range(20):
        if store.refresh_count >= 2:
            break
        await asyncio.sleep(0.01)
    assert store.refresh_count >= 2
    store.values[lease.key] = {"instance_id": "b" * 32}
    with pytest.raises(RuntimeError, match="runtime_ready_lease_lost:decision"):
        await asyncio.wait_for(task, timeout=0.1)


@pytest.mark.asyncio
async def test_readiness_lease_retries_transient_refresh_error_within_ttl() -> None:
    class _TransientStore(_RecordingHotStateStore):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0
            self.recovered = asyncio.Event()

        async def compare_refresh(
            self,
            key: str,
            expected_value: object,
            *,
            ttl_seconds: float,
        ) -> bool:
            self.attempts += 1
            if self.attempts == 1:
                raise ConnectionError("redis://secret@host")
            self.recovered.set()
            return await super().compare_refresh(
                key,
                expected_value,
                ttl_seconds=ttl_seconds,
            )

    store = _TransientStore()
    logger = logging.getLogger("test.fs016.lease_retry")
    lease, _watchdog = await _claim_ready_lease(
        role="decision",
        store=store,
        logger=logger,
    )
    assert lease.phase == _RUNTIME_READY_PHASE_READY
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        _maintain_runtime_ready_lease(
            lease=lease,
            hot_state_store=store,
            logger=logger,
            stop_event=stop_event,
            ttl_seconds=0.31,
            renew_interval=0.1,
            required=True,
        )
    )
    await asyncio.wait_for(store.recovered.wait(), timeout=0.5)
    stop_event.set()
    await asyncio.wait_for(task, timeout=0.2)
    assert store.attempts >= 2


@pytest.mark.asyncio
async def test_readiness_lease_persistent_refresh_error_is_fixed_and_sanitized() -> None:
    class _BrokenRefreshStore(_RecordingHotStateStore):
        async def compare_refresh(self, *_args, **_kwargs) -> bool:
            raise ConnectionError("redis://credential@host")

    store = _BrokenRefreshStore()
    logger = logging.getLogger("test.fs016.lease_retry_exhausted")
    lease, _watchdog = await _claim_ready_lease(
        role="execution",
        store=store,
        logger=logger,
    )
    assert lease.phase == _RUNTIME_READY_PHASE_READY
    with pytest.raises(
        RuntimeError,
        match="runtime_ready_lease_refresh_failed:execution",
    ) as raised:
        await asyncio.wait_for(
            _maintain_runtime_ready_lease(
                lease=lease,
                hot_state_store=store,
                logger=logger,
                stop_event=asyncio.Event(),
                ttl_seconds=0.06,
                renew_interval=0.02,
                required=True,
            ),
            timeout=0.3,
        )
    assert "credential" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True


@pytest.mark.asyncio
async def test_readiness_lease_refreshes_beyond_original_ttl() -> None:
    """只要 owner 仍健康，lease 必须跨越初始 TTL 持续存在。"""

    from aats.storage.hot_state_store import InMemoryHotStateStore

    store = InMemoryHotStateStore()
    logger = logging.getLogger("test.fs016.lease_beyond_initial_ttl")
    lease, _watchdog = await _claim_ready_lease(
        role="market",
        store=store,
        logger=logger,
    )
    assert lease.phase == _RUNTIME_READY_PHASE_READY
    # promotion 使用生产 TTL；为时间可控的单测把同一 owner 改成 60ms lease。
    await store.set(lease.key, lease.payload, ttl_seconds=0.06)
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        _maintain_runtime_ready_lease(
            lease=lease,
            hot_state_store=store,
            logger=logger,
            stop_event=stop_event,
            ttl_seconds=0.06,
            renew_interval=0.02,
            shutdown_margin=0.01,
            required=True,
        )
    )
    await asyncio.sleep(0.15)
    assert await store.get(lease.key) == lease.payload
    stop_event.set()
    await asyncio.wait_for(task, timeout=0.1)


@pytest.mark.asyncio
async def test_readiness_lease_hung_refresh_fails_by_local_deadline() -> None:
    class _HungRefreshStore(_RecordingHotStateStore):
        async def compare_refresh(self, *_args, **_kwargs) -> bool:
            await asyncio.Future()

    store = _HungRefreshStore()
    logger = logging.getLogger("test.fs016.lease_hung_refresh")
    lease, _watchdog = await _claim_ready_lease(
        role="execution",
        store=store,
        logger=logger,
    )
    assert lease.phase == _RUNTIME_READY_PHASE_READY
    started = asyncio.get_running_loop().time()
    with pytest.raises(
        RuntimeError,
        match="runtime_ready_lease_refresh_failed:execution",
    ):
        await asyncio.wait_for(
            _maintain_runtime_ready_lease(
                lease=lease,
                hot_state_store=store,
                logger=logger,
                stop_event=asyncio.Event(),
                ttl_seconds=0.06,
                renew_interval=0.02,
                required=True,
            ),
            timeout=0.2,
        )
    assert asyncio.get_running_loop().time() - started < 0.15


@pytest.mark.asyncio
async def test_provisioning_hung_refresh_preserves_absolute_cleanup_margin(
    monkeypatch,
) -> None:
    """单次 Redis 挂起也必须在绝对停机点前返回给有序清理。"""

    class _HungRefreshStore(_RecordingHotStateStore):
        async def compare_refresh(self, *_args, **_kwargs) -> bool:
            await asyncio.Future()

    monkeypatch.setattr(
        process_lifecycle,
        "_RUNTIME_READY_FORCE_EXIT_GRACE_SECONDS",
        0.01,
    )
    store = _HungRefreshStore()
    logger = logging.getLogger("test.fs016.provisioning_hung_refresh")
    lease, _watchdog = await _claim_ready_lease(
        role="execution",
        store=store,
        logger=logger,
    )
    started = asyncio.get_running_loop().time()
    with pytest.raises(
        RuntimeError,
        match="runtime_ready_provisioning_timeout:execution",
    ):
        await asyncio.wait_for(
            _maintain_runtime_ready_lease(
                lease=lease,
                hot_state_store=store,
                logger=logger,
                stop_event=asyncio.Event(),
                ttl_seconds=0.30,
                renew_interval=0.02,
                shutdown_margin=0.03,
                required=True,
                absolute_hard_deadline_monotonic=started + 0.06,
            ),
            timeout=0.15,
        )
    assert asyncio.get_running_loop().time() - started < 0.10


@pytest.mark.asyncio
async def test_readiness_refresh_response_delay_cannot_extend_safety_deadline() -> None:
    class _DelayedResponseStore(_RecordingHotStateStore):
        def __init__(self) -> None:
            super().__init__()
            self.refresh_started: float | None = None
            self.attempts = 0

        async def compare_refresh(
            self,
            key: str,
            expected_value: object,
            *,
            ttl_seconds: float,
        ) -> bool:
            self.attempts += 1
            if self.attempts == 1:
                self.refresh_started = asyncio.get_running_loop().time()
                refreshed = await super().compare_refresh(
                    key,
                    expected_value,
                    ttl_seconds=ttl_seconds,
                )
                # 模拟 Redis 已执行 PEXPIRE，但响应在网络中延迟；本地 not-after
                # 必须仍以请求开始时刻为准，不能从响应返回时刻重新起算。
                await asyncio.sleep(0.025)
                return refreshed
            await asyncio.Future()

    store = _DelayedResponseStore()
    logger = logging.getLogger("test.fs016.lease_delayed_response")
    lease, _watchdog = await _claim_ready_lease(
        role="market",
        store=store,
        logger=logger,
    )
    assert lease.phase == _RUNTIME_READY_PHASE_READY
    with pytest.raises(
        RuntimeError,
        match="runtime_ready_lease_refresh_failed:market",
    ):
        await asyncio.wait_for(
            _maintain_runtime_ready_lease(
                lease=lease,
                hot_state_store=store,
                logger=logger,
                stop_event=asyncio.Event(),
                ttl_seconds=0.09,
                renew_interval=0.02,
                shutdown_margin=0.03,
                required=True,
            ),
            timeout=0.2,
        )
    assert store.refresh_started is not None
    elapsed_from_server_refresh = (
        asyncio.get_running_loop().time() - store.refresh_started
    )
    assert elapsed_from_server_refresh < 0.075


@pytest.mark.asyncio
async def test_old_instance_withdraw_cannot_delete_new_owner() -> None:
    store = _RecordingHotStateStore()
    logger = logging.getLogger("test.fs016.fencing")
    lease = await _announce_runtime_ready(
        role="execution",
        hot_state_store=store,
        logger=logger,
        generation=GENERATION,
        required=True,
    )
    assert lease is not None
    new_owner = dict(lease.payload)
    new_owner["instance_id"] = "b" * 32
    store.values[lease.key] = new_owner

    await _withdraw_runtime_ready(
        lease=lease,
        hot_state_store=store,
        logger=logger,
    )
    assert store.values[lease.key] == new_owner
    assert store.deleted == []


@pytest.mark.asyncio
async def test_run_process_cancels_startup_when_runtime_lease_is_lost(
    monkeypatch,
) -> None:
    store = _RecordingHotStateStore()
    for peer in ("market", "decision", "gateway"):
        store.values[_ready_key(peer, generation=GENERATION)] = _lease_payload(peer)
    lease_may_fail = asyncio.Event()
    calls: list[str] = []
    delivery_gates: list[NatsDeliveryGate] = []

    async def _start() -> None:
        calls.append("start_entered")
        lease_may_fail.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            calls.append("start_cancelled")
            raise

    async def _stop() -> None:
        assert _ready_key("execution", generation=GENERATION) in store.values
        calls.append("stop")

    runtime = SimpleNamespace(
        hot_state_store=store,
        background_tasks=[],
        start_background_tasks=_start,
        stop_background_tasks=_stop,
    )

    async def _fail_lease(**_kwargs) -> None:
        if _kwargs["lease"].phase == _RUNTIME_READY_PHASE_PROVISIONING:
            await _kwargs["stop_event"].wait()
            return
        await lease_may_fail.wait()
        raise RuntimeError("runtime_ready_lease_lost:execution")

    async def _build(final_settings, **build_kwargs):
        delivery_gates.append(
            await _run_strict_build_hook(
                final_settings=final_settings,
                build_kwargs=build_kwargs,
                store=store,
            )
        )
        return runtime

    monkeypatch.setattr(process_lifecycle, "build_runtime", _build)
    monkeypatch.setattr(
        process_lifecycle,
        "configure_logging_for_settings",
        lambda _settings: None,
    )
    monkeypatch.setattr(
        process_lifecycle,
        "_maintain_runtime_ready_lease",
        _fail_lease,
    )

    result = await asyncio.wait_for(
        run_process(
            process_role="execution",
            app_name="test.fs016.lease_loss",
            settings=_settings(),
            stop_event=asyncio.Event(),
        ),
        timeout=1.0,
    )
    assert result == 1
    assert calls == ["start_entered", "start_cancelled", "stop"]
    assert len(_FakeWatchdog.instances) == 1
    assert _FakeWatchdog.instances[0].fatal
    assert not _FakeWatchdog.instances[0].disarmed
    assert len(delivery_gates) == 1
    assert delivery_gates[0].aborted
    assert not delivery_gates[0].activated


@pytest.mark.asyncio
async def test_run_process_exits_nonzero_on_steady_state_lease_loss(
    monkeypatch,
) -> None:
    store = _RecordingHotStateStore()
    for peer in ("market", "decision", "gateway"):
        store.values[_ready_key(peer, generation=GENERATION)] = _lease_payload(peer)
    steady_state = asyncio.Event()
    fail_lease = asyncio.Event()
    calls: list[str] = []
    delivery_gates: list[NatsDeliveryGate] = []

    async def _start() -> None:
        calls.append("start")

    async def _stop() -> None:
        assert _ready_key("execution", generation=GENERATION) in store.values
        calls.append("stop")

    async def _heartbeat(
        _role, *, stop_event, logger, started_event=None
    ) -> None:
        del logger
        if started_event is not None:
            started_event.set()
        calls.append("steady")
        steady_state.set()
        await stop_event.wait()

    async def _never_business_failure():
        await asyncio.Future()

    runtime = SimpleNamespace(
        hot_state_store=store,
        background_tasks=[],
        start_background_tasks=_start,
        stop_background_tasks=_stop,
        wait_for_critical_background_task_failure=_never_business_failure,
    )

    async def _fail_lease(**_kwargs) -> None:
        if _kwargs["lease"].phase == _RUNTIME_READY_PHASE_PROVISIONING:
            await _kwargs["stop_event"].wait()
            return
        await fail_lease.wait()
        raise RuntimeError("runtime_ready_lease_lost:execution")

    async def _build(final_settings, **build_kwargs):
        delivery_gates.append(
            await _run_strict_build_hook(
                final_settings=final_settings,
                build_kwargs=build_kwargs,
                store=store,
            )
        )
        return runtime

    monkeypatch.setattr(process_lifecycle, "build_runtime", _build)
    monkeypatch.setattr(
        process_lifecycle,
        "configure_logging_for_settings",
        lambda _settings: None,
    )
    monkeypatch.setattr(
        process_lifecycle,
        "_maintain_runtime_ready_lease",
        _fail_lease,
    )
    monkeypatch.setattr(process_lifecycle, "_heartbeat_loop", _heartbeat)

    process_task = asyncio.create_task(
        run_process(
            process_role="execution",
            app_name="test.fs016.steady_lease_loss",
            settings=_settings(),
            stop_event=asyncio.Event(),
        )
    )
    await asyncio.wait_for(steady_state.wait(), timeout=0.5)
    fail_lease.set()
    result = await asyncio.wait_for(process_task, timeout=0.5)

    assert result == 1
    assert calls == ["start", "steady", "stop"]
    # Fatal lease loss 不得主动删除 owner；保留 TTL fencing，避免清理尚未完成时
    # 新实例立即进入。fake store 不模拟 TTL，所以 key 仍可见是正确契约。
    assert _ready_key("execution", generation=GENERATION) in store.values
    assert len(_FakeWatchdog.instances) == 1
    assert _FakeWatchdog.instances[0].fatal
    assert not _FakeWatchdog.instances[0].disarmed
    assert len(delivery_gates) == 1
    assert delivery_gates[0].aborted
    assert not delivery_gates[0].activated


def test_runtime_ready_deadline_watchdog_fatal_is_sticky_and_cannot_disarm() -> None:
    # Watchdog 指向一次性 harness PID；即使实现回归也绝不能杀 pytest 主进程。
    source = """
from aats.bootstrap.process_lifecycle import (
    _RuntimeReadyDeadlineWatchdog,
    _runtime_ready_clock,
)
w = _RuntimeReadyDeadlineWatchdog(
    role="execution",
    deadline_monotonic=_runtime_ready_clock() + 30.0,
)
tightened = _runtime_ready_clock() + 20.0
print(w.mark_fatal_and_tighten(deadline_monotonic=tightened), flush=True)
print(w.fatal, w.disarm(), flush=True)
print(w.rearm_success(deadline_monotonic=_runtime_ready_clock() + 60.0), flush=True)
print(w.deadline_monotonic <= tightened + 0.001, flush=True)
w._terminate_child()
w._close_connections()
"""
    completed = subprocess.run(
        (sys.executable, "-c", source),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == ["True", "True False", "False", "True"]


def test_runtime_ready_watchdog_preserves_popen_startup_error(monkeypatch) -> None:
    """Popen 在赋值前失败时，清理不能用 AttributeError 覆盖根因。"""

    def _raise_startup_error(*_args, **_kwargs):
        raise OSError("synthetic watchdog spawn failure")

    monkeypatch.setattr(subprocess, "Popen", _raise_startup_error)
    with pytest.raises(OSError, match="synthetic watchdog spawn failure"):
        _REAL_RUNTIME_READY_DEADLINE_WATCHDOG(
            role="execution",
            deadline_monotonic=_runtime_ready_clock() + 30.0,
        )


def test_windows_watchdog_wait_failure_terminates_parent_fail_closed() -> None:
    """WAIT_FAILED 不是 parent 已退出证据；child 必须直接执行硬终止。"""

    class _Kernel32:
        def __init__(self) -> None:
            self.terminate_calls: list[tuple[object, int]] = []

        def WaitForSingleObject(self, _handle: object, _timeout: int) -> int:
            return 0xFFFFFFFF

        def TerminateProcess(self, handle: object, exit_code: int) -> bool:
            self.terminate_calls.append((handle, exit_code))
            return True

    kernel32 = _Kernel32()
    guard = object.__new__(readiness_watchdog._WindowsParentGuard)
    guard._kernel32 = kernel32
    guard._ctypes = SimpleNamespace(get_last_error=lambda: 5)
    guard._handle = object()
    guard._wait_object_0 = 0x00000000
    guard._wait_timeout = 0x00000102
    guard._wait_failed = 0xFFFFFFFF

    assert guard.is_alive() is False
    assert kernel32.terminate_calls == [(guard._handle, 1)]


def test_runtime_ready_deadline_watchdog_expires_during_gil_starvation() -> None:
    # 60s switch interval + pure-Python busy loop keeps the harness GIL; only真正
    # 独立的 watchdog process 能在 deadline 内终止它。time.sleep() 会释放 GIL，
    # 不能证明该性质。
    source = """
import sys
import time
from aats.bootstrap.process_lifecycle import (
    _RuntimeReadyDeadlineWatchdog,
    _runtime_ready_clock,
)
_RuntimeReadyDeadlineWatchdog(
    role="market",
    deadline_monotonic=_runtime_ready_clock() + 0.4,
)
print("ARMED", flush=True)
sys.setswitchinterval(60.0)
deadline = _runtime_ready_clock() + 10.0
while _runtime_ready_clock() < deadline:
    pass
"""
    process = subprocess.Popen(
        (sys.executable, "-c", source),
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "ARMED"
    started = time.monotonic()
    stdout, stderr = process.communicate(timeout=3)
    elapsed = time.monotonic() - started
    assert process.returncode != 0, stdout + stderr
    assert elapsed < 1.5


def test_runtime_ready_watchdog_unexpected_exit_immediately_kills_parent() -> None:
    """child watchdog 自身死亡时，不得等下一轮 Redis refresh 才发现。"""

    source = """
import time
from aats.bootstrap.process_lifecycle import (
    _RuntimeReadyDeadlineWatchdog,
    _runtime_ready_clock,
)
w = _RuntimeReadyDeadlineWatchdog(
    role="risk",
    deadline_monotonic=_runtime_ready_clock() + 30.0,
)
print(w._process.pid, flush=True)
time.sleep(30.0)
"""
    process = subprocess.Popen(
        (sys.executable, "-c", source),
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    child_pid = int(process.stdout.readline().strip())
    started = time.monotonic()
    os.kill(child_pid, signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=3)
    elapsed = time.monotonic() - started
    assert process.returncode != 0, stdout + stderr
    assert elapsed < 1.5


@pytest.mark.asyncio
async def test_takeover_quarantine_enforces_full_old_owner_survival_window() -> None:
    slept: list[float] = []

    async def _record_sleep(seconds: float) -> None:
        slept.append(seconds)

    with pytest.raises(ValueError, match="maximum protocol-v2 owner survival"):
        await _REAL_RUNTIME_READY_TAKEOVER_QUARANTINE(
            role="execution",
            logger=logging.getLogger("test.fs016.quarantine.invalid"),
            duration_seconds=49.999,
            _sleep=_record_sleep,
        )
    await _REAL_RUNTIME_READY_TAKEOVER_QUARANTINE(
        role="execution",
        logger=logging.getLogger("test.fs016.quarantine.valid"),
        duration_seconds=55.0,
        _sleep=_record_sleep,
    )
    assert slept == [55.0]


@pytest.mark.asyncio
async def test_run_process_stops_heartbeat_and_arms_watchdog_before_hung_cleanup(
    monkeypatch,
) -> None:
    store = _RecordingHotStateStore()
    for peer in ("market", "decision", "gateway"):
        store.values[_ready_key(peer, generation=GENERATION)] = _lease_payload(peer)
    steady_state = asyncio.Event()
    fail_lease = asyncio.Event()
    heartbeat_stopped = asyncio.Event()
    cleanup_entered = asyncio.Event()
    allow_cleanup = asyncio.Event()
    delivery_gates: list[NatsDeliveryGate] = []

    async def _start() -> None:
        return None

    async def _stop() -> None:
        cleanup_entered.set()
        await allow_cleanup.wait()

    async def _heartbeat(
        _role, *, stop_event, logger, started_event=None
    ) -> None:
        del logger
        if started_event is not None:
            started_event.set()
        steady_state.set()
        await stop_event.wait()
        heartbeat_stopped.set()

    async def _never_business_failure():
        await asyncio.Future()

    runtime = SimpleNamespace(
        process_role="execution",
        hot_state_store=store,
        background_tasks=[],
        critical_background_tasks={},
        start_background_tasks=_start,
        stop_background_tasks=_stop,
        wait_for_critical_background_task_failure=_never_business_failure,
    )

    async def _fail_lease(**_kwargs) -> None:
        if _kwargs["lease"].phase == _RUNTIME_READY_PHASE_PROVISIONING:
            await _kwargs["stop_event"].wait()
            return
        await fail_lease.wait()
        raise RuntimeError("runtime_ready_lease_lost:execution")

    async def _build(final_settings, **build_kwargs):
        delivery_gates.append(
            await _run_strict_build_hook(
                final_settings=final_settings,
                build_kwargs=build_kwargs,
                store=store,
            )
        )
        return runtime

    monkeypatch.setattr(process_lifecycle, "build_runtime", _build)
    monkeypatch.setattr(
        process_lifecycle,
        "configure_logging_for_settings",
        lambda _settings: None,
    )
    monkeypatch.setattr(
        process_lifecycle,
        "_maintain_runtime_ready_lease",
        _fail_lease,
    )
    monkeypatch.setattr(process_lifecycle, "_heartbeat_loop", _heartbeat)

    process_task = asyncio.create_task(
        run_process(
            process_role="execution",
            app_name="test.fs016.hung_cleanup",
            settings=_settings(),
            stop_event=asyncio.Event(),
        )
    )
    await asyncio.wait_for(steady_state.wait(), timeout=0.5)
    fail_lease.set()
    await asyncio.wait_for(heartbeat_stopped.wait(), timeout=0.5)
    await asyncio.wait_for(cleanup_entered.wait(), timeout=0.5)

    assert len(_FakeWatchdog.instances) == 1
    watchdog = _FakeWatchdog.instances[0]
    assert watchdog.fatal
    assert not watchdog.disarmed
    assert len(delivery_gates) == 1
    assert delivery_gates[0].aborted
    assert not delivery_gates[0].activated
    assert not process_task.done()

    allow_cleanup.set()
    assert await asyncio.wait_for(process_task, timeout=0.5) == 1
    assert watchdog.fatal
    assert not watchdog.disarmed


@pytest.mark.asyncio
async def test_gateway_critical_supervisor_requests_process_shutdown(
    monkeypatch,
) -> None:
    requested: list[str] = []
    readiness_failures: list[str] = []
    failure = SimpleNamespace(
        task_name="aats-runtime-ready-lease-gateway",
        failure_kind="exception",
        error_type="RuntimeError",
        stalled_seconds=None,
        timeout_seconds=None,
    )

    async def _wait_for_failure():
        await asyncio.sleep(0)
        return failure

    runtime = SimpleNamespace(
        process_role="gateway",
        wait_for_critical_background_task_failure=_wait_for_failure,
    )
    monkeypatch.setattr(
        gateway_main,
        "_request_gateway_process_shutdown",
        lambda: requested.append("shutdown"),
    )
    await gateway_main._supervise_gateway_critical_failure(
        runtime=runtime,
        stopping=asyncio.Event(),
        logger=logging.getLogger("test.fs016.gateway_supervisor"),
        on_readiness_failure=lambda: readiness_failures.append("fatal"),
    )
    assert requested == ["shutdown"]
    assert readiness_failures == ["fatal"]


@pytest.mark.asyncio
async def test_gateway_steady_state_lease_loss_requests_shutdown_and_cleans_up(
    monkeypatch,
) -> None:
    store = _RecordingHotStateStore()
    for peer in ("market", "decision", "execution"):
        store.values[_ready_key(peer, generation=GENERATION)] = _lease_payload(peer)
    registered: list[asyncio.Task[None]] = []
    fail_lease = asyncio.Event()
    shutdown_requested = asyncio.Event()
    calls: list[str] = []
    delivery_gates: list[NatsDeliveryGate] = []

    async def _start() -> None:
        calls.append("start")

    async def _stop() -> None:
        assert _ready_key("gateway", generation=GENERATION) in store.values
        calls.append("stop")

    def _register(task, **_kwargs) -> None:
        registered.append(task)

    async def _wait_for_failure():
        while not registered:
            await asyncio.sleep(0)
        await asyncio.gather(registered[0], return_exceptions=True)
        return SimpleNamespace(
            task_name="aats-runtime-ready-lease-gateway",
            failure_kind="exception",
            error_type="RuntimeError",
            stalled_seconds=None,
            timeout_seconds=None,
        )

    runtime = SimpleNamespace(
        process_role="gateway",
        hot_state_store=store,
        start_background_tasks=_start,
        stop_background_tasks=_stop,
        register_background_task=_register,
        wait_for_critical_background_task_failure=_wait_for_failure,
    )

    async def _fail_lease(**_kwargs) -> None:
        if _kwargs["lease"].phase == _RUNTIME_READY_PHASE_PROVISIONING:
            await _kwargs["stop_event"].wait()
            return
        await fail_lease.wait()
        raise RuntimeError("runtime_ready_lease_lost:gateway")

    async def _build(final_settings, **build_kwargs):
        delivery_gates.append(
            await _run_strict_build_hook(
                final_settings=final_settings,
                build_kwargs=build_kwargs,
                store=store,
            )
        )
        return runtime

    monkeypatch.setattr(gateway_main, "load_settings", lambda: _settings())
    monkeypatch.setattr(
        gateway_main,
        "configure_logging_for_settings",
        lambda _settings: None,
    )
    monkeypatch.setattr(gateway_main, "build_runtime", _build)
    monkeypatch.setattr(
        gateway_main,
        "_maintain_runtime_ready_lease",
        _fail_lease,
    )
    monkeypatch.setattr(
        gateway_main,
        "_request_gateway_process_shutdown",
        shutdown_requested.set,
    )
    monkeypatch.setattr(
        gateway_main,
        "start_dashboard_snapshot_plane",
        lambda *_args, **_kwargs: asyncio.sleep(0),
    )
    monkeypatch.setattr(
        gateway_main,
        "stop_dashboard_snapshot_plane",
        lambda *_args, **_kwargs: asyncio.sleep(0),
    )
    monkeypatch.setattr("aats.data_platform.db.validate_rdp_schema", lambda: None)

    local_app = FastAPI()
    async with gateway_main.lifespan(local_app):
        assert calls == ["start"]
        fail_lease.set()
        await asyncio.wait_for(shutdown_requested.wait(), timeout=0.5)

    assert calls == ["start", "stop"]
    assert _ready_key("gateway", generation=GENERATION) in store.values
    assert len(_FakeWatchdog.instances) == 1
    watchdog = _FakeWatchdog.instances[0]
    assert watchdog.fatal
    assert not watchdog.disarmed
    assert len(delivery_gates) == 1
    assert delivery_gates[0].aborted
    assert not delivery_gates[0].activated


@pytest.mark.asyncio
async def test_gateway_critical_supervisor_ignores_normal_shutdown(
    monkeypatch,
) -> None:
    requested: list[str] = []

    async def _never_fails():
        await asyncio.Future()

    runtime = SimpleNamespace(
        wait_for_critical_background_task_failure=_never_fails,
    )
    stopping = asyncio.Event()
    stopping.set()
    monkeypatch.setattr(
        gateway_main,
        "_request_gateway_process_shutdown",
        lambda: requested.append("shutdown"),
    )
    await gateway_main._supervise_gateway_critical_failure(
        runtime=runtime,
        stopping=stopping,
        logger=logging.getLogger("test.fs016.gateway_supervisor_stop"),
    )
    assert requested == []


@pytest.mark.asyncio
async def test_gateway_critical_supervisor_failure_itself_requests_shutdown(
    monkeypatch,
) -> None:
    requested: list[str] = []

    async def _watcher_fails():
        raise ConnectionError("sensitive endpoint")

    runtime = SimpleNamespace(
        process_role="gateway",
        wait_for_critical_background_task_failure=_watcher_fails,
    )
    monkeypatch.setattr(
        gateway_main,
        "_request_gateway_process_shutdown",
        lambda: requested.append("shutdown"),
    )
    await gateway_main._supervise_gateway_critical_failure(
        runtime=runtime,
        stopping=asyncio.Event(),
        logger=logging.getLogger("test.fs016.gateway_supervisor_failure"),
    )
    assert requested == ["shutdown"]


def test_gateway_shutdown_request_uses_graceful_platform_signal(monkeypatch) -> None:
    raised: list[object] = []
    killed: list[tuple[int, object]] = []
    fake_signal = SimpleNamespace(
        SIGINT="sigint",
        SIGTERM="sigterm",
        raise_signal=lambda sig: raised.append(sig),
    )
    monkeypatch.setattr(gateway_main, "signal", fake_signal)
    monkeypatch.setattr(
        gateway_main,
        "os",
        SimpleNamespace(
            name="nt",
            getpid=lambda: 123,
            kill=lambda pid, sig: killed.append((pid, sig)),
        ),
    )
    gateway_main._request_gateway_process_shutdown()
    assert raised == ["sigterm"]
    assert killed == []

    raised.clear()
    monkeypatch.setattr(
        gateway_main,
        "os",
        SimpleNamespace(
            name="posix",
            getpid=lambda: 123,
            kill=lambda pid, sig: killed.append((pid, sig)),
        ),
    )
    gateway_main._request_gateway_process_shutdown()
    assert raised == []
    assert killed == [(123, "sigterm")]


@pytest.mark.asyncio
async def test_strict_wait_requires_exact_generation_and_role_then_succeeds() -> None:
    store = _RecordingHotStateStore()
    logger = logging.getLogger("test.fs016.wait")
    peer_key = _ready_key("market", generation=GENERATION)

    async def _assert_rejected(payload: dict[str, object]) -> None:
        store.values[peer_key] = payload
        with pytest.raises(
            RuntimeError,
            match="runtime_ready_gate_timeout:decision:market",
        ):
            await _wait_for_peer_roles_ready(
                role="decision",
                hot_state_store=store,
                logger=logger,
                peers=("market",),
                timeout_seconds=0.0,
                poll_interval=0.0,
                generation=GENERATION,
                required=True,
            )

    await _assert_rejected(_lease_payload("execution"))

    missing_diagnostics = _lease_payload("market")
    missing_diagnostics.pop("announced_ts")
    await _assert_rejected(missing_diagnostics)

    await _assert_rejected(
        _lease_payload("market", generation="old-generation")
    )
    await _assert_rejected(_lease_payload("market", protocol=1))
    await _assert_rejected(
        _lease_payload("market", phase=_RUNTIME_READY_PHASE_PROVISIONING)
    )

    store.values[peer_key] = _lease_payload("market")
    await _wait_for_peer_roles_ready(
        role="decision",
        hot_state_store=store,
        logger=logger,
        peers=("market",),
        timeout_seconds=0.0,
        poll_interval=0.0,
        generation=GENERATION,
        required=True,
    )


@pytest.mark.asyncio
async def test_strict_poll_error_is_fixed_failure_and_withdraw_is_exact() -> None:
    logger = logging.getLogger("test.fs016.poll")

    class _BrokenStore:
        async def get_many(self, _keys):
            raise ConnectionError("redis://credential@host")

    with pytest.raises(RuntimeError, match="runtime_ready_gate_poll_failed:execution") as raised:
        await _wait_for_peer_roles_ready(
            role="execution",
            hot_state_store=_BrokenStore(),
            logger=logger,
            peers=("decision",),
            generation=GENERATION,
            required=True,
        )
    assert "credential" not in str(raised.value)

    with pytest.raises(RuntimeError, match="runtime_ready_gate_generation_required:execution"):
        await _wait_for_peer_roles_ready(
            role="execution",
            hot_state_store=_RecordingHotStateStore(),
            logger=logger,
            peers=("decision",),
            required=True,
        )

    store = _RecordingHotStateStore()
    lease = await _announce_runtime_ready(
        role="execution",
        hot_state_store=store,
        logger=logger,
        generation=GENERATION,
        required=True,
    )
    assert lease is not None
    await _withdraw_runtime_ready(
        lease=lease,
        hot_state_store=store,
        logger=logger,
    )
    assert store.deleted == [lease.key]
    assert lease.key not in store.values


@pytest.mark.asyncio
async def test_run_process_missing_generation_aborts_gate_before_nats(
    monkeypatch,
) -> None:
    store = _RecordingHotStateStore()
    delivery_gates: list[NatsDeliveryGate] = []
    nats_started: list[str] = []

    async def _build(final_settings, **build_kwargs):
        hook = build_kwargs.get("before_event_bus_start")
        gate = build_kwargs.get("nats_delivery_gate")
        assert callable(hook)
        assert isinstance(gate, NatsDeliveryGate)
        delivery_gates.append(gate)
        await hook(store, final_settings)
        nats_started.append("nats")
        raise AssertionError("missing generation must fail before NATS")

    monkeypatch.setattr(process_lifecycle, "build_runtime", _build)
    monkeypatch.setattr(
        process_lifecycle,
        "configure_logging_for_settings",
        lambda _settings: None,
    )

    result = await run_process(
        process_role="market",
        app_name="test.fs016.run_process_missing_generation",
        settings=_settings(runtime_readiness_generation=None),
        stop_event=asyncio.Event(),
    )

    assert result == 1
    assert nats_started == []
    assert len(delivery_gates) == 1
    assert delivery_gates[0].aborted
    assert not delivery_gates[0].activated
    assert _FakeWatchdog.instances == []


@pytest.mark.asyncio
async def test_gateway_missing_generation_aborts_gate_before_nats(
    monkeypatch,
) -> None:
    store = _RecordingHotStateStore()
    schema_calls = 0
    delivery_gates: list[NatsDeliveryGate] = []
    nats_started: list[str] = []

    def _validate_schema() -> None:
        nonlocal schema_calls
        schema_calls += 1

    async def _build(final_settings, **build_kwargs):
        hook = build_kwargs.get("before_event_bus_start")
        gate = build_kwargs.get("nats_delivery_gate")
        assert callable(hook)
        assert isinstance(gate, NatsDeliveryGate)
        delivery_gates.append(gate)
        await hook(store, final_settings)
        nats_started.append("nats")
        raise AssertionError("missing generation must fail before NATS")

    monkeypatch.setattr(
        gateway_main,
        "load_settings",
        lambda: _settings(runtime_readiness_generation=None),
    )
    monkeypatch.setattr(
        gateway_main,
        "configure_logging_for_settings",
        lambda _settings: None,
    )
    monkeypatch.setattr(gateway_main, "build_runtime", _build)
    monkeypatch.setattr("aats.data_platform.db.validate_rdp_schema", _validate_schema)

    with pytest.raises(
        RuntimeError,
        match="runtime_ready_gate_generation_required:gateway",
    ):
        async with gateway_main.lifespan(FastAPI()):
            raise AssertionError("lifespan must not yield")

    assert schema_calls == 1
    assert nats_started == []
    assert len(delivery_gates) == 1
    assert delivery_gates[0].aborted
    assert not delivery_gates[0].activated
    assert _FakeWatchdog.instances == []


@pytest.mark.asyncio
async def test_run_process_build_failure_after_claim_aborts_gate_and_fences_owner(
    monkeypatch,
) -> None:
    store = _RecordingHotStateStore()
    delivery_gates: list[NatsDeliveryGate] = []

    async def _build(final_settings, **build_kwargs):
        delivery_gates.append(
            await _run_strict_build_hook(
                final_settings=final_settings,
                build_kwargs=build_kwargs,
                store=store,
            )
        )
        raise RuntimeError("synthetic build failure")

    monkeypatch.setattr(process_lifecycle, "build_runtime", _build)
    monkeypatch.setattr(
        process_lifecycle,
        "configure_logging_for_settings",
        lambda _settings: None,
    )

    result = await run_process(
        process_role="market",
        app_name="test.fs016.run_process_build_failure",
        settings=_settings(),
        stop_event=asyncio.Event(),
    )

    owner = store.values[_ready_key("market", generation=GENERATION)]
    assert result == 1
    assert owner["phase"] == _RUNTIME_READY_PHASE_PROVISIONING
    assert store.deleted == []
    assert store.closed
    assert len(delivery_gates) == 1
    assert delivery_gates[0].aborted
    assert not delivery_gates[0].activated
    assert len(_FakeWatchdog.instances) == 1
    assert _FakeWatchdog.instances[0].fatal
    assert not _FakeWatchdog.instances[0].disarmed


@pytest.mark.asyncio
async def test_gateway_build_failure_after_claim_aborts_gate_and_fences_owner(
    monkeypatch,
) -> None:
    store = _RecordingHotStateStore()
    delivery_gates: list[NatsDeliveryGate] = []

    async def _build(final_settings, **build_kwargs):
        delivery_gates.append(
            await _run_strict_build_hook(
                final_settings=final_settings,
                build_kwargs=build_kwargs,
                store=store,
            )
        )
        raise RuntimeError("synthetic gateway build failure")

    monkeypatch.setattr(gateway_main, "load_settings", lambda: _settings())
    monkeypatch.setattr(
        gateway_main,
        "configure_logging_for_settings",
        lambda _settings: None,
    )
    monkeypatch.setattr(gateway_main, "build_runtime", _build)
    monkeypatch.setattr("aats.data_platform.db.validate_rdp_schema", lambda: None)

    with pytest.raises(RuntimeError, match="synthetic gateway build failure"):
        async with gateway_main.lifespan(FastAPI()):
            raise AssertionError("lifespan must not yield")

    owner = store.values[_ready_key("gateway", generation=GENERATION)]
    assert owner["phase"] == _RUNTIME_READY_PHASE_PROVISIONING
    assert store.deleted == []
    assert store.closed
    assert len(delivery_gates) == 1
    assert delivery_gates[0].aborted
    assert not delivery_gates[0].activated
    assert len(_FakeWatchdog.instances) == 1
    assert _FakeWatchdog.instances[0].fatal
    assert not _FakeWatchdog.instances[0].disarmed


@pytest.mark.asyncio
async def test_monolith_nats_run_process_does_not_inject_split_delivery_gate(
    monkeypatch,
) -> None:
    injected_gates: list[object] = []
    calls: list[str] = []
    stop_event = asyncio.Event()
    stop_event.set()

    async def _start() -> None:
        calls.append("start")

    async def _stop() -> None:
        calls.append("stop")

    runtime = SimpleNamespace(
        background_tasks=[],
        start_background_tasks=_start,
        stop_background_tasks=_stop,
    )

    async def _build(_final_settings, **build_kwargs):
        injected_gates.append(build_kwargs.get("nats_delivery_gate"))
        return runtime

    async def _heartbeat(
        _role, *, stop_event, logger, started_event=None
    ) -> None:
        del logger
        if started_event is not None:
            started_event.set()
        await stop_event.wait()

    monkeypatch.setattr(process_lifecycle, "build_runtime", _build)
    monkeypatch.setattr(
        process_lifecycle,
        "configure_logging_for_settings",
        lambda _settings: None,
    )
    monkeypatch.setattr(process_lifecycle, "_heartbeat_loop", _heartbeat)

    result = await run_process(
        process_role="monolith",
        app_name="test.fs016.monolith_nats_no_split_gate",
        settings=_settings(event_bus_backend="nats"),
        stop_event=stop_event,
    )

    assert result == 0
    assert injected_gates == [None]
    assert calls == ["start", "stop"]
    assert _FakeWatchdog.instances == []


@pytest.mark.asyncio
async def test_gateway_monolith_nats_does_not_inject_split_delivery_gate(
    monkeypatch,
) -> None:
    injected_gates: list[object] = []
    calls: list[str] = []

    async def _start() -> None:
        calls.append("start")

    async def _stop() -> None:
        calls.append("stop")

    runtime = SimpleNamespace(
        process_role="monolith",
        start_background_tasks=_start,
        stop_background_tasks=_stop,
        register_background_task=lambda *_args, **_kwargs: None,
    )

    async def _build(_final_settings, **build_kwargs):
        injected_gates.append(build_kwargs.get("nats_delivery_gate"))
        return runtime

    monkeypatch.setenv("AATS_PROCESS_ROLE", "monolith")
    monkeypatch.setattr(
        gateway_main,
        "load_settings",
        lambda: _settings(event_bus_backend="nats"),
    )
    monkeypatch.setattr(
        gateway_main,
        "configure_logging_for_settings",
        lambda _settings: None,
    )
    monkeypatch.setattr(gateway_main, "build_runtime", _build)
    monkeypatch.setattr(
        gateway_main,
        "start_dashboard_snapshot_plane",
        lambda *_args, **_kwargs: asyncio.sleep(0),
    )
    monkeypatch.setattr(
        gateway_main,
        "stop_dashboard_snapshot_plane",
        lambda *_args, **_kwargs: asyncio.sleep(0),
    )
    monkeypatch.setattr("aats.data_platform.db.validate_rdp_schema", lambda: None)

    async with gateway_main.lifespan(FastAPI()):
        calls.append("yield")

    assert injected_gates == [None]
    assert calls == ["start", "yield", "stop"]
    assert _FakeWatchdog.instances == []


@pytest.mark.asyncio
async def test_run_process_rejects_same_tick_build_and_provisioning_lease_failure(
    monkeypatch,
) -> None:
    store = _RecordingHotStateStore()
    delivery_gates: list[NatsDeliveryGate] = []
    stop_observations: list[tuple[bool, bool]] = []
    calls: list[str] = []
    original_stop = process_lifecycle._stop_provisioning_lease_before_promotion

    async def _maintain(**kwargs) -> None:
        assert kwargs.get("suppress_failures_when_stopping") is False
        await kwargs["stop_event"].wait()
        raise RuntimeError("synthetic same-tick daemon lease failure")

    async def _start() -> None:
        calls.append("start")

    async def _stop() -> None:
        calls.append("stop")

    runtime = SimpleNamespace(
        background_tasks=[],
        start_background_tasks=_start,
        stop_background_tasks=_stop,
    )

    async def _build(final_settings, **build_kwargs):
        gate = await _run_strict_build_hook(
            final_settings=final_settings,
            build_kwargs=build_kwargs,
            store=store,
        )
        delivery_gates.append(gate)
        return runtime

    async def _observe_stop(**kwargs) -> None:
        stop_observations.append(
            (
                kwargs["lease_task"].done(),
                delivery_gates[0].aborted,
            )
        )
        await original_stop(**kwargs)

    monkeypatch.setattr(process_lifecycle, "build_runtime", _build)
    monkeypatch.setattr(
        process_lifecycle,
        "configure_logging_for_settings",
        lambda _settings: None,
    )
    monkeypatch.setattr(
        process_lifecycle,
        "_maintain_runtime_ready_lease",
        _maintain,
    )
    monkeypatch.setattr(
        process_lifecycle,
        "_stop_provisioning_lease_before_promotion",
        _observe_stop,
    )

    result = await run_process(
        process_role="market",
        app_name="test.fs016.same_tick_daemon_lease_failure",
        settings=_settings(),
        stop_event=asyncio.Event(),
    )

    assert result == 1
    # Build returned while the maintainer was still live. Promotion freezes the
    # lease in the same turn; an in-flight refresh failure must propagate rather
    # than be mistaken for a clean stop.
    assert stop_observations == [(False, False)]
    assert calls == ["stop"]
    assert delivery_gates[0].aborted
    assert store.deleted == []
    assert store.values[_ready_key("market", generation=GENERATION)][
        "phase"
    ] == _RUNTIME_READY_PHASE_PROVISIONING
    assert len(_FakeWatchdog.instances) == 1
    assert _FakeWatchdog.instances[0].fatal
    assert not _FakeWatchdog.instances[0].disarmed


@pytest.mark.asyncio
async def test_gateway_rejects_same_tick_build_and_provisioning_lease_failure(
    monkeypatch,
) -> None:
    store = _RecordingHotStateStore()
    delivery_gates: list[NatsDeliveryGate] = []
    stop_observations: list[tuple[bool, bool]] = []
    calls: list[str] = []
    original_stop = gateway_main._stop_provisioning_lease_before_promotion

    async def _maintain(**kwargs) -> None:
        assert kwargs.get("suppress_failures_when_stopping") is False
        await kwargs["stop_event"].wait()
        raise RuntimeError("synthetic same-tick gateway lease failure")

    async def _start() -> None:
        calls.append("start")

    async def _stop() -> None:
        calls.append("stop")

    runtime = SimpleNamespace(
        process_role="gateway",
        background_tasks=[],
        start_background_tasks=_start,
        stop_background_tasks=_stop,
        register_background_task=lambda *_args, **_kwargs: None,
    )

    async def _build(final_settings, **build_kwargs):
        gate = await _run_strict_build_hook(
            final_settings=final_settings,
            build_kwargs=build_kwargs,
            store=store,
        )
        delivery_gates.append(gate)
        return runtime

    async def _observe_stop(**kwargs) -> None:
        stop_observations.append(
            (
                kwargs["lease_task"].done(),
                delivery_gates[0].aborted,
            )
        )
        await original_stop(**kwargs)

    monkeypatch.setattr(gateway_main, "load_settings", lambda: _settings())
    monkeypatch.setattr(
        gateway_main,
        "configure_logging_for_settings",
        lambda _settings: None,
    )
    monkeypatch.setattr(gateway_main, "build_runtime", _build)
    monkeypatch.setattr(gateway_main, "_maintain_runtime_ready_lease", _maintain)
    monkeypatch.setattr(
        gateway_main,
        "_stop_provisioning_lease_before_promotion",
        _observe_stop,
    )
    monkeypatch.setattr("aats.data_platform.db.validate_rdp_schema", lambda: None)

    with pytest.raises(
        RuntimeError,
        match="synthetic same-tick gateway lease failure",
    ):
        async with gateway_main.lifespan(FastAPI()):
            raise AssertionError("lifespan must not yield")

    assert stop_observations == [(False, False)]
    assert calls == ["stop"]
    assert delivery_gates[0].aborted
    assert store.deleted == []
    assert store.values[_ready_key("gateway", generation=GENERATION)][
        "phase"
    ] == _RUNTIME_READY_PHASE_PROVISIONING
    assert len(_FakeWatchdog.instances) == 1
    assert _FakeWatchdog.instances[0].fatal
    assert not _FakeWatchdog.instances[0].disarmed


@pytest.mark.asyncio
async def test_run_process_pre_promotion_bus_failure_never_publishes_ready(
    monkeypatch,
) -> None:
    """Daemon build 尾部丢 durable 时必须停在 PROVISIONING。"""

    store = _RecordingHotStateStore()
    delivery_gates: list[NatsDeliveryGate] = []
    calls: list[str] = []
    runtime = SimpleNamespace(
        background_tasks=[],
        bus=None,
        start_background_tasks=lambda: asyncio.sleep(0),
        stop_background_tasks=lambda: asyncio.sleep(0),
    )

    class _DeletedDurableBus:
        def __init__(self, gate: NatsDeliveryGate) -> None:
            self._gate = gate

        async def verify_ready_for_promotion(self) -> None:
            calls.append("verify_deleted_durable")
            self._gate.abort()
            raise RuntimeError("nats_not_ready_for_promotion")

    async def _start() -> None:
        calls.append("start")

    async def _stop() -> None:
        calls.append("stop")

    runtime.start_background_tasks = _start
    runtime.stop_background_tasks = _stop

    async def _build(final_settings, **build_kwargs):
        gate = await _run_strict_build_hook(
            final_settings=final_settings,
            build_kwargs=build_kwargs,
            store=store,
        )
        delivery_gates.append(gate)
        runtime.bus = _DeletedDurableBus(gate)
        return runtime

    monkeypatch.setattr(process_lifecycle, "build_runtime", _build)
    monkeypatch.setattr(
        process_lifecycle,
        "configure_logging_for_settings",
        lambda _settings: None,
    )

    result = await run_process(
        process_role="execution",
        app_name="test.fs016.daemon_pre_promotion_bus_failure",
        settings=_settings(),
        stop_event=asyncio.Event(),
    )

    assert result == 1
    assert calls == ["verify_deleted_durable", "stop"]
    assert len(delivery_gates) == 1
    assert delivery_gates[0].aborted
    assert not delivery_gates[0].activated
    assert store.values[_ready_key("execution", generation=GENERATION)][
        "phase"
    ] == _RUNTIME_READY_PHASE_PROVISIONING
    assert store.deleted == []
    assert len(_FakeWatchdog.instances) == 1
    assert _FakeWatchdog.instances[0].fatal
    assert not _FakeWatchdog.instances[0].disarmed


@pytest.mark.asyncio
async def test_gateway_pre_promotion_disconnect_never_publishes_ready(
    monkeypatch,
) -> None:
    """Gateway build 尾部断线时不得 yield lifespan 或晋级 READY。"""

    store = _RecordingHotStateStore()
    delivery_gates: list[NatsDeliveryGate] = []
    calls: list[str] = []
    runtime = SimpleNamespace(
        process_role="gateway",
        background_tasks=[],
        bus=None,
        start_background_tasks=lambda: asyncio.sleep(0),
        stop_background_tasks=lambda: asyncio.sleep(0),
        register_background_task=lambda *_args, **_kwargs: None,
    )

    class _DisconnectedBus:
        def __init__(self, gate: NatsDeliveryGate) -> None:
            self._gate = gate

        async def verify_ready_for_promotion(self) -> None:
            calls.append("verify_disconnected")
            self._gate.abort()
            raise RuntimeError("nats_not_ready_for_promotion")

    async def _start() -> None:
        calls.append("start")

    async def _stop() -> None:
        calls.append("stop")

    runtime.start_background_tasks = _start
    runtime.stop_background_tasks = _stop

    async def _build(final_settings, **build_kwargs):
        gate = await _run_strict_build_hook(
            final_settings=final_settings,
            build_kwargs=build_kwargs,
            store=store,
        )
        delivery_gates.append(gate)
        runtime.bus = _DisconnectedBus(gate)
        return runtime

    monkeypatch.setattr(gateway_main, "load_settings", lambda: _settings())
    monkeypatch.setattr(
        gateway_main,
        "configure_logging_for_settings",
        lambda _settings: None,
    )
    monkeypatch.setattr(gateway_main, "build_runtime", _build)
    monkeypatch.setattr("aats.data_platform.db.validate_rdp_schema", lambda: None)

    with pytest.raises(
        RuntimeError,
        match=(
            "nats_not_ready_for_promotion|"
            "runtime_ready_delivery_gate_aborted:gateway"
        ),
    ):
        async with gateway_main.lifespan(FastAPI()):
            calls.append("yield")

    assert calls == ["verify_disconnected", "stop"]
    assert len(delivery_gates) == 1
    assert delivery_gates[0].aborted
    assert not delivery_gates[0].activated
    assert store.values[_ready_key("gateway", generation=GENERATION)][
        "phase"
    ] == _RUNTIME_READY_PHASE_PROVISIONING
    assert store.deleted == []
    assert len(_FakeWatchdog.instances) == 1
    assert _FakeWatchdog.instances[0].fatal
    assert not _FakeWatchdog.instances[0].disarmed


@pytest.mark.asyncio
async def test_run_process_gate_abort_fences_lease_while_runtime_build_is_blocked(
    monkeypatch,
) -> None:
    store = _RecordingHotStateStore()
    delivery_gates: list[NatsDeliveryGate] = []
    build_blocked = asyncio.Event()
    release_build = asyncio.Event()
    lease_started = asyncio.Event()
    lease_frozen = asyncio.Event()
    absolute_deadlines: list[float | None] = []

    async def _maintain(**kwargs) -> None:
        assert kwargs.get("suppress_failures_when_stopping") is False
        absolute_deadlines.append(
            kwargs.get("absolute_hard_deadline_monotonic")
        )
        lease_started.set()
        await kwargs["stop_event"].wait()
        lease_frozen.set()

    async def _build(final_settings, **build_kwargs):
        delivery_gates.append(
            await _run_strict_build_hook(
                final_settings=final_settings,
                build_kwargs=build_kwargs,
                store=store,
            )
        )
        await lease_started.wait()
        build_blocked.set()
        await release_build.wait()
        raise RuntimeError("synthetic blocked daemon build failure")

    monkeypatch.setattr(process_lifecycle, "build_runtime", _build)
    monkeypatch.setattr(
        process_lifecycle,
        "configure_logging_for_settings",
        lambda _settings: None,
    )
    monkeypatch.setattr(
        process_lifecycle,
        "_maintain_runtime_ready_lease",
        _maintain,
    )

    process_task = asyncio.create_task(
        run_process(
            process_role="market",
            app_name="test.fs016.run_process_build_gate_abort",
            settings=_settings(),
            stop_event=asyncio.Event(),
        )
    )
    try:
        await asyncio.wait_for(build_blocked.wait(), timeout=0.5)
        assert len(delivery_gates) == 1
        assert len(_FakeWatchdog.instances) == 1
        delivery_gates[0].abort()

        await asyncio.wait_for(lease_frozen.wait(), timeout=0.5)
        watchdog = _FakeWatchdog.instances[0]
        assert watchdog.fatal
        assert not watchdog.disarmed
        assert absolute_deadlines[0] is not None
        assert store.values[_ready_key("market", generation=GENERATION)][
            "phase"
        ] == _RUNTIME_READY_PHASE_PROVISIONING
        assert store.deleted == []
        assert not store.closed
    finally:
        release_build.set()

    assert await asyncio.wait_for(process_task, timeout=0.5) == 1
    assert store.deleted == []
    assert store.closed


@pytest.mark.asyncio
async def test_gateway_gate_abort_fences_lease_while_runtime_build_is_blocked(
    monkeypatch,
) -> None:
    store = _RecordingHotStateStore()
    delivery_gates: list[NatsDeliveryGate] = []
    build_blocked = asyncio.Event()
    release_build = asyncio.Event()
    lease_started = asyncio.Event()
    lease_frozen = asyncio.Event()
    absolute_deadlines: list[float | None] = []

    async def _maintain(**kwargs) -> None:
        assert kwargs.get("suppress_failures_when_stopping") is False
        absolute_deadlines.append(
            kwargs.get("absolute_hard_deadline_monotonic")
        )
        lease_started.set()
        await kwargs["stop_event"].wait()
        lease_frozen.set()

    async def _build(final_settings, **build_kwargs):
        delivery_gates.append(
            await _run_strict_build_hook(
                final_settings=final_settings,
                build_kwargs=build_kwargs,
                store=store,
            )
        )
        await lease_started.wait()
        build_blocked.set()
        await release_build.wait()
        raise RuntimeError("synthetic blocked gateway build failure")

    async def _serve() -> None:
        with pytest.raises(
            RuntimeError,
            match="runtime_ready_delivery_gate_aborted:gateway",
        ):
            async with gateway_main.lifespan(FastAPI()):
                raise AssertionError("lifespan must not yield")

    monkeypatch.setattr(gateway_main, "load_settings", lambda: _settings())
    monkeypatch.setattr(
        gateway_main,
        "configure_logging_for_settings",
        lambda _settings: None,
    )
    monkeypatch.setattr(gateway_main, "build_runtime", _build)
    monkeypatch.setattr(gateway_main, "_maintain_runtime_ready_lease", _maintain)
    monkeypatch.setattr("aats.data_platform.db.validate_rdp_schema", lambda: None)

    lifespan_task = asyncio.create_task(_serve())
    try:
        await asyncio.wait_for(build_blocked.wait(), timeout=0.5)
        assert len(delivery_gates) == 1
        assert len(_FakeWatchdog.instances) == 1
        delivery_gates[0].abort()

        await asyncio.wait_for(lease_frozen.wait(), timeout=0.5)
        watchdog = _FakeWatchdog.instances[0]
        assert watchdog.fatal
        assert not watchdog.disarmed
        assert absolute_deadlines[0] is not None
        assert store.values[_ready_key("gateway", generation=GENERATION)][
            "phase"
        ] == _RUNTIME_READY_PHASE_PROVISIONING
        assert store.deleted == []
        assert not store.closed
    finally:
        release_build.set()

    await asyncio.wait_for(lifespan_task, timeout=0.5)
    assert store.deleted == []
    assert store.closed


@pytest.mark.asyncio
async def test_provisioning_absolute_deadline_survives_refresh_and_fences_hung_build(
    monkeypatch,
) -> None:
    store = _RecordingHotStateStore()
    delivery_gates: list[NatsDeliveryGate] = []
    build_blocked = asyncio.Event()
    release_build = asyncio.Event()
    original_maintainer = _maintain_runtime_ready_lease
    original_hard_deadline = process_lifecycle._runtime_ready_hard_deadline

    async def _fast_maintain(**kwargs) -> None:
        assert kwargs.get("absolute_hard_deadline_monotonic") is not None
        assert kwargs.get("suppress_failures_when_stopping") is False
        await original_maintainer(
            **kwargs,
            ttl_seconds=0.09,
            renew_interval=0.01,
            shutdown_margin=0.02,
        )

    async def _build(final_settings, **build_kwargs):
        delivery_gates.append(
            await _run_strict_build_hook(
                final_settings=final_settings,
                build_kwargs=build_kwargs,
                store=store,
            )
        )
        build_blocked.set()
        await release_build.wait()
        raise RuntimeError("synthetic post-deadline daemon build failure")

    def _fast_hard_deadline(**kwargs) -> float:
        kwargs["force_exit_grace"] = 0.01
        kwargs["provisioning_exit_guard"] = 0.01
        return original_hard_deadline(**kwargs)

    monkeypatch.setattr(
        process_lifecycle,
        "_RUNTIME_READY_MAX_PROVISIONING_SECONDS",
        0.10,
    )
    monkeypatch.setattr(
        process_lifecycle,
        "_RUNTIME_READY_FORCE_EXIT_GRACE_SECONDS",
        0.01,
    )
    monkeypatch.setattr(process_lifecycle, "build_runtime", _build)
    monkeypatch.setattr(
        process_lifecycle,
        "_runtime_ready_hard_deadline",
        _fast_hard_deadline,
    )
    monkeypatch.setattr(
        process_lifecycle,
        "configure_logging_for_settings",
        lambda _settings: None,
    )
    monkeypatch.setattr(
        process_lifecycle,
        "_maintain_runtime_ready_lease",
        _fast_maintain,
    )

    process_task = asyncio.create_task(
        run_process(
            process_role="market",
            app_name="test.fs016.provisioning_absolute_deadline",
            settings=_settings(),
            stop_event=asyncio.Event(),
        )
    )
    try:
        await asyncio.wait_for(build_blocked.wait(), timeout=0.5)
        watchdog = _FakeWatchdog.instances[0]
        absolute_deadline = watchdog.initial_deadline_monotonic

        await asyncio.wait_for(delivery_gates[0].wait_aborted(), timeout=0.5)
        assert watchdog.fatal
        assert not watchdog.disarmed
        assert len(watchdog.rearm_deadlines) >= 2
        assert all(
            deadline <= absolute_deadline + 0.001
            for deadline in watchdog.rearm_deadlines
        )
        assert watchdog.deadline_monotonic <= absolute_deadline + 0.001
        assert store.values[_ready_key("market", generation=GENERATION)][
            "phase"
        ] == _RUNTIME_READY_PHASE_PROVISIONING
        assert store.deleted == []
    finally:
        release_build.set()

    assert await asyncio.wait_for(process_task, timeout=0.5) == 1
    assert store.deleted == []
    assert store.closed


@pytest.mark.asyncio
async def test_run_process_claims_before_nats_and_activates_after_peer_ready(
    monkeypatch,
) -> None:
    store = _RecordingHotStateStore()
    calls: list[str] = []
    delivery_gates: list[NatsDeliveryGate] = []
    peer_wait_entered = asyncio.Event()
    peers_ready = asyncio.Event()
    stop_event = asyncio.Event()
    stop_event.set()

    async def _start() -> None:
        gate = delivery_gates[0]
        assert gate.activated
        assert not gate.aborted
        assert store.values[_ready_key("execution")]["phase"] == (
            _RUNTIME_READY_PHASE_READY
        )
        calls.append("background_started")

    async def _stop() -> None:
        assert _ready_key("execution") in store.values
        calls.append("stop")

    runtime = SimpleNamespace(
        background_tasks=[],
        start_background_tasks=_start,
        stop_background_tasks=_stop,
    )

    async def _build(final_settings, **build_kwargs):
        gate = await _run_strict_build_hook(
            final_settings=final_settings,
            build_kwargs=build_kwargs,
            store=store,
        )
        delivery_gates.append(gate)
        assert store.values[_ready_key("execution")]["phase"] == (
            _RUNTIME_READY_PHASE_PROVISIONING
        )
        calls.append("claimed_before_nats")
        assert not gate.activated
        assert not gate.aborted
        calls.append("nats_connected")
        return runtime

    async def _wait_for_peers(**_kwargs) -> None:
        gate = delivery_gates[0]
        assert store.values[_ready_key("execution")]["phase"] == (
            _RUNTIME_READY_PHASE_READY
        )
        assert not gate.activated
        assert not gate.aborted
        calls.append("waiting_for_peers")
        peer_wait_entered.set()
        await peers_ready.wait()
        calls.append("peers_ready")

    async def _heartbeat(
        _role, *, stop_event, logger, started_event=None
    ) -> None:
        del logger
        if started_event is not None:
            started_event.set()
        await stop_event.wait()

    monkeypatch.setattr(process_lifecycle, "build_runtime", _build)
    monkeypatch.setattr(
        process_lifecycle,
        "configure_logging_for_settings",
        lambda _settings: None,
    )
    monkeypatch.setattr(
        process_lifecycle,
        "_wait_for_peer_roles_ready",
        _wait_for_peers,
    )
    monkeypatch.setattr(process_lifecycle, "_heartbeat_loop", _heartbeat)

    process_task = asyncio.create_task(
        run_process(
            process_role="execution",
            app_name="test.fs016.run_process_order",
            settings=_settings(),
            stop_event=stop_event,
        )
    )
    await asyncio.wait_for(peer_wait_entered.wait(), timeout=0.5)
    assert calls == [
        "claimed_before_nats",
        "nats_connected",
        "waiting_for_peers",
    ]
    assert not delivery_gates[0].activated
    assert not delivery_gates[0].aborted

    peers_ready.set()
    assert await asyncio.wait_for(process_task, timeout=0.5) == 0
    assert calls == [
        "claimed_before_nats",
        "nats_connected",
        "waiting_for_peers",
        "peers_ready",
        "background_started",
        "stop",
    ]
    # 正常 shutdown 在任何 bus cleanup 前关闭 delivery gate；激活只允许存在于
    # steady state，进程退出后的终态必须是 sticky ABORT。
    assert not delivery_gates[0].activated
    assert delivery_gates[0].aborted
    assert _ready_key("execution") not in store.values
    assert len(_FakeWatchdog.instances) == 1
    assert _FakeWatchdog.instances[0].disarmed
    assert not _FakeWatchdog.instances[0].fatal


@pytest.mark.asyncio
async def test_gateway_claims_before_nats_and_activates_after_peer_ready(
    monkeypatch,
) -> None:
    store = _RecordingHotStateStore()
    calls: list[str] = []
    delivery_gates: list[NatsDeliveryGate] = []
    peer_wait_entered = asyncio.Event()
    peers_ready = asyncio.Event()
    lifespan_yielded = asyncio.Event()
    stop_lifespan = asyncio.Event()

    async def _start() -> None:
        gate = delivery_gates[0]
        assert gate.activated
        assert not gate.aborted
        assert store.values[_ready_key("gateway")]["phase"] == (
            _RUNTIME_READY_PHASE_READY
        )
        calls.append("background_started")

    async def _stop() -> None:
        assert _ready_key("gateway") in store.values
        calls.append("stop")

    runtime = SimpleNamespace(
        process_role="gateway",
        start_background_tasks=_start,
        stop_background_tasks=_stop,
        register_background_task=lambda *_args, **_kwargs: None,
    )

    async def _build(final_settings, **build_kwargs):
        gate = await _run_strict_build_hook(
            final_settings=final_settings,
            build_kwargs=build_kwargs,
            store=store,
        )
        delivery_gates.append(gate)
        assert store.values[_ready_key("gateway")]["phase"] == (
            _RUNTIME_READY_PHASE_PROVISIONING
        )
        calls.append("claimed_before_nats")
        assert not gate.activated
        assert not gate.aborted
        calls.append("nats_connected")
        return runtime

    async def _wait_for_peers(**_kwargs) -> None:
        gate = delivery_gates[0]
        assert store.values[_ready_key("gateway")]["phase"] == (
            _RUNTIME_READY_PHASE_READY
        )
        assert not gate.activated
        assert not gate.aborted
        calls.append("waiting_for_peers")
        peer_wait_entered.set()
        await peers_ready.wait()
        calls.append("peers_ready")

    async def _serve(local_app: FastAPI) -> None:
        async with gateway_main.lifespan(local_app):
            calls.append("lifespan_yielded")
            lifespan_yielded.set()
            await stop_lifespan.wait()

    monkeypatch.setattr(gateway_main, "load_settings", lambda: _settings())
    monkeypatch.setattr(
        gateway_main,
        "configure_logging_for_settings",
        lambda _settings: None,
    )
    monkeypatch.setattr(gateway_main, "build_runtime", _build)
    monkeypatch.setattr(
        gateway_main,
        "_wait_for_peer_roles_ready",
        _wait_for_peers,
    )
    monkeypatch.setattr(
        gateway_main,
        "start_dashboard_snapshot_plane",
        lambda *_args, **_kwargs: asyncio.sleep(0),
    )
    monkeypatch.setattr(
        gateway_main,
        "stop_dashboard_snapshot_plane",
        lambda *_args, **_kwargs: asyncio.sleep(0),
    )
    monkeypatch.setattr("aats.data_platform.db.validate_rdp_schema", lambda: None)

    local_app = FastAPI()
    lifespan_task = asyncio.create_task(_serve(local_app))
    await asyncio.wait_for(peer_wait_entered.wait(), timeout=0.5)
    assert calls == [
        "claimed_before_nats",
        "nats_connected",
        "waiting_for_peers",
    ]
    assert not delivery_gates[0].activated
    assert not delivery_gates[0].aborted

    peers_ready.set()
    await asyncio.wait_for(lifespan_yielded.wait(), timeout=0.5)
    assert calls == [
        "claimed_before_nats",
        "nats_connected",
        "waiting_for_peers",
        "peers_ready",
        "background_started",
        "lifespan_yielded",
    ]
    assert delivery_gates[0].activated
    assert not delivery_gates[0].aborted

    stop_lifespan.set()
    await asyncio.wait_for(lifespan_task, timeout=0.5)
    assert calls[-1] == "stop"
    assert _ready_key("gateway") not in store.values
    assert len(_FakeWatchdog.instances) == 1
    assert _FakeWatchdog.instances[0].disarmed
    assert not _FakeWatchdog.instances[0].fatal


@pytest.mark.asyncio
async def test_gateway_barrier_failure_aborts_gate_without_starting_publishers(
    monkeypatch,
) -> None:
    store = _RecordingHotStateStore()
    calls: list[str] = []
    delivery_gates: list[NatsDeliveryGate] = []

    async def _start() -> None:
        calls.append("start")

    async def _stop() -> None:
        calls.append("stop")

    runtime = SimpleNamespace(
        hot_state_store=store,
        start_background_tasks=_start,
        stop_background_tasks=_stop,
        register_background_task=lambda *_args, **_kwargs: None,
    )

    async def _build(final_settings, **build_kwargs):
        delivery_gates.append(
            await _run_strict_build_hook(
                final_settings=final_settings,
                build_kwargs=build_kwargs,
                store=store,
            )
        )
        return runtime

    async def _fail_wait(**_kwargs) -> None:
        raise RuntimeError("runtime_ready_gate_timeout:gateway:market")

    monkeypatch.setattr(gateway_main, "load_settings", lambda: _settings())
    monkeypatch.setattr(
        gateway_main,
        "configure_logging_for_settings",
        lambda _settings: None,
    )
    monkeypatch.setattr(gateway_main, "build_runtime", _build)
    monkeypatch.setattr(gateway_main, "_wait_for_peer_roles_ready", _fail_wait)
    monkeypatch.setattr("aats.data_platform.db.validate_rdp_schema", lambda: None)

    local_app = FastAPI()
    with pytest.raises(RuntimeError, match="runtime_ready_gate_timeout"):
        async with gateway_main.lifespan(local_app):
            raise AssertionError("lifespan must not yield")

    assert calls == ["stop"]
    assert not hasattr(local_app.state, "runtime")
    assert len(delivery_gates) == 1
    assert delivery_gates[0].aborted
    assert not delivery_gates[0].activated
    assert len(_FakeWatchdog.instances) == 1
    assert _FakeWatchdog.instances[0].fatal
    assert not _FakeWatchdog.instances[0].disarmed


@pytest.mark.asyncio
async def test_gateway_startup_is_cancelled_and_gate_aborted_when_lease_fails(
    monkeypatch,
) -> None:
    store = _RecordingHotStateStore()
    for peer in ("market", "decision", "execution"):
        store.values[_ready_key(peer, generation=GENERATION)] = _lease_payload(peer)
    start_entered = asyncio.Event()
    calls: list[str] = []
    delivery_gates: list[NatsDeliveryGate] = []

    async def _start() -> None:
        assert delivery_gates[0].activated
        calls.append("start_entered")
        start_entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            calls.append("start_cancelled")
            raise

    async def _stop() -> None:
        assert _ready_key("gateway", generation=GENERATION) in store.values
        calls.append("stop")

    runtime = SimpleNamespace(
        hot_state_store=store,
        start_background_tasks=_start,
        stop_background_tasks=_stop,
        register_background_task=lambda *_args, **_kwargs: None,
    )

    async def _fail_lease(**_kwargs) -> None:
        if _kwargs["lease"].phase == _RUNTIME_READY_PHASE_PROVISIONING:
            await _kwargs["stop_event"].wait()
            return
        await start_entered.wait()
        raise RuntimeError("runtime_ready_lease_lost:gateway")

    async def _build(final_settings, **build_kwargs):
        delivery_gates.append(
            await _run_strict_build_hook(
                final_settings=final_settings,
                build_kwargs=build_kwargs,
                store=store,
            )
        )
        return runtime

    monkeypatch.setattr(gateway_main, "load_settings", lambda: _settings())
    monkeypatch.setattr(
        gateway_main,
        "configure_logging_for_settings",
        lambda _settings: None,
    )
    monkeypatch.setattr(gateway_main, "build_runtime", _build)
    monkeypatch.setattr(
        gateway_main,
        "_maintain_runtime_ready_lease",
        _fail_lease,
    )
    monkeypatch.setattr("aats.data_platform.db.validate_rdp_schema", lambda: None)

    local_app = FastAPI()
    with pytest.raises(RuntimeError, match="runtime_ready_lease_lost:gateway"):
        async with gateway_main.lifespan(local_app):
            raise AssertionError("lifespan must not yield")

    assert calls == ["start_entered", "start_cancelled", "stop"]
    assert not hasattr(local_app.state, "runtime")
    assert len(delivery_gates) == 1
    assert delivery_gates[0].aborted
    assert not delivery_gates[0].activated
    assert len(_FakeWatchdog.instances) == 1
    assert _FakeWatchdog.instances[0].fatal
    assert not _FakeWatchdog.instances[0].disarmed


def test_standard_deploy_generates_and_injects_required_generation() -> None:
    deploy_source = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    compose_source = (
        REPO_ROOT / "deploy" / "wsl2-dev" / "docker-compose.aats.yml"
    ).read_text(encoding="utf-8")

    assert "prepare_runtime_readiness_generation()" in deploy_source
    assert deploy_source.index("step_sync\n") < deploy_source.index(
        "prepare_runtime_readiness_generation\n"
    ) < deploy_source.index("step_build\n")
    assert "AATS_RUNTIME_READINESS_GENERATION='" in deploy_source
    assert "AATS_DEPLOYED_GIT_COMMIT='" in deploy_source
    assert "--runtime-readiness-generation '$RUNTIME_READINESS_GENERATION'" in deploy_source
    assert (
        'AATS_RUNTIME_READINESS_GENERATION: "${AATS_RUNTIME_READINESS_GENERATION:?'
        in compose_source
    )
    assert (
        'AATS_DEPLOYED_GIT_COMMIT: "${AATS_DEPLOYED_GIT_COMMIT:?'
        in compose_source
    )
