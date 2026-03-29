from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field


class TradeDragProfile(BaseModel):
    model_name: str = "generic"
    cost_model_enabled: bool = True
    edge_reference_bps: Decimal = Decimal("0")
    expected_hold_hours: Decimal = Decimal("0")
    expected_funding_events: int = 0
    borrow_hour_windows: int = 0
    ideal_open_fee_bps: Decimal = Decimal("0")
    ideal_close_fee_bps: Decimal = Decimal("0")
    executable_spread_bps: Decimal = Decimal("0")
    executable_slippage_bps: Decimal = Decimal("0")
    execution_mismatch_bps: Decimal = Decimal("0")
    funding_cost_bps: Decimal = Decimal("0")
    borrow_cost_bps: Decimal = Decimal("0")
    transfer_cost_bps: Decimal = Decimal("0")
    time_decay_cost_bps: Decimal = Decimal("0")
    explicit_cost_components_bps: dict[str, Decimal] = Field(default_factory=dict)
    execution_drag_components_bps: dict[str, Decimal] = Field(default_factory=dict)
    legacy_total_cost_bps: Decimal = Decimal("0")
    cost_source_flags: list[str] = Field(default_factory=list)


class TradeDragEstimate(BaseModel):
    model_name: str = "generic"
    edge_reference_bps: Decimal = Decimal("0")
    ideal_open_fee_bps: Decimal = Decimal("0")
    ideal_close_fee_bps: Decimal = Decimal("0")
    ideal_total_fee_bps: Decimal = Decimal("0")
    executable_spread_bps: Decimal = Decimal("0")
    executable_slippage_bps: Decimal = Decimal("0")
    execution_mismatch_bps: Decimal = Decimal("0")
    funding_cost_bps: Decimal = Decimal("0")
    borrow_cost_bps: Decimal = Decimal("0")
    transfer_cost_bps: Decimal = Decimal("0")
    time_decay_cost_bps: Decimal = Decimal("0")
    explicit_cost_components_bps: dict[str, Decimal] = Field(default_factory=dict)
    execution_drag_components_bps: dict[str, Decimal] = Field(default_factory=dict)
    ideal_total_cost_bps: Decimal = Decimal("0")
    executable_total_drag_bps: Decimal = Decimal("0")
    ideal_edge_bps: Decimal = Decimal("0")
    executable_edge_bps: Decimal = Decimal("0")
    breakeven_reference_bps: Decimal = Decimal("0")
    expected_hold_hours: Decimal = Decimal("0")
    expected_funding_events: int = 0
    borrow_hour_windows: int = 0
    cost_confidence: float = 0.0
    cost_source_flags: list[str] = Field(default_factory=list)


