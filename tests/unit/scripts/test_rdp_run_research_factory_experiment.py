import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.rdp_run_research_factory_experiment import _resolve_factor_input, main


def test_rdp_run_research_factory_experiment_requires_database_url_env(monkeypatch, capsys) -> None:
    monkeypatch.delenv("RDP_DATABASE_URL", raising=False)

    exit_code = main(
        [
            "--symbol",
            "BTC-USDT-SWAP",
            "--timeframe",
            "15m",
            "--start",
            "2026-05-01",
            "--end",
            "2026-05-02",
            "--factor-expression",
            "Return(close, 1)",
            "--execution-cost-summary",
            "artifacts/research/execution_cost_summary.json",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 2
    assert payload["status"] == "failed"
    assert payload["candidate_generated"] is False
    assert "RDP_DATABASE_URL" in payload["error"]
    assert ".env files are not read" in payload["error"]


def test_rdp_run_research_factory_experiment_accepts_factor_proposal_input(tmp_path: Path) -> None:
    proposal_path = tmp_path / "artifacts" / "research" / "research_factory" / "proposals" / "proposal.json"
    proposal_path.parent.mkdir(parents=True)
    proposal_path.write_text(
        json.dumps(
            {
                "hypothesis": "Close momentum may retain net edge.",
                "factor_expression": "Return(close, 1)",
                "rationale": "Run the DSL proposal through novelty and evidence gates.",
            }
        ),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        factor_expression=None,
        factor_proposal=proposal_path,
        artifact_root=tmp_path / "artifacts" / "research" / "research_factory" / "experiments",
    )

    proposal, factor_expression = _resolve_factor_input(
        args,
        created_at=datetime(2026, 5, 17, tzinfo=timezone.utc),
    )

    assert proposal is not None
    assert proposal.factor_expression == "Return(close, 1)"
    assert factor_expression == "Return(close, 1)"


def test_rdp_run_research_factory_experiment_rejects_proposal_expression_mismatch(tmp_path: Path) -> None:
    proposal_path = tmp_path / "artifacts" / "research" / "research_factory" / "proposals" / "proposal.json"
    proposal_path.parent.mkdir(parents=True)
    proposal_path.write_text(
        json.dumps(
            {
                "hypothesis": "Two bar close momentum may retain net edge.",
                "factor_expression": "Return(close, 2)",
                "rationale": "The CLI must not silently run a different factor.",
            }
        ),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        factor_expression="Return(close, 1)",
        factor_proposal=proposal_path,
        artifact_root=tmp_path / "artifacts" / "research" / "research_factory" / "experiments",
    )

    with pytest.raises(ValueError, match="must match"):
        _resolve_factor_input(args, created_at=datetime(2026, 5, 17, tzinfo=timezone.utc))
