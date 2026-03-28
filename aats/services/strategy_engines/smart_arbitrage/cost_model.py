from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any

from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.services.fee_resolver import EffectiveFeeResolver
from aats.services.portfolio_service.decimals import to_decimal
from aats.services.strategy_engines.smart_arbitrage.schemas import ArbitrageCostBreakdown
from aats.services.trade_costs import TradeCostService
from aats.services.trade_drag import TradeDragCalculator, TradeDragProfile


def build_cost_breakdown(
    *,
    settings: AATSSettings,
    basis_bps: Decimal,
    execution_mode: str | None,
    spot_symbol: str | None = None,
    hedge_symbol: str | None = None,
    account_service: Any | None = None,
) -> ArbitrageCostBreakdown:
    basis_abs = basis_bps.copy_abs()
    expected_hold_hours = max(to_decimal(settings.smart_arbitrage_expected_hold_hours), Decimal("0"))
    scoped_account_service = (
        account_service
        if settings.smart_arbitrage_fee_source_mode == "account_schedule"
        else None
    )
    funding_account_service = (
        account_service
        if settings.smart_arbitrage_funding_source_mode == "account_proxy"
        else None
    )
    fee_resolver = EffectiveFeeResolver(settings=settings, account_service=scoped_account_service)
    funding_fee_resolver = EffectiveFeeResolver(settings=settings, account_service=funding_account_service)
    source_flags: list[str] = []
    drag_calculator = TradeDragCalculator()
    trade_cost_service = TradeCostService(settings=settings, fee_resolver=fee_resolver)

    if not settings.smart_arbitrage_cost_model_enabled:
        fallback_total = max(to_decimal(settings.smart_arbitrage_estimated_cost_bps), Decimal("0"))
        estimate = drag_calculator.estimate(
            profile=TradeDragProfile(
                model_name="smart_arbitrage",
                cost_model_enabled=False,
                edge_reference_bps=basis_abs,
                expected_hold_hours=expected_hold_hours,
                legacy_total_cost_bps=fallback_total,
            )
        )
        return ArbitrageCostBreakdown(
            ideal_total_cost_bps=estimate.ideal_total_cost_bps,
            executable_total_drag_bps=estimate.executable_total_drag_bps,
            ideal_edge_bps=estimate.ideal_edge_bps,
            executable_edge_bps=estimate.executable_edge_bps,
            breakeven_basis_bps=estimate.breakeven_reference_bps,
            expected_hold_hours=estimate.expected_hold_hours,
            expected_funding_events=estimate.expected_funding_events,
            borrow_hour_windows=estimate.borrow_hour_windows,
            cost_confidence=estimate.cost_confidence,
            cost_source_flags=estimate.cost_source_flags,
            estimated_total_cost_bps=estimate.executable_total_drag_bps,
            net_edge_bps=estimate.executable_edge_bps,
        )

    ideal_open_fee_bps, ideal_close_fee_bps, fee_flags = _fee_cost_components(
        settings=settings,
        trade_cost_service=trade_cost_service,
        execution_mode=execution_mode,
        spot_symbol=spot_symbol,
        hedge_symbol=hedge_symbol,
    )
    source_flags.extend(fee_flags)
    ideal_total_fee_bps = ideal_open_fee_bps + ideal_close_fee_bps

    executable_spread_bps, spread_flags = _spread_cost_component(
        settings=settings,
        trade_cost_service=trade_cost_service,
        execution_mode=execution_mode,
        spot_symbol=spot_symbol,
        hedge_symbol=hedge_symbol,
    )
    source_flags.extend(spread_flags)
    executable_slippage_bps, slippage_flags = _slippage_cost_component(
        settings=settings,
        trade_cost_service=trade_cost_service,
        execution_mode=execution_mode,
        spot_symbol=spot_symbol,
        hedge_symbol=hedge_symbol,
    )
    source_flags.extend(slippage_flags)
    execution_mismatch_bps = max(to_decimal(settings.smart_arbitrage_estimated_execution_mismatch_bps), Decimal("0"))
    if execution_mismatch_bps > Decimal("0"):
        source_flags.append("execution_mismatch_configured")
    transfer_cost_bps = max(to_decimal(settings.smart_arbitrage_estimated_transfer_cost_bps), Decimal("0"))
    if transfer_cost_bps > Decimal("0"):
        source_flags.append("transfer_cost_configured")
    time_decay_cost_bps = max(to_decimal(settings.smart_arbitrage_time_decay_bps_per_hour), Decimal("0")) * expected_hold_hours
    if time_decay_cost_bps > Decimal("0"):
        source_flags.append("time_decay_configured")

    funding_reference_ts = _funding_reference_ts(account_service=account_service)
    funding_schedule = _funding_schedule(
        account_service=account_service,
        hedge_symbol=hedge_symbol,
    )
    expected_funding_events, funding_event_projection_active, funding_schedule_source_flag = _expected_funding_events(
        settings=settings,
        expected_hold_hours=expected_hold_hours,
        reference_ts=funding_reference_ts,
        funding_schedule=funding_schedule,
    )
    if funding_schedule_source_flag is not None:
        source_flags.append(funding_schedule_source_flag)
    funding_cost_bps, funding_flags = _funding_cost_component(
        settings=settings,
        fee_resolver=funding_fee_resolver,
        hedge_symbol=hedge_symbol,
        expected_funding_events=expected_funding_events,
        funding_event_projection_active=funding_event_projection_active,
    )
    source_flags.extend(funding_flags)

    borrow_cost_bps, borrow_hour_windows, borrow_flags = _borrow_cost_component(
        settings=settings,
        execution_mode=execution_mode,
        expected_hold_hours=expected_hold_hours,
    )
    source_flags.extend(borrow_flags)

    fallback_total = max(to_decimal(settings.smart_arbitrage_estimated_cost_bps), Decimal("0"))
    estimate = drag_calculator.estimate(
        profile=TradeDragProfile(
            model_name="smart_arbitrage",
            cost_model_enabled=True,
            edge_reference_bps=basis_abs,
            expected_hold_hours=expected_hold_hours,
            expected_funding_events=expected_funding_events,
            borrow_hour_windows=borrow_hour_windows,
            ideal_open_fee_bps=ideal_open_fee_bps,
            ideal_close_fee_bps=ideal_close_fee_bps,
            executable_spread_bps=executable_spread_bps,
            executable_slippage_bps=executable_slippage_bps,
            execution_mismatch_bps=execution_mismatch_bps,
            funding_cost_bps=funding_cost_bps,
            borrow_cost_bps=borrow_cost_bps,
            transfer_cost_bps=transfer_cost_bps,
            time_decay_cost_bps=time_decay_cost_bps,
            legacy_total_cost_bps=fallback_total,
            cost_source_flags=source_flags,
        )
    )
    estimated_slippage_total = executable_spread_bps + executable_slippage_bps + execution_mismatch_bps
    estimated_inventory_total = transfer_cost_bps + time_decay_cost_bps

    return ArbitrageCostBreakdown(
        ideal_open_fee_bps=estimate.ideal_open_fee_bps,
        ideal_close_fee_bps=estimate.ideal_close_fee_bps,
        ideal_total_fee_bps=estimate.ideal_total_fee_bps,
        executable_spread_bps=estimate.executable_spread_bps,
        executable_slippage_bps=estimate.executable_slippage_bps,
        execution_mismatch_bps=estimate.execution_mismatch_bps,
        funding_cost_bps=estimate.funding_cost_bps,
        borrow_cost_bps=estimate.borrow_cost_bps,
        transfer_cost_bps=estimate.transfer_cost_bps,
        time_decay_cost_bps=estimate.time_decay_cost_bps,
        ideal_total_cost_bps=estimate.ideal_total_cost_bps,
        executable_total_drag_bps=estimate.executable_total_drag_bps,
        ideal_edge_bps=estimate.ideal_edge_bps,
        executable_edge_bps=estimate.executable_edge_bps,
        breakeven_basis_bps=estimate.breakeven_reference_bps,
        expected_hold_hours=estimate.expected_hold_hours,
        expected_funding_events=estimate.expected_funding_events,
        borrow_hour_windows=estimate.borrow_hour_windows,
        cost_confidence=estimate.cost_confidence,
        cost_source_flags=estimate.cost_source_flags,
        estimated_fee_bps=ideal_total_fee_bps,
        estimated_slippage_bps=estimated_slippage_total,
        estimated_funding_bps=estimate.funding_cost_bps,
        estimated_borrow_bps=estimate.borrow_cost_bps,
        estimated_inventory_cost_bps=estimated_inventory_total,
        estimated_total_cost_bps=estimate.executable_total_drag_bps,
        net_edge_bps=estimate.executable_edge_bps,
    )


