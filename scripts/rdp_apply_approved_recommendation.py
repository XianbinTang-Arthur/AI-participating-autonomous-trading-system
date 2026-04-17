#!/usr/bin/env python3
"""从已批准 recommendation 应用参数到 active parameter set.

工作包 C 交付物：受控的 apply 流程。

前置条件:
  - recommendation 状态必须为 approved
  - recommendation 必须有 target_parameter_set_id
  - target parameter set 必须在 governance registry 中存在

用法:
    python scripts/rdp_apply_approved_recommendation.py \
        --recommendation-id rec_xxx --actor operator_name

    python scripts/rdp_apply_approved_recommendation.py \
        --recommendation-id rec_xxx --dry-run

退出码:
    0 = 成功
    1 = 错误

说明:
    prod 环境默认拒绝 direct apply；请改用 scripts/rdp_create_parameter_release.py。
    A-0.5 起 prod 写闸改由 API 层的 session-bound HMAC apply-token 强制，
    通过 ``POST /rdp/operator-tokens`` 签发后，在 apply/rollback 请求头带
    ``X-Rdp-Apply-Token`` 即可；旧 ``RDP_PRODUCTION_APPLY_ENABLED`` flag 已废弃。
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
    p = argparse.ArgumentParser(description="Apply approved recommendation")
    p.add_argument(
        "--recommendation-id",
        required=True,
        help="已批准的 recommendation_id",
    )
    p.add_argument(
        "--actor",
        default="operator",
        help="操作人",
    )
    p.add_argument(
        "--notes",
        default=None,
        help="操作备注",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="仅预览，不实际写入",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    project_root = ROOT

    from aats.data_platform.decision_system.active_parameter_apply import (
        apply_approved_recommendation,
    )

    result = apply_approved_recommendation(
        project_root,
        recommendation_id=args.recommendation_id,
        actor=args.actor,
        notes=args.notes,
        dry_run=args.dry_run,
    )

    if not result["ok"]:
        print(f"[ERROR] {result['message']}")
        return 1

    print(f"[OK] {result['message']}")
    print()
    print(f"  Operation:     {result.get('operation_type', 'apply')}")
    print(f"  Combo:         {result.get('combo_key')}")
    print(f"  Parameter Set: {result.get('parameter_set_id')}")
    print(f"  Values:        {json.dumps(result.get('values', {}), ensure_ascii=False)}")
    if result.get("from_parameter_set_id"):
        print(f"  Previous:      {result['from_parameter_set_id']}")
    if result.get("operation_id"):
        print(f"  Operation ID:  {result['operation_id']}")

    if not args.dry_run:
        print()
        print("后续操作:")
        print("  如果主交易系统正在运行，需要重启或 reload 使新参数生效:")
        print("    方式 1: 重启 API gateway")
        print("    方式 2: 调用 POST /system/rebaseline")

    return 0


if __name__ == "__main__":
    sys.exit(main())
