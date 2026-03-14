from __future__ import annotations

import asyncio
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    summary = asyncio.run(
        main(
            iterations=args.iterations,
            interval_seconds=args.interval_seconds,
        )
    )
    print(json.dumps(summary, indent=2, default=str))
