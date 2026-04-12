"""Parameter Sets DB — governance.parameter_sets 读写层.

将 parameter_registry.json 中的 parameter_sets 列表持久化到 Postgres。
提供 5 个函数，对齐 parameter_registry.py 的文件操作语义。

依赖:
  - governance.parameter_sets 表 (ParameterSetModel in rdp_models.py)
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
    """UPSERT 一条 parameter set 记录.

    INSERT ... ON CONFLICT (parameter_set_id) DO UPDATE
    保证幂等。调用方应在同一个事务中使用。
    """
    session.execute(
        text("""
            INSERT INTO governance.parameter_sets
                (parameter_set_id, family, symbol, timeframe,
                 source_round_id, source_phase, dataset_version,
                 values, confidence, status,
                 created_at, frozen_at, deprecated_at, notes)
            VALUES
                (:ps_id, :family, :symbol, :timeframe,
                 :src_round, :src_phase, :ds_ver,
                 CAST(:vals AS jsonb), :confidence, :status,
                 :created_at, :frozen_at, :deprecated_at, :notes)
            ON CONFLICT (parameter_set_id) DO UPDATE SET
                family          = EXCLUDED.family,
                symbol          = EXCLUDED.symbol,
                timeframe       = EXCLUDED.timeframe,
                source_round_id = EXCLUDED.source_round_id,
                source_phase    = EXCLUDED.source_phase,
                dataset_version = EXCLUDED.dataset_version,
                values          = EXCLUDED.values,
                confidence      = EXCLUDED.confidence,
                status          = EXCLUDED.status,
                frozen_at       = EXCLUDED.frozen_at,
                deprecated_at   = EXCLUDED.deprecated_at,
                notes           = EXCLUDED.notes
        """),
        {
            "ps_id": parameter_set_id,
            "family": family,
            "symbol": symbol,
            "timeframe": timeframe.lower(),
            "src_round": source_round_id,
            "src_phase": source_phase,
            "ds_ver": dataset_version,
            "vals": _json_dumps(values),
            "confidence": confidence,
            "status": status,
            "created_at": _parse_dt(created_at) or datetime.now(timezone.utc),
            "frozen_at": _parse_dt(frozen_at),
            "deprecated_at": _parse_dt(deprecated_at),
            "notes": notes,
        },
    )
    log.info("DB upsert parameter_set: %s (%s/%s, status=%s)",
             parameter_set_id, family, timeframe, status)


# ── UPDATE 状态 ─────────────────────────────────────────────────────

def db_update_parameter_set_status(
    session: Session,
    parameter_set_id: str,
    *,
    status: str,
    frozen_at: str | None = None,
    deprecated_at: str | None = None,
    notes: str | None = None,
) -> bool:
    """更新 parameter_set 状态（freeze / deprecate）.

    Returns True 如果确实更新了一行。
    """
    # 构建动态 SET 子句
    set_parts = ["status = :status"]
    params: dict[str, Any] = {"ps_id": parameter_set_id, "status": status}

    if frozen_at is not None:
        set_parts.append("frozen_at = :frozen_at")
        params["frozen_at"] = _parse_dt(frozen_at) or datetime.now(timezone.utc)
    if deprecated_at is not None:
        set_parts.append("deprecated_at = :deprecated_at")
        params["deprecated_at"] = _parse_dt(deprecated_at) or datetime.now(timezone.utc)
    if notes is not None:
        set_parts.append("notes = :notes")
        params["notes"] = notes

    sql = f"UPDATE governance.parameter_sets SET {', '.join(set_parts)} WHERE parameter_set_id = :ps_id"
    result = session.execute(text(sql), params)
    updated = result.rowcount > 0
    if updated:
        log.info("DB update parameter_set status: %s -> %s", parameter_set_id, status)
    return updated


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

def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _parse_dt(val: str | None) -> datetime | None:
    """将 ISO 字符串解析为 datetime，None 原样返回."""
    if val is None:
        return None
    try:
        return datetime.fromisoformat(val)
    except (ValueError, TypeError):
        return None


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
