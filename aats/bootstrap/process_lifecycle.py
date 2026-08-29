"""Stage 5d：4 进程拓扑下的统一进程生命周期 helper。

设计动机：
* gateway / market / decision / execution 4 个 entry script 的启动序列除了
  process_role 不同之外几乎完全一致：load_settings → configure_logging →
  build_runtime(process_role=...) → start_background_tasks → 等 SIGTERM →
  stop_background_tasks。
* 把这段 boilerplate 抽出来集中维护，避免 4 个 main.py 里重复且容易漂移
  （某次只改了 3 个 entry 忘了第 4 个的故事会反复发生）。
* 同时也是 5e smoke 测试的注入点：smoke 测试通过手动触发同一个 stop event
  就能干净地把进程关掉、检查退出码与日志。
* FS-006：启动完成后同时监督 OS stop 与显式注册的关键长期 task。关键 task
  非预期结束时停止健康心跳、执行清理并返回非零，不能继续伪装 healthy。

跨平台说明：
* Linux 上用 loop.add_signal_handler 注册 SIGTERM/SIGINT 是 asyncio 推荐路径。
* Windows 上 add_signal_handler 不可用（NotImplementedError），降级用
  signal.signal()。Windows 不是生产平台，但本地开发与单元测试会跑到，
  所以必须 fail-soft。

Readiness barrier (B1, protocol v2)：
* build_runtime 在任何 NATS I/O 前取得 global-role、instance-fenced Redis
  PROVISIONING ownership；generation 只在 payload 中隔离 peer barrier。
* durable consumers 装配完成后 CAS 为 READY，持续续租并等待同代次 peer READY；
  构建期发布先有界缓存，随后先 flush，再开放 callbacks/background publishers。
* 目的：让 market 等 publisher 只有在 decision/execution/gateway 的 durable
  consumer 创建完成后才开始 publish。这是 nats_retention_global_architecture_sow.md
  §B1 对 INTEREST retention 切换的硬前置——INTEREST 下 publish 发生在
  consumer 就位前就会消息丢失。
* 四主进程使用 NATS/hybrid 时，announce/poll/timeout 必须失败关闭。当前一般
  events stream 已是 INTEREST，不能再沿用早期 LIMITS fallback。
* lease 绑定标准部署生成的 generation，旧部署残留不能满足新部署 barrier；
  owner-aware 原子 refresh/delete 防止旧实例覆盖或删除新实例。
"""
from __future__ import annotations

import asyncio
import math
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

from aats.bootstrap.config import ApplicationRuntime, build_runtime, load_settings
from aats.bootstrap.logging import configure_logging_for_settings, get_logger
from aats.bootstrap.settings import ALLOWED_PROCESS_ROLES, AATSSettings
from aats.bus.nats_bus import NatsDeliveryGate
from aats.storage.hot_state_store import HotStateStore


# 跨进程 entry 共享的 logger 命名空间。每个 entry 自己再 get 一个细分 logger。
_LIFECYCLE_LOGGER = "aats.bootstrap.process_lifecycle"


# ────────────────────────────────────────────────────────────────
# Readiness barrier (B1)
# ────────────────────────────────────────────────────────────────

# Redis key 前缀：每个 role 在任何 NATS I/O 前独占一个全局 ownership key。
# generation 只进入 payload；其他进程只接受同代次且 phase=READY 的 peer。
_RUNTIME_READY_KEY_PREFIX = "aats:runtime:owner:"

# Ready lease TTL 与续租周期：它是 global-role consumer provisioning
# ownership，不再是只写一次、5 分钟后自然消失的启动 key。TTL 必须至少是续租周期
# 的 3 倍；进程崩溃后最多保留一个 TTL，正常退出由 owner-aware delete 立即释放。
_RUNTIME_READY_TTL_SECONDS: float = 60.0
_RUNTIME_READY_RENEW_INTERVAL_SECONDS: float = 10.0
_RUNTIME_READY_SHUTDOWN_MARGIN_SECONDS: float = 30.0
_RUNTIME_READY_FORCE_EXIT_GRACE_SECONDS: float = 10.0
_RUNTIME_READY_PROVISIONING_EXIT_GUARD_SECONDS: float = 10.0
# 从 global-role claim 到 READY promotion 的绝对上界。它覆盖 55s takeover
# quarantine 与 runtime assembly，并给标准部署 210s health budget 留出诊断/重启
# 空间；续租只能延长 Redis TTL，绝不能延长这个进程级启动 fence。
_RUNTIME_READY_MAX_PROVISIONING_SECONDS: float = 180.0
# Redis key 可能因重启、人工误删或旧版 allkeys-lru 淘汰而提前消失。新 owner
# 在任何 NATS I/O 前必须静默隔离，至少覆盖旧 protocol-v2 进程从最后一次成功
# 写 lease 到进程级 hard fence 的最大存活窗（PROVISIONING=TTL-guard=50s）。
# 额外 5s 吸收调度/时钟粒度；v1->v2 首发仍必须 full-down，不能靠本隔离兼容旧二进制。
_RUNTIME_READY_TAKEOVER_QUARANTINE_SECONDS: float = 55.0
_RUNTIME_READY_LEASE_PROTOCOL = 2
_RUNTIME_READY_PHASE_PROVISIONING = "PROVISIONING"
_RUNTIME_READY_PHASE_READY = "READY"

# 轮询 peer ready 的间隔。50ms~1s 之间折中：太密 Redis QPS 浪费，太稀 startup
# 延迟感知慢。500ms 对 startup order 影响 < 1s，可接受。
_PEER_READY_POLL_INTERVAL_SECONDS: float = 0.5

# Peer ready 等待超时。四主进程 NATS/hybrid 路径超时后失败关闭；60s 窗口覆盖
# build_runtime 的 slice builder + cache hydrate 全流程（历史实测 10-30s）。
_PEER_READY_TIMEOUT_SECONDS: float = 60.0


# Peer 依赖映射：每个 role 在 start_background_tasks 前必须等哪些 peer role
# 的 subscribe 就位。
#
# 当前生产所有主 role 互相订阅（market 也订阅 execution 的 account_snapshots、
# kill_switch_state 等；详见运行时 nats consumer 清单），所以保守策略：
# 每个 role 等其他所有主 role 就位。monolith（单进程模式）无 peer。
#
# gateway daemon 依赖（rdp-daemon / liquidations-daemon / microstructure-collector）
# 不走 build_runtime 主流程，不参与此 barrier。
_PEER_READINESS_MAP: dict[str, tuple[str, ...]] = {
    "market": ("decision", "execution", "gateway"),
    "decision": ("market", "execution", "gateway"),
    "execution": ("market", "decision", "gateway"),
    "gateway": ("market", "decision", "execution"),
    "monolith": (),
}


_MAIN_PROCESS_ROLES = frozenset({"market", "decision", "execution", "gateway"})


def _runtime_ready_clock_ns() -> int:
    """Lease 时钟必须计入宿主 suspend；Redis TTL 不会因 VM 睡眠暂停。"""

    if os.name == "nt":
        import ctypes
        get_tick_count_64 = ctypes.windll.kernel32.GetTickCount64
        get_tick_count_64.argtypes = ()
        get_tick_count_64.restype = ctypes.c_ulonglong
        return int(get_tick_count_64()) * 1_000_000
    clock_boottime = getattr(time, "CLOCK_BOOTTIME", None)
    if clock_boottime is None:
        raise RuntimeError("CLOCK_BOOTTIME is required for runtime readiness")
    return time.clock_gettime_ns(clock_boottime)


def _runtime_ready_clock() -> float:
    return _runtime_ready_clock_ns() / 1_000_000_000


@dataclass(frozen=True, slots=True)
class _RuntimeReadyLease:
    role: str
    generation: str | None
    instance_id: str
    announced_ts: str
    pid: int
    phase: str
    # 仅用于本进程保守 fencing，不进入 Redis payload。以 claim 请求发出前
    # 的 monotonic 时刻计算，绝不能用响应返回时刻高估 Redis 实际 PTTL。
    expires_not_after_monotonic: float

    @property
    def key(self) -> str:
        return _ready_key(self.role, generation=self.generation)

    @property
    def payload(self) -> dict[str, Any]:
        # 每次返回全新 dict；lease 本身只保存 immutable scalar，避免 InMemory
        # backend 因共享 dict 引用而与 Redis byte-CAS 产生不同 fencing 语义。
        return {
            "lease_protocol": _RUNTIME_READY_LEASE_PROTOCOL,
            "process_role": self.role,
            "generation": self.generation,
            "instance_id": self.instance_id,
            "announced_ts": self.announced_ts,
            "pid": self.pid,
            "phase": self.phase,
        }


def _ready_key(role: str, *, generation: str | None = None) -> str:
    # generation 只属于 payload/peer barrier，绝不能分区 ownership key；否则
    # 新旧部署可各自 NX 成功并同时执行同一 role。保留参数仅为调用兼容。
    del generation
    return f"{_RUNTIME_READY_KEY_PREFIX}{role}"


def _strict_peer_readiness_required(*, role: str, settings: AATSSettings) -> bool:
    backend = str(getattr(settings, "event_bus_backend", "in_memory") or "in_memory")
    return role in _MAIN_PROCESS_ROLES and backend in {"hybrid", "nats"}


def _runtime_readiness_generation(
    *,
    role: str,
    settings: AATSSettings,
    required: bool,
) -> str | None:
    raw_generation = getattr(settings, "runtime_readiness_generation", None)
    generation = str(raw_generation or "").strip() or None
    if required and generation is None:
        raise RuntimeError(f"runtime_ready_gate_generation_required:{role}")
    return generation


def _validate_runtime_readiness_backend(
    *,
    role: str,
    settings: AATSSettings,
    required: bool,
) -> None:
    """严格跨进程 ownership 只能建立在共享 Redis truth 上。"""

    backend = str(getattr(settings, "hot_state_backend", "memory") or "memory")
    if required and backend != "redis":
        raise RuntimeError(f"runtime_ready_gate_redis_required:{role}")


