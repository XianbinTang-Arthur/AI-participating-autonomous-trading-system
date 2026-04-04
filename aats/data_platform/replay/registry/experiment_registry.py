"""Experiment registry: metadata and artifact tracking.

Phase 2 设计决策 §10：
- 每次实验必须可追踪、可比较、可复现
- Registry 只负责元数据、产物引用、状态
- Registry 不负责存放大体量逐 bar replay 数据

写入 PostgreSQL research.experiments 和 research.experiment_summaries。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Experiment CRUD
# ---------------------------------------------------------------------------

def create_experiment(
    session: Session,
    *,
    family: str,
    symbol: str,
    timeframe: str,
    dataset_version: str,
    parameter_overrides: dict[str, Any],
    window_start_ts: datetime | None = None,
    window_end_ts: datetime | None = None,
    scan_run_id: UUID | None = None,
    notes: str | None = None,
) -> UUID:
    """创建新实验记录，状态为 pending，返回 experiment_id。"""
    row = session.execute(
        text("""
            INSERT INTO research.experiments
                (family, symbol, timeframe, dataset_version, parameter_overrides,
                 window_start_ts, window_end_ts, scan_run_id, notes, status)
            VALUES
                (:family, :symbol, :timeframe, :dv, :po,
                 :ws, :we, :scan, :notes, 'pending')
            RETURNING experiment_id
        """),
        {
            "family": family,
            "symbol": symbol,
            "timeframe": timeframe,
            "dv": dataset_version,
            "po": json.dumps(parameter_overrides, ensure_ascii=False),
            "ws": window_start_ts,
            "we": window_end_ts,
            "scan": str(scan_run_id) if scan_run_id else None,
            "notes": notes,
        },
    )
    experiment_id = row.scalar_one()
    session.flush()
    log.info("Created experiment %s (family=%s, symbol=%s)", experiment_id, family, symbol)
    return experiment_id


def mark_experiment_running(session: Session, experiment_id: UUID) -> None:
    """将实验标记为 running。"""
    session.execute(
        text("""
            UPDATE research.experiments
            SET status = 'running',
                started_at = :now,
                updated_at = :now
            WHERE experiment_id = :eid
        """),
        {"eid": str(experiment_id), "now": _utcnow()},
    )
    session.flush()


def mark_experiment_succeeded(
    session: Session,
    experiment_id: UUID,
    *,
    bar_count: int | None = None,
    result_path: str | None = None,
    summary_path: str | None = None,
    report_path: str | None = None,
) -> None:
    """将实验标记为 succeeded，回写产物路径。"""
    session.execute(
        text("""
            UPDATE research.experiments
            SET status = 'succeeded',
                finished_at = :now,
                updated_at = :now,
                bar_count = COALESCE(:bc, bar_count),
                result_path = COALESCE(:rp, result_path),
                summary_path = COALESCE(:sp, summary_path),
                report_path = COALESCE(:rep, report_path)
            WHERE experiment_id = :eid
        """),
        {
            "eid": str(experiment_id),
            "now": _utcnow(),
            "bc": bar_count,
            "rp": result_path,
            "sp": summary_path,
            "rep": report_path,
        },
    )
    session.flush()
    log.info("Experiment %s succeeded.", experiment_id)


def mark_experiment_failed(
    session: Session,
    experiment_id: UUID,
    *,
    error_message: str | None = None,
) -> None:
    """将实验标记为 failed。"""
    session.execute(
        text("""
            UPDATE research.experiments
            SET status = 'failed',
                finished_at = :now,
                updated_at = :now,
                error_message = :err
            WHERE experiment_id = :eid
        """),
        {"eid": str(experiment_id), "now": _utcnow(), "err": error_message},
    )
    session.flush()
    log.warning("Experiment %s failed: %s", experiment_id, error_message)


# ---------------------------------------------------------------------------
# Experiment Summary
# ---------------------------------------------------------------------------

def upsert_experiment_summary(
    session: Session,
    experiment_id: UUID,
    *,
    summary: dict[str, Any],
) -> UUID:
    """写入或更新 experiment_summaries 记录。"""
    row = session.execute(
        text("""
            INSERT INTO research.experiment_summaries
                (experiment_id, total_bars, opening_count, blocked_count,
                 hold_count, close_count,
                 selectable_count, execution_compatible_count,
                 selectable_ratio, execution_compatible_ratio,
                 mean_long_score, mean_short_score,
                 mean_expected_edge_bps, median_expected_edge_bps,
                 p25_expected_edge_bps, p75_expected_edge_bps,
                 top_blocking_reasons, state_distribution, action_distribution)
            VALUES
                (:eid, :tb, :oc, :bc, :hc, :cc,
                 :sc, :ec, :sr, :er,
                 :mls, :mss, :mee, :mdee, :p25, :p75,
                 :tbr, :sd, :ad)
            ON CONFLICT (experiment_id) DO UPDATE SET
                total_bars = EXCLUDED.total_bars,
                opening_count = EXCLUDED.opening_count,
                blocked_count = EXCLUDED.blocked_count,
                hold_count = EXCLUDED.hold_count,
                close_count = EXCLUDED.close_count,
                selectable_count = EXCLUDED.selectable_count,
                execution_compatible_count = EXCLUDED.execution_compatible_count,
                selectable_ratio = EXCLUDED.selectable_ratio,
                execution_compatible_ratio = EXCLUDED.execution_compatible_ratio,
                mean_long_score = EXCLUDED.mean_long_score,
                mean_short_score = EXCLUDED.mean_short_score,
                mean_expected_edge_bps = EXCLUDED.mean_expected_edge_bps,
                median_expected_edge_bps = EXCLUDED.median_expected_edge_bps,
                p25_expected_edge_bps = EXCLUDED.p25_expected_edge_bps,
                p75_expected_edge_bps = EXCLUDED.p75_expected_edge_bps,
                top_blocking_reasons = EXCLUDED.top_blocking_reasons,
                state_distribution = EXCLUDED.state_distribution,
                action_distribution = EXCLUDED.action_distribution,
                updated_at = now()
            RETURNING experiment_summary_id
        """),
        {
            "eid": str(experiment_id),
            "tb": summary.get("total_bars", 0),
            "oc": summary.get("opening_count", 0),
            "bc": summary.get("blocked_count", 0),
            "hc": summary.get("hold_count", 0),
            "cc": summary.get("close_count", 0),
            "sc": summary.get("selectable_count", 0),
            "ec": summary.get("execution_compatible_count", 0),
            "sr": summary.get("selectable_ratio"),
            "er": summary.get("execution_compatible_ratio"),
            "mls": summary.get("mean_long_score"),
            "mss": summary.get("mean_short_score"),
            "mee": summary.get("mean_expected_edge_bps"),
            "mdee": summary.get("median_expected_edge_bps"),
            "p25": summary.get("p25_expected_edge_bps"),
            "p75": summary.get("p75_expected_edge_bps"),
            "tbr": json.dumps(summary.get("top_blocking_reasons", []), ensure_ascii=False),
            "sd": json.dumps(summary.get("state_distribution", {}), ensure_ascii=False),
            "ad": json.dumps(summary.get("action_distribution", {}), ensure_ascii=False),
        },
    )
    sid = row.scalar_one()
    session.flush()
    log.info("Upserted experiment summary %s for experiment %s", sid, experiment_id)
    return sid


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def get_experiment(session: Session, experiment_id: UUID) -> dict[str, Any] | None:
    """按 experiment_id 查询实验记录。"""
    row = session.execute(
        text("SELECT * FROM research.experiments WHERE experiment_id = :eid"),
        {"eid": str(experiment_id)},
    ).mappings().fetchone()
    return dict(row) if row else None


def list_experiments(
    session: Session,
    *,
    family: str | None = None,
    symbol: str | None = None,
    timeframe: str | None = None,
    status: str | None = None,
    scan_run_id: UUID | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """列出实验记录，支持过滤。"""
    conditions: list[str] = []
    params: dict[str, Any] = {"lim": limit}
    if family:
        conditions.append("family = :family")
        params["family"] = family
    if symbol:
        conditions.append("symbol = :symbol")
        params["symbol"] = symbol
    if timeframe:
        conditions.append("timeframe = :tf")
        params["tf"] = timeframe
    if status:
        conditions.append("status = :status")
        params["status"] = status
    if scan_run_id:
        conditions.append("scan_run_id = :scan")
        params["scan"] = str(scan_run_id)

    where = " AND ".join(conditions) if conditions else "TRUE"
    rows = session.execute(
        text(f"SELECT * FROM research.experiments WHERE {where} ORDER BY created_at DESC LIMIT :lim"),
        params,
    ).mappings().fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
