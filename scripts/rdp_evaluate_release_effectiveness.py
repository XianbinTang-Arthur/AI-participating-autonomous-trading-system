#!/usr/bin/env python3
"""评估 Release Effectiveness.

用法:
    python scripts/rdp_evaluate_release_effectiveness.py --release-id rel_20260404_...
    python scripts/rdp_evaluate_release_effectiveness.py --release-id rel_20260404_... --json

退出码:
    0 = effective
    1 = ineffective 或 rollback_triggered
    2 = mixed
    3 = insufficient_evidence
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
    p = argparse.ArgumentParser(description="评估 Release Effectiveness")
    p.add_argument("--release-id", required=True, help="Release ID")
    p.add_argument("--json", action="store_true", help="JSON 输出")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    from aats.data_platform.metrics.release_effectiveness import (
        evaluate_release_effectiveness,
    )

    result = evaluate_release_effectiveness(ROOT, args.release_id)

    if result.get("error"):
        print(f"[ERROR] {result['error']}")
        return 3

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"Release Effectiveness Evaluation")
        print(f"  Release:    {result.get('release_id')}")
        print(f"  Family:     {result.get('family')}")
        print(f"  Timeframe:  {result.get('timeframe')}")
        print(f"  Conclusion: {result.get('conclusion', '?')}")
        print(f"  Detail:     {result.get('detail', '')}")
        if result.get("baseline_comparison_conclusion"):
            print(f"  Baseline:   {result['baseline_comparison_conclusion']}")
        print()

        for dim in result.get("dimensions", []):
            icon = {
                "positive": "[+]",
                "negative": "[-]",
                "mixed": "[~]",
                "unknown": "[?]",
            }.get(dim["score"], "[?]")
            print(f"  {icon} {dim['dimension']}: {dim.get('detail', '')}")

    conclusion = result.get("conclusion", "")

    # ── P2: rollback_triggered 时自动执行回滚 ──────────────────────
    if conclusion == "rollback_triggered":
        from aats.data_platform.metrics.release_effectiveness import (
            enforce_pending_rollbacks,
        )

        print("\n[ACTION] 检测到 rollback_triggered，执行自动回滚...")
        rb_results = enforce_pending_rollbacks(ROOT)
        for rb in rb_results:
            if rb.get("ok"):
                rb_msg = rb.get("rollback_result", {}).get("message", "")
                print(f"  [OK] {rb.get('release_id')}: {rb_msg}")
            else:
                rb_err = rb.get("error") or rb.get("rollback_result", {}).get("message", "")
                print(f"  [FAIL] {rb.get('release_id')}: {rb_err}")

    if conclusion == "effective":
        return 0
    if conclusion in ("ineffective", "rollback_triggered"):
        return 1
    if conclusion == "mixed":
        return 2
    return 3  # insufficient_evidence


if __name__ == "__main__":
    sys.exit(main())
