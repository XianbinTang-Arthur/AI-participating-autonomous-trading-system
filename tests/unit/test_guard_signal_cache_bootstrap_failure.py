"""2026-04-21 C2 anchor test #3 · GuardSignalHotStateCache bootstrap 失败的 fail-closed 证明。

## 背景

audit agent 发现：``GuardSignalHotStateCache.bootstrap()`` 里对 Redis 的
``get()`` 调用有 try/except（见 guard_signal_cache.py:167-176），成功时
恢复 snapshot、失败时 log warning 继续。但**没有明确测试证明"bootstrap
失败后下一次 snapshot() 会返回 _FAIL_CLOSED_SENTINEL"**。

这是个 defensive gap：已有 `test_bootstrap_with_empty_redis` 覆盖
"Redis 空但 get 成功"的场景；缺"Redis get() 抛异常"的场景。

## 本测试锁定

1. bootstrap 里 Redis.get() 抛异常 → cache 仍 bootstrapped=True，
   `_latest` 保持空 dict（不会崩）
2. 随后 snapshot() 返回 `_FAIL_CLOSED_SENTINEL`（只减仓 + safe_to_trade=False）
3. 返回的 sentinel 能被 RiskEngine 识别进入硬拒路径（通过检查字段
   `only_reduce_required=True` 和 `only_reduce_reasons` 非空）

## 为什么这个测试重要

在生产中，Redis 暂时断连（主从切换、配置热重载、网络抖动）是真实可
能事件。此时 bootstrap 如果返回"silently 静默成功但没数据"，RiskEngine
必须**认定 guard 失效 → 只减仓**，而不是"没数据 = 默认安全"。

`_FAIL_CLOSED_SENTINEL` 的存在就是为了这个场景（见 guard_signal_cache.py
doc string "注意：之前返回空 dict {}，RiskEngine 对空 dict 的所有 .get()
默认值都是 permissive ..."）。本测试证明这条路径真的工作。
"""
from __future__ import annotations

import logging
import unittest

from aats.services.governance_engine.guard_signal_cache import (
    GuardSignalHotStateCache,
    _FAIL_CLOSED_SENTINEL,
)
from aats.storage.hot_state_store import InMemoryHotStateStore


def _make_logger() -> logging.Logger:
    return logging.getLogger("test_guard_signal_cache_bootstrap_failure")


class _ExplodingHotStateStore(InMemoryHotStateStore):
    """HotStateStore 子类：get() 时一律抛异常，模拟 Redis 断连/协议错误等。"""

    def __init__(self, *, exception_type: type[Exception] = RuntimeError, message: str = "redis_connection_refused") -> None:
        super().__init__()
        self._exception_type = exception_type
        self._message = message

    async def get(self, key: str):  # type: ignore[override]
        raise self._exception_type(self._message)


class _TimeoutHotStateStore(InMemoryHotStateStore):
    """模拟 Redis 超时（TimeoutError，更贴近真实故障）。"""

    async def get(self, key: str):  # type: ignore[override]
        raise TimeoutError("redis_get_timeout_5s")


