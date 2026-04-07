"""Stage 3 单元测试：build_runtime 内 process_role 解析优先级。

测试 _resolve_effective_process_role 纯函数：
  1) 显式 kwarg 强制指定 → 用 kwarg
  2) kwarg=None，settings.process_role 有值 → 用 settings
  3) kwarg=None，settings.process_role=None → None（monolith 兜底）

不实际 await build_runtime（依赖 storage / 各种重型 service），
只验入参 → 决策的纯逻辑。
"""
from __future__ import annotations

import pytest

from aats.bootstrap.config import _resolve_effective_process_role
from aats.bootstrap.settings import (
    AATSSettings,
    PROCESS_ROLE_DECISION,
    PROCESS_ROLE_EXECUTION,
    PROCESS_ROLE_GATEWAY,
    PROCESS_ROLE_MARKET,
    PROCESS_ROLE_MONOLITH,
)


def _make_settings(role: str | None) -> AATSSettings:
    """构造一个仅指定 process_role 的 AATSSettings。"""
    return AATSSettings.model_validate({"process_role": role})


# ─────────────────────────────────────────────────────────────────────
# 优先级 1：显式 kwarg 永远优先
# ─────────────────────────────────────────────────────────────────────


def test_kwarg_overrides_settings() -> None:
    """kwarg 与 settings 同时存在时，kwarg 胜出。"""
    settings = _make_settings(PROCESS_ROLE_DECISION)
    resolved = _resolve_effective_process_role(
        kwarg_role=PROCESS_ROLE_EXECUTION,
        settings=settings,
    )
    assert resolved == PROCESS_ROLE_EXECUTION


def test_kwarg_overrides_even_when_settings_is_none() -> None:
    """kwarg 给值，settings 为 None 时也用 kwarg。"""
    settings = _make_settings(None)
    resolved = _resolve_effective_process_role(
        kwarg_role=PROCESS_ROLE_GATEWAY,
        settings=settings,
    )
    assert resolved == PROCESS_ROLE_GATEWAY


@pytest.mark.parametrize(
    "kwarg_role",
    [
        PROCESS_ROLE_MONOLITH,
        PROCESS_ROLE_GATEWAY,
        PROCESS_ROLE_MARKET,
        PROCESS_ROLE_DECISION,
        PROCESS_ROLE_EXECUTION,
    ],
)
def test_all_valid_kwarg_roles_pass_through(kwarg_role: str) -> None:
    """合法 role 集合里的每个值都能从 kwarg 透传。"""
    settings = _make_settings(None)
    resolved = _resolve_effective_process_role(
        kwarg_role=kwarg_role,
        settings=settings,
    )
    assert resolved == kwarg_role


# ─────────────────────────────────────────────────────────────────────
# 优先级 2：kwarg=None 时回落到 settings.process_role
# ─────────────────────────────────────────────────────────────────────


def test_settings_used_when_kwarg_is_none() -> None:
    """kwarg=None，settings.process_role 有值 → 用 settings。"""
    settings = _make_settings(PROCESS_ROLE_DECISION)
    resolved = _resolve_effective_process_role(
        kwarg_role=None,
        settings=settings,
    )
    assert resolved == PROCESS_ROLE_DECISION


@pytest.mark.parametrize(
    "settings_role",
    [
        PROCESS_ROLE_MONOLITH,
        PROCESS_ROLE_GATEWAY,
        PROCESS_ROLE_MARKET,
        PROCESS_ROLE_DECISION,
        PROCESS_ROLE_EXECUTION,
    ],
)
def test_all_valid_settings_roles_pass_through(settings_role: str) -> None:
    """合法 role 集合里的每个值都能从 settings 透传。"""
    settings = _make_settings(settings_role)
    resolved = _resolve_effective_process_role(
        kwarg_role=None,
        settings=settings,
    )
    assert resolved == settings_role


# ─────────────────────────────────────────────────────────────────────
# 优先级 3：双 None 兜底为 None（monolith）
# ─────────────────────────────────────────────────────────────────────


def test_both_none_returns_none() -> None:
    """kwarg=None，settings.process_role=None → 返回 None（monolith 默认）。"""
    settings = _make_settings(None)
    resolved = _resolve_effective_process_role(
        kwarg_role=None,
        settings=settings,
    )
    assert resolved is None


# ─────────────────────────────────────────────────────────────────────
# 不依赖 settings 验证器之外的额外归一化
# ─────────────────────────────────────────────────────────────────────


def test_settings_value_is_already_normalized() -> None:
    """settings.process_role 经 validator 归一化后，本函数应原样返回。

    确认 _resolve_effective_process_role 不会再做一次 strip/lower——
    这是 M2 的核心：把归一化下沉到 validator 单一职责。
    """
    settings = _make_settings("  EXECUTION  ")  # validator 应处理为 "execution"
    assert settings.process_role == "execution"
    resolved = _resolve_effective_process_role(
        kwarg_role=None,
        settings=settings,
    )
    assert resolved == "execution"
