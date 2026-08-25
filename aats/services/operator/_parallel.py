"""面板子查询并行执行工具。

冷启动时各 build_*() 方法内 10-17 个互相独立的数据库/服务查询全部串行执行，
总时间 = 所有查询之和（18-20 秒）。本模块提供 parallel_fetch()，把独立查询
提交到线程池并行跑，总时间 ≈ 最慢单个查询（~3 秒）。

线程池策略
=========
外层（depth=0）使用模块级共享有界 executor（默认 12 workers），而非每次调用
创建临时池。dashboard_bundle 按 panel 做 asyncio.to_thread 并发，每个 panel
可能再调 parallel_fetch；如果每次调用创建 10 worker 的新池，7 个 panel 同时
跑会产生 70 个线程同时查 DB，容易超过连接池并变成排队。共享池自然限流。

嵌套处理（S4 改动，2026-04-20，见
``docs/task/gateway_slow_query_systematic_fix_sow.md §S4``）：
-----------------------------------------------------------

历史上嵌套调用（一个 parallel_fetch worker 线程里再次调 parallel_fetch）
被无脑降级为**串行**执行。这是为了防止共享池内自己等自己饿死（外层占满
12 个 worker，每个 worker 再去 submit 任务到同一个共享池 → 新任务排队，
外层永远不 release slot，死锁）。

但生产中 ``build_recovery_view`` / ``build_system_mode`` 的外层会被一个
panel 调用，然后它内部调 ``blockers`` 作为一个子任务，``blockers`` 内部
又并行调 9 路 ``recovery / snapshot / latest_reconciliation ...``。这 9 路
全串行化后 wall=18s；如果能并行化 wall 能降到 3s。

新策略：**按嵌套深度分三档**。

- ``depth=0``（真正的外层，如 dashboard_bundle panel）：共享池（_SHARED_MAX_WORKERS=12）
- ``depth=1``（外层 worker 内第一次嵌套）：临时创建本地小池（_INNER_MAX_WORKERS=4）。
  不占共享池 slot，不和其他外层 panel 互相饥饿
- ``depth≥2``（内层再嵌套）：退化为串行。3 层嵌套在生产代码里应该极罕见，
  简化处理，避免线程总数无限膨胀

线程总数上限：
    共享池 12 × (1 + 本地小池 4) = 60

Gateway DB 连接池由 ``aats/storage/connection_budget.py`` 固定为 32 个声明上限。
线程数可以高于连接数；多余查询在 SQLAlchemy ``pool_timeout`` 处背压，避免四个
进程各自把线程上限等同于连接上限。目标负载下的超时率仍须单独验证。

异常安全：某个 future.result() 抛异常时，cancel 尚未启动的 pending
futures、等待已在运行的 futures 收敛，确保不会有残留任务占用共享池。
"""
from __future__ import annotations

import logging
import threading
import time as _time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable

_logger = logging.getLogger("aats.operator_api.parallel")

# ── 外层共享池：多 panel fan-out 时的限流阀 ────────────────────
_SHARED_MAX_WORKERS = 12
_shared_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()

# ── 内层本地池：嵌套 parallel_fetch 的隔离通道 ─────────────────
# 选 4 而不是 12 的理由：
# 1. 内层 fan-out 通常 3-9 路（recovery_view 9 路、system_mode 4 路），4 已够
#    让大部分嵌套场景并行化
# 2. 控制线程总数上限 = 共享池 12 × (1 + 本地小池 4) = 60；它可以高于
#    Gateway DB pool 的 32 上限，超额查询由 pool_timeout 背压而不是扩连接
_INNER_MAX_WORKERS = 4

# ── 嵌套检测（thread-local depth counter）────────────────────
# depth=0 表示当前线程不在任何 parallel_fetch worker 里（第一次调进来）
# depth=1 表示当前线程是外层 parallel_fetch 的 worker，再次调 parallel_fetch
#         会走本地小池
# depth=2+ 表示已经在内层 worker 里，再调 parallel_fetch 串行执行
_nesting_guard: threading.local = threading.local()

# ── 异常路径 drain 超时 ─────────────────────────────────────────
_DRAIN_TIMEOUT_SECONDS = 30


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


def _current_depth() -> int:
    return getattr(_nesting_guard, "depth", 0)


