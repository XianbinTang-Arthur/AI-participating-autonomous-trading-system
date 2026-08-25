from __future__ import annotations

import importlib.util
import json
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SCRIPT = REPO_ROOT / "scripts" / "deploy.sh"
EVIDENCE_SCRIPT = REPO_ROOT / "scripts" / "write_deployment_evidence.py"
READINESS_GENERATION = "aaaaaaaaaaaa-20260824T000000Z-123-456"


def test_deployment_evidence_supports_wsl_system_python_310() -> None:
    source = EVIDENCE_SCRIPT.read_text(encoding="utf-8")

    assert "from datetime import UTC" not in source
    assert "timezone.utc" in source


def _load_evidence_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("write_deployment_evidence", EVIDENCE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_deploy_gate(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "scripts/deploy.sh", *args],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_deploy_requires_explicit_profile_before_any_wsl_call() -> None:
    result = _run_deploy_gate()

    assert result.returncode == 2
    assert "必须显式指定 --profile" in result.stderr
    assert "找不到 wsl 命令" not in result.stderr


@pytest.mark.parametrize(
    "profile",
    ["spot-live", "derivatives-live", "derivatives-live-monolith"],
)
def test_deploy_rejects_every_live_profile_and_yes_cannot_override(profile: str) -> None:
    result = _run_deploy_gate("--profile", profile, "--yes")

    assert result.returncode == 5
    assert "REAL-MONEY PRODUCTION: NO-GO" in result.stderr
    assert "找不到 wsl 命令" not in result.stderr


def test_deploy_order_and_failure_posture_are_fail_closed() -> None:
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert source.index("require_explicit_non_live_profile") < source.index("resolve_profile \"$PROFILE\"")
    assert source.index("step_build\n") < source.index("step_down\n")
    assert source.index("step_schema_migrate\n") < source.index("step_app_up\n")
    assert "应用 docker compose up 返回非零；继续" not in source
    assert "docker compose $COMPOSE_CMD_ARGS down --timeout 5\" ||" not in source
    assert "模拟栈基础检查通过（不是 trading-ready 或生产放行）" in source


def test_future_derivatives_live_topology_includes_live_only_collectors() -> None:
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert (
        'echo "aats-gateway aats-market aats-decision aats-execution aats-rdp-daemon '
        'aats-liquidations-daemon aats-microstructure-collector"'
    ) in source


def test_entrypoint_wrappers_default_to_simulation_and_reject_live() -> None:
    paths = [
        REPO_ROOT / "scripts" / "keepalive_wsl2_aats.ps1",
        REPO_ROOT / "scripts" / "prewarm_wsl2_aats.ps1",
        REPO_ROOT / "scripts" / "register_wsl2_aats_startup_task.ps1",
        REPO_ROOT / ".codex" / "skills" / "wsl2-deploy" / "scripts" / "run-deploy.ps1",
    ]
    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert "[string]$Profile = 'derivatives'" in source
        assert "REAL-MONEY PRODUCTION is NO-GO" in source


def test_lifecycle_helpers_keep_legacy_stop_and_remove_paths_available() -> None:
    keepalive = (REPO_ROOT / "scripts" / "keepalive_wsl2_aats.ps1").read_text(encoding="utf-8")
    startup_task = (REPO_ROOT / "scripts" / "register_wsl2_aats_startup_task.ps1").read_text(
        encoding="utf-8"
    )

    assert "$Action -eq 'Start'" in keepalive
    assert "Stop and Status remain available for legacy cleanup" in keepalive
    assert "-not $Remove" in startup_task


def test_evidence_packet_contains_only_simulation_identity_and_explicit_unknowns() -> None:
    module = _load_evidence_module()
    commit = "a" * 40
    image_id = "sha256:" + "b" * 64

    def fake_run(args: tuple[str, ...], _cwd: Path | None = None) -> str:
        if args[:3] == ("git", "rev-parse", "HEAD"):
            return commit
        if args[:4] == ("docker", "image", "inspect", "aats-base:dev"):
            return image_id
        if "{{.State.Status}}" in args:
            return "running"
        if "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}" in args:
            return "healthy"
        if "{{.Image}}" in args:
            return image_id
        if "{{json .NetworkSettings.Ports}}" in args:
            return json.dumps({"8001/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8001"}]})
        raise AssertionError(args)

    payload = module.build_evidence(
        repo_root=REPO_ROOT,
        profile="derivatives",
        overlay="docker-compose.aats.derivatives.yml",
        schema_job_status="passed",
        runtime_readiness_generation=READINESS_GENERATION,
        required_containers=("aats-gateway", "aats-execution"),
        run=fake_run,
        generated_at=datetime(2026, 8, 24, tzinfo=UTC),
    )

    assert payload["status"] == "simulation_stack_healthy"
    assert payload["production_ready"] is False
    assert payload["trading_ready"] is False
    assert payload["deployed_commit"] == commit
    assert payload["base_image_id"] == image_id
    assert payload["runtime_readiness_generation"] == READINESS_GENERATION
    assert payload["schema_contract"] == {
        "job_status": "passed",
        "clone_manifest_verified": False,
        "consistent_rollback_verified": False,
    }
    assert len(payload["required_containers"]) == 2
    assert payload["gateway_published_bindings"] == [
        {"container_port": "8001/tcp", "host_ip": "127.0.0.1", "host_port": "8001"}
    ]
    encoded = json.dumps(payload).lower()
    for forbidden in ("password", "api_key", "token", "database_url", "dsn"):
        assert forbidden not in encoded


