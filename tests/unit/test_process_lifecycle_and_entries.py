"""Stage 5 单元测试：4 进程拓扑装配 (子任务 5d)。

本组测试覆盖：

1. aats.bootstrap.process_lifecycle.run_process 的契约：
   * 必须显式接受合法 process_role，None / 非法值要早抛
   * 一次完整的「build → start → wait stop_event → stop」生命周期
   * 异常路径下也必须走 stop_background_tasks（finally 语义）

2. apps/{market_gateway,decision_engine,execution_engine,api_gateway}/main.py
   入口必须存在、必须显式声明各自的 process_role 常量。

3. deploy/wsl2-dev/Dockerfile 与 docker-compose.aats.yml 的关键字段：
   * Dockerfile 必须基于 python 3.12 + 安装 .[nats] + tini PID 1
   * compose 必须有 4 个服务、共享同一个 image、各自 AATS_PROCESS_ROLE 不同

这些测试都不依赖 docker / Postgres / NATS，纯静态/in-memory 检查。
"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from aats.bootstrap import process_lifecycle
from aats.bootstrap.process_lifecycle import (
    HEARTBEAT_DIR_ENV,
    _heartbeat_loop,
    _heartbeat_path,
    _resolve_process_role,
    run_process,
)
from aats.bootstrap.settings import (
    PROCESS_ROLE_DECISION,
    PROCESS_ROLE_EXECUTION,
    PROCESS_ROLE_GATEWAY,
    PROCESS_ROLE_MARKET,
    PROCESS_ROLE_MONOLITH,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


# ─────────────────────────────────────────────────────────────────────
# 1) _resolve_process_role：合法集合校验
# ─────────────────────────────────────────────────────────────────────


def test_resolve_process_role_accepts_all_four_slice_roles_and_monolith() -> None:
    for role in (
        PROCESS_ROLE_GATEWAY,
        PROCESS_ROLE_MARKET,
        PROCESS_ROLE_DECISION,
        PROCESS_ROLE_EXECUTION,
        PROCESS_ROLE_MONOLITH,
    ):
        assert _resolve_process_role(requested=role) == role


def test_resolve_process_role_rejects_none() -> None:
    with pytest.raises(ValueError, match="必须显式传入 process_role"):
        _resolve_process_role(requested=None)


def test_resolve_process_role_rejects_unknown_value() -> None:
    with pytest.raises(ValueError, match="不在合法集合"):
        _resolve_process_role(requested="not_a_real_role")


# ─────────────────────────────────────────────────────────────────────
# 2) run_process 生命周期：build → start → wait → stop
# ─────────────────────────────────────────────────────────────────────


class _FakeRuntime:
    """最小 runtime stub，记录调用顺序便于断言。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.background_tasks: list = []

    async def start_background_tasks(self) -> None:
        self.calls.append("start")

    async def stop_background_tasks(self) -> None:
        self.calls.append("stop")


def test_run_process_happy_path_calls_start_then_waits_then_stops() -> None:
    """正常路径：build_runtime → start → 等 stop_event → stop，顺序与次数都要对。"""
    fake_runtime = _FakeRuntime()
    fake_settings = SimpleNamespace()  # configure_logging_for_settings 只读极少字段

    async def _fake_build(settings, *, process_role):
        fake_runtime.calls.append(f"build:{process_role}")
        return fake_runtime

    async def _run() -> int:
        stop_event = asyncio.Event()
        stop_event.set()  # 立刻 set，让 wait() 立即返回
        with patch.object(process_lifecycle, "build_runtime", side_effect=_fake_build), patch.object(
            process_lifecycle, "configure_logging_for_settings"
        ), patch.object(process_lifecycle, "load_settings", return_value=fake_settings):
            return await run_process(
                process_role=PROCESS_ROLE_MARKET,
                app_name="test.market",
                settings=fake_settings,
                stop_event=stop_event,
            )

    rc = asyncio.run(_run())
    assert rc == 0
    assert fake_runtime.calls == [f"build:{PROCESS_ROLE_MARKET}", "start", "stop"]


