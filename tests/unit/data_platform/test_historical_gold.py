from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from aats.data_platform.data_governance import historical_gold
from aats.data_platform.data_governance.historical_gold import (
    _input_lineage,
    _verify_succeeded_artifact,
    execute_historical_gold,
    plan_historical_gold,
    start_historical_gold,
)
from aats.data_platform.data_governance.contracts import canonical_json_bytes
from aats.data_platform.data_governance.instrument_lineage import (
    instrument_contract_snapshot_registry_identity,
    instrument_contract_snapshot_source_key,
    instrument_snapshot_temporal_evidence_reason,
)
from aats.domain.instrument_contract import InstrumentContract
from aats.domain.instrument_contract_snapshot import InstrumentContractSnapshot


START = datetime(2026, 8, 1, tzinfo=timezone.utc)
END = START + timedelta(days=1)
_REAL_SOURCE_CONTENT_GUARD = historical_gold._assert_gold_source_content_sealed


@pytest.fixture(autouse=True)
def _simulate_future_manifest_verifier(monkeypatch) -> None:
    monkeypatch.setattr(
        historical_gold,
        "instrument_snapshot_temporal_evidence_reason",
        lambda _snapshot: None,
    )
    monkeypatch.setattr(
        historical_gold,
        "_assert_gold_source_content_sealed",
        lambda _symbol, _inputs: None,
    )


def _snapshot(*, contract_value: str = "0.01") -> InstrumentContractSnapshot:
    return InstrumentContractSnapshot(
        venue="OKX",
        contract=InstrumentContract(
            symbol="BTC-USDT-SWAP",
            instrument_type="swap",
            contract_type="linear",
            base_currency="BTC",
            quote_currency="USDT",
            settle_currency="USDT",
            contract_value=Decimal(contract_value),
            contract_multiplier=Decimal("1"),
            contract_value_currency="BTC",
            lot_size=Decimal("1"),
            min_size=Decimal("1"),
            tick_size=Decimal("0.1"),
        ),
        observed_at=END,
        effective_from=START,
        effective_to=END,
        evidence_kind="observed_forward",
        source_locator="prospective://okx/public/instruments/BTC-USDT-SWAP",
        source_schema="aats-instrument-observation-window-v1",
        source_payload_sha256=hashlib.sha256(
            f"payload:{contract_value}".encode()
        ).hexdigest(),
    )


def _bundle(
    bundle_id: str,
    *,
    purpose: str,
    role: str,
    rows: int,
    symbol: str = "BTC-USDT",
    content_key: str | None = None,
    source_id: str = "00000000-0000-0000-0000-000000000001",
    coverage_start: datetime = START,
    coverage_end: datetime = END,
    snapshot: InstrumentContractSnapshot | None = None,
    spot_binding: bool = False,
    snapshot_source_id: str = "00000000-0000-0000-0000-000000000099",
) -> dict:
    stable_key = content_key or f"{purpose}:{role}:{coverage_start}:{coverage_end}"
    provenance = {
        "source_key": f"source:{stable_key}",
        "row_count": rows,
        "gap_manifest": {
            "raw_partition_sha256": [
                hashlib.sha256(f"raw:{stable_key}".encode()).hexdigest()
            ]
        },
    }
    component = {
        "source_id": source_id,
        "symbol": symbol,
        "role": role,
        "provenance": provenance,
    }
    eligibility_report: dict = {}
    if spot_binding:
        binding_material = {
            "policy_version": "instrument-contract-binding-v1",
            "required": False,
            "eligible": True,
            "snapshot_digest": None,
            "reason_codes": (),
        }
        eligibility_report["instrument_contract_binding"] = {
            **binding_material,
            "evidence_fingerprint": hashlib.sha256(
                canonical_json_bytes(binding_material)
            ).hexdigest(),
        }
    if snapshot is not None:
        provenance["instrument_contract_snapshot"] = snapshot.to_dict()
        component["instrument_snapshot_digest"] = snapshot.digest
        component["instrument_snapshot_source_id"] = snapshot_source_id
        binding_material = {
            "policy_version": "instrument-contract-binding-v1",
            "required": symbol.endswith("-SWAP"),
            "eligible": True,
            "snapshot_digest": snapshot.digest,
            "reason_codes": (),
        }
        eligibility_report["instrument_contract_binding"] = {
            **binding_material,
            "evidence_fingerprint": hashlib.sha256(
                canonical_json_bytes(binding_material)
            ).hexdigest(),
        }
    return {
        "bundle_id": bundle_id,
        "bundle_key": stable_key,
        "purpose": purpose,
        "eligibility_mode": "historical_research",
        "status": "ELIGIBLE",
        "fingerprint": hashlib.sha256(f"bundle:{stable_key}".encode()).hexdigest(),
        "dataset_version": f"dataset-{role}",
        "coverage_start": coverage_start,
        "coverage_end": coverage_end,
        "component_sources": [component],
        "eligibility_report": eligibility_report,
    }


