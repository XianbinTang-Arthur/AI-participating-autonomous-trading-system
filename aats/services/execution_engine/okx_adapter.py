from __future__ import annotations

from decimal import Decimal, ROUND_DOWN
from typing import Any

from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import new_id, utc_now
from aats.schemas.execution import FillEvent, OrderIntent, OrderState
from aats.schemas.exchange import InstrumentMetadata
from aats.services.execution_engine.exchange_adapter import ExchangeAdapter
from aats.services.execution_engine.okx_account import OKXAccountService
from aats.services.execution_engine.okx_rest import OKXRESTClient
from aats.services.governance_engine.mode import RuntimeModeController


class OKXOrderPayloadBuilder:
    def build(self, *, intent: OrderIntent, instrument: InstrumentMetadata) -> dict[str, str]:
        quantity = self._round_down(value=intent.quantity, step=instrument.lot_size)
        if quantity <= 0.0 or quantity < instrument.min_size:
            raise ValueError(
                f"Order quantity below OKX minimum size symbol={intent.symbol} quantity={intent.quantity} min_size={instrument.min_size}"
            )

        payload = {
            "instId": instrument.instrument_id,
            "tdMode": "cash",
            "side": intent.side,
            "ordType": intent.order_type,
            "sz": self._render_decimal(quantity),
            "clOrdId": self._client_order_id(intent),
        }
        if intent.order_type == "limit" and intent.limit_price is not None:
            limit_price = self._round_down(value=intent.limit_price, step=instrument.tick_size)
            payload["px"] = self._render_decimal(limit_price)
        if intent.order_type == "market" and intent.side == "buy":
            payload["tgtCcy"] = "base_ccy"
        return payload

    @staticmethod
    def _client_order_id(intent: OrderIntent) -> str:
        rendered = intent.idempotency_key.replace("-", "")[:32]
        return rendered if rendered else new_id("okx")[:32]

    @staticmethod
    def _round_down(*, value: float, step: float) -> float:
        if step <= 0.0:
            return value
        ratio = Decimal(str(value)) / Decimal(str(step))
        rounded = ratio.quantize(Decimal("1"), rounding=ROUND_DOWN) * Decimal(str(step))
        return float(rounded)

    @staticmethod
    def _render_decimal(value: float) -> str:
        rendered = format(Decimal(str(value)).normalize(), "f")
        return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


class OKXExecutionAdapter(ExchangeAdapter):
    def __init__(
        self,
        *,
        settings: AATSSettings,
        client: OKXRESTClient,
        account_service: OKXAccountService,
        mode_controller: RuntimeModeController,
        payload_builder: OKXOrderPayloadBuilder | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.account_service = account_service
        self.mode_controller = mode_controller
        self.payload_builder = payload_builder or OKXOrderPayloadBuilder()

    async def submit(self, intent: OrderIntent) -> tuple[OrderState, list[FillEvent]]:
        snapshot = await self.account_service.refresh()
        instrument = self.account_service.instrument_metadata(intent.symbol)
        if snapshot is None or instrument is None:
            raise RuntimeError(f"OKX instrument metadata unavailable for symbol={intent.symbol}")

        payload = self.payload_builder.build(intent=intent, instrument=instrument)
        now = utc_now()
        live_allowed = (
            self.mode_controller.mode == "guarded_live"
            and self.settings.execution_backend == "okx"
            and self.settings.live_submit_enabled
            and not self.settings.guarded_execution_dry_run
        )
        if not live_allowed:
            return (
                OrderState(
                    decision_id=intent.decision_id,
                    intent_id=intent.intent_id,
                    client_order_id=payload["clOrdId"],
                    venue="OKX",
                    exchange_order_id=None,
                    status="DRY_RUN" if self.settings.guarded_execution_dry_run else "BLOCKED",
                    submitted_ts=now,
                    last_update_ts=now,
                    requested_qty=intent.quantity,
                    filled_qty=0.0,
                    remaining_qty=intent.quantity,
                    average_fill_price=None,
                    fees=0.0,
                    cancel_reason="live_submit_disabled",
                    submission_payload={key: str(value) for key, value in payload.items()},
                ),
                [],
            )

        response = await self.client.place_order(payload)
        data = response.get("data", [])
        if not data:
            raise RuntimeError("OKX place_order returned no data")
        row = data[0]
        order_id = str(row.get("ordId")) if row.get("ordId") else None
        return (
            OrderState(
                decision_id=intent.decision_id,
                intent_id=intent.intent_id,
                client_order_id=payload["clOrdId"],
                venue="OKX",
                exchange_order_id=order_id,
                status="LIVE" if order_id else "SUBMITTED",
                submitted_ts=now,
                last_update_ts=now,
                requested_qty=intent.quantity,
                filled_qty=0.0,
                remaining_qty=intent.quantity,
                average_fill_price=None,
                fees=0.0,
                cancel_reason=None,
                submission_payload={key: str(value) for key, value in payload.items()},
            ),
            [],
        )

    def readiness(self) -> dict[str, Any]:
        account_status = self.account_service.status()
        return {
            "ready": account_status["credentials_configured"] and account_status["enabled"],
            "backend": "okx",
            "mode": self.mode_controller.mode,
            "live_submit_enabled": self.settings.live_submit_enabled,
            "guarded_execution_dry_run": self.settings.guarded_execution_dry_run,
            "account_status": account_status,
        }
