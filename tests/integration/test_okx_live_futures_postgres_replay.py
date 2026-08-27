from __future__ import annotations

import json
import unittest
from decimal import Decimal
from urllib.parse import parse_qs

import httpx

from aats.bootstrap.settings import AATSSettings
from aats.bus.memory_bus import InMemoryEventBus
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.audit import DecisionAuditRecord
from aats.schemas.common import utc_now
from aats.schemas.decision import BaselineAssessment, DecisionContext, PositionTarget
from aats.schemas.execution import ExecutionPlan, OrderIntent
from aats.schemas.features import FeatureSnapshot
from aats.schemas.governance import PolicyDecision, RiskDecision
from aats.schemas.market import MarketSnapshot
from aats.schemas.reconciliation import ReconciliationReport
from aats.schemas.system import HealthSnapshot
from aats.services.execution_engine.okx_account import OKXAccountService
from aats.services.execution_engine.okx_adapter import OKXExecutionAdapter
from aats.services.execution_engine.okx_rest import OKXRESTClient
from aats.services.execution_engine.order_manager import OrderManager
from aats.services.execution_engine.outbox import PostgresExecutionOutboxPublisher
from aats.services.governance_engine.kill_switch import KillSwitch
from aats.services.governance_engine.mode import RuntimeModeController
from aats.services.portfolio_service.pnl import PortfolioPnLCalculator
from aats.services.portfolio_service.positions import PortfolioService, PortfolioState
from aats.services.portfolio_service.reconstruction import PortfolioReconstructionService
from aats.services.portfolio_service.snapshots import PortfolioSnapshotBuilder
from aats.services.reconciliation_service.replay import ReplayEngine
from aats.storage.audit_repo_postgres import PostgresAuditRepository
from aats.storage.event_store_postgres import PostgresEventStore
from aats.storage.execution_repo_postgres import PostgresExecutionRepository
from aats.storage.fill_outcome_repo_postgres import PostgresFillOutcomeRepository
from aats.storage.obligation_repo_postgres import PostgresExecutionObligationRepository
from aats.storage.outbox_repo_postgres import PostgresOutboxRepository
from aats.storage.portfolio_repo_postgres import PostgresPortfolioRepository
from tests.support.postgres import temporary_postgres_runtime


