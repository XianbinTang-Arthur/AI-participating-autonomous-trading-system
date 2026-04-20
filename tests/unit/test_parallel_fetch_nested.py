"""S4 嵌套守卫重写的契约测试。

验证 gateway_slow_query_systematic_fix_sow.md §S4 引入的改动：

- 外层 (depth=0)：共享池，fan-out 并行
- 嵌套 (depth=1)：本地小池（非共享池），fan-out 依然并行
- 3 层嵌套 (depth≥2)：串行降级，避免线程无限膨胀
- 异常路径还原 depth
- 空输入 / 单元素短路保持原语义

原实现对 depth=1 直接串行化，让 build_recovery_view / build_system_mode
这些"一个 panel 内部并行 9 路子查询"的场景从 3s 退化到 18s。
新实现让 depth=1 走本地小池，收益回来。
"""
from __future__ import annotations

import threading
import time
import unittest

from aats.services.operator._parallel import (
    _INNER_MAX_WORKERS,
    _SHARED_MAX_WORKERS,
    _nesting_guard,
    parallel_fetch,
)


def _sleep_and_return(duration: float, value: str) -> str:
    time.sleep(duration)
    return value


class TestParallelFetchShortCircuits(unittest.TestCase):
    def test_empty_returns_empty(self) -> None:
        self.assertEqual(parallel_fetch({}), {})

    def test_single_item_runs_inline(self) -> None:
        # 单元素路径不起线程池
        result = parallel_fetch({"x": lambda: 42})
        self.assertEqual(result, {"x": 42})


class TestParallelFetchOuterParallelism(unittest.TestCase):
    def test_outer_fanout_is_parallel(self) -> None:
        """外层 depth=0：4 个 100ms 任务并行，总时延 < 2 × 单任务。

        若串行则 wall ≈ 400ms；若并行 wall ≈ 100ms。留 200ms 阈值。
        """
        t0 = time.monotonic()
        result = parallel_fetch({
            "a": lambda: _sleep_and_return(0.1, "a"),
            "b": lambda: _sleep_and_return(0.1, "b"),
            "c": lambda: _sleep_and_return(0.1, "c"),
            "d": lambda: _sleep_and_return(0.1, "d"),
        })
        wall = time.monotonic() - t0
        self.assertEqual(result, {"a": "a", "b": "b", "c": "c", "d": "d"})
        self.assertLess(wall, 0.2, f"expected parallel, got wall={wall:.3f}s")


class TestParallelFetchNestedParallelism(unittest.TestCase):
    def test_one_level_nested_is_still_parallel(self) -> None:
        """depth=1 走本地小池，fan-out 仍并行。

        这是 S4 改动的核心验证：原实现串行，新实现并行。
        """
        inner_wall_holder: dict[str, float] = {}

        def outer_task_that_fans_out_inner() -> str:
            t0 = time.monotonic()
            r = parallel_fetch({
                "i1": lambda: _sleep_and_return(0.1, "i1"),
                "i2": lambda: _sleep_and_return(0.1, "i2"),
                "i3": lambda: _sleep_and_return(0.1, "i3"),
            })
            inner_wall_holder["wall"] = time.monotonic() - t0
            return "".join(r.values())

        # 外层触发 depth=0 → worker 线程里跑 outer_task → 内部调
        # parallel_fetch → 此时 depth=1 → 必须走本地小池并行
        parallel_fetch({"outer": outer_task_that_fans_out_inner})

        inner_wall = inner_wall_holder["wall"]
        self.assertLess(
            inner_wall, 0.2,
            f"S4 regression: depth=1 fan-out should be parallel via inner pool, "
            f"got serial wall={inner_wall:.3f}s",
        )

    def test_two_levels_nested_degrades_to_serial(self) -> None:
        """depth≥2 串行降级：3 个 50ms 任务应 ≥ 3 × 单任务时延。

        避免线程总数在 3 层嵌套时无界膨胀。

        注意：每一层必须有至少 2 个任务，否则走 ``len(callables)==1`` 短路
        直接 inline 执行，depth 不会被抬高，测试层级失效。
        """
        level3_wall_holder: dict[str, float] = {}

        def level3_fan_out() -> str:
            t0 = time.monotonic()
            r = parallel_fetch({
                "x": lambda: _sleep_and_return(0.05, "x"),
                "y": lambda: _sleep_and_return(0.05, "y"),
                "z": lambda: _sleep_and_return(0.05, "z"),
            })
            level3_wall_holder["wall"] = time.monotonic() - t0
            return "".join(r.values())

        def level2_worker() -> str:
            # 2 个 key 确保 parallel_fetch 走 worker thread → depth=2
            return parallel_fetch({
                "l3": level3_fan_out,
                "l3_sibling": lambda: "sib",
            })["l3"]

        # 2 个 key 确保 parallel_fetch 走 worker thread → depth=1
        parallel_fetch({
            "l2": level2_worker,
            "l2_sibling": lambda: "sib",
        })

        level3_wall = level3_wall_holder["wall"]
        self.assertGreaterEqual(
            level3_wall, 0.14,
            f"depth=2 should serialize 3 × 50ms tasks, got wall={level3_wall:.3f}s",
        )


