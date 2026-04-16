#!/usr/bin/env python3
"""Run the latest automated strategy tuning review."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行自动策略调优审查")
    parser.add_argument("--dry-run", action="store_true", help="只评估，不写 artifacts 和提案注册表")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    from aats.data_platform.operations.strategy_tuning_review import (
        build_strategy_tuning_review,
    )

    result = build_strategy_tuning_review(
        ROOT,
        save_results=not args.dry_run,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("Strategy Tuning Review")
        print(f"  Review ID:             {result.get('review_id')}")
        print(f"  Step2 Round:           {result.get('step2_round_id')}")
        print(f"  Phase4 Round:          {result.get('phase4_round_id')}")
        print(f"  Global Recommendation: {result.get('global_recommendation')}")
        print(f"  Cost Gate Recheck:     {result.get('recommend_cost_gate_reassessment')}")
        print(f"  Proposal Count:        {result.get('proposal_count', 0)}")
        print()
        for combo in result.get("combos", []):
            print(f"- {combo.get('combo_key')}")
            print(
                f"  blocker={combo.get('dominant_blocker')} "
                f"focus={combo.get('suggested_focus')} "
                f"cost_gate={combo.get('recommend_cost_gate_reassessment')}"
            )
            print(f"  rationale={combo.get('rationale')}")
        if result.get("proposals"):
            print()
            print("Generated Proposals:")
            for proposal in result["proposals"]:
                print(
                    f"  - {proposal.get('combo_key')} {proposal.get('parameter')}: "
                    f"{proposal.get('current_value')} -> {proposal.get('proposed_value')} "
                    f"[{proposal.get('status')}]"
                )
        if result.get("artifacts"):
            print()
            print(f"Summary: {result['artifacts'].get('summary_path')}")
            print(f"Report:  {result['artifacts'].get('report_path')}")
        if result.get("proposal_registry_path"):
            print(f"Registry: {result.get('proposal_registry_path')}")

    print()
    print("Strategy tuning review completed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