class TestOKXLiveFuturesPostgresReplay(unittest.IsolatedAsyncioTestCase):
    async def test_live_futures_submit_persists_postgres_state_and_replay_remains_clean(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "guarded_live",
                "market_data_backend": "okx",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "live_submit_enabled": True,
                "guarded_execution_dry_run": False,
                "okx_simulated_trading": False,
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-240329",
                "allowed_symbols": ("BTC-USDT-240329",),
                "max_notional_per_symbol": 10_000.0,
                "max_open_orders": 2,
                "max_target_leverage": 5.0,
                "initial_usdt_balance": 100.0,
                "okx_api_key": "live_key",
                "okx_api_secret": "live_secret",
                "okx_api_passphrase": "live_passphrase",
            }
        )
        captured_requests: list[dict[str, object]] = []

        def response_payload(request: httpx.Request) -> httpx.Response:
            query = {key: values[-1] for key, values in parse_qs(request.url.query.decode("utf-8")).items()}
            request_body = request.content.decode("utf-8") if request.content else ""
            json_body = json.loads(request_body) if request_body else None
            captured_requests.append(
                {
                    "method": request.method,
                    "path": request.url.path,
                    "query": query,
                    "headers": {key.lower(): value for key, value in request.headers.items()},
                    "json": json_body,
                }
            )

            if request.url.path == "/api/v5/account/balance":
                return httpx.Response(
                    200,
                    json={
                        "code": "0",
                        "data": [{"details": [{"ccy": "USDT", "eq": "100", "cashBal": "100", "availEq": "100", "availBal": "100"}]}],
                    },
                )
            if request.url.path == "/api/v5/account/instruments":
                inst_type = query.get("instType")
                if inst_type == "FUTURES":
                    return httpx.Response(
                        200,
                        json={
                            "code": "0",
                            "data": [
                                {
                                    "instId": "BTC-USDT-240329",
                                    "instType": "FUTURES",
                                    "instFamily": "BTC-USDT",
                                    "uly": "BTC-USDT",
                                    "settleCcy": "USDT",
                                    "ctValCcy": "BTC",
                                    "ctVal": "0.01",
                                    "ctMult": "1",
                                    "ctType": "linear",
                                    "lotSz": "1",
                                    "tickSz": "0.1",
                                    "minSz": "1",
                                    "lever": "20",
                                    "maxMktSz": "100",
                                    "maxLmtSz": "100",
                                    "state": "live",
                                }
                            ],
                        },
                    )
                return httpx.Response(200, json={"code": "0", "data": []})
            if request.url.path == "/api/v5/trade/orders-pending":
                return httpx.Response(200, json={"code": "0", "data": []})
            if request.url.path == "/api/v5/trade/fills":
                if query.get("ordId") == "ord_live_future_pg_1":
                    return httpx.Response(
                        200,
                        json={
                            "code": "0",
                            "data": [
                                {
                                    "instId": "BTC-USDT-240329",
                                    "ordId": "ord_live_future_pg_1",
                                    "clOrdId": query.get("clOrdId", "unknown"),
                                    "tradeId": "trade_live_future_pg_1",
                                    "side": "buy",
                                    "fillSz": "2",
                                    "fillPx": "80000",
                                    "fee": "-0.12",
                                    "feeCcy": "USDT",
                                    "fillTime": "1700000001000",
                                }
                            ],
                        },
                    )
                return httpx.Response(200, json={"code": "0", "data": []})
            if request.url.path == "/api/v5/account/positions":
                if query.get("instType") == "FUTURES":
                    return httpx.Response(
                        200,
                        json={
                            "code": "0",
                            "data": [
                                {
                                    "instId": "BTC-USDT-240329",
                                    "pos": "0",
                                    "avgPx": "0",
                                    "markPx": "80000",
                                    "notionalUsd": "0",
                                    "posSide": "net",
                                    "mgnMode": "cross",
                                    "ccy": "USDT",
                                    "lever": "3",
                                }
                            ],
                        },
                    )
                return httpx.Response(200, json={"code": "0", "data": []})
            if request.url.path == "/api/v5/account/config":
                return httpx.Response(
                    200,
                    json={"code": "0", "data": [{"acctLv": "2", "posMode": "net_mode", "autoLoan": False}]},
                )
            if request.url.path == "/api/v5/account/trade-fee":
                return httpx.Response(200, json={"code": "0", "data": [{"maker": "-0.0008", "taker": "0.001"}]})
            if request.url.path == "/api/v5/account/account-position-risk":
                return httpx.Response(
                    200,
                    json={"code": "0", "data": [{"adjEq": "100", "availEq": "100", "imr": "5", "mmr": "2", "mgnRatio": "50"}]},
                )
            if request.url.path == "/api/v5/system/status":
                return httpx.Response(200, json={"code": "0", "data": []})
            if request.url.path == "/api/v5/account/max-size":
                return httpx.Response(200, json={"code": "0", "data": [{"maxBuy": "10", "maxSell": "10"}]})
            if request.url.path == "/api/v5/trade/order" and request.method == "POST":
                assert isinstance(json_body, dict)
                return httpx.Response(
                    200,
                    json={"code": "0", "data": [{"sCode": "0", "sMsg": "", "ordId": "ord_live_future_pg_1", "clOrdId": json_body["clOrdId"]}]},
                )
            if request.url.path == "/api/v5/trade/order" and request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "code": "0",
                        "data": [
                            {
                                "instId": "BTC-USDT-240329",
                                "ordId": "ord_live_future_pg_1",
                                "clOrdId": query.get("clOrdId", "unknown"),
                                "state": "filled",
                                "sz": "2",
                                "accFillSz": "2",
                                "avgPx": "80000",
                                "cTime": "1700000000000",
                                "uTime": "1700000001000",
                            }
                        ],
                    },
                )
            raise AssertionError(f"unexpected_okx_request:{request.method}:{request.url}")

        with temporary_postgres_runtime(use_migrations=True) as (database_runtime, _admin_engine, _schema_name):
            event_store = PostgresEventStore(database_runtime.session_factory)
            audit_repo = PostgresAuditRepository(database_runtime.session_factory)
            execution_repo = PostgresExecutionRepository(database_runtime.session_factory)
            obligation_repo = PostgresExecutionObligationRepository(database_runtime.session_factory)
            outbox_repo = PostgresOutboxRepository(database_runtime.session_factory)
            portfolio_repo = PostgresPortfolioRepository(database_runtime.session_factory)
            fill_outcome_repo = PostgresFillOutcomeRepository(database_runtime.session_factory)
            bus = InMemoryEventBus(event_store=event_store)
            kill_switch = KillSwitch()
            controller = RuntimeModeController(settings=settings, kill_switch=kill_switch)
            rest_client = OKXRESTClient(settings=settings)
            rest_client._client = httpx.AsyncClient(
                base_url=settings.okx_rest_url,
                transport=httpx.MockTransport(response_payload),
            )
            account_service = OKXAccountService(settings=settings, client=rest_client)
            adapter = OKXExecutionAdapter(
                settings=settings,
                client=rest_client,
                account_service=account_service,
                mode_controller=controller,
                price_provider=lambda _symbol: Decimal("80000"),
            )
            execution_outbox = PostgresExecutionOutboxPublisher(
                session_factory=database_runtime.session_factory,
                event_store=event_store,
                execution_repo=execution_repo,
                obligation_repo=obligation_repo,
                outbox_repo=outbox_repo,
                bus=bus,
            )
            portfolio_service = PortfolioService(
                bus=bus,
                state=PortfolioState(
                    initial_usdt_balance=Decimal("100"),
                    default_product_type="derivatives",
                    default_margin_mode="cross",
                ),
                snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
                portfolio_repo=portfolio_repo,
                fill_outcome_repo=fill_outcome_repo,
                price_provider=lambda _symbol: Decimal("80000"),
            )
            await bus.subscribe(topics.FILL_EVENTS, portfolio_service.handle_fill_event)
            manager = OrderManager(
                settings=settings,
                bus=bus,
                adapter=adapter,
                execution_repo=execution_repo,
                execution_outbox_publisher=execution_outbox,
                kill_switch=kill_switch,
            )

            try:
                await portfolio_service.bootstrap_snapshot(snapshot_origin="runtime_bootstrap")
                initial_snapshot_event = event_store.latest(topics.PORTFOLIO_SNAPSHOTS)
                self.assertIsNotNone(initial_snapshot_event)
                if initial_snapshot_event is None:
                    self.fail("expected_initial_portfolio_snapshot")
                decision_id = "decision_live_future_postgres_1"
                now = utc_now()
                market_event = build_envelope(
                    topic=topics.MARKET_SNAPSHOTS,
                    key="BTC-USDT-240329",
                    payload_model=MarketSnapshot(
                        symbol="BTC-USDT-240329",
                        exchange="OKX",
                        snapshot_ts=now,
                        best_bid=Decimal("79999.9"),
                        best_ask=Decimal("80000.1"),
                        last_price=Decimal("80000"),
                        bid_size=Decimal("10"),
                        ask_size=Decimal("10"),
                        volume_24h=Decimal("1000"),
                        kline_15m={"open": "79900", "high": "80100", "low": "79800", "close": "80000"},
                        kline_1h={"open": "79000", "high": "80500", "low": "78800", "close": "80000"},
                    ),
                    source_component="test",
                )
                feature_event = build_envelope(
                    topic=topics.FEATURE_SNAPSHOTS,
                    key="BTC-USDT-240329",
                    payload_model=FeatureSnapshot(
                        symbol="BTC-USDT-240329",
                        snapshot_ts=now,
                        market_snapshot_ref=market_event.event_id,
                        trend_strength=0.72,
                        volatility_state="medium",
                        volatility_value=0.35,
                        momentum_score=0.61,
                        liquidity_score=0.78,
                        regime_indicator="trend",
                        regime_confidence=0.8,
                        multi_timeframe_alignment=0.75,
                        composite_alpha_score=0.42,
                        suggested_position_scale=0.3,
                        volatility_target_scale=0.9,
                        feature_version="test",
                    ),
                    source_component="test",
                )
                health_event = build_envelope(
                    topic=topics.HEALTH_SNAPSHOTS,
                    key="system",
                    payload_model=HealthSnapshot(
                        decision_id=decision_id,
                        mode="guarded_live",
                        operating_state="guarded_live_enabled",
                        status="ok",
                        halted=False,
                    ),
                    source_component="test",
                )
                context_event = build_envelope(
                    topic=topics.DECISION_CONTEXTS,
                    key="BTC-USDT-240329",
                    payload_model=DecisionContext(
                        decision_id=decision_id,
                        symbol="BTC-USDT-240329",
                        timeframe="15m",
                        as_of_ts=now,
                        market_snapshot_ref=market_event.event_id,
                        feature_snapshot_ref=feature_event.event_id,
                        portfolio_snapshot_ref=initial_snapshot_event.event_id,
                        health_snapshot_ref=health_event.event_id,
                        mode="guarded_live",
                        current_position_qty=Decimal("0"),
                        product_type="derivatives",
                        current_exposure_side="flat",
                        current_target_leverage=1.0,
                    ),
                    source_component="test",
                )
                baseline_event = build_envelope(
                    topic=topics.BASELINE_ASSESSMENTS,
                    key="BTC-USDT-240329",
                    payload_model=BaselineAssessment(
                        decision_id=decision_id,
                        symbol="BTC-USDT-240329",
                        regime="trend",
                        direction_bias="long",
                        trend_strength=0.72,
                        volatility_state="medium",
                        confidence=0.8,
                        composite_alpha_score=0.42,
                        suggested_position_scale=0.3,
                        volatility_target_scale=0.9,
                        holding_horizon="intraday",
                        engine_version="test",
                    ),
                    source_component="test",
                )
                target_event = build_envelope(
                    topic=topics.POSITION_TARGETS,
                    key="BTC-USDT-240329",
                    payload_model=PositionTarget(
                        decision_id=decision_id,
                        symbol="BTC-USDT-240329",
                        current_position_qty=Decimal("0"),
                        target_position_qty=Decimal("0.02"),
                        delta_position_qty=Decimal("0.02"),
                        current_notional=Decimal("0"),
                        target_notional=Decimal("1600"),
                        rebalance_reason="test_live_future_postgres_replay",
                        urgency="medium",
                        max_slippage_tolerance_bps=20,
                        source_mix={"baseline": 1.0},
                        decision_expiry_ts=now,
                        product_type="derivatives",
                        current_exposure_side="flat",
                        target_exposure_side="long",
                        position_intent="open_long",
                        target_leverage=3.0,
                        margin_mode="cross",
                        expected_signal_edge_bps=20,
                        expected_cost_bps=4,
                        expected_net_edge_bps=16,
                    ),
                    source_component="test",
                )
                policy_event = build_envelope(
                    topic=topics.POLICY_DECISIONS,
                    key="BTC-USDT-240329",
                    payload_model=PolicyDecision(
                        decision_id=decision_id,
                        mode="guarded_live",
                        allowed=True,
                        execution_allowed=True,
                        submission_allowed=True,
                        dry_run_only=False,
                        requires_human_approval=False,
                        allowed_symbols=["BTC-USDT-240329"],
                        allowed_execution_styles=["exchange", "taker", "market"],
                    ),
                    source_component="test",
                )
                risk_event = build_envelope(
                    topic=topics.RISK_DECISIONS,
                    key="BTC-USDT-240329",
                    payload_model=RiskDecision(
                        decision_id=decision_id,
                        approved=True,
                        modified=False,
                        capped_target_position_qty=Decimal("0.02"),
                        capped_target_notional=Decimal("1600"),
                        required_initial_margin=Decimal("30"),
                        projected_margin_usage=Decimal("0.3"),
                        projected_notional=Decimal("1600"),
                        current_open_order_count=0,
                        risk_budget_multiplier=Decimal("1"),
                        execution_aggressiveness_multiplier=Decimal("1"),
                        risk_score=0.1,
                    ),
                    source_component="test",
                )
                plan_event = build_envelope(
                    topic=topics.EXECUTION_PLANS,
                    key="BTC-USDT-240329",
                    payload_model=ExecutionPlan(
                        plan_id="plan_live_future_postgres_1",
                        decision_id=decision_id,
                        symbol="BTC-USDT-240329",
                        current_position_qty=Decimal("0"),
                        target_position_qty=Decimal("0.02"),
                        approved_target_position_qty=Decimal("0.02"),
                        delta_qty=Decimal("0.02"),
                        side="buy",
                        execution_style="taker",
                        order_type="market",
                        time_in_force="IOC",
                        urgency="medium",
                        max_slippage_tolerance_bps=20,
                        reference_price=Decimal("80000"),
                        td_mode="cross",
                        position_mode="net_mode",
                        pos_side="net",
                        instrument_family="BTC-USDT",
                        settle_currency="USDT",
                        required_initial_margin=Decimal("30"),
                        projected_margin_usage=Decimal("0.3"),
                        projected_notional=Decimal("1600"),
                        risk_budget_multiplier=Decimal("1"),
                        execution_aggressiveness_multiplier=Decimal("1"),
                        product_type="derivatives",
                        target_leverage=3.0,
                        margin_mode="cross",
                        exposure_side="long",
                        execution_action="enter",
                        position_intent="open_long",
                    ),
                    source_component="test",
                )
                intent = OrderIntent(
                    intent_id="intent_live_future_postgres_1",
                    decision_id=decision_id,
                    symbol="BTC-USDT-240329",
                    side="buy",
                    quantity=Decimal("0.02"),
                    execution_style="taker",
                    order_type="market",
                    urgency="medium",
                    time_in_force="IOC",
                    max_slippage_tolerance_bps=20,
                    td_mode="cross",
                    position_mode="net_mode",
                    pos_side="net",
                    instrument_family="BTC-USDT",
                    settle_currency="USDT",
                    required_initial_margin=Decimal("30"),
                    projected_margin_usage=Decimal("0.3"),
                    projected_notional=Decimal("1600"),
                    risk_budget_multiplier=Decimal("1"),
                    execution_aggressiveness_multiplier=Decimal("1"),
                    idempotency_key="intent_live_future_postgres_1",
                    product_type="derivatives",
                    target_leverage=3.0,
                    margin_mode="cross",
                    exposure_side="long",
                    execution_action="enter",
                    position_intent="open_long",
                )
                intent_event = build_envelope(
                    topic=topics.ORDER_INTENTS,
                    key=intent.intent_id,
                    payload_model=intent,
                    source_component="test",
                )

                for envelope in (
                    market_event,
                    feature_event,
                    health_event,
                    context_event,
                    baseline_event,
                    target_event,
                    policy_event,
                    risk_event,
                    plan_event,
                    intent_event,
                ):
                    event_store.append(envelope)

                await manager.handle_order_intent(
                    {
                        "topic": topics.ORDER_INTENTS,
                        "key": intent.intent_id,
                        "payload": intent_event.model_dump(mode="json"),
                    }
                )

                decision_events = event_store.by_decision(decision_id)
                order_update_refs = [
                    envelope.event_id for envelope in decision_events if envelope.topic == topics.ORDER_UPDATES
                ]
                fill_refs = [envelope.event_id for envelope in decision_events if envelope.topic == topics.FILL_EVENTS]
                portfolio_snapshot_refs = [
                    envelope.event_id for envelope in decision_events if envelope.topic == topics.PORTFOLIO_SNAPSHOTS
                ]
                self.assertEqual(len(fill_refs), 1)
                self.assertTrue(order_update_refs)
                self.assertEqual(len(portfolio_snapshot_refs), 1)

                reconciliation = ReconciliationReport(
                    reconciliation_id="recon_live_future_postgres_1",
                    decision_id=decision_id,
                    portfolio_snapshot_ref=portfolio_snapshot_refs[0],
                    as_of_ts=utc_now(),
                    product_type="derivatives",
                    margin_mode="cross",
                    allowed_symbols=["BTC-USDT-240329"],
                    exchange_comparison_enabled=False,
                    order_diff={},
                    fill_diff={},
                    balance_diff={},
                    position_diff={},
                    severity="CLEAN",
                    remediation_action=None,
                    halt_required=False,
                )
                reconciliation_event = build_envelope(
                    topic=topics.RECONCILIATION_REPORTS,
                    key="BTC-USDT-240329",
                    payload_model=reconciliation,
                    source_component="test",
                )
                event_store.append(reconciliation_event)

                record = DecisionAuditRecord(
                    decision_id=decision_id,
                    decision_context_ref=context_event.event_id,
                    baseline_assessment_ref=baseline_event.event_id,
                    position_target_ref=target_event.event_id,
                    policy_decision_ref=policy_event.event_id,
                    risk_decision_ref=risk_event.event_id,
                    execution_plan_ref=plan_event.event_id,
                    order_intent_refs=[intent_event.event_id],
                    order_state_refs=order_update_refs,
                    fill_event_refs=fill_refs,
                    portfolio_delta_ref=portfolio_snapshot_refs[0],
                    reconciliation_refs=[reconciliation_event.event_id],
                )
                audit_repo.upsert(record)
                event_store.append(
                    build_envelope(
                        topic=topics.AUDIT_RECORDS,
                        key=decision_id,
                        payload_model=record,
                        source_component="test",
                    )
                )

                replay = ReplayEngine(
                    event_store=event_store,
                    reconstruction_service=PortfolioReconstructionService(
                        initial_usdt_balance=settings.initial_usdt_balance,
                        snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
                    ),
                    audit_repo=audit_repo,
                    portfolio_repo=portfolio_repo,
                ).replay(decision_id=decision_id)

                self.assertEqual(replay.divergence_count, 0)
                self.assertEqual(replay.portfolio_issues, [])
                self.assertEqual(replay.decision_chain_issues, [])
                self.assertEqual(replay.execution_chain_issues, [])
                self.assertEqual(replay.audit_issues, [])
                self.assertEqual(replay.baseline_switch_issues, [])
                self.assertIsNotNone(replay.final_reconstructed_snapshot)
                self.assertIsNotNone(replay.final_stored_snapshot)

                stored_state = execution_repo.order_states()[0]
                stored_fill = execution_repo.fills()[0]
                latest_snapshot = portfolio_repo.latest()
                self.assertEqual(stored_state.status, "FILLED")
                self.assertEqual(stored_state.submission_mode, "guarded_live_submit")
                self.assertEqual(stored_state.symbol, "BTC-USDT-240329")
                self.assertEqual(stored_fill.fill_qty, Decimal("0.02"))
                self.assertEqual(event_store.count(topic=topics.ORDER_UPDATES), 3)
                self.assertEqual(event_store.count(topic=topics.FILL_EVENTS), 1)
                self.assertEqual(len(fill_outcome_repo.outcomes()), 1)
                self.assertIsNotNone(latest_snapshot)
                if latest_snapshot is None:
                    self.fail("expected_latest_portfolio_snapshot")
                self.assertEqual(len(latest_snapshot.positions), 1)
                self.assertEqual(latest_snapshot.positions[0].symbol, "BTC-USDT-240329")
                self.assertEqual(latest_snapshot.positions[0].position_qty, Decimal("0.02"))

                place_order_requests = [
                    request
                    for request in captured_requests
                    if request["path"] == "/api/v5/trade/order" and request["method"] == "POST"
                ]
                self.assertEqual(len(place_order_requests), 1)
                self.assertEqual(place_order_requests[0]["json"]["sz"], "2")
                self.assertNotIn("x-simulated-trading", place_order_requests[0]["headers"])
            finally:
                await rest_client.aclose()


if __name__ == "__main__":
    unittest.main()