async def _announce_runtime_ready(
    *,
    role: str,
    hot_state_store: HotStateStore | None,
    logger,
    generation: str | None = None,
    required: bool = False,
) -> _RuntimeReadyLease | None:
    """在任何 NATS I/O 前独占本 role 的 PROVISIONING ownership。

    peer 只接受后续原子 promotion 得到的 READY payload。hot_state_store 为
    None 时只有 optional/InMemory 场景可 no-op；strict NATS split 路径失败。
    """
    if required and generation is None:
        raise RuntimeError(f"runtime_ready_gate_generation_required:{role}")
    if hot_state_store is None:
        if required:
            raise RuntimeError(f"runtime_ready_gate_hot_state_required:{role}")
        logger.info(
            "runtime_ready_gate_skipped_no_hot_state",
            extra={
                "event": "runtime_ready_gate_skipped_no_hot_state",
                "process_role": role,
            },
        )
        return None
    claim_started_monotonic = _runtime_ready_clock()
    lease = _RuntimeReadyLease(
        role=role,
        generation=generation,
        instance_id=uuid.uuid4().hex,
        announced_ts=datetime.now(timezone.utc).isoformat(),
        pid=os.getpid(),
        phase=_RUNTIME_READY_PHASE_PROVISIONING,
        expires_not_after_monotonic=(
            claim_started_monotonic + _RUNTIME_READY_TTL_SECONDS
        ),
    )
    try:
        acquired = await hot_state_store.set_if_absent(
            lease.key,
            lease.payload,
            ttl_seconds=_RUNTIME_READY_TTL_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "runtime_ready_gate_announce_failed",
            extra={
                "event": "runtime_ready_gate_announce_failed",
                "process_role": role,
                "error_type": type(exc).__name__,
            },
        )
        if required:
            raise RuntimeError(
                f"runtime_ready_gate_announce_failed:{role}"
            ) from None
        return None
    if not acquired:
        logger.warning(
            "runtime_ready_gate_instance_conflict",
            extra={
                "event": "runtime_ready_gate_instance_conflict",
                "process_role": role,
                "generation": generation,
            },
        )
        if required:
            raise RuntimeError(f"runtime_ready_gate_instance_conflict:{role}")
        return None
    logger.info(
        "runtime_ready_gate_announced",
        extra={
            "event": "runtime_ready_gate_announced",
            "process_role": role,
            "generation": generation,
        },
    )
    return lease


async def _promote_runtime_ready(
    *,
    lease: _RuntimeReadyLease,
    hot_state_store: HotStateStore,
    watchdog: _RuntimeReadyDeadlineWatchdog,
    logger,
    ttl_seconds: float = _RUNTIME_READY_TTL_SECONDS,
) -> _RuntimeReadyLease:
    """原子把本 owner 从 PROVISIONING 转为 peer 可接受的 READY。"""

    if lease.phase != _RUNTIME_READY_PHASE_PROVISIONING:
        raise RuntimeError(f"runtime_ready_lease_invalid_phase:{lease.role}")
    transition_started_monotonic = _runtime_ready_clock()
    ready_lease = replace(
        lease,
        phase=_RUNTIME_READY_PHASE_READY,
        announced_ts=datetime.now(timezone.utc).isoformat(),
        expires_not_after_monotonic=(
            transition_started_monotonic + ttl_seconds
        ),
    )
    try:
        promoted = await hot_state_store.compare_replace(
            lease.key,
            lease.payload,
            ready_lease.payload,
            ttl_seconds=ttl_seconds,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "runtime_ready_lease_promotion_failed",
            extra={
                "event": "runtime_ready_lease_promotion_failed",
                "process_role": lease.role,
                "generation": lease.generation,
                "error_type": type(exc).__name__,
            },
        )
        raise RuntimeError(
            f"runtime_ready_lease_promotion_failed:{lease.role}"
        ) from None
    if not promoted:
        # CAS=False 已证明本实例不再拥有 key；不能做日志、清理或 10 秒 grace。
        watchdog.force_exit_now()
        raise RuntimeError(f"runtime_ready_lease_lost:{lease.role}")
    hard_deadline = _runtime_ready_hard_deadline(
        expires_not_after_monotonic=ready_lease.expires_not_after_monotonic,
        phase=ready_lease.phase,
    )
    if not watchdog.rearm_success(deadline_monotonic=hard_deadline):
        watchdog.force_exit_now()
        raise RuntimeError(f"runtime_ready_lease_watchdog_rearm_failed:{lease.role}")
    logger.info(
        "runtime_ready_lease_promoted",
        extra={
            "event": "runtime_ready_lease_promoted",
            "process_role": lease.role,
            "generation": lease.generation,
        },
    )
    return ready_lease


async def _maintain_runtime_ready_lease(
    *,
    lease: _RuntimeReadyLease,
    hot_state_store: HotStateStore,
    logger,
    stop_event: asyncio.Event,
    ttl_seconds: float = _RUNTIME_READY_TTL_SECONDS,
    renew_interval: float = _RUNTIME_READY_RENEW_INTERVAL_SECONDS,
    shutdown_margin: float | None = None,
    required: bool = False,
    deadline_watchdog: _RuntimeReadyDeadlineWatchdog | None = None,
    absolute_hard_deadline_monotonic: float | None = None,
    suppress_failures_when_stopping: bool = True,
) -> None:
    """续租本实例 readiness；ownership 丢失时绝不普通 set 抢回。"""

    effective_shutdown_margin = (
        min(_RUNTIME_READY_SHUTDOWN_MARGIN_SECONDS, ttl_seconds / 2.0)
        if shutdown_margin is None
        else float(shutdown_margin)
    )
    if (
        not math.isfinite(ttl_seconds)
        or not math.isfinite(renew_interval)
        or not math.isfinite(effective_shutdown_margin)
        or renew_interval <= 0.0
        or effective_shutdown_margin <= 0.0
        or effective_shutdown_margin >= ttl_seconds - renew_interval
        or (
            ttl_seconds < renew_interval * 3
            and not math.isclose(
                ttl_seconds,
                renew_interval * 3,
                rel_tol=1e-12,
                abs_tol=0.0,
            )
        )
    ):
        raise ValueError(
            "runtime readiness TTL must be at least 3x renew interval and "
            "leave a positive shutdown margin before expiry"
        )
    if absolute_hard_deadline_monotonic is not None and (
        not math.isfinite(absolute_hard_deadline_monotonic)
        or absolute_hard_deadline_monotonic
        <= _runtime_ready_clock() + _RUNTIME_READY_FORCE_EXIT_GRACE_SECONDS
    ):
        raise ValueError(
            "runtime readiness absolute hard deadline must leave the force-exit grace"
        )
    logger.info(
        "runtime_ready_lease_started",
        extra={
            "event": "runtime_ready_lease_started",
            "process_role": lease.role,
            "generation": lease.generation,
            "ttl_seconds": ttl_seconds,
            "renew_interval_seconds": renew_interval,
            "shutdown_margin_seconds": effective_shutdown_margin,
            "absolute_hard_deadline_monotonic": (
                absolute_hard_deadline_monotonic
            ),
        },
    )
    # 测试可注入更短 TTL；生产默认下 min 会保留 claim 请求发出前记录的
    # 更保守时刻，绝不会因 lease task 较晚启动而延长初始 ownership。
    lease_expires_not_after = min(
        lease.expires_not_after_monotonic,
        _runtime_ready_clock() + ttl_seconds,
    )
    next_delay = renew_interval
    consecutive_failures = 0
    provisioning_shutdown_deadline = (
        absolute_hard_deadline_monotonic
        - _RUNTIME_READY_FORCE_EXIT_GRACE_SECONDS
        if absolute_hard_deadline_monotonic is not None
        else None
    )
    while not stop_event.is_set():
        safety_deadline = lease_expires_not_after - effective_shutdown_margin
        if provisioning_shutdown_deadline is not None:
            safety_deadline = min(
                safety_deadline,
                provisioning_shutdown_deadline,
            )
        remaining_before_shutdown = safety_deadline - _runtime_ready_clock()
        if remaining_before_shutdown <= 0.0:
            provisioning_timeout = (
                provisioning_shutdown_deadline is not None
                and provisioning_shutdown_deadline <= safety_deadline
            )
            logger.warning(
                (
                    "runtime_ready_provisioning_timeout"
                    if provisioning_timeout
                    else "runtime_ready_lease_safety_window_exhausted"
                ),
                extra={
                    "event": (
                        "runtime_ready_provisioning_timeout"
                        if provisioning_timeout
                        else "runtime_ready_lease_safety_window_exhausted"
                    ),
                    "process_role": lease.role,
                    "generation": lease.generation,
                    "consecutive_failures": consecutive_failures,
                },
            )
            if required:
                raise RuntimeError(
                    (
                        f"runtime_ready_provisioning_timeout:{lease.role}"
                        if provisioning_timeout
                        else f"runtime_ready_lease_refresh_failed:{lease.role}"
                    )
                ) from None
            return
        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=min(next_delay, remaining_before_shutdown),
            )
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            return
        try:
            # Redis client 自身通常有 socket timeout，但 readiness fencing 不能
            # 依赖后端配置碰巧小于 lease TTL。用本地 lease deadline 约束单次
            # refresh；后端永久挂起时也必须在旧 ownership 最迟过期时失败关闭。
            refresh_started_monotonic = _runtime_ready_clock()
            remaining_before_refresh = (
                lease_expires_not_after
                - effective_shutdown_margin
                - refresh_started_monotonic
            )
            if provisioning_shutdown_deadline is not None:
                remaining_before_refresh = min(
                    remaining_before_refresh,
                    provisioning_shutdown_deadline
                    - refresh_started_monotonic,
                )
            if remaining_before_refresh <= 0.0:
                raise TimeoutError("runtime readiness lease deadline elapsed")
            refreshed = await asyncio.wait_for(
                hot_state_store.compare_refresh(
                    lease.key,
                    lease.payload,
                    ttl_seconds=ttl_seconds,
                ),
                timeout=remaining_before_refresh,
            )
        except Exception as exc:  # noqa: BLE001
            if stop_event.is_set():
                # 正常 shutdown 已冻结续租并收紧独立 watchdog；不要把正在途中的
                # refresh 异常升级成 immediate hard exit。PROVISIONING->READY
                # transition 则必须传播已经在途的异常，防止同 tick 竞态把失租
                # 吞成一次“正常停止 maintainer”。
                if suppress_failures_when_stopping:
                    return
                if required:
                    raise RuntimeError(
                        f"runtime_ready_lease_refresh_failed:{lease.role}"
                    ) from None
                return
            consecutive_failures += 1
            now = _runtime_ready_clock()
            safety_deadline = (
                lease_expires_not_after - effective_shutdown_margin
            )
            if provisioning_shutdown_deadline is not None:
                safety_deadline = min(
                    safety_deadline,
                    provisioning_shutdown_deadline,
                )
            if consecutive_failures == 1 or now >= safety_deadline:
                logger.warning(
                    "runtime_ready_lease_refresh_failed",
                    extra={
                        "event": "runtime_ready_lease_refresh_failed",
                        "process_role": lease.role,
                        "generation": lease.generation,
                        "error_type": type(exc).__name__,
                        "consecutive_failures": consecutive_failures,
                    },
                )
            if now >= safety_deadline:
                if required:
                    provisioning_timeout = (
                        provisioning_shutdown_deadline is not None
                        and provisioning_shutdown_deadline
                        <= safety_deadline
                    )
                    raise RuntimeError(
                        (
                            f"runtime_ready_provisioning_timeout:{lease.role}"
                            if provisioning_timeout
                            else f"runtime_ready_lease_refresh_failed:{lease.role}"
                        )
                    ) from None
                return
            remaining = safety_deadline - now
            next_delay = min(1.0, renew_interval, remaining / 2)
            continue
        if not refreshed:
            if deadline_watchdog is not None:
                # CAS=False 是确定性 ownership loss；先零宽限进程 fencing，
                # 再允许测试替身返回后走诊断日志/异常。
                deadline_watchdog.force_exit_now()
            logger.warning(
                "runtime_ready_lease_lost",
                extra={
                    "event": "runtime_ready_lease_lost",
                    "process_role": lease.role,
                    "generation": lease.generation,
                },
            )
            if required:
                raise RuntimeError(f"runtime_ready_lease_lost:{lease.role}")
            return
        if stop_event.is_set():
            # begin_shutdown 之后 facade 拒绝 REARM；in-flight refresh 即使成功
            # 也只能有序返回，不能把正常停机误判成 watchdog rearm failure。
            return
        consecutive_failures = 0
        # Redis 在执行 PEXPIRE 时开始倒计时；以请求发出前时刻 + TTL
        # 作为保守 not-after，响应延迟只能缩短本地窗口，不能放大 ownership。
        lease_expires_not_after = refresh_started_monotonic + ttl_seconds
        if deadline_watchdog is not None:
            hard_deadline = _runtime_ready_hard_deadline(
                expires_not_after_monotonic=lease_expires_not_after,
                phase=lease.phase,
                shutdown_margin=effective_shutdown_margin,
            )
            if absolute_hard_deadline_monotonic is not None:
                hard_deadline = min(
                    hard_deadline,
                    absolute_hard_deadline_monotonic,
                )
            if not deadline_watchdog.rearm_success(
                deadline_monotonic=hard_deadline
            ):
                if (
                    stop_event.is_set()
                    and deadline_watchdog.shutdown
                    and not deadline_watchdog.fatal
                ):
                    return
                deadline_watchdog.force_exit_now()
                raise RuntimeError(
                    f"runtime_ready_lease_watchdog_rearm_failed:{lease.role}"
                ) from None
        next_delay = renew_interval


