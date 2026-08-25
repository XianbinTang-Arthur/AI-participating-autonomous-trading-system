"""Causal Top-5 orderbook and trade-event execution replay.

The model never extrapolates liquidity beyond observed depth.  Passive orders
only receive fills from later public trade events after a conservative visible
queue-ahead estimate has been consumed.  It is research-only and cannot submit
orders or mutate runtime state.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, Mapping, Sequence


L2_EVENT_REPLAY_MODEL_VERSION = "l2_event_replay_v1"
Side = Literal["buy", "sell"]
OrderKind = Literal["market", "ioc_limit", "post_only"]
ReplayStatus = Literal["filled", "partial_fill", "no_fill", "rejected"]


def _decimal(value: Decimal | int | str, *, field_name: str, positive: bool = True) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field_name}_must_be_decimal_compatible")
    result = value if isinstance(value, Decimal) else Decimal(str(value))
    if not result.is_finite() or (positive and result <= 0):
        raise ValueError(f"{field_name}_must_be_finite_positive")
    return result


def _aware_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name}_must_be_timezone_aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class OrderBookLevel:
    price: Decimal
    quantity: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "price", _decimal(self.price, field_name="level_price"))
        object.__setattr__(
            self,
            "quantity",
            _decimal(self.quantity, field_name="level_quantity"),
        )


@dataclass(frozen=True, slots=True)
class L2OrderBookSnapshot:
    symbol: str
    ts: datetime
    collector_sequence: int
    bids: tuple[OrderBookLevel, ...]
    asks: tuple[OrderBookLevel, ...]
    payload_hash: str

    def __post_init__(self) -> None:
        symbol = self.symbol.strip().upper()
        if not symbol:
            raise ValueError("snapshot_symbol_required")
        if self.collector_sequence <= 0:
            raise ValueError("collector_sequence_must_be_positive")
        if not self.payload_hash.startswith("sha256:") or len(self.payload_hash) != 71:
            raise ValueError("snapshot_payload_hash_invalid")
        bids = tuple(self.bids)
        asks = tuple(self.asks)
        if not bids or not asks or len(bids) > 5 or len(asks) > 5:
            raise ValueError("snapshot_requires_one_to_five_levels_per_side")
        if any(left.price <= right.price for left, right in zip(bids, bids[1:])):
            raise ValueError("snapshot_bids_must_be_strictly_descending")
        if any(left.price >= right.price for left, right in zip(asks, asks[1:])):
            raise ValueError("snapshot_asks_must_be_strictly_ascending")
        if bids[0].price >= asks[0].price:
            raise ValueError("snapshot_book_crossed_or_locked")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "ts", _aware_utc(self.ts, field_name="snapshot_ts"))
        object.__setattr__(self, "bids", bids)
        object.__setattr__(self, "asks", asks)

    @property
    def mid_price(self) -> Decimal:
        return (self.bids[0].price + self.asks[0].price) / Decimal(2)


@dataclass(frozen=True, slots=True)
class L2TradeEvent:
    symbol: str
    ts: datetime
    trade_id: str
    price: Decimal
    quantity: Decimal
    aggressor_side: Side

    def __post_init__(self) -> None:
        symbol = self.symbol.strip().upper()
        if not symbol or not self.trade_id.strip():
            raise ValueError("trade_identity_required")
        if self.aggressor_side not in {"buy", "sell"}:
            raise ValueError("trade_aggressor_side_invalid")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "ts", _aware_utc(self.ts, field_name="trade_ts"))
        object.__setattr__(self, "price", _decimal(self.price, field_name="trade_price"))
        object.__setattr__(
            self,
            "quantity",
            _decimal(self.quantity, field_name="trade_quantity"),
        )


@dataclass(frozen=True, slots=True)
class L2OrderRequest:
    order_id: str
    symbol: str
    submitted_at: datetime
    side: Side
    order_kind: OrderKind
    target_quantity: Decimal
    expected_edge_bps: float
    limit_price: Decimal | None = None
    max_wait_ms: int = 2_000

    def __post_init__(self) -> None:
        if not self.order_id.strip() or not self.symbol.strip():
            raise ValueError("order_identity_required")
        if self.side not in {"buy", "sell"}:
            raise ValueError("order_side_invalid")
        if self.order_kind not in {"market", "ioc_limit", "post_only"}:
            raise ValueError("order_kind_invalid")
        if self.order_kind != "market" and self.limit_price is None:
            raise ValueError("limit_price_required")
        if self.order_kind == "market" and self.limit_price is not None:
            raise ValueError("market_order_must_not_have_limit_price")
        if self.max_wait_ms <= 0:
            raise ValueError("max_wait_ms_must_be_positive")
        if not math.isfinite(float(self.expected_edge_bps)):
            raise ValueError("expected_edge_bps_must_be_finite")
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        object.__setattr__(
            self,
            "submitted_at",
            _aware_utc(self.submitted_at, field_name="submitted_at"),
        )
        object.__setattr__(
            self,
            "target_quantity",
            _decimal(self.target_quantity, field_name="target_quantity"),
        )
        if self.limit_price is not None:
            object.__setattr__(
                self,
                "limit_price",
                _decimal(self.limit_price, field_name="limit_price"),
            )


@dataclass(frozen=True, slots=True)
class L2ReplayPolicy:
    taker_fee_bps: float = 5.0
    maker_fee_bps: float = 2.0
    queue_ahead_multiplier: Decimal = Decimal("1.0")
    max_snapshot_wait_ms: int = 2_000

    def __post_init__(self) -> None:
        for name in ("taker_fee_bps", "maker_fee_bps"):
            if not math.isfinite(float(getattr(self, name))):
                raise ValueError(f"{name}_must_be_finite")
        if self.taker_fee_bps < 0:
            raise ValueError("taker_fee_bps_must_be_non_negative")
        multiplier = _decimal(
            self.queue_ahead_multiplier,
            field_name="queue_ahead_multiplier",
            positive=False,
        )
        if multiplier < 0:
            raise ValueError("queue_ahead_multiplier_must_be_non_negative")
        if self.max_snapshot_wait_ms <= 0:
            raise ValueError("max_snapshot_wait_ms_must_be_positive")
        object.__setattr__(self, "queue_ahead_multiplier", multiplier)


@dataclass(frozen=True, slots=True)
class L2Fill:
    ts: datetime
    quantity: Decimal
    price: Decimal
    liquidity: Literal["maker", "taker"]
    fee_bps: float
    fee_notional: Decimal
    source_ref: str


@dataclass(frozen=True, slots=True)
class L2OrderReplayResult:
    order_id: str
    status: ReplayStatus
    target_quantity: Decimal
    filled_quantity: Decimal
    average_fill_price: Decimal | None
    arrival_mid_price: Decimal | None
    slippage_bps: float | None
    fee_bps_weighted: float | None
    fee_notional: Decimal
    total_cost_bps: float | None
    cost_adjusted_edge_bps: float | None
    fills: tuple[L2Fill, ...]
    reason_code: str


@dataclass(frozen=True, slots=True)
class L2ExecutionEvidence:
    format_version: int
    model_version: str
    symbol: str
    window_start: datetime
    window_end: datetime
    microstructure_eligibility_fingerprint: str
    request_count: int
    full_fill_ratio: float
    partial_fill_ratio: float
    no_fill_ratio: float
    rejected_ratio: float
    fee_bps_mean: float
    slippage_bps_mean: float
    cost_adjusted_edge_bps_mean: float
    results: tuple[L2OrderReplayResult, ...]
    limitations: tuple[str, ...]
    evidence_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))

    def execution_cost_summary(
        self,
        *,
        plan_id: str,
        timeframe: str,
        benchmark_segment: str,
        dataset_fingerprint: str,
    ) -> dict[str, Any]:
        if not plan_id.strip():
            raise ValueError("plan_id_required")
        return {
            "schema_version": "execution_cost_summary_v1",
            "plan_id": plan_id,
            "model_version": self.model_version,
            "source_run_id": self.evidence_fingerprint,
            "symbol": self.symbol,
            "timeframe": timeframe,
            "benchmark_segment": benchmark_segment,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "dataset_fingerprint": dataset_fingerprint,
            "dataset_fingerprint_compatibility": "compatible",
            "compatibility_reason": (
                "explicit caller-bound Gold and microstructure evidence fingerprints"
            ),
            "full_fill_ratio": self.full_fill_ratio,
            "partial_fill_ratio": self.partial_fill_ratio,
            "turnover": {"mean": 1.0},
            "fee": {"mean": self.fee_bps_mean},
            "funding": {"mean": 0.0},
            "slippage": {"mean": self.slippage_bps_mean},
            "cost_adjusted_edge": {"mean": self.cost_adjusted_edge_bps_mean},
            "l2_execution_evidence_fingerprint": self.evidence_fingerprint,
        }


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


def _snapshot_for_request(
    snapshots: Sequence[L2OrderBookSnapshot],
    request: L2OrderRequest,
    policy: L2ReplayPolicy,
) -> L2OrderBookSnapshot | None:
    deadline = request.submitted_at + timedelta(
        milliseconds=min(request.max_wait_ms, policy.max_snapshot_wait_ms)
    )
    for snapshot in snapshots:
        if request.submitted_at <= snapshot.ts <= deadline:
            return snapshot
        if snapshot.ts > deadline:
            break
    return None


def _marketable(request: L2OrderRequest, snapshot: L2OrderBookSnapshot) -> bool:
    if request.limit_price is None:
        return True
    if request.side == "buy":
        return request.limit_price >= snapshot.asks[0].price
    return request.limit_price <= snapshot.bids[0].price


def _status(request: L2OrderRequest, filled: Decimal, *, rejected: bool = False) -> ReplayStatus:
    if rejected:
        return "rejected"
    if filled == 0:
        return "no_fill"
    if filled < request.target_quantity:
        return "partial_fill"
    return "filled"


def _build_result(
    request: L2OrderRequest,
    snapshot: L2OrderBookSnapshot | None,
    fills: Sequence[L2Fill],
    *,
    reason_code: str,
    rejected: bool = False,
) -> L2OrderReplayResult:
    filled = sum((fill.quantity for fill in fills), Decimal(0))
    notional = sum((fill.quantity * fill.price for fill in fills), Decimal(0))
    fees = sum((fill.fee_notional for fill in fills), Decimal(0))
    average = notional / filled if filled > 0 else None
    mid = snapshot.mid_price if snapshot is not None else None
    if average is not None and mid is not None:
        direction = Decimal(1) if request.side == "buy" else Decimal(-1)
        slippage = float(direction * (average - mid) / mid * Decimal(10_000))
        weighted_fee = float(fees / notional * Decimal(10_000)) if notional > 0 else None
        total_cost = slippage + (weighted_fee or 0.0)
        adjusted_edge = request.expected_edge_bps - total_cost
    else:
        slippage = None
        weighted_fee = None
        total_cost = None
        adjusted_edge = None
    return L2OrderReplayResult(
        order_id=request.order_id,
        status=_status(request, filled, rejected=rejected),
        target_quantity=request.target_quantity,
        filled_quantity=filled,
        average_fill_price=average,
        arrival_mid_price=mid,
        slippage_bps=slippage,
        fee_bps_weighted=weighted_fee,
        fee_notional=fees,
        total_cost_bps=total_cost,
        cost_adjusted_edge_bps=adjusted_edge,
        fills=tuple(fills),
        reason_code=reason_code,
    )


def _replay_taker(
    request: L2OrderRequest,
    snapshot: L2OrderBookSnapshot,
    policy: L2ReplayPolicy,
    depth_consumed: dict[tuple[int, str, int], Decimal],
) -> L2OrderReplayResult:
    levels = snapshot.asks if request.side == "buy" else snapshot.bids
    remaining = request.target_quantity
    fills: list[L2Fill] = []
    for index, level in enumerate(levels):
        if request.limit_price is not None:
            outside_limit = (
                request.side == "buy" and level.price > request.limit_price
            ) or (request.side == "sell" and level.price < request.limit_price)
            if outside_limit:
                break
        key = (snapshot.collector_sequence, request.side, index)
        available = max(Decimal(0), level.quantity - depth_consumed.get(key, Decimal(0)))
        quantity = min(remaining, available)
        if quantity <= 0:
            continue
        fee = quantity * level.price * Decimal(str(policy.taker_fee_bps)) / Decimal(10_000)
        fills.append(
            L2Fill(
                ts=snapshot.ts,
                quantity=quantity,
                price=level.price,
                liquidity="taker",
                fee_bps=policy.taker_fee_bps,
                fee_notional=fee,
                source_ref=f"book:{snapshot.collector_sequence}:level:{index + 1}",
            )
        )
        depth_consumed[key] = depth_consumed.get(key, Decimal(0)) + quantity
        remaining -= quantity
        if remaining <= 0:
            break
    reason = "top5_depth_filled" if remaining <= 0 else "top5_depth_exhausted"
    return _build_result(request, snapshot, fills, reason_code=reason)


def _visible_queue(
    request: L2OrderRequest,
    snapshot: L2OrderBookSnapshot,
) -> Decimal | None:
    levels = snapshot.bids if request.side == "buy" else snapshot.asks
    for level in levels:
        if level.price == request.limit_price:
            return level.quantity
    return None


def _trade_hits_passive_order(request: L2OrderRequest, trade: L2TradeEvent) -> bool:
    if request.limit_price is None:
        return False
    if request.side == "buy":
        return trade.aggressor_side == "sell" and trade.price <= request.limit_price
    return trade.aggressor_side == "buy" and trade.price >= request.limit_price


def _replay_post_only(
    request: L2OrderRequest,
    snapshot: L2OrderBookSnapshot,
    trades: Sequence[L2TradeEvent],
    policy: L2ReplayPolicy,
    trade_consumed: dict[int, Decimal],
) -> L2OrderReplayResult:
    if _marketable(request, snapshot):
        return _build_result(
            request,
            snapshot,
            (),
            reason_code="post_only_would_cross",
            rejected=True,
        )
    visible = _visible_queue(request, snapshot)
    if visible is None:
        return _build_result(
            request,
            snapshot,
            (),
            reason_code="post_only_price_level_not_observed",
        )
    queue_ahead = visible * policy.queue_ahead_multiplier
    remaining = request.target_quantity
    deadline = request.submitted_at + timedelta(milliseconds=request.max_wait_ms)
    fills: list[L2Fill] = []
    for index, trade in enumerate(trades):
        if trade.ts <= snapshot.ts:
            continue
        if trade.ts > deadline:
            break
        if not _trade_hits_passive_order(request, trade):
            continue
        available = max(Decimal(0), trade.quantity - trade_consumed.get(index, Decimal(0)))
        if available <= 0:
            continue
        queue_consumption = min(queue_ahead, available)
        queue_ahead -= queue_consumption
        available -= queue_consumption
        # Public trade volume used to clear pre-existing exchange queue cannot
        # also be assigned to another simulated order.
        trade_consumed[index] = trade_consumed.get(index, Decimal(0)) + queue_consumption
        if queue_ahead > 0 or available <= 0:
            continue
        quantity = min(remaining, available)
        fee = (
            quantity
            * request.limit_price
            * Decimal(str(policy.maker_fee_bps))
            / Decimal(10_000)
        )
        fills.append(
            L2Fill(
                ts=trade.ts,
                quantity=quantity,
                price=request.limit_price,
                liquidity="maker",
                fee_bps=policy.maker_fee_bps,
                fee_notional=fee,
                source_ref=f"trade:{trade.trade_id}",
            )
        )
        trade_consumed[index] = trade_consumed.get(index, Decimal(0)) + quantity
        remaining -= quantity
        if remaining <= 0:
            break
    if remaining <= 0:
        reason = "passive_queue_filled"
    elif fills:
        reason = "passive_queue_partial_fill"
    elif queue_ahead > 0:
        reason = "passive_queue_not_cleared"
    else:
        reason = "passive_no_remaining_trade_liquidity"
    return _build_result(request, snapshot, fills, reason_code=reason)


def replay_l2_orders(
    *,
    snapshots: Sequence[L2OrderBookSnapshot],
    trades: Sequence[L2TradeEvent],
    requests: Sequence[L2OrderRequest],
    microstructure_eligibility_fingerprint: str,
    policy: L2ReplayPolicy | None = None,
) -> L2ExecutionEvidence:
    """Replay requests in submission order with shared observed liquidity."""

    if not microstructure_eligibility_fingerprint.strip():
        raise ValueError("microstructure_eligibility_fingerprint_required")
    if not snapshots or not requests:
        raise ValueError("snapshots_and_requests_required")
    selected_policy = policy or L2ReplayPolicy()
    ordered_snapshots = tuple(sorted(snapshots, key=lambda item: (item.ts, item.collector_sequence)))
    ordered_trades = tuple(sorted(trades, key=lambda item: (item.ts, item.trade_id)))
    ordered_requests = tuple(sorted(requests, key=lambda item: (item.submitted_at, item.order_id)))
    symbol = ordered_snapshots[0].symbol
    if any(item.symbol != symbol for item in (*ordered_snapshots, *ordered_trades, *ordered_requests)):
        raise ValueError("l2_replay_symbol_mismatch")
    if any(
        left.collector_sequence >= right.collector_sequence
        for left, right in zip(ordered_snapshots, ordered_snapshots[1:])
    ):
        raise ValueError("collector_sequence_not_strictly_increasing")
    depth_consumed: dict[tuple[int, str, int], Decimal] = {}
    trade_consumed: dict[int, Decimal] = {}
    results: list[L2OrderReplayResult] = []
    for request in ordered_requests:
        snapshot = _snapshot_for_request(ordered_snapshots, request, selected_policy)
        if snapshot is None:
            results.append(
                _build_result(
                    request,
                    None,
                    (),
                    reason_code="fresh_arrival_snapshot_missing",
                )
            )
            continue
        if request.order_kind == "post_only":
            result = _replay_post_only(
                request,
                snapshot,
                ordered_trades,
                selected_policy,
                trade_consumed,
            )
        else:
            result = _replay_taker(
                request,
                snapshot,
                selected_policy,
                depth_consumed,
            )
        results.append(result)

    count = len(results)
    ratios = {
        name: sum(result.status == name for result in results) / count
        for name in ("filled", "partial_fill", "no_fill", "rejected")
    }
    fee_values = [result.fee_bps_weighted for result in results if result.fee_bps_weighted is not None]
    slippage_values = [result.slippage_bps for result in results if result.slippage_bps is not None]
    edge_values = [
        result.cost_adjusted_edge_bps
        for result in results
        if result.cost_adjusted_edge_bps is not None
    ]
    payload = {
        "model_version": L2_EVENT_REPLAY_MODEL_VERSION,
        "symbol": symbol,
        "microstructure_eligibility_fingerprint": microstructure_eligibility_fingerprint,
        "policy": _jsonable(asdict(selected_policy)),
        "snapshot_hashes": [snapshot.payload_hash for snapshot in ordered_snapshots],
        "requests": _jsonable([asdict(request) for request in ordered_requests]),
        "results": _jsonable([asdict(result) for result in results]),
    }
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return L2ExecutionEvidence(
        format_version=1,
        model_version=L2_EVENT_REPLAY_MODEL_VERSION,
        symbol=symbol,
        window_start=ordered_snapshots[0].ts,
        window_end=ordered_snapshots[-1].ts,
        microstructure_eligibility_fingerprint=microstructure_eligibility_fingerprint,
        request_count=count,
        full_fill_ratio=ratios["filled"],
        partial_fill_ratio=ratios["partial_fill"],
        no_fill_ratio=ratios["no_fill"],
        rejected_ratio=ratios["rejected"],
        fee_bps_mean=sum(fee_values) / len(fee_values) if fee_values else 0.0,
        slippage_bps_mean=(
            sum(slippage_values) / len(slippage_values) if slippage_values else 0.0
        ),
        cost_adjusted_edge_bps_mean=(
            sum(edge_values) / len(edge_values) if edge_values else 0.0
        ),
        results=tuple(results),
        limitations=(
            "Top-5 displayed depth only; no extrapolation beyond observed levels",
            "queue cancellations do not reduce queue ahead",
            "public trade prints approximate passive queue consumption",
            "research-only model; not exchange matching-engine truth",
        ),
        evidence_fingerprint=f"l2_{fingerprint}",
    )
