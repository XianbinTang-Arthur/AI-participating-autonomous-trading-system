from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from aats.data_platform.data_governance.historical_campaign import (
    OkxBulkLinkClient,
    assess_campaign_capacity,
    build_campaign_manifest,
    download_verified_file,
    observe_capacity,
    start_campaign,
    validate_campaign_manifest,
)


UTC = timezone.utc


def _one_day_manifest() -> dict:
    day = datetime(2026, 8, 1, tzinfo=UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        module = json.loads(request.content)["module"]
        dates = [day, day + timedelta(days=1)] if module == "1" else [day]
        groups = []
        for value in dates:
            date = value.date().isoformat()
            kind = "trades" if module == "1" else "L2orderbook-400lv"
            filename = f"BTC-USDT-SWAP-{kind}-{date}.zip"
            groups.append(
                {
                    "filename": filename,
                    "url": f"https://static.okx.com/cdn/{filename}",
                }
            )
        return httpx.Response(
            200,
            json={"code": "0", "data": {"details": [{"groupDetails": groups}]}},
        )

    capacity = assess_campaign_capacity(
        requested_days=1,
        current_database_bytes=1,
        disk_total_bytes=1_000_000_000_000,
        disk_free_bytes=900_000_000_000,
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        return build_campaign_manifest(
            OkxBulkLinkClient(client, request_interval_seconds=0),
            symbol="BTC-USDT-SWAP",
            start=day,
            end=day + timedelta(days=1),
            capacity=capacity,
        )


def test_capacity_gate_allows_30_days_and_blocks_90_days() -> None:
    common = {
        "current_database_bytes": 37_456_927_767,
        "disk_total_bytes": 1_081_101_176_832,
        "disk_free_bytes": 801_632_419_840,
    }

    thirty = assess_campaign_capacity(requested_days=30, **common)
    ninety = assess_campaign_capacity(requested_days=90, **common)

    assert thirty.approved is True
    assert thirty.reason_code == "capacity_projection_within_safety_floor"
    assert ninety.approved is False
    assert ninety.reason_code == "capacity_projection_exceeds_safe_free_bytes"
    assert ninety.projected_incremental_bytes > ninety.safe_available_bytes


def test_capacity_observation_accepts_not_yet_created_nested_target(
    tmp_path: Path,
) -> None:
    class _Scalar:
        def scalar_one(self):
            return 123

    class _CapacitySession:
        def execute(self, _statement):
            return _Scalar()

    report = observe_capacity(
        _CapacitySession(),
        tmp_path / "campaigns" / "new-run" / "raw",
        requested_days=1,
    )

    assert report.current_database_bytes == 123
    assert report.disk_total_bytes > 0


def test_bulk_manifest_accounts_for_trade_utc_boundary() -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    end = start + timedelta(days=2)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        module = payload["module"]
        begin = datetime.fromtimestamp(payload["dateQuery"]["begin"] / 1000, tz=UTC)
        finish = datetime.fromtimestamp(payload["dateQuery"]["end"] / 1000, tz=UTC)
        groups = []
        cursor = begin
        while cursor <= finish:
            day = cursor.date().isoformat()
            compact = day.replace("-", "")
            if module == "1":
                filename = f"BTC-USDT-SWAP-trades-{day}.zip"
                url = f"https://static.okx.com/cdn/trades/{compact}/{filename}"
            else:
                filename = f"BTC-USDT-SWAP-L2orderbook-400lv-{day}.tar.gz"
                url = f"https://static.okx.com/cdn/l2/{compact}/{filename}"
            groups.append({"filename": filename, "url": url, "sizeMB": "1"})
            cursor += timedelta(days=1)
        return httpx.Response(
            200,
            json={"code": "0", "data": {"details": [{"groupDetails": groups}]}},
        )

    capacity = assess_campaign_capacity(
        requested_days=2,
        current_database_bytes=1,
        disk_total_bytes=1_000_000_000_000,
        disk_free_bytes=900_000_000_000,
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        manifest = build_campaign_manifest(
            OkxBulkLinkClient(client, request_interval_seconds=0),
            symbol="BTC-USDT-SWAP",
            start=start,
            end=end,
            capacity=capacity,
        )

    assert len(manifest["partitions"]) == 2
    assert [item["date"] for item in manifest["partitions"][0]["trade_files"]] == [
        "2026-08-01",
        "2026-08-02",
    ]
    assert manifest["partitions"][1]["trade_files"][1]["date"] == "2026-08-03"
    assert len(manifest["manifest_fingerprint"]) == 64


def test_verified_download_resumes_only_with_matching_sidecar(tmp_path: Path) -> None:
    payload = b"official-history"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload, headers={"content-length": str(len(payload))})

    target = tmp_path / "BTC-USDT-SWAP-trades-2026-08-01.zip"
    url = f"https://static.okx.com/cdn/{target.name}"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = download_verified_file(client, url=url, target=target)
        second = download_verified_file(client, url=url, target=target)

    assert first.resumed is False
    assert second.resumed is True
    assert first.sha256 == second.sha256
    assert target.read_bytes() == payload

    sidecar = target.with_name(target.name + ".sha256.json")
    metadata = json.loads(sidecar.read_text(encoding="utf-8"))
    metadata["sha256"] = "0" * 64
    sidecar.write_text(json.dumps(metadata), encoding="utf-8")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="existing_download_checksum_mismatch"):
            download_verified_file(client, url=url, target=target)


def test_verified_download_never_overwrites_unverifiable_existing_file(
    tmp_path: Path,
) -> None:
    target = tmp_path / "BTC-USDT-SWAP-trades-2026-08-01.zip"
    target.write_bytes(b"operator-owned-unverified-data")
    url = f"https://static.okx.com/cdn/{target.name}"

    with httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200))) as client:
        with pytest.raises(RuntimeError, match="existing_download_unverifiable"):
            download_verified_file(client, url=url, target=target)

    assert target.read_bytes() == b"operator-owned-unverified-data"


