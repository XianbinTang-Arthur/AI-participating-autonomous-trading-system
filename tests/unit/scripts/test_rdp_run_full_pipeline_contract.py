from __future__ import annotations

from types import SimpleNamespace

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
