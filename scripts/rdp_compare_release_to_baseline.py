#!/usr/bin/env python3
"""对比 Release 与 Baseline.

用法:
    python scripts/rdp_compare_release_to_baseline.py --release-id rel_20260404_...
    python scripts/rdp_compare_release_to_baseline.py --release-id rel_20260404_... --json

退出码:
    0 = improvement 或 neutral
    1 = regression
    2 = insufficient_evidence 或 no_baseline
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
    p = argparse.ArgumentParser(description="对比 Release 与 Baseline")
    p.add_argument("--release-id", required=True, help="Release ID")
    p.add_argument("--json", action="store_true", help="JSON 输出")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    from aats.data_platform.metrics.baseline_comparison import (
        compare_release_to_baseline,
    )

    result = compare_release_to_baseline(ROOT, args.release_id)

    if result.get("error"):
        print(f"[ERROR] {result['error']}")
        return 2

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"Baseline Comparison")
        print(f"  Release:    {result.get('release_id')}")
        print(f"  Family:     {result.get('family')}")
        print(f"  Timeframe:  {result.get('timeframe')}")
        print(f"  Baseline:   {result.get('baseline_type', 'none')}")
        print(f"  Conclusion: {result.get('conclusion', '?')}")
        print(f"  Detail:     {result.get('detail', '')}")
        print()

        diffs = result.get("parameter_diffs", [])
        if diffs:
            print(f"  Parameter Changes ({len(diffs)}):")
            for d in diffs:
                print(
                    f"    {d['parameter']}: {d.get('baseline')} → {d.get('current')}"
                    f" (delta={d.get('delta', '?')})"
                )
            print()

    conclusion = result.get("conclusion", "")
    if conclusion == "regression":
        return 1
    if conclusion in ("insufficient_evidence", "no_baseline"):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
