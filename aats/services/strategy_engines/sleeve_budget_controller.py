from __future__ import annotations

from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.schemas.decision import BaselineAssessment
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, quantize_decimal, to_decimal
from aats.services.strategy_engines.sleeve_reason_codes import (
    BASELINE_VOLATILITY_CONTRACTION_ACTIVE,
    BUDGET_CONTRACTED_TO_ZERO,
    HARD_LOSS_BUDGET_BLOCK,
    NO_BUDGET_CONTRACTION,
    PNL_CONTRACTION_ACTIVE,
    RECONCILIATION_CONTRACTION_ACTIVE,
    RECONCILIATION_HARD_BLOCK,
    SCALE_BELOW_MIN_TRADEABLE_STEP,
    unique_reason_codes,
)
from aats.services.strategy_engines.sleeve_routing_models import (
    BudgetControlDecision,
    RawSleeveCandidateInputs,
)


class SleeveBudgetController:
    def __init__(self, settings: AATSSettings) -> None:
        self.settings = settings

    def evaluate(
        self,
        *,
        raw: RawSleeveCandidateInputs,
        baseline: BaselineAssessment,
        recent_net_pnl: Decimal,
        latest_reconciliation,
    ) -> BudgetControlDecision:
        min_budget = self._decimal(self.settings.strategy_sleeve_auto_min_budget_multiplier)
        reconciliation_cap = self._decimal(self.settings.strategy_sleeve_auto_reconciliation_contraction_multiplier)
        base_scale = Decimal("1")
        custom_multiplier = Decimal("1")
        reconciliation_multiplier = Decimal("1")
        pnl_multiplier = Decimal("1")
        capacity_multiplier = Decimal("1")
        reasons: list[str] = []
        trace: list[str] = [f"base_scale={format(base_scale, 'f')}"]

        if not raw.protective_intent:
            if self.settings.strategy_sleeve_auto_volatility_cap_enabled:
                volatility_cap = self._clamp(
                    to_decimal(baseline.volatility_target_scale),
                    lower=min_budget,
                    upper=Decimal("1"),
                )
                if volatility_cap < Decimal("1") - EPSILON_DECIMAL_12:
                    custom_multiplier = volatility_cap
                    reasons.append(BASELINE_VOLATILITY_CONTRACTION_ACTIVE)
                    trace.append(f"baseline_volatility_cap={format(volatility_cap, 'f')}")

            if latest_reconciliation is not None:
                if latest_reconciliation.halt_required or latest_reconciliation.resume_blocking:
                    if raw.active_inventory:
                        reconciliation_multiplier = min(reconciliation_multiplier, reconciliation_cap)
                        reasons.append(RECONCILIATION_CONTRACTION_ACTIVE)
                        trace.append(
                            f"reconciliation_resume_blocking={format(reconciliation_multiplier, 'f')}"
                        )
                    else:
                        reconciliation_multiplier = Decimal("0")
                        reasons.append(RECONCILIATION_HARD_BLOCK)
                        trace.append("reconciliation_hard_block=0")
                elif (
                    latest_reconciliation.only_reduce_required
                    or latest_reconciliation.review_required
                    or str(latest_reconciliation.severity or "").upper() not in {"", "CLEAN"}
                ):
                    reconciliation_multiplier = min(reconciliation_multiplier, reconciliation_cap)
                    reasons.append(RECONCILIATION_CONTRACTION_ACTIVE)
                    trace.append(
                        f"reconciliation_contraction={format(reconciliation_multiplier, 'f')}"
                    )

            if recent_net_pnl < -EPSILON_DECIMAL_12:
                soft_loss = self._decimal(self.settings.strategy_sleeve_auto_soft_loss_usdt)
                hard_loss = self._decimal(self.settings.strategy_sleeve_auto_hard_loss_usdt)
                if raw.family != "directional" and not raw.active_inventory and hard_loss > EPSILON_DECIMAL_12:
                    if abs(recent_net_pnl) >= hard_loss:
                        pnl_multiplier = Decimal("0")
                        reasons.append(HARD_LOSS_BUDGET_BLOCK)
                        trace.append("hard_loss_block=0")
                if pnl_multiplier > EPSILON_DECIMAL_12 and soft_loss > EPSILON_DECIMAL_12:
                    loss_ratio = min(abs(recent_net_pnl) / soft_loss, Decimal("1"))
                    soft_loss_multiplier = max(
                        min_budget,
                        Decimal("1") - (loss_ratio * Decimal("0.5")),
                    )
                    if soft_loss_multiplier < Decimal("1") - EPSILON_DECIMAL_12:
                        pnl_multiplier = min(pnl_multiplier, soft_loss_multiplier)
                        reasons.append(PNL_CONTRACTION_ACTIVE)
                        trace.append(f"pnl_contraction={format(pnl_multiplier, 'f')}")

        effective_scale = min(
            base_scale,
            custom_multiplier,
            reconciliation_multiplier,
            pnl_multiplier,
            capacity_multiplier,
        )
        if effective_scale <= EPSILON_DECIMAL_12:
            effective_scale = Decimal("0")
            reasons.append(BUDGET_CONTRACTED_TO_ZERO)

        scaled_delta = quantize_decimal(raw.delta_position_qty * effective_scale)
        budget_zero_suppressed = abs(raw.delta_position_qty) > Decimal("0") and abs(scaled_delta) <= EPSILON_DECIMAL_12
        if budget_zero_suppressed and effective_scale > EPSILON_DECIMAL_12:
            reasons.append(SCALE_BELOW_MIN_TRADEABLE_STEP)
        elif budget_zero_suppressed:
            reasons.append(BUDGET_CONTRACTED_TO_ZERO)
        scaled_target = raw.current_position_qty + scaled_delta
        scaled_legs = self._scale_legs(raw.requested_legs, effective_scale)

        contraction_reason_codes = unique_reason_codes(reasons or [NO_BUDGET_CONTRACTION])
        return BudgetControlDecision(
            requested_delta_position_qty=quantize_decimal(raw.delta_position_qty),
            requested_target_position_qty=quantize_decimal(raw.target_position_qty),
            base_scale=quantize_decimal(base_scale),
            effective_scale=quantize_decimal(effective_scale),
            pnl_contraction_multiplier=quantize_decimal(pnl_multiplier),
            reconciliation_contraction_multiplier=quantize_decimal(reconciliation_multiplier),
            capacity_contraction_multiplier=quantize_decimal(capacity_multiplier),
            custom_contraction_multiplier=quantize_decimal(custom_multiplier),
            scaled_delta_position_qty=scaled_delta,
            scaled_target_position_qty=quantize_decimal(scaled_target),
            scaled_legs=scaled_legs,
            contraction_reason_codes=contraction_reason_codes,
            scale_trace=tuple(trace),
            budget_zero_suppressed=budget_zero_suppressed,
        )

    @staticmethod
    def _scale_legs(legs: tuple, multiplier: Decimal) -> tuple:
        scaled = []
        for leg in legs:
            current_qty = to_decimal(leg.current_position_qty)
            delta_qty = to_decimal(leg.delta_position_qty)
            scaled_delta = quantize_decimal(delta_qty * multiplier)
            scaled.append(
                leg.model_copy(
                    update={
                        "delta_position_qty": scaled_delta,
                        "target_position_qty": current_qty + scaled_delta,
                        "note": (
                            f"{leg.note} | auto_budget_effective_scale={format(multiplier, 'f')}"
                            if leg.note
                            else f"auto_budget_effective_scale={format(multiplier, 'f')}"
                        ),
                    }
                )
            )
        return tuple(scaled)

    @staticmethod
    def _clamp(value: Decimal, *, lower: Decimal, upper: Decimal) -> Decimal:
        if value < lower:
            return lower
        if value > upper:
            return upper
        return value

    @staticmethod
    def _decimal(value: Decimal | float | int | str | None) -> Decimal:
        return to_decimal(value)
