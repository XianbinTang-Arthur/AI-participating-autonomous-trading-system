from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from aats.schemas.execution import OrderState
from aats.services.execution_engine.orderbook_snapshot_refs import (
    capture_orderbook_snapshot_refs_for_event,
)


class _FakeMappingResult:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    def first(self) -> dict[str, Any] | None:
        return self._row


class _FakeExecuteResult:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    def mappings(self) -> _FakeMappingResult:
        return _FakeMappingResult(self._row)


class _FakeSession:
    def __init__(self, rows: list[dict[str, Any] | None]) -> None:
        self._rows = list(rows)
        self.execute_calls: list[dict[str, Any]] = []

    def execute(self, _statement: Any, params: dict[str, Any]) -> _FakeExecuteResult:
        self.execute_calls.append(dict(params))
        row = self._rows.pop(0) if self._rows else None
        return _FakeExecuteResult(row)


class _FailingSession:
    def execute(self, _statement: Any, _params: dict[str, Any]) -> None:
        raise RuntimeError("bronze_orderbook_unavailable")


class TestOrderbookSnapshotRefCapture(unittest.TestCase):
    def test_captures_pre_and_post_books5_refs(self) -> None:
        event_time = datetime(2026, 4, 25, 3, 48, 30, 500000, tzinfo=timezone.utc)
        pre_ts = datetime(2026, 4, 25, 3, 48, 30, tzinfo=timezone.utc)
        post_ts = datetime(2026, 4, 25, 3, 48, 31, tzinfo=timezone.utc)
        session = _FakeSession([{"ts": pre_ts}, {"ts": post_ts}])

        refs = capture_orderbook_snapshot_refs_for_event(
            session,  # type: ignore[arg-type]
            symbol="BTC-USDT-SWAP",
            event_time=event_time,
        )

        self.assertEqual(
            refs["pre_event_orderbook_snapshot_ref"],
            "bronze.market_orderbook_books5:BTC-USDT-SWAP:2026-04-25T03:48:30.000000Z",
        )
        self.assertEqual(
            refs["post_event_orderbook_snapshot_ref"],
            "bronze.market_orderbook_books5:BTC-USDT-SWAP:2026-04-25T03:48:31.000000Z",
        )
        self.assertEqual(len(session.execute_calls), 2)

    def test_preserves_explicit_refs_and_only_captures_missing_side(self) -> None:
        event_time = datetime(2026, 4, 25, 3, 48, 30, tzinfo=timezone.utc)
        session = _FakeSession([{"ts": event_time}])

        refs = capture_orderbook_snapshot_refs_for_event(
            session,  # type: ignore[arg-type]
            symbol="BTC-USDT-SWAP",
            event_time=event_time,
            existing_refs={
                "pre_event_orderbook_snapshot_ref": "upstream_pre_ref",
                "post_event_orderbook_snapshot_ref": None,
            },
        )

        self.assertEqual(refs["pre_event_orderbook_snapshot_ref"], "upstream_pre_ref")
        self.assertEqual(
            refs["post_event_orderbook_snapshot_ref"],
            "bronze.market_orderbook_books5:BTC-USDT-SWAP:2026-04-25T03:48:30.000000Z",
        )
        self.assertEqual(len(session.execute_calls), 1)

    def test_missing_symbol_or_time_returns_existing_shape_without_query(self) -> None:
        session = _FakeSession([{"ts": datetime.now(timezone.utc)}])

        refs = capture_orderbook_snapshot_refs_for_event(
            session,  # type: ignore[arg-type]
            symbol="",
            event_time=None,
        )

        self.assertEqual(
            refs,
            {
                "pre_event_orderbook_snapshot_ref": None,
                "post_event_orderbook_snapshot_ref": None,
            },
        )
        self.assertEqual(session.execute_calls, [])

    def test_bronze_query_failure_keeps_missing_refs_fail_soft(self) -> None:
        refs = capture_orderbook_snapshot_refs_for_event(
            _FailingSession(),  # type: ignore[arg-type]
            symbol="BTC-USDT-SWAP",
            event_time=datetime(2026, 4, 25, 3, 48, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(
            refs,
            {
                "pre_event_orderbook_snapshot_ref": None,
                "post_event_orderbook_snapshot_ref": None,
            },
        )


class TestConvergedRepoOrderbookSnapshotCapture(unittest.TestCase):
    class _OrderRepo:
        def __init__(self) -> None:
            self.created_raw_payload: dict[str, Any] | None = None

        def get_order_by_client_order_id_in_session(
            self, _session: Any, _client_order_id: str, *, for_update: bool = False
        ) -> None:
            return None

        def create_order_in_session(
            self,
            _session: Any,
            *,
            order_id: str,
            intent: Any,
            initial_state: str,
            created_at: datetime,
            raw_payload: dict[str, Any],
        ) -> None:
            self.created_raw_payload = raw_payload

    def test_converged_repo_captures_submit_orderbook_refs(self) -> None:
        from aats.services.execution_engine.state_machine import OrderStateMachine
        from aats.storage.execution_repo_converged_postgres import ConvergedPostgresExecutionRepository

        event_time = datetime(2026, 4, 25, 3, 48, 30, 500000, tzinfo=timezone.utc)
        session = _FakeSession(
            [
                {"ts": datetime(2026, 4, 25, 3, 48, 30, tzinfo=timezone.utc)},
                {"ts": datetime(2026, 4, 25, 3, 48, 31, tzinfo=timezone.utc)},
            ]
        )
        order_repo = self._OrderRepo()
        repo = object.__new__(ConvergedPostgresExecutionRepository)
        repo.execution_order_repo = order_repo  # type: ignore[attr-defined]
        repo.execution_order_history_repo = None  # type: ignore[attr-defined]
        repo.state_machine = OrderStateMachine()  # type: ignore[attr-defined]

        repo.save_order_state_in_session(
            session=session,  # type: ignore[arg-type]
            state=OrderState(
                decision_id="decision_orderbook_capture",
                intent_id="intent_orderbook_capture",
                symbol="BTC-USDT-SWAP",
                client_order_id="cl_orderbook_capture",
                status="SUBMITTING",
                submitted_ts=event_time,
                last_update_ts=event_time,
                requested_qty=Decimal("0.01"),
                remaining_qty=Decimal("0.01"),
            ),
        )

        payload = order_repo.created_raw_payload
        assert payload is not None
        market_context = payload["lifecycle_snapshot_refs"]["submit"]["market_context_snapshot_refs"]
        self.assertEqual(
            market_context["pre_event_orderbook_snapshot_ref"],
            "bronze.market_orderbook_books5:BTC-USDT-SWAP:2026-04-25T03:48:30.000000Z",
        )
        self.assertEqual(
            market_context["post_event_orderbook_snapshot_ref"],
            "bronze.market_orderbook_books5:BTC-USDT-SWAP:2026-04-25T03:48:31.000000Z",
        )
        self.assertEqual(market_context["capture_status"], "captured")


if __name__ == "__main__":
    unittest.main()
