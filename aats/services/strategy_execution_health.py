from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.execution import FillEvent
from aats.schemas.portfolio import PortfolioSnapshot
from aats.services.accounting import resolve_symbol_currencies, try_fill_fee_cost_in_quote
from aats.services.fill_ordering import fill_processing_sort_key
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, is_effectively_zero, to_decimal

_SMALL_PNL_CHURN_MULTIPLIER = Decimal("1.25")
_BPS_SCALE = Decimal("10000")


@dataclass(frozen=True, slots=True)
class ClosedTradeOutcome:
    timestamp: datetime
    fill_id: str
    net_realized_pnl: Decimal
    gross_realized_pnl: Decimal
    fee_cost_quote: Decimal
    close_notional: Decimal
    net_edge_bps: Decimal
    is_win: bool
    is_small_churn: bool
    is_low_edge: bool
    is_residual_exit: bool = False


@dataclass(slots=True)
class _LifecycleAccumulator:
    opened_at: datetime
    total_fee_quote: Decimal = Decimal("0")
    close_notional: Decimal = Decimal("0")
    net_realized_pnl: Decimal = Decimal("0")
    exit_fill_ids: list[str] = field(default_factory=list)
    guard_excluded_exit_count: int = 0

    def add_entry_fee(self, fee_cost_quote: Decimal) -> None:
        self.total_fee_quote += to_decimal(fee_cost_quote)

    def add_exit_fragment(
        self,
        *,
        fill_id: str,
        fee_cost_quote: Decimal,
        close_notional: Decimal,
        net_realized_pnl: Decimal,
        is_guard_excluded: bool,
    ) -> None:
        self.total_fee_quote += to_decimal(fee_cost_quote)
        self.close_notional += to_decimal(close_notional)
        self.net_realized_pnl += to_decimal(net_realized_pnl)
        self.exit_fill_ids.append(str(fill_id))
        if is_guard_excluded:
            self.guard_excluded_exit_count += 1

    def close_outcome(self, *, settings: AATSSettings, timestamp: datetime) -> ClosedTradeOutcome:
        fee_cost_quote = to_decimal(self.total_fee_quote)
        net_realized_pnl = to_decimal(self.net_realized_pnl)
        gross_realized_pnl = net_realized_pnl + fee_cost_quote
        close_notional = to_decimal(self.close_notional)
        net_edge_bps = (
            (net_realized_pnl / close_notional) * _BPS_SCALE
            if close_notional > EPSILON_DECIMAL_12
            else Decimal("0")
        )
        churn_cutoff = fee_cost_quote * _SMALL_PNL_CHURN_MULTIPLIER
        exit_fill_count = len(self.exit_fill_ids)
        return ClosedTradeOutcome(
            timestamp=timestamp,
            fill_id=self.exit_fill_ids[-1] if self.exit_fill_ids else "",
            net_realized_pnl=net_realized_pnl,
            gross_realized_pnl=gross_realized_pnl,
            fee_cost_quote=fee_cost_quote,
            close_notional=close_notional,
            net_edge_bps=net_edge_bps,
            is_win=net_realized_pnl > 0,
            is_small_churn=(
                abs(net_realized_pnl) <= churn_cutoff
                if churn_cutoff > 0
                else is_effectively_zero(net_realized_pnl)
            ),
            is_low_edge=net_edge_bps <= Decimal(str(settings.strategy_low_edge_threshold_bps)),
            is_residual_exit=exit_fill_count > 0 and self.guard_excluded_exit_count >= exit_fill_count,
        )


