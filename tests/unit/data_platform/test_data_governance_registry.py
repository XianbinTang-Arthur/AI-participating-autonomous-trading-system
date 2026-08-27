from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from aats.data_platform.data_governance.registry import (
    finalize_historical_bundle,
    import_source_record,
    persist_historical_bundle,
    register_instrument_contract_snapshot_source,
    reserve_historical_bundle,
)
from aats.domain.instrument_contract_snapshot import (
    instrument_contract_observation_window_from_metadata,
)
from aats.schemas.exchange import InstrumentMetadata


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


def _snapshot():
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
        first_observed_at=START,
        last_observed_at=START + timedelta(days=1),
        observation_evidence_sha256="e" * 64,
        source_locator="immutable://test/instrument-observation-window",
    )


def _source(
    *,
    gaps=(),
    row_count: int = 10,
    bound: bool = True,
    retrieved_at: datetime | None = None,
):
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
        retrieved_at=retrieved_at or START + timedelta(days=1),
        instrument_contract_snapshot=_snapshot() if bound else None,
    )


def test_l2_bundle_is_eligible_only_with_complete_causal_evidence() -> None:
    session = _Session()

    bundle_id, report = persist_historical_bundle(
        session,
        source_id="00000000-0000-0000-0000-000000000001",
        source=_source(bound=False),
        symbol="BTC-USDT",
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
    assert "#- '{0,provenance,retrieved_at}'" in session.sql
    assert "dataset_bundles.eligibility_report = EXCLUDED.eligibility_report" in session.sql


def test_exact_bundle_retry_ignores_only_retrieval_audit_timestamp() -> None:
    first_session = _Session()
    second_session = _Session()
    first_source = _source(
        bound=False,
        retrieved_at=START + timedelta(hours=1),
    )
    second_source = _source(
        bound=False,
        retrieved_at=START + timedelta(hours=2),
    )

    persist_historical_bundle(
        first_session,
        source_id="00000000-0000-0000-0000-000000000001",
        source=first_source,
        symbol="BTC-USDT",
        role="l2_event_history",
        purpose="l2_replay",
        coverage_ratio=1.0,
        causal_time_check=True,
    )
    persist_historical_bundle(
        second_session,
        source_id="00000000-0000-0000-0000-000000000001",
        source=second_source,
        symbol="BTC-USDT",
        role="l2_event_history",
        purpose="l2_replay",
        coverage_ratio=1.0,
        causal_time_check=True,
    )

    assert first_session.params["bundle_key"] == second_session.params["bundle_key"]
    assert first_session.params["fingerprint"] == second_session.params["fingerprint"]
    first_components = json.loads(first_session.params["components"])
    second_components = json.loads(second_session.params["components"])
    assert (
        first_components[0]["provenance"].pop("retrieved_at")
        != second_components[0]["provenance"].pop("retrieved_at")
    )
    assert first_components == second_components
    assert "#- '{0,provenance,retrieved_at}'" in second_session.sql


def test_classified_gap_relies_on_coverage_and_causality_gates() -> None:
    session = _Session()
    source = _source(
        gaps=(
            {
                "reason": "sequence_discontinuity",
                "gap_start": START.isoformat(),
                "gap_end": (START + timedelta(seconds=1)).isoformat(),
            },
        ),
        bound=False,
    )

    _, report = persist_historical_bundle(
        session,
        source_id="00000000-0000-0000-0000-000000000001",
        source=source,
        symbol="BTC-USDT",
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
        ),
        bound=False,
    )

    _, report = persist_historical_bundle(
        session,
        source_id="00000000-0000-0000-0000-000000000001",
        source=source,
        symbol="BTC-USDT",
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
            sql = str(statement)
            self.calls.append((sql, params))
            if "INSERT INTO meta.dataset_bundles" in sql and "'BUILDING'" in sql:
                return _MappingResult()
            return _FinalResult()

    session = _ReservationSession()
    preliminary = _source(bound=False)
    bundle_id, reservation = reserve_historical_bundle(
        session,
        source_id="00000000-0000-0000-0000-000000000001",
        source=preliminary,
        symbol="BTC-USDT",
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
        ),
        bound=False,
    )
    finalized_id, report = finalize_historical_bundle(
        session,
        bundle_id=bundle_id,
        reservation_fingerprint=reservation,
        source_id="00000000-0000-0000-0000-000000000001",
        source=final,
        symbol="BTC-USDT",
        role="l2_event_history",
        purpose="l2_replay",
        coverage_ratio=0.9,
        causal_time_check=False,
    )

    assert finalized_id == bundle_id
    assert report.eligible is False
    bundle_calls = [
        call for call in session.calls if "meta.dataset_bundles" in call[0]
    ]
    assert bundle_calls[0][1]["bundle_key"] == bundle_calls[1][1]["bundle_key"]
    assert "status = 'BUILDING'" in bundle_calls[1][0]


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


@pytest.mark.parametrize(
    "operation",
    ("persist", "reserve", "finalize"),
)
def test_bundle_rejects_noncanonical_source_uuid_before_database_side_effect(
    operation: str,
) -> None:
    class _NoDatabaseAccess:
        def execute(self, *_args, **_kwargs):  # pragma: no cover - safety assertion
            raise AssertionError("noncanonical source UUID must fail before DB access")

    common = {
        "session": _NoDatabaseAccess(),
        "source_id": "{00000000-0000-0000-0000-000000000001}",
        "source": _source(),
        "symbol": "BTC-USDT-SWAP",
        "role": "l2_event_history",
        "purpose": "l2_replay",
    }
    with pytest.raises(ValueError, match="bundle_source_id_invalid"):
        if operation == "persist":
            persist_historical_bundle(
                **common,
                coverage_ratio=1.0,
                causal_time_check=True,
            )
        elif operation == "reserve":
            reserve_historical_bundle(**common)
        else:
            finalize_historical_bundle(
                **common,
                bundle_id="00000000-0000-0000-0000-000000000123",
                reservation_fingerprint="a" * 64,
                coverage_ratio=1.0,
                causal_time_check=True,
            )


def test_derivative_bundle_without_snapshot_is_ineligible_but_still_auditable() -> None:
    session = _Session()

    _, report = persist_historical_bundle(
        session,
        source_id="00000000-0000-0000-0000-000000000001",
        source=_source(bound=False),
        symbol="BTC-USDT-SWAP",
        role="l2_event_history",
        purpose="l2_replay",
        coverage_ratio=1.0,
        causal_time_check=True,
    )

    assert report.eligible is False
    assert report.reason_codes == ("derivative_instrument_metadata_required",)
    assert session.params["status"] == "INELIGIBLE"
    persisted_report = json.loads(session.params["report"])
    assert persisted_report["instrument_contract_binding"]["eligible"] is False


def test_unverified_observation_window_is_not_registered_as_authoritative() -> None:
    session = _Session()

    register_instrument_contract_snapshot_source(session, _snapshot())

    assert session.params["source_kind"] == "third_party"
    assert session.params["truth_tier"] == "external_unverified"