def test_bulk_link_resolution_rejects_duplicate_calendar_date() -> None:
    day = datetime(2026, 8, 1, tzinfo=UTC)
    filename = "BTC-USDT-SWAP-trades-2026-08-01.zip"

    def handler(_request: httpx.Request) -> httpx.Response:
        groups = [
            {
                "filename": filename,
                "url": f"https://static.okx.com/cdn/a/{filename}",
            },
            {
                "filename": filename,
                "url": f"https://static.okx.com/cdn/b/{filename}",
            },
        ]
        return httpx.Response(
            200,
            json={"code": "0", "data": {"details": [{"groupDetails": groups}]}},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        resolver = OkxBulkLinkClient(client, request_interval_seconds=0)
        with pytest.raises(RuntimeError, match="duplicate_date"):
            resolver.resolve(
                module="1",
                instrument_family="BTC-USDT",
                start_date=day,
                end_date_inclusive=day,
            )


def test_bulk_link_resolution_chunks_ranges_at_official_seven_day_limit() -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    observed_ranges: list[tuple[datetime, datetime]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        begin = datetime.fromtimestamp(payload["dateQuery"]["begin"] / 1000, tz=UTC)
        end = datetime.fromtimestamp(payload["dateQuery"]["end"] / 1000, tz=UTC)
        observed_ranges.append((begin, end))
        groups = []
        cursor = begin
        while cursor <= end:
            day = cursor.date().isoformat()
            filename = f"BTC-USDT-SWAP-trades-{day}.zip"
            groups.append(
                {
                    "filename": filename,
                    "url": f"https://static.okx.com/cdn/{filename}",
                }
            )
            cursor += timedelta(days=1)
        return httpx.Response(
            200,
            json={"code": "0", "data": {"details": [{"groupDetails": groups}]}},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        links = OkxBulkLinkClient(client, request_interval_seconds=0).resolve(
            module="1",
            instrument_family="BTC-USDT",
            start_date=start,
            end_date_inclusive=start + timedelta(days=9),
        )

    assert observed_ranges == [
        (start, start + timedelta(days=6)),
        (start + timedelta(days=7), start + timedelta(days=9)),
    ]
    assert len(links) == 10
    assert len({item["date"] for item in links}) == 10


def test_manifest_validation_detects_material_tampering() -> None:
    manifest = _one_day_manifest()
    manifest["symbol"] = "ETH-USDT-SWAP"

    with pytest.raises(ValueError, match="manifest_fingerprint_mismatch"):
        validate_campaign_manifest(manifest)


class _StartResult:
    def __init__(self, row: dict | None = None):
        self.row = row

    def mappings(self):
        return self

    def one_or_none(self):
        return self.row


class _StartSession:
    def __init__(self, row: dict):
        self.row = row
        self.calls = 0

    def execute(self, _statement, _params):
        self.calls += 1
        return _StartResult(self.row if self.calls == 1 else None)


@pytest.mark.parametrize(
    ("database_status", "returned_status", "expected_calls"),
    [
        ("PLANNED", "started", 2),
        ("SUCCEEDED", "already_succeeded", 1),
    ],
)
def test_start_campaign_returns_control_status_not_stale_database_status(
    database_status: str,
    returned_status: str,
    expected_calls: int,
) -> None:
    manifest = _one_day_manifest()
    session = _StartSession(
        {
            "campaign_id": "00000000-0000-0000-0000-000000000001",
            "status": database_status,
            "capacity_report": manifest["capacity_report"],
            "manifest": manifest,
            "checkpoint": {},
        }
    )

    state = start_campaign(session, "00000000-0000-0000-0000-000000000001")

    assert state["status"] == returned_status
    assert session.calls == expected_calls
