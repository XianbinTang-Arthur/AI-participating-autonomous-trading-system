"""L3 回归：advisory lock keys 集中注册表。

过去 governance scheduler / release_cycle 各自硬编码 bigint key（magic number），
两个模块复用同一把锁时没人能从读代码时察觉。L3 把 key 集中到 _db_util，caller
通过 ``ADVISORY_LOCK_KEYS["<purpose>"]`` 引用，新增锁必须先在注册表登记。

本文件锁定：
  1. 注册表至少覆盖当前已知用途
  2. 所有 caller 都走 ADVISORY_LOCK_KEYS[...] 而不是在 caller 侧重新硬编码
     （静态扫源码，避免重新磁盘硬编码 0x4141xxxx 的回归）
  3. key 值是 Postgres bigint 合法范围
"""

from __future__ import annotations

import pathlib
import re

import pytest

from aats.data_platform.governance._db_util import ADVISORY_LOCK_KEYS


_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent


def test_registry_contains_known_purposes() -> None:
    """注册表必须包含当前路径依赖的 key；新加 key 必须同步更新此列表。"""
    assert "governance_scheduler_singleton" in ADVISORY_LOCK_KEYS
    assert "release_cycle_per_release" in ADVISORY_LOCK_KEYS


def test_registry_values_are_postgres_bigint_safe() -> None:
    """pg_try_advisory_lock(bigint)：值必须落在 int64 范围。"""
    for purpose, key in ADVISORY_LOCK_KEYS.items():
        assert isinstance(key, int), f"{purpose} 的 key 必须是 int，实际 {type(key).__name__}"
        assert -(2**63) <= key < 2**63, (
            f"{purpose} 的 key={hex(key)} 超出 int64 范围"
        )


def test_registry_values_are_pairwise_distinct() -> None:
    """不同 purpose 必须对应不同的 key；否则两把锁互斥就坏了。"""
    keys = list(ADVISORY_LOCK_KEYS.values())
    assert len(keys) == len(set(keys)), (
        f"ADVISORY_LOCK_KEYS 中存在重复 key；key 碰撞会让两把互不相关的锁互斥: {ADVISORY_LOCK_KEYS!r}"
    )


def test_no_caller_hardcodes_0x4141_magic_numbers() -> None:
    """静态扫：除了 _db_util.py 定义行外，源码里不允许出现 0x4141XXXX 字面量。

    动机：运维迁移 / 重命名 purpose 时如果有人硬编码了同一把 key，会悄悄绕开
    注册表。这里锁死"注册表是唯一定义点"。
    """
    governance_dir = _PROJECT_ROOT / "aats"
    registry_source = _PROJECT_ROOT / "aats/data_platform/governance/_db_util.py"

    pattern = re.compile(r"0x4141[0-9a-fA-F]{4}")

    offenders: list[tuple[pathlib.Path, int, str]] = []
    for path in governance_dir.rglob("*.py"):
        if path.resolve() == registry_source.resolve():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                offenders.append((path, lineno, line.strip()))

    assert offenders == [], (
        "发现硬编码的 0x4141XXXX advisory lock key，必须改走 "
        "ADVISORY_LOCK_KEYS[...]：\n"
        + "\n".join(f"  {p}:{n}: {s}" for p, n, s in offenders)
    )
