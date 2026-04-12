"""Recommendations & Active Decisions DB — governance 读写层.

将 recommendation_registry.json 和 active_decision_registry.json
持久化到 Postgres governance schema。

依赖:
  - governance.recommendations 表 (RecommendationModel in rdp_models.py)
  - governance.active_decisions 表 (ActiveDecisionModel in rdp_models.py)
  - aats.data_platform.db 的连接管理
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from ._db_util import VALID_REC_STATUSES, VALID_REC_TYPES, parse_dt

log = logging.getLogger(__name__)


# =====================================================================
# Recommendations CRUD
# =====================================================================


def db_upsert_recommendation(
    session: Session,
    *,
    recommendation_id: str,
    family: str,
    timeframe: str,
    recommendation_type: str,
    confidence: str,
    reason: str,
    symbol: str = "BTC-USDT-SWAP",
    target_parameter_set_id: str | None = None,
    evidence_bundle_ref: str | None = None,
    status: str = "draft",
    approved_by: str | None = None,
    approved_at: str | None = None,
    review_notes: str | None = None,
    rejected_by: str | None = None,
    rejected_at: str | None = None,
    superseded_by: str | None = None,
    superseded_at: str | None = None,
    superseded_by_recommendation_id: str | None = None,
    created_at: str | None = None,
) -> None:
    """UPSERT 一条 recommendation 记录.

    INSERT ... ON CONFLICT (recommendation_id) DO UPDATE
    """
    if status not in VALID_REC_STATUSES:
        raise ValueError(f"非法 recommendation status: {status!r}，合法值: {sorted(VALID_REC_STATUSES)}")
    if recommendation_type not in VALID_REC_TYPES:
        raise ValueError(
            f"非法 recommendation_type: {recommendation_type!r}，合法值: {sorted(VALID_REC_TYPES)}"
        )

    session.execute(
        text("""
            INSERT INTO governance.recommendations
                (recommendation_id, family, symbol, timeframe,
                 recommendation_type, target_parameter_set_id,
                 confidence, reason, evidence_bundle_ref,
                 status, approved_by, approved_at, review_notes,
                 rejected_by, rejected_at,
                 superseded_by, superseded_at, superseded_by_recommendation_id,
                 created_at)
            VALUES
                (:rec_id, :family, :symbol, :timeframe,
                 :rec_type, :target_ps_id,
                 :confidence, :reason, :evidence_ref,
                 :status, :approved_by, :approved_at, :review_notes,
                 :rejected_by, :rejected_at,
                 :superseded_by, :superseded_at, :superseded_by_rec_id,
                 :created_at)
            ON CONFLICT (recommendation_id) DO UPDATE SET
                family                         = EXCLUDED.family,
                symbol                         = EXCLUDED.symbol,
                timeframe                      = EXCLUDED.timeframe,
                recommendation_type            = EXCLUDED.recommendation_type,
                target_parameter_set_id        = EXCLUDED.target_parameter_set_id,
                confidence                     = EXCLUDED.confidence,
                reason                         = EXCLUDED.reason,
                evidence_bundle_ref            = EXCLUDED.evidence_bundle_ref,
                status                         = EXCLUDED.status,
                approved_by                    = EXCLUDED.approved_by,
                approved_at                    = EXCLUDED.approved_at,
                review_notes                   = EXCLUDED.review_notes,
                rejected_by                    = EXCLUDED.rejected_by,
                rejected_at                    = EXCLUDED.rejected_at,
                superseded_by                  = EXCLUDED.superseded_by,
                superseded_at                  = EXCLUDED.superseded_at,
                superseded_by_recommendation_id = EXCLUDED.superseded_by_recommendation_id
        """),
        {
            "rec_id": recommendation_id,
            "family": family,
            "symbol": symbol,
            "timeframe": timeframe.lower(),
            "rec_type": recommendation_type,
            "target_ps_id": target_parameter_set_id,
            "confidence": confidence,
            "reason": reason,
            "evidence_ref": evidence_bundle_ref,
            "status": status,
            "approved_by": approved_by,
            "approved_at": parse_dt(approved_at),
            "review_notes": review_notes,
            "rejected_by": rejected_by,
            "rejected_at": parse_dt(rejected_at),
            "superseded_by": superseded_by,
            "superseded_at": parse_dt(superseded_at),
            "superseded_by_rec_id": superseded_by_recommendation_id,
            "created_at": parse_dt(created_at) or datetime.now(timezone.utc),
        },
    )
    log.info("DB upsert recommendation: %s (%s/%s, status=%s)",
             recommendation_id, family, timeframe, status)


def db_update_recommendation_status(
    session: Session,
    recommendation_id: str,
    *,
    status: str,
    approved_by: str | None = None,
    approved_at: str | None = None,
    review_notes: str | None = None,
    rejected_by: str | None = None,
    rejected_at: str | None = None,
    superseded_by: str | None = None,
    superseded_at: str | None = None,
    superseded_by_recommendation_id: str | None = None,
) -> bool:
    """更新 recommendation 审批状态.

    Returns True 如果确实更新了一行。
    """
    set_parts = ["status = :status"]
    params: dict[str, Any] = {"rec_id": recommendation_id, "status": status}

    if approved_by is not None:
        set_parts.append("approved_by = :approved_by")
        params["approved_by"] = approved_by
    if approved_at is not None:
        set_parts.append("approved_at = :approved_at")
        params["approved_at"] = parse_dt(approved_at) or datetime.now(timezone.utc)
    if review_notes is not None:
        set_parts.append("review_notes = :review_notes")
        params["review_notes"] = review_notes
    if rejected_by is not None:
        set_parts.append("rejected_by = :rejected_by")
        params["rejected_by"] = rejected_by
    if rejected_at is not None:
        set_parts.append("rejected_at = :rejected_at")
        params["rejected_at"] = parse_dt(rejected_at) or datetime.now(timezone.utc)
    if superseded_by is not None:
        set_parts.append("superseded_by = :superseded_by")
        params["superseded_by"] = superseded_by
    if superseded_at is not None:
        set_parts.append("superseded_at = :superseded_at")
        params["superseded_at"] = parse_dt(superseded_at) or datetime.now(timezone.utc)
    if superseded_by_recommendation_id is not None:
        set_parts.append("superseded_by_recommendation_id = :superseded_by_rec_id")
        params["superseded_by_rec_id"] = superseded_by_recommendation_id

    sql = f"UPDATE governance.recommendations SET {', '.join(set_parts)} WHERE recommendation_id = :rec_id"
    result = session.execute(text(sql), params)
    updated = result.rowcount > 0
    if updated:
        log.info("DB update recommendation status: %s -> %s", recommendation_id, status)
    return updated


_REC_SELECT_COLUMNS = """\
recommendation_id, family, symbol, timeframe,
recommendation_type, target_parameter_set_id,
confidence, reason, evidence_bundle_ref,
status, approved_by, approved_at, review_notes,
rejected_by, rejected_at,
superseded_by, superseded_at, superseded_by_recommendation_id,
created_at"""


def db_find_recommendation(
    session: Session,
    recommendation_id: str,
) -> dict[str, Any] | None:
    """按 recommendation_id 查询单条."""
    row = session.execute(
        text(f"""
            SELECT {_REC_SELECT_COLUMNS}
            FROM governance.recommendations
            WHERE recommendation_id = :rec_id
        """),
        {"rec_id": recommendation_id},
    ).fetchone()

    if row is None:
        return None
    return _rec_row_to_dict(row)


def db_find_recommendations(
    session: Session,
    *,
    family: str | None = None,
    timeframe: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """按条件查询 recommendations."""
    where_parts: list[str] = []
    params: dict[str, Any] = {}

    if family is not None:
        where_parts.append("family = :family")
        params["family"] = family
    if timeframe is not None:
        where_parts.append("timeframe = :timeframe")
        params["timeframe"] = timeframe.lower()
    if status is not None:
        where_parts.append("status = :status")
        params["status"] = status

    where_clause = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""
    sql = f"""
        SELECT {_REC_SELECT_COLUMNS}
        FROM governance.recommendations
        {where_clause}
        ORDER BY created_at DESC
    """
    rows = session.execute(text(sql), params).fetchall()
    return [_rec_row_to_dict(r) for r in rows]


def db_load_recommendation_registry(session: Session) -> dict[str, Any]:
    """导出全量 recommendations，返回与文件 registry 兼容的 dict 格式.

    Returns
    -------
    dict  格式:
        {
          "generated_at": "...",
          "recommendations": [
            {"recommendation_id": "rec_xxx", ...},
            ...
          ]
        }
    """
    rows = session.execute(
        text(f"""
            SELECT {_REC_SELECT_COLUMNS}
            FROM governance.recommendations
            ORDER BY created_at
        """)
    ).fetchall()

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "recommendations": [_rec_row_to_dict(r) for r in rows],
    }


# =====================================================================
# Active Decisions CRUD
# =====================================================================


def db_upsert_active_decision(
    session: Session,
    *,
    family: str,
    timeframe: str,
    current_status: str,
    symbol: str = "BTC-USDT-SWAP",
    active_parameter_set_id: str | None = None,
    last_recommendation_id: str | None = None,
    notes: str | None = None,
) -> None:
    """UPSERT 一条 active decision 记录.

    INSERT ... ON CONFLICT (family, timeframe) DO UPDATE
    """
    combo_key = f"{family}_{timeframe.lower()}"
    session.execute(
        text("""
            INSERT INTO governance.active_decisions
                (family, symbol, timeframe, combo_key,
                 current_status, active_parameter_set_id,
                 last_recommendation_id, last_updated_at, notes)
            VALUES
                (:family, :symbol, :timeframe, :combo_key,
                 :status, :active_ps_id,
                 :last_rec_id, :now, :notes)
            ON CONFLICT (family, timeframe) DO UPDATE SET
                symbol                    = EXCLUDED.symbol,
                combo_key                 = EXCLUDED.combo_key,
                current_status            = EXCLUDED.current_status,
                active_parameter_set_id   = EXCLUDED.active_parameter_set_id,
                last_recommendation_id    = EXCLUDED.last_recommendation_id,
                last_updated_at           = EXCLUDED.last_updated_at,
                notes                     = EXCLUDED.notes
        """),
        {
            "family": family,
            "symbol": symbol,
            "timeframe": timeframe.lower(),
            "combo_key": combo_key,
            "status": current_status,
            "active_ps_id": active_parameter_set_id,
            "last_rec_id": last_recommendation_id,
            "now": datetime.now(timezone.utc),
            "notes": notes,
        },
    )
    log.info("DB upsert active_decision: %s -> %s", combo_key, current_status)


def db_load_active_decisions(session: Session) -> dict[str, Any]:
    """导出全量 active decisions，返回与文件 registry 兼容的 dict 格式.

    Returns
    -------
    dict  格式:
        {
          "generated_at": "...",
          "decisions": [
            {"family": ..., "timeframe": ..., "combo_key": ..., ...},
            ...
          ]
        }
    """
    rows = session.execute(
        text("""
            SELECT family, symbol, timeframe, combo_key,
                   current_status, active_parameter_set_id,
                   last_recommendation_id, last_updated_at, notes
            FROM governance.active_decisions
            ORDER BY family, timeframe
        """)
    ).fetchall()

    decisions = []
    for row in rows:
        decisions.append({
            "family": row.family,
            "symbol": row.symbol,
            "timeframe": row.timeframe,
            "combo_key": row.combo_key,
            "current_status": row.current_status,
            "active_parameter_set_id": row.active_parameter_set_id,
            "last_recommendation_id": row.last_recommendation_id,
            "last_updated_at": row.last_updated_at.isoformat() if row.last_updated_at else None,
            "notes": row.notes,
        })

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "decisions": decisions,
    }


# =====================================================================
# 工具函数
# =====================================================================


def _rec_row_to_dict(row: Any) -> dict[str, Any]:
    """将 recommendation SQL 结果行转换为与文件 registry 兼容的 dict."""
    d: dict[str, Any] = {
        "recommendation_id": row.recommendation_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "family": row.family,
        "symbol": row.symbol,
        "timeframe": row.timeframe,
        "recommendation_type": row.recommendation_type,
        "target_parameter_set_id": row.target_parameter_set_id,
        "confidence": row.confidence,
        "reason": row.reason,
        "evidence_bundle_ref": row.evidence_bundle_ref,
        "status": row.status,
    }
    # 可选字段仅在有值时写入，保持与文件格式一致
    if row.approved_by:
        d["approved_by"] = row.approved_by
    if row.approved_at:
        d["approved_at"] = row.approved_at.isoformat()
    if row.review_notes:
        d["review_notes"] = row.review_notes
    if row.rejected_by:
        d["rejected_by"] = row.rejected_by
    if row.rejected_at:
        d["rejected_at"] = row.rejected_at.isoformat()
    if row.superseded_by:
        d["superseded_by"] = row.superseded_by
    if row.superseded_at:
        d["superseded_at"] = row.superseded_at.isoformat()
    if row.superseded_by_recommendation_id:
        d["superseded_by_recommendation_id"] = row.superseded_by_recommendation_id
    return d
