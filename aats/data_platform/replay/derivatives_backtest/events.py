"""Closed, canonical source-event union for derivatives replay v1."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping, TypeAlias

from aats.data_platform.governance.typed_json_identity import typed_json_sha256

from .contracts import (
    DERIVATIVES_BACKTEST_SYMBOL,
    DerivativesBacktestContractError,
    LiquidityRoleV1,
    canonical_accounting_decimal,
    parse_canonical_accounting_decimal,
    require_finite_decimal,
    require_non_negative_decimal,
    require_positive_decimal,
)
from .snapshot_refs import DerivativesSnapshotRefsV1, ImmutableSnapshotRefV1
from .wire import (
    canonical_utc_timestamp,
    require_canonical_utc_timestamp,
    require_canonical_uuid,
    require_exact_int,
    require_exact_mapping_keys,
    require_identifier,
    require_sha256,
    require_utc_datetime,
)


DERIVATIVES_REPLAY_EVENT_SCHEMA = "derivatives-replay-event/v1"
DERIVATIVES_EVENT_ORDERING_POLICY_ID = "derivatives-event-ordering/v1"
DERIVATIVES_MAX_SOURCE_SEQUENCE = (1 << 63) - 1


class DerivativeEventKindV1(StrEnum):
    CONTRACT_TIER_EFFECTIVE = "contract_tier_effective"
    INDEX_PRICE = "index_price"
    MARK_PRICE = "mark_price"
    FUNDING_SETTLEMENT = "funding_settlement"
    TRADABLE = "tradable"
    BAR_CLOSE = "bar_close"


EVENT_PHASE_PRIORITY_V1 = MappingProxyType({
    DerivativeEventKindV1.CONTRACT_TIER_EFFECTIVE: 5,
    DerivativeEventKindV1.INDEX_PRICE: 10,
    DerivativeEventKindV1.MARK_PRICE: 20,
    DerivativeEventKindV1.FUNDING_SETTLEMENT: 30,
    DerivativeEventKindV1.TRADABLE: 50,
    DerivativeEventKindV1.BAR_CLOSE: 60,
})
EXPECTED_EVENT_STREAM_ID_V1 = MappingProxyType(
    {
        DerivativeEventKindV1.CONTRACT_TIER_EFFECTIVE: "contract-schedule",
        DerivativeEventKindV1.INDEX_PRICE: "index-price",
        DerivativeEventKindV1.MARK_PRICE: "mark-price",
        DerivativeEventKindV1.FUNDING_SETTLEMENT: "funding-settlement",
        DerivativeEventKindV1.TRADABLE: "tradable",
        DerivativeEventKindV1.BAR_CLOSE: "bar-close",
    }
)
SINGLETON_EVENT_KINDS_PER_TIMESTAMP_V1 = frozenset(
    {
        DerivativeEventKindV1.CONTRACT_TIER_EFFECTIVE,
        DerivativeEventKindV1.FUNDING_SETTLEMENT,
        DerivativeEventKindV1.TRADABLE,
        DerivativeEventKindV1.BAR_CLOSE,
    }
)
BAR_FEATURE_STREAM_ID_V1 = "bar-features"


@dataclass(frozen=True, slots=True)
class SourceRecordRefV1:
    """Exact parent and record identities; locators are deliberately absent."""

    stream_id: str
    source_registry_id: str
    parent_artifact_sha256: str
    source_record_sha256: str

    def __post_init__(self) -> None:
        require_identifier(self.stream_id, "stream_id")
        require_canonical_uuid(self.source_registry_id, "source_registry_id")
        require_sha256(self.parent_artifact_sha256, "parent_artifact_sha256")
        require_sha256(self.source_record_sha256, "source_record_sha256")

    def to_dict(self) -> dict[str, str]:
        return {
            "stream_id": self.stream_id,
            "source_registry_id": self.source_registry_id,
            "parent_artifact_sha256": self.parent_artifact_sha256,
            "source_record_sha256": self.source_record_sha256,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SourceRecordRefV1:
        payload = require_exact_mapping_keys(
            value,
            {
                "stream_id",
                "source_registry_id",
                "parent_artifact_sha256",
                "source_record_sha256",
            },
            "source_record_ref_shape_invalid",
        )
        return cls(
            stream_id=require_identifier(payload["stream_id"], "stream_id"),
            source_registry_id=require_canonical_uuid(
                payload["source_registry_id"],
                "source_registry_id",
            ),
            parent_artifact_sha256=require_sha256(
                payload["parent_artifact_sha256"],
                "parent_artifact_sha256",
            ),
            source_record_sha256=require_sha256(
                payload["source_record_sha256"],
                "source_record_sha256",
            ),
        )


def _revalidate_source_ref(
    value: SourceRecordRefV1,
    *,
    invalid_code: str = "source_record_ref_invalid",
) -> SourceRecordRefV1:
    if type(value) is not SourceRecordRefV1:
        raise DerivativesBacktestContractError(invalid_code)
    try:
        validated = SourceRecordRefV1.from_dict(value.to_dict())
    except DerivativesBacktestContractError:
        raise
    except (AttributeError, KeyError, OverflowError, TypeError, ValueError) as exc:
        raise DerivativesBacktestContractError(
            "source_record_ref_revalidation_failed"
        ) from exc
    if validated != value:
        raise DerivativesBacktestContractError(
            "source_record_ref_revalidation_mismatch"
        )
    return validated


def _revalidate_snapshot_set(
    value: DerivativesSnapshotRefsV1,
) -> DerivativesSnapshotRefsV1:
    if type(value) is not DerivativesSnapshotRefsV1:
        raise DerivativesBacktestContractError("snapshot_set_invalid")
    try:
        validated = DerivativesSnapshotRefsV1.from_dict(value.to_dict())
    except DerivativesBacktestContractError:
        raise
    except (AttributeError, KeyError, OverflowError, TypeError, ValueError) as exc:
        raise DerivativesBacktestContractError(
            "snapshot_set_revalidation_failed"
        ) from exc
    if validated != value:
        raise DerivativesBacktestContractError(
            "snapshot_set_revalidation_mismatch"
        )
    return validated


def _revalidate_snapshot_ref(
    value: ImmutableSnapshotRefV1,
) -> ImmutableSnapshotRefV1:
    if type(value) is not ImmutableSnapshotRefV1:
        raise DerivativesBacktestContractError("funding_schedule_ref_invalid")
    try:
        validated = ImmutableSnapshotRefV1.from_dict(value.to_dict())
    except DerivativesBacktestContractError:
        raise
    except (AttributeError, KeyError, OverflowError, TypeError, ValueError) as exc:
        raise DerivativesBacktestContractError(
            "snapshot_ref_revalidation_failed"
        ) from exc
    if validated != value:
        raise DerivativesBacktestContractError(
            "snapshot_ref_revalidation_mismatch"
        )
    return validated


@dataclass(frozen=True, slots=True)
class ReplayEventHeaderV1:
    event_type: DerivativeEventKindV1
    ts: datetime
    source_sequence: int
    event_id: str
    source_ref: SourceRecordRefV1

    def __post_init__(self) -> None:
        if type(self.event_type) is not DerivativeEventKindV1:
            raise DerivativesBacktestContractError("event_type_invalid")
        require_utc_datetime(self.ts, "event_ts")
        require_exact_int(
            self.source_sequence,
            "source_sequence",
            minimum=0,
            maximum=DERIVATIVES_MAX_SOURCE_SEQUENCE,
        )
        require_sha256(self.event_id, "event_id")
        object.__setattr__(
            self,
            "source_ref",
            _revalidate_source_ref(self.source_ref),
        )

    def identity_fields(self) -> dict[str, Any]:
        return {
            "schema": DERIVATIVES_REPLAY_EVENT_SCHEMA,
            "event_type": self.event_type.value,
            "symbol": DERIVATIVES_BACKTEST_SYMBOL,
            "ts": canonical_utc_timestamp(self.ts, "event_ts"),
            "source_sequence": self.source_sequence,
            "source_ref": self.source_ref.to_dict(),
        }


def _event_id(header_fields: Mapping[str, Any], body: Mapping[str, Any]) -> str:
    return typed_json_sha256({**dict(header_fields), **dict(body)})


def _wire_payload(
    header: ReplayEventHeaderV1,
    body: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        **header.identity_fields(),
        "event_id": header.event_id,
        **dict(body),
    }


def _validate_header(
    header: ReplayEventHeaderV1,
    *,
    event_type: DerivativeEventKindV1,
    body: Mapping[str, Any],
) -> ReplayEventHeaderV1:
    if type(header) is not ReplayEventHeaderV1 or header.event_type is not event_type:
        raise DerivativesBacktestContractError("event_header_type_mismatch")
    validated = ReplayEventHeaderV1(
        event_type=header.event_type,
        ts=header.ts,
        source_sequence=header.source_sequence,
        event_id=header.event_id,
        source_ref=header.source_ref,
    )
    if validated != header:
        raise DerivativesBacktestContractError("event_header_revalidation_mismatch")
    if validated.source_ref.stream_id != EXPECTED_EVENT_STREAM_ID_V1[event_type]:
        raise DerivativesBacktestContractError("event_source_stream_mismatch")
    expected = _event_id(validated.identity_fields(), body)
    if validated.event_id != expected:
        raise DerivativesBacktestContractError("event_id_mismatch")
    return validated


def _build_header(
    *,
    event_type: DerivativeEventKindV1,
    ts: datetime,
    source_sequence: int,
    source_ref: SourceRecordRefV1,
    body: Mapping[str, Any],
) -> ReplayEventHeaderV1:
    provisional = ReplayEventHeaderV1(
        event_type=event_type,
        ts=ts,
        source_sequence=source_sequence,
        event_id="0" * 64,
        source_ref=source_ref,
    )
    return ReplayEventHeaderV1(
        event_type=event_type,
        ts=ts,
        source_sequence=source_sequence,
        event_id=_event_id(provisional.identity_fields(), body),
        source_ref=source_ref,
    )


@dataclass(frozen=True, slots=True)
class ContractTierEffectiveEventV1:
    header: ReplayEventHeaderV1
    snapshot_refs: DerivativesSnapshotRefsV1

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "snapshot_refs",
            _revalidate_snapshot_set(self.snapshot_refs),
        )
        body = self.identity_body()
        object.__setattr__(
            self,
            "header",
            _validate_header(
                self.header,
                event_type=DerivativeEventKindV1.CONTRACT_TIER_EFFECTIVE,
                body=body,
            ),
        )
        for ref in (
            self.snapshot_refs.instrument,
            self.snapshot_refs.position_tier,
            self.snapshot_refs.execution_fee,
            self.snapshot_refs.funding_schedule,
        ):
            if ref.effective_from > self.header.ts or (
                ref.effective_to is not None
                and self.header.ts >= ref.effective_to
            ):
                raise DerivativesBacktestContractError(
                    "snapshot_not_effective_at_activation",
                    field=ref.kind.value,
                )

    def body(self) -> dict[str, Any]:
        return {"snapshot_refs": self.snapshot_refs.to_dict()}

    def identity_body(self) -> dict[str, Any]:
        return {"snapshot_refs": self.snapshot_refs.semantic_identity_dict()}

    def to_dict(self) -> dict[str, Any]:
        return _wire_payload(self.header, self.body())

    @classmethod
    def create(
        cls,
        *,
        ts: datetime,
        source_sequence: int,
        source_ref: SourceRecordRefV1,
        snapshot_refs: DerivativesSnapshotRefsV1,
    ) -> ContractTierEffectiveEventV1:
        snapshot_refs = _revalidate_snapshot_set(snapshot_refs)
        body = {"snapshot_refs": snapshot_refs.semantic_identity_dict()}
        return cls(
            header=_build_header(
                event_type=DerivativeEventKindV1.CONTRACT_TIER_EFFECTIVE,
                ts=ts,
                source_sequence=source_sequence,
                source_ref=source_ref,
                body=body,
            ),
            snapshot_refs=snapshot_refs,
        )


@dataclass(frozen=True, slots=True)
class IndexPriceEventV1:
    header: ReplayEventHeaderV1
    price: Decimal

    def __post_init__(self) -> None:
        require_positive_decimal(self.price, "index_price")
        object.__setattr__(
            self,
            "header",
            _validate_header(
                self.header,
                event_type=DerivativeEventKindV1.INDEX_PRICE,
                body=self.body(),
            ),
        )

    def body(self) -> dict[str, str]:
        return {"price": canonical_accounting_decimal(self.price, "index_price")}

    def to_dict(self) -> dict[str, Any]:
        return _wire_payload(self.header, self.body())

    @classmethod
    def create(cls, **kwargs: Any) -> IndexPriceEventV1:
        price = kwargs.pop("price")
        body = {"price": canonical_accounting_decimal(price, "index_price")}
        return cls(
            header=_build_header(
                event_type=DerivativeEventKindV1.INDEX_PRICE,
                body=body,
                **kwargs,
            ),
            price=price,
        )


@dataclass(frozen=True, slots=True)
class MarkPriceEventV1:
    header: ReplayEventHeaderV1
    price: Decimal

    def __post_init__(self) -> None:
        require_positive_decimal(self.price, "mark_price")
        object.__setattr__(
            self,
            "header",
            _validate_header(
                self.header,
                event_type=DerivativeEventKindV1.MARK_PRICE,
                body=self.body(),
            ),
        )

    def body(self) -> dict[str, str]:
        return {"price": canonical_accounting_decimal(self.price, "mark_price")}

    def to_dict(self) -> dict[str, Any]:
        return _wire_payload(self.header, self.body())

    @classmethod
    def create(cls, **kwargs: Any) -> MarkPriceEventV1:
        price = kwargs.pop("price")
        body = {"price": canonical_accounting_decimal(price, "mark_price")}
        return cls(
            header=_build_header(
                event_type=DerivativeEventKindV1.MARK_PRICE,
                body=body,
                **kwargs,
            ),
            price=price,
        )


@dataclass(frozen=True, slots=True)
class FundingSettlementEventV1:
    header: ReplayEventHeaderV1
    rate: Decimal
    schedule_ref: ImmutableSnapshotRefV1
    observed_at_ts: datetime

    def __post_init__(self) -> None:
        rate = require_finite_decimal(self.rate, "funding_rate")
        if not Decimal("-1") < rate < Decimal("1"):
            raise DerivativesBacktestContractError("funding_rate_invalid")
        object.__setattr__(
            self,
            "schedule_ref",
            _revalidate_snapshot_ref(self.schedule_ref),
        )
        if self.schedule_ref.kind.value != "funding_schedule":
            raise DerivativesBacktestContractError("funding_schedule_ref_invalid")
        observed = require_utc_datetime(self.observed_at_ts, "observed_at_ts")
        object.__setattr__(
            self,
            "header",
            _validate_header(
                self.header,
                event_type=DerivativeEventKindV1.FUNDING_SETTLEMENT,
                body=self.identity_body(),
            ),
        )
        if observed > self.header.ts:
            raise DerivativesBacktestContractError("funding_observation_in_future")

    def body(self) -> dict[str, Any]:
        return {
            "rate": canonical_accounting_decimal(self.rate, "funding_rate"),
            "schedule_ref": self.schedule_ref.to_dict(),
            "observed_at_ts": canonical_utc_timestamp(
                self.observed_at_ts,
                "observed_at_ts",
            ),
        }

    def identity_body(self) -> dict[str, Any]:
        return {
            "rate": canonical_accounting_decimal(self.rate, "funding_rate"),
            "schedule_ref": self.schedule_ref.semantic_identity_dict(),
            "observed_at_ts": canonical_utc_timestamp(
                self.observed_at_ts,
                "observed_at_ts",
            ),
        }

    def to_dict(self) -> dict[str, Any]:
        return _wire_payload(self.header, self.body())

    @classmethod
    def create(
        cls,
        *,
        ts: datetime,
        source_sequence: int,
        source_ref: SourceRecordRefV1,
        rate: Decimal,
        schedule_ref: ImmutableSnapshotRefV1,
        observed_at_ts: datetime,
    ) -> FundingSettlementEventV1:
        schedule_ref = _revalidate_snapshot_ref(schedule_ref)
        body = {
            "rate": canonical_accounting_decimal(rate, "funding_rate"),
            "schedule_ref": schedule_ref.semantic_identity_dict(),
            "observed_at_ts": canonical_utc_timestamp(
                observed_at_ts,
                "observed_at_ts",
            ),
        }
        return cls(
            header=_build_header(
                event_type=DerivativeEventKindV1.FUNDING_SETTLEMENT,
                ts=ts,
                source_sequence=source_sequence,
                source_ref=source_ref,
                body=body,
            ),
            rate=rate,
            schedule_ref=schedule_ref,
            observed_at_ts=observed_at_ts,
        )


@dataclass(frozen=True, slots=True)
class TradableEventV1:
    header: ReplayEventHeaderV1
    reference_price: Decimal
    available_contracts: Decimal
    liquidity_role: LiquidityRoleV1

    def __post_init__(self) -> None:
        require_positive_decimal(self.reference_price, "reference_price")
        require_non_negative_decimal(
            self.available_contracts,
            "available_contracts",
        )
        if self.liquidity_role is not LiquidityRoleV1.TAKER:
            raise DerivativesBacktestContractError(
                "liquidity_role_out_of_v1_scope"
            )
        object.__setattr__(
            self,
            "header",
            _validate_header(
                self.header,
                event_type=DerivativeEventKindV1.TRADABLE,
                body=self.body(),
            ),
        )

    def body(self) -> dict[str, str]:
        return {
            "reference_price": canonical_accounting_decimal(
                self.reference_price,
                "reference_price",
            ),
            "available_contracts": canonical_accounting_decimal(
                self.available_contracts,
                "available_contracts",
            ),
            "liquidity_role": self.liquidity_role.value,
        }

    def to_dict(self) -> dict[str, Any]:
        return _wire_payload(self.header, self.body())

    @classmethod
    def create(
        cls,
        *,
        ts: datetime,
        source_sequence: int,
        source_ref: SourceRecordRefV1,
        reference_price: Decimal,
        available_contracts: Decimal,
        liquidity_role: LiquidityRoleV1 = LiquidityRoleV1.TAKER,
    ) -> TradableEventV1:
        body = {
            "reference_price": canonical_accounting_decimal(
                reference_price,
                "reference_price",
            ),
            "available_contracts": canonical_accounting_decimal(
                available_contracts,
                "available_contracts",
            ),
            "liquidity_role": liquidity_role.value,
        }
        return cls(
            header=_build_header(
                event_type=DerivativeEventKindV1.TRADABLE,
                ts=ts,
                source_sequence=source_sequence,
                source_ref=source_ref,
                body=body,
            ),
            reference_price=reference_price,
            available_contracts=available_contracts,
            liquidity_role=liquidity_role,
        )


@dataclass(frozen=True, slots=True)
class BarCloseEventV1:
    header: ReplayEventHeaderV1
    bar_start_ts: datetime
    bar_end_ts: datetime
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    volume_contracts: Decimal
    feature_ref: SourceRecordRefV1

    def __post_init__(self) -> None:
        start = require_utc_datetime(self.bar_start_ts, "bar_start_ts")
        end = require_utc_datetime(self.bar_end_ts, "bar_end_ts")
        open_price = require_positive_decimal(self.open_price, "open_price")
        high = require_positive_decimal(self.high_price, "high_price")
        low = require_positive_decimal(self.low_price, "low_price")
        close = require_positive_decimal(self.close_price, "close_price")
        require_non_negative_decimal(self.volume_contracts, "volume_contracts")
        if high < max(open_price, close, low) or low > min(open_price, close, high):
            raise DerivativesBacktestContractError("bar_ohlc_invalid")
        object.__setattr__(
            self,
            "feature_ref",
            _revalidate_source_ref(
                self.feature_ref,
                invalid_code="feature_ref_invalid",
            ),
        )
        if self.feature_ref.stream_id != BAR_FEATURE_STREAM_ID_V1:
            raise DerivativesBacktestContractError("bar_feature_stream_mismatch")
        object.__setattr__(
            self,
            "header",
            _validate_header(
                self.header,
                event_type=DerivativeEventKindV1.BAR_CLOSE,
                body=self.body(),
            ),
        )
        if (
            end - start != timedelta(minutes=15)
            or self.header.ts != end
            or start.minute % 15 != 0
            or start.second != 0
            or start.microsecond != 0
            or end.minute % 15 != 0
            or end.second != 0
            or end.microsecond != 0
        ):
            raise DerivativesBacktestContractError("bar_window_invalid")

    def body(self) -> dict[str, Any]:
        return {
            "bar_start_ts": canonical_utc_timestamp(
                self.bar_start_ts,
                "bar_start_ts",
            ),
            "bar_end_ts": canonical_utc_timestamp(self.bar_end_ts, "bar_end_ts"),
            "ohlcv": {
                "open": canonical_accounting_decimal(self.open_price, "open_price"),
                "high": canonical_accounting_decimal(self.high_price, "high_price"),
                "low": canonical_accounting_decimal(self.low_price, "low_price"),
                "close": canonical_accounting_decimal(
                    self.close_price,
                    "close_price",
                ),
                "volume_contracts": canonical_accounting_decimal(
                    self.volume_contracts,
                    "volume_contracts",
                ),
            },
            "feature_ref": self.feature_ref.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        return _wire_payload(self.header, self.body())

    @classmethod
    def create(
        cls,
        *,
        ts: datetime,
        source_sequence: int,
        source_ref: SourceRecordRefV1,
        bar_start_ts: datetime,
        bar_end_ts: datetime,
        open_price: Decimal,
        high_price: Decimal,
        low_price: Decimal,
        close_price: Decimal,
        volume_contracts: Decimal,
        feature_ref: SourceRecordRefV1,
    ) -> BarCloseEventV1:
        feature_ref = _revalidate_source_ref(
            feature_ref,
            invalid_code="feature_ref_invalid",
        )
        body = {
            "bar_start_ts": canonical_utc_timestamp(bar_start_ts, "bar_start_ts"),
            "bar_end_ts": canonical_utc_timestamp(bar_end_ts, "bar_end_ts"),
            "ohlcv": {
                "open": canonical_accounting_decimal(open_price, "open_price"),
                "high": canonical_accounting_decimal(high_price, "high_price"),
                "low": canonical_accounting_decimal(low_price, "low_price"),
                "close": canonical_accounting_decimal(close_price, "close_price"),
                "volume_contracts": canonical_accounting_decimal(
                    volume_contracts,
                    "volume_contracts",
                ),
            },
            "feature_ref": feature_ref.to_dict(),
        }
        return cls(
            header=_build_header(
                event_type=DerivativeEventKindV1.BAR_CLOSE,
                ts=ts,
                source_sequence=source_sequence,
                source_ref=source_ref,
                body=body,
            ),
            bar_start_ts=bar_start_ts,
            bar_end_ts=bar_end_ts,
            open_price=open_price,
            high_price=high_price,
            low_price=low_price,
            close_price=close_price,
            volume_contracts=volume_contracts,
            feature_ref=feature_ref,
        )


DerivativeReplayEventV1: TypeAlias = (
    ContractTierEffectiveEventV1
    | IndexPriceEventV1
    | MarkPriceEventV1
    | FundingSettlementEventV1
    | TradableEventV1
    | BarCloseEventV1
)


_EVENT_CLASS_BY_KIND = {
    DerivativeEventKindV1.CONTRACT_TIER_EFFECTIVE: ContractTierEffectiveEventV1,
    DerivativeEventKindV1.INDEX_PRICE: IndexPriceEventV1,
    DerivativeEventKindV1.MARK_PRICE: MarkPriceEventV1,
    DerivativeEventKindV1.FUNDING_SETTLEMENT: FundingSettlementEventV1,
    DerivativeEventKindV1.TRADABLE: TradableEventV1,
    DerivativeEventKindV1.BAR_CLOSE: BarCloseEventV1,
}


def _unchecked_event_order_key(
    event: DerivativeReplayEventV1,
) -> tuple[datetime, int, int, str]:
    if type(event) not in set(_EVENT_CLASS_BY_KIND.values()):
        raise DerivativesBacktestContractError("event_type_invalid")
    return (
        event.header.ts,
        EVENT_PHASE_PRIORITY_V1[event.header.event_type],
        event.header.source_sequence,
        event.header.event_id,
    )


def event_order_key(event: DerivativeReplayEventV1) -> tuple[datetime, int, int, str]:
    """Return the fixed order key after strict full-event revalidation."""

    if type(event) not in set(_EVENT_CLASS_BY_KIND.values()):
        raise DerivativesBacktestContractError("event_type_invalid")
    try:
        validated = parse_derivative_replay_event(event.to_dict())
    except DerivativesBacktestContractError:
        raise
    except (AttributeError, KeyError, OverflowError, TypeError, ValueError) as exc:
        raise DerivativesBacktestContractError("event_revalidation_failed") from exc
    if type(validated) is not type(event) or validated != event:
        raise DerivativesBacktestContractError("event_revalidation_mismatch")
    return _unchecked_event_order_key(validated)


def _parse_header(payload: Mapping[str, Any]) -> ReplayEventHeaderV1:
    if payload.get("schema") != DERIVATIVES_REPLAY_EVENT_SCHEMA:
        raise DerivativesBacktestContractError("event_schema_invalid")
    if payload.get("symbol") != DERIVATIVES_BACKTEST_SYMBOL:
        raise DerivativesBacktestContractError("event_symbol_out_of_v1_scope")
    raw_kind = payload.get("event_type")
    if type(raw_kind) is not str:
        raise DerivativesBacktestContractError("event_type_invalid")
    try:
        kind = DerivativeEventKindV1(raw_kind)
    except ValueError as exc:
        raise DerivativesBacktestContractError("event_type_invalid") from exc
    return ReplayEventHeaderV1(
        event_type=kind,
        ts=require_canonical_utc_timestamp(payload.get("ts"), "event_ts"),
        source_sequence=require_exact_int(
            payload.get("source_sequence"),
            "source_sequence",
            minimum=0,
            maximum=DERIVATIVES_MAX_SOURCE_SEQUENCE,
        ),
        event_id=require_sha256(payload.get("event_id"), "event_id"),
        source_ref=SourceRecordRefV1.from_dict(payload.get("source_ref")),
    )


def parse_derivative_replay_event(
    value: Mapping[str, Any],
) -> DerivativeReplayEventV1:
    if type(value) is not dict:
        raise DerivativesBacktestContractError("event_shape_invalid")
    header = _parse_header(value)
    common = {
        "schema",
        "event_type",
        "symbol",
        "ts",
        "source_sequence",
        "event_id",
        "source_ref",
    }
    kind = header.event_type
    if kind is DerivativeEventKindV1.CONTRACT_TIER_EFFECTIVE:
        require_exact_mapping_keys(
            value,
            common | {"snapshot_refs"},
            "event_shape_invalid",
        )
        return ContractTierEffectiveEventV1(
            header=header,
            snapshot_refs=DerivativesSnapshotRefsV1.from_dict(
                value["snapshot_refs"]
            ),
        )
    if kind in {DerivativeEventKindV1.INDEX_PRICE, DerivativeEventKindV1.MARK_PRICE}:
        require_exact_mapping_keys(value, common | {"price"}, "event_shape_invalid")
        price = parse_canonical_accounting_decimal(value["price"], "price")
        event_class = (
            IndexPriceEventV1
            if kind is DerivativeEventKindV1.INDEX_PRICE
            else MarkPriceEventV1
        )
        return event_class(header=header, price=price)
    if kind is DerivativeEventKindV1.FUNDING_SETTLEMENT:
        require_exact_mapping_keys(
            value,
            common | {"rate", "schedule_ref", "observed_at_ts"},
            "event_shape_invalid",
        )
        return FundingSettlementEventV1(
            header=header,
            rate=parse_canonical_accounting_decimal(value["rate"], "funding_rate"),
            schedule_ref=ImmutableSnapshotRefV1.from_dict(value["schedule_ref"]),
            observed_at_ts=require_canonical_utc_timestamp(
                value["observed_at_ts"],
                "observed_at_ts",
            ),
        )
    if kind is DerivativeEventKindV1.TRADABLE:
        require_exact_mapping_keys(
            value,
            common | {"reference_price", "available_contracts", "liquidity_role"},
            "event_shape_invalid",
        )
        if type(value["liquidity_role"]) is not str:
            raise DerivativesBacktestContractError("liquidity_role_invalid")
        try:
            role = LiquidityRoleV1(value["liquidity_role"])
        except ValueError as exc:
            raise DerivativesBacktestContractError("liquidity_role_invalid") from exc
        return TradableEventV1(
            header=header,
            reference_price=parse_canonical_accounting_decimal(
                value["reference_price"],
                "reference_price",
            ),
            available_contracts=parse_canonical_accounting_decimal(
                value["available_contracts"],
                "available_contracts",
            ),
            liquidity_role=role,
        )
    if kind is DerivativeEventKindV1.BAR_CLOSE:
        require_exact_mapping_keys(
            value,
            common | {"bar_start_ts", "bar_end_ts", "ohlcv", "feature_ref"},
            "event_shape_invalid",
        )
        ohlcv = require_exact_mapping_keys(
            value["ohlcv"],
            {"open", "high", "low", "close", "volume_contracts"},
            "bar_ohlcv_shape_invalid",
        )
        return BarCloseEventV1(
            header=header,
            bar_start_ts=require_canonical_utc_timestamp(
                value["bar_start_ts"],
                "bar_start_ts",
            ),
            bar_end_ts=require_canonical_utc_timestamp(
                value["bar_end_ts"],
                "bar_end_ts",
            ),
            open_price=parse_canonical_accounting_decimal(ohlcv["open"], "open_price"),
            high_price=parse_canonical_accounting_decimal(ohlcv["high"], "high_price"),
            low_price=parse_canonical_accounting_decimal(ohlcv["low"], "low_price"),
            close_price=parse_canonical_accounting_decimal(
                ohlcv["close"],
                "close_price",
            ),
            volume_contracts=parse_canonical_accounting_decimal(
                ohlcv["volume_contracts"],
                "volume_contracts",
            ),
            feature_ref=SourceRecordRefV1.from_dict(value["feature_ref"]),
        )
    raise DerivativesBacktestContractError("event_type_invalid")  # pragma: no cover


__all__ = [
    "DERIVATIVES_EVENT_ORDERING_POLICY_ID",
    "DERIVATIVES_MAX_SOURCE_SEQUENCE",
    "DERIVATIVES_REPLAY_EVENT_SCHEMA",
    "EVENT_PHASE_PRIORITY_V1",
    "EXPECTED_EVENT_STREAM_ID_V1",
    "BAR_FEATURE_STREAM_ID_V1",
    "BarCloseEventV1",
    "ContractTierEffectiveEventV1",
    "DerivativeEventKindV1",
    "DerivativeReplayEventV1",
    "FundingSettlementEventV1",
    "IndexPriceEventV1",
    "MarkPriceEventV1",
    "ReplayEventHeaderV1",
    "SINGLETON_EVENT_KINDS_PER_TIMESTAMP_V1",
    "SourceRecordRefV1",
    "TradableEventV1",
    "event_order_key",
    "parse_derivative_replay_event",
]