class TestBootstrapFailureFallsBackToSentinel(unittest.IsolatedAsyncioTestCase):
    """bootstrap 里 Redis.get() 抛异常时的 fail-closed 链路。"""

    async def test_redis_get_exception_still_marks_bootstrapped(self) -> None:
        """get() 抛异常 → bootstrap 不 raise，cache.bootstrapped=True。

        这是契约：bootstrap 不应把 Redis 失败往上抛，因为系统必须能在
        degraded 模式下继续提供 fail-closed snapshot。
        """
        cache = GuardSignalHotStateCache(
            signal_name="derivatives_live",
            logger=_make_logger(),
        )
        store = _ExplodingHotStateStore()
        # 关键断言：不 raise
        await cache.bootstrap(hot_state_store=store, process_role="decision")
        self.assertTrue(
            cache.bootstrapped,
            "Redis 抛异常时 bootstrap 仍应 mark bootstrapped=True（不把故障往上抛）",
        )

    async def test_redis_get_exception_keeps_latest_empty(self) -> None:
        """get() 异常 → _latest 没被污染（仍是空 dict）。"""
        cache = GuardSignalHotStateCache(
            signal_name="derivatives_live",
            logger=_make_logger(),
        )
        store = _ExplodingHotStateStore()
        await cache.bootstrap(hot_state_store=store, process_role="decision")
        # _latest 应该是空 —— 这驱动了下一步 snapshot 返回 sentinel
        self.assertEqual(cache._latest, {})

    async def test_redis_get_exception_then_snapshot_returns_fail_closed_sentinel(self) -> None:
        """★ 核心不变性：bootstrap 失败 → snapshot() 返回 _FAIL_CLOSED_SENTINEL。

        这是审计的关键断言：Redis 坏掉时 RiskEngine 看到的 snapshot 必须
        包含 `only_reduce_required=True` 和 `only_reduce_reasons` 非空，
        以便硬拒绝 open 新仓。
        """
        cache = GuardSignalHotStateCache(
            signal_name="derivatives_live",
            logger=_make_logger(),
        )
        store = _ExplodingHotStateStore()
        await cache.bootstrap(hot_state_store=store, process_role="decision")

        snapshot = cache.snapshot()

        # 核心：只减仓 + 不安全交易
        self.assertTrue(
            snapshot["only_reduce_required"],
            "Redis 失败后 snapshot 必须 only_reduce_required=True（fail-closed）",
        )
        self.assertFalse(
            snapshot["safe_to_trade"],
            "Redis 失败后 safe_to_trade 必须是 False",
        )
        # only_reduce_reasons 必须非空（RiskEngine 路径 A 硬拒需要非空 list）
        self.assertIsInstance(snapshot.get("only_reduce_reasons"), list)
        self.assertGreater(
            len(snapshot["only_reduce_reasons"]),
            0,
            "only_reduce_reasons 必须非空，否则 RiskEngine 的硬拒路径不会触发",
        )
        # 状态标签
        self.assertEqual(snapshot.get("status"), "stale")

    async def test_redis_timeout_also_falls_back_to_sentinel(self) -> None:
        """不仅 RuntimeError，TimeoutError 等其他异常也应走同一路径。"""
        cache = GuardSignalHotStateCache(
            signal_name="recovery",
            logger=_make_logger(),
        )
        store = _TimeoutHotStateStore()
        await cache.bootstrap(hot_state_store=store, process_role="decision")
        snapshot = cache.snapshot()
        self.assertTrue(snapshot["only_reduce_required"])
        self.assertFalse(snapshot["safe_to_trade"])

    async def test_callable_interface_also_returns_sentinel_after_failure(self) -> None:
        """__call__ 是 recovery_status_provider 的接口，行为必须和 snapshot() 一致。"""
        cache = GuardSignalHotStateCache(
            signal_name="recovery",
            logger=_make_logger(),
        )
        store = _ExplodingHotStateStore()
        await cache.bootstrap(hot_state_store=store, process_role="decision")

        via_snapshot = cache.snapshot()
        via_callable = cache()  # __call__

        self.assertEqual(via_snapshot["only_reduce_required"], via_callable["only_reduce_required"])
        self.assertEqual(via_snapshot["safe_to_trade"], via_callable["safe_to_trade"])
        self.assertEqual(via_snapshot["status"], via_callable["status"])

    async def test_snapshot_before_bootstrap_returns_sentinel(self) -> None:
        """极端场景：cache 创建但从未 bootstrap 也要 fail-closed。

        保护：任何代码路径意外提前访问 snapshot（例如 fixture 顺序问题、
        early import-time 调用）都不应 permissive。
        """
        cache = GuardSignalHotStateCache(
            signal_name="derivatives_live",
            logger=_make_logger(),
        )
        # 没调 bootstrap
        self.assertFalse(cache.bootstrapped)
        snapshot = cache.snapshot()
        self.assertTrue(snapshot["only_reduce_required"])
        self.assertFalse(snapshot["safe_to_trade"])

    async def test_sentinel_matches_module_constant(self) -> None:
        """验证 sentinel 返回的字段与模块级 _FAIL_CLOSED_SENTINEL 一致。

        防止未来有人在 snapshot() 里魔改返回值但忘了更新 _FAIL_CLOSED_SENTINEL
        （或反之），导致两处字段漂移。
        """
        cache = GuardSignalHotStateCache(
            signal_name="derivatives_live",
            logger=_make_logger(),
        )
        store = _ExplodingHotStateStore()
        await cache.bootstrap(hot_state_store=store, process_role="decision")

        snapshot = cache.snapshot()

        for key, expected_value in _FAIL_CLOSED_SENTINEL.items():
            self.assertIn(key, snapshot, f"sentinel 缺字段 {key}")
            self.assertEqual(
                snapshot[key],
                expected_value,
                f"字段 {key} 值偏离 _FAIL_CLOSED_SENTINEL: got {snapshot[key]!r}, "
                f"expected {expected_value!r}",
            )


class TestFreshDataNotSentinel(unittest.IsolatedAsyncioTestCase):
    """反向 smoke：Redis 正常 + 有新鲜数据时 **不能** 返回 sentinel。

    否则 fail-closed 就变成 fail-always，系统永远不交易了。
    """

    async def test_fresh_bootstrap_with_valid_data_returns_actual_data(self) -> None:
        store = InMemoryHotStateStore()

        # 先让一个 writer publish 一条新鲜快照
        writer = GuardSignalHotStateCache(
            signal_name="derivatives_live",
            logger=_make_logger(),
        )
        await writer.bootstrap(hot_state_store=store, process_role="execution")
        await writer.publish(
            {
                "status": "healthy",
                "only_reduce_required": False,
                "safe_to_trade": True,
                "auto_halt_required": False,
            }
        )

        # reader bootstrap
        reader = GuardSignalHotStateCache(
            signal_name="derivatives_live",
            logger=_make_logger(),
        )
        await reader.bootstrap(hot_state_store=store, process_role="decision")

        snapshot = reader.snapshot()
        # 确认真的拿到了 healthy 数据，不是 sentinel
        self.assertEqual(snapshot["status"], "healthy")
        self.assertFalse(snapshot["only_reduce_required"])
        self.assertTrue(snapshot["safe_to_trade"])
        self.assertNotIn("_stale", snapshot, "fresh data 不应带 _stale marker")


if __name__ == "__main__":
    unittest.main()
