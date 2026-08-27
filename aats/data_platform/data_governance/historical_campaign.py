"""Capacity-gated official bulk-history campaign planning and download."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import urllib.parse
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, localcontext
from pathlib import Path
from typing import Any, Mapping

import httpx
from sqlalchemy import text

from aats.data_platform.data_governance.contracts import canonical_json_bytes
from aats.data_platform.data_governance.instrument_lineage import (
    instrument_snapshot_temporal_evidence_reason,
    load_verified_instrument_contract_snapshot,
)
from aats.data_platform.governance._atomic_io import atomic_json_write
from aats.domain.instrument_contract_snapshot import (
    InstrumentContractSnapshot,
    parse_instrument_contract_snapshot,
)
from aats.domain.instrument_scope import (
    INSTRUMENT_SCOPE_UNSUPPORTED_REASON,
    classify_instrument_scope,
)


OKX_BULK_LINK_PATH = "/priapi/v5/broker/public/trade-data/download-link"
LEGACY_CAMPAIGN_SCHEMA = "aats.historical_campaign.v1"
CAMPAIGN_SCHEMA = "aats.historical_campaign.v2"
_ALLOWED_DOWNLOAD_HOSTS = {"static.okx.com"}
_SAFE_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,239}$")
_DATE_IN_FILE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_CAPACITY_FIELDS = {
    "capacity_policy_version",
    "requested_days",
    "current_database_bytes",
    "disk_total_bytes",
    "disk_free_bytes",
    "calibrated_database_bytes_per_day",
    "calibrated_raw_bytes_per_day",
    "database_overhead_multiplier",
    "raw_copy_multiplier",
    "projected_incremental_bytes",
    "required_reserve_bytes",
    "safe_available_bytes",
    "approved",
    "reason_code",
}
_PARTITION_FIELDS = {
    "coverage_start",
    "coverage_end",
    "trade_files",
    "l2_file",
}
_LINK_FIELDS = {"filename", "date", "size_mb", "url"}

# Calibrated on the verified 2026-08-20 BTC-USDT-SWAP sample.  The safety
# multipliers account for indexes/WAL and for both download + immutable raw copy.
CALIBRATED_DATABASE_BYTES_PER_DAY = 8_100_000_000
CALIBRATED_RAW_BYTES_PER_DAY = 592_000_000
DATABASE_OVERHEAD_MULTIPLIER = 1.25
RAW_COPY_MULTIPLIER = 2.10
MINIMUM_RESERVE_BYTES = 200_000_000_000
RESERVE_DISK_RATIO = 0.20
CAPACITY_POLICY_VERSION = "aats.historical_campaign.capacity.v1"
MAX_DOWNLOAD_FILE_BYTES = 32 * 1024**3
DOWNLOAD_SIZE_HEADROOM_RATIO = Decimal("1.10")
DOWNLOAD_SIZE_HEADROOM_BYTES = 1024**2
DOWNLOAD_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)
DOWNLOAD_TOTAL_TIMEOUT_SECONDS = 15 * 60.0
CAMPAIGN_EXECUTION_UNAVAILABLE_REASON = (
    "historical_campaign_execution_unavailable_until_"
    "persistent_fencing_and_immutable_silver"
)


@dataclass(frozen=True)
class CapacityReport:
    capacity_policy_version: str
    requested_days: int
    current_database_bytes: int
    disk_total_bytes: int
    disk_free_bytes: int
    calibrated_database_bytes_per_day: int
    calibrated_raw_bytes_per_day: int
    database_overhead_multiplier: float
    raw_copy_multiplier: float
    projected_incremental_bytes: int
    required_reserve_bytes: int
    safe_available_bytes: int
    approved: bool
    reason_code: str


@dataclass(frozen=True)
class DownloadResult:
    filename: str
    path: str
    sha256: str
    size_bytes: int
    resumed: bool


def assess_campaign_capacity(
    *,
    requested_days: int,
    current_database_bytes: int,
    disk_total_bytes: int,
    disk_free_bytes: int,
    database_bytes_per_day: int = CALIBRATED_DATABASE_BYTES_PER_DAY,
    raw_bytes_per_day: int = CALIBRATED_RAW_BYTES_PER_DAY,
) -> CapacityReport:
    if (
        database_bytes_per_day != CALIBRATED_DATABASE_BYTES_PER_DAY
        or raw_bytes_per_day != CALIBRATED_RAW_BYTES_PER_DAY
    ):
        raise ValueError("historical_campaign_capacity_policy_override_forbidden")
    values = (
        requested_days,
        current_database_bytes,
        disk_total_bytes,
        disk_free_bytes,
        database_bytes_per_day,
        raw_bytes_per_day,
    )
    if requested_days <= 0 or any(value < 0 for value in values[1:]):
        raise ValueError("historical_campaign_capacity_input_invalid")
    reserve = max(MINIMUM_RESERVE_BYTES, int(disk_total_bytes * RESERVE_DISK_RATIO))
    safe_available = max(0, disk_free_bytes - reserve)
    projected = int(
        requested_days
        * (
            database_bytes_per_day * DATABASE_OVERHEAD_MULTIPLIER
            + raw_bytes_per_day * RAW_COPY_MULTIPLIER
        )
    )
    approved = projected <= safe_available
    return CapacityReport(
        capacity_policy_version=CAPACITY_POLICY_VERSION,
        requested_days=requested_days,
        current_database_bytes=current_database_bytes,
        disk_total_bytes=disk_total_bytes,
        disk_free_bytes=disk_free_bytes,
        calibrated_database_bytes_per_day=database_bytes_per_day,
        calibrated_raw_bytes_per_day=raw_bytes_per_day,
        database_overhead_multiplier=DATABASE_OVERHEAD_MULTIPLIER,
        raw_copy_multiplier=RAW_COPY_MULTIPLIER,
        projected_incremental_bytes=projected,
        required_reserve_bytes=reserve,
        safe_available_bytes=safe_available,
        approved=approved,
        reason_code=(
            "capacity_projection_within_safety_floor"
            if approved
            else "capacity_projection_exceeds_safe_free_bytes"
        ),
    )


def observe_capacity(session, storage_root: Path, *, requested_days: int) -> CapacityReport:
    resolved = storage_root.expanduser().resolve()
    existing = resolved
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    if not existing.exists():
        raise ValueError("historical_campaign_storage_ancestor_missing")
    usage = shutil.disk_usage(existing)
    database_bytes = int(
        session.execute(text("SELECT pg_database_size(current_database())")).scalar_one()
    )
    return assess_campaign_capacity(
        requested_days=requested_days,
        current_database_bytes=database_bytes,
        disk_total_bytes=usage.total,
        disk_free_bytes=usage.free,
    )


class OkxBulkLinkClient:
    """Fail-closed adapter for the public download-link service used by OKX UI."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        base_url: str = "https://www.okx.com",
        request_interval_seconds: float = 1.0,
    ) -> None:
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.request_interval_seconds = request_interval_seconds

    def resolve(
        self,
        *,
        module: str,
        instrument_family: str,
        start_date: datetime,
        end_date_inclusive: datetime,
    ) -> list[dict[str, Any]]:
        if module not in {"1", "4"}:
            raise ValueError("okx_bulk_module_not_allowlisted")
        if start_date.tzinfo is None or end_date_inclusive.tzinfo is None:
            raise ValueError("okx_bulk_link_dates_must_be_aware")
        if end_date_inclusive < start_date:
            raise ValueError("okx_bulk_link_range_invalid")
        # The public UI endpoint currently rejects ranges whose endpoint
        # inclusive selection exceeds seven calendar dates (OKX code 50076).
        # Split into non-overlapping seven-date chunks and verify the merged
        # calendar below.
        bodies: list[Mapping[str, Any]] = []
        cursor = start_date
        while cursor <= end_date_inclusive:
            chunk_end = min(cursor + timedelta(days=6), end_date_inclusive)
            bodies.append(
                self._request_range(
                    module=module,
                    instrument_family=instrument_family,
                    start_date=cursor,
                    end_date_inclusive=chunk_end,
                )
            )
            cursor = chunk_end + timedelta(days=1)
            if cursor <= end_date_inclusive and self.request_interval_seconds:
                time.sleep(self.request_interval_seconds)

        links: list[dict[str, Any]] = []
        for body in bodies:
            details = body.get("data", {}).get("details", [])
            if not isinstance(details, list):
                raise RuntimeError("okx_bulk_link_details_invalid")
            for detail in details:
                groups = detail.get("groupDetails", []) if isinstance(detail, dict) else []
                if not isinstance(groups, list):
                    raise RuntimeError("okx_bulk_link_group_invalid")
                for group in groups:
                    if not isinstance(group, dict):
                        raise RuntimeError("okx_bulk_link_item_invalid")
                    filename = str(group.get("filename", ""))
                    url = str(group.get("url", ""))
                    _validate_download_identity(filename, url)
                    match = _DATE_IN_FILE.search(filename)
                    if match is None:
                        raise RuntimeError("okx_bulk_link_filename_date_missing")
                    _validate_campaign_filename_identity(
                        filename,
                        expected_symbol=f"{instrument_family}-SWAP",
                        expected_date=match.group(1),
                        expected_kind=("trade" if module == "1" else "l2"),
                    )
                    links.append(
                        {
                            "filename": filename,
                            "date": match.group(1),
                            "size_mb": _normalize_declared_size_mb(
                                group.get("sizeMB")
                            ),
                            "url": url,
                        }
                    )
        expected_days = (end_date_inclusive.date() - start_date.date()).days + 1
        by_date = {item["date"]: item for item in links}
        if len(by_date) != len(links):
            raise RuntimeError("okx_bulk_link_duplicate_date")
        if len(by_date) != expected_days:
            raise RuntimeError("okx_bulk_link_date_coverage_incomplete")
        return [by_date[key] for key in sorted(by_date)]

    def _request_range(
        self,
        *,
        module: str,
        instrument_family: str,
        start_date: datetime,
        end_date_inclusive: datetime,
    ) -> Mapping[str, Any]:
        payload = {
            "module": module,
            "instType": "SWAP",
            "instQueryParam": {"instFamilyList": [instrument_family]},
            "dateQuery": {
                "dateAggrType": "daily",
                "begin": int(start_date.timestamp() * 1000),
                "end": int(end_date_inclusive.timestamp() * 1000),
            },
        }
        body: Mapping[str, Any] | None = None
        for attempt in range(6):
            response = self.client.post(
                f"{self.base_url}{OKX_BULK_LINK_PATH}",
                json=payload,
                timeout=30.0,
            )
            try:
                candidate = response.json()
            except ValueError as exc:
                raise RuntimeError("okx_bulk_link_response_not_json") from exc
            code = str(candidate.get("code", "")) if isinstance(candidate, dict) else ""
            if response.status_code == 200 and code == "0":
                body = candidate
                break
            if response.status_code == 429 or response.status_code >= 500 or code == "50011":
                if attempt == 5:
                    raise RuntimeError("okx_bulk_link_retry_exhausted")
                time.sleep(min(2**attempt, 16))
                continue
            raise RuntimeError("okx_bulk_link_request_rejected")
        if body is None:
            raise RuntimeError("okx_bulk_link_response_missing")
        return body