async def _wait_for_peer_roles_ready(
    *,
    role: str,
    hot_state_store: HotStateStore | None,
    logger,
    peers: Sequence[str] | None = None,
    timeout_seconds: float = _PEER_READY_TIMEOUT_SECONDS,
    poll_interval: float = _PEER_READY_POLL_INTERVAL_SECONDS,
    generation: str | None = None,
    required: bool = False,
) -> None:
    """阻塞等 peer role 都写完自己的 ready key。

    strict NATS split 路径的 Redis 异常或超时会抛固定 RuntimeError，确保 publisher
    不启动；optional/InMemory 路径保留兼容返回。

    ``peers`` 默认读 ``_PEER_READINESS_MAP[role]``；测试可手动注入。
    ``hot_state_store`` 为 None 时直接返回（InMemory 场景 / 单进程 smoke）。
    """
    if required and generation is None:
        raise RuntimeError(f"runtime_ready_gate_generation_required:{role}")
    if hot_state_store is None:
        if required:
            raise RuntimeError(f"runtime_ready_gate_hot_state_required:{role}")
        return
    peer_list = list(peers if peers is not None else _PEER_READINESS_MAP.get(role, ()))
    if not peer_list:
        return
    logger.info(
        "runtime_ready_gate_wait_start",
        extra={
            "event": "runtime_ready_gate_wait_start",
            "process_role": role,
            "peers": peer_list,
            "timeout_seconds": timeout_seconds,
            "generation": generation,
        },
    )
    if not math.isfinite(timeout_seconds) or timeout_seconds < 0.0:
        raise ValueError("peer readiness timeout must be finite and non-negative")
    deadline = _runtime_ready_clock() + timeout_seconds
    first_poll = True
    while True:
        peer_keys = {
            peer: _ready_key(peer, generation=generation)
            for peer in peer_list
        }
        remaining_timeout = deadline - _runtime_ready_clock()
        timed_out = remaining_timeout <= 0.0
        ready_values: dict[str, Any] = {}
        if first_poll and timed_out:
            # ``timeout_seconds=0`` 仍保留一次非阻塞 snapshot 读取：调用方可用它
            # 做“只检查当前事实、不等待”的严格探针。后续轮次不得越过整体 deadline。
            try:
                ready_values = await asyncio.wait_for(
                    hot_state_store.get_many(list(peer_keys.values())),
                    timeout=0.001,
                )
            except asyncio.TimeoutError:
                pass
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "runtime_ready_gate_poll_failed",
                    extra={
                        "event": "runtime_ready_gate_poll_failed",
                        "process_role": role,
                        "error_type": type(exc).__name__,
                        "generation": generation,
                    },
                )
                if required:
                    raise RuntimeError(
                        f"runtime_ready_gate_poll_failed:{role}"
                    ) from None
                return
        elif not timed_out:
            try:
                ready_values = await asyncio.wait_for(
                    hot_state_store.get_many(list(peer_keys.values())),
                    timeout=remaining_timeout,
                )
            except asyncio.TimeoutError:
                # 单次 Redis read 也受整体 peer deadline 约束；否则后端永久
                # 挂起时 lease 仍可在另一连接续租，进程会永远卡在启动阶段。
                timed_out = True
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "runtime_ready_gate_poll_failed",
                    extra={
                        "event": "runtime_ready_gate_poll_failed",
                        "process_role": role,
                        "error_type": type(exc).__name__,
                        "generation": generation,
                    },
                )
                if required:
                    raise RuntimeError(
                        f"runtime_ready_gate_poll_failed:{role}"
                    ) from None
                return
        first_poll = False
        missing: list[str] = []
        for peer, peer_key in peer_keys.items():
            payload = ready_values.get(peer_key)
            if not isinstance(payload, dict):
                missing.append(peer)
                continue
            protocol = payload.get("lease_protocol")
            if (
                type(protocol) is not int
                or protocol != _RUNTIME_READY_LEASE_PROTOCOL
            ):
                missing.append(peer)
                continue
            if payload.get("process_role") != peer:
                missing.append(peer)
                continue
            if payload.get("phase") != _RUNTIME_READY_PHASE_READY:
                missing.append(peer)
                continue
            if generation is not None and payload.get("generation") != generation:
                missing.append(peer)
                continue
            instance_id = payload.get("instance_id")
            if (
                not isinstance(instance_id, str)
                or len(instance_id) != 32
                or any(char not in "0123456789abcdef" for char in instance_id)
            ):
                missing.append(peer)
                continue
            announced_ts = payload.get("announced_ts")
            pid = payload.get("pid")
            if (
                not isinstance(announced_ts, str)
                or not announced_ts.strip()
                or type(pid) is not int
                or pid <= 0
            ):
                missing.append(peer)
        if not missing:
            logger.info(
                "runtime_ready_gate_all_peers_ready",
                extra={
                    "event": "runtime_ready_gate_all_peers_ready",
                    "process_role": role,
                    "peers": peer_list,
                    "generation": generation,
                    "elapsed_seconds": round(
                        _runtime_ready_clock() - (deadline - timeout_seconds), 2
                    ),
                },
            )
            return
        if timed_out or _runtime_ready_clock() >= deadline:
            logger.warning(
                "runtime_ready_gate_timeout",
                extra={
                    "event": "runtime_ready_gate_timeout",
                    "process_role": role,
                    "missing_peers": missing,
                    "timeout_seconds": timeout_seconds,
                    "generation": generation,
                },
            )
            if required:
                raise RuntimeError(
                    f"runtime_ready_gate_timeout:{role}:{','.join(missing)}"
                )
            return
        await asyncio.sleep(poll_interval)


