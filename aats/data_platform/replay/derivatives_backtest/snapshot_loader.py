"""Stable raw-byte loader for non-promotable LF-B1.2 snapshot fixtures.

The current Legacy registry cannot prove immutable historical authority.  This
module therefore accepts only an explicit ``synthetic_test_only`` authority
and returns a result that is permanently ineligible for capital promotion.
It still enforces the complete byte, semantic, path, scope, and effective-time
boundary so the engine contract can be tested without inventing provenance.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping

from aats.data_platform.governance.research_artifact_contract import (
    decode_strict_json_artifact,
    read_stable_regular_artifact_file,
)
from aats.data_platform.governance.typed_json_identity import (
    canonical_typed_json_bytes,
    typed_json_sha256,
)
from aats.domain.instrument_contract import InstrumentContractError
from aats.domain.instrument_contract_snapshot import InstrumentContractSnapshot

from .contracts import (
    DERIVATIVES_BACKTEST_SYMBOL,
    DerivativesBacktestContractError,
    ExecutionFeeScheduleV1,
    FundingRateScheduleV1,
    LinearPerpetualContractV1,
    PositionTierV1,
    canonical_accounting_decimal,
    parse_canonical_accounting_decimal,
)
from .snapshot_refs import (
    DERIVATIVES_SNAPSHOT_MAX_BYTES,
    DerivativesSnapshotRefsV1,
    ImmutableSnapshotRefV1,
    SnapshotKindV1,
)
from .wire import (
    require_canonical_utc_timestamp,
    require_canonical_uuid,
    require_exact_int,
    require_exact_mapping_keys,
    require_identifier,
    require_sha256,
    require_utc_datetime,
)


DERIVATIVES_SNAPSHOT_ENVELOPE_SCHEMA = "derivatives-snapshot-envelope/v1"
DERIVATIVES_SNAPSHOT_AUTHORITY_SYNTHETIC = "synthetic_test_only"
DERIVATIVES_SNAPSHOT_MAX_DIRECTORY_ENTRIES = 10_000
_SNAPSHOT_PAYLOAD_SCHEMA_BY_KIND = MappingProxyType(
    {
        SnapshotKindV1.INSTRUMENT: "derivatives-instrument-snapshot-payload/v1",
        SnapshotKindV1.POSITION_TIER: "derivatives-position-tier-snapshot-payload/v1",
        SnapshotKindV1.EXECUTION_FEE: "derivatives-execution-fee-snapshot-payload/v1",
        SnapshotKindV1.FUNDING_SCHEDULE: "derivatives-funding-schedule-snapshot-payload/v1",
    }
)


@dataclass(frozen=True, slots=True)
class ResolvedFundingScheduleV1:
    schedule: FundingRateScheduleV1
    cadence: timedelta
    settlement_anchor_ts: datetime

    def __post_init__(self) -> None:
        if type(self.schedule) is not FundingRateScheduleV1:
            raise DerivativesBacktestContractError("funding_schedule_invalid")
        if (
            type(self.cadence) is not timedelta
            or self.cadence < timedelta(minutes=1)
            or self.cadence > timedelta(days=7)
            or self.cadence.microseconds != 0
        ):
            raise DerivativesBacktestContractError("funding_cadence_invalid")
        # The canonical timestamp parser is the only constructor used by the
        # loader; formatting here proves the object remains UTC-aware.
        from .wire import canonical_utc_timestamp

        canonical_utc_timestamp(self.settlement_anchor_ts, "settlement_anchor_ts")


@dataclass(frozen=True, slots=True)
class LoadedSnapshotArtifactV1:
    ref: ImmutableSnapshotRefV1
    raw_bytes: bytes

    def __post_init__(self) -> None:
        if type(self.ref) is not ImmutableSnapshotRefV1:
            raise DerivativesBacktestContractError("snapshot_ref_invalid")
        if type(self.raw_bytes) is not bytes or not self.raw_bytes:
            raise DerivativesBacktestContractError("snapshot_raw_bytes_invalid")
        try:
            validated_ref = ImmutableSnapshotRefV1.from_dict(self.ref.to_dict())
        except DerivativesBacktestContractError:
            raise
        except (AttributeError, KeyError, OverflowError, TypeError, ValueError) as exc:
            raise DerivativesBacktestContractError(
                "snapshot_ref_revalidation_failed"
            ) from exc
        if validated_ref != self.ref:
            raise DerivativesBacktestContractError(
                "snapshot_ref_revalidation_mismatch"
            )
        _validated_envelope_from_bytes(validated_ref, self.raw_bytes)
        object.__setattr__(self, "ref", validated_ref)


@dataclass(frozen=True, slots=True)
class LoadedDerivativesSnapshotSetV1:
    refs: DerivativesSnapshotRefsV1
    replay_start_ts: datetime
    replay_end_ts: datetime
    instrument_contract: LinearPerpetualContractV1
    position_tier: PositionTierV1
    execution_fee: ExecutionFeeScheduleV1
    funding_schedule: ResolvedFundingScheduleV1
    artifacts: tuple[LoadedSnapshotArtifactV1, ...]
    snapshot_set_fingerprint: str
    authority_status: str = DERIVATIVES_SNAPSHOT_AUTHORITY_SYNTHETIC
    capital_promotion_eligible: bool = False

    def __post_init__(self) -> None:
        if type(self.refs) is not DerivativesSnapshotRefsV1:
            raise DerivativesBacktestContractError("snapshot_set_invalid")
        try:
            validated_refs = DerivativesSnapshotRefsV1.from_dict(
                self.refs.to_dict()
            )
        except DerivativesBacktestContractError:
            raise
        except (AttributeError, KeyError, OverflowError, TypeError, ValueError) as exc:
            raise DerivativesBacktestContractError(
                "snapshot_set_revalidation_failed"
            ) from exc
        if validated_refs != self.refs:
            raise DerivativesBacktestContractError(
                "snapshot_set_revalidation_mismatch"
            )
        start_ts = require_utc_datetime(self.replay_start_ts, "start_ts")
        end_ts = require_utc_datetime(self.replay_end_ts, "end_ts")
        validated_refs.validate_window(start=start_ts, end=end_ts)
        if type(self.instrument_contract) is not LinearPerpetualContractV1:
            raise DerivativesBacktestContractError("instrument_contract_invalid")
        if type(self.position_tier) is not PositionTierV1:
            raise DerivativesBacktestContractError("position_tier_invalid")
        if type(self.execution_fee) is not ExecutionFeeScheduleV1:
            raise DerivativesBacktestContractError("execution_fee_schedule_invalid")
        if type(self.funding_schedule) is not ResolvedFundingScheduleV1:
            raise DerivativesBacktestContractError("funding_schedule_invalid")
        if (
            type(self.artifacts) is not tuple
            or len(self.artifacts) != 4
            or any(type(artifact) is not LoadedSnapshotArtifactV1 for artifact in self.artifacts)
        ):
            raise DerivativesBacktestContractError("snapshot_artifact_set_invalid")
        validated_artifacts = tuple(
            LoadedSnapshotArtifactV1(
                ref=artifact.ref,
                raw_bytes=artifact.raw_bytes,
            )
            for artifact in self.artifacts
        )
        if tuple(artifact.ref for artifact in validated_artifacts) != (
            validated_refs.instrument,
            validated_refs.position_tier,
            validated_refs.execution_fee,
            validated_refs.funding_schedule,
        ):
            raise DerivativesBacktestContractError("snapshot_artifact_set_mismatch")
        require_sha256(self.snapshot_set_fingerprint, "snapshot_set_fingerprint")
        if self.snapshot_set_fingerprint != validated_refs.fingerprint:
            raise DerivativesBacktestContractError("snapshot_set_fingerprint_mismatch")
        expected_values = (
            _load_instrument(
                validated_artifacts[0],
                start_ts=start_ts,
                end_ts=end_ts,
            ),
            _load_position_tier(validated_artifacts[1]),
            _load_execution_fee(validated_artifacts[2]),
            _load_funding_schedule(validated_artifacts[3]),
        )
        if expected_values != (
            self.instrument_contract,
            self.position_tier,
            self.execution_fee,
            self.funding_schedule,
        ):
            raise DerivativesBacktestContractError(
                "snapshot_derived_contract_mismatch"
            )
        object.__setattr__(self, "refs", validated_refs)
        object.__setattr__(self, "artifacts", validated_artifacts)
        object.__setattr__(self, "instrument_contract", expected_values[0])
        object.__setattr__(self, "position_tier", expected_values[1])
        object.__setattr__(self, "execution_fee", expected_values[2])
        object.__setattr__(self, "funding_schedule", expected_values[3])
        if self.authority_status != DERIVATIVES_SNAPSHOT_AUTHORITY_SYNTHETIC:
            raise DerivativesBacktestContractError("snapshot_authority_invalid")
        if self.capital_promotion_eligible is not False:
            raise DerivativesBacktestContractError(
                "synthetic_snapshot_cannot_be_promotable"
            )


@dataclass(frozen=True, slots=True)
class _SnapshotEnvelopeV1:
    kind: SnapshotKindV1
    payload_schema: str
    snapshot_id: str
    source_registry_id: str
    source_seal_fingerprint: str
    source_schema: str
    effective_from: datetime
    effective_to: datetime | None
    authority_status: str
    payload: Mapping[str, Any]


def _parse_envelope(value: Mapping[str, Any]) -> _SnapshotEnvelopeV1:
    envelope = require_exact_mapping_keys(
        value,
        {
            "schema",
            "kind",
            "payload_schema",
            "venue",
            "symbol",
            "instrument_type",
            "contract_type",
            "settle_currency",
            "margin_mode",
            "position_mode",
            "snapshot_id",
            "source_registry_id",
            "source_seal_fingerprint",
            "source_schema",
            "effective_window",
            "authority_status",
            "payload",
        },
        "snapshot_envelope_shape_invalid",
    )
    if envelope["schema"] != DERIVATIVES_SNAPSHOT_ENVELOPE_SCHEMA:
        raise DerivativesBacktestContractError("snapshot_envelope_schema_invalid")
    if type(envelope["kind"]) is not str:
        raise DerivativesBacktestContractError("snapshot_kind_invalid")
    try:
        kind = SnapshotKindV1(envelope["kind"])
    except ValueError as exc:
        raise DerivativesBacktestContractError("snapshot_kind_invalid") from exc
    if (
        envelope["payload_schema"] != _SNAPSHOT_PAYLOAD_SCHEMA_BY_KIND[kind]
        or envelope["venue"] != "OKX"
        or envelope["symbol"] != DERIVATIVES_BACKTEST_SYMBOL
        or envelope["instrument_type"] != "SWAP"
        or envelope["contract_type"] != "linear"
        or envelope["settle_currency"] != "USDT"
        or envelope["margin_mode"] != "isolated"
        or envelope["position_mode"] != "single_position"
    ):
        raise DerivativesBacktestContractError("snapshot_scope_out_of_v1_scope")
    window = require_exact_mapping_keys(
        envelope["effective_window"],
        {"start", "end"},
        "snapshot_effective_window_invalid",
    )
    if envelope["authority_status"] != DERIVATIVES_SNAPSHOT_AUTHORITY_SYNTHETIC:
        raise DerivativesBacktestContractError(
            "snapshot_managed_source_seal_verifier_unavailable"
        )
    if type(envelope["source_schema"]) is not str or not envelope["source_schema"]:
        raise DerivativesBacktestContractError("snapshot_source_schema_invalid")
    if type(envelope["payload"]) is not dict:
        raise DerivativesBacktestContractError("snapshot_payload_invalid")
    return _SnapshotEnvelopeV1(
        kind=kind,
        payload_schema=envelope["payload_schema"],
        snapshot_id=require_canonical_uuid(envelope["snapshot_id"], "snapshot_id"),
        source_registry_id=require_canonical_uuid(
            envelope["source_registry_id"],
            "source_registry_id",
        ),
        source_seal_fingerprint=require_sha256(
            envelope["source_seal_fingerprint"],
            "source_seal_fingerprint",
        ),
        source_schema=envelope["source_schema"],
        effective_from=require_canonical_utc_timestamp(
            window["start"],
            "effective_from",
        ),
        effective_to=(
            None
            if window["end"] is None
            else require_canonical_utc_timestamp(window["end"], "effective_to")
        ),
        authority_status=envelope["authority_status"],
        payload=envelope["payload"],
    )


def _match_ref(
    ref: ImmutableSnapshotRefV1,
    envelope: _SnapshotEnvelopeV1,
    raw_bytes: bytes,
    decoded: Mapping[str, Any],
) -> None:
    if (
        envelope.kind is not ref.kind
        or envelope.snapshot_id != ref.snapshot_id
        or envelope.source_registry_id != ref.source_registry_id
        or envelope.source_seal_fingerprint != ref.source_seal_fingerprint
        or envelope.source_schema != ref.source_schema
        or envelope.effective_from != ref.effective_from
        or envelope.effective_to != ref.effective_to
    ):
        raise DerivativesBacktestContractError("snapshot_ref_payload_mismatch")
    if len(raw_bytes) != ref.size_bytes:
        raise DerivativesBacktestContractError("snapshot_size_mismatch")
    if hashlib.sha256(raw_bytes).hexdigest() != ref.raw_sha256:
        raise DerivativesBacktestContractError("snapshot_raw_digest_mismatch")
    if typed_json_sha256(decoded) != ref.semantic_sha256:
        raise DerivativesBacktestContractError("snapshot_semantic_digest_mismatch")


def _validated_envelope_from_bytes(
    ref: ImmutableSnapshotRefV1,
    raw_bytes: bytes,
) -> _SnapshotEnvelopeV1:
    if len(raw_bytes) != ref.size_bytes:
        raise DerivativesBacktestContractError("snapshot_size_mismatch")
    if hashlib.sha256(raw_bytes).hexdigest() != ref.raw_sha256:
        raise DerivativesBacktestContractError("snapshot_raw_digest_mismatch")
    try:
        decoded = decode_strict_json_artifact(raw_bytes, expected_type=dict)
        canonical = canonical_typed_json_bytes(decoded)
    except ValueError as exc:
        raise DerivativesBacktestContractError("snapshot_json_invalid") from exc
    if raw_bytes != canonical:
        raise DerivativesBacktestContractError("snapshot_bytes_non_canonical")
    envelope = _parse_envelope(decoded)
    _match_ref(ref, envelope, raw_bytes, decoded)
    return envelope


def _validate_root(root: Path) -> tuple[Path, tuple[int, int, int]]:
    if not isinstance(root, Path) or not root.is_absolute() or root.is_symlink():
        raise DerivativesBacktestContractError("snapshot_root_invalid")
    try:
        root_stat = root.lstat()
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise DerivativesBacktestContractError("snapshot_root_invalid") from exc
    if not stat.S_ISDIR(root_stat.st_mode) or resolved != root:
        raise DerivativesBacktestContractError("snapshot_root_invalid")
    return resolved, (root_stat.st_dev, root_stat.st_ino, root_stat.st_mode)


def _read_ref(root: Path, ref: ImmutableSnapshotRefV1) -> LoadedSnapshotArtifactV1:
    relative = PurePosixPath(ref.relative_path)
    path = _resolve_exact_case_path(root, relative)
    parent = path.parent
    try:
        resolved_parent = parent.resolve(strict=True)
    except OSError as exc:
        raise DerivativesBacktestContractError("snapshot_path_invalid") from exc
    if not resolved_parent.is_relative_to(root):
        raise DerivativesBacktestContractError("snapshot_path_invalid")
    cursor = root
    for part in relative.parts[:-1]:
        cursor = cursor / part
        if cursor.is_symlink():
            raise DerivativesBacktestContractError("snapshot_path_invalid")
    try:
        raw_bytes = read_stable_regular_artifact_file(
            path,
            parent=resolved_parent,
            max_bytes=DERIVATIVES_SNAPSHOT_MAX_BYTES,
        )
    except ValueError as exc:
        raise DerivativesBacktestContractError("snapshot_stable_read_failed") from exc
    return LoadedSnapshotArtifactV1(
        ref=ref,
        raw_bytes=raw_bytes,
    )


def _resolve_exact_case_path(root: Path, relative: PurePosixPath) -> Path:
    """Resolve every segment with cross-platform, case-exact semantics."""

    cursor = root
    total_entries_scanned = 0
    final_index = len(relative.parts) - 1
    for part_index, expected_part in enumerate(relative.parts):
        matches: list[tuple[str, os.stat_result]] = []
        try:
            with os.scandir(cursor) as entries:
                for entry in entries:
                    total_entries_scanned += 1
                    if total_entries_scanned > DERIVATIVES_SNAPSHOT_MAX_DIRECTORY_ENTRIES:
                        raise DerivativesBacktestContractError(
                            "snapshot_directory_entry_limit_exceeded"
                        )
                    if entry.name.casefold() == expected_part.casefold():
                        entry_stat = entry.stat(follow_symlinks=False)
                        file_attributes = getattr(entry_stat, "st_file_attributes", 0)
                        is_reparse = bool(file_attributes & 0x400)
                        is_junction = bool(
                            getattr(entry, "is_junction", lambda: False)()
                        )
                        if entry.is_symlink() or is_reparse or is_junction:
                            raise DerivativesBacktestContractError(
                                "snapshot_path_invalid"
                            )
                        if part_index < final_index and not stat.S_ISDIR(
                            entry_stat.st_mode
                        ):
                            raise DerivativesBacktestContractError(
                                "snapshot_path_invalid"
                            )
                        if part_index == final_index and not stat.S_ISREG(
                            entry_stat.st_mode
                        ):
                            raise DerivativesBacktestContractError(
                                "snapshot_path_invalid"
                            )
                        matches.append((entry.name, entry_stat))
        except OSError as exc:
            raise DerivativesBacktestContractError("snapshot_path_invalid") from exc
        names = [name for name, _entry_stat in matches]
        if names != [expected_part]:
            if matches:
                raise DerivativesBacktestContractError(
                    "snapshot_path_case_mismatch"
                )
            raise DerivativesBacktestContractError("snapshot_path_invalid")
        next_cursor = cursor / expected_part
        try:
            resolved_next = next_cursor.resolve(strict=True)
        except OSError as exc:
            raise DerivativesBacktestContractError("snapshot_path_invalid") from exc
        if not resolved_next.is_relative_to(root):
            raise DerivativesBacktestContractError("snapshot_path_invalid")
        cursor = next_cursor
    return cursor


def _payload(artifact: LoadedSnapshotArtifactV1) -> Mapping[str, Any]:
    return _validated_envelope_from_bytes(
        artifact.ref,
        artifact.raw_bytes,
    ).payload


def _load_instrument(
    artifact: LoadedSnapshotArtifactV1,
    *,
    start_ts: datetime,
    end_ts: datetime,
) -> LinearPerpetualContractV1:
    payload = _payload(artifact)
    try:
        snapshot = InstrumentContractSnapshot.from_dict(payload)
    except InstrumentContractError as exc:
        raise DerivativesBacktestContractError("instrument_snapshot_invalid") from exc
    # Exact round-trip prevents the shared compatibility parser from
    # normalizing JSON numbers, alternate Decimal strings, or offset times.
    if snapshot.to_dict() != payload:
        raise DerivativesBacktestContractError("instrument_snapshot_non_canonical")
    try:
        snapshot.validate_window(
            symbol=DERIVATIVES_BACKTEST_SYMBOL,
            start=start_ts,
            end=end_ts,
        )
    except InstrumentContractError as exc:
        raise DerivativesBacktestContractError(
            "instrument_snapshot_effective_window_unproven"
        ) from exc
    ref = artifact.ref
    if (
        snapshot.venue != "OKX"
        or snapshot.contract.symbol != DERIVATIVES_BACKTEST_SYMBOL
        or snapshot.effective_from != ref.effective_from
        or snapshot.effective_to != ref.effective_to
        or snapshot.source_schema != ref.source_schema
    ):
        raise DerivativesBacktestContractError("instrument_snapshot_scope_mismatch")
    contract = snapshot.contract
    return LinearPerpetualContractV1(
        contract_value=canonical_accounting_decimal(
            contract.contract_value,
            "contract_value",
        ),
        contract_multiplier=canonical_accounting_decimal(
            contract.contract_multiplier,
            "contract_multiplier",
        ),
        lot_size=canonical_accounting_decimal(contract.lot_size, "lot_size"),
        min_size=canonical_accounting_decimal(contract.min_size, "min_size"),
        tick_size=canonical_accounting_decimal(contract.tick_size, "tick_size"),
    )


def _load_position_tier(artifact: LoadedSnapshotArtifactV1) -> PositionTierV1:
    payload = require_exact_mapping_keys(
        _payload(artifact),
        {
            "tier_id",
            "minimum_notional_inclusive",
            "maximum_notional_inclusive",
            "maximum_leverage",
            "maintenance_margin_rate",
            "maintenance_margin_deduction",
            "liquidation_fee_rate",
        },
        "position_tier_snapshot_shape_invalid",
    )
    return PositionTierV1(
        tier_id=require_exact_int(payload["tier_id"], "tier_id", minimum=1),
        minimum_notional_inclusive=parse_canonical_accounting_decimal(
            payload["minimum_notional_inclusive"],
            "minimum_notional_inclusive",
        ),
        maximum_notional_inclusive=parse_canonical_accounting_decimal(
            payload["maximum_notional_inclusive"],
            "maximum_notional_inclusive",
        ),
        maximum_leverage=parse_canonical_accounting_decimal(
            payload["maximum_leverage"],
            "maximum_leverage",
        ),
        maintenance_margin_rate=parse_canonical_accounting_decimal(
            payload["maintenance_margin_rate"],
            "maintenance_margin_rate",
        ),
        maintenance_margin_deduction=parse_canonical_accounting_decimal(
            payload["maintenance_margin_deduction"],
            "maintenance_margin_deduction",
        ),
        liquidation_fee_rate=parse_canonical_accounting_decimal(
            payload["liquidation_fee_rate"],
            "liquidation_fee_rate",
        ),
    )


def _load_execution_fee(artifact: LoadedSnapshotArtifactV1) -> ExecutionFeeScheduleV1:
    payload = require_exact_mapping_keys(
        _payload(artifact),
        {
            "account_fee_tier_id",
            "maker_fee_rate",
            "taker_fee_rate",
            "fee_asset",
        },
        "execution_fee_snapshot_shape_invalid",
    )
    require_identifier(payload["account_fee_tier_id"], "account_fee_tier_id")
    return ExecutionFeeScheduleV1(
        maker_fee_rate=parse_canonical_accounting_decimal(
            payload["maker_fee_rate"],
            "maker_fee_rate",
        ),
        taker_fee_rate=parse_canonical_accounting_decimal(
            payload["taker_fee_rate"],
            "taker_fee_rate",
        ),
        fee_asset=payload["fee_asset"],
    )


def _load_funding_schedule(
    artifact: LoadedSnapshotArtifactV1,
) -> ResolvedFundingScheduleV1:
    payload = require_exact_mapping_keys(
        _payload(artifact),
        {
            "minimum_rate_inclusive",
            "maximum_rate_inclusive",
            "schedule_id",
            "cadence_seconds",
            "settlement_anchor_ts",
        },
        "funding_schedule_snapshot_shape_invalid",
    )
    cadence_seconds = require_exact_int(
        payload["cadence_seconds"],
        "cadence_seconds",
        minimum=60,
        maximum=7 * 24 * 60 * 60,
    )
    schedule_id = require_canonical_uuid(payload["schedule_id"], "schedule_id")
    if schedule_id != artifact.ref.snapshot_id:
        raise DerivativesBacktestContractError("funding_schedule_id_mismatch")
    return ResolvedFundingScheduleV1(
        schedule=FundingRateScheduleV1(
            minimum_rate_inclusive=parse_canonical_accounting_decimal(
                payload["minimum_rate_inclusive"],
                "minimum_funding_rate",
            ),
            maximum_rate_inclusive=parse_canonical_accounting_decimal(
                payload["maximum_rate_inclusive"],
                "maximum_funding_rate",
            ),
        ),
        cadence=timedelta(seconds=cadence_seconds),
        settlement_anchor_ts=require_canonical_utc_timestamp(
            payload["settlement_anchor_ts"],
            "settlement_anchor_ts",
        ),
    )


def load_non_promotable_derivatives_snapshot_set(
    refs: DerivativesSnapshotRefsV1,
    *,
    snapshot_root: Path,
    start_ts: datetime,
    end_ts: datetime,
) -> LoadedDerivativesSnapshotSetV1:
    """Load a byte-exact synthetic set; never authorize capital promotion."""

    if type(refs) is not DerivativesSnapshotRefsV1:
        raise DerivativesBacktestContractError("snapshot_set_invalid")
    try:
        validated_refs = DerivativesSnapshotRefsV1.from_dict(refs.to_dict())
    except DerivativesBacktestContractError:
        raise
    except (AttributeError, KeyError, OverflowError, TypeError, ValueError) as exc:
        raise DerivativesBacktestContractError(
            "snapshot_set_revalidation_failed"
        ) from exc
    if validated_refs != refs:
        raise DerivativesBacktestContractError(
            "snapshot_set_revalidation_mismatch"
        )
    refs = validated_refs
    refs.validate_window(start=start_ts, end=end_ts)
    root, root_identity = _validate_root(snapshot_root)
    ordered_refs = (
        refs.instrument,
        refs.position_tier,
        refs.execution_fee,
        refs.funding_schedule,
    )
    artifacts = tuple(_read_ref(root, ref) for ref in ordered_refs)
    try:
        after = root.lstat()
    except OSError as exc:
        raise DerivativesBacktestContractError("snapshot_root_changed") from exc
    if (after.st_dev, after.st_ino, after.st_mode) != root_identity:
        raise DerivativesBacktestContractError("snapshot_root_changed")
    return LoadedDerivativesSnapshotSetV1(
        refs=refs,
        replay_start_ts=start_ts,
        replay_end_ts=end_ts,
        instrument_contract=_load_instrument(
            artifacts[0],
            start_ts=start_ts,
            end_ts=end_ts,
        ),
        position_tier=_load_position_tier(artifacts[1]),
        execution_fee=_load_execution_fee(artifacts[2]),
        funding_schedule=_load_funding_schedule(artifacts[3]),
        artifacts=artifacts,
        snapshot_set_fingerprint=refs.fingerprint,
    )


__all__ = [
    "DERIVATIVES_SNAPSHOT_AUTHORITY_SYNTHETIC",
    "DERIVATIVES_SNAPSHOT_ENVELOPE_SCHEMA",
    "DERIVATIVES_SNAPSHOT_MAX_DIRECTORY_ENTRIES",
    "LoadedDerivativesSnapshotSetV1",
    "ResolvedFundingScheduleV1",
    "load_non_promotable_derivatives_snapshot_set",
]