def _fee_cost_components(
    *,
    settings: AATSSettings,
    trade_cost_service: TradeCostService,
    execution_mode: str | None,
    spot_symbol: str | None,
    hedge_symbol: str | None,
) -> tuple[Decimal, Decimal, list[str]]:
    spot_margin_mode = (
        settings.smart_arbitrage_margin_short_spot_margin_mode
        if execution_mode == "margin_reverse_carry"
        else "cash"
    )
    spot_fee_bps = trade_cost_service.estimated_execution_fee_bps_decimal(
        symbol=spot_symbol,
        product_type="spot",
        margin_mode=spot_margin_mode,
        execution_style="taker",
        order_type="market",
    )
    hedge_fee_bps = trade_cost_service.estimated_execution_fee_bps_decimal(
        symbol=hedge_symbol,
        product_type="derivatives",
        margin_mode=settings.margin_mode,
        execution_style="taker",
        order_type="market",
    )
    source_flag = (
        "fee_account_schedule"
        if settings.smart_arbitrage_fee_source_mode == "account_schedule"
        else "fee_trade_cost_defaults"
    )
    if spot_fee_bps > Decimal("0") or hedge_fee_bps > Decimal("0"):
        open_fee_bps = spot_fee_bps + hedge_fee_bps
        close_fee_bps = open_fee_bps
        return open_fee_bps, close_fee_bps, [source_flag]
    return Decimal("0"), Decimal("0"), ["fee_absent"]


