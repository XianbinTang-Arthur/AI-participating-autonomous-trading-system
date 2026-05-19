import importlib.util
import json
from pathlib import Path


def script_path() -> Path:
    return Path("scripts") / "rdp_generate_research_observation_summary.py"


def load_script_module():
    spec = importlib.util.spec_from_file_location(
        "rdp_generate_research_observation_summary_under_test",
        script_path(),
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def research_root(tmp_path: Path) -> Path:
    return tmp_path / "artifacts" / "research" / "research_factory"


def write_events(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": "2026-05-18T00:00:00+00:00",
        "bar_ts": "2026-05-18T00:00:00+00:00",
        "recommendation_id": "rec_cli_obs_summary",
        "candidate_id": "cand_cli_obs_summary",
        "experiment_id": "exp_cli_obs_summary",
        "mode": "paper",
        "signal": True,
        "paper_intent": True,
        "fillable": True,
        "partial_fill": False,
        "fee_bps": 5.0,
        "slippage_bps": 2.0,
        "funding_bps": 0.5,
        "cost_adjusted_edge_bps": 1.25,
        "drawdown": 0.04,
        "metric_drift": 0.1,
        "abort_triggered": False,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return path


def base_args(tmp_path: Path) -> list[str]:
    root = research_root(tmp_path)
    events = write_events(root / "observation_events" / "events.jsonl")
    output = root / "observation_inputs" / "summary.json"
    return [
        "--events-jsonl",
        str(events),
        "--output",
        str(output),
        "--recommendation-id",
        "rec_cli_obs_summary",
        "--candidate-id",
        "cand_cli_obs_summary",
        "--experiment-id",
        "exp_cli_obs_summary",
        "--mode",
        "paper",
    ]


def test_observation_summary_cli_success_prints_json(capsys, tmp_path: Path) -> None:
    module = load_script_module()

    code = module.main(base_args(tmp_path))

    payload = json.loads(capsys.readouterr().out)
    summary = json.loads(Path(payload["summary_path"]).read_text(encoding="utf-8"))
    assert code == 0
    assert payload["status"] == "succeeded"
    assert payload["runtime_mutation_allowed"] is False
    assert payload["okx_write_allowed"] is False
    assert summary["paper_intent_count"] == 1


def test_observation_summary_cli_failure_prints_json(capsys, tmp_path: Path) -> None:
    module = load_script_module()
    args = base_args(tmp_path)
    candidate_index = args.index("--candidate-id")
    args[candidate_index + 1] = "cand_other"

    code = module.main(args)

    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["status"] == "failed"
    assert "candidate_id" in payload["error"]
    assert payload["active_parameter_write_allowed"] is False


def test_observation_summary_cli_does_not_load_dotenv() -> None:
    script = script_path().read_text(encoding="utf-8")
    assert "load_dotenv" not in script
    assert "dotenv" not in script
    assert ".env." not in script
