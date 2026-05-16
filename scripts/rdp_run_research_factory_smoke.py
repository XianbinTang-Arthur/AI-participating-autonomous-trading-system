#!/usr/bin/env python3
"""Run the Research Factory research-only smoke loop."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aats.data_platform.research_factory.smoke import (  # noqa: E402
    DEFAULT_SMOKE_ARTIFACT_ROOT,
    DEFAULT_SMOKE_EXPERIMENT_ID,
    DEFAULT_SMOKE_FACTOR_EXPRESSION,
    ResearchFactorySmokeConfig,
    run_research_factory_smoke,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a deterministic Research Factory smoke experiment."
    )
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=DEFAULT_SMOKE_ARTIFACT_ROOT,
        help="Research artifact root under artifacts/research.",
    )
    parser.add_argument(
        "--experiment-id",
        default=DEFAULT_SMOKE_EXPERIMENT_ID,
        help="Safe experiment id for the smoke artifact directory.",
    )
    parser.add_argument(
        "--factor-expression",
        default=DEFAULT_SMOKE_FACTOR_EXPRESSION,
        help="Safe Research Factory factor DSL expression.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace the target smoke experiment directory under the artifact root.",
    )
    parser.add_argument("--fee-bps", type=float, default=5.0)
    parser.add_argument("--slippage-bps", type=float, default=2.0)
    parser.add_argument("--funding-bps", type=float, default=0.5)
    parser.add_argument("--periods-per-year", type=float, default=1.0)
    parser.add_argument(
        "--execution-cost-summary",
        type=Path,
        default=None,
        help="Optional Phase 4 execution_cost_summary.json to merge into Research Factory metrics.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = run_research_factory_smoke(
            ResearchFactorySmokeConfig(
                artifact_root=args.artifact_root,
                experiment_id=args.experiment_id,
                factor_expression=args.factor_expression,
                overwrite=args.overwrite,
                fee_bps=args.fee_bps,
                slippage_bps=args.slippage_bps,
                funding_bps=args.funding_bps,
                periods_per_year=args.periods_per_year,
                execution_cost_summary_path=args.execution_cost_summary,
            )
        )
    except Exception as exc:
        print(
            json.dumps(
                {"status": "failed", "candidate_generated": False, "error": str(exc)},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 2

    print(result.to_json(), end="")
    return 0 if result.status == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