class _Mapped:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def one_or_none(self):
        return self.row

    def scalar_one_or_none(self):
        if isinstance(self.row, dict) and "bundle_id" in self.row:
            return self.row["bundle_id"]
        return self.row


class _Session:
    def __init__(self, rows: dict[str, dict], *, tamper_registry: bool = False):
        self.rows = rows
        self.tamper_registry = tamper_registry
        self.statements: list[str] = []

    def execute(self, _statement, params):
        statement = str(_statement)
        self.statements.append(statement)
        if "FROM meta.data_source_registry" in statement:
            requested_source_id = str(params["source_id"])
            for row in self.rows.values():
                for component in row["component_sources"]:
                    if (
                        str(component.get("instrument_snapshot_source_id"))
                        != requested_source_id
                    ):
                        continue
                    snapshot = InstrumentContractSnapshot.from_dict(
                        component["provenance"]["instrument_contract_snapshot"]
                    )
                    return _Mapped(
                        {
                            "source_key": instrument_contract_snapshot_source_key(
                                snapshot
                            ),
                            "source_kind": "third_party",
                            "provider": "OKX",
                            "source_locator": (
                                "tampered://registry"
                                if self.tamper_registry
                                else snapshot.source_locator
                            ),
                            "schema_version": snapshot.source_schema,
                            "truth_tier": "external_unverified",
                            "source_metadata": {
                                "record_type": "instrument_contract_snapshot_v1",
                                "identity": (
                                    instrument_contract_snapshot_registry_identity(
                                        snapshot
                                    )
                                ),
                                "snapshot": snapshot.to_dict(),
                            },
                        }
                    )
            return _Mapped(None)
        return _Mapped(self.rows[params["bundle_id"]])


class _NeverExecuteSession:
    def execute(self, *_args, **_kwargs):  # pragma: no cover - assertion helper
        raise AssertionError("Gold guard must run before any database operation")


@pytest.mark.parametrize(
    "symbol",
    ("DOGE-USDT", "DOGE-USDT-SWAP", "BTC-USDT-240927"),
)
def test_historical_gold_rejects_unproven_scope_before_query(symbol: str) -> None:
    with pytest.raises(
        ValueError,
        match="instrument_scope_unsupported_or_unproven",
    ):
        plan_historical_gold(
            _NeverExecuteSession(),
            symbol=symbol,
            timeframe="15m",
            candle_bundle_id="10000000-0000-0000-0000-000000000001",
            funding_bundle_id=None,
            git_commit="a" * 40,
        )


def test_spot_gold_content_identity_excludes_database_uuid_audit_references() -> None:
    first_bundle_id = "10000000-0000-0000-0000-000000000001"
    second_bundle_id = "20000000-0000-0000-0000-000000000002"
    first = plan_historical_gold(
        _Session(
            {
                first_bundle_id: _bundle(
                    first_bundle_id,
                    purpose="ohlcv_research",
                    role="candles",
                    rows=96,
                    content_key="same-candle-content",
                    source_id="30000000-0000-0000-0000-000000000003",
                )
            }
        ),
        symbol="BTC-USDT",
        timeframe="15m",
        candle_bundle_id=first_bundle_id,
        funding_bundle_id=None,
        git_commit="a" * 40,
    )
    second = plan_historical_gold(
        _Session(
            {
                second_bundle_id: _bundle(
                    second_bundle_id,
                    purpose="ohlcv_research",
                    role="candles",
                    rows=96,
                    content_key="same-candle-content",
                    source_id="40000000-0000-0000-0000-000000000004",
                    coverage_start=START.astimezone(
                        timezone(timedelta(hours=8))
                    ),
                    coverage_end=END.astimezone(timezone(timedelta(hours=8))),
                )
            }
        ),
        symbol="BTC-USDT",
        timeframe="15m",
        candle_bundle_id=second_bundle_id,
        funding_bundle_id=None,
        git_commit="a" * 40,
    )

    assert first.input_fingerprint == second.input_fingerprint
    assert first.operation_key == second.operation_key
    assert first.candle.bundle_id != second.candle.bundle_id
    assert first.candle.source_id != second.candle.source_id


