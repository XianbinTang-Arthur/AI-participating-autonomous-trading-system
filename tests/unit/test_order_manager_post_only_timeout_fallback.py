"""order_manager post_only timeout orchestration — 单元测试.

Layer 4: _execute_post_only_with_timeout_fallback (见 evidence doc §3.5).

锁定契约:
1. intent.execution_style=="post_only" → 走 post_only orchestration (而不是直接 submit)
2. submit 立即 FILLED → 直接返回, 不触发 fallback
3. submit 立即 REJECTED → 立即 fallback (不等 timeout)
4. submit 后 SUBMITTED + timeout 内成交 → 返回 FILLED, 不 fallback
5. submit 后 SUBMITTED + timeout 后未成交 → cancel + 用 remaining_qty 重下 fallback intent
6. cancel race (cancel 时正好 fill 完) → 返回, 不重复下单
7. cancel 失败 → 返回 refreshed state, 不 fallback (避免 double-spend)
8. fallback intent 的关键字段:
   - execution_style="bounded_taker_cap"
   - order_type="market"
   - time_in_force="IOC"
   - quantity=remaining_qty
   - intent_id 末尾带 ":pof" suffix
9. fallback_mode != "bounded_taker" → raise RuntimeError
10. H2 regression: execution_style != post_only 走原 path (split / single submit), 不触发 orchestration
"""

from __future__ import annotations

import asyncio
import unittest
from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.bus.memory_bus import InMemoryEventBus
from aats.schemas.common import utc_now
from aats.schemas.execution import FillEvent, OrderIntent, OrderState
from aats.services.execution_engine.order_manager import OrderManager
from aats.services.governance_engine.kill_switch import KillSwitch
from aats.storage.event_store import InMemoryEventStore
from aats.storage.execution_repo import InMemoryExecutionRepository


def _make_post_only_intent(
    *,
    intent_id: str = "intent_post_only",
    decision_id: str = "decision_post_only",
    quantity: Decimal = Decimal("0.01"),
    side: str = "sell",
    idempotency_key: str = "clord_post_only",
) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        execution_chain_id=f"chain_{intent_id}",
        decision_id=decision_id,
        symbol="BTC-USDT-SWAP",
        side=side,  # type: ignore[arg-type]
        quantity=quantity,
        execution_style="post_only",  # ← Layer 4 触发信号
        order_type="limit",
        limit_price=Decimal("100.008"),
        reference_price=Decimal("100"),
        urgency="medium",
        time_in_force="GTC",
        reduce_only=True,
        close_only=True,
        position_mode="long_short_mode",
        pos_side="long",
        execution_action="exit",
        leg_action="close",
        position_intent="close_long",
        product_type="derivatives",
        margin_mode="cross",
        exposure_side="long",
        idempotency_key=idempotency_key,
    )


def _build_state(
    *,
    intent: OrderIntent,
    client_order_id: str,
    status: str = "SUBMITTED",
    filled_qty: Decimal | None = None,
    remaining_qty: Decimal | None = None,
    cancel_reason: str | None = None,
) -> OrderState:
    filled = filled_qty if filled_qty is not None else Decimal("0")
    remaining = remaining_qty if remaining_qty is not None else (intent.quantity - filled)
    submitted_ts = utc_now()
    return OrderState(
        decision_id=intent.decision_id,
        execution_chain_id=intent.execution_chain_id,
        execution_attempt_id=intent.execution_attempt_id,
        intent_id=intent.intent_id,
        symbol=intent.symbol,
        client_order_id=client_order_id,
        venue="OKX",
        exchange_order_id=f"ord_{client_order_id}",
        status=status,  # type: ignore[arg-type]
        submission_mode="guarded_simulated_submit",
        exchange_status="live" if status == "SUBMITTED" else status.lower(),
        submitted_ts=submitted_ts,
        last_update_ts=submitted_ts,
        requested_qty=intent.quantity,
        filled_qty=filled,
        remaining_qty=remaining,
        average_fill_price=None if filled <= 0 else Decimal("100"),
        fees=Decimal("0"),
        reduce_only=intent.reduce_only,
        close_only=intent.close_only,
        position_mode=intent.position_mode,
        pos_side=intent.pos_side,
        product_type=intent.product_type,
        margin_mode=intent.margin_mode,
        exposure_side=intent.exposure_side,
        execution_action=intent.execution_action,
        leg_action=intent.leg_action,
        position_intent=intent.position_intent,
        cancel_reason=cancel_reason,
        submission_payload={},
    )


