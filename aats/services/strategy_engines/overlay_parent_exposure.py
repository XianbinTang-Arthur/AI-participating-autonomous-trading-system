from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from aats.bootstrap.settings import AATSSettings
from aats.schemas.decision import DecisionContext, PositionTarget
from aats.schemas.strategy_runtime import StrategyFamily
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, to_decimal

OverlayParentExposureLifecycleState = Literal[
    "flat",
    "target_only",
    "inventory_only",
    "target_and_inventory",
]
OverlayParentExposureSourceOfTruth = Literal[
    "target_position",
    "inventory",
    "mixed",
    "flat",
]


@dataclass(frozen=True, slots=True)
class OverlayMainLegContract:
    symbol: str
    target_leverage: float
    margin_mode: str
    long_target_qty: Decimal
    short_target_qty: Decimal
    source: str = "directional_target"


@dataclass(frozen=True, slots=True)
class OverlayParentExposureLifecycle:
    symbol: str
    target_leverage: float
    margin_mode: str
    target_long_qty: Decimal
    target_short_qty: Decimal
    current_long_qty: Decimal
    current_short_qty: Decimal
    target_qty: Decimal = Decimal("0")
    current_qty: Decimal = Decimal("0")
    effective_qty: Decimal = Decimal("0")
    target_signal: str = "flat"
    current_signal: str = "flat"
    effective_signal: str = "flat"
    signal_source: str = "target_position"
    source_of_truth: OverlayParentExposureSourceOfTruth = "flat"
    parent_family: StrategyFamily = "directional"
    lifecycle_state: OverlayParentExposureLifecycleState = "flat"
    target_active: bool = False
    inventory_active: bool = False
    source: str = "directional_target_with_inventory_continuity"


OverlayParentExposureContract = OverlayParentExposureLifecycle


def context_or_settings_margin_mode(*, settings: AATSSettings, context: DecisionContext) -> str:
    current_position_state = context.current_position_state
    if current_position_state is not None:
        normalized = str(current_position_state.margin_mode or "").strip().lower()
        if normalized in {"cash", "cross", "isolated"}:
            return normalized
    return str(settings.margin_mode)


def exposure_side(quantity: Decimal) -> str:
    if quantity > EPSILON_DECIMAL_12:
        return "long"
    if quantity < -EPSILON_DECIMAL_12:
        return "short"
    return "flat"


def signed_exposure_qty(*, signal: str, long_qty: Decimal, short_qty: Decimal) -> Decimal:
    if signal == "long":
        return max(long_qty, Decimal("0"))
    if signal == "short":
        return -max(short_qty, Decimal("0"))
    return Decimal("0")


def resolve_overlay_main_leg_signal_from_inventory(*, context: DecisionContext) -> str:
    current_long_qty = to_decimal(context.current_long_position_qty)
    current_short_qty = to_decimal(context.current_short_position_qty)
    current_exposure_side = str(context.current_exposure_side or "").strip().lower()
    if current_exposure_side == "long" and current_long_qty > EPSILON_DECIMAL_12:
        return "long"
    if current_exposure_side == "short" and current_short_qty > EPSILON_DECIMAL_12:
        return "short"
    if current_long_qty > current_short_qty + EPSILON_DECIMAL_12:
        return "long"
    if current_short_qty > current_long_qty + EPSILON_DECIMAL_12:
        return "short"
    if current_long_qty > EPSILON_DECIMAL_12:
        return "long"
    if current_short_qty > EPSILON_DECIMAL_12:
        return "short"
    return "flat"


def directional_primary_leg_targets(
    directional_target: PositionTarget,
) -> tuple[Decimal, Decimal, float, str | None, str | None, str] | None:
    strategy_execution_legs = tuple(getattr(directional_target, "strategy_execution_legs", ()) or ())
    primary_legs = tuple(
        leg
        for leg in strategy_execution_legs
        if getattr(leg, "family", None) == "directional" and getattr(leg, "role", None) == "primary"
    )
    if not primary_legs:
        return None
    long_target_qty = Decimal("0")
    short_target_qty = Decimal("0")
    target_leverage = 0.0
    margin_mode: str | None = None
    symbol: str | None = None
    for leg in primary_legs:
        symbol = symbol or str(getattr(leg, "symbol", "") or "").strip() or None
        target_leverage = max(float(getattr(leg, "target_leverage", 0.0) or 0.0), target_leverage)
        leg_margin_mode = str(getattr(leg, "margin_mode", "") or "").strip().lower()
        if margin_mode is None and leg_margin_mode in {"cash", "cross", "isolated"}:
            margin_mode = leg_margin_mode
        leg_target_qty = to_decimal(getattr(leg, "target_position_qty", Decimal("0")) or Decimal("0"))
        if getattr(leg, "pos_side", None) == "long":
            long_target_qty = max(leg_target_qty, Decimal("0"))
        elif getattr(leg, "pos_side", None) == "short":
            short_target_qty = max(-leg_target_qty, Decimal("0"))
    return long_target_qty, short_target_qty, target_leverage, margin_mode, symbol, "directional_primary_legs"


def resolve_overlay_parent_exposure_from_direct_args(
    *,
    settings: AATSSettings,
    context: DecisionContext,
    long_target_qty: Decimal | None,
    short_target_qty: Decimal | None,
    parent_family: StrategyFamily = "directional",
) -> OverlayParentExposureLifecycle:
    target_leverage = max(float(getattr(context, "current_target_leverage", 0.0) or 0.0), 0.0)
    if target_leverage <= 0.0:
        target_leverage = max(float(settings.default_target_leverage), 1.0)
    return _build_overlay_parent_exposure(
        parent_family=parent_family,
        symbol=context.symbol,
        target_leverage=target_leverage,
        margin_mode=context_or_settings_margin_mode(settings=settings, context=context),
        target_long_qty=max(to_decimal(long_target_qty or Decimal("0")), Decimal("0")),
        target_short_qty=max(to_decimal(short_target_qty or Decimal("0")), Decimal("0")),
        current_long_qty=to_decimal(context.current_long_position_qty),
        current_short_qty=to_decimal(context.current_short_position_qty),
        context=context,
        source="direct_target_args",
    )


