from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aats.data_platform.data_governance.registry import (
    finalize_historical_bundle,
    import_source_record,
    persist_historical_bundle,
    reserve_historical_bundle,
)


START = datetime(2026, 8, 26, tzinfo=timezone.utc)


class _Result:
    def scalar_one_or_none(self) -> str:
        return "00000000-0000-0000-0000-000000000123"


class _Session:
    sql = ""
    params = None

    def execute(self, statement, params):
        self.sql = str(statement)
        self.params = params
        return _Result()


def _source(*, gaps=(), row_count: int = 10):
    return import_source_record(
        source_key="okx-bulk:l2:v1",
        source_kind="okx_bulk",
        provider="OKX",
        source_locator="official file",
        coverage_start=START,
        coverage_end=START + timedelta(days=1),
        timestamp_semantics="exchange event time",
        schema_version="okx-v5",
        dataset_version="rdp-official-history-v1",
        transform_version="causal-v1",
        git_commit="a" * 40,
        raw_partition_sha256=("b" * 64,),
        row_count=row_count,
        gaps=gaps,
        retrieved_at=START + timedelta(days=1),
    )


def test_l2_bundle_is_eligible_only_with_complete_causal_evidence() -> None:
    session = _Session()

    bundle_id, report = persist_historical_bundle(
        session,
        source_id="00000000-0000-0000-0000-000000000001",
        source=_source(),
        symbol="BTC-USDT-SWAP",
        role="l2_event_history",
        purpose="l2_replay",
        coverage_ratio=1.0,
        causal_time_check=True,
    )

    assert bundle_id.endswith("0123")
    assert report.eligible is True
    assert session.params["status"] == "ELIGIBLE"
    assert "ON CONFLICT (bundle_key)" in session.sql
    assert "dataset_bundles.fingerprint = EXCLUDED.fingerprint" in session.sql


def test_classified_gap_relies_on_coverage_and_causality_gates() -> None:
    session = _Session()
    source = _source(
        gaps=(
            {
                "reason": "sequence_discontinuity",
                "gap_start": START.isoformat(),
                "gap_end": (START + timedelta(seconds=1)).isoformat(),
            },
        )
    )

    _, report = persist_historical_bundle(
        session,
        source_id="00000000-0000-0000-0000-000000000001",
        source=source,
        symbol="BTC-USDT-SWAP",
        role="l2_event_history",
        purpose="l2_replay",
        coverage_ratio=0.9,
        causal_time_check=False,
    )

    assert report.eligible is False
    assert "known_gaps:okx-bulk:l2:v1" not in report.reason_codes
    assert "coverage_ratio_below_minimum:okx-bulk:l2:v1" in report.reason_codes
    assert "causal_time_check_failed:okx-bulk:l2:v1" in report.reason_codes
    assert session.params["status"] == "INELIGIBLE"


def test_classified_boundary_gap_is_eligible_above_coverage_floor() -> None:
    session = _Session()
    source = _source(
        gaps=(
            {
                "reason": "state_unavailable",
                "gap_start": START.isoformat(),
                "gap_end": (START + timedelta(milliseconds=500)).isoformat(),
                "missing_samples": 1,
            },
        )
    )

    _, report = persist_historical_bundle(
        session,
        source_id="00000000-0000-0000-0000-000000000001",
        source=source,
        symbol="BTC-USDT-SWAP",
        role="l2_event_history",
        purpose="l2_replay",
        coverage_ratio=0.99999,
        causal_time_check=True,
    )

    assert report.eligible is True
    assert report.policy.policy_version == "historical-research-v2"
    assert report.reason_codes == ()
    assert session.params["status"] == "ELIGIBLE"


def test_building_reservation_is_finalized_with_derived_gap_evidence() -> None:
    class _MappingResult:
        def mappings(self):
            return self

        def one_or_none(self):
            return {
                "bundle_id": "00000000-0000-0000-0000-000000000123",
                "status": "BUILDING",
                "fingerprint": "reservation",
            }

    class _FinalResult:
        def scalar_one_or_none(self):
            return "00000000-0000-0000-0000-000000000123"

    class _ReservationSession:
        def __init__(self) -> None:
            self.calls = []

        def execute(self, statement, params):
            self.calls.append((str(statement), params))
            return _MappingResult() if len(self.calls) == 1 else _FinalResult()

    session = _ReservationSession()
    preliminary = _source()
    bundle_id, reservation = reserve_historical_bundle(
        session,
        source_id="00000000-0000-0000-0000-000000000001",
        source=preliminary,
        symbol="BTC-USDT-SWAP",
        role="l2_event_history",
        purpose="l2_replay",
    )
    final = _source(
        gaps=(
            {
                "reason": "state_unavailable",
                "gap_start": START.isoformat(),
                "gap_end": (START + timedelta(seconds=1)).isoformat(),
            },
        )
    )
    finalized_id, report = finalize_historical_bundle(
        session,
        bundle_id=bundle_id,
        reservation_fingerprint=reservation,
        source_id="00000000-0000-0000-0000-000000000001",
        source=final,
        symbol="BTC-USDT-SWAP",
        role="l2_event_history",
        purpose="l2_replay",
        coverage_ratio=0.9,
        causal_time_check=False,
    )

    assert finalized_id == bundle_id
    assert report.eligible is False
    assert session.calls[0][1]["bundle_key"] == session.calls[1][1]["bundle_key"]
    assert "status = 'BUILDING'" in session.calls[1][0]


def test_bundle_rejects_unsafe_or_noncanonical_symbol() -> None:
    with pytest.raises(ValueError, match="bundle_symbol_invalid"):
        persist_historical_bundle(
            _Session(),
            source_id="00000000-0000-0000-0000-000000000001",
            source=_source(),
            symbol="btc/usdt",
            role="trades",
            purpose="trade_flow_research",
            coverage_ratio=1.0,
            causal_time_check=True,
        )
