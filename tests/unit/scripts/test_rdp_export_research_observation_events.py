import importlib.util
import json
from pathlib import Path


def script_path() -> Path:
    return Path("scripts") / "rdp_export_research_observation_events.py"


def load_script_module():
    spec = importlib.util.spec_from_file_location(
        "rdp_export_research_observation_events_under_test",
        script_path(),
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def research_root(tmp_path: Path) -> Path:
    return tmp_path / "artifacts" / "research" / "research_factory"


def write_source_events(path: Path, *, include_edge: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": "shadow_evt_cli",
        "created_at": "2026-05-18T00:00:00+00:00",
        "payload": {
            "bar_ts": "2026-05-18T00:00:00+00:00",
            "signal": True,
            "fillable": True,
            "partial_fill": False,
            "fee_bps": 5.0,
            "slippage_bps": 2.0,
            "funding_bps": 0.5,
            "drawdown": 0.04,
            "metric_drift": 0.1,
            "abort_triggered": False,
        },
    }
    if include_edge:
        payload["payload"]["cost_adjusted_edge_bps"] = 1.25
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return path


def base_args(tmp_path: Path, *, include_edge: bool = True) -> list[str]:
    root = research_root(tmp_path)
    source = write_source_events(root / "source_events" / "shadow_decisions.jsonl", include_edge=include_edge)
    output = root / "observation_events" / "events.jsonl"
    return [
        "--source-events",
        str(source),
        "--output-events",
        str(output),
        "--recommendation-id",
        "rec_cli_export",
        "--candidate-id",
        "cand_cli_export",
        "--experiment-id",
        "exp_cli_export",
        "--mode",
        "shadow",
        "--source-kind",
        "shadow_decision",
    ]


def test_event_export_cli_success_prints_json(capsys, tmp_path: Path) -> None:
    module = load_script_module()

    code = module.main(base_args(tmp_path))

    payload = json.loads(capsys.readouterr().out)
    events_path = Path(payload["events_path"])
    event_payload = json.loads(events_path.read_text(encoding="utf-8").splitlines()[0])
    assert code == 0
    assert payload["status"] == "succeeded"
    assert payload["observation_event_count"] == 1
    assert payload["runtime_mutation_allowed"] is False
    assert payload["dry_run_execution_allowed"] is False
    assert event_payload["recommendation_id"] == "rec_cli_export"


def test_event_export_cli_failure_prints_json(capsys, tmp_path: Path) -> None:
    module = load_script_module()

    code = module.main(base_args(tmp_path, include_edge=False))

    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["status"] == "failed"
    assert "cost_adjusted_edge_bps" in payload["error"]
    assert payload["active_parameter_write_allowed"] is False
    assert payload["okx_write_allowed"] is False


def test_event_export_cli_does_not_load_dotenv() -> None:
    script = script_path().read_text(encoding="utf-8")
    assert "load_dotenv" not in script
    assert "dotenv" not in script
    assert ".env." not in script
