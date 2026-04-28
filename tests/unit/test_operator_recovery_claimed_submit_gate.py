from __future__ import annotations

from types import SimpleNamespace

from aats.services.operator.recovery_queries import RecoveryQueryFacade


class _Owner:
    runtime = SimpleNamespace(execution_command_repo=None)

    def __init__(self, *, latest_order: dict, fills: list[object] | None = None) -> None:
        self._latest_order = latest_order
        self._fills = list(fills or [])

    def latest_order(self) -> dict:
        return dict(self._latest_order)

    def _control_plane_order_state(self, client_order_id: str) -> dict:
        return dict(self._latest_order | {"client_order_id": client_order_id})

    def _control_plane_fills_for_order(self, _client_order_id: str) -> list[object]:
        return list(self._fills)

    def _claimed_submit_command_for_order(self, _order) -> dict:
        return {
            "command_id": "cmd_claimed",
            "idempotency_key": "submit:cl_stuck",
            "command_type": "submit",
            "state": "CLAIMED",
        }


def test_claimed_submit_recovery_gate_surfaces_exact_confirmation() -> None:
    owner = _Owner(
        latest_order={
            "client_order_id": "cl_stuck",
            "status": "SUBMITTING",
            "venue_order_id": None,
            "intent_id": "intent_stuck",
            "position_intent": "close_long",
            "reduce_only": True,
            "close_only": True,
        },
    )

    gate = RecoveryQueryFacade(owner)._claimed_submit_recovery_gate()

    assert gate["active"] is True
    assert gate["status"] == "awaiting_external_operator_confirmation"
    assert gate["blocker"] == "external_operator_confirmation_required_before_resolve_stuck_submission"
    assert gate["client_order_id"] == "cl_stuck"
    assert gate["command_id"] == "cmd_claimed"
    assert gate["required_operator_confirmation"] == "resolve_claimed_submit_as_failed:cl_stuck"
    assert gate["next_action"] == "verify_okx_absence_then_resolve_with_exact_confirmation"


def test_claimed_submit_recovery_gate_is_inactive_when_local_fill_exists() -> None:
    owner = _Owner(
        latest_order={
            "client_order_id": "cl_stuck",
            "status": "SUBMITTING",
            "venue_order_id": None,
        },
        fills=[object()],
    )

    gate = RecoveryQueryFacade(owner)._claimed_submit_recovery_gate()

    assert gate["active"] is False
    assert gate["status"] == "latest_order_has_local_fills"
    assert gate["local_fill_count"] == 1
