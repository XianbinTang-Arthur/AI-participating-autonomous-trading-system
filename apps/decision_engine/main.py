from __future__ import annotations

import asyncio

from aats.bootstrap.config import build_runtime, load_settings
from aats.bootstrap.logging import configure_logging_for_settings, get_logger


async def main(*, iterations: int | None = None, interval_seconds: float | None = None) -> dict:
    settings = load_settings()
    configure_logging_for_settings(settings)
    logger = get_logger("apps.decision_engine")

    runtime = await build_runtime(settings)
    try:
        effective_iterations = iterations if iterations is not None else settings.local_publish_iterations
        effective_interval = (
            interval_seconds if interval_seconds is not None else settings.local_publish_interval_seconds
        )

        snapshots = await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=effective_iterations,
            interval_seconds=effective_interval,
        )
        logger.info(
            "Completed local paper loop for %s with %s market snapshots on %s",
            settings.default_symbol,
            len(snapshots),
            settings.primary_timeframe,
        )
        latest_audit = runtime.event_store.latest("system.audit_records")
        return {
            "symbol": settings.default_symbol,
            "storage_mode": settings.storage_mode,
            "operating_state": runtime.mode_controller.operating_state(),
            "snapshots_published": len(snapshots),
            "latest_price": runtime.market_gateway.latest_price(settings.default_symbol),
            "order_count": len(runtime.execution_repo.order_states()),
            "fill_count": len(runtime.execution_repo.fills()),
            "audit_record_count": runtime.audit_repo.count(),
            "metrics": runtime.metrics.snapshot(),
            "latest_audit": latest_audit.model_dump(mode="json") if latest_audit is not None else None,
            "portfolio": runtime.portfolio_repo.latest().model_dump(mode="json") if runtime.portfolio_repo.latest() else None,
            "reconciliation": (
                runtime.reconciliation_repo.latest().model_dump(mode="json")
                if runtime.reconciliation_repo.latest()
                else None
            ),
        }
    finally:
        await runtime.stop_background_tasks()


if __name__ == "__main__":
    asyncio.run(main())
