from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any

from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import new_id, utc_now
from aats.schemas.execution import (
    FillEvent,
    OrderIntent,
    OrderState,
    close_only_from_position_intent,
    default_close_only_reason,
    default_reduce_only_reason,
    execution_action_from_position_intent,
    pos_side_from_position_intent,
    reduce_only_from_position_intent,
)
from aats.schemas.exchange import ExchangeAccountSnapshot, ExchangeFill, ExchangePosition, InstrumentMetadata
from aats.services.execution_engine.exchange_adapter import ExchangeAdapter
from aats.services.execution_engine.okx_account import OKXAccountService, datetime_from_ms
from aats.services.execution_engine.okx_rest import OKXRESTClient, OKXRequestError
from aats.services.governance_engine.health import SystemHealthService
from aats.services.governance_engine.mode import RuntimeModeController
from aats.services.governance_engine.runtime_layers import EnvironmentCapabilities, PolicyProfile
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, to_decimal
from aats.bootstrap.logging import correlation_fields, get_logger, log_event
from aats.storage.base import ExecutionObligationRepository


OKX_DEMO_SUBMISSION_TARGETS = {"okx_demo_spot", "okx_demo_derivatives"}
OKX_LIVE_SUBMISSION_TARGETS = {"okx_live_spot", "okx_live_derivatives"}
OKX_SUPPORTED_SUBMISSION_TARGETS = OKX_DEMO_SUBMISSION_TARGETS | OKX_LIVE_SUBMISSION_TARGETS


class OKXOrderPayloadBuilder:
    def build(
        self,
        *,
        intent: OrderIntent,
        instrument: InstrumentMetadata,
        validate: bool = True,
    ) -> dict[str, str]:
        payload = self._base_payload(intent=intent, instrument=instrument, validate=validate)
        if intent.product_type == "derivatives":
            payload.update(self._build_derivatives_payload(intent=intent))
        else:
            payload.update(self._build_spot_payload(intent=intent))
        return payload

    def _base_payload(
        self,
        *,
        intent: OrderIntent,
        instrument: InstrumentMetadata,
        validate: bool,
    ) -> dict[str, str]:
        quantity = self._rounded_exchange_quantity(
            intent=intent,
            instrument=instrument,
            validate=validate,
        )
        payload = {
            "instId": instrument.instrument_id,
            "tdMode": intent.td_mode or ("cash" if intent.product_type == "spot" else intent.margin_mode),
            "side": intent.side,
            "ordType": self._order_type(intent),
            "sz": self._render_decimal(quantity),
            "clOrdId": self._client_order_id(intent),
        }
        if intent.order_type == "limit" and intent.limit_price is not None:
            limit_price = self._round_down(value=intent.limit_price, step=instrument.tick_size)
            payload["px"] = self._render_decimal(limit_price)
        return payload

    def _build_derivatives_payload(self, *, intent: OrderIntent) -> dict[str, str]:
        payload = {
            "lever": self._render_decimal(max(to_decimal(intent.target_leverage), Decimal("1"))),
        }
        pos_side = intent.pos_side or pos_side_from_position_intent(
            position_intent=intent.position_intent,
            position_mode=intent.position_mode,
        )
        if pos_side in {"long", "short"}:
            payload["posSide"] = pos_side
        if intent.reduce_only:
            payload["reduceOnly"] = "true"
        return payload

    @staticmethod
    def _build_spot_payload(*, intent: OrderIntent) -> dict[str, str]:
        if intent.order_type == "market" and intent.side == "buy":
            return {"tgtCcy": "base_ccy"}
        return {}

    @staticmethod
    def _order_type(intent: OrderIntent) -> str:
        if intent.order_type != "limit":
            return intent.order_type
        tif = str(intent.time_in_force or "IOC").upper()
        if tif == "IOC":
            return "ioc"
        if tif == "FOK":
            return "fok"
        return "limit"

    @staticmethod
    def _exchange_quantity(*, intent: OrderIntent, instrument: InstrumentMetadata) -> Decimal:
        if intent.product_type != "derivatives":
            return intent.quantity
        contract_value = max(instrument.contract_value, Decimal("0"))
        if contract_value <= 0:
            return intent.quantity
        return intent.quantity / contract_value

    def _rounded_exchange_quantity(
        self,
        *,
        intent: OrderIntent,
        instrument: InstrumentMetadata,
        validate: bool = True,
    ) -> Decimal:
        quantity = self._exchange_quantity(intent=intent, instrument=instrument)
        quantity = self._round_down(value=quantity, step=instrument.lot_size)
        if validate and (quantity <= 0 or quantity < instrument.min_size):
            raise ValueError("okx_order_quantity_below_min_size")
        return max(quantity, Decimal("0"))

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
    def _round_down(*, value: Decimal, step: Decimal) -> Decimal:
        if step <= 0:
            return value
        ratio = value / step
        return ratio.quantize(Decimal("1"), rounding=ROUND_DOWN) * step

    @staticmethod
    def _render_decimal(value: Decimal) -> str:
        rendered = format(value.normalize(), "f")
        return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


