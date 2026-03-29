from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from aats.bootstrap.settings import AATSSettings
from aats.bus.base import EventBus
from aats.events import topics
from aats.events.envelopes import publish_model
from aats.schemas.common import utc_now
from aats.schemas.decision import PositionTarget
from aats.schemas.execution import LegOrderIntent, OrderObligation
from aats.schemas.exchange import ExchangeAccountSnapshot, ExchangeOpenOrder, ExchangePosition
from aats.schemas.governance import (
    DerivativesExposureLimits,
    DerivativesExposureMetrics,
    LegRiskConstraint,
    RiskDecision,
)
from aats.services.decision_engine.trigger_policy import DecisionTriggerPolicy
from aats.services.execution_engine.okx_account import OKXAccountService
from aats.services.fee_resolver import EffectiveFeeResolver
from aats.services.governance_engine.adaptive_controls import (
    reconciliation_clean_from_safety_state,
    resolve_execution_aggressiveness_state,
    resolve_risk_budget_state,
)
from aats.services.governance_engine.health import SystemHealthService
from aats.services.governance_engine.mode import RuntimeModeController
from aats.services.governance_engine.runtime_layers import EnvironmentCapabilities, PolicyProfile
from aats.services.accounting import remaining_obligation_amount, resolve_symbol_currencies, spot_buy_quote_requirement
from aats.services.portfolio_service.instrument_states import (
    instrument_position_state_for_symbol,
    instrument_position_states_from_exchange_positions,
)
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, to_decimal
from aats.services.runtime_scope import latest_matching_reconciliation, runtime_state_scope
from aats.storage.base import ExecutionObligationRepository, ReconciliationRepository


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
        reconciliation_repo: ReconciliationRepository | None = None,
        live_runtime_guard_provider: Any | None = None,
        trial_guard_provider: Any | None = None,
        recovery_status_provider: Callable[[], Any] | None = None,
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
        self.reconciliation_repo = reconciliation_repo
        self.live_runtime_guard_provider = live_runtime_guard_provider
        self.trial_guard_provider = trial_guard_provider
        self.recovery_status_provider = recovery_status_provider
        self.runtime_scope = runtime_state_scope(settings)

    def evaluate(self, target: PositionTarget) -> RiskDecision:
        adaptive_state = self._adaptive_control_states(target=target)
        risk_budget_multiplier = to_decimal(adaptive_state["risk_budget"]["multiplier"])
        execution_aggressiveness_multiplier = to_decimal(
            adaptive_state["execution_aggressiveness"]["multiplier"]
        )
        max_abs_qty = to_decimal(self.settings.max_abs_position_qty) * risk_budget_multiplier
        max_notional = to_decimal(self.settings.max_notional_per_symbol) * risk_budget_multiplier
        target_position_qty = to_decimal(target.target_position_qty)
        target_leverage = to_decimal(target.target_leverage)
        capped_qty = max(min(target_position_qty, max_abs_qty), -max_abs_qty)
        constraints_applied: list[str] = []
        if capped_qty != target_position_qty:
            constraints_applied.append("max_abs_qty")

        mark_price = self._safe_reference_price(target.symbol)
        projected_notional = abs(capped_qty) * mark_price
        if projected_notional > max_notional and abs(target_position_qty) > EPSILON_DECIMAL_12:
            notional_scale = max_notional / projected_notional
            capped_qty *= notional_scale
            projected_notional = abs(capped_qty) * mark_price
            constraints_applied.append("max_notional_per_symbol")

        capped_target_leverage = self._capped_target_leverage(target.target_leverage)
        if abs(to_decimal(capped_target_leverage) - target_leverage) > EPSILON_DECIMAL_12:
            constraints_applied.append("max_target_leverage")

        current_open_order_count = self._current_open_order_count(target.symbol)
        approved = True
        rejection_reasons: list[str] = []
        required_initial_margin: Decimal | None = None
        projected_margin_usage: Decimal | None = None
        only_reduce_required = False
        leg_only_reduce_constraints: list[LegRiskConstraint] = []
        risk_limit_breached = False
        liquidation_buffer_remaining: Decimal | None = None
        flatten_required = False
        current_derivatives_exposure: DerivativesExposureMetrics | None = None
        projected_derivatives_exposure: DerivativesExposureMetrics | None = None
        derivatives_exposure_limits: DerivativesExposureLimits | None = None

        if self.policy_profile.balance_checks_required and self.environment_capabilities.account_state_source_kind == "exchange":
            if self._is_derivatives_target(target):
                derivatives_result = self._evaluate_derivatives_pretrade(
                    target=target,
                    capped_qty=capped_qty,
                    mark_price=mark_price,
                    capped_target_leverage=capped_target_leverage,
                    current_open_order_count=current_open_order_count,
                    risk_budget_multiplier=risk_budget_multiplier,
                )
                capped_qty = derivatives_result["capped_qty"]
                projected_notional = derivatives_result["projected_notional"]
                required_initial_margin = derivatives_result["required_initial_margin"]
                projected_margin_usage = derivatives_result["projected_margin_usage"]
                only_reduce_required = derivatives_result["only_reduce_required"]
                leg_only_reduce_constraints = derivatives_result["leg_only_reduce_constraints"]
                risk_limit_breached = derivatives_result["risk_limit_breached"]
                liquidation_buffer_remaining = derivatives_result["liquidation_buffer_remaining"]
                flatten_required = derivatives_result["flatten_required"]
                current_derivatives_exposure = derivatives_result["current_derivatives_exposure"]
                projected_derivatives_exposure = derivatives_result["projected_derivatives_exposure"]
                derivatives_exposure_limits = derivatives_result["derivatives_exposure_limits"]
                constraints_applied.extend(derivatives_result["constraints_applied"])
                rejection_reasons.extend(derivatives_result["rejection_reasons"])
                approved = approved and derivatives_result["approved"]
            else:
                spot_rejection_reasons = self._evaluate_spot_balance_constraints(
                    target=target,
                    capped_qty=capped_qty,
                    mark_price=mark_price,
                )
                if spot_rejection_reasons:
                    approved = False
                    rejection_reasons.extend(spot_rejection_reasons)
                if current_open_order_count >= self.settings.max_open_orders:
                    approved = False
                    rejection_reasons.append("max_open_orders_reached")

        if self.policy_profile.enforce_health_blockers:
            health_blockers = [
                *self.health_service.execution_blockers(),
                *self.health_service.submission_blockers(),
            ]
            if health_blockers:
                approved = False
                rejection_reasons.extend(health_blockers)

        constraints_applied = list(dict.fromkeys(item for item in constraints_applied if item))
        if risk_budget_multiplier < Decimal("0.999999"):
            constraints_applied.append("risk_budget_multiplier_applied")
        if execution_aggressiveness_multiplier < Decimal("0.999999"):
            constraints_applied.append("execution_aggressiveness_contracted")
        constraints_applied = list(dict.fromkeys(item for item in constraints_applied if item))
        rejection_reasons = list(dict.fromkeys(item for item in rejection_reasons if item))
        capped_target_notional = abs(capped_qty) * mark_price
        modified = bool(constraints_applied) or abs(capped_qty - target_position_qty) > EPSILON_DECIMAL_12
        risk_score = self._risk_score(
            capped_qty=capped_qty,
            max_abs_qty=max_abs_qty,
            capped_target_notional=capped_target_notional,
            projected_margin_usage=projected_margin_usage,
        )
        halt_required = bool(
            any(
                reason.endswith("_halt_required") or reason.endswith("_auto_halt")
                for reason in rejection_reasons
            )
            or adaptive_state["risk_budget"].get("auto_halt_required")
            or adaptive_state["execution_aggressiveness"].get("auto_halt_required")
        )
        return RiskDecision(
            decision_id=target.decision_id,
            approved=approved,
            modified=modified,
            capped_target_position_qty=capped_qty,
            capped_target_notional=capped_target_notional,
            required_initial_margin=required_initial_margin,
            projected_margin_usage=projected_margin_usage,
            projected_notional=projected_notional,
            current_open_order_count=current_open_order_count,
            risk_budget_multiplier=risk_budget_multiplier,
            risk_budget_state=adaptive_state["risk_budget"],
            execution_aggressiveness_multiplier=execution_aggressiveness_multiplier,
            execution_aggressiveness_state=adaptive_state["execution_aggressiveness"],
            constraints_applied=constraints_applied,
            risk_score=risk_score,
            flatten_required=flatten_required,
            halt_required=halt_required,
            only_reduce_required=only_reduce_required,
            leg_only_reduce_constraints=leg_only_reduce_constraints,
            risk_limit_breached=risk_limit_breached,
            liquidation_buffer_remaining=liquidation_buffer_remaining,
            current_derivatives_exposure=current_derivatives_exposure,
            projected_derivatives_exposure=projected_derivatives_exposure,
            derivatives_exposure_limits=derivatives_exposure_limits,
            rejection_reasons=rejection_reasons,
        )

    def evaluate_leg_order(self, leg_intent: LegOrderIntent) -> RiskDecision:
        snapshot = self._snapshot()
        enforce_exchange_margin_checks = self.environment_capabilities.account_state_source_kind == "exchange"
        mark_price = self._safe_reference_price(leg_intent.symbol)
        risk_budget_multiplier = Decimal("1")
        execution_aggressiveness_multiplier = Decimal("1")
        current_open_order_count = self._current_open_order_count(leg_intent.symbol)
        settle_currency = self._settle_or_quote_currency(leg_intent.symbol)
        available_equity = self._available_derivatives_equity(
            snapshot=snapshot,
            settle_currency=settle_currency,
        )
        equity_base = self._equity_base(
            snapshot=snapshot,
            settle_currency=settle_currency,
            available_equity=available_equity,
        )
        risk_snapshot = snapshot.risk_snapshot if snapshot is not None else None
        current_initial_margin = (
            Decimal("0")
            if risk_snapshot is None or risk_snapshot.initial_margin_requirement is None
            else to_decimal(risk_snapshot.initial_margin_requirement)
        )
        current_exposure = self._current_derivatives_exposure_metrics(
            snapshot=snapshot,
            symbol=leg_intent.symbol,
            mark_price=mark_price,
            equity_base=equity_base,
            total_initial_margin_hint=current_initial_margin,
        )
        projected_exposure = self._project_derivatives_exposure_for_leg_order(
            current=current_exposure,
            leg_intent=leg_intent,
            mark_price=mark_price,
            equity_base=equity_base,
        )
        exposure_limits = self._derivatives_exposure_limits(risk_budget_multiplier=risk_budget_multiplier)
        projected_notional = projected_exposure.gross_notional
        fee_multiplier = Decimal("1") + (
            self.fee_resolver.taker_fee_bps_decimal(symbol=leg_intent.symbol) / Decimal("10000")
        )
        leverage = max(to_decimal(leg_intent.target_leverage), Decimal("1"))
        added_notional = self._leg_added_notional(
            current=current_exposure,
            projected=projected_exposure,
            leg=leg_intent.pos_side,
        )
        required_initial_margin = (added_notional * fee_multiplier) / leverage
        total_pending_notional = self._total_pending_notional(snapshot=snapshot)
        pending_initial_margin = total_pending_notional / leverage if leverage > 0 else Decimal("0")
        projected_margin_usage = Decimal("0")
        if equity_base > EPSILON_DECIMAL_12:
            projected_margin_usage = (
                current_initial_margin + pending_initial_margin + required_initial_margin
            ) / equity_base
        elif added_notional > EPSILON_DECIMAL_12:
            projected_margin_usage = Decimal("1")

        only_reduce_constraints = self._base_leg_only_reduce_constraints(
            current=current_exposure,
            product_type=leg_intent.product_type,
            exposure_limits=exposure_limits,
            daily_realized_loss=self._daily_realized_loss_usdt(),
            strategy_bundle_id=leg_intent.strategy_bundle_id,
        )
        constraints_applied: list[str] = []
        rejection_reasons: list[str] = []
        risk_limit_breached = False
        blocked_leg_reasons = self._limit_breaches_for_leg_order(
            leg_intent=leg_intent,
            current=current_exposure,
            projected=projected_exposure,
            exposure_limits=exposure_limits,
            projected_margin_usage=projected_margin_usage,
            available_equity=available_equity,
            required_initial_margin=required_initial_margin,
            leverage=leverage,
            current_open_order_count=current_open_order_count,
            risk_budget_multiplier=risk_budget_multiplier,
            total_pending_notional=total_pending_notional,
            current_total_position_notional=self._current_total_position_notional(snapshot=snapshot),
            symbol_pending_notional=self._pending_notional_for_symbol(
                snapshot=snapshot,
                symbol=leg_intent.symbol,
            ),
            daily_realized_loss=self._daily_realized_loss_usdt(),
            enforce_exchange_margin_checks=enforce_exchange_margin_checks,
        )
        for leg, reasons in blocked_leg_reasons.items():
            only_reduce_constraints[leg] = list(dict.fromkeys([*(only_reduce_constraints.get(leg) or []), *reasons]))

        selected_leg_reasons = list(only_reduce_constraints.get(leg_intent.pos_side, []))
        approved = True
        if leg_intent.action == "open" and selected_leg_reasons:
            approved = False
            risk_limit_breached = True
            rejection_reasons.extend(selected_leg_reasons)
            rejection_reasons.append("leg_only_reduce_mode_active")
            constraints_applied.extend(["only_reduce_required", *selected_leg_reasons])

        projected_exposure = self._apply_projected_margin_to_exposure(
            exposure=projected_exposure,
            current=current_exposure,
            leg=leg_intent.pos_side,
            action=leg_intent.action,
            required_initial_margin=required_initial_margin,
        )

        leg_only_reduce_constraints = self._leg_constraints_from_reason_map(only_reduce_constraints)
        risk_score = self._risk_score(
            capped_qty=projected_exposure.net_position_qty,
            max_abs_qty=to_decimal(self.settings.max_abs_position_qty) * risk_budget_multiplier,
            capped_target_notional=projected_exposure.net_notional,
            projected_margin_usage=projected_margin_usage,
        )
        return RiskDecision(
            decision_id=leg_intent.decision_id,
            approved=approved,
            modified=bool(selected_leg_reasons),
            capped_target_position_qty=projected_exposure.net_position_qty,
            capped_target_notional=projected_exposure.net_notional,
            required_initial_margin=required_initial_margin,
            projected_margin_usage=projected_margin_usage,
            projected_notional=projected_notional,
            current_open_order_count=current_open_order_count,
            risk_budget_multiplier=risk_budget_multiplier,
            risk_budget_state={},
            execution_aggressiveness_multiplier=execution_aggressiveness_multiplier,
            execution_aggressiveness_state={},
            constraints_applied=list(dict.fromkeys(item for item in constraints_applied if item)),
            risk_score=risk_score,
            flatten_required=False,
            halt_required=False,
            only_reduce_required=bool(leg_only_reduce_constraints),
            leg_only_reduce_constraints=leg_only_reduce_constraints,
            risk_limit_breached=risk_limit_breached,
            liquidation_buffer_remaining=max(
                to_decimal(self.settings.max_margin_usage_fraction) - projected_margin_usage,
                Decimal("0"),
            ),
            current_derivatives_exposure=current_exposure,
            projected_derivatives_exposure=projected_exposure,
            derivatives_exposure_limits=exposure_limits,
            rejection_reasons=list(dict.fromkeys(item for item in rejection_reasons if item)),
        )

    def evaluate_leg_order_bundle(self, leg_intents: list[LegOrderIntent]) -> RiskDecision:
        intents = [
            item
            for item in leg_intents
            if str(item.product_type or "") == "derivatives"
            and str(item.position_mode or "") == "long_short_mode"
            and str(item.pos_side or "") in {"long", "short"}
        ]
        decision_id = next((str(item.decision_id) for item in intents if str(item.decision_id or "").strip()), "bundle")
        if not intents:
            return RiskDecision(
                decision_id=decision_id,
                approved=False,
                modified=False,
                capped_target_position_qty=Decimal("0"),
                risk_score=0.0,
                risk_limit_breached=True,
                rejection_reasons=["empty_leg_order_bundle"],
            )

        symbols = {
            str(item.symbol)
            for item in intents
            if str(item.symbol or "").strip()
        }
        if len(symbols) != 1:
            return RiskDecision(
                decision_id=decision_id,
                approved=False,
                modified=False,
                capped_target_position_qty=Decimal("0"),
                risk_score=0.0,
                risk_limit_breached=True,
                rejection_reasons=["multi_symbol_leg_bundle_not_supported"],
            )

        symbol = next(iter(symbols))
        snapshot = self._snapshot()
        mark_price = self._safe_reference_price(symbol)
        settle_currency = self._settle_or_quote_currency(symbol)
        available_equity = self._available_derivatives_equity(
            snapshot=snapshot,
            settle_currency=settle_currency,
        )
        equity_base = self._equity_base(
            snapshot=snapshot,
            settle_currency=settle_currency,
            available_equity=available_equity,
        )
        risk_snapshot = snapshot.risk_snapshot if snapshot is not None else None
        current_initial_margin = (
            Decimal("0")
            if risk_snapshot is None or risk_snapshot.initial_margin_requirement is None
            else to_decimal(risk_snapshot.initial_margin_requirement)
        )
        current_exposure = self._current_derivatives_exposure_metrics(
            snapshot=snapshot,
            symbol=symbol,
            mark_price=mark_price,
            equity_base=equity_base,
            total_initial_margin_hint=current_initial_margin,
        )
        exposure_limits = self._derivatives_exposure_limits(risk_budget_multiplier=Decimal("1"))
        projected_exposure = current_exposure
        total_required_initial_margin = Decimal("0")
        total_added_notional = Decimal("0")
        max_target_leverage = Decimal("1")
        opening_leg_count = 0
        involved_legs = sorted({str(item.pos_side) for item in intents if str(item.pos_side or "").strip()})
        for leg_intent in intents:
            previous_exposure = projected_exposure
            projected_exposure = self._project_derivatives_exposure_for_leg_order(
                current=projected_exposure,
                leg_intent=leg_intent,
                mark_price=mark_price,
                equity_base=equity_base,
            )
            leverage = max(to_decimal(leg_intent.target_leverage), Decimal("1"))
            max_target_leverage = max(max_target_leverage, leverage)
            if leg_intent.action == "open":
                opening_leg_count += 1
                fee_multiplier = Decimal("1") + (
                    self.fee_resolver.taker_fee_bps_decimal(symbol=leg_intent.symbol) / Decimal("10000")
                )
                added_notional = self._leg_added_notional(
                    current=previous_exposure,
                    projected=projected_exposure,
                    leg=leg_intent.pos_side,
                )
                total_added_notional += added_notional
                total_required_initial_margin += (added_notional * fee_multiplier) / leverage

        pending_initial_margin = (
            self._total_pending_notional(snapshot=snapshot) / max_target_leverage
            if max_target_leverage > Decimal("0")
            else Decimal("0")
        )
        projected_margin_usage = Decimal("0")
        if equity_base > EPSILON_DECIMAL_12:
            projected_margin_usage = (
                current_initial_margin + pending_initial_margin + total_required_initial_margin
            ) / equity_base
        elif total_added_notional > EPSILON_DECIMAL_12:
            projected_margin_usage = Decimal("1")

        only_reduce_constraints = self._base_leg_only_reduce_constraints(
            current=current_exposure,
            product_type="derivatives",
            exposure_limits=exposure_limits,
            daily_realized_loss=self._daily_realized_loss_usdt(),
            strategy_bundle_id=next(
                (
                    str(item.strategy_bundle_id)
                    for item in intents
                    if str(item.strategy_bundle_id or "").strip()
                ),
                None,
            ),
        )
        current_open_order_count = self._current_open_order_count(symbol)
        rejection_reasons: list[str] = []
        enforce_exchange_margin_checks = self.environment_capabilities.account_state_source_kind == "exchange"

        if current_open_order_count + opening_leg_count > self.settings.max_open_orders:
            rejection_reasons.append("max_open_orders_reached")
        if enforce_exchange_margin_checks and available_equity + EPSILON_DECIMAL_12 < total_required_initial_margin:
            rejection_reasons.append("insufficient_initial_margin")
        if (
            enforce_exchange_margin_checks
            and
            to_decimal(self.settings.max_margin_usage_fraction) > Decimal("0")
            and projected_margin_usage > to_decimal(self.settings.max_margin_usage_fraction) + EPSILON_DECIMAL_12
        ):
            rejection_reasons.append("liquidation_buffer_breached")
        if (
            exposure_limits.risk_max_long_notional > Decimal("0")
            and projected_exposure.long_notional > exposure_limits.risk_max_long_notional + EPSILON_DECIMAL_12
            and projected_exposure.long_notional > current_exposure.long_notional + EPSILON_DECIMAL_12
        ):
            rejection_reasons.append("risk_max_long_notional_exceeded")
            self._append_reasons_to_legs(only_reduce_constraints, ["long"], ["risk_max_long_notional_exceeded"])
        if (
            exposure_limits.risk_max_short_notional > Decimal("0")
            and projected_exposure.short_notional > exposure_limits.risk_max_short_notional + EPSILON_DECIMAL_12
            and projected_exposure.short_notional > current_exposure.short_notional + EPSILON_DECIMAL_12
        ):
            rejection_reasons.append("risk_max_short_notional_exceeded")
            self._append_reasons_to_legs(only_reduce_constraints, ["short"], ["risk_max_short_notional_exceeded"])
        if (
            exposure_limits.risk_max_gross_notional > Decimal("0")
            and projected_exposure.gross_notional > exposure_limits.risk_max_gross_notional + EPSILON_DECIMAL_12
            and projected_exposure.gross_notional > current_exposure.gross_notional + EPSILON_DECIMAL_12
        ):
            rejection_reasons.append("risk_max_gross_notional_exceeded")
            self._append_reasons_to_legs(only_reduce_constraints, involved_legs or ["long", "short"], ["risk_max_gross_notional_exceeded"])
        net_legs = [projected_exposure.net_exposure_side] if projected_exposure.net_exposure_side in {"long", "short"} else (involved_legs or ["long", "short"])
        if (
            exposure_limits.risk_max_net_notional > Decimal("0")
            and projected_exposure.net_notional > exposure_limits.risk_max_net_notional + EPSILON_DECIMAL_12
            and projected_exposure.net_notional > current_exposure.net_notional + EPSILON_DECIMAL_12
        ):
            rejection_reasons.append("risk_max_net_notional_exceeded")
            self._append_reasons_to_legs(only_reduce_constraints, net_legs, ["risk_max_net_notional_exceeded"])

        symbol_pending_notional = self._pending_notional_for_symbol(snapshot=snapshot, symbol=symbol)
        max_pending_notional_per_symbol = to_decimal(self.settings.max_pending_notional_per_symbol)
        if (
            max_pending_notional_per_symbol > Decimal("0")
            and symbol_pending_notional + total_added_notional > max_pending_notional_per_symbol + EPSILON_DECIMAL_12
        ):
            rejection_reasons.append("max_pending_notional_per_symbol_exceeded")

        total_pending_notional = self._total_pending_notional(snapshot=snapshot)
        current_total_position_notional = self._current_total_position_notional(snapshot=snapshot)
        max_total_open_notional = to_decimal(self.settings.max_total_open_notional)
        if (
            max_total_open_notional > Decimal("0")
            and current_total_position_notional + total_pending_notional + total_added_notional > max_total_open_notional + EPSILON_DECIMAL_12
        ):
            rejection_reasons.append("max_total_open_notional_exceeded")

        daily_realized_loss = self._daily_realized_loss_usdt()
        max_daily_realized_loss_usdt = to_decimal(self.settings.max_daily_realized_loss_usdt)
        if (
            max_daily_realized_loss_usdt > Decimal("0")
            and daily_realized_loss > max_daily_realized_loss_usdt + EPSILON_DECIMAL_12
            and projected_exposure.net_notional > current_exposure.net_notional + EPSILON_DECIMAL_12
        ):
            rejection_reasons.append("max_daily_realized_loss_usdt_exceeded")

        only_reduce_trigger = min(
            to_decimal(self.settings.derivatives_only_reduce_trigger_margin_fraction),
            to_decimal(self.settings.max_margin_usage_fraction),
        )
        if (
            enforce_exchange_margin_checks
            and
            only_reduce_trigger > Decimal("0")
            and projected_margin_usage >= only_reduce_trigger - EPSILON_DECIMAL_12
            and projected_exposure.net_notional > current_exposure.net_notional + EPSILON_DECIMAL_12
        ):
            rejection_reasons.append("derivatives_margin_usage_requires_only_reduce")
            self._append_reasons_to_legs(
                only_reduce_constraints,
                net_legs,
                ["derivatives_margin_usage_requires_only_reduce"],
            )

        rejection_reasons = list(dict.fromkeys(item for item in rejection_reasons if item))
        leg_only_reduce_constraints = self._leg_constraints_from_reason_map(only_reduce_constraints)
        approved = not rejection_reasons
        return RiskDecision(
            decision_id=decision_id,
            approved=approved,
            modified=not approved,
            capped_target_position_qty=projected_exposure.net_position_qty,
            capped_target_notional=projected_exposure.net_notional,
            required_initial_margin=total_required_initial_margin,
            projected_margin_usage=projected_margin_usage,
            projected_notional=projected_exposure.gross_notional,
            current_open_order_count=current_open_order_count,
            risk_budget_multiplier=Decimal("1"),
            risk_budget_state={"bundle": True},
            execution_aggressiveness_multiplier=Decimal("1"),
            execution_aggressiveness_state={"bundle": True},
            constraints_applied=[] if approved else ["bundle_leg_risk_constraints_applied"],
            risk_score=self._risk_score(
                capped_qty=projected_exposure.net_position_qty,
                max_abs_qty=to_decimal(self.settings.max_abs_position_qty),
                capped_target_notional=projected_exposure.net_notional,
                projected_margin_usage=projected_margin_usage,
            ),
            flatten_required=False,
            halt_required=False,
            only_reduce_required=bool(leg_only_reduce_constraints),
            leg_only_reduce_constraints=leg_only_reduce_constraints,
            risk_limit_breached=not approved,
            liquidation_buffer_remaining=max(
                to_decimal(self.settings.max_margin_usage_fraction) - projected_margin_usage,
                Decimal("0"),
            ),
            current_derivatives_exposure=current_exposure,
            projected_derivatives_exposure=projected_exposure,
            derivatives_exposure_limits=exposure_limits,
            rejection_reasons=rejection_reasons,
        )

    def _current_derivatives_exposure_metrics(
        self,
        *,
        snapshot: ExchangeAccountSnapshot | None,
        symbol: str,
        mark_price: Decimal,
        equity_base: Decimal,
        total_initial_margin_hint: Decimal | None = None,
    ) -> DerivativesExposureMetrics:
        current_state = self._current_derivatives_instrument_state(snapshot=snapshot, symbol=symbol)
        return self._derivatives_exposure_metrics_from_state(
            state=current_state,
            symbol=symbol,
            mark_price=mark_price,
            equity_base=equity_base,
            total_initial_margin_hint=total_initial_margin_hint,
        )

    def _project_derivatives_exposure_for_leg_order(
        self,
        *,
        current: DerivativesExposureMetrics,
        leg_intent: LegOrderIntent,
        mark_price: Decimal,
        equity_base: Decimal,
    ) -> DerivativesExposureMetrics:
        long_qty = current.long_position_qty
        short_qty = current.short_position_qty
        delta_qty = abs(to_decimal(leg_intent.quantity))

        if leg_intent.pos_side == "long":
            if leg_intent.action == "open":
                long_qty += delta_qty
            else:
                long_qty = max(long_qty - delta_qty, Decimal("0"))
        else:
            if leg_intent.action == "open":
                short_qty += delta_qty
            else:
                short_qty = max(short_qty - delta_qty, Decimal("0"))

        return self._build_derivatives_exposure_metrics(
            long_qty=long_qty,
            short_qty=short_qty,
            mark_price=mark_price,
            equity_base=equity_base,
            long_initial_margin=current.long_initial_margin,
            short_initial_margin=current.short_initial_margin,
            total_initial_margin=current.total_initial_margin,
        )

    def _project_derivatives_exposure_for_target(
        self,
        *,
        target_qty: Decimal,
        mark_price: Decimal,
        equity_base: Decimal,
        current: DerivativesExposureMetrics,
    ) -> DerivativesExposureMetrics:
        long_qty = max(target_qty, Decimal("0"))
        short_qty = max(-target_qty, Decimal("0"))
        return self._build_derivatives_exposure_metrics(
            long_qty=long_qty,
            short_qty=short_qty,
            mark_price=mark_price,
            equity_base=equity_base,
            long_initial_margin=current.long_initial_margin,
            short_initial_margin=current.short_initial_margin,
            total_initial_margin=current.total_initial_margin,
        )

    def _current_derivatives_instrument_state(
        self,
        *,
        snapshot: ExchangeAccountSnapshot | None,
        symbol: str,
    ):
        if snapshot is None:
            return None
        states = instrument_position_states_from_exchange_positions(
            snapshot.positions,
            position_mode=self._snapshot_position_mode(snapshot),
            product_type="derivatives",
        )
        return instrument_position_state_for_symbol(states, symbol)

    def _derivatives_exposure_metrics_from_state(
        self,
        *,
        state,
        symbol: str,
        mark_price: Decimal,
        equity_base: Decimal,
        total_initial_margin_hint: Decimal | None = None,
    ) -> DerivativesExposureMetrics:
        if state is None:
            return self._build_derivatives_exposure_metrics(
                long_qty=Decimal("0"),
                short_qty=Decimal("0"),
                mark_price=mark_price,
                equity_base=equity_base,
                total_initial_margin=total_initial_margin_hint,
            )

        long_initial_margin = sum(
            abs(to_decimal(leg.margin_allocated))
            for leg in state.legs
            if self._leg_side_from_position_qty(leg.pos_side, leg.position_qty) == "long"
        )
        short_initial_margin = sum(
            abs(to_decimal(leg.margin_allocated))
            for leg in state.legs
            if self._leg_side_from_position_qty(leg.pos_side, leg.position_qty) == "short"
        )
        total_initial_margin = long_initial_margin + short_initial_margin
        if total_initial_margin <= EPSILON_DECIMAL_12:
            total_initial_margin = (
                None
                if total_initial_margin_hint is None or total_initial_margin_hint <= EPSILON_DECIMAL_12
                else total_initial_margin_hint
            )

        return self._build_derivatives_exposure_metrics(
            long_qty=abs(to_decimal(state.long_position_qty)),
            short_qty=abs(to_decimal(state.short_position_qty)),
            mark_price=mark_price,
            equity_base=equity_base,
            long_notional=(
                None
                if abs(to_decimal(state.long_position_notional)) <= EPSILON_DECIMAL_12
                else abs(to_decimal(state.long_position_notional))
            ),
            short_notional=(
                None
                if abs(to_decimal(state.short_position_notional)) <= EPSILON_DECIMAL_12
                else abs(to_decimal(state.short_position_notional))
            ),
            long_initial_margin=(
                None if long_initial_margin <= EPSILON_DECIMAL_12 else long_initial_margin
            ),
            short_initial_margin=(
                None if short_initial_margin <= EPSILON_DECIMAL_12 else short_initial_margin
            ),
            total_initial_margin=total_initial_margin,
        )

    def _build_derivatives_exposure_metrics(
        self,
        *,
        long_qty: Decimal,
        short_qty: Decimal,
        mark_price: Decimal,
        equity_base: Decimal,
        long_notional: Decimal | None = None,
        short_notional: Decimal | None = None,
        long_initial_margin: Decimal | None = None,
        short_initial_margin: Decimal | None = None,
        total_initial_margin: Decimal | None = None,
    ) -> DerivativesExposureMetrics:
        resolved_long_notional = (
            abs(to_decimal(long_notional))
            if long_notional is not None
            else abs(long_qty) * mark_price
        )
        resolved_short_notional = (
            abs(to_decimal(short_notional))
            if short_notional is not None
            else abs(short_qty) * mark_price
        )
        net_position_qty = long_qty - short_qty
        gross_position_qty = abs(long_qty) + abs(short_qty)
        net_notional = abs(resolved_long_notional - resolved_short_notional)
        gross_notional = resolved_long_notional + resolved_short_notional
        net_exposure_side = "flat"
        if net_position_qty > EPSILON_DECIMAL_12:
            net_exposure_side = "long"
        elif net_position_qty < -EPSILON_DECIMAL_12:
            net_exposure_side = "short"

        gross_leverage = None
        net_leverage = None
        if equity_base > EPSILON_DECIMAL_12:
            gross_leverage = gross_notional / equity_base
            net_leverage = net_notional / equity_base

        return DerivativesExposureMetrics(
            long_position_qty=abs(long_qty),
            short_position_qty=abs(short_qty),
            net_position_qty=net_position_qty,
            gross_position_qty=gross_position_qty,
            long_notional=resolved_long_notional,
            short_notional=resolved_short_notional,
            net_notional=net_notional,
            gross_notional=gross_notional,
            net_exposure_side=net_exposure_side,
            long_initial_margin=(
                None
                if long_initial_margin is None or abs(long_initial_margin) <= EPSILON_DECIMAL_12
                else abs(long_initial_margin)
            ),
            short_initial_margin=(
                None
                if short_initial_margin is None or abs(short_initial_margin) <= EPSILON_DECIMAL_12
                else abs(short_initial_margin)
            ),
            total_initial_margin=(
                None
                if total_initial_margin is None or abs(total_initial_margin) <= EPSILON_DECIMAL_12
                else abs(total_initial_margin)
            ),
            gross_leverage=gross_leverage,
            net_leverage=net_leverage,
        )

    def _apply_projected_margin_to_exposure(
        self,
        *,
        exposure: DerivativesExposureMetrics,
        current: DerivativesExposureMetrics,
        leg: str,
        action: str,
        required_initial_margin: Decimal,
    ) -> DerivativesExposureMetrics:
        if action != "open" or required_initial_margin <= EPSILON_DECIMAL_12:
            return exposure
        current_long_margin = to_decimal(current.long_initial_margin or Decimal("0"))
        current_short_margin = to_decimal(current.short_initial_margin or Decimal("0"))
        if leg == "long":
            projected_long_margin = current_long_margin + required_initial_margin
            projected_short_margin = current_short_margin
        else:
            projected_long_margin = current_long_margin
            projected_short_margin = current_short_margin + required_initial_margin
        projected_total_margin = projected_long_margin + projected_short_margin
        return exposure.model_copy(
            update={
                "long_initial_margin": (
                    None
                    if projected_long_margin <= EPSILON_DECIMAL_12
                    else projected_long_margin
                ),
                "short_initial_margin": (
                    None
                    if projected_short_margin <= EPSILON_DECIMAL_12
                    else projected_short_margin
                ),
                "total_initial_margin": (
                    None
                    if projected_total_margin <= EPSILON_DECIMAL_12
                    else projected_total_margin
                ),
            }
        )

    def _derivatives_exposure_limits(
        self,
        *,
        risk_budget_multiplier: Decimal,
    ) -> DerivativesExposureLimits:
        configured_long = to_decimal(self.settings.risk_max_long_notional)
        configured_short = to_decimal(self.settings.risk_max_short_notional)
        configured_gross = to_decimal(self.settings.risk_max_gross_notional)
        configured_net = to_decimal(self.settings.risk_max_net_notional)
        legacy_gross = to_decimal(self.settings.max_gross_notional_per_symbol)
        legacy_net = to_decimal(self.settings.max_notional_per_symbol)
        return DerivativesExposureLimits(
            risk_max_long_notional=(
                (configured_long if configured_long > Decimal("0") else legacy_gross)
                * risk_budget_multiplier
            ),
            risk_max_short_notional=(
                (configured_short if configured_short > Decimal("0") else legacy_gross)
                * risk_budget_multiplier
            ),
            risk_max_gross_notional=(
                (configured_gross if configured_gross > Decimal("0") else legacy_gross)
                * risk_budget_multiplier
            ),
            risk_max_net_notional=(
                (configured_net if configured_net > Decimal("0") else legacy_net)
                * risk_budget_multiplier
            ),
        )

    @staticmethod
    def _leg_added_notional(
        *,
        current: DerivativesExposureMetrics,
        projected: DerivativesExposureMetrics,
        leg: str,
    ) -> Decimal:
        if leg == "long":
            return max(projected.long_notional - current.long_notional, Decimal("0"))
        return max(projected.short_notional - current.short_notional, Decimal("0"))

    def _base_leg_only_reduce_constraints(
        self,
        *,
        current: DerivativesExposureMetrics,
        product_type: str,
        exposure_limits: DerivativesExposureLimits,
        daily_realized_loss: Decimal,
        strategy_bundle_id: str | None = None,
    ) -> dict[str, list[str]]:
        _ = product_type
        reason_map: dict[str, list[str]] = {}
        held_or_all_legs = self._held_or_all_legs(current=current)

        self._append_reasons_to_legs(
            reason_map,
            held_or_all_legs,
            self._reconciliation_only_reduce_reasons(),
        )
        self._append_reasons_to_legs(
            reason_map,
            held_or_all_legs,
            self._runtime_guard_only_reduce_reasons(),
        )
        self._append_reasons_to_legs(
            reason_map,
            held_or_all_legs,
            self._recovery_status_only_reduce_reasons(strategy_bundle_id=strategy_bundle_id),
        )

        if (
            exposure_limits.risk_max_long_notional > Decimal("0")
            and current.long_notional > exposure_limits.risk_max_long_notional + EPSILON_DECIMAL_12
        ):
            self._append_reasons_to_legs(reason_map, ["long"], ["risk_max_long_notional_exceeded"])
        if (
            exposure_limits.risk_max_short_notional > Decimal("0")
            and current.short_notional > exposure_limits.risk_max_short_notional + EPSILON_DECIMAL_12
        ):
            self._append_reasons_to_legs(reason_map, ["short"], ["risk_max_short_notional_exceeded"])
        if (
            exposure_limits.risk_max_gross_notional > Decimal("0")
            and current.gross_notional > exposure_limits.risk_max_gross_notional + EPSILON_DECIMAL_12
        ):
            self._append_reasons_to_legs(
                reason_map,
                ["long", "short"],
                ["risk_max_gross_notional_exceeded"],
            )
        if (
            exposure_limits.risk_max_net_notional > Decimal("0")
            and current.net_notional > exposure_limits.risk_max_net_notional + EPSILON_DECIMAL_12
        ):
            self._append_reasons_to_legs(
                reason_map,
                self._net_side_or_all_legs(current=current),
                ["risk_max_net_notional_exceeded"],
            )

        max_daily_realized_loss_usdt = to_decimal(self.settings.max_daily_realized_loss_usdt)
        if (
            max_daily_realized_loss_usdt > Decimal("0")
            and daily_realized_loss > max_daily_realized_loss_usdt + EPSILON_DECIMAL_12
        ):
            self._append_reasons_to_legs(
                reason_map,
                held_or_all_legs,
                ["max_daily_realized_loss_usdt_exceeded"],
            )

        return {
            leg: list(dict.fromkeys(reasons))
            for leg, reasons in reason_map.items()
            if reasons
        }

    def _limit_breaches_for_leg_order(
        self,
        *,
        leg_intent: LegOrderIntent,
        current: DerivativesExposureMetrics,
        projected: DerivativesExposureMetrics,
        exposure_limits: DerivativesExposureLimits,
        projected_margin_usage: Decimal,
        available_equity: Decimal,
        required_initial_margin: Decimal,
        leverage: Decimal,
        current_open_order_count: int,
        risk_budget_multiplier: Decimal,
        total_pending_notional: Decimal,
        current_total_position_notional: Decimal,
        symbol_pending_notional: Decimal,
        daily_realized_loss: Decimal,
        enforce_exchange_margin_checks: bool,
    ) -> dict[str, list[str]]:
        if leg_intent.action != "open":
            return {}

        reasons: list[str] = []
        if current_open_order_count >= self.settings.max_open_orders:
            reasons.append("max_open_orders_reached")
        if to_decimal(leg_intent.target_leverage) > to_decimal(self.policy_profile.max_target_leverage) + EPSILON_DECIMAL_12:
            reasons.append("max_target_leverage_exceeded")
        if enforce_exchange_margin_checks and available_equity + EPSILON_DECIMAL_12 < required_initial_margin:
            reasons.append("insufficient_initial_margin")
        if (
            enforce_exchange_margin_checks
            and
            to_decimal(self.settings.max_margin_usage_fraction) > Decimal("0")
            and projected_margin_usage > to_decimal(self.settings.max_margin_usage_fraction) + EPSILON_DECIMAL_12
        ):
            reasons.append("liquidation_buffer_breached")

        projected_leg_notional = (
            projected.long_notional if leg_intent.pos_side == "long" else projected.short_notional
        )
        projected_pending_notional = symbol_pending_notional + self._leg_added_notional(
            current=current,
            projected=projected,
            leg=leg_intent.pos_side,
        )
        projected_total_open_notional = (
            current_total_position_notional
            + total_pending_notional
            + self._leg_added_notional(current=current, projected=projected, leg=leg_intent.pos_side)
        )

        if (
            leg_intent.pos_side == "long"
            and exposure_limits.risk_max_long_notional > Decimal("0")
            and projected_leg_notional > exposure_limits.risk_max_long_notional + EPSILON_DECIMAL_12
        ):
            reasons.append("risk_max_long_notional_exceeded")
        if (
            leg_intent.pos_side == "short"
            and exposure_limits.risk_max_short_notional > Decimal("0")
            and projected_leg_notional > exposure_limits.risk_max_short_notional + EPSILON_DECIMAL_12
        ):
            reasons.append("risk_max_short_notional_exceeded")
        if (
            exposure_limits.risk_max_gross_notional > Decimal("0")
            and projected.gross_notional > exposure_limits.risk_max_gross_notional + EPSILON_DECIMAL_12
        ):
            reasons.append("risk_max_gross_notional_exceeded")
        if (
            exposure_limits.risk_max_net_notional > Decimal("0")
            and projected.net_notional > exposure_limits.risk_max_net_notional + EPSILON_DECIMAL_12
            and projected.net_notional > current.net_notional + EPSILON_DECIMAL_12
            and projected.net_exposure_side == leg_intent.pos_side
        ):
            reasons.append("risk_max_net_notional_exceeded")

        max_pending_notional_per_symbol = (
            to_decimal(self.settings.max_pending_notional_per_symbol) * risk_budget_multiplier
        )
        if (
            max_pending_notional_per_symbol > Decimal("0")
            and projected_pending_notional > max_pending_notional_per_symbol + EPSILON_DECIMAL_12
        ):
            reasons.append("max_pending_notional_per_symbol_exceeded")

        max_total_open_notional = to_decimal(self.settings.max_total_open_notional) * risk_budget_multiplier
        if (
            max_total_open_notional > Decimal("0")
            and projected_total_open_notional > max_total_open_notional + EPSILON_DECIMAL_12
        ):
            reasons.append("max_total_open_notional_exceeded")

        max_daily_realized_loss_usdt = to_decimal(self.settings.max_daily_realized_loss_usdt)
        if (
            max_daily_realized_loss_usdt > Decimal("0")
            and daily_realized_loss > max_daily_realized_loss_usdt + EPSILON_DECIMAL_12
            and projected.net_notional > current.net_notional + EPSILON_DECIMAL_12
        ):
            reasons.append("max_daily_realized_loss_usdt_exceeded")

        if (
            not reasons
            and enforce_exchange_margin_checks
            and min(
                to_decimal(self.settings.derivatives_only_reduce_trigger_margin_fraction),
                to_decimal(self.settings.max_margin_usage_fraction),
            ) > Decimal("0")
            and projected_margin_usage >= min(
                to_decimal(self.settings.derivatives_only_reduce_trigger_margin_fraction),
                to_decimal(self.settings.max_margin_usage_fraction),
            ) - EPSILON_DECIMAL_12
            and projected.net_notional > current.net_notional + EPSILON_DECIMAL_12
        ):
            reasons.append("derivatives_margin_usage_requires_only_reduce")

        if not reasons:
            return {}
        return {leg_intent.pos_side: list(dict.fromkeys(reasons))}

    @staticmethod
    def _leg_constraints_from_reason_map(
        reason_map: dict[str, list[str]],
    ) -> list[LegRiskConstraint]:
        constraints: list[LegRiskConstraint] = []
        for leg in ("long", "short"):
            reasons = [str(item) for item in (reason_map.get(leg) or []) if str(item).strip()]
            if not reasons:
                continue
            constraints.append(
                LegRiskConstraint(
                    leg=leg,
                    only_reduce_required=True,
                    reasons=list(dict.fromkeys(reasons)),
                )
            )
        return constraints

    @staticmethod
    def _append_reasons_to_legs(
        reason_map: dict[str, list[str]],
        legs: list[str],
        reasons: list[str],
    ) -> None:
        cleaned_reasons = [str(item) for item in reasons if str(item).strip()]
        if not cleaned_reasons:
            return
        for leg in legs:
            reason_map.setdefault(leg, [])
            reason_map[leg].extend(cleaned_reasons)

    @staticmethod
    def _held_or_all_legs(*, current: DerivativesExposureMetrics) -> list[str]:
        legs: list[str] = []
        if current.long_position_qty > EPSILON_DECIMAL_12:
            legs.append("long")
        if current.short_position_qty > EPSILON_DECIMAL_12:
            legs.append("short")
        return legs or ["long", "short"]

    @staticmethod
    def _net_side_or_all_legs(*, current: DerivativesExposureMetrics) -> list[str]:
        if current.net_exposure_side in {"long", "short"}:
            return [current.net_exposure_side]
        return ["long", "short"]

    @staticmethod
    def _snapshot_position_mode(snapshot: ExchangeAccountSnapshot | None) -> str | None:
        if snapshot is None:
            return None
        if (
            snapshot.account_configuration is not None
            and snapshot.account_configuration.position_mode not in {None, ""}
        ):
            return snapshot.account_configuration.position_mode
        return snapshot.position_mode

    @staticmethod
    def _leg_side_from_position_qty(pos_side: str | None, quantity: Decimal) -> str:
        if pos_side in {"long", "short"}:
            return pos_side
        if quantity < -EPSILON_DECIMAL_12:
            return "short"
        if quantity > EPSILON_DECIMAL_12:
            return "long"
        return "flat"

    def _evaluate_spot_balance_constraints(
        self,
        *,
        target: PositionTarget,
        capped_qty: Decimal,
        mark_price: Decimal,
    ) -> list[str]:
        delta_qty = capped_qty - to_decimal(target.current_position_qty)
        base_currency, quote_currency = self._symbol_currencies(target.symbol)
        taker_fee_bps = self.fee_resolver.taker_fee_bps_decimal(symbol=target.symbol)
        margin_short_reasons = self._smart_arbitrage_margin_short_reasons(
            target=target,
            delta_qty=delta_qty,
        )
        if margin_short_reasons:
            return margin_short_reasons
        margin_short_open_sell = self._is_smart_arbitrage_margin_short_open(
            target=target,
            delta_qty=delta_qty,
        )
        if delta_qty > EPSILON_DECIMAL_12 and quote_currency is not None:
            required_quote = spot_buy_quote_requirement(
                quantity=abs(delta_qty),
                reference_price=mark_price,
                max_slippage_tolerance_bps=target.max_slippage_tolerance_bps,
                taker_fee_bps=taker_fee_bps,
            ) or Decimal("0")
            available_quote = self._available_balance(quote_currency)
            if available_quote + EPSILON_DECIMAL_12 < required_quote:
                return ["insufficient_quote_balance"]
        if not margin_short_open_sell and delta_qty < -EPSILON_DECIMAL_12 and base_currency is not None:
            required_base = abs(delta_qty)
            available_base = self._available_balance(base_currency)
            if available_base + EPSILON_DECIMAL_12 < required_base:
                return ["insufficient_base_balance"]
        return []

    def _smart_arbitrage_margin_short_reasons(
        self,
        *,
        target: PositionTarget,
        delta_qty: Decimal,
    ) -> list[str]:
        if not self._is_smart_arbitrage_margin_short_open(target=target, delta_qty=delta_qty):
            return []
        if not self.settings.smart_arbitrage_margin_short_enabled:
            return ["smart_arbitrage_margin_short_disabled"]
        if not self.settings.smart_arbitrage_margin_short_execution_ready:
            return ["smart_arbitrage_margin_short_execution_not_ready"]
        configured_mode = getattr(self.settings, "smart_arbitrage_margin_short_spot_margin_mode", "cross")
        if target.margin_mode != configured_mode:
            return ["smart_arbitrage_margin_short_margin_mode_mismatch"]
        return []

    @staticmethod
    def _is_smart_arbitrage_margin_short_open(
        *,
        target: PositionTarget,
        delta_qty: Decimal,
    ) -> bool:
        return bool(
            target.strategy_family == "smart_arbitrage"
            and target.product_type == "spot"
            and target.strategy_execution_mode == "margin_reverse_carry"
            and delta_qty < -EPSILON_DECIMAL_12
        )

    def _evaluate_derivatives_pretrade(
        self,
        *,
        target: PositionTarget,
        capped_qty: Decimal,
        mark_price: Decimal,
        capped_target_leverage: float,
        current_open_order_count: int,
        risk_budget_multiplier: Decimal,
    ) -> dict[str, Any]:
        current_qty = to_decimal(target.current_position_qty)
        settle_currency = self._settle_or_quote_currency(target.symbol)
        snapshot = self._snapshot()
        fee_multiplier = Decimal("1") + (
            self.fee_resolver.taker_fee_bps_decimal(symbol=target.symbol) / Decimal("10000")
        )
        projected_notional = abs(capped_qty) * mark_price
        added_exposure_qty = self._derivatives_added_exposure_qty(
            current_qty=current_qty,
            target_qty=capped_qty,
        )
        added_exposure_notional = abs(added_exposure_qty) * mark_price
        leverage = max(to_decimal(capped_target_leverage), Decimal("1"))
        required_initial_margin = (added_exposure_notional * fee_multiplier) / leverage
        risk_snapshot = snapshot.risk_snapshot if snapshot is not None else None
        available_equity = self._available_derivatives_equity(
            snapshot=snapshot,
            settle_currency=settle_currency,
        )
        equity_base = self._equity_base(
            snapshot=snapshot,
            settle_currency=settle_currency,
            available_equity=available_equity,
        )
        total_pending_notional = self._total_pending_notional(snapshot=snapshot)
        symbol_pending_notional = self._pending_notional_for_symbol(
            snapshot=snapshot,
            symbol=target.symbol,
        )
        current_total_position_notional = self._current_total_position_notional(snapshot=snapshot)
        current_initial_margin = (
            Decimal("0")
            if risk_snapshot is None or risk_snapshot.initial_margin_requirement is None
            else to_decimal(risk_snapshot.initial_margin_requirement)
        )
        current_derivatives_exposure = self._current_derivatives_exposure_metrics(
            snapshot=snapshot,
            symbol=target.symbol,
            mark_price=mark_price,
            equity_base=equity_base,
            total_initial_margin_hint=current_initial_margin,
        )
        pending_initial_margin = total_pending_notional / leverage if leverage > 0 else Decimal("0")
        projected_margin_usage = Decimal("0")
        if equity_base > EPSILON_DECIMAL_12:
            projected_margin_usage = (
                current_initial_margin + pending_initial_margin + required_initial_margin
            ) / equity_base
        elif added_exposure_notional > EPSILON_DECIMAL_12:
            projected_margin_usage = Decimal("1")

        hard_margin_cap = to_decimal(self.settings.max_margin_usage_fraction)
        only_reduce_threshold = min(
            to_decimal(self.settings.derivatives_only_reduce_trigger_margin_fraction),
            hard_margin_cap,
        )
        liquidation_buffer_remaining = max(hard_margin_cap - projected_margin_usage, Decimal("0"))
        projected_pending_notional = symbol_pending_notional + added_exposure_notional
        projected_total_open_notional = current_total_position_notional + total_pending_notional + added_exposure_notional
        daily_realized_loss = self._daily_realized_loss_usdt()
        exposure_increasing = added_exposure_notional > EPSILON_DECIMAL_12
        derivatives_exposure_limits = self._derivatives_exposure_limits(
            risk_budget_multiplier=risk_budget_multiplier
        )
        projected_derivatives_exposure = self._project_derivatives_exposure_for_target(
            target_qty=capped_qty,
            mark_price=mark_price,
            equity_base=equity_base,
            current=current_derivatives_exposure,
        )
        if capped_qty > EPSILON_DECIMAL_12:
            projected_derivatives_exposure = projected_derivatives_exposure.model_copy(
                update={
                    "long_initial_margin": required_initial_margin
                    if required_initial_margin > EPSILON_DECIMAL_12
                    else projected_derivatives_exposure.long_initial_margin,
                    "total_initial_margin": (
                        current_initial_margin + required_initial_margin
                        if current_initial_margin + required_initial_margin > EPSILON_DECIMAL_12
                        else projected_derivatives_exposure.total_initial_margin
                    ),
                }
            )
        elif capped_qty < -EPSILON_DECIMAL_12:
            projected_derivatives_exposure = projected_derivatives_exposure.model_copy(
                update={
                    "short_initial_margin": required_initial_margin
                    if required_initial_margin > EPSILON_DECIMAL_12
                    else projected_derivatives_exposure.short_initial_margin,
                    "total_initial_margin": (
                        current_initial_margin + required_initial_margin
                        if current_initial_margin + required_initial_margin > EPSILON_DECIMAL_12
                        else projected_derivatives_exposure.total_initial_margin
                    ),
                }
            )

        only_reduce_causes: list[str] = []
        risk_limit_breached = False
        if exposure_increasing:
            if current_open_order_count >= self.settings.max_open_orders:
                only_reduce_causes.append("max_open_orders_reached")
                risk_limit_breached = True
            if to_decimal(target.target_leverage) > to_decimal(self.policy_profile.max_target_leverage) + EPSILON_DECIMAL_12:
                only_reduce_causes.append("max_target_leverage_exceeded")
                risk_limit_breached = True
            if available_equity + EPSILON_DECIMAL_12 < required_initial_margin:
                only_reduce_causes.append("insufficient_initial_margin")
                risk_limit_breached = True
            if hard_margin_cap > Decimal("0") and projected_margin_usage > hard_margin_cap + EPSILON_DECIMAL_12:
                only_reduce_causes.append("liquidation_buffer_breached")
                risk_limit_breached = True
            if (
                derivatives_exposure_limits.risk_max_long_notional > Decimal("0")
                and projected_derivatives_exposure.long_notional
                > derivatives_exposure_limits.risk_max_long_notional + EPSILON_DECIMAL_12
            ):
                only_reduce_causes.append("risk_max_long_notional_exceeded")
                risk_limit_breached = True
            if (
                derivatives_exposure_limits.risk_max_short_notional > Decimal("0")
                and projected_derivatives_exposure.short_notional
                > derivatives_exposure_limits.risk_max_short_notional + EPSILON_DECIMAL_12
            ):
                only_reduce_causes.append("risk_max_short_notional_exceeded")
                risk_limit_breached = True
            if (
                derivatives_exposure_limits.risk_max_gross_notional > Decimal("0")
                and projected_derivatives_exposure.gross_notional
                > derivatives_exposure_limits.risk_max_gross_notional + EPSILON_DECIMAL_12
            ):
                only_reduce_causes.append("risk_max_gross_notional_exceeded")
                risk_limit_breached = True
            if (
                derivatives_exposure_limits.risk_max_net_notional > Decimal("0")
                and projected_derivatives_exposure.net_notional
                > derivatives_exposure_limits.risk_max_net_notional + EPSILON_DECIMAL_12
            ):
                only_reduce_causes.append("risk_max_net_notional_exceeded")
                risk_limit_breached = True
            max_gross_notional_per_symbol = (
                to_decimal(self.settings.max_gross_notional_per_symbol) * risk_budget_multiplier
            )
            if max_gross_notional_per_symbol > Decimal("0") and projected_notional > max_gross_notional_per_symbol + EPSILON_DECIMAL_12:
                only_reduce_causes.append("max_gross_notional_per_symbol_exceeded")
                risk_limit_breached = True
            max_pending_notional_per_symbol = (
                to_decimal(self.settings.max_pending_notional_per_symbol) * risk_budget_multiplier
            )
            if max_pending_notional_per_symbol > Decimal("0") and projected_pending_notional > max_pending_notional_per_symbol + EPSILON_DECIMAL_12:
                only_reduce_causes.append("max_pending_notional_per_symbol_exceeded")
                risk_limit_breached = True
            max_total_open_notional = (
                to_decimal(self.settings.max_total_open_notional) * risk_budget_multiplier
            )
            if max_total_open_notional > Decimal("0") and projected_total_open_notional > max_total_open_notional + EPSILON_DECIMAL_12:
                only_reduce_causes.append("max_total_open_notional_exceeded")
                risk_limit_breached = True
            max_daily_realized_loss_usdt = to_decimal(self.settings.max_daily_realized_loss_usdt)
            if max_daily_realized_loss_usdt > Decimal("0") and daily_realized_loss > max_daily_realized_loss_usdt + EPSILON_DECIMAL_12:
                only_reduce_causes.append("max_daily_realized_loss_usdt_exceeded")
                risk_limit_breached = True
            if (
                not risk_limit_breached
                and only_reduce_threshold > Decimal("0")
                and projected_margin_usage >= only_reduce_threshold - EPSILON_DECIMAL_12
            ):
                only_reduce_causes.append("derivatives_margin_usage_requires_only_reduce")

        only_reduce_required = bool(only_reduce_causes)
        constraints_applied: list[str] = []
        rejection_reasons: list[str] = []
        approved = True
        flattened_target_qty = capped_qty
        leg_reason_map = self._base_leg_only_reduce_constraints(
            current=current_derivatives_exposure,
            product_type=target.product_type,
            exposure_limits=derivatives_exposure_limits,
            daily_realized_loss=daily_realized_loss,
            strategy_bundle_id=target.strategy_bundle_id,
        )
        if only_reduce_causes:
            self._append_reasons_to_legs(
                leg_reason_map,
                self._held_or_all_legs(current=current_derivatives_exposure),
                only_reduce_causes,
            )
        if only_reduce_required:
            flattened_target_qty = self._reduce_only_target_qty(
                current_qty=current_qty,
                target_qty=capped_qty,
            )
            constraints_applied.extend(["only_reduce_required", *only_reduce_causes])
            if abs(flattened_target_qty - current_qty) <= EPSILON_DECIMAL_12:
                approved = False
                rejection_reasons.extend(only_reduce_causes)
                rejection_reasons.append("only_reduce_mode_active")
        flatten_required = (
            only_reduce_required
            and abs(current_qty) > EPSILON_DECIMAL_12
            and abs(flattened_target_qty) <= EPSILON_DECIMAL_12
        )
        recovery_only_reduce_reasons = self._reconciliation_only_reduce_reasons()
        if recovery_only_reduce_reasons:
            only_reduce_required = True
            risk_limit_breached = True
            self._append_reasons_to_legs(
                leg_reason_map,
                self._held_or_all_legs(current=current_derivatives_exposure),
                recovery_only_reduce_reasons,
            )
            flattened_target_qty = self._reduce_only_target_qty(
                current_qty=current_qty,
                target_qty=flattened_target_qty,
            )
            constraints_applied.extend(["only_reduce_required", *recovery_only_reduce_reasons])
            if abs(flattened_target_qty - current_qty) <= EPSILON_DECIMAL_12:
                approved = False
                rejection_reasons.extend(recovery_only_reduce_reasons)
                rejection_reasons.append("only_reduce_mode_active")
            flatten_required = (
                abs(current_qty) > EPSILON_DECIMAL_12
                and abs(flattened_target_qty) <= EPSILON_DECIMAL_12
            )
        runtime_guard_only_reduce_reasons = self._runtime_guard_only_reduce_reasons()
        if runtime_guard_only_reduce_reasons:
            only_reduce_required = True
            risk_limit_breached = True
            self._append_reasons_to_legs(
                leg_reason_map,
                self._held_or_all_legs(current=current_derivatives_exposure),
                runtime_guard_only_reduce_reasons,
            )
            flattened_target_qty = self._reduce_only_target_qty(
                current_qty=current_qty,
                target_qty=flattened_target_qty,
            )
            constraints_applied.extend(["only_reduce_required", *runtime_guard_only_reduce_reasons])
            if abs(flattened_target_qty - current_qty) <= EPSILON_DECIMAL_12:
                approved = False
                rejection_reasons.extend(runtime_guard_only_reduce_reasons)
                rejection_reasons.append("only_reduce_mode_active")
            flatten_required = (
                abs(current_qty) > EPSILON_DECIMAL_12
                and abs(flattened_target_qty) <= EPSILON_DECIMAL_12
            )
        recovery_status_only_reduce_reasons = self._recovery_status_only_reduce_reasons(
            strategy_bundle_id=target.strategy_bundle_id,
        )
        if recovery_status_only_reduce_reasons:
            only_reduce_required = True
            risk_limit_breached = True
            self._append_reasons_to_legs(
                leg_reason_map,
                self._held_or_all_legs(current=current_derivatives_exposure),
                recovery_status_only_reduce_reasons,
            )
            flattened_target_qty = self._reduce_only_target_qty(
                current_qty=current_qty,
                target_qty=flattened_target_qty,
            )
            constraints_applied.extend(["only_reduce_required", *recovery_status_only_reduce_reasons])
            if abs(flattened_target_qty - current_qty) <= EPSILON_DECIMAL_12:
                approved = False
                rejection_reasons.extend(recovery_status_only_reduce_reasons)
                rejection_reasons.append("only_reduce_mode_active")
            flatten_required = (
                abs(current_qty) > EPSILON_DECIMAL_12
                and abs(flattened_target_qty) <= EPSILON_DECIMAL_12
            )
        flattened_projected_exposure = self._project_derivatives_exposure_for_target(
            target_qty=flattened_target_qty,
            mark_price=mark_price,
            equity_base=equity_base,
            current=current_derivatives_exposure,
        )
        flattened_projected_exposure = flattened_projected_exposure.model_copy(
            update={
                "total_initial_margin": (
                    current_initial_margin + required_initial_margin
                    if current_initial_margin + required_initial_margin > EPSILON_DECIMAL_12
                    else flattened_projected_exposure.total_initial_margin
                )
            }
        )
        return {
            "approved": approved,
            "capped_qty": flattened_target_qty,
            "projected_notional": projected_notional,
            "required_initial_margin": required_initial_margin,
            "projected_margin_usage": projected_margin_usage,
            "only_reduce_required": only_reduce_required,
            "leg_only_reduce_constraints": self._leg_constraints_from_reason_map(leg_reason_map),
            "risk_limit_breached": risk_limit_breached,
            "liquidation_buffer_remaining": liquidation_buffer_remaining,
            "flatten_required": flatten_required,
            "current_derivatives_exposure": current_derivatives_exposure,
            "projected_derivatives_exposure": flattened_projected_exposure,
            "derivatives_exposure_limits": derivatives_exposure_limits,
            "constraints_applied": list(dict.fromkeys(item for item in constraints_applied if item)),
            "rejection_reasons": list(dict.fromkeys(item for item in rejection_reasons if item)),
        }

    def _risk_score(
        self,
        *,
        capped_qty: Decimal,
        max_abs_qty: Decimal,
        capped_target_notional: Decimal,
        projected_margin_usage: Decimal | None,
    ) -> float:
        qty_ratio = (
            min(float(abs(capped_qty) / max_abs_qty), 1.0)
            if max_abs_qty > EPSILON_DECIMAL_12
            else 0.0
        )
        notional_ratio = 0.0
        max_gross_notional = to_decimal(self.settings.max_gross_notional_per_symbol)
        if max_gross_notional > EPSILON_DECIMAL_12:
            notional_ratio = min(float(capped_target_notional / max_gross_notional), 1.0)
        margin_ratio = 0.0
        if projected_margin_usage is not None and to_decimal(self.settings.max_margin_usage_fraction) > EPSILON_DECIMAL_12:
            margin_ratio = min(
                float(projected_margin_usage / to_decimal(self.settings.max_margin_usage_fraction)),
                1.0,
            )
        return max(qty_ratio, notional_ratio, margin_ratio)

    def _capped_target_leverage(self, leverage: float) -> float:
        return max(1.0, min(leverage, self.policy_profile.max_target_leverage))

    def _snapshot(self) -> ExchangeAccountSnapshot | None:
        snapshot_getter = getattr(self.account_service, "latest_snapshot", None)
        return snapshot_getter() if callable(snapshot_getter) else None

    def _safe_reference_price(self, symbol: str) -> Decimal:
        price = self._safe_price(symbol)
        return price if price > Decimal("0") else Decimal("0")

    def _safe_price(self, symbol: str) -> Decimal:
        try:
            price = to_decimal(self.price_provider(symbol))
        except Exception:
            price = Decimal("0")
        return max(price, Decimal("0"))

    def _available_balance(self, currency: str | None) -> Decimal:
        if currency is None:
            return Decimal("0")
        snapshot = self._snapshot()
        if snapshot is None:
            return Decimal("0")
        exchange_available = sum(balance.available for balance in snapshot.balances if balance.currency == currency)
        local_reserved = sum(
            remaining_obligation_amount(obligation)
            for obligation in self._active_local_obligations()
            if obligation.reserve_currency == currency
        )
        return max(exchange_available - local_reserved, Decimal("0"))

    def _available_derivatives_equity(
        self,
        *,
        snapshot: ExchangeAccountSnapshot | None,
        settle_currency: str | None,
    ) -> Decimal:
        if snapshot is not None and snapshot.risk_snapshot is not None:
            risk_snapshot = snapshot.risk_snapshot
            for value in (
                risk_snapshot.available_equity,
                risk_snapshot.adjusted_equity,
                risk_snapshot.total_equity,
            ):
                if value is not None and to_decimal(value) > Decimal("0"):
                    return to_decimal(value)
        return self._available_balance(settle_currency)

    def _equity_base(
        self,
        *,
        snapshot: ExchangeAccountSnapshot | None,
        settle_currency: str | None,
        available_equity: Decimal,
    ) -> Decimal:
        if snapshot is not None and snapshot.risk_snapshot is not None:
            risk_snapshot = snapshot.risk_snapshot
            for value in (
                risk_snapshot.adjusted_equity,
                risk_snapshot.total_equity,
                risk_snapshot.available_equity,
            ):
                if value is not None and to_decimal(value) > Decimal("0"):
                    return to_decimal(value)
        return available_equity if available_equity > Decimal("0") else self._available_balance(settle_currency)

    def _current_open_order_count(self, symbol: str) -> int:
        return self.account_service.open_order_count(symbol=symbol) + sum(
            1
            for obligation in self._active_local_obligations()
            if obligation.symbol == symbol
        )

    def _active_local_obligations(self) -> list[OrderObligation]:
        if self.obligation_repo is None:
            return []
        snapshot = self._snapshot()
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

    def _settle_or_quote_currency(self, symbol: str) -> str | None:
        instrument_getter = getattr(self.account_service, "instrument_metadata", None)
        instrument = instrument_getter(symbol) if callable(instrument_getter) else None
        settle_currency = None if instrument is None else str(getattr(instrument, "settle_currency", "") or "").strip()
        if settle_currency:
            return settle_currency
        _base_currency, quote_currency = self._symbol_currencies(symbol)
        return quote_currency

    @staticmethod
    def _is_derivatives_target(target: PositionTarget) -> bool:
        return target.product_type == "derivatives"

    @staticmethod
    def _derivatives_added_exposure_qty(
        *,
        current_qty: Decimal,
        target_qty: Decimal,
    ) -> Decimal:
        current_abs = abs(current_qty)
        target_abs = abs(target_qty)
        if current_abs <= EPSILON_DECIMAL_12:
            return abs(target_qty)
        if target_abs <= EPSILON_DECIMAL_12:
            return Decimal("0")
        if current_qty > 0 and target_qty > 0:
            return max(target_abs - current_abs, Decimal("0"))
        if current_qty < 0 and target_qty < 0:
            return max(target_abs - current_abs, Decimal("0"))
        return target_abs

    @staticmethod
    def _reduce_only_target_qty(
        *,
        current_qty: Decimal,
        target_qty: Decimal,
    ) -> Decimal:
        if abs(current_qty) <= EPSILON_DECIMAL_12:
            return Decimal("0")
        if current_qty > 0:
            return min(max(target_qty, Decimal("0")), current_qty)
        return max(min(target_qty, Decimal("0")), current_qty)

    def _reconciliation_only_reduce_reasons(self) -> list[str]:
        report = self._latest_scoped_reconciliation()
        if report is None or not bool(getattr(report, "only_reduce_required", False)):
            return []
        reasons = getattr(report, "only_reduce_reasons", None)
        if isinstance(reasons, list):
            return [str(item) for item in reasons if str(item).strip()]
        return ["derivatives_exchange_position_without_local_execution_chain"]

    def _runtime_guard_only_reduce_reasons(self) -> list[str]:
        provider = self.live_runtime_guard_provider
        if provider is None:
            return []
        snapshot_getter = getattr(provider, "snapshot", None)
        payload = snapshot_getter() if callable(snapshot_getter) else None
        if not isinstance(payload, dict) or not bool(payload.get("only_reduce_required")):
            return []
        return [str(item) for item in (payload.get("only_reduce_reasons") or []) if str(item).strip()]

    def _recovery_status_only_reduce_reasons(
        self,
        *,
        strategy_bundle_id: str | None = None,
    ) -> list[str]:
        payload = self._recovery_status_payload()
        if not isinstance(payload, dict) or not bool(payload.get("only_reduce_required")):
            return []
        reasons = [
            str(item)
            for item in (payload.get("only_reduce_reasons") or [])
            if str(item).strip()
        ]
        if self._structured_open_orders_belong_to_current_bundle(
            payload=payload,
            strategy_bundle_id=strategy_bundle_id,
        ):
            reasons = [
                reason
                for reason in reasons
                if reason != "strategy_bundle_recovery_in_progress"
            ]
        return reasons

    @staticmethod
    def _structured_open_orders_belong_to_current_bundle(
        *,
        payload: dict[str, Any],
        strategy_bundle_id: str | None,
    ) -> bool:
        normalized_bundle_id = str(strategy_bundle_id or "").strip()
        if not normalized_bundle_id:
            return False
        if int(payload.get("unbundled_open_order_count") or 0) != 0:
            return False
        bundle_summaries = payload.get("bundle_summaries")
        if not isinstance(bundle_summaries, list) or not bundle_summaries:
            return False
        normalized_summaries: list[dict[str, Any]] = []
        for item in bundle_summaries:
            if not isinstance(item, dict):
                return False
            normalized_summaries.append(item)
        return all(
            str(summary.get("bundle_id") or "").strip() == normalized_bundle_id
            and bool(summary.get("recoverable", False))
            and str(summary.get("recovery_state") or "").strip()
            in {"structured_open_orders", "partial_fill_recovery"}
            for summary in normalized_summaries
        )

    def _runtime_guard_state(self) -> dict[str, Any]:
        provider = self.live_runtime_guard_provider
        if provider is None:
            return {}
        snapshot_getter = getattr(provider, "snapshot", None)
        payload = snapshot_getter() if callable(snapshot_getter) else None
        return payload if isinstance(payload, dict) else {}

    def _trial_guard_state(self) -> dict[str, Any]:
        provider = self.trial_guard_provider
        if provider is None:
            return {}
        snapshot_getter = getattr(provider, "snapshot", None)
        payload = snapshot_getter() if callable(snapshot_getter) else None
        return payload if isinstance(payload, dict) else {}

    def _adaptive_control_states(self, *, target: PositionTarget) -> dict[str, Any]:
        _ = target
        health_snapshot = self.health_service.snapshot()
        runtime_guard = self._runtime_guard_state()
        trial_guard = self._trial_guard_state()
        projected_margin_usage = runtime_guard.get("projected_margin_usage_fraction")
        if projected_margin_usage is None:
            projected_margin_usage = runtime_guard.get("projected_margin_usage")
        recovery_status = self._recovery_status_payload()
        recovery_safe_to_trade = bool(recovery_status.get("safe_to_trade", True)) if recovery_status else True
        recovery_review_required = bool(recovery_status.get("review_required", False)) if recovery_status else False
        recovery_only_reduce_required = bool(recovery_status.get("only_reduce_required", False)) if recovery_status else False
        safety_state = {
            "safe_to_trade": not bool(health_snapshot.blockers) and recovery_safe_to_trade,
            "review_required": (
                "operator_rebaseline_required" in (health_snapshot.blockers or [])
                or recovery_review_required
            ),
            "market_snapshot_fresh": not any(
                blocker == "market_data_stale"
                for blocker in (health_snapshot.blockers or [])
            ),
            "account_snapshot_fresh": not any(
                blocker == "account_state_stale"
                for blocker in (health_snapshot.blockers or [])
            ),
            "reconciliation_halt_required": "reconciliation_halt_required" in (health_snapshot.blockers or []),
            "reconciliation_review_required": "operator_rebaseline_required" in (health_snapshot.blockers or []),
            "reconciliation_severity": "clean"
            if not any(
                blocker in {"reconciliation_stale", "reconciliation_halt_required", "operator_rebaseline_required"}
                for blocker in (health_snapshot.blockers or [])
            )
            else "review_required",
        }
        execution_errors = len(self.health_service.execution_blockers())
        risk_budget = resolve_risk_budget_state(
            self.settings,
            execution_error_count=execution_errors,
            safe_to_trade=bool(safety_state.get("safe_to_trade", True)),
            review_required=bool(safety_state.get("review_required", False)),
            market_snapshot_fresh=bool(safety_state.get("market_snapshot_fresh", True)),
            account_snapshot_fresh=bool(safety_state.get("account_snapshot_fresh", True)),
            reconciliation_clean=reconciliation_clean_from_safety_state(safety_state),
            only_reduce_required=bool(runtime_guard.get("only_reduce_required")) or recovery_only_reduce_required,
            auto_halt_required=bool(runtime_guard.get("auto_halt_required")),
            risk_snapshot_stage=runtime_guard.get("risk_snapshot_stage"),
            trial_guard_breached=str(trial_guard.get("status") or "").lower() == "breached",
            current_margin_usage_fraction=runtime_guard.get("current_initial_margin_usage_fraction"),
            projected_margin_usage_fraction=projected_margin_usage,
            nearest_liquidation_gap_ratio=runtime_guard.get("nearest_liquidation_gap_ratio"),
        )
        execution_aggressiveness = resolve_execution_aggressiveness_state(
            self.settings,
            execution_error_count=execution_errors,
            safe_to_trade=bool(safety_state.get("safe_to_trade", True)),
            review_required=bool(safety_state.get("review_required", False)),
            market_snapshot_fresh=bool(safety_state.get("market_snapshot_fresh", True)),
            account_snapshot_fresh=bool(safety_state.get("account_snapshot_fresh", True)),
            reconciliation_clean=reconciliation_clean_from_safety_state(safety_state),
            only_reduce_required=bool(runtime_guard.get("only_reduce_required")) or recovery_only_reduce_required,
            auto_halt_required=bool(runtime_guard.get("auto_halt_required")),
            risk_snapshot_stage=runtime_guard.get("risk_snapshot_stage"),
            trial_guard_breached=str(trial_guard.get("status") or "").lower() == "breached",
            current_margin_usage_fraction=runtime_guard.get("current_initial_margin_usage_fraction"),
            projected_margin_usage_fraction=projected_margin_usage,
            nearest_liquidation_gap_ratio=runtime_guard.get("nearest_liquidation_gap_ratio"),
        )
        return {
            "risk_budget": {
                **risk_budget,
                "source": "risk_engine_snapshot",
                "runtime_guard_status": runtime_guard.get("status"),
                "trial_guard_status": trial_guard.get("status"),
                "risk_snapshot_stage": runtime_guard.get("risk_snapshot_stage"),
                "auto_halt_required": bool(runtime_guard.get("auto_halt_required")),
            },
            "execution_aggressiveness": {
                **execution_aggressiveness,
                "source": "risk_engine_snapshot",
                "runtime_guard_status": runtime_guard.get("status"),
                "trial_guard_status": trial_guard.get("status"),
                "risk_snapshot_stage": runtime_guard.get("risk_snapshot_stage"),
                "auto_halt_required": bool(runtime_guard.get("auto_halt_required")),
            },
        }

    def _recovery_status_payload(self) -> dict[str, Any]:
        provider = self.recovery_status_provider
        if provider is None:
            return {}
        payload = provider() if callable(provider) else provider
        if payload is None:
            return {}
        if hasattr(payload, "model_dump"):
            dumped = payload.model_dump(mode="json")
            return dumped if isinstance(dumped, dict) else {}
        return payload if isinstance(payload, dict) else {}

    def _latest_scoped_reconciliation(self):
        if self.reconciliation_repo is None:
            return None
        latest_for_scope = getattr(self.reconciliation_repo, "latest_for_scope", None)
        if callable(latest_for_scope):
            return latest_for_scope(scope=self.runtime_scope)
        history_getter = getattr(self.reconciliation_repo, "history", None)
        if callable(history_getter):
            return latest_matching_reconciliation(history_getter(), self.runtime_scope)
        latest_getter = getattr(self.reconciliation_repo, "latest", None)
        return latest_getter() if callable(latest_getter) else None

    def _current_total_position_notional(self, *, snapshot: ExchangeAccountSnapshot | None) -> Decimal:
        if snapshot is None:
            return Decimal("0")
        return sum(
            (self._position_notional(position) for position in snapshot.positions),
            start=Decimal("0"),
        )

    def _pending_notional_for_symbol(
        self,
        *,
        snapshot: ExchangeAccountSnapshot | None,
        symbol: str,
    ) -> Decimal:
        if snapshot is None:
            return Decimal("0")
        return sum(
            (
                self._open_order_remaining_notional(order)
                for order in snapshot.open_orders
                if self._open_order_symbol(order) == symbol
            ),
            start=Decimal("0"),
        )

    def _total_pending_notional(self, *, snapshot: ExchangeAccountSnapshot | None) -> Decimal:
        if snapshot is None:
            return Decimal("0")
        return sum(
            (self._open_order_remaining_notional(order) for order in snapshot.open_orders),
            start=Decimal("0"),
        )

    def _open_order_remaining_notional(self, order: ExchangeOpenOrder) -> Decimal:
        remaining_qty = max(to_decimal(order.quantity) - to_decimal(order.filled_quantity), Decimal("0"))
        if remaining_qty <= EPSILON_DECIMAL_12:
            return Decimal("0")
        reference_price = (
            to_decimal(order.price)
            if order.price is not None and to_decimal(order.price) > Decimal("0")
            else self._safe_price(self._open_order_symbol(order))
        )
        return remaining_qty * max(reference_price, Decimal("0"))

    @staticmethod
    def _open_order_symbol(order: ExchangeOpenOrder) -> str:
        instrument_id = str(getattr(order, "instrument_id", "") or "").strip()
        return instrument_id

    def _position_notional(self, position: ExchangePosition) -> Decimal:
        if position.notional_usd is not None:
            return abs(to_decimal(position.notional_usd))
        reference_price = (
            to_decimal(position.mark_price)
            if position.mark_price is not None and to_decimal(position.mark_price) > Decimal("0")
            else self._safe_price(position.symbol)
        )
        return abs(to_decimal(position.quantity)) * max(reference_price, Decimal("0"))

    def _daily_realized_loss_usdt(self) -> Decimal:
        bills_getter = getattr(self.account_service, "latest_recent_bills", None)
        if not callable(bills_getter):
            return Decimal("0")
        today_utc = utc_now().astimezone(timezone.utc).date()
        realized_loss = Decimal("0")
        for row in bills_getter():
            if not isinstance(row, dict):
                continue
            bill_ts = self._bill_timestamp(row)
            if bill_ts is None or bill_ts.astimezone(timezone.utc).date() != today_utc:
                continue
            pnl = row.get("pnl")
            if pnl in {None, ""}:
                continue
            pnl_value = to_decimal(pnl)
            if pnl_value < Decimal("0"):
                realized_loss += abs(pnl_value)
        return realized_loss

    @staticmethod
    def _bill_timestamp(row: dict[str, Any]) -> datetime | None:
        for key in ("ts", "billTs", "fillTime"):
            value = row.get(key)
            if value in {None, ""}:
                continue
            try:
                return datetime.fromtimestamp(int(str(value)) / 1000, tz=timezone.utc)
            except (TypeError, ValueError, OSError):
                continue
        return None

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