def test_run_process_invokes_extra_setup_hook_after_start() -> None:
    """extra_setup 必须在 start_background_tasks 之后、stop 之前被调用。"""
    fake_runtime = _FakeRuntime()
    extra_calls: list[str] = []

    async def _fake_build(settings, *, process_role):
        return fake_runtime

    async def _extra_setup(runtime) -> None:
        # extra_setup 看到的 runtime 必须是同一个 object
        assert runtime is fake_runtime
        extra_calls.append("extra")

    async def _run() -> int:
        stop_event = asyncio.Event()
        stop_event.set()
        with patch.object(process_lifecycle, "build_runtime", side_effect=_fake_build), patch.object(
            process_lifecycle, "configure_logging_for_settings"
        ):
            return await run_process(
                process_role=PROCESS_ROLE_GATEWAY,
                app_name="test.gateway",
                settings=SimpleNamespace(),
                stop_event=stop_event,
                extra_setup=_extra_setup,
            )

    rc = asyncio.run(_run())
    assert rc == 0
    assert extra_calls == ["extra"]
    # extra 必须排在 start 之后
    assert fake_runtime.calls.index("start") < fake_runtime.calls.index("stop")


def test_run_process_runs_stop_even_when_extra_setup_raises() -> None:
    """关键回归守卫：extra_setup 抛异常时，stop_background_tasks 仍然必须跑。

    这是 finally 语义的核心 — 如果坏掉的话会导致 NATS 连接、DB pool、
    OKX private WS 这些重资源在崩溃路径下泄露。
    """
    fake_runtime = _FakeRuntime()

    async def _fake_build(settings, *, process_role):
        return fake_runtime

    async def _broken_extra_setup(_runtime) -> None:
        raise RuntimeError("simulated boot failure")

    async def _run() -> int:
        stop_event = asyncio.Event()
        stop_event.set()
        with patch.object(process_lifecycle, "build_runtime", side_effect=_fake_build), patch.object(
            process_lifecycle, "configure_logging_for_settings"
        ):
            return await run_process(
                process_role=PROCESS_ROLE_DECISION,
                app_name="test.decision",
                settings=SimpleNamespace(),
                stop_event=stop_event,
                extra_setup=_broken_extra_setup,
            )

    rc = asyncio.run(_run())
    # 异常被 run_process 内部 catch，返回 1（让 sys.exit(rc) 写入正确退出码）
    assert rc == 1
    # 关键：stop 必须出现在 calls 里
    assert "stop" in fake_runtime.calls, "extra_setup 抛错时 stop_background_tasks 也必须跑"


def test_run_process_returns_one_when_build_runtime_raises() -> None:
    """build_runtime 自身抛错时，runtime 还没创建，stop 不应当被调用，但退出码必须是 1。"""

    async def _broken_build(settings, *, process_role):
        raise RuntimeError("DB connection refused")

    async def _run() -> int:
        with patch.object(process_lifecycle, "build_runtime", side_effect=_broken_build), patch.object(
            process_lifecycle, "configure_logging_for_settings"
        ):
            return await run_process(
                process_role=PROCESS_ROLE_EXECUTION,
                app_name="test.execution",
                settings=SimpleNamespace(),
                stop_event=asyncio.Event(),
            )

    rc = asyncio.run(_run())
    assert rc == 1


def test_run_process_rejects_invalid_role_before_touching_runtime() -> None:
    """非法 role 要在还没 load_settings 之前抛掉，避免污染日志/连接资源。"""

    async def _run() -> None:
        with patch.object(process_lifecycle, "build_runtime") as build_mock, patch.object(
            process_lifecycle, "configure_logging_for_settings"
        ):
            with pytest.raises(ValueError):
                await run_process(
                    process_role="invalid_role",
                    app_name="test.invalid",
                    settings=SimpleNamespace(),
                    stop_event=asyncio.Event(),
                )
            build_mock.assert_not_called()

    asyncio.run(_run())