def build_campaign_manifest(
    client: OkxBulkLinkClient,
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    capacity: CapacityReport,
    instrument_contract_snapshot: InstrumentContractSnapshot | Mapping[str, Any] | None = None,
    instrument_snapshot_source_id: str | None = None,
) -> dict[str, Any]:
    normalized_symbol = str(symbol or "").strip().upper()
    if classify_instrument_scope(normalized_symbol) != "swap":
        raise ValueError(INSTRUMENT_SCOPE_UNSUPPORTED_REASON)
    symbol = normalized_symbol
    if not capacity.approved:
        raise RuntimeError(capacity.reason_code)
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise ValueError("historical_campaign_window_invalid")
    if (
        start.utcoffset() != timedelta(0)
        or end.utcoffset() != timedelta(0)
        or start.time() != datetime.min.time()
        or end.time() != datetime.min.time()
    ):
        raise ValueError("historical_campaign_window_must_be_utc_days")
    requested_days = (end.date() - start.date()).days
    if requested_days != capacity.requested_days:
        raise ValueError("historical_campaign_capacity_window_mismatch")
    snapshot = validate_campaign_snapshot_evidence(
        instrument_contract_snapshot,
        symbol=symbol,
        start=start,
        end=end,
    )
    if not instrument_snapshot_source_id:
        raise ValueError("historical_campaign_snapshot_source_reference_required")
    try:
        parsed_source_id = uuid.UUID(str(instrument_snapshot_source_id))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "historical_campaign_snapshot_source_reference_invalid"
        ) from exc
    if str(instrument_snapshot_source_id) != str(parsed_source_id):
        raise ValueError("historical_campaign_snapshot_source_reference_invalid")
    family = symbol.removesuffix("-SWAP")
    trade_links = client.resolve(
        module="1",
        instrument_family=family,
        start_date=start,
        end_date_inclusive=end,
    )
    if client.request_interval_seconds:
        time.sleep(client.request_interval_seconds)
    l2_links = client.resolve(
        module="4",
        instrument_family=family,
        start_date=start,
        end_date_inclusive=end - timedelta(days=1),
    )
    trade_by_date = {item["date"]: item for item in trade_links}
    l2_by_date = {item["date"]: item for item in l2_links}
    partitions: list[dict[str, Any]] = []
    cursor = start
    while cursor < end:
        date_key = cursor.date().isoformat()
        next_key = (cursor + timedelta(days=1)).date().isoformat()
        if date_key not in trade_by_date or next_key not in trade_by_date:
            raise RuntimeError("historical_campaign_trade_partition_incomplete")
        if date_key not in l2_by_date:
            raise RuntimeError("historical_campaign_l2_partition_incomplete")
        partitions.append(
            {
                "coverage_start": cursor.isoformat(),
                "coverage_end": (cursor + timedelta(days=1)).isoformat(),
                "trade_files": [trade_by_date[date_key], trade_by_date[next_key]],
                "l2_file": l2_by_date[date_key],
            }
        )
        cursor += timedelta(days=1)
    manifest = {
        "schema": CAMPAIGN_SCHEMA,
        "symbol": symbol,
        "coverage_start": start.isoformat(),
        "coverage_end": end.isoformat(),
        "requested_days": requested_days,
        "capacity_report": asdict(capacity),
        "partitions": partitions,
    }
    manifest["instrument_contract_snapshot"] = snapshot.to_dict()
    manifest["instrument_snapshot_source_id"] = instrument_snapshot_source_id
    manifest["manifest_fingerprint"] = _campaign_manifest_fingerprint(manifest)
    return manifest


