from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from pathlib import Path

import httpx
import pytest

from aats.data_platform.data_governance import historical_campaign
from aats.data_platform.data_governance.historical_campaign import (
    CAMPAIGN_SCHEMA,
    LEGACY_CAMPAIGN_SCHEMA,
    OkxBulkLinkClient,
    assess_campaign_capacity,
    build_campaign_manifest,
    download_manifest_files,
    download_verified_file,
    finish_campaign,
    observe_capacity,
    register_campaign,
    start_campaign,
    update_campaign_checkpoint,
    validate_campaign_manifest,
)
from aats.data_platform.data_governance.contracts import canonical_json_bytes
from aats.data_platform.data_governance.instrument_lineage import (
    instrument_contract_snapshot_registry_identity,
    instrument_contract_snapshot_source_key,
)
from aats.domain.instrument_contract_snapshot import (
    instrument_contract_observation_window_from_metadata,
)
from aats.schemas.exchange import InstrumentMetadata


UTC = timezone.utc
CAMPAIGN_ID = "00000000-0000-0000-0000-000000000001"


def _snapshot_for(observed_at: datetime):
    metadata = InstrumentMetadata(
        instrument_id="BTC-USDT-SWAP",
        symbol="BTC-USDT-SWAP",
        base_currency="BTC",
        quote_currency="USDT",
        lot_size=Decimal("1"),
        tick_size=Decimal("0.1"),
        min_size=Decimal("1"),
        contract_value=Decimal("0.01"),
        contract_multiplier=Decimal("1"),
        contract_type="linear",
        instrument_type="SWAP",
        underlying="BTC-USDT",
        settle_currency="USDT",
        contract_value_currency="BTC",
        state="live",
    )
    return instrument_contract_observation_window_from_metadata(
        metadata,
        venue="OKX",
        first_observed_at=observed_at,
        last_observed_at=observed_at + timedelta(days=2),
        observation_evidence_sha256="e" * 64,
        source_locator="immutable://test/instrument-observation-window",
    )


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
                    "sizeMB": "1",
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
    temporal_verifier = historical_campaign.instrument_snapshot_temporal_evidence_reason
    historical_campaign.instrument_snapshot_temporal_evidence_reason = lambda _snapshot: None
    try:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            return build_campaign_manifest(
                OkxBulkLinkClient(client, request_interval_seconds=0),
                symbol="BTC-USDT-SWAP",
                start=day,
                end=day + timedelta(days=1),
                capacity=capacity,
                instrument_contract_snapshot=_snapshot_for(day),
                instrument_snapshot_source_id="00000000-0000-0000-0000-000000000099",
            )
    finally:
        historical_campaign.instrument_snapshot_temporal_evidence_reason = temporal_verifier


def _legacy_manifest() -> dict:
    manifest = _one_day_manifest()
    manifest.pop("instrument_contract_snapshot")
    manifest.pop("instrument_snapshot_source_id")
    manifest["capacity_report"].pop("capacity_policy_version")
    manifest["schema"] = LEGACY_CAMPAIGN_SCHEMA
    manifest.pop("manifest_fingerprint")
    manifest["manifest_fingerprint"] = hashlib.sha256(
        canonical_json_bytes(manifest)
    ).hexdigest()
    return manifest


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