# ─────────────────────────────────────────────────────────────────────
# 3) 4 个 entry script：必须存在并显式声明 process_role
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("module_path", "expected_role_constant"),
    [
        ("apps.market_gateway.main", "PROCESS_ROLE_MARKET"),
        ("apps.decision_engine.main", "PROCESS_ROLE_DECISION"),
        ("apps.execution_engine.main", "PROCESS_ROLE_EXECUTION"),
    ],
)
def test_slice_entry_modules_use_lifecycle_helper_with_correct_role(
    module_path: str, expected_role_constant: str
) -> None:
    """3 个 daemon entry 必须 import process_lifecycle helper 并使用对应 role。

    走静态源码检查：避免在 unit test 里真的去 build_runtime（那是集成测试的工作）。
    """
    import importlib

    module = importlib.import_module(module_path)
    source = inspect.getsource(module)

    assert "run_process_sync" in source, (
        f"{module_path} 必须使用 process_lifecycle.run_process_sync helper，"
        f"不应当再有手写的 build_runtime + asyncio.run boilerplate"
    )
    assert expected_role_constant in source, (
        f"{module_path} 必须显式 import {expected_role_constant} 并传给 run_process_sync"
    )
    # 必须有 if __name__ == "__main__" 守卫，否则 docker CMD 拿不到退出码
    assert '__name__ == "__main__"' in source
    assert "sys.exit" in source


def test_api_gateway_entry_passes_explicit_process_role_to_build_runtime() -> None:
    """gateway entry 仍然走 FastAPI lifespan，但必须显式传 process_role 给 build_runtime。

    历史背景：旧版本 lifespan 调 build_runtime(settings) 不传 role，会落到 monolith
    fallback。Stage 5d 之后必须显式 gateway，否则 4 进程拓扑下 gateway 会去抢
    decision/execution 的 advisory lock。
    """
    from apps.api_gateway import main as gateway_main

    source = inspect.getsource(gateway_main)
    assert "process_role=" in source, (
        "api_gateway lifespan 必须显式给 build_runtime 传 process_role 参数"
    )
    assert "PROCESS_ROLE_GATEWAY" in source
    assert "AATS_PROCESS_ROLE" in source, (
        "必须从 AATS_PROCESS_ROLE 读取 role 以便 docker compose 注入"
    )


# ─────────────────────────────────────────────────────────────────────
# 4) Dockerfile + 4 服务 compose：关键字段
# ─────────────────────────────────────────────────────────────────────


def test_dockerfile_uses_python_312_and_installs_nats_extra_with_tini() -> None:
    dockerfile = REPO_ROOT / "deploy" / "wsl2-dev" / "Dockerfile"
    assert dockerfile.exists(), f"缺失 {dockerfile}"
    text = dockerfile.read_text(encoding="utf-8")

    # 与 WSL2 venv 一致的 3.12 base image，避免 3.13/3.14 周边轮子缺口
    assert "python:3.12-slim" in text
    # 4 进程拓扑必须装 nats extra
    assert '.[nats]' in text or '.[nats]"' in text
    # tini 作为 PID 1 才能正确传 SIGTERM
    assert "tini" in text
    assert 'ENTRYPOINT ["/usr/bin/tini"' in text
    # 非 root 用户
    assert "USER aats" in text


