#!/usr/bin/env python3
"""Phase 5-A: 构建 Artifact Index.

扫描所有 artifact 目录，生成 artifact_index.json。

Usage:
    python scripts/rdp_build_artifact_index.py

    python scripts/rdp_build_artifact_index.py --phase phase3 --phase phase4

    python scripts/rdp_build_artifact_index.py \
        --output artifacts/governance/artifact_index.json
"""

from __future__ import annotations

import argparse
import json
import logging
import pathlib
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rdp_build_artifact_index")

_SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from aats.data_platform.governance.artifact_index import build_artifact_index
from aats.data_platform.governance.snapshot_db import (
    SNAPSHOT_ARTIFACT_INDEX,
    save_governance_snapshot,
)

_DEFAULT_OUTPUT = "artifacts/governance/artifact_index.json"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 5-A: 构建 Artifact Index",
    )
    parser.add_argument("--artifact-root", default=str(_PROJECT_ROOT))
    parser.add_argument("--phase", action="append", default=None,
                        help="限定扫描 phase，可多次指定")
    parser.add_argument("--output", default=_DEFAULT_OUTPUT)
    args = parser.parse_args()

    project_root = pathlib.Path(args.artifact_root)
    phases = args.phase  # None = 全部

    log.info("构建 artifact index...")
    log.info("  Project root: %s", project_root)
    log.info("  Phases: %s", phases or "all")

    index = build_artifact_index(project_root, phases=phases)

    # 输出
    output_path = project_root / args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(index, f, indent=2, ensure_ascii=False, default=str)
    if not save_governance_snapshot(snapshot_type=SNAPSHOT_ARTIFACT_INDEX, payload=index):
        log.warning("artifact_index DB upsert failed; file artifact kept as audit copy")

    summary = index["summary"]
    print()
    print("=== Artifact Index ===")
    print(f"Total: {summary['total_artifacts']}")
    print(f"  Rounds: {summary['rounds']}")
    print(f"  Experiments: {summary['experiments']}")
    print(f"  With manifest: {summary['with_manifest']}")
    print(f"  Valid manifests: {summary['valid_manifests']}")
    print(f"By category: {json.dumps(summary['by_category'], indent=2)}")
    print(f"Output: {output_path}")


if __name__ == "__main__":
    main()
