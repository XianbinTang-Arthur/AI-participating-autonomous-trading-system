#!/usr/bin/env python3
"""构建 RDP 告警摘要.

用法:
    python scripts/rdp_build_alert_summary.py
    python scripts/rdp_build_alert_summary.py --json
    python scripts/rdp_build_alert_summary.py --acknowledge ALERT_ID

退出码:
    0 = 成功
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
    p = argparse.ArgumentParser(description="构建 RDP 告警摘要")
    p.add_argument("--json", action="store_true", help="JSON 输出")
    p.add_argument("--acknowledge", metavar="ALERT_ID", help="确认指定告警")
    p.add_argument("--current", action="store_true", help="查看当前告警（不重新检查）")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    from aats.data_platform.operations.alerting import (
        acknowledge_alert,
        build_alert_summary,
        format_alert_summary_text,
        load_current_alerts,
    )

    if args.acknowledge:
        ok = acknowledge_alert(ROOT, args.acknowledge)
        if ok:
            print(f"Alert {args.acknowledge} acknowledged.")
        else:
            print(f"Alert {args.acknowledge} not found.")
        return 0

    if args.current:
        summary = load_current_alerts(ROOT)
        if summary is None:
            print("No current alerts. Run reliability check first.")
            return 0
    else:
        from aats.data_platform.operations.reliability_checks import run_all_checks
        results = run_all_checks(ROOT)
        summary = build_alert_summary(ROOT, results)

    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        print(format_alert_summary_text(summary))

    return 0


if __name__ == "__main__":
    sys.exit(main())
