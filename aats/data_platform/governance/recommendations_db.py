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

from ._db_util import (
    ADVISORY_LOCK_KEYS,
    VALID_REC_STATUSES,
    VALID_REC_TYPES,
    parse_dt,
)
from ._exceptions import DBConflictError

log = logging.getLogger(__name__)

_RECOMMENDATION_TRANSITIONS = {
    "draft": frozenset({"approved", "rejected", "superseded"}),
    "approved": frozenset({"superseded"}),
    "rejected": frozenset(),
    "superseded": frozenset(),
}
_RECOMMENDATION_SCOPE_LOCK_KEY = ADVISORY_LOCK_KEYS[
    "recommendation_scope_write"
]


def _validate_recommendation_transition(
    *,
    current_statuses: str | tuple[str, ...] | list[str] | None,
    new_status: str,
) -> None:
    if current_statuses is None:
        raise ValueError("recommendation_transition_requires_expected_status")
    statuses = (
        (current_statuses,)
        if isinstance(current_statuses, str)
        else tuple(current_statuses)
    )
    if not statuses or any(status not in VALID_REC_STATUSES for status in statuses):
        raise ValueError("recommendation_transition_expected_status_invalid")
    if any(
        new_status not in _RECOMMENDATION_TRANSITIONS[current]
        for current in statuses
    ):
        raise ValueError(
            "recommendation_transition_not_allowed: "
            f"{sorted(statuses)}->{new_status}"
        )


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
    source_round_id: str | None = None,
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
    """Insert a recommendation or verify an identity-equivalent retry.

    Business content is insert-once.  Approval/rejection/supersession must use
    the dedicated CAS transition functions below, so an old approval cannot be
    rebound to a new target or evidence reference by an UPSERT.
    """
    if status not in VALID_REC_STATUSES:
        raise ValueError(
            f"非法 recommendation status: {status!r}，合法值: "
            f"{sorted(VALID_REC_STATUSES)}"
        )
    if status != "draft":
        raise ValueError(
            "recommendation 初始 status 只能为 'draft'；"
            "approval/rejection/supersession 必须使用专用 CAS 转移"
        )
    if any(
        value is not None
        for value in (
            approved_by,
            approved_at,
            rejected_by,
            rejected_at,
            superseded_by,
            superseded_at,
            superseded_by_recommendation_id,
        )
    ):
        raise ValueError("draft recommendation 不能携带终态审计字段")
    if recommendation_type not in VALID_REC_TYPES:
        raise ValueError(
            f"非法 recommendation_type: {recommendation_type!r}，合法值: {sorted(VALID_REC_TYPES)}"
        )

    result = session.execute(
        text("""
            INSERT INTO governance.recommendations
                (recommendation_id, family, symbol, timeframe,
                 recommendation_type, target_parameter_set_id,
                 source_round_id,
                 confidence, reason, evidence_bundle_ref,
                 status, approved_by, approved_at, review_notes,
                 rejected_by, rejected_at,
                 superseded_by, superseded_at, superseded_by_recommendation_id,
                 created_at)
            VALUES
                (:rec_id, :family, :symbol, :timeframe,
                 :rec_type, :target_ps_id,
                 :source_round_id,
                 :confidence, :reason, :evidence_ref,
                 :status, :approved_by, :approved_at, :review_notes,
                 :rejected_by, :rejected_at,
                 :superseded_by, :superseded_at, :superseded_by_rec_id,
                 :created_at)
            ON CONFLICT (recommendation_id) DO UPDATE SET
                recommendation_id = governance.recommendations.recommendation_id
            WHERE governance.recommendations.family IS NOT DISTINCT FROM EXCLUDED.family
              AND governance.recommendations.symbol IS NOT DISTINCT FROM EXCLUDED.symbol
              AND governance.recommendations.timeframe IS NOT DISTINCT FROM EXCLUDED.timeframe
              AND governance.recommendations.recommendation_type IS NOT DISTINCT FROM EXCLUDED.recommendation_type
              AND governance.recommendations.target_parameter_set_id IS NOT DISTINCT FROM EXCLUDED.target_parameter_set_id
              AND governance.recommendations.source_round_id IS NOT DISTINCT FROM EXCLUDED.source_round_id
              AND governance.recommendations.confidence IS NOT DISTINCT FROM EXCLUDED.confidence
              AND governance.recommendations.reason IS NOT DISTINCT FROM EXCLUDED.reason
              AND governance.recommendations.evidence_bundle_ref IS NOT DISTINCT FROM EXCLUDED.evidence_bundle_ref
            RETURNING recommendation_id
        """),
        {
            "rec_id": recommendation_id,
            "family": family,
            "symbol": symbol,
            "timeframe": timeframe.lower(),
            "rec_type": recommendation_type,
            "target_ps_id": target_parameter_set_id,
            "source_round_id": source_round_id,
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
    if result.fetchone() is None:
        raise DBConflictError("recommendation_immutable_identity_conflict")
    log.info("DB insert/verify recommendation: %s (%s/%s, requested_status=%s)",
             recommendation_id, family, timeframe, status)


def db_insert_recommendation_superseding_drafts(
    session: Session,
    *,
    recommendation: dict[str, Any],
) -> list[str]:
    """Insert one draft and supersede prior same-scope drafts atomically."""

    family = recommendation.get("family")
    symbol = recommendation.get("symbol", "BTC-USDT-SWAP")
    timeframe = str(recommendation.get("timeframe") or "").lower()
    recommendation_type = recommendation.get("recommendation_type")
    recommendation_id = recommendation.get("recommendation_id")
    if not all(
        isinstance(value, str) and value.strip()
        for value in (
            family,
            symbol,
            timeframe,
            recommendation_type,
            recommendation_id,
        )
    ):
        raise ValueError("recommendation_atomic_insert_identity_invalid")

    scope_key = "|".join(
        (str(family), str(symbol), timeframe, str(recommendation_type))
    )
    session.execute(
        text("SELECT pg_advisory_xact_lock(:namespace, hashtext(:scope_key))"),
        {"namespace": _RECOMMENDATION_SCOPE_LOCK_KEY, "scope_key": scope_key},
    )
    db_upsert_recommendation(
        session,
        recommendation_id=str(recommendation_id),
        family=str(family),
        timeframe=timeframe,
        recommendation_type=str(recommendation_type),
        confidence=str(recommendation.get("confidence") or "low"),
        reason=str(recommendation.get("reason") or ""),
        symbol=str(symbol),
        target_parameter_set_id=recommendation.get("target_parameter_set_id"),
        source_round_id=recommendation.get("source_round_id"),
        evidence_bundle_ref=recommendation.get("evidence_bundle_ref"),
        status="draft",
        created_at=recommendation.get("created_at"),
    )
    canonical_new = session.execute(
        text(
            "SELECT status FROM governance.recommendations "
            "WHERE recommendation_id = :rec_id FOR UPDATE"
        ),
        {"rec_id": recommendation_id},
    ).fetchone()
    if canonical_new is None or canonical_new.status != "draft":
        raise DBConflictError("recommendation_lifecycle_conflict")

    prior_rows = session.execute(
        text(
            """
            SELECT recommendation_id
            FROM governance.recommendations
            WHERE family = :family
              AND symbol = :symbol
              AND timeframe = :timeframe
              AND recommendation_type = :recommendation_type
              AND status = 'draft'
              AND recommendation_id != :recommendation_id
            FOR UPDATE
            """
        ),
        {
            "family": family,
            "symbol": symbol,
            "timeframe": timeframe,
            "recommendation_type": recommendation_type,
            "recommendation_id": recommendation_id,
        },
    ).fetchall()
    prior_ids = [str(row.recommendation_id) for row in prior_rows]
    if prior_ids:
        id_params = {
            f"old_id_{index}": prior_id
            for index, prior_id in enumerate(prior_ids)
        }
        placeholders = ", ".join(f":{key}" for key in id_params)
        session.execute(
            text(
                f"""
                UPDATE governance.recommendations
                SET status = 'superseded',
                    superseded_by = 'system',
                    superseded_at = :superseded_at,
                    superseded_by_recommendation_id = :new_rec_id
                WHERE recommendation_id IN ({placeholders})
                  AND status = 'draft'
                """
            ),
            {
                **id_params,
                "superseded_at": parse_dt(recommendation.get("created_at"))
                or datetime.now(timezone.utc),
                "new_rec_id": recommendation_id,
            },
        )
    return prior_ids


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
    expected_current_status: str | tuple[str, ...] | None = None,
    expected_identity: dict[str, Any] | None = None,
) -> bool:
    """更新 recommendation 审批状态.

    Parameters
    ----------
    expected_current_status:
        若不为 None，则在 SQL 中加 ``WHERE status IN (...)`` 守卫，用于检测
        并发审批竞态：两个 operator 同时点 approve 时，其中一个的 Python 端
        ``rec["status"] == "draft"`` 检查通过，但 DB 已经被先到的请求改写为
        ``approved``，此时 UPDATE 的 rowcount=0，本函数返回 False，API 层可以
        把这种情况映射成"状态已被他人改写"。
    expected_identity:
        可选的 recommendation 不可变身份快照。提供后，UPDATE 除状态 CAS 外
        还会以 ``IS NOT DISTINCT FROM`` 精确比较 family / symbol / timeframe /
        recommendation_type / target_parameter_set_id / source_round_id /
        evidence_bundle_ref。这样资格校验与状态写入之间即使同 ID 行被替换或
        改写，也不会把已校验的结论错误应用到另一条业务记录。

    Returns
    -------
    bool
        True 当且仅当 rowcount > 0（确实更新了一行）。False 表示 recommendation
        不存在，或 ``expected_current_status`` 过滤器不匹配。
    """
    if status not in VALID_REC_STATUSES:
        raise ValueError("recommendation_transition_status_invalid")
    _validate_recommendation_transition(
        current_statuses=expected_current_status,
        new_status=status,
    )

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

    where_parts = ["recommendation_id = :rec_id"]
    if expected_current_status is not None:
        if isinstance(expected_current_status, str):
            where_parts.append("status = :expected_status")
            params["expected_status"] = expected_current_status
        else:
            # tuple/list → IN (:st0, :st1, ...) 按位置绑定参数，避免字面量拼接
            placeholders: list[str] = []
            for idx, st in enumerate(expected_current_status):
                key = f"expected_status_{idx}"
                placeholders.append(f":{key}")
                params[key] = st
            where_parts.append(f"status IN ({', '.join(placeholders)})")

    if expected_identity is not None:
        identity_columns = (
            "family",
            "symbol",
            "timeframe",
            "recommendation_type",
            "target_parameter_set_id",
            "source_round_id",
            "confidence",
            "reason",
            "evidence_bundle_ref",
        )
        unknown = set(expected_identity) - set(identity_columns)
        if unknown:
            raise ValueError(
                "expected_identity 包含不受支持的字段: "
                f"{sorted(unknown)}"
            )
        missing = set(identity_columns) - set(expected_identity)
        if missing:
            raise ValueError(
                "expected_identity 缺少身份字段: "
                f"{sorted(missing)}"
            )
        for column in identity_columns:
            key = f"expected_identity_{column}"
            where_parts.append(f"{column} IS NOT DISTINCT FROM :{key}")
            params[key] = expected_identity[column]

    sql = (
        f"UPDATE governance.recommendations SET {', '.join(set_parts)} "
        f"WHERE {' AND '.join(where_parts)}"
    )
    result = session.execute(text(sql), params)
    updated = result.rowcount > 0
    if updated:
        log.info("DB update recommendation status: %s -> %s", recommendation_id, status)
    elif expected_current_status is not None or expected_identity is not None:
        log.warning(
            "DB update recommendation status skipped: %s expected_status=%s "
            "identity_bound=%s (row not updated, probably raced with another operator)",
            recommendation_id, expected_current_status,
            expected_identity is not None,
        )
    return updated


def db_transition_recommendation_status(
    session: Session,
    *,
    recommendation_id: str,
    new_status: str,
    expected_current_status: str | tuple[str, ...] | list[str],
    actor: str,
    at: str | datetime | None = None,
    notes: str | None = None,
    superseded_by_recommendation_id: str | None = None,
) -> bool:
    """状态转移 + CAS 的统一入口，供 approve / reject / supersede handler 使用。

    与 ``db_update_recommendation_status`` 相比，这一版专门面向"状态流转"语义：

    - 调用方不用手动挑 approved_by / rejected_by / superseded_by 字段；传 ``actor``
      和 ``at``，函数根据 ``new_status`` 自动写入对应列：

      * ``new_status="approved"``  → ``approved_by``、``approved_at``
      * ``new_status="rejected"``  → ``rejected_by``、``rejected_at``
      * ``new_status="superseded"``→ ``superseded_by``、``superseded_at``（可选
        ``superseded_by_recommendation_id``）
      * 其它 status（如退回 draft）仅更新 ``status``

    - ``expected_current_status`` 是 CAS 守卫：当前状态必须在该集合内 UPDATE
      才会命中。未命中返回 ``False``，调用方可以把这种情况映射为 HTTP 409。

    Parameters
    ----------
    at:
        操作时间戳；``None`` 时使用当前 UTC 时间。支持 ISO8601 字符串或
        ``datetime``。
    notes:
        可选备注，写入 ``review_notes`` 列。

    Returns
    -------
    bool
        ``True`` 当且仅当 UPDATE ``rowcount > 0``；``False`` 表示 recommendation
        不存在 或 当前状态不在 ``expected_current_status`` 里（并发竞态 / 非法转移）。
    """
    if new_status not in VALID_REC_STATUSES:
        raise ValueError(
            f"非法 recommendation status: {new_status!r}，"
            f"合法值: {sorted(VALID_REC_STATUSES)}"
        )
    _validate_recommendation_transition(
        current_statuses=expected_current_status,
        new_status=new_status,
    )

    at_value = parse_dt(at) if at is not None else datetime.now(timezone.utc)

    set_parts: list[str] = ["status = :status"]
    params: dict[str, Any] = {
        "rec_id": recommendation_id,
        "status": new_status,
    }

    if new_status == "approved":
        set_parts.append("approved_by = :actor")
        set_parts.append("approved_at = :at")
        params["actor"] = actor
        params["at"] = at_value
    elif new_status == "rejected":
        set_parts.append("rejected_by = :actor")
        set_parts.append("rejected_at = :at")
        params["actor"] = actor
        params["at"] = at_value
    elif new_status == "superseded":
        set_parts.append("superseded_by = :actor")
        set_parts.append("superseded_at = :at")
        params["actor"] = actor
        params["at"] = at_value
        if superseded_by_recommendation_id is not None:
            set_parts.append("superseded_by_recommendation_id = :superseded_by_rec_id")
            params["superseded_by_rec_id"] = superseded_by_recommendation_id

    if notes is not None:
        set_parts.append("review_notes = :review_notes")
        params["review_notes"] = notes

    if isinstance(expected_current_status, str):
        where_status_clause = "status = :expected_status"
        params["expected_status"] = expected_current_status
    else:
        placeholders: list[str] = []
        for idx, st in enumerate(expected_current_status):
            key = f"expected_status_{idx}"
            placeholders.append(f":{key}")
            params[key] = st
        where_status_clause = f"status IN ({', '.join(placeholders)})"

    sql = (
        f"UPDATE governance.recommendations SET {', '.join(set_parts)} "
        f"WHERE recommendation_id = :rec_id AND {where_status_clause}"
    )
    result = session.execute(text(sql), params)
    updated = result.rowcount > 0
    if updated:
        log.info(
            "DB transition recommendation: %s -> %s (actor=%s)",
            recommendation_id, new_status, actor,
        )
    else:
        log.warning(
            "DB transition recommendation miss: %s expected=%s new=%s "
            "(行不存在或状态已被其他进程抢先改写)",
            recommendation_id, expected_current_status, new_status,
        )
    return updated


_REC_SELECT_COLUMNS = """\
recommendation_id, family, symbol, timeframe,
recommendation_type, target_parameter_set_id,
source_round_id,
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


def db_get_recommendation(
    session: Session,
    recommendation_id: str,
) -> dict[str, Any] | None:
    """按 recommendation_id 查询单条（``db_find_recommendation`` 的语义别名）。

    新代码（尤其是 rdp_routes.py 的 approve / reject / supersede handler）推荐
    用这个更直白的名字；``db_find_recommendation`` 保留以兼容已有调用。
    """
    return db_find_recommendation(session, recommendation_id)


def db_get_recommendation_for_update(
    session: Session,
    recommendation_id: str,
) -> dict[str, Any] | None:
    """Load one recommendation under a row lock for a capital transaction."""
    row = session.execute(
        text(f"""
            SELECT {_REC_SELECT_COLUMNS}
            FROM governance.recommendations
            WHERE recommendation_id = :rec_id
            FOR UPDATE
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


def db_find_recommendations_for_evidence_bundle(
    session: Session,
    *,
    evidence_bundle_ref: str,
) -> list[dict[str, Any]]:
    """Load the complete recommendation set published by one Phase 6 round."""

    rows = session.execute(
        text(
            f"""
            SELECT {_REC_SELECT_COLUMNS}
            FROM governance.recommendations
            WHERE evidence_bundle_ref = :evidence_bundle_ref
            ORDER BY recommendation_id ASC
            """
        ),
        {"evidence_bundle_ref": evidence_bundle_ref},
    ).fetchall()
    return [_rec_row_to_dict(row) for row in rows]


def _build_recommendations_filter(
    *,
    status: str | None,
    family: str | None,
    timeframe: str | None,
    recommendation_type: str | None,
) -> tuple[str, dict[str, Any]]:
    """list / count 共用的 WHERE 子句构造。返回 ``(where_clause, params)``。"""
    where_parts: list[str] = []
    params: dict[str, Any] = {}
    if status is not None:
        where_parts.append("status = :status")
        params["status"] = status
    if family is not None:
        where_parts.append("family = :family")
        params["family"] = family
    if timeframe is not None:
        where_parts.append("timeframe = :timeframe")
        params["timeframe"] = timeframe.lower()
    if recommendation_type is not None:
        where_parts.append("recommendation_type = :rec_type")
        params["rec_type"] = recommendation_type
    where_clause = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""
    return where_clause, params


def db_list_recommendations(
    session: Session,
    *,
    status: str | None = None,
    family: str | None = None,
    timeframe: str | None = None,
    recommendation_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """分页列出 recommendations，按 ``created_at DESC`` 排序。

    ``db_find_recommendations`` 的增强版，主要区别：

    - 额外支持 ``recommendation_type`` 过滤
    - 支持 ``limit`` / ``offset`` 分页，避免把全部 recommendations 一次拉到内存
    """
    if limit < 0:
        raise ValueError(f"limit 必须 >= 0: {limit}")
    if offset < 0:
        raise ValueError(f"offset 必须 >= 0: {offset}")

    where_clause, params = _build_recommendations_filter(
        status=status, family=family, timeframe=timeframe,
        recommendation_type=recommendation_type,
    )
    params["limit"] = limit
    params["offset"] = offset
    sql = f"""
        SELECT {_REC_SELECT_COLUMNS}
        FROM governance.recommendations
        {where_clause}
        ORDER BY created_at DESC
        LIMIT :limit OFFSET :offset
    """
    rows = session.execute(text(sql), params).fetchall()
    return [_rec_row_to_dict(r) for r in rows]


def db_count_recommendations(
    session: Session,
    *,
    status: str | None = None,
    family: str | None = None,
    timeframe: str | None = None,
    recommendation_type: str | None = None,
) -> int:
    """统计符合过滤条件的 recommendations 数量（供分页 total 使用）。"""
    where_clause, params = _build_recommendations_filter(
        status=status, family=family, timeframe=timeframe,
        recommendation_type=recommendation_type,
    )
    sql = f"SELECT COUNT(*) AS cnt FROM governance.recommendations{where_clause}"
    row = session.execute(text(sql), params).fetchone()
    if row is None:
        return 0
    return int(row.cnt)


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
    preserve_existing_active_parameter_set: bool = False,
    last_recommendation_id: str | None = None,
    notes: str | None = None,
) -> bool:
    """UPSERT 一条 active decision 记录.

    ``preserve_existing_active_parameter_set`` is for non-promotion decision
    rounds.  The value is resolved only after the shared combo lock and row
    lock are held, so keep/pause/review cannot erase or race an applied set.
    """
    from .active_params_db import db_try_acquire_parameter_apply_lock

    timeframe_norm = timeframe.lower()
    combo_key = f"{family}_{timeframe_norm}"
    if not db_try_acquire_parameter_apply_lock(
        session,
        family=family,
        timeframe=timeframe_norm,
    ):
        raise RuntimeError(
            f"active decision mutation lock busy for {combo_key}"
        )
    existing = db_get_active_decision_for_update(
        session,
        family=family,
        timeframe=timeframe_norm,
    )
    if (
        preserve_existing_active_parameter_set
        and active_parameter_set_id is not None
    ):
        raise ValueError(
            "active_decision_preserve_parameter_set_cannot_replace"
        )
    # A safety pause is sticky.  Automated decision-round snapshots are not an
    # authorization to clear it; only a future explicit operator reconciliation
    # API may perform that transition.
    if (
        isinstance(existing, dict)
        and existing.get("current_status") == "pause"
        and current_status != "pause"
    ):
        log.warning(
            "active_decision pause preserved for %s; rejected automatic transition to %s",
            combo_key,
            current_status,
        )
        return False
    effective_active_parameter_set_id = active_parameter_set_id
    if preserve_existing_active_parameter_set:
        effective_active_parameter_set_id = (
            existing.get("active_parameter_set_id")
            if isinstance(existing, dict)
            else None
        )
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
            "timeframe": timeframe_norm,
            "combo_key": combo_key,
            "status": current_status,
            "active_ps_id": effective_active_parameter_set_id,
            "last_rec_id": last_recommendation_id,
            "now": datetime.now(timezone.utc),
            "notes": notes,
        },
    )
    log.info("DB upsert active_decision: %s -> %s", combo_key, current_status)
    return True


