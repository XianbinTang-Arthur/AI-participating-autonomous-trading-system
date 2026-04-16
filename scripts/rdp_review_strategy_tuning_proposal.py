#!/usr/bin/env python3
"""Review an automated strategy tuning proposal."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="审核自动生成的策略调优提案")
    parser.add_argument("--proposal-id", required=True, help="待审核的 proposal_id")
    parser.add_argument(
        "--action",
        required=True,
        choices=["approve", "reject"],
        help="审核动作",
    )
    parser.add_argument("--reviewer", default="operator", help="审核人")
    parser.add_argument("--notes", default=None, help="审核备注")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    from aats.data_platform.operations.strategy_tuning_registry import (
        review_strategy_tuning_proposal,
    )

    result = review_strategy_tuning_proposal(
        ROOT,
        proposal_id=args.proposal_id,
        action=args.action,
        reviewer=args.reviewer,
        notes=args.notes,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("Strategy Tuning Proposal Review")
        print(f"  OK:           {result.get('ok')}")
        print(f"  Message:      {result.get('message')}")
        proposal = result.get("proposal") or {}
        if proposal:
            print(f"  Proposal ID:  {proposal.get('proposal_id')}")
            print(f"  Combo:        {proposal.get('combo_key')}")
            print(f"  Parameter:    {proposal.get('parameter')}")
            print(f"  Status:       {proposal.get('status')}")
            print(f"  Proposed:     {proposal.get('proposed_value')}")
        if result.get("registry_path"):
            print(f"  Registry:     {result.get('registry_path')}")

    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
