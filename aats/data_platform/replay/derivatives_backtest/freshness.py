"""Event-time mark/index freshness for derivatives replay v1.

Freshness state is a deterministic projection of hash-bound replay events. It
is deliberately *not* an in-process authorization token: the future event-set
preflight owns source authority, while every value object here preserves and
revalidates the complete source event identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from .contracts import DerivativesBacktestContractError
from .events import (
    IndexPriceEventV1,
    MarkPriceEventV1,
    parse_derivative_replay_event,
)
from .wire import require_utc_datetime


DERIVATIVES_PRICE_FRESHNESS_POLICY_ID = "derivatives-price-freshness/v1"
DERIVATIVES_MAX_PRICE_AGE = timedelta(seconds=60)

PriceEventV1 = IndexPriceEventV1 | MarkPriceEventV1


@dataclass(frozen=True, slots=True)
class PriceObservationV1:
    """A price observation retaining its complete, self-validating event."""

    event: PriceEventV1

    def __post_init__(self) -> None:
        if type(self.event) not in {IndexPriceEventV1, MarkPriceEventV1}:
            raise DerivativesBacktestContractError("price_event_invalid")
        try:
            validated = parse_derivative_replay_event(self.event.to_dict())
        except DerivativesBacktestContractError:
            raise
        except (AttributeError, KeyError, OverflowError, TypeError, ValueError) as exc:
            raise DerivativesBacktestContractError(
                "price_event_revalidation_failed"
            ) from exc
        if type(validated) is not type(self.event):
            raise DerivativesBacktestContractError(
                "price_event_revalidation_mismatch"
            )
        # Keep a defensive deep value copy so later mutation of caller-owned
        # event/header/source-ref objects cannot alter an issued observation.
        object.__setattr__(self, "event", validated)

    @property
    def kind(self) -> Literal["mark", "index"]:
        return "mark" if type(self.event) is MarkPriceEventV1 else "index"

    @property
    def ts(self) -> datetime:
        return self.event.header.ts

    @property
    def source_sequence(self) -> int:
        return self.event.header.source_sequence

    @property
    def event_id(self) -> str:
        return self.event.header.event_id

    @property
    def price(self) -> Decimal:
        return self.event.price

    @property
    def source_key(self) -> tuple[datetime, int]:
        return (self.ts, self.source_sequence)


@dataclass(frozen=True, slots=True)
class PriceFreshnessStateV1:
    """Derived cursor; source authority remains with event-set preflight."""

    latest_mark: PriceObservationV1 | None
    latest_index: PriceObservationV1 | None

    def __post_init__(self) -> None:
        if self.latest_mark is not None and (
            type(self.latest_mark) is not PriceObservationV1
            or self.latest_mark.kind != "mark"
        ):
            raise DerivativesBacktestContractError("mark_price_cursor_invalid")
        if self.latest_mark is not None:
            object.__setattr__(
                self,
                "latest_mark",
                PriceObservationV1(event=self.latest_mark.event),
            )
        if self.latest_index is not None and (
            type(self.latest_index) is not PriceObservationV1
            or self.latest_index.kind != "index"
        ):
            raise DerivativesBacktestContractError("index_price_cursor_invalid")
        if self.latest_index is not None:
            object.__setattr__(
                self,
                "latest_index",
                PriceObservationV1(event=self.latest_index.event),
            )


@dataclass(frozen=True, slots=True)
class FreshPricePairV1:
    mark: PriceObservationV1
    index: PriceObservationV1
    use_ts: datetime

    def __post_init__(self) -> None:
        resolved_use_ts = require_utc_datetime(self.use_ts, "price_use_ts")
        if (
            type(self.mark) is not PriceObservationV1
            or self.mark.kind != "mark"
            or type(self.index) is not PriceObservationV1
            or self.index.kind != "index"
        ):
            raise DerivativesBacktestContractError("fresh_price_pair_invalid")
        object.__setattr__(
            self,
            "mark",
            _require_fresh(
                self.mark,
                expected_kind="mark",
                use_ts=resolved_use_ts,
            ),
        )
        object.__setattr__(
            self,
            "index",
            _require_fresh(
                self.index,
                expected_kind="index",
                use_ts=resolved_use_ts,
            ),
        )


def empty_price_freshness_state() -> PriceFreshnessStateV1:
    return PriceFreshnessStateV1(latest_mark=None, latest_index=None)


def apply_price_event(
    state: PriceFreshnessStateV1,
    event: IndexPriceEventV1 | MarkPriceEventV1,
) -> PriceFreshnessStateV1:
    if type(state) is not PriceFreshnessStateV1:
        raise DerivativesBacktestContractError("price_freshness_state_invalid")
    state = PriceFreshnessStateV1(
        latest_mark=state.latest_mark,
        latest_index=state.latest_index,
    )
    if type(event) is IndexPriceEventV1:
        observation = PriceObservationV1(event=event)
        if (
            state.latest_index is not None
            and observation.source_key <= state.latest_index.source_key
        ):
            raise DerivativesBacktestContractError("index_price_cursor_regressed")
        return PriceFreshnessStateV1(
            latest_mark=state.latest_mark,
            latest_index=observation,
        )
    if type(event) is MarkPriceEventV1:
        observation = PriceObservationV1(event=event)
        if (
            state.latest_mark is not None
            and observation.source_key <= state.latest_mark.source_key
        ):
            raise DerivativesBacktestContractError("mark_price_cursor_regressed")
        return PriceFreshnessStateV1(
            latest_mark=observation,
            latest_index=state.latest_index,
        )
    raise DerivativesBacktestContractError("price_event_invalid")


def _require_fresh(
    observation: PriceObservationV1 | None,
    *,
    expected_kind: Literal["mark", "index"],
    use_ts: datetime,
) -> PriceObservationV1:
    if observation is None:
        raise DerivativesBacktestContractError(f"{expected_kind}_price_missing")
    if type(observation) is not PriceObservationV1 or observation.kind != expected_kind:
        raise DerivativesBacktestContractError(f"{expected_kind}_price_cursor_invalid")
    observation = PriceObservationV1(event=observation.event)
    age = use_ts - observation.ts
    if age < timedelta(0):
        raise DerivativesBacktestContractError(f"{expected_kind}_price_from_future")
    if age > DERIVATIVES_MAX_PRICE_AGE:
        raise DerivativesBacktestContractError(f"{expected_kind}_price_stale")
    return observation


def require_prices_as_of(
    state: PriceFreshnessStateV1,
    *,
    use_ts: datetime,
) -> FreshPricePairV1:
    """Require independent mark and index observations at replay event-time."""

    if type(state) is not PriceFreshnessStateV1:
        raise DerivativesBacktestContractError("price_freshness_state_invalid")
    resolved_use_ts = require_utc_datetime(use_ts, "price_use_ts")
    mark = _require_fresh(
        state.latest_mark,
        expected_kind="mark",
        use_ts=resolved_use_ts,
    )
    index = _require_fresh(
        state.latest_index,
        expected_kind="index",
        use_ts=resolved_use_ts,
    )
    return FreshPricePairV1(mark=mark, index=index, use_ts=resolved_use_ts)


def require_end_valuation_prices(
    state: PriceFreshnessStateV1,
    *,
    end_ts: datetime,
) -> FreshPricePairV1:
    """End valuation requires source observations strictly before ``end_ts``."""

    resolved_end = require_utc_datetime(end_ts, "end_ts")
    pair = require_prices_as_of(state, use_ts=resolved_end)
    if pair.mark.ts >= resolved_end:
        raise DerivativesBacktestContractError("end_mark_timestamp_invalid")
    if pair.index.ts >= resolved_end:
        raise DerivativesBacktestContractError("end_index_timestamp_invalid")
    return pair


__all__ = [
    "DERIVATIVES_MAX_PRICE_AGE",
    "DERIVATIVES_PRICE_FRESHNESS_POLICY_ID",
    "FreshPricePairV1",
    "PriceFreshnessStateV1",
    "apply_price_event",
    "empty_price_freshness_state",
    "require_end_valuation_prices",
    "require_prices_as_of",
]