@dataclass(frozen=True, slots=True)
class StrategyExecutionHealthSnapshot:
    symbol: str
    current_position_opened_at: datetime | None
    last_position_closed_at: datetime | None
    latest_fill_timestamp: datetime | None
    recent_closed_trade_count: int
    recent_win_rate: float
    recent_fee_drag_ratio: float
    recent_churn_ratio: float
    recent_low_edge_trade_streak: int
    recent_low_edge_trade_at: datetime | None
    recent_gross_realized_pnl: Decimal
    recent_net_realized_pnl: Decimal
    recent_fee_total: Decimal
    recent_guard_eligible_net_realized_pnl: Decimal | None = None
    recent_guard_eligible_closed_trade_count: int | None = None
    recent_guard_eligible_win_rate: float | None = None
    recent_guard_eligible_fee_drag_ratio: float | None = None
    recent_guard_eligible_churn_ratio: float | None = None
    recent_guard_eligible_low_edge_trade_streak: int | None = None
    recent_guard_eligible_low_edge_trade_at: datetime | None = None

    def active_guardrails(
        self,
        *,
        settings: AATSSettings,
        as_of: datetime,
        current_position_qty: Decimal,
    ) -> dict[str, object]:
        flags: list[str] = []
        cooldowns: dict[str, float] = {}

        if (
            not is_effectively_zero(current_position_qty)
            and self.current_position_opened_at is not None
            and settings.strategy_min_hold_seconds > 0
        ):
            held_for = max((as_of - self.current_position_opened_at).total_seconds(), 0.0)
            remaining = max(settings.strategy_min_hold_seconds - held_for, 0.0)
            if remaining > 0:
                flags.append("min_hold_active")
                cooldowns["min_hold_remaining_seconds"] = remaining

        if self.last_position_closed_at is not None and settings.strategy_post_close_cooldown_seconds > 0:
            since_close = max((as_of - self.last_position_closed_at).total_seconds(), 0.0)
            remaining = max(settings.strategy_post_close_cooldown_seconds - since_close, 0.0)
            if remaining > 0:
                flags.append("post_close_cooldown_active")
                cooldowns["post_close_cooldown_remaining_seconds"] = remaining

        use_guard_eligible_low_edge = (
            (self.recent_guard_eligible_closed_trade_count or 0) > 0
            or self.recent_guard_eligible_low_edge_trade_at is not None
        )
        guard_eligible_low_edge_trade_streak = (
            self.recent_guard_eligible_low_edge_trade_streak
            if use_guard_eligible_low_edge
            else self.recent_low_edge_trade_streak
        )
        guard_eligible_low_edge_trade_at = (
            self.recent_guard_eligible_low_edge_trade_at
            if use_guard_eligible_low_edge
            else self.recent_low_edge_trade_at
        )
        if (
            guard_eligible_low_edge_trade_streak >= settings.strategy_low_edge_streak_limit > 0
            and guard_eligible_low_edge_trade_at is not None
            and settings.strategy_low_edge_cooldown_seconds > 0
        ):
            since_low_edge = max((as_of - guard_eligible_low_edge_trade_at).total_seconds(), 0.0)
            remaining = max(settings.strategy_low_edge_cooldown_seconds - since_low_edge, 0.0)
            if remaining > 0:
                flags.append("low_edge_cooldown_active")
                cooldowns["low_edge_cooldown_remaining_seconds"] = remaining

        guard_eligible_closed_trade_count = (
            self.recent_guard_eligible_closed_trade_count
            if (self.recent_guard_eligible_closed_trade_count or 0) > 0
            else self.recent_closed_trade_count
        )
        guard_eligible_churn_ratio = (
            self.recent_guard_eligible_churn_ratio
            if (self.recent_guard_eligible_closed_trade_count or 0) > 0
            else self.recent_churn_ratio
        )
        guard_eligible_fee_drag_ratio = (
            self.recent_guard_eligible_fee_drag_ratio
            if (self.recent_guard_eligible_closed_trade_count or 0) > 0
            else self.recent_fee_drag_ratio
        )
        if guard_eligible_closed_trade_count >= settings.strategy_performance_guard_min_closed_trades:
            if guard_eligible_fee_drag_ratio > settings.strategy_max_fee_drag_ratio:
                flags.append("fee_drag_elevated")
            if guard_eligible_churn_ratio > settings.strategy_max_churn_ratio:
                flags.append("churn_elevated")

        return {
            "flags": flags,
            "cooldowns": cooldowns,
        }

    def as_payload(
        self,
        *,
        settings: AATSSettings,
        as_of: datetime,
        current_position_qty: Decimal,
    ) -> dict[str, object]:
        guardrails = self.active_guardrails(
            settings=settings,
            as_of=as_of,
            current_position_qty=current_position_qty,
        )
        return {
            "symbol": self.symbol,
            "current_position_opened_at": self.current_position_opened_at,
            "last_position_closed_at": self.last_position_closed_at,
            "latest_fill_timestamp": self.latest_fill_timestamp,
            "recent_closed_trade_count": self.recent_closed_trade_count,
            "recent_win_rate": self.recent_win_rate,
            "recent_fee_drag_ratio": self.recent_fee_drag_ratio,
            "recent_churn_ratio": self.recent_churn_ratio,
            "recent_low_edge_trade_streak": self.recent_low_edge_trade_streak,
            "recent_low_edge_trade_at": self.recent_low_edge_trade_at,
            "recent_gross_realized_pnl": float(self.recent_gross_realized_pnl),
            "recent_net_realized_pnl": float(self.recent_net_realized_pnl),
            "recent_fee_total": float(self.recent_fee_total),
            "recent_guard_eligible_net_realized_pnl": (
                None
                if self.recent_guard_eligible_net_realized_pnl is None
                else float(self.recent_guard_eligible_net_realized_pnl)
            ),
            "recent_guard_eligible_closed_trade_count": self.recent_guard_eligible_closed_trade_count,
            "recent_guard_eligible_win_rate": self.recent_guard_eligible_win_rate,
            "recent_guard_eligible_fee_drag_ratio": self.recent_guard_eligible_fee_drag_ratio,
            "recent_guard_eligible_churn_ratio": self.recent_guard_eligible_churn_ratio,
            "recent_guard_eligible_low_edge_trade_streak": self.recent_guard_eligible_low_edge_trade_streak,
            "recent_guard_eligible_low_edge_trade_at": self.recent_guard_eligible_low_edge_trade_at,
            "guardrail_flags": guardrails["flags"],
            "cooldowns": guardrails["cooldowns"],
        }


