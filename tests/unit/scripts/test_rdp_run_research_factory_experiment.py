import json

from scripts.rdp_run_research_factory_experiment import main


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