def _spread_cost_component(
    *,
    settings: AATSSettings,
    trade_cost_service: TradeCostService,
    execution_mode: str | None,
    spot_symbol: str | None,
    hedge_symbol: str | None,
) -> tuple[Decimal, list[str]]:
    spot_margin_mode = (
        settings.smart_arbitrage_margin_short_spot_margin_mode
        if execution_mode == "margin_reverse_carry"
        else "cash"
    )
    spot_spread_bps = trade_cost_service.default_spread_bps(
        symbol=spot_symbol,
        product_type="spot",
        margin_mode=spot_margin_mode,
    )
    hedge_spread_bps = trade_cost_service.default_spread_bps(
        symbol=hedge_symbol,
        product_type="derivatives",
        margin_mode=settings.margin_mode,
    )
    total = spot_spread_bps + hedge_spread_bps
    if total > Decimal("0"):
        return total, ["spread_trade_cost_defaults"]
    return Decimal("0"), ["spread_absent"]


def _slippage_cost_component(
    *,
    settings: AATSSettings,
    trade_cost_service: TradeCostService,
    execution_mode: str | None,
    spot_symbol: str | None,
    hedge_symbol: str | None,
) -> tuple[Decimal, list[str]]:
    spot_margin_mode = (
        settings.smart_arbitrage_margin_short_spot_margin_mode
        if execution_mode == "margin_reverse_carry"
        else "cash"
    )
    spot_slippage_bps = trade_cost_service.default_slippage_bps(
        symbol=spot_symbol,
        product_type="spot",
        margin_mode=spot_margin_mode,
    )
    hedge_slippage_bps = trade_cost_service.default_slippage_bps(
        symbol=hedge_symbol,
        product_type="derivatives",
        margin_mode=settings.margin_mode,
    )
    total = spot_slippage_bps + hedge_slippage_bps
    if total > Decimal("0"):
        return total, ["slippage_trade_cost_defaults"]
    return Decimal("0"), ["slippage_absent"]