def validate_campaign_snapshot_evidence(
    instrument_contract_snapshot: InstrumentContractSnapshot | Mapping[str, Any] | None,
    *,
    symbol: str,
    start: datetime,
    end: datetime,
) -> InstrumentContractSnapshot:
    """Validate contract-time evidence before any DB or network side effect."""

    if instrument_contract_snapshot is None:
        raise RuntimeError("historical_campaign_contract_metadata_unbound")
    snapshot = parse_instrument_contract_snapshot(instrument_contract_snapshot)
    snapshot.validate_window(symbol=symbol, start=start, end=end)
    evidence_reason = instrument_snapshot_temporal_evidence_reason(snapshot)
    if evidence_reason is not None:
        raise RuntimeError(evidence_reason)
    return snapshot


def register_campaign(session, manifest: Mapping[str, Any]) -> tuple[str, str]:
    validate_campaign_manifest(manifest)
    if manifest.get("schema") != CAMPAIGN_SCHEMA:
        raise RuntimeError("historical_campaign_contract_metadata_unbound")
    _verify_campaign_snapshot_anchor(session, manifest)
    operation_key = _campaign_operation_key(manifest)
    capacity = manifest.get("capacity_report")
    if not isinstance(capacity, Mapping):
        raise ValueError("historical_campaign_capacity_report_invalid")
    status = "PLANNED" if bool(capacity.get("approved")) else "BLOCKED"
    row = session.execute(
        text(
            """
            INSERT INTO meta.historical_campaign_runs (
                operation_key, symbol, coverage_start, coverage_end,
                requested_days, status, capacity_report, manifest
            ) VALUES (
                :operation_key, :symbol, :coverage_start, :coverage_end,
                :requested_days, :status, CAST(:capacity AS jsonb),
                CAST(:manifest AS jsonb)
            ) ON CONFLICT (operation_key) DO UPDATE SET
                operation_key = EXCLUDED.operation_key
            WHERE meta.historical_campaign_runs.symbol = EXCLUDED.symbol
              AND meta.historical_campaign_runs.coverage_start = EXCLUDED.coverage_start
              AND meta.historical_campaign_runs.coverage_end = EXCLUDED.coverage_end
              AND meta.historical_campaign_runs.requested_days = EXCLUDED.requested_days
              AND meta.historical_campaign_runs.capacity_report = EXCLUDED.capacity_report
              AND meta.historical_campaign_runs.manifest = EXCLUDED.manifest
            RETURNING campaign_id, status
            """
        ),
        {
            "operation_key": operation_key,
            "symbol": manifest["symbol"],
            "coverage_start": manifest["coverage_start"],
            "coverage_end": manifest["coverage_end"],
            "requested_days": manifest["requested_days"],
            "status": status,
            "capacity": json.dumps(capacity, sort_keys=True),
            "manifest": json.dumps(manifest, sort_keys=True),
        },
    ).one_or_none()
    if row is None:
        raise RuntimeError("historical_campaign_immutable_identity_conflict")
    return str(row.campaign_id), str(row.status)


