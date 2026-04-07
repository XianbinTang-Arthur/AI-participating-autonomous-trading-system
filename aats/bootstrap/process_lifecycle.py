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
"""
from __future__ import annotations

import asyncio
import signal
import sys
from typing import Awaitable, Callable

from aats.bootstrap.config import ApplicationRuntime, build_runtime, load_settings
from aats.bootstrap.logging import configure_logging_for_settings, get_logger
from aats.bootstrap.settings import ALLOWED_PROCESS_ROLES, AATSSettings


# 跨进程 entry 共享的 logger 命名空间。每个 entry 自己再 get 一个细分 logger。
_LIFECYCLE_LOGGER = "aats.bootstrap.process_lifecycle"


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
    try:
        runtime = await build_runtime(effective_settings, process_role=role)
        await runtime.start_background_tasks()
        if extra_setup is not None:
            await extra_setup(runtime)

        # 注册信号 + 等待 stop。stop_event 在测试里可以预先注入并提前 set 来跳过等待。
        local_stop = stop_event if stop_event is not None else asyncio.Event()
        if stop_event is None:
            _install_shutdown_signals(stop_event=local_stop, logger=logger)

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
