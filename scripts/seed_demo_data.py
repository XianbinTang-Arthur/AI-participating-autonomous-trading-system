from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aats.bootstrap.config import build_runtime, load_settings


async def main() -> dict:
    settings = load_settings()
    runtime = await build_runtime(settings)
    snapshot = await runtime.market_gateway.seed_demo_snapshot(settings.default_symbol)
    return snapshot.model_dump(mode="json")


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main()), indent=2, default=str))
