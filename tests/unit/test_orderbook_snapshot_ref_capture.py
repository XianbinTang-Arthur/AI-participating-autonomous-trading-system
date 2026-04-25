from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import patch

from aats.schemas.execution import OrderState
from aats.services.execution_engine.orderbook_snapshot_refs import (
    OrderbookSnapshotReadSource,
    build_orderbook_snapshot_read_source,
    capture_orderbook_snapshot_refs_for_event,
    default_orderbook_snapshot_read_source,
    reset_default_orderbook_snapshot_read_source_for_tests,
    resolve_orderbook_snapshot_ref_row,
    resolve_orderbook_market_context_db_url,
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
        self.close_count = 0

    def execute(self, _statement: Any, params: dict[str, Any]) -> _FakeExecuteResult:
        self.execute_calls.append(dict(params))
        row = self._rows.pop(0) if self._rows else None
        return _FakeExecuteResult(row)

    def close(self) -> None:
        self.close_count += 1


class _FailingSession:
    def execute(self, _statement: Any, _params: dict[str, Any]) -> None:
        raise RuntimeError("bronze_orderbook_unavailable")


class TestOrderbookSnapshotRefCapture(unittest.TestCase):
    def test_resolves_market_context_db_url_with_explicit_precedence(self) -> None:
        env = {
            "AATS_MARKET_CONTEXT_DB_URL": "postgresql+psycopg://user:pw@host:5432/explicit_context",
            "AATS_ACTIVE_PARAMETER_DB_URL": "postgresql+psycopg://user:pw@host:5432/active_parameters",
            "RDP_DATABASE_URL": "postgresql+psycopg://user:pw@host:5432/rdp",
        }

        self.assertEqual(
            resolve_orderbook_market_context_db_url(env),
            "postgresql+psycopg://user:pw@host:5432/explicit_context",
        )
        env.pop("AATS_MARKET_CONTEXT_DB_URL")
        self.assertEqual(
            resolve_orderbook_market_context_db_url(env),
            "postgresql+psycopg://user:pw@host:5432/active_parameters",
        )
        env.pop("AATS_ACTIVE_PARAMETER_DB_URL")
        self.assertEqual(
            resolve_orderbook_market_context_db_url(env),
            "postgresql+psycopg://user:pw@host:5432/rdp",
        )

    def test_build_source_uses_read_only_postgres_options_and_source_name(self) -> None:
        url = "postgresql+psycopg://user:pw@host:5432/aats_research"

        with patch("aats.services.execution_engine.orderbook_snapshot_refs.create_engine") as create_engine_mock:
            source = build_orderbook_snapshot_read_source(url)

        self.assertEqual(source.source_name, "aats_research")
        self.assertEqual(create_engine_mock.call_args.args, (url,))
        self.assertIn("default_transaction_read_only=on", create_engine_mock.call_args.kwargs["connect_args"]["options"])
        self.assertEqual(create_engine_mock.call_args.kwargs["pool_size"], 1)
        self.assertEqual(create_engine_mock.call_args.kwargs["max_overflow"], 1)

    def test_default_source_invalid_url_fails_soft(self) -> None:
        reset_default_orderbook_snapshot_read_source_for_tests()
        with patch.dict("os.environ", {"AATS_MARKET_CONTEXT_DB_URL": "not-a-db-url"}, clear=True):
            self.assertIsNone(default_orderbook_snapshot_read_source())
        reset_default_orderbook_snapshot_read_source_for_tests()

    def test_resolves_bbo_ref_row_with_stable_checksum(self) -> None:
        ts = datetime(2026, 4, 25, 3, 48, 30, tzinfo=timezone.utc)
        row = {
            "symbol": "BTC-USDT-SWAP",
            "ts": ts,
            "source_ts": ts,
            "bid_px": Decimal("77000.1000000000"),
            "bid_sz": Decimal("0.1000"),
            "ask_px": Decimal("77000.2000000000"),
            "ask_sz": Decimal("0.2000"),
            "ingest_run_id": "11111111-1111-1111-1111-111111111111",
            "received_at": ts,
        }
        session = _FakeSession([row, dict(row)])
        source = OrderbookSnapshotReadSource(
            session_factory=lambda: session,  # type: ignore[arg-type]
            source_name="aats_research",
        )

        first = resolve_orderbook_snapshot_ref_row(
            "aats_research.bronze.market_orderbook_bbo:BTC-USDT-SWAP:2026-04-25T03:48:30.000000Z",
            expected_symbol="BTC-USDT-SWAP",
            market_context_source=source,
            use_default_source=False,
        )
        second = resolve_orderbook_snapshot_ref_row(
            "aats_research.bronze.market_orderbook_bbo:BTC-USDT-SWAP:2026-04-25T03:48:30.000000Z",
            expected_symbol="BTC-USDT-SWAP",
            market_context_source=source,
            use_default_source=False,
        )

        self.assertEqual(first["row_lookup_status"], "row_resolved")
        self.assertTrue(first["row_exists"])
        self.assertTrue(first["content_checksum"].startswith("sha256:"))
        self.assertEqual(first["content_checksum"], second["content_checksum"])
        self.assertEqual(first["sequence_key"]["source_ts"], "2026-04-25T03:48:30.000000Z")

    def test_ref_row_resolver_reports_missing_row_without_default_source(self) -> None:
        session = _FakeSession([None])
        source = OrderbookSnapshotReadSource(
            session_factory=lambda: session,  # type: ignore[arg-type]
            source_name="aats_research",
        )

        payload = resolve_orderbook_snapshot_ref_row(
            "bronze.market_orderbook_books5:BTC-USDT-SWAP:2026-04-25T03:48:30.000000Z",
            expected_symbol="BTC-USDT-SWAP",
            market_context_source=source,
            use_default_source=False,
        )

        self.assertEqual(payload["ts"], "2026-04-25T03:48:30.000000Z")
        self.assertEqual(payload["row_lookup_status"], "row_missing")
        self.assertFalse(payload["row_exists"])
        self.assertIn("orderbook_row_missing", payload["missing_evidence"])

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

    def test_captures_from_readonly_market_context_source_first(self) -> None:
        event_time = datetime(2026, 4, 25, 3, 48, 30, 500000, tzinfo=timezone.utc)
        execution_session = _FailingSession()
        source_session = _FakeSession(
            [
                {"ts": datetime(2026, 4, 25, 3, 48, 30, tzinfo=timezone.utc)},
                {"ts": datetime(2026, 4, 25, 3, 48, 31, tzinfo=timezone.utc)},
            ]
        )
        source = OrderbookSnapshotReadSource(
            session_factory=lambda: source_session,  # type: ignore[arg-type]
            source_name="aats_research",
        )

        refs = capture_orderbook_snapshot_refs_for_event(
            execution_session,  # type: ignore[arg-type]
            symbol="BTC-USDT-SWAP",
            event_time=event_time,
            market_context_source=source,
        )

        self.assertEqual(
            refs["pre_event_orderbook_snapshot_ref"],
            "aats_research.bronze.market_orderbook_books5:BTC-USDT-SWAP:2026-04-25T03:48:30.000000Z",
        )
        self.assertEqual(
            refs["post_event_orderbook_snapshot_ref"],
            "aats_research.bronze.market_orderbook_books5:BTC-USDT-SWAP:2026-04-25T03:48:31.000000Z",
        )
        self.assertEqual(len(source_session.execute_calls), 2)
        self.assertEqual(source_session.close_count, 1)

    def test_market_context_source_failure_falls_back_to_execution_session(self) -> None:
        event_time = datetime(2026, 4, 25, 3, 48, 30, tzinfo=timezone.utc)
        execution_session = _FakeSession([{"ts": event_time}, {"ts": event_time}])
        source = OrderbookSnapshotReadSource(
            session_factory=lambda: _FailingSession(),  # type: ignore[arg-type]
            source_name="aats_research",
        )

        refs = capture_orderbook_snapshot_refs_for_event(
            execution_session,  # type: ignore[arg-type]
            symbol="BTC-USDT-SWAP",
            event_time=event_time,
            market_context_source=source,
        )

        self.assertEqual(
            refs["pre_event_orderbook_snapshot_ref"],
            "bronze.market_orderbook_books5:BTC-USDT-SWAP:2026-04-25T03:48:30.000000Z",
        )
        self.assertEqual(
            refs["post_event_orderbook_snapshot_ref"],
            "bronze.market_orderbook_books5:BTC-USDT-SWAP:2026-04-25T03:48:30.000000Z",
        )
        self.assertEqual(len(execution_session.execute_calls), 2)

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

    def test_converged_repo_uses_market_context_read_source(self) -> None:
        from aats.services.execution_engine.state_machine import OrderStateMachine
        from aats.storage.execution_repo_converged_postgres import ConvergedPostgresExecutionRepository

        event_time = datetime(2026, 4, 25, 3, 48, 30, 500000, tzinfo=timezone.utc)
        execution_session = _FailingSession()
        source_session = _FakeSession(
            [
                {"ts": datetime(2026, 4, 25, 3, 48, 30, tzinfo=timezone.utc)},
                {"ts": datetime(2026, 4, 25, 3, 48, 31, tzinfo=timezone.utc)},
            ]
        )
        order_repo = self._OrderRepo()
        repo = object.__new__(ConvergedPostgresExecutionRepository)
        repo.execution_order_repo = order_repo  # type: ignore[attr-defined]
        repo.execution_order_history_repo = None  # type: ignore[attr-defined]
        repo.orderbook_snapshot_read_source = OrderbookSnapshotReadSource(  # type: ignore[attr-defined]
            session_factory=lambda: source_session,  # type: ignore[arg-type]
            source_name="aats_research",
        )
        repo.state_machine = OrderStateMachine()  # type: ignore[attr-defined]

        repo.save_order_state_in_session(
            session=execution_session,  # type: ignore[arg-type]
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
            "aats_research.bronze.market_orderbook_books5:BTC-USDT-SWAP:2026-04-25T03:48:30.000000Z",
        )
        self.assertEqual(
            market_context["post_event_orderbook_snapshot_ref"],
            "aats_research.bronze.market_orderbook_books5:BTC-USDT-SWAP:2026-04-25T03:48:31.000000Z",
        )
        self.assertEqual(market_context["capture_status"], "captured")


if __name__ == "__main__":
    unittest.main()