def _fee_to_gross_ratio(*, fee_total: Decimal, gross_realized: Decimal) -> float:
    if abs(gross_realized) > EPSILON_DECIMAL_12:
        return float(fee_total / abs(gross_realized))
    return 1.0 if fee_total > 0 else 0.0


def _fill_fee_quote(fill: FillEvent) -> Decimal:
    base_currency, quote_currency = resolve_symbol_currencies(fill.symbol)
    fee_cost_quote, _fee_error = try_fill_fee_cost_in_quote(
        fill=fill,
        base_currency=base_currency,
        quote_currency=quote_currency,
    )
    return to_decimal(fee_cost_quote or Decimal("0"))


def compute_strategy_execution_health(
    *,
    settings: AATSSettings,
    symbol: str,
    fills: list[FillEvent],
    snapshots: list[PortfolioSnapshot],
    current_position_qty: Decimal,
    current_long_position_qty: Decimal | None = None,
    current_short_position_qty: Decimal | None = None,
    guard_excluded_fill_ids: set[str] | None = None,
    as_of: datetime | None = None,
) -> StrategyExecutionHealthSnapshot:
    ordered_fills = sorted(
        [fill for fill in fills if fill.symbol == symbol],
        key=fill_processing_sort_key,
    )
    ordered_snapshots = sorted(snapshots, key=lambda item: item.snapshot_ts)
    realized_delta_by_fill_id = _realized_delta_by_fill_id(ordered_snapshots)
    resolved_current_long_qty = to_decimal(current_long_position_qty or Decimal("0"))
    resolved_current_short_qty = to_decimal(current_short_position_qty or Decimal("0"))
    excluded_fill_ids = set(guard_excluded_fill_ids or ())

    if _symbol_health_should_use_leg_lifecycle(
        fills=ordered_fills,
        current_long_position_qty=resolved_current_long_qty,
        current_short_position_qty=resolved_current_short_qty,
    ):
        return _compute_hedge_mode_strategy_execution_health(
            settings=settings,
            symbol=symbol,
            fills=ordered_fills,
            realized_delta_by_fill_id=realized_delta_by_fill_id,
            current_long_position_qty=resolved_current_long_qty,
            current_short_position_qty=resolved_current_short_qty,
            guard_excluded_fill_ids=excluded_fill_ids,
            as_of=as_of,
        )

    current_position_opened_at, last_position_closed_at, outcomes = _walk_symbol_fills(
        settings=settings,
        fills=ordered_fills,
        realized_delta_by_fill_id=realized_delta_by_fill_id,
        current_position_qty=current_position_qty,
        guard_excluded_fill_ids=excluded_fill_ids,
    )
    return _strategy_health_snapshot_from_outcomes(
        settings=settings,
        symbol=symbol,
        current_position_opened_at=current_position_opened_at,
        last_position_closed_at=last_position_closed_at,
        latest_fill_timestamp=ordered_fills[-1].ingestion_timestamp if ordered_fills else None,
        as_of=as_of,
        outcomes=outcomes,
    )


