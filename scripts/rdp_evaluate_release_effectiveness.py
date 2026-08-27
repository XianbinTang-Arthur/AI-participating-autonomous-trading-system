#!/usr/bin/env python3
"""评估 Release Effectiveness.

用法:
    python scripts/rdp_evaluate_release_effectiveness.py --release-id rel_20260404_...
    python scripts/rdp_evaluate_release_effectiveness.py --release-id rel_20260404_... --json
    python scripts/rdp_evaluate_release_effectiveness.py --release-id rel_20260404_... --enforce

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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="评估 Release Effectiveness")
    p.add_argument("--release-id", required=True, help="Release ID")
    p.add_argument("--json", action="store_true", help="JSON 输出")
    action = p.add_mutually_exclusive_group()
    action.add_argument(
        "--dry-run",
        action="store_true",
        help="只评估，不保存 effectiveness，也不执行风险收敛动作",
    )
    action.add_argument(
        "--enforce",
        action="store_true",
        help="对 rollback_triggered 结论显式执行受控风险收敛",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    from aats.data_platform.metrics.release_effectiveness import (
        evaluate_release_effectiveness,
    )

    result = evaluate_release_effectiveness(
        ROOT,
        args.release_id,
        save_result=not args.dry_run,
    )

    if result.get("error"):
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(f"[ERROR] {result['error']}")
        return 3

    conclusion = result.get("conclusion", "")
    output = dict(result)

    if conclusion == "rollback_triggered":
        if args.enforce:
            from aats.data_platform.metrics.release_effectiveness import (
                enforce_pending_rollbacks,
            )

            rb_results = enforce_pending_rollbacks(
                ROOT,
                release_ids={args.release_id},
            )
            output["risk_convergence"] = {
                "requested": True,
                "results": rb_results,
            }
        else:
            output["risk_convergence"] = {
                "requested": False,
                "status": "not_enforced",
                "reason": "explicit --enforce is required",
            }

    if not args.json:
        print("Release Effectiveness Evaluation")
        print(f"  Release:    {output.get('release_id')}")
        print(f"  Family:     {output.get('family')}")
        print(f"  Timeframe:  {output.get('timeframe')}")
        print(f"  Conclusion: {output.get('conclusion', '?')}")
        print(f"  Detail:     {output.get('detail', '')}")
        if output.get("baseline_comparison_conclusion"):
            print(f"  Baseline:   {output['baseline_comparison_conclusion']}")
        print()

        for dim in output.get("dimensions", []):
            icon = {
                "positive": "[+]",
                "negative": "[-]",
                "mixed": "[~]",
                "unknown": "[?]",
            }.get(dim["score"], "[?]")
            print(f"  {icon} {dim['dimension']}: {dim.get('detail', '')}")
        if conclusion == "rollback_triggered" and not args.enforce:
            print("\n[SAFE] 未执行风险收敛；如需执行必须显式传入 --enforce。")
        elif conclusion == "rollback_triggered":
            print("\n[ACTION] 已显式请求风险收敛：")
            for rb in output["risk_convergence"]["results"]:
                if rb.get("ok"):
                    rb_msg = rb.get("rollback_result", {}).get("message", "")
                    print(f"  [OK] {rb.get('release_id')}: {rb_msg}")
                else:
                    rb_err = rb.get("error") or rb.get("rollback_result", {}).get(
                        "message", ""
                    )
                    print(f"  [FAIL] {rb.get('release_id')}: {rb_err}")
    else:
        print(json.dumps(output, indent=2, ensure_ascii=False))

    if conclusion == "effective":
        return 0
    if conclusion in ("ineffective", "rollback_triggered"):
        return 1
    if conclusion == "mixed":
        return 2
    return 3  # insufficient_evidence


if __name__ == "__main__":
    sys.exit(main())