def test_bulk_manifest_accounts_for_trade_utc_boundary(monkeypatch) -> None:
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
    monkeypatch.setattr(
        historical_campaign,
        "instrument_snapshot_temporal_evidence_reason",
        lambda _snapshot: None,
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        manifest = build_campaign_manifest(
            OkxBulkLinkClient(client, request_interval_seconds=0),
            symbol="BTC-USDT-SWAP",
            start=start,
            end=end,
            capacity=capacity,
            instrument_contract_snapshot=_snapshot_for(start),
            instrument_snapshot_source_id="00000000-0000-0000-0000-000000000099",
        )

    assert len(manifest["partitions"]) == 2
    assert [item["date"] for item in manifest["partitions"][0]["trade_files"]] == [
        "2026-08-01",
        "2026-08-02",
    ]
    assert manifest["partitions"][1]["trade_files"][1]["date"] == "2026-08-03"
    assert len(manifest["manifest_fingerprint"]) == 64
    assert manifest["schema"] == CAMPAIGN_SCHEMA
    assert (
        manifest["instrument_contract_snapshot"]["snapshot_digest"]
        == _snapshot_for(start).digest
    )


def test_campaign_build_rejects_unverified_snapshot_before_link_resolution() -> None:
    class _NoLinkResolution:
        request_interval_seconds = 0

        def resolve(self, **_kwargs):  # pragma: no cover - safety assertion
            raise AssertionError("unverified snapshot must stop before link resolution")

    start = datetime(2026, 8, 1, tzinfo=UTC)
    capacity = assess_campaign_capacity(
        requested_days=1,
        current_database_bytes=1,
        disk_total_bytes=1_000_000_000_000,
        disk_free_bytes=900_000_000_000,
    )

    with pytest.raises(
        RuntimeError,
        match="instrument_snapshot_observation_evidence_unverified",
    ):
        build_campaign_manifest(
            _NoLinkResolution(),  # type: ignore[arg-type]
            symbol="BTC-USDT-SWAP",
            start=start,
            end=start + timedelta(days=1),
            capacity=capacity,
            instrument_contract_snapshot=_snapshot_for(start),
            instrument_snapshot_source_id="00000000-0000-0000-0000-000000000099",
        )


def test_campaign_build_requires_snapshot_source_before_link_resolution(
    monkeypatch,
) -> None:
    class _NoLinkResolution:
        request_interval_seconds = 0

        def resolve(self, **_kwargs):  # pragma: no cover - safety assertion
            raise AssertionError("missing source reference must stop before links")

    monkeypatch.setattr(
        historical_campaign,
        "instrument_snapshot_temporal_evidence_reason",
        lambda _snapshot: None,
    )
    start = datetime(2026, 8, 1, tzinfo=UTC)
    capacity = assess_campaign_capacity(
        requested_days=1,
        current_database_bytes=1,
        disk_total_bytes=1_000_000_000_000,
        disk_free_bytes=900_000_000_000,
    )

    with pytest.raises(
        ValueError,
        match="historical_campaign_snapshot_source_reference_required",
    ):
        build_campaign_manifest(
            _NoLinkResolution(),  # type: ignore[arg-type]
            symbol="BTC-USDT-SWAP",
            start=start,
            end=start + timedelta(days=1),
            capacity=capacity,
            instrument_contract_snapshot=_snapshot_for(start),
            instrument_snapshot_source_id=None,
        )


@pytest.mark.parametrize(
    "source_id",
    (
        "00000000-0000-0000-0000-0000000000AA",
        "{00000000-0000-0000-0000-000000000099}",
    ),
)
def test_campaign_build_rejects_noncanonical_snapshot_source_before_links(
    source_id: str,
    monkeypatch,
) -> None:
    class _NoLinkResolution:
        request_interval_seconds = 0

        def resolve(self, **_kwargs):  # pragma: no cover - safety assertion
            raise AssertionError("noncanonical source id must stop before links")

    monkeypatch.setattr(
        historical_campaign,
        "instrument_snapshot_temporal_evidence_reason",
        lambda _snapshot: None,
    )
    start = datetime(2026, 8, 1, tzinfo=UTC)
    capacity = assess_campaign_capacity(
        requested_days=1,
        current_database_bytes=1,
        disk_total_bytes=1_000_000_000_000,
        disk_free_bytes=900_000_000_000,
    )

    with pytest.raises(
        ValueError,
        match="historical_campaign_snapshot_source_reference_invalid",
    ):
        build_campaign_manifest(
            _NoLinkResolution(),  # type: ignore[arg-type]
            symbol="BTC-USDT-SWAP",
            start=start,
            end=start + timedelta(days=1),
            capacity=capacity,
            instrument_contract_snapshot=_snapshot_for(start),
            instrument_snapshot_source_id=source_id,
        )


@pytest.mark.parametrize(
    "symbol",
    ("DOGE-USDT-SWAP", "BTC-USDT-FUTURES"),
)
def test_campaign_build_rejects_unproven_scope_before_link_resolution(
    symbol: str,
) -> None:
    class _NoLinkResolution:
        request_interval_seconds = 0

        def resolve(self, **_kwargs):  # pragma: no cover - safety assertion
            raise AssertionError("unsupported scope must not resolve download links")

    start = datetime(2026, 8, 1, tzinfo=UTC)
    capacity = assess_campaign_capacity(
        requested_days=1,
        current_database_bytes=1,
        disk_total_bytes=1_000_000_000_000,
        disk_free_bytes=900_000_000_000,
    )

    with pytest.raises(
        ValueError,
        match="instrument_scope_unsupported_or_unproven",
    ):
        build_campaign_manifest(
            _NoLinkResolution(),  # type: ignore[arg-type]
            symbol=symbol,
            start=start,
            end=start + timedelta(days=1),
            capacity=capacity,
        )


def test_campaign_validation_rejects_fingerprinted_unknown_scope() -> None:
    manifest = _one_day_manifest()
    manifest["symbol"] = "DOGE-USDT-SWAP"
    manifest["manifest_fingerprint"] = historical_campaign._campaign_manifest_fingerprint(
        manifest
    )

    with pytest.raises(
        ValueError,
        match="instrument_scope_unsupported_or_unproven",
    ):
        validate_campaign_manifest(manifest)


def test_legacy_v1_manifest_remains_readable_but_cannot_claim_binding() -> None:
    manifest = _legacy_manifest()

    assert "capacity_policy_version" not in manifest["capacity_report"]
    assert validate_campaign_manifest(manifest) == manifest["manifest_fingerprint"]

    class _NoDatabaseAccess:
        def execute(self, *_args, **_kwargs):  # pragma: no cover - safety assertion
            raise AssertionError("legacy manifest must fail before database access")

    with pytest.raises(
        RuntimeError,
        match="historical_campaign_contract_metadata_unbound",
    ):
        register_campaign(_NoDatabaseAccess(), manifest)


def test_real_legacy_v1_capacity_shape_preserves_old_read_only_semantics() -> None:
    manifest = _legacy_manifest()
    report = manifest["capacity_report"]
    report["calibrated_database_bytes_per_day"] = 123
    report["calibrated_raw_bytes_per_day"] = 456
    report["database_overhead_multiplier"] = 1.0
    report["raw_copy_multiplier"] = 1.0
    report["projected_incremental_bytes"] = 579
    report["approved"] = True
    report["reason_code"] = "capacity_projection_within_safety_floor"
    manifest["manifest_fingerprint"] = historical_campaign._campaign_manifest_fingerprint(
        manifest
    )

    assert validate_campaign_manifest(manifest) == manifest["manifest_fingerprint"]


def test_legacy_v1_keeps_baseline_tolerance_for_uninterpreted_fields() -> None:
    manifest = _legacy_manifest()
    manifest["capacity_report"]["capacity_policy_version"] = (
        historical_campaign.CAPACITY_POLICY_VERSION
    )
    manifest["legacy_audit_note"] = "accepted-by-v1-validator"
    manifest["partitions"][0]["l2_file"]["size_mb"] = "1.00"
    manifest["manifest_fingerprint"] = historical_campaign._campaign_manifest_fingerprint(
        manifest
    )

    assert validate_campaign_manifest(manifest) == manifest["manifest_fingerprint"]


def test_legacy_v1_manifest_cannot_trigger_download_side_effects(
    tmp_path: Path,
) -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, content=b"must-not-download")

    target = tmp_path / "must-not-be-created"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(
            RuntimeError,
            match="historical_campaign_contract_metadata_unbound",
        ):
            download_manifest_files(client, _legacy_manifest(), target)

    assert requests == 0
    assert not target.exists()