def _symbol_health_should_use_leg_lifecycle(
    *,
    fills: list[FillEvent],
    current_long_position_qty: Decimal,
    current_short_position_qty: Decimal,
) -> bool:
    if current_long_position_qty > EPSILON_DECIMAL_12 or current_short_position_qty > EPSILON_DECIMAL_12:
        return True
    return any(_fill_leg(fill) in {"long", "short"} for fill in fills)


def _compute_hedge_mode_strategy_execution_health(
    *,
    settings: AATSSettings,
    symbol: str,
    fills: list[FillEvent],
    realized_delta_by_fill_id: dict[str, Decimal],
    current_long_position_qty: Decimal,
    current_short_position_qty: Decimal,
    guard_excluded_fill_ids: set[str],
    as_of: datetime | None,
) -> StrategyExecutionHealthSnapshot:
    long_opened_at, long_last_closed_at, long_outcomes = _walk_leg_fills(
        settings=settings,
        fills=fills,
        realized_delta_by_fill_id=realized_delta_by_fill_id,
        current_position_qty=current_long_position_qty,
        leg="long",
        guard_excluded_fill_ids=guard_excluded_fill_ids,
    )
    short_opened_at, short_last_closed_at, short_outcomes = _walk_leg_fills(
        settings=settings,
        fills=fills,
        realized_delta_by_fill_id=realized_delta_by_fill_id,
        current_position_qty=current_short_position_qty,
        leg="short",
        guard_excluded_fill_ids=guard_excluded_fill_ids,
    )
    open_anchors = [
        anchor
        for current_qty, anchor in (
            (current_long_position_qty, long_opened_at),
            (current_short_position_qty, short_opened_at),
        )
        if current_qty > EPSILON_DECIMAL_12 and anchor is not None
    ]
    current_position_opened_at = min(open_anchors) if open_anchors else None
    if current_long_position_qty > EPSILON_DECIMAL_12 or current_short_position_qty > EPSILON_DECIMAL_12:
        last_position_closed_at = None
    else:
        close_anchors = [anchor for anchor in (long_last_closed_at, short_last_closed_at) if anchor is not None]
        last_position_closed_at = max(close_anchors) if close_anchors else None
    latest_fill_timestamp = next(
        (
            fill.ingestion_timestamp
            for fill in reversed(fills)
            if _fill_leg(fill) in {"long", "short"}
        ),
        None,
    )
    outcomes = sorted(
        [*long_outcomes, *short_outcomes],
        key=lambda item: (item.timestamp, item.fill_id),
    )
    return _strategy_health_snapshot_from_outcomes(
        settings=settings,
        symbol=symbol,
        current_position_opened_at=current_position_opened_at,
        last_position_closed_at=last_position_closed_at,
        latest_fill_timestamp=latest_fill_timestamp,
        as_of=as_of,
        outcomes=outcomes,
    )


