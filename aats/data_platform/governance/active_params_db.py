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
        # JSONB 列正常情况下一定是 dict,但若 DB 被手工订正成标量/数组(historical
        # migration 事故或 ORM bypass 写入),下游 `_build_applied_recommendation_ids`
        # 等消费者会 AttributeError。在最接近 DB 的加载层做类型护栏,把坏数据
        # 降级为空 dict + log,避免整条读路径崩。
        param_values = row.param_values if isinstance(row.param_values, dict) else {}
        active_sets[combo_key] = {
            "parameter_set_id": row.parameter_set_id,
            "family": row.family,
            "timeframe": row.timeframe,
            "values": param_values,
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

    加 ``FOR UPDATE`` 是为了防止并发两个 rollback 请求同时从同一条 history
    行推导出同一个目标：第二个请求会在事务结束前被阻塞，避免双写竞态。

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
            FOR UPDATE
        """),
        {"family": family, "timeframe": timeframe.lower()},
    ).fetchone()

    if row is None:
        return None
    return row.from_parameter_set_id


# ── Rollback 目标强校验 ─────────────────────────────────────────────


def validate_rollback_target(
    session: Session,
    family: str,
    timeframe: str,
    target_parameter_set_id: str,
) -> tuple[bool, str]:
    """校验 rollback 目标合法性。返回 ``(ok, reason_if_rejected)``.

    见 ``docs/task/rdp_hardening_batch_a_detailed_design.md §2.3``。6 条规则：

    1. parameter_sets 存在 + family/timeframe 匹配
    2. status ∈ {frozen, released}
    3. 归属正确（与 1 同查询）
    4. parameter_apply_history 有一条 ``operation_type='apply'``、
       ``to_parameter_set_id=target`` 的历史（证明 target 曾是 live）
    5. active_parameter_sets 当前值 ≠ target（避免自回滚）
    6. recommendations 表有一条 ``status IN ('approved','applied','rolled_back')``
       指向 target 的记录（批准链路）

    全部通过才返回 ``(True, "")``；任何一条失败，立即短路并返回英文理由码。
    调用方负责映射到 HTTP 语义（422）与结构化日志审计。
    """
    tf = timeframe.lower()

    # 规则 1+2+3: parameter_sets 存在 + 状态合法 + 归属正确
    row = session.execute(
        text("""
            SELECT status FROM governance.parameter_sets
            WHERE parameter_set_id = :pid
              AND family = :family
              AND timeframe = :tf
        """),
        {"pid": target_parameter_set_id, "family": family, "tf": tf},
    ).fetchone()
    if row is None:
        return False, "target_not_found_or_wrong_combo"
    if row.status not in ("frozen", "released"):
        return False, f"target_status_illegal:{row.status}"

    # 规则 4: 历史凭证（必须在该 combo 下作为 apply 的 to 出现过）
    history_row = session.execute(
        text("""
            SELECT 1 FROM governance.parameter_apply_history
            WHERE family = :family
              AND timeframe = :tf
              AND operation_type = 'apply'
              AND to_parameter_set_id = :pid
            LIMIT 1
        """),
        {"family": family, "tf": tf, "pid": target_parameter_set_id},
    ).fetchone()
    if history_row is None:
        return False, "no_apply_history_for_target"

    # 规则 5: 不是当前生效（同时锁住 active 行，防止校验后被改）
    current_row = session.execute(
        text("""
            SELECT parameter_set_id FROM governance.active_parameter_sets
            WHERE family = :family AND timeframe = :tf
            FOR UPDATE
        """),
        {"family": family, "tf": tf},
    ).fetchone()
    if (
        current_row is not None
        and current_row.parameter_set_id == target_parameter_set_id
    ):
        return False, "target_is_currently_active"

    # 规则 6: 批准链路（至少一条 approved/applied/rolled_back recommendation）
    rec_row = session.execute(
        text("""
            SELECT 1 FROM governance.recommendations
            WHERE target_parameter_set_id = :pid
              AND family = :family
              AND timeframe = :tf
              AND status IN ('approved', 'applied', 'rolled_back')
            LIMIT 1
        """),
        {"pid": target_parameter_set_id, "family": family, "tf": tf},
    ).fetchone()
    if rec_row is None:
        return False, "no_approved_recommendation_lineage"

    return True, ""


def db_get_parameter_set_values(
    session: Session,
    parameter_set_id: str,
    *,
    family: str | None = None,
    timeframe: str | None = None,
) -> dict[str, Any] | None:
    """从 DB 读 parameter_sets.values + source_round_id，并推导 lineage rec id.

    Rollback 强制从 DB 读目标 values，不再依赖 JSON registry —— 这是 A-0.1
    的核心收口：消除"写 JSON → 读 JSON → 写 DB"的注入通道。

    ``approval_recommendation_id`` 不是 ``parameter_sets`` 的列，而是挂在
    ``active_parameter_sets``/``parameter_apply_history`` 上。这里通过查询
    最近一次把该 target 作为 ``to_parameter_set_id`` 的 apply 历史来推导
    lineage（与 validator 规则 4 使用的是同一证据源）。
    """
    row = session.execute(
        text("""
            SELECT values AS param_values,
                   source_round_id
            FROM governance.parameter_sets
            WHERE parameter_set_id = :pid
        """),
        {"pid": parameter_set_id},
    ).fetchone()
    if row is None:
        return None

    lineage_rec_id: str | None = None
    params: dict[str, Any] = {"pid": parameter_set_id}
    where_extra = ""
    if family is not None and timeframe is not None:
        where_extra = " AND family = :family AND timeframe = :tf"
        params["family"] = family
        params["tf"] = timeframe.lower()
    lineage_row = session.execute(
        text(f"""
            SELECT recommendation_id
            FROM governance.parameter_apply_history
            WHERE operation_type = 'apply'
              AND to_parameter_set_id = :pid
              {where_extra}
            ORDER BY created_at DESC
            LIMIT 1
        """),
        params,
    ).fetchone()
    if lineage_row is not None:
        lineage_rec_id = lineage_row.recommendation_id

    return {
        "values": row.param_values,
        "source_round_id": row.source_round_id,
        "approval_recommendation_id": lineage_rec_id,
    }


# ── 工具函数 ──────────────────────────────────────────────────────────

def _json_dumps(obj: Any) -> str:
    """序列化为 JSON 字符串（给 JSONB 参数用）."""
    return json.dumps(obj, ensure_ascii=False, default=str)
