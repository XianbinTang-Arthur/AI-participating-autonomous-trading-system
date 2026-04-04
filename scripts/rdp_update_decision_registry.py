#!/usr/bin/env python3
"""Phase 6-E: 更新 Decision / Recommendation Registry.

手动管理 recommendation 状态和 active decision。

Usage:
    # 查看 recommendation registry
    python scripts/rdp_update_decision_registry.py --action show-recommendations

    # 查看 active decision registry
    python scripts/rdp_update_decision_registry.py --action show-decisions

    # 批准一条 recommendation
    python scripts/rdp_update_decision_registry.py --action approve \
        --recommendation-id rec_20260404_123456_abc123

    # 拒绝一条 recommendation
    python scripts/rdp_update_decision_registry.py --action reject \
        --recommendation-id rec_20260404_123456_abc123

    # 查看 evidence bundle 索引
    python scripts/rdp_update_decision_registry.py --action show-bundles
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
log = logging.getLogger("rdp_update_registry")

_SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from aats.data_platform.decision_system.recommendation_registry import (
    load_recommendation_registry,
    save_recommendation_registry,
    load_active_decision_registry,
    load_evidence_bundle_index,
)

_REC_REGISTRY = "artifacts/decision_system/recommendation_registry.json"
_DEC_REGISTRY = "artifacts/decision_system/active_decision_registry.json"
_BUNDLE_INDEX = "artifacts/decision_system/evidence_bundle_index.json"


def _print_recommendations(recs: list[dict], *, verbose: bool = False) -> None:
    if not recs:
        print("  (empty)")
        return
    for rec in recs:
        status_icon = {
            "draft": "DRFT",
            "approved": "APPR",
            "rejected": "REJT",
            "superseded": "SUPR",
        }.get(rec.get("status", ""), "????")
        print(f"  [{status_icon}] {rec['recommendation_id']}")
        print(f"         {rec['family']} / {rec['timeframe']}: {rec['recommendation_type']}")
        print(f"         confidence: {rec.get('confidence')}")
        if verbose:
            print(f"         reason: {rec.get('reason', '')[:120]}")
            print(f"         evidence: {rec.get('evidence_bundle_ref')}")
            print(f"         target_ps: {rec.get('target_parameter_set_id')}")
            print(f"         created: {rec.get('created_at')}")
        print()


def _print_decisions(decisions: list[dict]) -> None:
    if not decisions:
        print("  (empty)")
        return
    for d in decisions:
        icon = {
            "keep_active": "ACT",
            "lower_priority": "LOW",
            "pause": "PAU",
            "require_review": "REV",
        }.get(d.get("current_status", ""), "???")
        print(f"  [{icon}] {d.get('combo_key', d.get('family', '?') + '_' + d.get('timeframe', '?'))}")
        print(f"         status: {d['current_status']}")
        print(f"         active_ps: {d.get('active_parameter_set_id')}")
        print(f"         last_rec: {d.get('last_recommendation_id')}")
        print(f"         updated: {d.get('last_updated_at')}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 6-E: Decision / Recommendation Registry 管理",
    )
    parser.add_argument("--action", required=True,
                        choices=[
                            "show-recommendations", "show-decisions", "show-bundles",
                            "approve", "reject",
                        ])
    parser.add_argument("--recommendation-id", default=None)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    rec_path = _PROJECT_ROOT / _REC_REGISTRY
    dec_path = _PROJECT_ROOT / _DEC_REGISTRY
    bundle_path = _PROJECT_ROOT / _BUNDLE_INDEX

    if args.action == "show-recommendations":
        reg = load_recommendation_registry(rec_path)
        recs = reg.get("recommendations", [])
        print(f"\n=== Recommendation Registry ({len(recs)} items) ===\n")
        _print_recommendations(recs, verbose=args.verbose)

    elif args.action == "show-decisions":
        reg = load_active_decision_registry(dec_path)
        decisions = reg.get("decisions", [])
        print(f"\n=== Active Decision Registry ({len(decisions)} items) ===\n")
        _print_decisions(decisions)

    elif args.action == "show-bundles":
        idx = load_evidence_bundle_index(bundle_path)
        bundles = idx.get("bundles", [])
        print(f"\n=== Evidence Bundle Index ({len(bundles)} items) ===\n")
        for b in bundles:
            print(f"  Round: {b.get('round_id')}")
            print(f"    phases: {b.get('phases_with_data')}")
            print(f"    completeness: {b.get('completeness_ratio', 0):.0%}")
            print(f"    created: {b.get('created_at')}")
            print()

    elif args.action in ("approve", "reject"):
        if not args.recommendation_id:
            print("ERROR: --recommendation-id 必须指定", file=sys.stderr)
            sys.exit(1)

        reg = load_recommendation_registry(rec_path)
        found = False
        new_status = "approved" if args.action == "approve" else "rejected"

        for rec in reg.get("recommendations", []):
            if rec["recommendation_id"] == args.recommendation_id:
                found = True
                old_status = rec.get("status")
                if args.dry_run:
                    print(f"[DRY-RUN] {args.recommendation_id}: {old_status} -> {new_status}")
                else:
                    rec["status"] = new_status
                    save_recommendation_registry(reg, rec_path)
                    print(f"{args.recommendation_id}: {old_status} -> {new_status}")
                break

        if not found:
            print(f"ERROR: 未找到 recommendation: {args.recommendation_id}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
