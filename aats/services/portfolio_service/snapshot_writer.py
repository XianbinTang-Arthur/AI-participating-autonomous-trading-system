from __future__ import annotations

from typing import Any

from aats.bootstrap.logging import log_event
from aats.schemas.portfolio import PortfolioSnapshot
from aats.storage.base import PortfolioRepository
from aats.storage.portfolio_repo_postgres import PostgresPortfolioRepository


class PortfolioSnapshotWriterRequiredError(RuntimeError):
    """Raised when production Postgres snapshot writes bypass the outbox writer."""


def save_snapshot_direct_legacy_only(
    *,
    portfolio_repo: PortfolioRepository,
    snapshot: PortfolioSnapshot,
    source_component: str,
    logger: Any,
) -> None:
    """Direct repository write fallback for non-Postgres tests and legacy demos.

    Production Postgres runtime must route portfolio snapshot writes through
    ``PostgresPortfolioOutboxPublisher`` so the snapshot row, durable event, and
    outbox row advance together.
    """

    ensure_direct_snapshot_write_allowed(
        portfolio_repo=portfolio_repo,
        source_component=source_component,
        logger=logger,
        decision_id=snapshot.decision_id,
        snapshot_origin=snapshot.snapshot_origin,
        product_type=snapshot.product_type,
        margin_mode=snapshot.margin_mode,
    )

    log_event(
        logger,
        "portfolio_snapshot_legacy_direct_save",
        level="warning",
        source_component=source_component,
        decision_id=snapshot.decision_id,
        snapshot_origin=snapshot.snapshot_origin,
        repo_type=type(portfolio_repo).__name__,
    )
    portfolio_repo.save_snapshot(snapshot)


def ensure_direct_snapshot_write_allowed(
    *,
    portfolio_repo: PortfolioRepository,
    source_component: str,
    logger: Any,
    decision_id: str | None = None,
    snapshot_origin: str | None = None,
    product_type: str | None = None,
    margin_mode: str | None = None,
) -> None:
    if isinstance(portfolio_repo, PostgresPortfolioRepository):
        log_event(
            logger,
            "portfolio_snapshot_outbox_writer_required",
            level="error",
            source_component=source_component,
            decision_id=decision_id,
            snapshot_origin=snapshot_origin,
            product_type=product_type,
            margin_mode=margin_mode,
        )
        raise PortfolioSnapshotWriterRequiredError(
            "portfolio_snapshot_writer_required: Postgres portfolio snapshots "
            "must be persisted via PostgresPortfolioOutboxPublisher"
        )