class OKXExecutionAdapter(ExchangeAdapter):
    def __init__(
        self,
        *,
        settings: AATSSettings,
        client: OKXRESTClient,
        account_service: OKXAccountService,
        mode_controller: RuntimeModeController,
        obligation_repo: ExecutionObligationRepository | None = None,
        environment_capabilities: EnvironmentCapabilities | None = None,
        policy_profile: PolicyProfile | None = None,
        health_service: SystemHealthService | None = None,
        price_provider: Callable[[str], Decimal] | None = None,
        payload_builder: OKXOrderPayloadBuilder | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.account_service = account_service
        self.mode_controller = mode_controller
        self.obligation_repo = obligation_repo
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

        intent = self._normalize_intent_for_account_snapshot(intent=intent, snapshot=snapshot)
        try:
            payload = self.payload_builder.build(intent=intent, instrument=instrument)
        except ValueError as exc:
            reason = str(exc) or "okx_payload_build_rejected"
            payload = self.payload_builder.build(intent=intent, instrument=instrument, validate=False)
            self._last_submission_payload = dict(payload)
            self._last_error = reason
            self._log_blocked_submit(intent=intent, reason=reason)
            return self._blocked_state(intent=intent, payload=payload, reason=reason), []
        payload = self._normalize_payload_for_account_mode(
            payload=payload,
            position_mode=intent.position_mode,
        )
        self._last_submission_payload = dict(payload)
        submitted_ts = utc_now()
        gate_error = self._submission_gate_error(intent=intent)
        if gate_error is not None:
            self._last_error = gate_error
            self._log_blocked_submit(intent=intent, reason=gate_error)
            return self._blocked_state(intent=intent, payload=payload, reason=gate_error), []
        semantic_error = self._derivatives_submission_semantic_error(
            intent=intent,
            instrument=instrument,
            snapshot=snapshot,
            payload=payload,
        )
        if semantic_error is not None:
            self._last_error = semantic_error
            self._log_blocked_submit(intent=intent, reason=semantic_error)
            return self._blocked_state(intent=intent, payload=payload, reason=semantic_error), []
        max_size_error = await self._max_size_gate_error(intent=intent, payload=payload)
        if max_size_error is not None:
            self._last_error = max_size_error
            self._log_blocked_submit(intent=intent, reason=max_size_error)
            return self._blocked_state(intent=intent, payload=payload, reason=max_size_error), []
        slippage_error = self._slippage_gate_error(intent=intent)
        if slippage_error is not None:
            self._last_error = slippage_error
            self._log_blocked_submit(intent=intent, reason=slippage_error)
            return self._blocked_state(intent=intent, payload=payload, reason=slippage_error), []

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
            accepted_state = self._submitted_state(
                intent=intent,
                payload=payload,
                submitted_ts=submitted_ts,
                client_order_id=client_order_id,
                order_id=order_id,
            )
            try:
                order_detail = await self._load_order_detail(
                    symbol=intent.symbol,
                    order_id=order_id,
                    client_order_id=client_order_id,
                )
                if order_detail is None:
                    self._last_error = None
                    return accepted_state, []
                state = self._map_order_state(
                    intent=intent,
                    payload=payload,
                    order_row=order_detail,
                    submitted_ts=submitted_ts,
                )
            except Exception as exc:
                self._last_error = str(exc)
                return self._recoverable_order_state(
                    state=accepted_state,
                    error=str(exc),
                ), []

            try:
                exchange_fills = self._latest_private_order_fills(
                    symbol=intent.symbol,
                    order_id=state.exchange_order_id,
                    client_order_id=state.client_order_id,
                )
                if not exchange_fills:
                    fills_payload = await self.client.get_fills(
                        symbol=intent.symbol,
                        order_id=state.exchange_order_id,
                        limit=self.settings.okx_fill_fetch_limit,
                    )
                    exchange_fills = self._select_exchange_fills(
                        exchange_fills=self._parse_fill_rows(fills_payload),
                        order_id=state.exchange_order_id,
                        client_order_id=state.client_order_id,
                    )
                fills = self._map_fill_events(
                    intent=self._intent_from_state(state),
                    client_order_id=state.client_order_id,
                    exchange_fills=exchange_fills,
                )
                self._last_error = None
                return state, fills
            except Exception as exc:
                self._last_error = str(exc)
                return self._recoverable_order_state(
                    state=state,
                    error=str(exc),
                ), []
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

            intent = self._intent_from_state(order_state)
            try:
                order_detail = await self._load_order_detail(
                    symbol=order_state.symbol,
                    order_id=order_state.exchange_order_id,
                    client_order_id=order_state.client_order_id,
                )
                if order_detail is None:
                    self._last_error = None
                    return cancel_pending, []
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
            except Exception as exc:
                self._last_error = str(exc)
                return self._recoverable_order_state(
                    state=cancel_pending,
                    error=str(exc),
                ), []
            try:
                exchange_fills = self._latest_private_order_fills(
                    symbol=order_state.symbol,
                    order_id=order_state.exchange_order_id,
                    client_order_id=order_state.client_order_id,
                )
                if not exchange_fills:
                    fills_payload = await self.client.get_fills(
                        symbol=order_state.symbol,
                        order_id=order_state.exchange_order_id,
                        limit=self.settings.okx_fill_fetch_limit,
                    )
                    exchange_fills = self._select_exchange_fills(
                        exchange_fills=self._parse_fill_rows(fills_payload),
                        order_id=order_state.exchange_order_id,
                        client_order_id=order_state.client_order_id,
                    )
                fills = self._map_fill_events(
                    intent=self._intent_from_state(state),
                    client_order_id=order_state.client_order_id,
                    exchange_fills=exchange_fills,
                )
                self._last_error = None
                return state, fills
            except Exception as exc:
                self._last_error = str(exc)
                return self._recoverable_order_state(
                    state=state,
                    error=str(exc),
                ), []
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
                if self._load_private_order_detail_only(symbol=state.symbol, client_order_id=state.client_order_id) is None:
                    continue
            refreshed_state = state
            try:
                order_detail = await self._load_order_detail(
                    symbol=state.symbol,
                    order_id=state.exchange_order_id,
                    client_order_id=state.client_order_id,
                )
                if order_detail is not None:
                    intent = self._intent_from_state(state)
                    refreshed_state = self._map_order_state(
                        intent=intent,
                        payload=state.submission_payload,
                        order_row=order_detail,
                        submitted_ts=state.submitted_ts or utc_now(),
                    )
                exchange_fills = self._latest_private_order_fills(
                    symbol=state.symbol,
                    order_id=refreshed_state.exchange_order_id,
                    client_order_id=refreshed_state.client_order_id,
                )
                if not exchange_fills:
                    fills_payload = await self.client.get_fills(
                        symbol=state.symbol,
                        order_id=refreshed_state.exchange_order_id,
                        limit=self.settings.okx_fill_fetch_limit,
                    )
                    exchange_fills = self._select_exchange_fills(
                        exchange_fills=self._parse_fill_rows(fills_payload),
                        order_id=refreshed_state.exchange_order_id,
                        client_order_id=refreshed_state.client_order_id,
                    )
                fills.extend(
                    self._map_fill_events(
                        intent=self._intent_from_state(refreshed_state),
                        client_order_id=refreshed_state.client_order_id,
                        exchange_fills=exchange_fills,
                    )
                )
                refreshed_states.append(refreshed_state)
                self._last_error = None
            except Exception as exc:
                self._last_error = str(exc)
                refreshed_states.append(
                    self._recoverable_order_state(state=refreshed_state, error=str(exc))
                )
        return refreshed_states, fills

    def _load_private_order_detail_only(
        self,
        *,
        symbol: str,
        client_order_id: str | None,
    ) -> dict[str, Any] | None:
        if client_order_id is None:
            return None
        private_ws_lookup = getattr(self.account_service, "latest_private_order_row", None)
        if not callable(private_ws_lookup):
            return None
        row = private_ws_lookup(symbol=symbol, order_id=None, client_order_id=client_order_id)
        return row if isinstance(row, dict) else None

    def readiness(self) -> dict[str, Any]:
        account_status = self.account_service.status()
        gate_status = self._gate_status()
        effective_taker_fee_bps = None
        if hasattr(self.account_service, "effective_taker_fee_bps"):
            effective_taker_fee_bps = self.account_service.effective_taker_fee_bps()  # type: ignore[call-arg]
        return {
            "ready": account_status["credentials_configured"] and account_status["enabled"],
            "backend": "okx",
            "mode": self.mode_controller.mode,
            "execution_mode": self._execution_mode_label(),
            "live_submit_enabled": self.settings.live_submit_enabled,
            "guarded_execution_dry_run": self.settings.guarded_execution_dry_run,
            "okx_simulated_trading": self.settings.okx_simulated_trading,
            "exchange_submit_allowed": gate_status["exchange_submit_allowed"],
            "submit_blocked_reasons": gate_status["submit_blocked_reasons"],
            "safety_gates": gate_status["safety_gates"],
            "last_error": self._last_error,
            "last_submission_payload": self._last_submission_payload,
            "okx_max_order_quantity_precheck_enabled": self.settings.okx_max_order_quantity_precheck_enabled,
            "effective_taker_fee_bps": effective_taker_fee_bps,
            "account_status": account_status,
            "environment_capabilities": self.environment_capabilities.to_dict(),
            "policy_profile": self.policy_profile.to_dict(),
        }

    def _log_blocked_submit(self, *, intent: OrderIntent, reason: str) -> None:
        log_event(
            self.logger,
            "okx_submit_blocked",
            level="warning",
            **correlation_fields(
                decision_id=intent.decision_id,
                intent_id=intent.intent_id,
                symbol=intent.symbol,
                reason=reason,
            ),
        )

    def _submission_target(self) -> str:
        return str(self.environment_capabilities.exchange_submission_target or "")

    def _execution_mode_label(self) -> str:
        target = self._submission_target()
        if target == "okx_demo_derivatives":
            return "guarded_simulated_submit_derivatives"
        if target == "okx_demo_spot":
            return "guarded_simulated_submit"
        if target == "okx_live_derivatives":
            return "guarded_live_submit_derivatives"
        if target == "okx_live_spot":
            return "guarded_live_submit"
        return "guarded_live_blocked"

    def _submitted_order_mode(self) -> str:
        target = self._submission_target()
        if target in OKX_LIVE_SUBMISSION_TARGETS:
            return "guarded_live_submit"
        return "guarded_simulated_submit"

    def _dry_run_order_mode(self) -> str:
        target = self._submission_target()
        if target in OKX_LIVE_SUBMISSION_TARGETS:
            return "guarded_live_dry_run"
        if target in OKX_DEMO_SUBMISSION_TARGETS:
            return "guarded_simulated_dry_run"
        return "guarded_dry_run"

    def _normalize_intent_for_account_snapshot(
        self,
        *,
        intent: OrderIntent,
        snapshot: ExchangeAccountSnapshot,
    ) -> OrderIntent:
        account_position_mode = self._account_position_mode(snapshot)
        position_mode = (
            intent.position_mode
            if intent.position_mode in {"net_mode", "long_short_mode"}
            else account_position_mode
        )
        pos_side = intent.pos_side
        if pos_side in {None, ""}:
            pos_side = pos_side_from_position_intent(
                position_intent=intent.position_intent,
                position_mode=position_mode,
            )
        td_mode = intent.td_mode or ("cash" if intent.product_type == "spot" else intent.margin_mode)
        reduce_only = bool(intent.reduce_only or reduce_only_from_position_intent(intent.position_intent))
        close_only = bool(intent.close_only or close_only_from_position_intent(intent.position_intent))
        if intent.only_reduce_required and position_mode in {"net_mode", "long_short_mode"}:
            reducible_qty = self._reducible_position_quantity(
                snapshot=snapshot,
                symbol=intent.symbol,
                position_mode=position_mode,
                pos_side=pos_side,
                side=intent.side,
            )
            if reducible_qty > EPSILON_DECIMAL_12:
                reduce_only = True
        return intent.model_copy(
            update={
                "td_mode": td_mode,
                "position_mode": position_mode,
                "pos_side": pos_side,
                "reduce_only": reduce_only,
                "close_only": close_only,
                "reduce_only_reason": (
                    intent.reduce_only_reason
                    or default_reduce_only_reason(
                        position_intent=intent.position_intent,
                        reduce_only=reduce_only,
                    )
                    or ("risk_only_reduce_required" if intent.only_reduce_required and reduce_only else None)
                ),
                "close_only_reason": (
                    intent.close_only_reason
                    or default_close_only_reason(
                        position_intent=intent.position_intent,
                        close_only=close_only,
                    )
                ),
            }
        )

    def _derivatives_submission_semantic_error(
        self,
        *,
        intent: OrderIntent,
        instrument: InstrumentMetadata,
        snapshot: ExchangeAccountSnapshot,
        payload: dict[str, str],
    ) -> str | None:
        if intent.product_type != "derivatives":
            return None
        td_mode = str(intent.td_mode or intent.margin_mode or "").strip().lower()
        if td_mode not in {"cross", "isolated"}:
            return "okx_td_mode_incompatible_with_derivatives"
        if intent.margin_mode in {"cross", "isolated"} and td_mode != intent.margin_mode:
            return "okx_td_mode_margin_mode_mismatch"

        account_position_mode = self._account_position_mode(snapshot)
        if account_position_mode in {None, ""}:
            return "okx_position_mode_missing"
        if intent.position_mode not in {None, "", account_position_mode}:
            return "okx_position_mode_mismatch"

        pos_side = None if intent.pos_side in {None, ""} else str(intent.pos_side)
        if account_position_mode == "long_short_mode":
            if pos_side not in {"long", "short"}:
                return "okx_pos_side_missing_for_long_short_mode"
            expected_pos_side = pos_side_from_position_intent(
                position_intent=intent.position_intent,
                position_mode="long_short_mode",
            )
            if expected_pos_side in {"long", "short"} and pos_side != expected_pos_side:
                return "okx_pos_side_mismatch_with_position_intent"
        elif pos_side not in {None, "net"}:
            return "okx_pos_side_disallowed_for_net_mode"

        exchange_qty = to_decimal(payload.get("sz", "0"))
        max_size = (
            instrument.max_limit_size
            if intent.order_type == "limit"
            else instrument.max_market_size
        )
        if max_size is not None and max_size > Decimal("0") and exchange_qty - max_size > EPSILON_DECIMAL_12:
            return "okx_order_size_exceeds_instrument_limit"
        if (
            instrument.max_leverage is not None
            and instrument.max_leverage > Decimal("0")
            and to_decimal(intent.target_leverage) - instrument.max_leverage > EPSILON_DECIMAL_12
        ):
            return "okx_leverage_exceeds_instrument_limit"

        reducible_qty = self._reducible_position_quantity(
            snapshot=snapshot,
            symbol=intent.symbol,
            position_mode=account_position_mode,
            pos_side=pos_side,
            side=intent.side,
        )
        reduce_path = bool(
            intent.reduce_only
            or intent.only_reduce_required
            or reduce_only_from_position_intent(intent.position_intent)
            or intent.execution_action in {"reduce", "exit"}
        )
        close_path = bool(
            intent.close_only
            or close_only_from_position_intent(intent.position_intent)
        )
        if close_path and not reduce_path:
            return "okx_close_only_requires_reduce_only"
        if close_path and reducible_qty <= EPSILON_DECIMAL_12:
            return "okx_close_only_without_reducible_position"
        if close_path and intent.quantity - reducible_qty > EPSILON_DECIMAL_12:
            return "okx_close_only_exceeds_reducible_position"
        if reduce_path and reducible_qty <= EPSILON_DECIMAL_12:
            return "okx_reduce_only_without_reducible_position"
        if reduce_path and intent.quantity - reducible_qty > EPSILON_DECIMAL_12:
            return (
                "okx_reduce_only_required_by_risk"
                if intent.only_reduce_required
                else "okx_reduce_only_would_increase_exposure"
            )
        if intent.only_reduce_required and not reduce_path:
            return "okx_reduce_only_required_by_risk"
        return None

    @staticmethod
    def _account_position_mode(snapshot: ExchangeAccountSnapshot) -> str | None:
        if snapshot.account_configuration is not None and snapshot.account_configuration.position_mode not in {None, ""}:
            return str(snapshot.account_configuration.position_mode)
        if snapshot.position_mode in {None, ""}:
            return None
        return str(snapshot.position_mode)

    def _reducible_position_quantity(
        self,
        *,
        snapshot: ExchangeAccountSnapshot,
        symbol: str,
        position_mode: str | None,
        pos_side: str | None,
        side: str,
    ) -> Decimal:
        positions = [position for position in snapshot.positions if position.symbol == symbol]
        if position_mode == "long_short_mode":
            if pos_side not in {"long", "short"}:
                return Decimal("0")
            matching_positions = [
                position
                for position in positions
                if str(position.side or "").lower() == pos_side
            ]
            reducible = sum((abs(to_decimal(position.quantity)) for position in matching_positions), start=Decimal("0"))
            if pos_side == "long" and side != "sell":
                return Decimal("0")
            if pos_side == "short" and side != "buy":
                return Decimal("0")
            return reducible

        net_quantity = sum((self._signed_position_quantity(position) for position in positions), start=Decimal("0"))
        if net_quantity > EPSILON_DECIMAL_12 and side == "sell":
            return net_quantity
        if net_quantity < -EPSILON_DECIMAL_12 and side == "buy":
            return abs(net_quantity)
        return Decimal("0")

    @staticmethod
    def _signed_position_quantity(position: ExchangePosition) -> Decimal:
        quantity = to_decimal(position.quantity)
        side = str(position.side or "").strip().lower()
        if side == "short":
            return -abs(quantity)
        if side == "long":
            return abs(quantity)
        return quantity

    def _submission_gate_error(self, *, intent: OrderIntent) -> str | None:
        if self.mode_controller.kill_switch.halted:
            return "kill_switch_active"
        if self.environment_capabilities.execution_adapter_kind != "okx":
            return "mode_not_guarded_live"
        if self.policy_profile.dry_run_only:
            return "guarded_execution_dry_run"
        if not self.environment_capabilities.exchange_submission_enabled:
            return "live_submit_disabled"
        if self._submission_target() not in OKX_SUPPORTED_SUBMISSION_TARGETS:
            return "okx_simulated_trading_required"
        if intent.symbol not in self.settings.allowed_symbols:
            return "symbol_not_allowed"
        if self._current_open_order_count(intent.symbol) >= self.settings.max_open_orders:
            return "max_open_orders_reached"
        price = to_decimal(self.price_provider(intent.symbol)) if self.price_provider is not None else Decimal("0")
        if price > 0 and (intent.quantity * price) > to_decimal(self.settings.max_notional_per_symbol):
            return "max_notional_per_symbol_exceeded"
        account_status = self.account_service.status()
        if not account_status.get("ready", False):
            return "account_not_ready"
        if self.health_service is not None:
            blockers = self.health_service.execution_blockers()
            if blockers:
                return blockers[0]
        return None

    async def _max_size_gate_error(self, *, intent: OrderIntent, payload: dict[str, str]) -> str | None:
        if not self.settings.okx_max_order_quantity_precheck_enabled:
            return None
        requested_size = payload.get("sz")
        td_mode = payload.get("tdMode")
        if requested_size in {None, ""} or td_mode in {None, ""}:
            return None
        reference_price = intent.limit_price or intent.reference_price
        if reference_price is None and self.price_provider is not None:
            reference_price = to_decimal(self.price_provider(intent.symbol))
        try:
            response = await self.client.get_max_order_quantity(
                symbol=intent.symbol,
                td_mode=str(td_mode),
                leverage=intent.target_leverage if intent.product_type == "derivatives" else None,
                price=reference_price,
            )
        except Exception as exc:
            return f"okx_max_order_quantity_precheck_failed:{type(exc).__name__}"
        row = self._first_row(response)
        requested = to_decimal(requested_size)
        max_allowed = self._max_size_from_row(row=row, side=intent.side)
        if max_allowed is None or max_allowed <= Decimal("0"):
            return None
        if requested - max_allowed > Decimal("1e-12"):
            return f"okx_max_order_quantity_exceeded:{requested}>{max_allowed}"
        return None

    def _gate_status(self) -> dict[str, Any]:
        account_status = self.account_service.status()
        health_blockers = self.health_service.execution_blockers() if self.health_service is not None else []
        submission_target = self._submission_target()
        submission_target_supported = submission_target in OKX_SUPPORTED_SUBMISSION_TARGETS
        submission_target_is_demo = submission_target in OKX_DEMO_SUBMISSION_TARGETS
        submission_target_is_live = submission_target in OKX_LIVE_SUBMISSION_TARGETS
        okx_environment_matches_target = (
            (self.settings.okx_simulated_trading and submission_target_is_demo)
            or (not self.settings.okx_simulated_trading and submission_target_is_live)
        )
        safety_gates = {
            "mode_is_guarded_live": self.environment_capabilities.execution_adapter_kind == "okx",
            "execution_backend_is_okx": self.environment_capabilities.execution_adapter_kind == "okx",
            "submission_target_supported": submission_target_supported,
            "submission_target_is_demo": submission_target_is_demo,
            "submission_target_is_live": submission_target_is_live,
            "okx_environment_matches_target": okx_environment_matches_target,
            "live_submit_enabled": self.environment_capabilities.exchange_submission_enabled,
            "dry_run_disabled": not self.policy_profile.dry_run_only,
            "halt_state_clear": not self.mode_controller.kill_switch.halted,
            "account_ready": bool(account_status.get("ready", False)),
            "health_checks_clear": not health_blockers,
            "symbol_allowlist_configured": bool(self.settings.allowed_symbols),
            "max_notional_cap_configured": self.settings.max_notional_per_symbol > 0.0,
            "max_open_orders_configured": self.settings.max_open_orders > 0,
        }
        blocked_reasons: list[str] = []
        if not safety_gates["halt_state_clear"]:
            blocked_reasons.append("kill_switch_active")
        if not safety_gates["mode_is_guarded_live"]:
            blocked_reasons.append("mode_not_guarded_live")
        if not safety_gates["execution_backend_is_okx"]:
            blocked_reasons.append("execution_backend_not_okx")
        if not safety_gates["submission_target_supported"] or not safety_gates["okx_environment_matches_target"]:
            blocked_reasons.append("okx_simulated_trading_required")
        if not safety_gates["live_submit_enabled"]:
            blocked_reasons.append("live_submit_disabled")
        if not safety_gates["dry_run_disabled"]:
            blocked_reasons.append("guarded_execution_dry_run")
        if not safety_gates["account_ready"]:
            blocked_reasons.append("account_not_ready")
        blocked_reasons.extend([reason for reason in health_blockers if reason not in blocked_reasons])
        return {
            "exchange_submit_allowed": not blocked_reasons,
            "submit_blocked_reasons": blocked_reasons,
            "safety_gates": safety_gates,
        }

    def _current_open_order_count(self, symbol: str) -> int:
        exchange_count = self.account_service.open_order_count(symbol=symbol)
        if self.obligation_repo is None:
            return exchange_count
        snapshot_getter = getattr(self.account_service, "latest_snapshot", None)
        snapshot = snapshot_getter() if callable(snapshot_getter) else None
        visible_client_order_ids = {
            order.client_order_id
            for order in (snapshot.open_orders if snapshot is not None else [])
            if order.client_order_id
        }
        local_pending = sum(
            1
            for obligation in self.obligation_repo.active_obligations()
            if obligation.symbol == symbol and obligation.client_order_id not in visible_client_order_ids
        )
        return exchange_count + local_pending

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
            submission_mode=self._dry_run_order_mode() if status == "DRY_RUN" else "guarded_blocked",
            exchange_status="blocked",
            exchange_status_history=["blocked"],
            submitted_ts=now,
            last_update_ts=now,
            last_exchange_update_ts=now,
            requested_qty=intent.quantity,
            filled_qty=Decimal("0"),
            remaining_qty=intent.quantity,
            average_fill_price=None,
            fees=Decimal("0"),
            reduce_only=intent.reduce_only,
            close_only=intent.close_only,
            td_mode=intent.td_mode,
            position_mode=intent.position_mode,
            pos_side=intent.pos_side,
            reduce_only_reason=intent.reduce_only_reason,
            close_only_reason=intent.close_only_reason,
            instrument_family=intent.instrument_family,
            settle_currency=intent.settle_currency,
            product_type=intent.product_type,
            target_leverage=intent.target_leverage,
            margin_mode=intent.margin_mode,
            exposure_side=intent.exposure_side,
            execution_action=intent.execution_action,
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
            submission_mode=self._submitted_order_mode(),
            exchange_status="live",
            exchange_status_history=["live"],
            submitted_ts=submitted_ts,
            last_update_ts=submitted_ts,
            last_exchange_update_ts=submitted_ts,
            requested_qty=intent.quantity,
            filled_qty=Decimal("0"),
            remaining_qty=intent.quantity,
            average_fill_price=None,
            fees=Decimal("0"),
            reduce_only=intent.reduce_only,
            close_only=intent.close_only,
            td_mode=intent.td_mode,
            position_mode=intent.position_mode,
            pos_side=intent.pos_side,
            reduce_only_reason=intent.reduce_only_reason,
            close_only_reason=intent.close_only_reason,
            instrument_family=intent.instrument_family,
            settle_currency=intent.settle_currency,
            product_type=intent.product_type,
            target_leverage=intent.target_leverage,
            margin_mode=intent.margin_mode,
            exposure_side=intent.exposure_side,
            execution_action=intent.execution_action,
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
            submission_mode=self._submitted_order_mode(),
            exchange_status="rejected",
            exchange_status_history=["rejected"],
            submitted_ts=submitted_ts,
            last_update_ts=submitted_ts,
            last_exchange_update_ts=submitted_ts,
            requested_qty=intent.quantity,
            filled_qty=Decimal("0"),
            remaining_qty=intent.quantity,
            average_fill_price=None,
            fees=Decimal("0"),
            reduce_only=intent.reduce_only,
            close_only=intent.close_only,
            td_mode=intent.td_mode,
            position_mode=intent.position_mode,
            pos_side=intent.pos_side,
            reduce_only_reason=intent.reduce_only_reason,
            close_only_reason=intent.close_only_reason,
            instrument_family=intent.instrument_family,
            settle_currency=intent.settle_currency,
            product_type=intent.product_type,
            target_leverage=intent.target_leverage,
            margin_mode=intent.margin_mode,
            exposure_side=intent.exposure_side,
            execution_action=intent.execution_action,
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
            submission_mode=self._submitted_order_mode(),
            exchange_status="failed",
            exchange_status_history=["failed"],
            submitted_ts=submitted_ts,
            last_update_ts=submitted_ts,
            last_exchange_update_ts=submitted_ts,
            requested_qty=intent.quantity,
            filled_qty=Decimal("0"),
            remaining_qty=intent.quantity,
            average_fill_price=None,
            fees=Decimal("0"),
            reduce_only=intent.reduce_only,
            close_only=intent.close_only,
            td_mode=intent.td_mode,
            position_mode=intent.position_mode,
            pos_side=intent.pos_side,
            reduce_only_reason=intent.reduce_only_reason,
            close_only_reason=intent.close_only_reason,
            instrument_family=intent.instrument_family,
            settle_currency=intent.settle_currency,
            product_type=intent.product_type,
            target_leverage=intent.target_leverage,
            margin_mode=intent.margin_mode,
            exposure_side=intent.exposure_side,
            execution_action=intent.execution_action,
            position_intent=intent.position_intent,
            cancel_reason=error,
            execution_error=error,
            submission_payload=self._state_submission_payload(intent=intent, payload=payload),
        )

    @staticmethod
    def _recoverable_order_state(
        *,
        state: OrderState,
        error: str,
    ) -> OrderState:
        return state.model_copy(
            update={
                "last_update_ts": utc_now(),
                "execution_error": error,
            }
        )

    async def _load_order_detail(
        self,
        *,
        symbol: str,
        order_id: str | None,
        client_order_id: str | None,
    ) -> dict[str, Any] | None:
        private_ws_lookup = getattr(self.account_service, "latest_private_order_row", None)
        if callable(private_ws_lookup):
            private_row = private_ws_lookup(symbol=symbol, order_id=order_id, client_order_id=client_order_id)
            if private_row is not None:
                return private_row
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

    def _latest_private_order_fills(
        self,
        *,
        symbol: str,
        order_id: str | None,
        client_order_id: str | None,
    ) -> list[ExchangeFill]:
        private_ws_lookup = getattr(self.account_service, "latest_private_order_fills", None)
        if not callable(private_ws_lookup):
            return []
        rows = private_ws_lookup(symbol=symbol, order_id=order_id, client_order_id=client_order_id)
        return rows if isinstance(rows, list) else []

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
            to_decimal(order_row.get("avgPx"))
            if order_row.get("avgPx") not in {None, ""}
            else None
        )
        instrument = self.account_service.instrument_metadata(intent.symbol)
        requested_qty = self._internal_quantity(
            symbol=intent.symbol,
            quantity=to_decimal(order_row.get("sz", intent.quantity)),
            instrument=instrument,
            product_type=intent.product_type,
        )
        filled_qty = self._internal_quantity(
            symbol=intent.symbol,
            quantity=to_decimal(order_row.get("accFillSz", "0")),
            instrument=instrument,
            product_type=intent.product_type,
        )
        remaining_qty = max(requested_qty - filled_qty, Decimal("0"))
        fees = abs(to_decimal(order_row.get("fee", "0")))
        canceled_ts = last_update_ts if status in {"CANCELED", "EXPIRED"} else None
        return OrderState(
            decision_id=intent.decision_id,
            intent_id=intent.intent_id,
            symbol=intent.symbol,
            client_order_id=str(order_row.get("clOrdId") or payload.get("clOrdId") or intent.idempotency_key),
            venue="OKX",
            exchange_order_id=str(order_row.get("ordId")) if order_row.get("ordId") else None,
            status=status,
            submission_mode=self._submitted_order_mode(),
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
            reduce_only=intent.reduce_only,
            close_only=intent.close_only,
            td_mode=intent.td_mode or str(order_row.get("tdMode") or payload.get("tdMode") or intent.margin_mode),
            position_mode=intent.position_mode,
            pos_side=(
                str(order_row.get("posSide"))
                if order_row.get("posSide") not in {None, ""}
                else intent.pos_side
            ),
            reduce_only_reason=intent.reduce_only_reason,
            close_only_reason=intent.close_only_reason,
            instrument_family=intent.instrument_family,
            settle_currency=intent.settle_currency,
            product_type=intent.product_type,
            target_leverage=intent.target_leverage,
            margin_mode=intent.margin_mode,
            exposure_side=intent.exposure_side,
            execution_action=intent.execution_action,
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
        cumulative_qty = Decimal("0")
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
                    reduce_only=intent.reduce_only,
                    close_only=intent.close_only,
                    td_mode=intent.td_mode,
                    position_mode=intent.position_mode,
                    pos_side=intent.pos_side,
                    reduce_only_reason=intent.reduce_only_reason,
                    close_only_reason=intent.close_only_reason,
                    instrument_family=intent.instrument_family,
                    settle_currency=intent.settle_currency,
                    product_type=intent.product_type,
                    target_leverage=intent.target_leverage,
                    margin_mode=intent.margin_mode,
                    exposure_side=intent.exposure_side,
                    execution_action=intent.execution_action,
                    position_intent=intent.position_intent,
                    liquidity_role="taker",
                    exchange_timestamp=fill.fill_ts or utc_now(),
                    ingestion_timestamp=utc_now(),
                    order_status_after_fill=(
                        "FILLED"
                        if abs(cumulative_qty - intent.quantity) <= EPSILON_DECIMAL_12
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
        state_payload.setdefault("tdMode", intent.td_mode or intent.margin_mode)
        state_payload.setdefault("targetLeverage", str(intent.target_leverage))
        if intent.execution_action is not None:
            state_payload.setdefault("executionAction", intent.execution_action)
        state_payload.setdefault("positionIntent", intent.position_intent)
        state_payload.setdefault("positionMode", intent.position_mode or "")
        state_payload.setdefault("posSide", intent.pos_side or "")
        state_payload.setdefault("reduceOnly", "true" if intent.reduce_only else "false")
        state_payload.setdefault("closeOnly", "true" if intent.close_only else "false")
        state_payload.setdefault("reduceOnlyReason", intent.reduce_only_reason or "")
        state_payload.setdefault("closeOnlyReason", intent.close_only_reason or "")
        state_payload.setdefault("instrumentFamily", intent.instrument_family or "")
        state_payload.setdefault("settleCurrency", intent.settle_currency or "")
        state_payload.setdefault("requiredInitialMargin", "" if intent.required_initial_margin is None else str(intent.required_initial_margin))
        state_payload.setdefault("projectedMarginUsage", "" if intent.projected_margin_usage is None else str(intent.projected_margin_usage))
        state_payload.setdefault("projectedNotional", "" if intent.projected_notional is None else str(intent.projected_notional))
        state_payload.setdefault("onlyReduceRequired", "true" if intent.only_reduce_required else "false")
        state_payload.setdefault("riskLimitBreached", "true" if intent.risk_limit_breached else "false")
        state_payload.setdefault(
            "liquidationBufferRemaining",
            "" if intent.liquidation_buffer_remaining is None else str(intent.liquidation_buffer_remaining),
        )
        if intent.reference_price is not None:
            state_payload.setdefault("referencePrice", str(intent.reference_price))
        if intent.max_slippage_tolerance_bps is not None:
            state_payload.setdefault("maxSlippageToleranceBps", str(intent.max_slippage_tolerance_bps))
        return state_payload

    def _parse_fill_rows(self, payload: dict[str, Any]) -> list[ExchangeFill]:
        rows: list[ExchangeFill] = []
        for row in payload.get("data", []):
            fill_ts = row.get("fillTime") or row.get("ts")
            fill_id = str(row.get("tradeId") or row.get("billId") or row.get("fillId") or "")
            if not fill_id:
                fill_id = f"{row.get('ordId', 'unknown')}-{fill_ts or 'unknown'}"
            symbol = str(row.get("instId"))
            instrument = self.account_service.instrument_metadata(symbol)
            rows.append(
                ExchangeFill(
                    fill_id=fill_id,
                    exchange_order_id=str(row.get("ordId") or ""),
                    client_order_id=str(row.get("clOrdId")) if row.get("clOrdId") else None,
                    instrument_id=symbol,
                    symbol=symbol,
                    side=str(row.get("side")),
                    fill_qty=self._internal_quantity(
                        symbol=symbol,
                        quantity=to_decimal(row.get("fillSz", row.get("sz", "0"))),
                        instrument=instrument,
                        product_type="derivatives" if "-SWAP" in symbol else "spot",
                    ),
                    fill_price=to_decimal(row.get("fillPx", row.get("px", "0"))),
                    fee_amount=abs(to_decimal(row.get("fee", "0"))),
                    fee_currency=str(row.get("feeCcy")) if row.get("feeCcy") else None,
                    fill_ts=self._row_timestamp(fill_ts),
                )
            )
        return rows

    def _normalize_payload_for_account_mode(
        self,
        *,
        payload: dict[str, str],
        position_mode: str | None = None,
    ) -> dict[str, str]:
        resolved_position_mode = position_mode
        if resolved_position_mode in {None, ""}:
            snapshot_getter = getattr(self.account_service, "latest_snapshot", None)
            snapshot = snapshot_getter() if callable(snapshot_getter) else None
            if snapshot is not None:
                resolved_position_mode = self._account_position_mode(snapshot)
        if resolved_position_mode == "net_mode":
            payload = dict(payload)
            payload.pop("posSide", None)
        return payload

    @staticmethod
    def _internal_quantity(
        *,
        symbol: str,
        quantity: Decimal,
        instrument: InstrumentMetadata | None,
        product_type: str,
    ) -> Decimal:
        if product_type != "derivatives" or instrument is None or "-SWAP" not in symbol:
            return quantity
        contract_value = max(instrument.contract_value, Decimal("0"))
        if contract_value <= 0:
            return quantity
        return quantity * contract_value

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
    def _max_size_from_row(*, row: dict[str, Any], side: str) -> Decimal | None:
        field_names = ("maxBuy", "maxBuySz") if side == "buy" else ("maxSell", "maxSellSz")
        for field_name in field_names:
            value = row.get(field_name)
            if value in {None, ""}:
                continue
            return to_decimal(value)
        return None

    @staticmethod
    def _intent_from_state(state: OrderState) -> OrderIntent:
        payload = state.submission_payload
        side = str(payload.get("side", "buy"))
        order_type = str(payload.get("ordType", "market"))
        limit_price = to_decimal(payload["px"]) if "px" in payload and payload["px"] not in {"", None} else None
        reduce_only = str(payload.get("reduceOnly", "false")).lower() == "true" or state.reduce_only
        close_only = str(payload.get("closeOnly", "false")).lower() == "true" or state.close_only
        position_mode = (
            str(payload.get("positionMode"))
            if payload.get("positionMode") not in {"", None}
            else state.position_mode
        )
        pos_side = (
            str(payload.get("posSide"))
            if payload.get("posSide") not in {"", None}
            else state.pos_side
        )
        if pos_side in {None, ""}:
            pos_side = pos_side_from_position_intent(
                position_intent=state.position_intent,
                position_mode=position_mode if position_mode in {"net_mode", "long_short_mode"} else None,
            )
        td_mode = (
            str(payload.get("tdMode"))
            if payload.get("tdMode") not in {"", None}
            else state.td_mode or state.margin_mode
        )
        return OrderIntent(
            intent_id=state.intent_id,
            decision_id=state.decision_id,
            symbol=state.symbol,
            side="buy" if side == "buy" else "sell",
            quantity=state.requested_qty,
            execution_style="exchange",
            order_type="limit" if order_type in {"limit", "ioc", "fok", "post_only"} else "market",
            limit_price=limit_price,
            reference_price=to_decimal(payload["referencePrice"])
            if "referencePrice" in payload and payload["referencePrice"] not in {"", None}
            else None,
            urgency="medium",
            time_in_force="IOC" if order_type in {"ioc", "fok"} else "GTC" if order_type == "limit" else "IOC",
            max_slippage_tolerance_bps=int(payload["maxSlippageToleranceBps"])
            if "maxSlippageToleranceBps" in payload and payload["maxSlippageToleranceBps"] not in {"", None}
            else None,
            reduce_only=reduce_only,
            close_only=close_only,
            td_mode=td_mode,  # type: ignore[arg-type]
            position_mode=position_mode,  # type: ignore[arg-type]
            pos_side=pos_side,  # type: ignore[arg-type]
            reduce_only_reason=(
                str(payload.get("reduceOnlyReason"))
                if payload.get("reduceOnlyReason") not in {"", None}
                else state.reduce_only_reason
                or default_reduce_only_reason(
                    position_intent=state.position_intent,
                    reduce_only=reduce_only,
                )
            ),
            close_only_reason=(
                str(payload.get("closeOnlyReason"))
                if payload.get("closeOnlyReason") not in {"", None}
                else state.close_only_reason
                or default_close_only_reason(
                    position_intent=state.position_intent,
                    close_only=close_only,
                )
            ),
            instrument_family=(
                str(payload.get("instrumentFamily"))
                if payload.get("instrumentFamily") not in {"", None}
                else state.instrument_family
            ),
            settle_currency=(
                str(payload.get("settleCurrency"))
                if payload.get("settleCurrency") not in {"", None}
                else state.settle_currency
            ),
            idempotency_key=state.client_order_id,
            product_type=state.product_type,
            target_leverage=state.target_leverage,
            margin_mode=state.margin_mode,
            exposure_side=state.exposure_side,
            required_initial_margin=(
                to_decimal(payload["requiredInitialMargin"])
                if payload.get("requiredInitialMargin") not in {"", None}
                else None
            ),
            projected_margin_usage=(
                to_decimal(payload["projectedMarginUsage"])
                if payload.get("projectedMarginUsage") not in {"", None}
                else None
            ),
            projected_notional=(
                to_decimal(payload["projectedNotional"])
                if payload.get("projectedNotional") not in {"", None}
                else None
            ),
            only_reduce_required=str(payload.get("onlyReduceRequired", "false")).lower() == "true",
            risk_limit_breached=str(payload.get("riskLimitBreached", "false")).lower() == "true",
            liquidation_buffer_remaining=(
                to_decimal(payload["liquidationBufferRemaining"])
                if payload.get("liquidationBufferRemaining") not in {"", None}
                else None
            ),
            execution_action=(
                state.execution_action
                or (str(payload.get("executionAction")) if payload.get("executionAction") not in {"", None} else None)
                or execution_action_from_position_intent(state.position_intent)
            ),
            position_intent=state.position_intent,
        )

    def _slippage_gate_error(self, *, intent: OrderIntent) -> str | None:
        if (
            self.price_provider is None
            or intent.reference_price is None
            or intent.reference_price <= 0
            or intent.max_slippage_tolerance_bps is None
            or intent.max_slippage_tolerance_bps <= 0
        ):
            return None
        execution_price = to_decimal(self.price_provider(intent.symbol))
        if execution_price <= 0:
            return None
        slippage_fraction = to_decimal(intent.max_slippage_tolerance_bps) / Decimal("10000")
        if intent.side == "buy":
            allowed_price = intent.reference_price * (Decimal("1") + slippage_fraction)
            if execution_price > allowed_price:
                return f"slippage_tolerance_exceeded:price={float(execution_price):.12f}>allowed={float(allowed_price):.12f}"
            return None
        allowed_price = intent.reference_price * (Decimal("1") - slippage_fraction)
        if execution_price < allowed_price:
            return f"slippage_tolerance_exceeded:price={float(execution_price):.12f}<allowed={float(allowed_price):.12f}"
        return None
