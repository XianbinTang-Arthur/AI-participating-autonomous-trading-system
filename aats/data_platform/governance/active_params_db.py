"""Active Parameters DB — governance.active_parameter_sets 读写层.

将 active parameter 的存储从 JSON 文件迁移到 Postgres governance schema。
提供 6 个函数，1:1 替换原有的文件操作。

依赖:
  - governance.active_parameter_sets 表  (migration 0013)
  - governance.parameter_apply_history 表 (migration 0013)
  - aats.data_platform.db 的连接管理
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


# ── UPSERT (写入 / 更新 active 参数) ────────────────────────────────

def db_upsert_active_set(
    session: Session,
    *,
    family: str,
    timeframe: str,
    parameter_set_id: str,
    values: dict[str, Any],
    source_round_id: str | None = None,
    approval_recommendation_id: str | None = None,
    applied_by: str = "operator",
) -> None:
    """UPSERT 一条 active parameter set 记录.

    INSERT ... ON CONFLICT (family, timeframe) DO UPDATE
    保证原子性。调用方应在同一个事务中配合 db_append_history() 使用。
    """
    session.execute(
        text("""
            INSERT INTO governance.active_parameter_sets
                (family, timeframe, parameter_set_id, values,
                 source_round_id, approval_recommendation_id,
                 applied_by, applied_at, updated_at)
            VALUES
                (:family, :timeframe, :ps_id, CAST(:vals AS jsonb),
                 :src_round, :approval_rec,
                 :applied_by, :now, :now)
            ON CONFLICT (family, timeframe) DO UPDATE SET
                parameter_set_id            = EXCLUDED.parameter_set_id,
                values                      = EXCLUDED.values,
                source_round_id             = EXCLUDED.source_round_id,
                approval_recommendation_id  = EXCLUDED.approval_recommendation_id,
                applied_by                  = EXCLUDED.applied_by,
                applied_at                  = EXCLUDED.applied_at,
                updated_at                  = EXCLUDED.updated_at
        """),
        {
            "family": family,
            "timeframe": timeframe.lower(),
            "ps_id": parameter_set_id,
            "vals": _json_dumps(values),
            "src_round": source_round_id,
            "approval_rec": approval_recommendation_id,
            "applied_by": applied_by,
            "now": datetime.now(timezone.utc),
        },
    )
    log.info("DB upsert active set: %s/%s -> %s", family, timeframe, parameter_set_id)


# ── SELECT 全部 active 记录 ──────────────────────────────────────────

def db_load_active_registry(session: Session) -> dict[str, Any]:
    """SELECT 全部 active parameter sets，返回与文件 registry 相同的格式.

    Returns
    -------
    dict  格式:
        {
          "generated_at": "...",
          "active_sets": {
            "independent_15m": {
              "parameter_set_id": "ps_xxx",
              "family": "independent",
              "timeframe": "15m",
              "values": { ... }
            },
            ...
          }
        }
    """
    rows = session.execute(
        text("""
            SELECT family, timeframe, parameter_set_id,
                   values AS param_values,
                   source_round_id, approval_recommendation_id,
                   applied_by, applied_at
            FROM governance.active_parameter_sets
            ORDER BY family, timeframe
        """)
    ).fetchall()

    active_sets: dict[str, Any] = {}
    for row in rows:
        combo_key = f"{row.family}_{row.timeframe}"
        active_sets[combo_key] = {
            "parameter_set_id": row.parameter_set_id,
            "family": row.family,
            "timeframe": row.timeframe,
            "values": row.param_values,
            "source_round_id": row.source_round_id,
            "approval_recommendation_id": row.approval_recommendation_id,
            "applied_by": row.applied_by,
            "applied_at": row.applied_at.isoformat() if row.applied_at else None,
        }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "active_sets": active_sets,
    }


# ── SELECT 单条 ──────────────────────────────────────────────────────

def db_get_active_set(
    session: Session,
    family: str,
    timeframe: str,
) -> dict[str, Any]:
    """获取指定 family/timeframe 的 active 参数值.

    Returns
    -------
    dict  参数值字典，如果不存在返回空 dict。
    """
    row = session.execute(
        text("""
            SELECT values AS param_values FROM governance.active_parameter_sets
            WHERE family = :family AND timeframe = :timeframe
        """),
        {"family": family, "timeframe": timeframe.lower()},
    ).fetchone()

    if row is None:
        return {}
    return row.param_values


# ── DELETE 单条 ──────────────────────────────────────────────────────

def db_clear_active_set(
    session: Session,
    family: str,
    timeframe: str,
) -> bool:
    """删除指定 family/timeframe 的 active 记录.

    Returns
    -------
    bool  是否确实删除了一行。
    """
    result = session.execute(
        text("""
            DELETE FROM governance.active_parameter_sets
            WHERE family = :family AND timeframe = :timeframe
        """),
        {"family": family, "timeframe": timeframe.lower()},
    )
    deleted = result.rowcount > 0
    if deleted:
        log.info("DB cleared active set: %s/%s", family, timeframe)
    return deleted


# ── INSERT 审计日志 ──────────────────────────────────────────────────

def db_append_history(
    session: Session,
    *,
    operation_id: str,
    operation_type: str,
    family: str,
    timeframe: str,
    from_parameter_set_id: str | None = None,
    to_parameter_set_id: str | None = None,
    recommendation_id: str | None = None,
    actor: str = "operator",
    notes: str | None = None,
) -> None:
    """INSERT 一条审计日志（不可变，只追加）."""
    session.execute(
        text("""
            INSERT INTO governance.parameter_apply_history
                (operation_id, operation_type, family, timeframe,
                 from_parameter_set_id, to_parameter_set_id,
                 recommendation_id, actor, notes, created_at)
            VALUES
                (:op_id, :op_type, :family, :timeframe,
                 :from_ps, :to_ps,
                 :rec_id, :actor, :notes, :now)
        """),
        {
            "op_id": operation_id,
            "op_type": operation_type,
            "family": family,
            "timeframe": timeframe.lower(),
            "from_ps": from_parameter_set_id,
            "to_ps": to_parameter_set_id,
            "rec_id": recommendation_id,
            "actor": actor,
            "notes": notes,
            "now": datetime.now(timezone.utc),
        },
    )
    log.info("DB history: %s %s/%s (%s)", operation_type, family, timeframe, operation_id)


# ── 查询上一版本（用于 rollback） ─────────────────────────────────

def db_get_previous_set_id(
    session: Session,
    family: str,
    timeframe: str,
) -> str | None:
    """查找最近一条 apply/rollback 操作的 from_parameter_set_id.

    用于 rollback：当前 active 是最新一条 apply 的 to_parameter_set_id，
    那么上一版就是那条记录的 from_parameter_set_id。

    Returns
    -------
    str | None  上一版的 parameter_set_id，如果没有历史则返回 None。
    """
    row = session.execute(
        text("""
            SELECT from_parameter_set_id
            FROM governance.parameter_apply_history
            WHERE family = :family
              AND timeframe = :timeframe
              AND operation_type IN ('apply', 'rollback')
            ORDER BY created_at DESC
            LIMIT 1
        """),
        {"family": family, "timeframe": timeframe.lower()},
    ).fetchone()

    if row is None:
        return None
    return row.from_parameter_set_id


# ── 工具函数 ──────────────────────────────────────────────────────────

def _json_dumps(obj: Any) -> str:
    """序列化为 JSON 字符串（给 JSONB 参数用）."""
    return json.dumps(obj, ensure_ascii=False, default=str)
