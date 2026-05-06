#!/usr/bin/env python3
"""Generate a read-only missed-market replay report from live AATS events."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aats.services.operator.missed_market_replay import (  # noqa: E402
    analyze_dataset,
    build_markdown_report,
    dumps_json_report,
    fetch_replay_dataset,
    parse_replay_timestamp,
    write_report,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a read-only missed-market replay report.",
    )
    parser.add_argument("--symbol", default="BTC-USDT-SWAP")
    parser.add_argument("--start", required=True, help="timezone-aware start timestamp")
    parser.add_argument("--end", required=True, help="timezone-aware end timestamp")
    parser.add_argument(
        "--database-url-env",
        default="AATS_DATABASE_URL",
        help="environment variable containing the SQLAlchemy database URL",
    )
    parser.add_argument(
        "--format",
        choices=["markdown", "json"],
        default="markdown",
        help="output format",
    )
    parser.add_argument("--output", type=Path, help="optional output file")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    database_url = os.environ.get(args.database_url_env)
    if not database_url:
        print(
            f"[ERROR] database URL environment variable is not set: {args.database_url_env}",
            file=sys.stderr,
        )
        return 2

    try:
        start_ts = parse_replay_timestamp(args.start)
        end_ts = parse_replay_timestamp(args.end)
        if end_ts <= start_ts:
            raise ValueError("--end must be later than --start")
        dataset = fetch_replay_dataset(
            database_url=database_url,
            symbol=args.symbol,
            start_ts=start_ts,
            end_ts=end_ts,
        )
        analysis = analyze_dataset(dataset)
        content = (
            dumps_json_report(dataset, analysis)
            if args.format == "json"
            else build_markdown_report(dataset, analysis)
        )
        if args.output:
            write_report(args.output, content)
            print(f"[OK] replay report written: {args.output}")
        else:
            print(content)
        return 0
    except Exception as exc:
        print(f"[ERROR] missed-market replay failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

