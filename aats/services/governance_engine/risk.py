from __future__ import annotations

from decimal import Decimal
from typing import Callable

from aats.bootstrap.settings import AATSSettings
from aats.bus.base import EventBus
from aats.events import topics
from aats.events.envelopes import publish_model
from aats.schemas.decision import PositionTarget
from aats.schemas.execution import OrderObligation
from aats.schemas.governance import RiskDecision
from aats.services.decision_engine.trigger_policy import DecisionTriggerPolicy
from aats.services.execution_engine.okx_account import OKXAccountService
from aats.services.fee_resolver import EffectiveFeeResolver
from aats.services.governance_engine.health import SystemHealthService
from aats.services.governance_engine.mode import RuntimeModeController
from aats.services.governance_engine.runtime_layers import EnvironmentCapabilities, PolicyProfile
from aats.services.accounting import remaining_obligation_amount, resolve_symbol_currencies, spot_buy_quote_requirement
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, to_decimal
from aats.storage.base import ExecutionObligationRepository


class RiskEngine:
    def __init__(
        self,
        *,
        settings: AATSSettings,
        account_service: OKXAccountService,
        health_service: SystemHealthService,
        trigger_policy: DecisionTriggerPolicy,
        price_provider: Callable[[str], Decimal],
        mode_controller: RuntimeModeController,
        obligation_repo: ExecutionObligationRepository | None = None,
        environment_capabilities: EnvironmentCapabilities | None = None,
        policy_profile: PolicyProfile | None = None,
        fee_resolver: EffectiveFeeResolver | None = None,
    ) -> None:
        self.settings = settings
        self.account_service = account_service
        self.health_service = health_service
        self.trigger_policy = trigger_policy
        self.price_provider = price_provider
        self.mode_controller = mode_controller
        self.obligation_repo = obligation_repo
        self.environment_capabilities = environment_capabilities or mode_controller.environment_capabilities
        self.policy_profile = policy_profile or mode_controller.policy_profile
        self.fee_resolver = fee_resolver or EffectiveFeeResolver(
            settings=settings,
            account_service=account_service,
        )

    def evaluate(self, target: PositionTarget) -> RiskDecision:
        max_abs_qty = to_decimal(self.settings.max_abs_position_qty)
        max_notional = to_decimal(self.settings.max_notional_per_symbol)
        target_position_qty = to_decimal(target.target_position_qty)
        current_position_qty = to_decimal(target.current_position_qty)
        target_leverage = to_decimal(target.target_leverage)
        capped_qty = max(min(target_position_qty, max_abs_qty), -max_abs_qty)
        constraints_applied: list[str] = []
        if capped_qty != target_position_qty:
            constraints_applied.append("max_abs_qty")

        mark_price = to_decimal(self.price_provider(target.symbol))
        target_notional = abs(capped_qty) * mark_price
        if target_notional > max_notional and abs(target_position_qty) > EPSILON_DECIMAL_12:
            notional_scale = max_notional / target_notional
            capped_qty *= notional_scale
            target_notional = max_notional
            constraints_applied.append("max_notional_per_symbol")

        current_open_order_count = self._current_open_order_count(target.symbol)
        approved = True
        rejection_reasons: list[str] = []
        capped_target_leverage = self._capped_target_leverage(target.target_leverage)
        if abs(to_decimal(capped_target_leverage) - target_leverage) > EPSILON_DECIMAL_12:
            constraints_applied.append("max_target_leverage")
        if current_open_order_count >= self.settings.max_open_orders:
            approved = False
            rejection_reasons.append("max_open_orders_reached")

        if self.policy_profile.balance_checks_required and self.environment_capabilities.account_state_source_kind == "exchange":
            delta_qty = capped_qty - current_position_qty
            base_currency, quote_currency = self._symbol_currencies(target.symbol)
            taker_fee_bps = self.fee_resolver.taker_fee_bps_decimal(symbol=target.symbol)
            fee_multiplier = Decimal("1") + (taker_fee_bps / Decimal("10000"))
            if self.environment_capabilities.position_directionality == "bi_directional":
                if target_leverage > to_decimal(self.policy_profile.max_target_leverage) + EPSILON_DECIMAL_12:
                    approved = False
                    rejection_reasons.append("max_target_leverage_exceeded")
                required_margin = abs(delta_qty) * mark_price * fee_multiplier / to_decimal(max(capped_target_leverage, 1.0))
                available_quote = self._available_balance(quote_currency) if quote_currency is not None else Decimal("0")
                max_margin_capacity = available_quote * to_decimal(self.settings.max_margin_usage_fraction)
                if max_margin_capacity + EPSILON_DECIMAL_12 < required_margin:
                    approved = False
                    rejection_reasons.append("insufficient_initial_margin")
                projected_notional = abs(capped_qty) * mark_price
                if available_quote > 0:
                    margin_usage = required_margin / available_quote
                    if margin_usage > to_decimal(max(self.settings.max_margin_usage_fraction - self.settings.liquidation_buffer_fraction, 0.0)):
                        approved = False
                        rejection_reasons.append("liquidation_buffer_breached")
                if projected_notional > max_notional * to_decimal(max(capped_target_leverage, 1.0)):
                    approved = False
                    rejection_reasons.append("max_gross_notional_per_symbol_exceeded")
            else:
                if delta_qty > EPSILON_DECIMAL_12 and quote_currency is not None:
                    required_quote = spot_buy_quote_requirement(
                        quantity=abs(delta_qty),
                        reference_price=mark_price,
                        max_slippage_tolerance_bps=target.max_slippage_tolerance_bps,
                        taker_fee_bps=taker_fee_bps,
                    ) or Decimal("0")
                    available_quote = self._available_balance(quote_currency)
                    if available_quote + EPSILON_DECIMAL_12 < required_quote:
                        approved = False
                        rejection_reasons.append("insufficient_quote_balance")
                elif delta_qty < -EPSILON_DECIMAL_12 and base_currency is not None:
                    required_base = abs(delta_qty)
                    available_base = self._available_balance(base_currency)
                    if available_base + EPSILON_DECIMAL_12 < required_base:
                        approved = False
                        rejection_reasons.append("insufficient_base_balance")

        if self.policy_profile.enforce_health_blockers:
            health_blockers = self.health_service.execution_blockers()
            if health_blockers:
                approved = False
                rejection_reasons.extend(health_blockers)

        modified = bool(constraints_applied)
        risk_score = min(float(abs(capped_qty) / max_abs_qty), 1.0) if max_abs_qty else 0.0
        halt_required = any(reason.endswith("_halt_required") for reason in rejection_reasons)
        return RiskDecision(
            decision_id=target.decision_id,
            approved=approved,
            modified=modified,
            capped_target_position_qty=capped_qty,
            capped_target_notional=target_notional,
            current_open_order_count=current_open_order_count,
            constraints_applied=constraints_applied,
            risk_score=risk_score,
            flatten_required=False,
            halt_required=halt_required,
            rejection_reasons=rejection_reasons,
        )

    def _capped_target_leverage(self, leverage: float) -> float:
        return max(1.0, min(leverage, self.policy_profile.max_target_leverage))

    def _available_balance(self, currency: str | None) -> Decimal:
        if currency is None:
            return Decimal("0")
        snapshot_getter = getattr(self.account_service, "latest_snapshot", None)
        snapshot = snapshot_getter() if callable(snapshot_getter) else None
        if snapshot is None:
            return Decimal("0")
        exchange_available = sum(balance.available for balance in snapshot.balances if balance.currency == currency)
        local_reserved = sum(
            remaining_obligation_amount(obligation)
            for obligation in self._active_local_obligations()
            if obligation.reserve_currency == currency
        )
        return max(exchange_available - local_reserved, Decimal("0"))

    def _current_open_order_count(self, symbol: str) -> int:
        return self.account_service.open_order_count(symbol=symbol) + sum(
            1
            for obligation in self._active_local_obligations()
            if obligation.symbol == symbol
        )

    def _active_local_obligations(self) -> list[OrderObligation]:
        if self.obligation_repo is None:
            return []
        snapshot_getter = getattr(self.account_service, "latest_snapshot", None)
        snapshot = snapshot_getter() if callable(snapshot_getter) else None
        visible_client_order_ids = {
            order.client_order_id
            for order in (snapshot.open_orders if snapshot is not None else [])
            if order.client_order_id
        }
        return [
            obligation
            for obligation in self.obligation_repo.active_obligations()
            if obligation.client_order_id not in visible_client_order_ids
        ]

    def _symbol_currencies(self, symbol: str) -> tuple[str | None, str | None]:
        instrument_getter = getattr(self.account_service, "instrument_metadata", None)
        return resolve_symbol_currencies(
            symbol,
            instrument_lookup=instrument_getter if callable(instrument_getter) else None,
        )

    async def publish_decision(
        self,
        *,
        bus: EventBus,
        target: PositionTarget,
        decision: RiskDecision,
    ) -> None:
        await publish_model(
            bus=bus,
            topic=topics.RISK_DECISIONS,
            key=target.symbol,
            payload_model=decision,
            source_component="governance_engine",
        )
