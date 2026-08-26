from __future__ import annotations

from types import SimpleNamespace
import json

from scripts import rdp_run_full_pipeline


def test_exit_two_is_partial_only_for_research_batch_phases(monkeypatch) -> None:
    monkeypatch.setattr(
        rdp_run_full_pipeline.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=2),
    )

    phase4 = rdp_run_full_pipeline._run_phase("phase4", ["python", "phase4.py"])
    decision = rdp_run_full_pipeline._run_phase("decision", ["python", "decision.py"])

    assert phase4["status"] == "partial_success"
    assert decision["status"] == "failed"


def test_decision_phase_captures_structured_business_result(monkeypatch) -> None:
    payload = {
        "round_id": "round_1",
        "readiness": "not_ready_attribution_issue",
        "research_outcome": "blocked_by_attribution",
    }
    monkeypatch.setattr(
        rdp_run_full_pipeline.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=(
                rdp_run_full_pipeline._DECISION_RESULT_PREFIX
                + json.dumps(payload)
                + "\n"
            ),
            stderr="",
        ),
    )

    result = rdp_run_full_pipeline._run_phase(
        "decision",
        ["python", "decision.py"],
        result_prefix=rdp_run_full_pipeline._DECISION_RESULT_PREFIX,
    )

    assert result["status"] == "success"
    assert result["structured_result"] == payload


def test_decision_phase_fails_closed_when_result_marker_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(
        rdp_run_full_pipeline.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="decision finished without contract marker\n",
            stderr="",
        ),
    )

    result = rdp_run_full_pipeline._run_phase(
        "decision",
        ["python", "decision.py"],
        result_prefix=rdp_run_full_pipeline._DECISION_RESULT_PREFIX,
    )

    assert result["status"] == "failed"
    assert result["exit_code"] == 0
    assert "missing structured result marker" in result["error"]


def test_pipeline_marker_propagates_research_outcome(capsys) -> None:
    decision_result = {
        "round_id": "round_1",
        "readiness": "not_ready_attribution_issue",
        "research_outcome": "blocked_by_attribution",
    }

    rdp_run_full_pipeline._emit_pipeline_result(
        pipeline_id="pipeline_1",
        status="succeeded",
        results=[
            {
                "phase": "decision",
                "status": "success",
                "structured_result": decision_result,
            }
        ],
    )

    marker = capsys.readouterr().out.strip()
    payload = json.loads(marker.removeprefix(rdp_run_full_pipeline._PIPELINE_RESULT_PREFIX))
    assert payload["research_outcome"] == "blocked_by_attribution"
    assert payload["decision_round_id"] == "round_1"
    assert payload["readiness"] == "not_ready_attribution_issue"
