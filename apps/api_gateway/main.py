from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Callable
from contextlib import asynccontextmanager
from time import monotonic

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from aats.api.auth_routes import (
    auth_router,
    invalidate_bundle_cache,
    start_dashboard_snapshot_plane,
    stop_dashboard_snapshot_plane,
)
from aats.api.rdp_profile_routes import profile_router as rdp_profile_router
from aats.api.rdp_routes import rdp_router
from aats.api.routes import router
from aats.api.security_headers import (
    DEFAULT_GATEWAY_ALLOWED_HOSTS,
    GatewayBrowserSecurityMiddleware,
)
from aats.api.ui import ui_router
from aats.bootstrap.config import ApplicationRuntime, build_runtime, load_settings
from aats.bootstrap.logging import get_logger as _get_lifecycle_logger
from aats.bootstrap.process_lifecycle import (
    _RUNTIME_READY_FORCE_EXIT_GRACE_SECONDS,
    _RUNTIME_READY_MAX_PROVISIONING_SECONDS,
    _RuntimeReadyDeadlineWatchdog,
    _activate_runtime_event_delivery,
    _announce_runtime_ready,
    _await_task_while_runtime_ready,
    _cancel_runtime_tasks_for_readiness_failure,
    _hard_exit_process,
    _maintain_runtime_ready_lease,
    _promote_runtime_ready,
    _runtime_ready_hard_deadline,
    _runtime_ready_clock,
    _runtime_readiness_generation,
    _strict_peer_readiness_required,
    _stop_provisioning_lease_before_promotion,
    _stop_runtime_ready_lease_task,
    _validate_runtime_readiness_backend,
    _verify_runtime_event_bus_ready_for_promotion,
    _wait_for_peer_roles_ready,
    _wait_for_runtime_takeover_quarantine,
    _withdraw_runtime_ready,
)
from aats.bootstrap.logging import configure_logging_for_settings
from aats.bootstrap.settings import (
    AATSSettings,
    ALLOWED_PROCESS_ROLES,
    PROCESS_ROLE_GATEWAY,
    PROCESS_ROLE_MONOLITH,
)
from aats.bus.nats_bus import NatsDeliveryGate
from aats.bootstrap.telemetry import start_span
from aats.data_platform.governance._exceptions import (
    DBConstraintViolation,
    DBUnavailableError,
)

# 任何 mutation 请求（POST/PATCH/PUT/DELETE）成功后都要把 dashboard bundle
# 缓存清空一次。否则用户切 mode / 触发 halt / 激活 profile 后，紧接着的
# refreshDashboard 仍可能在 2 秒 TTL 窗口里命中上一个快照，让 UI 看不到
# 刚刚发生的状态变化。只在 2xx/3xx 响应上清缓存——失败请求不应该污染缓存
# （也不产生状态变化，所以原样保留缓存即可）。
_MUTATING_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})
_AUTH_SNAPSHOT_REFRESH_EXEMPT_PATH_PREFIXES = ("/auth/",)
_DASHBOARD_SNAPSHOT_MUTATION_REFRESH_COOLDOWN_SECONDS = 5.0
_DASHBOARD_SNAPSHOT_REFRESH_LOCK_ATTR = "_dashboard_snapshot_mutation_refresh_lock"
_DASHBOARD_SNAPSHOT_REFRESH_LAST_ATTR = "_dashboard_snapshot_mutation_refresh_last_at"
_DASHBOARD_SNAPSHOT_MUTATION_EAGER_PANEL_PREFIXES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("/ai/operating-mode/", ("aiConfigModel",)),
    ("/rdp/", ("rdpWorkspace",)),
    ("/strategy-profiles/", ("profileControlSummary",)),
)


_FASTAPI_ROLES: frozenset[str] = frozenset({PROCESS_ROLE_GATEWAY, PROCESS_ROLE_MONOLITH})


def _request_gateway_process_shutdown() -> None:
    """让 uvicorn 优雅退出，由容器 restart policy 接管关键任务故障。"""

    if os.name == "nt":
        # Windows 的 os.kill(SIGTERM) 等价于 TerminateProcess，可能跳过
        # FastAPI lifespan cleanup；raise_signal(SIGTERM) 走已注册的 Uvicorn
        # handler。不要发第二个 SIGINT：Uvicorn 会把它解释为 force-exit。
        signal.raise_signal(signal.SIGTERM)
        return
    os.kill(os.getpid(), signal.SIGTERM)


