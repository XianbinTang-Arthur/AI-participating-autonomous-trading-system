from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aats.bootstrap.env_profiles import load_profiled_dotenv_into_process
from apps.decision_engine.main import main


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the local in-memory AATS paper loop.")
    parser.add_argument("--iterations", type=int, default=None, help="Number of local market snapshots to publish.")
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=None,
        help="Sleep interval between market snapshots.",
    )
    parser.add_argument(
        "--profile",
        choices=("spot", "derivatives"),
        required=True,
        help="必填。选择启动时加载的环境模板。",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    load_profiled_dotenv_into_process(ROOT, args.profile)
    summary = asyncio.run(
        main(
            iterations=args.iterations,
            interval_seconds=args.interval_seconds,
        )
    )
    print(json.dumps(summary, indent=2, default=str))