def start_campaign(
    session,
    campaign_id: str,
    *,
    resume_running: bool = False,
) -> dict[str, Any]:
    del session, campaign_id, resume_running
    raise RuntimeError(CAMPAIGN_EXECUTION_UNAVAILABLE_REASON)


def update_campaign_checkpoint(
    session,
    campaign_id: str,
    *,
    checkpoint_key: str,
    payload: Mapping[str, Any],
) -> None:
    del session, campaign_id, checkpoint_key, payload
    raise RuntimeError(CAMPAIGN_EXECUTION_UNAVAILABLE_REASON)


def finish_campaign(
    session,
    campaign_id: str,
    *,
    succeeded: bool,
    error_type: str | None = None,
) -> None:
    del session, campaign_id, succeeded, error_type
    raise RuntimeError(CAMPAIGN_EXECUTION_UNAVAILABLE_REASON)


def download_manifest_files(
    client: httpx.Client,
    manifest: Mapping[str, Any],
    target_dir: Path,
) -> list[DownloadResult]:
    del client, target_dir
    validate_campaign_manifest(manifest)
    if manifest.get("schema") != CAMPAIGN_SCHEMA:
        raise RuntimeError("historical_campaign_contract_metadata_unbound")
    raise RuntimeError("historical_campaign_standalone_download_disabled")