def test_spot_gold_accepts_explicit_no_snapshot_binding_report() -> None:
    bundle_id = "10000000-0000-0000-0000-000000000001"
    plan = plan_historical_gold(
        _Session(
            {
                bundle_id: _bundle(
                    bundle_id,
                    purpose="ohlcv_research",
                    role="candles",
                    rows=96,
                    spot_binding=True,
                )
            }
        ),
        symbol="BTC-USDT",
        timeframe="15m",
        candle_bundle_id=bundle_id,
        funding_bundle_id=None,
        git_commit="a" * 40,
    )

    assert plan.candle.instrument_snapshot_digest is None
    assert plan.candle.instrument_snapshot_source_id is None


def test_spot_gold_rejects_partial_snapshot_material() -> None:
    bundle_id = "10000000-0000-0000-0000-000000000001"
    row = _bundle(
        bundle_id,
        purpose="ohlcv_research",
        role="candles",
        rows=96,
        spot_binding=True,
    )
    row["component_sources"][0]["instrument_snapshot_digest"] = "a" * 64

    with pytest.raises(
        ValueError,
        match="historical_gold_instrument_contract_binding_invalid",
    ):
        plan_historical_gold(
            _Session({bundle_id: row}),
            symbol="BTC-USDT",
            timeframe="15m",
            candle_bundle_id=bundle_id,
            funding_bundle_id=None,
            git_commit="a" * 40,
        )


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    [
        ("reason_codes", None),
        ("required", 0),
        ("eligible", 1),
        ("unexpected", True),
    ],
)
def test_spot_gold_rejects_partial_or_loosely_typed_binding_report(
    field_name: str,
    replacement,
) -> None:
    bundle_id = "10000000-0000-0000-0000-000000000001"
    row = _bundle(
        bundle_id,
        purpose="ohlcv_research",
        role="candles",
        rows=96,
        spot_binding=True,
    )
    binding = row["eligibility_report"]["instrument_contract_binding"]
    if replacement is None:
        binding.pop(field_name)
    else:
        binding[field_name] = replacement

    with pytest.raises(
        ValueError,
        match="historical_gold_instrument_contract_binding_invalid",
    ):
        plan_historical_gold(
            _Session({bundle_id: row}),
            symbol="BTC-USDT",
            timeframe="15m",
            candle_bundle_id=bundle_id,
            funding_bundle_id=None,
            git_commit="a" * 40,
        )


def test_auxiliary_content_identity_is_sorted_independently_of_caller_order() -> None:
    candle_id = "10000000-0000-0000-0000-000000000001"
    first_aux_id = "20000000-0000-0000-0000-000000000002"
    second_aux_id = "30000000-0000-0000-0000-000000000003"
    rows = {
        candle_id: _bundle(
            candle_id,
            purpose="ohlcv_research",
            role="candles",
            rows=96,
            content_key="candle-content",
        ),
        first_aux_id: _bundle(
            first_aux_id,
            purpose="mark_price_research",
            role="mark_price_bar",
            rows=48,
            content_key="mark-a",
            coverage_end=START + timedelta(hours=12),
        ),
        second_aux_id: _bundle(
            second_aux_id,
            purpose="mark_price_research",
            role="mark_price_bar",
            rows=48,
            content_key="mark-b",
            coverage_start=START + timedelta(hours=12),
        ),
    }
    session = _Session(rows)

    first = plan_historical_gold(
        session,
        symbol="BTC-USDT",
        timeframe="15m",
        candle_bundle_id=candle_id,
        funding_bundle_id=None,
        auxiliary_bundle_ids=(second_aux_id, first_aux_id),
        git_commit="a" * 40,
    )
    second = plan_historical_gold(
        session,
        symbol="BTC-USDT",
        timeframe="15m",
        candle_bundle_id=candle_id,
        funding_bundle_id=None,
        auxiliary_bundle_ids=(first_aux_id, second_aux_id),
        git_commit="a" * 40,
    )

    assert first.input_fingerprint == second.input_fingerprint
    assert first.operation_key == second.operation_key
    assert [item.bundle_key for item in first.auxiliary] == ["mark-a", "mark-b"]


