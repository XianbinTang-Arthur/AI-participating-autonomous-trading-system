from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from aats.data_platform.decision_system.candidate_selector import (
    select_parameter_upgrade_candidates,
)
from aats.data_platform.decision_system.decision_engine import (
    decide_family_timeframe_status,
)
from aats.data_platform.decision_system.evidence_bundle import collect_phase2_evidence
from aats.data_platform.governance import artifact_index, manifest_validation, round_status
from aats.data_platform.governance.parameter_identity import (
    parameter_values_fingerprint,
)
from aats.data_platform.governance.parameter_registry import (
    import_from_parameter_candidates,
)
from aats.data_platform.replay.diagnostics.replay_diagnostics import extract_comparison_rows
from aats.domain.instrument_contract import InstrumentContract


def _write_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_v2_promotion_bundle(
    project_root: Path,
    *,
    family: str = "independent",
    timeframe: str = "15m",
    include_promotion_metrics: bool = True,
) -> Path:
    """Write the smallest complete, hash-bound promotion fixture."""

    bundle_dir = project_root / "artifacts" / "research" / "backtests" / "run-v2"
    resolved_parameters = {"signal_edge_scale_bps": "10"}
    adapter_identity = "tests.adapters.IndependentAdapter"
    adapter_algorithm_version = "independent-replay/v2"
    instrument_contract = {
        "symbol": "BTC-USDT",
        "instrument_type": "SPOT",
        "contract_type": "spot",
        "base_currency": "BTC",
        "quote_currency": "USDT",
        "settle_currency": "USDT",
        "contract_value": "1",
        "contract_multiplier": "1",
        "contract_value_currency": "BTC",
        "lot_size": "0.0001",
        "min_size": "0.0001",
        "tick_size": "0.1",
    }
    contract_fingerprint = InstrumentContract(
        **{
            **instrument_contract,
            "contract_value": Decimal(instrument_contract["contract_value"]),
            "contract_multiplier": Decimal(
                instrument_contract["contract_multiplier"]
            ),
            "lot_size": Decimal(instrument_contract["lot_size"]),
            "min_size": Decimal(instrument_contract["min_size"]),
            "tick_size": Decimal(instrument_contract["tick_size"]),
        }
    ).fingerprint
    _write_json(
        bundle_dir / "summary.json",
        {
            "artifact_kind": "backtest_run_summary",
            "artifact_schema_version": "backtest-run/v2",
            "config": {
                "symbol": "BTC-USDT",
                "instrument_contract": instrument_contract,
                "family": family,
                "timeframe": timeframe,
                "execution_model_version": "next_bar_event_v2",
                "fill_model_version": "ohlcv_participation_cap_contract_v3",
            },
            "resolved_parameters": resolved_parameters,
            "adapter_identity": adapter_identity,
            "adapter_algorithm_version": adapter_algorithm_version,
            "cadence_gap_count": 0,
            "summary": {
                "bar_count": 30,
                "fill_count": 12,
                "settlement_currency": "USDT",
                "instrument_symbol": "BTC-USDT",
                "instrument_contract_fingerprint": contract_fingerprint,
                "risk_metric_policy_id": "calendar-365.25-bar-pnl-increment/v1",
            },
            "decisions_count": 30,
            "fills_count": 12,
            "start_ts": "2026-04-16T00:00:00+00:00",
            "end_ts": "2026-04-16T07:30:00+00:00",
        },
    )
    (bundle_dir / "equity_curve.csv").write_text(
        "ts_ms,equity\n1776297600000,0\n",
        encoding="utf-8",
    )
    _write_json(bundle_dir / "cost_validation.json", {})
    _write_json(bundle_dir / "cost_diagnostics.json", {"diagnostics": []})
    _write_json(bundle_dir / "execution_timeline.json", [])
    if include_promotion_metrics:
        _write_json(
            bundle_dir / "phase2_promotion_metrics.json",
            {
                "artifact_kind": "phase2_promotion_metrics",
                "artifact_schema_version": "phase2-promotion-metrics/v1",
                "family": family,
                "timeframe": timeframe,
                "total_bars": 30,
                "opening_count": 12,
                "positive_edge_ratio": 0.75,
                "mean_expected_edge_bps": 4.2,
                "execution_compatible_ratio": 0.8,
                "selectable_ratio": 0.6,
            },
        )

    artifact_names = [
        "summary.json",
        "equity_curve.csv",
        "cost_validation.json",
        "cost_diagnostics.json",
        "execution_timeline.json",
    ]
    if include_promotion_metrics:
        artifact_names.append("phase2_promotion_metrics.json")
    artifact_hashes = {
        name: hashlib.sha256((bundle_dir / name).read_bytes()).hexdigest()
        for name in artifact_names
    }
    artifact_set_fingerprint = hashlib.sha256(
        json.dumps(
            {
                "artifact_schema_version": "backtest-run/v2",
                "artifact_sha256": artifact_hashes,
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    manifest_path = bundle_dir / "manifest.json"
    _write_json(
        manifest_path,
        {
            "artifact_kind": "backtest_run_manifest",
            "artifact_schema_version": "backtest-run/v2",
            "complete": True,
            "run_fingerprint": "1" * 64,
            "artifact_set_fingerprint": artifact_set_fingerprint,
            "instrument_arithmetic_policy_id": "instrument-arithmetic/v1",
            "fill_model_version": "ohlcv_participation_cap_contract_v3",
            "contract_lineage_status": "calculation_contract_only_unverified",
            "settlement_currency": "USDT",
            "instrument_symbol": "BTC-USDT",
            "instrument_contract_fingerprint": contract_fingerprint,
            "instrument_contract": instrument_contract,
            "resolved_parameters": resolved_parameters,
            "adapter_identity": adapter_identity,
            "adapter_algorithm_version": adapter_algorithm_version,
            "cadence_gap_count": 0,
            "risk_metric_policy_id": "calendar-365.25-bar-pnl-increment/v1",
            "artifact_sha256": artifact_hashes,
        },
    )
    return manifest_path


def _load_script_module(name: str, relative_path: str):
    path = Path(relative_path).resolve()
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _promotion_supporting_evidence(phase2_evidence: dict) -> dict:
    """Supply positive non-Phase2 dimensions to isolate the Phase2 boundary."""

    values_fingerprint = parameter_values_fingerprint({"entry_threshold": 1.0})
    source_round_id = "20260827_120000_a1b2c3d4"
    lineage = {
        "source_step3_round_id": source_round_id,
        "parameter_values_fingerprint": values_fingerprint,
    }
    return {
        "phase2_evidence": phase2_evidence,
        "phase3_evidence": {
            "latest_round": {
                "combos": {
                    "independent_15m": {
                        **lineage,
                        "status": "succeeded",
                        "top_failure_modes": {
                            "total_failures": 0,
                            "total_success": 10,
                        },
                    }
                }
            }
        },
        "phase4_evidence": {
            "latest_round": {
                "combos": {
                    "independent_15m": {
                        **lineage,
                        "cost_summary": {
                            "cost_adjusted_edge_mean": 1.0,
                            "full_fill_ratio": 1.0,
                        }
                    }
                }
            }
        },
        "phase5_governance_evidence": {"quality_health": "healthy"},
    }


def test_extract_comparison_rows_supports_canonical_schema() -> None:
    summary = {
        "experiment_count": 2,
        "comparison": [
            {"label": "a", "opening_count": 1},
            {"label": "b", "opening_count": 2},
        ],
    }

    rows = extract_comparison_rows(summary)

    assert [row["label"] for row in rows] == ["a", "b"]


def test_collect_phase2_evidence_quarantines_legacy_step2_stats(tmp_path: Path) -> None:
    round_dir = tmp_path / "artifacts" / "research" / "step2_rounds" / "20260416_000000_demo"
    # round_manifest.json 必须存在，否则 snapshot 会被标记为 file_incomplete，
    # evidence_bundle 会按无可信证据处理（防止半成品目录污染 Phase2 证据链）。
    _write_json(
        round_dir / "round_manifest.json",
        {
            "round_id": "20260416_000000_demo",
            "overall_status": "completed",
            "started_at": "2026-04-16T00:00:00Z",
            "finished_at": "2026-04-16T00:05:00Z",
        },
    )
    _write_json(
        round_dir / "family_timeframe_summary.json",
        {
            "experiments": [
                {
                    "family": "independent",
                    "symbol": "BTC-USDT",
                    "timeframe": "15m",
                    "opening_count": 12,
                    "positive_edge_ratio": 0.75,
                    "mean_expected_edge_bps": 4.2,
                    "execution_compatible_ratio": 0.8,
                },
            ],
        },
    )
    _write_json(
        round_dir / "scan_comparison_summary.json",
        {
            "comparison": [
                {
                    "family": "independent",
                    "symbol": "BTC-USDT",
                    "timeframe": "15m",
                    "label": "scan-a",
                    "opening_count": 20,
                    "positive_edge_ratio": 0.6,
                    "mean_expected_edge_bps": 3.5,
                    "execution_compatible_ratio": 0.7,
                },
            ],
        },
    )

    evidence = collect_phase2_evidence(
        tmp_path,
        expected_symbol="BTC-USDT-SWAP",
    )
    combo = evidence["combo_stats"]["independent_15m"]

    assert combo["available"] is False
    assert combo["experiments_with_openings"] == 0
    assert combo["max_opening_count"] == 0
    assert evidence["global_stats"]["total_experiments"] == 0
    assert evidence["promotion_eligible_experiment_count"] == 0
    assert evidence["promotion_evidence_status"] == "unavailable"
    assert evidence["promotion_evidence_reason"] == (
        "derivatives_phase2_promotion_evidence_unavailable"
    )
    assert evidence["combo_stats"]["independent_15m"][
        "fallback_reason"
    ] == "derivatives_phase2_promotion_evidence_unavailable"
    assert evidence["promotion_ineligible_experiment_count"] == 2
    assert len(evidence["audit_best_experiments"]) == 2
    assert {
        item["promotion_qualification_reason"]
        for item in evidence["audit_best_experiments"]
    } == {"backtest_manifest_missing"}


def test_collect_phase2_evidence_ignores_non_round_debug_dirs(tmp_path: Path) -> None:
    rounds_root = tmp_path / "artifacts" / "research" / "step2_rounds"
    _write_json(
        rounds_root / "20260416_000000_ab12cd34" / "round_manifest.json",
        {
            "round_id": "20260416_000000_ab12cd34",
            "overall_status": "completed",
            "started_at": "2026-04-16T00:00:00Z",
            "finished_at": "2026-04-16T00:05:00Z",
        },
    )
    _write_json(
        rounds_root / "20260416_000000_ab12cd34" / "scan_comparison_summary.json",
        {
            "comparison": [
                {
                    "family": "independent",
                    "timeframe": "1h",
                    "label": "scan-a",
                    "opening_count": 15,
                    "positive_edge_ratio": 0.55,
                    "mean_expected_edge_bps": 2.5,
                    "execution_compatible_ratio": 0.4,
                },
            ],
        },
    )
    _write_json(
        rounds_root / "debug_batch_wsl" / "scan_comparison_summary.json",
        {"comparison": []},
    )

    evidence = collect_phase2_evidence(tmp_path)
    combo = evidence["combo_stats"]["independent_1h"]

    assert combo["available"] is False
    assert combo["total_experiments"] == 0
    assert combo["max_opening_count"] == 0
    assert evidence["promotion_ineligible_experiment_count"] == 1
    assert evidence["audit_best_experiments"][0]["opening_count"] == 15
    assert "debug_batch_wsl" not in evidence["audit_best_experiments"][0]["id"]


def test_collect_phase2_evidence_refuses_step2_round_without_manifest(tmp_path: Path) -> None:
    """回归：缺 round_manifest.json 的 Step2 目录不能污染 Phase2 证据链。

    曾经的 bug：scan_comparison_summary.json 存在就被当作 available=True 的 combo
    证据，导致半成品/残留目录把 experiments_with_openings>=1 伪装成真可交易证据，
    推动 readiness gate 进入 ready_for_next_live_test。
    """
    round_dir = tmp_path / "artifacts" / "research" / "step2_rounds" / "20260416_111111_nomanif"
    # 故意不写 round_manifest.json
    _write_json(
        round_dir / "scan_comparison_summary.json",
        {
            "comparison": [
                {
                    "family": "independent",
                    "symbol": "BTC-USDT",
                    "timeframe": "15m",
                    "label": "scan-incomplete",
                    "opening_count": 999,
                    "positive_edge_ratio": 0.9,
                    "mean_expected_edge_bps": 9.9,
                    "execution_compatible_ratio": 0.9,
                },
            ],
        },
    )

    evidence = collect_phase2_evidence(tmp_path)
    combo = evidence["combo_stats"]["independent_15m"]

    assert combo["available"] is False, (
        "缺 round_manifest.json 的 Step2 目录不能被 Phase2 证据链当作可用"
    )
    assert combo["experiments_with_openings"] == 0


def test_collect_phase2_evidence_accepts_only_hash_bound_v2_metrics(
    tmp_path: Path,
) -> None:
    manifest_path = _write_v2_promotion_bundle(tmp_path)
    round_dir = (
        tmp_path
        / "artifacts"
        / "research"
        / "step2_rounds"
        / "20260416_000000_deadbeef"
    )
    _write_json(
        round_dir / "round_manifest.json",
        {
            "round_id": round_dir.name,
            "overall_status": "completed",
            "started_at": "2026-04-16T00:00:00Z",
            "finished_at": "2026-04-16T00:05:00Z",
        },
    )
    _write_json(
        round_dir / "family_timeframe_summary.json",
        {
            "experiments": [
                {
                    "family": "independent",
                    "symbol": "BTC-USDT",
                    "timeframe": "15m",
                    # These legacy values are deliberately different. Promotion
                    # aggregation must use the hash-bound metrics artifact.
                    "opening_count": 999,
                    "positive_edge_ratio": 1.0,
                    "backtest_manifest_path": manifest_path.relative_to(
                        tmp_path
                    ).as_posix(),
                }
            ]
        },
    )

    evidence = collect_phase2_evidence(tmp_path)
    combo = evidence["combo_stats"]["independent_15m"]

    assert combo["available"] is True
    assert combo["total_experiments"] == 1
    assert combo["max_opening_count"] == 12
    assert combo["mean_positive_edge_ratio"] == 0.75
    assert evidence["promotion_eligible_experiment_count"] == 1
    assert evidence["promotion_ineligible_experiment_count"] == 0
    assert evidence["best_experiments"][0]["promotion_qualification_reason"] == (
        "qualified"
    )
    selected = select_parameter_upgrade_candidates(
        [
            {
                "parameter_set_id": "ps-qualified",
                    "family": "independent",
                    "timeframe": "15m",
                    "status": "frozen",
                    "source_round_id": "20260827_120000_a1b2c3d4",
                    "values": {"entry_threshold": 1.0},
            }
        ],
        _promotion_supporting_evidence(evidence),
    )
    assert selected[0]["decision"] == "promote_candidate"


def test_collect_phase2_evidence_rejects_spot_bundle_for_swap_scope(
    tmp_path: Path,
) -> None:
    manifest_path = _write_v2_promotion_bundle(tmp_path)
    round_dir = (
        tmp_path
        / "artifacts"
        / "research"
        / "step2_rounds"
        / "20260416_000000_deadbeef"
    )
    _write_json(
        round_dir / "round_manifest.json",
        {
            "round_id": round_dir.name,
            "overall_status": "completed",
            "started_at": "2026-04-16T00:00:00Z",
            "finished_at": "2026-04-16T00:05:00Z",
        },
    )
    _write_json(
        round_dir / "family_timeframe_summary.json",
        {
            "experiments": [
                {
                    "family": "independent",
                    "symbol": "BTC-USDT-SWAP",
                    "timeframe": "15m",
                    "backtest_manifest_path": manifest_path.relative_to(
                        tmp_path
                    ).as_posix(),
                }
            ]
        },
    )

    evidence = collect_phase2_evidence(
        tmp_path,
        expected_symbol="BTC-USDT-SWAP",
    )

    assert evidence["combo_stats"]["independent_15m"]["available"] is False
    assert evidence["promotion_eligible_experiment_count"] == 0
    assert evidence["promotion_evidence_reason"] == (
        "derivatives_phase2_promotion_evidence_unavailable"
    )
    assert evidence["audit_best_experiments"][0][
        "promotion_qualification_reason"
    ] == "phase2_promotion_metrics_scope_mismatch"


def test_empty_derivatives_phase2_reports_stable_unavailable_reason(
    tmp_path: Path,
) -> None:
    from aats.data_platform.decision_system.readiness_evaluator import (
        evaluate_promotion_readiness,
    )

    evidence = collect_phase2_evidence(
        tmp_path,
        expected_symbol="BTC-USDT-SWAP",
    )

    assert evidence["promotion_eligible_experiment_count"] == 0
    assert evidence["target_symbol"] == "BTC-USDT-SWAP"
    assert evidence["promotion_evidence_reason"] == (
        "derivatives_phase2_promotion_evidence_unavailable"
    )
    assert all(
        stats["fallback_reason"]
        == "derivatives_phase2_promotion_evidence_unavailable"
        for stats in evidence["combo_stats"].values()
    )
    readiness = evaluate_promotion_readiness(
        {"phase2_evidence": evidence},
        [],
        [],
    )
    research_check = next(
        check
        for check in readiness["checks"]
        if check["check"] == "research_stability"
    )
    assert research_check["detail"] == (
        "derivatives_phase2_promotion_evidence_unavailable"
    )
    assert any(
        "derivatives_phase2_promotion_evidence_unavailable" in blocker
        for blocker in readiness["blockers"]
    )


def test_collect_phase2_evidence_rejects_tampered_manifest_bound_metrics(
    tmp_path: Path,
) -> None:
    manifest_path = _write_v2_promotion_bundle(tmp_path)
    metrics_path = manifest_path.parent / "phase2_promotion_metrics.json"
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    metrics["opening_count"] = 29
    _write_json(metrics_path, metrics)
    round_dir = (
        tmp_path
        / "artifacts"
        / "research"
        / "step2_rounds"
        / "20260416_000000_deadbeef"
    )
    _write_json(
        round_dir / "round_manifest.json",
        {
            "round_id": round_dir.name,
            "overall_status": "completed",
            "started_at": "2026-04-16T00:00:00Z",
            "finished_at": "2026-04-16T00:05:00Z",
        },
    )
    _write_json(
        round_dir / "scan_comparison_summary.json",
        {
            "comparison": [
                {
                    "family": "independent",
                    "symbol": "BTC-USDT",
                    "timeframe": "15m",
                    "opening_count": 999,
                    "positive_edge_ratio": 1.0,
                    "backtest_manifest_path": manifest_path.relative_to(
                        tmp_path
                    ).as_posix(),
                }
            ]
        },
    )

    evidence = collect_phase2_evidence(tmp_path)

    assert evidence["combo_stats"]["independent_15m"]["available"] is False
    assert evidence["promotion_eligible_experiment_count"] == 0
    assert evidence["promotion_ineligible_experiment_count"] == 1
    assert evidence["audit_best_experiments"][0][
        "promotion_qualification_reason"
    ] == "backtest_manifest_artifact_hash_mismatch"


def test_collect_phase2_evidence_rejects_v2_manifest_without_metrics_schema(
    tmp_path: Path,
) -> None:
    manifest_path = _write_v2_promotion_bundle(
        tmp_path,
        include_promotion_metrics=False,
    )
    round_dir = (
        tmp_path
        / "artifacts"
        / "research"
        / "step2_rounds"
        / "20260416_000000_deadbeef"
    )
    _write_json(
        round_dir / "round_manifest.json",
        {
            "round_id": round_dir.name,
            "overall_status": "completed",
            "started_at": "2026-04-16T00:00:00Z",
            "finished_at": "2026-04-16T00:05:00Z",
        },
    )
    _write_json(
        round_dir / "family_timeframe_summary.json",
        {
            "experiments": [
                {
                    "family": "independent",
                    "symbol": "BTC-USDT",
                    "timeframe": "15m",
                    "opening_count": 20,
                    "positive_edge_ratio": 0.8,
                    "backtest_manifest_path": manifest_path.relative_to(
                        tmp_path
                    ).as_posix(),
                }
            ]
        },
    )

    evidence = collect_phase2_evidence(tmp_path)

    assert evidence["combo_stats"]["independent_15m"]["available"] is False
    assert evidence["audit_best_experiments"][0][
        "promotion_qualification_reason"
    ] == "phase2_promotion_metrics_not_manifest_bound"


def test_collect_phase2_evidence_binds_index_scope_to_verified_metrics(
    tmp_path: Path,
) -> None:
    manifest_path = _write_v2_promotion_bundle(tmp_path)
    artifact_index = {
        "artifacts": [
            {
                "artifact_id": "indexed-directional-15m",
                "artifact_type": "experiment",
                "phase": "phase2_step1",
                "path": manifest_path.parent.relative_to(tmp_path).as_posix(),
                # Governance metadata disagrees with the manifest-bound metrics.
                "family": "directional",
                "symbol": "BTC-USDT",
                "timeframe": "15m",
                "diagnostics_summary": {
                    "opening_count": 999,
                    "positive_edge_ratio": 1.0,
                    "backtest_manifest_path": manifest_path.relative_to(
                        tmp_path
                    ).as_posix(),
                },
            }
        ]
    }

    evidence = collect_phase2_evidence(tmp_path, artifact_index=artifact_index)

    assert evidence["promotion_eligible_experiment_count"] == 0
    assert evidence["combo_stats"]["directional_15m"]["available"] is False
    assert evidence["audit_best_experiments"][0][
        "promotion_qualification_reason"
    ] == "phase2_promotion_metrics_scope_mismatch"


def test_collect_phase2_evidence_does_not_mix_old_index_with_canonical_round(
    tmp_path: Path,
) -> None:
    """A canonical empty round must not borrow eligibility from old artifacts."""

    manifest_path = _write_v2_promotion_bundle(tmp_path)
    artifact_index = {
        "artifacts": [
            {
                "artifact_id": "old-eligible-independent-15m",
                "artifact_type": "experiment",
                "phase": "phase2_step1",
                "path": manifest_path.parent.relative_to(tmp_path).as_posix(),
                "family": "independent",
                "symbol": "BTC-USDT",
                "timeframe": "15m",
                "diagnostics_summary": {
                    "opening_count": 12,
                    "positive_edge_ratio": 0.75,
                    "backtest_manifest_path": manifest_path.relative_to(
                        tmp_path
                    ).as_posix(),
                },
            }
        ]
    }
    canonical_snapshot = {
        "round_id": "20260828_010000_deadbeef",
        "phase": "phase2_step2",
        "status": "succeeded",
        "round_path": str(
            tmp_path
            / "artifacts"
            / "research"
            / "step2_rounds"
            / "20260828_010000_deadbeef"
        ),
        "manifest": {
            "round_id": "20260828_010000_deadbeef",
            "status": "succeeded",
            "scope": {"symbol": "BTC-USDT"},
        },
        "summary": {
            "family_timeframe_summary": {"experiments": []},
            "scan_comparison_summary": {"comparison": []},
        },
        "data_source": "file",
    }

    with patch(
        "aats.data_platform.decision_system.evidence_bundle."
        "load_latest_research_round_snapshot",
        return_value=canonical_snapshot,
    ):
        evidence = collect_phase2_evidence(
            tmp_path,
            artifact_index=artifact_index,
            expected_symbol="BTC-USDT",
        )

    assert evidence["promotion_eligible_experiment_count"] == 0
    assert evidence["combo_stats"]["independent_15m"]["available"] is False
    assert evidence["promotion_evidence_status"] == "unavailable"
    # The historical record remains visible for audit, but is excluded from
    # canonical qualification aggregation.
    assert evidence["experiments"][0]["id"] == "old-eligible-independent-15m"
    assert evidence["audit_best_experiments"] == []


def test_collect_phase2_evidence_selects_exact_step2_snapshot_not_latest(
    tmp_path: Path,
) -> None:
    exact_round_id = "20260827_100000_1234abcd"
    exact_snapshot = {
        "round_id": exact_round_id,
        "phase": "phase2_step2",
        "status": "succeeded",
        "round_path": str(
            tmp_path
            / "artifacts"
            / "research"
            / "step2_rounds"
            / exact_round_id
        ),
        "started_at": "2026-08-27T10:00:00+00:00",
        "finished_at": "2026-08-27T10:01:00+00:00",
        "replay_only": False,
        "manifest": {
            "round_id": exact_round_id,
            "phase": "step2",
            "status": "succeeded",
            "scope": {"symbol": "BTC-USDT-SWAP"},
        },
        "summary": {
            "family_timeframe_summary": {"experiments": []},
            "scan_comparison_summary": {"comparison": []},
        },
        "conclusion": {},
        "artifacts": {},
        "data_source": "db",
    }

    with (
        patch(
            "aats.data_platform.decision_system.evidence_bundle."
            "load_research_round_snapshot",
            return_value=exact_snapshot,
        ) as exact_loader,
        patch(
            "aats.data_platform.decision_system.evidence_bundle."
            "load_latest_research_round_snapshot",
            side_effect=AssertionError("latest Step2 lookup is forbidden"),
        ),
    ):
        evidence = collect_phase2_evidence(
            tmp_path,
            expected_step2_round_id=exact_round_id,
            expected_symbol="BTC-USDT-SWAP",
        )

    exact_loader.assert_called_once_with(
        round_id=exact_round_id,
        project_root=tmp_path,
        require_managed_db_truth=False,
    )
    assert evidence["canonical_step2_round_id"] == exact_round_id
    assert evidence["canonical_step2_snapshot_data_source"] == "db"
    assert len(evidence["canonical_step2_snapshot_sha256"]) == 64
    assert evidence["round_selection_error"] is None


def test_collect_phase2_evidence_missing_exact_step2_never_falls_back(
    tmp_path: Path,
) -> None:
    manifest_path = _write_v2_promotion_bundle(tmp_path)
    artifact_index = {
        "artifacts": [
            {
                "artifact_id": "old-eligible-independent-15m",
                "artifact_type": "experiment",
                "phase": "phase2_step1",
                "path": manifest_path.parent.relative_to(tmp_path).as_posix(),
                "family": "independent",
                "symbol": "BTC-USDT",
                "timeframe": "15m",
                "diagnostics_summary": {
                    "backtest_manifest_path": manifest_path.relative_to(
                        tmp_path
                    ).as_posix(),
                },
            }
        ]
    }

    with patch(
        "aats.data_platform.decision_system.evidence_bundle."
        "load_research_round_snapshot",
        return_value=None,
    ):
        evidence = collect_phase2_evidence(
            tmp_path,
            artifact_index=artifact_index,
            expected_step2_round_id="20260827_100000_1234abcd",
            expected_symbol="BTC-USDT-SWAP",
        )

    assert evidence["round_selection_error"] == "expected_step2_snapshot_missing"
    assert evidence["promotion_eligible_experiment_count"] == 0
    assert evidence["combo_stats"]["independent_15m"]["available"] is False


def test_import_from_parameter_candidates_skips_placeholder_and_none(tmp_path: Path) -> None:
    candidates_path = tmp_path / "parameter_candidates.json"
    _write_json(
        candidates_path,
        {
            "candidates": {
                "independent_15m": {"signal_edge_scale_bps": 12.0},
                "directional_15m": {"_note": "placeholder"},
                "independent_1h": {"signal_edge_scale_bps": None},
            },
            "pending_validation": ["signal_edge_scale_bps in independent_15m"],
        },
    )

    imported = import_from_parameter_candidates(
        candidates_path,
        source_round_id="round-x",
        initial_status="candidate",
    )

    assert len(imported) == 1
    assert imported[0]["family"] == "independent"
    assert imported[0]["timeframe"] == "15m"
    assert imported[0]["confidence"] == "low"


def test_candidate_selector_rejects_legacy_phase2_bundle_without_policy() -> None:
    legacy_phase2 = {
        "combo_stats": {
            "independent_15m": {
                "available": True,
                "experiments_with_openings": 99,
                "max_opening_count": 999,
                "mean_positive_edge_ratio": 1.0,
            }
        }
    }

    selected = select_parameter_upgrade_candidates(
        [
            {
                "parameter_set_id": "ps-legacy",
                "family": "independent",
                "timeframe": "15m",
                "status": "frozen",
                "values": {"entry_threshold": 1.0},
            }
        ],
        _promotion_supporting_evidence(legacy_phase2),
    )

    assert selected[0]["decision"] == "hold"
    phase2_score = selected[0]["dimension_scores"][0]
    assert phase2_score["score"] == 0.0
    assert any("缺少 Phase 2 有效证据" in item for item in phase2_score["details"])
    assert any(
        "promotion_qualification_policy_unsupported" in item
        for item in phase2_score["details"]
    )


def test_candidate_selector_has_explicit_phase2_promotion_hard_gate() -> None:
    parameter_set = {
        "parameter_set_id": "ps-unqualified-high-score",
        "family": "independent",
        "timeframe": "15m",
        "status": "frozen",
        "values": {"entry_threshold": 1.0},
    }
    with patch(
        "aats.data_platform.decision_system.candidate_selector._evaluate_phase2_score",
        return_value={
            "dimension": "phase2_research",
            "score": 3.0,
            "max_score": 3.0,
            "promotion_evidence_qualified": False,
            "details": ["legacy evidence"],
        },
    ), patch(
        "aats.data_platform.decision_system.candidate_selector._evaluate_phase3_score",
        return_value={"dimension": "phase3", "score": 2.0, "max_score": 2.0, "details": []},
    ), patch(
        "aats.data_platform.decision_system.candidate_selector._evaluate_phase4_score",
        return_value={"dimension": "phase4", "score": 2.0, "max_score": 2.0, "details": []},
    ), patch(
        "aats.data_platform.decision_system.candidate_selector._evaluate_governance_score",
        return_value={"dimension": "phase5", "score": 2.0, "max_score": 2.0, "details": []},
    ):
        selected = select_parameter_upgrade_candidates([parameter_set], {})

    assert selected[0]["score_ratio"] == 1.0
    assert selected[0]["decision"] == "hold"


def test_candidate_selector_requires_target_combo_positive_edge_hard_gate() -> None:
    phase2 = {
        "promotion_qualification_policy": "phase2-promotion-metrics/v1",
        "combo_stats": {
            "independent_15m": {
                "available": True,
                "family": "independent",
                "timeframe": "15m",
                "combo_key": "independent_15m",
                "total_experiments": 1,
                "experiments_with_openings": 1,
                "max_opening_count": 1,
                "mean_positive_edge_ratio": 0.0,
            }
        },
    }
    evidence = _promotion_supporting_evidence(phase2)
    evidence["phase5_governance_evidence"] = {"quality_health": "healthy"}
    selected = select_parameter_upgrade_candidates(
        [
            {
                "parameter_set_id": "ps-zero-edge",
                "source_round_id": "20260827_120000_a1b2c3d4",
                "family": "independent",
                "symbol": "BTC-USDT-SWAP",
                "timeframe": "15m",
                "status": "candidate",
                "values": {"entry_threshold": 1.0},
            }
        ],
        evidence,
    )

    assert selected[0]["decision"] != "promote_candidate"
    assert selected[0]["dimension_scores"][0][
        "promotion_evidence_qualified"
    ] is False


def test_readiness_cannot_borrow_edge_from_another_combo_for_promotion() -> None:
    from aats.data_platform.decision_system.readiness_evaluator import (
        evaluate_promotion_readiness,
    )

    evidence = {
        "phase2_evidence": {
            "promotion_qualification_policy": "phase2-promotion-metrics/v1",
            "combo_stats": {
                "independent_15m": {
                    "available": True,
                    "experiments_with_openings": 1,
                    "max_opening_count": 1,
                    "mean_positive_edge_ratio": 0.0,
                },
                "directional_1h": {
                    "available": True,
                    "experiments_with_openings": 1,
                    "max_opening_count": 2,
                    "mean_positive_edge_ratio": 0.9,
                },
            },
        },
        "phase3_evidence": {},
        "phase4_evidence": {},
        "phase5_governance_evidence": {},
    }
    result = evaluate_promotion_readiness(
        evidence,
        [
            {
                "decision": "promote_candidate",
                "parameter_set_id": "ps-zero-edge",
                "family": "independent",
                "timeframe": "15m",
                "score_ratio": 0.9,
            }
        ],
        [],
    )

    research_check = next(
        check
        for check in result["checks"]
        if check["check"] == "research_stability"
    )
    assert research_check["passed"] is False
    assert "independent_15m" in research_check["detail"]


def test_select_parameter_upgrade_candidates_dedupes_per_combo() -> None:
    parameter_sets = [
        {"parameter_set_id": "ps_a", "family": "independent", "timeframe": "15m"},
        {"parameter_set_id": "ps_b", "family": "independent", "timeframe": "15m"},
        {"parameter_set_id": "ps_c", "family": "directional", "timeframe": "1h"},
    ]
    fake_results = [
        {
            "parameter_set_id": "ps_a",
            "family": "independent",
            "timeframe": "15m",
            "decision": "hold",
            "score_ratio": 0.5,
            "total_score": 4.0,
        },
        {
            "parameter_set_id": "ps_b",
            "family": "independent",
            "timeframe": "15m",
            "decision": "promote_candidate",
            "score_ratio": 0.8,
            "total_score": 6.0,
        },
        {
            "parameter_set_id": "ps_c",
            "family": "directional",
            "timeframe": "1h",
            "decision": "hold",
            "score_ratio": 0.4,
            "total_score": 3.0,
        },
    ]

    with patch(
        "aats.data_platform.decision_system.candidate_selector.evaluate_parameter_set",
        side_effect=fake_results,
    ):
        selected = select_parameter_upgrade_candidates(parameter_sets, {})

    assert [item["parameter_set_id"] for item in selected] == ["ps_b", "ps_c"]


def test_parameter_candidate_preserves_source_round_lineage() -> None:
    parameter_set = {
        "parameter_set_id": "ps_lineage",
        "source_round_id": "round_source_001",
        "family": "independent",
        "timeframe": "15m",
        "status": "candidate",
        "values": {"entry_threshold": 1.0},
    }

    with patch(
        "aats.data_platform.decision_system.candidate_selector._evaluate_phase2_score",
        return_value={"dimension": "phase2", "score": 0.0, "max_score": 3.0, "details": []},
    ), patch(
        "aats.data_platform.decision_system.candidate_selector._evaluate_phase3_score",
        return_value={"dimension": "phase3", "score": 0.0, "max_score": 2.0, "details": []},
    ), patch(
        "aats.data_platform.decision_system.candidate_selector._evaluate_phase4_score",
        return_value={"dimension": "phase4", "score": 0.0, "max_score": 2.0, "details": []},
    ), patch(
        "aats.data_platform.decision_system.candidate_selector._evaluate_governance_score",
        return_value={"dimension": "phase5", "score": 0.0, "max_score": 2.0, "details": []},
    ):
        selected = select_parameter_upgrade_candidates([parameter_set], {})

    assert selected[0]["source_round_id"] == "round_source_001"


def test_decision_engine_ignores_failure_ratio_for_replay_only_phase3() -> None:
    evidence_bundle = {
        "phase2_evidence": {
            "combo_stats": {
                "independent_15m": {
                    "available": True,
                    "experiments_with_openings": 3,
                    "max_opening_count": 3,
                    "mean_positive_edge_ratio": 0.4,
                },
            },
        },
        "phase3_evidence": {
            "latest_round": {
                "replay_only": True,
                "combos": {
                    "independent_15m": {
                        "status": "succeeded",
                        "top_failure_modes": {
                            "total_failures": 100,
                            "total_success": 0,
                        },
                    },
                },
            },
        },
        "phase4_evidence": {
            "latest_round": {
                "combos": {
                    "independent_15m": {
                        "cost_summary": {
                            "cost_adjusted_edge_mean": 1.2,
                            "full_fill_ratio": 1.0,
                        },
                    },
                },
            },
        },
        "phase5_governance_evidence": {"quality_health": "healthy"},
    }

    result = decide_family_timeframe_status(
        "independent",
        "15m",
        evidence_bundle,
    )

    assert result["decision"] == "keep_active"
    assert result["signal_summary"]["severe_negative"] == 0


def test_full_pipeline_has_no_unbound_latest_parameter_lookup() -> None:
    module = _load_script_module("rdp_run_full_pipeline_test", "scripts/rdp_run_full_pipeline.py")
    assert not hasattr(module, "_find_latest_params_json")


def test_governance_roots_include_modern_step_rounds() -> None:
    assert artifact_index.KNOWN_ARTIFACT_ROOTS["step2_rounds"]["path"] == "artifacts/research/step2_rounds"
    assert artifact_index.KNOWN_ARTIFACT_ROOTS["step3_rounds"]["path"] == "artifacts/research/step3_rounds"
    assert round_status.PHASE_ARTIFACT_ROOTS["phase2_step2"] == "artifacts/research/step2_rounds"
    assert round_status.PHASE_ARTIFACT_ROOTS["phase2_step3"] == "artifacts/research/step3_rounds"
    assert "phase2_step3" in manifest_validation.KNOWN_PHASES


def test_validate_artifacts_script_knows_phase2_step3() -> None:
    module = _load_script_module("rdp_validate_artifacts_test", "scripts/rdp_validate_artifacts.py")

    assert module._PHASE_ROOTS["phase2_step2"] == ["artifacts/research/step2_rounds"]
    assert module._PHASE_ROOTS["phase2_step3"] == ["artifacts/research/step3_rounds"]


def test_validate_artifacts_fix_is_disabled_before_manifest_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script_module(
        "rdp_validate_artifacts_read_only_test",
        "scripts/rdp_validate_artifacts.py",
    )
    manifest_path = (
        tmp_path
        / "artifacts/research/attribution_rounds/legacy_round/round_manifest.json"
    )
    manifest_path.parent.mkdir(parents=True)
    original = b'{"round_id":"legacy_round"}\n'
    manifest_path.write_bytes(original)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rdp_validate_artifacts.py",
            "--artifact-root",
            str(tmp_path),
            "--phase",
            "phase3",
            "--fix",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        module.main()

    assert exc_info.value.code == 2
    assert manifest_path.read_bytes() == original
