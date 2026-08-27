"""Immutable, time-bounded instrument-contract evidence.

The arithmetic contract describes *how* to interpret exchange quantities.  A
snapshot additionally proves *which* contract definition was observed, where
it came from, and the half-open time window for which that evidence may be
used.  Current public instrument metadata must never be applied
retroactively: an ``observed_forward`` snapshot can only become effective at
or after its observation time.

This module is intentionally side-effect free.  It accepts only the public,
whitelisted contract fields and contains no database, network, settings, or
account dependency.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal, Mapping

from aats.domain.instrument_contract import (
    INSTRUMENT_ARITHMETIC_POLICY_ID,
    InstrumentContract,
    InstrumentContractError,
    instrument_contract_from_metadata,
)
from aats.schemas.exchange import InstrumentMetadata


INSTRUMENT_CONTRACT_SNAPSHOT_SCHEMA = "aats.instrument_contract_snapshot.v1"
SnapshotEvidenceKind = Literal["observed_forward", "authoritative_history"]
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EVIDENCE_KINDS = frozenset({"observed_forward", "authoritative_history"})
INSTRUMENT_OBSERVATION_WINDOW_SCHEMA = "aats-instrument-observation-window-v1"
_MAX_CANONICAL_DECIMAL_DIGITS = 256
_MAX_CANONICAL_DECIMAL_EXPONENT = 256


@dataclass(frozen=True, slots=True)
class InstrumentContractSnapshot:
    """One immutable contract definition with explicit temporal provenance."""

    venue: str
    contract: InstrumentContract
    observed_at: datetime
    effective_from: datetime
    effective_to: datetime | None
    evidence_kind: SnapshotEvidenceKind
    source_locator: str
    source_schema: str
    source_payload_sha256: str

    def __post_init__(self) -> None:
        venue = str(self.venue or "").strip().upper()
        source_locator = str(self.source_locator or "").strip()
        source_schema = str(self.source_schema or "").strip()
        source_payload_sha256 = str(self.source_payload_sha256 or "").strip()
        if not venue:
            raise InstrumentContractError("instrument_snapshot_venue_required")
        if not isinstance(self.contract, InstrumentContract):
            raise InstrumentContractError("instrument_snapshot_contract_invalid")
        if self.evidence_kind not in _EVIDENCE_KINDS:
            raise InstrumentContractError("instrument_snapshot_evidence_kind_invalid")
        if not source_locator or not source_schema:
            raise InstrumentContractError("instrument_snapshot_source_required")
        if not _SHA256.fullmatch(source_payload_sha256):
            raise InstrumentContractError("instrument_snapshot_source_digest_invalid")

        observed_at = _utc(self.observed_at, "instrument_snapshot_observed_at_invalid")
        effective_from = _utc(
            self.effective_from,
            "instrument_snapshot_effective_window_invalid",
        )
        effective_to = (
            None
            if self.effective_to is None
            else _utc(
                self.effective_to,
                "instrument_snapshot_effective_window_invalid",
            )
        )
        if effective_to is not None and effective_to <= effective_from:
            raise InstrumentContractError("instrument_snapshot_effective_window_invalid")
        if self.evidence_kind == "observed_forward":
            if effective_to is not None and effective_to > observed_at:
                raise InstrumentContractError("instrument_snapshot_window_unproven")
            if effective_from < observed_at and (
                source_schema != INSTRUMENT_OBSERVATION_WINDOW_SCHEMA
                or effective_to != observed_at
            ):
                raise InstrumentContractError("instrument_snapshot_window_unproven")

        object.__setattr__(self, "venue", venue)
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "effective_from", effective_from)
        object.__setattr__(self, "effective_to", effective_to)
        object.__setattr__(self, "source_locator", source_locator)
        object.__setattr__(self, "source_schema", source_schema)
        object.__setattr__(self, "source_payload_sha256", source_payload_sha256)

    @property
    def digest(self) -> str:
        """Return the SHA-256 identity of the canonical snapshot payload."""

        return hashlib.sha256(_canonical_json_bytes(self.canonical_payload())).hexdigest()

    def canonical_payload(self) -> dict[str, Any]:
        contract = self.contract
        return {
            "schema": INSTRUMENT_CONTRACT_SNAPSHOT_SCHEMA,
            "arithmetic_policy_id": INSTRUMENT_ARITHMETIC_POLICY_ID,
            "venue": self.venue,
            "instrument": {
                "symbol": contract.symbol,
                "instrument_type": contract.instrument_type,
                "contract_type": contract.contract_type,
                "base_currency": contract.base_currency,
                "quote_currency": contract.quote_currency,
                "settle_currency": contract.settle_currency,
                "contract_value": _canonical_decimal(contract.contract_value),
                "contract_multiplier": _canonical_decimal(
                    contract.contract_multiplier
                ),
                "contract_value_currency": contract.contract_value_currency,
                "lot_size": _canonical_decimal(contract.lot_size),
                "min_size": _canonical_decimal(contract.min_size),
                "tick_size": _canonical_decimal(contract.tick_size),
            },
            "observed_at": _utc_iso(self.observed_at),
            "effective_window": {
                "start": _utc_iso(self.effective_from),
                "end": (
                    None if self.effective_to is None else _utc_iso(self.effective_to)
                ),
            },
            "evidence": {
                "kind": self.evidence_kind,
                "source_locator": self.source_locator,
                "source_schema": self.source_schema,
                "source_payload_sha256": self.source_payload_sha256,
            },
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self.canonical_payload()
        payload["snapshot_digest"] = self.digest
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> InstrumentContractSnapshot:
        """Parse and verify a canonical serialized snapshot."""

        if not isinstance(value, Mapping):
            raise InstrumentContractError("instrument_snapshot_shape_invalid")
        if set(value) != {
            "schema",
            "arithmetic_policy_id",
            "venue",
            "instrument",
            "observed_at",
            "effective_window",
            "evidence",
            "snapshot_digest",
        }:
            raise InstrumentContractError("instrument_snapshot_shape_invalid")
        if value.get("schema") != INSTRUMENT_CONTRACT_SNAPSHOT_SCHEMA:
            raise InstrumentContractError("instrument_snapshot_schema_invalid")
        if value.get("arithmetic_policy_id") != INSTRUMENT_ARITHMETIC_POLICY_ID:
            raise InstrumentContractError("instrument_snapshot_arithmetic_policy_invalid")
        instrument = value.get("instrument")
        window = value.get("effective_window")
        evidence = value.get("evidence")
        if (
            not isinstance(instrument, Mapping)
            or not isinstance(window, Mapping)
            or not isinstance(evidence, Mapping)
            or set(instrument)
            != {
                "symbol",
                "instrument_type",
                "contract_type",
                "base_currency",
                "quote_currency",
                "settle_currency",
                "contract_value",
                "contract_multiplier",
                "contract_value_currency",
                "lot_size",
                "min_size",
                "tick_size",
            }
            or set(window) != {"start", "end"}
            or set(evidence)
            != {
                "kind",
                "source_locator",
                "source_schema",
                "source_payload_sha256",
            }
        ):
            raise InstrumentContractError("instrument_snapshot_shape_invalid")
        try:
            contract = InstrumentContract(
                symbol=str(instrument["symbol"]),
                instrument_type=str(instrument["instrument_type"]),
                contract_type=str(instrument["contract_type"]),  # type: ignore[arg-type]
                base_currency=str(instrument["base_currency"]),
                quote_currency=str(instrument["quote_currency"]),
                settle_currency=str(instrument["settle_currency"]),
                contract_value=Decimal(str(instrument["contract_value"])),
                contract_multiplier=Decimal(str(instrument["contract_multiplier"])),
                contract_value_currency=str(instrument["contract_value_currency"]),
                lot_size=Decimal(str(instrument["lot_size"])),
                min_size=Decimal(str(instrument["min_size"])),
                tick_size=Decimal(str(instrument["tick_size"])),
            )
            snapshot = cls(
                venue=str(value["venue"]),
                contract=contract,
                observed_at=_parse_time(value["observed_at"]),
                effective_from=_parse_time(window["start"]),
                effective_to=(
                    None if window["end"] is None else _parse_time(window["end"])
                ),
                evidence_kind=str(evidence["kind"]),  # type: ignore[arg-type]
                source_locator=str(evidence["source_locator"]),
                source_schema=str(evidence["source_schema"]),
                source_payload_sha256=str(evidence["source_payload_sha256"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, InstrumentContractError):
                raise
            raise InstrumentContractError("instrument_snapshot_shape_invalid") from exc
        observed_digest = str(value.get("snapshot_digest") or "")
        if not _SHA256.fullmatch(observed_digest) or observed_digest != snapshot.digest:
            raise InstrumentContractError("instrument_snapshot_digest_mismatch")
        return snapshot

    def validate_window(
        self,
        *,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> None:
        """Fail closed unless this snapshot proves the complete half-open window."""

        normalized_symbol = str(symbol or "").strip().upper()
        if normalized_symbol != self.contract.symbol:
            raise InstrumentContractError("instrument_snapshot_symbol_mismatch")
        window_start = _utc(start, "instrument_snapshot_effective_window_invalid")
        window_end = _utc(end, "instrument_snapshot_effective_window_invalid")
        if window_end <= window_start:
            raise InstrumentContractError("instrument_snapshot_effective_window_invalid")
        if self.effective_from > window_start or (
            self.effective_to is not None and window_end > self.effective_to
        ):
            raise InstrumentContractError("instrument_snapshot_window_unproven")
        if self.evidence_kind == "observed_forward" and window_end > self.observed_at:
            raise InstrumentContractError("instrument_snapshot_window_unproven")


def instrument_contract_snapshot_from_metadata(
    instrument: InstrumentMetadata,
    *,
    venue: str,
    observed_at: datetime,
    source_locator: str,
    source_schema: str,
    evidence_kind: SnapshotEvidenceKind = "observed_forward",
    effective_from: datetime | None = None,
    effective_to: datetime | None = None,
    source_payload_sha256: str | None = None,
) -> InstrumentContractSnapshot:
    """Build a snapshot from whitelisted, already parsed public metadata.

    ``effective_from`` defaults to ``observed_at``.  The legacy implementation
    does not treat the ``authoritative_history`` label as independent proof, so
    a locally supplied snapshot still cannot claim an earlier window.
    """

    contract = instrument_contract_from_metadata(instrument)
    observed = _utc(observed_at, "instrument_snapshot_observed_at_invalid")
    if (
        evidence_kind == "observed_forward"
        and effective_from is not None
        and _utc(effective_from, "instrument_snapshot_effective_window_invalid")
        != observed
    ):
        raise InstrumentContractError("instrument_snapshot_window_unproven")
    effective = observed if effective_from is None else effective_from
    expiry = effective_to
    payload_digest = source_payload_sha256 or hashlib.sha256(
        _canonical_json_bytes(_metadata_source_payload(instrument))
    ).hexdigest()
    return InstrumentContractSnapshot(
        venue=venue,
        contract=contract,
        observed_at=observed,
        effective_from=effective,
        effective_to=expiry,
        evidence_kind=evidence_kind,
        source_locator=source_locator,
        source_schema=source_schema,
        source_payload_sha256=payload_digest,
    )


def instrument_contract_observation_window_from_metadata(
    instrument: InstrumentMetadata,
    *,
    venue: str,
    first_observed_at: datetime,
    last_observed_at: datetime,
    observation_evidence_sha256: str,
    source_locator: str,
) -> InstrumentContractSnapshot:
    """Seal an already captured interval of identical public observations.

    The caller must provide the aggregate digest produced by the prospective
    capture evidence.  This function does not fetch, backdate, or infer that
    evidence.
    """

    first = _utc(
        first_observed_at,
        "instrument_snapshot_effective_window_invalid",
    )
    last = _utc(last_observed_at, "instrument_snapshot_observed_at_invalid")
    if last <= first:
        raise InstrumentContractError("instrument_snapshot_effective_window_invalid")
    return InstrumentContractSnapshot(
        venue=venue,
        contract=instrument_contract_from_metadata(instrument),
        observed_at=last,
        effective_from=first,
        effective_to=last,
        evidence_kind="observed_forward",
        source_locator=source_locator,
        source_schema=INSTRUMENT_OBSERVATION_WINDOW_SCHEMA,
        source_payload_sha256=observation_evidence_sha256,
    )


def parse_instrument_contract_snapshot(
    value: InstrumentContractSnapshot | Mapping[str, Any],
) -> InstrumentContractSnapshot:
    if isinstance(value, InstrumentContractSnapshot):
        return value
    return InstrumentContractSnapshot.from_dict(value)


def _metadata_source_payload(instrument: InstrumentMetadata) -> dict[str, Any]:
    """Return the public whitelist hashed as source observation evidence."""

    fields = (
        "instrument_id",
        "symbol",
        "base_currency",
        "quote_currency",
        "lot_size",
        "tick_size",
        "min_size",
        "contract_value",
        "contract_multiplier",
        "contract_type",
        "instrument_type",
        "instrument_family",
        "underlying",
        "settle_currency",
        "contract_value_currency",
        "list_ts",
        "expiry_ts",
        "state",
    )
    payload: dict[str, Any] = {}
    for field_name in fields:
        value = getattr(instrument, field_name)
        if isinstance(value, Decimal):
            payload[field_name] = _canonical_decimal(value)
        elif isinstance(value, datetime):
            payload[field_name] = _utc_iso(value)
        else:
            payload[field_name] = value
    return payload


def _canonical_decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise InstrumentContractError("instrument_snapshot_decimal_invalid")

    sign, digits, exponent = value.as_tuple()
    if (
        len(digits) > _MAX_CANONICAL_DECIMAL_DIGITS
        or abs(exponent) > _MAX_CANONICAL_DECIMAL_EXPONENT
    ):
        raise InstrumentContractError("instrument_snapshot_decimal_invalid")
    coefficient = "".join(str(digit) for digit in digits) or "0"
    if exponent >= 0:
        integer = coefficient + ("0" * exponent)
        fraction = ""
    else:
        decimal_point = len(coefficient) + exponent
        if decimal_point <= 0:
            integer = "0"
            fraction = ("0" * -decimal_point) + coefficient
        else:
            integer = coefficient[:decimal_point]
            fraction = coefficient[decimal_point:]

    integer = integer.lstrip("0") or "0"
    fraction = fraction.rstrip("0")
    if integer == "0" and not fraction:
        return "0"
    rendered = integer if not fraction else f"{integer}.{fraction}"
    return f"-{rendered}" if sign else rendered


def _canonical_json_bytes(value: Any) -> bytes:
    import json

    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _utc(value: datetime, reason: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise InstrumentContractError(reason)
    return value.astimezone(timezone.utc)


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise InstrumentContractError("instrument_snapshot_shape_invalid")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InstrumentContractError("instrument_snapshot_shape_invalid") from exc


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "INSTRUMENT_CONTRACT_SNAPSHOT_SCHEMA",
    "INSTRUMENT_OBSERVATION_WINDOW_SCHEMA",
    "InstrumentContractSnapshot",
    "SnapshotEvidenceKind",
    "instrument_contract_snapshot_from_metadata",
    "instrument_contract_observation_window_from_metadata",
    "parse_instrument_contract_snapshot",
]