def test_swap_requires_funding_before_loading_any_bundle() -> None:
    with pytest.raises(ValueError, match="swap_requires_funding_bundle"):
        plan_historical_gold(
            _Session({}),
            symbol="BTC-USDT-SWAP",
            timeframe="15m",
            candle_bundle_id="10000000-0000-0000-0000-000000000001",
            funding_bundle_id=None,
            git_commit="a" * 40,
        )


def test_hand_built_derivative_plan_cannot_bypass_start_or_execute_guard(
    monkeypatch,
) -> None:
    candle_id = "10000000-0000-0000-0000-000000000001"
    spot_plan = plan_historical_gold(
        _Session(
            {
                candle_id: _bundle(
                    candle_id,
                    purpose="ohlcv_research",
                    role="candles",
                    rows=96,
                )
            }
        ),
        symbol="BTC-USDT",
        timeframe="15m",
        candle_bundle_id=candle_id,
        funding_bundle_id=None,
        git_commit="a" * 40,
    )
    forged_derivative_plan = replace(spot_plan, symbol="BTC-USDT-SWAP")
    monkeypatch.setattr(
        historical_gold,
        "_assert_gold_source_content_sealed",
        _REAL_SOURCE_CONTENT_GUARD,
    )
    session = _NeverExecuteSession()

    with pytest.raises(ValueError, match="instrument_contract_unbound"):
        start_historical_gold(session, forged_derivative_plan)
    with pytest.raises(ValueError, match="instrument_contract_unbound"):
        execute_historical_gold(
            session,
            forged_derivative_plan,
            artifact_id="50000000-0000-0000-0000-000000000005",
        )


def test_spot_gold_start_rebuilds_plan_before_state_side_effect() -> None:
    candle_id = "10000000-0000-0000-0000-000000000001"
    session = _Session(
        {
            candle_id: _bundle(
                candle_id,
                purpose="ohlcv_research",
                role="candles",
                rows=96,
            )
        }
    )
    plan = plan_historical_gold(
        session,
        symbol="BTC-USDT",
        timeframe="15m",
        candle_bundle_id=candle_id,
        funding_bundle_id=None,
        git_commit="a" * 40,
    )
    forged = replace(
        plan,
        candle=replace(plan.candle, dataset_version="forged-version"),
    )
    session.statements.clear()

    with pytest.raises(
        RuntimeError,
        match="historical_gold_bundle_changed_or_ineligible",
    ):
        start_historical_gold(session, forged)

    assert not any(
        "INSERT INTO meta.historical_research_artifacts" in statement
        for statement in session.statements
    )


