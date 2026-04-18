"""governance.system_config KV 表访问 — 带 version CAS + 审计。

设计动机(R1-06 / R2-06):
  原 v1 设计把 feature flag 放 env var,但 env var 会被 CI 镜像层泄露
  (批次 A 刚解决的问题)。改走 DB KV 表,每次 flip 都要 apply_token,且
  对并发写用 version CAS 防丢失更新。

典型用法:
  # 读(hot path 友好,普通 SELECT)
  from aats.data_platform.governance.system_config_db import (
      get_system_config, set_system_config,
  )
  flag = get_system_config(session, "profile_upgrade_auto_apply_enabled")
  if not flag.value:
      raise HTTPException(403, "shadow period")

  # 写(带 CAS,会写 history)
  set_system_config(
      session,
      key="profile_upgrade_auto_apply_enabled",
      new_value=True,
      expected_version=1,
      actor="operator-alice",
  )
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text


class SystemConfigError(Exception):
    """system_config 通用错误。"""


class KeyNotFoundError(SystemConfigError):
    """请求的 key 在表里不存在。"""


class VersionConflictError(SystemConfigError):
    """CAS 失败 — expected_version 与 DB 不符。客户端应重读后重试。"""

    def __init__(self, key: str, expected: int, actual: int):
        super().__init__(
            f"system_config CAS conflict: key={key!r} "
            f"expected_version={expected} != current_version={actual}"
        )
        self.key = key
        self.expected = expected
        self.actual = actual


@dataclass(frozen=True)
class SystemConfigEntry:
    key: str
    value: Any       # JSON-decoded
    version: int
    updated_by: str


def get_system_config(session: Any, key: str) -> SystemConfigEntry:
    """读一个 key,找不到抛 KeyNotFoundError。"""
    row = session.execute(text("""
        SELECT key, value, version, updated_by
        FROM governance.system_config
        WHERE key = :key
    """), {"key": key}).first()

    if row is None:
        raise KeyNotFoundError(f"system_config key not found: {key!r}")

    raw = row.value
    # value 列是 JSONB — psycopg 自动 parse,但有些版本返回 str
    if isinstance(raw, str):
        value = json.loads(raw)
    else:
        value = raw

    return SystemConfigEntry(
        key=row.key,
        value=value,
        version=int(row.version),
        updated_by=row.updated_by,
    )


def get_system_config_or_default(
    session: Any, key: str, *, default: Any = None
) -> Any:
    """便捷函数:读 value,找不到返回 default。"""
    try:
        return get_system_config(session, key).value
    except KeyNotFoundError:
        return default


def set_system_config(
    session: Any,
    *,
    key: str,
    new_value: Any,
    expected_version: int,
    actor: str,
    notes: str | None = None,
) -> SystemConfigEntry:
    """CAS 写入。version 冲突抛 VersionConflictError。

    同时写 system_config_history 审计表(在同一事务里)。
    """
    if not actor or "|" in actor:
        raise ValueError(f"invalid actor: {actor!r}")

    # 1. 先读旧值(用于 history)
    old_row = session.execute(text("""
        SELECT value, version FROM governance.system_config WHERE key = :key
    """), {"key": key}).first()

    if old_row is None:
        raise KeyNotFoundError(f"system_config key not found: {key!r}")

    old_version = int(old_row.version)
    if old_version != expected_version:
        raise VersionConflictError(key, expected_version, old_version)

    old_value = old_row.value if not isinstance(old_row.value, str) else json.loads(old_row.value)
    new_value_json = json.dumps(new_value, ensure_ascii=False)

    # 2. CAS UPDATE
    update_row = session.execute(text("""
        UPDATE governance.system_config
        SET value = :new_val::jsonb,
            version = version + 1,
            updated_by = :actor,
            updated_at = NOW(),
            notes = COALESCE(:notes, notes)
        WHERE key = :key AND version = :expected
        RETURNING version
    """), {
        "key": key,
        "new_val": new_value_json,
        "actor": actor,
        "expected": expected_version,
        "notes": notes,
    }).first()

    if update_row is None:
        # 并发情况:另一个 writer 在读-写之间插入了 update
        current_version_row = session.execute(text("""
            SELECT version FROM governance.system_config WHERE key = :key
        """), {"key": key}).first()
        raise VersionConflictError(
            key, expected_version,
            int(current_version_row.version) if current_version_row else -1
        )

    new_version = int(update_row.version)

    # 3. 写 history
    session.execute(text("""
        INSERT INTO governance.system_config_history
            (key, old_value, new_value, old_version, new_version, changed_by)
        VALUES
            (:key, :old_val::jsonb, :new_val::jsonb, :old_ver, :new_ver, :actor)
    """), {
        "key": key,
        "old_val": json.dumps(old_value, ensure_ascii=False),
        "new_val": new_value_json,
        "old_ver": old_version,
        "new_ver": new_version,
        "actor": actor,
    })

    return SystemConfigEntry(
        key=key,
        value=new_value,
        version=new_version,
        updated_by=actor,
    )


def list_config_history(session: Any, key: str, limit: int = 50) -> list[dict[str, Any]]:
    """查 key 的变更历史,最新在前。"""
    rows = session.execute(text("""
        SELECT key, old_value, new_value, old_version, new_version, changed_by, changed_at
        FROM governance.system_config_history
        WHERE key = :key
        ORDER BY changed_at DESC
        LIMIT :limit
    """), {"key": key, "limit": limit}).all()

    def _decode(v: Any) -> Any:
        if isinstance(v, str):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                return v
        return v

    return [
        {
            "key": r.key,
            "old_value": _decode(r.old_value),
            "new_value": _decode(r.new_value),
            "old_version": r.old_version,
            "new_version": r.new_version,
            "changed_by": r.changed_by,
            "changed_at": r.changed_at,
        }
        for r in rows
    ]
