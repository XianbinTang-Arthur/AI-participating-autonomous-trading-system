#!/usr/bin/env python3
"""Generate immutable JSON and Markdown RDP coverage evidence."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from aats.data_platform.data_governance.coverage import audit_coverage, render_markdown
from aats.data_platform.db import get_engine
from aats.data_platform.governance._atomic_io import (
    immutable_bytes_write,
    immutable_json_write,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="RDP 数据覆盖只读审计")
    parser.add_argument("--window-days", type=int, default=90)
    parser.add_argument("--statement-timeout-ms", type=int, default=15_000)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=_ROOT / "artifacts/data_governance/coverage",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.output_dir.expanduser().is_absolute():
        print("--output-dir 必须是绝对路径", file=sys.stderr)
        return 4
    report = audit_coverage(
        get_engine(),
        window_days=args.window_days,
        statement_timeout_ms=args.statement_timeout_ms,
        project_root=str(_ROOT),
    )
    operation_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    base = args.output_dir.expanduser().resolve() / f"coverage_{operation_id}"
    json_digest = immutable_json_write(report, base.with_suffix(".json"))
    markdown_digest = immutable_bytes_write(
        render_markdown(report).encode("utf-8"),
        base.with_suffix(".md"),
    )
    print(
        f"coverage_audit_ok json={base.with_suffix('.json')} sha256={json_digest} "
        f"markdown={base.with_suffix('.md')} sha256={markdown_digest}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
