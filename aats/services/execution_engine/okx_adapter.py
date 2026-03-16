from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any

from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import new_id, utc_now
from aats.schemas.execution import FillEvent, OrderIntent, OrderState
from aats.schemas.exchange import ExchangeFill, InstrumentMetadata
from aats.services.execution_engine.exchange_adapter import ExchangeAdapter
from aats.services.execution_engine.okx_account import OKXAccountService, datetime_from_ms
from aats.services.execution_engine.okx_rest import OKXRESTClient, OKXRequestError
from aats.services.governance_engine.health import SystemHealthService
from aats.services.governance_engine.mode import RuntimeModeController
from aats.services.governance_engine.runtime_layers import EnvironmentCapabilities, PolicyProfile
from aats.bootstrap.logging import correlation_fields, get_logger, log_event


class OKXOrderPayloadBuilder:
    def build(self, *, intent: OrderIntent, instrument: InstrumentMetadata) -> dict[str, str]:
        quantity = self._round_down(value=intent.quantity, step=instrument.lot_size)
        if quantity <= 0.0 or quantity < instrument.min_size:
            raise ValueError(
                f"Order quantity below OKX minimum size symbol={intent.symbol} quantity={intent.quantity} min_size={instrument.min_size}"
            )

        payload = {
            "instId": instrument.instrument_id,
            "tdMode": "cash" if intent.product_type == "spot" else intent.margin_mode,
            "side": intent.side,
            "ordType": intent.order_type,
            "sz": self._render_decimal(quantity),
            "clOrdId": self._client_order_id(intent),
        }
        if intent.product_type == "derivatives":
            payload["lever"] = self._render_decimal(max(intent.target_leverage, 1.0))
            payload["posSide"] = "long" if intent.exposure_side == "long" else "short"
            if intent.reduce_only:
                payload["reduceOnly"] = "true"
        if intent.order_type == "limit" and intent.limit_price is not None:
            limit_price = self._round_down(value=intent.limit_price, step=instrument.tick_size)
            payload["px"] = self._render_decimal(limit_price)
        if intent.order_type == "market" and intent.side == "buy":
            payload["tgtCcy"] = "base_ccy"
        return payload

    @staticmethod
    def _client_order_id(intent: OrderIntent) -> str:
        sanitized = "".join(
            ch for ch in intent.idempotency_key if ch.isascii() and ch.isalnum()
        )
        if not sanitized:
            sanitized = "".join(
                ch for ch in new_id("okx") if ch.isascii() and ch.isalnum()
            )
        digest = hashlib.sha256(sanitized.encode("utf-8")).hexdigest()
        return f"cl{digest[:30]}"

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
        environment_capabilities: EnvironmentCapabilities | None = None,
        policy_profile: PolicyProfile | None = None,
        health_service: SystemHealthService | None = None,
        price_provider: Callable[[str], float] | None = None,
        payload_builder: OKXOrderPayloadBuilder | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.account_service = account_service
        self.mode_controller = mode_controller
        self.environment_capabilities = environment_capabilities or mode_controller.environment_capabilities
        self.policy_profile = policy_profile or mode_controller.policy_profile
        self.health_service = health_service
        self.price_provider = price_provider
        self.payload_builder = payload_builder or OKXOrderPayloadBuilder()
        self._last_error: str | None = None
        self._last_submission_payload: dict[str, str] | None = None
        self.logger = get_logger("aats.okx_execution_adapter")

    def preview_client_order_id(self, intent: OrderIntent) -> str | None:
        return self.payload_builder._client_order_id(intent)

    async def submit(self, intent: OrderIntent) -> tuple[OrderState, list[FillEvent]]:
        snapshot = await self.account_service.refresh()
        instrument = self.account_service.instrument_metadata(intent.symbol)
        if snapshot is None or instrument is None:
            raise RuntimeError(f"OKX instrument metadata unavailable for symbol={intent.symbol}")

        payload = self.payload_builder.build(intent=intent, instrument=instrument)
        self._last_submission_payload = dict(payload)
        submitted_ts = utc_now()
        gate_error = self._submission_gate_error(intent=intent)
        if gate_error is not None:
            self._last_error = gate_error
            log_event(
                self.logger,
                "okx_submit_blocked",
                level="warning",
                **correlation_fields(
                    decision_id=intent.decision_id,
                    intent_id=intent.intent_id,
                    symbol=intent.symbol,
                    reason=gate_error,
                ),
            )
            return self._blocked_state(intent=intent, payload=payload, reason=gate_error), []

        try:
            response = await self.client.place_order(payload)
            row = self._first_row(response)
            row_code = str(row.get("sCode") or "0")
            row_message = str(row.get("sMsg") or "")
            if row_code != "0":
                self._last_error = row_message or f"submit_rejected_code_{row_code}"
                return (
                    self._rejected_state(
                        intent=intent,
                        payload=payload,
                        submitted_ts=submitted_ts,
                        error=row_message or row_code,
                    ),
                    [],
                )

            client_order_id = str(row.get("clOrdId") or payload["clOrdId"])
            order_id = str(row.get("ordId")) if row.get("ordId") else None
            order_detail = await self._load_order_detail(
                symbol=intent.symbol,
                order_id=order_id,
                client_order_id=client_order_id,
            )
            if order_detail is None:
                state = self._submitted_state(
                    intent=intent,
                    payload=payload,
                    submitted_ts=submitted_ts,
                    client_order_id=client_order_id,
                    order_id=order_id,
                )
                return state, []

            state = self._map_order_state(
                intent=intent,
                payload=payload,
                order_row=order_detail,
                submitted_ts=submitted_ts,
            )
            fills_payload = await self.client.get_fills(
                symbol=intent.symbol,
                order_id=state.exchange_order_id,
                limit=self.settings.okx_fill_fetch_limit,
            )
            fills = self._map_fill_events(
                intent=intent,
                client_order_id=state.client_order_id,
                exchange_fills=self._select_exchange_fills(
                    exchange_fills=self._parse_fill_rows(fills_payload),
                    order_id=state.exchange_order_id,
                    client_order_id=state.client_order_id,
                ),
            )
            self._last_error = None
            return state, fills
        except Exception as exc:
            self._last_error = str(exc)
            return (
                self._failed_state(
                    intent=intent,
                    payload=payload,
                    submitted_ts=submitted_ts,
                    error=str(exc),
                ),
                [],
            )

    async def cancel(self, order_state: OrderState) -> tuple[OrderState, list[FillEvent]]:
        if order_state.exchange_order_id is None:
            return (
                order_state.model_copy(
                    update={
                        "status": "FAILED",
                        "last_update_ts": utc_now(),
                        "execution_error": "missing_exchange_order_id_for_cancel",
                    }
                ),
                [],
            )
        if order_state.status in {"FILLED", "CANCELED", "REJECTED", "FAILED", "EXPIRED"}:
            return order_state, []

        cancel_pending = order_state.model_copy(
            update={
                "status": "CANCEL_PENDING",
                "cancellation_requested_ts": utc_now(),
                "last_update_ts": utc_now(),
            }
        )
        try:
            response = await self.client.cancel_order(
                {
                    "instId": order_state.symbol,
                    "ordId": order_state.exchange_order_id,
                    "clOrdId": order_state.client_order_id,
                }
            )
            row = self._first_row(response)
            row_code = str(row.get("sCode") or "0")
            row_message = str(row.get("sMsg") or "")
            if row_code != "0":
                self._last_error = row_message or f"cancel_rejected_code_{row_code}"
                return (
                    cancel_pending.model_copy(
                        update={
                            "status": "FAILED",
                            "execution_error": self._last_error,
                            "cancel_reason": self._last_error,
                            "last_exchange_update_ts": utc_now(),
                        }
                    ),
                    [],
                )

            order_detail = await self._load_order_detail(
                symbol=order_state.symbol,
                order_id=order_state.exchange_order_id,
                client_order_id=order_state.client_order_id,
            )
            if order_detail is None:
                return cancel_pending, []
            intent = self._intent_from_state(order_state)
            state = self._map_order_state(
                intent=intent,
                payload=order_state.submission_payload,
                order_row=order_detail,
                submitted_ts=order_state.submitted_ts or utc_now(),
            )
            state = state.model_copy(
                update={
                    "cancellation_requested_ts": cancel_pending.cancellation_requested_ts,
                    "cancel_reason": state.cancel_reason or cancel_pending.cancel_reason,
                }
            )
            fills_payload = await self.client.get_fills(
                symbol=order_state.symbol,
                order_id=order_state.exchange_order_id,
                limit=self.settings.okx_fill_fetch_limit,
            )
            fills = self._map_fill_events(
                intent=intent,
                client_order_id=order_state.client_order_id,
                exchange_fills=self._select_exchange_fills(
                    exchange_fills=self._parse_fill_rows(fills_payload),
                    order_id=order_state.exchange_order_id,
                    client_order_id=order_state.client_order_id,
                ),
            )
            self._last_error = None
            return state, fills
        except Exception as exc:
            self._last_error = str(exc)
            return (
                cancel_pending.model_copy(
                    update={
                        "status": "FAILED",
                        "execution_error": str(exc),
                        "cancel_reason": str(exc),
                        "last_exchange_update_ts": utc_now(),
                    }
                ),
                [],
            )

    async def sync(self, open_order_states: list[OrderState]) -> tuple[list[OrderState], list[FillEvent]]:
        if self.environment_capabilities.execution_adapter_kind != "okx":
            return [], []

        refreshed_states: list[OrderState] = []
        fills: list[FillEvent] = []
        for state in open_order_states:
            if state.venue != "OKX":
                continue
            # A local order can sit in SUBMITTING while adapter-side safety gates
            # are still being evaluated. Do not query the exchange until we have
            # an exchange order id or a post-submit state that can be resolved by
            # client order id.
            if state.status in {"CREATED", "SUBMITTING"} and state.exchange_order_id is None:
                continue
            try:
                order_detail = await self._load_order_detail(
                    symbol=state.symbol,
                    order_id=state.exchange_order_id,
                    client_order_id=state.client_order_id,
                )
                if order_detail is not None:
                    intent = self._intent_from_state(state)
                    refreshed_states.append(
                        self._map_order_state(
                            intent=intent,
                            payload=state.submission_payload,
                            order_row=order_detail,
                            submitted_ts=state.submitted_ts or utc_now(),
                        )
                    )
                fills_payload = await self.client.get_fills(
                    symbol=state.symbol,
                    order_id=state.exchange_order_id,
                    limit=self.settings.okx_fill_fetch_limit,
                )
                fills.extend(
                    self._map_fill_events(
                        intent=self._intent_from_state(state),
                        client_order_id=state.client_order_id,
                        exchange_fills=self._select_exchange_fills(
                            exchange_fills=self._parse_fill_rows(fills_payload),
                            order_id=state.exchange_order_id,
                            client_order_id=state.client_order_id,
                        ),
                    )
                )
                self._last_error = None
            except Exception as exc:
                self._last_error = str(exc)
                refreshed_states.append(
                    state.model_copy(
                        update={
                            "status": "FAILED",
                            "last_update_ts": utc_now(),
                            "execution_error": str(exc),
                        }
                    )
                )
        return refreshed_states, fills

    def readiness(self) -> dict[str, Any]:
        account_status = self.account_service.status()
        gate_status = self._gate_status()
        return {
            "ready": account_status["credentials_configured"] and account_status["enabled"],
            "backend": "okx",
            "mode": self.mode_controller.mode,
            "execution_mode": (
                "guarded_simulated_submit_derivatives"
                if self.environment_capabilities.exchange_submission_target == "okx_demo_derivatives"
                else "guarded_simulated_submit"
                if self.environment_capabilities.exchange_submission_target == "okx_demo_spot"
                else "guarded_live_blocked"
            ),
            "live_submit_enabled": self.settings.live_submit_enabled,
            "guarded_execution_dry_run": self.settings.guarded_execution_dry_run,
            "okx_simulated_trading": self.settings.okx_simulated_trading,
            "exchange_submit_allowed": gate_status["exchange_submit_allowed"],
            "submit_blocked_reasons": gate_status["submit_blocked_reasons"],
            "safety_gates": gate_status["safety_gates"],
            "last_error": self._last_error,
            "last_submission_payload": self._last_submission_payload,
            "account_status": account_status,
            "environment_capabilities": self.environment_capabilities.to_dict(),
            "policy_profile": self.policy_profile.to_dict(),
        }

    def _submission_gate_error(self, *, intent: OrderIntent) -> str | None:
        live_submission_requested = self.settings.live_submit_enabled and not self.settings.guarded_execution_dry_run
        if self.mode_controller.kill_switch.halted:
            return "kill_switch_active"
        if self.environment_capabilities.execution_adapter_kind != "okx":
            return "mode_not_guarded_live"
        if self.policy_profile.dry_run_only:
            return "guarded_execution_dry_run"
        if self.policy_profile.real_money_submission_structurally_blocked and live_submission_requested:
            return "real_money_live_not_supported"
        if not self.environment_capabilities.exchange_submission_enabled:
            return "live_submit_disabled"
        if self.environment_capabilities.exchange_submission_target not in {"okx_demo_spot", "okx_demo_derivatives"}:
            return "okx_simulated_trading_required"
        if intent.symbol not in self.settings.allowed_symbols:
            return "symbol_not_allowed"
        if self.account_service.open_order_count(symbol=intent.symbol) >= self.settings.max_open_orders:
            return "max_open_orders_reached"
        price = self.price_provider(intent.symbol) if self.price_provider is not None else 0.0
        if price > 0.0 and (intent.quantity * price) > self.settings.max_notional_per_symbol:
            return "max_notional_per_symbol_exceeded"
        account_status = self.account_service.status()
        if not account_status.get("ready", False):
            return "account_not_ready"
        if self.health_service is not None:
            blockers = self.health_service.execution_blockers()
            if blockers:
                return blockers[0]
        return None

    def _gate_status(self) -> dict[str, Any]:
        account_status = self.account_service.status()
        health_blockers = self.health_service.execution_blockers() if self.health_service is not None else []
        live_submission_requested = self.settings.live_submit_enabled and not self.settings.guarded_execution_dry_run
        safety_gates = {
            "mode_is_guarded_live": self.environment_capabilities.execution_adapter_kind == "okx",
            "execution_backend_is_okx": self.environment_capabilities.execution_adapter_kind == "okx",
            "simulated_trading_enabled": self.environment_capabilities.exchange_submission_target in {"okx_demo_spot", "okx_demo_derivatives"},
            "live_submit_enabled": self.environment_capabilities.exchange_submission_enabled,
            "dry_run_disabled": not self.policy_profile.dry_run_only,
            "halt_state_clear": not self.mode_controller.kill_switch.halted,
            "account_ready": bool(account_status.get("ready", False)),
            "health_checks_clear": not health_blockers,
            "symbol_allowlist_configured": bool(self.settings.allowed_symbols),
            "max_notional_cap_configured": self.settings.max_notional_per_symbol > 0.0,
            "max_open_orders_configured": self.settings.max_open_orders > 0,
            "real_money_submission_blocked": self.policy_profile.real_money_submission_structurally_blocked and live_submission_requested,
        }
        blocked_reasons: list[str] = []
        if not safety_gates["halt_state_clear"]:
            blocked_reasons.append("kill_switch_active")
        if not safety_gates["mode_is_guarded_live"]:
            blocked_reasons.append("mode_not_guarded_live")
        if not safety_gates["execution_backend_is_okx"]:
            blocked_reasons.append("execution_backend_not_okx")
        if not safety_gates["simulated_trading_enabled"]:
            blocked_reasons.append("okx_simulated_trading_required")
        if not safety_gates["live_submit_enabled"]:
            blocked_reasons.append("live_submit_disabled")
        if not safety_gates["dry_run_disabled"]:
            blocked_reasons.append("guarded_execution_dry_run")
        if not safety_gates["account_ready"]:
            blocked_reasons.append("account_not_ready")
        if safety_gates["real_money_submission_blocked"]:
            blocked_reasons.append("real_money_live_not_supported")
        blocked_reasons.extend([reason for reason in health_blockers if reason not in blocked_reasons])
        return {
            "exchange_submit_allowed": not blocked_reasons,
            "submit_blocked_reasons": blocked_reasons,
            "safety_gates": safety_gates,
        }

    def _blocked_state(self, *, intent: OrderIntent, payload: dict[str, str], reason: str) -> OrderState:
        now = utc_now()
        status = "DRY_RUN" if reason == "guarded_execution_dry_run" else "BLOCKED"
        return OrderState(
            decision_id=intent.decision_id,
            intent_id=intent.intent_id,
            symbol=intent.symbol,
            client_order_id=payload["clOrdId"],
            venue="OKX",
            exchange_order_id=None,
            status=status,
            submission_mode="guarded_simulated_dry_run" if status == "DRY_RUN" else "guarded_blocked",
            exchange_status="blocked",
            exchange_status_history=["blocked"],
            submitted_ts=now,
            last_update_ts=now,
            last_exchange_update_ts=now,
            requested_qty=intent.quantity,
            filled_qty=0.0,
            remaining_qty=intent.quantity,
            average_fill_price=None,
            fees=0.0,
            product_type=intent.product_type,
            target_leverage=intent.target_leverage,
            margin_mode=intent.margin_mode,
            exposure_side=intent.exposure_side,
            position_intent=intent.position_intent,
            cancel_reason=reason,
            execution_error=reason,
            submission_payload=self._state_submission_payload(intent=intent, payload=payload),
        )

    def _submitted_state(
        self,
        *,
        intent: OrderIntent,
        payload: dict[str, str],
        submitted_ts: datetime,
        client_order_id: str,
        order_id: str | None,
    ) -> OrderState:
        return OrderState(
            decision_id=intent.decision_id,
            intent_id=intent.intent_id,
            symbol=intent.symbol,
            client_order_id=client_order_id,
            venue="OKX",
            exchange_order_id=order_id,
            status="SUBMITTED",
            submission_mode="guarded_simulated_submit",
            exchange_status="live",
            exchange_status_history=["live"],
            submitted_ts=submitted_ts,
            last_update_ts=submitted_ts,
            last_exchange_update_ts=submitted_ts,
            requested_qty=intent.quantity,
            filled_qty=0.0,
            remaining_qty=intent.quantity,
            average_fill_price=None,
            fees=0.0,
            product_type=intent.product_type,
            target_leverage=intent.target_leverage,
            margin_mode=intent.margin_mode,
            exposure_side=intent.exposure_side,
            position_intent=intent.position_intent,
            cancel_reason=None,
            execution_error=None,
            submission_payload=self._state_submission_payload(intent=intent, payload=payload),
        )

    def _rejected_state(
        self,
        *,
        intent: OrderIntent,
        payload: dict[str, str],
        submitted_ts: datetime,
        error: str,
    ) -> OrderState:
        return OrderState(
            decision_id=intent.decision_id,
            intent_id=intent.intent_id,
            symbol=intent.symbol,
            client_order_id=payload["clOrdId"],
            venue="OKX",
            exchange_order_id=None,
            status="REJECTED",
            submission_mode="guarded_simulated_submit",
            exchange_status="rejected",
            exchange_status_history=["rejected"],
            submitted_ts=submitted_ts,
            last_update_ts=submitted_ts,
            last_exchange_update_ts=submitted_ts,
            requested_qty=intent.quantity,
            filled_qty=0.0,
            remaining_qty=intent.quantity,
            average_fill_price=None,
            fees=0.0,
            product_type=intent.product_type,
            target_leverage=intent.target_leverage,
            margin_mode=intent.margin_mode,
            exposure_side=intent.exposure_side,
            position_intent=intent.position_intent,
            cancel_reason=error,
            execution_error=error,
            submission_payload=self._state_submission_payload(intent=intent, payload=payload),
        )

    def _failed_state(
        self,
        *,
        intent: OrderIntent,
        payload: dict[str, str],
        submitted_ts: datetime,
        error: str,
    ) -> OrderState:
        return OrderState(
            decision_id=intent.decision_id,
            intent_id=intent.intent_id,
            symbol=intent.symbol,
            client_order_id=payload["clOrdId"],
            venue="OKX",
            exchange_order_id=None,
            status="FAILED",
            submission_mode="guarded_simulated_submit",
            exchange_status="failed",
            exchange_status_history=["failed"],
            submitted_ts=submitted_ts,
            last_update_ts=submitted_ts,
            last_exchange_update_ts=submitted_ts,
            requested_qty=intent.quantity,
            filled_qty=0.0,
            remaining_qty=intent.quantity,
            average_fill_price=None,
            fees=0.0,
            product_type=intent.product_type,
            target_leverage=intent.target_leverage,
            margin_mode=intent.margin_mode,
            exposure_side=intent.exposure_side,
            position_intent=intent.position_intent,
            cancel_reason=error,
            execution_error=error,
            submission_payload=self._state_submission_payload(intent=intent, payload=payload),
        )

    async def _load_order_detail(
        self,
        *,
        symbol: str,
        order_id: str | None,
        client_order_id: str | None,
    ) -> dict[str, Any] | None:
        try:
            response = await self.client.get_order(
                symbol=symbol,
                order_id=order_id,
                client_order_id=client_order_id,
            )
        except OKXRequestError as exc:
            if exc.path == "/api/v5/trade/order" and exc.code == "51603":
                return None
            raise
        data = response.get("data", [])
        if not data:
            return None
        return dict(data[0])

    @staticmethod
    def _first_row(response: dict[str, Any]) -> dict[str, Any]:
        data = response.get("data", [])
        if not data:
            raise RuntimeError("OKX place_order returned no data")
        return dict(data[0])

    def _map_order_state(
        self,
        *,
        intent: OrderIntent,
        payload: dict[str, str],
        order_row: dict[str, Any],
        submitted_ts: datetime,
    ) -> OrderState:
        exchange_status = str(order_row.get("state") or "live").lower()
        status = self._map_status(exchange_status)
        last_update_ts = self._row_timestamp(order_row.get("uTime")) or utc_now()
        average_fill_price = (
            float(order_row.get("avgPx"))
            if order_row.get("avgPx") not in {None, ""}
            else None
        )
        requested_qty = float(order_row.get("sz", intent.quantity) or intent.quantity)
        filled_qty = float(order_row.get("accFillSz", 0.0) or 0.0)
        remaining_qty = max(requested_qty - filled_qty, 0.0)
        fees = abs(float(order_row.get("fee", 0.0) or 0.0))
        canceled_ts = last_update_ts if status in {"CANCELED", "EXPIRED"} else None
        return OrderState(
            decision_id=intent.decision_id,
            intent_id=intent.intent_id,
            symbol=intent.symbol,
            client_order_id=str(order_row.get("clOrdId") or payload.get("clOrdId") or intent.idempotency_key),
            venue="OKX",
            exchange_order_id=str(order_row.get("ordId")) if order_row.get("ordId") else None,
            status=status,
            submission_mode="guarded_simulated_submit",
            exchange_status=exchange_status,
            exchange_status_history=[exchange_status],
            submitted_ts=self._row_timestamp(order_row.get("cTime")) or submitted_ts,
            last_update_ts=last_update_ts,
            last_exchange_update_ts=last_update_ts,
            cancellation_requested_ts=(
                submitted_ts
                if status == "CANCEL_PENDING"
                else None
            ),
            canceled_ts=canceled_ts,
            requested_qty=requested_qty,
            filled_qty=filled_qty,
            remaining_qty=remaining_qty,
            average_fill_price=average_fill_price,
            fees=fees,
            product_type=intent.product_type,
            target_leverage=intent.target_leverage,
            margin_mode=intent.margin_mode,
            exposure_side=intent.exposure_side,
            position_intent=intent.position_intent,
            cancel_reason=str(order_row.get("cancelSource")) if order_row.get("cancelSource") else None,
            execution_error=None,
            submission_payload=self._state_submission_payload(intent=intent, payload=payload),
        )

    def _map_fill_events(
        self,
        *,
        intent: OrderIntent,
        client_order_id: str,
        exchange_fills: list[ExchangeFill],
    ) -> list[FillEvent]:
        fills: list[FillEvent] = []
        cumulative_qty = 0.0
        sorted_fills = sorted(
            exchange_fills,
            key=lambda item: (item.fill_ts or datetime.fromtimestamp(0, timezone.utc), item.fill_id or ""),
        )
        for fill in sorted_fills:
            if fill.symbol != intent.symbol:
                continue
            cumulative_qty += fill.fill_qty
            fills.append(
                FillEvent(
                    fill_id=fill.fill_id or new_id("okxfill"),
                    decision_id=intent.decision_id,
                    intent_id=intent.intent_id,
                    client_order_id=client_order_id,
                    exchange_order_id=fill.exchange_order_id,
                    symbol=fill.symbol,
                    venue="OKX",
                    side="buy" if fill.side == "buy" else "sell",
                    fill_qty=fill.fill_qty,
                    fill_price=fill.fill_price,
                    fee_amount=fill.fee_amount,
                    fee_currency=fill.fee_currency,
                    product_type=intent.product_type,
                    target_leverage=intent.target_leverage,
                    margin_mode=intent.margin_mode,
                    exposure_side=intent.exposure_side,
                    position_intent=intent.position_intent,
                    liquidity_role="taker",
                    exchange_timestamp=fill.fill_ts or utc_now(),
                    ingestion_timestamp=utc_now(),
                    order_status_after_fill=(
                        "FILLED"
                        if abs(cumulative_qty - intent.quantity) < 1e-12
                        else "PARTIALLY_FILLED"
                    ),
                )
            )
        return fills

    @staticmethod
    def _state_submission_payload(*, intent: OrderIntent, payload: dict[str, str]) -> dict[str, str]:
        state_payload = {key: str(value) for key, value in payload.items()}
        state_payload.setdefault("productType", intent.product_type)
        state_payload.setdefault("marginMode", intent.margin_mode)
        state_payload.setdefault("targetLeverage", str(intent.target_leverage))
        state_payload.setdefault("positionIntent", intent.position_intent)
        state_payload.setdefault("posSide", intent.exposure_side)
        return state_payload

    def _parse_fill_rows(self, payload: dict[str, Any]) -> list[ExchangeFill]:
        rows: list[ExchangeFill] = []
        for row in payload.get("data", []):
            fill_ts = row.get("fillTime") or row.get("ts")
            fill_id = str(row.get("tradeId") or row.get("billId") or row.get("fillId") or "")
            if not fill_id:
                fill_id = f"{row.get('ordId', 'unknown')}-{fill_ts or 'unknown'}"
            rows.append(
                ExchangeFill(
                    fill_id=fill_id,
                    exchange_order_id=str(row.get("ordId") or ""),
                    client_order_id=str(row.get("clOrdId")) if row.get("clOrdId") else None,
                    instrument_id=str(row.get("instId")),
                    symbol=str(row.get("instId")),
                    side=str(row.get("side")),
                    fill_qty=float(row.get("fillSz", row.get("sz", 0.0)) or 0.0),
                    fill_price=float(row.get("fillPx", row.get("px", 0.0)) or 0.0),
                    fee_amount=abs(float(row.get("fee", 0.0) or 0.0)),
                    fee_currency=str(row.get("feeCcy")) if row.get("feeCcy") else None,
                    fill_ts=self._row_timestamp(fill_ts),
                )
            )
        return rows

    @staticmethod
    def _select_exchange_fills(
        *,
        exchange_fills: list[ExchangeFill],
        order_id: str | None,
        client_order_id: str | None,
    ) -> list[ExchangeFill]:
        if order_id:
            filtered = [fill for fill in exchange_fills if fill.exchange_order_id == order_id]
            if filtered:
                return filtered
        if client_order_id:
            filtered = [fill for fill in exchange_fills if fill.client_order_id == client_order_id]
            if filtered:
                return filtered
        if order_id or client_order_id:
            return []
        return exchange_fills

    @staticmethod
    def _map_status(exchange_status: str) -> str:
        normalized = exchange_status.lower()
        mapping = {
            "live": "SUBMITTED",
            "partially_filled": "PARTIALLY_FILLED",
            "filled": "FILLED",
            "canceled": "CANCELED",
            "cancelled": "CANCELED",
            "order_failed": "FAILED",
            "effective": "SUBMITTED",
            "rejected": "REJECTED",
            "failed": "FAILED",
            "expired": "EXPIRED",
        }
        return mapping.get(normalized, "SUBMITTED")

    @staticmethod
    def _row_timestamp(value: Any) -> datetime | None:
        if value in {None, ""}:
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return datetime_from_ms(str(value))

    @staticmethod
    def _intent_from_state(state: OrderState) -> OrderIntent:
        payload = state.submission_payload
        side = str(payload.get("side", "buy"))
        order_type = str(payload.get("ordType", "market"))
        limit_price = float(payload["px"]) if "px" in payload else None
        return OrderIntent(
            intent_id=state.intent_id,
            decision_id=state.decision_id,
            symbol=state.symbol,
            side="buy" if side == "buy" else "sell",
            quantity=state.requested_qty,
            execution_style="exchange",
            order_type="limit" if order_type == "limit" else "market",
            limit_price=limit_price,
            urgency="medium",
            time_in_force="IOC",
            reduce_only=str(payload.get("reduceOnly", "false")).lower() == "true",
            close_only=False,
            idempotency_key=state.client_order_id,
            product_type=state.product_type,
            target_leverage=state.target_leverage,
            margin_mode=state.margin_mode,
            exposure_side=state.exposure_side,
            position_intent=state.position_intent,
        )
