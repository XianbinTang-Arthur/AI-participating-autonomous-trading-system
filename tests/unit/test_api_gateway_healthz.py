"""Stage 7 单元测试：apps.api_gateway.main 的 /healthz endpoint。

设计动机（与 /system/health 的区别）：
* /system/health 是 operator/UI 用的诊断 endpoint，需要全量 portfolio /
  reconciliation / market 状态，依赖 runtime 上多个 slice service。
* /healthz 是 docker compose healthcheck 专用 liveness probe：
    - 必须不依赖任何 slice service（gateway role 下 market_gateway / execution_adapter
      都是 None，否则会 NPE）
    - 必须不要求 auth（docker healthcheck curl 不带 Bearer token）
    - 必须挂在 FastAPI app 上而不是挂到带 require_read_access 的 router 上

这组测试通过静态源码 + 直接调函数的方式覆盖这些不变量，避免引入
TestClient + lifespan + build_runtime 的重型依赖（那是 integration 测试的工作）。
"""
from __future__ import annotations

import asyncio
import inspect
import os
from unittest.mock import patch

import pytest

from apps.api_gateway import main as gateway_main
from aats.bootstrap.settings import (
    PROCESS_ROLE_DECISION,
    PROCESS_ROLE_EXECUTION,
    PROCESS_ROLE_GATEWAY,
    PROCESS_ROLE_MARKET,
    PROCESS_ROLE_MONOLITH,
)


# ─────────────────────────────────────────────────────────────────────
# 1) /healthz 函数行为：直接 await，验证返回值与 process_role 对齐
# ─────────────────────────────────────────────────────────────────────


def test_healthz_returns_ok_with_gateway_role_by_default(monkeypatch) -> None:
    """默认 AATS_PROCESS_ROLE 不设时返回 gateway。"""
    monkeypatch.delenv("AATS_PROCESS_ROLE", raising=False)
    result = asyncio.run(gateway_main.healthz())
    assert result == {"status": "ok", "process_role": PROCESS_ROLE_GATEWAY}


def test_healthz_returns_explicit_gateway_role(monkeypatch) -> None:
    monkeypatch.setenv("AATS_PROCESS_ROLE", "gateway")
    result = asyncio.run(gateway_main.healthz())
    assert result["status"] == "ok"
    assert result["process_role"] == PROCESS_ROLE_GATEWAY


def test_healthz_returns_monolith_role_when_set(monkeypatch) -> None:
    """monolith 兼容路径：单进程模式下也允许从 api_gateway entry 启动。"""
    monkeypatch.setenv("AATS_PROCESS_ROLE", "monolith")
    result = asyncio.run(gateway_main.healthz())
    assert result["process_role"] == PROCESS_ROLE_MONOLITH


@pytest.mark.parametrize(
    "non_gateway_role",
    [PROCESS_ROLE_MARKET, PROCESS_ROLE_DECISION, PROCESS_ROLE_EXECUTION],
)
def test_healthz_falls_back_to_gateway_for_non_gateway_roles(monkeypatch, non_gateway_role) -> None:
    """如果有人误把 AATS_PROCESS_ROLE=market 注入到 api_gateway 进程，
    _resolved_process_role() 会兜底回 gateway —— /healthz 必须反映这一点。
    """
    monkeypatch.setenv("AATS_PROCESS_ROLE", non_gateway_role)
    result = asyncio.run(gateway_main.healthz())
    # api_gateway 进程不应当承担 market/decision/execution role，必须兜底回 gateway
    assert result["process_role"] == PROCESS_ROLE_GATEWAY


def test_healthz_handles_invalid_role_env_value(monkeypatch) -> None:
    """非法值不能让 healthz 抛错，必须兜底回 gateway。"""
    monkeypatch.setenv("AATS_PROCESS_ROLE", "not_a_real_role")
    result = asyncio.run(gateway_main.healthz())
    assert result["process_role"] == PROCESS_ROLE_GATEWAY


# ─────────────────────────────────────────────────────────────────────
# 2) /healthz 注册位置：必须挂在 app 上，不能挂在 require_read_access router 上
# ─────────────────────────────────────────────────────────────────────


def test_healthz_route_is_registered_on_app_directly_without_auth() -> None:
    """关键回归：/healthz 必须直接注册在 FastAPI app 上，不能进 routes.py 的
    require_read_access router，否则 docker healthcheck 不带 token 会 401。
    """
    app = gateway_main.app
    # 找 /healthz 路由
    healthz_routes = [route for route in app.routes if getattr(route, "path", None) == "/healthz"]
    assert len(healthz_routes) == 1, "必须有且仅有一个 /healthz 路由"
    route = healthz_routes[0]

    # 该路由的 dependant 必须没有 require_read_access / require_write_access 这种
    # auth dependency。FastAPI 把这些塞在 dependant.dependencies 链里。
    deps = getattr(route, "dependant", None)
    if deps is None:
        # 某些 fastapi 版本 dependant 直接挂在 route 自身
        deps = route
    # 简单字符串扫：看路由源码或 dependant 字符串里是否提到 require_*_access
    # 更稳妥的判断：直接走源码检查 main.py 里 healthz 的注册方式
    source = inspect.getsource(gateway_main)
    healthz_def_idx = source.find("async def healthz")
    assert healthz_def_idx > 0, "main.py 必须定义 healthz 函数"
    # 往上找最近的装饰器，必须是 @app.get("/healthz") 而不是 @router.get(...)
    pre_def = source[:healthz_def_idx]
    last_decorator_idx = pre_def.rfind("@")
    assert last_decorator_idx > 0
    decorator_line = pre_def[last_decorator_idx:].split("\n")[0]
    assert "@app.get" in decorator_line, (
        f"healthz 必须用 @app.get 装饰器（绕过 router 的 require_read_access auth），"
        f"实际装饰器：{decorator_line}"
    )
    assert '"/healthz"' in decorator_line


def test_healthz_response_body_shape_is_dict_with_status_and_process_role() -> None:
    """response shape 守卫：status 与 process_role 两个字段都必须是字符串。"""
    result = asyncio.run(gateway_main.healthz())
    assert isinstance(result, dict)
    assert set(result.keys()) == {"status", "process_role"}
    assert isinstance(result["status"], str)
    assert isinstance(result["process_role"], str)
    assert result["status"] == "ok"