def db_set_combo_pause(
    session: Session,
    *,
    family: str,
    timeframe: str,
    reason: str,
) -> bool:
    """Bug 8 Layer 2: 把 combo 的 active_decision.current_status 设为 'pause'.

    只更新 ``current_status``/``notes``/``last_updated_at``，**不动**
    ``active_parameter_set_id`` 和 ``last_recommendation_id`` (那些是治理决策层
    的真相源，不应被 pause 动作改写)。

    触发路径：auto-rollback 无合法 target 时的保护兜底 (规则 2 的 deprecated
    时间门控拒绝 + 没有其他 alternate)，通过 combo-level pause 阻止未来新 apply
    (pre_apply_gate decision_consistency check 会 block severity=block)，
    但不强制切换当前 live 参数。

    Returns
    -------
    bool
        True: UPDATE 成功（combo 有既存 active_decision 行）
        False: combo 没有 active_decision 行（UPDATE 0 行），调用方应该 log
        warning 而不是创建新 row——pause 是对已存在决策的降级，不是凭空写入。
    """
    from .active_params_db import db_try_acquire_parameter_apply_lock

    if not db_try_acquire_parameter_apply_lock(
        session,
        family=family,
        timeframe=timeframe,
    ):
        raise RuntimeError(
            f"active decision mutation lock busy for {family}_{timeframe.lower()}"
        )
    # Lock the mutable row before changing it so every decision writer has the
    # same lock order: combo advisory lock, then active_decisions row lock.
    existing = db_get_active_decision_for_update(
        session,
        family=family,
        timeframe=timeframe,
    )
    if existing is None:
        log.warning(
            "db_set_combo_pause: combo family=%s timeframe=%s 无 active_decision 记录",
            family,
            timeframe.lower(),
        )
        return False
    result = session.execute(
        text("""
            UPDATE governance.active_decisions
            SET current_status = 'pause',
                last_updated_at = :now,
                notes = :reason
            WHERE family = :family AND timeframe = :tf
        """),
        {
            "family": family,
            "tf": timeframe.lower(),
            "now": datetime.now(timezone.utc),
            "reason": reason,
        },
    )
    rowcount = result.rowcount or 0
    if rowcount > 0:
        log.info(
            "combo_paused family=%s timeframe=%s reason=%s",
            family, timeframe.lower(), reason,
        )
        return True
    log.warning(
        "db_set_combo_pause: combo family=%s timeframe=%s 无 active_decision 记录，"
        "跳过 pause 写入 (可能是首次 apply 未产生决策层记录)",
        family, timeframe.lower(),
    )
    return False


