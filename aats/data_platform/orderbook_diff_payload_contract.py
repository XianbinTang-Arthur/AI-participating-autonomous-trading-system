"""Contract for future local orderbook diff payload persistence.

This module is intentionally side-effect free. It defines the minimum evidence
contract that a later runtime-affecting collector task must satisfy before AATS
can claim full local orderbook diff payload or collector-sequence truth.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from typing import Any

ORDERBOOK_DIFF_PAYLOAD_CONTRACT_VERSION = "orderbook_diff_payload_contract_v1"
ORDERBOOK_DIFF_PAYLOAD_SCHEMA_VERSION = "orderbook_diff_payload_v1"
ORDERBOOK_ROW_CHECKSUM_VERSION = "orderbook_row_v1"

ORDERBOOK_DIFF_PAYLOAD_TABLE = "bronze.market_orderbook_payloads"
COLLECTOR_SEQUENCE_SCOPE = "per_ingest_run_symbol_channel"

SUPPORTED_SNAPSHOT_TABLES = frozenset({
    "bronze.market_orderbook_bbo",
    "bronze.market_orderbook_books5",
})

ORDERBOOK_BBO_CONTENT_FIELDS = (
    "bid_px",
    "bid_sz",
    "ask_px",
    "ask_sz",
)

ORDERBOOK_BOOKS5_CONTENT_FIELDS = (
    "bid_px_1",
    "bid_sz_1",
    "bid_px_2",
    "bid_sz_2",
    "bid_px_3",
    "bid_sz_3",
    "bid_px_4",
    "bid_sz_4",
    "bid_px_5",
    "bid_sz_5",
    "ask_px_1",
    "ask_sz_1",
    "ask_px_2",
    "ask_sz_2",
    "ask_px_3",
    "ask_sz_3",
    "ask_px_4",
    "ask_sz_4",
    "ask_px_5",
    "ask_sz_5",
)

ORDERBOOK_ROW_CONTENT_FIELDS = {
    "bronze.market_orderbook_bbo": ORDERBOOK_BBO_CONTENT_FIELDS,
    "bronze.market_orderbook_books5": ORDERBOOK_BOOKS5_CONTENT_FIELDS,
}

CAPTURE_STATUS_SNAPSHOT_ONLY = "snapshot_only_diff_payload_missing"
CAPTURE_STATUS_DIFF_PERSISTED = "diff_payload_persisted"
CAPTURE_STATUS_DIFF_UNAVAILABLE = "diff_payload_unavailable"

CAPTURE_STATUSES = frozenset({
    CAPTURE_STATUS_SNAPSHOT_ONLY,
    CAPTURE_STATUS_DIFF_PERSISTED,
    CAPTURE_STATUS_DIFF_UNAVAILABLE,
})

BASE_REQUIRED_FIELDS = (
    "storage_table",
    "snapshot_table",
    "symbol",
    "ts",
    "source_ts",
    "collector_sequence",
    "collector_sequence_scope",
    "row_checksum",
    "checksum_version",
    "capture_status",
    "ingest_run_id",
    "received_at",
)

DIFF_PAYLOAD_REQUIRED_FIELDS = (
    "payload_hash",
    "payload_schema_version",
    "payload_kind",
    "raw_payload",
)

OPTIONAL_FIELDS = (
    "exchange_sequence_id",
    "previous_payload_hash",
    "channel",
    "capture_reason",
    "missing_evidence",
)

READ_PROJECTION_FIELDS = (
    "storage_table",
    "snapshot_table",
    "symbol",
    "ts",
    "source_ts",
    "collector_sequence",
    "collector_sequence_scope",
    "row_checksum",
    "checksum_version",
    "capture_status",
    "payload_hash",
    "payload_schema_version",
    "payload_kind",
    "exchange_sequence_id",
    "previous_payload_hash",
    "ingest_run_id",
    "received_at",
)

_SENSITIVE_KEY_FRAGMENTS = (
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "cookie",
    "database_url",
    "db_url",
    "dsn",
    "passphrase",
    "password",
    "secret",
    "token",
)


@dataclass(frozen=True, slots=True)
class ContractValidation:
    """Validation result for a prospective orderbook payload record."""

    ok: bool
    missing_fields: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def orderbook_diff_payload_contract_spec() -> dict[str, Any]:
    """Return the machine-readable persistence contract.

    The contract uses a bronze sidecar table rather than the execution DB. It
    references existing sampled snapshot rows by `(snapshot_table, symbol, ts,
    row_checksum)` and stores collector-sequence and payload evidence there.
    """

    return {
        "contract_version": ORDERBOOK_DIFF_PAYLOAD_CONTRACT_VERSION,
        "storage_table": ORDERBOOK_DIFF_PAYLOAD_TABLE,
        "supported_snapshot_tables": sorted(SUPPORTED_SNAPSHOT_TABLES),
        "sequence_scope": COLLECTOR_SEQUENCE_SCOPE,
        "primary_identity": [
            "snapshot_table",
            "symbol",
            "ts",
            "row_checksum",
        ],
        "required_write_fields": {
            "base": list(BASE_REQUIRED_FIELDS),
            CAPTURE_STATUS_DIFF_PERSISTED: list(BASE_REQUIRED_FIELDS + DIFF_PAYLOAD_REQUIRED_FIELDS),
            CAPTURE_STATUS_SNAPSHOT_ONLY: list(BASE_REQUIRED_FIELDS),
            CAPTURE_STATUS_DIFF_UNAVAILABLE: list(BASE_REQUIRED_FIELDS),
        },
        "optional_fields": list(OPTIONAL_FIELDS),
        "read_projection_fields": list(READ_PROJECTION_FIELDS),
        "capture_statuses": sorted(CAPTURE_STATUSES),
        "required_constraints": [
            "storage_table must be bronze.market_orderbook_payloads",
            "snapshot_table must be one of the supported bronze orderbook snapshot tables",
            "collector_sequence must be a positive collector-local integer",
            "collector_sequence_scope must be per_ingest_run_symbol_channel",
            "row_checksum must use checksum_version orderbook_row_v1",
            "diff_payload_persisted requires payload_hash, payload_schema_version, payload_kind, raw_payload",
            "raw_payload must not contain credential-like keys",
            "no execution schema/table may store payload truth",
        ],
        "recommended_indexes": [
            ["snapshot_table", "symbol", "ts"],
            ["snapshot_table", "symbol", "collector_sequence"],
            ["snapshot_table", "symbol", "source_ts"],
        ],
    }


def required_write_fields_for_status(capture_status: str) -> tuple[str, ...]:
    """Return required write fields for a capture status."""

    if capture_status == CAPTURE_STATUS_DIFF_PERSISTED:
        return BASE_REQUIRED_FIELDS + DIFF_PAYLOAD_REQUIRED_FIELDS
    if capture_status in {CAPTURE_STATUS_SNAPSHOT_ONLY, CAPTURE_STATUS_DIFF_UNAVAILABLE}:
        return BASE_REQUIRED_FIELDS
    return BASE_REQUIRED_FIELDS + ("unsupported_capture_status",)


def validate_orderbook_diff_payload_record(record: Mapping[str, Any]) -> ContractValidation:
    """Validate a prospective persistence record against the contract."""

    capture_status = _string_value(record.get("capture_status"))
    required_fields = required_write_fields_for_status(capture_status)
    missing_fields = tuple(field for field in required_fields if _is_missing(record.get(field)))

    errors: list[str] = []
    warnings: list[str] = []

    storage_table = _string_value(record.get("storage_table"))
    if storage_table != ORDERBOOK_DIFF_PAYLOAD_TABLE:
        errors.append("storage_table_must_be_bronze_orderbook_payloads")
    if storage_table.startswith("execution."):
        errors.append("execution_db_payload_storage_forbidden")

    snapshot_table = _string_value(record.get("snapshot_table"))
    if snapshot_table and snapshot_table not in SUPPORTED_SNAPSHOT_TABLES:
        errors.append("unsupported_snapshot_table")

    if capture_status and capture_status not in CAPTURE_STATUSES:
        errors.append("unsupported_capture_status")

    if _string_value(record.get("collector_sequence_scope")) not in {"", COLLECTOR_SEQUENCE_SCOPE}:
        errors.append("invalid_collector_sequence_scope")

    collector_sequence = record.get("collector_sequence")
    if not _is_missing(collector_sequence) and not _is_positive_int(collector_sequence):
        errors.append("collector_sequence_must_be_positive_int")

    checksum_version = _string_value(record.get("checksum_version"))
    if checksum_version and checksum_version != ORDERBOOK_ROW_CHECKSUM_VERSION:
        errors.append("unsupported_checksum_version")

    row_checksum = _string_value(record.get("row_checksum"))
    if row_checksum and not _is_sha256_reference(row_checksum):
        errors.append("invalid_row_checksum")

    payload_hash = _string_value(record.get("payload_hash"))
    if payload_hash and not _is_sha256_reference(payload_hash):
        errors.append("invalid_payload_hash")

    if capture_status == CAPTURE_STATUS_DIFF_PERSISTED:
        payload_schema_version = _string_value(record.get("payload_schema_version"))
        if payload_schema_version and payload_schema_version != ORDERBOOK_DIFF_PAYLOAD_SCHEMA_VERSION:
            errors.append("unsupported_payload_schema_version")
        raw_payload = record.get("raw_payload")
        if not _is_missing(raw_payload):
            errors.extend(_sensitive_payload_key_errors(raw_payload))
    elif not _is_missing(record.get("raw_payload")):
        warnings.append("raw_payload_ignored_when_diff_not_persisted")

    ok = not missing_fields and not errors
    return ContractValidation(
        ok=ok,
        missing_fields=missing_fields,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


def compute_orderbook_row_checksum(snapshot_table: str, row: Mapping[str, Any]) -> str:
    """Return the stable checksum for a persisted bbo/books5 snapshot row.

    This mirrors the read-side checksum contract used by execution-science
    resolvers so collector-produced sidecar rows can join back to snapshot
    rows without adding mutable foreign keys to the high-write bronze tables.
    """

    if snapshot_table not in ORDERBOOK_ROW_CONTENT_FIELDS:
        raise ValueError(f"unsupported snapshot table: {snapshot_table!r}")
    payload = {
        "checksum_version": ORDERBOOK_ROW_CHECKSUM_VERSION,
        "table_name": snapshot_table,
        "symbol": _canonical_checksum_value(row.get("symbol")),
        "ts": _canonical_checksum_value(row.get("ts")),
        "source_ts": _canonical_checksum_value(row.get("source_ts")),
        "fields": {
            field: _canonical_checksum_value(row.get(field))
            for field in ORDERBOOK_ROW_CONTENT_FIELDS[snapshot_table]
        },
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(serialized.encode('utf-8')).hexdigest()}"


def compute_orderbook_payload_hash(raw_payload: Any) -> str:
    """Return a deterministic public-payload hash for sidecar storage."""

    sanitized = _json_safe_payload_value(raw_payload)
    serialized = json.dumps(
        sanitized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return f"sha256:{hashlib.sha256(serialized.encode('utf-8')).hexdigest()}"


def _string_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _canonical_checksum_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, datetime):
        normalized = value
        if normalized.tzinfo is None:
            normalized = normalized.replace(tzinfo=timezone.utc)
        else:
            normalized = normalized.astimezone(timezone.utc)
        return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, Decimal):
        if value.is_zero():
            return "0"
        return format(value.normalize(), "f")
    return str(value)


def _json_safe_payload_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe_payload_value(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe_payload_value(child) for child in value]
    if isinstance(value, datetime):
        return _canonical_checksum_value(value)
    if isinstance(value, Decimal):
        return _canonical_checksum_value(value)
    return value


def _is_missing(value: Any) -> bool:
    return value is None or value == ""


def _is_positive_int(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value > 0
    if isinstance(value, str) and value.isdecimal():
        return int(value) > 0
    return False


def _is_sha256_reference(value: str) -> bool:
    if not value.startswith("sha256:"):
        return False
    digest = value.removeprefix("sha256:")
    return len(digest) == 64 and all(ch in "0123456789abcdef" for ch in digest)


def _sensitive_payload_key_errors(value: Any, *, path: str = "raw_payload") -> list[str]:
    errors: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            key_lower = key_text.lower()
            if any(fragment in key_lower for fragment in _SENSITIVE_KEY_FRAGMENTS):
                errors.append(f"sensitive_payload_key:{path}.{key_text}")
            errors.extend(_sensitive_payload_key_errors(child, path=f"{path}.{key_text}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            errors.extend(_sensitive_payload_key_errors(child, path=f"{path}[{index}]"))
    return errors


__all__ = [
    "BASE_REQUIRED_FIELDS",
    "CAPTURE_STATUS_DIFF_PERSISTED",
    "CAPTURE_STATUS_DIFF_UNAVAILABLE",
    "CAPTURE_STATUS_SNAPSHOT_ONLY",
    "CAPTURE_STATUSES",
    "COLLECTOR_SEQUENCE_SCOPE",
    "ContractValidation",
    "DIFF_PAYLOAD_REQUIRED_FIELDS",
    "ORDERBOOK_BBO_CONTENT_FIELDS",
    "ORDERBOOK_DIFF_PAYLOAD_CONTRACT_VERSION",
    "ORDERBOOK_DIFF_PAYLOAD_SCHEMA_VERSION",
    "ORDERBOOK_DIFF_PAYLOAD_TABLE",
    "ORDERBOOK_BOOKS5_CONTENT_FIELDS",
    "ORDERBOOK_ROW_CHECKSUM_VERSION",
    "ORDERBOOK_ROW_CONTENT_FIELDS",
    "READ_PROJECTION_FIELDS",
    "SUPPORTED_SNAPSHOT_TABLES",
    "compute_orderbook_payload_hash",
    "compute_orderbook_row_checksum",
    "orderbook_diff_payload_contract_spec",
    "required_write_fields_for_status",
    "validate_orderbook_diff_payload_record",
]
