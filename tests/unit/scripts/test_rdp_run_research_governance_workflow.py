import importlib.util
import json
from pathlib import Path

import pytest


def script_path() -> Path:
    return Path("scripts") / "rdp_run_research_governance_workflow.py"


def load_script_module():
    spec = importlib.util.spec_from_file_location(
        "rdp_run_research_governance_workflow_under_test",
        script_path(),
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def base_args(tmp_path: Path) -> list[str]:
    root = tmp_path / "artifacts" / "research" / "research_factory" / "experiments"
    execution = root.parent / "inputs" / "execution_cost_summary.json"
    observation = root.parent / "inputs" / "observation_summary.json"
    execution.parent.mkdir(parents=True)
    execution.write_text("{}", encoding="utf-8")
    observation.write_text("{}", encoding="utf-8")
    return [
        "--symbol",
        "BTC-USDT-SWAP",
        "--timeframe",
        "1h",
        "--start",
        "2026-05-01T00:00:00Z",
        "--end",
        "2026-05-02T00:00:00Z",
        "--factor-expression",
        "Return(close, 1)",
        "--research-profile",
        "paper_review",
        "--execution-cost-summary",
        str(execution),
        "--observation-summary",
        str(observation),
        "--observation-source-type",
        "paper",
        "--artifact-root",
        str(root),
        "--workflow-id",
        "wf_cli_test",
        "--database-url-env",
        "TEST_RDP_DATABASE_URL",
    ]


def test_cli_missing_research_profile_prints_json(capsys, tmp_path: Path) -> None:
    module = load_script_module()
    args = base_args(tmp_path)
    profile_index = args.index("--research-profile")
    del args[profile_index : profile_index + 2]

    with pytest.raises(SystemExit) as exc:
        module.main(args)

    payload = json.loads(capsys.readouterr().out)
    assert exc.value.code == 2
    assert payload["status"] == "failed"
    assert "research-profile" in payload["error"]
    assert payload["runtime_mutation_allowed"] is False


def test_cli_missing_db_env_prints_json(monkeypatch, capsys, tmp_path: Path) -> None:
    module = load_script_module()
    monkeypatch.delenv("TEST_RDP_DATABASE_URL", raising=False)

    code = module.main(base_args(tmp_path))

    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["status"] == "failed"
    assert "TEST_RDP_DATABASE_URL" in payload["error"]
    assert ".env files are not read" in payload["error"]


def test_cli_invalid_observation_summary_failure_is_json(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    module = load_script_module()
    monkeypatch.setenv("TEST_RDP_DATABASE_URL", "postgresql://example.invalid/db")
    install_fake_db(monkeypatch, module)

    def fail_workflow(*args, **kwargs):
        raise ValueError("invalid observation summary")

    monkeypatch.setattr(module, "run_research_governance_workflow", fail_workflow)

    code = module.main(base_args(tmp_path))

    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["status"] == "failed"
    assert payload["error"] == "invalid observation summary"
    assert payload["okx_write_allowed"] is False


def test_cli_success_prints_workflow_json_with_injected_workflow(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    module = load_script_module()
    monkeypatch.setenv("TEST_RDP_DATABASE_URL", "postgresql://example.invalid/db")
    install_fake_db(monkeypatch, module)

    class FakeResult:
        status = "preapply_review_pending"

        def to_json(self):
            return json.dumps(
                {
                    "workflow_id": "wf_cli_test",
                    "status": self.status,
                    "workflow_dir": "artifacts/research/research_factory/workflows/wf_cli_test",
                    "next_step": "operator_preapply_review",
                    "runtime_mutation_allowed": False,
                },
                ensure_ascii=False,
                sort_keys=True,
            ) + "\n"

    captured = {}

    def fake_workflow(config, *, data_source):
        captured["profile"] = config.experiment_config.research_profile
        captured["data_source"] = data_source
        return FakeResult()

    monkeypatch.setattr(module, "run_research_governance_workflow", fake_workflow)

    code = module.main(base_args(tmp_path))

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["status"] == "preapply_review_pending"
    assert payload["next_step"] == "operator_preapply_review"
    assert payload["runtime_mutation_allowed"] is False
    assert captured["profile"] == "paper_review"


def test_cli_does_not_load_dotenv() -> None:
    script = script_path().read_text(encoding="utf-8")
    assert "load_dotenv" not in script
    assert "dotenv" not in script
    assert ".env." not in script


def install_fake_db(monkeypatch, module) -> None:
    class FakeEngine:
        def dispose(self):
            return None

    class FakeSession:
        def __enter__(self):
            return object()

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeSessionFactory:
        def __call__(self):
            return FakeSession()

    monkeypatch.setattr(module, "create_engine", lambda *args, **kwargs: FakeEngine())
    monkeypatch.setattr(module, "sessionmaker", lambda *args, **kwargs: FakeSessionFactory())
