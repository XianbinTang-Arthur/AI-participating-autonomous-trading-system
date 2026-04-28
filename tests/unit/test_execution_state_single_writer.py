from __future__ import annotations

import logging
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aats.events import topics
from aats.schemas.common import utc_now
from aats.schemas.execution import OrderState
from aats.services.operator.reconciliation_system_queries import ReconciliationSystemQueryFacade
from aats.services.recovery_control.exchange_order_reconciler import reconcile_stuck_orders


def _order_state(*, client_order_id: str, status: str = "SUBMITTING") -> OrderState:
    now = utc_now()
    return OrderState(
        decision_id=f"decision_{client_order_id}",
        intent_id=f"intent_{client_order_id}",
        symbol="BTC-USDT-SWAP",
        client_order_id=client_order_id,
        venue="OKX",
        status=status,
        submission_mode="guarded_live_submit",
        submitted_ts=now,
        last_update_ts=now,
        requested_qty=Decimal("0.001"),
        remaining_qty=Decimal("0.001"),
        product_type="derivatives",
        margin_mode="cross",
        exposure_side="long",
        execution_action="enter",
        position_intent="open_long",
        submission_payload={},
    )


class _RecordingOutboxWriter:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def persist_order_state(self, **kwargs: Any) -> OrderState:
        self.calls.append(kwargs)
        return kwargs["order_state"]


class _NoDirectExecutionRepo:
    def save_order_state(self, _state: OrderState) -> OrderState:
        raise AssertionError("direct execution_repo.save_order_state must not be used")


class _RecordingBus:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def publish(self, *, topic: str, key: str, payload: dict[str, Any]) -> None:
        self.messages.append({"topic": topic, "key": key, "payload": payload})


class _FakeReport:
    reconciliation_id = "recon_1"
    decision_id = "decision_ord_stuck"
    severity = "CLEAN"
    halt_required = False
    review_required = False
    exchange_comparison_enabled = True
    mismatch_reasons: list[str] = []
    safety_impacts: list[str] = []

    def model_dump(self, *, mode: str = "python") -> dict[str, Any]:
        return {
            "reconciliation_id": self.reconciliation_id,
            "decision_id": self.decision_id,
            "severity": self.severity,
            "halt_required": self.halt_required,
            "review_required": self.review_required,
            "exchange_comparison_enabled": self.exchange_comparison_enabled,
            "mismatch_reasons": self.mismatch_reasons,
            "safety_impacts": self.safety_impacts,
        }


class _FakeReconciliationService:
    async def validate_now(self, *, reason: str) -> _FakeReport:
        self.reason = reason
        return _FakeReport()


