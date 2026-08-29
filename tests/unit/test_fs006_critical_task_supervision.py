"""FS-006：关键后台 task 结束不得与健康进程并存。"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from apps.api_gateway import main as gateway_main
from aats.bootstrap import process_lifecycle
from aats.bootstrap.config import (
    ApplicationRuntime,
    CriticalBackgroundTaskFailure,
)
from aats.bootstrap.process_lifecycle import run_process
from aats.bootstrap.settings import PROCESS_ROLE_EXECUTION
from aats.services.decision_engine.trigger import DecisionCycleTrigger
from aats.services.governance_engine.abort_hooks import AbortHookService
from aats.services.market_gateway.gateway import MarketDataGateway


class _SupervisedFakeRuntime:
    register_background_task = ApplicationRuntime.register_background_task
    mark_critical_background_task_success = (
        ApplicationRuntime.mark_critical_background_task_success
    )
    critical_background_task_failure = (
        ApplicationRuntime.critical_background_task_failure
    )
    wait_for_critical_background_task_failure = (
        ApplicationRuntime.wait_for_critical_background_task_failure
    )

    def __init__(self, outcome: str) -> None:
        self.outcome = outcome
        self.background_tasks: list[asyncio.Task] = []
        self.critical_background_tasks: dict[str, asyncio.Task] = {}
        self.critical_background_task_progress = {}
        self.hot_state_store = None
        self.stopped = False

    async def start_background_tasks(self) -> None:
        async def _worker() -> None:
            await asyncio.sleep(0)
            if self.outcome == "exception":
                raise RuntimeError("sensitive-exception-body-must-not-leak")
            if self.outcome == "complete":
                return
            await asyncio.Future()

        task = asyncio.create_task(_worker(), name="aats_execution_command_flow")
        self.register_background_task(
            task,
            name="aats_execution_command_flow",
            critical=self.outcome != "noncritical",
            progress_timeout_seconds=(
                0.02 if self.outcome == "stalled" else None
            ),
        )
        if self.outcome == "cancelled":
            task.cancel()

    async def stop_background_tasks(self) -> None:
        for task in self.background_tasks:
            if not task.done():
                task.cancel()
            try:
                await task
            except (asyncio.CancelledError, RuntimeError):
                pass
        self.stopped = True


async def _noop_readiness(**_kwargs) -> None:
    return None


def _run_supervised_process(outcome: str, *, external_stop: bool = False):
    runtime = _SupervisedFakeRuntime(outcome)
    heartbeat_ticks = 0

    async def _fake_build(_settings, *, process_role):
        assert process_role == PROCESS_ROLE_EXECUTION
        return runtime

    async def _fake_heartbeat(_role, *, stop_event, logger, **_kwargs):
        del logger
        nonlocal heartbeat_ticks
        started_event = _kwargs.get("started_event")
        if started_event is not None:
            started_event.set()
        while not stop_event.is_set():
            heartbeat_ticks += 1
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=0.005)
            except TimeoutError:
                pass

    async def _run() -> int:
        stop_event = asyncio.Event()
        if external_stop:
            asyncio.get_running_loop().call_later(0.03, stop_event.set)
        with (
            patch.object(
                process_lifecycle,
                "build_runtime",
                side_effect=_fake_build,
            ),
            patch.object(
                process_lifecycle,
                "configure_logging_for_settings",
            ),
            patch.object(
                process_lifecycle,
                "_announce_runtime_ready",
                side_effect=_noop_readiness,
            ),
            patch.object(
                process_lifecycle,
                "_wait_for_peer_roles_ready",
                side_effect=_noop_readiness,
            ),
            patch.object(
                process_lifecycle,
                "_heartbeat_loop",
                side_effect=_fake_heartbeat,
            ),
        ):
            return await asyncio.wait_for(
                run_process(
                    process_role=PROCESS_ROLE_EXECUTION,
                    app_name="test.fs006",
                    settings=SimpleNamespace(),
                    stop_event=stop_event,
                ),
                timeout=0.5,
            )

    rc = asyncio.run(_run())
    return rc, runtime, heartbeat_ticks


@pytest.mark.parametrize(
    ("outcome", "expected_kind", "expected_error_type"),
    [
        ("exception", "exception", "RuntimeError"),
        ("complete", "unexpected_completion", None),
        ("cancelled", "cancelled", "CancelledError"),
    ],
)
def test_run_process_returns_nonzero_when_critical_task_ends(
    outcome: str,
    expected_kind: str,
    expected_error_type: str | None,
) -> None:
    rc, runtime, ticks_at_return = _run_supervised_process(outcome)

    assert rc == 1
    assert runtime.stopped is True
    failure = runtime.critical_background_task_failure()
    assert failure == CriticalBackgroundTaskFailure(
        task_name="aats_execution_command_flow",
        failure_kind=expected_kind,
        error_type=expected_error_type,
    )
    assert ticks_at_return >= 1


def test_noncritical_task_completion_does_not_fail_process() -> None:
    rc, runtime, _ticks = _run_supervised_process(
        "noncritical",
        external_stop=True,
    )

    assert rc == 0
    assert runtime.stopped is True
    assert runtime.critical_background_task_failure() is None


def test_pending_critical_task_stall_fails_process_without_external_stop() -> None:
    rc, runtime, heartbeat_ticks = _run_supervised_process("stalled")

    assert rc == 1
    assert runtime.stopped is True
    assert heartbeat_ticks >= 1


def test_critical_task_name_cannot_silently_replace_another_task() -> None:
    async def _run() -> None:
        runtime = _SupervisedFakeRuntime("complete")
        first = asyncio.create_task(
            asyncio.sleep(60),
            name="duplicate-critical-name",
        )
        second = asyncio.create_task(
            asyncio.sleep(60),
            name="duplicate-critical-name",
        )
        runtime.register_background_task(first, critical=True)
        with pytest.raises(RuntimeError, match="already registered"):
            runtime.register_background_task(second, critical=True)
        first.cancel()
        second.cancel()
        await asyncio.gather(first, second, return_exceptions=True)

    asyncio.run(_run())


@pytest.mark.parametrize(
    "timeout_seconds",
    [0.0, -1.0, float("nan"), float("inf")],
)
def test_progress_timeout_must_be_finite_and_positive(
    timeout_seconds: float,
) -> None:
    async def _run() -> None:
        runtime = _SupervisedFakeRuntime("complete")
        task = asyncio.create_task(asyncio.sleep(60), name="invalid-timeout")
        try:
            with pytest.raises(ValueError, match="finite and positive"):
                runtime.register_background_task(
                    task,
                    critical=True,
                    progress_timeout_seconds=timeout_seconds,
                )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(_run())


def test_noncritical_task_cannot_declare_progress_timeout() -> None:
    async def _run() -> None:
        runtime = _SupervisedFakeRuntime("complete")
        task = asyncio.create_task(
            asyncio.sleep(60),
            name="noncritical-timeout",
        )
        try:
            with pytest.raises(ValueError, match="non-critical"):
                runtime.register_background_task(
                    task,
                    critical=False,
                    progress_timeout_seconds=1.0,
                )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(_run())


def test_success_checkpoint_extends_progress_deadline() -> None:
    async def _run() -> None:
        runtime = _SupervisedFakeRuntime("complete")
        task = asyncio.create_task(asyncio.sleep(60), name="periodic-task")
        runtime.register_background_task(
            task,
            critical=True,
            progress_timeout_seconds=0.08,
        )
        waiter = asyncio.create_task(
            runtime.wait_for_critical_background_task_failure()
        )
        try:
            await asyncio.sleep(0.05)
            runtime.mark_critical_background_task_success("periodic-task")
            await asyncio.sleep(0.04)
            assert waiter.done() is False
            failure = await asyncio.wait_for(waiter, timeout=0.08)
            assert failure.task_name == "periodic-task"
            assert failure.failure_kind == "stalled"
            assert failure.error_type is None
            assert failure.stalled_seconds is not None
            assert failure.timeout_seconds == 0.08
        finally:
            task.cancel()
            waiter.cancel()
            await asyncio.gather(task, waiter, return_exceptions=True)

    asyncio.run(_run())


def test_pending_event_driven_task_without_budget_is_not_time_classified() -> None:
    async def _run() -> None:
        runtime = _SupervisedFakeRuntime("complete")
        task = asyncio.create_task(asyncio.sleep(60), name="event-driven-task")
        runtime.register_background_task(task, critical=True)
        try:
            await asyncio.sleep(0.03)
            assert runtime.critical_background_task_failure() is None
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(_run())


def test_failure_snapshot_never_contains_exception_body() -> None:
    async def _run() -> CriticalBackgroundTaskFailure:
        runtime = _SupervisedFakeRuntime("exception")
        await runtime.start_background_tasks()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        failure = runtime.critical_background_task_failure()
        assert failure is not None
        return failure

    failure = asyncio.run(_run())
    assert failure.error_type == "RuntimeError"
    assert "sensitive-exception-body" not in repr(failure)


def test_background_failure_sink_outage_does_not_terminate_caller() -> None:
    class _UnavailableEventStore:
        def latest(self, *_args, **_kwargs):
            raise RuntimeError("event-store-unavailable")

    async def _run() -> None:
        runtime = ApplicationRuntime.__new__(ApplicationRuntime)
        runtime.background_failure_messages = {}
        runtime.event_store = _UnavailableEventStore()
        runtime.logger = object()
        with patch("aats.bootstrap.config.log_event"):
            await runtime._record_background_failure(
                subsystem="phase1_shadow_monitor",
                exc=RuntimeError("database-unavailable"),
            )
        assert "phase1_shadow_monitor" in runtime.background_failure_messages

    asyncio.run(_run())


def test_background_recovery_sink_outage_retains_failure_without_raising() -> None:
    class _UnavailableEventStore:
        def append(self, *_args, **_kwargs):
            raise RuntimeError("event-store-unavailable")

    async def _run() -> None:
        runtime = ApplicationRuntime.__new__(ApplicationRuntime)
        runtime.background_failure_messages = {
            "phase1_shadow_monitor": "previous-failure"
        }
        runtime.event_store = _UnavailableEventStore()
        runtime.logger = object()
        with patch("aats.bootstrap.config.log_event"):
            await runtime._record_background_recovery(
                subsystem="phase1_shadow_monitor"
            )
        assert runtime.background_failure_messages == {
            "phase1_shadow_monitor": "previous-failure"
        }

    asyncio.run(_run())


def test_phase1_shadow_monitor_records_recovery_before_success_checkpoint() -> None:
    source = inspect.getsource(ApplicationRuntime._monitor_phase1_shadow_loop)

    recovery_offset = source.index("_record_background_recovery")
    success_offset = source.index("mark_critical_background_task_success")
    assert recovery_offset < success_offset


def test_service_owned_long_running_tasks_are_exposed_for_supervision() -> None:
    async def _run() -> None:
        task = asyncio.create_task(asyncio.sleep(60), name="owned-task")
        try:
            market_gateway = MarketDataGateway.__new__(MarketDataGateway)
            market_gateway._background_task = task
            market_gateway._fallback_task = None
            assert market_gateway.critical_background_tasks() == (task,)

            decision_trigger = DecisionCycleTrigger.__new__(DecisionCycleTrigger)
            decision_trigger._dispatcher_task = task
            assert decision_trigger.background_task is task

            abort_hook = AbortHookService.__new__(AbortHookService)
            abort_hook._task = task
            assert abort_hook.background_task is task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(_run())


def test_runtime_declares_trading_and_guard_loops_critical() -> None:
    source = inspect.getsource(ApplicationRuntime.start_background_tasks)
    required_names = {
        "aats_okx_private_account_ws",
        "aats_reconciliation_refresh",
        "aats_okx_account_refresh",
        "aats_okx_execution_sync",
        "aats_execution_outbox_flush",
        "aats_execution_command_flow",
        "aats_phase1_shadow_monitor",
        "aats_trial_guard_monitor",
    }
    for task_name in required_names:
        task_offset = source.index(task_name)
        declaration = source[task_offset : task_offset + 160]
        assert "critical=True" in declaration, task_name


def test_runtime_declares_progress_budget_for_fixed_period_critical_loops() -> None:
    source = inspect.getsource(ApplicationRuntime.start_background_tasks)
    periodic_task_names = {
        "aats_reconciliation_refresh",
        "aats_okx_account_refresh",
        "aats_okx_execution_sync",
        "aats_execution_outbox_flush",
        "aats_execution_command_flow",
        "aats_phase1_shadow_monitor",
        "aats_trial_guard_monitor",
    }
    for task_name in periodic_task_names:
        task_offset = source.index(task_name)
        declaration = source[task_offset : task_offset + 700]
        assert "progress_timeout_seconds=" in declaration, task_name

    event_driven_task_offset = source.index("aats_okx_private_account_ws")
    event_driven_declaration = source[
        event_driven_task_offset : event_driven_task_offset + 220
    ]
    assert "progress_timeout_seconds=" not in event_driven_declaration


def test_healthz_returns_503_with_safe_detail_on_critical_failure(
    monkeypatch,
) -> None:
    monkeypatch.setenv("AATS_PROCESS_ROLE", "gateway")
    sentinel = object()
    previous_runtime = getattr(gateway_main.app.state, "runtime", sentinel)
    failure = CriticalBackgroundTaskFailure(
        task_name="aats_execution_command_flow",
        failure_kind="exception",
        error_type="RuntimeError",
    )
    gateway_main.app.state.runtime = SimpleNamespace(
        critical_background_task_failure=lambda: failure
    )
    try:
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(gateway_main.healthz())
    finally:
        if previous_runtime is sentinel:
            del gateway_main.app.state.runtime
        else:
            gateway_main.app.state.runtime = previous_runtime

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == {
        "status": "unhealthy",
        "reason": "critical_background_task_failed",
        "task_name": "aats_execution_command_flow",
        "failure_kind": "exception",
        "error_type": "RuntimeError",
    }
    assert "sensitive-exception-body" not in str(exc_info.value.detail)


def test_healthz_returns_503_with_safe_stall_metadata(monkeypatch) -> None:
    monkeypatch.setenv("AATS_PROCESS_ROLE", "gateway")
    sentinel = object()
    previous_runtime = getattr(gateway_main.app.state, "runtime", sentinel)
    failure = CriticalBackgroundTaskFailure(
        task_name="aats_reconciliation_refresh",
        failure_kind="stalled",
        stalled_seconds=181.25,
        timeout_seconds=180.0,
    )
    gateway_main.app.state.runtime = SimpleNamespace(
        critical_background_task_failure=lambda: failure
    )
    try:
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(gateway_main.healthz())
    finally:
        if previous_runtime is sentinel:
            del gateway_main.app.state.runtime
        else:
            gateway_main.app.state.runtime = previous_runtime

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == {
        "status": "unhealthy",
        "reason": "critical_background_task_failed",
        "task_name": "aats_reconciliation_refresh",
        "failure_kind": "stalled",
        "error_type": None,
        "stalled_seconds": 181.25,
        "timeout_seconds": 180.0,
    }
