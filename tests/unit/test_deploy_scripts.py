from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_sync_to_wsl2_pull_tracks_current_source_head() -> None:
    text = (REPO_ROOT / "scripts" / "sync_to_wsl2.sh").read_text(encoding="utf-8")

    assert "git fetch '$WIN_PROJECT_WSL' main" not in text
    assert "git symbolic-ref --quiet --short HEAD" in text
    assert "git rev-parse HEAD" in text


def test_sync_to_wsl2_branch_drift_repair_does_not_swallow_checkout_failures() -> None:
    text = (REPO_ROOT / "scripts" / "sync_to_wsl2.sh").read_text(encoding="utf-8")

    assert "checkout -b '$source_branch' FETCH_HEAD 2>/dev/null || true" not in text
    assert "git -C $WSL_PROJECT fetch '$WIN_PROJECT_WSL' '$source_branch'" in text


def test_deploy_script_rejects_dirty_synced_deploys() -> None:
    text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")

    assert "当前同步机制只会部署已提交的 Git HEAD" in text
    assert "继续部署 WSL2 侧现有代码？[y/N]" in text


def test_deploy_script_commit_only_uses_precisely_staged_files() -> None:
    text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")

    assert "\n            git add -A" not in text
    assert "--commit 只提交已精确暂存的文件" in text
    assert "repo_has_staged_changes()" in text
    assert "repo_has_unstaged_or_untracked_changes()" in text


def test_runtime_evidence_directories_are_gitignored() -> None:
    text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")

    assert "deploy/wsl2-dev/runtime/deployment-evidence/" in text
    assert "deploy/wsl2-dev/runtime/execution-funnel-evidence/" in text


def test_deploy_script_health_check_covers_current_topology() -> None:
    text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")

    assert 'echo "aats-gateway aats-rdp-daemon aats-liquidations-daemon aats-microstructure-collector"' in text
    assert 'echo "aats-gateway aats-market aats-decision aats-execution aats-rdp-daemon"' in text
    assert 'echo "aats-gateway aats-market aats-decision aats-execution aats-rdp-daemon aats-liquidations-daemon aats-microstructure-collector"' in text
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


def test_deploy_script_compose_failures_are_not_swallowed() -> None:
    text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")

    assert "基础设施 docker compose up 返回非零；继续检查实际容器状态" not in text
    assert "应用 docker compose up 返回非零；继续进入健康检查确认实际容器状态" not in text
    assert "docker compose $COMPOSE_CMD_ARGS down --timeout 5\" ||" not in text
    assert "up -d --wait --wait-timeout 90" in text
    assert "应用服务启动命令已返回，等待健康检查确认" in text
    assert "step_app_up" in text
    assert "step_health" in text


def test_deploy_script_accepts_root_and_legacy_wsl2_env_file_locations() -> None:
    text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")

    assert 'test -f $WSL_PROJECT/.env.wsl2' in text
    assert 'test -f $WSL_PROJECT/$DEPLOY_DIR/.env.wsl2' in text


def test_deploy_script_reports_actual_wsl_deployed_head() -> None:
    text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")

    assert "Windows HEAD:" in text
    assert "WSL HEAD:" in text
    assert "实际部署版本" in text
    assert "git -C $WSL_PROJECT rev-parse HEAD" in text


def test_deploy_script_syncs_postgres_password_with_psql_variables() -> None:
    text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")

    assert "-v pg_user=" in text
    assert "-v pg_password=" in text
    assert 'ALTER USER :"pg_user" PASSWORD :\'pg_password\';' in text


def test_postgres_probes_use_an_existing_database_and_redis_host_is_prepared() -> None:
    deploy_text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    compose_text = (
        REPO_ROOT / "deploy" / "wsl2-dev" / "docker-compose.yml"
    ).read_text(encoding="utf-8")

    assert "docker exec aats-postgres sh -lc 'pg_isready" not in deploy_text
    assert "up -d --wait --wait-timeout 90" in deploy_text
    assert 'pg_isready -U \\"$$POSTGRES_USER\\" -d \\"$$POSTGRES_DB\\"' in compose_text
    assert "ensure_wsl_runtime_prerequisites" in deploy_text
    assert "vm.overcommit_memory=1" in deploy_text
    assert 'wsl -d "$DISTRO" -u root bash -c' in deploy_text


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


def test_deploy_runbook_no_longer_points_to_stale_sync_or_bootstrap_paths() -> None:
    runbook = (REPO_ROOT / "deploy" / "wsl2-dev" / "RUNBOOK.md").read_text(encoding="utf-8")
    readme = (REPO_ROOT / "deploy" / "wsl2-dev" / "README.md").read_text(encoding="utf-8")
    sync_workflow = (REPO_ROOT / "docs" / "operations" / "wsl2_sync_workflow.md").read_text(
        encoding="utf-8"
    )

    assert "单独 rsync 到 `~/aats-deploy/`" not in runbook
    assert "python3 -m aats.scripts.bootstrap_database" not in runbook
    assert "python3 -m aats.api.main" not in runbook
    assert "docker compose --env-file deploy/wsl2-dev/.env.wsl2" not in runbook
    assert "docker compose down -v" not in readme
    assert "envs/.env.wsl2-dev" not in readme
    assert "docker compose --env-file .env.wsl2 up -d" not in sync_workflow
    assert "bash scripts/deploy.sh --profile derivatives --skip-commit" in sync_workflow
