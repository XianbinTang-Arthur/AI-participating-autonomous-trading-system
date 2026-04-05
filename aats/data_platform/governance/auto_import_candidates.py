"""自动导入最新 Step 3 参数候选到治理层 Registry.

解决的问题:
  Step 3 产出 parameter_candidates_merged.json (14 参数/combo)，
  但此前从未自动导入到 current_parameter_registry.json，
  导致 registry 中只存储了 4 个参数（网格扫描 3 个 + scale 1 个），
  其余 10 个关键参数（entry_threshold、close_threshold 等）丢失。

使用方式:
  # CLI 独立调用
  python -m aats.data_platform.governance.auto_import_candidates --run

  # 被 governance_cycle workflow 或 full pipeline 自动调用
  from aats.data_platform.governance.auto_import_candidates import (
      auto_import_latest_candidates,
  )
  result = auto_import_latest_candidates(project_root)
"""

from __future__ import annotations

import json
import logging
import pathlib
from datetime import datetime, timezone
from typing import Any

from .parameter_registry import (
    add_parameter_set,
    deprecate_parameter_set,
    find_parameter_sets,
    import_from_parameter_candidates,
    load_registry,
    save_registry,
)

log = logging.getLogger(__name__)

_STEP3_ARTIFACT_DIR = "artifacts/research/step3_rounds"
_REGISTRY_PATH = "artifacts/governance/current_parameter_registry.json"


# ── 查找最新候选文件 ───────────────────────────────────────────────────


def find_latest_step3_candidates(
    project_root: pathlib.Path,
) -> pathlib.Path | None:
    """查找最新的 Step 3 parameter_candidates_merged.json.

    按 round 目录名倒序排列，返回第一个包含该文件的路径。
    """
    step3_dir = project_root / _STEP3_ARTIFACT_DIR
    if not step3_dir.exists():
        log.warning("Step 3 artifact 目录不存在: %s", step3_dir)
        return None

    rounds = sorted(
        [d for d in step3_dir.iterdir() if d.is_dir()],
        key=lambda d: d.name,
        reverse=True,
    )

    for round_dir in rounds:
        candidates_file = round_dir / "parameter_candidates_merged.json"
        if candidates_file.exists():
            log.info("找到最新 Step 3 候选文件: %s", candidates_file)
            return candidates_file

    log.warning("未找到任何 parameter_candidates_merged.json")
    return None


# ── 自动导入主逻辑 ──────────────────────────────────────────────────


