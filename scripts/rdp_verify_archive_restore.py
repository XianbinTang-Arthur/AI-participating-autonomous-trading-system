#!/usr/bin/env python3
"""在隔离临时表中验证一个 RDP Parquet 归档可恢复。"""

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

from aats.data_platform.data_governance.archive import (  # noqa: E402
    ArchiveScope,
    verify_archive_restore_drill,
)
from aats.data_platform.db import get_session_factory  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parquet", required=True, type=Path)
    parser.add_argument("--batch-size", type=int, default=2_000)
    parser.add_argument("--confirm", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    path = args.parquet.expanduser()
    if not path.is_absolute() or not path.resolve().is_file():
        print("--parquet 必须是存在的绝对文件", file=sys.stderr)
        return 4
    if args.batch_size <= 0:
        print("--batch-size 必须为正整数", file=sys.stderr)
        return 4
    manifest_path = path.resolve().with_suffix(".manifest.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        scope = ArchiveScope(
            source_id=str(manifest["source_id"]),
            dataset_name=str(manifest["dataset_name"]),
            table=str(manifest["table"]),
            symbol=str(manifest["symbol"]),
            coverage_start=datetime.fromisoformat(str(manifest["coverage_start"])),
            coverage_end=datetime.fromisoformat(str(manifest["coverage_end"])),
        )
        expected_sha256 = str(manifest["sha256"])
        expected_rows = int(manifest["row_count"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"ERROR: {type(exc).__name__}", file=sys.stderr)
        return 3
    plan = {
        "parquet": str(path.resolve()),
        "dataset_name": scope.dataset_name,
        "symbol": scope.symbol,
        "coverage_start": scope.coverage_start.isoformat(),
        "coverage_end": scope.coverage_end.isoformat(),
        "expected_rows": expected_rows,
        "permanent_database_writes": False,
    }
    if not args.confirm:
        print(json.dumps({"dry_run": True, "plan": plan}, indent=2, ensure_ascii=False))
        return 2
    try:
        result = verify_archive_restore_drill(
            get_session_factory(),
            scope,
            path.resolve(),
            expected_sha256=expected_sha256,
            expected_rows=expected_rows,
            batch_size=args.batch_size,
        )
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}", file=sys.stderr)
        return 3
    print(json.dumps(asdict(result), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
