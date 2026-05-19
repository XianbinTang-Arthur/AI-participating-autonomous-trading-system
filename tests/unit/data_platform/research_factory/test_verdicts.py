import json
import shutil
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aats.data_platform.research_factory.verdicts import (
    CandidateVerdict,
    build_candidate_verdict_from_workflow,
    build_candidate_verdict_from_payloads,
    update_candidate_verdict_board,
)

UTC = timezone.utc


@pytest.fixture
def workspace_tmp_path() -> Iterator[Path]:
    path = Path(".pytest_workspace_tmp") / f"research_factory_verdicts_{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def research_factory_root(tmp_path: Path) -> Path:
    return tmp_path / "artifacts" / "research" / "research_factory"


def dt(day: int) -> datetime:
    return datetime(2026, 5, day, tzinfo=UTC)


def base_payloads(**overrides):
    workflow_summary = {
        "workflow_id": "wf_001",
        "status": "preapply_review_pending",
        "profile": "paper_review",
        "experiment_id": "exp_001",
        "candidate_id": "cand_001",
        "observation_gate_passed": True,
        "reference_integrity_passed": True,
        "risk_flags": [],
        "blocking_failures": [],
    }
    checklist = {
        "readiness": {
            "candidate_gate_passed": True,
            "evidence_bundle_passed": True,
            "observation_gate_passed": True,
            "reference_integrity_passed": True,
        }
    }
    candidate = {
        "candidate_id": "cand_001",
        "experiment_id": "exp_001",
        "candidate_type": "factor",
        "payload": {
            "factor_expression": "Return(close, 1)",
            "dataset_fingerprint": "fp_001",
        },
        "metrics": {
            "net_annualized_return": 0.12,
            "max_drawdown": 0.08,
            "cost_adjusted_edge_bps_mean": 1.2,
            "fillable_ratio": 0.9,
            "partial_fill_ratio": 0.05,
        },
        "gate": {"passed": True, "failures": []},
    }
    metrics = dict(candidate["metrics"])
    observation_gate = {
        "passed": True,
        "failures": [],
    }
    observation_result = {
        "cost_adjusted_edge_bps_mean": 1.4,
        "fillable_ratio": 0.91,
        "partial_fill_ratio": 0.04,
        "drawdown": 0.07,
    }
    evidence_bundle = {"passed": True, "failures": []}
    preapply = {"status": "preapply_ready", "failure_reasons": []}
    experiment_spec = {
        "dataset": {
            "symbol": "BTC-USDT-SWAP",
            "timeframe": "1h",
        }
    }
    payloads = {
        "workflow_summary": workflow_summary,
        "operator_checklist": checklist,
        "candidate_artifact": candidate,
        "metrics_snapshot": metrics,
        "observation_gate_result": observation_gate,
        "observation_result": observation_result,
        "evidence_bundle": evidence_bundle,
        "preapply_evidence_package": preapply,
        "experiment_spec": experiment_spec,
        "created_at": dt(18),
    }
    for key, value in overrides.items():
        payloads[key] = value
    return payloads


def build_verdict(**overrides):
    return build_candidate_verdict_from_payloads(**base_payloads(**overrides))


def test_positive_executable_edge_when_all_gates_pass() -> None:
    verdict = build_verdict()

    assert verdict.verdict == "positive_executable_edge"
    assert verdict.next_action == "review_preapply_evidence"
    assert verdict.runtime_mutation_allowed is False
    assert verdict.okx_write_allowed is False


def test_negative_edge_rejects_candidate() -> None:
    verdict = build_verdict(
        observation_result={
            "cost_adjusted_edge_bps_mean": -0.1,
            "fillable_ratio": 0.91,
            "partial_fill_ratio": 0.04,
            "drawdown": 0.07,
        }
    )

    assert verdict.verdict == "reject"
    assert verdict.next_action == "archive"
    assert "cost-adjusted edge" in verdict.reason


def test_insufficient_observation_keeps_observing() -> None:
    workflow_summary = dict(base_payloads()["workflow_summary"])
    workflow_summary["observation_gate_passed"] = False
    observation_gate = {
        "passed": False,
        "failures": ["observed_bars=4 < 48", "observed_events=0 < 10"],
    }

    verdict = build_verdict(
        workflow_summary=workflow_summary,
        observation_gate_result=observation_gate,
    )

    assert verdict.verdict == "keep_observing"
    assert verdict.next_action == "request_more_observation"


