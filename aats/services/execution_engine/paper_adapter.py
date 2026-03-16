from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from aats.schemas.common import new_id
from aats.schemas.execution import FillEvent, OrderIntent, OrderState
from aats.services.execution_engine.exchange_adapter import ExchangeAdapter
from aats.services.governance_engine.runtime_layers import EnvironmentCapabilities


class PaperExecutionAdapter(ExchangeAdapter):
    def __init__(
        self,
        *,
        price_provider: Callable[[str], float],
        taker_fee_bps: float,
        environment_capabilities: EnvironmentCapabilities | None = None,
    ) -> None:
        self.price_provider = price_provider
        self.taker_fee_bps = taker_fee_bps
        self.environment_capabilities = environment_capabilities or EnvironmentCapabilities(
            market_data_source_kind="demo",
            account_state_source_kind="disabled",
            execution_adapter_kind="paper",
            execution_route="paper_local",
            exchange_submission_target="none",
            exchange_submission_possible=False,
            exchange_submission_enabled=False,
            persistent_storage_required=False,
            exchange_coupled=False,
            local_only=True,
        )

    async def submit(self, intent: OrderIntent) -> tuple[OrderState, list[FillEvent]]:
        now = datetime.now(timezone.utc)
        client_order_id = new_id("clord")
        exchange_order_id = new_id("paper")
        fill_price = self.price_provider(intent.symbol)
        fee_amount = intent.quantity * fill_price * (self.taker_fee_bps / 10_000.0)
        state = OrderState(
            decision_id=intent.decision_id,
            intent_id=intent.intent_id,
            symbol=intent.symbol,
            client_order_id=client_order_id,
            venue="PAPER",
            exchange_order_id=exchange_order_id,
            status="FILLED",
            submission_mode=(
                "paper_derivatives_local"
                if intent.product_type == "derivatives"
                else "paper_local"
            ),
            exchange_status="filled",
            exchange_status_history=["filled"],
            submitted_ts=now,
            last_update_ts=now,
            last_exchange_update_ts=now,
            requested_qty=intent.quantity,
            filled_qty=intent.quantity,
            remaining_qty=0.0,
            average_fill_price=fill_price,
            fees=fee_amount,
            product_type=intent.product_type,
            target_leverage=intent.target_leverage,
            margin_mode=intent.margin_mode,
            exposure_side=intent.exposure_side,
            position_intent=intent.position_intent,
            submission_payload={
                "instId": intent.symbol,
                "side": intent.side,
                "sz": str(intent.quantity),
                "ordType": intent.order_type,
                "productType": intent.product_type,
                "marginMode": intent.margin_mode,
                "targetLeverage": str(intent.target_leverage),
                "positionIntent": intent.position_intent,
            },
        )
        fill = FillEvent(
            fill_id=new_id("fill"),
            decision_id=intent.decision_id,
            intent_id=intent.intent_id,
            client_order_id=client_order_id,
            exchange_order_id=exchange_order_id,
            symbol=intent.symbol,
            venue="PAPER",
            side=intent.side,
            fill_qty=intent.quantity,
            fill_price=fill_price,
            fee_amount=fee_amount,
            fee_currency="USDT",
            product_type=intent.product_type,
            target_leverage=intent.target_leverage,
            margin_mode=intent.margin_mode,
            exposure_side=intent.exposure_side,
            position_intent=intent.position_intent,
            liquidity_role="taker",
            exchange_timestamp=now,
            ingestion_timestamp=now,
            order_status_after_fill="FILLED",
        )
        return state, [fill]

    async def cancel(self, order_state: OrderState) -> tuple[OrderState, list[FillEvent]]:
        if order_state.status == "FILLED":
            return order_state, []
        now = datetime.now(timezone.utc)
        canceled = order_state.model_copy(
            update={
                "status": "CANCELED",
                "exchange_status": "canceled",
                "exchange_status_history": [*order_state.exchange_status_history, "canceled"],
                "last_update_ts": now,
                "last_exchange_update_ts": now,
                "canceled_ts": now,
                "cancel_reason": "paper_cancel",
            }
        )
        return canceled, []

    async def sync(self, open_order_states: list[OrderState]) -> tuple[list[OrderState], list[FillEvent]]:
        _ = open_order_states
        return [], []

    def readiness(self) -> dict[str, object]:
        return {
            "ready": True,
            "backend": "paper",
            "execution_mode": "paper_local",
            "exchange_submit_allowed": False,
            "submit_blocked_reasons": ["paper_execution_has_no_exchange_submission"],
            "live_submit_enabled": False,
            "environment_capabilities": self.environment_capabilities.to_dict(),
        }
