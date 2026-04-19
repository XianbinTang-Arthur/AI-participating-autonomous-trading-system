#!/usr/bin/env python3
"""RDP Bug 6 retry 延迟机制 schema 迁移: 给 rdp_task_queue 加 earliest_start_at.

背景
----
R3 (roadmap Bug 6 retry) 让 daemon 在 workflow failed 后自动产生 15min 延迟的
retry task。delay 通过 rdp_task_queue.earliest_start_at 字段实现: claim 时过滤
``earliest_start_at <= now()``。

rdp_models.py 的 ORM 已新增该列 (``server_default='now()'``)，新 DB 初始化
时自动带上。但**现有实盘 DB** 不会自动拿到新列，需要一次 ALTER TABLE。

本脚本幂等地给 governance.rdp_task_queue 添加 earliest_start_at 列:
  - 已有该列 → no-op
  - 无该列 → ADD COLUMN with DEFAULT now()
  - 现有 pending 任务: earliest_start_at = now() (立即 claimable)，不影响节奏

用法
----
    python scripts/rdp_migration_bug6_retry_add_earliest_start_at.py --dry-run
    python scripts/rdp_migration_bug6_retry_add_earliest_start_at.py --apply

连接
----
沿用 governance_db 解析链 (AATS_ACTIVE_PARAMETER_DB_URL → RDP_DATABASE_URL)。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from aats.data_platform.governance._db_util import try_governance_db  # noqa: E402


def run_migration(*, dry_run: bool) -> int:
    engine, ok = try_governance_db()
    if not ok or engine is None:
        print("[ERROR] 无法连接 governance DB", file=sys.stderr)
        return 2

    try:
        with Session(engine) as session:
            row = session.execute(
                text(
                    """
                    SELECT column_name, column_default
                    FROM information_schema.columns
                    WHERE table_schema = 'governance'
                      AND table_name = 'rdp_task_queue'
                      AND column_name = 'earliest_start_at'
                    """,
                ),
            ).fetchone()

            if row is not None:
                print(
                    f"[OK] earliest_start_at 列已存在 (default={row.column_default!r})"
                    "，无需迁移",
                )
                return 0

            print("[INFO] governance.rdp_task_queue 缺 earliest_start_at 列，计划 ADD COLUMN")
            if dry_run:
                print("[DRY RUN] 未实际执行 ALTER TABLE")
                return 0

            session.execute(
                text(
                    """
                    ALTER TABLE governance.rdp_task_queue
                    ADD COLUMN earliest_start_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    """,
                ),
            )
            session.commit()
            print("[OK] earliest_start_at 列已添加，现有 pending 任务默认立即可领")
            return 0
    finally:
        engine.dispose()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true", help="仅显示将执行什么，不实际改 schema")
    p.add_argument("--apply", action="store_true", help="实际执行 ALTER TABLE")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if not args.dry_run and not args.apply:
        print("[ERROR] 必须指定 --dry-run 或 --apply", file=sys.stderr)
        return 2
    return run_migration(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
