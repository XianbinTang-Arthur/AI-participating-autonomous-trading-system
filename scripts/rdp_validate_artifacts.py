#!/usr/bin/env python3
"""Phase 5-A: Artifact / Manifest 规范校验.

扫描 artifact 目录中的 round_manifest.json，校验是否符合 Phase 5 统一规范。

Usage:
    python scripts/rdp_validate_artifacts.py

    python scripts/rdp_validate_artifacts.py --phase phase3

    python scripts/rdp_validate_artifacts.py --output artifacts/governance/validation_report.json

Exit codes:
    0 = 全部通过
    1 = 有 error 级别问题
"""

from __future__ import annotations

import argparse
import json
import logging
import pathlib
import sys
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rdp_validate_artifacts")

# 确保项目根目录在 sys.path
_SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from aats.data_platform.governance.manifest_validation import (
    validate_manifest_file,
)


# Phase -> artifact roots 映射
_PHASE_ROOTS: dict[str, list[str]] = {
    "phase2_step1": [
        "artifacts/research/calibration_batches",
        "artifacts/research/calibration_rounds",
    ],
    "phase2_step2": ["artifacts/research/step2_rounds"],
    "phase2_step3": ["artifacts/research/step3_rounds"],
    "phase3": ["artifacts/research/attribution_rounds"],
    "phase4": ["artifacts/research/execution_rounds"],
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 5-A: Artifact / Manifest 规范校验",
    )
    parser.add_argument("--artifact-root", default=str(_PROJECT_ROOT))
    parser.add_argument("--phase", default=None, help="限定 phase (phase3, phase4, ...)")
    parser.add_argument("--output", default=None, help="输出 JSON 路径")
    parser.add_argument(
        "--fix",
        action="store_true",
        help="已禁用：审计 manifest 不允许原地自动改写",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="兼容参数；validator 本身始终只读",
    )
    args = parser.parse_args()
    if args.fix:
        parser.error(
            "--fix 已禁用：validator 只读；legacy manifest 必须迁移到新的 "
            "artifact/round，并重新建立 digest 与 index，禁止原地覆盖审计证据"
        )

    project_root = pathlib.Path(args.artifact_root)
    results = []
    total_errors = 0
    total_warnings = 0

    for phase, rel_paths in _PHASE_ROOTS.items():
        if args.phase and phase != args.phase:
            continue

        phase_manifests: list[pathlib.Path] = []
        for rel_path in rel_paths:
            root = project_root / rel_path
            if not root.exists():
                log.info("跳过: %s (不存在)", root)
                continue
            phase_manifests.extend(root.rglob("round_manifest.json"))

        if not phase_manifests:
            log.info("跳过 phase=%s (无 manifest)", phase)
            continue

        log.info("扫描 %s: %d 个 manifest", phase, len(phase_manifests))

        for mf in sorted(phase_manifests):
            vr = validate_manifest_file(mf)
            results.append(vr.to_dict())
            total_errors += vr.error_count
            total_warnings += vr.warning_count

            if vr.is_valid:
                log.info("  [OK] %s", mf.parent.name)
            else:
                log.warning("  [FAIL] %s (%d errors, %d warnings)",
                            mf.parent.name, vr.error_count, vr.warning_count)
                for issue in vr.issues:
                    if issue.level == "error":
                        log.error("    E: %s - %s", issue.field, issue.message)
                    elif issue.level == "warning":
                        log.warning("    W: %s - %s", issue.field, issue.message)

    # 输出汇总
    print()
    print("=== Artifact Validation Summary ===")
    print(f"Total manifests: {len(results)}")
    print(f"Errors: {total_errors}")
    print(f"Warnings: {total_warnings}")

    if args.output:
        output_path = pathlib.Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_manifests": len(results),
            "total_errors": total_errors,
            "total_warnings": total_warnings,
            "results": results,
        }
        with output_path.open("w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False, default=str)
        print(f"Report: {output_path}")

    if total_errors > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
