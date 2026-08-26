#!/usr/bin/env python3
"""Archive-gated microstructure Bronze/Staging retention housekeeping.

参考:
    docs/design/p1d_phase1a_implementation_design_2026_04_20.md §6.x
    docs/task/microstructure_bronze_retention_sow.md

对以下表按设计文档保留策略删除历史行:

    bronze.market_trades               -> 30 days
    bronze.market_orderbook_bbo        -> 14 days
    bronze.market_orderbook_books5     -> 14 days
    staging.market_oi_funding_ticks    -> 7  days

默认 dry-run (只 SELECT COUNT)。实际删除需要 --apply --confirm 双层保护，
并在同一事务内先验证所有目标表的全部到期 UTC 日分区。任何分区缺少唯一的
DELETE_ELIGIBLE 归档，或 manifest/SHA-256/行数不一致，全部删除都会回滚为零。

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


def _preflight_archived_partitions(
    session, table: str, cutoff: datetime
) -> list[tuple[object, object]]:
    """Lock and verify every expired UTC-day partition without deleting rows."""
    from sqlalchemy import text

    from aats.data_platform.data_governance.archive import (
        ArchiveScope,
        verify_archive_artifact,
    )

    cutoff_day = cutoff.replace(hour=0, minute=0, second=0, microsecond=0)
    session.execute(text(f"LOCK TABLE {table} IN SHARE ROW EXCLUSIVE MODE"))
    expired = session.execute(
        text(
            f"SELECT symbol, date_trunc('day', ts) AS coverage_start, "
            f"date_trunc('day', ts) + interval '1 day' AS coverage_end, "
            f"COUNT(*) AS row_count FROM {table} WHERE ts < :cutoff "
            f"GROUP BY symbol, coverage_start ORDER BY coverage_start, symbol"
        ),
        {"cutoff": cutoff_day},
    ).mappings().all()
    evidence: list[tuple[object, object]] = []
    for partition in expired:
        archives = session.execute(
            text(
                "SELECT partition_id, source_id, storage_path, sha256, row_count "
                "FROM meta.archive_partitions "
                "WHERE dataset_name = :dataset_name AND symbol = :symbol "
                "AND coverage_start = :start AND coverage_end = :end "
                "AND state = 'DELETE_ELIGIBLE' FOR UPDATE"
            ),
            {
                "dataset_name": table,
                "symbol": partition["symbol"],
                "start": partition["coverage_start"],
                "end": partition["coverage_end"],
            },
        ).mappings().all()
        if len(archives) != 1:
            raise RuntimeError(
                "archive_evidence_missing_or_overlapping:"
                f"{table}:{partition['symbol']}:"
                f"{partition['coverage_start']}"
            )
        archive = archives[0]
        source_rows = int(partition["row_count"])
        if int(archive["row_count"]) != source_rows:
            raise RuntimeError("archive_source_row_count_mismatch")
        verify_archive_artifact(
            Path(str(archive["storage_path"])),
            expected_sha256=str(archive["sha256"]),
            expected_rows=source_rows,
            expected_scope=ArchiveScope(
                source_id=str(archive["source_id"]),
                dataset_name=table,
                table=table,
                symbol=str(partition["symbol"]),
                coverage_start=partition["coverage_start"],
                coverage_end=partition["coverage_end"],
            ),
        )
        evidence.append((partition, archive))

    return evidence


def _delete_preflighted_partitions(
    session, table: str, evidence: list[tuple[object, object]]
) -> int:
    """Delete only the exact partitions returned by the completed preflight."""
    from sqlalchemy import text

    deleted_total = 0
    for partition, archive in evidence:
        result = session.execute(
            text(
                f"DELETE FROM {table} WHERE symbol = :symbol "
                "AND ts >= :start AND ts < :end"
            ),
            {
                "symbol": partition["symbol"],
                "start": partition["coverage_start"],
                "end": partition["coverage_end"],
            },
        )
        deleted = int(result.rowcount or 0)
        if deleted != int(partition["row_count"]):
            raise RuntimeError("retention_delete_row_count_mismatch")
        state_result = session.execute(
            text(
                "UPDATE meta.archive_partitions SET state = 'DELETED', "
                "deleted_at = NOW(), updated_at = NOW() "
                "WHERE partition_id = :partition_id "
                "AND state = 'DELETE_ELIGIBLE'"
            ),
            {"partition_id": archive["partition_id"]},
        )
        if int(state_result.rowcount or 0) != 1:
            raise RuntimeError("archive_deleted_state_transition_conflict")
        deleted_total += deleted
    return deleted_total


def _run_dry_run(
    session_factory: Callable[[], object],
    now: datetime,
) -> RunSummary:
    summary = RunSummary(mode="dry_run", started_at=now.isoformat())
    for table, days in RETENTION_PLAN.items():
        cutoff = (now - timedelta(days=days)).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        try:
            with session_factory() as session:
                n = _count_rows_before_cutoff(session, table, cutoff)
        except Exception as exc:
            error_type = type(exc).__name__
            log.error(
                "[retention] dry-run COUNT failed for %s: %s",
                table,
                error_type,
            )
            summary.tables.append(
                TableResult(
                    table=table,
                    retention_days=days,
                    cutoff_ts=cutoff.isoformat(),
                    mode="error",
                    error=error_type,
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
    cutoffs = {
        table: (now - timedelta(days=days)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        for table, days in RETENTION_PLAN.items()
    }
    try:
        with session_factory() as session:
            preflight = {
                table: _preflight_archived_partitions(session, table, cutoffs[table])
                for table in RETENTION_PLAN
            }
            deleted_by_table = {
                table: _delete_preflighted_partitions(
                    session, table, preflight[table]
                )
                for table in RETENTION_PLAN
            }
    except Exception as exc:
        error_type = type(exc).__name__
        log.error(
            "[retention] global preflight/delete failed; rolling back: %s",
            error_type,
        )
        for table, days in RETENTION_PLAN.items():
            summary.tables.append(
                TableResult(
                    table=table,
                    retention_days=days,
                    cutoff_ts=cutoffs[table].isoformat(),
                    mode="error",
                    error=error_type,
                )
            )
        return summary

    for table, days in RETENTION_PLAN.items():
        deleted = deleted_by_table[table]
        log.info(
            "[retention] applied %s retention=%dd cutoff=%s deleted_rows=%d",
            table,
            days,
            cutoffs[table].isoformat(),
            deleted,
        )
        summary.tables.append(
            TableResult(
                table=table,
                retention_days=days,
                cutoff_ts=cutoffs[table].isoformat(),
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
