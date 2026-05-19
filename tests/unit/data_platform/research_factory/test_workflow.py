import json
import shutil
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aats.data_platform.research_factory.datasets.gold_bars import GoldBarRecord
from aats.data_platform.research_factory.real_data import (
    GoldReplayLoadResult,
    ResearchFactoryExperimentConfig,
)
from aats.data_platform.research_factory.observation_sources import OBSERVATION_SUMMARY_SCHEMA_VERSION
from aats.data_platform.research_factory.workflow import (
    ResearchGovernanceWorkflowConfig,
    run_research_governance_workflow,
)

UTC = timezone.utc
START = datetime(2026, 5, 1, tzinfo=UTC)


@pytest.fixture
def workspace_tmp_path() -> Iterator[Path]:
    path = Path(".pytest_workspace_tmp") / f"research_governance_workflow_{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class FakeDataSource:
    def __init__(self, load_result: GoldReplayLoadResult) -> None:
        self.load_result = load_result

    def load(self, **kwargs) -> GoldReplayLoadResult:
        return self.load_result


def artifact_root(tmp_path: Path) -> Path:
    return tmp_path / "artifacts" / "research" / "research_factory" / "experiments"


def research_factory_root(tmp_path: Path) -> Path:
    return tmp_path / "artifacts" / "research" / "research_factory"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def gold_records() -> tuple[GoldBarRecord, ...]:
    closes = (100.0, 100.8, 101.3, 102.1, 102.6, 103.4, 104.0, 104.9, 105.5, 106.4, 107.2, 108.3)
    records: list[GoldBarRecord] = []
    for index, close in enumerate(closes):
        records.append(
            GoldBarRecord(
                symbol="BTC-USDT-SWAP",
                timeframe="1h",
                ts=START + timedelta(hours=index),
                open=close - 0.25,
                high=close + 0.75,
                low=close - 1.0,
                close=close,
                volume=10_000 + index * 100,
                funding_rate=0.0001,
                metadata={
                    "source_candle_dataset_version": "v1.0",
                    "source_funding_dataset_version": "funding_v1",
                    "build_run_id": "build-1",
                },
            )
        )
    return tuple(records)


def load_result() -> GoldReplayLoadResult:
    records = gold_records()
    return GoldReplayLoadResult(
        records=records,
        source_watermark={
            "gold_table": "gold.market_swap_replay_bars_1h",
            "row_count": len(records),
            "min_ts": records[0].ts.isoformat(),
            "max_ts": records[-1].ts.isoformat(),
            "source_candle_dataset_versions": ["v1.0"],
            "source_funding_dataset_versions": ["funding_v1"],
            "build_run_ids": ["build-1"],
        },
        gold_table="gold.market_swap_replay_bars_1h",
        dataset_version="v1.0",
    )


def write_execution_cost_summary(path: Path, *, cost_adjusted_edge: float = 1.75) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "execution_cost_summary_v1",
                "source_run_id": "phase4-run-1",
                "symbol": "BTC-USDT-SWAP",
                "timeframe": "1h",
                "window_start": START.isoformat(),
                "window_end": (START + timedelta(hours=12)).isoformat(),
                "dataset_fingerprint_compatibility": "compatible",
                "compatibility_reason": "unit test fixture uses the same configured dataset window",
                "full_fill_ratio": 0.9,
                "partial_fill_ratio": 0.1,
                "turnover": {"mean": 0.5},
                "fee": {"mean": 4.5},
                "funding": {"mean": 0.2},
                "slippage": {"mean": 1.5},
                "cost_adjusted_edge": {"mean": cost_adjusted_edge},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def write_observation_summary(
    path: Path,
    *,
    experiment_id: str,
    observed_bars: int = 96,
    observed_events: int = 12,
    cost_adjusted_edge: float = 1.1,
) -> None:
    candidate_id = f"cand_{experiment_id}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": OBSERVATION_SUMMARY_SCHEMA_VERSION,
                "mode": "shadow",
                "recommendation_id": f"rec_{candidate_id}",
                "candidate_id": candidate_id,
                "experiment_id": experiment_id,
                "observation_start": (START + timedelta(hours=12)).isoformat(),
                "observation_end": (START + timedelta(hours=24)).isoformat(),
                "observed_bars": observed_bars,
                "observed_events": observed_events,
                "signal_count": 15,
                "paper_intent_count": 0,
                "fillable_ratio": 0.92,
                "partial_fill_ratio": 0.04,
                "fee_bps_mean": 5.0,
                "slippage_bps_mean": 1.8,
                "funding_bps_mean": 0.4,
                "cost_adjusted_edge_bps_mean": cost_adjusted_edge,
                "drawdown": 0.08,
                "metric_drift": 0.12,
                "abort_triggered": False,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def experiment_config(
    root: Path,
    execution_summary: Path,
    *,
    experiment_id: str,
    research_profile: str | None = "smoke",
) -> ResearchFactoryExperimentConfig:
    return ResearchFactoryExperimentConfig(
        symbol="BTC-USDT-SWAP",
        timeframe="1h",
        start=START,
        end=START + timedelta(hours=12),
        factor_expression="Return(close, 1)",
        research_profile=research_profile,
        artifact_root=root,
        experiment_id=experiment_id,
        train_ratio=0.4,
        valid_ratio=0.2,
        test_ratio=0.4,
        execution_cost_summary_path=execution_summary,
        require_execution_realism=True,
        overwrite=True,
        timestamp=START,
    )


