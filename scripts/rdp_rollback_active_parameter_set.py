#!/usr/bin/env python3
"""回滚 active parameter set 到上一版本.

工作包 C 交付物：受控的 rollback 流程。

回滚行为:
  1. 查找指定 family/timeframe 的上一个 active parameter set
  2. 将其重新写为 active
  3. 写入 rollback history
  4. 输出 rollback 结果

用法:
    # 自动回滚到上一版本
    python scripts/rdp_rollback_active_parameter_set.py \
        --family independent --timeframe 15m --actor operator_name

    # 回滚到指定版本
    python scripts/rdp_rollback_active_parameter_set.py \
        --family independent --timeframe 15m \
        --to-parameter-set-id ps_xxx --actor operator_name

    # 预览
    python scripts/rdp_rollback_active_parameter_set.py \
        --family independent --timeframe 15m --dry-run

退出码:
    0 = 成功
    1 = 错误
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Rollback active parameter set")
    p.add_argument("--family", required=True, help="策略家族: independent / directional")
    p.add_argument("--timeframe", required=True, help="时间框架: 15m / 1h")
    p.add_argument(
        "--to-parameter-set-id",
        default=None,
        help="指定回滚目标 parameter_set_id（可选，不指定则自动回滚到上一版）",
    )
    p.add_argument("--actor", default="operator", help="操作人")
    p.add_argument("--notes", default=None, help="操作备注")
    p.add_argument("--dry-run", action="store_true", help="仅预览")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    project_root = ROOT

    from aats.data_platform.decision_system.active_parameter_apply import (
        rollback_active_parameter_set,
    )

    result = rollback_active_parameter_set(
        project_root,
        family=args.family,
        timeframe=args.timeframe,
        to_parameter_set_id=args.to_parameter_set_id,
        actor=args.actor,
        notes=args.notes,
        dry_run=args.dry_run,
    )

    if not result["ok"]:
        print(f"[ERROR] {result['message']}")
        return 1

    print(f"[OK] {result['message']}")
    print()
    print(f"  Operation: {result.get('operation_type', 'rollback')}")
    print(f"  Combo:     {result.get('combo_key')}")
    print(f"  From:      {result.get('from_parameter_set_id')}")
    print(f"  To:        {result.get('to_parameter_set_id')}")
    print(f"  Values:    {json.dumps(result.get('values', {}), ensure_ascii=False)}")
    if result.get("operation_id"):
        print(f"  Op ID:     {result['operation_id']}")

    if not args.dry_run:
        print()
        print("后续操作:")
        print("  如果主交易系统正在运行，需要重启或 reload 使新参数生效:")
        print("    方式 1: 重启 API gateway")
        print("    方式 2: 调用 POST /system/rebaseline")

    return 0


if __name__ == "__main__":
    sys.exit(main())
