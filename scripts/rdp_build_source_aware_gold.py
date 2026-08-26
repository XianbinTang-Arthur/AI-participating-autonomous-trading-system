#!/usr/bin/env python3
"""从 ELIGIBLE 历史 bundle 构建可追溯 Gold、质量报告与制品索引。"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from aats.data_platform.data_governance.coverage import git_commit
from aats.data_platform.data_governance.historical_gold import (
    execute_historical_gold,
    fail_historical_gold,
    plan_historical_gold,
    start_historical_gold,
)
from aats.data_platform.db import get_session


def _utc(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("时间必须带 UTC offset")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--timeframe", choices=("15m", "1H"), required=True)
    parser.add_argument("--candle-bundle-id", required=True)
    parser.add_argument("--funding-bundle-id")
    parser.add_argument("--auxiliary-bundle-id", action="append", default=[])
    parser.add_argument("--start", type=_utc)
    parser.add_argument("--end", type=_utc)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if (args.start is None) != (args.end is None):
        print("--start 与 --end 必须同时使用", file=sys.stderr)
        return 4
    if args.apply != args.confirm:
        print("--apply 与 --confirm 必须同时使用", file=sys.stderr)
        return 4
    with get_session() as session:
        plan = plan_historical_gold(
            session,
            symbol=args.symbol,
            timeframe=args.timeframe,
            candle_bundle_id=args.candle_bundle_id,
            funding_bundle_id=args.funding_bundle_id,
            auxiliary_bundle_ids=args.auxiliary_bundle_id,
            coverage_start=args.start,
            coverage_end=args.end,
            git_commit=git_commit(str(_ROOT)),
        )
    if not args.apply:
        print(
            json.dumps(
                {"dry_run": True, "plan": asdict(plan), "live_side_effects": False},
                indent=2,
                ensure_ascii=False,
                default=str,
            )
        )
        return 2

    with get_session() as session:
        start_status, artifact_id = start_historical_gold(session, plan)
    if start_status == "already_succeeded":
        print(json.dumps({"status": start_status, "artifact_id": artifact_id}, indent=2))
        return 0
    try:
        with get_session() as session:
            result = execute_historical_gold(session, plan, artifact_id=artifact_id)
    except Exception as exc:
        try:
            with get_session() as session:
                fail_historical_gold(session, artifact_id, type(exc).__name__)
        except Exception as state_exc:
            print(
                f"ERROR: failed to persist terminal state: {type(state_exc).__name__}",
                file=sys.stderr,
            )
        print(f"ERROR: {type(exc).__name__}", file=sys.stderr)
        return 3
    print(json.dumps(asdict(result), indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
