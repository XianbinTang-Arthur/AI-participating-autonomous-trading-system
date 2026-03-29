from __future__ import annotations

from decimal import Decimal

from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, to_decimal
from aats.services.strategy_engines.smart_arbitrage.schemas import ArbitragePairState


def resolve_pair_state(
    *,
    pair_id: str,
    account_spot_qty: Decimal,
    account_hedge_qty: Decimal,
    sleeve_spot_qty: Decimal,
    sleeve_hedge_qty: Decimal,
    basis_bps: Decimal,
    exit_threshold_bps: Decimal,
    account_cash_spot_qty: Decimal | None = None,
    account_margin_spot_qty: Decimal | None = None,
    sleeve_cash_spot_qty: Decimal | None = None,
    sleeve_margin_spot_qty: Decimal | None = None,
) -> ArbitragePairState:
    current_cash_spot_qty = to_decimal(sleeve_spot_qty if sleeve_cash_spot_qty is None else sleeve_cash_spot_qty)
    current_margin_spot_qty = to_decimal(Decimal("0") if sleeve_margin_spot_qty is None else sleeve_margin_spot_qty)
    current_spot_qty = current_cash_spot_qty + current_margin_spot_qty
    current_hedge_qty = to_decimal(sleeve_hedge_qty)
    current_short_qty = abs(min(current_hedge_qty, Decimal("0")))
    current_long_qty = max(current_hedge_qty, Decimal("0"))
    positive_pair_qty = min(max(current_cash_spot_qty, Decimal("0")), current_short_qty)
    inventory_reverse_pair_qty = min(abs(min(current_cash_spot_qty, Decimal("0"))), current_long_qty)
    remaining_reverse_long_qty = max(current_long_qty - inventory_reverse_pair_qty, Decimal("0"))
    margin_reverse_pair_qty = min(abs(min(current_margin_spot_qty, Decimal("0"))), remaining_reverse_long_qty)
    reverse_pair_qty = inventory_reverse_pair_qty + margin_reverse_pair_qty
    current_pair_qty = positive_pair_qty + reverse_pair_qty
    resolved_account_cash_spot_qty = to_decimal(account_spot_qty if account_cash_spot_qty is None else account_cash_spot_qty)
    resolved_account_margin_spot_qty = to_decimal(
        Decimal("0") if account_margin_spot_qty is None else account_margin_spot_qty
    )
    current_account_spot_qty = resolved_account_cash_spot_qty + resolved_account_margin_spot_qty
    foreign_spot_qty = current_account_spot_qty - current_spot_qty
    foreign_hedge_qty = to_decimal(account_hedge_qty) - current_hedge_qty
    unpaired_spot_qty = (
        max(max(current_cash_spot_qty, Decimal("0")) - positive_pair_qty, Decimal("0"))
        + max(abs(min(current_cash_spot_qty, Decimal("0"))) - inventory_reverse_pair_qty, Decimal("0"))
        + max(abs(min(current_margin_spot_qty, Decimal("0"))) - margin_reverse_pair_qty, Decimal("0"))
    )
    unpaired_hedge_qty = (
        max(current_short_qty - positive_pair_qty, Decimal("0"))
        + max(current_long_qty - reverse_pair_qty, Decimal("0"))
    )
    blocking_reasons: list[str] = []
    reverse_mode_conflict = (
        inventory_reverse_pair_qty > EPSILON_DECIMAL_12 and margin_reverse_pair_qty > EPSILON_DECIMAL_12
    )
    if positive_pair_qty > EPSILON_DECIMAL_12 and reverse_pair_qty > EPSILON_DECIMAL_12:
        current_direction = "mixed"
        blocking_reasons.append("smart_arbitrage_mixed_pair_direction_detected")
    elif reverse_mode_conflict:
        current_direction = "mixed"
        blocking_reasons.append("smart_arbitrage_mixed_reverse_execution_modes_detected")
    elif (
        positive_pair_qty > EPSILON_DECIMAL_12
        or current_cash_spot_qty > EPSILON_DECIMAL_12
        or current_hedge_qty < -EPSILON_DECIMAL_12
    ):
        current_direction = "positive_carry"
    elif (
        reverse_pair_qty > EPSILON_DECIMAL_12
        or current_cash_spot_qty < -EPSILON_DECIMAL_12
        or current_margin_spot_qty < -EPSILON_DECIMAL_12
        or current_hedge_qty > EPSILON_DECIMAL_12
    ):
        current_direction = "reverse_carry"
    else:
        current_direction = "flat"
    recovery_required = (
        current_direction in {"positive_carry", "reverse_carry"}
        and (unpaired_spot_qty > EPSILON_DECIMAL_12 or unpaired_hedge_qty > EPSILON_DECIMAL_12)
    )
    unwind_required = False
    state_phase = "inactive"
    if current_direction == "positive_carry":
        unwind_required = basis_bps <= exit_threshold_bps
        state_phase = "recovery" if recovery_required else ("unwinding" if unwind_required else "active")
    elif current_direction == "reverse_carry":
        unwind_required = basis_bps >= -exit_threshold_bps
        state_phase = "recovery" if recovery_required else ("unwinding" if unwind_required else "active")
    elif current_direction == "mixed":
        state_phase = "blocked"
    return ArbitragePairState(
        pair_id=pair_id,
        state_phase=state_phase,  # type: ignore[arg-type]
        current_direction=current_direction,  # type: ignore[arg-type]
        current_spot_qty=current_spot_qty,
        current_cash_spot_qty=current_cash_spot_qty,
        current_margin_spot_qty=current_margin_spot_qty,
        current_hedge_qty=current_hedge_qty,
        current_positive_pair_qty=positive_pair_qty,
        current_reverse_pair_qty=reverse_pair_qty,
        current_inventory_reverse_pair_qty=inventory_reverse_pair_qty,
        current_margin_reverse_pair_qty=margin_reverse_pair_qty,
        current_pair_qty=current_pair_qty,
        current_account_spot_qty=current_account_spot_qty,
        current_account_cash_spot_qty=resolved_account_cash_spot_qty,
        current_account_margin_spot_qty=resolved_account_margin_spot_qty,
        current_account_hedge_qty=to_decimal(account_hedge_qty),
        foreign_spot_qty=foreign_spot_qty,
        foreign_hedge_qty=foreign_hedge_qty,
        unpaired_spot_qty=unpaired_spot_qty,
        unpaired_hedge_qty=unpaired_hedge_qty,
        current_short_qty=current_short_qty,
        current_long_qty=current_long_qty,
        recovery_required=recovery_required,
        unwind_required=unwind_required,
        blocking_reasons=blocking_reasons,
    )