def test_aats_compose_defines_four_slice_services_with_distinct_process_roles() -> None:
    """compose 必须包含 4 个服务，每个 AATS_PROCESS_ROLE 不同，且共享同一份镜像。"""
    compose_path = REPO_ROOT / "deploy" / "wsl2-dev" / "docker-compose.aats.yml"
    assert compose_path.exists(), f"缺失 {compose_path}"
    text = compose_path.read_text(encoding="utf-8")

    # 4 个 service block 必须存在
    for service_name in ("aats-gateway", "aats-market", "aats-decision", "aats-execution"):
        assert f"{service_name}:" in text, f"compose 缺少 service {service_name}"

    # 4 种 role 都要在文件里出现
    for role in ("gateway", "market", "decision", "execution"):
        assert f"AATS_PROCESS_ROLE: {role}" in text, (
            f"compose 缺少 AATS_PROCESS_ROLE: {role}"
        )

    # 共享镜像 aats-base:dev（避免 4 个服务各自重复构建）
    assert "image: aats-base:dev" in text
    # gateway 必须暴露端口（通过 AATS_API_PORT 变量，默认 8000），其他不暴露
    assert "${AATS_API_PORT:-8000}" in text
    assert '"${AATS_API_PORT:-8000}:${AATS_API_PORT:-8000}"' in text, (
        "gateway 端口映射必须显式暴露 AATS_API_PORT，便于 WSL 独立 dockerd 被 Windows 宿主机访问"
    )
    # event bus 必须切到 hybrid（4 进程拓扑跨进程通信）
    assert "AATS_EVENT_BUS_BACKEND: hybrid" in text
    # 必须复用基础设施 compose 的 aats network
    assert "external: true" in text
    # Stage 7 修复：gateway 必须用 /healthz（不是 /system/health）
    # 精确匹配 healthcheck test 行 URL，避免误伤注释
    assert "/healthz" in text
    assert "http://localhost:${AATS_API_PORT:-8000}/system/health" not in text, (
        "gateway healthcheck 应当用 /healthz 而非 /system/health (后者在 gateway role 下会 NPE)"
    )
    # Stage 7 修复：3 个 daemon 必须有 heartbeat 文件 healthcheck
    for role in ("market", "decision", "execution"):
        assert f"/tmp/aats_{role}_heartbeat" in text, (
            f"daemon {role} 缺少心跳文件 healthcheck"
        )


def test_dockerfile_runtime_installs_curl_for_gateway_healthcheck() -> None:
    """Stage 7 回归：runtime 段必须装 curl，否则 gateway 容器 healthcheck 找不到 curl 永远 unhealthy。"""
    dockerfile = REPO_ROOT / "deploy" / "wsl2-dev" / "Dockerfile"
    text = dockerfile.read_text(encoding="utf-8")

    # 找到 runtime stage 之后的 RUN apt-get install 段
    runtime_marker = "FROM python:3.12-slim AS runtime"
    runtime_idx = text.find(runtime_marker)
    assert runtime_idx >= 0, "Dockerfile 必须有 runtime stage"
    runtime_section = text[runtime_idx:]
    # runtime 段的第一个 apt-get install 必须包含 curl
    install_idx = runtime_section.find("apt-get install")
    assert install_idx >= 0, "runtime stage 必须有 apt-get install"
    # 找到这条 install 命令的结束（rm -rf 之前）
    install_end = runtime_section.find("rm -rf", install_idx)
    install_block = runtime_section[install_idx:install_end]
    assert "curl" in install_block, (
        "Dockerfile runtime 段必须装 curl，否则 gateway healthcheck 命令找不到 curl"
    )


def test_dockerfile_runtime_copies_root_migrations_for_live_startup() -> None:
    """Golden path 回归：runtime image 必须带上 /app/migrations，否则 apply_current_migrations 只会对空目录工作。"""
    dockerfile = REPO_ROOT / "deploy" / "wsl2-dev" / "Dockerfile"
    text = dockerfile.read_text(encoding="utf-8")

    assert "COPY --chown=aats:aats migrations/ ./migrations/" in text, (
        "Dockerfile runtime 段必须复制根 migrations/ 目录，否则 live 启动时 "
        "apply_current_migrations() 看不到任何 SQL migration，schema_migrations 会保持空表"
    )


# ─────────────────────────────────────────────────────────────────────
# 5) Stage 7 心跳：daemon 进程的 docker healthcheck 信号源
# ─────────────────────────────────────────────────────────────────────


def test_heartbeat_path_uses_role_in_filename(tmp_path) -> None:
    """每个 role 的心跳文件名必须包含 role，避免 4 个进程互相覆盖。"""
    for role in ("market", "decision", "execution", "gateway"):
        path = _heartbeat_path(role, base_dir=tmp_path)
        assert path.name == f"aats_{role}_heartbeat"
        assert path.parent == tmp_path


