"""R7-X1: ``_intent_from_state`` 方向字段恢复回归测试。

跨进程审查发现：``OrderState.submission_payload['side']`` 是 buy/sell
方向的**唯一**持久化源（OrderState schema 无顶层 side 列）。旧代码在
side 缺失时默认 ``"buy"``，会在 corruption / DLQ replay 等边界场景把
原 short 单静默恢复成 long，触发反向建仓风险。

修复后的恢复语义必须是：
    1. payload["side"] 合法（"buy"/"sell"） → 直接用
    2. 缺失 / 非法 → 用 state.position_intent 推导（可审计的三重持久
       化字段，10 个枚举值全部映射到确定方向）
    3. 两者都不可用 → raise（拒绝静默默认方向）
"""

from __future__ import annotations

import unittest
from decimal import Decimal

from aats.schemas.execution import OrderState
from aats.services.execution_engine.okx_adapter import OKXExecutionAdapter


def _build_state(*, payload: dict[str, str], position_intent: str = "open_long") -> OrderState:
    return OrderState(
        decision_id="decision_r7x1",
        intent_id="intent_r7x1",
        symbol="BTC-USDT-SWAP",
        client_order_id="cl_r7x1",
        venue="OKX",
        status="SUBMITTED",
        submission_mode="guarded_live_submit",
        requested_qty=Decimal("0.01"),
        filled_qty=Decimal("0"),
        remaining_qty=Decimal("0.01"),
        product_type="derivatives",
        margin_mode="cross",
        position_mode="long_short_mode",
        position_intent=position_intent,  # type: ignore[arg-type]
        submission_payload=payload,
    )


class TestIntentFromStateDirectionRecovery(unittest.TestCase):
    def test_happy_path_payload_side_buy_used(self) -> None:
        """payload["side"]="buy" 时直接采用（happy path，不触碰 fallback）。"""
        state = _build_state(payload={"side": "buy", "ordType": "market"})
        intent = OKXExecutionAdapter._intent_from_state(state)
        self.assertEqual(intent.side, "buy")

    def test_happy_path_payload_side_sell_used(self) -> None:
        """payload["side"]="sell" 时直接采用（覆盖所有 short 开仓 / long 平仓）。"""
        state = _build_state(
            payload={"side": "sell", "ordType": "market"},
            position_intent="open_short",
        )
        intent = OKXExecutionAdapter._intent_from_state(state)
        self.assertEqual(intent.side, "sell")

    def test_missing_side_derives_sell_from_open_short_position_intent(self) -> None:
        """**核心回归**：payload 缺 side 但 position_intent=open_short 时，
        必须推导出 sell。旧代码会默认 buy → 反向建仓。"""
        state = _build_state(
            payload={"ordType": "market"},  # 故意不给 side
            position_intent="open_short",
        )
        intent = OKXExecutionAdapter._intent_from_state(state)
        self.assertEqual(intent.side, "sell", "open_short 必须恢复为 sell，不能默认 buy")

    def test_missing_side_derives_sell_from_close_long_position_intent(self) -> None:
        """payload 缺 side + position_intent=close_long → 必须推导 sell（平多）。"""
        state = _build_state(
            payload={"ordType": "market"},
            position_intent="close_long",
        )
        intent = OKXExecutionAdapter._intent_from_state(state)
        self.assertEqual(intent.side, "sell")

    def test_missing_side_derives_buy_from_close_short_position_intent(self) -> None:
        """payload 缺 side + position_intent=close_short → 必须推导 buy（平空）。"""
        state = _build_state(
            payload={"ordType": "market"},
            position_intent="close_short",
        )
        intent = OKXExecutionAdapter._intent_from_state(state)
        self.assertEqual(intent.side, "buy")

    def test_missing_side_derives_buy_from_reduce_short_position_intent(self) -> None:
        """payload 缺 side + position_intent=reduce_short → 必须推导 buy。"""
        state = _build_state(
            payload={"ordType": "market"},
            position_intent="reduce_short",
        )
        intent = OKXExecutionAdapter._intent_from_state(state)
        self.assertEqual(intent.side, "buy")

    def test_empty_string_side_treated_as_missing(self) -> None:
        """payload["side"]="" 等同缺失，走 position_intent 推导路径。"""
        state = _build_state(
            payload={"side": "", "ordType": "market"},
            position_intent="scale_in_short",
        )
        intent = OKXExecutionAdapter._intent_from_state(state)
        self.assertEqual(intent.side, "sell", "空字符串 side 必须 fall through 到 position_intent 推导")

    def test_invalid_side_value_treated_as_missing(self) -> None:
        """payload["side"]="long"（错值）不能被当成 buy 用。"""
        state = _build_state(
            payload={"side": "long", "ordType": "market"},
            position_intent="open_short",
        )
        intent = OKXExecutionAdapter._intent_from_state(state)
        self.assertEqual(intent.side, "sell", "非 buy/sell 的错值必须 fall through 到 position_intent 推导")


if __name__ == "__main__":
    unittest.main()