def test_legacy_v1_manifest_cannot_start_or_access_database() -> None:
    class _NoDatabaseAccess:
        def execute(self, *_args):  # pragma: no cover - safety assertion
            raise AssertionError("frozen execution must fail before database access")

    with pytest.raises(
        RuntimeError,
        match="execution_unavailable_until_persistent_fencing_and_immutable_silver",
    ):
        start_campaign(_NoDatabaseAccess(), CAMPAIGN_ID)


def test_unverified_v2_manifest_cannot_trigger_download_side_effects(
    tmp_path: Path,
) -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, content=b"must-not-download")

    target = tmp_path / "must-not-be-created-v2"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(
            RuntimeError,
            match="historical_campaign_standalone_download_disabled",
        ):
            download_manifest_files(client, _one_day_manifest(), target)

    assert requests == 0
    assert not target.exists()


def test_verified_download_resumes_only_with_matching_sidecar(
    tmp_path: Path,
    monkeypatch,
) -> None:
    payload = b"official-history"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload, headers={"content-length": str(len(payload))})

    target = tmp_path / "BTC-USDT-SWAP-trades-2026-08-01.zip"
    url = f"https://static.okx.com/cdn/{target.name}"
    monkeypatch.setattr(historical_campaign, "_assert_download_capacity", lambda *_args: None)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = download_verified_file(
            client,
            url=url,
            target=target,
            maximum_download_bytes=1024,
        )
        second = download_verified_file(
            client,
            url=url,
            target=target,
            maximum_download_bytes=1024,
        )

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
            download_verified_file(
                client,
                url=url,
                target=target,
                maximum_download_bytes=1024,
            )


