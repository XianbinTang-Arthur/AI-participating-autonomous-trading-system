from __future__ import annotations

import argparse
import json
from pathlib import Path

from aats.data_platform.metrics.effectiveness_backfill import (
    backfill_release_effectiveness,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backfill rolled_back release effectiveness results.")
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--release-id", action="append", default=[])
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    result = backfill_release_effectiveness(
        args.project_root,
        release_ids=args.release_id or None,
        save_result=not args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("error_count", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
