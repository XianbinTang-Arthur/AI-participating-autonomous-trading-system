#!/usr/bin/env python3
"""RDP Bug 8 一次性清理: cancel 存量的 rollback_triggered effectiveness evaluations.

背景
----
Bug 8 修复 (Layer 0) 放宽 validate_rollback_target 接受 deprecated (≤30 天)
后，下次 observation_cycle 会把所有 ``conclusion='rollback_triggered'`` 且未
``rollback_cancelled``/``rollback_enforced`` 的评估真正执行回滚。

在修复上线后、首次 observation_cycle 运行前，存量评估里有 4 条未处理的
rollback_triggered 对应当前 4 个 active parameter_sets。如果不清理，Bug 8
生效瞬间会把实盘 4 个 active 切换到上一代，触发非计划的配置切换。

本脚本为这些存量 evaluations 打上 ``rollback_cancelled=true`` + reason，
让它们不再触发 auto-rollback，未来新的 release 走完整 Bug 8 闭环。

设计决策: 为什么不硬删这些 evaluation?
  - evaluation 是 observation 的审计记录，保留原始结论可追溯
  - rollback_cancelled 是既有字段 (release_effectiveness.py:484-490
    已用于 "later successful release 覆盖" 场景)，语义正确
  - 下次 observation_cycle 会重新评估，如果仍判 rollback → 再次进入
    pending；但那时 Bug 8 路径已经完整，可以走正常 rollback / soft pause

清理范围: conclusion='rollback_triggered'
              AND rollback_cancelled IS NOT TRUE
              AND rollback_enforced IS NOT TRUE

用法
----
    # dry-run 看会影响哪些
    python scripts/rdp_migration_bug8_cancel_stale_rollbacks.py --dry-run

    # 实际执行 (幂等)
    python scripts/rdp_migration_bug8_cancel_stale_rollbacks.py --apply

连接
----
沿用 governance_db 解析链 (AATS_ACTIVE_PARAMETER_DB_URL → RDP_DATABASE_URL)。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from aats.data_platform.governance._db_util import try_governance_db  # noqa: E402


_CANCEL_REASON = "pre_bug8_manual_clear_stale_rollback_recommendations"


def run_migration(*, dry_run: bool) -> int:
    engine, ok = try_governance_db()
    if not ok or engine is None:
        print("[ERROR] 无法连接 governance DB", file=sys.stderr)
        return 2

    try:
        with Session(engine) as session:
            # 查找所有候选: conclusion=rollback_triggered 且未 cancelled/enforced
            rows = session.execute(
                text(
                    """
                    SELECT release_id, family, timeframe, conclusion,
                           (payload->>'rollback_cancelled')::boolean AS cancelled,
                           (payload->>'rollback_enforced')::boolean AS enforced,
                           evaluated_at
                    FROM governance.release_effectiveness
                    WHERE conclusion = 'rollback_triggered'
                      AND ((payload->>'rollback_cancelled') IS DISTINCT FROM 'true')
                      AND ((payload->>'rollback_enforced') IS DISTINCT FROM 'true')
                    ORDER BY evaluated_at DESC
                    """,
                ),
            ).fetchall()

            if not rows:
                print("[INFO] 没有未处理的 rollback_triggered evaluation，无需清理")
                return 0

            print(f"[INFO] 发现 {len(rows)} 条未处理的 rollback_triggered:")
            for row in rows:
                print(
                    f"  [NEEDS CANCEL] release={row.release_id} "
                    f"family={row.family} tf={row.timeframe} "
                    f"evaluated_at={row.evaluated_at}",
                )

            if dry_run:
                print("\n[DRY RUN] 未执行实际 UPDATE")
                return 0

            # 实际执行: 通过 jsonb_set 把 rollback_cancelled / reason / at 打进
            # payload 顶层 (与 release_effectiveness.py:484-490 的内存侧写入
            # 字段名一致, save_effectiveness_registry 会覆盖 payload)
            now_iso = datetime.now(timezone.utc).isoformat()
            result = session.execute(
                text(
                    """
                    UPDATE governance.release_effectiveness
                    SET payload = jsonb_set(
                        jsonb_set(
                            jsonb_set(
                                payload,
                                '{rollback_cancelled}', 'true'::jsonb
                            ),
                            '{rollback_cancelled_at}', to_jsonb(:now::text)
                        ),
                        '{rollback_cancelled_reason}', to_jsonb(:reason::text)
                    ),
                    updated_at = now()
                    WHERE conclusion = 'rollback_triggered'
                      AND ((payload->>'rollback_cancelled') IS DISTINCT FROM 'true')
                      AND ((payload->>'rollback_enforced') IS DISTINCT FROM 'true')
                    """,
                ),
                {"now": now_iso, "reason": _CANCEL_REASON},
            )
            session.commit()
            updated = result.rowcount or 0
            print(f"\n[OK] {updated} 条 evaluation 已标记为 rollback_cancelled")
            return 0
    finally:
        engine.dispose()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true", help="仅显示将影响的条目，不执行")
    p.add_argument("--apply", action="store_true", help="实际执行清理")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if not args.dry_run and not args.apply:
        print("[ERROR] 必须指定 --dry-run 或 --apply", file=sys.stderr)
        return 2
    return run_migration(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
