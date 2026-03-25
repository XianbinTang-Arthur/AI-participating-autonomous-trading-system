from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aats.bootstrap.config import build_storage_backends, load_settings
from aats.bootstrap.env_profiles import load_profiled_dotenv_into_process


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Archive cold AATS event-store records.")
    parser.add_argument(
        "--profile",
        choices=("spot", "derivatives", "spot_live", "derivatives_live"),
        required=True,
        help="Profile template to load before archiving.",
    )
    parser.add_argument("--before-ts", type=str, default=None, help="Archive events strictly older than this ISO-8601 timestamp.")
    parser.add_argument("--before-hours", type=float, default=None, help="Archive events older than N hours from now.")
    parser.add_argument("--before-days", type=float, default=None, help="Archive events older than N days from now.")
    parser.add_argument("--summary-only", action="store_true", help="Only print archive summary; do not move events.")
    return parser.parse_args()


def _parse_before_ts(args: argparse.Namespace) -> datetime | None:
    if args.before_ts:
        return datetime.fromisoformat(args.before_ts.replace("Z", "+00:00"))
    if args.before_hours is not None:
        return datetime.now().astimezone() - timedelta(hours=float(args.before_hours))
    if args.before_days is not None:
        return datetime.now().astimezone() - timedelta(days=float(args.before_days))
    return None


def main() -> None:
    args = parse_args()
    load_profiled_dotenv_into_process(ROOT, args.profile)
    settings = load_settings()
    storage = build_storage_backends(settings)
    try:
        before = storage.event_store.archive_summary()
        if args.summary_only:
            result = {
                "status": "summary_only",
                "profile": args.profile,
                "summary": before,
            }
            print(json.dumps(result, indent=2, default=str))
            return

        before_ts = _parse_before_ts(args)
        if before_ts is None:
            raise SystemExit("One of --before-ts, --before-hours, or --before-days is required unless --summary-only is used.")
        archived = storage.event_store.archive_before(before_ts=before_ts)
        after = storage.event_store.archive_summary()
        result = {
            "status": "completed",
            "profile": args.profile,
            "before_ts": before_ts,
            "archived": archived,
            "summary_before": before,
            "summary_after": after,
        }
        print(json.dumps(result, indent=2, default=str))
    finally:
        if storage.database_runtime is not None:
            storage.database_runtime.dispose()


if __name__ == "__main__":
    main()
