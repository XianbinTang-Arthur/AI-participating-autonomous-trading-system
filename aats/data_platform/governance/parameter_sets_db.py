"""Parameter Sets DB — governance.parameter_sets 读写层.

将 parameter_registry.json 中的 parameter_sets 列表持久化到 Postgres。
提供 5 个函数，对齐 parameter_registry.py 的文件操作语义。

依赖:
  - governance.parameter_sets 表 (ParameterSetModel in rdp_models.py)
  - aats.data_platform.db 的连接管理
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from ._db_util import VALID_PS_STATUSES, json_dumps, parse_dt
from ._exceptions import DBConflictError
from .parameter_identity import (
    parameter_set_immutable_identity,
    parameter_values_fingerprint,
)

log = logging.getLogger(__name__)

_VALID_PARAMETER_SET_INSERT_STATUSES = frozenset({"draft", "candidate"})
_GENERIC_PARAMETER_SET_TRANSITIONS = {
    "draft": frozenset({"candidate", "frozen", "deprecated"}),
    "candidate": frozenset({"frozen", "deprecated"}),
    "frozen": frozenset({"deprecated"}),
    "released": frozenset(),
    "deprecated": frozenset(),
}


# ── UPSERT (写入 / 更新参数集) ──────────────────────────────────────

def db_upsert_parameter_set(
    session: Session,
    *,
    parameter_set_id: str,
    family: str,
    timeframe: str,
    values: dict[str, Any],
    status: str = "draft",
    symbol: str = "BTC-USDT-SWAP",
    source_round_id: str | None = None,
    source_phase: str | None = None,
    dataset_version: str = "v1.0",
    confidence: str | None = None,
    created_at: str | None = None,
    frozen_at: str | None = None,
    deprecated_at: str | None = None,
    notes: str | None = None,
) -> None:
    """Insert one parameter set or verify an identity-equivalent retry.

    ``parameter_set_id`` is an insert-once business identity.  Lifecycle
    changes use :func:`db_update_parameter_set_status`; an idempotent retry
    must never regress status or replace the values/source behind old evidence.
    """
    if status not in _VALID_PARAMETER_SET_INSERT_STATUSES:
        raise ValueError(
            f"非法 parameter_set 初始 status: {status!r}，合法值: "
            f"{sorted(_VALID_PARAMETER_SET_INSERT_STATUSES)}"
        )
    if frozen_at is not None or deprecated_at is not None:
        raise ValueError(
            "新建 parameter set 不能携带 frozen_at/deprecated_at；"
            "生命周期审计字段必须由专用状态事务产生"
        )

    values_identity_sha256 = parameter_values_fingerprint(values)

    result = session.execute(
        text("""
            INSERT INTO governance.parameter_sets
                (parameter_set_id, family, symbol, timeframe,
                 source_round_id, source_phase, dataset_version,
                 values, typed_json_identity_sha256, confidence, status,
                 created_at, frozen_at, deprecated_at, notes)
            VALUES
                (:ps_id, :family, :symbol, :timeframe,
                 :src_round, :src_phase, :ds_ver,
                 CAST(:vals AS jsonb), :typed_json_identity_sha256,
                 :confidence, :status,
                 :created_at, :frozen_at, :deprecated_at, :notes)
            ON CONFLICT (parameter_set_id) DO UPDATE SET
                typed_json_identity_sha256 = COALESCE(
                    governance.parameter_sets.typed_json_identity_sha256,
                    EXCLUDED.typed_json_identity_sha256
                )
            WHERE governance.parameter_sets.family IS NOT DISTINCT FROM EXCLUDED.family
              AND governance.parameter_sets.symbol IS NOT DISTINCT FROM EXCLUDED.symbol
              AND governance.parameter_sets.timeframe IS NOT DISTINCT FROM EXCLUDED.timeframe
              AND governance.parameter_sets.source_round_id IS NOT DISTINCT FROM EXCLUDED.source_round_id
              AND governance.parameter_sets.source_phase IS NOT DISTINCT FROM EXCLUDED.source_phase
              AND governance.parameter_sets.dataset_version IS NOT DISTINCT FROM EXCLUDED.dataset_version
              AND governance.parameter_sets.values::text IS NOT DISTINCT FROM EXCLUDED.values::text
              AND (
                    governance.parameter_sets.typed_json_identity_sha256 IS NULL
                    OR governance.parameter_sets.typed_json_identity_sha256
                       = EXCLUDED.typed_json_identity_sha256
              )
              AND governance.parameter_sets.confidence IS NOT DISTINCT FROM EXCLUDED.confidence
            RETURNING parameter_set_id
        """),
        {
            "ps_id": parameter_set_id,
            "family": family,
            "symbol": symbol,
            "timeframe": timeframe.lower(),
            "src_round": source_round_id,
            "src_phase": source_phase,
            "ds_ver": dataset_version,
            "vals": json_dumps(values),
            "typed_json_identity_sha256": values_identity_sha256,
            "confidence": confidence,
            "status": status,
            "created_at": parse_dt(created_at) or datetime.now(timezone.utc),
            "frozen_at": parse_dt(frozen_at),
            "deprecated_at": parse_dt(deprecated_at),
            "notes": notes,
        },
    )
    if result.fetchone() is None:
        raise DBConflictError("parameter_set_immutable_identity_conflict")
    log.info("DB insert/verify parameter_set: %s (%s/%s, requested_status=%s)",
             parameter_set_id, family, timeframe, status)


# ── UPDATE 状态 ─────────────────────────────────────────────────────

def db_update_parameter_set_status(
    session: Session,
    parameter_set_id: str,
    *,
    status: str,
    expected_current_status: str,
    frozen_at: str | None = None,
    deprecated_at: str | None = None,
    notes: str | None = None,
) -> bool:
    """按期望当前状态推进 parameter_set 生命周期.

    ``expected_current_status`` 把读到的状态绑定到写入，避免自动导入把已经由
    apply 晋级为 ``released`` 的候选异步覆盖成 ``deprecated``。返回 ``False``
    表示记录不存在或 CAS 已被其他事务抢先推进。
    """
    if status not in VALID_PS_STATUSES:
        raise ValueError(
            f"非法 parameter_set status: {status!r}，合法值: "
            f"{sorted(VALID_PS_STATUSES)}"
        )
    if expected_current_status not in VALID_PS_STATUSES:
        raise ValueError(
            f"非法 expected_current_status: {expected_current_status!r}，合法值: "
            f"{sorted(VALID_PS_STATUSES)}"
        )
    if status not in _GENERIC_PARAMETER_SET_TRANSITIONS[expected_current_status]:
        raise ValueError(
            "parameter_set_transition_not_allowed: "
            f"{expected_current_status}->{status}; released 只能由受控 apply/rollback "
            "资本事务推进"
        )

    # 构建动态 SET 子句
    set_parts = ["status = :status"]
    params: dict[str, Any] = {"ps_id": parameter_set_id, "status": status}

    if frozen_at is not None:
        set_parts.append("frozen_at = :frozen_at")
        params["frozen_at"] = parse_dt(frozen_at) or datetime.now(timezone.utc)
    if deprecated_at is not None:
        set_parts.append("deprecated_at = :deprecated_at")
        params["deprecated_at"] = parse_dt(deprecated_at) or datetime.now(timezone.utc)
    if notes is not None:
        set_parts.append("notes = :notes")
        params["notes"] = notes

    where_parts = ["parameter_set_id = :ps_id"]
    where_parts.append("status = :expected_current_status")
    params["expected_current_status"] = expected_current_status
    sql = (
        f"UPDATE governance.parameter_sets SET {', '.join(set_parts)} "
        f"WHERE {' AND '.join(where_parts)}"
    )
    result = session.execute(text(sql), params)
    updated = result.rowcount > 0
    if updated:
        log.info("DB update parameter_set status: %s -> %s", parameter_set_id, status)
    return updated


def db_publish_parameter_set_candidates(
    session: Session,
    *,
    expected_parameter_sets: list[dict[str, Any]],
) -> int:
    """Atomically expose one complete imported round as candidates.

    Importers first persist every member as ``draft``.  This function then
    locks and identity-checks the complete expected set before moving all draft
    members to ``candidate`` in one transaction.  Decision readers can
    therefore observe either zero or the full round, never a crash-recovery
    prefix.  An exact retry after normal lifecycle progression is idempotent:
    candidate/frozen/released/deprecated rows are verified but never regressed.
    A draft mixed with terminal lifecycle state fails closed because official
    atomic publication cannot create that state.  ``released`` remains owned
    by the apply/rollback capital path.
    """

    if not expected_parameter_sets:
        raise ValueError("parameter_candidate_publication_empty")
    expected_by_id: dict[str, dict[str, Any]] = {}
    for parameter_set in expected_parameter_sets:
        parameter_set_id = parameter_set.get("parameter_set_id")
        if not isinstance(parameter_set_id, str) or not parameter_set_id:
            raise ValueError("parameter_candidate_publication_id_invalid")
        if parameter_set_id in expected_by_id:
            raise ValueError("parameter_candidate_publication_id_duplicate")
        expected_by_id[parameter_set_id] = parameter_set

    params = {
        f"ps_id_{index}": parameter_set_id
        for index, parameter_set_id in enumerate(expected_by_id)
    }
    placeholders = ", ".join(f":{key}" for key in params)
    rows = session.execute(
        text(
            f"""
            SELECT parameter_set_id, family, symbol, timeframe,
                   source_round_id, source_phase, dataset_version,
                   values AS param_values, confidence, status,
                   created_at, frozen_at, deprecated_at, notes
            FROM governance.parameter_sets
            WHERE parameter_set_id IN ({placeholders})
            FOR UPDATE
            """
        ),
        params,
    ).fetchall()
    if len(rows) != len(expected_by_id):
        raise DBConflictError("parameter_candidate_publication_incomplete")

    stored_by_id = {
        row.parameter_set_id: _row_to_dict(row)
        for row in rows
    }
    stored_statuses: set[str] = set()
    for parameter_set_id, expected in expected_by_id.items():
        stored = stored_by_id.get(parameter_set_id)
        if (
            stored is None
            or stored.get("status") not in VALID_PS_STATUSES
            or parameter_set_immutable_identity(stored)
            != parameter_set_immutable_identity(expected)
        ):
            raise DBConflictError("parameter_candidate_publication_identity_conflict")
        stored_statuses.add(str(stored["status"]))

    if "draft" in stored_statuses and not stored_statuses.issubset(
        {"draft", "candidate"}
    ):
        raise DBConflictError("parameter_candidate_publication_lifecycle_conflict")

    update_result = session.execute(
        text(
            f"""
            UPDATE governance.parameter_sets
            SET status = 'candidate'
            WHERE parameter_set_id IN ({placeholders})
              AND status = 'draft'
            """
        ),
        params,
    )
    return int(update_result.rowcount or 0)


def db_deprecate_superseded_candidate(
    session: Session,
    *,
    existing_parameter_set: dict[str, Any],
    replacement_parameter_set: dict[str, Any],
    deprecated_at: str,
    notes: str | None = None,
) -> bool:
    """Deprecate an old candidate only while its replacement is a candidate.

    The check and write share the capital-path combo advisory lock.  An apply,
    rollback, or concurrent lifecycle transition can therefore win first, but
    an importer can never remove the fallback candidate after the replacement
    has already progressed to a terminal/non-candidate state.
    """

    old_family = str(existing_parameter_set.get("family") or "").strip().lower()
    new_family = str(replacement_parameter_set.get("family") or "").strip().lower()
    old_timeframe = str(existing_parameter_set.get("timeframe") or "").strip().lower()
    new_timeframe = str(replacement_parameter_set.get("timeframe") or "").strip().lower()
    old_symbol = str(existing_parameter_set.get("symbol") or "").strip()
    new_symbol = str(replacement_parameter_set.get("symbol") or "").strip()
    old_id = existing_parameter_set.get("parameter_set_id")
    replacement_id = replacement_parameter_set.get("parameter_set_id")
    if (
        not isinstance(old_id, str)
        or not old_id
        or not isinstance(replacement_id, str)
        or not replacement_id
        or old_id == replacement_id
        or not old_family
        or old_family != new_family
        or not old_timeframe
        or old_timeframe != new_timeframe
        or not old_symbol
        or old_symbol != new_symbol
    ):
        raise ValueError("parameter_candidate_supersession_scope_invalid")

    from .active_params_db import db_try_acquire_parameter_apply_lock

    if not db_try_acquire_parameter_apply_lock(
        session,
        family=new_family,
        timeframe=new_timeframe,
    ):
        return False

    rows = session.execute(
        text(
            """
            SELECT parameter_set_id, family, symbol, timeframe,
                   source_round_id, source_phase, dataset_version,
                   values AS param_values, confidence, status,
                   created_at, frozen_at, deprecated_at, notes
            FROM governance.parameter_sets
            WHERE parameter_set_id IN (:old_id, :replacement_id)
            FOR UPDATE
            """
        ),
        {"old_id": old_id, "replacement_id": replacement_id},
    ).fetchall()
    if len(rows) != 2:
        raise DBConflictError("parameter_candidate_supersession_incomplete")
    stored_by_id = {row.parameter_set_id: _row_to_dict(row) for row in rows}
    stored_old = stored_by_id.get(old_id)
    stored_replacement = stored_by_id.get(replacement_id)
    if (
        stored_old is None
        or stored_replacement is None
        or parameter_set_immutable_identity(stored_old)
        != parameter_set_immutable_identity(existing_parameter_set)
        or parameter_set_immutable_identity(stored_replacement)
        != parameter_set_immutable_identity(replacement_parameter_set)
    ):
        raise DBConflictError("parameter_candidate_supersession_identity_conflict")
    if (
        stored_old.get("status") != "candidate"
        or stored_replacement.get("status") != "candidate"
    ):
        return False

    result = session.execute(
        text(
            """
            UPDATE governance.parameter_sets
            SET status = 'deprecated',
                deprecated_at = :deprecated_at,
                notes = COALESCE(:notes, notes)
            WHERE parameter_set_id = :old_id
              AND status = 'candidate'
            """
        ),
        {
            "old_id": old_id,
            "deprecated_at": parse_dt(deprecated_at),
            "notes": notes,
        },
    )
    return int(result.rowcount or 0) == 1


# ── SELECT 按条件查询 ───────────────────────────────────────────────

def db_find_parameter_sets(
    session: Session,
    *,
    family: str | None = None,
    timeframe: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """按条件查询 parameter sets."""
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
        SELECT parameter_set_id, family, symbol, timeframe,
               source_round_id, source_phase, dataset_version,
               values AS param_values, confidence, status,
               created_at, frozen_at, deprecated_at, notes
        FROM governance.parameter_sets
        {where_clause}
        ORDER BY created_at DESC
    """
    rows = session.execute(text(sql), params).fetchall()
    return [_row_to_dict(r) for r in rows]