def test_heartbeat_path_respects_env_dir_override(monkeypatch, tmp_path) -> None:
    """AATS_HEARTBEAT_DIR 必须能覆盖默认 /tmp，便于单测和某些受限容器环境。"""
    monkeypatch.setenv(HEARTBEAT_DIR_ENV, str(tmp_path))
    path = _heartbeat_path("market")
    assert path.parent == tmp_path
    assert path.name == "aats_market_heartbeat"


def test_heartbeat_loop_touches_file_then_stops_on_event(tmp_path) -> None:
    """心跳 loop 必须周期性 touch 文件，stop_event set 后立即退出。"""

    async def _run() -> None:
        stop_event = asyncio.Event()
        path = _heartbeat_path("market", base_dir=tmp_path)
        # 用 0.05s 间隔加快测试
        loop_task = asyncio.create_task(
            _heartbeat_loop(
                "market",
                stop_event=stop_event,
                logger=_DummyLogger(),
                interval=0.05,
                base_dir=tmp_path,
            )
        )
        # 等几个心跳周期
        await asyncio.sleep(0.2)
        assert path.exists(), "心跳文件应当被创建"
        first_mtime = path.stat().st_mtime
        await asyncio.sleep(0.15)
        second_mtime = path.stat().st_mtime
        assert second_mtime >= first_mtime, "心跳 loop 应当周期性更新 mtime"

        # 触发停止
        stop_event.set()
        await asyncio.wait_for(loop_task, timeout=1.0)
        # 文件不被删除（保留最后 mtime 给 docker stop 兜底）
        assert path.exists(), "stop 后心跳文件应保留，不主动删除"

    asyncio.run(_run())


def test_run_process_starts_and_stops_heartbeat_alongside_runtime(tmp_path, monkeypatch) -> None:
    """run_process 必须在 build_runtime 后启动心跳，且 stop 期间心跳要保持到 stop_background_tasks 之后。"""
    monkeypatch.setenv(HEARTBEAT_DIR_ENV, str(tmp_path))
    fake_runtime = _FakeRuntime()

    async def _fake_build(settings, *, process_role):
        fake_runtime.calls.append(f"build:{process_role}")
        return fake_runtime

    async def _run() -> int:
        stop_event = asyncio.Event()

        async def _set_stop_after_first_heartbeat() -> None:
            # 等心跳文件出现
            heartbeat_file = _heartbeat_path("market", base_dir=tmp_path)
            for _ in range(50):
                if heartbeat_file.exists():
                    break
                await asyncio.sleep(0.02)
            assert heartbeat_file.exists(), "run_process 启动后心跳文件必须先生成"
            stop_event.set()

        with patch.object(process_lifecycle, "build_runtime", side_effect=_fake_build), patch.object(
            process_lifecycle, "configure_logging_for_settings"
        ):
            asyncio.create_task(_set_stop_after_first_heartbeat())
            return await run_process(
                process_role=PROCESS_ROLE_MARKET,
                app_name="test.market",
                settings=SimpleNamespace(),
                stop_event=stop_event,
            )

    rc = asyncio.run(_run())
    assert rc == 0
    # build / start / stop 顺序仍然正确
    assert fake_runtime.calls == [f"build:{PROCESS_ROLE_MARKET}", "start", "stop"]
    # 心跳文件应当存在（不主动删）
    assert _heartbeat_path("market", base_dir=tmp_path).exists()


class _DummyLogger:
    """最小 logger stub：吃掉所有 info/warning/exception 调用。"""

    def info(self, *args, **kwargs) -> None:
        pass

    def warning(self, *args, **kwargs) -> None:
        pass

    def exception(self, *args, **kwargs) -> None:
        pass


def test_compose_entrypoint_injects_uvicorn_tls_when_operator_cert_env_is_present() -> None:
    text = (REPO_ROOT / "scripts" / "compose_entrypoint.py").read_text(encoding="utf-8")

    assert "_maybe_inject_uvicorn_tls" in text
    assert "--ssl-certfile" in text
    assert "--ssl-keyfile" in text
    assert "AATS_OPERATOR_TLS_CERT_FILE" in text
    assert "AATS_OPERATOR_TLS_KEY_FILE" in text