def download_verified_file(
    client: httpx.Client,
    *,
    url: str,
    target: Path,
    maximum_download_bytes: int,
) -> DownloadResult:
    if (
        type(maximum_download_bytes) is not int
        or maximum_download_bytes <= 0
        or maximum_download_bytes > MAX_DOWNLOAD_FILE_BYTES
    ):
        raise ValueError("historical_campaign_download_limit_invalid")
    _validate_download_identity(target.name, url)
    target = target.resolve()
    sidecar = target.with_name(target.name + ".sha256.json")
    _assert_download_capacity(target, maximum_download_bytes)
    if target.is_file() and sidecar.is_file():
        metadata = json.loads(sidecar.read_text(encoding="utf-8"))
        digest = _sha256_file(target)
        if (
            metadata.get("sha256") == digest
            and int(metadata.get("size_bytes", -1)) == target.stat().st_size
            and 0 < target.stat().st_size <= maximum_download_bytes
            and target.stat().st_size <= MAX_DOWNLOAD_FILE_BYTES
        ):
            return DownloadResult(
                filename=target.name,
                path=str(target),
                sha256=digest,
                size_bytes=target.stat().st_size,
                resumed=True,
            )
        raise RuntimeError("historical_campaign_existing_download_checksum_mismatch")
    if target.exists() or sidecar.exists():
        # An unpaired target or sidecar has no trustworthy provenance.  Never
        # silently replace operator data; quarantine/removal is an explicit
        # recovery action outside the downloader.
        raise RuntimeError("historical_campaign_existing_download_unverifiable")
    partial = target.with_name(target.name + ".part")
    if partial.exists():
        partial.unlink()
    digest = hashlib.sha256()
    size = 0
    deadline = time.monotonic() + DOWNLOAD_TOTAL_TIMEOUT_SECONDS
    try:
        with client.stream(
            "GET",
            url,
            timeout=DOWNLOAD_TIMEOUT,
            follow_redirects=False,
        ) as response:
            if response.is_redirect:
                raise RuntimeError("historical_campaign_download_redirect_rejected")
            response.raise_for_status()
            final = urllib.parse.urlparse(str(response.url))
            if final.scheme != "https" or final.hostname not in _ALLOWED_DOWNLOAD_HOSTS:
                raise RuntimeError("historical_campaign_download_redirect_rejected")
            declared = _validated_content_length(
                response.headers.get("content-length"),
                maximum_download_bytes=maximum_download_bytes,
            )
            with partial.open("xb") as handle:
                for chunk in response.iter_bytes(1024 * 1024):
                    if not chunk:
                        continue
                    if time.monotonic() > deadline:
                        raise RuntimeError(
                            "historical_campaign_download_total_timeout"
                        )
                    if size + len(chunk) > maximum_download_bytes:
                        raise RuntimeError(
                            "historical_campaign_download_size_limit_exceeded"
                        )
                    if size + len(chunk) > declared:
                        raise RuntimeError(
                            "historical_campaign_download_length_mismatch"
                        )
                    _assert_download_capacity(target, len(chunk))
                    handle.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            _assert_download_capacity(target, 0)
            if time.monotonic() > deadline:
                raise RuntimeError("historical_campaign_download_total_timeout")
            if declared != size:
                raise RuntimeError("historical_campaign_download_length_mismatch")
        os.replace(partial, target)
        atomic_json_write(
            {"sha256": digest.hexdigest(), "size_bytes": size, "filename": target.name},
            sidecar,
        )
    except Exception:
        if partial.exists():
            partial.unlink()
        raise
    return DownloadResult(
        filename=target.name,
        path=str(target),
        sha256=digest.hexdigest(),
        size_bytes=size,
        resumed=False,
    )


def write_campaign_manifest(path: Path, manifest: Mapping[str, Any]) -> None:
    validate_campaign_manifest(manifest)
    atomic_json_write(dict(manifest), path.expanduser().resolve())


