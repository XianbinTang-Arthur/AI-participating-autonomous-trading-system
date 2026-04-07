"""Stage 3 单元测试：AATS_PROCESS_ROLE 环境变量门控。

覆盖：
1. settings.process_role 默认 None（monolith 行为兼容）
2. AATSSettings 验证器：合法集合、归一化、空值处理、非法值拒绝
3. AATS_PROCESS_ROLE 环境变量被 BaseSettings 自动加载
4. process_role → scoped_runtime_lock_key 派生路径
"""
from __future__ import annotations

import os

import pytest

from aats.bootstrap.settings import (
    ALLOWED_PROCESS_ROLES,
    PROCESS_ROLE_DECISION,
    PROCESS_ROLE_EXECUTION,
    PROCESS_ROLE_GATEWAY,
    PROCESS_ROLE_MARKET,
    PROCESS_ROLE_MONOLITH,
    AATSSettings,
)
from aats.storage.session import scoped_runtime_lock_key


# ─────────────────────────────────────────────────────────────────────
# 验证器
# ─────────────────────────────────────────────────────────────────────


def test_default_process_role_is_none() -> None:
    """默认未设置 process_role：表示 monolith 模式（向后兼容）。"""
    settings = AATSSettings.model_validate({})
    assert settings.process_role is None


def test_allowed_roles_set_contents() -> None:
    """合法集合 = {monolith, gateway, market, decision, execution}。"""
    assert ALLOWED_PROCESS_ROLES == frozenset(
        {
            PROCESS_ROLE_MONOLITH,
            PROCESS_ROLE_GATEWAY,
            PROCESS_ROLE_MARKET,
            PROCESS_ROLE_DECISION,
            PROCESS_ROLE_EXECUTION,
        }
    )


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("monolith", "monolith"),
        ("MONOLITH", "monolith"),
        ("Gateway", "gateway"),
        ("  market  ", "market"),
        ("decision", "decision"),
        ("EXECUTION", "execution"),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_process_role_normalized(raw: object, expected: str | None) -> None:
    """字符串归一化：去空白 + 转小写；空值视为 None。"""
    settings = AATSSettings.model_validate({"process_role": raw})
    assert settings.process_role == expected


@pytest.mark.parametrize(
    "bad_value",
    ["bogus", "trading", "frontend", "core", "executor", "x"],
)
def test_invalid_process_role_rejected(bad_value: str) -> None:
    """非法 process_role 必须被 ValueError 拒绝。"""
    with pytest.raises(ValueError, match="process_role"):
        AATSSettings.model_validate({"process_role": bad_value})


def test_non_string_process_role_rejected() -> None:
    """非字符串类型被拒绝（int/list/dict 等）。"""
    with pytest.raises(ValueError, match="process_role must be string"):
        AATSSettings.model_validate({"process_role": 123})


# ─────────────────────────────────────────────────────────────────────
# 环境变量加载
# ─────────────────────────────────────────────────────────────────────


def test_env_var_aats_process_role_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    """AATS_PROCESS_ROLE 环境变量必须被 BaseSettings 自动加载。"""
    monkeypatch.setenv("AATS_PROCESS_ROLE", "decision")
    # 用 pydantic-settings 真实加载链路（不是 model_validate({})）
    settings = AATSSettings()
    assert settings.process_role == "decision"


def test_env_var_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量大小写/空白也走归一化。"""
    monkeypatch.setenv("AATS_PROCESS_ROLE", "  Execution  ")
    settings = AATSSettings()
    assert settings.process_role == "execution"


def test_env_var_invalid_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量非法值在加载阶段就抛错。"""
    monkeypatch.setenv("AATS_PROCESS_ROLE", "bogus_role")
    with pytest.raises(ValueError, match="process_role"):
        AATSSettings()


def test_env_var_unset_falls_back_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """未设置环境变量时 process_role 仍为 None。"""
    monkeypatch.delenv("AATS_PROCESS_ROLE", raising=False)
    settings = AATSSettings()
    assert settings.process_role is None


# ─────────────────────────────────────────────────────────────────────
# 与 scoped_runtime_lock_key 联动
# ─────────────────────────────────────────────────────────────────────


def test_each_role_yields_distinct_lock_key() -> None:
    """4 进程角色派生的 advisory lock_key 必须互不相同，
    且都不等于 monolith 默认值。"""
    base = 42_420_001
    db = "postgresql+asyncpg://user@host:5432/aats"
    monolith_key = scoped_runtime_lock_key(
        database_url=db,
        base_lock_key=base,
        process_role=None,
    )
    role_keys = {
        role: scoped_runtime_lock_key(
            database_url=db,
            base_lock_key=base,
            process_role=role,
        )
        for role in (
            PROCESS_ROLE_MONOLITH,
            PROCESS_ROLE_GATEWAY,
            PROCESS_ROLE_MARKET,
            PROCESS_ROLE_DECISION,
            PROCESS_ROLE_EXECUTION,
        )
    }
    # 4 个真切片角色彼此不同
    distinct_slice_keys = {
        role_keys[r]
        for r in (
            PROCESS_ROLE_GATEWAY,
            PROCESS_ROLE_MARKET,
            PROCESS_ROLE_DECISION,
            PROCESS_ROLE_EXECUTION,
        )
    }
    assert len(distinct_slice_keys) == 4
    # monolith（默认 None）和显式 "monolith" 应当一致
    assert monolith_key == role_keys[PROCESS_ROLE_MONOLITH]
    # 切片角色都不等于 monolith
    for role in (
        PROCESS_ROLE_GATEWAY,
        PROCESS_ROLE_MARKET,
        PROCESS_ROLE_DECISION,
        PROCESS_ROLE_EXECUTION,
    ):
        assert role_keys[role] != monolith_key


def test_lock_keys_within_postgres_bigint_range() -> None:
    """所有派生 key 都必须落在 Postgres signed bigint 安全范围。"""
    base = 42_420_001
    db = "postgresql+asyncpg://user@host:5432/aats"
    bigint_max = (1 << 63) - 1
    for role in (None, *ALLOWED_PROCESS_ROLES):
        key = scoped_runtime_lock_key(
            database_url=db,
            base_lock_key=base,
            process_role=role,
        )
        assert 0 <= key <= bigint_max, (role, key)