def test_succeeded_gold_recomputes_fingerprint_from_business_columns() -> None:
    candle_id = "10000000-0000-0000-0000-000000000001"
    plan = plan_historical_gold(
        _Session(
            {
                candle_id: _bundle(
                    candle_id,
                    purpose="ohlcv_research",
                    role="candles",
                    rows=96,
                )
            }
        ),
        symbol="BTC-USDT",
        timeframe="15m",
        candle_bundle_id=candle_id,
        funding_bundle_id=None,
        coverage_start=START,
        coverage_end=START + timedelta(minutes=15),
        git_commit="a" * 40,
    )
    lineage = [_input_lineage(item) for item in plan.inputs]
    content_lineage = [item["content_identity"] for item in lineage]
    original_payload = {
        "symbol": plan.symbol,
        "timeframe": plan.timeframe,
        "ts": START.isoformat().replace("+00:00", "Z"),
        "open": Decimal("100"),
        "high": Decimal("102"),
        "low": Decimal("99"),
        "close": Decimal("100"),
        "volume": Decimal("1"),
        "quote_volume": Decimal("100"),
        "is_closed": True,
        "aligned_funding_rate": None,
        "funding_source_ts": None,
        "transform_version": plan.transform_version,
        "source_content_lineage": content_lineage,
    }
    stored_fingerprint = hashlib.sha256(
        canonical_json_bytes(original_payload)
    ).hexdigest()
    tampered_row = {
        **original_payload,
        "ts": START,
        "close": Decimal("101"),
        "source_candle_bundle_id": plan.candle.bundle_id,
        "source_funding_bundle_id": None,
        "source_lineage": json.loads(
            json.dumps(lineage, sort_keys=True, default=str)
        ),
        "output_fingerprint": stored_fingerprint,
    }
    tampered_row.pop("source_content_lineage")

    class _GoldVerifyResult:
        def __init__(self, *, meta=None, rows=None):
            self.meta = meta
            self.rows = rows

        def one(self):
            return self.meta

        def mappings(self):
            return self

        def all(self):
            return self.rows

    class _GoldVerifySession:
        def execute(self, statement, _params):
            if "LEFT JOIN gold.historical_replay_bars" in str(statement):
                return _GoldVerifyResult(
                    meta=SimpleNamespace(
                        row_count=1,
                        actual_rows=1,
                        input_fingerprint=plan.input_fingerprint,
                        output_fingerprint="f" * 64,
                    )
                )
            return _GoldVerifyResult(rows=[tampered_row])

    with pytest.raises(
        RuntimeError,
        match="historical_gold_succeeded_row_content_mismatch",
    ):
        _verify_succeeded_artifact(
            _GoldVerifySession(),
            "50000000-0000-0000-0000-000000000005",
            plan,
        )


def test_unbound_derivative_bundle_cannot_enter_gold_planning() -> None:
    candle_id = "10000000-0000-0000-0000-000000000001"
    funding_id = "20000000-0000-0000-0000-000000000002"
    session = _Session(
        {
            candle_id: _bundle(
                candle_id,
                purpose="ohlcv_research",
                role="candles",
                rows=96,
                symbol="BTC-USDT-SWAP",
            ),
            funding_id: _bundle(
                funding_id,
                purpose="funding_research",
                role="funding",
                rows=3,
                symbol="BTC-USDT-SWAP",
            ),
        }
    )

    with pytest.raises(ValueError, match="instrument_contract_unbound"):
        plan_historical_gold(
            session,
            symbol="BTC-USDT-SWAP",
            timeframe="15m",
            candle_bundle_id=candle_id,
            funding_bundle_id=funding_id,
            git_commit="a" * 40,
        )


def test_contract_bound_derivative_gold_fails_closed_until_silver_is_sealed(
    monkeypatch,
) -> None:
    candle_id = "10000000-0000-0000-0000-000000000001"
    funding_id = "20000000-0000-0000-0000-000000000002"
    snapshot = _snapshot()
    session = _Session(
        {
            candle_id: _bundle(
                candle_id,
                purpose="ohlcv_research",
                role="candles",
                rows=96,
                symbol="BTC-USDT-SWAP",
                snapshot=snapshot,
            ),
            funding_id: _bundle(
                funding_id,
                purpose="funding_research",
                role="funding",
                rows=3,
                symbol="BTC-USDT-SWAP",
                snapshot=snapshot,
            ),
        }
    )
    monkeypatch.setattr(
        historical_gold,
        "_assert_gold_source_content_sealed",
        _REAL_SOURCE_CONTENT_GUARD,
    )

    with pytest.raises(ValueError, match="historical_gold_source_content_unsealed"):
        plan_historical_gold(
            session,
            symbol="BTC-USDT-SWAP",
            timeframe="15m",
            candle_bundle_id=candle_id,
            funding_bundle_id=funding_id,
            git_commit="a" * 40,
        )


