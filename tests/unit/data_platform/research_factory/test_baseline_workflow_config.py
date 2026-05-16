import json
from pathlib import Path
from typing import Any

from aats.data_platform.research_factory.specs import (
    ALLOWED_WORKFLOW_STAGES,
    FORBIDDEN_WORKFLOW_OUTPUT_TERMS,
    METRIC_FIELDS,
)


def test_baseline_workflow_config_is_research_only_and_parseable() -> None:
    config_path = Path("configs/research_factory/baseline_workflow.json")
    config = json.loads(config_path.read_text(encoding="utf-8"))

    assert config["workflow_id"] == "research_factory_baseline_v1"
    assert config["governance_mode"] == "candidate_only"
    assert config["artifact_root"] == "artifacts/research/research_factory"
    assert config["automation_contract"]["external_trigger_only"] is True
    assert config["automation_contract"]["no_internal_scheduler"] is True
    assert config["automation_contract"]["no_runtime_effect"] is True
    assert config["hard_boundaries"]["read_env_files"] is False
    assert config["hard_boundaries"]["modify_live_execution"] is False
    assert config["hard_boundaries"]["call_okx_write_apis"] is False
    assert config["hard_boundaries"]["deploy"] is False
    assert config["hard_boundaries"]["introduce_qlib_or_rdagent_runtime_dependency"] is False

    stage_names = [stage["name"] for stage in config["stages"]]
    assert stage_names == ["dataset", "feature", "experiment", "benchmark", "governance", "sandbox"]
    assert set(stage_names).issubset(ALLOWED_WORKFLOW_STAGES)
    assert config["stages"][4]["action"] == "candidate_gate"

    required_metrics = set(config["required_metrics"])
    assert required_metrics.issubset(set(METRIC_FIELDS))
    for metric_name in (
        "net_annualized_return",
        "max_drawdown",
        "cost_adjusted_edge_bps_mean",
        "fillable_ratio",
    ):
        assert metric_name in required_metrics

    outputs = [
        output
        for stage in config["stages"]
        for output in stage.get("outputs", [])
    ]
    lowered_outputs = " ".join(outputs).lower()
    for forbidden in FORBIDDEN_WORKFLOW_OUTPUT_TERMS:
        assert forbidden not in lowered_outputs

    assert not _contains_forbidden_runtime_dependency(config)


def _contains_forbidden_runtime_dependency(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_contains_forbidden_runtime_dependency(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_forbidden_runtime_dependency(item) for item in value)
    if isinstance(value, str):
        lowered = value.lower()
        return "qlib" in lowered or "rdagent" in lowered
    return False