def validate_campaign_manifest(manifest: Mapping[str, Any]) -> str:
    schema = manifest.get("schema")
    if schema not in {CAMPAIGN_SCHEMA, LEGACY_CAMPAIGN_SCHEMA}:
        raise ValueError("historical_campaign_manifest_schema_invalid")
    if schema == LEGACY_CAMPAIGN_SCHEMA:
        return _validate_legacy_campaign_manifest(manifest)
    expected_fields = {
        "schema",
        "symbol",
        "coverage_start",
        "coverage_end",
        "requested_days",
        "capacity_report",
        "partitions",
        "manifest_fingerprint",
    }
    expected_fields.update(
        {
            "instrument_contract_snapshot",
            "instrument_snapshot_source_id",
        }
    )
    if set(manifest) != expected_fields:
        raise ValueError("historical_campaign_manifest_shape_invalid")
    raw_symbol = manifest.get("symbol")
    symbol = str(raw_symbol or "").strip().upper()
    if raw_symbol != symbol:
        raise ValueError("historical_campaign_manifest_symbol_noncanonical")
    if classify_instrument_scope(symbol) != "swap":
        raise ValueError(INSTRUMENT_SCOPE_UNSUPPORTED_REASON)
    if (
        "instrument_contract_snapshot" not in manifest
        or not isinstance(manifest.get("instrument_snapshot_source_id"), str)
        or not str(manifest.get("instrument_snapshot_source_id") or "").strip()
    ):
        raise ValueError("historical_campaign_manifest_contract_binding_invalid")
    try:
        raw_source_id = str(manifest["instrument_snapshot_source_id"])
        parsed_source_id = uuid.UUID(raw_source_id)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            "historical_campaign_manifest_contract_binding_invalid"
        ) from exc
    if raw_source_id != str(parsed_source_id):
        raise ValueError("historical_campaign_manifest_contract_binding_invalid")
    try:
        requested_days = manifest["requested_days"]
        if type(requested_days) is not int:
            raise TypeError
        start = _parse_canonical_campaign_time(manifest["coverage_start"])
        end = _parse_canonical_campaign_time(manifest["coverage_end"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("historical_campaign_window_invalid") from exc
    if (
        start.time() != datetime.min.time()
        or end.time() != datetime.min.time()
        or requested_days <= 0
        or end - start != timedelta(days=requested_days)
    ):
        raise ValueError("historical_campaign_window_invalid")
    _validate_campaign_capacity_report(
        manifest.get("capacity_report"),
        requested_days=requested_days,
    )
    _validate_campaign_partitions(
        manifest.get("partitions"),
        start=start,
        requested_days=requested_days,
        symbol=symbol,
    )
    _validate_campaign_download_budget(
        manifest["partitions"],
        requested_days=requested_days,
        safe_available_bytes=int(
            manifest["capacity_report"]["safe_available_bytes"]
        ),
    )
    fingerprint = str(manifest.get("manifest_fingerprint", ""))
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ValueError("historical_campaign_manifest_fingerprint_invalid")
    observed = _campaign_manifest_fingerprint(manifest)
    if observed != fingerprint:
        raise ValueError("historical_campaign_manifest_fingerprint_mismatch")
    snapshot = campaign_instrument_contract_snapshot(
        manifest,
        symbol=symbol,
        coverage_start=start,
        coverage_end=end,
    )
    if (
        snapshot is None
        or not isinstance(manifest["instrument_contract_snapshot"], Mapping)
        or snapshot.to_dict() != dict(manifest["instrument_contract_snapshot"])
    ):
        raise ValueError("historical_campaign_manifest_contract_binding_invalid")
    return fingerprint


def _validate_legacy_campaign_manifest(manifest: Mapping[str, Any]) -> str:
    """Reproduce the v1 read-only validator without backporting v2 rules."""

    fingerprint = str(manifest.get("manifest_fingerprint", ""))
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ValueError("historical_campaign_manifest_fingerprint_invalid")
    if _campaign_manifest_fingerprint(manifest) != fingerprint:
        raise ValueError("historical_campaign_manifest_fingerprint_mismatch")
    capacity = manifest.get("capacity_report")
    if (
        not isinstance(capacity, Mapping)
        or not isinstance(capacity.get("approved"), bool)
    ):
        raise ValueError("historical_campaign_capacity_report_invalid")
    partitions = manifest.get("partitions")
    if not isinstance(partitions, list) or not partitions:
        raise ValueError("historical_campaign_partitions_invalid")
    try:
        start = datetime.fromisoformat(str(manifest["coverage_start"]))
        end = datetime.fromisoformat(str(manifest["coverage_end"]))
        requested_days = int(manifest["requested_days"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("historical_campaign_window_invalid") from exc
    if (
        start.tzinfo is None
        or end.tzinfo is None
        or start.utcoffset() != timedelta(0)
        or end.utcoffset() != timedelta(0)
        or start.time() != datetime.min.time()
        or end.time() != datetime.min.time()
        or requested_days <= 0
        or end - start != timedelta(days=requested_days)
        or len(partitions) != requested_days
        or capacity.get("requested_days") != requested_days
    ):
        raise ValueError("historical_campaign_window_invalid")
    return fingerprint


def _parse_canonical_campaign_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise TypeError("campaign timestamp must be text")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("campaign timestamp must be UTC")
    canonical = parsed.astimezone(timezone.utc).isoformat()
    if value != canonical:
        raise ValueError("campaign timestamp must use canonical UTC ISO format")
    return parsed.astimezone(timezone.utc)


def _validate_campaign_capacity_report(
    value: Any,
    *,
    requested_days: int,
) -> None:
    if not isinstance(value, Mapping) or set(value) != _CAPACITY_FIELDS:
        raise ValueError("historical_campaign_capacity_report_invalid")
    int_fields = (
        "requested_days",
        "current_database_bytes",
        "disk_total_bytes",
        "disk_free_bytes",
        "calibrated_database_bytes_per_day",
        "calibrated_raw_bytes_per_day",
        "projected_incremental_bytes",
        "required_reserve_bytes",
        "safe_available_bytes",
    )
    if any(type(value.get(name)) is not int for name in int_fields):
        raise ValueError("historical_campaign_capacity_report_invalid")
    if any(int(value[name]) < 0 for name in int_fields):
        raise ValueError("historical_campaign_capacity_report_invalid")
    if (
        type(value.get("database_overhead_multiplier")) is not float
        or type(value.get("raw_copy_multiplier")) is not float
        or type(value.get("approved")) is not bool
        or not isinstance(value.get("reason_code"), str)
        or value["requested_days"] != requested_days
        or value["disk_free_bytes"] > value["disk_total_bytes"]
    ):
        raise ValueError("historical_campaign_capacity_report_invalid")
    if (
        value.get("capacity_policy_version") != CAPACITY_POLICY_VERSION
        or value["calibrated_database_bytes_per_day"]
        != CALIBRATED_DATABASE_BYTES_PER_DAY
        or value["calibrated_raw_bytes_per_day"]
        != CALIBRATED_RAW_BYTES_PER_DAY
        or value["database_overhead_multiplier"]
        != DATABASE_OVERHEAD_MULTIPLIER
        or value["raw_copy_multiplier"] != RAW_COPY_MULTIPLIER
    ):
        raise ValueError("historical_campaign_capacity_report_invalid")
    reserve = max(
        MINIMUM_RESERVE_BYTES,
        int(value["disk_total_bytes"] * RESERVE_DISK_RATIO),
    )
    safe_available = max(0, value["disk_free_bytes"] - reserve)
    projected = int(
        requested_days
        * (
            value["calibrated_database_bytes_per_day"]
            * value["database_overhead_multiplier"]
            + value["calibrated_raw_bytes_per_day"]
            * value["raw_copy_multiplier"]
        )
    )
    approved = projected <= safe_available
    reason = (
        "capacity_projection_within_safety_floor"
        if approved
        else "capacity_projection_exceeds_safe_free_bytes"
    )
    if (
        value["required_reserve_bytes"] != reserve
        or value["safe_available_bytes"] != safe_available
        or value["projected_incremental_bytes"] != projected
        or value["approved"] is not approved
        or value["reason_code"] != reason
    ):
        raise ValueError("historical_campaign_capacity_report_invalid")


def _validate_campaign_partitions(
    value: Any,
    *,
    start: datetime,
    requested_days: int,
    symbol: str,
) -> None:
    if not isinstance(value, list) or len(value) != requested_days:
        raise ValueError("historical_campaign_partitions_invalid")
    for index, partition in enumerate(value):
        if not isinstance(partition, Mapping) or set(partition) != _PARTITION_FIELDS:
            raise ValueError("historical_campaign_partitions_invalid")
        expected_start = start + timedelta(days=index)
        expected_end = expected_start + timedelta(days=1)
        try:
            observed_start = _parse_canonical_campaign_time(
                partition["coverage_start"]
            )
            observed_end = _parse_canonical_campaign_time(partition["coverage_end"])
        except (TypeError, ValueError) as exc:
            raise ValueError("historical_campaign_partitions_invalid") from exc
        if observed_start != expected_start or observed_end != expected_end:
            raise ValueError("historical_campaign_partitions_invalid")
        trade_files = partition["trade_files"]
        if not isinstance(trade_files, list) or len(trade_files) != 2:
            raise ValueError("historical_campaign_partitions_invalid")
        _validate_campaign_link(
            trade_files[0],
            expected_date=expected_start,
            expected_symbol=symbol,
            expected_kind="trade",
        )
        _validate_campaign_link(
            trade_files[1],
            expected_date=expected_end,
            expected_symbol=symbol,
            expected_kind="trade",
        )
        _validate_campaign_link(
            partition["l2_file"],
            expected_date=expected_start,
            expected_symbol=symbol,
            expected_kind="l2",
        )


def _validate_campaign_link(
    value: Any,
    *,
    expected_date: datetime,
    expected_symbol: str,
    expected_kind: str,
) -> None:
    if not isinstance(value, Mapping) or set(value) != _LINK_FIELDS:
        raise ValueError("historical_campaign_partitions_invalid")
    if (
        value.get("date") != expected_date.date().isoformat()
        or not isinstance(value.get("filename"), str)
        or not isinstance(value.get("url"), str)
        or not isinstance(value.get("size_mb"), str)
    ):
        raise ValueError("historical_campaign_partitions_invalid")
    try:
        if value["size_mb"] != _normalize_declared_size_mb(value["size_mb"]):
            raise ValueError("declared size must be canonical")
        _validate_download_identity(str(value["filename"]), str(value["url"]))
        _validate_campaign_filename_identity(
            str(value["filename"]),
            expected_symbol=expected_symbol,
            expected_date=expected_date.date().isoformat(),
            expected_kind=expected_kind,
        )
    except (RuntimeError, ValueError) as exc:
        raise ValueError("historical_campaign_partitions_invalid") from exc


def declared_download_maximum_bytes(size_mb: Any) -> int:
    """Return the fixed-policy streaming ceiling for one declared file size."""

    normalized = _normalize_declared_size_mb(size_mb)
    with localcontext() as context:
        context.prec = 40
        declared_bytes = Decimal(normalized) * Decimal(1_000_000)
        maximum = int(
            (
                declared_bytes * DOWNLOAD_SIZE_HEADROOM_RATIO
                + Decimal(DOWNLOAD_SIZE_HEADROOM_BYTES)
            ).to_integral_value(rounding=ROUND_CEILING)
        )
    if maximum <= 0 or maximum > MAX_DOWNLOAD_FILE_BYTES:
        raise ValueError("historical_campaign_download_limit_invalid")
    return maximum


def _normalize_declared_size_mb(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("okx_bulk_link_size_invalid")
    raw = str(value)
    if not raw or len(raw) > 48 or raw.strip() != raw:
        raise ValueError("okx_bulk_link_size_invalid")
    try:
        parsed = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("okx_bulk_link_size_invalid") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError("okx_bulk_link_size_invalid")
    digits = parsed.as_tuple().digits
    exponent = parsed.as_tuple().exponent
    if len(digits) > 40 or exponent < -40 or exponent > 9:
        raise ValueError("okx_bulk_link_size_invalid")
    with localcontext() as context:
        context.prec = 80
        declared_bytes = (
            parsed * Decimal(1_000_000)
        ).to_integral_value(rounding=ROUND_CEILING)
        normalized = format(parsed, "f")
    if declared_bytes > MAX_DOWNLOAD_FILE_BYTES:
        raise ValueError("okx_bulk_link_size_invalid")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    normalized = normalized.lstrip("+")
    return normalized


def _validate_campaign_download_budget(
    partitions: list[Any],
    *,
    requested_days: int,
    safe_available_bytes: int,
) -> None:
    unique: dict[str, tuple[str, int]] = {}
    for partition in partitions:
        for item in (*partition["trade_files"], partition["l2_file"]):
            filename = str(item["filename"])
            identity = (
                str(item["url"]),
                declared_download_maximum_bytes(item["size_mb"]),
            )
            previous = unique.get(filename)
            if previous is not None and previous != identity:
                raise ValueError("historical_campaign_partitions_invalid")
            unique[filename] = identity
    total_maximum = sum(value[1] for value in unique.values())
    fixed_raw_budget = int(
        requested_days
        * CALIBRATED_RAW_BYTES_PER_DAY
        * RAW_COPY_MULTIPLIER
    )
    if total_maximum > min(fixed_raw_budget, safe_available_bytes):
        raise ValueError("historical_campaign_download_budget_exceeded")


def _validate_campaign_filename_identity(
    filename: str,
    *,
    expected_symbol: str,
    expected_date: str,
    expected_kind: str,
) -> None:
    if expected_kind == "trade":
        allowed = {f"{expected_symbol}-trades-{expected_date}.zip"}
    elif expected_kind == "l2":
        stem = f"{expected_symbol}-L2orderbook-400lv-{expected_date}"
        allowed = {f"{stem}.zip", f"{stem}.tar.gz"}
    else:  # pragma: no cover - internal callers use the two literal kinds
        raise ValueError("historical_campaign_link_kind_invalid")
    if filename not in allowed:
        raise RuntimeError("okx_bulk_link_filename_identity_mismatch")


def _campaign_operation_key(manifest: Mapping[str, Any]) -> str:
    identity = {
        "contract": "historical-campaign-operation-v2",
        "schema": manifest["schema"],
        "symbol": manifest["symbol"],
        "coverage_start": manifest["coverage_start"],
        "coverage_end": manifest["coverage_end"],
    }
    return "hist-campaign-" + hashlib.sha256(
        canonical_json_bytes(identity)
    ).hexdigest()


def _campaign_manifest_fingerprint(manifest: Mapping[str, Any]) -> str:
    """Hash campaign content while keeping database UUIDs as audit references."""

    material = dict(manifest)
    material.pop("manifest_fingerprint", None)
    if material.get("schema") == CAMPAIGN_SCHEMA:
        material.pop("instrument_snapshot_source_id", None)
    return hashlib.sha256(canonical_json_bytes(material)).hexdigest()


def campaign_instrument_contract_snapshot(
    manifest: Mapping[str, Any],
    *,
    symbol: str | None = None,
    coverage_start: datetime | None = None,
    coverage_end: datetime | None = None,
) -> InstrumentContractSnapshot | None:
    """Return a verified binding, or ``None`` for an explicit legacy manifest."""

    raw = manifest.get("instrument_contract_snapshot")
    if raw is None:
        return None
    snapshot = parse_instrument_contract_snapshot(raw)
    if symbol is not None and coverage_start is not None and coverage_end is not None:
        snapshot.validate_window(
            symbol=symbol,
            start=coverage_start,
            end=coverage_end,
        )
    return snapshot


def _verify_campaign_snapshot_anchor(
    session,
    manifest: Mapping[str, Any],
) -> None:
    snapshot = campaign_instrument_contract_snapshot(manifest)
    source_id = str(manifest.get("instrument_snapshot_source_id") or "")
    if snapshot is None or not source_id:
        raise RuntimeError("historical_campaign_contract_metadata_unbound")
    registered = load_verified_instrument_contract_snapshot(
        session,
        snapshot_source_id=source_id,
    )
    if registered.to_dict() != snapshot.to_dict():
        raise RuntimeError("instrument_snapshot_source_anchor_mismatch")
    evidence_reason = instrument_snapshot_temporal_evidence_reason(registered)
    if evidence_reason is not None:
        raise RuntimeError(evidence_reason)


def _validated_content_length(
    value: str | None,
    *,
    maximum_download_bytes: int,
) -> int:
    if value is None or not re.fullmatch(r"[1-9][0-9]*", value):
        raise RuntimeError("historical_campaign_download_length_invalid")
    declared = int(value)
    if declared > maximum_download_bytes or declared > MAX_DOWNLOAD_FILE_BYTES:
        raise RuntimeError("historical_campaign_download_size_limit_exceeded")
    return declared


def _assert_download_capacity(target: Path, maximum_download_bytes: int) -> None:
    existing = target.parent
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    if not existing.exists():
        raise RuntimeError("historical_campaign_storage_ancestor_missing")
    usage = shutil.disk_usage(existing)
    reserve = max(MINIMUM_RESERVE_BYTES, int(usage.total * RESERVE_DISK_RATIO))
    if usage.free - reserve < maximum_download_bytes:
        raise RuntimeError("historical_campaign_download_capacity_reserve_breached")


def _validate_download_identity(filename: str, url: str) -> None:
    if not _SAFE_FILE.fullmatch(filename) or Path(filename).name != filename:
        raise ValueError("okx_bulk_filename_invalid")
    parsed = urllib.parse.urlparse(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("okx_bulk_download_url_rejected") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _ALLOWED_DOWNLOAD_HOSTS
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("okx_bulk_download_url_rejected")
    if Path(urllib.parse.unquote(parsed.path)).name != filename:
        raise ValueError("okx_bulk_download_filename_mismatch")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "CAMPAIGN_SCHEMA",
    "LEGACY_CAMPAIGN_SCHEMA",
    "CAMPAIGN_EXECUTION_UNAVAILABLE_REASON",
    "CapacityReport",
    "DownloadResult",
    "OkxBulkLinkClient",
    "assess_campaign_capacity",
    "build_campaign_manifest",
    "campaign_instrument_contract_snapshot",
    "download_manifest_files",
    "download_verified_file",
    "declared_download_maximum_bytes",
    "finish_campaign",
    "observe_capacity",
    "register_campaign",
    "start_campaign",
    "update_campaign_checkpoint",
    "validate_campaign_manifest",
    "validate_campaign_snapshot_evidence",
    "write_campaign_manifest",
]