async def _supervise_gateway_critical_failure(
    *,
    runtime,
    stopping: asyncio.Event,
    logger,
    readiness_lease_task: asyncio.Task[None] | None = None,
    on_readiness_failure: Callable[[], None] | None = None,
) -> None:
    """Gateway 不能只报 503；关键 task 失败后必须让进程真正退出。"""

    wait_for_failure = getattr(
        runtime,
        "wait_for_critical_background_task_failure",
        None,
    )
    if not callable(wait_for_failure):
        return
    failure_task = asyncio.create_task(
        wait_for_failure(),
        name="aats-gateway-critical-failure-wait",
    )
    stopping_task = asyncio.create_task(
        stopping.wait(),
        name="aats-gateway-stopping-wait",
    )
    try:
        done, _pending = await asyncio.wait(
            (failure_task, stopping_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stopping_task in done or stopping.is_set():
            return
        failure = failure_task.result()
        if on_readiness_failure is not None:
            # 不只 lease failure：任一关键业务 task 失败后 Uvicorn lifespan
            # cleanup 都可能卡死，必须先 arm 独立 hard deadline 再发 SIGTERM。
            on_readiness_failure()
        elif str(failure.task_name).startswith("aats-runtime-ready-lease-"):
            if on_readiness_failure is None:
                # 防御性 fallback：生产 lifespan 总会传 callback；独立调用也不能
                # 因监督 wiring 漂移而失去进程级 fencing。
                _hard_exit_process(1)
        logger.error(
            "gateway_critical_task_failed_process_shutdown",
            extra={
                "event": "gateway_critical_task_failed_process_shutdown",
                "process_role": getattr(runtime, "process_role", "gateway"),
                "task_name": failure.task_name,
                "failure_kind": failure.failure_kind,
                "error_type": failure.error_type,
                "stalled_seconds": getattr(failure, "stalled_seconds", None),
                "timeout_seconds": getattr(failure, "timeout_seconds", None),
            },
        )
        _request_gateway_process_shutdown()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        if stopping.is_set():
            return
        if on_readiness_failure is not None:
            on_readiness_failure()
        logger.error(
            "gateway_critical_supervisor_failed_process_shutdown",
            extra={
                "event": "gateway_critical_supervisor_failed_process_shutdown",
                "process_role": getattr(runtime, "process_role", "gateway"),
                "error_type": type(exc).__name__,
            },
        )
        _request_gateway_process_shutdown()
    finally:
        for task in (failure_task, stopping_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(failure_task, stopping_task, return_exceptions=True)


def _resolved_process_role() -> str:
    """Stage 5d：FastAPI gateway 进程默认 role=gateway，但允许 monolith 兼容旧路径。

    通过 AATS_PROCESS_ROLE=monolith 让 api_gateway 同时承担 4 个 slice（开发机
    与单机部署的零依赖路径）。生产 4 进程拓扑下应当置 AATS_PROCESS_ROLE=gateway。

    Fail-fast 校验（LF-017）：未设置环境变量 → 默认 gateway；如果**设置了**值
    但不在合法集合里（如 "gateways" 之类的 typo）就抛 ValueError，避免
    运维静默降级成 gateway 导致的排查困难。此外，合法但不是 gateway/monolith
    的 role（market/decision/execution）明显走错了二进制入口，也抛。
    """
    raw_env = os.environ.get("AATS_PROCESS_ROLE")
    if raw_env is None:
        return PROCESS_ROLE_GATEWAY
    raw = raw_env.strip().lower()
    if not raw:
        return PROCESS_ROLE_GATEWAY
    if raw not in ALLOWED_PROCESS_ROLES:
        raise ValueError(
            f"AATS_PROCESS_ROLE={raw_env!r} is not in allowed set "
            f"{sorted(ALLOWED_PROCESS_ROLES)}. Common typo: did you mean "
            f"'{PROCESS_ROLE_GATEWAY}'?"
        )
    if raw not in _FASTAPI_ROLES:
        raise ValueError(
            f"AATS_PROCESS_ROLE={raw_env!r} is valid but not eligible for the "
            f"api_gateway FastAPI process. Eligible roles: {sorted(_FASTAPI_ROLES)}. "
            f"For role={raw!r}, use apps/{raw}_* entrypoint instead."
        )
    return raw


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    configure_logging_for_settings(settings)
    resolved_role = _resolved_process_role()
    # FS-009：在构造业务 runtime、宣布 readiness 或启动任何后台任务之前，
    # 只读验证 RDP schema。校验失败时本进程不能产生业务副作用。
    from aats.data_platform.db import validate_rdp_schema

    validate_rdp_schema()
    # Readiness barrier (B1) — gateway 用 FastAPI lifespan 而不是 process_lifecycle.run_process，
    # 所以要在这里挂接与 daemon 相同的两阶段 ownership。PROVISIONING 必须在
    # 任何 NATS I/O 前取得；完整 build 后才 CAS 为 READY，全部 peer READY 后
    # 才开放 callback delivery 与 publisher。
    _lifespan_logger = _get_lifecycle_logger("apps.api_gateway.lifespan")
    runtime: ApplicationRuntime | None = None
    readiness_hot_state = None
    readiness_required = False
    readiness_generation: str | None = None
    readiness_lease = None
    readiness_deadline_watchdog: _RuntimeReadyDeadlineWatchdog | None = None
    dashboard_started = False
    readiness_lease_task: asyncio.Task[None] | None = None
    takeover_quarantine_task: asyncio.Task[None] | None = None
    peer_wait_task: asyncio.Task[None] | None = None
    delivery_activation_task: asyncio.Task[None] | None = None
    background_start_task: asyncio.Task[None] | None = None
    runtime_build_task: asyncio.Task[ApplicationRuntime] | None = None
    pre_promotion_bus_check_task: asyncio.Task[None] | None = None
    readiness_lease_stop = asyncio.Event()
    readiness_delivery_gate = NatsDeliveryGate()
    gateway_stopping = asyncio.Event()
    critical_supervisor_task: asyncio.Task[None] | None = None

    def _arm_gateway_readiness_failure_shutdown() -> None:
        # Fencing 在任何日志或 await 前完成；fatal 一旦置位就不能被 finally
        # disarm。若 asyncio/GIL 清理卡死，独立子进程仍会在有界 grace 后强退。
        readiness_lease_stop.set()
        readiness_delivery_gate.abort()
        if readiness_deadline_watchdog is None:
            _hard_exit_process(1)
            return
        if readiness_deadline_watchdog.fatal:
            return
        tightened = readiness_deadline_watchdog.mark_fatal_and_tighten(
            deadline_monotonic=(
                _runtime_ready_clock() + _RUNTIME_READY_FORCE_EXIT_GRACE_SECONDS
            )
        )
        if not tightened:
            readiness_deadline_watchdog.force_exit_now()
        if runtime is not None:
            _cancel_runtime_tasks_for_readiness_failure(
                runtime=runtime,
                lease_task=readiness_lease_task,
                logger=_lifespan_logger,
            )
        _lifespan_logger.critical(
            "runtime_ready_lease_shutdown_watchdog_fatal",
            extra={
                "event": "runtime_ready_lease_shutdown_watchdog_fatal",
                "process_role": resolved_role,
                "grace_seconds": _RUNTIME_READY_FORCE_EXIT_GRACE_SECONDS,
            },
        )

    def _supervise_gateway_readiness_lease_task(
        task: asyncio.Task[None],
        *,
        stop_signal: asyncio.Event,
    ) -> None:
        def _on_done(_task: asyncio.Task[None]) -> None:
            if not stop_signal.is_set():
                _arm_gateway_readiness_failure_shutdown()

        task.add_done_callback(_on_done)

    async def _claim_readiness_before_event_bus(
        hot_state_store,
        final_settings: AATSSettings,
    ) -> None:
        nonlocal readiness_required
        nonlocal readiness_generation
        nonlocal readiness_hot_state
        nonlocal readiness_lease
        nonlocal readiness_lease_task
        nonlocal readiness_deadline_watchdog
        nonlocal takeover_quarantine_task

        readiness_required = _strict_peer_readiness_required(
            role=resolved_role,
            settings=final_settings,
        )
        _validate_runtime_readiness_backend(
            role=resolved_role,
            settings=final_settings,
            required=readiness_required,
        )
        readiness_generation = _runtime_readiness_generation(
            role=resolved_role,
            settings=final_settings,
            required=readiness_required,
        )
        readiness_hot_state = hot_state_store
        if not readiness_required:
            readiness_delivery_gate.activate()
            return
        readiness_lease = await _announce_runtime_ready(
            role=resolved_role,
            hot_state_store=hot_state_store,
            logger=_lifespan_logger,
            generation=readiness_generation,
            required=True,
        )
        if readiness_lease is None:
            raise RuntimeError(
                f"runtime_ready_lease_required:{resolved_role}"
            )
        provisioning_hard_deadline = (
            _runtime_ready_clock() + _RUNTIME_READY_MAX_PROVISIONING_SECONDS
        )
        hard_deadline = _runtime_ready_hard_deadline(
            expires_not_after_monotonic=(
                readiness_lease.expires_not_after_monotonic
            ),
            phase=readiness_lease.phase,
        )
        hard_deadline = min(hard_deadline, provisioning_hard_deadline)
        try:
            readiness_deadline_watchdog = _RuntimeReadyDeadlineWatchdog(
                role=resolved_role,
                deadline_monotonic=hard_deadline,
            )
        except Exception as exc:
            await _withdraw_runtime_ready(
                lease=readiness_lease,
                hot_state_store=hot_state_store,
                logger=_lifespan_logger,
            )
            raise RuntimeError(
                f"runtime_ready_lease_watchdog_start_failed:{resolved_role}"
            ) from exc
        _lifespan_logger.info(
            "runtime_ready_lease_watchdog_armed",
            extra={
                "event": "runtime_ready_lease_watchdog_armed",
                "process_role": resolved_role,
                "generation": readiness_generation,
                "phase": readiness_lease.phase,
            },
        )
        readiness_lease_task = asyncio.create_task(
            _maintain_runtime_ready_lease(
                lease=readiness_lease,
                hot_state_store=hot_state_store,
                logger=_lifespan_logger,
                stop_event=readiness_lease_stop,
                required=True,
                deadline_watchdog=readiness_deadline_watchdog,
                absolute_hard_deadline_monotonic=(
                    provisioning_hard_deadline
                ),
                suppress_failures_when_stopping=False,
            ),
            name=f"aats-runtime-ready-provisioning-lease-{resolved_role}",
        )
        _supervise_gateway_readiness_lease_task(
            readiness_lease_task,
            stop_signal=readiness_lease_stop,
        )
        takeover_quarantine_task = asyncio.create_task(
            _wait_for_runtime_takeover_quarantine(
                role=resolved_role,
                logger=_lifespan_logger,
            ),
            name=(
                f"aats-runtime-ready-takeover-quarantine-{resolved_role}"
            ),
        )
        await _await_task_while_runtime_ready(
            operation_task=takeover_quarantine_task,
            lease_task=readiness_lease_task,
            role=resolved_role,
            on_lease_terminated=_arm_gateway_readiness_failure_shutdown,
            delivery_gate=readiness_delivery_gate,
        )

    try:
        readiness_required = _strict_peer_readiness_required(
            role=resolved_role,
            settings=settings,
        )
        runtime_build_task = asyncio.create_task(
            build_runtime(
                settings,
                process_role=resolved_role,
                before_event_bus_start=_claim_readiness_before_event_bus,
                nats_delivery_gate=(
                    readiness_delivery_gate if readiness_required else None
                ),
            ),
            name=f"aats-runtime-build-{resolved_role}",
        )
        runtime = await _await_task_while_runtime_ready(
            operation_task=runtime_build_task,
            lease_task=None,
            role=resolved_role,
            on_lease_terminated=_arm_gateway_readiness_failure_shutdown,
            delivery_gate=readiness_delivery_gate,
        )
        if readiness_required:
            if (
                readiness_lease is None
                or readiness_hot_state is None
                or readiness_deadline_watchdog is None
            ):
                raise RuntimeError(
                    f"runtime_ready_lease_required:{resolved_role}"
                )
            if readiness_delivery_gate.aborted:
                _arm_gateway_readiness_failure_shutdown()
                raise RuntimeError(
                    f"runtime_ready_delivery_gate_aborted:{resolved_role}"
                )
            pre_promotion_bus_check_task = asyncio.create_task(
                _verify_runtime_event_bus_ready_for_promotion(
                    runtime=runtime,
                    role=resolved_role,
                ),
                name=(
                    "aats-runtime-bus-pre-promotion-check-"
                    f"{resolved_role}"
                ),
            )
            await _await_task_while_runtime_ready(
                operation_task=pre_promotion_bus_check_task,
                lease_task=readiness_lease_task,
                role=resolved_role,
                on_lease_terminated=(
                    _arm_gateway_readiness_failure_shutdown
                ),
                delivery_gate=readiness_delivery_gate,
            )
            await _stop_provisioning_lease_before_promotion(
                lease_task=readiness_lease_task,
                stop_event=readiness_lease_stop,
                role=resolved_role,
                on_unexpected_termination=(
                    _arm_gateway_readiness_failure_shutdown
                ),
            )
            readiness_lease_task = None
            readiness_lease_stop = asyncio.Event()
            readiness_lease = await _promote_runtime_ready(
                lease=readiness_lease,
                hot_state_store=readiness_hot_state,
                watchdog=readiness_deadline_watchdog,
                logger=_lifespan_logger,
            )
            readiness_lease_task = asyncio.create_task(
                _maintain_runtime_ready_lease(
                    lease=readiness_lease,
                    hot_state_store=readiness_hot_state,
                    logger=_lifespan_logger,
                    stop_event=readiness_lease_stop,
                    required=True,
                    deadline_watchdog=readiness_deadline_watchdog,
                ),
                name=f"aats-runtime-ready-lease-{resolved_role}",
            )
            _supervise_gateway_readiness_lease_task(
                readiness_lease_task,
                stop_signal=readiness_lease_stop,
            )
            register_task = getattr(runtime, "register_background_task", None)
            if not callable(register_task):
                raise RuntimeError(
                    f"runtime_ready_lease_supervision_required:{resolved_role}"
                )
            register_task(
                readiness_lease_task,
                name=f"aats-runtime-ready-lease-{resolved_role}",
                critical=True,
                owned_by_runtime=False,
            )
            peer_wait_task = asyncio.create_task(
                _wait_for_peer_roles_ready(
                    role=resolved_role,
                    hot_state_store=readiness_hot_state,
                    logger=_lifespan_logger,
                    generation=readiness_generation,
                    required=True,
                ),
                name=f"aats-runtime-ready-peer-wait-{resolved_role}",
            )
            await _await_task_while_runtime_ready(
                operation_task=peer_wait_task,
                lease_task=readiness_lease_task,
                role=resolved_role,
                on_lease_terminated=_arm_gateway_readiness_failure_shutdown,
                delivery_gate=readiness_delivery_gate,
            )
            delivery_activation_task = asyncio.create_task(
                _activate_runtime_event_delivery(
                    runtime=runtime,
                    delivery_gate=readiness_delivery_gate,
                    role=resolved_role,
                ),
                name=f"aats-runtime-delivery-activation-{resolved_role}",
            )
            await _await_task_while_runtime_ready(
                operation_task=delivery_activation_task,
                lease_task=readiness_lease_task,
                role=resolved_role,
                on_lease_terminated=_arm_gateway_readiness_failure_shutdown,
                delivery_gate=readiness_delivery_gate,
            )
        else:
            if not readiness_delivery_gate.activate():
                raise RuntimeError(
                    f"runtime_ready_delivery_gate_aborted:{resolved_role}"
                )
        background_start_task = asyncio.create_task(
            runtime.start_background_tasks(),
            name=f"aats-runtime-background-start-{resolved_role}",
        )
        await _await_task_while_runtime_ready(
            operation_task=background_start_task,
            lease_task=readiness_lease_task,
            role=resolved_role,
            on_lease_terminated=_arm_gateway_readiness_failure_shutdown,
            delivery_gate=readiness_delivery_gate,
        )
        critical_supervisor_task = asyncio.create_task(
            _supervise_gateway_critical_failure(
                runtime=runtime,
                stopping=gateway_stopping,
                logger=_lifespan_logger,
                readiness_lease_task=readiness_lease_task,
                on_readiness_failure=(
                    _arm_gateway_readiness_failure_shutdown
                    if readiness_deadline_watchdog is not None
                    else None
                ),
            ),
            name="aats-gateway-critical-supervisor",
        )
        app.state.runtime = runtime
        await start_dashboard_snapshot_plane(app, runtime)
        dashboard_started = True
        yield
    except BaseException:
        readiness_delivery_gate.abort()
        if (
            readiness_required
            and readiness_deadline_watchdog is not None
            and not readiness_lease_stop.is_set()
        ):
            _arm_gateway_readiness_failure_shutdown()
        raise
    finally:
        if (
            readiness_required
            and not readiness_lease_stop.is_set()
            and (
                (
                    readiness_lease_task is not None
                    and readiness_lease_task.done()
                )
                or (
                    runtime is None
                    and readiness_lease is not None
                    and readiness_deadline_watchdog is not None
                )
            )
        ):
            _arm_gateway_readiness_failure_shutdown()
        if (
            readiness_deadline_watchdog is not None
            and not readiness_deadline_watchdog.fatal
            and not readiness_deadline_watchdog.disarmed
        ):
            readiness_lease_stop.set()
            if not readiness_deadline_watchdog.begin_shutdown(
                deadline_monotonic=(
                    _runtime_ready_clock() + _RUNTIME_READY_FORCE_EXIT_GRACE_SECONDS
                )
            ):
                readiness_deadline_watchdog.force_exit_now()
        gateway_stopping.set()
        if critical_supervisor_task is not None:
            if not critical_supervisor_task.done():
                critical_supervisor_task.cancel()
            await asyncio.gather(
                critical_supervisor_task,
                return_exceptions=True,
            )
        if dashboard_started:
            await stop_dashboard_snapshot_plane(app)
        if peer_wait_task is not None and not peer_wait_task.done():
            peer_wait_task.cancel()
            await asyncio.gather(peer_wait_task, return_exceptions=True)
        if (
            takeover_quarantine_task is not None
            and not takeover_quarantine_task.done()
        ):
            takeover_quarantine_task.cancel()
            await asyncio.gather(
                takeover_quarantine_task,
                return_exceptions=True,
            )
        if (
            delivery_activation_task is not None
            and not delivery_activation_task.done()
        ):
            delivery_activation_task.cancel()
            await asyncio.gather(
                delivery_activation_task,
                return_exceptions=True,
            )
        if background_start_task is not None and not background_start_task.done():
            background_start_task.cancel()
            await asyncio.gather(background_start_task, return_exceptions=True)
        if runtime_build_task is not None and not runtime_build_task.done():
            runtime_build_task.cancel()
            await asyncio.gather(runtime_build_task, return_exceptions=True)
        if (
            pre_promotion_bus_check_task is not None
            and not pre_promotion_bus_check_task.done()
        ):
            pre_promotion_bus_check_task.cancel()
            await asyncio.gather(
                pre_promotion_bus_check_task,
                return_exceptions=True,
            )
        if runtime is not None:
            lease_released = False

            async def _release_readiness_after_business_stop() -> None:
                nonlocal lease_released
                if (
                    readiness_deadline_watchdog is not None
                    and readiness_deadline_watchdog.fatal
                ):
                    return
                await _stop_runtime_ready_lease_task(
                    lease_task=readiness_lease_task,
                    stop_event=readiness_lease_stop,
                )
                if (
                    readiness_deadline_watchdog is not None
                    and not readiness_deadline_watchdog.disarm()
                ):
                    readiness_deadline_watchdog.force_exit_now()
                    raise RuntimeError(
                        "runtime_ready_lease_watchdog_disarm_failed:"
                        f"{resolved_role}"
                    )
                await _withdraw_runtime_ready(
                    lease=readiness_lease,
                    hot_state_store=readiness_hot_state,
                    logger=_lifespan_logger,
                )
                lease_released = True

            try:
                if isinstance(runtime, ApplicationRuntime):
                    await runtime.stop_background_tasks(
                        before_hot_state_close=(
                            _release_readiness_after_business_stop
                        ),
                    )
                else:
                    await runtime.stop_background_tasks()
                    await _release_readiness_after_business_stop()
            finally:
                if not lease_released:
                    # 业务 stop 失败时只停止续租，保留 TTL fencing；fatal
                    # watchdog 也不会在此被取消。
                    await _stop_runtime_ready_lease_task(
                        lease_task=readiness_lease_task,
                        stop_event=readiness_lease_stop,
                    )
        elif readiness_hot_state is not None:
            # claim 后 build 失败时没有完整 runtime 可证明 bus/worker 已停。
            # watchdog 已存在则保留 TTL ownership 并由 sticky fatal 强退，禁止
            # 提前 delete 让新实例与部分构建的旧实例重叠。只有 pre-I/O 的
            # watchdog 构造失败才允许撤销。
            await _stop_runtime_ready_lease_task(
                lease_task=readiness_lease_task,
                stop_event=readiness_lease_stop,
            )
            readiness_delivery_gate.abort()
            if readiness_deadline_watchdog is None:
                await _withdraw_runtime_ready(
                    lease=readiness_lease,
                    hot_state_store=readiness_hot_state,
                    logger=_lifespan_logger,
                )
            await readiness_hot_state.close()


app = FastAPI(title="AATS API Gateway", lifespan=lifespan)


def _is_successful_mutation(method: str, status_code: int) -> bool:
    return method.upper() in _MUTATING_METHODS and 200 <= int(status_code) < 400


def _dashboard_snapshot_refresh_exempt_path(path: str) -> bool:
    normalized_path = "/" + str(path or "").lstrip("/")
    return any(normalized_path.startswith(prefix) for prefix in _AUTH_SNAPSHOT_REFRESH_EXEMPT_PATH_PREFIXES)


def _should_refresh_dashboard_snapshots_after_mutation(method: str, path: str, status_code: int) -> bool:
    if not _is_successful_mutation(method, status_code):
        return False
    return not _dashboard_snapshot_refresh_exempt_path(path)


def _eager_dashboard_snapshot_panels_for_mutation(path: str) -> tuple[str, ...]:
    normalized_path = "/" + str(path or "").lstrip("/")
    panels: list[str] = []
    for prefix, panel_keys in _DASHBOARD_SNAPSHOT_MUTATION_EAGER_PANEL_PREFIXES:
        if normalized_path.startswith(prefix):
            panels.extend(panel_keys)
    return tuple(dict.fromkeys(panels))


def _dashboard_snapshot_refresh_lock(app_state: object) -> asyncio.Lock:
    lock = getattr(app_state, _DASHBOARD_SNAPSHOT_REFRESH_LOCK_ATTR, None)
    if isinstance(lock, asyncio.Lock):
        return lock
    lock = asyncio.Lock()
    setattr(app_state, _DASHBOARD_SNAPSHOT_REFRESH_LOCK_ATTR, lock)
    return lock


async def _refresh_dashboard_snapshots_after_mutation(request: Request, *, reason: str) -> bool:
    plane = getattr(request.app.state, "dashboard_snapshot_plane", None)
    invalidate = getattr(plane, "invalidate_all", None)
    enqueue_scheduled = getattr(plane, "enqueue_scheduled", None)
    if not callable(invalidate) or not callable(enqueue_scheduled):
        return False

    await invalidate(reason=reason)

    lock = _dashboard_snapshot_refresh_lock(request.app.state)
    now = monotonic()
    async with lock:
        last_refresh_at = getattr(request.app.state, _DASHBOARD_SNAPSHOT_REFRESH_LAST_ATTR, None)
        if (
            isinstance(last_refresh_at, float)
            and now - last_refresh_at < _DASHBOARD_SNAPSHOT_MUTATION_REFRESH_COOLDOWN_SECONDS
        ):
            return False
        setattr(request.app.state, _DASHBOARD_SNAPSHOT_REFRESH_LAST_ATTR, now)

    await enqueue_scheduled(reason=reason)
    enqueue_panels = getattr(plane, "enqueue_panels", None)
    if callable(enqueue_panels):
        await enqueue_panels(
            _eager_dashboard_snapshot_panels_for_mutation(request.url.path),
            reason=reason,
        )
    return True


@app.middleware("http")
async def _invalidate_bundle_cache_on_mutation(request: Request, call_next):
    response = await call_next(request)
    if _is_successful_mutation(request.method, response.status_code):
        invalidate_bundle_cache()
        if _should_refresh_dashboard_snapshots_after_mutation(
            request.method,
            request.url.path,
            response.status_code,
        ):
            await _refresh_dashboard_snapshots_after_mutation(
                request,
                reason=f"{request.method.lower()}_mutation",
            )
    return response


# Stage 8：把每一条 HTTP 请求作为 trace 的 root span。所有在 handler 内部
# 发起的 NatsEventBus.publish 都会自动挂在这条 span 下面（通过 OTel 的
# current context）。span name 用 "gateway.http.<METHOD> <url_path>"，
# Jaeger UI 默认按 service + span name 聚合统计 P50/P99。
# 不用 opentelemetry-instrumentation-fastapi 是为了：
#   1) 避免再引一个 optional extra
#   2) 保持 span 命名与 docs/task/stage_8_otel_integration_design.md §D5
#      的 "process_role.module.action" 规范完全对齐
#   3) /healthz / /metrics / /favicon.ico 这类 noise 路径可以在这里显式过滤
# 设计文档：docs/task/stage_8_otel_integration_design.md §D5
_TELEMETRY_IGNORED_PATHS = frozenset({
    "/healthz",
    "/metrics",
    "/favicon.ico",
})


@app.middleware("http")
async def _gateway_trace_root_span(request: Request, call_next):
    path = request.url.path
    if path in _TELEMETRY_IGNORED_PATHS:
        return await call_next(request)
    with start_span(
        f"gateway.http.{request.method} {path}",
        attributes={
            "http.method": request.method,
            "http.route": path,
            "http.scheme": request.url.scheme,
            "http.host": request.url.hostname or "",
            "aats.process_role": _resolved_process_role(),
        },
    ) as span:
        response = await call_next(request)
        try:
            span.set_attribute("http.status_code", response.status_code)
        except Exception:
            pass
        return response


# Stage 7 修复：lightweight liveness probe 给 docker compose healthcheck 专用。
# 与 /system/health 的区别：
#   * /system/health 是 operator/UI 用的诊断 endpoint，需要全量 portfolio /
#     reconciliation / market 状态，依赖 runtime 上多个 slice service。
#     在 gateway-only role 下 runtime.market_gateway / runtime.execution_adapter
#     都是 None（被 _SLICE_REQUIRED_ROLES 门控），调用会 NPE。
#   * /healthz 不依赖 slice 业务查询，但会读取 runtime 的关键 task 监督快照；
#     任一长期关键 task 非预期结束必须 503，不能让容器继续伪装 healthy。
# 直接挂在 `app` 上而不是挂到 routes.py 的 router 上，是为了绕过 router 级
# require_read_access dependency —— docker healthcheck curl 不带 Bearer token。
@app.get("/healthz")
async def healthz() -> dict[str, str]:
    runtime = getattr(app.state, "runtime", None)
    inspect_failure = getattr(runtime, "critical_background_task_failure", None)
    critical_failure = inspect_failure() if callable(inspect_failure) else None
    if critical_failure is not None:
        detail = {
            "status": "unhealthy",
            "reason": "critical_background_task_failed",
            "task_name": critical_failure.task_name,
            "failure_kind": critical_failure.failure_kind,
            "error_type": critical_failure.error_type,
        }
        stalled_seconds = getattr(critical_failure, "stalled_seconds", None)
        timeout_seconds = getattr(critical_failure, "timeout_seconds", None)
        if stalled_seconds is not None:
            detail["stalled_seconds"] = stalled_seconds
        if timeout_seconds is not None:
            detail["timeout_seconds"] = timeout_seconds
        raise HTTPException(
            status_code=503,
            detail=detail,
        )
    return {"status": "ok", "process_role": _resolved_process_role()}


# A-0.3：governance DB 写路径的统一错误→HTTP 映射。
# DBUnavailableError 是基础设施级不可达（e.g. 数据库宕机 / 连接耗尽），
# HTTP 契约上等价于 503 Service Unavailable，客户端应当重试；
# DBConstraintViolation 代表 FK / UQ / CHECK 被触发，通常是上游脏数据或业务
# 规则违反，映射成 422 Unprocessable Entity 让 operator / UI 看到原因。
# 这两条 handler 把写路径的异常收口到明确的 HTTP 语义，避免 FastAPI 默认
# 500 Internal Server Error 把基础设施故障与业务逻辑 bug 混成一锅。
@app.exception_handler(DBUnavailableError)
async def _handle_db_unavailable(_request: Request, exc: DBUnavailableError) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "ok": False,
            "error": "db_unavailable",
            "message": str(exc),
        },
    )


@app.exception_handler(DBConstraintViolation)
async def _handle_db_constraint_violation(
    _request: Request, exc: DBConstraintViolation,
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "ok": False,
            "error": "db_constraint_violation",
            "message": str(exc),
        },
    )


app.include_router(auth_router)
app.include_router(ui_router)
app.include_router(router)
app.include_router(rdp_router)
app.include_router(rdp_profile_router)

# Phase 3H / FS-020：最后注册，使其成为最外层 user middleware。当前 Gateway
# 仅允许本机 Host；future 远程域名必须与 proxy/TLS/网络边界一起独立设计。
app.add_middleware(
    GatewayBrowserSecurityMiddleware,
    allowed_hosts=DEFAULT_GATEWAY_ALLOWED_HOSTS,
)
