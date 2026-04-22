"""2026-04-22 LF-014 anchor · OKX WS 连续失败阈值 escalation。

## 背景

OKX 公开 WS 断连后本来就有指数退避重连。但**持续连不上**（例如 OKX 侧
长时间挂了、网络问题）时，运维人员不能只靠逐条 okx_ws_error 日志
判断 "这是偶发还是持续问题"。

## 不做 REST fallback（设计取舍）

- REST 拉数据和 WS 推数据语义不同（latency / frequency / schema）
- 切 REST 的下游 feature engine 期望 WS 节奏
- 当前 1-symbol 规模，持续 WS 失败 = 停交易 + page operator 才对
- 所以本 fix 只升级 observability，不动执行语义

## 本测试锁定

1. `_WS_CIRCUIT_OPEN_CONSECUTIVE_FAILURES = 10` 常量存在
2. 常量 > 0（0 会立刻触发假告警）
3. 常量 < 100（避免要等几十次失败才发警）

实际 "10 次连续失败→升级 critical" 的行为逻辑依赖活 WS 连接，不在
unit test 层覆盖；留给 integration test。本测试保护常量语义不被改乱。
"""
from __future__ import annotations

import unittest

from aats.services.market_gateway.okx_websocket import OKXPublicWebSocketClient


class TestWsCircuitOpenThreshold(unittest.TestCase):
    def test_threshold_constant_present(self) -> None:
        self.assertTrue(
            hasattr(OKXPublicWebSocketClient, "_WS_CIRCUIT_OPEN_CONSECUTIVE_FAILURES"),
            "circuit_open threshold 常量必须存在（LF-014 anchor）",
        )

    def test_threshold_is_positive_int(self) -> None:
        value = OKXPublicWebSocketClient._WS_CIRCUIT_OPEN_CONSECUTIVE_FAILURES
        self.assertIsInstance(value, int)
        self.assertGreater(value, 0, "阈值必须 > 0，否则第一次 error 就告警")

    def test_threshold_reasonable_bound(self) -> None:
        """10 次重连失败 × 平均 ~30s 退避 = ~5 min，合理 page operator 时机。
        不要改到 > 50（半小时以上才告警）或 < 3（偶发抖动也告警）。"""
        value = OKXPublicWebSocketClient._WS_CIRCUIT_OPEN_CONSECUTIVE_FAILURES
        self.assertGreaterEqual(value, 3)
        self.assertLessEqual(value, 50)

    def test_circuit_open_event_name_stable(self) -> None:
        """source-level 校验: _consume 里用 "okx_ws_circuit_open" 作为 event name，
        Grafana alert 里的 promql / logql 按这个名字索引，改了必须同步。"""
        import inspect

        src = inspect.getsource(OKXPublicWebSocketClient._consume)
        self.assertIn(
            "okx_ws_circuit_open",
            src,
            "circuit_open event name 被改了 —— Grafana alert 规则也要同步改",
        )
        self.assertIn(
            "okx_ws_circuit_closed",
            src,
            "circuit 恢复事件也是 observability 需要的",
        )


if __name__ == "__main__":
    unittest.main()
