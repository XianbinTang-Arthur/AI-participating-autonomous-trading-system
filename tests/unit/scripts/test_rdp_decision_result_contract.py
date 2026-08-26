from __future__ import annotations

import json
from types import SimpleNamespace
from pathlib import Path

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


def test_decision_parameter_sets_use_db_first_registry_without_json_file(
    monkeypatch,
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "artifacts/governance/current_parameter_registry.json"
    assert registry_path.exists() is False
    observed_paths = []
    monkeypatch.setattr(
        rdp_run_decision_round,
        "load_registry",
        lambda path: observed_paths.append(path) or {
            "parameter_sets": [
                {"parameter_set_id": "candidate", "status": "candidate"},
                {"parameter_set_id": "frozen", "status": "frozen"},
                {"parameter_set_id": "draft", "status": "draft"},
            ],
        },
    )

    selected = rdp_run_decision_round._load_decision_parameter_sets(
        tmp_path,
        include_draft=False,
    )

    assert observed_paths == [registry_path]
    assert [item["parameter_set_id"] for item in selected] == ["candidate", "frozen"]


def test_registry_batch_sync_preserves_recommendation_source_round(monkeypatch) -> None:
    from contextlib import contextmanager

    from aats.data_platform.decision_system import recommendation_registry
    from aats.data_platform.governance import recommendations_db

    captured = []

    class _Session:
        def __init__(self, _engine):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @contextmanager
        def begin(self):
            yield

    class _Engine:
        def dispose(self):
            pass

    monkeypatch.setattr(recommendation_registry, "try_governance_db", lambda: (_Engine(), True))
    monkeypatch.setattr("sqlalchemy.orm.Session", _Session)
    monkeypatch.setattr(
        recommendations_db,
        "db_upsert_recommendation",
        lambda _session, **kwargs: captured.append(kwargs),
    )
    monkeypatch.setattr(recommendations_db, "db_upsert_active_decision", lambda *_a, **_k: None)

    recommendation_registry._sync_registries_to_db_best_effort(
        {
            "recommendations": [
                {
                    "recommendation_id": "rec_1",
                    "family": "independent",
                    "timeframe": "15m",
                    "recommendation_type": "parameter_upgrade",
                    "source_round_id": "round_source_001",
                }
            ]
        },
        {"decisions": []},
    )

    assert captured[0]["source_round_id"] == "round_source_001"


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


def test_scheduled_workflow_marker_propagates_direct_decision_result(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        rdp_run_scheduled_workflow,
        "parse_args",
        lambda: SimpleNamespace(
            workflow="decision_cycle",
            list=False,
            dry_run=False,
            no_stop_on_failure=False,
        ),
    )
    monkeypatch.setattr(
        workflow_dispatcher,
        "run_workflow",
        lambda *_args, **_kwargs: {
            "run_id": "workflow_2",
            "workflow": "decision_cycle",
            "overall_status": "success",
            "succeeded": 1,
            "failed": 0,
            "skipped": 0,
            "tasks": [
                {
                    "name": "decision_round",
                    "status": "success",
                    "decision_result": {
                        "research_outcome": "blocked_by_attribution",
                        "round_id": "round_2",
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
    assert payload["decision_round_id"] == "round_2"
