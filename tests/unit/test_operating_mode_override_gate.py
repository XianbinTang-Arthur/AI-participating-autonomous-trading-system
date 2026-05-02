"""Regression: UI /ai/operating-mode/select 必须被 AATS_ALLOW_UI_OPERATING_MODE_OVERRIDE 门控.

2026-04-20 code review C1 finding:
  Governance 3 份 doc (runtime_trading_mode_semantics / frozen_parameters /
  alpha_evidence_gate) 暗示 "UI 不可切 mode", 但代码层 /ai/operating-mode/select
  endpoint admin auth 后直接 set_manual_operating_mode_override 一路放行.

修复: 在 auth_routes.py:select_ai_operating_mode 加一层 env gate,
  AATS_ALLOW_UI_OPERATING_MODE_OVERRIDE != "true" → 403.

本测试用 inspect.getsource 静态 + 动态绑定两层校验, 防 regression.
"""

from __future__ import annotations

import inspect

import pytest

from aats.services.operator.ui_capabilities import (
    UI_OPERATING_MODE_OVERRIDE_DISABLED_REASON,
    UI_OPERATING_MODE_OVERRIDE_ENV,
    ui_operating_mode_override_policy,
)


def test_select_ai_operating_mode_endpoint_source_contains_env_gate() -> None:
    """契约: select_ai_operating_mode 函数源码必须含 AATS_ALLOW_UI_OPERATING_MODE_OVERRIDE 判断.

    失败暗示: 有人移除了 env gate, governance doc (§3.6) 里的"默认 false"
    声明立即失效, UI admin 按钮再次能绕过 §3.5 持久化流程.
    """
    from aats.api.auth_routes import select_ai_operating_mode

    src = inspect.getsource(select_ai_operating_mode)
    assert "AATS_ALLOW_UI_OPERATING_MODE_OVERRIDE" in src, (
        "select_ai_operating_mode 必须读 AATS_ALLOW_UI_OPERATING_MODE_OVERRIDE env. "
        "见 docs/governance/runtime_trading_mode_semantics.md §3.6."
    )
    assert "HTTPException" in src and "403" in src, (
        "env 未启用时必须 raise HTTPException(status_code=403), 不是静默 fallthrough."
    )


def test_operating_mode_override_default_is_disabled(monkeypatch) -> None:
    """默认环境变量不设或设为 false, 路由应返回 403 并带 hint.

    模拟 FastAPI call: 仅调 raise 分支的前置 env check, 不走 _query 真实逻辑.
    """
    from fastapi import HTTPException

    from aats.api.auth_routes import select_ai_operating_mode

    # 清掉可能的默认值
    monkeypatch.delenv("AATS_ALLOW_UI_OPERATING_MODE_OVERRIDE", raising=False)

    # 用 mock principal + payload 跑到 env check 分支
    class _Req:
        pass

    class _P:
        role = "admin"
        identity = "test_admin"
        auth_source = "test"

    class _Payload:
        mode = "ai_decision_maker"
        reason = "unit_test_default_gate"

    with pytest.raises(HTTPException) as exc_info:
        # FastAPI handler 是 async; 用 asyncio.run (Py3.14 不再自动建 loop)
        import asyncio
        asyncio.run(
            select_ai_operating_mode(
                request=_Req(),
                payload=_Payload(),
                principal=_P(),
            )
        )

    err = exc_info.value
    assert err.status_code == 403
    detail = str(err.detail)
    assert "AATS_ALLOW_UI_OPERATING_MODE_OVERRIDE" in detail
    assert "runtime_trading_mode_semantics.md" in detail
    assert "§3.6" in detail or "3.6" in detail


@pytest.mark.parametrize("env_value", ["true", "True", "TRUE", "1", "yes", "Yes"])
def test_operating_mode_override_truthy_values_accepted(monkeypatch, env_value) -> None:
    """AATS_ALLOW_UI_OPERATING_MODE_OVERRIDE=true/1/yes (任意大小写) 应放行, 落入 _query 路径.

    注: 放行后的逻辑 (_query(request).set_ai_operating_mode) 不是本测试范围,
    只验 gate check 不再 raise 403.
    """
    from aats.api.auth_routes import select_ai_operating_mode

    monkeypatch.setenv("AATS_ALLOW_UI_OPERATING_MODE_OVERRIDE", env_value)

    class _Req:
        app = type("_App", (), {"state": type("_S", (), {})()})()

    class _P:
        role = "admin"
        identity = "test_admin"
        auth_source = "test"

    class _Payload:
        mode = "ai_decision_maker"
        reason = "unit_test_true_gate"

    # 放行后会落到 _query(request), 没 query 服务注入会抛别的异常 (AttributeError /
    # TypeError / RuntimeError). 只要**不是** status_code=403 HTTPException 即证 gate 放行.
    import asyncio
    try:
        asyncio.run(
            select_ai_operating_mode(
                request=_Req(),
                payload=_Payload(),
                principal=_P(),
            )
        )
    except Exception as exc:
        # 接受任何非 403 HTTPException 或其他异常
        from fastapi import HTTPException
        if isinstance(exc, HTTPException):
            assert exc.status_code != 403, (
                f"AATS_ALLOW_UI_OPERATING_MODE_OVERRIDE={env_value!r} 应放行, "
                f"但路由仍返回 403: {exc.detail}"
            )


def test_ai_runtime_ui_override_policy_defaults_to_disabled(monkeypatch) -> None:
    """前端不能猜测按钮可用性；/ai/runtime 必须有后端能力字段可读。"""
    monkeypatch.delenv(UI_OPERATING_MODE_OVERRIDE_ENV, raising=False)

    policy = ui_operating_mode_override_policy()

    assert policy == {
        "enabled": False,
        "source": "environment",
        "disabled_reason": UI_OPERATING_MODE_OVERRIDE_DISABLED_REASON,
    }
    assert UI_OPERATING_MODE_OVERRIDE_ENV not in policy


@pytest.mark.parametrize("env_value", ["true", "TRUE", "1", "yes", "Yes"])
def test_ai_runtime_ui_override_policy_truthy_values_enable_capability(
    monkeypatch,
    env_value,
) -> None:
    monkeypatch.setenv(UI_OPERATING_MODE_OVERRIDE_ENV, env_value)

    policy = ui_operating_mode_override_policy()

    assert policy == {
        "enabled": True,
        "source": "environment",
        "disabled_reason": None,
    }