def _expected_funding_events(
    *,
    settings: AATSSettings,
    expected_hold_hours: Decimal,
    reference_ts: datetime | None,
    funding_schedule: dict[str, Any] | None,
) -> tuple[int, bool, str | None]:
    explicit_events = max(int(settings.smart_arbitrage_expected_funding_events or 0), 0)
    if explicit_events > 0:
        return explicit_events, False, "funding_events_explicit_override"
    normalized_reference_ts = _normalize_reference_ts(reference_ts or utc_now())
    if expected_hold_hours <= Decimal("0"):
        return 0, False, None
    if funding_schedule:
        next_funding_ts = funding_schedule.get("next_funding_time")
        interval_hours = funding_schedule.get("funding_interval_hours")
        updated_at = funding_schedule.get("updated_at")
        schedule_reference_ts = updated_at if isinstance(updated_at, datetime) else normalized_reference_ts
        if isinstance(next_funding_ts, datetime):
            count, projection_active = _count_funding_events(
                reference_ts=schedule_reference_ts,
                expected_hold_hours=expected_hold_hours,
                next_funding_ts=next_funding_ts,
                interval_hours=None if interval_hours in {None, ""} else to_decimal(interval_hours),
            )
            return count, projection_active, "funding_schedule_exchange_actual"
    interval_hours = max(to_decimal(settings.smart_arbitrage_funding_interval_hours), Decimal("0"))
    if interval_hours <= Decimal("0"):
        return 0, False, None
    next_funding_ts = _next_funding_timestamp(
        reference_ts=normalized_reference_ts,
        interval_hours=interval_hours,
    )
    count, projection_active = _count_funding_events(
        reference_ts=normalized_reference_ts,
        expected_hold_hours=expected_hold_hours,
        next_funding_ts=next_funding_ts,
        interval_hours=interval_hours,
    )
    return count, projection_active, "funding_schedule_projected_from_config"


def _funding_reference_ts(*, account_service: Any | None) -> datetime:
    getter = getattr(account_service, "latest_snapshot", None)
    if callable(getter):
        snapshot = getter()
        fetched_at = getattr(snapshot, "fetched_at", None)
        if isinstance(fetched_at, datetime):
            return _normalize_reference_ts(fetched_at)
    return utc_now()


def _normalize_reference_ts(timestamp: datetime) -> datetime:
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _decimal_hours_to_timedelta(hours: Decimal) -> timedelta:
    return timedelta(seconds=float(hours * Decimal("3600")))


def _funding_schedule(
    *,
    account_service: Any | None,
    hedge_symbol: str | None,
) -> dict[str, Any] | None:
    if hedge_symbol in {None, ""}:
        return None
    getter = getattr(account_service, "funding_schedule", None)
    if not callable(getter):
        return None
    try:
        schedule = getter(symbol=hedge_symbol)
    except TypeError:
        schedule = getter(hedge_symbol)
    if not isinstance(schedule, dict) or not schedule.get("available"):
        return None
    return schedule


def _next_funding_timestamp(
    *,
    reference_ts: datetime,
    interval_hours: Decimal,
) -> datetime | None:
    interval_window = _decimal_hours_to_timedelta(interval_hours)
    interval_seconds = Decimal(str(interval_window.total_seconds()))
    if interval_seconds <= Decimal("0"):
        return None
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    elapsed_seconds = Decimal(str((reference_ts - epoch).total_seconds()))
    intervals_elapsed = int((elapsed_seconds / interval_seconds).to_integral_value(rounding=ROUND_FLOOR))
    next_offset_seconds = interval_seconds * Decimal(intervals_elapsed + 1)
    next_ts = epoch + timedelta(seconds=float(next_offset_seconds))
    if next_ts <= reference_ts:
        return next_ts + interval_window
    return next_ts


def _count_funding_events(
    *,
    reference_ts: datetime,
    expected_hold_hours: Decimal,
    next_funding_ts: datetime | None,
    interval_hours: Decimal | None,
) -> tuple[int, bool]:
    if next_funding_ts is None:
        return 0, False
    normalized_reference_ts = _normalize_reference_ts(reference_ts)
    normalized_next_funding_ts = _normalize_reference_ts(next_funding_ts)
    hold_window = _decimal_hours_to_timedelta(expected_hold_hours)
    hold_end_ts = normalized_reference_ts + hold_window
    if hold_end_ts < normalized_next_funding_ts:
        return 0, True
    resolved_interval_hours = None
    if interval_hours is not None:
        resolved_interval_hours = max(to_decimal(interval_hours), Decimal("0"))
    if resolved_interval_hours is None or resolved_interval_hours <= Decimal("0"):
        return 1, True
    interval_window = _decimal_hours_to_timedelta(resolved_interval_hours)
    interval_seconds = Decimal(str(interval_window.total_seconds()))
    if interval_seconds <= Decimal("0"):
        return 1, True
    remaining_seconds = Decimal(str((hold_end_ts - normalized_next_funding_ts).total_seconds()))
    additional_events = 0
    if remaining_seconds > Decimal("0"):
        additional_events = int((remaining_seconds / interval_seconds).to_integral_value(rounding=ROUND_FLOOR))
    return 1 + additional_events, True


