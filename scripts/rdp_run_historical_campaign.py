#!/usr/bin/env python3
"""执行已登记的官方历史恢复 campaign，并在每个阶段写入 checkpoint。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from aats.data_platform.data_governance.historical_campaign_runner import (
    run_historical_campaign,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--storage-root", required=True, type=Path)
    parser.add_argument("--resume-running", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args(argv)
    if args.apply != args.confirm:
        print("--apply 与 --confirm 必须同时使用", file=sys.stderr)
        return 4
    if not args.storage_root.expanduser().is_absolute():
        print("--storage-root 必须是绝对路径", file=sys.stderr)
        return 4
    if not args.apply:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "campaign_id": args.campaign_id,
                    "storage_root": str(args.storage_root.expanduser().resolve()),
                    "live_side_effects": False,
                    "private_account_access": False,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 2
    try:
        result = run_historical_campaign(
            campaign_id=args.campaign_id,
            storage_root=args.storage_root,
            project_root=_ROOT,
            resume_running=args.resume_running,
        )
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}", file=sys.stderr)
        return 3
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