async def _withdraw_runtime_ready(
    *,
    lease: _RuntimeReadyLease | None,
    hot_state_store: HotStateStore | None,
    logger,
    timeout_seconds: float = 2.0,
) -> None:
    """Best-effort owner-aware 删除本实例 lease，不覆盖原始退出原因。"""

    if hot_state_store is None or lease is None:
        return
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0.0:
        raise ValueError("runtime readiness withdraw timeout must be positive")
    try:
        deleted = await asyncio.wait_for(
            hot_state_store.compare_delete(lease.key, lease.payload),
            timeout=timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "runtime_ready_gate_withdraw_failed",
            extra={
                "event": "runtime_ready_gate_withdraw_failed",
                "process_role": lease.role,
                "generation": lease.generation,
                "error_type": type(exc).__name__,
            },
        )
        return
    if not deleted:
        logger.info(
            "runtime_ready_gate_withdraw_skipped_not_owner",
            extra={
                "event": "runtime_ready_gate_withdraw_skipped_not_owner",
                "process_role": lease.role,
                "generation": lease.generation,
            },
        )
        return
    logger.info(
        "runtime_ready_gate_withdrawn",
        extra={
            "event": "runtime_ready_gate_withdrawn",
            "process_role": lease.role,
            "generation": lease.generation,
        },
    )


async def _stop_runtime_ready_lease_task(
    *,
    lease_task: asyncio.Task[None] | None,
    stop_event: asyncio.Event,
    timeout_seconds: float = 2.0,
    propagate_errors: bool = False,
) -> None:
    """停止 lease task；其既有异常不得截断业务清理。"""

    stop_event.set()
    if lease_task is None:
        return
    timed_out = False
    if not lease_task.done():
        _done, pending = await asyncio.wait(
            (lease_task,),
            timeout=timeout_seconds,
        )
        if pending:
            timed_out = True
            lease_task.cancel()
    results = await asyncio.gather(lease_task, return_exceptions=True)
    if not propagate_errors:
        return
    if timed_out:
        raise RuntimeError("runtime_ready_lease_stop_timeout")
    result = results[0]
    if isinstance(result, BaseException):
        raise result


async def _stop_provisioning_lease_before_promotion(
    *,
    lease_task: asyncio.Task[None] | None,
    stop_event: asyncio.Event,
    role: str,
    on_unexpected_termination: Callable[[], None],
) -> None:
    """Stop PROVISIONING refresh without swallowing a same-tick lease failure."""

    if lease_task is None:
        on_unexpected_termination()
        raise RuntimeError(f"runtime_ready_lease_required:{role}")
    if lease_task.done():
        on_unexpected_termination()
        lease_task.result()
        raise RuntimeError(f"runtime_ready_lease_stopped:{role}")
    try:
        await _stop_runtime_ready_lease_task(
            lease_task=lease_task,
            stop_event=stop_event,
            propagate_errors=True,
        )
    except BaseException:
        on_unexpected_termination()
        raise


def _hard_exit_process(exit_code: int) -> None:
    """最后一道进程级 fencing；仅供 readiness 失租看门狗调用。"""

    os._exit(exit_code)


def _runtime_ready_hard_deadline(
    *,
    expires_not_after_monotonic: float,
    phase: str,
    shutdown_margin: float = _RUNTIME_READY_SHUTDOWN_MARGIN_SECONDS,
    force_exit_grace: float = _RUNTIME_READY_FORCE_EXIT_GRACE_SECONDS,
    provisioning_exit_guard: float = (
        _RUNTIME_READY_PROVISIONING_EXIT_GUARD_SECONDS
    ),
) -> float:
    """返回保守 hard-exit 时刻；READY 在 graceful stop 后保留有界清理窗。"""

    values = (
        expires_not_after_monotonic,
        shutdown_margin,
        force_exit_grace,
        provisioning_exit_guard,
    )
    if not all(math.isfinite(value) for value in values):
        raise ValueError("runtime readiness watchdog deadlines must be finite")
    if not 0.0 < force_exit_grace < shutdown_margin:
        raise ValueError("runtime readiness force-exit grace must be below margin")
    if provisioning_exit_guard <= 0.0:
        raise ValueError("runtime readiness provisioning guard must be positive")
    if phase == _RUNTIME_READY_PHASE_PROVISIONING:
        return expires_not_after_monotonic - provisioning_exit_guard
    if phase == _RUNTIME_READY_PHASE_READY:
        return (
            expires_not_after_monotonic
            - shutdown_margin
            + force_exit_grace
        )
    raise ValueError(f"unsupported runtime readiness phase: {phase}")


class _RuntimeReadyDeadlineWatchdog:
    """独立子进程、sticky 的 lease deadline fence。

    子进程从 PROVISIONING claim 成功即存在，不依赖被保护进程的 asyncio、线程
    调度或 GIL。它只加载标准库 watchdog 模块，并先取得 pidfd/Windows process
    handle，避免 PID 重用。fatal 一旦置位不可 disarm/rearm。
    """

    _CONTROL_ACK_TIMEOUT_SECONDS = 2.0
    _STARTUP_TIMEOUT_SECONDS = 5.0

    def __init__(self, *, role: str, deadline_monotonic: float) -> None:
        if not math.isfinite(deadline_monotonic):
            raise ValueError("runtime readiness watchdog deadline must be finite")
        self.role = role
        self._lock = threading.Lock()
        self._deadline_monotonic = float(deadline_monotonic)
        self._fatal = False
        self._shutdown = False
        self._disarmed = False
        self._firing = False
        # 捕获构造时的 exit callable；测试即使在断言失败后 monkeypatch teardown，
        # 存活线程也不会回落到真实 os._exit 杀死整个 pytest 进程。
        self._exit_process = _hard_exit_process
        self._nonce = uuid.uuid4().hex
        self._sequence = 0
        self._acknowledgements: queue.Queue[str | None] = queue.Queue()
        self._child_loss_expected = threading.Event()
        self._child_lost = threading.Event()
        self._supervision_armed = threading.Event()
        # Popen itself may fail before assigning the child. Constructor cleanup
        # must preserve that original error instead of raising AttributeError.
        self._process: subprocess.Popen[bytes] | None = None
        creation_flags = (
            subprocess.CREATE_NO_WINDOW
            if os.name == "nt"
            else 0
        )
        watchdog_command = [
            sys.executable,
            "-m",
            "aats.bootstrap.readiness_watchdog",
            "--parent-pid",
            str(os.getpid()),
            "--deadline-ns",
            str(int(self._deadline_monotonic * 1_000_000_000)),
            "--nonce",
            self._nonce,
        ]
        parent_pidfd: int | None = None
        popen_extra: dict[str, Any] = {}
        if os.name != "nt":
            if not hasattr(os, "pidfd_open") or not hasattr(
                signal,
                "pidfd_send_signal",
            ):
                raise RuntimeError("pidfd readiness watchdog support is required")
            parent_pidfd = os.pidfd_open(os.getpid(), 0)
            watchdog_command.extend(("--parent-pidfd", str(parent_pidfd)))
            popen_extra["pass_fds"] = (parent_pidfd,)
        else:
            from aats.bootstrap.readiness_watchdog import (
                windows_process_creation_token,
            )

            watchdog_command.extend(
                (
                    "--parent-creation-token",
                    str(windows_process_creation_token(os.getpid())),
                )
            )
        started_before_ns = _runtime_ready_clock_ns()
        try:
            self._process = subprocess.Popen(
                watchdog_command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
                creationflags=creation_flags,
                start_new_session=(os.name != "nt"),
                **popen_extra,
            )
            if parent_pidfd is not None:
                os.close(parent_pidfd)
                parent_pidfd = None
            self._reader_thread = threading.Thread(
                target=self._read_acknowledgements,
                name=f"aats-runtime-ready-watchdog-ack-{role}",
                daemon=True,
            )
            self._reader_thread.start()
            acknowledgement = self._get_ack(self._STARTUP_TIMEOUT_SECONDS)
            started_after_ns = _runtime_ready_clock_ns()
            fields = acknowledgement.split(" ") if acknowledgement else []
            if len(fields) != 3 or fields[:2] != ["READY", self._nonce]:
                raise RuntimeError("runtime readiness watchdog startup rejected")
            child_clock_ns = int(fields[2])
            if not started_before_ns <= child_clock_ns <= started_after_ns:
                raise RuntimeError("runtime readiness watchdog clock mismatch")
            if self._process.poll() is not None:
                raise RuntimeError("runtime readiness watchdog exited during startup")
            self._supervision_armed.set()
            if self._child_lost.is_set():
                raise RuntimeError(
                    "runtime readiness watchdog exited during startup"
                )
        except BaseException:
            if parent_pidfd is not None:
                os.close(parent_pidfd)
            self._terminate_child()
            self._close_connections()
            raise

    def _read_acknowledgements(self) -> None:
        stdout = getattr(self._process, "stdout", None)
        if stdout is None:
            self._report_watchdog_channel_loss()
            return
        try:
            while True:
                frame = stdout.readline(513)
                if not frame:
                    self._report_watchdog_channel_loss()
                    return
                if len(frame) > 512 or not frame.endswith(b"\n"):
                    self._report_watchdog_channel_loss()
                    return
                try:
                    decoded = frame.rstrip(b"\r\n").decode("ascii")
                except UnicodeDecodeError:
                    self._report_watchdog_channel_loss()
                    return
                self._acknowledgements.put(decoded)
        except (OSError, ValueError):
            self._report_watchdog_channel_loss()

    def _report_watchdog_channel_loss(self) -> None:
        self._child_lost.set()
        self._acknowledgements.put(None)
        # watchdog 自身意外死亡时，健康父进程不应继续到下一次 10s lease
        # refresh 才发现。ACK reader 独立于 asyncio，通道 EOF 后立即 fail-closed。
        # 构造失败与显式 DISARM/terminate 会先标 expected，不得误杀父进程。
        if (
            self._supervision_armed.is_set()
            and not self._child_loss_expected.is_set()
        ):
            self._exit_process(1)

    def _get_ack(self, timeout_seconds: float) -> str | None:
        try:
            return self._acknowledgements.get(timeout=timeout_seconds)
        except queue.Empty:
            return None

    @property
    def deadline_monotonic(self) -> float:
        with self._lock:
            return self._deadline_monotonic

    @property
    def fatal(self) -> bool:
        with self._lock:
            return self._fatal

    @property
    def disarmed(self) -> bool:
        with self._lock:
            return self._disarmed

    @property
    def shutdown(self) -> bool:
        with self._lock:
            return self._shutdown

    def _exchange(
        self,
        *,
        opcode: str,
        deadline_monotonic: float | None = None,
    ) -> tuple[str, float | None] | None:
        if self._process.poll() is not None:
            return None
        self._sequence += 1
        fields = ["AATS_RDW_V1", self._nonce, str(self._sequence), opcode]
        if deadline_monotonic is not None:
            fields.append(str(int(deadline_monotonic * 1_000_000_000)))
        frame = (" ".join(fields) + "\n").encode("ascii")
        stdin = self._process.stdin
        if stdin is None:
            return None
        try:
            stdin.write(frame)
            stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            return None
        acknowledgement = self._get_ack(self._CONTROL_ACK_TIMEOUT_SECONDS)
        if not acknowledgement:
            return None
        ack_fields = acknowledgement.split(" ")
        if (
            len(ack_fields) not in {4, 5}
            or ack_fields[:3]
            != ["ACK", self._nonce, str(self._sequence)]
        ):
            return None
        returned_deadline = (
            int(ack_fields[4]) / 1_000_000_000
            if len(ack_fields) == 5
            else None
        )
        return ack_fields[3], returned_deadline

    def _close_connections(self) -> None:
        process = self._process
        if process is None:
            return
        for connection in (
            getattr(process, "stdin", None),
            getattr(process, "stdout", None),
        ):
            if connection is None:
                continue
            try:
                connection.close()
            except Exception:
                pass

    def _terminate_child(self) -> None:
        self._child_loss_expected.set()
        process = getattr(self, "_process", None)
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=1.0)
        except Exception:
            try:
                process.kill()
                process.wait(timeout=1.0)
            except Exception:
                pass

    def rearm_success(self, *, deadline_monotonic: float) -> bool:
        if not math.isfinite(deadline_monotonic):
            raise ValueError("runtime readiness watchdog deadline must be finite")
        with self._lock:
            if self._fatal or self._shutdown or self._disarmed or self._firing:
                return False
            acknowledgement = self._exchange(
                opcode="REARM",
                deadline_monotonic=float(deadline_monotonic),
            )
            if not acknowledgement or acknowledgement[0] != "REARMED":
                self._fatal = True
                return False
            if acknowledgement[1] is None:
                self._fatal = True
                return False
            self._deadline_monotonic = acknowledgement[1]
            return True

    def begin_shutdown(self, *, deadline_monotonic: float) -> bool:
        """冻结续租并收紧停机上界；安全清理完成后仍允许 DISARM。"""

        if not math.isfinite(deadline_monotonic):
            raise ValueError("runtime readiness watchdog deadline must be finite")
        with self._lock:
            if self._fatal or self._disarmed or self._firing:
                return False
            acknowledgement = self._exchange(
                opcode="SHUTDOWN",
                deadline_monotonic=float(deadline_monotonic),
            )
            if not acknowledgement or acknowledgement[0] != "SHUTDOWN":
                self._fatal = True
                return False
            if acknowledgement[1] is None:
                self._fatal = True
                return False
            self._shutdown = True
            self._deadline_monotonic = acknowledgement[1]
            return True

    def mark_fatal_and_tighten(self, *, deadline_monotonic: float) -> bool:
        if not math.isfinite(deadline_monotonic):
            raise ValueError("runtime readiness watchdog deadline must be finite")
        with self._lock:
            if self._disarmed or self._firing:
                return False
            acknowledgement = self._exchange(
                opcode="FATAL",
                deadline_monotonic=float(deadline_monotonic),
            )
            self._fatal = True
            if not acknowledgement or acknowledgement[0] != "FATAL":
                return False
            if acknowledgement[1] is None:
                return False
            self._deadline_monotonic = acknowledgement[1]
            return True

    def force_exit_now(self) -> None:
        with self._lock:
            if self._disarmed:
                return
            self._fatal = True
            self._firing = True
        self._exit_process(1)

    def disarm(self) -> bool:
        with self._lock:
            if self._fatal or self._firing:
                return False
            self._child_loss_expected.set()
            acknowledgement = self._exchange(opcode="DISARM")
            if not acknowledgement or acknowledgement[0] != "DISARMED":
                self._fatal = True
                return False
            self._disarmed = True
            # 解除 child reader 对 stdin 的阻塞，确保解释器能在 ACK 后及时退出。
            if self._process.stdin is not None:
                self._process.stdin.close()
        try:
            self._process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            self._terminate_child()
            return False
        self._close_connections()
        return True


