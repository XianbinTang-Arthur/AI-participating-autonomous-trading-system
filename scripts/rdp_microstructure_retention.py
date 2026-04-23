#!/usr/bin/env python3
"""P1-D Phase 1A — Microstructure Bronze/Staging retention housekeeping.

参考:
    docs/design/p1d_phase1a_implementation_design_2026_04_20.md §6.x
    docs/task/microstructure_bronze_retention_sow.md

对以下表按设计文档保留策略删除历史行:

    bronze.market_trades               -> 30 days
    bronze.market_orderbook_bbo        -> 14 days
    bronze.market_orderbook_books5     -> 14 days
    staging.market_oi_funding_ticks    -> 7  days

默认 dry-run (只 SELECT COUNT); 实际删除需要 --apply --confirm 双层保护,
风格对齐 scripts/maintenance/backfill_event_store_archive.py。

Exit codes
==========
- 0 = apply 成功 (所有目标表 DELETE 成功提交)
- 2 = dry-run 完成
- 3 = DB / 参数错误
- 4 = --apply 未配 --confirm 的保护错误
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rdp_microstructure_retention")


# Retention plan — 严格对齐 p1d_phase1a_implementation_design_2026_04_20.md §6.x。
# 修改前请先更新设计文档 + SOW, 不允许在这里随意调。
RETENTION_PLAN: dict[str, int] = {
    "bronze.market_trades": 30,
    "bronze.market_orderbook_bbo": 14,
    "bronze.market_orderbook_books5": 14,
    "staging.market_oi_funding_ticks": 7,
}


@dataclass
class TableResult:
    table: str
    retention_days: int
    cutoff_ts: str
    mode: str  # "dry_run" | "applied" | "error"
    row_count: int = 0  # dry-run: 匹配 cutoff 的行数; apply: 实际删除行数
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "table": self.table,
            "retention_days": self.retention_days,
            "cutoff_ts": self.cutoff_ts,
            "mode": self.mode,
            "row_count": self.row_count,
            "error": self.error,
        }


@dataclass
class RunSummary:
    mode: str
    started_at: str
    tables: list[TableResult] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "started_at": self.started_at,
            "tables": [t.as_dict() for t in self.tables],
            "total_rows": sum(t.row_count for t in self.tables),
            "errors": [t.table for t in self.tables if t.error],
        }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Microstructure Bronze/Staging retention housekeeping. "
            "Default dry-run; --apply --confirm to actually DELETE."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅 SELECT COUNT 不 DELETE (默认行为; 显式写上也可).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="切开 dry-run, 真正 DELETE (必须配 --confirm).",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="配合 --apply 的显式确认 (双层保护).",
    )
    return parser.parse_args(argv)


def _count_rows_before_cutoff(session, table: str, cutoff: datetime) -> int:
    """SELECT COUNT(*) FROM <table> WHERE ts < :cutoff."""
    from sqlalchemy import text

    # table 名来自 RETENTION_PLAN 常量, 非用户输入, 直接插值安全。
    row = session.execute(
        text(f"SELECT COUNT(*) AS n FROM {table} WHERE ts < :cutoff"),
        {"cutoff": cutoff},
    ).fetchone()
    if row is None:
        return 0
    # Row 可能是 tuple 也可能是 mapping, 兼容两种访问
    try:
        return int(row.n)
    except AttributeError:
        return int(row[0])


def _delete_rows_before_cutoff(session, table: str, cutoff: datetime) -> int:
    """DELETE FROM <table> WHERE ts < :cutoff, 返回 rowcount."""
    from sqlalchemy import text

    result = session.execute(
        text(f"DELETE FROM {table} WHERE ts < :cutoff"),
        {"cutoff": cutoff},
    )
    return int(result.rowcount or 0)


def _run_dry_run(
    session_factory: Callable[[], object],
    now: datetime,
) -> RunSummary:
    summary = RunSummary(mode="dry_run", started_at=now.isoformat())
    for table, days in RETENTION_PLAN.items():
        cutoff = now - timedelta(days=days)
        try:
            with session_factory() as session:
                n = _count_rows_before_cutoff(session, table, cutoff)
        except Exception as exc:
            log.error(
                "[retention] dry-run COUNT failed for %s: %r", table, exc,
            )
            summary.tables.append(
                TableResult(
                    table=table,
                    retention_days=days,
                    cutoff_ts=cutoff.isoformat(),
                    mode="error",
                    error=repr(exc),
                )
            )
            continue
        log.info(
            "[retention] dry-run %s retention=%dd cutoff=%s pending_rows=%d",
            table, days, cutoff.isoformat(), n,
        )
        summary.tables.append(
            TableResult(
                table=table,
                retention_days=days,
                cutoff_ts=cutoff.isoformat(),
                mode="dry_run",
                row_count=n,
            )
        )
    return summary


def _run_apply(
    session_factory: Callable[[], object],
    now: datetime,
) -> RunSummary:
    summary = RunSummary(mode="applied", started_at=now.isoformat())
    for table, days in RETENTION_PLAN.items():
        cutoff = now - timedelta(days=days)
        try:
            with session_factory() as session:
                deleted = _delete_rows_before_cutoff(session, table, cutoff)
        except Exception as exc:
            log.error(
                "[retention] apply DELETE failed for %s: %r", table, exc,
            )
            summary.tables.append(
                TableResult(
                    table=table,
                    retention_days=days,
                    cutoff_ts=cutoff.isoformat(),
                    mode="error",
                    error=repr(exc),
                )
            )
            continue
        log.info(
            "[retention] applied %s retention=%dd cutoff=%s deleted_rows=%d",
            table, days, cutoff.isoformat(), deleted,
        )
        summary.tables.append(
            TableResult(
                table=table,
                retention_days=days,
                cutoff_ts=cutoff.isoformat(),
                mode="applied",
                row_count=deleted,
            )
        )
    return summary


def _build_session_factory() -> Callable[[], object]:
    """封装 get_session import, 便于单测 monkeypatch."""
    from aats.data_platform.db import get_session

    return get_session


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # 保护层: --apply 必须配 --confirm
    if args.apply and not args.confirm:
        log.error(
            "[retention] --apply 必须与 --confirm 同时使用 (保护层)",
        )
        return 4
    # --confirm 单独没意义; 当 warning 处理, 走默认 dry-run
    if args.confirm and not args.apply:
        log.warning(
            "[retention] --confirm 在无 --apply 时被忽略, 走 dry-run",
        )

    apply_mode = args.apply and args.confirm

    try:
        session_factory = _build_session_factory()
    except Exception as exc:
        log.error("[retention] 无法获取 DB session factory: %r", exc)
        return 3

    now = datetime.now(timezone.utc)
    log.info(
        "[retention] start mode=%s now=%s tables=%s",
        "applied" if apply_mode else "dry_run",
        now.isoformat(),
        list(RETENTION_PLAN.keys()),
    )

    if apply_mode:
        summary = _run_apply(session_factory, now)
    else:
        summary = _run_dry_run(session_factory, now)

    print(json.dumps(summary.as_dict(), indent=2, default=str))

    if apply_mode:
        # 任何 per-table error 都视作 apply 不完全成功 -> 返 3 让调度方能看见
        if any(t.error for t in summary.tables):
            return 3
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
