from __future__ import annotations

from decimal import Decimal
from typing import Any, Protocol

from aats.bootstrap.logging import log_event
from aats.schemas.execution import OrderObligation
from aats.storage.base import ExecutionObligationRepository
from aats.storage.obligation_repo_postgres import PostgresExecutionObligationRepository


class ObligationWriterRequiredError(RuntimeError):
    """Raised when production Postgres obligation writes bypass the writer."""


class ObligationWriter(Protocol):
    def persist_obligation_sync(
        self,
        *,
        obligation: OrderObligation,
        source_component: str,
        reason_code: str,
    ) -> OrderObligation:
        ...


def ensure_direct_obligation_write_allowed(
    *,
    obligation_repo: ExecutionObligationRepository,
    source_component: str,
    logger: Any,
    operation: str,
    client_order_id: str | None = None,
) -> None:
    if isinstance(obligation_repo, PostgresExecutionObligationRepository):
        log_event(
            logger,
            "execution_obligation_writer_required",
            level="error",
            source_component=source_component,
            operation=operation,
            client_order_id=client_order_id,
        )
        raise ObligationWriterRequiredError(
            "obligation_writer_required: Postgres execution obligations must "
            "be persisted via PostgresExecutionOutboxPublisher"
        )


def save_obligation_direct_legacy_only(
    *,
    obligation_repo: ExecutionObligationRepository,
    obligation: OrderObligation,
    source_component: str,
    reason_code: str,
    logger: Any,
) -> OrderObligation:
    ensure_direct_obligation_write_allowed(
        obligation_repo=obligation_repo,
        source_component=source_component,
        logger=logger,
        operation=reason_code,
        client_order_id=obligation.client_order_id,
    )
    log_event(
        logger,
        "execution_obligation_legacy_direct_save",
        level="warning",
        source_component=source_component,
        reason_code=reason_code,
        client_order_id=obligation.client_order_id,
        obligation_id=obligation.obligation_id,
        status=obligation.status,
        repo_type=type(obligation_repo).__name__,
    )
    return obligation_repo.save_obligation(obligation)


def reserve_obligation_direct_legacy_only(
    *,
    obligation_repo: ExecutionObligationRepository,
    obligation: OrderObligation,
    snapshot_available_balance: Decimal,
    epsilon: Decimal,
    source_component: str,
    logger: Any,
) -> OrderObligation:
    ensure_direct_obligation_write_allowed(
        obligation_repo=obligation_repo,
        source_component=source_component,
        logger=logger,
        operation="reserve_obligation_transactional",
        client_order_id=obligation.client_order_id,
    )
    log_event(
        logger,
        "execution_obligation_legacy_direct_reserve",
        level="warning",
        source_component=source_component,
        client_order_id=obligation.client_order_id,
        obligation_id=obligation.obligation_id,
        status=obligation.status,
        repo_type=type(obligation_repo).__name__,
    )
    return obligation_repo.reserve_obligation_transactional(
        obligation=obligation,
        snapshot_available_balance=snapshot_available_balance,
        epsilon=epsilon,
    )
