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
    validate_campaign_snapshot_evidence,
    write_campaign_manifest,
)
from aats.data_platform.data_governance.registry import (
    register_instrument_contract_snapshot_source,
)
from aats.data_platform.db import get_session
from aats.domain.instrument_contract_snapshot import (
    InstrumentContractSnapshot,
)
from aats.domain.instrument_scope import (
    INSTRUMENT_SCOPE_UNSUPPORTED_REASON,
    classify_instrument_scope,
)


def _day(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTC-USDT-SWAP")
    parser.add_argument("--start", required=True, type=_day)
    parser.add_argument("--days", required=True, type=int)
    parser.add_argument("--storage-root", required=True, type=Path)
    parser.add_argument("--manifest-output", required=True, type=Path)
    parser.add_argument(
        "--instrument-snapshot",
        type=Path,
        help="可选的绝对路径合约快照；缺失时 campaign 只保留未绑定原始证据",
    )
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
    symbol = str(args.symbol or "").strip().upper()
    if classify_instrument_scope(symbol) != "swap":
        print(INSTRUMENT_SCOPE_UNSUPPORTED_REASON, file=sys.stderr)
        return 4
    snapshot: InstrumentContractSnapshot | None = None
    if args.instrument_snapshot is not None:
        snapshot_path = args.instrument_snapshot.expanduser()
        if not snapshot_path.is_absolute() or not snapshot_path.is_file():
            print("--instrument-snapshot 必须指向现有绝对路径文件", file=sys.stderr)
            return 4
        try:
            snapshot_payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
            snapshot = InstrumentContractSnapshot.from_dict(snapshot_payload)
            snapshot.validate_window(
                symbol=symbol,
                start=args.start,
                end=args.start + timedelta(days=args.days),
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"instrument snapshot 无效: {exc}", file=sys.stderr)
            return 4
    if args.apply and snapshot is None:
        print(
            "完整 campaign 必须提供覆盖全窗口的 instrument snapshot；"
            "未绑定历史只允许保留既有只读证据",
            file=sys.stderr,
        )
        return 4
    if snapshot is not None:
        try:
            validate_campaign_snapshot_evidence(
                snapshot,
                symbol=symbol,
                start=args.start,
                end=args.start + timedelta(days=args.days),
            )
        except (RuntimeError, ValueError) as exc:
            print(f"instrument snapshot 证据不可用: {exc}", file=sys.stderr)
            return 4
    snapshot_source_id = None
    with get_session() as session:
        capacity = observe_capacity(session, args.storage_root, requested_days=args.days)
        if snapshot is not None and capacity.approved and args.apply:
            snapshot_source_id = register_instrument_contract_snapshot_source(
                session,
                snapshot,
            )
    plan = {
        "symbol": symbol,
        "coverage_start": args.start.isoformat(),
        "coverage_end": (args.start + timedelta(days=args.days)).isoformat(),
        "storage_root": str(args.storage_root.expanduser().resolve()),
        "capacity_report": asdict(capacity),
        "network": False,
        "live_side_effects": False,
        "instrument_snapshot_digest": None if snapshot is None else snapshot.digest,
        "contract_binding_status": "UNBOUND" if snapshot is None else "BOUND",
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
            symbol=symbol,
            start=args.start,
            end=args.start + timedelta(days=args.days),
            capacity=capacity,
            instrument_contract_snapshot=snapshot,
            instrument_snapshot_source_id=snapshot_source_id,
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