def test_reference_integrity_failure_rejects_candidate() -> None:
    workflow_summary = dict(base_payloads()["workflow_summary"])
    workflow_summary["reference_integrity_passed"] = False
    workflow_summary["blocking_failures"] = ["reference_integrity: missing evidence ref"]

    verdict = build_verdict(workflow_summary=workflow_summary)

    assert verdict.verdict == "reject"
    assert verdict.reason == "reference integrity failed"


def test_execution_compatibility_keeps_observing() -> None:
    workflow_summary = dict(base_payloads()["workflow_summary"])
    workflow_summary["risk_flags"] = ["execution_evidence_uses_dataset_compatibility"]

    verdict = build_verdict(workflow_summary=workflow_summary)

    assert verdict.verdict == "keep_observing"
    assert verdict.reason == "execution evidence uses dataset compatibility mode"


def test_failed_workflow_without_candidate_artifact_still_builds_reject_verdict(
    workspace_tmp_path: Path,
) -> None:
    root = research_factory_root(workspace_tmp_path)
    experiment_dir = root / "experiments" / "exp_gate_failed"
    workflow_dir = root / "workflows" / "wf_gate_failed"
    experiment_dir.mkdir(parents=True)
    workflow_dir.mkdir(parents=True)
    (experiment_dir / "experiment_spec.json").write_text(
        json.dumps(
            {
                "dataset": {
                    "symbol": "BTC-USDT-SWAP",
                    "timeframe": "15m",
                },
                "features": [{"name": "research_factor", "expression": "ZScore(Return(close, 4), 20)"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (experiment_dir / "metrics_snapshot.json").write_text(
        json.dumps(
            {
                "net_annualized_return": -0.05,
                "max_drawdown": 0.08,
                "cost_adjusted_edge_bps_mean": 1.0,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (experiment_dir / "evidence_bundle.json").write_text(
        json.dumps({"passed": True, "failures": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    summary_path = workflow_dir / "workflow_summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "workflow_id": "wf_gate_failed",
                "status": "failed",
                "profile": "real_factor_research",
                "experiment_id": "exp_gate_failed",
                "failed_stage": "real_data_experiment",
                "blocking_failures": ["candidate gate failed: net_annualized_return <= 0"],
                "artifact_refs": {
                    "workflow_summary": "workflows/wf_gate_failed/workflow_summary.json",
                    "experiment_manifest": "experiments/exp_gate_failed/experiment_manifest.json",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    verdict = build_candidate_verdict_from_workflow(summary_path)

    assert verdict.candidate_id == "cand_exp_gate_failed"
    assert verdict.factor_expression == "ZScore(Return(close, 4), 20)"
    assert verdict.verdict == "reject"
    assert verdict.reason == "candidate gate failed"


def test_verdict_next_action_rejects_runtime_apply_text() -> None:
    with pytest.raises(ValueError, match="next_action"):
        CandidateVerdict(
            candidate_id="cand_001",
            experiment_id="exp_001",
            workflow_id="wf_001",
            symbol="BTC-USDT-SWAP",
            timeframe="1h",
            factor_expression="Return(close, 1)",
            research_profile="paper_review",
            net_annualized_return=0.1,
            max_drawdown=0.1,
            cost_adjusted_edge_bps_mean=1.0,
            fillable_ratio=0.9,
            partial_fill_ratio=0.05,
            observation_gate_passed=True,
            reference_integrity_passed=True,
            risk_flags=(),
            verdict="positive_executable_edge",
            reason="valid research-only verdict",
            next_action="active_parameter_apply",
        )


def test_update_candidate_verdict_board_writes_jsonl_and_markdown(
    workspace_tmp_path: Path,
) -> None:
    root = research_factory_root(workspace_tmp_path)
    verdict = build_verdict()

    jsonl_path, md_path = update_candidate_verdict_board(
        (verdict,),
        board_root=root / "verdicts",
    )

    entries = [json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines()]
    markdown = md_path.read_text(encoding="utf-8")
    assert entries[0]["verdict"] == "positive_executable_edge"
    assert entries[0]["runtime_mutation_allowed"] is False
    assert "Research Factory Candidate Verdict Board" in markdown
    assert "does not authorize runtime mutation" in markdown
