"""2026-04-21 A1 · okx_private_websocket keepalive task 静默死监控测试。

## 背景

`run_forever` 用 `asyncio.create_task(self._keepalive_loop(websocket))` 起
keepalive 任务，旧实现只在外层 finally 里 await 它。如果 _keepalive_loop
内部抛异常，task 会静默"死亡"（异常存在 task object 里但没人读），主循环
继续 `async for raw_message in websocket` ——ping 停发，OKX 30s 后 4004
side-close。这 30 秒我们丢 fill / balance 更新，对实盘账户很危险。

## 本文件测试的不变性

`OKXPrivateWebSocketClient._assert_keepalive_alive(task)`：
- task 还在跑 → 不抛
- task done 带异常 → RuntimeError 包装原异常 raise（__cause__ 指向原异常）
- task done 无异常（正常 return）→ RuntimeError raise（keepalive_loop 不该 return）
- task cancelled → 应当被视为 done 带异常，同样 raise（cancel 后 exception()
  会 raise CancelledError —— 这种情况只有我们自己主动 cancel 时发生，外层
  已在处理了；但 watchdog 仍然做 guard）

未来有人"优化"把 `_assert_keepalive_alive` 删掉或改成 silent log，本测试
立即报红。
"""
from __future__ import annotations

import asyncio
import unittest

from aats.services.execution_engine.okx_private_websocket import (
    OKXPrivateWebSocketClient,
)


class TestKeepaliveGuard(unittest.IsolatedAsyncioTestCase):
    async def test_alive_task_does_not_raise(self) -> None:
        """task 还在跑 → helper 静默返回。"""
        async def infinite_loop() -> None:
            while True:
                await asyncio.sleep(10)

        task = asyncio.create_task(infinite_loop())
        try:
            # 应当 silent return
            OKXPrivateWebSocketClient._assert_keepalive_alive(task)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def test_dead_task_with_exception_raises_wrapped_runtime_error(self) -> None:
        """task 抛异常死了 → helper raise RuntimeError, __cause__ 是原异常。"""
        async def boom() -> None:
            raise ValueError("simulated keepalive failure")

        task = asyncio.create_task(boom())
        # 等 task 跑完并把异常存进去
        await asyncio.sleep(0.05)
        self.assertTrue(task.done())

        with self.assertRaises(RuntimeError) as ctx:
            OKXPrivateWebSocketClient._assert_keepalive_alive(task)

        # 错误消息包含原异常类型
        self.assertIn("ValueError", str(ctx.exception))
        self.assertIn("simulated keepalive failure", str(ctx.exception))
        # __cause__ 链到原异常
        self.assertIsInstance(ctx.exception.__cause__, ValueError)
        self.assertEqual(
            str(ctx.exception.__cause__),
            "simulated keepalive failure",
        )

    async def test_dead_task_without_exception_raises(self) -> None:
        """task 正常 return 死了（不该发生，但仍然是 fatal） → helper raise。

        这条保护很重要：如果未来有人把 `_keepalive_loop` 改成带 break 提前
        return，主循环静默继续 = ping 停发 = 30s 后连接死 = 丢单。
        """
        async def return_too_early() -> None:
            return  # 立即返回，不抛异常

        task = asyncio.create_task(return_too_early())
        await asyncio.sleep(0.05)
        self.assertTrue(task.done())
        self.assertIsNone(task.exception())  # 真的没有异常

        with self.assertRaises(RuntimeError) as ctx:
            OKXPrivateWebSocketClient._assert_keepalive_alive(task)

        self.assertIn("completed unexpectedly", str(ctx.exception))

    async def test_cancelled_task_raises(self) -> None:
        """task 被 cancel → helper 也 raise。

        主循环只在 finally 里 cancel keepalive_task，所以 watchdog 看到
        cancelled 一般意味着外部（非主循环）干预。仍然视为 fatal 触发
        重连链路（新 keepalive 进来后 watchdog 继续守）。
        """
        async def infinite_loop() -> None:
            while True:
                await asyncio.sleep(10)

        task = asyncio.create_task(infinite_loop())
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self.assertTrue(task.done())
        self.assertTrue(task.cancelled())

        with self.assertRaises(RuntimeError):
            # cancelled task: task.exception() 会 raise CancelledError
            # _assert_keepalive_alive 里 keepalive_exc = task.exception() 本身
            # 会 raise —— 所以 RuntimeError 不一定包装 CancelledError，
            # 但调用方仍然看到一个 exception 逃出，触发重连路径
            OKXPrivateWebSocketClient._assert_keepalive_alive(task)


if __name__ == "__main__":
    unittest.main()
