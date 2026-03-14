from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from aats.schemas.common import new_id
from aats.schemas.execution import FillEvent, OrderIntent, OrderState
from aats.services.execution_engine.exchange_adapter import ExchangeAdapter


class PaperExecutionAdapter(ExchangeAdapter):
    def __init__(self, *, price_provider: Callable[[str], float], taker_fee_bps: float) -> None:
        self.price_provider = price_provider
        self.taker_fee_bps = taker_fee_bps

    async def submit(self, intent: OrderIntent) -> tuple[OrderState, list[FillEvent]]:
        now = datetime.now(timezone.utc)
        client_order_id = new_id("clord")
        exchange_order_id = new_id("paper")
        fill_price = self.price_provider(intent.symbol)
        fee_amount = intent.quantity * fill_price * (self.taker_fee_bps / 10_000.0)
        state = OrderState(
            decision_id=intent.decision_id,
            intent_id=intent.intent_id,
            client_order_id=client_order_id,
            venue="PAPER",
            exchange_order_id=exchange_order_id,
            status="FILLED",
            submitted_ts=now,
            last_update_ts=now,
            requested_qty=intent.quantity,
            filled_qty=intent.quantity,
            remaining_qty=0.0,
            average_fill_price=fill_price,
            fees=fee_amount,
            submission_payload={
                "instId": intent.symbol,
                "side": intent.side,
                "sz": str(intent.quantity),
                "ordType": intent.order_type,
            },
        )
        fill = FillEvent(
            fill_id=new_id("fill"),
            decision_id=intent.decision_id,
            intent_id=intent.intent_id,
            client_order_id=client_order_id,
            exchange_order_id=exchange_order_id,
            symbol=intent.symbol,
            side=intent.side,
            fill_qty=intent.quantity,
            fill_price=fill_price,
            fee_amount=fee_amount,
            liquidity_role="taker",
            exchange_timestamp=now,
            ingestion_timestamp=now,
        )
        return state, [fill]

    def readiness(self) -> dict[str, object]:
        return {"ready": True, "backend": "paper", "live_submit_enabled": False}
