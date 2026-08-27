from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from aats.data_platform.data_governance import historical_rebuild
from aats.data_platform.data_governance.historical_rebuild import (
    HistoricalRebuildPlan,
    TRANSFORM_VERSION,
    _plan_scope_payload,
    _verify_source_material,
    plan_historical_rebuild,
    start_historical_rebuild,
    verified_historical_rebuild_output_fingerprints,
)
from aats.data_platform.data_governance.instrument_lineage import (
    instrument_contract_snapshot_registry_identity,
    instrument_contract_snapshot_source_key,
    instrument_snapshot_temporal_evidence_reason,
)
from aats.domain.instrument_contract_snapshot import (
    instrument_contract_observation_window_from_metadata,
)
from aats.schemas.exchange import InstrumentMetadata


START = datetime(2026, 8, 25, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _simulate_future_manifest_verifier(monkeypatch) -> None:
    monkeypatch.setattr(
        historical_rebuild,
        "instrument_snapshot_temporal_evidence_reason",
        lambda _snapshot: None,
    )


class _Rows:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def one_or_none(self):
        return self.row

    def one(self):
        return self.row


class _Session:
    def __init__(self, row, *, registry_row=None):
        self.row = row
        self.registry_row = registry_row or _snapshot_registry_row()
        self.statements: list[str] = []

    def execute(self, _statement, _params):
        statement = str(_statement)
        self.statements.append(statement)
        if "FROM meta.data_source_registry" in statement:
            return _Rows(self.registry_row)
        return _Rows(self.row)


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


def _snapshot_registry_row():
    snapshot = _snapshot()
    return {
        "source_key": instrument_contract_snapshot_source_key(snapshot),
        "source_kind": "third_party",
        "provider": "OKX",
        "source_locator": snapshot.source_locator,
        "schema_version": snapshot.source_schema,
        "truth_tier": "external_unverified",
        "source_metadata": {
            "record_type": "instrument_contract_snapshot_v1",
            "identity": instrument_contract_snapshot_registry_identity(snapshot),
            "snapshot": snapshot.to_dict(),
        },
    }


def _bundle_row(
    *,
    status: str = "ELIGIBLE",
    purpose: str = "l2_replay",
    bundle_id: str = "00000000-0000-0000-0000-000000000123",
    source_id: str = "00000000-0000-0000-0000-000000000001",
    coverage_start: datetime = START,
    coverage_end: datetime = START + timedelta(days=1),
    symbol: str = "BTC-USDT-SWAP",
    with_snapshot: bool = True,
):
    snapshot = _snapshot()
    component = {
        "source_id": source_id,
        "symbol": symbol,
        "role": "l2_event_history",
        "provenance": {
            "source_key": "okx-bulk:l2:v1",
            "row_count": 10,
            "gap_manifest": {"raw_partition_sha256": ["a" * 64]},
        },
    }
    if with_snapshot:
        component.update(
            {
                "instrument_snapshot_digest": snapshot.digest,
                "instrument_snapshot_source_id": (
                    "00000000-0000-0000-0000-000000000099"
                ),
            }
        )
        component["provenance"]["instrument_contract_snapshot"] = snapshot.to_dict()
    return {
        "bundle_id": bundle_id,
        "bundle_key": "l2:stable-content-key",
        "fingerprint": "b" * 64,
        "purpose": purpose,
        "status": status,
        "coverage_start": coverage_start,
        "coverage_end": coverage_end,
        "component_sources": [component],
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


def test_spot_rebuild_plan_does_not_require_derivative_snapshot() -> None:
    plan = plan_historical_rebuild(
        _Session(_bundle_row(symbol="BTC-USDT", with_snapshot=False)),
        bundle_id="00000000-0000-0000-0000-000000000123",
        git_commit="a" * 40,
    )

    assert plan.symbol == "BTC-USDT"
    assert plan.instrument_snapshot_digest is None
    assert plan.instrument_snapshot_source_id is None


def test_spot_rebuild_rejects_partial_contract_material() -> None:
    row = _bundle_row(symbol="BTC-USDT", with_snapshot=False)
    row["component_sources"][0]["instrument_snapshot_digest"] = "a" * 64

    with pytest.raises(
        ValueError,
        match="historical_rebuild_instrument_contract_binding_invalid",
    ):
        plan_historical_rebuild(
            _Session(row),
            bundle_id="00000000-0000-0000-0000-000000000123",
            git_commit="a" * 40,
        )


def test_rebuild_rejects_unknown_instrument_scope() -> None:
    with pytest.raises(
        ValueError,
        match="instrument_scope_unsupported_or_unproven",
    ):
        plan_historical_rebuild(
            _Session(
                _bundle_row(
                    symbol="BTC-USDT-260925",
                    with_snapshot=False,
                )
            ),
            bundle_id="00000000-0000-0000-0000-000000000123",
            git_commit="a" * 40,
        )


def test_rebuild_operation_identity_excludes_database_generated_ids() -> None:
    first = plan_historical_rebuild(
        _Session(_bundle_row()),
        bundle_id="00000000-0000-0000-0000-000000000123",
        git_commit="a" * 40,
    )
    second = plan_historical_rebuild(
        _Session(
            _bundle_row(
                bundle_id="00000000-0000-0000-0000-000000000456",
                source_id="00000000-0000-0000-0000-000000000002",
            )
        ),
        bundle_id="00000000-0000-0000-0000-000000000456",
        git_commit="a" * 40,
    )

    assert first.operation_key == second.operation_key


def test_rebuild_operation_identity_normalizes_timezone_offsets() -> None:
    first = plan_historical_rebuild(
        _Session(_bundle_row()),
        bundle_id="00000000-0000-0000-0000-000000000123",
        git_commit="a" * 40,
    )
    offset = timezone(timedelta(hours=8))
    shifted_start = START.astimezone(offset)
    second = plan_historical_rebuild(
        _Session(
            _bundle_row(
                coverage_start=shifted_start,
                coverage_end=shifted_start + timedelta(days=1),
            )
        ),
        bundle_id="00000000-0000-0000-0000-000000000123",
        git_commit="a" * 40,
    )

    assert first.operation_key == second.operation_key
    shifted_plan = replace(
        first,
        coverage_start=first.coverage_start.astimezone(offset),
        coverage_end=first.coverage_end.astimezone(offset),
    )
    assert _plan_scope_payload(first) == _plan_scope_payload(shifted_plan)


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


def test_rebuild_plan_reanchors_embedded_snapshot_in_registry() -> None:
    registry_row = _snapshot_registry_row()
    registry_row["source_locator"] = "tampered://registry-row"

    with pytest.raises(ValueError, match="instrument_snapshot_source_anchor_mismatch"):
        plan_historical_rebuild(
            _Session(_bundle_row(), registry_row=registry_row),
            bundle_id="00000000-0000-0000-0000-000000000123",
            git_commit="a" * 40,
        )


def test_rebuild_rejects_self_declared_observation_window(monkeypatch) -> None:
    monkeypatch.setattr(
        historical_rebuild,
        "instrument_snapshot_temporal_evidence_reason",
        instrument_snapshot_temporal_evidence_reason,
    )

    with pytest.raises(
        ValueError,
        match="instrument_snapshot_observation_evidence_unverified",
    ):
        plan_historical_rebuild(
            _Session(_bundle_row()),
            bundle_id="00000000-0000-0000-0000-000000000123",
            git_commit="a" * 40,
        )


def test_rebuild_plan_rejects_malformed_snapshot_source_reference() -> None:
    malformed = _bundle_row()
    malformed["component_sources"][0]["instrument_snapshot_source_id"] = "bad"
    with pytest.raises(
        ValueError,
        match="instrument_snapshot_source_reference_invalid",
    ):
        plan_historical_rebuild(
            _Session(malformed),
            bundle_id="00000000-0000-0000-0000-000000000123",
            git_commit="a" * 40,
        )


def test_rebuild_start_verifies_current_plan_before_state_side_effect(
    monkeypatch,
) -> None:
    plan = HistoricalRebuildPlan(
        operation_key="hist-rebuild-test",
        bundle_id="00000000-0000-0000-0000-000000000123",
        bundle_fingerprint="b" * 64,
        bundle_key="l2:stable-content-key",
        purpose="l2_replay",
        symbol="BTC-USDT-SWAP",
        coverage_start=START,
        coverage_end=START + timedelta(days=1),
        source_id="00000000-0000-0000-0000-000000000001",
        source_key="okx-bulk:l2:v1",
        instrument_snapshot_digest=_snapshot().digest,
        instrument_snapshot_source_id=(
            "00000000-0000-0000-0000-000000000099"
        ),
        source_row_count=10,
        raw_partition_sha256=("a" * 64,),
        transform_version=TRANSFORM_VERSION,
        git_commit="c" * 40,
    )
    verified: list[HistoricalRebuildPlan] = []

    def reject_stale_plan(_session, observed_plan):
        verified.append(observed_plan)
        raise RuntimeError("historical_bundle_changed_or_ineligible")

    monkeypatch.setattr(
        historical_rebuild,
        "_verify_plan_is_current",
        reject_stale_plan,
    )

    class _NoStateWrite:
        def execute(self, *_args, **_kwargs):  # pragma: no cover - safety assertion
            raise AssertionError("state write must not precede plan verification")

    with pytest.raises(
        RuntimeError,
        match="historical_bundle_changed_or_ineligible",
    ):
        start_historical_rebuild(_NoStateWrite(), plan)

    assert verified == [plan]


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    [
        ("source_id", "00000000-0000-0000-0000-000000000777"),
        ("source_key", "okx-bulk:l2:other"),
        ("source_row_count", 11),
        ("raw_partition_sha256", ("f" * 64,)),
        ("coverage_end", START + timedelta(hours=12)),
    ],
)
def test_rebuild_start_rejects_hand_built_plan_material_before_insert(
    field_name: str,
    replacement,
) -> None:
    session = _Session(_bundle_row())
    plan = plan_historical_rebuild(
        session,
        bundle_id="00000000-0000-0000-0000-000000000123",
        git_commit="a" * 40,
    )
    session.statements.clear()

    with pytest.raises(
        RuntimeError,
        match="historical_bundle_changed_or_ineligible",
    ):
        start_historical_rebuild(
            session,
            replace(plan, **{field_name: replacement}),
        )

    assert not any("INSERT INTO meta.data_rebuild_runs" in sql for sql in session.statements)


def test_rebuild_start_rejects_hand_built_unsupported_purpose_before_insert() -> None:
    row = _bundle_row(purpose="mark_price_research")
    session = _Session(row)
    plan = HistoricalRebuildPlan(
        operation_key="hist-rebuild-" + ("a" * 64),
        bundle_id=str(row["bundle_id"]),
        bundle_fingerprint=str(row["fingerprint"]),
        bundle_key=str(row["bundle_key"]),
        purpose="mark_price_research",
        symbol="BTC-USDT-SWAP",
        coverage_start=row["coverage_start"],
        coverage_end=row["coverage_end"],
        source_id=row["component_sources"][0]["source_id"],
        source_key=row["component_sources"][0]["provenance"]["source_key"],
        instrument_snapshot_digest=_snapshot().digest,
        instrument_snapshot_source_id=(
            "00000000-0000-0000-0000-000000000099"
        ),
        source_row_count=10,
        raw_partition_sha256=("a" * 64,),
        transform_version=TRANSFORM_VERSION,
        git_commit="a" * 40,
    )

    with pytest.raises(
        RuntimeError,
        match="historical_bundle_changed_or_ineligible",
    ):
        start_historical_rebuild(session, plan)

    assert not any("INSERT INTO meta.data_rebuild_runs" in sql for sql in session.statements)


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
        bundle_key="l2:stable-content-key",
        purpose="l2_replay",
        symbol="BTC-USDT-SWAP",
        coverage_start=START,
        coverage_end=START + timedelta(days=1),
        source_id="00000000-0000-0000-0000-000000000001",
        source_key="okx-bulk:l2:v1",
        instrument_snapshot_digest=_snapshot().digest,
        instrument_snapshot_source_id="00000000-0000-0000-0000-000000000099",
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


def test_succeeded_rebuild_recomputes_business_row_fingerprint() -> None:
    class _FingerprintRows:
        def mappings(self):
            return self

        def all(self):
            return [
                {
                    "output_fingerprint": "a" * 64,
                    "computed_fingerprint": "b" * 64,
                }
            ]

    class _FingerprintSession:
        def execute(self, _statement, _params):
            return _FingerprintRows()

    with pytest.raises(
        RuntimeError,
        match="historical_rebuild_succeeded_row_content_mismatch",
    ):
        verified_historical_rebuild_output_fingerprints(
            _FingerprintSession(),
            purpose="trade_flow_research",
            bundle_id="00000000-0000-0000-0000-000000000123",
            symbol="BTC-USDT",
            coverage_start=START,
            coverage_end=START + timedelta(days=1),
            bundle_fingerprint="b" * 64,
            instrument_snapshot_digest=None,
        )


def test_rebuild_sql_hashes_values_at_persisted_numeric_precision() -> None:
    class _ScalarRows:
        def scalar_one(self):
            return 2

    class _WriteRows:
        rowcount = 1

    class _CaptureSession:
        def __init__(self):
            self.statements: list[str] = []

        def execute(self, statement, _params):
            sql = str(statement)
            self.statements.append(sql)
            if sql.lstrip().startswith("SELECT"):
                return _ScalarRows()
            return _WriteRows()

    plan = HistoricalRebuildPlan(
        operation_key="hist-rebuild-test",
        bundle_id="00000000-0000-0000-0000-000000000123",
        bundle_fingerprint="b" * 64,
        bundle_key="trade:stable-content-key",
        purpose="trade_flow_research",
        symbol="BTC-USDT",
        coverage_start=START,
        coverage_end=START + timedelta(days=1),
        source_id="00000000-0000-0000-0000-000000000001",
        source_key="okx-rest:trades:v1",
        instrument_snapshot_digest=None,
        instrument_snapshot_source_id=None,
        source_row_count=2,
        raw_partition_sha256=("a" * 64,),
        transform_version=TRANSFORM_VERSION,
        git_commit="c" * 40,
    )
    session = _CaptureSession()

    historical_rebuild._rebuild_trade_flow(session, plan)

    write_sql = next(
        sql for sql in session.statements if "INSERT INTO silver" in sql
    )
    assert "CAST(SUM(sz) AS NUMERIC(38, 18)) AS total_size" in write_sql
    assert "AS NUMERIC(28, 12)\n                       ) AS vwap" in write_sql
    assert "AS NUMERIC(28, 12)\n                       ) AS trade_flow_imbalance" in write_sql

    orderbook_session = _CaptureSession()
    historical_rebuild._rebuild_orderbook(orderbook_session, plan)
    orderbook_sql = next(
        sql for sql in orderbook_session.statements if "INSERT INTO silver" in sql
    )
    assert "CAST(bbo.mid_price_mean AS NUMERIC(28, 12))" in orderbook_sql
    assert "CAST(bbo.spread_bps_mean AS NUMERIC(28, 12))" in orderbook_sql
    assert "CAST(bbo.top_imbalance_mean AS NUMERIC(28, 12))" in orderbook_sql
