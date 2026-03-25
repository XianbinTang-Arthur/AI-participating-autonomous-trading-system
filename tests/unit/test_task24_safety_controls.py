from __future__ import annotations

import unittest
from decimal import Decimal

from aats.bootstrap.config import build_runtime
from aats.bootstrap.settings import AATSSettings
from aats.bus.memory_bus import InMemoryEventBus
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.common import utc_now
from aats.schemas.execution import FillEvent, OrderIntent
from aats.schemas.exchange import ExchangeAccountSnapshot, ExchangeBalance
from aats.services.execution_engine.obligations import ExecutionObligationService
from aats.services.execution_engine.paper_adapter import PaperExecutionAdapter
from aats.services.portfolio_service.pnl import PortfolioPnLCalculator
from aats.services.portfolio_service.positions import PortfolioService, PortfolioState
from aats.services.portfolio_service.snapshots import PortfolioSnapshotBuilder
from aats.storage.event_store import InMemoryEventStore
from aats.storage.fill_outcome_repo import InMemoryFillOutcomeRepository
from aats.storage.obligation_repo import InMemoryExecutionObligationRepository


class _FailingPortfolioRepository:
    def save_snapshot(self, snapshot) -> None:
        _ = snapshot
        raise RuntimeError("snapshot_save_failed")

    def latest(self):
        return None

    def history(self):
        return []

    def recent_history(self, *, limit: int):
        _ = limit
        return []

    def history_for_scope(self, *, scope, limit=None):
        _ = (scope, limit)
        return []

    def latest_for_scope(self, *, scope):
        _ = scope
        return None


