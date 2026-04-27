from __future__ import annotations

from typing import Any

from aats.bootstrap.logging import log_event
from aats.schemas.execution import FillEvent, OrderState
from aats.storage.base import ExecutionRepository
from aats.storage.execution_order_repo import ExecutionOrderHistoryRepository, ExecutionOrderRepository
from aats.storage.execution_order_repo_postgres import PostgresExecutionOrderRepository
from aats.storage.execution_repo_converged_postgres import ConvergedPostgresExecutionRepository
from aats.storage.execution_repo_postgres import PostgresExecutionRepository


class ExecutionStateWriterRequiredError(RuntimeError):
    """Raised when production Postgres execution writes bypass the outbox writer."""


_POSTGRES_EXECUTION_REPO_TYPES = (
    PostgresExecutionRepository,
    ConvergedPostgresExecutionRepository,
)


def ensure_direct_execution_write_allowed(
    *,
    execution_repo: ExecutionRepository,
    source_component: str,
    logger: Any,
    operation: str,
    client_order_id: str | None = None,
    fill_id: str | None = None,
) -> None:
    if isinstance(execution_repo, _POSTGRES_EXECUTION_REPO_TYPES):
        log_event(
            logger,
            "execution_state_outbox_writer_required",
            level="error",
            source_component=source_component,
            operation=operation,
            client_order_id=client_order_id,
            fill_id=fill_id,
        )
        raise ExecutionStateWriterRequiredError(
            "execution_state_writer_required: Postgres execution state must "
            "be persisted via PostgresExecutionOutboxPublisher"
        )


def save_order_state_direct_legacy_only(
    *,
    execution_repo: ExecutionRepository,
    order_state: OrderState,
    source_component: str,
    logger: Any,
) -> OrderState:
    ensure_direct_execution_write_allowed(
        execution_repo=execution_repo,
        source_component=source_component,
        logger=logger,
        operation="save_order_state",
        client_order_id=order_state.client_order_id,
    )
    log_event(
        logger,
        "execution_order_state_legacy_direct_save",
        level="warning",
        source_component=source_component,
        client_order_id=order_state.client_order_id,
        status=order_state.status,
        repo_type=type(execution_repo).__name__,
    )
    return execution_repo.save_order_state(order_state)


def save_fill_direct_legacy_only(
    *,
    execution_repo: ExecutionRepository,
    fill: FillEvent,
    source_component: str,
    logger: Any,
) -> bool:
    ensure_direct_execution_write_allowed(
        execution_repo=execution_repo,
        source_component=source_component,
        logger=logger,
        operation="save_fill",
        client_order_id=fill.client_order_id,
        fill_id=fill.fill_id,
    )
    log_event(
        logger,
        "execution_fill_legacy_direct_save",
        level="warning",
        source_component=source_component,
        client_order_id=fill.client_order_id,
        fill_id=fill.fill_id,
        repo_type=type(execution_repo).__name__,
    )
    return execution_repo.save_fill(fill)


def sync_execution_order_truth_direct_legacy_only(
    *,
    order_state: OrderState,
    phase1_execution_shadow_service: Any | None,
    execution_order_repo: ExecutionOrderRepository | None,
    execution_order_history_repo: ExecutionOrderHistoryRepository | None,
    source_component: str,
    history_reason_code: str,
    logger: Any,
) -> None:
    """Legacy fallback for non-outbox execution-order truth mirrors."""

    if phase1_execution_shadow_service is not None:
        phase1_execution_shadow_service.shadow_order_state(order_state=order_state)
        return
    if execution_order_repo is None:
        return
    if isinstance(execution_order_repo, PostgresExecutionOrderRepository):
        log_event(
            logger,
            "execution_order_truth_outbox_writer_required",
            level="error",
            source_component=source_component,
            client_order_id=order_state.client_order_id,
            status=order_state.status,
        )
        raise ExecutionStateWriterRequiredError(
            "execution_order_truth_writer_required: Postgres execution order "
            "truth must be synchronized via PostgresExecutionOutboxPublisher"
        )

    existing = execution_order_repo.get_order_by_client_order_id(order_state.client_order_id)
    if existing is None:
        return
    previous_state = str(existing["state"])
    execution_order_repo.update_order_state(
        order_id=str(existing["order_id"]),
        expected_state_version=int(existing["state_version"]),
        next_state=order_state.status,
        venue_order_id=order_state.exchange_order_id,
        last_exchange_ts=order_state.last_exchange_update_ts,
        updated_at=order_state.last_update_ts or order_state.created_at,
        raw_payload=order_state.model_dump(mode="python"),
    )
    if (
        execution_order_history_repo is not None
        and previous_state != order_state.status
    ):
        execution_order_history_repo.append_transition(
            order_id=str(existing["order_id"]),
            from_state=previous_state,
            to_state=order_state.status,
            reason_code=history_reason_code,
            source=source_component,
            source_message_id=order_state.intent_id,
            payload=order_state.model_dump(mode="python"),
            created_at=order_state.last_update_ts or order_state.created_at,
        )
