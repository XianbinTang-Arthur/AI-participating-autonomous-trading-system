from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

from aats.data_platform.decision_system.candidate_selector import (
    select_parameter_upgrade_candidates,
)
from aats.data_platform.decision_system.decision_engine import (
    decide_family_timeframe_status,
)
from aats.data_platform.decision_system.evidence_bundle import collect_phase2_evidence
from aats.data_platform.governance import artifact_index, manifest_validation, round_status
from aats.data_platform.governance.parameter_registry import (
    import_from_parameter_candidates,
)
from aats.data_platform.replay.diagnostics.replay_diagnostics import extract_comparison_rows


def _write_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _load_script_module(name: str, relative_path: str):
    path = Path(relative_path).resolve()
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


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


def test_collect_phase2_evidence_reads_step2_round_combo_stats(tmp_path: Path) -> None:
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

    evidence = collect_phase2_evidence(tmp_path)
    combo = evidence["combo_stats"]["independent_15m"]

    assert combo["available"] is True
    assert combo["experiments_with_openings"] == 2
    assert combo["max_opening_count"] == 20
    assert evidence["global_stats"]["total_experiments"] == 2


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

    assert combo["available"] is True
    assert combo["total_experiments"] == 1
    assert combo["max_opening_count"] == 15


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


def test_full_pipeline_prefers_step3_merged_over_step2(tmp_path: Path) -> None:
    module = _load_script_module("rdp_run_full_pipeline_test", "scripts/rdp_run_full_pipeline.py")
    research_root = tmp_path / "artifacts" / "research"
    _write_json(
        research_root / "step2_rounds" / "20260416_120000_x" / "parameter_candidates.json",
        {"candidates": {"independent_15m": {"signal_edge_scale_bps": 10}}},
    )
    step3_path = research_root / "step3_rounds" / "20260415_120000_y" / "parameter_candidates_merged.json"
    _write_json(
        step3_path,
        {"candidates": {"independent_15m": {"signal_edge_scale_bps": 12}}},
    )

    with patch.object(module, "_PROJECT_ROOT", tmp_path):
        found = module._find_latest_params_json()

    assert found == step3_path


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
