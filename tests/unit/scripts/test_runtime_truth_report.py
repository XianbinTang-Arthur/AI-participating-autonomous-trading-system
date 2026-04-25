from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "runtime_truth_report.py"


def load_module():
    spec = importlib.util.spec_from_file_location("runtime_truth_report", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_redact_secret_text_masks_urls_and_tokens() -> None:
    mod = load_module()
    raw = (
        "postgresql+psycopg://user:pass@host:5432/db "
        "redis://:secret@redis:6379/0 api_key=abc token:xyz password=hunter2"
    )

    redacted = mod.redact_secret_text(raw)

    assert "user:pass" not in redacted
    assert "secret@redis" not in redacted
    assert "abc" not in redacted
    assert "xyz" not in redacted
    assert "hunter2" not in redacted
    assert "<redacted" in redacted


def test_parse_git_divergence_and_status_header() -> None:
    mod = load_module()

    assert mod.parse_left_right_count("2\t3") == {"ahead": 2, "behind": 3}
    assert mod.parse_left_right_count("bad") == {"ahead": None, "behind": None}
    assert mod.parse_git_status_header("## main...origin/main [ahead 4, behind 1]") == {
        "branch": "main",
        "tracking": "origin/main",
        "ahead": 4,
        "behind": 1,
        "raw": "## main...origin/main [ahead 4, behind 1]",
    }


def test_container_health_requires_all_app_containers_healthy() -> None:
    mod = load_module()
    statuses = mod.parse_docker_ps(
        "\n".join(
            [
                "aats-gateway\tUp 1 minute (healthy)",
                "aats-market\tUp 1 minute (healthy)",
                "aats-decision\tUp 1 minute (healthy)",
                "aats-execution\tUp 1 minute (healthy)",
                "aats-rdp-daemon\tUp 1 minute (healthy)",
            ],
        ),
    )

    summary = mod.summarize_container_health(statuses)

    assert summary["all_required_app_containers_healthy"] is True
    assert summary["required"]["aats-gateway"]["healthy"] is True


def test_bash_cd_target_preserves_home_expansion() -> None:
    mod = load_module()

    assert mod.bash_cd_target("~/aats") == "$HOME/aats"
    assert mod.bash_cd_target("plain/path") == "plain/path"


def test_dashboard_auth_required_is_not_confused_with_runtime_mode() -> None:
    mod = load_module()
    payload = {
        "panels": {
            "mode": {"data": None, "error": "operator_auth_required"},
            "aiRuntime": {"data": None, "error": "operator_auth_required"},
        },
        "auth": {
            "access_state": "auth_required",
            "primary_error": "operator_auth_required",
            "blocked_panel_keys": ["mode", "aiRuntime"],
        },
    }

    summary = mod.summarize_dashboard_bundle(payload)

    assert summary["status"] == "auth_required"
    assert summary["effective_operating_mode"] == {
        "status": "unknown_auth_required",
        "value": None,
    }


def test_db_probe_command_does_not_embed_database_url() -> None:
    mod = load_module()

    command = " ".join(mod.db_probe_command("Ubuntu", "aats-gateway"))

    assert "postgresql://" not in command
    assert "postgresql+psycopg://" not in command
    assert "DATABASE_URL" not in command
    assert "AATS_DATABASE_URL" not in command


def test_parse_db_probe_returns_only_json_payload() -> None:
    mod = load_module()
    payload = {
        "ok": True,
        "portfolio_allocation_decisions": 32633,
        "execution_fills": 25,
        "latest_decision": {
            "decision_id": "decision_1",
            "symbol": "BTC-USDT-SWAP",
            "route_action": "advisory_only",
            "primary_family": "independent",
        },
    }

    parsed = mod.parse_db_probe(json.dumps(payload), "")

    assert parsed == payload


def test_blocking_findings_separate_report_generation_from_runtime_state() -> None:
    mod = load_module()
    report = {
        "git": {
            "windows": {
                "dirty": True,
                "origin_divergence": {"ahead": 0, "behind": 0},
            },
            "deployed_matches_windows": True,
        },
        "deployment_health": {
            "gateway_health": {"ok": True},
            "containers": {"all_required_app_containers_healthy": True},
        },
        "database_truth": {"ok": True},
    }

    assert mod.collect_blocking_findings(report) == ["windows_worktree_dirty"]
