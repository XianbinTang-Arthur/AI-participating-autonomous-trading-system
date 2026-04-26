from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_sync_to_wsl2_pull_tracks_current_source_head() -> None:
    text = (REPO_ROOT / "scripts" / "sync_to_wsl2.sh").read_text(encoding="utf-8")

    assert "git fetch '$WIN_PROJECT_WSL' main" not in text
    assert "git symbolic-ref --quiet --short HEAD" in text
    assert "git rev-parse HEAD" in text


def test_deploy_script_rejects_dirty_synced_deploys() -> None:
    text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")

    assert "当前同步机制只会部署已提交的 Git HEAD" in text
    assert "继续部署 WSL2 侧现有代码？[y/N]" in text


def test_deploy_script_health_check_covers_current_topology() -> None:
    text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")

    assert 'echo "aats-gateway aats-rdp-daemon"' in text
    assert 'echo "aats-gateway aats-market aats-decision aats-execution aats-rdp-daemon"' in text
    assert "all_required_app_containers_healthy" in text


def test_deploy_script_health_helpers_check_each_container_safely() -> None:
    text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")

    assert 'state="$(wsl_run "docker inspect --format' in text
    assert '[[ "$state" == "running healthy" ]] || return 1' in text
    assert "printf '%s %s\\n' \"$c\" \"$state\"" in text
    assert "printf '%s missing\\n' \"$c\"" in text


def test_deploy_script_health_check_logs_progress_and_gateway_state() -> None:
    text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")

    assert "gateway_health_ok()" in text
    assert "required_app_container_states_compact()" in text
    assert "gateway_state=\"未就绪\"" in text
    assert "gateway_state=\"已就绪\"" in text
    assert "健康检查进度" in text
    assert "容器=${container_states}" in text


def test_deploy_script_accepts_root_and_legacy_wsl2_env_file_locations() -> None:
    text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")

    assert 'test -f $WSL_PROJECT/.env.wsl2' in text
    assert 'test -f $WSL_PROJECT/$DEPLOY_DIR/.env.wsl2' in text


def test_deploy_script_provisions_tls_for_live_profiles_and_uses_https_health_checks() -> None:
    text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    compose_text = (REPO_ROOT / "deploy" / "wsl2-dev" / "docker-compose.aats.yml").read_text(encoding="utf-8")
    gitignore_text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")

    assert "is_live_profile()" in text
    assert "ensure_operator_tls_assets()" in text
    assert "AATS_OPERATOR_TLS_CERT_FILE" in text
    assert "AATS_OPERATOR_TLS_KEY_FILE" in text
    assert "curl -kfs https://127.0.0.1:$port/healthz" in text
    assert "runtime/operator-tls" in gitignore_text
    assert "AATS_OPERATOR_TLS_CERT_FILE" in compose_text
    assert "AATS_OPERATOR_TLS_KEY_FILE" in compose_text
    assert "runtime/operator-tls:/app/deploy/wsl2-dev/runtime/operator-tls:ro" in compose_text
    assert "curl -kfs https://localhost:${AATS_API_PORT:-8000}/healthz" in compose_text


def test_derivatives_live_overlay_enables_execution_command_flow() -> None:
    compose_text = (
        REPO_ROOT / "deploy" / "wsl2-dev" / "docker-compose.aats.derivatives-live.yml"
    ).read_text(encoding="utf-8")

    assert 'AATS_EXECUTION_COMMAND_FLOW_ENABLED: "true"' in compose_text
