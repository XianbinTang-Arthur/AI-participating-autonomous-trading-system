from __future__ import annotations

from types import SimpleNamespace

from aats.services.operator.account_queries import AccountQueryFacade


class _FakeOwner:
    def __init__(self, *, orders: list[dict], fills: list[dict] | None = None) -> None:
        self.orders = orders
        self.fills = fills or []
        self.runtime = SimpleNamespace(
            execution_adapter=SimpleNamespace(readiness=lambda: {"ready": True}),
        )

    def latest_order(self):
        return self.orders[0] if self.orders else None

    def latest_fill(self):
        return self.fills[0] if self.fills else None

    def _latest_scoped_reconciliation(self):
        return None

    def recovery_view(self):
        return {"recovery_state": "normal_operation"}

    def system_mode(self):
        return {"execution_route": "derivatives_live"}

    def _execution_record_payload(self, record):
        payload = dict(record)
        if "state" in payload and "status" not in payload:
            payload["status"] = payload["state"]
        return payload

    def execution_errors(self):
        return {"errors": []}

    def _phase5_control_plane_enabled(self):
        return True

    def _phase5_order_rows(self, *, limit=None, offset=0):
        rows = self.orders[offset:]
        return rows[:limit] if limit is not None else rows

    def _phase5_fill_rows(self, *, limit=None, offset=0):
        rows = self.fills[offset:]
        return rows[:limit] if limit is not None else rows


def test_execution_latest_exposes_terminal_no_fill_explanation_for_blocked_directional_decision() -> None:
    owner = _FakeOwner(
        orders=[
            {
                "order_id": "order-close-long",
                "client_order_id": "client-close-long",
                "decision_id": "decision-terminal-no-fill",
                "state": "BLOCKED",
                "position_intent": "close_long",
                "execution_style": "taker",
                "source_system": "local_order_manager",
                "updated_at": "2026-04-27T09:49:54Z",
                "product_type": "derivatives",
                "margin_mode": "cross",
                "symbol": "BTC-USDT-SWAP",
            },
            {
                "order_id": "order-open-short",
                "client_order_id": "client-open-short",
                "decision_id": "decision-terminal-no-fill",
                "state": "BLOCKED",
                "position_intent": "open_short",
                "execution_style": "semantic_dup_snapshot_blocked",
                "source_system": "semantic_dup_snapshot_blocked",
                "updated_at": "2026-04-27T09:49:54Z",
                "product_type": "derivatives",
                "margin_mode": "cross",
                "symbol": "BTC-USDT-SWAP",
            },
        ]
    )

    payload = AccountQueryFacade(owner).build_execution_latest()

    explanation = payload["terminal_no_fill_explanation"]
    assert explanation["classification"] == "terminal_order_surface_without_fill"
    assert explanation["reason"] == "terminal_order_blocked_before_fill"
    assert explanation["terminal_states"] == ["BLOCKED"]
    assert explanation["terminal_position_intents"] == ["close_long", "open_short"]
    assert explanation["terminal_execution_styles"] == ["taker", "semantic_dup_snapshot_blocked"]
    assert explanation["execution_order_count"] == 2
    assert explanation["fill_surface_present"] is False


def test_execution_latest_does_not_mark_terminal_no_fill_when_decision_has_fill() -> None:
    owner = _FakeOwner(
        orders=[
            {
                "order_id": "order-filled",
                "client_order_id": "client-filled",
                "decision_id": "decision-filled",
                "state": "FILLED",
                "position_intent": "open_long",
                "product_type": "derivatives",
                "margin_mode": "cross",
                "symbol": "BTC-USDT-SWAP",
            }
        ],
        fills=[
            {
                "fill_id": "fill-filled",
                "decision_id": "decision-filled",
                "order_id": "order-filled",
                "product_type": "derivatives",
                "margin_mode": "cross",
                "symbol": "BTC-USDT-SWAP",
            }
        ],
    )

    payload = AccountQueryFacade(owner).build_execution_latest()

    assert payload["terminal_no_fill_explanation"] is None
