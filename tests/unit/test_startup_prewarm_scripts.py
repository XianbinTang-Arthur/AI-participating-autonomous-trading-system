from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_prewarm_script_waits_for_docker_and_gateway_health() -> None:
    text = (REPO_ROOT / "scripts" / "prewarm_wsl2_aats.ps1").read_text(encoding="utf-8")

    assert "docker info >/dev/null 2>&1" in text
    assert "curl -fs 'http://127.0.0.1:$Port/healthz'" in text
    assert "Test-RequiredContainersHealthy" in text


def test_prewarm_script_picks_https_for_live_profiles() -> None:
    text = (REPO_ROOT / "scripts" / "prewarm_wsl2_aats.ps1").read_text(encoding="utf-8")

    assert "function Get-HealthScheme" in text
    assert "'spot-live' { return 'https' }" in text
    assert "'derivatives-live' { return 'https' }" in text
    assert "'derivatives-live-monolith' { return 'https' }" in text
    assert "curl -kfs 'https://127.0.0.1:$Port/healthz'" in text


def test_prewarm_script_repairs_only_via_standard_wrapper() -> None:
    text = (REPO_ROOT / "scripts" / "prewarm_wsl2_aats.ps1").read_text(encoding="utf-8")

    assert ".codex\\skills\\wsl2-deploy\\scripts\\run-deploy.ps1" in text
    assert "'-SkipSync'" in text
    assert "'-SkipCommit'" in text
    assert "'-AssumeYes'" in text
    assert "triggering repair deploy via standard wrapper" in text


def test_run_deploy_wrapper_supports_assume_yes() -> None:
    text = (REPO_ROOT / ".codex" / "skills" / "wsl2-deploy" / "scripts" / "run-deploy.ps1").read_text(
        encoding="utf-8"
    )

    assert "[switch]$AssumeYes" in text
    assert "$deployArgs += '--yes'" in text


def test_deploy_sh_supports_non_interactive_skip_sync() -> None:
    text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")

    assert "--yes|-y)" in text
    assert "ASSUME_YES=true" in text
    assert '[[ "$ASSUME_YES" == true ]]' in text
    assert "[[ -t 0 ]]" in text
    assert "exit 4" in text


def test_prewarm_script_ensures_keepalive_before_health_checks() -> None:
    text = (REPO_ROOT / "scripts" / "prewarm_wsl2_aats.ps1").read_text(encoding="utf-8")

    assert "keepalive_wsl2_aats.ps1" in text
    assert "'-Action'" in text
    assert "'Start'" in text
    assert "ensuring WSL keepalive is running" in text


def test_prewarm_script_keeps_current_topology_mapping() -> None:
    text = (REPO_ROOT / "scripts" / "prewarm_wsl2_aats.ps1").read_text(encoding="utf-8")

    assert "@('aats-gateway', 'aats-rdp-daemon', 'aats-liquidations-daemon', 'aats-microstructure-collector')" in text
    assert "@('aats-gateway', 'aats-market', 'aats-decision', 'aats-execution', 'aats-rdp-daemon')" in text
    assert "@('aats-gateway', 'aats-market', 'aats-decision', 'aats-execution', 'aats-rdp-daemon', 'aats-liquidations-daemon', 'aats-microstructure-collector')" in text


def test_register_script_creates_logon_task_and_supports_remove() -> None:
    text = (REPO_ROOT / "scripts" / "register_wsl2_aats_startup_task.ps1").read_text(encoding="utf-8")

    assert "New-ScheduledTaskTrigger -AtLogOn" in text
    assert "Register-ScheduledTask" in text
    assert "Unregister-ScheduledTask" in text
    assert "AATS-WSL2-Prewarm-" in text


def test_register_script_uses_hidden_powershell_action_for_prewarm() -> None:
    text = (REPO_ROOT / "scripts" / "register_wsl2_aats_startup_task.ps1").read_text(encoding="utf-8")

    assert "prewarm_wsl2_aats.ps1" in text
    assert "-WindowStyle Hidden" in text
    assert "powershell.exe" in text
    assert "keepalive + prewarm" in text


def test_keepalive_script_starts_hidden_wsl_loop_and_tracks_state() -> None:
    text = (REPO_ROOT / "scripts" / "keepalive_wsl2_aats.ps1").read_text(encoding="utf-8")

    assert "Start-Process -WindowStyle Hidden -FilePath 'wsl.exe'" in text
    assert "while true; do sleep 3600; done" in text
    assert "Get-CimInstance Win32_Process" in text
    assert "ConvertTo-Json" in text
    assert "AATS_WSL_KEEPALIVE_" in text
