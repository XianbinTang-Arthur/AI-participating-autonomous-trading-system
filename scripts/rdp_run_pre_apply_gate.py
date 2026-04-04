#!/usr/bin/env python3
"""运行 Pre-Apply Policy Gate.

工作包 A: 在 apply 前检查门禁条件。

用法:
    python scripts/rdp_run_pre_apply_gate.py \
        --recommendation-id rec_xxx

    python scripts/rdp_run_pre_apply_gate.py \
        --recommendation-id rec_xxx --output gate_result.json

退出码:
    0 = gate pass (allow apply)
    1 = gate block (不允许 apply)
    2 = gate warn (允许但有警告)
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
    p = argparse.ArgumentParser(description="Run pre-apply policy gate")
    p.add_argument("--recommendation-id", required=True)
    p.add_argument("--output", default=None, help="输出 JSON 结果到指定文件")
    p.add_argument("--dry-run", action="store_true", help="不保存结果到 artifacts")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    from aats.data_platform.production_workflow.pre_apply_gate import (
        run_pre_apply_gate,
    )

    result = run_pre_apply_gate(
        ROOT,
        args.recommendation_id,
        save_result=not args.dry_run,
    )

    # 输出摘要
    status = result["gate_status"].upper()
    allow = "YES" if result["allow_apply"] else "NO"
    print(f"Gate Status: {status}")
    print(f"Allow Apply: {allow}")
    print(f"Checks: {result['passed_checks']}/{result['total_checks']} passed")
    print()

    for check in result["checks"]:
        icon = "[PASS]" if check["passed"] else "[FAIL]"
        print(f"  {icon} {check['name']} ({check['severity']}): {check['detail']}")

    if result["blocking_reasons"]:
        print()
        print("BLOCKING REASONS:")
        for r in result["blocking_reasons"]:
            print(f"  - {r}")

    if result["warnings"]:
        print()
        print("WARNINGS:")
        for w in result["warnings"]:
            print(f"  - {w}")

    if args.output:
        out_path = Path(args.output)
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\n[OK] 结果已保存到: {out_path}")

    if result["gate_status"] == "block":
        return 1
    elif result["gate_status"] == "warn":
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