class TradeDragCalculator:
    def estimate(self, *, profile: TradeDragProfile) -> TradeDragEstimate:
        reference_edge_bps = _non_negative(profile.edge_reference_bps)
        fallback_total = _non_negative(profile.legacy_total_cost_bps)
        expected_hold_hours = _non_negative(profile.expected_hold_hours)
        expected_funding_events = max(int(profile.expected_funding_events or 0), 0)
        borrow_hour_windows = max(int(profile.borrow_hour_windows or 0), 0)

        if not profile.cost_model_enabled:
            confidence = 0.35 if fallback_total > Decimal("0") else 0.25
            return TradeDragEstimate(
                model_name=profile.model_name,
                edge_reference_bps=reference_edge_bps,
                ideal_total_cost_bps=fallback_total,
                executable_total_drag_bps=fallback_total,
                ideal_edge_bps=reference_edge_bps - fallback_total,
                executable_edge_bps=reference_edge_bps - fallback_total,
                breakeven_reference_bps=fallback_total,
                expected_hold_hours=expected_hold_hours,
                expected_funding_events=expected_funding_events,
                borrow_hour_windows=borrow_hour_windows,
                cost_confidence=confidence,
                cost_source_flags=["cost_model_disabled", "legacy_estimated_cost_fallback"],
            )

        source_flags = list(dict.fromkeys(profile.cost_source_flags))
        ideal_open_fee_bps = Decimal(str(profile.ideal_open_fee_bps))
        ideal_close_fee_bps = Decimal(str(profile.ideal_close_fee_bps))
        ideal_total_fee_bps = ideal_open_fee_bps + ideal_close_fee_bps
        executable_spread_bps = _non_negative(profile.executable_spread_bps)
        executable_slippage_bps = _non_negative(profile.executable_slippage_bps)
        execution_mismatch_bps = _non_negative(profile.execution_mismatch_bps)
        funding_cost_bps = _non_negative(profile.funding_cost_bps)
        borrow_cost_bps = _non_negative(profile.borrow_cost_bps)
        transfer_cost_bps = _non_negative(profile.transfer_cost_bps)
        time_decay_cost_bps = _non_negative(profile.time_decay_cost_bps)
        explicit_cost_components_bps = _normalized_cost_components(profile.explicit_cost_components_bps)
        execution_drag_components_bps = _normalized_cost_components(profile.execution_drag_components_bps)
        explicit_component_total = sum(explicit_cost_components_bps.values(), start=Decimal("0"))
        execution_drag_component_total = sum(execution_drag_components_bps.values(), start=Decimal("0"))

        ideal_total_cost_bps = (
            ideal_total_fee_bps
            + funding_cost_bps
            + borrow_cost_bps
            + transfer_cost_bps
            + explicit_component_total
        )
        executable_total_drag_bps = (
            ideal_total_cost_bps
            + executable_spread_bps
            + executable_slippage_bps
            + execution_mismatch_bps
            + time_decay_cost_bps
            + execution_drag_component_total
        )
        if abs(executable_total_drag_bps) <= Decimal("1e-12") and fallback_total > Decimal("0"):
            ideal_total_cost_bps = fallback_total
            executable_total_drag_bps = fallback_total
            source_flags.append("legacy_estimated_cost_fallback")

        estimated_execution_drag = (
            executable_spread_bps
            + executable_slippage_bps
            + execution_mismatch_bps
            + execution_drag_component_total
        )
        ideal_edge_bps = reference_edge_bps - ideal_total_cost_bps
        executable_edge_bps = reference_edge_bps - executable_total_drag_bps
        cost_confidence = _cost_confidence(
            source_flags=source_flags,
            has_fee=abs(ideal_total_fee_bps) > Decimal("1e-12"),
            has_execution_drag=estimated_execution_drag > Decimal("0"),
            has_funding=funding_cost_bps > Decimal("0"),
            has_borrow=borrow_cost_bps > Decimal("0"),
        )

        return TradeDragEstimate(
            model_name=profile.model_name,
            edge_reference_bps=reference_edge_bps,
            ideal_open_fee_bps=ideal_open_fee_bps,
            ideal_close_fee_bps=ideal_close_fee_bps,
            ideal_total_fee_bps=ideal_total_fee_bps,
            executable_spread_bps=executable_spread_bps,
            executable_slippage_bps=executable_slippage_bps,
            execution_mismatch_bps=execution_mismatch_bps,
            funding_cost_bps=funding_cost_bps,
            borrow_cost_bps=borrow_cost_bps,
            transfer_cost_bps=transfer_cost_bps,
            time_decay_cost_bps=time_decay_cost_bps,
            explicit_cost_components_bps=explicit_cost_components_bps,
            execution_drag_components_bps=execution_drag_components_bps,
            ideal_total_cost_bps=ideal_total_cost_bps,
            executable_total_drag_bps=executable_total_drag_bps,
            ideal_edge_bps=ideal_edge_bps,
            executable_edge_bps=executable_edge_bps,
            breakeven_reference_bps=executable_total_drag_bps,
            expected_hold_hours=expected_hold_hours,
            expected_funding_events=expected_funding_events,
            borrow_hour_windows=borrow_hour_windows,
            cost_confidence=cost_confidence,
            cost_source_flags=list(dict.fromkeys(source_flags)),
        )


def _non_negative(value: Decimal) -> Decimal:
    return max(value, Decimal("0"))


def _normalized_cost_components(raw_components: dict[str, Decimal]) -> dict[str, Decimal]:
    normalized: dict[str, Decimal] = {}
    for name, value in raw_components.items():
        component_name = str(name or "").strip()
        if not component_name:
            continue
        normalized[component_name] = _non_negative(value)
    return normalized


def _cost_confidence(
    *,
    source_flags: list[str],
    has_fee: bool,
    has_execution_drag: bool,
    has_funding: bool,
    has_borrow: bool,
) -> float:
    confidence = 0.30
    if "legacy_estimated_cost_fallback" in source_flags:
        confidence = max(confidence, 0.35)
    if any(flag == "fee_account_schedule" for flag in source_flags):
        confidence += 0.25
    elif any(flag.startswith("fee_configured") for flag in source_flags):
        confidence += 0.18
    elif has_fee:
        confidence += 0.10
    if has_execution_drag:
        confidence += 0.15
    if any(flag.startswith("funding_account_proxy") for flag in source_flags):
        confidence += 0.12
    elif has_funding:
        confidence += 0.08
    if "borrow_apr_window_model" in source_flags:
        confidence += 0.10
    elif has_borrow:
        confidence += 0.06
    if "time_decay_configured" in source_flags:
        confidence += 0.04
    if "transfer_cost_configured" in source_flags:
        confidence += 0.03
    return max(0.25, min(round(confidence, 4), 0.95))