def resolve_overlay_parent_exposure_lifecycle(
    *,
    settings: AATSSettings,
    context: DecisionContext,
    directional_target: PositionTarget,
    parent_family: StrategyFamily = "directional",
) -> OverlayParentExposureLifecycle:
    source = "directional_target_with_inventory_continuity"
    target_position_qty = to_decimal(directional_target.target_position_qty)
    target_long_qty = max(target_position_qty, Decimal("0"))
    target_short_qty = max(-target_position_qty, Decimal("0"))
    target_leverage = float(getattr(directional_target, "target_leverage", 0.0) or 0.0)
    margin_mode = str(getattr(directional_target, "margin_mode", "") or "").strip().lower()
    symbol = str(getattr(directional_target, "symbol", "") or "").strip()
    directional_primary_legs = directional_primary_leg_targets(directional_target)
    if directional_primary_legs is not None:
        (
            target_long_qty,
            target_short_qty,
            target_leverage_from_legs,
            margin_mode_from_legs,
            symbol_from_legs,
            source,
        ) = directional_primary_legs
        if target_leverage_from_legs > 0.0:
            target_leverage = target_leverage_from_legs
        if margin_mode_from_legs is not None:
            margin_mode = margin_mode_from_legs
        if symbol_from_legs:
            symbol = symbol_from_legs

    if not symbol:
        symbol = context.symbol
        source = "context_or_settings_fallback"

    if target_leverage <= 0.0:
        target_leverage = max(float(getattr(context, "current_target_leverage", 0.0) or 0.0), 0.0)
        source = "context_or_settings_fallback"
    if target_leverage <= 0.0:
        target_leverage = max(float(settings.default_target_leverage), 1.0)
        source = "context_or_settings_fallback"

    if margin_mode not in {"cash", "cross", "isolated"}:
        margin_mode = context_or_settings_margin_mode(settings=settings, context=context)
        source = "context_or_settings_fallback"
    return _build_overlay_parent_exposure(
        parent_family=parent_family,
        symbol=symbol,
        target_leverage=target_leverage,
        margin_mode=margin_mode,
        target_long_qty=target_long_qty,
        target_short_qty=target_short_qty,
        current_long_qty=to_decimal(context.current_long_position_qty),
        current_short_qty=to_decimal(context.current_short_position_qty),
        context=context,
        source=source,
    )


def resolve_overlay_main_leg_contract(
    parent_exposure: OverlayParentExposureLifecycle,
) -> OverlayMainLegContract:
    return OverlayMainLegContract(
        symbol=parent_exposure.symbol,
        target_leverage=parent_exposure.target_leverage,
        margin_mode=parent_exposure.margin_mode,
        long_target_qty=parent_exposure.target_long_qty,
        short_target_qty=parent_exposure.target_short_qty,
        source=parent_exposure.source,
    )


def _build_overlay_parent_exposure(
    *,
    symbol: str,
    target_leverage: float,
    margin_mode: str,
    target_long_qty: Decimal,
    target_short_qty: Decimal,
    current_long_qty: Decimal,
    current_short_qty: Decimal,
    parent_family: StrategyFamily = "directional",
    context: DecisionContext | None = None,
    source: str,
) -> OverlayParentExposureLifecycle:
    target_signal = exposure_side(target_long_qty - target_short_qty)
    current_signal = (
        resolve_overlay_main_leg_signal_from_inventory(context=context)
        if context is not None
        else exposure_side(current_long_qty - current_short_qty)
    )
    effective_signal = target_signal
    signal_source = "target_position"
    if effective_signal == "flat" and current_signal != "flat":
        effective_signal = current_signal
        signal_source = "inventory"
    target_active = target_signal != "flat"
    inventory_active = current_signal != "flat"
    if target_active and inventory_active:
        lifecycle_state: OverlayParentExposureLifecycleState = "target_and_inventory"
        source_of_truth: OverlayParentExposureSourceOfTruth = "mixed"
    elif target_active:
        lifecycle_state = "target_only"
        source_of_truth = "target_position"
    elif inventory_active:
        lifecycle_state = "inventory_only"
        source_of_truth = "inventory"
    else:
        lifecycle_state = "flat"
        source_of_truth = "flat"
    target_qty = signed_exposure_qty(
        signal=target_signal,
        long_qty=target_long_qty,
        short_qty=target_short_qty,
    )
    current_qty = signed_exposure_qty(
        signal=current_signal,
        long_qty=current_long_qty,
        short_qty=current_short_qty,
    )
    effective_qty = target_qty if signal_source == "target_position" else current_qty
    return OverlayParentExposureLifecycle(
        parent_family=parent_family,
        symbol=symbol,
        target_leverage=target_leverage,
        margin_mode=margin_mode,
        target_long_qty=target_long_qty,
        target_short_qty=target_short_qty,
        current_long_qty=current_long_qty,
        current_short_qty=current_short_qty,
        target_qty=target_qty,
        current_qty=current_qty,
        effective_qty=effective_qty,
        target_signal=target_signal,
        current_signal=current_signal,
        effective_signal=effective_signal,
        signal_source=signal_source,
        source_of_truth=source_of_truth,
        lifecycle_state=lifecycle_state,
        target_active=target_active,
        inventory_active=inventory_active,
        source=source,
    )