def test_spot_gold_fails_closed_until_silver_content_is_bundle_bound(
    monkeypatch,
) -> None:
    candle_id = "10000000-0000-0000-0000-000000000001"
    session = _Session(
        {
            candle_id: _bundle(
                candle_id,
                purpose="ohlcv_research",
                role="candles",
                rows=96,
                symbol="BTC-USDT",
                spot_binding=True,
            )
        }
    )
    monkeypatch.setattr(
        historical_gold,
        "_assert_gold_source_content_sealed",
        _REAL_SOURCE_CONTENT_GUARD,
    )

    with pytest.raises(ValueError, match="historical_gold_source_content_unsealed"):
        plan_historical_gold(
            session,
            symbol="BTC-USDT",
            timeframe="15m",
            candle_bundle_id=candle_id,
            funding_bundle_id=None,
            git_commit="a" * 40,
        )


def test_contract_bound_gold_reanchors_snapshot_registry_before_planning() -> None:
    candle_id = "10000000-0000-0000-0000-000000000001"
    funding_id = "20000000-0000-0000-0000-000000000002"
    snapshot = _snapshot()
    rows = {
        candle_id: _bundle(
            candle_id,
            purpose="ohlcv_research",
            role="candles",
            rows=96,
            symbol="BTC-USDT-SWAP",
            snapshot=snapshot,
        ),
        funding_id: _bundle(
            funding_id,
            purpose="funding_research",
            role="funding",
            rows=3,
            symbol="BTC-USDT-SWAP",
            snapshot=snapshot,
        ),
    }

    with pytest.raises(
        ValueError,
        match="instrument_snapshot_source_anchor_mismatch",
    ):
        plan_historical_gold(
            _Session(rows, tamper_registry=True),
            symbol="BTC-USDT-SWAP",
            timeframe="15m",
            candle_bundle_id=candle_id,
            funding_bundle_id=funding_id,
            git_commit="a" * 40,
        )


def test_contract_bound_gold_rejects_self_declared_observation_window(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        historical_gold,
        "instrument_snapshot_temporal_evidence_reason",
        instrument_snapshot_temporal_evidence_reason,
    )
    candle_id = "10000000-0000-0000-0000-000000000001"
    funding_id = "20000000-0000-0000-0000-000000000002"
    snapshot = _snapshot()
    session = _Session(
        {
            candle_id: _bundle(
                candle_id,
                purpose="ohlcv_research",
                role="candles",
                rows=96,
                symbol="BTC-USDT-SWAP",
                snapshot=snapshot,
            ),
            funding_id: _bundle(
                funding_id,
                purpose="funding_research",
                role="funding",
                rows=3,
                symbol="BTC-USDT-SWAP",
                snapshot=snapshot,
            ),
        }
    )

    with pytest.raises(
        ValueError,
        match="instrument_snapshot_observation_evidence_unverified",
    ):
        plan_historical_gold(
            session,
            symbol="BTC-USDT-SWAP",
            timeframe="15m",
            candle_bundle_id=candle_id,
            funding_bundle_id=funding_id,
            git_commit="a" * 40,
        )


def test_derivative_inputs_with_different_contract_digests_fail_closed() -> None:
    candle_id = "10000000-0000-0000-0000-000000000001"
    funding_id = "20000000-0000-0000-0000-000000000002"
    session = _Session(
        {
            candle_id: _bundle(
                candle_id,
                purpose="ohlcv_research",
                role="candles",
                rows=96,
                symbol="BTC-USDT-SWAP",
                snapshot=_snapshot(contract_value="0.01"),
            ),
            funding_id: _bundle(
                funding_id,
                purpose="funding_research",
                role="funding",
                rows=3,
                symbol="BTC-USDT-SWAP",
                snapshot=_snapshot(contract_value="0.02"),
                snapshot_source_id=(
                    "00000000-0000-0000-0000-000000000098"
                ),
            ),
        }
    )

    with pytest.raises(ValueError, match="instrument_snapshot_mismatch"):
        plan_historical_gold(
            session,
            symbol="BTC-USDT-SWAP",
            timeframe="15m",
            candle_bundle_id=candle_id,
            funding_bundle_id=funding_id,
            git_commit="a" * 40,
        )
