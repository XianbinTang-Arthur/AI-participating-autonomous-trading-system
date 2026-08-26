#!/usr/bin/env python3
"""容量预检并登记 OKX 官方 trade/L2 多日恢复计划。"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from aats.data_platform.data_governance.historical_campaign import (
    OkxBulkLinkClient,
    build_campaign_manifest,
    observe_capacity,
    register_campaign,
    write_campaign_manifest,
)
from aats.data_platform.db import get_session


def _day(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTC-USDT-SWAP")
    parser.add_argument("--start", required=True, type=_day)
    parser.add_argument("--days", required=True, type=int)
    parser.add_argument("--storage-root", required=True, type=Path)
    parser.add_argument("--manifest-output", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.days <= 0:
        print("--days 必须为正整数", file=sys.stderr)
        return 4
    if not args.storage_root.expanduser().is_absolute() or not args.manifest_output.expanduser().is_absolute():
        print("存储与 manifest 路径必须是绝对路径", file=sys.stderr)
        return 4
    if args.apply != args.confirm:
        print("--apply 与 --confirm 必须同时使用", file=sys.stderr)
        return 4
    with get_session() as session:
        capacity = observe_capacity(session, args.storage_root, requested_days=args.days)
    plan = {
        "symbol": args.symbol.upper(),
        "coverage_start": args.start.isoformat(),
        "coverage_end": (args.start + timedelta(days=args.days)).isoformat(),
        "storage_root": str(args.storage_root.expanduser().resolve()),
        "capacity_report": asdict(capacity),
        "network": False,
        "live_side_effects": False,
    }
    if not args.apply:
        print(json.dumps({"dry_run": True, "plan": plan}, indent=2, ensure_ascii=False))
        return 2 if capacity.approved else 3
    if not capacity.approved:
        print(json.dumps({"status": "BLOCKED", **plan}, indent=2, ensure_ascii=False))
        return 3
    with httpx.Client(headers={"User-Agent": "AATS-RDP-Historical-Recovery/1.0"}) as client:
        manifest = build_campaign_manifest(
            OkxBulkLinkClient(client),
            symbol=args.symbol.upper(),
            start=args.start,
            end=args.start + timedelta(days=args.days),
            capacity=capacity,
        )
    with get_session() as session:
        campaign_id, status = register_campaign(session, manifest)
    write_campaign_manifest(args.manifest_output, manifest)
    print(
        json.dumps(
            {
                "campaign_id": campaign_id,
                "status": status,
                "manifest_output": str(args.manifest_output.expanduser().resolve()),
                "manifest_fingerprint": manifest["manifest_fingerprint"],
                "capacity_report": asdict(capacity),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
