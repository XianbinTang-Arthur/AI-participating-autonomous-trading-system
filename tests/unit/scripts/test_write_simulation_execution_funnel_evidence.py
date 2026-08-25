from __future__ import annotations

from scripts.write_simulation_execution_funnel_evidence import (
    _EVENT_ROWS,
    _FILL_ROWS,
    _ORDER_ROWS,
)


def test_event_query_recovers_symbol_keyed_risk_decisions() -> None:
    normalized_sql = " ".join(str(_EVENT_ROWS).split())

    assert "COALESCE(symbol, event_key) = :symbol" in normalized_sql
    assert "decision_id IN (SELECT decision_id FROM scoped_decisions)" in normalized_sql


def test_order_and_fill_queries_are_scoped_to_target_decisions() -> None:
    normalized_order_sql = " ".join(str(_ORDER_ROWS).split())
    normalized_fill_sql = " ".join(str(_FILL_ROWS).split())

    assert "decision_id = ANY(:decision_ids)" in normalized_order_sql
    assert "decision_id = ANY(:decision_ids)" in normalized_fill_sql
