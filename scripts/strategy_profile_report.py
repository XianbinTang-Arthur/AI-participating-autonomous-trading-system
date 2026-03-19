from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from aats.bootstrap.config import build_runtime
from aats.bootstrap.settings import AATSSettings
from aats.services.operator.strategy_profiles import StrategyProfileControlService


async def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Export the current strategy profile space and comparison report."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional output path. Defaults to stdout.",
    )
    args = parser.parse_args()

    settings = AATSSettings.model_validate({})
    runtime = await build_runtime(settings)
    try:
        report = StrategyProfileControlService(runtime).snapshot()
        payload = {
            "scope": report.get("scope"),
            "activation": report.get("activation"),
            "profile_space": report.get("profile_space"),
            "comparison_report": report.get("comparison_report"),
            "latest_optimization_report": report.get("latest_optimization_report"),
            "execution_parameter_suggestion_capability": report.get(
                "execution_parameter_suggestion_capability"
            ),
        }
        rendered = json.dumps(payload, ensure_ascii=False, indent=2)
        if args.output is None:
            print(rendered)
        else:
            args.output.write_text(rendered, encoding="utf-8")
    finally:
        await runtime.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
