"""Round Manager CLI — 刷新 active round 索引.

Phase 5 治理层: 扫描所有 round 目录，更新 active_round_index.json。

Usage:
    python -m aats.data_platform.governance.round_manager --refresh-index
"""

from __future__ import annotations

import argparse
import logging
import pathlib
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("governance.round_manager")

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Round Manager: 刷新 active round 索引",
    )
    parser.add_argument(
        "--refresh-index", action="store_true",
        help="扫描所有 round 目录，更新 active_round_index.json",
    )
    args = parser.parse_args(argv)

    if not args.refresh_index:
        parser.print_help()
        return 2

    from .round_status import build_active_round_index
    from ._atomic_io import atomic_json_write
    from .snapshot_db import SNAPSHOT_ACTIVE_ROUND_INDEX, save_governance_snapshot

    log.info("刷新 active round index...")
    index = build_active_round_index(_PROJECT_ROOT)

    gov_dir = _PROJECT_ROOT / "artifacts" / "governance"
    gov_dir.mkdir(parents=True, exist_ok=True)
    out_path = gov_dir / "active_round_index.json"
    atomic_json_write(index, out_path)
    if not save_governance_snapshot(
        snapshot_type=SNAPSHOT_ACTIVE_ROUND_INDEX,
        payload=index,
    ):
        log.error("Active round index DB snapshot 写入失败；拒绝使用陈旧数据库真源")
        return 1

    summary = index["summary"]
    phases = summary.get("phases_with_rounds", [])
    log.info(
        "Active round index: %d rounds, phases: %s",
        summary["total_rounds"],
        ", ".join(phases) if phases else "(none)",
    )
    log.info("写入 -> %s", out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
