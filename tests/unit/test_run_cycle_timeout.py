"""2026-04-22 LF-003 anchor · DecisionOrchestrator.run_cycle 必须有全局 timeout。

## 背景

`run_cycle` 内部做多个 publish_model + asyncio.to_thread。NATS 背压、
JetStream 同步写慢、PG 锁等待都可能让某次 cycle 挂几十秒。trigger.py 的
`_timeframe_locks` 把同 (symbol, timeframe) 串行化 → 一个周期挂住 = 该
symbol+timeframe 后续决策全部卡队。

修复：`asyncio.wait_for(_run_cycle_body, timeout=30s)`。超时 → 走
`_publish_failure_best_effort` → trigger backoff 接管。

## 本测试锚定

1. `_RUN_CYCLE_TIMEOUT_SECONDS = 30.0`（改值必须同步改 api-client.js
   DEFAULT_TIMEOUT_MS 和 SOW）
2. `_run_cycle_body` 执行时间 > timeout → `run_cycle` raise TimeoutError
3. Timeout 触发 `_publish_failure_best_effort` 把孤儿事件标记成失败
4. Timeout 后 exception 上抛，不 silent swallow（trigger backoff 靠这个）

未来若有人删 `asyncio.wait_for` 或把 timeout 改得离谱（0 或 None），
本测试立即红。
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aats.services.decision_engine.orchestrator import DecisionOrchestrator


class TestRunCycleTimeoutConstant(unittest.TestCase):
    def test_timeout_constant_is_30_seconds(self) -> None:
        """anchor：timeout 必须 30s。改这个值需要同步改 SOW + 前端。"""
        self.assertEqual(
            DecisionOrchestrator._RUN_CYCLE_TIMEOUT_SECONDS,
            30.0,
            "_RUN_CYCLE_TIMEOUT_SECONDS=30s 是性能红线 (略低于前端 "
            "DEFAULT_TIMEOUT_MS=30s + retry buffer)。改值必须同步前端 + SOW。",
        )

    def test_timeout_constant_type_is_float(self) -> None:
        """timeout 必须是 float/int，不能是 None（那样就禁用了保护）。"""
        value = DecisionOrchestrator._RUN_CYCLE_TIMEOUT_SECONDS
        self.assertIsInstance(value, (int, float))
        self.assertGreater(value, 0, "timeout 必须 > 0，否则等同禁用")

    def test_timeout_less_than_frontend_30s(self) -> None:
        """timeout 应 ≤ 前端 30s 保证 trigger backoff 有机会接管。"""
        self.assertLessEqual(
            DecisionOrchestrator._RUN_CYCLE_TIMEOUT_SECONDS,
            30.0,
            "超过 30s 等于永远触发不了，前端先 abort 了",
        )


class TestRunCycleTimeoutBehavior(unittest.IsolatedAsyncioTestCase):
    """验证 run_cycle 实际行为：挂住的 _run_cycle_body 会被 timeout 砍掉。"""

    async def test_run_cycle_raises_timeout_when_body_hangs(self) -> None:
        """核心不变性：_run_cycle_body 挂住 → run_cycle 抛 TimeoutError。"""
        orch = DecisionOrchestrator.__new__(DecisionOrchestrator)
        orch.logger = MagicMock()
        orch.bus = MagicMock()

        # 替换 timeout 为短值让测试快速跑完
        with patch.object(DecisionOrchestrator, "_RUN_CYCLE_TIMEOUT_SECONDS", 0.1):
            hang_forever = AsyncMock()

            async def _hang(**kwargs):
                await asyncio.sleep(10)  # 远超 timeout
                return "never returns"

            hang_forever.side_effect = _hang
            orch._run_cycle_body = hang_forever

            # _publish_failure_best_effort 应被调，但不用做真事
            orch._publish_failure_best_effort = AsyncMock()

            with self.assertRaises(asyncio.TimeoutError):
                await orch.run_cycle(symbol="BTC-USDT-SWAP", timeframe="1m")

            # 验证 failure 有 publish（给 reconciliation 把孤儿事件标记 failed）
            orch._publish_failure_best_effort.assert_called_once()
            call_kwargs = orch._publish_failure_best_effort.call_args.kwargs
            self.assertEqual(call_kwargs["symbol"], "BTC-USDT-SWAP")
            self.assertEqual(call_kwargs["timeframe"], "1m")
            self.assertIsInstance(call_kwargs["exc"], asyncio.TimeoutError)

    async def test_run_cycle_returns_normally_when_body_fast(self) -> None:
        """正路径：_run_cycle_body 快速完成 → 正常返回 PositionTarget。"""
        orch = DecisionOrchestrator.__new__(DecisionOrchestrator)
        orch.logger = MagicMock()
        orch.bus = MagicMock()
        orch._publish_failure_best_effort = AsyncMock()

        expected_target = MagicMock(name="PositionTarget")
        fast_body = AsyncMock(return_value=expected_target)
        orch._run_cycle_body = fast_body

        # 用默认 timeout（30s）—— fast_body 应该瞬间完成
        result = await orch.run_cycle(symbol="BTC-USDT-SWAP", timeframe="1m")

        self.assertIs(result, expected_target)
        orch._publish_failure_best_effort.assert_not_called()

    async def test_non_timeout_exception_also_publishes_failure(self) -> None:
        """其他异常（非 timeout）也要 publish failure，不是只有 timeout 才 publish。

        防止 LF-003 的 fix 误伤既有 P1-11 的孤儿事件处理。
        """
        orch = DecisionOrchestrator.__new__(DecisionOrchestrator)
        orch.logger = MagicMock()
        orch.bus = MagicMock()
        orch._publish_failure_best_effort = AsyncMock()

        async def _raise_runtime(**kwargs):
            raise RuntimeError("body explodes")

        orch._run_cycle_body = AsyncMock(side_effect=_raise_runtime)

        with self.assertRaises(RuntimeError):
            await orch.run_cycle(symbol="BTC-USDT-SWAP", timeframe="1m")

        orch._publish_failure_best_effort.assert_called_once()
        call_kwargs = orch._publish_failure_best_effort.call_args.kwargs
        self.assertIsInstance(call_kwargs["exc"], RuntimeError)


if __name__ == "__main__":
    unittest.main()
