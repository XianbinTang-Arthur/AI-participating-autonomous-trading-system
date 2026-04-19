"""Path B Phase 1 — event_store 热表冷启动归档 backfill。

一次性脚本：把 ``event_store`` 中早于 ``--older-than-days`` 天的行搬运到
``event_store_archive``。幂等（已存在于 archive 的 event_id 跳过）。

用法
====

**Dry-run（仅统计，不改数据）**

    python scripts/maintenance/backfill_event_store_archive.py \\
        --profile derivatives_live \\
        --older-than-days 14 \\
        --dry-run

输出样例::

    [backfill] profile=derivatives_live older_than_days=14 dry_run=True
    [backfill] cutoff_ts=2026-04-05T22:00:00+08:00
    [backfill] event_store 当前 oldest_ts=2026-04-17T04:47:36+08:00
    [backfill] 将归档 0 行，最早 ts=None
    [backfill] 退出码: 2 (dry-run 完成，未改动数据)

**实际执行（需要 --confirm 显式确认）**

    python scripts/maintenance/backfill_event_store_archive.py \\
        --profile derivatives_live \\
        --older-than-days 14 \\
        --apply \\
        --confirm

**注意**：`--apply` 必须配合 `--confirm`，否则拒绝执行。这是一层保护，
避免误操作。`--batch-size` 控制每个事务的行数，默认 10000。

退出码
======
- 0 = 执行成功
- 2 = dry-run 完成
- 3 = 参数错误 / 数据库不可达
- 4 = 用户未显式 --confirm 但传了 --apply
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill archive table from event_store hot table. "
            "Phase 1: retention 14 days."
        )
    )
    parser.add_argument(
        "--profile",
        choices=("spot", "derivatives", "spot_live", "derivatives_live"),
        required=True,
        help="Profile template to load (.env.<profile>).",
    )
    parser.add_argument(
        "--older-than-days",
        type=float,
        default=14.0,
        help="归档 event_timestamp 早于 now() - N 天的行。默认 14。",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=10_000,
        help="单次事务搬运行数上限，默认 10000。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅统计将要归档的行数，不改数据（退出码 2）。",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="实际搬运数据（需与 --confirm 同时使用）。",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="显式确认执行 --apply（保护层）。",
    )
    parser.add_argument(
        "--max-batches",
        type=int,
        default=None,
        help="最多执行 N 个 batch 后退出（None = 不限，搬完为止）。",
    )
    return parser.parse_args()


def _summarize_pending(session, cutoff: datetime) -> dict[str, object]:
    """查询将要归档的行数 + 最早 ts（dry-run 用）。"""
    from sqlalchemy import func, select
    from aats.storage.sqlalchemy_models import EventEnvelopeModel

    pending_count = (
        session.scalar(
            select(func.count(EventEnvelopeModel.sequence_id)).where(
                EventEnvelopeModel.event_timestamp < cutoff
            )
        )
        or 0
    )
    pending_oldest = session.scalar(
        select(func.min(EventEnvelopeModel.event_timestamp)).where(
            EventEnvelopeModel.event_timestamp < cutoff
        )
    )
    pending_newest = session.scalar(
        select(func.max(EventEnvelopeModel.event_timestamp)).where(
            EventEnvelopeModel.event_timestamp < cutoff
        )
    )
    return {
        "pending_count": int(pending_count),
        "pending_oldest_ts": pending_oldest.isoformat() if pending_oldest else None,
        "pending_newest_ts": pending_newest.isoformat() if pending_newest else None,
    }


def main() -> int:
    args = parse_args()

    # 参数互斥校验
    if args.apply and not args.confirm:
        print(
            "[backfill] ERROR: --apply 必须与 --confirm 同时使用（保护层）",
            file=sys.stderr,
        )
        return 4
    if not args.apply and not args.dry_run:
        # 默认 dry-run，用户未显式指定任一模式
        args.dry_run = True

    # 装载配置
    from aats.bootstrap.config import build_storage_backends, load_settings
    from aats.bootstrap.env_profiles import load_profiled_dotenv_into_process
    from aats.storage.housekeeping import DatabaseHousekeeping

    load_profiled_dotenv_into_process(ROOT, args.profile)
    settings = load_settings()
    storage = build_storage_backends(settings)

    if storage.database_runtime is None:
        print(
            "[backfill] ERROR: no database_runtime — 当前 profile 未配 Postgres",
            file=sys.stderr,
        )
        return 3

    housekeeping = DatabaseHousekeeping(
        session_factory=storage.database_runtime.session_factory
    )

    cutoff = datetime.now(timezone.utc) - timedelta(days=float(args.older_than_days))

    print(
        f"[backfill] profile={args.profile} "
        f"older_than_days={args.older_than_days} "
        f"dry_run={args.dry_run} apply={args.apply} "
        f"batch_size={args.batch_size} max_batches={args.max_batches}"
    )
    print(f"[backfill] cutoff_ts={cutoff.isoformat()}")

    try:
        # 预扫描
        with storage.database_runtime.session_factory() as session:
            summary_before = _summarize_pending(session, cutoff)
        print(
            f"[backfill] 将归档 {summary_before['pending_count']} 行，"
            f"最早 ts={summary_before['pending_oldest_ts']}, "
            f"最新 ts={summary_before['pending_newest_ts']}"
        )

        if args.dry_run:
            # dry-run 通过 archive_hot_event_store(dry_run=True) 再一次校验
            report = housekeeping.archive_hot_event_store(
                older_than_days=float(args.older_than_days),
                batch_size=args.batch_size,
                dry_run=True,
            )
            output = {
                "mode": "dry_run",
                "report": report.as_dict(),
                "summary_before": summary_before,
            }
            print(json.dumps(output, indent=2, default=str))
            return 2

        # 实际 apply
        print("[backfill] 开始执行 archive_hot_event_store …")
        report = housekeeping.archive_hot_event_store(
            older_than_days=float(args.older_than_days),
            batch_size=args.batch_size,
            dry_run=False,
            max_batches=args.max_batches,
        )

        # 执行后再扫一次
        with storage.database_runtime.session_factory() as session:
            summary_after = _summarize_pending(session, cutoff)

        output = {
            "mode": "applied",
            "report": report.as_dict(),
            "summary_before": summary_before,
            "summary_after_still_pending": summary_after,
        }
        print(json.dumps(output, indent=2, default=str))
        return 0
    finally:
        if storage.database_runtime is not None:
            storage.database_runtime.dispose()


if __name__ == "__main__":
    sys.exit(main())
