from __future__ import annotations

import json
from types import SimpleNamespace

from aats.data_platform.operations import workflow_dispatcher
from scripts import rdp_run_decision_round, rdp_run_scheduled_workflow


def test_research_outcome_maps_readiness_without_losing_blocker_type() -> None:
    assert (
        rdp_run_decision_round._research_outcome_from_readiness(
            "ready_for_next_live_test",
        )
        == "eligible"
    )
    assert (
        rdp_run_decision_round._research_outcome_from_readiness(
            "not_ready_attribution_issue",
        )
        == "blocked_by_attribution"
    )
    assert (
        rdp_run_decision_round._research_outcome_from_readiness(
            "not_ready_execution_issue",
        )
        == "blocked_by_execution"
    )


def test_decision_result_marker_contains_release_relevant_truth(capsys) -> None:
    rdp_run_decision_round._emit_decision_result(
        round_id="round_1",
        readiness_report={
            "readiness": "not_ready_attribution_issue",
            "overall_confidence": "high",
            "checks_passed": 5,
            "checks_total": 7,
            "blockers": ["attribution unavailable"],
        },
        upgrade_candidates=[
            {"decision": "promote_candidate"},
            {"decision": "hold"},
        ],
        ft_decisions=[
            {"decision": "keep_active"},
            {"decision": "keep_active"},
        ],
    )

    marker = capsys.readouterr().out.strip()
    payload = json.loads(
        marker.removeprefix(rdp_run_decision_round._DECISION_RESULT_PREFIX),
    )
    assert payload["round_id"] == "round_1"
    assert payload["research_outcome"] == "blocked_by_attribution"
    assert payload["promote_candidate_count"] == 1
    assert payload["decision_counts"] == {"keep_active": 2}


def test_scheduled_workflow_marker_propagates_pipeline_business_result(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        rdp_run_scheduled_workflow,
        "parse_args",
        lambda: SimpleNamespace(
            workflow="research_cycle",
            list=False,
            dry_run=False,
            no_stop_on_failure=False,
        ),
    )
    monkeypatch.setattr(
        workflow_dispatcher,
        "run_workflow",
        lambda *_args, **_kwargs: {
            "run_id": "workflow_1",
            "workflow": "research_cycle",
            "overall_status": "success",
            "succeeded": 1,
            "failed": 0,
            "skipped": 0,
            "tasks": [
                {
                    "name": "full_pipeline",
                    "status": "success",
                    "pipeline_result": {
                        "research_outcome": "blocked_by_attribution",
                        "decision_round_id": "round_1",
                        "readiness": "not_ready_attribution_issue",
                    },
                }
            ],
        },
    )

    assert rdp_run_scheduled_workflow.main() == 0
    marker = next(
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith(rdp_run_scheduled_workflow._WORKFLOW_RESULT_PREFIX)
    )
    payload = json.loads(
        marker.removeprefix(rdp_run_scheduled_workflow._WORKFLOW_RESULT_PREFIX),
    )
    assert payload["research_outcome"] == "blocked_by_attribution"
    assert payload["decision_round_id"] == "round_1"