async def _wait_for_runtime_takeover_quarantine(
    *,
    role: str,
    logger,
    duration_seconds: float = _RUNTIME_READY_TAKEOVER_QUARANTINE_SECONDS,
    _sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """新 owner 在任何 NATS I/O 前等待旧 v2 owner 的最大存活窗结束。"""

    minimum_seconds = (
        _RUNTIME_READY_TTL_SECONDS
        - _RUNTIME_READY_PROVISIONING_EXIT_GUARD_SECONDS
    )
    if (
        not math.isfinite(duration_seconds)
        or duration_seconds < minimum_seconds
    ):
        raise ValueError(
            "runtime readiness takeover quarantine must cover the maximum "
            "protocol-v2 owner survival window"
        )
    logger.info(
        "runtime_ready_takeover_quarantine_started",
        extra={
            "event": "runtime_ready_takeover_quarantine_started",
            "process_role": role,
            "duration_seconds": duration_seconds,
        },
    )
    await _sleep(duration_seconds)
    logger.info(
        "runtime_ready_takeover_quarantine_completed",
        extra={
            "event": "runtime_ready_takeover_quarantine_completed",
            "process_role": role,
            "duration_seconds": duration_seconds,
        },
    )


def _cancel_runtime_tasks_for_readiness_failure(
    *,
    runtime: Any,
    lease_task: asyncio.Task[None] | None,
    logger,
) -> None:
    """同步请求业务 task 停止，缩短失租后仍可能产生副作用的窗口。"""

    candidates: list[asyncio.Task[Any]] = list(
        getattr(runtime, "background_tasks", ()) or ()
    )
    critical_tasks = getattr(runtime, "critical_background_tasks", None) or {}
    candidates.extend(critical_tasks.values())
    seen: set[int] = set()
    for task in candidates:
        task_identity = id(task)
        if task_identity in seen or task is lease_task:
            continue
        seen.add(task_identity)
        try:
            if not task.done():
                task.cancel()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "runtime_ready_lease_task_cancel_failed",
                extra={
                    "event": "runtime_ready_lease_task_cancel_failed",
                    "process_role": getattr(runtime, "process_role", None),
                    "error_type": type(exc).__name__,
                },
            )