def test_evidence_writer_refuses_overwrite(tmp_path: Path) -> None:
    module = _load_evidence_module()
    payload = {
        "generated_at": "2026-08-24T00:00:00+00:00",
        "deployed_commit": "c" * 40,
        "profile": "derivatives",
    }

    target = module.write_evidence(repo_root=tmp_path, payload=payload)
    try:
        assert stat.S_IMODE(target.stat().st_mode) & stat.S_IWUSR == 0
        with pytest.raises(FileExistsError):
            module.write_evidence(repo_root=tmp_path, payload=payload)
    finally:
        target.chmod(0o644)


def test_evidence_builder_rejects_live_or_unhealthy_container() -> None:
    module = _load_evidence_module()
    with pytest.raises(ValueError, match="simulation_profile"):
        module.build_evidence(
            repo_root=REPO_ROOT,
            profile="derivatives-live",
            overlay="live.yml",
            schema_job_status="passed",
            runtime_readiness_generation=READINESS_GENERATION,
            required_containers=("aats-gateway",),
        )

    commit = "d" * 40
    image_id = "sha256:" + "e" * 64

    def unhealthy_run(args: tuple[str, ...], _cwd: Path | None = None) -> str:
        if args[:3] == ("git", "rev-parse", "HEAD"):
            return commit
        if args[:4] == ("docker", "image", "inspect", "aats-base:dev"):
            return image_id
        if "{{.State.Status}}" in args:
            return "running"
        if "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}" in args:
            return "unhealthy"
        if "{{.Image}}" in args:
            return image_id
        raise AssertionError(args)

    with pytest.raises(RuntimeError, match="required_container_not_healthy"):
        module.build_evidence(
            repo_root=REPO_ROOT,
            profile="derivatives",
            overlay="docker-compose.aats.derivatives.yml",
            schema_job_status="passed",
            runtime_readiness_generation=READINESS_GENERATION,
            required_containers=("aats-gateway",),
            run=unhealthy_run,
        )


@pytest.mark.parametrize("generation", ["", "bad generation", "bad/generation", "x" * 129])
def test_evidence_builder_rejects_invalid_runtime_readiness_generation(generation: str) -> None:
    module = _load_evidence_module()

    with pytest.raises(ValueError, match="invalid_runtime_readiness_generation"):
        module.build_evidence(
            repo_root=REPO_ROOT,
            profile="derivatives",
            overlay="docker-compose.aats.derivatives.yml",
            schema_job_status="passed",
            runtime_readiness_generation=generation,
            required_containers=("aats-gateway",),
        )
