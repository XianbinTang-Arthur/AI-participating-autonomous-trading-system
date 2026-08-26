"""Capacity-gated official bulk-history campaign planning and download."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import urllib.parse
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

import httpx
from sqlalchemy import text

from aats.data_platform.data_governance.contracts import canonical_json_bytes
from aats.data_platform.governance._atomic_io import atomic_json_write


OKX_BULK_LINK_PATH = "/priapi/v5/broker/public/trade-data/download-link"
CAMPAIGN_SCHEMA = "aats.historical_campaign.v1"
_ALLOWED_DOWNLOAD_HOSTS = {"static.okx.com"}
_SAFE_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,239}$")
_DATE_IN_FILE = re.compile(r"(\d{4}-\d{2}-\d{2})")

# Calibrated on the verified 2026-08-20 BTC-USDT-SWAP sample.  The safety
# multipliers account for indexes/WAL and for both download + immutable raw copy.
CALIBRATED_DATABASE_BYTES_PER_DAY = 8_100_000_000
CALIBRATED_RAW_BYTES_PER_DAY = 592_000_000
DATABASE_OVERHEAD_MULTIPLIER = 1.25
RAW_COPY_MULTIPLIER = 2.10
MINIMUM_RESERVE_BYTES = 200_000_000_000
RESERVE_DISK_RATIO = 0.20


@dataclass(frozen=True)
class CapacityReport:
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
                    links.append(
                        {
                            "filename": filename,
                            "date": match.group(1),
                            "size_mb": str(group.get("sizeMB", "")),
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
) -> dict[str, Any]:
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
    if not symbol.endswith("-USDT-SWAP"):
        raise ValueError("historical_campaign_symbol_unsupported")
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
    manifest["manifest_fingerprint"] = hashlib.sha256(
        canonical_json_bytes(manifest)
    ).hexdigest()
    return manifest


def register_campaign(session, manifest: Mapping[str, Any]) -> tuple[str, str]:
    fingerprint = validate_campaign_manifest(manifest)
    operation_key = "hist-campaign-" + fingerprint
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
    ).one()
    return str(row.campaign_id), str(row.status)


def start_campaign(session, campaign_id: str, *, resume_running: bool = False) -> dict[str, Any]:
    row = session.execute(
        text(
            "SELECT campaign_id, status, capacity_report, manifest, checkpoint "
            "FROM meta.historical_campaign_runs "
            "WHERE campaign_id = CAST(:campaign_id AS UUID) FOR UPDATE"
        ),
        {"campaign_id": campaign_id},
    ).mappings().one_or_none()
    if row is None:
        raise ValueError("historical_campaign_not_found")
    validate_campaign_manifest(row["manifest"])
    if not bool(row["capacity_report"].get("approved")):
        raise RuntimeError("historical_campaign_capacity_not_approved")
    if row["status"] == "SUCCEEDED":
        return {**dict(row), "status": "already_succeeded"}
    if row["status"] == "RUNNING" and not resume_running:
        raise RuntimeError("historical_campaign_already_running")
    if row["status"] not in {"PLANNED", "FAILED", "RUNNING"}:
        raise RuntimeError("historical_campaign_not_startable")
    session.execute(
        text(
            "UPDATE meta.historical_campaign_runs SET status = 'RUNNING', "
            "started_at = COALESCE(started_at, NOW()), ended_at = NULL, "
            "error_message = NULL, updated_at = NOW() "
            "WHERE campaign_id = CAST(:campaign_id AS UUID)"
        ),
        {"campaign_id": campaign_id},
    )
    return {**dict(row), "status": "started"}


def update_campaign_checkpoint(
    session,
    campaign_id: str,
    *,
    checkpoint_key: str,
    payload: Mapping[str, Any],
) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", checkpoint_key):
        raise ValueError("historical_campaign_checkpoint_key_invalid")
    result = session.execute(
        text(
            "UPDATE meta.historical_campaign_runs SET checkpoint = checkpoint || "
            "jsonb_build_object(:checkpoint_key, CAST(:payload AS jsonb)), "
            "updated_at = NOW() WHERE campaign_id = CAST(:campaign_id AS UUID) "
            "AND status = 'RUNNING'"
        ),
        {
            "campaign_id": campaign_id,
            "checkpoint_key": checkpoint_key,
            "payload": json.dumps(dict(payload), sort_keys=True, default=str),
        },
    )
    if int(result.rowcount or 0) != 1:
        raise RuntimeError("historical_campaign_checkpoint_transition_conflict")


def finish_campaign(
    session,
    campaign_id: str,
    *,
    succeeded: bool,
    error_type: str | None = None,
) -> None:
    if succeeded and error_type is not None:
        raise ValueError("historical_campaign_success_cannot_have_error")
    if not succeeded and not error_type:
        raise ValueError("historical_campaign_failure_requires_error")
    result = session.execute(
        text(
            "UPDATE meta.historical_campaign_runs SET status = :status, "
            "error_message = :error_type, ended_at = NOW(), updated_at = NOW() "
            "WHERE campaign_id = CAST(:campaign_id AS UUID) AND status = 'RUNNING'"
        ),
        {
            "campaign_id": campaign_id,
            "status": "SUCCEEDED" if succeeded else "FAILED",
            "error_type": error_type,
        },
    )
    if int(result.rowcount or 0) != 1:
        raise RuntimeError("historical_campaign_terminal_transition_conflict")


def download_manifest_files(
    client: httpx.Client,
    manifest: Mapping[str, Any],
    target_dir: Path,
) -> list[DownloadResult]:
    validate_campaign_manifest(manifest)
    if not bool(manifest.get("capacity_report", {}).get("approved")):
        raise RuntimeError("historical_campaign_capacity_not_approved")
    resolved = target_dir.expanduser().resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    unique: dict[str, str] = {}
    for partition in manifest.get("partitions", []):
        for item in (*partition["trade_files"], partition["l2_file"]):
            filename = str(item["filename"])
            url = str(item["url"])
            _validate_download_identity(filename, url)
            if filename in unique and unique[filename] != url:
                raise RuntimeError("historical_campaign_filename_url_conflict")
            unique[filename] = url
    return [
        download_verified_file(client, url=url, target=resolved / filename)
        for filename, url in sorted(unique.items())
    ]


def download_verified_file(
    client: httpx.Client,
    *,
    url: str,
    target: Path,
) -> DownloadResult:
    _validate_download_identity(target.name, url)
    target = target.resolve()
    sidecar = target.with_name(target.name + ".sha256.json")
    if target.is_file() and sidecar.is_file():
        metadata = json.loads(sidecar.read_text(encoding="utf-8"))
        digest = _sha256_file(target)
        if (
            metadata.get("sha256") == digest
            and int(metadata.get("size_bytes", -1)) == target.stat().st_size
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
    try:
        with client.stream("GET", url, timeout=None, follow_redirects=True) as response:
            response.raise_for_status()
            final = urllib.parse.urlparse(str(response.url))
            if final.scheme != "https" or final.hostname not in _ALLOWED_DOWNLOAD_HOSTS:
                raise RuntimeError("historical_campaign_download_redirect_rejected")
            with partial.open("xb") as handle:
                for chunk in response.iter_bytes(1024 * 1024):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            declared = response.headers.get("content-length")
            if declared is not None and int(declared) != size:
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
    if manifest.get("schema") != CAMPAIGN_SCHEMA:
        raise ValueError("historical_campaign_manifest_schema_invalid")
    fingerprint = str(manifest.get("manifest_fingerprint", ""))
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ValueError("historical_campaign_manifest_fingerprint_invalid")
    material = dict(manifest)
    material.pop("manifest_fingerprint", None)
    observed = hashlib.sha256(canonical_json_bytes(material)).hexdigest()
    if observed != fingerprint:
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


def _validate_download_identity(filename: str, url: str) -> None:
    if not _SAFE_FILE.fullmatch(filename) or Path(filename).name != filename:
        raise ValueError("okx_bulk_filename_invalid")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_DOWNLOAD_HOSTS:
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
    "CapacityReport",
    "DownloadResult",
    "OkxBulkLinkClient",
    "assess_campaign_capacity",
    "build_campaign_manifest",
    "download_manifest_files",
    "download_verified_file",
    "finish_campaign",
    "observe_capacity",
    "register_campaign",
    "start_campaign",
    "update_campaign_checkpoint",
    "validate_campaign_manifest",
    "write_campaign_manifest",
]