# ── SELECT 单条 ─────────────────────────────────────────────────────

def db_get_parameter_set(
    session: Session,
    parameter_set_id: str,
) -> dict[str, Any] | None:
    """按 parameter_set_id 查询单条."""
    row = session.execute(
        text("""
            SELECT parameter_set_id, family, symbol, timeframe,
                   source_round_id, source_phase, dataset_version,
                   values AS param_values, confidence, status,
                   created_at, frozen_at, deprecated_at, notes
            FROM governance.parameter_sets
            WHERE parameter_set_id = :ps_id
        """),
        {"ps_id": parameter_set_id},
    ).fetchone()

    if row is None:
        return None
    return _row_to_dict(row)


# ── SELECT 全量（兼容文件 registry 格式）─────────────────────────────

def db_load_full_registry(session: Session) -> dict[str, Any]:
    """导出全量 parameter sets，返回与文件 registry 兼容的 dict 格式.

    Returns
    -------
    dict  格式:
        {
          "generated_at": "...",
          "parameter_sets": [
            {"parameter_set_id": "ps_xxx", "family": ..., "values": {...}, ...},
            ...
          ]
        }
    """
    rows = session.execute(
        text("""
            SELECT parameter_set_id, family, symbol, timeframe,
                   source_round_id, source_phase, dataset_version,
                   values AS param_values, confidence, status,
                   created_at, frozen_at, deprecated_at, notes
            FROM governance.parameter_sets
            ORDER BY created_at
        """)
    ).fetchall()

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "parameter_sets": [_row_to_dict(r) for r in rows],
    }


# ── 工具函数 ──────────────────────────────────────────────────────────

def _row_to_dict(row: Any) -> dict[str, Any]:
    """将 SQL 结果行转换为与文件 registry 兼容的 dict."""
    return {
        "parameter_set_id": row.parameter_set_id,
        "family": row.family,
        "symbol": row.symbol,
        "timeframe": row.timeframe,
        "source_round_id": row.source_round_id,
        "source_phase": row.source_phase,
        "dataset_version": row.dataset_version,
        "values": row.param_values,
        "confidence": row.confidence,
        "status": row.status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "frozen_at": row.frozen_at.isoformat() if row.frozen_at else None,
        "deprecated_at": row.deprecated_at.isoformat() if row.deprecated_at else None,
        "notes": row.notes,
    }