def test_verified_download_never_overwrites_unverifiable_existing_file(
    tmp_path: Path,
) -> None:
    target = tmp_path / "BTC-USDT-SWAP-trades-2026-08-01.zip"
    target.write_bytes(b"operator-owned-unverified-data")
    url = f"https://static.okx.com/cdn/{target.name}"

    with httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200))) as client:
        with pytest.raises(RuntimeError, match="existing_download_unverifiable"):
            download_verified_file(
                client,
                url=url,
                target=target,
                maximum_download_bytes=1024,
            )

    assert target.read_bytes() == b"operator-owned-unverified-data"


def test_bulk_link_resolution_rejects_duplicate_calendar_date() -> None:
    day = datetime(2026, 8, 1, tzinfo=UTC)
    filename = "BTC-USDT-SWAP-trades-2026-08-01.zip"

    def handler(_request: httpx.Request) -> httpx.Response:
        groups = [
            {
                "filename": filename,
                "url": f"https://static.okx.com/cdn/a/{filename}",
                "sizeMB": "1",
            },
            {
                "filename": filename,
                "url": f"https://static.okx.com/cdn/b/{filename}",
                "sizeMB": "1",
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
                    "sizeMB": "1",
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

    with pytest.raises(ValueError, match="historical_campaign_partitions_invalid"):
        validate_campaign_manifest(manifest)

    malformed_reference = _one_day_manifest()
    malformed_reference["instrument_snapshot_source_id"] = "not-a-uuid"
    with pytest.raises(
        ValueError,
        match="historical_campaign_manifest_contract_binding_invalid",
    ):
        validate_campaign_manifest(malformed_reference)

    noncanonical_reference = _one_day_manifest()
    noncanonical_reference["instrument_snapshot_source_id"] = (
        "{00000000-0000-0000-0000-000000000099}"
    )
    with pytest.raises(
        ValueError,
        match="historical_campaign_manifest_contract_binding_invalid",
    ):
        validate_campaign_manifest(noncanonical_reference)


def test_manifest_rejects_equivalent_noncanonical_time_and_partition_shape() -> None:
    noncanonical_time = _one_day_manifest()
    noncanonical_time["coverage_start"] = "2026-08-01T00:00:00Z"
    noncanonical_time["manifest_fingerprint"] = (
        historical_campaign._campaign_manifest_fingerprint(noncanonical_time)
    )
    with pytest.raises(ValueError, match="historical_campaign_window_invalid"):
        validate_campaign_manifest(noncanonical_time)

    extra_trade = _one_day_manifest()
    extra_trade["partitions"][0]["trade_files"].append(
        dict(extra_trade["partitions"][0]["trade_files"][1])
    )
    extra_trade["manifest_fingerprint"] = (
        historical_campaign._campaign_manifest_fingerprint(extra_trade)
    )
    with pytest.raises(ValueError, match="historical_campaign_partitions_invalid"):
        validate_campaign_manifest(extra_trade)


@pytest.mark.parametrize("mutation", ("symbol", "date", "kind"))
def test_manifest_rejects_partition_filename_identity_mismatch(
    mutation: str,
) -> None:
    manifest = _one_day_manifest()
    link = manifest["partitions"][0]["trade_files"][0]
    if mutation == "symbol":
        filename = link["filename"].replace("BTC-USDT-SWAP", "ETH-USDT-SWAP")
    elif mutation == "date":
        filename = link["filename"].replace("2026-08-01", "2026-07-31")
    else:
        filename = link["filename"].replace("trades", "L2orderbook-400lv")
    link["filename"] = filename
    link["url"] = f"https://static.okx.com/test/{filename}"
    manifest["manifest_fingerprint"] = (
        historical_campaign._campaign_manifest_fingerprint(manifest)
    )

    with pytest.raises(ValueError, match="historical_campaign_partitions_invalid"):
        validate_campaign_manifest(manifest)


def test_bulk_resolver_rejects_wrong_instrument_filename() -> None:
    day = datetime(2026, 8, 1, tzinfo=UTC)
    filename = "ETH-USDT-SWAP-trades-2026-08-01.zip"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": "0",
                "data": {
                    "details": [
                        {
                            "groupDetails": [
                                {
                                    "filename": filename,
                                    "url": f"https://static.okx.com/test/{filename}",
                                    "sizeMB": "1",
                                }
                            ]
                        }
                    ]
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(
            RuntimeError,
            match="okx_bulk_link_filename_identity_mismatch",
        ):
            OkxBulkLinkClient(client, request_interval_seconds=0).resolve(
                module="1",
                instrument_family="BTC-USDT",
                start_date=day,
                end_date_inclusive=day,
            )


def test_campaign_operation_identity_excludes_dynamic_capacity_and_url() -> None:
    first = _one_day_manifest()
    second = json.loads(json.dumps(first))
    second["capacity_report"]["current_database_bytes"] += 1024
    second["partitions"][0]["trade_files"][0]["url"] = (
        "https://static.okx.com/alternate/"
        + second["partitions"][0]["trade_files"][0]["filename"]
    )
    second["manifest_fingerprint"] = (
        historical_campaign._campaign_manifest_fingerprint(second)
    )

    validate_campaign_manifest(first)
    validate_campaign_manifest(second)
    assert first["manifest_fingerprint"] != second["manifest_fingerprint"]
    assert historical_campaign._campaign_operation_key(
        first
    ) == historical_campaign._campaign_operation_key(second)


def test_v2_manifest_content_fingerprint_excludes_registry_uuid() -> None:
    manifest = _one_day_manifest()
    original_fingerprint = manifest["manifest_fingerprint"]
    manifest["instrument_snapshot_source_id"] = (
        "00000000-0000-0000-0000-000000000098"
    )

    assert validate_campaign_manifest(manifest) == original_fingerprint


class _StartResult:
    def __init__(self, row: dict | None = None, *, rowcount: int = 0):
        self.row = row
        self.rowcount = rowcount

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
        statement = str(_statement)
        if "FROM meta.data_source_registry" in statement:
            snapshot = _snapshot_for(datetime(2026, 8, 1, tzinfo=UTC))
            return _StartResult(
                {
                    "source_key": instrument_contract_snapshot_source_key(snapshot),
                    "source_kind": "third_party",
                    "provider": "OKX",
                    "source_locator": snapshot.source_locator,
                    "schema_version": snapshot.source_schema,
                    "truth_tier": "external_unverified",
                    "source_metadata": {
                        "record_type": "instrument_contract_snapshot_v1",
                        "identity": instrument_contract_snapshot_registry_identity(
                            snapshot
                        ),
                        "snapshot": snapshot.to_dict(),
                    },
                }
            )
        if statement.lstrip().startswith("UPDATE meta.historical_campaign_runs"):
            return _StartResult(rowcount=1)
        return _StartResult(self.row if self.calls == 1 else None)


class _LegacyTransitionSession:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement, _params):
        self.statements.append(str(statement))
        return _StartResult({"manifest": _legacy_manifest()})


@pytest.mark.parametrize("transition", ["checkpoint", "finish"])
def test_legacy_v1_running_campaign_cannot_mutate_state(transition: str) -> None:
    session = _LegacyTransitionSession()
    with pytest.raises(
        RuntimeError,
        match="execution_unavailable_until_persistent_fencing_and_immutable_silver",
    ):
        if transition == "checkpoint":
            update_campaign_checkpoint(
                session,
                "00000000-0000-0000-0000-000000000001",
                checkpoint_key="download:trades",
                payload={"status": "succeeded"},
            )
        else:
            finish_campaign(
                session,
                "00000000-0000-0000-0000-000000000001",
                succeeded=False,
                error_type="test_failure",
            )

    assert session.statements == []


def test_start_campaign_is_frozen_before_database_access() -> None:
    class _NoDatabaseAccess:
        def execute(self, *_args):  # pragma: no cover - safety assertion
            raise AssertionError("frozen execution must fail before database access")

    with pytest.raises(
        RuntimeError,
        match="execution_unavailable_until_persistent_fencing_and_immutable_silver",
    ):
        start_campaign(_NoDatabaseAccess(), CAMPAIGN_ID)


def test_resume_campaign_is_frozen_before_database_access() -> None:
    session = _StartSession({})
    with pytest.raises(
        RuntimeError,
        match="execution_unavailable_until_persistent_fencing_and_immutable_silver",
    ):
        start_campaign(
            session,
            CAMPAIGN_ID,
            resume_running=True,
        )

    assert session.calls == 0


class _LeaseStateSession:
    def __init__(self, manifest: dict, *, status: str = "PLANNED") -> None:
        self.row = {
            "campaign_id": CAMPAIGN_ID,
            "status": status,
            "capacity_report": manifest["capacity_report"],
            "manifest": manifest,
            "checkpoint": {},
        }
        self.updates = 0

    def execute(self, statement, params):
        sql = str(statement)
        if sql.lstrip().startswith("SELECT"):
            return _StartResult(dict(self.row))
        self.updates += 1
        if "SET status = 'RUNNING'" in sql:
            self.row["status"] = "RUNNING"
            self.row["checkpoint"].pop("_terminal", None)
            self.row["checkpoint"]["_execution"] = {
                "schema": params["authority_schema"],
                "attempt_id": params["attempt_id"],
                "token_sha256": params["token_sha256"],
            }
        elif "SET checkpoint = checkpoint" in sql:
            self.row["checkpoint"][params["checkpoint_key"]] = json.loads(
                params["payload"]
            )
        elif "SET status = :status" in sql:
            self.row["status"] = params["status"]
        return _StartResult(rowcount=1)


def test_campaign_resume_never_creates_or_rotates_attempt_state() -> None:
    session = _LeaseStateSession(_one_day_manifest(), status="RUNNING")

    with pytest.raises(
        RuntimeError,
        match="execution_unavailable_until_persistent_fencing_and_immutable_silver",
    ):
        start_campaign(session, CAMPAIGN_ID, resume_running=True)

    assert session.updates == 0


def test_campaign_execution_freeze_exposes_no_lease_api() -> None:
    assert not hasattr(historical_campaign, "CampaignExecutionLease")
    assert not hasattr(historical_campaign, "campaign_execution_lease")


def test_campaign_execution_freeze_reason_is_stable() -> None:
    assert historical_campaign.CAMPAIGN_EXECUTION_UNAVAILABLE_REASON == (
        "historical_campaign_execution_unavailable_until_"
        "persistent_fencing_and_immutable_silver"
    )


@pytest.mark.parametrize("transition", ["checkpoint", "finish_failure", "finish_success"])
def test_campaign_mutations_are_frozen_before_database_access(transition: str) -> None:
    class _NoDatabaseAccess:
        def execute(self, *_args):  # pragma: no cover - safety assertion
            raise AssertionError("frozen transition must fail before database access")

    with pytest.raises(
        RuntimeError,
        match="execution_unavailable_until_persistent_fencing_and_immutable_silver",
    ):
        if transition == "checkpoint":
            update_campaign_checkpoint(
                _NoDatabaseAccess(),
                CAMPAIGN_ID,
                checkpoint_key="candle:15m",
                payload={"status": "succeeded"},
            )
        else:
            finish_campaign(
                _NoDatabaseAccess(),
                CAMPAIGN_ID,
                succeeded=transition == "finish_success",
                error_type=None if transition == "finish_success" else "attacker",
            )


@pytest.mark.parametrize("reserved", ("_execution", "_terminal"))
def test_checkpoint_cannot_overwrite_reserved_authority_keys(
    reserved: str,
) -> None:
    class _NoDatabaseAccess:
        def execute(self, *_args):  # pragma: no cover - safety assertion
            raise AssertionError("reserved keys must fail before database access")

    with pytest.raises(
        RuntimeError,
        match="execution_unavailable_until_persistent_fencing_and_immutable_silver",
    ):
        update_campaign_checkpoint(
            _NoDatabaseAccess(),
            CAMPAIGN_ID,
            checkpoint_key=reserved,
            payload={},
        )


@pytest.mark.parametrize("status", ("RUNNING", "SUCCEEDED"))
def test_campaign_success_never_short_circuits_without_sealed_verifier(
    status: str,
) -> None:
    session = _LeaseStateSession(_one_day_manifest(), status=status)

    with pytest.raises(
        RuntimeError,
        match="execution_unavailable_until_persistent_fencing_and_immutable_silver",
    ):
        if status == "RUNNING":
            finish_campaign(
                session,
                CAMPAIGN_ID,
                succeeded=True,
            )
        else:
            start_campaign(
                session,
                CAMPAIGN_ID,
            )

    assert session.updates == 0


def test_capacity_policy_cannot_be_reduced_inside_refingerprinted_manifest() -> None:
    manifest = _one_day_manifest()
    report = manifest["capacity_report"]
    report["calibrated_database_bytes_per_day"] = 0
    report["calibrated_raw_bytes_per_day"] = 0
    report["database_overhead_multiplier"] = 0.0
    report["raw_copy_multiplier"] = 0.0
    report["projected_incremental_bytes"] = 0
    manifest["manifest_fingerprint"] = historical_campaign._campaign_manifest_fingerprint(
        manifest
    )

    with pytest.raises(ValueError, match="capacity_report_invalid"):
        validate_campaign_manifest(manifest)


def test_capacity_assessment_rejects_caller_policy_override() -> None:
    with pytest.raises(ValueError, match="capacity_policy_override_forbidden"):
        assess_campaign_capacity(
            requested_days=1,
            current_database_bytes=1,
            disk_total_bytes=1_000_000_000_000,
            disk_free_bytes=900_000_000_000,
            database_bytes_per_day=0,
            raw_bytes_per_day=0,
        )


def test_declared_size_canonicalization_is_decimal_context_independent() -> None:
    raw = "1.123456789012345678901234567890123456789"
    with localcontext() as context:
        context.prec = 10
        low_precision = historical_campaign._normalize_declared_size_mb(raw)
    with localcontext() as context:
        context.prec = 50
        high_precision = historical_campaign._normalize_declared_size_mb(raw)

    assert low_precision == raw
    assert high_precision == raw


@pytest.mark.parametrize("size_mb", ("0", "-1", "NaN", "01.0", "999999999999"))
def test_manifest_rejects_unsafe_or_noncanonical_declared_size(size_mb: str) -> None:
    manifest = _one_day_manifest()
    manifest["partitions"][0]["l2_file"]["size_mb"] = size_mb
    manifest["manifest_fingerprint"] = historical_campaign._campaign_manifest_fingerprint(
        manifest
    )

    with pytest.raises(ValueError, match="historical_campaign_partitions_invalid"):
        validate_campaign_manifest(manifest)


def test_manifest_rejects_aggregate_unique_download_budget() -> None:
    manifest = _one_day_manifest()
    for item in (
        *manifest["partitions"][0]["trade_files"],
        manifest["partitions"][0]["l2_file"],
    ):
        item["size_mb"] = "500"
    manifest["manifest_fingerprint"] = historical_campaign._campaign_manifest_fingerprint(
        manifest
    )

    with pytest.raises(ValueError, match="download_budget_exceeded"):
        validate_campaign_manifest(manifest)


def test_verified_download_enforces_length_timeout_and_manual_redirect(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "BTC-USDT-SWAP-trades-2026-08-01.zip"
    url = f"https://static.okx.com/cdn/{target.name}"
    monkeypatch.setattr(historical_campaign, "_assert_download_capacity", lambda *_args: None)
    observed_timeout = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal observed_timeout
        observed_timeout = request.extensions["timeout"]
        return httpx.Response(
            302,
            headers={"location": f"https://evil.example/{target.name}"},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="download_redirect_rejected"):
            download_verified_file(
                client,
                url=url,
                target=target,
                maximum_download_bytes=1024,
            )

    assert observed_timeout == {
        "connect": 10.0,
        "read": 60.0,
        "write": 10.0,
        "pool": 10.0,
    }
    assert not target.exists()


def test_verified_download_rejects_oversized_content_length_before_write(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "BTC-USDT-SWAP-trades-2026-08-01.zip"
    url = f"https://static.okx.com/cdn/{target.name}"
    monkeypatch.setattr(historical_campaign, "_assert_download_capacity", lambda *_args: None)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x", headers={"content-length": "2048"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="download_size_limit_exceeded"):
            download_verified_file(
                client,
                url=url,
                target=target,
                maximum_download_bytes=1024,
            )

    assert not target.exists()


def test_verified_download_rejects_stream_bytes_beyond_declared_length(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "BTC-USDT-SWAP-trades-2026-08-01.zip"
    url = f"https://static.okx.com/cdn/{target.name}"
    monkeypatch.setattr(historical_campaign, "_assert_download_capacity", lambda *_args: None)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"abcdef", headers={"content-length": "5"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="download_length_mismatch"):
            download_verified_file(
                client,
                url=url,
                target=target,
                maximum_download_bytes=1024,
            )

    assert not target.exists()
    assert not target.with_name(target.name + ".part").exists()


def test_verified_download_enforces_total_wall_clock_deadline(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "BTC-USDT-SWAP-trades-2026-08-01.zip"
    url = f"https://static.okx.com/cdn/{target.name}"
    monkeypatch.setattr(historical_campaign, "_assert_download_capacity", lambda *_args: None)
    ticks = iter((0.0, historical_campaign.DOWNLOAD_TOTAL_TIMEOUT_SECONDS + 1.0))
    monkeypatch.setattr(historical_campaign.time, "monotonic", lambda: next(ticks))

    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                content=b"x",
                headers={"content-length": "1"},
            )
        )
    ) as client:
        with pytest.raises(RuntimeError, match="download_total_timeout"):
            download_verified_file(
                client,
                url=url,
                target=target,
                maximum_download_bytes=1024,
            )

    assert not target.exists()


def test_verified_download_checks_reserve_before_network(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "BTC-USDT-SWAP-trades-2026-08-01.zip"
    url = f"https://static.okx.com/cdn/{target.name}"
    requests = 0
    usage = type("Usage", (), {"total": 1_000_000_000_000, "free": 1})()
    monkeypatch.setattr(historical_campaign.shutil, "disk_usage", lambda _path: usage)

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, content=b"x")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="capacity_reserve_breached"):
            download_verified_file(
                client,
                url=url,
                target=target,
                maximum_download_bytes=1024,
            )

    assert requests == 0