def compute_leg_strategy_execution_health(
    *,
    settings: AATSSettings,
    symbol: str,
    fills: list[FillEvent],
    snapshots: list[PortfolioSnapshot],
    current_long_position_qty: Decimal,
    current_short_position_qty: Decimal,
    guard_excluded_fill_ids: set[str] | None = None,
    as_of: datetime | None = None,
) -> dict[str, StrategyExecutionHealthSnapshot]:
    ordered_fills = sorted(
        [fill for fill in fills if fill.symbol == symbol],
        key=fill_processing_sort_key,
    )
    ordered_snapshots = sorted(snapshots, key=lambda item: item.snapshot_ts)
    realized_delta_by_fill_id = _realized_delta_by_fill_id(ordered_snapshots)
    excluded_fill_ids = set(guard_excluded_fill_ids or ())
    return {
        "long": _compute_leg_strategy_execution_health_snapshot(
            settings=settings,
            symbol=symbol,
            fills=ordered_fills,
            realized_delta_by_fill_id=realized_delta_by_fill_id,
            current_position_qty=current_long_position_qty,
            leg="long",
            guard_excluded_fill_ids=excluded_fill_ids,
            as_of=as_of,
        ),
        "short": _compute_leg_strategy_execution_health_snapshot(
            settings=settings,
            symbol=symbol,
            fills=ordered_fills,
            realized_delta_by_fill_id=realized_delta_by_fill_id,
            current_position_qty=current_short_position_qty,
            leg="short",
            guard_excluded_fill_ids=excluded_fill_ids,
            as_of=as_of,
        ),
    }


def _compute_leg_strategy_execution_health_snapshot(
    *,
    settings: AATSSettings,
    symbol: str,
    fills: list[FillEvent],
    realized_delta_by_fill_id: dict[str, Decimal],
    current_position_qty: Decimal,
    leg: str,
    guard_excluded_fill_ids: set[str],
    as_of: datetime | None,
) -> StrategyExecutionHealthSnapshot:
    current_position_opened_at, last_position_closed_at, outcomes = _walk_leg_fills(
        settings=settings,
        fills=fills,
        realized_delta_by_fill_id=realized_delta_by_fill_id,
        current_position_qty=current_position_qty,
        leg=leg,
        guard_excluded_fill_ids=guard_excluded_fill_ids,
    )
    return _strategy_health_snapshot_from_outcomes(
        settings=settings,
        symbol=symbol,
        current_position_opened_at=current_position_opened_at,
        last_position_closed_at=last_position_closed_at,
        latest_fill_timestamp=next(
            (
                fill.ingestion_timestamp
                for fill in reversed(fills)
                if _fill_leg(fill) == leg
            ),
            None,
        ),
        as_of=as_of,
        outcomes=outcomes,
    )


