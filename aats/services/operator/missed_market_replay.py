"""Read-only missed-market replay diagnostics for operator investigations."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool


ZERO = Decimal("0")
DEFAULT_COST_BPS = Decimal("11.5")
DEFAULT_MOMENTUM_THRESHOLDS_BPS = (
    Decimal("10"),
    Decimal("20"),
    Decimal("30"),
    Decimal("40"),
    Decimal("50"),
    Decimal("75"),
)


@dataclass(frozen=True)
class MarketTick:
    ts: datetime
    price: Decimal


@dataclass(frozen=True)
class BaselineAssessmentRow:
    ts: datetime
    direction_bias: str
    trend_strength: Decimal | None = None
    composite_alpha: Decimal | None = None


@dataclass(frozen=True)
class DirectionalIntentRow:
    ts: datetime
    requested_target_qty: Decimal
    requested_delta_qty: Decimal
    target_notional: Decimal
    execution_behavior: str
    execution_control_mode: str
    expected_cost_bps: Decimal
    control_reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class CostGateCandidateRow:
    ts: datetime
    side: str
    candidate_target_qty: Decimal
    expected_cost_bps: Decimal
    signal_edge_bps: Decimal
    required_edge_bps: Decimal


@dataclass(frozen=True)
class TargetEvent:
    ts: datetime
    target_qty: Decimal
    cost_bps: Decimal = DEFAULT_COST_BPS
    note: str = ""


@dataclass(frozen=True)
class BucketSummary:
    bucket: datetime
    first_price: Decimal
    last_price: Decimal
    baseline_long: int = 0
    baseline_short: int = 0
    baseline_flat: int = 0
    intent_long: int = 0
    intent_short: int = 0
    intent_zero: int = 0
    suppressed: int = 0

    @property
    def price_delta(self) -> Decimal:
        return self.last_price - self.first_price


@dataclass(frozen=True)
class SimulationResult:
    label: str
    target_changes: int
    turnover: Decimal
    gross_pnl: Decimal
    estimated_cost: Decimal
    net_pnl: Decimal
    max_abs_qty: Decimal
    end_qty: Decimal


@dataclass(frozen=True)
class ReplayDataset:
    symbol: str
    start_ts: datetime
    end_ts: datetime
    market: tuple[MarketTick, ...]
    baselines: tuple[BaselineAssessmentRow, ...]
    directional_intents: tuple[DirectionalIntentRow, ...]
    cost_candidates: tuple[CostGateCandidateRow, ...]
    event_counts: Mapping[str, int]
    execution_counts: Mapping[str, int]


@dataclass(frozen=True)
class ReplayAnalysis:
    buckets: tuple[BucketSummary, ...]
    reason_counts: Mapping[str, int]
    directional_intent_counts: Mapping[str, int]
    median_target_notional: Decimal
    target_following: SimulationResult
    bucket_majority: SimulationResult
    cost_buffer_only_budget_on: SimulationResult
    cost_candidate_only: SimulationResult
    momentum_results: Mapping[str, SimulationResult]


def parse_replay_timestamp(value: str) -> datetime:
    """Parse a timezone-aware timestamp accepted by the operator CLI."""
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    if len(normalized) >= 3 and normalized[-3] in {"+", "-"} and normalized[-2:].isdigit():
        normalized = f"{normalized}:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone offset")
    return parsed


def analyze_dataset(
    dataset: ReplayDataset,
    *,
    bucket_minutes: int = 15,
    momentum_thresholds_bps: Sequence[Decimal] = DEFAULT_MOMENTUM_THRESHOLDS_BPS,
) -> ReplayAnalysis:
    buckets = build_bucket_summaries(
        market=dataset.market,
        baselines=dataset.baselines,
        intents=dataset.directional_intents,
        bucket_minutes=bucket_minutes,
    )
    median_notional = median_abs_target_notional(dataset.directional_intents)
    target_following = simulate_target_events(
        dataset.market,
        _target_following_events(dataset.directional_intents),
        label="target_following_no_budget_zero",
        flatten_at_end=False,
    )
    bucket_majority = simulate_target_events(
        dataset.market,
        bucket_majority_events(
            buckets=buckets,
            target_notional=median_notional,
            cost_bps=DEFAULT_COST_BPS,
        ),
        label="bucket_majority_no_budget_zero",
        flatten_at_end=True,
    )
    cost_candidate_only = simulate_target_events(
        dataset.market,
        cost_candidate_events(dataset.cost_candidates),
        label="cost_candidate_only",
        flatten_at_end=True,
    )
    momentum_results = {
        str(threshold): simulate_target_events(
            dataset.market,
            momentum_events(
                market=dataset.market,
                target_notional=median_notional,
                threshold_bps=threshold,
                cost_bps=DEFAULT_COST_BPS,
            ),
            label=f"momentum_{threshold}_bps",
            flatten_at_end=True,
        )
        for threshold in momentum_thresholds_bps
    }
    return ReplayAnalysis(
        buckets=buckets,
        reason_counts=count_budget_reasons(dataset.directional_intents),
        directional_intent_counts=count_directional_intents(dataset.directional_intents),
        median_target_notional=median_notional,
        target_following=target_following,
        bucket_majority=bucket_majority,
        cost_buffer_only_budget_on=SimulationResult(
            label="cost_buffer_only_budget_zero_still_on",
            target_changes=0,
            turnover=ZERO,
            gross_pnl=ZERO,
            estimated_cost=ZERO,
            net_pnl=ZERO,
            max_abs_qty=ZERO,
            end_qty=ZERO,
        ),
        cost_candidate_only=cost_candidate_only,
        momentum_results=momentum_results,
    )


def build_bucket_summaries(
    *,
    market: Sequence[MarketTick],
    baselines: Sequence[BaselineAssessmentRow],
    intents: Sequence[DirectionalIntentRow],
    bucket_minutes: int,
) -> tuple[BucketSummary, ...]:
    market_by_bucket: dict[datetime, list[MarketTick]] = {}
    for tick in market:
        market_by_bucket.setdefault(_floor_bucket(tick.ts, bucket_minutes), []).append(tick)

    baseline_counts: dict[datetime, Counter[str]] = {}
    for row in baselines:
        bucket = _floor_bucket(row.ts, bucket_minutes)
        baseline_counts.setdefault(bucket, Counter())[row.direction_bias or "flat"] += 1

    intent_counts: dict[datetime, Counter[str]] = {}
    for row in intents:
        bucket = _floor_bucket(row.ts, bucket_minutes)
        counter = intent_counts.setdefault(bucket, Counter())
        counter[_direction_from_qty(row.requested_delta_qty)] += 1
        if row.execution_behavior == "suppressed_after_approval":
            counter["suppressed"] += 1

    buckets = sorted(set(market_by_bucket) | set(baseline_counts) | set(intent_counts))
    summaries: list[BucketSummary] = []
    for bucket in buckets:
        ticks = market_by_bucket.get(bucket)
        if not ticks:
            continue
        baseline = baseline_counts.get(bucket, Counter())
        intent = intent_counts.get(bucket, Counter())
        summaries.append(
            BucketSummary(
                bucket=bucket,
                first_price=ticks[0].price,
                last_price=ticks[-1].price,
                baseline_long=baseline["long"],
                baseline_short=baseline["short"],
                baseline_flat=baseline["flat"],
                intent_long=intent["long"],
                intent_short=intent["short"],
                intent_zero=intent["zero"],
                suppressed=intent["suppressed"],
            )
        )
    return tuple(summaries)


def simulate_target_events(
    market: Sequence[MarketTick],
    events: Iterable[TargetEvent],
    *,
    label: str,
    flatten_at_end: bool,
) -> SimulationResult:
    ticks = tuple(sorted(market, key=lambda item: item.ts))
    if not ticks:
        return _empty_result(label)

    ordered_events = _dedupe_target_events(sorted(events, key=lambda item: item.ts))
    if flatten_at_end and ordered_events:
        ordered_events.append(
            TargetEvent(
                ts=ticks[-1].ts,
                target_qty=ZERO,
                cost_bps=ordered_events[-1].cost_bps,
                note="window_end_flatten",
            )
        )
        ordered_events = _dedupe_target_events(ordered_events)
    if not ordered_events:
        return _empty_result(label)

    gross = ZERO
    estimated_cost = ZERO
    turnover = ZERO
    position = ZERO
    max_abs_qty = ZERO
    changes = 0

    for index, event in enumerate(ordered_events):
        price = _price_at_or_before(ticks, event.ts)
        delta = event.target_qty - position
        if delta != ZERO:
            trade_notional = abs(delta) * price
            turnover += trade_notional
            estimated_cost += trade_notional * event.cost_bps / Decimal("10000")
            position = event.target_qty
            max_abs_qty = max(max_abs_qty, abs(position))
            changes += 1

        next_price = (
            _price_at_or_before(ticks, ordered_events[index + 1].ts)
            if index + 1 < len(ordered_events)
            else ticks[-1].price
        )
        gross += position * (next_price - price)

    return SimulationResult(
        label=label,
        target_changes=changes,
        turnover=turnover,
        gross_pnl=gross,
        estimated_cost=estimated_cost,
        net_pnl=gross - estimated_cost,
        max_abs_qty=max_abs_qty,
        end_qty=position,
    )


def bucket_majority_events(
    *,
    buckets: Sequence[BucketSummary],
    target_notional: Decimal,
    cost_bps: Decimal,
) -> tuple[TargetEvent, ...]:
    events: list[TargetEvent] = []
    for bucket in buckets:
        direction = _majority_direction(
            long_count=bucket.intent_long,
            short_count=bucket.intent_short,
            zero_count=bucket.intent_zero,
        )
        target_qty = ZERO
        if direction == "long":
            target_qty = target_notional / bucket.first_price
        elif direction == "short":
            target_qty = -(target_notional / bucket.first_price)
        events.append(
            TargetEvent(
                ts=bucket.bucket,
                target_qty=target_qty,
                cost_bps=cost_bps,
                note=f"bucket_majority_{direction}",
            )
        )
    return tuple(events)


def cost_candidate_events(candidates: Sequence[CostGateCandidateRow]) -> tuple[TargetEvent, ...]:
    events: list[TargetEvent] = []
    for candidate in candidates:
        target_qty = candidate.candidate_target_qty
        if candidate.side == "sell" and target_qty > ZERO:
            target_qty = -target_qty
        events.append(
            TargetEvent(
                ts=candidate.ts,
                target_qty=target_qty,
                cost_bps=candidate.expected_cost_bps,
                note="cost_gate_candidate",
            )
        )
    return tuple(events)


def momentum_events(
    *,
    market: Sequence[MarketTick],
    target_notional: Decimal,
    threshold_bps: Decimal,
    cost_bps: Decimal,
    lookback_seconds: int = 300,
    cooldown_seconds: int = 300,
) -> tuple[TargetEvent, ...]:
    ticks = tuple(sorted(market, key=lambda item: item.ts))
    events: list[TargetEvent] = []
    current_signal = "zero"
    last_emit_ts: datetime | None = None

    for tick in ticks:
        prior = _price_at_or_before_seconds(ticks, tick.ts, lookback_seconds)
        if prior is None or prior == ZERO:
            continue
        change_bps = (tick.price - prior) / prior * Decimal("10000")
        if change_bps >= threshold_bps:
            signal = "long"
        elif change_bps <= -threshold_bps:
            signal = "short"
        else:
            signal = "zero"
        if signal == current_signal:
            continue
        if last_emit_ts is not None and (tick.ts - last_emit_ts).total_seconds() < cooldown_seconds:
            continue
        target_qty = ZERO
        if signal == "long":
            target_qty = target_notional / tick.price
        elif signal == "short":
            target_qty = -(target_notional / tick.price)
        events.append(
            TargetEvent(
                ts=tick.ts,
                target_qty=target_qty,
                cost_bps=cost_bps,
                note=f"momentum_{change_bps:.4f}_bps",
            )
        )
        current_signal = signal
        last_emit_ts = tick.ts
    return tuple(events)


def count_directional_intents(intents: Sequence[DirectionalIntentRow]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in intents:
        counts[f"intent_{_direction_from_qty(row.requested_delta_qty)}"] += 1
        if row.requested_delta_qty != ZERO:
            counts["intent_nonzero"] += 1
        if row.execution_behavior == "suppressed_after_approval":
            counts["suppressed_after_approval"] += 1
    counts["total"] = len(intents)
    return dict(counts)


def count_budget_reasons(intents: Sequence[DirectionalIntentRow]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in intents:
        for reason in row.control_reason_codes:
            counts[reason] += 1
    return dict(counts)


def median_abs_target_notional(intents: Sequence[DirectionalIntentRow]) -> Decimal:
    values = sorted(
        abs(row.target_notional)
        for row in intents
        if row.target_notional != ZERO and row.requested_target_qty != ZERO
    )
    if not values:
        return ZERO
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) / Decimal("2")


def fetch_replay_dataset(
    *,
    database_url: str,
    symbol: str,
    start_ts: datetime,
    end_ts: datetime,
) -> ReplayDataset:
    engine = create_engine(database_url, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            event_counts = {
                row["event_type"]: int(row["n"])
                for row in conn.execute(
                    text(
                        """
                        select event_type, count(*) as n
                        from event_store
                        where event_timestamp >= :start_ts
                          and event_timestamp < :end_ts
                          and event_type in (
                            'MarketSnapshot',
                            'BaselineAssessment',
                            'StrategySleeveIntent',
                            'PositionTarget',
                            'DecisionOutcome'
                          )
                        group by event_type
                        """
                    ),
                    {"start_ts": start_ts, "end_ts": end_ts},
                ).mappings()
            }
            execution_counts = _fetch_execution_counts(conn, start_ts=start_ts, end_ts=end_ts)
            return ReplayDataset(
                symbol=symbol,
                start_ts=start_ts,
                end_ts=end_ts,
                market=_fetch_market(conn, symbol=symbol, start_ts=start_ts, end_ts=end_ts),
                baselines=_fetch_baselines(conn, symbol=symbol, start_ts=start_ts, end_ts=end_ts),
                directional_intents=_fetch_intents(
                    conn,
                    symbol=symbol,
                    start_ts=start_ts,
                    end_ts=end_ts,
                ),
                cost_candidates=_fetch_cost_candidates(
                    conn,
                    symbol=symbol,
                    start_ts=start_ts,
                    end_ts=end_ts,
                ),
                event_counts=event_counts,
                execution_counts=execution_counts,
            )
    finally:
        engine.dispose()


def build_markdown_report(dataset: ReplayDataset, analysis: ReplayAnalysis) -> str:
    lines: list[str] = [
        f"# Missed Market Replay - {dataset.symbol}",
        "",
        "## Window",
        "",
        f"- start: `{dataset.start_ts.isoformat()}`",
        f"- end: `{dataset.end_ts.isoformat()}`",
        f"- market snapshots: `{len(dataset.market)}`",
        f"- directional intents: `{len(dataset.directional_intents)}`",
        f"- cost candidates: `{len(dataset.cost_candidates)}`",
        f"- execution counts: `{dict(dataset.execution_counts)}`",
        "",
        "## Directional Intent Counts",
        "",
    ]
    for key, value in sorted(analysis.directional_intent_counts.items()):
        lines.append(f"- `{key}`: `{value}`")
    lines.extend(["", "## Top Budget Reasons", ""])
    for reason, value in sorted(analysis.reason_counts.items(), key=lambda item: (-item[1], item[0]))[:12]:
        lines.append(f"- `{reason}`: `{value}`")

    lines.extend(
        [
            "",
            "## Bucket Timeline",
            "",
            "| bucket | first | last | delta | baseline L/S/F | intent L/S/Z | suppressed |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for bucket in analysis.buckets:
        lines.append(
            "| "
            f"{bucket.bucket.isoformat()} | {_fmt(bucket.first_price)} | {_fmt(bucket.last_price)} | "
            f"{_fmt(bucket.price_delta)} | "
            f"{bucket.baseline_long}/{bucket.baseline_short}/{bucket.baseline_flat} | "
            f"{bucket.intent_long}/{bucket.intent_short}/{bucket.intent_zero} | "
            f"{bucket.suppressed} |"
        )

    lines.extend(
        [
            "",
            "## Counterfactuals",
            "",
            "| scenario | changes | turnover | gross PnL | cost | net PnL | max qty | end qty |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for result in [
        analysis.target_following,
        analysis.bucket_majority,
        analysis.cost_buffer_only_budget_on,
        analysis.cost_candidate_only,
    ]:
        lines.append(_simulation_markdown_row(result))
    for threshold, result in analysis.momentum_results.items():
        lines.append(_simulation_markdown_row(result, label=f"momentum_{threshold}_bps"))

    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "- `cost_buffer_only_budget_zero_still_on` is intentionally zero because relaxing cost "
            "does not create executable delta while budget zero suppression remains active.",
            "- `target_following_no_budget_zero` estimates the high-churn path and should be "
            "treated as a warning against simply disabling budget suppression.",
            "- `bucket_majority_no_budget_zero` estimates lower-churn episode-like behavior; "
            "it is the safer candidate for follow-up design.",
            "",
        ]
    )
    return "\n".join(lines)


def write_report(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def analysis_to_jsonable(dataset: ReplayDataset, analysis: ReplayAnalysis) -> dict[str, Any]:
    return {
        "symbol": dataset.symbol,
        "start_ts": dataset.start_ts.isoformat(),
        "end_ts": dataset.end_ts.isoformat(),
        "event_counts": dict(dataset.event_counts),
        "execution_counts": dict(dataset.execution_counts),
        "directional_intent_counts": dict(analysis.directional_intent_counts),
        "reason_counts": dict(analysis.reason_counts),
        "median_target_notional": _json_decimal(analysis.median_target_notional),
        "counterfactuals": {
            "target_following": _result_to_dict(analysis.target_following),
            "bucket_majority": _result_to_dict(analysis.bucket_majority),
            "cost_buffer_only_budget_on": _result_to_dict(analysis.cost_buffer_only_budget_on),
            "cost_candidate_only": _result_to_dict(analysis.cost_candidate_only),
            "momentum": {
                threshold: _result_to_dict(result)
                for threshold, result in analysis.momentum_results.items()
            },
        },
        "buckets": [
            {
                "bucket": bucket.bucket.isoformat(),
                "first_price": _json_decimal(bucket.first_price),
                "last_price": _json_decimal(bucket.last_price),
                "price_delta": _json_decimal(bucket.price_delta),
                "baseline_long": bucket.baseline_long,
                "baseline_short": bucket.baseline_short,
                "baseline_flat": bucket.baseline_flat,
                "intent_long": bucket.intent_long,
                "intent_short": bucket.intent_short,
                "intent_zero": bucket.intent_zero,
                "suppressed": bucket.suppressed,
            }
            for bucket in analysis.buckets
        ],
    }


def dumps_json_report(dataset: ReplayDataset, analysis: ReplayAnalysis) -> str:
    return json.dumps(
        analysis_to_jsonable(dataset, analysis),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


def _fetch_execution_counts(conn: Any, *, start_ts: datetime, end_ts: datetime) -> dict[str, int]:
    result: dict[str, int] = {}
    for table_name in ("execution_orders", "execution_commands", "execution_fills"):
        row = conn.execute(
            text(
                f"""
                select count(*) as n
                from {table_name}
                where created_at >= :start_ts
                  and created_at < :end_ts
                """
            ),
            {"start_ts": start_ts, "end_ts": end_ts},
        ).mappings().one()
        result[table_name] = int(row["n"])
    return result


def _fetch_market(
    conn: Any,
    *,
    symbol: str,
    start_ts: datetime,
    end_ts: datetime,
) -> tuple[MarketTick, ...]:
    rows = conn.execute(
        text(
            """
            select event_timestamp as ts,
                   nullif(payload->>'last_price', '')::numeric as price
            from event_store
            where event_type = 'MarketSnapshot'
              and symbol = :symbol
              and event_timestamp >= :start_ts
              and event_timestamp < :end_ts
              and nullif(payload->>'last_price', '') is not null
            order by event_timestamp
            """
        ),
        {"symbol": symbol, "start_ts": start_ts, "end_ts": end_ts},
    ).mappings()
    return tuple(MarketTick(ts=row["ts"], price=_decimal(row["price"])) for row in rows)


def _fetch_baselines(
    conn: Any,
    *,
    symbol: str,
    start_ts: datetime,
    end_ts: datetime,
) -> tuple[BaselineAssessmentRow, ...]:
    rows = conn.execute(
        text(
            """
            select event_timestamp as ts,
                   payload->>'direction_bias' as direction_bias,
                   nullif(payload->>'trend_strength', '')::numeric as trend_strength,
                   nullif(payload->>'composite_alpha_score', '')::numeric as composite_alpha
            from event_store
            where event_type = 'BaselineAssessment'
              and symbol = :symbol
              and event_timestamp >= :start_ts
              and event_timestamp < :end_ts
            order by event_timestamp
            """
        ),
        {"symbol": symbol, "start_ts": start_ts, "end_ts": end_ts},
    ).mappings()
    return tuple(
        BaselineAssessmentRow(
            ts=row["ts"],
            direction_bias=row["direction_bias"] or "flat",
            trend_strength=_optional_decimal(row["trend_strength"]),
            composite_alpha=_optional_decimal(row["composite_alpha"]),
        )
        for row in rows
    )


def _fetch_intents(
    conn: Any,
    *,
    symbol: str,
    start_ts: datetime,
    end_ts: datetime,
) -> tuple[DirectionalIntentRow, ...]:
    rows = conn.execute(
        text(
            """
            select event_timestamp as ts,
                   nullif(payload->>'requested_target_position_qty', '')::numeric as requested_target_qty,
                   nullif(payload->>'requested_delta_position_qty', '')::numeric as requested_delta_qty,
                   nullif(payload->>'target_notional', '')::numeric as target_notional,
                   payload->>'execution_behavior' as execution_behavior,
                   payload->>'execution_control_mode' as execution_control_mode,
                   nullif(payload->'metrics'->>'expected_cost_bps', '')::numeric as expected_cost_bps,
                   payload->'control_reason_codes' as control_reason_codes
            from event_store
            where event_type = 'StrategySleeveIntent'
              and symbol = :symbol
              and payload->>'family' = 'directional'
              and event_timestamp >= :start_ts
              and event_timestamp < :end_ts
            order by event_timestamp
            """
        ),
        {"symbol": symbol, "start_ts": start_ts, "end_ts": end_ts},
    ).mappings()
    return tuple(
        DirectionalIntentRow(
            ts=row["ts"],
            requested_target_qty=_optional_decimal(row["requested_target_qty"]) or ZERO,
            requested_delta_qty=_optional_decimal(row["requested_delta_qty"]) or ZERO,
            target_notional=_optional_decimal(row["target_notional"]) or ZERO,
            execution_behavior=row["execution_behavior"] or "",
            execution_control_mode=row["execution_control_mode"] or "",
            expected_cost_bps=_optional_decimal(row["expected_cost_bps"]) or DEFAULT_COST_BPS,
            control_reason_codes=_parse_reason_codes(row["control_reason_codes"]),
        )
        for row in rows
    )


def _fetch_cost_candidates(
    conn: Any,
    *,
    symbol: str,
    start_ts: datetime,
    end_ts: datetime,
) -> tuple[CostGateCandidateRow, ...]:
    rows = conn.execute(
        text(
            """
            with targets as (
              select event_timestamp,
                     json_array_elements(payload->'decision_outcome'->'decision_blocker_chain') as chain
              from event_store
              where event_type = 'PositionTarget'
                and symbol = :symbol
                and event_timestamp >= :start_ts
                and event_timestamp < :end_ts
            ),
            candidates as (
              select event_timestamp,
                     json_array_elements(chain->'candidates') as candidate
              from targets
              where chain->>'stage' = 'cost_gate'
                and (chain->'reasons')::text like '%expected_edge_below_cost_buffer%'
            )
            select event_timestamp as ts,
                   candidate->>'side' as side,
                   nullif(candidate->>'candidate_target_qty', '')::numeric as candidate_target_qty,
                   nullif(candidate->>'expected_cost_bps', '')::numeric as expected_cost_bps,
                   nullif(candidate->>'signal_edge_bps', '')::numeric as signal_edge_bps,
                   nullif(candidate->>'required_edge_bps', '')::numeric as required_edge_bps
            from candidates
            order by event_timestamp
            """
        ),
        {"symbol": symbol, "start_ts": start_ts, "end_ts": end_ts},
    ).mappings()
    return tuple(
        CostGateCandidateRow(
            ts=row["ts"],
            side=row["side"] or "",
            candidate_target_qty=_optional_decimal(row["candidate_target_qty"]) or ZERO,
            expected_cost_bps=_optional_decimal(row["expected_cost_bps"]) or DEFAULT_COST_BPS,
            signal_edge_bps=_optional_decimal(row["signal_edge_bps"]) or ZERO,
            required_edge_bps=_optional_decimal(row["required_edge_bps"]) or ZERO,
        )
        for row in rows
    )


def _target_following_events(intents: Sequence[DirectionalIntentRow]) -> tuple[TargetEvent, ...]:
    return tuple(
        TargetEvent(
            ts=row.ts,
            target_qty=row.requested_target_qty,
            cost_bps=row.expected_cost_bps,
            note=f"directional_{row.execution_behavior}",
        )
        for row in intents
    )


def _dedupe_target_events(events: Sequence[TargetEvent]) -> list[TargetEvent]:
    deduped: list[TargetEvent] = []
    last_target: Decimal | None = None
    for event in events:
        if last_target is None or event.target_qty != last_target:
            deduped.append(event)
            last_target = event.target_qty
    return deduped


def _price_at_or_before(ticks: Sequence[MarketTick], ts: datetime) -> Decimal:
    candidate = ticks[0]
    for tick in ticks:
        if tick.ts > ts:
            break
        candidate = tick
    return candidate.price


def _price_at_or_before_seconds(
    ticks: Sequence[MarketTick],
    ts: datetime,
    seconds: int,
) -> Decimal | None:
    target_ts = ts.timestamp() - seconds
    candidate: MarketTick | None = None
    for tick in ticks:
        if tick.ts.timestamp() > target_ts:
            break
        candidate = tick
    return None if candidate is None else candidate.price


def _floor_bucket(ts: datetime, bucket_minutes: int) -> datetime:
    minute = (ts.minute // bucket_minutes) * bucket_minutes
    return ts.replace(minute=minute, second=0, microsecond=0)


def _direction_from_qty(qty: Decimal) -> str:
    if qty > ZERO:
        return "long"
    if qty < ZERO:
        return "short"
    return "zero"


def _majority_direction(*, long_count: int, short_count: int, zero_count: int) -> str:
    if long_count > short_count and long_count > zero_count:
        return "long"
    if short_count > long_count and short_count > zero_count:
        return "short"
    return "zero"


def _parse_reason_codes(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        parsed = json.loads(value)
    else:
        parsed = value
    if not isinstance(parsed, list):
        return ()
    return tuple(str(item) for item in parsed)


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return _decimal(value)


def _decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _empty_result(label: str) -> SimulationResult:
    return SimulationResult(
        label=label,
        target_changes=0,
        turnover=ZERO,
        gross_pnl=ZERO,
        estimated_cost=ZERO,
        net_pnl=ZERO,
        max_abs_qty=ZERO,
        end_qty=ZERO,
    )


def _simulation_markdown_row(result: SimulationResult, *, label: str | None = None) -> str:
    return (
        f"| {label or result.label} | {result.target_changes} | "
        f"{_fmt(result.turnover)} | {_fmt(result.gross_pnl)} | "
        f"{_fmt(result.estimated_cost)} | {_fmt(result.net_pnl)} | "
        f"{_fmt(result.max_abs_qty)} | {_fmt(result.end_qty)} |"
    )


def _result_to_dict(result: SimulationResult) -> dict[str, Any]:
    return {
        "label": result.label,
        "target_changes": result.target_changes,
        "turnover": _json_decimal(result.turnover),
        "gross_pnl": _json_decimal(result.gross_pnl),
        "estimated_cost": _json_decimal(result.estimated_cost),
        "net_pnl": _json_decimal(result.net_pnl),
        "max_abs_qty": _json_decimal(result.max_abs_qty),
        "end_qty": _json_decimal(result.end_qty),
    }


def _fmt(value: Decimal) -> str:
    return f"{value:.6f}"


def _json_decimal(value: Decimal) -> str:
    return format(value, "f")

