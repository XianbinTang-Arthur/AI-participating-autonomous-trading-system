from __future__ import annotations

from typing import Any

from aats.bootstrap.logging import log_event
from aats.schemas.portfolio import FillOutcomeRecord
from aats.storage.fill_outcome_repo_postgres import PostgresFillOutcomeRepository


class PortfolioProjectionWriterRequiredError(RuntimeError):
    """Raised when production Postgres fill projection writes bypass the outbox."""


def save_fill_outcome_direct_legacy_only(
    *,
    fill_outcome_repo: object,
    outcome: FillOutcomeRecord,
    source_component: str,
    logger: Any,
) -> FillOutcomeRecord:
    if isinstance(fill_outcome_repo, PostgresFillOutcomeRepository):
        log_event(
            logger,
            "portfolio_projection_outbox_writer_required",
            level="error",
            source_component=source_component,
            fill_id=outcome.fill_id,
            order_id=outcome.order_id,
            symbol=outcome.symbol,
        )
        raise PortfolioProjectionWriterRequiredError(
            "portfolio_projection_writer_required: Postgres fill projections "
            "must be persisted via PostgresPortfolioOutboxPublisher"
        )
    log_event(
        logger,
        "portfolio_fill_outcome_legacy_direct_save",
        level="warning",
        source_component=source_component,
        fill_id=outcome.fill_id,
        order_id=outcome.order_id,
        symbol=outcome.symbol,
        repo_type=type(fill_outcome_repo).__name__,
    )
    return fill_outcome_repo.save_outcome(outcome)
