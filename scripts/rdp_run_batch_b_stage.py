#!/usr/bin/env python3
"""Advanced operator tool for selected Batch B apply/rollback stages.

The canonical forward path is ``scripts/apply_schema_migrations.py`` (or the
compatibility initializer ``scripts/rdp_init_db.py``), both of which run the
complete ledgered chain.  This tool is reserved for an approved partial-stage
operation and still enforces canonical predecessors/checksums.

USAGE
=====

Dry-run (只 SQL parse,不执行):
    python scripts/rdp_run_batch_b_stage.py --stage batch_b_11_silver_numeric_widen --dry-run

Apply (真跑,必须 --confirm-prod):
    python scripts/rdp_run_batch_b_stage.py --stage batch_b_11_silver_numeric_widen --confirm-prod

Apply 多个 stage (逗号分隔):
    python scripts/rdp_run_batch_b_stage.py --stage batch_b_11_silver_numeric_widen,batch_b_09_mark_ls_history --confirm-prod

Rollback (逆序回滚):
    python scripts/rdp_run_batch_b_stage.py --stage batch_b_11_silver_numeric_widen --rollback --confirm-prod

Exit codes:
    0 = 成功 (dry-run 或 apply)
    1 = migration 报错 / rollback 失败
    2 = 参数校验失败 (缺 --confirm-prod 等)
"""

from __future__ import annotations

import argparse
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rdp_run_batch_b_stage")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Batch B 受控部分 stage apply/rollback 工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--stage",
        required=True,
        help=(
            "要执行的 Batch B stage 名 (不带 .sql 后缀),"
            "逗号分隔多个。合法值在 aats.data_platform.migrations._batch_b.BATCH_B_STAGES"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="解析 SQL 但不执行 (也不创建 engine session)。不 mutate DB。",
    )
    parser.add_argument(
        "--rollback",
        action="store_true",
        help="跑对偶 rollback SQL 而非前向 apply。",
    )
    parser.add_argument(
        "--confirm-prod",
        action="store_true",
        help=(
            "真正对 DB 执行 migration 的必要 flag。"
            "不加 --dry-run 也不加 --confirm-prod 时, 脚本拒绝运行。"
        ),
    )
    args = parser.parse_args()

    from aats.data_platform.migrations._batch_b import (
        BATCH_B_STAGES,
        _load_sql,
        run_batch_b_migrations,
        run_batch_b_rollback,
    )

    stages = [s.strip() for s in args.stage.split(",") if s.strip()]
    unknown = [s for s in stages if s not in BATCH_B_STAGES]
    if unknown:
        log.error("unknown stages %r; valid: %s", unknown, BATCH_B_STAGES)
        return 2

    if not args.dry_run and not args.confirm_prod:
        log.error(
            "stage(s) %r would mutate DB; pass --dry-run to parse-only, "
            "or --confirm-prod to apply.", stages,
        )
        return 2

    if args.dry_run:
        for stage in stages:
            sql = _load_sql(stage, rollback=args.rollback)
            kind = "rollback" if args.rollback else "forward"
            log.info(
                "[dry-run] %s stage=%s chars=%d (not executed)",
                kind, stage, len(sql),
            )
        return 0

    # apply 或 rollback: 真跑
    from aats.data_platform.config import get_settings
    from aats.data_platform.db import get_engine

    settings = get_settings()
    log.info(
        "running approved %s batch_b stages=%s",
        "rollback" if args.rollback else "apply", stages,
    )

    engine = get_engine(settings)
    report = (
        run_batch_b_rollback(engine, stages=stages)
        if args.rollback
        else run_batch_b_migrations(engine, stages=stages)
    )

    for stage_result in report.stages:
        if stage_result.ok:
            log.info("  [OK] %s", stage_result.stage)
        else:
            log.error("  [FAIL] %s: %s", stage_result.stage, stage_result.error_message)

    if not report.ok:
        log.error("batch B stage application FAILED: %s", report.error_message)
        return 1
    log.info("batch B stage application complete: %d stage(s) OK", len(report.stages))
    return 0


if __name__ == "__main__":
    sys.exit(main())
