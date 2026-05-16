import json

from scripts.rdp_run_research_factory_smoke import main


def test_rdp_run_research_factory_smoke_cli_accepts_execution_summary(
    tmp_path,
    capsys,
) -> None:
    artifact_root = tmp_path / "artifacts" / "research" / "research_factory" / "experiments"
    execution_summary = tmp_path / "execution_cost_summary.json"
    execution_summary.write_text(
        json.dumps(
            {
                "full_fill_ratio": 1.0,
                "partial_fill_ratio": 0.0,
                "turnover": {"mean": 0.5},
                "fee": {"mean": 5.0},
                "funding": {"mean": 0.1},
                "slippage": {"mean": 1.0},
                "cost_adjusted_edge": {"mean": 2.0},
            }
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--artifact-root",
            str(artifact_root),
            "--experiment-id",
            "rf_smoke_cli_exec",
            "--execution-cost-summary",
            str(execution_summary),
            "--overwrite",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["status"] == "succeeded"
    assert payload["candidate_generated"] is True
    assert payload["recommendation_ref"] == "research_recommendation.json"
