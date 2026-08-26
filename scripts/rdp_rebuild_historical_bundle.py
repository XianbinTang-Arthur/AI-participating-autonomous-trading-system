#!/usr/bin/env python3
"""从 ELIGIBLE 历史 bundle 确定性重建隔离的 Silver 数据。"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from aats.data_platform.data_governance.coverage import git_commit
from aats.data_platform.data_governance.historical_rebuild import (
    execute_historical_rebuild,
    fail_historical_rebuild,
    plan_historical_rebuild,
    start_historical_rebuild,
)
from aats.data_platform.db import get_session


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-id", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.apply != args.confirm:
        print("--apply 与 --confirm 必须同时使用", file=sys.stderr)
        return 4
    with get_session() as session:
        plan = plan_historical_rebuild(
            session,
            bundle_id=args.bundle_id,
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
        start_status = start_historical_rebuild(session, plan)
    if start_status == "already_succeeded":
        print(
            json.dumps(
                {
                    "status": "already_succeeded",
                    "operation_key": plan.operation_key,
                },
                indent=2,
            )
        )
        return 0
    try:
        with get_session() as session:
            result = execute_historical_rebuild(session, plan)
    except Exception as exc:
        try:
            with get_session() as session:
                fail_historical_rebuild(
                    session,
                    plan.operation_key,
                    type(exc).__name__,
                )
        except Exception as status_exc:
            print(
                "ERROR: failed to persist rebuild failure state: "
                f"{type(status_exc).__name__}",
                file=sys.stderr,
            )
        print(f"ERROR: {type(exc).__name__}", file=sys.stderr)
        return 3
    print(json.dumps(asdict(result), indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