def test_governance_workflow_creates_review_pending_chain(workspace_tmp_path: Path) -> None:
    root = artifact_root(workspace_tmp_path)
    execution_summary = root.parent / "phase4" / "execution_cost_summary.json"
    observation_summary = root.parent / "observation_inputs" / "shadow_summary.json"
    write_execution_cost_summary(execution_summary)
    write_observation_summary(observation_summary, experiment_id="rf_governance_success")

    result = run_research_governance_workflow(
        ResearchGovernanceWorkflowConfig(
            experiment_config=experiment_config(
                root,
                execution_summary,
                experiment_id="rf_governance_success",
            ),
            observation_summary_path=observation_summary,
            workflow_id="wf_governance_success",
            allow_smoke_profile=True,
            timestamp=START,
        ),
        data_source=FakeDataSource(load_result()),
    )

    factory_root = research_factory_root(workspace_tmp_path)
    workflow_summary = read_json(factory_root / "workflows" / "wf_governance_success" / "workflow_summary.json")
    registry_entries = read_jsonl(factory_root / "registry" / "research_memory.jsonl")

    assert result.status == "preapply_review_pending"
    assert result.reference_integrity_passed is True
    assert result.observation_gate_passed is True
    assert workflow_summary["next_step"] == "operator_preapply_review"
    assert workflow_summary["runtime_mutation_allowed"] is False
    assert workflow_summary["artifact_refs"]["candidate_artifact"] == (
        "experiments/rf_governance_success/candidate_artifact.json"
    )
    assert workflow_summary["artifact_refs"]["operator_review_summary"] == (
        "workflows/wf_governance_success/preapply_review_summary.md"
    )
    assert workflow_summary["risk_flags"] == ["execution_evidence_uses_dataset_compatibility"]
    assert workflow_summary["blocking_failures"] == []
    assert workflow_summary["failed_stage"] is None
    assert workflow_summary["blocking_artifact"] is None
    assert workflow_summary["next_debug_action"] == (
        "inspect operator_review_checklist.json before any separate manual design review"
    )
    stage_results = workflow_summary["stage_results"]
    assert [stage["stage_name"] for stage in stage_results] == [
        "real_data_experiment",
        "observation",
        "observation_gate",
        "preapply_package",
        "reference_integrity",
        "preapply_review",
        "registry_memory",
        "workflow_summary",
    ]
    assert all(stage["runtime_mutation_allowed"] is False for stage in stage_results)
    assert all(stage["status"] == "succeeded" for stage in stage_results)
    assert (factory_root / "preapply" / result.package_id / "preapply_evidence_package.json").exists()
    assert (
        factory_root
        / "preapply_reviews"
        / result.preapply_review_id
        / "evidence_reference_integrity_report.json"
    ).exists()
    assert (factory_root / "workflows" / "wf_governance_success" / "preapply_review_summary.md").exists()
    checklist = read_json(factory_root / "workflows" / "wf_governance_success" / "operator_review_checklist.json")
    review_manifest = read_json(
        factory_root / "preapply_reviews" / result.preapply_review_id / "preapply_review_manifest.json"
    )
    assert checklist["runtime_mutation_allowed"] is False
    assert checklist["active_parameter_write_allowed"] is False
    assert checklist["runtime_config_write_allowed"] is False
    assert checklist["okx_write_allowed"] is False
    assert checklist["dry_run_execution_allowed"] is False
    assert checklist["failed_stage"] is None
    assert checklist["blocking_artifact"] is None
    assert checklist["schema_version"] == "research_operator_review_checklist_v2"
    assert checklist["readiness"]["all_required_refs_present"] is True
    assert checklist["readiness"]["reference_integrity_passed"] is True
    assert checklist["readiness"]["observation_gate_passed"] is True
    assert checklist["readiness"]["candidate_gate_passed"] is True
    assert checklist["readiness"]["runtime_mutation_allowed"] is False
    assert checklist["readiness"]["operator_decision_required"] is True
    assert checklist["allowed_next_actions"] == [
        "review_preapply_evidence",
        "request_more_observation",
        "reject_candidate",
        "archive_candidate",
    ]
    assert "active_parameter_apply" in checklist["forbidden_next_actions"]
    assert "okx_write" in checklist["forbidden_next_actions"]
    assert "runtime_config_write" in checklist["forbidden_next_actions"]
    assert "auto_apply" in checklist["forbidden_next_actions"]
    assert "dry_run_execute" in checklist["forbidden_next_actions"]
    assert checklist["stage_results"][0]["stage_name"] == "real_data_experiment"
    assert "does not authorize active parameter changes" in checklist["no_runtime_mutation_statement"]
    operator_summary = (
        factory_root / "workflows" / "wf_governance_success" / "preapply_review_summary.md"
    ).read_text(encoding="utf-8")
    assert "## 1. Candidate Overview" in operator_summary
    assert "## 3. Data Evidence" in operator_summary
    assert "## 9. Verdict" in operator_summary
    assert "## 11. Forbidden Actions" in operator_summary
    assert "## 12. Explicit Non-Authorization Statement" in operator_summary
    assert "dry-run execution" in operator_summary
    assert review_manifest["output_refs"]["evidence_reference_integrity_report"] == (
        "evidence_reference_integrity_report.json"
    )
    assert {entry["status"] for entry in registry_entries} >= {
        "recommendation_ready",
        "observation_eligible_for_preapply",
        "preapply_ready",
    }


