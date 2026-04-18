#!/usr/bin/env python3
"""RDP Bug 9 一次性数据迁移: 当前 active parameter_sets 的 status 从 deprecated → released.

背景
----
governance.parameter_sets 的 status 字段原设计有 ``released`` 值，但代码里
从未被写入过（grep 零命中）。导致所有曾 live 的参数在 parameter_sets 表里
都是 deprecated 状态，validate_rollback_target 规则 2 永远拒绝 rollback。

代码侧修复 (aats/data_platform/decision_system/active_parameter_apply.py)
已让 apply 事务内写 status='released'，覆盖未来所有新 apply。

本脚本专门处理**存量数据**：把当前仍然 active (通过 active_parameter_sets
表反向定位) 的 parameter_sets 行，从 deprecated/frozen 升级到 released。

不触碰历史 parameter_sets（那些曾 live 但已被覆盖的仍然保留 deprecated；
"deprecated target 能否 rollback" 是独立的 Bug 8 fallback 策略，本迁移
不越权）。

用法
----
    # dry-run 查看将改动什么
    python scripts/rdp_migration_bug9_promote_active_to_released.py --dry-run

    # 实际执行（幂等，已是 released 的跳过）
    python scripts/rdp_migration_bug9_promote_active_to_released.py --apply

连接
----
沿用 governance_db 解析链：AATS_ACTIVE_PARAMETER_DB_URL → RDP_DATABASE_URL。
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
            rows = session.execute(
                text(
                    """
                    SELECT ps.parameter_set_id, ps.family, ps.timeframe, ps.status,
                           aps.applied_at
                    FROM governance.parameter_sets ps
                    INNER JOIN governance.active_parameter_sets aps
                      ON ps.parameter_set_id = aps.parameter_set_id
                     AND ps.family = aps.family
                     AND ps.timeframe = aps.timeframe
                    ORDER BY ps.family, ps.timeframe
                    """,
                ),
            ).fetchall()

            if not rows:
                print("[INFO] 未发现任何 active parameter_set，无需迁移")
                return 0

            print(f"[INFO] 发现 {len(rows)} 个 active parameter_sets:")
            need_update = []
            for row in rows:
                tag = "[OK already released]" if row.status == "released" else "[NEEDS UPDATE]"
                print(
                    f"  {tag} family={row.family:<12} tf={row.timeframe:<4} "
                    f"status={row.status:<12} pid={row.parameter_set_id} "
                    f"applied_at={row.applied_at}",
                )
                if row.status != "released":
                    need_update.append(row.parameter_set_id)

            if not need_update:
                print("\n[INFO] 所有 active parameter_sets 已是 released，无需迁移")
                return 0

            print(f"\n[INFO] 将 {len(need_update)} 条 parameter_sets 升级到 released")
            if dry_run:
                print("[DRY RUN] 未实际执行 UPDATE")
                return 0

            # 幂等 UPDATE：
            # - status 改 released
            # - frozen_at 只在还是 NULL 时赋值（保留历史 freeze 时间）
            result = session.execute(
                text(
                    """
                    UPDATE governance.parameter_sets
                    SET status = 'released',
                        frozen_at = COALESCE(frozen_at, now())
                    WHERE parameter_set_id = ANY(:pids)
                      AND status != 'released'
                    """,
                ),
                {"pids": need_update},
            )
            session.commit()
            updated = result.rowcount or 0
            print(f"[OK] {updated} 行已升级到 released")
            return 0
    finally:
        engine.dispose()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true", help="仅显示将改动什么，不执行")
    p.add_argument("--apply", action="store_true", help="实际执行迁移")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if not args.dry_run and not args.apply:
        print("[ERROR] 必须指定 --dry-run 或 --apply", file=sys.stderr)
        return 2
    return run_migration(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