def _funding_cost_component(
    *,
    settings: AATSSettings,
    fee_resolver: EffectiveFeeResolver,
    hedge_symbol: str | None,
    expected_funding_events: int,
    funding_event_projection_active: bool,
) -> tuple[Decimal, list[str]]:
    if not settings.smart_arbitrage_funding_cost_enabled:
        return Decimal("0"), ["funding_disabled"]

    source_flags: list[str] = []
    if settings.smart_arbitrage_funding_source_mode == "account_proxy" and hedge_symbol:
        proxy_bps = max(fee_resolver.funding_fee_bps_decimal(symbol=hedge_symbol), Decimal("0"))
        if proxy_bps > Decimal("0"):
            if expected_funding_events > 0:
                source_flags.append("funding_account_proxy_per_event")
                return proxy_bps * Decimal(expected_funding_events), source_flags
            if funding_event_projection_active:
                source_flags.append("funding_outside_projected_hold_window")
                return Decimal("0"), source_flags
            source_flags.append("funding_account_proxy_total")
            return proxy_bps, source_flags

    configured_bps = max(to_decimal(settings.smart_arbitrage_estimated_funding_bps), Decimal("0"))
    if configured_bps > Decimal("0"):
        if expected_funding_events > 0:
            source_flags.append("funding_configured_per_event")
            return configured_bps * Decimal(expected_funding_events), source_flags
        if funding_event_projection_active:
            source_flags.append("funding_outside_projected_hold_window")
            return Decimal("0"), source_flags
        source_flags.append("funding_configured_total")
        return configured_bps, source_flags

    source_flags.append("funding_absent")
    return Decimal("0"), source_flags


def _borrow_cost_component(
    *,
    settings: AATSSettings,
    execution_mode: str | None,
    expected_hold_hours: Decimal,
) -> tuple[Decimal, int, list[str]]:
    if not settings.smart_arbitrage_borrow_cost_enabled or execution_mode != "margin_reverse_carry":
        return Decimal("0"), 0, ["borrow_disabled"]

    borrow_hour_windows = (
        0
        if expected_hold_hours <= Decimal("0")
        else max(1, int(expected_hold_hours.to_integral_value(rounding=ROUND_CEILING)))
    )
    source_flags: list[str] = []

    if settings.smart_arbitrage_borrow_source_mode == "apr_window_model":
        # Config uses percentage semantics: `18` means `18% APR`, not `18x`.
        apr_percent = max(to_decimal(settings.smart_arbitrage_estimated_borrow_apr), Decimal("0"))
        interest_free_ratio = min(max(to_decimal(settings.smart_arbitrage_borrow_interest_free_ratio), Decimal("0")), Decimal("1"))
        if apr_percent > Decimal("0") and borrow_hour_windows > 0:
            apr = apr_percent / Decimal("100")
            effective_ratio = max(Decimal("1") - interest_free_ratio, Decimal("0"))
            borrow_bps = effective_ratio * apr * Decimal(borrow_hour_windows) / Decimal("8760") * Decimal("10000")
            source_flags.append("borrow_apr_window_model")
            return borrow_bps, borrow_hour_windows, source_flags

    configured_bps = max(to_decimal(settings.smart_arbitrage_estimated_borrow_bps), Decimal("0"))
    if configured_bps > Decimal("0"):
        source_flags.append("borrow_configured_total")
        return configured_bps, borrow_hour_windows, source_flags

    source_flags.append("borrow_absent")
    return Decimal("0"), borrow_hour_windows, source_flags