class TestTask24SafetyControls(unittest.IsolatedAsyncioTestCase):
    async def test_portfolio_service_rolls_back_state_when_snapshot_save_fails(self) -> None:
        event_store = InMemoryEventStore()
        state = PortfolioState(initial_usdt_balance=1_000.0)
        service = PortfolioService(
            bus=InMemoryEventBus(event_store=event_store, persistence_mode="strict"),
            state=state,
            snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
            portfolio_repo=_FailingPortfolioRepository(),
            fill_outcome_repo=InMemoryFillOutcomeRepository(),
            price_provider=lambda _symbol: 100.0,
        )
        fill = FillEvent(
            fill_id="fill_portfolio_rollback",
            decision_id="decision_portfolio_rollback",
            intent_id="intent_portfolio_rollback",
            client_order_id="clord_portfolio_rollback",
            exchange_order_id="paper_portfolio_rollback",
            symbol="BTC-USDT",
            venue="PAPER",
            side="buy",
            fill_qty=1.0,
            fill_price=100.0,
            fee_amount=0.5,
            fee_currency="USDT",
            liquidity_role="taker",
            exchange_timestamp=utc_now(),
            ingestion_timestamp=utc_now(),
            order_status_after_fill="FILLED",
        )
        message = {
            "topic": topics.FILL_EVENTS,
            "key": fill.symbol,
            "payload": build_envelope(
                topic=topics.FILL_EVENTS,
                key=fill.symbol,
                payload_model=fill,
                source_component="test",
            ).model_dump(mode="json"),
        }

        with self.assertRaisesRegex(RuntimeError, "snapshot_save_failed"):
            await service.handle_fill_event(message)

        self.assertEqual(state.balances["USDT"], Decimal("1000.0"))
        self.assertNotIn("BTC", state.balances)
        self.assertEqual(state.positions, {})
        self.assertFalse(state.has_applied_fill(fill.fill_id))
        self.assertEqual(state.realized_pnl, Decimal("0"))
        self.assertEqual(state.total_fees_paid, Decimal("0"))
        failure_events = event_store.by_topic(topics.PROCESSING_FAILURES)
        self.assertEqual(len(failure_events), 1)
        self.assertEqual(failure_events[0].payload["stage"], "portfolio_snapshot_persist")
        self.assertEqual(failure_events[0].payload["fill_id"], fill.fill_id)

    async def test_obligation_consumption_is_idempotent_for_duplicate_fill(self) -> None:
        snapshot = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            balances=[ExchangeBalance(currency="USDT", total=150.0, available=150.0, frozen=0.0)],
        )
        repo = InMemoryExecutionObligationRepository()
        service = ExecutionObligationService(
            settings=AATSSettings.model_validate({"account_backend": "okx", "account_read_enabled": True}),
            obligation_repo=repo,
            account_snapshot_loader=lambda: _return_snapshot(snapshot),
            price_provider=lambda _symbol: 100.0,
        )
        intent = OrderIntent(
            intent_id="intent_obligation_idempotent",
            decision_id="decision_obligation_idempotent",
            symbol="BTC-USDT",
            side="buy",
            quantity=1.0,
            execution_style="exchange",
            order_type="market",
            reference_price=100.0,
            urgency="medium",
            time_in_force="IOC",
            max_slippage_tolerance_bps=100,
            idempotency_key="clord_obligation_idempotent",
        )
        obligation = await service.reserve_for_intent(intent=intent, client_order_id="clord_obligation_idempotent")
        self.assertIsNotNone(obligation)
        self.assertEqual(obligation.reserved_amount, Decimal("101.0505"))

        fill = FillEvent(
            fill_id="fill_duplicate_once",
            decision_id=intent.decision_id,
            intent_id=intent.intent_id,
            client_order_id="clord_obligation_idempotent",
            exchange_order_id="ord_obligation_idempotent",
            symbol=intent.symbol,
            venue="OKX",
            side="buy",
            fill_qty=1.0,
            fill_price=100.0,
            fee_amount=0.0,
            fee_currency="USDT",
            liquidity_role="taker",
            exchange_timestamp=utc_now(),
            ingestion_timestamp=utc_now(),
            order_status_after_fill="FILLED",
        )

        first = service.consume_for_fill(fill)
        second = service.consume_for_fill(fill)

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(first.consumed_amount, Decimal("100.0"))
        self.assertEqual(second.consumed_amount, Decimal("100.0"))
        self.assertEqual(second.consumed_fill_ids, ["fill_duplicate_once"])

    async def test_paper_adapter_rejects_execution_when_slippage_tolerance_is_exceeded(self) -> None:
        adapter = PaperExecutionAdapter(
            price_provider=lambda _symbol: 102.0,
            taker_fee_bps=5.0,
        )
        intent = OrderIntent(
            intent_id="intent_slippage_guard",
            decision_id="decision_slippage_guard",
            symbol="BTC-USDT",
            side="buy",
            quantity=1.0,
            execution_style="exchange",
            order_type="market",
            reference_price=100.0,
            urgency="medium",
            time_in_force="IOC",
            max_slippage_tolerance_bps=100,
            idempotency_key="slippage_guard",
        )

        state, fills = await adapter.submit(intent)

        self.assertEqual(state.status, "REJECTED")
        self.assertEqual(state.execution_error, "slippage_tolerance_exceeded")
        self.assertEqual(fills, [])

    async def test_paper_adapter_expires_limit_ioc_when_price_does_not_cross(self) -> None:
        adapter = PaperExecutionAdapter(
            price_provider=lambda _symbol: Decimal("101"),
            taker_fee_bps=5.0,
        )
        intent = OrderIntent(
            intent_id="intent_limit_ioc_expired",
            decision_id="decision_limit_ioc_expired",
            symbol="BTC-USDT",
            side="buy",
            quantity=Decimal("1"),
            execution_style="bounded_limit_ioc",
            order_type="limit",
            limit_price=Decimal("100.1"),
            reference_price=Decimal("100"),
            urgency="medium",
            time_in_force="IOC",
            max_slippage_tolerance_bps=20,
            idempotency_key="limit_ioc_expired",
        )

        state, fills = await adapter.submit(intent)

        self.assertEqual(state.status, "EXPIRED")
        self.assertEqual(state.cancel_reason, "paper_limit_ioc_not_crossed")
        self.assertEqual(fills, [])

    async def test_paper_adapter_fills_limit_ioc_when_price_crosses_cap(self) -> None:
        adapter = PaperExecutionAdapter(
            price_provider=lambda _symbol: Decimal("100.05"),
            taker_fee_bps=5.0,
        )
        intent = OrderIntent(
            intent_id="intent_limit_ioc_filled",
            decision_id="decision_limit_ioc_filled",
            symbol="BTC-USDT",
            side="buy",
            quantity=Decimal("1"),
            execution_style="bounded_limit_ioc",
            order_type="limit",
            limit_price=Decimal("100.1"),
            reference_price=Decimal("100"),
            urgency="medium",
            time_in_force="IOC",
            max_slippage_tolerance_bps=20,
            idempotency_key="limit_ioc_filled",
        )

        state, fills = await adapter.submit(intent)

        self.assertEqual(state.status, "FILLED")
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].fill_price, Decimal("100.05"))

    async def test_background_loop_failure_emits_structured_processing_failure(self) -> None:
        runtime = await build_runtime(
            AATSSettings.model_validate(
                {
                    "storage_mode": "memory",
                    "market_data_backend": "demo",
                    "execution_backend": "paper",
                    "account_backend": "disabled",
                    "account_read_enabled": False,
                }
            )
        )

        runtime._record_background_failure(subsystem="unit_test_loop", exc=RuntimeError("boom"))

        error_summary = runtime.event_store.latest(topics.EXECUTION_ERROR_SUMMARIES, key="unit_test_loop")
        processing_failure = runtime.event_store.latest(topics.PROCESSING_FAILURES, key="unit_test_loop")
        self.assertIsNotNone(error_summary)
        self.assertIsNotNone(processing_failure)
        self.assertEqual(error_summary.payload["subsystem"], "unit_test_loop")
        self.assertEqual(processing_failure.payload["subsystem"], "unit_test_loop")
        self.assertEqual(processing_failure.payload["stage"], "background_loop")


async def _return_snapshot(snapshot: ExchangeAccountSnapshot) -> ExchangeAccountSnapshot:
    return snapshot


if __name__ == "__main__":
    unittest.main()
