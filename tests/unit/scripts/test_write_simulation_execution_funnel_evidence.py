from __future__ import annotations

from scripts.write_simulation_execution_funnel_evidence import _EVENT_ROWS


def test_event_query_recovers_symbol_keyed_risk_decisions() -> None:
    normalized_sql = " ".join(str(_EVENT_ROWS).split())

    assert "COALESCE(symbol, event_key) = :symbol" in normalized_sql
