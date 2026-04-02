from __future__ import annotations

from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, to_decimal

from .adaptive import IndependentAdaptiveSnapshot
from .models import IndependentBookAction, IndependentSizingOutcome
from .models import IndependentLeg


def compute_entry_target_qty(
    *,
    settings: AATSSettings,
    directional_leg_target_qty: Decimal,
) -> Decimal:
    return max(to_decimal(settings.default_order_qty), to_decimal(directional_leg_target_qty))


def compute_scale_in_target_qty(
    *,
    base_target_qty: Decimal,
    current_qty: Decimal,
) -> Decimal:
    return max(to_decimal(base_target_qty), to_decimal(current_qty))


def compute_size_down_multiplier(
    *,
    current_qty: Decimal,
    target_qty: Decimal,
) -> Decimal:
    current_qty = max(to_decimal(current_qty), Decimal("0"))
    target_qty = max(to_decimal(target_qty), Decimal("0"))
    if current_qty <= EPSILON_DECIMAL_12:
        return Decimal("1")
    return max(Decimal("0"), min(Decimal("1"), target_qty / current_qty))


def compute_leg_capital_multiplier(
    *,
    base_target_qty: Decimal,
    target_qty: Decimal,
) -> Decimal:
    base_target_qty = max(to_decimal(base_target_qty), Decimal("0"))
    target_qty = max(to_decimal(target_qty), Decimal("0"))
    if base_target_qty <= EPSILON_DECIMAL_12:
        return Decimal("1")
    return max(Decimal("0"), target_qty / base_target_qty)


def build_sizing_outcome(
    *,
    book_action: IndependentBookAction,
    current_qty: Decimal,
    target_qty: Decimal,
    base_target_qty: Decimal,
    sizing_reason_codes: tuple[str, ...] = (),
) -> IndependentSizingOutcome:
    size_multiplier = (
        compute_size_down_multiplier(current_qty=current_qty, target_qty=target_qty)
        if book_action in {"de_risk", "close_failed_thesis", "close_stale_thesis"}
        else Decimal("1")
    )
    capital_multiplier = compute_leg_capital_multiplier(
        base_target_qty=base_target_qty,
        target_qty=target_qty,
    )
    return IndependentSizingOutcome(
        target_qty=target_qty,
        base_target_qty=base_target_qty,
        size_multiplier=size_multiplier,
        capital_multiplier=capital_multiplier,
        scale_in_allowed=book_action == "scale_in",
        sizing_reason_codes=sizing_reason_codes,
    )


def resolve_entry_size_multiplier(
    *,
    settings: AATSSettings,
    leg: IndependentLeg,
    threshold_snapshot: IndependentAdaptiveSnapshot | None,
) -> tuple[Decimal, tuple[str, ...]]:
    multiplier = Decimal("1")
    reasons: list[str] = []
    if (
        bool(settings.strategy_hedge_independent_size_down_entry_enabled)
        and threshold_snapshot is not None
        and threshold_snapshot.capital_multiplier is not None
    ):
        capital_multiplier = max(
            float(settings.strategy_hedge_independent_entry_size_down_floor),
            min(float(threshold_snapshot.capital_multiplier), 1.0),
        )
        multiplier *= to_decimal(capital_multiplier)
        if capital_multiplier < 1.0:
            reasons.append(f"independent_{leg}_book_size_down_entry_enabled")
    if (
        bool(settings.strategy_hedge_independent_long_short_asymmetry_enabled)
        and leg == "short"
    ):
        asymmetry_multiplier = max(
            0.0,
            min(float(settings.strategy_hedge_independent_short_asymmetry_penalty_multiplier), 1.0),
        )
        multiplier *= to_decimal(asymmetry_multiplier)
        if asymmetry_multiplier < 1.0:
            reasons.append("independent_short_book_asymmetry_penalty_applied")
    return multiplier, tuple(reasons)
