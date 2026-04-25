from __future__ import annotations

from types import SimpleNamespace

from aats.services.operator.query_service import OperatorQueryService


class _FakeStateScope:
    product_type = "derivatives"
    margin_mode = "cross"

    @staticmethod
    def symbol_allowed(symbol: str | None) -> bool:
        return symbol == "BTC-USDT-SWAP"


class _FakeExecutionRepo:
    def __init__(
        self,
        *,
        order_states: list[dict] | None = None,
        fills: list[dict] | None = None,
    ) -> None:
        self._order_states = order_states or []
        self._fills = fills or []

    def order_states_for_scope(self, **_kwargs):
        return self._order_states

    def order_states_for_decision(self, decision_id: str):
        return [order for order in self._order_states if order.get("decision_id") == decision_id]

    def fills_for_decisions(self, decision_ids: list[str]):
        allowed = set(decision_ids)
        return [fill for fill in self._fills if fill.get("decision_id") in allowed]


def _service(
    *,
    order_states: list[dict] | None = None,
    fills: list[dict] | None = None,
) -> OperatorQueryService:
    query = OperatorQueryService.__new__(OperatorQueryService)
    query.state_scope = _FakeStateScope()
    query.runtime = SimpleNamespace(
        execution_repo=_FakeExecutionRepo(order_states=order_states, fills=fills),
    )
    return query


def _audit(**overrides):
    defaults = {
        "decision_context_ref": "ctx-1",
        "position_target_ref": "target-1",
        "policy_decision_ref": "policy-1",
        "risk_decision_ref": "risk-1",
        "decision_outcome_ref": "outcome-1",
        "execution_plan_ref": "plan-1",
        "order_intent_refs": [],
        "order_state_refs": [],
        "fill_event_refs": [],
        "reconciliation_refs": [],
        "strategy_sleeve_intent_refs": [],
        "portfolio_delta_refs": [],
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_decision_truth_chain_links_repo_order_fill_and_lifecycle_refs() -> None:
    query = _service(
        order_states=[
            {
                "decision_id": "decision-1",
                "symbol": "BTC-USDT-SWAP",
                "product_type": "derivatives",
                "margin_mode": "cross",
                "client_order_id": "order-1",
                "status": "FILLED",
                "lifecycle_snapshot_refs": {
                    "submit": {
                        "market_snapshot_ref": "mkt-1",
                        "feature_snapshot_ref": "feat-1",
                        "portfolio_snapshot_ref": "port-1",
                        "health_snapshot_ref": "health-1",
                    }
                },
            }
        ],
        fills=[
            {
                "decision_id": "decision-1",
                "symbol": "BTC-USDT-SWAP",
                "product_type": "derivatives",
                "margin_mode": "cross",
                "client_order_id": "order-1",
                "fill_id": "fill-1",
            }
        ],
    )

    payload = query._decision_truth_chain_payload(
        decision_id="decision-1",
        audit=_audit(order_intent_refs=["intent-1"]),
        order_updates=[],
        fills=[],
    )

    assert payload["complete"] is True
    assert payload["overall_status"] == "complete"
    assert payload["order"]["status"] == "linked"
    assert payload["order"]["client_order_ids"] == ["order-1"]
    assert payload["fill"]["status"] == "linked"
    assert payload["fill"]["fill_ids"] == ["fill-1"]
    assert payload["lifecycle"]["status"] == "linked"
    assert payload["lifecycle"]["stages"] == ["submit"]
    assert payload["provenance"]["status"] == "linked"


def test_decision_truth_chain_distinguishes_clean_no_execution_from_missing_links() -> None:
    query = _service()

    payload = query._decision_truth_chain_payload(
        decision_id="decision-1",
        audit=_audit(),
        order_updates=[],
        fills=[],
    )

    assert payload["complete"] is True
    assert payload["order"]["status"] == "absent_no_order_intent"
    assert payload["fill"]["status"] == "absent_no_order"
    assert payload["lifecycle"]["status"] == "absent_no_execution_record"
    assert payload["missing_evidence"] == []


def test_decision_truth_chain_marks_order_intent_without_order_state_as_missing() -> None:
    query = _service()

    payload = query._decision_truth_chain_payload(
        decision_id="decision-1",
        audit=_audit(order_intent_refs=["intent-1"]),
        order_updates=[],
        fills=[],
    )

    assert payload["complete"] is False
    assert payload["overall_status"] == "incomplete"
    assert payload["order"]["status"] == "missing_after_order_intent"
    assert "missing_after_order_intent" in payload["missing_evidence"]


def test_decision_truth_chain_marks_provenance_gaps_as_partial() -> None:
    query = _service()

    payload = query._decision_truth_chain_payload(
        decision_id="decision-1",
        audit=_audit(risk_decision_ref=None),
        order_updates=[],
        fills=[],
    )

    assert payload["complete"] is False
    assert payload["provenance"]["status"] == "partial"
    assert payload["provenance"]["missing_required_refs"] == ["risk_decision_ref"]
    assert "provenance_partial" in payload["missing_evidence"]
