"""Artifact Index CLI — 构建并验证 artifact 索引.

Phase 5 治理层: 扫描 artifacts/ 目录，生成 artifact_index.json。

Usage:
    python -m aats.data_platform.governance.artifact_indexer --validate
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
log = logging.getLogger("governance.artifact_indexer")

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Artifact Index: 构建并验证 artifact 索引",
    )
    parser.add_argument(
        "--validate", action="store_true",
        help="扫描 artifacts/ 目录，构建 artifact_index.json",
    )
    args = parser.parse_args()

    if not args.validate:
        parser.print_help()
        return 2

    from .artifact_index import build_artifact_index
    from ._atomic_io import atomic_json_write

    log.info("构建 artifact index...")
    index = build_artifact_index(_PROJECT_ROOT)

    gov_dir = _PROJECT_ROOT / "artifacts" / "governance"
    gov_dir.mkdir(parents=True, exist_ok=True)
    out_path = gov_dir / "artifact_index.json"
    atomic_json_write(index, out_path)

    summary = index["summary"]
    log.info(
        "Artifact index: %d artifacts (%d rounds, %d experiments), %d valid manifests",
        summary["total_artifacts"],
        summary["rounds"],
        summary["experiments"],
        summary["valid_manifests"],
    )
    log.info("写入 -> %s", out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