def _walk_symbol_fills(
    *,
    settings: AATSSettings,
    fills: list[FillEvent],
    realized_delta_by_fill_id: dict[str, Decimal],
    current_position_qty: Decimal,
    guard_excluded_fill_ids: set[str],
) -> tuple[datetime | None, datetime | None, list[ClosedTradeOutcome]]:
    position_qty = Decimal("0")
    current_position_opened_at: datetime | None = None
    last_position_closed_at: datetime | None = None
    latest_explicit_close_at: datetime | None = None
    outcomes: list[ClosedTradeOutcome] = []
    active_lifecycle: _LifecycleAccumulator | None = None

    for fill in fills:
        if _is_explicit_close_fill(fill):
            latest_explicit_close_at = fill.ingestion_timestamp
        fill_qty = to_decimal(fill.fill_qty)
        signed_qty = fill_qty if fill.side == "buy" else -fill_qty
        previous_qty = position_qty
        close_qty = Decimal("0")
        open_qty = Decimal("0")
        if is_effectively_zero(previous_qty):
            open_qty = abs(signed_qty)
        elif previous_qty * signed_qty > 0:
            open_qty = abs(signed_qty)
        elif previous_qty * signed_qty < 0:
            close_qty = min(abs(previous_qty), abs(signed_qty))
            if abs(signed_qty) > abs(previous_qty):
                open_qty = abs(signed_qty) - abs(previous_qty)
        position_qty = previous_qty + signed_qty
        opening_continues_lifecycle = open_qty > EPSILON_DECIMAL_12 and previous_qty * signed_qty > 0
        opening_new_lifecycle = open_qty > EPSILON_DECIMAL_12 and (
            is_effectively_zero(previous_qty) or previous_qty * signed_qty < 0
        )
        fee_cost_quote = _fill_fee_quote(fill)
        close_fee_quote = Decimal("0")
        if fill_qty > EPSILON_DECIMAL_12 and close_qty > EPSILON_DECIMAL_12:
            close_fee_quote = fee_cost_quote * (close_qty / fill_qty)
        open_fee_quote = fee_cost_quote - close_fee_quote
        if opening_continues_lifecycle:
            if active_lifecycle is None:
                active_lifecycle = _LifecycleAccumulator(opened_at=fill.ingestion_timestamp)
            active_lifecycle.add_entry_fee(open_fee_quote)
        if close_qty > EPSILON_DECIMAL_12:
            if active_lifecycle is None:
                active_lifecycle = _LifecycleAccumulator(opened_at=fill.ingestion_timestamp)
            active_lifecycle.add_exit_fragment(
                fill_id=fill.fill_id,
                fee_cost_quote=close_fee_quote,
                close_notional=close_qty * to_decimal(fill.fill_price),
                net_realized_pnl=to_decimal(realized_delta_by_fill_id.get(fill.fill_id, Decimal("0"))),
                is_guard_excluded=str(fill.fill_id or "").strip() in guard_excluded_fill_ids,
            )
        lifecycle_closed = (
            not is_effectively_zero(previous_qty)
            and (
                is_effectively_zero(position_qty)
                or previous_qty * position_qty < 0
            )
        )
        if lifecycle_closed and active_lifecycle is not None:
            outcomes.append(
                active_lifecycle.close_outcome(
                    settings=settings,
                    timestamp=fill.ingestion_timestamp,
                )
            )
            last_position_closed_at = fill.ingestion_timestamp
            active_lifecycle = None
            current_position_opened_at = None
        if opening_new_lifecycle and not is_effectively_zero(position_qty):
            active_lifecycle = _LifecycleAccumulator(opened_at=fill.ingestion_timestamp)
            active_lifecycle.add_entry_fee(open_fee_quote)
            current_position_opened_at = fill.ingestion_timestamp

    if is_effectively_zero(current_position_qty):
        current_position_opened_at = None
        if latest_explicit_close_at is not None:
            last_position_closed_at = max(
                item
                for item in (last_position_closed_at, latest_explicit_close_at)
                if item is not None
            )
    elif is_effectively_zero(position_qty) or (position_qty > 0) != (current_position_qty > 0):
        current_position_opened_at = None
    elif active_lifecycle is not None:
        current_position_opened_at = active_lifecycle.opened_at
    return current_position_opened_at, last_position_closed_at, outcomes


