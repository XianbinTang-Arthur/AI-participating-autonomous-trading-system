from __future__ import annotations

from decimal import Decimal

from aats.schemas.common import utc_now
from aats.schemas.execution import OrderIntent
from aats.storage.execution_order_labels import (
    EXECUTION_ORDER_LABEL_MAX_LENGTH,
    execution_order_storage_label,
)
from aats.storage.execution_order_repo_postgres import PostgresExecutionOrderRepository


class _FakeSession:
    def __init__(self) -> None:
        self.added = None

    def get(self, *_args, **_kwargs):
        return None

    def scalar(self, *_args, **_kwargs):
        return None

    def add(self, row) -> None:
        self.added = row


def _intent(**updates) -> OrderIntent:
    base = {
        "intent_id": "intent_storage_label",
        "decision_id": "decision_storage_label",
        "symbol": "BTC-USDT-SWAP",
        "side": "sell",
        "quantity": Decimal("0.0028"),
        "execution_style": "semantic_duplicate_snapshot_blocked",
        "order_type": "market",
        "urgency": "medium",
        "time_in_force": "IOC",
        "idempotency_key": "cl_storage_label",
        "product_type": "derivatives",
        "margin_mode": "cross",
        "exposure_side": "short",
        "position_intent": "open_short",
    }
    base.update(updates)
    return OrderIntent(**base)


def test_known_blocked_labels_fit_execution_order_columns() -> None:
    semantic_label = execution_order_storage_label("semantic_duplicate_snapshot_blocked")
    convergence_label = execution_order_storage_label("risk_increase_convergence_blocked")

    assert semantic_label == "semantic_dup_snapshot_blocked"
    assert convergence_label == "risk_convergence_blocked"
    assert len(semantic_label) <= EXECUTION_ORDER_LABEL_MAX_LENGTH
    assert len(convergence_label) <= EXECUTION_ORDER_LABEL_MAX_LENGTH


def test_unknown_long_label_is_stable_and_bounded() -> None:
    raw = "custom_execution_label_that_is_far_too_long_for_execution_order_columns"

    first = execution_order_storage_label(raw)
    second = execution_order_storage_label(raw)

    assert first == second
    assert len(first) <= EXECUTION_ORDER_LABEL_MAX_LENGTH
    assert first != raw


def test_execution_order_repo_projects_storage_labels_without_mutating_payload() -> None:
    repo = PostgresExecutionOrderRepository(session_factory=lambda: None)  # type: ignore[arg-type]
    session = _FakeSession()
    raw_payload = {
        "client_order_id": "cl_storage_label",
        "source_system": "semantic_duplicate_snapshot_blocked",
        "order_state": {"submission_mode": "semantic_duplicate_snapshot_blocked"},
    }

    repo.create_order_in_session(
        session,  # type: ignore[arg-type]
        order_id="cl_storage_label",
        intent=_intent(),
        initial_state="BLOCKED",
        created_at=utc_now(),
        raw_payload=raw_payload,
    )

    assert session.added is not None
    assert session.added.execution_style == "semantic_dup_snapshot_blocked"
    assert session.added.source_system == "semantic_dup_snapshot_blocked"
    assert session.added.raw_payload["source_system"] == "semantic_duplicate_snapshot_blocked"
    assert (
        session.added.raw_payload["order_state"]["submission_mode"]
        == "semantic_duplicate_snapshot_blocked"
    )
