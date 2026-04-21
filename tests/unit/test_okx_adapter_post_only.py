"""OKXOrderPayloadBuilder._order_type post_only 翻译 — 单元测试.

Layer 3: okx_adapter._order_type 根据 intent.execution_style 翻译:
   execution_style == "post_only"  → 返回 "post_only" (OKX 原生 ordType)
   其他 → 按原 IOC/FOK/limit/market 路径

锁定契约 (docs/design/post_only_maker_exit_mode_2026_04_21.md §3.4):
1. OrderIntent.order_type 的 Literal 仍是 ["market", "limit"] — 不扩展到 post_only
   (避免跨 schemas/execution.py + strategy_runtime.py + planner.py + 测试夹具共 10+ 处改动)
2. post_only 的信号载体是 execution_style 字段 (自由字符串 str)
3. _order_type 在出站时读 execution_style, 翻译为 exchange-specific ordType="post_only"
4. H2 regression: execution_style != "post_only" 时, ioc/fok/limit/market 路径不变
"""

from __future__ import annotations

import unittest
from decimal import Decimal

from aats.schemas.execution import OrderIntent
from aats.services.execution_engine.okx_adapter import OKXOrderPayloadBuilder


def _make_intent(
    *,
    execution_style: str = "taker",
    order_type: str = "market",
    time_in_force: str = "IOC",
    side: str = "buy",
) -> OrderIntent:
    return OrderIntent(
        intent_id="intent_test",
        decision_id="decision_test",
        symbol="BTC-USDT-SWAP",
        side=side,  # type: ignore[arg-type]
        quantity=Decimal("0.01"),
        execution_style=execution_style,
        order_type=order_type,  # type: ignore[arg-type]
        limit_price=Decimal("100") if order_type == "limit" else None,
        urgency="medium",
        time_in_force=time_in_force,
        idempotency_key="idem_test",
    )


class PostOnlyOrderTypeTranslationTests(unittest.TestCase):
    """execution_style="post_only" 翻译为 ordType="post_only"."""

    def test_execution_style_post_only_lowercase_translates(self) -> None:
        intent = _make_intent(
            execution_style="post_only",
            order_type="limit",
            time_in_force="GTC",
        )
        self.assertEqual(OKXOrderPayloadBuilder._order_type(intent), "post_only")

    def test_execution_style_post_only_uppercase_normalized(self) -> None:
        """execution_style 大小写不敏感 — 小写后比较."""
        intent = _make_intent(
            execution_style="POST_ONLY",
            order_type="limit",
            time_in_force="GTC",
        )
        self.assertEqual(OKXOrderPayloadBuilder._order_type(intent), "post_only")

    def test_execution_style_post_only_mixed_case(self) -> None:
        intent = _make_intent(
            execution_style="Post_Only",
            order_type="limit",
            time_in_force="GTC",
        )
        self.assertEqual(OKXOrderPayloadBuilder._order_type(intent), "post_only")

    def test_post_only_overrides_ioc_translation(self) -> None:
        """post_only 优先于 IOC 翻译: 即使 time_in_force=IOC, 仍返回 post_only."""
        intent = _make_intent(
            execution_style="post_only",
            order_type="limit",
            time_in_force="IOC",  # 正常 IOC 会翻译为 "ioc"
        )
        # execution_style=post_only 分支优先命中
        self.assertEqual(OKXOrderPayloadBuilder._order_type(intent), "post_only")


class PostOnlyDoesNotRegressOtherModesTests(unittest.TestCase):
    """**H2 regression guard**: execution_style != "post_only" 时, ioc/fok/limit/market 路径不变."""

    def test_bounded_limit_ioc_still_returns_ioc(self) -> None:
        """H2 锁定: bounded_limit_ioc + order_type=limit + tif=IOC → ioc."""
        intent = _make_intent(
            execution_style="bounded_limit_ioc",
            order_type="limit",
            time_in_force="IOC",
        )
        self.assertEqual(OKXOrderPayloadBuilder._order_type(intent), "ioc")

    def test_taker_market_still_returns_market(self) -> None:
        intent = _make_intent(
            execution_style="taker",
            order_type="market",
            time_in_force="IOC",
        )
        self.assertEqual(OKXOrderPayloadBuilder._order_type(intent), "market")

    def test_limit_fok_still_returns_fok(self) -> None:
        intent = _make_intent(
            execution_style="limit_fok",
            order_type="limit",
            time_in_force="FOK",
        )
        self.assertEqual(OKXOrderPayloadBuilder._order_type(intent), "fok")

    def test_limit_gtc_returns_limit(self) -> None:
        """execution_style 不是 post_only 时 GTC 走普通 limit 路径."""
        intent = _make_intent(
            execution_style="maker",
            order_type="limit",
            time_in_force="GTC",
        )
        self.assertEqual(OKXOrderPayloadBuilder._order_type(intent), "limit")

    def test_empty_execution_style_does_not_trigger_post_only(self) -> None:
        intent = _make_intent(
            execution_style="",
            order_type="limit",
            time_in_force="IOC",
        )
        self.assertEqual(OKXOrderPayloadBuilder._order_type(intent), "ioc")


if __name__ == "__main__":
    unittest.main()
