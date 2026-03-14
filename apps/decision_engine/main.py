from __future__ import annotations

import asyncio

from aats.bootstrap.config import build_runtime, load_settings
from aats.bootstrap.logging import configure_logging, get_logger


async def main(*, iterations: int | None = None, interval_seconds: float | None = None) -> dict:
    settings = load_settings()
    configure_logging(settings.log_level)
    logger = get_logger("apps.decision_engine")

    runtime = await build_runtime(settings)
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
    return {
        "symbol": settings.default_symbol,
        "storage_mode": settings.storage_mode,
        "snapshots_published": len(snapshots),
        "latest_price": runtime.market_gateway.latest_price(settings.default_symbol),
        "order_count": len(runtime.execution_repo.order_states()),
        "fill_count": len(runtime.execution_repo.fills()),
        "audit_record_count": len(runtime.audit_repo.all()),
        "metrics": runtime.metrics.snapshot(),
        "latest_audit": runtime.audit_repo.all()[-1].model_dump(mode="json") if runtime.audit_repo.all() else None,
        "portfolio": runtime.portfolio_repo.latest().model_dump(mode="json") if runtime.portfolio_repo.latest() else None,
        "reconciliation": (
            runtime.reconciliation_repo.latest().model_dump(mode="json")
            if runtime.reconciliation_repo.latest()
            else None
        ),
    }


if __name__ == "__main__":
    asyncio.run(main())