async def _await_task_while_runtime_ready(
    *,
    operation_task: asyncio.Task[Any],
    lease_task: asyncio.Task[None] | None,
    role: str,
    on_lease_terminated: Callable[[], None] | None = None,
    delivery_gate: NatsDeliveryGate | None = None,
) -> Any:
    """等待启动阶段操作；lease 先结束时立即取消操作并失败关闭。"""

    if lease_task is None and delivery_gate is None:
        return await operation_task
    delivery_abort_task = (
        asyncio.create_task(
            delivery_gate.wait_aborted(),
            name=f"aats-runtime-delivery-abort-wait-{role}",
        )
        if delivery_gate is not None
        else None
    )
    waiters = [operation_task]
    if lease_task is not None:
        waiters.append(lease_task)
    if delivery_abort_task is not None:
        waiters.append(delivery_abort_task)
    try:
        done, _pending = await asyncio.wait(
            waiters,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if lease_task is not None and lease_task in done:
            # 必须先 arm 独立进程 watchdog，再等待 operation task 响应取消；后者可能
            # 永久吞掉 CancelledError，不能让它绕过 lease expiry 上界。
            if on_lease_terminated is not None:
                on_lease_terminated()
            if not operation_task.done():
                operation_task.cancel()
            await asyncio.gather(operation_task, return_exceptions=True)
            lease_task.result()
            raise RuntimeError(f"runtime_ready_lease_stopped:{role}")
        if delivery_abort_task is not None and delivery_abort_task in done:
            if on_lease_terminated is not None:
                on_lease_terminated()
            if not operation_task.done():
                operation_task.cancel()
            await asyncio.gather(operation_task, return_exceptions=True)
            raise RuntimeError(f"runtime_ready_delivery_gate_aborted:{role}")
        return await operation_task
    finally:
        if delivery_abort_task is not None and not delivery_abort_task.done():
            delivery_abort_task.cancel()
            await asyncio.gather(
                delivery_abort_task,
                return_exceptions=True,
            )


async def _activate_runtime_event_delivery(
    *,
    runtime: Any,
    delivery_gate: NatsDeliveryGate,
    role: str,
) -> None:
    """冲刷构建期 NATS 发布后再开放 callback；不允许裸 gate 绕过 flush。"""

    activate_bus = getattr(getattr(runtime, "bus", None), "activate_delivery", None)
    if callable(activate_bus):
        await activate_bus()
    elif not delivery_gate.activate():
        raise RuntimeError(f"runtime_ready_delivery_gate_aborted:{role}")
    if not delivery_gate.activated:
        raise RuntimeError(f"runtime_ready_delivery_gate_not_activated:{role}")


async def _verify_runtime_event_bus_ready_for_promotion(
    *,
    runtime: Any,
    role: str,
) -> None:
    """Require a fresh NATS connection/durable proof before publishing READY."""

    verify = getattr(
        getattr(runtime, "bus", None),
        "verify_ready_for_promotion",
        None,
    )
    if callable(verify):
        await verify()
        return
    if isinstance(runtime, ApplicationRuntime):
        raise RuntimeError(
            f"runtime_ready_bus_promotion_verifier_required:{role}"
        )


# Stage 7 修复：心跳文件目录。3 个 daemon 进程 (market/decision/execution)
# 没有 HTTP listener，docker compose 无法直接探活。process_lifecycle 启动一个
# background task 周期性更新 mtime，docker healthcheck 用 stat 检查 mtime
# 与当前时间差是否 < HEARTBEAT_STALE_AFTER_SECONDS。
# 默认目录用 /tmp（容器内非 root user 也能写），允许通过环境变量覆盖以便单测。
HEARTBEAT_INTERVAL_SECONDS = 5.0
HEARTBEAT_STALE_AFTER_SECONDS = 30
HEARTBEAT_DIR_ENV = "AATS_HEARTBEAT_DIR"


def _heartbeat_path(role: str, *, base_dir: Path | None = None) -> Path:
    """返回给定 role 的心跳文件路径。

    `base_dir` 显式传入用于单测；生产路径走环境变量 AATS_HEARTBEAT_DIR
    或默认 tempfile.gettempdir()（在容器里就是 /tmp）。
    """
    if base_dir is None:
        env_dir = os.environ.get(HEARTBEAT_DIR_ENV)
        base_dir = Path(env_dir) if env_dir else Path(tempfile.gettempdir())
    return base_dir / f"aats_{role}_heartbeat"


def _invalidate_previous_heartbeat(
    role: str,
    *,
    logger,
    base_dir: Path | None = None,
) -> None:
    """启动最早期删除同容器旧 mtime，防止 PROVISIONING 被误报 healthy。"""

    path = _heartbeat_path(role, base_dir=base_dir)
    try:
        path.unlink(missing_ok=True)
    except Exception as exc:
        logger.error(
            "process_lifecycle_heartbeat_invalidate_failed",
            extra={
                "event": "process_lifecycle_heartbeat_invalidate_failed",
                "process_role": role,
                "path": str(path),
                "error_type": type(exc).__name__,
            },
        )
        raise RuntimeError(
            f"process_lifecycle_heartbeat_invalidate_failed:{role}"
        ) from None


async def _heartbeat_loop(
    role: str,
    *,
    stop_event: asyncio.Event,
    logger,
    interval: float = HEARTBEAT_INTERVAL_SECONDS,
    base_dir: Path | None = None,
    started_event: asyncio.Event | None = None,
) -> None:
    """周期性 touch 心跳文件，直到 stop_event 被 set。

    设计要点：
    * 第一次 touch 在 loop 开头立即做，避免 healthcheck 在 start_period 内
      探到旧文件 / 不存在。
    * 用 mtime 而非内容：docker healthcheck 用 stat 比较 mtime，最小开销。
    * stop_event set 后 loop 退出，文件不主动删除；下一次同容器进程入口先
      invalidate，避免 restart policy 重启时沿用旧 mtime。
    * 任一 IO 异常使 task 失败；run_process 把它作为关键健康任务监督并退出，
      不能让“进程存活但永远 unhealthy”冒充可自愈。
    """
    path = _heartbeat_path(role, base_dir=base_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # pragma: no cover - 目录权限异常 logging 路径
        logger.warning(
            "process_lifecycle_heartbeat_mkdir_failed",
            extra={
                "event": "process_lifecycle_heartbeat_mkdir_failed",
                "process_role": role,
                "path": str(path),
                "error": str(exc),
            },
        )
        raise RuntimeError(
            f"process_lifecycle_heartbeat_mkdir_failed:{role}"
        ) from None

    logger.info(
        "process_lifecycle_heartbeat_started",
        extra={
            "event": "process_lifecycle_heartbeat_started",
            "process_role": role,
            "path": str(path),
            "interval_seconds": interval,
        },
    )

    first_touch = True
    while not stop_event.is_set():
        try:
            path.touch(exist_ok=True)
            # touch 在某些 fs 上不会更新 mtime，显式 utime 兜底
            os.utime(path, None)
        except Exception as exc:  # pragma: no cover - 心跳写失败 logging 路径
            logger.warning(
                "process_lifecycle_heartbeat_touch_failed",
                extra={
                    "event": "process_lifecycle_heartbeat_touch_failed",
                    "process_role": role,
                    "path": str(path),
                    "error": str(exc),
                },
            )
            raise RuntimeError(
                f"process_lifecycle_heartbeat_touch_failed:{role}"
            ) from None
        if first_touch:
            first_touch = False
            if started_event is not None:
                started_event.set()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue


async def _wait_for_heartbeat_initial_touch(
    *,
    heartbeat_task: asyncio.Task[None],
    started_event: asyncio.Event,
    role: str,
) -> None:
    """首次 touch 是 lifecycle ready 的硬前置；task 提前结束即失败。"""

    started_wait = asyncio.create_task(
        started_event.wait(),
        name=f"aats-heartbeat-initial-touch-{role}",
    )
    try:
        done, _pending = await asyncio.wait(
            (heartbeat_task, started_wait),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if heartbeat_task in done:
            heartbeat_task.result()
            raise RuntimeError(
                f"process_lifecycle_heartbeat_stopped:{role}"
            )
        await started_wait
    finally:
        if not started_wait.done():
            started_wait.cancel()
        await asyncio.gather(started_wait, return_exceptions=True)


def _resolve_process_role(*, requested: str | None) -> str:
    """把 entry 传入的 process_role 校验并返回，不允许 None / monolith 之外的非法值。

    monolith 也允许（向后兼容 + 单元测试场景），但调用方在 4 进程 entry 里
    应当显式传入 gateway/market/decision/execution。
    """
    if requested is None:
        raise ValueError(
            "process_lifecycle entry 必须显式传入 process_role；"
            "monolith fallback 应当走 apps/api_gateway/main.py 的旧路径"
        )
    if requested not in ALLOWED_PROCESS_ROLES:
        raise ValueError(
            f"process_role={requested!r} 不在合法集合 {sorted(ALLOWED_PROCESS_ROLES)} 中"
        )
    return requested


def _install_shutdown_signals(*, stop_event: asyncio.Event, logger) -> None:
    """注册 SIGTERM/SIGINT，触发后 set stop_event。

    Linux 优先 loop.add_signal_handler；Windows 走 signal.signal() 兜底。
    任何注册失败都不会抛——signal 是「能装就装」，最终用户也可以直接 Ctrl-C。
    """
    loop = asyncio.get_running_loop()
    handled: list[str] = []

    def _request_stop(signame: str) -> None:
        if stop_event.is_set():
            return
        logger.info(
            "process_lifecycle_signal_received",
            extra={"event": "process_lifecycle_signal_received", "signal": signame},
        )
        stop_event.set()

    # POSIX 路径
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _request_stop, sig.name)
                handled.append(sig.name)
            except (NotImplementedError, RuntimeError):
                # 某些受限运行环境（Jupyter、嵌套 loop）不支持 add_signal_handler
                pass

    # Windows 兜底 / POSIX 失败兜底
    if not handled:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, lambda _signum, _frame, _name=sig.name: _request_stop(_name))
                handled.append(sig.name)
            except (ValueError, OSError):
                # 子线程里调用 signal.signal 会抛 ValueError，跳过即可
                pass

    logger.info(
        "process_lifecycle_signals_installed",
        extra={
            "event": "process_lifecycle_signals_installed",
            "handled_signals": handled,
        },
    )


async def run_process(
    *,
    process_role: str,
    app_name: str,
    settings: AATSSettings | None = None,
    extra_setup: Callable[[ApplicationRuntime], Awaitable[None]] | None = None,
    stop_event: asyncio.Event | None = None,
) -> int:
    """4 进程 entry 共用的「build → start → wait → stop」编排。

    参数：
        process_role: gateway / market / decision / execution / monolith。
        app_name: 用于 logger 命名（如 "apps.market_gateway"）。
        settings: 测试注入用；生产路径传 None，函数内部 load_settings()。
        extra_setup: 可选的额外启动钩子（例如 gateway 进程要在这里挂载 FastAPI app）。
        stop_event: 测试注入用；生产路径传 None，函数内部新建并注册信号 handler。

    返回值：
        进程退出码（0 = 干净退出，1 = 启动或运行期异常）。
    """
    role = _resolve_process_role(requested=process_role)
    effective_settings = settings if settings is not None else load_settings()
    configure_logging_for_settings(effective_settings)
    logger = get_logger(app_name)

    logger.info(
        "process_lifecycle_starting",
        extra={
            "event": "process_lifecycle_starting",
            "process_role": role,
            "app_name": app_name,
        },
    )

    runtime: ApplicationRuntime | None = None
    heartbeat_task: asyncio.Task | None = None
    heartbeat_initial_task: asyncio.Task[None] | None = None
    stop_wait_task: asyncio.Task[bool] | None = None
    critical_wait_task: asyncio.Task[Any] | None = None
    peer_wait_task: asyncio.Task[None] | None = None
    takeover_quarantine_task: asyncio.Task[None] | None = None
    delivery_activation_task: asyncio.Task[None] | None = None
    background_start_task: asyncio.Task[None] | None = None
    runtime_build_task: asyncio.Task[ApplicationRuntime] | None = None
    pre_promotion_bus_check_task: asyncio.Task[None] | None = None
    readiness_lease_task: asyncio.Task[None] | None = None
    readiness_deadline_watchdog: _RuntimeReadyDeadlineWatchdog | None = None
    readiness_lease: _RuntimeReadyLease | None = None
    readiness_hot_state: HotStateStore | None = None
    readiness_generation: str | None = None
    readiness_required = False
    # 心跳 stop_event 与 run_process stop_event 分开：心跳必须在
    # stop_background_tasks 期间继续打，让 docker 看到容器仍在干净退出而非挂死。
    heartbeat_stop = asyncio.Event()
    heartbeat_started = asyncio.Event()
    readiness_lease_stop = asyncio.Event()
    readiness_delivery_gate = NatsDeliveryGate()

    def _arm_readiness_failure_shutdown() -> None:
        # 失租与普通优雅退出不同：立即停止健康心跳、把常驻 deadline
        # watchdog 置为 sticky fatal，并同步 cancel 已注册业务 task。
        # 续租必须在任何日志或 await 前冻结；否则 cleanup 卡住时仍可能
        # 把旧 owner TTL 延长 60 秒，叠加新实例 takeover quarantine，令
        # 自愈时延无界放大。
        readiness_lease_stop.set()
        heartbeat_stop.set()
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
                logger=logger,
            )
        logger.critical(
            "runtime_ready_lease_shutdown_watchdog_fatal",
            extra={
                "event": "runtime_ready_lease_shutdown_watchdog_fatal",
                "process_role": role,
                "grace_seconds": _RUNTIME_READY_FORCE_EXIT_GRACE_SECONDS,
            },
        )

    def _supervise_readiness_lease_task(
        task: asyncio.Task[None],
        *,
        stop_signal: asyncio.Event,
    ) -> None:
        def _on_done(_task: asyncio.Task[None]) -> None:
            if not stop_signal.is_set():
                _arm_readiness_failure_shutdown()

        task.add_done_callback(_on_done)

    async def _claim_readiness_before_event_bus(
        hot_state_store: HotStateStore,
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
            role=role,
            settings=final_settings,
        )
        _validate_runtime_readiness_backend(
            role=role,
            settings=final_settings,
            required=readiness_required,
        )
        readiness_generation = _runtime_readiness_generation(
            role=role,
            settings=final_settings,
            required=readiness_required,
        )
        readiness_hot_state = hot_state_store
        if not readiness_required:
            readiness_delivery_gate.activate()
            return
        readiness_lease = await _announce_runtime_ready(
            role=role,
            hot_state_store=hot_state_store,
            logger=logger,
            generation=readiness_generation,
            required=True,
        )
        if readiness_lease is None:
            raise RuntimeError(f"runtime_ready_lease_required:{role}")
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
                role=role,
                deadline_monotonic=hard_deadline,
            )
        except Exception as exc:
            await _withdraw_runtime_ready(
                lease=readiness_lease,
                hot_state_store=hot_state_store,
                logger=logger,
            )
            raise RuntimeError(
                f"runtime_ready_lease_watchdog_start_failed:{role}"
            ) from exc
        logger.info(
            "runtime_ready_lease_watchdog_armed",
            extra={
                "event": "runtime_ready_lease_watchdog_armed",
                "process_role": role,
                "generation": readiness_generation,
                "phase": readiness_lease.phase,
            },
        )
        # Claim 成功后先持续续租 PROVISIONING，再在任何 NATS I/O 前隔离 55s。
        # 这样 Redis restart/误删/历史 allkeys-lru 提前丢 key 时，新实例不会与
        # 仍未到本地 hard fence 的旧 protocol-v2 owner 发生业务重叠。
        readiness_lease_task = asyncio.create_task(
            _maintain_runtime_ready_lease(
                lease=readiness_lease,
                hot_state_store=hot_state_store,
                logger=logger,
                stop_event=readiness_lease_stop,
                required=True,
                deadline_watchdog=readiness_deadline_watchdog,
                absolute_hard_deadline_monotonic=(
                    provisioning_hard_deadline
                ),
                suppress_failures_when_stopping=False,
            ),
            name=f"aats-runtime-ready-provisioning-lease-{role}",
        )
        _supervise_readiness_lease_task(
            readiness_lease_task,
            stop_signal=readiness_lease_stop,
        )
        takeover_quarantine_task = asyncio.create_task(
            _wait_for_runtime_takeover_quarantine(role=role, logger=logger),
            name=f"aats-runtime-ready-takeover-quarantine-{role}",
        )
        await _await_task_while_runtime_ready(
            operation_task=takeover_quarantine_task,
            lease_task=readiness_lease_task,
            role=role,
            on_lease_terminated=_arm_readiness_failure_shutdown,
            delivery_gate=readiness_delivery_gate,
        )

    try:
        _invalidate_previous_heartbeat(role, logger=logger)
        readiness_required = _strict_peer_readiness_required(
            role=role,
            settings=effective_settings,
        )
        if isinstance(effective_settings, AATSSettings):
            runtime_build_task = asyncio.create_task(
                build_runtime(
                    effective_settings,
                    process_role=role,
                    before_event_bus_start=_claim_readiness_before_event_bus,
                    nats_delivery_gate=(
                        readiness_delivery_gate
                        if readiness_required
                        else None
                    ),
                ),
                name=f"aats-runtime-build-{role}",
            )
            # The lease task is created from inside build_runtime's pre-NATS
            # callback, so it cannot be captured as a waiter here. Its done
            # callback aborts this gate; NATS terminal callbacks do the same.
            # Monitoring the gate around the entire build closes both paths.
            runtime = await _await_task_while_runtime_ready(
                operation_task=runtime_build_task,
                lease_task=None,
                role=role,
                on_lease_terminated=_arm_readiness_failure_shutdown,
                delivery_gate=readiness_delivery_gate,
            )
        else:
            readiness_generation = _runtime_readiness_generation(
                role=role,
                settings=effective_settings,
                required=readiness_required,
            )
            runtime = await build_runtime(effective_settings, process_role=role)
        # ── Readiness barrier (B1) ─────────────────────────────
        # build_runtime 在任何 NATS I/O 前已 claim 全局 role PROVISIONING
        # ownership；所有 durable callback 由本地 gate 保持不可投递。完整装配
        # 返回后原子 CAS 为 READY，再等同代 peer READY，最后统一开放 callback
        # 并启动 publisher。
        if readiness_required:
            if (
                readiness_lease is None
                or readiness_hot_state is None
                or readiness_deadline_watchdog is None
            ):
                raise RuntimeError(f"runtime_ready_lease_required:{role}")
            if readiness_delivery_gate.aborted:
                _arm_readiness_failure_shutdown()
                raise RuntimeError(
                    f"runtime_ready_delivery_gate_aborted:{role}"
                )
            pre_promotion_bus_check_task = asyncio.create_task(
                _verify_runtime_event_bus_ready_for_promotion(
                    runtime=runtime,
                    role=role,
                ),
                name=f"aats-runtime-bus-pre-promotion-check-{role}",
            )
            await _await_task_while_runtime_ready(
                operation_task=pre_promotion_bus_check_task,
                lease_task=readiness_lease_task,
                role=role,
                on_lease_terminated=_arm_readiness_failure_shutdown,
                delivery_gate=readiness_delivery_gate,
            )
            # PROVISIONING maintainer 的 CAS payload 与 READY 不同；必须先有序
            # 停掉旧 maintainer，才能做唯一一次 PROVISIONING->READY CAS，避免
            # 二者竞态把本实例自己的 promotion 误判成失租。
            await _stop_provisioning_lease_before_promotion(
                lease_task=readiness_lease_task,
                stop_event=readiness_lease_stop,
                role=role,
                on_unexpected_termination=_arm_readiness_failure_shutdown,
            )
            readiness_lease_task = None
            readiness_lease_stop = asyncio.Event()
            readiness_lease = await _promote_runtime_ready(
                lease=readiness_lease,
                hot_state_store=readiness_hot_state,
                watchdog=readiness_deadline_watchdog,
                logger=logger,
            )
            readiness_lease_task = asyncio.create_task(
                _maintain_runtime_ready_lease(
                    lease=readiness_lease,
                    hot_state_store=readiness_hot_state,
                    logger=logger,
                    stop_event=readiness_lease_stop,
                    required=True,
                    deadline_watchdog=readiness_deadline_watchdog,
                ),
                name=f"aats-runtime-ready-lease-{role}",
            )
            _supervise_readiness_lease_task(
                readiness_lease_task,
                stop_signal=readiness_lease_stop,
            )
            peer_wait_task = asyncio.create_task(
                _wait_for_peer_roles_ready(
                    role=role,
                    hot_state_store=readiness_hot_state,
                    logger=logger,
                    generation=readiness_generation,
                    required=True,
                ),
                name=f"aats-runtime-ready-peer-wait-{role}",
            )
            await _await_task_while_runtime_ready(
                operation_task=peer_wait_task,
                lease_task=readiness_lease_task,
                role=role,
                on_lease_terminated=_arm_readiness_failure_shutdown,
                delivery_gate=readiness_delivery_gate,
            )
            delivery_activation_task = asyncio.create_task(
                _activate_runtime_event_delivery(
                    runtime=runtime,
                    delivery_gate=readiness_delivery_gate,
                    role=role,
                ),
                name=f"aats-runtime-delivery-activation-{role}",
            )
            await _await_task_while_runtime_ready(
                operation_task=delivery_activation_task,
                lease_task=readiness_lease_task,
                role=role,
                on_lease_terminated=_arm_readiness_failure_shutdown,
                delivery_gate=readiness_delivery_gate,
            )
        else:
            if not readiness_delivery_gate.activate():
                raise RuntimeError(f"runtime_ready_delivery_gate_aborted:{role}")
        background_start_task = asyncio.create_task(
            runtime.start_background_tasks(),
            name=f"aats-runtime-background-start-{role}",
        )
        await _await_task_while_runtime_ready(
            operation_task=background_start_task,
            lease_task=readiness_lease_task,
            role=role,
            on_lease_terminated=_arm_readiness_failure_shutdown,
            delivery_gate=readiness_delivery_gate,
        )
        if extra_setup is not None:
            await extra_setup(runtime)

        # 注册信号 + 等待 stop。stop_event 在测试里可以预先注入并提前 set 来跳过等待。
        local_stop = stop_event if stop_event is not None else asyncio.Event()
        if stop_event is None:
            _install_shutdown_signals(stop_event=local_stop, logger=logger)

        # Stage 7 修复：启动心跳 background task 给 docker compose healthcheck。
        # 与 runtime.background_tasks 解耦——心跳必须独立于业务任务存活，
        # 否则 stop_background_tasks 一调心跳就停了 docker 立刻把容器标 unhealthy。
        heartbeat_task = asyncio.create_task(
            _heartbeat_loop(
                role,
                stop_event=heartbeat_stop,
                logger=logger,
                started_event=heartbeat_started,
            ),
            name=f"aats-heartbeat-{role}",
        )
        heartbeat_initial_task = asyncio.create_task(
            _wait_for_heartbeat_initial_touch(
                heartbeat_task=heartbeat_task,
                started_event=heartbeat_started,
                role=role,
            ),
            name=f"aats-heartbeat-startup-{role}",
        )
        await _await_task_while_runtime_ready(
            operation_task=heartbeat_initial_task,
            lease_task=readiness_lease_task,
            role=role,
            on_lease_terminated=_arm_readiness_failure_shutdown,
            delivery_gate=readiness_delivery_gate,
        )

        logger.info(
            "process_lifecycle_ready",
            extra={
                "event": "process_lifecycle_ready",
                "process_role": role,
                "background_task_count": len(runtime.background_tasks),
            },
        )
        stop_wait_task = asyncio.create_task(
            local_stop.wait(),
            name=f"aats-stop-wait-{role}",
        )
        wait_for_critical_failure = getattr(
            runtime,
            "wait_for_critical_background_task_failure",
            None,
        )
        if callable(wait_for_critical_failure):
            critical_wait_task = asyncio.create_task(
                wait_for_critical_failure(),
                name=f"aats-critical-task-watch-{role}",
            )
            runtime_waiters = [
                stop_wait_task,
                critical_wait_task,
                heartbeat_task,
            ]
            if readiness_lease_task is not None:
                runtime_waiters.append(readiness_lease_task)
            done, _pending = await asyncio.wait(
                runtime_waiters,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if readiness_lease_task is not None and readiness_lease_task in done:
                _arm_readiness_failure_shutdown()
                readiness_lease_task.result()
                raise RuntimeError(f"runtime_ready_lease_stopped:{role}")
            if heartbeat_task in done:
                heartbeat_task.result()
                raise RuntimeError(
                    f"process_lifecycle_heartbeat_stopped:{role}"
                )
            critical_failure = None
            if critical_wait_task in done:
                critical_failure = critical_wait_task.result()
            else:
                inspect_failure = getattr(
                    runtime,
                    "critical_background_task_failure",
                    None,
                )
                if callable(inspect_failure):
                    critical_failure = inspect_failure()
            if critical_failure is not None:
                if readiness_deadline_watchdog is not None:
                    # 任何关键业务任务失败都必须有进程级退出上界；否则
                    # stop_background_tasks 卡死时 lease 会继续续租，Docker 也不会
                    # 因 unhealthy 自动 restart。
                    _arm_readiness_failure_shutdown()
                heartbeat_stop.set()
                logger.error(
                    "process_lifecycle_critical_task_failed",
                    extra={
                        "event": "process_lifecycle_critical_task_failed",
                        "process_role": role,
                        "task_name": critical_failure.task_name,
                        "failure_kind": critical_failure.failure_kind,
                        "error_type": critical_failure.error_type,
                        "stalled_seconds": getattr(
                            critical_failure,
                            "stalled_seconds",
                            None,
                        ),
                        "timeout_seconds": getattr(
                            critical_failure,
                            "timeout_seconds",
                            None,
                        ),
                    },
                )
                return 1
        else:
            runtime_waiters = [stop_wait_task, heartbeat_task]
            if readiness_lease_task is not None:
                runtime_waiters.append(readiness_lease_task)
            done, _pending = await asyncio.wait(
                runtime_waiters,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if readiness_lease_task is not None and readiness_lease_task in done:
                _arm_readiness_failure_shutdown()
                readiness_lease_task.result()
                raise RuntimeError(f"runtime_ready_lease_stopped:{role}")
            if heartbeat_task in done:
                heartbeat_task.result()
                raise RuntimeError(
                    f"process_lifecycle_heartbeat_stopped:{role}"
                )
        if readiness_deadline_watchdog is not None:
            # 正常 signal/stop 也必须有界：先冻结 lease 续租，再把 watchdog 收紧到
            # 10s；业务与 NATS 全停后才 DISARM + compare-delete owner。
            readiness_lease_stop.set()
            if not readiness_deadline_watchdog.begin_shutdown(
                deadline_monotonic=(
                    _runtime_ready_clock() + _RUNTIME_READY_FORCE_EXIT_GRACE_SECONDS
                )
            ):
                readiness_deadline_watchdog.force_exit_now()
        logger.info(
            "process_lifecycle_stopping",
            extra={"event": "process_lifecycle_stopping", "process_role": role},
        )
        return 0
    except Exception as exc:  # pragma: no cover - 顶层异常 logging 路径
        # 记录异常然后让 finally 走干净停机；不直接 raise 避免 main 拿不到退出码。
        readiness_delivery_gate.abort()
        if (
            readiness_required
            and readiness_deadline_watchdog is not None
            and not readiness_lease_stop.is_set()
        ):
            _arm_readiness_failure_shutdown()
        logger.exception(
            "process_lifecycle_failed",
            extra={
                "event": "process_lifecycle_failed",
                "process_role": role,
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )
        return 1
    finally:
        # BaseException（特别是 task.cancel()/KeyboardInterrupt/SystemExit）不会
        # 进入上面的 ``except Exception``。因此 finally 的第一个 await 之前也
        # 必须冻结 lease 并给整个 cleanup 设置硬上界；否则卡死的
        # stop_background_tasks 仍可由 maintainer 无限续租/rearm。正常 stop 与
        # fatal 路径已经分别进入 SHUTDOWN/FATAL，此处保持幂等。
        if readiness_deadline_watchdog is not None:
            readiness_lease_stop.set()
            readiness_delivery_gate.abort()
            if (
                not readiness_deadline_watchdog.fatal
                and not readiness_deadline_watchdog.shutdown
                and not readiness_deadline_watchdog.disarmed
            ):
                if not readiness_deadline_watchdog.begin_shutdown(
                    deadline_monotonic=(
                        _runtime_ready_clock()
                        + _RUNTIME_READY_FORCE_EXIT_GRACE_SECONDS
                    )
                ):
                    readiness_deadline_watchdog.force_exit_now()
        waiter_tasks = tuple(
            task
            for task in (
                stop_wait_task,
                critical_wait_task,
                heartbeat_initial_task,
                takeover_quarantine_task,
                peer_wait_task,
                delivery_activation_task,
                background_start_task,
                runtime_build_task,
                pre_promotion_bus_check_task,
            )
            if task is not None
        )
        for waiter_task in waiter_tasks:
            if not waiter_task.done():
                waiter_task.cancel()
        if waiter_tasks:
            await asyncio.gather(*waiter_tasks, return_exceptions=True)
        if runtime is not None:
            lease_released = False

            async def _release_readiness_after_business_stop() -> None:
                nonlocal lease_released
                if (
                    readiness_deadline_watchdog is not None
                    and readiness_deadline_watchdog.fatal
                ):
                    # Fatal 路径保留 owner 到 TTL，并由 sticky watchdog 强退；
                    # 即使业务 stop 已返回，也不能把异常退出伪装成 clean release。
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
                        f"runtime_ready_lease_watchdog_disarm_failed:{role}"
                    )
                await _withdraw_runtime_ready(
                    lease=readiness_lease,
                    hot_state_store=readiness_hot_state,
                    logger=logger,
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
            except Exception as stop_exc:  # pragma: no cover - 关闭路径异常 logging
                logger.exception(
                    "process_lifecycle_stop_failed",
                    extra={
                        "event": "process_lifecycle_stop_failed",
                        "process_role": role,
                        "error_type": type(stop_exc).__name__,
                        "error": str(stop_exc),
                    },
                )
            finally:
                if not lease_released:
                    # stop 未到安全释放点时只停止续租，让 Redis TTL 保持 fencing；
                    # 绝不在仍可能运行 publisher/worker 时主动删除 ownership。
                    await _stop_runtime_ready_lease_task(
                        lease_task=readiness_lease_task,
                        stop_event=readiness_lease_stop,
                    )
        elif readiness_hot_state is not None:
            # build_runtime 在 PROVISIONING claim 后失败时还没有完整 runtime
            # 可供确认 bus/worker 已停。watchdog 已存在说明 claim 后进入了受保护
            # build 区：此时绝不能 delete ownership 或 disarm，必须保留 TTL
            # fencing 并让 sticky fatal watchdog 强退。只有 watchdog 尚未创建的
            # pre-I/O 失败（例如构造 watchdog 失败）才可 owner-aware 撤销。
            await _stop_runtime_ready_lease_task(
                lease_task=readiness_lease_task,
                stop_event=readiness_lease_stop,
            )
            readiness_delivery_gate.abort()
            if readiness_deadline_watchdog is None:
                await _withdraw_runtime_ready(
                    lease=readiness_lease,
                    hot_state_store=readiness_hot_state,
                    logger=logger,
                )
            await readiness_hot_state.close()
        # 心跳 task 最后停 —— 确保 stop_background_tasks 整个期间 docker 仍能
        # 探到 healthy。心跳文件不主动删，留最后一次 mtime 给 docker stop 兜底。
        if heartbeat_task is not None:
            heartbeat_stop.set()
            try:
                await asyncio.wait_for(heartbeat_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                heartbeat_task.cancel()
            except Exception:  # pragma: no cover - 心跳收尾异常
                pass


def run_process_sync(
    *,
    process_role: str,
    app_name: str,
    extra_setup: Callable[[ApplicationRuntime], Awaitable[None]] | None = None,
) -> int:
    """同步包装：用 asyncio.run 启动 run_process，供 if __name__ == "__main__" 路径调用。

    返回值会作为 sys.exit(...) 的参数。
    """
    return asyncio.run(
        run_process(
            process_role=process_role,
            app_name=app_name,
            extra_setup=extra_setup,
        )
    )
