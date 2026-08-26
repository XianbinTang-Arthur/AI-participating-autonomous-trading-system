from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aats.data_platform.data_governance.historical_rebuild import (
    HistoricalRebuildPlan,
    TRANSFORM_VERSION,
    _verify_source_material,
    plan_historical_rebuild,
)


START = datetime(2026, 8, 25, tzinfo=timezone.utc)


class _Rows:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def one_or_none(self):
        return self.row


class _Session:
    def __init__(self, row):
        self.row = row

    def execute(self, _statement, _params):
        return _Rows(self.row)


def _bundle_row(*, status: str = "ELIGIBLE", purpose: str = "l2_replay"):
    return {
        "bundle_id": "00000000-0000-0000-0000-000000000123",
        "fingerprint": "b" * 64,
        "purpose": purpose,
        "status": status,
        "coverage_start": START,
        "coverage_end": START + timedelta(days=1),
        "component_sources": [
            {
                "source_id": "00000000-0000-0000-0000-000000000001",
                "symbol": "BTC-USDT-SWAP",
                "role": "l2_event_history",
                "provenance": {
                    "source_key": "okx-bulk:l2:v1",
                    "row_count": 10,
                    "gap_manifest": {"raw_partition_sha256": ["a" * 64]},
                },
            }
        ],
    }


def test_rebuild_plan_is_deterministic_and_bundle_scoped() -> None:
    first = plan_historical_rebuild(
        _Session(_bundle_row()),
        bundle_id="00000000-0000-0000-0000-000000000123",
        git_commit="a" * 40,
    )
    second = plan_historical_rebuild(
        _Session(_bundle_row()),
        bundle_id="00000000-0000-0000-0000-000000000123",
        git_commit="a" * 40,
    )

    assert first == second
    assert first.operation_key.startswith("hist-rebuild-")
    assert first.symbol == "BTC-USDT-SWAP"
    assert first.source_row_count == 10
    assert first.raw_partition_sha256 == ("a" * 64,)
    assert first.transform_version == TRANSFORM_VERSION


def test_rebuild_plan_fails_closed_for_ineligible_or_unsupported_bundle() -> None:
    with pytest.raises(ValueError, match="historical_bundle_not_eligible"):
        plan_historical_rebuild(
            _Session(_bundle_row(status="INELIGIBLE")),
            bundle_id="00000000-0000-0000-0000-000000000123",
            git_commit="a" * 40,
        )
    with pytest.raises(ValueError, match="historical_bundle_purpose_not_rebuildable"):
        plan_historical_rebuild(
            _Session(_bundle_row(purpose="mark_price_research")),
            bundle_id="00000000-0000-0000-0000-000000000123",
            git_commit="a" * 40,
        )


def test_rebuild_plan_requires_a_real_git_commit_and_nonempty_source() -> None:
    with pytest.raises(ValueError, match="historical_rebuild_git_commit_invalid"):
        plan_historical_rebuild(
            _Session(_bundle_row()),
            bundle_id="00000000-0000-0000-0000-000000000123",
            git_commit="unknown",
        )

    row = _bundle_row()
    row["component_sources"][0]["provenance"]["row_count"] = 0
    with pytest.raises(ValueError, match="historical_bundle_source_material_invalid"):
        plan_historical_rebuild(
            _Session(row),
            bundle_id="00000000-0000-0000-0000-000000000123",
            git_commit="a" * 40,
        )


class _MaterialRows:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def one(self):
        return self.row


class _MaterialSession:
    def __init__(self, row):
        self.row = row

    def execute(self, _statement, _params):
        return _MaterialRows(self.row)


def test_rebuild_source_material_requires_exact_partition_hash_set() -> None:
    plan = HistoricalRebuildPlan(
        operation_key="hist-rebuild-test",
        bundle_id="00000000-0000-0000-0000-000000000123",
        bundle_fingerprint="b" * 64,
        purpose="l2_replay",
        symbol="BTC-USDT-SWAP",
        coverage_start=START,
        coverage_end=START + timedelta(days=1),
        source_id="00000000-0000-0000-0000-000000000001",
        source_row_count=10,
        raw_partition_sha256=("a" * 64, "b" * 64),
        transform_version=TRANSFORM_VERSION,
        git_commit="c" * 40,
    )

    with pytest.raises(
        RuntimeError,
        match="historical_bundle_source_partition_mismatch",
    ):
        _verify_source_material(
            _MaterialSession(
                {
                    "row_count": 10,
                    "raw_hashes": ["a" * 64],
                }
            ),
            plan,
        )