class TestParallelFetchDepthLifecycle(unittest.TestCase):
    """验证 _nesting_guard.depth 状态机：调用前后主线程 depth 为 0。"""

    def setUp(self) -> None:
        # 主线程进入测试时不应有残留 depth
        if hasattr(_nesting_guard, "depth"):
            _nesting_guard.depth = 0

    def test_main_thread_depth_is_zero_before_and_after(self) -> None:
        self.assertEqual(getattr(_nesting_guard, "depth", 0), 0)
        parallel_fetch({
            "a": lambda: "a",
            "b": lambda: "b",
        })
        self.assertEqual(getattr(_nesting_guard, "depth", 0), 0)

    def test_worker_thread_depth_is_one_inside_outer(self) -> None:
        captured_depth: list[int] = []

        def peek() -> str:
            captured_depth.append(getattr(_nesting_guard, "depth", 0))
            return "ok"

        parallel_fetch({"a": peek, "b": peek})
        # 两个 worker 各自看到 depth=1
        self.assertEqual(captured_depth, [1, 1])

    def test_worker_thread_depth_is_two_inside_nested(self) -> None:
        captured_depth: list[int] = []

        def inner_peek() -> str:
            captured_depth.append(getattr(_nesting_guard, "depth", 0))
            return "ok"

        def outer_fan_out() -> str:
            parallel_fetch({"i1": inner_peek, "i2": inner_peek})
            return "ok"

        # 外层 ≥2 个 key，否则走 len==1 短路在主线程 inline 跑，
        # 内层进入时 depth 仍是 0 而非预期的 1
        parallel_fetch({
            "outer": outer_fan_out,
            "outer_sibling": lambda: "sib",
        })
        # 两个内层 worker 各自看到 depth=2
        self.assertEqual(captured_depth, [2, 2])


class TestParallelFetchExceptionPath(unittest.TestCase):
    def test_exception_restores_depth_on_outer_worker(self) -> None:
        """worker 抛异常时，depth 必须还原（避免 thread pool worker 复用时残留）。

        ThreadPoolExecutor 会复用 worker 线程，如果不还原 depth，下一批任务
        会误判嵌套深度。
        """
        executor_thread_ids: set[int] = set()
        depths_seen_after_exception: list[int] = []

        def probe_then_raise() -> str:
            executor_thread_ids.add(threading.get_ident())
            raise RuntimeError("intentional")

        def probe_depth() -> int:
            executor_thread_ids.add(threading.get_ident())
            return getattr(_nesting_guard, "depth", -1)

        with self.assertRaises(RuntimeError):
            parallel_fetch({"fail": probe_then_raise, "ok": lambda: "ok"})

        # 再跑一次，worker 复用时 depth 应该是 1（重新进入），而不是残留的值
        def recheck() -> str:
            depths_seen_after_exception.append(getattr(_nesting_guard, "depth", -1))
            return "ok"

        parallel_fetch({"a": recheck, "b": recheck})
        self.assertEqual(
            depths_seen_after_exception, [1, 1],
            f"exception path failed to restore depth; second run saw "
            f"{depths_seen_after_exception} instead of [1, 1]",
        )

    def test_exception_propagates(self) -> None:
        def bad() -> str:
            raise ValueError("boom")

        with self.assertRaises(ValueError):
            parallel_fetch({"a": bad, "b": lambda: "ok"})


class TestParallelFetchConfigConstants(unittest.TestCase):
    """防止后续人员下意识把常量改小/改大而不懂后果的"锚点测试"。"""

    def test_shared_max_workers_matches_sow_contract(self) -> None:
        # SOW §3 & §4.S4：共享池 12 workers
        self.assertEqual(_SHARED_MAX_WORKERS, 12)

    def test_inner_max_workers_matches_sow_contract(self) -> None:
        # SOW §4.S4.3：本地小池 4 workers（配合 DB pool 15+45=60）
        self.assertEqual(_INNER_MAX_WORKERS, 4)


if __name__ == "__main__":
    unittest.main()
