from __future__ import annotations

import unittest
from datetime import timedelta
from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.bus.memory_bus import InMemoryEventBus
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.ai_shadow import AIDegradationEvent
from aats.schemas.common import dump_payload_exact, utc_now
from aats.schemas.execution import OrderIntent, OrderState
from aats.schemas.operator import ProcessingFailureRecord
from aats.services.ai_service.inference import AIInferenceService
from aats.services.ai_service.prompt_builder import PromptBuilder
from aats.services.ai_service.validator import AssessmentValidator
from aats.services.accounting import UnsupportedFeeCurrencyError
from aats.services.execution_engine.order_manager import OrderManager
from aats.services.execution_engine.paper_adapter import PaperExecutionAdapter
from aats.services.governance_engine.kill_switch import KillSwitch
from aats.services.portfolio_service.pnl import PortfolioPnLCalculator
from aats.services.portfolio_service.positions import PortfolioService, PortfolioState
from aats.services.portfolio_service.reconstruction import PortfolioReconstructionService
from aats.services.portfolio_service.snapshots import PortfolioSnapshotBuilder
from aats.services.reconciliation_service.comparator import StateComparator
from aats.services.reconciliation_service.fetcher import ExchangeStateFetcher
from aats.services.reconciliation_service.repair import ReconciliationRepairService, ReconciliationService
from aats.storage.event_store import InMemoryEventStore
from aats.storage.execution_repo import InMemoryExecutionRepository
from aats.storage.fill_outcome_repo import InMemoryFillOutcomeRepository
from aats.storage.portfolio_repo import InMemoryPortfolioRepository
from aats.storage.reconciliation_repo import InMemoryReconciliationRepository
from aats.schemas.execution import FillEvent


class _NullFetcher:
    def latest_snapshot(self):
        return None


