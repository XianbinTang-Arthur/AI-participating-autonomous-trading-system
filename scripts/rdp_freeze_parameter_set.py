#!/usr/bin/env python3
"""Phase 5-B: 参数冻结 / 注册管理.

管理 parameter registry: 导入、查看、冻结、废弃参数集。

Usage:
    # 查看当前 registry
    python scripts/rdp_freeze_parameter_set.py --action show

    # 从 parameter_candidates.json 导入
    python scripts/rdp_freeze_parameter_set.py --action import \
        --source artifacts/research/experiments/.../parameter_candidates.json

    # 从 parameter_recommendations.json 导入
    python scripts/rdp_freeze_parameter_set.py --action import \
        --source artifacts/research/experiments/.../parameter_recommendations.json \
        --family independent --timeframe 15m

    # 冻结一个 parameter set
    python scripts/rdp_freeze_parameter_set.py --action freeze \
        --parameter-set-id ps_20260403_123456_abc123

    # 废弃一个 parameter set
    python scripts/rdp_freeze_parameter_set.py --action deprecate \
        --parameter-set-id ps_20260403_123456_abc123

    # 按条件筛选
    python scripts/rdp_freeze_parameter_set.py --action show \
        --family independent --status frozen
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
log = logging.getLogger("rdp_freeze_parameter_set")

_SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from aats.data_platform.governance.parameter_registry import (
    load_registry,
    save_registry,
    add_parameter_set,
    find_parameter_sets,
    freeze_parameter_set,
    deprecate_parameter_set,
    import_from_parameter_candidates,
    import_from_parameter_recommendations,
)

_DEFAULT_REGISTRY = "artifacts/governance/current_parameter_registry.json"


def _print_parameter_sets(sets: list[dict], *, verbose: bool = False) -> None:
    if not sets:
        print("  (empty)")
        return
    for ps in sets:
        status_icon = {
            "draft": "DRFT",
            "candidate": "CAND",
            "frozen": "FRZN",
            "deprecated": "DEPR",
        }.get(ps.get("status", ""), "????")
        print(f"  [{status_icon}] {ps['parameter_set_id']}")
        print(f"         {ps['family']} / {ps['timeframe']} / {ps['symbol']}")
        print(f"         source: {ps.get('source_phase', '?')} / {ps.get('source_round_id', '?')}")
        if verbose:
            print(f"         values: {json.dumps(ps.get('values', {}), ensure_ascii=False)}")
            print(f"         confidence: {ps.get('confidence')}")
            print(f"         created: {ps.get('created_at')}")
            if ps.get("frozen_at"):
                print(f"         frozen: {ps['frozen_at']}")
            if ps.get("notes"):
                print(f"         notes: {ps['notes']}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 5-B: 参数冻结 / 注册管理",
    )
    parser.add_argument("--action", required=True,
                        choices=["show", "import", "freeze", "deprecate"],
                        help="操作: show / import / freeze / deprecate")
    parser.add_argument("--registry", default=_DEFAULT_REGISTRY,
                        help="Registry JSON 路径")
    parser.add_argument("--source", default=None,
                        help="导入来源 JSON 文件路径")
    parser.add_argument("--parameter-set-id", default=None,
                        help="要操作的 parameter set ID")
    parser.add_argument("--family", default=None)
    parser.add_argument("--timeframe", default=None)
    parser.add_argument("--status", default=None)
    parser.add_argument("--source-round-id", default=None)
    parser.add_argument("--source-phase", default=None)
    parser.add_argument("--initial-status", default="draft",
                        choices=["draft", "candidate"])
    parser.add_argument("--notes", default=None)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    registry_path = _PROJECT_ROOT / args.registry
    registry = load_registry(registry_path)

    if args.action == "show":
        sets = find_parameter_sets(
            registry,
            family=args.family,
            timeframe=args.timeframe,
            status=args.status,
        )
        print(f"\n=== Parameter Registry ({len(sets)} sets) ===\n")
        _print_parameter_sets(sets, verbose=args.verbose)

    elif args.action == "import":
        if not args.source:
            print("ERROR: --source 必须指定", file=sys.stderr)
            sys.exit(1)

        source_path = pathlib.Path(args.source)
        if not source_path.exists():
            print(f"ERROR: 文件不存在: {source_path}", file=sys.stderr)
            sys.exit(1)

        # 判断格式
        with source_path.open(encoding="utf-8") as f:
            data = json.load(f)

        new_sets: list[dict] = []

        if "candidates" in data:
            # parameter_candidates.json
            new_sets = import_from_parameter_candidates(
                source_path,
                source_round_id=args.source_round_id,
                source_phase=args.source_phase or "phase2_step2",
                initial_status=args.initial_status,
            )
        elif "recommendations" in data:
            # parameter_recommendations.json
            if not args.family or not args.timeframe:
                print("ERROR: 导入 recommendations 需要 --family 和 --timeframe",
                      file=sys.stderr)
                sys.exit(1)
            ps = import_from_parameter_recommendations(
                source_path,
                family=args.family,
                timeframe=args.timeframe,
                source_round_id=args.source_round_id,
                source_phase=args.source_phase or "phase2_step1",
                initial_status=args.initial_status,
            )
            new_sets = [ps]
        else:
            print(f"ERROR: 无法识别文件格式: {source_path}", file=sys.stderr)
            sys.exit(1)

        print(f"\n=== 将导入 {len(new_sets)} 个 parameter set ===\n")
        _print_parameter_sets(new_sets, verbose=True)

        if not args.dry_run:
            for ps in new_sets:
                add_parameter_set(registry, ps)
            save_registry(registry, registry_path)
            print(f"已保存: {registry_path}")
        else:
            print("[DRY-RUN] 未实际写入")

    elif args.action == "freeze":
        if not args.parameter_set_id:
            print("ERROR: --parameter-set-id 必须指定", file=sys.stderr)
            sys.exit(1)

        if args.dry_run:
            print(f"[DRY-RUN] 将冻结: {args.parameter_set_id}")
        else:
            ok = freeze_parameter_set(
                registry, args.parameter_set_id, notes=args.notes,
            )
            if ok:
                save_registry(registry, registry_path)
                print(f"已冻结: {args.parameter_set_id}")
            else:
                print("冻结失败", file=sys.stderr)
                sys.exit(1)

    elif args.action == "deprecate":
        if not args.parameter_set_id:
            print("ERROR: --parameter-set-id 必须指定", file=sys.stderr)
            sys.exit(1)

        if args.dry_run:
            print(f"[DRY-RUN] 将废弃: {args.parameter_set_id}")
        else:
            ok = deprecate_parameter_set(
                registry, args.parameter_set_id, notes=args.notes,
            )
            if ok:
                save_registry(registry, registry_path)
                print(f"已废弃: {args.parameter_set_id}")
            else:
                print("废弃失败", file=sys.stderr)
                sys.exit(1)


if __name__ == "__main__":
    main()
