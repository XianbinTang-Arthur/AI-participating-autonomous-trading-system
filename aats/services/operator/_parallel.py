"""面板子查询并行执行工具。

冷启动时各 build_*() 方法内 10-17 个互相独立的数据库/服务查询全部串行执行，
总时间 = 所有查询之和（18-20 秒）。本模块提供 parallel_fetch()，把独立查询
提交到线程池并行跑，总时间 ≈ 最慢单个查询（~3 秒）。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable


def parallel_fetch(callables: dict[str, Callable[[], Any]], *, max_workers: int = 10) -> dict[str, Any]:
    """并行执行一组 {name: callable}，返回 {name: result}。

    每个 callable 在独立线程中执行。如果某个 callable 抛异常，
    该异常会在 future.result() 时原样抛出，中断整个方法——
    与串行版本的行为一致（上层有 panel-level 异常捕获兜底）。

    max_workers 默认 10：覆盖最大的 build_system_runtime 17 个查询
    中绝大多数是 I/O 等待型，10 线程足以饱和，不会给 DB 连接池造成压力。
    """
    if not callables:
        return {}
    # 只有 1 个查询时不需要线程池开销
    if len(callables) == 1:
        name, fn = next(iter(callables.items()))
        return {name: fn()}
    worker_count = min(len(callables), max_workers)
    results: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = {pool.submit(fn): name for name, fn in callables.items()}
        for future in futures:
            name = futures[future]
            results[name] = future.result()
    return results
