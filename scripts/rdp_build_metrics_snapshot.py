#!/usr/bin/env python3
"""生成 RDP Metrics Snapshot.

用法:
    python scripts/rdp_build_metrics_snapshot.py
    python scripts/rdp_build_metrics_snapshot.py --family independent --timeframe 15m
    python scripts/rdp_build_metrics_snapshot.py --json
    python scripts/rdp_build_metrics_snapshot.py --catalog

退出码: 0 = 成功
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
    p = argparse.ArgumentParser(description="生成 RDP Metrics Snapshot")
    p.add_argument("--family", default=None, help="按 family 筛选")
    p.add_argument("--timeframe", default=None, help="按 timeframe 筛选")
    p.add_argument("--json", action="store_true", help="JSON 输出")
    p.add_argument("--catalog", action="store_true", help="输出指标目录")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if args.catalog:
        from aats.data_platform.metrics.definitions import get_metric_catalog
        catalog = get_metric_catalog()
        if args.json:
            print(json.dumps(catalog, indent=2, ensure_ascii=False))
        else:
            print(f"RDP Metric Catalog ({len(catalog)} metrics)")
            print()
            current_layer = None
            for m in catalog:
                if m["layer"] != current_layer:
                    current_layer = m["layer"]
                    print(f"  [{current_layer.upper()}]")
                arrow = "↑" if m["direction"] == "higher_is_better" else "↓"
                print(f"    {arrow} {m['name']} ({m['unit']}) — {m['description']}")
            print()
        return 0

    from aats.data_platform.metrics.metric_registry import build_metrics_snapshot

    snapshot = build_metrics_snapshot(ROOT, args.family, args.timeframe)

    if args.json:
        print(json.dumps(snapshot, indent=2, ensure_ascii=False))
    else:
        print(f"RDP Metrics Snapshot")
        print(f"  ID:        {snapshot['snapshot_id']}")
        print(f"  Generated: {snapshot['generated_at']}")
        filt = snapshot.get("filter", {})
        if filt.get("family") or filt.get("timeframe"):
            print(f"  Filter:    family={filt.get('family')}, timeframe={filt.get('timeframe')}")
        print()

        for layer, metrics in snapshot["metrics_by_layer"].items():
            print(f"  [{layer.upper()}]")
            for k, v in metrics.items():
                print(f"    {k}: {v}")
            print()

        summary = snapshot.get("summary", {})
        print(
            f"  Total: {summary.get('total_metrics', 0)} metrics, "
            f"{summary.get('non_zero_metrics', 0)} non-zero"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
