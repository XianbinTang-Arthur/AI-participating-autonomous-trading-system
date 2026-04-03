from __future__ import annotations

from decimal import Decimal

from aats.services.portfolio_service.decimals import quantize_decimal, to_decimal
from aats.services.strategy_engines.sleeve_reason_codes import (
    APPROVED_BUT_BUDGET_ZERO_SUPPRESSED,
    COMPOSED_AS_ADVISORY_ONLY,
    COMPOSED_AS_HOLD_CURRENT,
    COMPOSED_AS_OVERRIDE_TARGET,
    COMPOSED_AS_PROTECTIVE_EXECUTION,
    unique_reason_codes,
)
from aats.services.strategy_engines.sleeve_routing_models import (
    BudgetControlDecision,
    ComposedSleeveRoutingDecision,
    ExecutionPermissionDecision,
    RawSleeveCandidateInputs,
)


class SleeveRoutingComposer:
    def compose(
        self,
        *,
        raw: RawSleeveCandidateInputs,
        permission: ExecutionPermissionDecision,
        budget: BudgetControlDecision,
    ) -> ComposedSleeveRoutingDecision:
        if permission.is_protective_override:
            composed_legs = self._annotate_legs(
                raw.requested_legs,
                notes=(
                    "execution_permission:protective_override",
                    "composition:protective_execute",
                ),
            )
            return ComposedSleeveRoutingDecision(
                route_action=raw.route_action,
                approved_for_execution=permission.approved_for_execution,
                execution_permission_mode=permission.permission_mode,
                execution_control_mode="protective_override",
                execution_behavior="protective_execute",
                requested_delta_position_qty=raw.delta_position_qty,
                composed_delta_position_qty=raw.delta_position_qty,
                requested_target_position_qty=raw.target_position_qty,
                composed_target_position_qty=raw.target_position_qty,
                requested_legs=raw.requested_legs,
                composed_legs=composed_legs,
                permission_reason_codes=permission.reason_codes,
                budget_reason_codes=budget.contraction_reason_codes,
                composition_reason_codes=unique_reason_codes([COMPOSED_AS_PROTECTIVE_EXECUTION]),
                budget_zero_suppressed=False,
            )

        if not permission.approved_for_execution:
            route_action = "hold_current" if raw.active_inventory else "advisory_only"
            composed_legs = self._hold_legs(
                raw.requested_legs,
                hold_notes=(
                    "execution_permission:denied",
                    f"composition:{route_action}",
                ),
            )
            return ComposedSleeveRoutingDecision(
                route_action=route_action,
                approved_for_execution=False,
                execution_permission_mode=permission.permission_mode,
                execution_control_mode="permission_denied",
                execution_behavior="hold_current" if route_action == "hold_current" else "advisory_only",
                requested_delta_position_qty=raw.delta_position_qty,
                composed_delta_position_qty=Decimal("0"),
                requested_target_position_qty=raw.target_position_qty,
                composed_target_position_qty=raw.current_position_qty,
                requested_legs=raw.requested_legs,
                composed_legs=composed_legs,
                permission_reason_codes=permission.reason_codes,
                budget_reason_codes=budget.contraction_reason_codes,
                composition_reason_codes=unique_reason_codes(
                    [COMPOSED_AS_HOLD_CURRENT if route_action == "hold_current" else COMPOSED_AS_ADVISORY_ONLY]
                ),
                budget_zero_suppressed=False,
            )

        if budget.budget_zero_suppressed:
            route_action = "hold_current" if raw.active_inventory else "advisory_only"
            composed_legs = self._hold_legs(
                raw.requested_legs,
                hold_notes=(
                    "execution_permission:approved",
                    "budget_control:zero_suppressed",
                    f"composition:{route_action}",
                ),
            )
            return ComposedSleeveRoutingDecision(
                route_action=route_action,
                approved_for_execution=True,
                execution_permission_mode=permission.permission_mode,
                execution_control_mode="budget_zero_suppressed",
                execution_behavior="suppressed_after_approval",
                requested_delta_position_qty=raw.delta_position_qty,
                composed_delta_position_qty=Decimal("0"),
                requested_target_position_qty=raw.target_position_qty,
                composed_target_position_qty=raw.current_position_qty,
                requested_legs=raw.requested_legs,
                composed_legs=composed_legs,
                permission_reason_codes=permission.reason_codes,
                budget_reason_codes=budget.contraction_reason_codes,
                composition_reason_codes=unique_reason_codes(
                    [APPROVED_BUT_BUDGET_ZERO_SUPPRESSED],
                    [COMPOSED_AS_HOLD_CURRENT if route_action == "hold_current" else COMPOSED_AS_ADVISORY_ONLY],
                ),
                budget_zero_suppressed=True,
            )

        composition_reason = (
            COMPOSED_AS_OVERRIDE_TARGET if raw.route_action == "override_target" else COMPOSED_AS_HOLD_CURRENT
        )
        composed_legs = self._annotate_legs(
            budget.scaled_legs,
            notes=(
                "execution_permission:approved",
                f"composition:{raw.route_action}",
            ),
        )
        return ComposedSleeveRoutingDecision(
            route_action=raw.route_action,
            approved_for_execution=True,
            execution_permission_mode=permission.permission_mode,
            execution_control_mode="approved",
            execution_behavior="execute_target" if raw.route_action == "override_target" else "hold_current",
            requested_delta_position_qty=raw.delta_position_qty,
            composed_delta_position_qty=budget.scaled_delta_position_qty,
            requested_target_position_qty=raw.target_position_qty,
            composed_target_position_qty=budget.scaled_target_position_qty,
            requested_legs=raw.requested_legs,
            composed_legs=composed_legs,
            permission_reason_codes=permission.reason_codes,
            budget_reason_codes=budget.contraction_reason_codes,
            composition_reason_codes=unique_reason_codes([composition_reason]),
            budget_zero_suppressed=False,
        )

    @staticmethod
    def _hold_legs(legs: tuple, *, hold_notes: tuple[str, ...]) -> tuple:
        held = []
        for leg in legs:
            current_qty = to_decimal(leg.current_position_qty)
            held.append(
                leg.model_copy(
                    update={
                        "delta_position_qty": Decimal("0"),
                        "target_position_qty": quantize_decimal(current_qty),
                        "note": SleeveRoutingComposer._combined_notes(leg.note, hold_notes),
                    }
                )
            )
        return tuple(held)

    @staticmethod
    def _annotate_legs(legs: tuple, *, notes: tuple[str, ...]) -> tuple:
        annotated = []
        for leg in legs:
            annotated.append(
                leg.model_copy(
                    update={
                        "note": SleeveRoutingComposer._combined_notes(leg.note, notes),
                    }
                )
            )
        return tuple(annotated)

    @staticmethod
    def _combined_notes(existing_note: str | None, notes: tuple[str, ...]) -> str:
        ordered_notes = [note for note in notes if str(note or "").strip()]
        if existing_note:
            ordered_notes.insert(0, existing_note)
        return " | ".join(ordered_notes)
