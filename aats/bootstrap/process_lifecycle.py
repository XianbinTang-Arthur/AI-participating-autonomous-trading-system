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

跨平台说明：
* Linux 上用 loop.add_signal_handler 注册 SIGTERM/SIGINT 是 asyncio 推荐路径。
* Windows 上 add_signal_handler 不可用（NotImplementedError），降级用
  signal.signal()。Windows 不是生产平台，但本地开发与单元测试会跑到，
  所以必须 fail-soft。

Readiness barrier (B1, 2026-04-20)：
* build_runtime 完成后（subscribe 全部就位）与 start_background_tasks 前
  （publisher 启动）之间，加一层跨进程 readiness gate：
    1. 本进程写 Redis key aats:runtime:ready:{role}
    2. 阻塞等 peer roles 都写完自己的 key（或超时 fallback）
* 目的：让 market 等 publisher 只有在 decision/execution/gateway 的 durable
  consumer 创建完成后才开始 publish。这是 nats_retention_global_architecture_sow.md
  §B1 对 INTEREST retention 切换的硬前置——INTEREST 下 publish 发生在
  consumer 就位前就会消息丢失。
* 超时 fallback 不硬失败，保持当前 LIMITS 模式下的向前兼容。
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Sequence

from aats.bootstrap.config import ApplicationRuntime, build_runtime, load_settings
from aats.bootstrap.logging import configure_logging_for_settings, get_logger
from aats.bootstrap.settings import ALLOWED_PROCESS_ROLES, AATSSettings
from aats.storage.hot_state_store import HotStateStore


# 跨进程 entry 共享的 logger 命名空间。每个 entry 自己再 get 一个细分 logger。
_LIFECYCLE_LOGGER = "aats.bootstrap.process_lifecycle"


# ────────────────────────────────────────────────────────────────
# Readiness barrier (B1)
# ────────────────────────────────────────────────────────────────

# Redis key 前缀：每个进程 build_runtime 完成后（subscribe 全部就位）写此 key。
# 其他进程的 publisher 启动前阻塞读此 key 确认 peer 就位。
_RUNTIME_READY_KEY_PREFIX = "aats:runtime:ready:"

# Ready key TTL：防止进程异常退出后 key 残留让新启动进程误判。5 分钟够覆盖
# 正常 startup window（build_runtime 通常 < 30s），又不会让"僵尸 key"挡新起
# 进程超过一个部署周期。
_RUNTIME_READY_TTL_SECONDS: float = 300.0

# 轮询 peer ready 的间隔。50ms~1s 之间折中：太密 Redis QPS 浪费，太稀 startup
# 延迟感知慢。500ms 对 startup order 影响 < 1s，可接受。
_PEER_READY_POLL_INTERVAL_SECONDS: float = 0.5

# Peer ready 等待超时。超时后 publisher 不再阻塞继续启动——这是 fallback 保护
# 模式，保持 LIMITS retention 下的向前兼容（消息不会丢，只是 consumer 落后
# 几秒）。INTEREST 切换后超时会真丢消息，但 60s 窗口足够覆盖 build_runtime
# 的 slice builder + cache hydrate 全流程（实测 10-30s）。
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


def _ready_key(role: str) -> str:
    return f"{_RUNTIME_READY_KEY_PREFIX}{role}"