def test_governance_workflow_requires_explicit_profile(workspace_tmp_path: Path) -> None:
    root = artifact_root(workspace_tmp_path)
    execution_summary = root.parent / "phase4" / "execution_cost_summary.json"
    observation_summary = root.parent / "observation_inputs" / "shadow_summary.json"
    write_execution_cost_summary(execution_summary)
    write_observation_summary(observation_summary, experiment_id="rf_governance_no_profile")

    with pytest.raises(ValueError, match="explicit research_profile"):
        ResearchGovernanceWorkflowConfig(
            experiment_config=experiment_config(
                root,
                execution_summary,
                experiment_id="rf_governance_no_profile",
                research_profile=None,
            ),
            observation_summary_path=observation_summary,
            timestamp=START,
        )


def test_governance_workflow_rejects_smoke_profile_without_opt_in(workspace_tmp_path: Path) -> None:
    root = artifact_root(workspace_tmp_path)
    execution_summary = root.parent / "phase4" / "execution_cost_summary.json"
    observation_summary = root.parent / "observation_inputs" / "shadow_summary.json"
    write_execution_cost_summary(execution_summary)
    write_observation_summary(observation_summary, experiment_id="rf_governance_smoke_blocked")

    with pytest.raises(ValueError, match="allow_smoke_profile=True"):
        ResearchGovernanceWorkflowConfig(
            experiment_config=experiment_config(
                root,
                execution_summary,
                experiment_id="rf_governance_smoke_blocked",
            ),
            observation_summary_path=observation_summary,
            timestamp=START,
        )