class _RecordingAdapter:
    """通用 stub adapter, 记录所有 submit/cancel 序列."""

    def __init__(self) -> None:
        self.submit_calls: list[OrderIntent] = []
        self.cancel_calls: list[OrderState] = []
        self.next_submit_status: list[str] = []  # FIFO 状态序列
        self.next_cancel_status: str = "CANCELED"
        self.cancel_remaining_qty: Decimal | None = None
        self.cancel_filled_qty: Decimal | None = None
        self.raise_on_cancel: bool = False

    def preview_client_order_id(self, intent: OrderIntent) -> str | None:
        return intent.idempotency_key

    def readiness(self) -> dict:
        return {
            "backend": "okx",
            "exchange_submit_allowed": True,
            "submit_blocked_reasons": [],
        }

    async def submit(self, intent: OrderIntent):
        self.submit_calls.append(intent)
        status = self.next_submit_status.pop(0) if self.next_submit_status else "SUBMITTED"
        if status == "FILLED":
            state = _build_state(
                intent=intent,
                client_order_id=intent.idempotency_key,
                status="FILLED",
                filled_qty=intent.quantity,
                remaining_qty=Decimal("0"),
            )
            fills = [
                FillEvent(
                    fill_id=f"fill_{intent.idempotency_key}",
                    decision_id=intent.decision_id,
                    intent_id=intent.intent_id,
                    client_order_id=intent.idempotency_key,
                    exchange_order_id=f"ord_{intent.idempotency_key}",
                    symbol=intent.symbol,
                    venue="OKX",
                    side=intent.side,
                    fill_qty=intent.quantity,
                    fill_price=Decimal("100"),
                    fee_amount=Decimal("0"),
                    reduce_only=intent.reduce_only,
                    close_only=intent.close_only,
                    position_mode=intent.position_mode,
                    pos_side=intent.pos_side,
                    product_type=intent.product_type,
                    margin_mode=intent.margin_mode,
                    exposure_side=intent.exposure_side,
                    execution_action=intent.execution_action,
                    leg_action=intent.leg_action,
                    position_intent=intent.position_intent,
                    liquidity_role="maker",
                    exchange_timestamp=utc_now(),
                    ingestion_timestamp=utc_now(),
                    order_status_after_fill="FILLED",
                )
            ]
            return state, fills
        # SUBMITTED / REJECTED / FAILED
        state = _build_state(
            intent=intent,
            client_order_id=intent.idempotency_key,
            status=status,
            cancel_reason="post_only_rejected_crosses_book"
            if status == "REJECTED"
            else None,
        )
        return state, []

    async def cancel(self, order_state: OrderState):
        self.cancel_calls.append(order_state)
        if self.raise_on_cancel:
            raise RuntimeError("cancel_failed_for_test")
        # cancel 后 remaining_qty 由测试控制 (覆盖 race 场景)
        remaining = (
            self.cancel_remaining_qty
            if self.cancel_remaining_qty is not None
            else order_state.remaining_qty
        )
        filled = (
            self.cancel_filled_qty
            if self.cancel_filled_qty is not None
            else order_state.filled_qty
        )
        return (
            order_state.model_copy(
                update={
                    "status": self.next_cancel_status,
                    "filled_qty": filled,
                    "remaining_qty": remaining,
                    "last_update_ts": utc_now(),
                }
            ),
            [],
        )

    async def sync(self, open_order_states):
        return [], []


def _make_manager(
    adapter: _RecordingAdapter,
    *,
    timeout_ms: float = 0.0,
    fallback_mode: str = "bounded_taker",
) -> tuple[OrderManager, InMemoryExecutionRepository]:
    settings = AATSSettings.model_validate(
        {
            "strategy_hedge_independent_post_only_timeout_ms": timeout_ms,
            "strategy_hedge_independent_post_only_fallback_mode": fallback_mode,
        }
    )
    execution_repo = InMemoryExecutionRepository()
    manager = OrderManager(
        settings=settings,
        bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
        adapter=adapter,
        execution_repo=execution_repo,
        kill_switch=KillSwitch(),
    )
    return manager, execution_repo