def _walk_leg_fills(
    *,
    settings: AATSSettings,
    fills: list[FillEvent],
    realized_delta_by_fill_id: dict[str, Decimal],
    current_position_qty: Decimal,
    leg: str,
    guard_excluded_fill_ids: set[str],
) -> tuple[datetime | None, datetime | None, list[ClosedTradeOutcome]]:
    position_qty = Decimal("0")
    current_position_opened_at: datetime | None = None
    last_position_closed_at: datetime | None = None
    latest_explicit_close_at: datetime | None = None
    outcomes: list[ClosedTradeOutcome] = []
    active_lifecycle: _LifecycleAccumulator | None = None

    for fill in fills:
        if _fill_leg(fill) != leg:
            continue
        if _is_explicit_close_fill(fill):
            latest_explicit_close_at = fill.ingestion_timestamp
        signed_qty = _leg_signed_fill_qty(fill=fill, leg=leg)
        previous_qty = position_qty
        position_qty = max(previous_qty + signed_qty, Decimal("0"))
        close_qty = Decimal("0")
        open_qty = Decimal("0")
        if signed_qty > EPSILON_DECIMAL_12:
            open_qty = signed_qty
        if previous_qty > EPSILON_DECIMAL_12 and signed_qty < -EPSILON_DECIMAL_12:
            close_qty = min(previous_qty, abs(signed_qty))
        fee_cost_quote = _fill_fee_quote(fill)
        fill_qty = to_decimal(fill.fill_qty)
        close_fee_quote = Decimal("0")
        if fill_qty > EPSILON_DECIMAL_12 and close_qty > EPSILON_DECIMAL_12:
            close_fee_quote = fee_cost_quote * (close_qty / fill_qty)
        open_fee_quote = fee_cost_quote - close_fee_quote

        if open_qty > EPSILON_DECIMAL_12:
            if active_lifecycle is None:
                active_lifecycle = _LifecycleAccumulator(opened_at=fill.ingestion_timestamp)
            active_lifecycle.add_entry_fee(open_fee_quote)
            if previous_qty <= EPSILON_DECIMAL_12:
                current_position_opened_at = fill.ingestion_timestamp

        if close_qty > EPSILON_DECIMAL_12:
            if active_lifecycle is None:
                active_lifecycle = _LifecycleAccumulator(opened_at=fill.ingestion_timestamp)
            active_lifecycle.add_exit_fragment(
                fill_id=fill.fill_id,
                fee_cost_quote=close_fee_quote,
                close_notional=close_qty * to_decimal(fill.fill_price),
                net_realized_pnl=to_decimal(realized_delta_by_fill_id.get(fill.fill_id, Decimal("0"))),
                is_guard_excluded=str(fill.fill_id or "").strip() in guard_excluded_fill_ids,
            )

        if previous_qty > EPSILON_DECIMAL_12 and position_qty <= EPSILON_DECIMAL_12 and active_lifecycle is not None:
            outcomes.append(
                active_lifecycle.close_outcome(
                    settings=settings,
                    timestamp=fill.ingestion_timestamp,
                )
            )
            last_position_closed_at = fill.ingestion_timestamp
            current_position_opened_at = None
            active_lifecycle = None

    if is_effectively_zero(current_position_qty):
        current_position_opened_at = None
        if latest_explicit_close_at is not None:
            last_position_closed_at = max(
                item
                for item in (last_position_closed_at, latest_explicit_close_at)
                if item is not None
            )
    elif is_effectively_zero(position_qty) or abs(position_qty - current_position_qty) > EPSILON_DECIMAL_12:
        current_position_opened_at = None
    elif active_lifecycle is not None:
        current_position_opened_at = active_lifecycle.opened_at
    return current_position_opened_at, last_position_closed_at, outcomes