async def _announce_runtime_ready(
    *,
    role: str,
    hot_state_store: HotStateStore | None,
    logger,
) -> None:
    """把本进程的 ready 信号写到 Redis，让其他 peer 能看到。

    在 build_runtime 完成（subscribe 已全部就位）后调用。hot_state_store
    为 None 时是 InMemory 单元测试场景，直接 no-op。
    """
    if hot_state_store is None:
        logger.info(
            "runtime_ready_gate_skipped_no_hot_state",
            extra={
                "event": "runtime_ready_gate_skipped_no_hot_state",
                "process_role": role,
            },
        )
        return
    value = {
        "process_role": role,
        "ready_ts": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
    }
    try:
        await hot_state_store.set(
            _ready_key(role),
            value,
            ttl_seconds=_RUNTIME_READY_TTL_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "runtime_ready_gate_announce_failed",
            extra={
                "event": "runtime_ready_gate_announce_failed",
                "process_role": role,
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )
        return
    logger.info(
        "runtime_ready_gate_announced",
        extra={
            "event": "runtime_ready_gate_announced",
            "process_role": role,
        },
    )


async def _wait_for_peer_roles_ready(
    *,
    role: str,
    hot_state_store: HotStateStore | None,
    logger,
    peers: Sequence[str] | None = None,
    timeout_seconds: float = _PEER_READY_TIMEOUT_SECONDS,
    poll_interval: float = _PEER_READY_POLL_INTERVAL_SECONDS,
) -> None:
    """阻塞等 peer role 都写完自己的 ready key。

    超时仍未就位时不硬失败，日志 warning 后返回——fallback 到 LIMITS
    retention 的向前兼容行为。

    ``peers`` 默认读 ``_PEER_READINESS_MAP[role]``；测试可手动注入。
    ``hot_state_store`` 为 None 时直接返回（InMemory 场景 / 单进程 smoke）。
    """
    if hot_state_store is None:
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
        },
    )
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            ready_values = await hot_state_store.get_many(
                [_ready_key(p) for p in peer_list]
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "runtime_ready_gate_poll_failed",
                extra={
                    "event": "runtime_ready_gate_poll_failed",
                    "process_role": role,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
            return
        missing = [p for p in peer_list if not ready_values.get(_ready_key(p))]
        if not missing:
            logger.info(
                "runtime_ready_gate_all_peers_ready",
                extra={
                    "event": "runtime_ready_gate_all_peers_ready",
                    "process_role": role,
                    "peers": peer_list,
                    "elapsed_seconds": round(
                        time.monotonic() - (deadline - timeout_seconds), 2
                    ),
                },
            )
            return
        if time.monotonic() >= deadline:
            logger.warning(
                "runtime_ready_gate_timeout",
                extra={
                    "event": "runtime_ready_gate_timeout",
                    "process_role": role,
                    "missing_peers": missing,
                    "timeout_seconds": timeout_seconds,
                },
            )
            return
        await asyncio.sleep(poll_interval)


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


async def _heartbeat_loop(
    role: str,
    *,
    stop_event: asyncio.Event,
    logger,
    interval: float = HEARTBEAT_INTERVAL_SECONDS,
    base_dir: Path | None = None,
) -> None:
    """周期性 touch 心跳文件，直到 stop_event 被 set。

    设计要点：
    * 第一次 touch 在 loop 开头立即做，避免 healthcheck 在 start_period 内
      探到旧文件 / 不存在。
    * 用 mtime 而非内容：docker healthcheck 用 stat 比较 mtime，最小开销。
    * stop_event set 后 loop 退出，文件**不**主动删除——保留最后一次 mtime
      让 docker stop 期间的最后一两次探活仍能拿到 healthy；docker rm 时容器
      被销毁，/tmp 自然清掉。
    * 任何 IO 异常都 swallow 并日志告警，不让心跳故障打死 daemon 进程。
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
        return

    logger.info(
        "process_lifecycle_heartbeat_started",
        extra={
            "event": "process_lifecycle_heartbeat_started",
            "process_role": role,
            "path": str(path),
            "interval_seconds": interval,
        },
    )

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
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue


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
    # 心跳 stop_event 与 run_process stop_event 分开：心跳必须在
    # stop_background_tasks 期间继续打，让 docker 看到容器仍在干净退出而非挂死。
    heartbeat_stop = asyncio.Event()
    try:
        runtime = await build_runtime(effective_settings, process_role=role)
        # ── Readiness barrier (B1) ─────────────────────────────
        # build_runtime 内部已做 _wire_event_subscriptions，本进程的 durable
        # consumer 已在 NATS server 注册。现在：
        #   (1) 写 Redis 告诉其他 peer "我准备好了"
        #   (2) 阻塞等其他 peer 也准备好（或超时 fallback）
        # 再调 start_background_tasks 启动 publisher（见 SOW §B1）。
        # getattr 兜底：测试场景可能传 InMemoryApplicationRuntime，没有
        # hot_state_store。
        _hot_state = getattr(runtime, "hot_state_store", None)
        await _announce_runtime_ready(
            role=role,
            hot_state_store=_hot_state,
            logger=logger,
        )
        await _wait_for_peer_roles_ready(
            role=role,
            hot_state_store=_hot_state,
            logger=logger,
        )
        await runtime.start_background_tasks()
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
            _heartbeat_loop(role, stop_event=heartbeat_stop, logger=logger),
            name=f"aats-heartbeat-{role}",
        )

        logger.info(
            "process_lifecycle_ready",
            extra={
                "event": "process_lifecycle_ready",
                "process_role": role,
                "background_task_count": len(runtime.background_tasks),
            },
        )
        await local_stop.wait()
        logger.info(
            "process_lifecycle_stopping",
            extra={"event": "process_lifecycle_stopping", "process_role": role},
        )
        return 0
    except Exception as exc:  # pragma: no cover - 顶层异常 logging 路径
        # 记录异常然后让 finally 走干净停机；不直接 raise 避免 main 拿不到退出码。
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
        if runtime is not None:
            try:
                await runtime.stop_background_tasks()
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