def auto_import_latest_candidates(
    project_root: pathlib.Path,
    *,
    initial_status: str = "candidate",
    deprecate_old: bool = True,
) -> dict[str, Any]:
    """自动导入最新 Step 3 参数候选到 registry.

    流程:
      1. 查找最新 parameter_candidates_merged.json
      2. 检查是否已导入（按 round_id 去重）
      3. 可选：废弃同 family/timeframe 的旧 candidate 参数集
      4. 导入全部参数（14 个/combo）到 registry

    Returns:
        {
            "status": "imported" | "already_imported" | "no_candidates",
            "imported_count": int,
            "deprecated_count": int,
            "source_file": str | None,
            "source_round_id": str | None,
            "parameter_sets": [...]
        }
    """
    result: dict[str, Any] = {
        "status": "no_candidates",
        "imported_count": 0,
        "deprecated_count": 0,
        "source_file": None,
        "source_round_id": None,
        "parameter_sets": [],
    }

    # Step 1: 查找最新候选文件
    candidates_file = find_latest_step3_candidates(project_root)
    if candidates_file is None:
        return result

    result["source_file"] = str(candidates_file)

    # 解析 round_id
    with candidates_file.open(encoding="utf-8") as f:
        data = json.load(f)
    source_round_id = data.get("round_id", candidates_file.parent.name)
    result["source_round_id"] = source_round_id

    # Step 2: 加载 registry，检查去重
    registry_path = project_root / _REGISTRY_PATH
    registry = load_registry(registry_path)

    existing_sets = registry.get("parameter_sets", [])
    already_imported = any(
        ps.get("source_round_id") == source_round_id
        and ps.get("source_phase") == "step3_merged"
        for ps in existing_sets
    )
    if already_imported:
        log.info("Round %s 的参数已导入过 registry，跳过", source_round_id)
        result["status"] = "already_imported"
        return result

    # Step 3: 导入全部参数
    new_sets = import_from_parameter_candidates(
        candidates_file,
        source_round_id=source_round_id,
        source_phase="step3_merged",
        initial_status=initial_status,
    )

    if not new_sets:
        log.warning("从 %s 未能解析到参数集 (文件存在但 candidates 为空)", candidates_file)
        result["status"] = "parse_empty"
        return result

    # Step 4: 可选 — 废弃同 family/timeframe 的旧 candidate
    deprecated_count = 0
    if deprecate_old:
        for new_ps in new_sets:
            old_candidates = find_parameter_sets(
                registry,
                family=new_ps["family"],
                timeframe=new_ps["timeframe"],
                status="candidate",
            )
            for old_ps in old_candidates:
                # 只废弃同源(step3_merged)的旧候选，避免误废弃手动创建的 A/B test 候选
                if (
                    old_ps["parameter_set_id"] != new_ps["parameter_set_id"]
                    and old_ps.get("source_phase") == "step3_merged"
                ):
                    deprecate_parameter_set(
                        registry,
                        old_ps["parameter_set_id"],
                        notes=(
                            f"被 round {source_round_id} 的新候选替代 "
                            f"({datetime.now(timezone.utc).isoformat()})"
                        ),
                    )
                    deprecated_count += 1

    # Step 5: 添加新参数集到 registry
    for ps in new_sets:
        add_parameter_set(registry, ps)
        log.info(
            "导入参数集: %s (%s/%s, %d 个参数)",
            ps["parameter_set_id"],
            ps["family"],
            ps["timeframe"],
            len(ps.get("values", {})),
        )

    save_registry(registry, registry_path)

    result["status"] = "imported"
    result["imported_count"] = len(new_sets)
    result["deprecated_count"] = deprecated_count
    result["parameter_sets"] = [
        {
            "id": ps["parameter_set_id"],
            "family": ps["family"],
            "timeframe": ps["timeframe"],
            "param_count": len(ps.get("values", {})),
            "status": ps["status"],
        }
        for ps in new_sets
    ]

    log.info(
        "自动导入完成: %d 个参数集导入, %d 个旧候选废弃",
        len(new_sets),
        deprecated_count,
    )
    return result


# ── CLI 入口 ──────────────────────────────────────────────────────────


def main() -> None:
    """CLI 入口: python -m aats.data_platform.governance.auto_import_candidates"""
    import argparse
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="自动导入最新 Step 3 参数候选到治理层 registry",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="执行导入",
    )
    parser.add_argument(
        "--project-root",
        default=".",
        help="项目根目录 (默认: 当前目录)",
    )
    parser.add_argument(
        "--initial-status",
        default="candidate",
        choices=["draft", "candidate"],
        help="新导入参数集的初始状态 (默认: candidate)",
    )
    parser.add_argument(
        "--no-deprecate-old",
        action="store_true",
        help="不废弃旧 candidate 参数集",
    )
    args = parser.parse_args()

    if not args.run:
        print("使用 --run 执行导入")
        sys.exit(0)

    project_root = pathlib.Path(args.project_root).resolve()
    result = auto_import_latest_candidates(
        project_root,
        initial_status=args.initial_status,
        deprecate_old=not args.no_deprecate_old,
    )

    print(f"\n=== 自动导入结果 ===")
    print(f"  状态: {result['status']}")
    print(f"  来源: {result['source_file']}")
    print(f"  Round ID: {result['source_round_id']}")
    print(f"  导入数量: {result['imported_count']}")
    print(f"  废弃数量: {result['deprecated_count']}")

    if result["parameter_sets"]:
        print(f"\n  新参数集:")
        for ps in result["parameter_sets"]:
            print(f"    [{ps['status'].upper()}] {ps['id']}")
            print(f"      {ps['family']}/{ps['timeframe']} — {ps['param_count']} 个参数")

    sys.exit(0 if result["status"] in ("imported", "already_imported") else
             2 if result["status"] == "parse_empty" else 1)


if __name__ == "__main__":
    main()