def db_get_active_decision_for_update(
    session: Session,
    *,
    family: str,
    timeframe: str,
) -> dict[str, Any] | None:
    """Lock and return the canonical decision for one parameter combo.

    Callers must already hold the combo mutation advisory lock.  The row lock
    makes the decision re-read and the subsequent capital mutation part of the
    same transaction boundary; a pause cannot be hidden by an older Gate
    snapshot.
    """
    row = session.execute(
        text(
            """
            SELECT family, symbol, timeframe, combo_key,
                   current_status, active_parameter_set_id,
                   last_recommendation_id, last_updated_at, notes
            FROM governance.active_decisions
            WHERE family = :family AND timeframe = :timeframe
            FOR UPDATE
            """
        ),
        {"family": family, "timeframe": timeframe.lower()},
    ).fetchone()
    if row is None:
        return None
    return {
        "family": row.family,
        "symbol": row.symbol,
        "timeframe": row.timeframe,
        "combo_key": row.combo_key,
        "current_status": row.current_status,
        "active_parameter_set_id": row.active_parameter_set_id,
        "last_recommendation_id": row.last_recommendation_id,
        "last_updated_at": (
            row.last_updated_at.isoformat() if row.last_updated_at else None
        ),
        "notes": row.notes,
    }


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
        "created_at": (
            row.created_at.astimezone(timezone.utc).isoformat()
            if row.created_at
            else None
        ),
        "family": row.family,
        "symbol": row.symbol,
        "timeframe": row.timeframe,
        "recommendation_type": row.recommendation_type,
        "target_parameter_set_id": row.target_parameter_set_id,
        "source_round_id": row.source_round_id,
        "confidence": row.confidence,
        "reason": row.reason,
        "evidence_bundle_ref": row.evidence_bundle_ref,
        "status": row.status,
    }
    # 可选字段仅在有值时写入，保持与文件格式一致
    if row.approved_by:
        d["approved_by"] = row.approved_by
    if row.approved_at:
        d["approved_at"] = row.approved_at.astimezone(timezone.utc).isoformat()
    if row.review_notes:
        d["review_notes"] = row.review_notes
    if row.rejected_by:
        d["rejected_by"] = row.rejected_by
    if row.rejected_at:
        d["rejected_at"] = row.rejected_at.astimezone(timezone.utc).isoformat()
    if row.superseded_by:
        d["superseded_by"] = row.superseded_by
    if row.superseded_at:
        d["superseded_at"] = row.superseded_at.astimezone(timezone.utc).isoformat()
    if row.superseded_by_recommendation_id:
        d["superseded_by_recommendation_id"] = row.superseded_by_recommendation_id
    return d