def parallel_fetch(callables: dict[str, Callable[[], Any]], *, max_workers: int = 10) -> dict[str, Any]:
    """并行执行一组 {name: callable}，返回 {name: result}。

    按嵌套深度选择执行模式：

    - 外层（depth=0）：共享池（12 workers），所有 panel fan-out 复用
    - 内层一次（depth=1）：本地小池（4 workers），不占共享池 slot
    - 内层二次+（depth≥2）：串行执行，避免线程无限膨胀

    ``max_workers`` 参数保留向后兼容，无实际作用。

    异常安全：某个 future 失败时 cancel 同批 pending futures、drain
    运行中 futures 再重抛，避免残留任务占满共享池。
    """
    if not callables:
        return {}
    # 只有 1 个查询时不需要线程池开销
    if len(callables) == 1:
        name, fn = next(iter(callables.items()))
        return {name: fn()}

    depth = _current_depth()

    if depth >= 2:
        # depth=2+ 已经在嵌套的嵌套里了（外层 worker → 内层 worker → 再次调
        # parallel_fetch）。继续创建新池会让线程总数无界。降级为串行。
        _logger.debug(
            "parallel_fetch_deep_nested_serial queries=%d depth=%d",
            len(callables), depth,
        )
        return {name: fn() for name, fn in callables.items()}

    if depth == 1:
        # 第一层嵌套：开本地小池。``with`` 块退出时池自动 shutdown，不会
        # 泄漏线程。
        inner_workers = min(_INNER_MAX_WORKERS, len(callables))
        _logger.debug(
            "parallel_fetch_nested_local queries=%d workers=%d",
            len(callables), inner_workers,
        )
        with ThreadPoolExecutor(
            max_workers=inner_workers,
            thread_name_prefix="parallel_fetch_inner",
        ) as inner:
            return _execute_with_executor(inner, callables, new_depth=2)

    # depth=0：外层走共享池
    executor = _get_shared_executor()
    return _execute_with_executor(executor, callables, new_depth=1)


def _execute_with_executor(
    executor: ThreadPoolExecutor,
    callables: dict[str, Callable[[], Any]],
    *,
    new_depth: int,
) -> dict[str, Any]:
    """在给定 executor 上并行跑 callables。

    ``new_depth`` 是 worker 线程开始执行时 ``_nesting_guard.depth`` 应被置
    成的值。外层调用传 1（让 worker 内再调 parallel_fetch 走本地小池），
    内层调用传 2（让 worker 内再调 parallel_fetch 走串行降级）。
    """
    wall_start = _time.monotonic()
    timings: dict[str, float] = {}

    def _timed(name: str, fn: Callable[[], Any]) -> Any:
        # 进 worker 线程时提升 depth；用 try/finally 保证异常路径也能还原。
        # worker 线程是从池里复用的，前一个任务可能已经把 depth 留在非零
        # 值，所以先记录 prev 再还原，而不是粗暴设回 0。
        prev_depth = _current_depth()
        _nesting_guard.depth = new_depth
        t0 = _time.monotonic()
        try:
            return fn()
        finally:
            timings[name] = _time.monotonic() - t0
            _nesting_guard.depth = prev_depth

    results: dict[str, Any] = {}
    futures: dict[Future[Any], str] = {
        executor.submit(_timed, name, fn): name for name, fn in callables.items()
    }
    try:
        for future in futures:
            name = futures[future]
            results[name] = future.result()
    except BaseException:
        # Cancel pending futures to free pool capacity.
        for f in futures:
            f.cancel()
        # Drain running tasks so they release their worker slot
        # before the exception propagates to the caller.
        for f in futures:
            if not f.cancelled():
                try:
                    f.result(timeout=_DRAIN_TIMEOUT_SECONDS)
                except Exception:
                    pass
        raise

    wall_elapsed = _time.monotonic() - wall_start
    if wall_elapsed > 2.0:
        sorted_timings = sorted(timings.items(), key=lambda x: -x[1])
        parts = " ".join(f"{k}={v:.3f}s" for k, v in sorted_timings[:5])
        _logger.warning(
            "parallel_fetch_slow wall=%.3fs queries=%d depth=%d top5=[%s]",
            wall_elapsed, len(callables), new_depth, parts,
        )
    return results
