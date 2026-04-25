from __future__ import annotations

from decimal import Decimal

from aats.schemas.execution import OrderState
from aats.storage.execution_repo import InMemoryExecutionRepository


def _order(*, client_order_id: str, decision_id: str, status: str = "SUBMITTED") -> OrderState:
    return OrderState(
        decision_id=decision_id,
        intent_id=f"intent-{client_order_id}",
        symbol="BTC-USDT-SWAP",
        client_order_id=client_order_id,
        venue="OKX",
        status=status,
        requested_qty=Decimal("1"),
        remaining_qty=Decimal("1"),
        product_type="derivatives",
        margin_mode="cross",
    )


def test_in_memory_order_states_for_decision_filters_and_sorts() -> None:
    repo = InMemoryExecutionRepository()
    repo.save_order_state(_order(client_order_id="order-2", decision_id="decision-2"))
    repo.save_order_state(_order(client_order_id="order-1", decision_id="decision-1"))
    repo.save_order_state(_order(client_order_id="order-3", decision_id="decision-1"))

    rows = repo.order_states_for_decision("decision-1")

    assert [row.client_order_id for row in rows] == ["order-1", "order-3"]


def test_in_memory_order_states_for_decision_ignores_empty_id() -> None:
    repo = InMemoryExecutionRepository()
    repo.save_order_state(_order(client_order_id="order-1", decision_id="decision-1"))

    assert repo.order_states_for_decision("") == []
