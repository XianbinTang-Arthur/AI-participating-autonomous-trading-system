from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aats.bootstrap.config import build_runtime, build_storage_backends, load_settings
from aats.services.portfolio_service.pnl import PortfolioPnLCalculator
from aats.services.portfolio_service.reconstruction import PortfolioReconstructionService
from aats.services.portfolio_service.snapshots import PortfolioSnapshotBuilder
from aats.services.reconciliation_service.replay import ReplayEngine


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay AATS events deterministically.")
    parser.add_argument(
        "--bootstrap-local-loop",
        action="store_true",
        help="Run a short local paper loop before replaying the event store.",
    )
    parser.add_argument("--iterations", type=int, default=None, help="Local loop iterations when bootstrapping.")
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=None,
        help="Local loop interval when bootstrapping.",
    )
    return parser.parse_args()


async def main(args: argparse.Namespace) -> dict:
    settings = load_settings()
    if args.bootstrap_local_loop:
        runtime = await build_runtime(settings)
        await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=args.iterations or settings.local_publish_iterations,
            interval_seconds=(
                args.interval_seconds
                if args.interval_seconds is not None
                else settings.local_publish_interval_seconds
            ),
        )
        event_store = runtime.event_store
    else:
        storage = build_storage_backends(settings)
        event_store = storage.event_store

    replay_engine = ReplayEngine(
        event_store=event_store,
        reconstruction_service=PortfolioReconstructionService(
            initial_usdt_balance=settings.initial_usdt_balance,
            snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
        ),
    )
    result = replay_engine.replay()
    return {
        "replayed_event_count": result.replayed_event_count,
        "stored_snapshot_count": result.stored_snapshot_count,
        "divergence_count": result.divergence_count,
        "divergences": result.divergences,
        "decision_chain_count": len(result.decision_chains),
        "final_reconstructed_snapshot": (
            result.final_reconstructed_snapshot.model_dump(mode="json")
            if result.final_reconstructed_snapshot is not None
            else None
        ),
        "final_stored_snapshot": (
            result.final_stored_snapshot.model_dump(mode="json")
            if result.final_stored_snapshot is not None
            else None
        ),
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main(parse_args())), indent=2, default=str))