class PostOnlySignalDetectionTests(unittest.TestCase):
    """_intent_signals_post_only 静态契约."""

    def test_post_only_lowercase_detected(self) -> None:
        intent = _make_post_only_intent()
        self.assertTrue(OrderManager._intent_signals_post_only(intent))

    def test_post_only_uppercase_detected(self) -> None:
        intent = _make_post_only_intent().model_copy(update={"execution_style": "POST_ONLY"})
        self.assertTrue(OrderManager._intent_signals_post_only(intent))

    def test_taker_not_detected(self) -> None:
        intent = _make_post_only_intent().model_copy(update={"execution_style": "taker"})
        self.assertFalse(OrderManager._intent_signals_post_only(intent))

    def test_bounded_limit_ioc_not_detected(self) -> None:
        intent = _make_post_only_intent().model_copy(
            update={"execution_style": "bounded_limit_ioc"}
        )
        self.assertFalse(OrderManager._intent_signals_post_only(intent))

    def test_empty_execution_style_not_detected(self) -> None:
        intent = _make_post_only_intent().model_copy(update={"execution_style": ""})
        self.assertFalse(OrderManager._intent_signals_post_only(intent))


class PostOnlyImmediateOutcomeTests(unittest.IsolatedAsyncioTestCase):
    """post_only 提交后立即终态的几种情况."""

    async def test_immediately_filled_returns_without_fallback(self) -> None:
        adapter = _RecordingAdapter()
        adapter.next_submit_status = ["FILLED"]
        manager, _ = _make_manager(adapter, timeout_ms=0.0)

        intent = _make_post_only_intent()
        state = await manager.process_submit_command(intent=intent)

        self.assertEqual(state.status, "FILLED")
        # 只 submit 一次, 没有 fallback
        self.assertEqual(len(adapter.submit_calls), 1)
        self.assertEqual(len(adapter.cancel_calls), 0)
        self.assertEqual(adapter.submit_calls[0].execution_style, "post_only")

    async def test_immediately_rejected_triggers_fallback(self) -> None:
        """OKX sCode≠0 (e.g., 51116 cross-book) → REJECTED → 立即 fallback (不等 timeout)."""
        adapter = _RecordingAdapter()
        # 第 1 次 submit: REJECTED; 第 2 次 (fallback): FILLED
        adapter.next_submit_status = ["REJECTED", "FILLED"]
        manager, _ = _make_manager(adapter, timeout_ms=999_999.0)  # 超大 timeout 验证 不 sleep

        intent = _make_post_only_intent()
        state = await manager.process_submit_command(intent=intent)

        self.assertEqual(state.status, "FILLED")
        self.assertEqual(len(adapter.submit_calls), 2)
        # 第 2 次 submit 是 fallback intent
        fallback = adapter.submit_calls[1]
        self.assertEqual(fallback.execution_style, "bounded_taker_cap")
        self.assertEqual(fallback.order_type, "market")
        self.assertEqual(fallback.time_in_force, "IOC")
        self.assertIsNone(fallback.limit_price)
        self.assertEqual(fallback.quantity, intent.quantity)
        self.assertTrue(fallback.intent_id.endswith(":pof"))
        # REJECTED → 不需要 cancel
        self.assertEqual(len(adapter.cancel_calls), 0)