class TestRound2SafetyRegressions(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_order_intent_is_logged_and_not_reprocessed(self) -> None:
        repo = InMemoryExecutionRepository()
        manager = OrderManager(
            settings=AATSSettings.model_validate({}),
            bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
            adapter=PaperExecutionAdapter(price_provider=lambda _symbol: 100.0, taker_fee_bps=5.0),
            execution_repo=repo,
            kill_switch=KillSwitch(),
        )
        intent = OrderIntent(
            intent_id="intent_duplicate_round2",
            decision_id="decision_duplicate_round2",
            symbol="BTC-USDT",
            side="buy",
            quantity=0.001,
            execution_style="exchange",
            order_type="market",
            urgency="medium",
            time_in_force="IOC",
            reduce_only=False,
            close_only=False,
            idempotency_key="duplicate_round2",
        )
        message = {
            "topic": topics.ORDER_INTENTS,
            "key": intent.symbol,
            "payload": build_envelope(
                topic=topics.ORDER_INTENTS,
                key=intent.symbol,
                payload_model=intent,
                source_component="test",
            ).model_dump(mode="json"),
        }

        await manager.handle_order_intent(message)
        with self.assertLogs("aats.execution_engine", level="WARNING") as logs:
            await manager.handle_order_intent(message)

        self.assertEqual(len(repo.order_states()), 1)
        self.assertTrue(any("duplicate_order_intent_ignored" in entry for entry in logs.output))

    def test_invalid_order_state_regression_is_logged(self) -> None:
        repo = InMemoryExecutionRepository()
        filled = OrderState(
            decision_id="decision_transition_round2",
            intent_id="intent_transition_round2",
            symbol="BTC-USDT",
            client_order_id="clord_transition_round2",
            venue="OKX",
            exchange_order_id="ord_transition_round2",
            status="FILLED",
            submission_mode="guarded_simulated_submit",
            submitted_ts=utc_now(),
            last_update_ts=utc_now(),
            requested_qty=1.0,
            filled_qty=1.0,
            remaining_qty=0.0,
            average_fill_price=100.0,
            fees=0.0,
        )
        regressed = filled.model_copy(update={"status": "SUBMITTED", "filled_qty": 0.0, "remaining_qty": 1.0})

        repo.save_order_state(filled)
        with self.assertLogs("aats.execution_repo", level="WARNING") as logs:
            merged = repo.save_order_state(regressed)

        self.assertEqual(merged.status, "FILLED")
        self.assertTrue(any("order_state_transition_rejected" in entry for entry in logs.output))

    def test_invalid_order_state_transition_is_rejected(self) -> None:
        repo = InMemoryExecutionRepository()
        submitted = OrderState(
            decision_id="decision_transition_invalid_round2",
            intent_id="intent_transition_invalid_round2",
            symbol="BTC-USDT",
            client_order_id="clord_transition_invalid_round2",
            venue="OKX",
            exchange_order_id="ord_transition_invalid_round2",
            status="SUBMITTED",
            submission_mode="guarded_simulated_submit",
            submitted_ts=utc_now(),
            last_update_ts=utc_now(),
            requested_qty=1.0,
            filled_qty=0.0,
            remaining_qty=1.0,
            average_fill_price=None,
            fees=0.0,
        )
        blocked = submitted.model_copy(update={"status": "BLOCKED"})

        repo.save_order_state(submitted)
        with self.assertRaises(ValueError):
            repo.save_order_state(blocked)

    def test_dump_payload_exact_preserves_decimal_precision(self) -> None:
        payload = dump_payload_exact(
            {
                "qty": Decimal("0.123456789123456789"),
                "price": Decimal("1.000000000000000001"),
                "nested": [Decimal("0.000000000000000019")],
            }
        )

        self.assertEqual(payload["qty"], "0.123456789123456789")
        self.assertEqual(payload["price"], "1.000000000000000001")
        self.assertEqual(payload["nested"][0], "0.000000000000000019")

    def test_ai_service_restores_degraded_state_from_durable_event(self) -> None:
        event_store = InMemoryEventStore()
        degradation_event = AIDegradationEvent(
            symbol="BTC-USDT",
            timeframe="15m",
            product_type="spot",
            margin_mode="cash",
            allowed_symbols=("BTC-USDT",),
            configured_operating_mode="ai_primary",
            effective_operating_mode="baseline_only",
            degraded=True,
            provider_degraded=True,
            outcome_review_required=False,
            auto_downgrade_active=True,
            reason_code="ai_timeout",
            consecutive_failures=3,
            consecutive_successes=0,
            recovery_probe_after=utc_now() + timedelta(minutes=5),
        )
        event_store.append(
            build_envelope(
                topic=topics.AI_DEGRADATION_EVENTS,
                key="BTC-USDT",
                payload_model=degradation_event,
                source_component="test",
            )
        )

        service = AIInferenceService(
            settings=AATSSettings.model_validate(
                {"ai_operating_mode": "ai_primary", "ai_provider": "disabled", "default_symbol": "BTC-USDT"}
            ),
            event_store=event_store,
            prompt_builder=PromptBuilder(),
            validator=AssessmentValidator(),
        )

        status = service.status()
        self.assertTrue(status["degraded"])
        self.assertTrue(status["provider_degraded"])
        self.assertEqual(status["effective_operating_mode"], "baseline_only")
        self.assertEqual(status["degradation_reason"], "ai_timeout")

    async def test_processing_failure_triggers_snapshot_repair_dispatch(self) -> None:
        service = ReconciliationService(
            settings=AATSSettings.model_validate({}),
            bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
            fetcher=ExchangeStateFetcher(account_service=_NullFetcher()),  # type: ignore[arg-type]
            comparator=StateComparator(),
            repair_service=ReconciliationRepairService(),
            reconciliation_repo=InMemoryReconciliationRepository(),
            execution_repo=InMemoryExecutionRepository(),
            portfolio_repo=InMemoryPortfolioRepository(),
            event_store=InMemoryEventStore(),
            reconstruction_service=PortfolioReconstructionService(
                initial_usdt_balance=10_000.0,
                snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
            ),
            price_provider=lambda _symbol: 100.0,
        )
        calls: list[tuple[str, str]] = []

        async def fake_repair_missing_portfolio_snapshot(*, reason: str = "background_refresh"):
            calls.append(("repair", reason))
            return None

        async def fake_validate_now(*, reason: str = "operator_validate"):
            calls.append(("validate", reason))
            return None

        service.repair_missing_portfolio_snapshot = fake_repair_missing_portfolio_snapshot  # type: ignore[method-assign]
        service.validate_now = fake_validate_now  # type: ignore[method-assign]
        failure = ProcessingFailureRecord(
            subsystem="portfolio_service",
            stage="portfolio_snapshot_persist",
            severity="error",
            message="snapshot_save_failed",
            decision_id="decision_processing_failure_round2",
            intent_id="intent_processing_failure_round2",
            order_id="clord_processing_failure_round2",
            fill_id="fill_processing_failure_round2",
            symbol="BTC-USDT",
            retriable=True,
            observed_at=utc_now(),
        )
        message = {
            "topic": topics.PROCESSING_FAILURES,
            "key": failure.symbol,
            "payload": build_envelope(
                topic=topics.PROCESSING_FAILURES,
                key=failure.symbol or "portfolio",
                payload_model=failure,
                source_component="test",
            ).model_dump(mode="json"),
        }

        await service.handle_processing_failure(message)

        self.assertEqual(
            calls,
            [("repair", "processing_failure_repair"), ("validate", "processing_failure_repair")],
        )

    async def test_fill_application_emits_immutable_balance_delta_event(self) -> None:
        event_store = InMemoryEventStore()
        bus = InMemoryEventBus(event_store=event_store, persistence_mode="strict")
        service = PortfolioService(
            bus=bus,
            state=PortfolioState(initial_usdt_balance=1_000.0),
            snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
            portfolio_repo=InMemoryPortfolioRepository(),
            fill_outcome_repo=InMemoryFillOutcomeRepository(),
            price_provider=lambda _symbol: 100.0,
        )
        fill = FillEvent(
            fill_id="fill_balance_delta_round2",
            decision_id="decision_balance_delta_round2",
            intent_id="intent_balance_delta_round2",
            client_order_id="clord_balance_delta_round2",
            exchange_order_id="ord_balance_delta_round2",
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

        await service.handle_fill_event(
            {
                "topic": topics.FILL_EVENTS,
                "key": fill.symbol,
                "payload": build_envelope(
                    topic=topics.FILL_EVENTS,
                    key=fill.symbol,
                    payload_model=fill,
                    source_component="test",
                ).model_dump(mode="json"),
            }
        )

        delta_event = event_store.latest(topics.PORTFOLIO_BALANCE_DELTAS, key="BTC-USDT")
        self.assertIsNotNone(delta_event)
        self.assertEqual(delta_event.payload["fill_id"], "fill_balance_delta_round2")
        self.assertEqual(str(delta_event.payload["balance_deltas"]["USDT"]), "-100.50")
        self.assertEqual(str(delta_event.payload["balance_deltas"]["BTC"]), "1.0")

    async def test_fill_apply_failure_emits_processing_failure_and_restores_portfolio_state(self) -> None:
        event_store = InMemoryEventStore()
        bus = InMemoryEventBus(event_store=event_store, persistence_mode="strict")
        service = PortfolioService(
            bus=bus,
            state=PortfolioState(initial_usdt_balance=1_000.0),
            snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
            portfolio_repo=InMemoryPortfolioRepository(),
            fill_outcome_repo=InMemoryFillOutcomeRepository(),
            price_provider=lambda _symbol: 100.0,
        )
        fill = FillEvent(
            fill_id="fill_unknown_fee_round2",
            decision_id="decision_unknown_fee_round2",
            intent_id="intent_unknown_fee_round2",
            client_order_id="clord_unknown_fee_round2",
            exchange_order_id="ord_unknown_fee_round2",
            symbol="BTC-USDT",
            venue="OKX",
            side="buy",
            fill_qty=1.0,
            fill_price=100.0,
            fee_amount=0.5,
            fee_currency="ETH",
            liquidity_role="taker",
            exchange_timestamp=utc_now(),
            ingestion_timestamp=utc_now(),
            order_status_after_fill="FILLED",
        )

        with self.assertRaisesRegex(
            UnsupportedFeeCurrencyError,
            "unsupported_fill_fee_currency:ETH:BTC-USDT:BTC:USDT",
        ):
            await service.handle_fill_event(
                {
                    "topic": topics.FILL_EVENTS,
                    "key": fill.symbol,
                    "payload": build_envelope(
                        topic=topics.FILL_EVENTS,
                        key=fill.symbol,
                        payload_model=fill,
                        source_component="test",
                    ).model_dump(mode="json"),
                }
            )

        processing_failure = event_store.latest(topics.PROCESSING_FAILURES, key="BTC-USDT")
        self.assertIsNotNone(processing_failure)
        assert processing_failure is not None
        self.assertEqual(processing_failure.payload["stage"], "fill_apply")
        self.assertEqual(processing_failure.payload["message"], "unsupported_fill_fee_currency:ETH:BTC-USDT:BTC:USDT")
        self.assertEqual(processing_failure.payload["details"]["fee_currency"], "ETH")
        self.assertEqual(processing_failure.payload["details"]["base_currency"], "BTC")
        self.assertEqual(processing_failure.payload["details"]["quote_currency"], "USDT")
        self.assertEqual(service.state.positions, {})
        self.assertEqual(service.state.balances["USDT"], Decimal("1000.0"))
        self.assertIsNone(event_store.latest(topics.PORTFOLIO_SNAPSHOTS, key="portfolio"))


if __name__ == "__main__":
    unittest.main()