class _OperatorOwner:
    state_scope = None
    logger = logging.getLogger("test_execution_state_single_writer.operator")

    def __init__(
        self,
        *,
        order: OrderState,
        outbox: _RecordingOutboxWriter,
        claimed_submit_command: dict[str, Any] | None = None,
    ) -> None:
        self._order = order
        self._claimed_submit_command = claimed_submit_command
        self.events: list[dict[str, Any]] = []
        self.runtime = SimpleNamespace(
            reconciliation_service=_FakeReconciliationService(),
            execution_outbox_publisher=outbox,
            execution_repo=_NoDirectExecutionRepo(),
            bus=_RecordingBus(),
        )

    def _control_plane_order_state(self, client_order_id: str) -> OrderState | None:
        return self._order if client_order_id == self._order.client_order_id else None

    def _control_plane_fills_for_order(self, _client_order_id: str) -> list[Any]:
        return []

    def recovery_view(self) -> dict[str, Any]:
        return {"recovery_state": "review_required"}

    async def _refresh_exchange_snapshot_for_resolution(self) -> dict[str, Any]:
        return {}

    def _stuck_submission_resolution(
        self,
        *,
        order: OrderState,
        fills: list[Any],
        exchange_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        return {"eligible": True, "reason_code": "exchange_absent_after_restart"}

    def _claimed_submit_command_for_order(self, order: OrderState) -> dict[str, Any] | None:
        if order.client_order_id != self._order.client_order_id:
            return None
        return self._claimed_submit_command

    def _update_recovery_status_for_report(self, _report: _FakeReport) -> None:
        return None

    def _invalidate_cache(self) -> None:
        return None

    def _append_event(self, *, topic: str, key: str, payload_model: Any) -> None:
        self.events.append({"topic": topic, "key": key, "payload_model": payload_model})

    def _persist_blocker_snapshot(
        self,
        *,
        source: str,
        runtime_state: str,
        mode_snapshot: dict[str, Any],
        blockers: list[Any],
    ) -> None:
        return None

    def system_health(self) -> dict[str, Any]:
        return {"runtime_state": "running"}

    def system_mode(self) -> dict[str, Any]:
        return {"mode": "live"}

    def blockers(self) -> list[Any]:
        return []


@pytest.mark.asyncio
async def test_operator_stuck_submission_resolution_persists_order_state_via_outbox() -> None:
    order = _order_state(client_order_id="ord_stuck")
    outbox = _RecordingOutboxWriter()
    owner = _OperatorOwner(order=order, outbox=outbox)

    result = await ReconciliationSystemQueryFacade(owner).resolve_stuck_submission(
        client_order_id=order.client_order_id,
        reason="operator_confirmed_absent_on_okx",
        actor_role="admin",
    )

    assert result["order"]["status"] == "FAILED"
    assert len(outbox.calls) == 1
    call = outbox.calls[0]
    persisted = call["order_state"]
    assert persisted.status == "FAILED"
    assert persisted.execution_error == "operator_resolved_stuck_submission_after_restart"
    assert call["source_component"] == "operator_api"
    assert call["emit_execution_error_summary"] is False
    assert call["sync_execution_order_truth"] is True
    assert call["history_reason_code"] == "operator_state_sync"
    assert [message["topic"] for message in owner.runtime.bus.messages] == [
        topics.EXECUTION_ERROR_SUMMARIES
    ]


@pytest.mark.asyncio
async def test_operator_claimed_submit_resolution_requires_explicit_confirmation() -> None:
    order = _order_state(client_order_id="ord_claimed_submit")
    outbox = _RecordingOutboxWriter()
    owner = _OperatorOwner(
        order=order,
        outbox=outbox,
        claimed_submit_command={
            "command_id": "cmd_claimed_submit",
            "idempotency_key": f"submit:{order.client_order_id}",
            "command_type": "submit",
            "state": "CLAIMED",
        },
    )

    with pytest.raises(ValueError) as excinfo:
        await ReconciliationSystemQueryFacade(owner).resolve_stuck_submission(
            client_order_id=order.client_order_id,
            reason="operator_confirmed_absent_on_okx",
            actor_role="admin",
        )

    assert str(excinfo.value) == (
        "stuck_submission_resolution_blocked:"
        "claimed_submit_requires_operator_confirmation"
    )
    assert outbox.calls == []


@pytest.mark.asyncio
async def test_operator_claimed_submit_resolution_records_confirmation_gate() -> None:
    order = _order_state(client_order_id="ord_claimed_confirmed")
    outbox = _RecordingOutboxWriter()
    owner = _OperatorOwner(
        order=order,
        outbox=outbox,
        claimed_submit_command={
            "command_id": "cmd_claimed_confirmed",
            "idempotency_key": f"submit:{order.client_order_id}",
            "command_type": "submit",
            "state": "CLAIMED",
        },
    )

    result = await ReconciliationSystemQueryFacade(owner).resolve_stuck_submission(
        client_order_id=order.client_order_id,
        reason="operator_confirmed_absent_on_okx",
        operator_confirmation=f"resolve_claimed_submit_as_failed:{order.client_order_id}",
        actor_role="admin",
    )

    assert result["order"]["status"] == "FAILED"
    assert result["resolution"]["claimed_submit_command_present"] is True
    assert result["resolution"]["claimed_submit_command_id"] == "cmd_claimed_confirmed"
    assert result["resolution"]["operator_confirmation_required"] is True
    assert result["resolution"]["operator_confirmation_matched"] is True
    action = next(event["payload_model"] for event in owner.events if event["topic"] == topics.OPERATOR_ACTIONS)
    assert action.details["claimed_submit_command_present"] is True
    assert action.details["claimed_submit_command_id"] == "cmd_claimed_confirmed"


class _FakeExchange:
    async def get_order(self, *, symbol: str, client_order_id: str | None = None) -> dict[str, Any]:
        return {"code": "51603", "data": []}


class _FakeFilledExchange:
    async def get_order(self, *, symbol: str, client_order_id: str | None = None) -> dict[str, Any]:
        return {
            "code": "0",
            "data": [
                {
                    "state": "filled",
                    "ordId": "okx_123",
                    "uTime": "1710000000000",
                    "accFillSz": "0.001",
                    "avgPx": "60000",
                    "fee": "0.03",
                }
            ],
        }


class _OrderStateRepo:
    def __init__(self, order: OrderState) -> None:
        self.order = order

    def get_order_state(self, client_order_id: str) -> OrderState | None:
        return self.order if client_order_id == self.order.client_order_id else None


class _NoLegacyOrderRepo:
    def update_order_state(self, *, client_order_id: str, updates: dict[str, Any]) -> bool:
        raise AssertionError("legacy order_repo.update_order_state must not be used")


@pytest.mark.asyncio
async def test_exchange_stuck_order_reconciler_persists_resolved_state_via_outbox() -> None:
    order = _order_state(client_order_id="ord_startup_stuck")
    outbox = _RecordingOutboxWriter()

    resolved, unreachable, notes = await reconcile_stuck_orders(
        open_orders=[
            {
                "order_id": order.client_order_id,
                "client_order_id": order.client_order_id,
                "symbol": order.symbol,
                "state": "SUBMITTING",
                "venue_order_id": None,
            }
        ],
        exchange_client=_FakeExchange(),
        order_repo=_NoLegacyOrderRepo(),
        order_state_repo=_OrderStateRepo(order),
        execution_outbox_publisher=outbox,
    )

    assert resolved == 1
    assert unreachable == 0
    assert notes == ["exchange_reconciled_stuck_orders:1"]
    assert len(outbox.calls) == 1
    call = outbox.calls[0]
    persisted = call["order_state"]
    assert persisted.client_order_id == order.client_order_id
    assert persisted.status == "FAILED"
    assert persisted.execution_error == "recovery_exchange_reconciler_order_not_found"
    assert call["source_component"] == "recovery_exchange_reconciler"
    assert call["sync_execution_order_truth"] is True
    assert call["history_reason_code"] == "exchange_order_reconcile"


@pytest.mark.asyncio
async def test_exchange_stuck_order_reconciler_preserves_okx_fee_sign() -> None:
    order = _order_state(client_order_id="ord_startup_filled")
    outbox = _RecordingOutboxWriter()

    resolved, unreachable, _notes = await reconcile_stuck_orders(
        open_orders=[
            {
                "order_id": order.client_order_id,
                "client_order_id": order.client_order_id,
                "symbol": order.symbol,
                "state": "SUBMITTING",
                "venue_order_id": None,
            }
        ],
        exchange_client=_FakeFilledExchange(),
        order_repo=_NoLegacyOrderRepo(),
        order_state_repo=_OrderStateRepo(order),
        execution_outbox_publisher=outbox,
    )

    assert resolved == 1
    assert unreachable == 0
    persisted = outbox.calls[0]["order_state"]
    assert persisted.status == "FILLED"
    assert persisted.exchange_order_id == "okx_123"
    assert persisted.filled_qty == Decimal("0.001")
    assert persisted.average_fill_price == Decimal("60000")
    assert persisted.fees == Decimal("-0.03")


def test_services_do_not_directly_write_execution_order_or_fill_state() -> None:
    allowed = {
        Path("aats/services/execution_engine/state_writer.py"),
    }
    violations: list[str] = []
    root = Path("aats/services")
    needles = ("execution_repo.save_order_state(", "execution_repo.save_fill(")
    for path in root.rglob("*.py"):
        normalized = Path(path.as_posix())
        if normalized in allowed:
            continue
        text = path.read_text(encoding="utf-8")
        for needle in needles:
            if needle in text:
                violations.append(f"{path}:{needle}")

    assert not violations


def test_operator_and_recovery_do_not_directly_update_execution_order_truth() -> None:
    violations: list[str] = []
    needles = ("execution_order_repo.update_order_state(", "execution_fill_repo.save_fill(")
    for root in (Path("aats/services/operator"), Path("aats/services/recovery_control")):
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for needle in needles:
                if needle in text:
                    violations.append(f"{path}:{needle}")

    assert not violations