class PostOnlyTimeoutFallbackTests(unittest.IsolatedAsyncioTestCase):
    """post_only 提交后挂单 → 等 timeout → 触发 cancel + fallback."""

    async def test_timeout_unfilled_cancels_and_fallback_full_qty(self) -> None:
        adapter = _RecordingAdapter()
        adapter.next_submit_status = ["SUBMITTED", "FILLED"]
        adapter.next_cancel_status = "CANCELED"
        adapter.cancel_remaining_qty = Decimal("0.01")  # 全单未成交
        adapter.cancel_filled_qty = Decimal("0")
        manager, repo = _make_manager(adapter, timeout_ms=0.0)

        intent = _make_post_only_intent()
        state = await manager.process_submit_command(intent=intent)

        self.assertEqual(state.status, "FILLED")
        self.assertEqual(len(adapter.submit_calls), 2)
        self.assertEqual(len(adapter.cancel_calls), 1)
        fallback = adapter.submit_calls[1]
        # fallback 用全单 0.01
        self.assertEqual(fallback.quantity, Decimal("0.01"))
        self.assertEqual(fallback.execution_style, "bounded_taker_cap")
        self.assertTrue(fallback.intent_id.endswith(":pof"))

    async def test_timeout_partially_filled_cancels_and_fallback_remaining_qty(self) -> None:
        """post_only 部分成交 → cancel → fallback 只下 remaining_qty."""
        adapter = _RecordingAdapter()
        adapter.next_submit_status = ["SUBMITTED", "FILLED"]
        adapter.next_cancel_status = "CANCELED"
        adapter.cancel_remaining_qty = Decimal("0.004")  # 只成交 0.006
        adapter.cancel_filled_qty = Decimal("0.006")
        manager, _ = _make_manager(adapter, timeout_ms=0.0)

        intent = _make_post_only_intent(quantity=Decimal("0.01"))
        state = await manager.process_submit_command(intent=intent)

        self.assertEqual(state.status, "FILLED")
        self.assertEqual(len(adapter.submit_calls), 2)
        fallback = adapter.submit_calls[1]
        self.assertEqual(fallback.quantity, Decimal("0.004"))

    async def test_cancel_race_filled_completely_no_fallback(self) -> None:
        """cancel 时正好 fill 完 → remaining_qty=0 → 不重复下单."""
        adapter = _RecordingAdapter()
        adapter.next_submit_status = ["SUBMITTED"]  # 只会 submit 一次
        adapter.next_cancel_status = "FILLED"
        adapter.cancel_remaining_qty = Decimal("0")
        adapter.cancel_filled_qty = Decimal("0.01")
        manager, _ = _make_manager(adapter, timeout_ms=0.0)

        intent = _make_post_only_intent()
        await manager.process_submit_command(intent=intent)

        # cancel 返回的 state remaining=0 → 没有 fallback
        self.assertEqual(len(adapter.submit_calls), 1)
        self.assertEqual(len(adapter.cancel_calls), 1)

    async def test_cancel_failure_no_fallback(self) -> None:
        """cancel 抛异常 → 不 fallback (避免 double-spend remaining)."""
        adapter = _RecordingAdapter()
        adapter.next_submit_status = ["SUBMITTED"]
        adapter.raise_on_cancel = True
        manager, _ = _make_manager(adapter, timeout_ms=0.0)

        intent = _make_post_only_intent()
        state = await manager.process_submit_command(intent=intent)

        # 只 submit 一次, cancel 失败, 没有 fallback
        self.assertEqual(len(adapter.submit_calls), 1)
        self.assertEqual(len(adapter.cancel_calls), 1)
        # 返回的是 cancel 前的 SUBMITTED state
        self.assertIn(state.status, {"SUBMITTED", "CANCEL_PENDING"})


