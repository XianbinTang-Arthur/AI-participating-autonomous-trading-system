from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_standard_deploy_binds_local_daemon_after_sync_before_build() -> None:
    text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    main = text.split("main() {", 1)[1]

    sync = main.index('run_locked_step "代码同步" step_sync')
    binding = main.index(
        'run_locked_step "Docker daemon 绑定" '
        "establish_local_docker_daemon_binding"
    )
    build = main.index('run_locked_step "镜像构建" step_build')

    assert sync < binding < build
    assert "scripts/docker_event_monitor.py daemon-binding" in text
    assert "daemon-binding-envelope" in text
    assert "tr -d '\\000\\r'" in text
    assert "sha256sum" in text
    assert "base64 --decode" in text


def test_every_standard_wsl_child_is_pinned_to_local_socket() -> None:
    text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    wrapper = text.split("wsl_run() {", 1)[1].split("wsl_root_run() {", 1)[0]
    root_wrapper = text.split("wsl_root_run() {", 1)[1].split(
        "release_deploy_lock() {",
        1,
    )[0]
    expected = (
        "export DOCKER_HOST='unix:///var/run/docker.sock'; "
        "unset DOCKER_CONTEXT;"
    )

    assert expected in wrapper
    assert expected in root_wrapper
    assert 'bash -c "$local_docker_command"' in wrapper
    assert 'bash -c "$local_docker_command"' in root_wrapper


def test_locked_steps_recheck_bound_daemon_before_and_after() -> None:
    text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    body = text.split("run_locked_step() {", 1)[1].split(
        "repo_has_uncommitted_changes() {",
        1,
    )[0]

    assert body.count("assert_local_docker_daemon_binding") == 2
    assert body.index('assert_local_docker_daemon_binding "$step_name 前"') < body.index(
        '"$@"'
    )
    assert body.index('assert_local_docker_daemon_binding "$step_name 后"') > body.index(
        '"$@"'
    )