def _strategy_health_snapshot_from_outcomes(
    *,
    settings: AATSSettings,
    symbol: str,
    current_position_opened_at: datetime | None,
    last_position_closed_at: datetime | None,
    latest_fill_timestamp: datetime | None,
    as_of: datetime | None = None,
    outcomes: list[ClosedTradeOutcome],
) -> StrategyExecutionHealthSnapshot:
    lookback = max(settings.strategy_health_lookback_trades, 1)
    recent_outcomes = list(outcomes)
    reference_timestamp = as_of or latest_fill_timestamp or utc_now()
    if (
        settings.strategy_health_lookback_window_seconds > 0
    ):
        cutoff = reference_timestamp - timedelta(seconds=settings.strategy_health_lookback_window_seconds)
        recent_outcomes = [item for item in recent_outcomes if item.timestamp >= cutoff]
    recent_outcomes = recent_outcomes[-lookback:]
    guard_eligible_outcomes = [item for item in recent_outcomes if not item.is_residual_exit]
    recent_fee_total = sum((item.fee_cost_quote for item in recent_outcomes), Decimal("0"))
    recent_net_realized = sum((item.net_realized_pnl for item in recent_outcomes), Decimal("0"))
    recent_gross_realized = sum((item.gross_realized_pnl for item in recent_outcomes), Decimal("0"))
    guard_eligible_fee_total = sum((item.fee_cost_quote for item in guard_eligible_outcomes), Decimal("0"))
    guard_eligible_net_realized = sum((item.net_realized_pnl for item in guard_eligible_outcomes), Decimal("0"))
    guard_eligible_gross_realized = sum((item.gross_realized_pnl for item in guard_eligible_outcomes), Decimal("0"))
    recent_win_rate = (
        float(sum(1 for item in recent_outcomes if item.is_win) / len(recent_outcomes))
        if recent_outcomes
        else 0.0
    )
    fee_drag_ratio = _fee_to_gross_ratio(
        fee_total=recent_fee_total,
        gross_realized=recent_gross_realized,
    )
    guard_eligible_fee_drag_ratio = _fee_to_gross_ratio(
        fee_total=guard_eligible_fee_total,
        gross_realized=guard_eligible_gross_realized,
    )
    churn_ratio = (
        float(sum(1 for item in recent_outcomes if item.is_small_churn) / len(recent_outcomes))
        if recent_outcomes
        else 0.0
    )
    guard_eligible_win_rate = (
        float(sum(1 for item in guard_eligible_outcomes if item.is_win) / len(guard_eligible_outcomes))
        if guard_eligible_outcomes
        else 0.0
    )
    guard_eligible_churn_ratio = (
        float(sum(1 for item in guard_eligible_outcomes if item.is_small_churn) / len(guard_eligible_outcomes))
        if guard_eligible_outcomes
        else 0.0
    )
    low_edge_streak = 0
    recent_low_edge_trade_at = None
    for item in reversed(recent_outcomes):
        if not item.is_low_edge:
            break
        low_edge_streak += 1
        recent_low_edge_trade_at = item.timestamp
    guard_eligible_low_edge_streak = 0
    recent_guard_eligible_low_edge_trade_at = None
    for item in reversed(guard_eligible_outcomes):
        if not item.is_low_edge:
            break
        guard_eligible_low_edge_streak += 1
        recent_guard_eligible_low_edge_trade_at = item.timestamp

    return StrategyExecutionHealthSnapshot(
        symbol=symbol,
        current_position_opened_at=current_position_opened_at,
        last_position_closed_at=last_position_closed_at,
        latest_fill_timestamp=latest_fill_timestamp,
        recent_closed_trade_count=len(recent_outcomes),
        recent_win_rate=recent_win_rate,
        recent_fee_drag_ratio=fee_drag_ratio,
        recent_churn_ratio=churn_ratio,
        recent_low_edge_trade_streak=low_edge_streak,
        recent_low_edge_trade_at=recent_low_edge_trade_at,
        recent_gross_realized_pnl=recent_gross_realized,
        recent_net_realized_pnl=recent_net_realized,
        recent_fee_total=recent_fee_total,
        recent_guard_eligible_net_realized_pnl=guard_eligible_net_realized,
        recent_guard_eligible_closed_trade_count=len(guard_eligible_outcomes),
        recent_guard_eligible_win_rate=guard_eligible_win_rate,
        recent_guard_eligible_fee_drag_ratio=guard_eligible_fee_drag_ratio,
        recent_guard_eligible_churn_ratio=guard_eligible_churn_ratio,
        recent_guard_eligible_low_edge_trade_streak=guard_eligible_low_edge_streak,
        recent_guard_eligible_low_edge_trade_at=recent_guard_eligible_low_edge_trade_at,
    )


def _fill_leg(fill: FillEvent) -> str | None:
    if fill.pos_side in {"long", "short"}:
        return str(fill.pos_side)
    normalized_intent = str(fill.position_intent or "").strip().lower()
    if normalized_intent.endswith("_long"):
        return "long"
    if normalized_intent.endswith("_short"):
        return "short"
    return None


def _is_explicit_close_fill(fill: FillEvent) -> bool:
    normalized_intent = str(fill.position_intent or "").strip().lower()
    return bool(
        fill.close_only
        or normalized_intent in {"close_long", "close_short"}
        or fill.execution_action == "close"
        or fill.leg_action == "close"
    )


def _leg_signed_fill_qty(*, fill: FillEvent, leg: str) -> Decimal:
    fill_qty = abs(to_decimal(fill.fill_qty))
    normalized_leg = str(leg)
    if normalized_leg == "long":
        return fill_qty if fill.side == "buy" else -fill_qty
    return fill_qty if fill.side == "sell" else -fill_qty


def _realized_delta_by_fill_id(snapshots: list[PortfolioSnapshot]) -> dict[str, Decimal]:
    rows: dict[str, Decimal] = {}
    previous_realized = None
    for snapshot in snapshots:
        delta = Decimal("0")
        if previous_realized is not None:
            delta = to_decimal(snapshot.realized_pnl) - previous_realized
        previous_realized = to_decimal(snapshot.realized_pnl)
        if snapshot.source_fill_id:
            rows[snapshot.source_fill_id] = delta
    return rows
