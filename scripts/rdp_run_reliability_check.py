#!/usr/bin/env python3
"""运行 RDP 可靠性检查.

用法:
    python scripts/rdp_run_reliability_check.py
    python scripts/rdp_run_reliability_check.py --json

退出码:
    0 = 全部通过 (healthy)
    1 = 有 critical 告警
    2 = 有 warning 告警
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(description="RDP 可靠性检查")
    p.add_argument("--json", action="store_true", help="JSON 输出")
    args = p.parse_args()

    from aats.data_platform.operations.reliability_checks import run_all_checks
    from aats.data_platform.operations.alerting import (
        build_alert_summary,
        format_alert_summary_text,
    )

    results = run_all_checks(ROOT)
    summary = build_alert_summary(ROOT, results)

    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        print(format_alert_summary_text(summary))

    if summary["critical_alerts"] > 0:
        return 1
    if summary["warning_alerts"] > 0:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