class PostOnlyFallbackIntentShapeTests(unittest.IsolatedAsyncioTestCase):
    """fallback intent 关键字段契约."""

    async def test_fallback_intent_field_overrides(self) -> None:
        adapter = _RecordingAdapter()
        adapter.next_submit_status = ["SUBMITTED", "FILLED"]
        adapter.cancel_remaining_qty = Decimal("0.01")
        adapter.cancel_filled_qty = Decimal("0")
        manager, _ = _make_manager(adapter, timeout_ms=0.0)

        intent = _make_post_only_intent(
            intent_id="intent_field_check",
            decision_id="decision_field_check",
            idempotency_key="clord_field_check",
        )
        await manager.process_submit_command(intent=intent)

        fallback = adapter.submit_calls[1]
        # 关键字段
        self.assertEqual(fallback.execution_style, "bounded_taker_cap")
        self.assertEqual(fallback.order_type, "market")
        self.assertEqual(fallback.time_in_force, "IOC")
        self.assertIsNone(fallback.limit_price)
        # ID suffix
        self.assertEqual(fallback.intent_id, "intent_field_check:pof")
        self.assertEqual(fallback.idempotency_key, "clord_field_check:pof")
        # 不变字段保留
        self.assertEqual(fallback.decision_id, "decision_field_check")
        self.assertEqual(fallback.symbol, "BTC-USDT-SWAP")
        self.assertEqual(fallback.side, "sell")
        self.assertTrue(fallback.reduce_only)
        self.assertTrue(fallback.close_only)
        self.assertEqual(fallback.execution_chain_id, intent.execution_chain_id)

    async def test_unsupported_fallback_mode_raises(self) -> None:
        """fallback_mode 是 Literal 中其他合法值 (e.g. passive_first), 但 Layer 4
        只实现了 bounded_taker → raise. 见 evidence doc §3.5: 当前只支持 bounded_taker."""
        adapter = _RecordingAdapter()
        adapter.next_submit_status = ["SUBMITTED"]
        adapter.cancel_remaining_qty = Decimal("0.01")
        adapter.cancel_filled_qty = Decimal("0")
        manager, _ = _make_manager(
            adapter,
            timeout_ms=0.0,
            fallback_mode="passive_first",  # 合法 Literal 但 Layer 4 不支持
        )

        intent = _make_post_only_intent()
        with self.assertRaises(RuntimeError) as ctx:
            await manager.process_submit_command(intent=intent)
        self.assertIn("post_only_unsupported_fallback_mode", str(ctx.exception))


class PostOnlyTimeoutConfigTests(unittest.TestCase):
    """timeout_ms 默认值 + invalid value 兜底."""

    def test_default_timeout_3000(self) -> None:
        adapter = _RecordingAdapter()
        manager, _ = _make_manager(adapter, timeout_ms=3000.0)
        self.assertEqual(manager._post_only_timeout_ms(), 3000.0)

    def test_zero_timeout_for_test_speed(self) -> None:
        adapter = _RecordingAdapter()
        manager, _ = _make_manager(adapter, timeout_ms=0.0)
        self.assertEqual(manager._post_only_timeout_ms(), 0.0)


class PostOnlyDoesNotRegressTakerPathTests(unittest.IsolatedAsyncioTestCase):
    """**H2 regression guard**: execution_style != post_only 走原 path."""

    async def test_taker_intent_does_not_trigger_orchestration(self) -> None:
        adapter = _RecordingAdapter()
        adapter.next_submit_status = ["SUBMITTED"]
        manager, _ = _make_manager(adapter, timeout_ms=999_999.0)  # 超大 timeout 证明不进 sleep

        intent = _make_post_only_intent().model_copy(
            update={
                "execution_style": "taker",
                "order_type": "market",
                "time_in_force": "IOC",
                "limit_price": None,
            }
        )
        # 用 wait_for 兜底 — 如果意外进了 post_only orchestration, 它会 sleep 999 秒
        state = await asyncio.wait_for(
            manager.process_submit_command(intent=intent),
            timeout=2.0,
        )

        # 只 submit 1 次, 没 cancel, 不进 orchestration
        self.assertEqual(len(adapter.submit_calls), 1)
        self.assertEqual(len(adapter.cancel_calls), 0)
        self.assertEqual(state.status, "SUBMITTED")

    async def test_bounded_limit_ioc_intent_does_not_trigger_orchestration(self) -> None:
        adapter = _RecordingAdapter()
        adapter.next_submit_status = ["SUBMITTED"]
        manager, _ = _make_manager(adapter, timeout_ms=999_999.0)

        intent = _make_post_only_intent().model_copy(
            update={
                "execution_style": "bounded_limit_ioc",
                "order_type": "limit",
                "time_in_force": "IOC",
            }
        )
        state = await asyncio.wait_for(
            manager.process_submit_command(intent=intent),
            timeout=2.0,
        )
        self.assertEqual(len(adapter.submit_calls), 1)
        self.assertEqual(len(adapter.cancel_calls), 0)
        self.assertEqual(state.status, "SUBMITTED")


if __name__ == "__main__":
    unittest.main()
