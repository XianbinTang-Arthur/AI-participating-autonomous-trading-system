"""面板子查询并行执行工具。

冷启动时各 build_*() 方法内 10-17 个互相独立的数据库/服务查询全部串行执行，
总时间 = 所有查询之和（18-20 秒）。本模块提供 parallel_fetch()，把独立查询
提交到线程池并行跑，总时间 ≈ 最慢单个查询（~3 秒）。

线程池策略
=========
使用模块级共享有界 executor（默认 12 workers），而非每次调用创建临时池。
dashboard_bundle 按 panel 做 asyncio.to_thread 并发，每个 panel 可能再调
parallel_fetch；如果每次调用创建 10 worker 的新池，7 个 panel 同时跑会
产生 70 个线程同时查 DB，容易超过连接池并变成排队。共享池自然限流。

嵌套检测：parallel_fetch 的 worker 线程内再次调用 parallel_fetch 时，
降级为串行执行，避免共享池内自身排队导致的吞吐倒退或饥饿。
"""
from __future__ import annotations

import logging
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

_logger = logging.getLogger("aats.operator_api.parallel")

# ── 共享有界线程池 ──────────────────────────────────────────────
_SHARED_MAX_WORKERS = 12
_shared_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()

# ── 嵌套检测 ────────────────────────────────────────────────────
_nesting_guard: threading.local = threading.local()


def _get_shared_executor() -> ThreadPoolExecutor:
    """Lazy-init module-level shared executor (double-checked locking)."""
    global _shared_executor
    if _shared_executor is None:
        with _executor_lock:
            if _shared_executor is None:
                _shared_executor = ThreadPoolExecutor(
                    max_workers=_SHARED_MAX_WORKERS,
                    thread_name_prefix="parallel_fetch",
                )
    return _shared_executor


def parallel_fetch(callables: dict[str, Callable[[], Any]], *, max_workers: int = 10) -> dict[str, Any]:
    """并行执行一组 {name: callable}，返回 {name: result}。

    每个 callable 在共享线程池中执行。如果某个 callable 抛异常，
    该异常会在 future.result() 时原样抛出，中断整个方法——
    与串行版本的行为一致（上层有 panel-level 异常捕获兜底）。

    ``max_workers`` 参数保留向后兼容；实际并发度由模块级共享池
    （_SHARED_MAX_WORKERS=12）控制，避免 dashboard bundle 多面板
    并发时按 panel×workers 放大线程数。

    嵌套检测：如果当前线程已在 parallel_fetch worker 内（例如某个
    panel 的 build_* 方法内又调了 parallel_fetch），自动降级为串行，
    防止共享池饥饿。
    """
    if not callables:
        return {}
    # 只有 1 个查询时不需要线程池开销
    if len(callables) == 1:
        name, fn = next(iter(callables.items()))
        return {name: fn()}

    # 嵌套检测：已在 parallel_fetch worker 线程内时降级串行，
    # 避免共享池自身排队导致吞吐倒退。
    if getattr(_nesting_guard, "active", False):
        _logger.debug(
            "parallel_fetch_nested_serial queries=%d",
            len(callables),
        )
        return {name: fn() for name, fn in callables.items()}

    wall_start = _time.monotonic()
    timings: dict[str, float] = {}

    def _timed(name: str, fn: Callable[[], Any]) -> Any:
        _nesting_guard.active = True
        t0 = _time.monotonic()
        try:
            return fn()
        finally:
            timings[name] = _time.monotonic() - t0
            _nesting_guard.active = False

    executor = _get_shared_executor()
    results: dict[str, Any] = {}
    futures = {executor.submit(_timed, name, fn): name for name, fn in callables.items()}
    for future in futures:
        name = futures[future]
        results[name] = future.result()

    wall_elapsed = _time.monotonic() - wall_start
    if wall_elapsed > 2.0:
        sorted_timings = sorted(timings.items(), key=lambda x: -x[1])
        parts = " ".join(f"{k}={v:.3f}s" for k, v in sorted_timings[:5])
        _logger.warning(
            "parallel_fetch_slow wall=%.3fs queries=%d top5=[%s]",
            wall_elapsed, len(callables), parts,
        )
    return results