def test_governance_workflow_failed_observation_gate_does_not_become_ready(workspace_tmp_path: Path) -> None:
    root = artifact_root(workspace_tmp_path)
    execution_summary = root.parent / "phase4" / "execution_cost_summary.json"
    observation_summary = root.parent / "observation_inputs" / "shadow_summary.json"
    write_execution_cost_summary(execution_summary)
    write_observation_summary(
        observation_summary,
        experiment_id="rf_governance_keep_reviewing",
        observed_bars=1,
        observed_events=0,
    )

    result = run_research_governance_workflow(
        ResearchGovernanceWorkflowConfig(
            experiment_config=experiment_config(
                root,
                execution_summary,
                experiment_id="rf_governance_keep_reviewing",
            ),
            observation_summary_path=observation_summary,
            workflow_id="wf_governance_keep_reviewing",
            allow_smoke_profile=True,
            timestamp=START,
        ),
        data_source=FakeDataSource(load_result()),
    )

    factory_root = research_factory_root(workspace_tmp_path)
    package = read_json(factory_root / "preapply" / result.package_id / "preapply_evidence_package.json")
    workflow_summary = read_json(
        factory_root / "workflows" / "wf_governance_keep_reviewing" / "workflow_summary.json"
    )
    checklist = read_json(
        factory_root / "workflows" / "wf_governance_keep_reviewing" / "operator_review_checklist.json"
    )
    registry_entries = read_jsonl(factory_root / "registry" / "research_memory.jsonl")

    assert result.status == "needs_more_observation"
    assert result.observation_gate_passed is False
    assert package["status"] == "needs_more_observation"
    assert package["review_decision"] == "keep_reviewing"
    assert workflow_summary["failed_stage"] == "observation_gate"
    assert workflow_summary["blocking_artifact"] == (
        f"observations/{result.observation_id}/observation_gate_result.json"
    )
    assert workflow_summary["next_debug_action"] == (
        f"inspect observations/{result.observation_id}/observation_gate_result.json"
    )
    blocked_stage = next(stage for stage in workflow_summary["stage_results"] if stage["stage_name"] == "observation_gate")
    assert blocked_stage["status"] == "blocked"
    assert blocked_stage["blocking_artifact"] == workflow_summary["blocking_artifact"]
    assert checklist["failed_stage"] == "observation_gate"
    assert checklist["blocking_artifact"] == workflow_summary["blocking_artifact"]
    assert checklist["readiness"]["observation_gate_passed"] is False
    assert checklist["readiness"]["preapply_package_ready"] is False
    assert {entry["status"] for entry in registry_entries} >= {
        "observation_keep_reviewing",
        "needs_more_observation",
    }


def test_governance_workflow_failed_experiment_records_stage(workspace_tmp_path: Path) -> None:
    root = artifact_root(workspace_tmp_path)
    execution_summary = root.parent / "phase4" / "execution_cost_summary.json"
    observation_summary = root.parent / "observation_inputs" / "shadow_summary.json"
    write_execution_cost_summary(execution_summary)
    write_observation_summary(observation_summary, experiment_id="rf_governance_no_data_source")

    result = run_research_governance_workflow(
        ResearchGovernanceWorkflowConfig(
            experiment_config=experiment_config(
                root,
                execution_summary,
                experiment_id="rf_governance_no_data_source",
            ),
            observation_summary_path=observation_summary,
            workflow_id="wf_governance_no_data_source",
            allow_smoke_profile=True,
            timestamp=START,
        )
    )

    factory_root = research_factory_root(workspace_tmp_path)
    workflow_summary = read_json(factory_root / "workflows" / "wf_governance_no_data_source" / "workflow_summary.json")

    assert result.status == "failed"
    assert result.error == "data_source is required; CLI should provide a GoldReplayDataSource"
    assert workflow_summary["failed_stage"] == "real_data_experiment"
    assert workflow_summary["blocking_artifact"] == (
        "experiments/rf_governance_no_data_source/experiment_manifest.json"
    )
    assert workflow_summary["blocking_failures"] == [
        "data_source is required; CLI should provide a GoldReplayDataSource"
    ]
    assert workflow_summary["next_debug_action"] == (
        "inspect experiments/rf_governance_no_data_source/experiment_manifest.json"
    )
    assert workflow_summary["stage_results"] == [
        {
            "artifact_refs": {
                "blocking_artifact": "experiments/rf_governance_no_data_source/experiment_manifest.json",
                "experiment_manifest": "experiments/rf_governance_no_data_source/experiment_manifest.json",
                "workflow_summary": "workflows/wf_governance_no_data_source/workflow_summary.json",
            },
            "blocking_artifact": "experiments/rf_governance_no_data_source/experiment_manifest.json",
            "blocking_failures": ["data_source is required; CLI should provide a GoldReplayDataSource"],
            "next_debug_action": "inspect experiments/rf_governance_no_data_source/experiment_manifest.json",
            "runtime_mutation_allowed": False,
            "stage_name": "real_data_experiment",
            "status": "failed",
        }
    ]
