from __future__ import annotations

from aats.bootstrap.settings import AATSSettings
from aats.bus.base import EventBus
from aats.events import topics
from aats.events.envelopes import publish_model
from aats.schemas.common import new_id
from aats.schemas.execution import ExecutionPlan, OrderIntent


class ExecutionPlanner:
    def __init__(self, *, settings: AATSSettings) -> None:
        self.settings = settings

    def build_plan(
        self,
        *,
        decision_id: str,
        symbol: str,
        current_position_qty: float,
        target_position_qty: float,
        approved_target_position_qty: float,
        delta_qty: float,
        urgency: str,
        max_slippage_tolerance_bps: int,
    ) -> ExecutionPlan | None:
        if abs(delta_qty) < 1e-12:
            return None

        normalized_urgency = urgency if urgency in {"low", "medium", "high"} else "medium"
        side = "buy" if delta_qty > 0 else "sell"
        return ExecutionPlan(
            plan_id=new_id("plan"),
            decision_id=decision_id,
            symbol=symbol,
            current_position_qty=current_position_qty,
            target_position_qty=target_position_qty,
            approved_target_position_qty=approved_target_position_qty,
            delta_qty=delta_qty,
            side=side,
            execution_style="taker",
            order_type="market",
            urgency=normalized_urgency,
            max_slippage_tolerance_bps=max_slippage_tolerance_bps,
        )

    def build_intent(self, *, plan: ExecutionPlan) -> OrderIntent | None:
        if abs(plan.delta_qty) < 1e-12:
            return None

        quantity = abs(plan.delta_qty)
        intent_id = new_id("intent")
        return OrderIntent(
            intent_id=intent_id,
            decision_id=plan.decision_id,
            symbol=plan.symbol,
            side=plan.side,
            quantity=quantity,
            execution_style=plan.execution_style,
            order_type=plan.order_type,
            urgency=plan.urgency,
            time_in_force="IOC",
            reduce_only=False,
            close_only=False,
            idempotency_key=intent_id,
        )

    async def publish_plan(self, *, bus: EventBus, plan: ExecutionPlan) -> None:
        await publish_model(
            bus=bus,
            topic=topics.EXECUTION_PLANS,
            key=plan.symbol,
            payload_model=plan,
            source_component="execution_engine",
        )

    async def publish_intent(self, *, bus: EventBus, intent: OrderIntent) -> None:
        await publish_model(
            bus=bus,
            topic=topics.ORDER_INTENTS,
            key=intent.symbol,
            payload_model=intent,
            source_component="execution_engine",
        )
