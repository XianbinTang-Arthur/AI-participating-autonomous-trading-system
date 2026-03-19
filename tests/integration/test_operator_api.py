from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aats.api.auth_routes import auth_router
from aats.api.routes import router
from aats.bootstrap.config import build_runtime
from aats.bootstrap.settings import AATSSettings
from aats.events.envelopes import build_envelope
from aats.schemas.audit import DecisionAuditRecord
from aats.schemas.ai_brief import AIDecisionBrief
from aats.schemas.ai_shadow import AIDegradationEvent, AITakeoverDecision
from aats.schemas.common import utc_now
from aats.schemas.decision import AIMarketAssessment
from aats.schemas.execution import FillEvent, OrderState
from aats.schemas.exchange import AccountBaselineSnapshot, ExchangeAccountSnapshot, ExchangeBalance, ExchangeOpenOrder
from aats.events import topics
from aats.schemas.operator import ExecutionErrorSummary, ReplayValidationSummary
from aats.schemas.operator import OperatorUserRecord
from aats.schemas.strategy_profiles import StrategyProfileMarketRegimeAssessment, StrategyProfileRecommendation
from aats.services.ai_service.provider import AIProviderResponse
from aats.services.operator.strategy_profiles import StrategyProfileControlService
from aats.services.operator.passwords import hash_password


class FakeOperatorAccountService:
    SNAPSHOT: ExchangeAccountSnapshot | None = None
    PRIVATE_ORDER_ROW: dict | None = None
    PRIVATE_ORDER_FILLS: list | None = None

    def __init__(self, *, settings, client, private_ws_client=None) -> None:
        self.settings = settings
        self.client = client
        self.private_ws_client = private_ws_client
        self._snapshot = self.SNAPSHOT

    async def refresh(self, *, force: bool = False):
        return self._snapshot

    def latest_snapshot(self):
        return self._snapshot

    def instrument_metadata(self, symbol: str):
        return None

    def open_order_count(self, symbol: str | None = None) -> int:
        return len(self._snapshot.open_orders) if self._snapshot is not None else 0

    def recent_fills(self, symbol: str | None = None):
        return list(self._snapshot.fills) if self._snapshot is not None else []

    async def recent_bills(self, *, symbol: str | None = None, limit: int | None = None):
        _ = symbol
        _ = limit
        return [
            {"billId": "bill_1", "type": "1", "subType": "173", "ccy": "USDT", "bal": "1000"},
            {"billId": "bill_2", "type": "2", "subType": "174", "ccy": "USDT", "bal": "998"},
        ]

    def recent_bills_summary(self):
        return {
            "available": True,
            "count": 2,
            "latest_bill_id": "bill_2",
            "latest_bill_ts": utc_now(),
            "currencies": ["USDT"],
            "top_categories": [
                {
                    "type": "1",
                    "sub_type": "173",
                    "currency": "USDT",
                    "count": 1,
                    "type_label": "transfer",
                    "sub_type_label": "funding_fee_expense",
                    "semantic_group": "funding_fee",
                    "human_label": "transfer:funding_fee_expense:USDT",
                }
            ],
            "last_error": None,
        }

    def latest_private_order_row(self, *, symbol: str, order_id: str | None = None, client_order_id: str | None = None):
        _ = symbol
        _ = order_id
        _ = client_order_id
        return self.PRIVATE_ORDER_ROW

    def latest_private_order_fills(self, *, symbol: str, order_id: str | None = None, client_order_id: str | None = None):
        _ = symbol
        _ = order_id
        _ = client_order_id
        return list(self.PRIVATE_ORDER_FILLS or [])

    def status(self):
        return {
            "backend": "okx",
            "enabled": True,
            "credentials_configured": True,
            "connected": self._snapshot is not None,
            "fresh": self._snapshot is not None,
            "last_update_ts": self._snapshot.fetched_at if self._snapshot is not None else None,
            "last_error": None,
            "ready": self._snapshot is not None,
            "detail": "fake_operator_account",
            "blockers": [] if self._snapshot is not None else ["account_snapshot_missing"],
        }


class FakeShadowProvider:
    async def generate_assessment(self, *, prompt: str, response_schema: dict[str, object]) -> AIProviderResponse:
        _ = prompt
        _ = response_schema
        return AIProviderResponse(
            provider_name="fake_shadow_provider",
            request_id="shadow_req",
            latency_ms=8.0,
            payload={
                "regime": "trend",
                "directional_edge": -0.42,
                "expected_volatility": 0.07,
                "confidence": 0.86,
                "uncertainty": 0.18,
                "expected_holding_horizon": "15m",
                "invalidation_conditions": ["trend_break", "book_flip"],
                "risk_tags": ["shadow_ok"],
                "rationale_summary": "shadow_primary_signal",
                "baseline_override_recommended": True,
                "override_reason_codes": ["ai_trend_override"],
                "execution_parameter_suggestion": {
                    "passive_bias": 0.78,
                    "maker_taker_bias": -0.3,
                    "max_cross_spread_bps": 3.5,
                    "slice_count": 2,
                    "max_participation_rate": 0.2,
                },
            },
        )


class TestOperatorAPI(unittest.IsolatedAsyncioTestCase):
    async def test_system_status_and_mode_endpoints_are_operator_readable(self) -> None:
        runtime = await self._runtime()
        app = self._app(runtime)
        with TestClient(app) as client:
            health = client.get("/system/health")
            mode = client.get("/system/mode")
            runtime_response = client.get("/system/runtime")
            blockers = client.get("/system/blockers")
            blocker_history = client.get("/system/blocker-history?limit=5")
            metrics = client.get("/system/metrics")

        self.assertEqual(health.status_code, 200)
        self.assertEqual(mode.status_code, 200)
        self.assertEqual(runtime_response.status_code, 200)
        self.assertEqual(blockers.status_code, 200)
        self.assertEqual(metrics.status_code, 200)

        health_payload = health.json()
        mode_payload = mode.json()
        runtime_payload = runtime_response.json()
        blockers_payload = blockers.json()
        blocker_history_payload = blocker_history.json()
        metrics_payload = metrics.json()

        self.assertIn("overall_status", health_payload)
        self.assertIn("subsystems", health_payload)
        self.assertIn("execution_summary", health_payload)
        self.assertIn("storage", health_payload["subsystems"])
        self.assertIn("audit_replay", health_payload["subsystems"])
        self.assertIn("portfolio_snapshot_repairs", health_payload["execution_summary"])
        self.assertEqual(mode_payload["config_profile"], "local_demo")
        self.assertEqual(mode_payload["market_data_backend"], "demo")
        self.assertEqual(mode_payload["execution_backend"], "paper")
        self.assertEqual(mode_payload["ai_operating_mode"], "baseline_only")
        self.assertEqual(mode_payload["runtime_profile"]["name"], "paper_local")
        self.assertEqual(mode_payload["environment_capabilities"]["execution_adapter_kind"], "paper")
        self.assertFalse(mode_payload["policy_profile"]["exchange_submission_allowed_in_principle"])
        self.assertFalse(mode_payload["recovery_policy"]["operator_rebaseline_supported"])
        self.assertFalse(mode_payload["execution_blocked"])
        self.assertTrue(mode_payload["submit_blocked"])
        self.assertIn("local_demo_no_exchange_submission", mode_payload["submit_blocked_reasons"])
        self.assertIn("paper_execution_has_no_exchange_submission", mode_payload["submit_blocked_reasons"])
        self.assertIsNone(mode_payload["blocked_reason"])
        self.assertEqual(runtime_payload["symbols"], ["BTC-USDT"])
        self.assertEqual(runtime_payload["enabled_timeframes"], ["15m"])
        self.assertGreaterEqual(runtime_payload["uptime_seconds"], 0.0)
        self.assertEqual(runtime_payload["runtime_profile"]["name"], "paper_local")
        self.assertEqual(runtime_payload["environment_capabilities"]["execution_route"], "paper_local")
        self.assertIn("baseline_takeover", runtime_payload)
        self.assertIn("decision_cycle_count", metrics_payload)
        self.assertIn("recent_execution_errors", metrics_payload)
        self.assertIn("exposure_summary", metrics_payload)
        self.assertIn("portfolio_snapshot_repair_count", metrics_payload)
        self.assertIn("strategy_execution_health", metrics_payload)
        self.assertIn("recent_churn_ratio", metrics_payload["strategy_execution_health"])
        self.assertIsInstance(blockers_payload["blockers"], list)
        self.assertTrue(any(item["submit_only"] for item in blockers_payload["blockers"]))
        self.assertIn("history", blocker_history_payload)
        self.assertIn("total_available", blocker_history_payload)
        self.assertIn("has_more", blocker_history_payload)
        self.assertIn(health_payload["runtime_state"], {"healthy", "degraded", "blocked", "halted"})

    async def test_operator_visibility_endpoints_cover_decision_execution_reconciliation_and_audit(self) -> None:
        runtime = await self._runtime()
        app = self._app(runtime)
        with TestClient(app) as client:
            latest_decision = client.get("/decision/latest").json()
            decision_id = latest_decision["decision_id"]
            recent_decisions = client.get("/decision/recent").json()
            decision_detail = client.get(f"/decision/{decision_id}").json()
            risk_latest = client.get("/risk/latest").json()
            risk_recent = client.get("/risk/recent?limit=2").json()
            policy_latest = client.get("/policy/latest").json()
            policy_recent = client.get("/policy/recent?limit=2").json()
            portfolio_latest = client.get("/portfolio/latest").json()
            portfolio_history = client.get("/portfolio/history?limit=5").json()
            balances = client.get("/balances").json()
            positions = client.get("/positions").json()
            orders_recent = client.get("/orders/recent").json()
            latest_order_id = orders_recent["orders"][0]["client_order_id"]
            order_detail = client.get(f"/orders/{latest_order_id}").json()
            fills_recent = client.get("/fills/recent").json()
            latest_fill_id = fills_recent["fills"][0]["fill_id"]
            fill_detail = client.get(f"/fills/{latest_fill_id}").json()
            execution_latest = client.get("/execution/latest").json()
            reconciliation_latest = client.get("/reconciliation/latest").json()
            reconciliation_recent = client.get("/reconciliation/recent").json()
            reconciliation_mismatches = client.get("/reconciliation/mismatches").json()
            latest_reconciliation_id = reconciliation_latest["reconciliation"]["reconciliation_id"]
            reconciliation_detail = client.get(f"/reconciliation/{latest_reconciliation_id}").json()
            audit_latest = client.get("/audit/latest").json()
            audit_detail = client.get(f"/audit/{decision_id}").json()
            replay_status_before = client.get("/replay/status").json()
            replay_validation = client.post(f"/replay/validate/{decision_id}").json()
            replay_status_after = client.get("/replay/status").json()
            replay_recent = client.get("/replay/recent-validations?limit=5").json()

        self.assertIsNotNone(decision_id)
        self.assertIn("strategy_execution_health", latest_decision)
        self.assertIn("strategy_execution_health", decision_detail)
        self.assertEqual(decision_detail["decision_id"], decision_id)
        self.assertTrue(recent_decisions["decisions"])
        self.assertIn("total_available", recent_decisions)
        self.assertIn("has_more", recent_decisions)
        self.assertEqual(risk_latest["decision_id"], decision_id)
        self.assertIn("total_available", risk_recent)
        self.assertIn("has_more", risk_recent)
        self.assertEqual(policy_latest["decision_id"], decision_id)
        self.assertIn("total_available", policy_recent)
        self.assertIn("has_more", policy_recent)
        self.assertIsNotNone(portfolio_latest["portfolio"])
        self.assertTrue(portfolio_history["snapshots"])
        self.assertIn("local_balances", balances)
        self.assertIn("local_positions", positions)
        self.assertTrue(orders_recent["orders"])
        self.assertIn("total_available", orders_recent)
        self.assertIn("has_more", orders_recent)
        self.assertEqual(order_detail["order"]["client_order_id"], latest_order_id)
        self.assertTrue(fills_recent["fills"])
        self.assertIn("total_available", fills_recent)
        self.assertIn("has_more", fills_recent)
        self.assertEqual(fill_detail["fill"]["fill_id"], latest_fill_id)
        self.assertIsNotNone(execution_latest["latest_order"])
        self.assertIsNotNone(reconciliation_latest["reconciliation"])
        self.assertIn("mismatch_categories", reconciliation_latest["mismatch_summary"])
        self.assertIn("exchange_bills_summary", reconciliation_latest)
        self.assertTrue(reconciliation_recent["reconciliations"])
        self.assertIn("total_available", reconciliation_recent)
        self.assertIn("has_more", reconciliation_recent)
        self.assertIn("exchange_bills_summary", reconciliation_recent)
        self.assertIsInstance(reconciliation_mismatches["mismatches"], list)
        self.assertEqual(
            reconciliation_detail["reconciliation"]["reconciliation_id"],
            latest_reconciliation_id,
        )
        self.assertIn("exchange_bills_summary", reconciliation_detail)
        self.assertIn("exchange_bills_explanations", reconciliation_detail)
        self.assertIsNotNone(audit_latest["audit"])
        self.assertEqual(audit_detail["audit"]["decision_id"], decision_id)
        self.assertIsNone(replay_status_before["last_validation"])
        self.assertEqual(replay_validation["decision_id"], decision_id)
        self.assertEqual(replay_validation["symbol"], runtime.settings.default_symbol)
        self.assertIsNotNone(replay_validation["regime"])
        self.assertIsNotNone(replay_validation["active_profile_id"])
        self.assertIn("chain_health_score", replay_validation)
        self.assertIn("execution_chain_issue_count", replay_validation)
        self.assertEqual(replay_validation["product_type"], runtime.settings.trading_product_type)
        self.assertTrue(replay_status_after["recent_validations"])
        self.assertTrue(replay_recent["validations"])
        self.assertIn("total_available", replay_recent)
        self.assertIn("has_more", replay_recent)

    async def test_ai_endpoints_expose_latest_assessment_and_shadow_decisions(self) -> None:
        runtime = await self._runtime(
            ai_operating_mode="ai_primary",
            ai_shadow_mode_enabled=True,
            ai_execution_suggestion_mode="shadow_translation",
            ai_provider="openai",
            openai_api_key="test-key",
            trading_product_type="derivatives",
            margin_mode="cross",
            strategy_short_bias_enabled=True,
            default_symbol="BTC-USDT-SWAP",
            allowed_symbols=("BTC-USDT-SWAP",),
        )
        runtime.ai_service.provider = FakeShadowProvider()
        runtime.ai_service._degraded = False
        runtime.ai_service._consecutive_failures = 0
        runtime.ai_service._consecutive_successes = 0
        await runtime.decision_engine.run_cycle(runtime.settings.default_symbol, runtime.settings.primary_timeframe)
        app = self._app(runtime)
        with TestClient(app) as client:
            overview = client.get("/ai/overview")
            performance_overview_before = client.get("/ai/performance/overview")
            latest = client.get("/ai/latest")
            recent = client.get("/ai/recent?limit=5")
            takeovers = client.get("/ai/takeovers/recent?limit=5")
            shadow_latest = client.get("/ai/shadow/latest")
            shadow_recent = client.get("/ai/shadow/recent?limit=5")
            runtime_status = client.get("/ai/runtime")
            evaluation = client.post("/ai/shadow/evaluate-now")
            evaluation_reused = client.post("/ai/shadow/evaluate-now")
            evaluations = client.get("/ai/shadow/evaluations?limit=5")
            performance_reports = client.get("/ai/performance/reports?limit=5")
            performance_overview = client.get("/ai/performance/overview")
            decision_id = client.get("/decision/latest").json()["decision_id"]
            decision_detail = client.get(f"/decision/{decision_id}")
            replay_validation = client.post(f"/replay/validate/{decision_id}")

        self.assertEqual(overview.status_code, 200)
        self.assertEqual(performance_overview_before.status_code, 200)
        self.assertEqual(performance_overview.status_code, 200)
        self.assertEqual(latest.status_code, 200)
        self.assertEqual(recent.status_code, 200)
        self.assertEqual(takeovers.status_code, 200)
        self.assertEqual(shadow_latest.status_code, 200)
        self.assertEqual(shadow_recent.status_code, 200)
        self.assertEqual(runtime_status.status_code, 200)
        self.assertEqual(evaluation.status_code, 200)
        self.assertEqual(evaluation_reused.status_code, 200)
        self.assertEqual(evaluations.status_code, 200)
        self.assertEqual(performance_reports.status_code, 200)
        self.assertEqual(decision_detail.status_code, 200)
        self.assertEqual(replay_validation.status_code, 200)

        self.assertTrue(overview.json()["runtime"]["provider_ready"])
        self.assertEqual(overview.json()["runtime"]["execution_suggestion_mode"], "shadow_translation")
        self.assertIn("takeover_summary", overview.json())
        self.assertIn("shadow_summary", overview.json())
        self.assertIn("performance_windows", overview.json())
        self.assertIn("downgrade_state", overview.json())
        self.assertIn("performance_view", overview.json())
        self.assertIn("latest_execution_suggestion", overview.json())
        self.assertIn("recent_reports", performance_overview.json())
        self.assertIn("replay_context", performance_overview.json())
        self.assertIsNone(performance_overview_before.json()["latest_report"])
        self.assertIsNotNone(latest.json()["assessment"])
        self.assertIsNotNone(latest.json()["execution_suggestion"])
        self.assertIsNotNone(shadow_latest.json()["shadow_decision"])
        self.assertIsNotNone(decision_detail.json()["ai_decision_brief"])
        self.assertIsNotNone(decision_detail.json()["ai_assessment"])
        self.assertIsNotNone(decision_detail.json()["ai_takeover_decision"])
        self.assertIsNotNone(decision_detail.json()["ai_decision_audit"])
        self.assertIsNotNone(decision_detail.json()["ai_economic_actionability"])
        self.assertIsNotNone(decision_detail.json()["ai_execution_suggestion"])
        self.assertTrue(decision_detail.json()["ai_shadow_decisions"])
        self.assertTrue(decision_detail.json()["ai_shadow_evaluations"])
        self.assertTrue(takeovers.json()["takeovers"])
        self.assertIn("direction_disagreement", takeovers.json()["takeovers"][0])
        self.assertTrue(takeovers.json()["takeovers"][0]["ai_execution_suggestion_present"])
        self.assertIn(
            shadow_latest.json()["shadow_decision"]["shadow_action_type"],
            {"same_as_baseline", "hold_instead", "entry_override", "exit_override", "reverse_override"},
        )
        self.assertEqual(
            decision_detail.json()["ai_execution_suggestion"]["latest_translation"]["status"],
            "shadow_translation",
        )
        self.assertIn(
            "required_total_edge_bps",
            decision_detail.json()["ai_economic_actionability"],
        )
        self.assertIn(
            "market_snapshot_fresh",
            decision_detail.json()["ai_decision_audit"],
        )
        self.assertFalse(
            decision_detail.json()["ai_execution_suggestion"]["latest_translation"]["applied_to_live_execution"]
        )
        self.assertTrue(recent.json()["assessments"])
        self.assertTrue(shadow_recent.json()["shadow_decisions"])
        self.assertEqual(evaluation.json()["status"], "evaluation_created")
        self.assertEqual(evaluation_reused.json()["status"], "evaluation_reused")
        self.assertTrue(evaluations.json()["evaluations"])
        self.assertTrue(performance_reports.json()["reports"])
        self.assertEqual(
            performance_reports.json()["reports"][0]["effective_operating_mode"],
            overview.json()["runtime"]["effective_operating_mode"],
        )
        self.assertEqual(
            performance_overview.json()["latest_report"]["report_id"],
            performance_reports.json()["reports"][0]["report_id"],
        )
        self.assertEqual(len(runtime.event_store.by_topic(topics.AI_SHADOW_EVALUATIONS)), 1)
        self.assertEqual(len(runtime.event_store.by_topic(topics.AI_PERFORMANCE_REPORTS)), 1)
        self.assertFalse(
            any("ai_" in issue for issue in replay_validation.json()["decision_chain_issues"]),
            replay_validation.json()["decision_chain_issues"],
        )
        self.assertFalse(
            any("ai_" in issue for issue in replay_validation.json()["audit_issues"]),
            replay_validation.json()["audit_issues"],
        )

    async def test_baseline_only_does_not_emit_ai_chain_events(self) -> None:
        runtime = await self._runtime(
            ai_operating_mode="baseline_only",
            ai_shadow_mode_enabled=True,
            ai_provider="openai",
            openai_api_key="test-key",
        )
        runtime.ai_service.provider = FakeShadowProvider()
        await runtime.decision_engine.run_cycle(runtime.settings.default_symbol, runtime.settings.primary_timeframe)
        app = self._app(runtime)
        with TestClient(app) as client:
            overview = client.get("/ai/overview")
            latest = client.get("/ai/latest")
            recent = client.get("/ai/recent?limit=5")
            takeovers = client.get("/ai/takeovers/recent?limit=5")
            shadow_latest = client.get("/ai/shadow/latest")
            shadow_recent = client.get("/ai/shadow/recent?limit=5")

        self.assertEqual(overview.status_code, 200)
        self.assertEqual(latest.status_code, 200)
        self.assertEqual(takeovers.status_code, 200)
        self.assertEqual(overview.json()["takeover_summary"]["attempted_count"], 0)
        self.assertEqual(overview.json()["shadow_summary"]["window_count"], 0)
        self.assertIsNone(latest.json()["brief"])
        self.assertIsNone(latest.json()["assessment"])
        self.assertIsNone(latest.json()["takeover"])
        self.assertEqual(recent.json()["assessments"], [])
        self.assertEqual(takeovers.json()["takeovers"], [])
        self.assertIsNone(shadow_latest.json()["shadow_decision"])
        self.assertEqual(shadow_recent.json()["shadow_decisions"], [])
        self.assertEqual(len(runtime.event_store.by_topic(topics.AI_DECISION_BRIEFS)), 0)
        self.assertEqual(len(runtime.event_store.by_topic(topics.AI_ASSESSMENTS)), 0)
        self.assertEqual(len(runtime.event_store.by_topic(topics.AI_SHADOW_DECISIONS)), 0)

    async def test_ai_endpoints_expose_bounded_live_translation_when_enabled(self) -> None:
        runtime = await self._runtime(
            ai_operating_mode="ai_primary",
            ai_shadow_mode_enabled=True,
            ai_execution_suggestion_mode="enabled_live",
            trading_product_type="derivatives",
            margin_mode="cross",
            strategy_short_bias_enabled=True,
            default_symbol="BTC-USDT-SWAP",
            allowed_symbols=("BTC-USDT-SWAP",),
        )
        runtime.ai_service.provider = FakeShadowProvider()
        runtime.ai_service._degraded = False
        runtime.ai_service._consecutive_failures = 0
        runtime.ai_service._consecutive_successes = 0
        runtime.ai_service._outcome_review_required = False
        runtime.ai_service._outcome_auto_downgraded = False
        await runtime.decision_engine.run_cycle(runtime.settings.default_symbol, runtime.settings.primary_timeframe)
        app = self._app(runtime)
        with TestClient(app) as client:
            latest = client.get("/ai/latest").json()
            detail = client.get(f"/decision/{client.get('/decision/latest').json()['decision_id']}").json()

        latest_translation = latest["execution_suggestion"]["latest_translation"]
        self.assertEqual(latest_translation["status"], "enabled")
        self.assertTrue(latest_translation["applied_to_live_execution"])
        self.assertEqual(latest["execution_suggestion"]["live_order_type"], "limit")
        self.assertEqual(latest["execution_suggestion"]["live_time_in_force"], "IOC")
        self.assertIsNotNone(latest["execution_suggestion"]["live_limit_price"])
        self.assertEqual(
            detail["ai_execution_suggestion"]["latest_translation"]["applied_live_fields"],
            ["execution_style", "order_type", "limit_price", "time_in_force"],
        )

    async def test_baseline_only_hides_historical_ai_events(self) -> None:
        runtime = await self._runtime(ai_operating_mode="baseline_only")
        brief = AIDecisionBrief(
            decision_id="decision_ai_hidden",
            symbol=runtime.settings.default_symbol,
            timeframe=runtime.settings.primary_timeframe,
            product_type=runtime.settings.trading_product_type,
            margin_mode=runtime.settings.margin_mode,
            last_price=100_000.0,
            regime_indicator="trend",
            regime_confidence=0.7,
            composite_alpha_score=0.3,
            momentum_score=0.01,
            volatility_state="medium",
            volatility_value=0.02,
            current_position_qty=0.0,
            current_exposure_side="flat",
            current_open_order_count=0,
            baseline_direction_bias="long",
            baseline_confidence=0.6,
            fee_bps=runtime.settings.paper_taker_fee_bps,
            max_slippage_tolerance_bps=float(runtime.settings.max_slippage_tolerance_bps),
            expected_slippage_proxy_bps=2.0,
            min_net_edge_bps=runtime.settings.strategy_min_net_edge_bps,
            safe_to_trade=True,
            review_required=False,
            halted=False,
            reconciliation_severity="CLEAN",
            reconciliation_halt_required=False,
            market_snapshot_fresh=True,
            account_snapshot_fresh=True,
            execution_condition="normal",
        )
        assessment = AIMarketAssessment(
            decision_id="decision_ai_hidden",
            symbol=runtime.settings.default_symbol,
            regime="trend",
            directional_edge=0.4,
            expected_volatility=0.08,
            confidence=0.8,
            uncertainty=0.2,
            expected_holding_horizon="15m",
            invalidation_conditions=["trend_break", "book_flip"],
            risk_tags=["provider_ok"],
            rationale_summary="historical_assessment",
            operating_mode="ai_primary",
            provider_name="fake",
            output_valid=True,
            fallback_used=False,
            degraded=False,
            calibrated_confidence=0.75,
            baseline_override_recommended=True,
            override_reason_codes=["ai_trend_override"],
            economically_actionable=True,
            estimated_edge_bps=40.0,
            estimated_cost_bps=12.0,
            estimated_net_edge_bps=28.0,
            source_mode="provider",
            execution_condition="normal",
            model_name="fake",
            model_version="1",
            prompt_version="1",
        )
        takeover = AITakeoverDecision(
            decision_id="decision_ai_hidden",
            symbol=runtime.settings.default_symbol,
            timeframe=runtime.settings.primary_timeframe,
            ai_takeover_allowed=True,
            ai_takeover_applied=True,
            baseline_direction="long",
            ai_direction="long",
            final_direction="long",
        )
        brief_event = build_envelope(
            topic=topics.AI_DECISION_BRIEFS,
            key=runtime.settings.default_symbol,
            payload_model=brief,
            source_component="test",
        )
        assessment_event = build_envelope(
            topic=topics.AI_ASSESSMENTS,
            key=runtime.settings.default_symbol,
            payload_model=assessment,
            source_component="test",
        )
        takeover_event = build_envelope(
            topic=topics.AI_TAKEOVER_DECISIONS,
            key=runtime.settings.default_symbol,
            payload_model=takeover,
            source_component="test",
        )
        runtime.event_store.append(brief_event)
        runtime.event_store.append(assessment_event)
        runtime.event_store.append(takeover_event)
        runtime.audit_repo.upsert(
            DecisionAuditRecord(
                decision_id="decision_ai_hidden",
                decision_context_ref=brief_event.event_id,
                ai_decision_brief_ref=brief_event.event_id,
                ai_market_assessment_ref=assessment_event.event_id,
                ai_takeover_decision_ref=takeover_event.event_id,
            )
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            latest = client.get("/ai/latest")
            recent = client.get("/ai/recent?limit=5")
            decision = client.get("/decision/decision_ai_hidden")

        self.assertEqual(latest.status_code, 200)
        self.assertIsNone(latest.json()["brief"])
        self.assertIsNone(latest.json()["assessment"])
        self.assertIsNone(latest.json()["takeover"])
        self.assertEqual(recent.json()["assessments"], [])
        self.assertIsNone(decision.json()["ai_decision_brief"])
        self.assertIsNone(decision.json()["ai_assessment"])
        self.assertIsNone(decision.json()["ai_takeover_decision"])

    async def test_recovery_blocks_when_ai_is_degraded_without_auto_downgrade(self) -> None:
        runtime = await self._runtime(
            ai_operating_mode="ai_primary",
            ai_auto_downgrade_enabled=False,
            ai_provider="openai",
            openai_api_key="test-key",
        )
        runtime.ai_service._degraded = True
        runtime.ai_service._degradation_reason = "ai_timeout"
        app = self._app(runtime)

        with TestClient(app) as client:
            recovery = client.get("/system/recovery")
            health = client.get("/system/health")

        self.assertEqual(recovery.status_code, 200)
        self.assertTrue(recovery.json()["recovery"]["review_required"])
        self.assertIn("ai_degraded_requires_manual_review", recovery.json()["recovery"]["resume_blocked_reasons"])
        blockers = [item["blocker"] for item in health.json()["blockers"]]
        self.assertIn("ai_degraded_requires_manual_review", blockers)

    async def test_recovery_does_not_block_when_ai_auto_downgrade_is_active(self) -> None:
        runtime = await self._runtime(
            ai_operating_mode="ai_primary",
            ai_auto_downgrade_enabled=True,
            ai_provider="openai",
            openai_api_key="test-key",
        )
        runtime.ai_service._degraded = True
        runtime.ai_service._degradation_reason = "ai_timeout"
        app = self._app(runtime)

        with TestClient(app) as client:
            recovery = client.get("/system/recovery")

        self.assertEqual(recovery.status_code, 200)
        self.assertFalse(recovery.json()["recovery"]["review_required"])
        self.assertNotIn("ai_degraded_requires_manual_review", recovery.json()["recovery"]["resume_blocked_reasons"])

    async def test_halt_resume_and_stale_market_blocker_are_visible(self) -> None:
        runtime = await self._runtime()
        app = self._app(runtime)
        with TestClient(app) as client:
            halted = client.post("/system/halt", json={"reason": "operator_test_halt"})
            health_after_halt = client.get("/system/health")
            resumed = client.post("/system/resume", json={"reason": "operator_test_resume"})

        self.assertEqual(halted.status_code, 200)
        self.assertEqual(health_after_halt.status_code, 200)
        self.assertEqual(resumed.status_code, 200)
        self.assertTrue(halted.json()["halted"])
        blockers = [item["blocker"] for item in health_after_halt.json()["blockers"]]
        self.assertIn("kill_switch_active", blockers)
        self.assertFalse(resumed.json()["halted"])

        latest_snapshot = runtime.market_gateway.latest_snapshot(runtime.settings.default_symbol)
        self.assertIsNotNone(latest_snapshot)
        runtime.market_gateway._latest_snapshots[runtime.settings.default_symbol] = latest_snapshot.model_copy(
            update={"snapshot_ts": utc_now() - timedelta(seconds=120)}
        )
        with TestClient(app) as client:
            stale_health = client.get("/system/health").json()
        stale_blockers = [item["blocker"] for item in stale_health["blockers"]]
        self.assertIn("market_data_stale", stale_blockers)

    async def test_manual_halt_marks_recovery_as_resume_blocked(self) -> None:
        runtime = await self._runtime()
        app = self._app(runtime)
        with TestClient(app) as client:
            halted = client.post("/system/halt", json={"reason": "operator_test_halt"})
            recovery = client.get("/system/recovery")

        self.assertEqual(halted.status_code, 200)
        self.assertEqual(recovery.status_code, 200)
        self.assertEqual(recovery.json()["recovery"]["recovery_state"], "resume_blocked")

    async def test_resume_ignores_kill_switch_as_the_only_blocker(self) -> None:
        runtime = await self._runtime()
        query = runtime  # keep runtime in scope for intent clarity
        app = self._app(query)
        with TestClient(app) as client:
            halted = client.post("/system/halt", json={"reason": "operator_test_halt"})
            resumed = client.post("/system/resume", json={"reason": "operator_test_resume"})

        self.assertEqual(halted.status_code, 200)
        self.assertEqual(resumed.status_code, 200)
        self.assertFalse(resumed.json()["halted"])
        self.assertIn(resumed.json()["status"], {"resumed", "already_resumed"})

    async def test_system_health_reports_reconciliation_staleness_consistently(self) -> None:
        runtime = await self._runtime()
        latest_report = runtime.reconciliation_repo.latest()
        self.assertIsNotNone(latest_report)
        runtime.reconciliation_repo.save_report(
            latest_report.model_copy(update={"as_of_ts": utc_now() - timedelta(seconds=601)})
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            health = client.get("/system/health").json()

        blockers = [item["blocker"] for item in health["blockers"]]
        self.assertIn("reconciliation_stale", blockers)
        self.assertFalse(health["subsystems"]["reconciliation"]["fresh"])
        self.assertFalse(health["subsystems"]["reconciliation"]["ready"])
        self.assertIn("reconciliation_stale", health["subsystems"]["reconciliation"]["blockers"])
        self.assertFalse(health["freshness"]["reconciliation_fresh"])

    async def test_operator_auth_enforces_read_write_split_and_reconciliation_validate(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_read_api_key="read-key",
            operator_write_api_key="write-key",
        )
        app = self._app(runtime)
        with TestClient(app) as client:
            unauthorized = client.get("/system/health")
            read_allowed = client.get("/system/health", headers={"X-AATS-API-Key": "read-key"})
            read_denied_write = client.post(
                "/system/halt",
                json={"reason": "should_fail"},
                headers={"X-AATS-API-Key": "read-key"},
            )
            write_allowed = client.post(
                "/system/halt",
                json={"reason": "authorized_halt"},
                headers={"X-AATS-API-Key": "write-key"},
            )
            reconciliation_validate = client.post(
                "/reconciliation/validate",
                json={"reason": "startup_check"},
                headers={"X-AATS-API-Key": "write-key"},
            )

        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(read_allowed.status_code, 200)
        self.assertEqual(read_denied_write.status_code, 403)
        self.assertEqual(write_allowed.status_code, 200)
        self.assertEqual(reconciliation_validate.status_code, 200)

    async def test_memory_storage_auth_surface_is_not_reported_as_database_backed(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
        )
        runtime.operator_repo.save_user(
            OperatorUserRecord(
                username="admin",
                password_hash=hash_password("secret"),
                role="admin",
            )
        )
        app = self._app(runtime)
        with TestClient(app) as client:
            providers = client.get("/auth/providers")
            login = client.post("/auth/login", json={"username": "admin", "password": "secret"})
            session = client.get("/auth/session")

        self.assertEqual(providers.status_code, 200)
        self.assertEqual(login.status_code, 200)
        self.assertEqual(session.status_code, 200)
        self.assertFalse(providers.json()["database_backed"])
        self.assertFalse(providers.json()["runtime_profile_control_enabled"])
        self.assertFalse(session.json()["database_backed"])

    async def test_session_login_enforces_viewer_and_operator_roles(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("viewer", "viewer-pass"), ("operator", "operator-pass"), ("admin", "admin-pass")],
        )
        app = self._app(runtime)
        with TestClient(app) as viewer_client:
            login = viewer_client.post("/auth/login", json={"username": "viewer", "password": "viewer-pass"})
            health = viewer_client.get("/system/health")
            halt_denied = viewer_client.post("/system/halt", json={"reason": "viewer_should_fail"})
            logout = viewer_client.post("/auth/logout")

        with TestClient(app) as operator_client:
            login_operator = operator_client.post("/auth/login", json={"username": "operator", "password": "operator-pass"})
            halt_denied_operator = operator_client.post("/system/halt", json={"reason": "operator_should_fail"})
            session = operator_client.get("/auth/session")

        with TestClient(app) as admin_client:
            login_admin = admin_client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            halt_allowed_admin = admin_client.post("/system/halt", json={"reason": "admin_should_work"})

        self.assertEqual(login.status_code, 200)
        self.assertEqual(health.status_code, 200)
        self.assertEqual(halt_denied.status_code, 403)
        self.assertEqual(logout.status_code, 200)
        self.assertEqual(login_operator.status_code, 200)
        self.assertEqual(halt_denied_operator.status_code, 403)
        self.assertEqual(halt_denied_operator.json()["detail"], "operator_admin_access_required")
        self.assertEqual(session.status_code, 200)
        self.assertTrue(session.json()["authenticated"])
        self.assertEqual(session.json()["role"], "operator")
        self.assertEqual(login_admin.status_code, 200)
        self.assertEqual(halt_allowed_admin.status_code, 200)

    async def test_session_login_rejects_invalid_credentials(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "correct-pass")],
        )
        app = self._app(runtime)
        with TestClient(app) as client:
            failed = client.post("/auth/login", json={"username": "admin", "password": "wrong-pass"})

        self.assertEqual(failed.status_code, 401)
        self.assertEqual(failed.json()["detail"], "operator_login_failed")

    async def test_disabled_database_operator_account_cannot_log_in(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "correct-pass")],
        )
        runtime.operator_repo.save_user(
            OperatorUserRecord(
                username="disabled-user",
                password_hash=hash_password("disabled-pass"),
                role="operator",
                enabled=False,
            )
        )
        app = self._app(runtime)
        with TestClient(app) as client:
            failed = client.post("/auth/login", json={"username": "disabled-user", "password": "disabled-pass"})

        self.assertEqual(failed.status_code, 401)
        self.assertEqual(failed.json()["detail"], "operator_login_failed")

    async def test_admin_can_manage_operator_users_and_audit_login_and_crud_actions(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            login = client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            users = client.get("/auth/users")
            created = client.post(
                "/auth/users",
                json={
                    "username": "viewer2",
                    "password": "viewer-pass",
                    "role": "viewer",
                    "enabled": True,
                },
            )
            updated = client.patch(
                "/auth/users/viewer2",
                json={"role": "operator", "enabled": False},
            )
            deleted = client.delete("/auth/users/viewer2")

        self.assertEqual(login.status_code, 200)
        self.assertEqual(users.status_code, 200)
        self.assertEqual(created.status_code, 200)
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(deleted.status_code, 200)

        stored_admin = runtime.operator_repo.get_by_username("admin")
        self.assertIsNotNone(stored_admin)
        self.assertIsNotNone(stored_admin.last_login_at)

        actions = [item.payload for item in runtime.event_store.by_topic(topics.OPERATOR_ACTIONS)]
        login_action = next(item for item in reversed(actions) if item["action"] == "login")
        create_action = next(item for item in reversed(actions) if item["action"] == "user_create")
        update_action = next(item for item in reversed(actions) if item["action"] == "user_update")
        delete_action = next(item for item in reversed(actions) if item["action"] == "user_delete")

        self.assertEqual(login_action["actor_identity"], "admin")
        self.assertEqual(login_action["actor_role"], "admin")
        self.assertEqual(create_action["details"]["target_username"], "viewer2")
        self.assertEqual(update_action["details"]["target_username"], "viewer2")
        self.assertEqual(delete_action["details"]["target_username"], "viewer2")

    async def test_runtime_profile_routes_report_env_switch_mode(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            login = client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            listing = client.get("/runtime-profiles")
            created = client.post("/runtime-profiles/drafts", json={"profile_label": "derivatives primary"})

        self.assertEqual(login.status_code, 200)
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json()["profile_source"], "env_fallback")
        self.assertFalse(listing.json()["management_enabled"])
        self.assertEqual(created.status_code, 409)
        self.assertEqual(created.json()["detail"], "runtime_profile_control_disabled")

    async def test_runtime_profile_stage_routes_are_disabled_in_env_switch_mode(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
        )
        runtime.execution_repo.save_order_state(
            OrderState(
                decision_id="decision_runtime_profile",
                intent_id="intent_runtime_profile",
                symbol="BTC-USDT",
                client_order_id="order_runtime_profile",
                status="SUBMITTED",
                requested_qty=0.001,
                remaining_qty=0.001,
            )
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            canceled = client.post("/runtime-profiles/pending/cancel")
            restart = client.post("/runtime-profiles/restart")

        self.assertEqual(canceled.status_code, 409)
        self.assertEqual(canceled.json()["detail"], "runtime_profile_control_disabled")
        self.assertEqual(restart.status_code, 409)
        self.assertEqual(restart.json()["detail"], "runtime_profile_control_disabled")

    async def test_strategy_profile_routes_seed_snapshot_and_generate_recommendation(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            login = client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            evaluated = client.post("/strategy-profiles/auto-tuning/evaluate-now")
            snapshot = client.get("/strategy-profiles")
            optimization_reports = client.get("/strategy-profiles/optimization/reports?limit=5&offset=0")
            selection_decisions = client.get("/strategy-profiles/selection-decisions?limit=5&offset=0")
            auto_rollback_policy = client.get("/strategy-profiles/auto-rollback-policy")
            activation_policy = client.get("/strategy-profiles/activation-policy")
            recommendations = client.get("/strategy-profiles/recommendations?limit=5&offset=0")

        self.assertEqual(login.status_code, 200)
        self.assertEqual(snapshot.status_code, 200)
        self.assertEqual(evaluated.status_code, 200)
        self.assertEqual(optimization_reports.status_code, 200)
        self.assertEqual(selection_decisions.status_code, 200)
        self.assertEqual(auto_rollback_policy.status_code, 200)
        self.assertEqual(activation_policy.status_code, 200)
        self.assertEqual(recommendations.status_code, 200)
        snapshot_payload = snapshot.json()
        self.assertEqual(snapshot_payload["activation"]["active_profile_id"], "range_defensive")
        self.assertTrue(snapshot_payload["revisions"])
        self.assertIn("safety_state", snapshot_payload)
        self.assertIn("evaluations", snapshot_payload)
        self.assertIn("profile_space", snapshot_payload)
        self.assertIn("comparison_report", snapshot_payload)
        self.assertIn("latest_optimization_report", snapshot_payload)
        self.assertIn("latest_selection_decision", snapshot_payload)
        self.assertIn("auto_rollback_policy", snapshot_payload)
        self.assertIn("activation_policy", snapshot_payload)
        self.assertIn("execution_parameter_suggestion_capability", snapshot_payload)
        self.assertEqual(snapshot_payload["profile_space"]["selection_mode"], "registered_profile_only")
        self.assertFalse(snapshot_payload["profile_space"]["free_form_parameter_generation_enabled"])
        self.assertFalse(snapshot_payload["execution_parameter_suggestion_capability"]["enabled"])
        self.assertTrue(snapshot_payload["comparison_report"]["rows"])
        evaluated_payload = evaluated.json()
        recommendation_payload = evaluated_payload["recommendation"]
        self.assertIn("safety_state", evaluated_payload)
        self.assertIn("current_evaluation", evaluated_payload)
        self.assertIn("evaluation_pipeline", evaluated_payload)
        self.assertIn("comparison_report", evaluated_payload)
        self.assertIn("optimization_report", evaluated_payload)
        self.assertIn("selection_decision", evaluated_payload)
        self.assertIn("bucket_scores", evaluated_payload["optimization_report"]["replay_summary"])
        self.assertIn("cross_bucket_scores", evaluated_payload["optimization_report"]["replay_summary"])
        self.assertIn("current_cross_bucket", evaluated_payload["optimization_report"]["replay_summary"])
        self.assertIn("offline_replay_pipeline", evaluated_payload["optimization_report"])
        self.assertIn("winner_selection_policy", evaluated_payload["optimization_report"])
        self.assertIn("version_experiments", evaluated_payload["optimization_report"])
        self.assertIn("window_reports", evaluated_payload["optimization_report"]["offline_replay_pipeline"])
        self.assertGreaterEqual(
            len(evaluated_payload["evaluation_pipeline"]),
            len(snapshot_payload["revisions"]),
        )
        self.assertTrue(
            any(item["summary"]["evaluation_mode"] == "heuristic_projection_v1" for item in evaluated_payload["evaluation_pipeline"])
        )
        self.assertIn(
            recommendation_payload["recommended_profile_id"],
            {"trend_normal", "trend_strict", "range_defensive", "high_volatility_defensive", "execution_degraded_safe"},
        )
        self.assertTrue(evaluated_payload["validation"]["auto_apply_allowed"])
        self.assertEqual(evaluated_payload["auto_activation"]["status"], "auto_applied")
        self.assertEqual(recommendations.json()["total_available"], 1)
        self.assertFalse(recommendations.json()["has_more"])
        self.assertTrue(optimization_reports.json()["reports"])
        self.assertTrue(selection_decisions.json()["decisions"])
        self.assertEqual(
            optimization_reports.json()["reports"][0]["recommended_profile_id"],
            evaluated_payload["optimization_report"]["recommended_profile_id"],
        )
        self.assertEqual(
            optimization_reports.json()["reports"][0]["winner_selection_policy"]["winner_profile_id"],
            evaluated_payload["optimization_report"]["winner_selection_policy"]["winner_profile_id"],
        )
        self.assertEqual(
            selection_decisions.json()["decisions"][0]["candidate_profile_id"],
            evaluated_payload["selection_decision"]["candidate_profile_id"],
        )
        stored = runtime.event_store.by_topic(topics.STRATEGY_PROFILE_RECOMMENDATIONS)
        self.assertEqual(len(stored), 1)
        comparison_reports = runtime.event_store.by_topic(topics.STRATEGY_PROFILE_COMPARISON_REPORTS)
        self.assertEqual(len(comparison_reports), 1)
        optimization_report_events = runtime.event_store.by_topic(topics.STRATEGY_PROFILE_OPTIMIZATION_REPORTS)
        self.assertEqual(len(optimization_report_events), 1)
        selection_decision_events = runtime.event_store.by_topic(topics.STRATEGY_PROFILE_SELECTION_DECISIONS)
        self.assertGreaterEqual(len(selection_decision_events), 1)
        activations = runtime.event_store.by_topic(topics.STRATEGY_PROFILE_ACTIVATIONS)
        self.assertEqual(len(activations), 1)

    async def test_strategy_profile_provider_output_is_limited_to_registered_profiles(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
            ai_provider="openai",
            openai_api_key="test-key",
        )
        app = self._app(runtime)

        async def _bad_profile_response(*args, **kwargs):
            _ = args
            _ = kwargs
            return AIProviderResponse(
                provider_name="test_provider",
                request_id="req_bad_profile",
                latency_ms=12.0,
                payload={
                    "recommended_profile_id": "unregistered_profile",
                    "fallback_profile_id": "trend_normal",
                    "confidence": 0.95,
                    "market_regime_assessment": {
                        "regime": "trend",
                        "volatility_state": "medium",
                        "execution_condition": "normal",
                    },
                    "reason_codes": ["bad_profile_test"],
                    "human_summary": "provider proposed an unknown profile",
                    "risk_notes": ["should_fallback"],
                    "valid_for_minutes": 60,
                },
            )

        with patch(
            "aats.services.ai_service.openai_provider.OpenAIProvider.generate_assessment",
            new=AsyncMock(side_effect=_bad_profile_response),
        ):
            with TestClient(app) as client:
                client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
                evaluated = client.post("/strategy-profiles/auto-tuning/evaluate-now")

        self.assertEqual(evaluated.status_code, 200)
        recommendation = evaluated.json()["recommendation"]
        self.assertIn(
            recommendation["recommended_profile_id"],
            {"trend_normal", "trend_strict", "range_defensive", "high_volatility_defensive", "execution_degraded_safe"},
        )
        self.assertNotEqual(recommendation["recommended_profile_id"], "unregistered_profile")
        self.assertEqual(recommendation["generated_by"], "rule_fallback")

    async def test_strategy_profile_optimization_and_selection_reports_are_versioned(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            first = client.post("/strategy-profiles/auto-tuning/evaluate-now").json()
            second = client.post("/strategy-profiles/auto-tuning/evaluate-now").json()

        first_report = first["optimization_report"]
        second_report = second["optimization_report"]
        first_decision = first["selection_decision"]
        second_decision = second["selection_decision"]

        self.assertEqual(first_report["version"], 1)
        self.assertEqual(second_report["version"], 2)
        self.assertEqual(second_report["parent_report_id"], first_report["report_id"])
        self.assertIn(first_decision["decision_status"], {"auto_activation_executed", "stable_keep_active", "recommended_not_executed", "auto_rollback_recommended", "execution_outcome_recorded"})
        self.assertIn(second_decision["decision_status"], {"auto_activation_executed", "stable_keep_active", "recommended_not_executed", "execution_outcome_recorded", "auto_rollback_recommended"})
        self.assertIn("execution_state", second_decision)
        self.assertGreater(second_decision["version"], first_decision["version"])
        self.assertIsNotNone(second_decision["parent_decision_id"])

    async def test_strategy_profile_selection_outcome_and_auto_rollback_are_written_back(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
        )
        recommendation = StrategyProfileRecommendation(
            product_type=runtime.settings.trading_product_type,
            margin_mode=runtime.settings.margin_mode,
            allowed_symbols=runtime.settings.allowed_symbols,
            active_profile_id="trend_normal",
            recommended_profile_id="range_defensive",
            confidence=0.85,
            market_regime_assessment=StrategyProfileMarketRegimeAssessment(
                regime="range",
                volatility_state="medium",
                execution_condition="normal",
            ),
            reason_codes=["manual_activation_for_outcome_writeback"],
            human_summary="activate defensive profile",
            risk_notes=[],
            valid_for_minutes=120,
            generated_by="test",
            input_digest="digest_outcome",
            input_snapshot={"source": "test"},
            expires_at=utc_now() + timedelta(minutes=120),
        )
        runtime.strategy_profile_repo.save_recommendation(recommendation)
        for index in range(3):
            runtime.execution_repo.save_fill(
                FillEvent(
                    fill_id=f"fill_outcome_{index}",
                    decision_id=f"decision_outcome_{index}",
                    intent_id=f"intent_outcome_{index}",
                    client_order_id=f"clord_outcome_{index}",
                    exchange_order_id=f"ex_outcome_{index}",
                    symbol=runtime.settings.default_symbol,
                    venue="PAPER",
                    side="buy",
                    fill_qty=0.001,
                    fill_price=100_000.0,
                    fee_amount=1.0,
                    fee_currency="USDT",
                    liquidity_role="taker",
                    exchange_timestamp=utc_now(),
                    ingestion_timestamp=utc_now(),
                    order_status_after_fill="FILLED",
                )
            )
        runtime.recovery_status = runtime.recovery_status.model_copy(
            update={
                "safe_to_trade": False,
                "review_required": True,
            }
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            activated = client.post(
                f"/strategy-profiles/recommendations/{recommendation.recommendation_id}/accept",
                json={"reason": "activate_now", "activation_mode": "manual_now"},
            )
            evaluated = client.post("/strategy-profiles/auto-tuning/evaluate-now")

        self.assertEqual(activated.status_code, 200)
        self.assertEqual(evaluated.status_code, 200)
        selection = evaluated.json()["selection_decision"]
        self.assertEqual(selection["decision_status"], "auto_rollback_recommended")
        self.assertEqual(selection["execution_state"], "executed")
        self.assertEqual(selection["execution_outcome"]["evaluation_status"], "degraded")
        self.assertTrue(selection["auto_rollback_recommendation"]["recommended"])
        self.assertEqual(selection["auto_rollback_recommendation"]["target_profile_id"], "trend_normal")

    async def test_strategy_profile_auto_rollback_policy_can_execute_rollback(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
            strategy_profile_auto_rollback_enabled=True,
            strategy_profile_auto_rollback_review_required_only=False,
            strategy_profile_auto_rollback_min_trade_count=1,
            strategy_profile_auto_rollback_cooldown_seconds=0.0,
        )
        recommendation = StrategyProfileRecommendation(
            product_type=runtime.settings.trading_product_type,
            margin_mode=runtime.settings.margin_mode,
            allowed_symbols=runtime.settings.allowed_symbols,
            active_profile_id="trend_normal",
            recommended_profile_id="range_defensive",
            confidence=0.85,
            market_regime_assessment=StrategyProfileMarketRegimeAssessment(
                regime="range",
                volatility_state="medium",
                execution_condition="normal",
            ),
            reason_codes=["manual_activation_for_auto_rollback"],
            human_summary="activate defensive profile",
            risk_notes=[],
            valid_for_minutes=120,
            generated_by="test",
            input_digest="digest_auto_rollback",
            input_snapshot={"source": "test"},
            expires_at=utc_now() + timedelta(minutes=120),
        )
        runtime.strategy_profile_repo.save_recommendation(recommendation)
        runtime.execution_repo.save_fill(
            FillEvent(
                fill_id="fill_auto_rollback",
                decision_id="decision_auto_rollback",
                intent_id="intent_auto_rollback",
                client_order_id="clord_auto_rollback",
                exchange_order_id="ex_auto_rollback",
                symbol=runtime.settings.default_symbol,
                venue="PAPER",
                side="buy",
                fill_qty=0.001,
                fill_price=100_000.0,
                fee_amount=1.0,
                fee_currency="USDT",
                liquidity_role="taker",
                exchange_timestamp=utc_now(),
                ingestion_timestamp=utc_now(),
                order_status_after_fill="FILLED",
            )
        )
        runtime.recovery_status = runtime.recovery_status.model_copy(
            update={"safe_to_trade": False, "review_required": True}
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            activated = client.post(
                f"/strategy-profiles/recommendations/{recommendation.recommendation_id}/accept",
                json={"reason": "activate_now", "activation_mode": "manual_now"},
            )
            evaluated = client.post("/strategy-profiles/auto-tuning/evaluate-now")

        self.assertEqual(activated.status_code, 200)
        self.assertEqual(evaluated.status_code, 200)
        self.assertEqual(evaluated.json()["auto_rollback"]["status"], "auto_rollback_executed")
        latest_selection = evaluated.json()["selection_decision"]
        self.assertEqual(latest_selection["decision_status"], "auto_rollback_executed")
        self.assertEqual(latest_selection["execution_state"], "rolled_back")
        self.assertTrue(latest_selection["auto_rollback_recommendation"]["executed"])
        self.assertEqual(runtime.strategy_profile_repo.activation_state(
            product_type=runtime.settings.trading_product_type,
            margin_mode=runtime.settings.margin_mode,
            allowed_symbols=runtime.settings.allowed_symbols,
        ).active_profile_id, "trend_normal")

    async def test_strategy_profile_optimization_uses_cross_bucket_replay_scoring(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
        )
        control = StrategyProfileControlService(runtime)
        regime = str((control._tuning_context().get("baseline") or {}).get("regime") or "uncertain")
        for validation_id, healthy in (("replay_a", True), ("replay_b", True), ("replay_c", False)):
            runtime.event_store.append(
                build_envelope(
                    topic=topics.REPLAY_VALIDATIONS,
                    key=runtime.settings.default_symbol,
                    payload_model=ReplayValidationSummary(
                        validation_id=validation_id,
                        validated_at=utc_now(),
                        decision_id=f"decision_{validation_id}",
                        symbol=runtime.settings.default_symbol,
                        regime=regime,
                        active_profile_id="trend_normal" if validation_id != "replay_c" else "trend_strict",
                        product_type=runtime.settings.trading_product_type,
                        margin_mode=runtime.settings.margin_mode,
                        allowed_symbols=runtime.settings.allowed_symbols,
                        replayed_event_count=10,
                        stored_snapshot_count=5,
                        divergence_count=0 if healthy else 2,
                        divergence_density=0.0 if healthy else 0.2,
                        chain_health_score=0.98 if healthy else 0.6,
                        healthy=healthy,
                    ),
                    source_component="test",
                )
            )
        app = self._app(runtime)

        with TestClient(app) as client:
            client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            evaluated = client.post("/strategy-profiles/auto-tuning/evaluate-now")

        self.assertEqual(evaluated.status_code, 200)
        replay_summary = evaluated.json()["optimization_report"]["replay_summary"]
        self.assertEqual(replay_summary["current_cross_bucket"]["count"], 2)
        self.assertEqual(
            evaluated.json()["optimization_report"]["offline_replay_pipeline"]["pipeline_version"],
            "offline_replay_pipeline_v2",
        )
        self.assertEqual(
            [item["window"] for item in evaluated.json()["optimization_report"]["offline_replay_pipeline"]["window_reports"]],
            [10, 20, 50],
        )
        self.assertTrue(
            any(
                item["symbol"] == runtime.settings.default_symbol
                and item["regime"] == regime
                and item["profile_id"] == "trend_normal"
                for item in replay_summary["cross_bucket_scores"]
            )
        )
        trend_normal = next(
            item for item in evaluated.json()["optimization_report"]["candidates"] if item["profile_id"] == "trend_normal"
        )
        self.assertIn("replay_symbol_regime_profile_bucket_available", trend_normal["reasons"])
        self.assertIn("offline_replay_breakdown", trend_normal)
        self.assertIn("experiments", trend_normal["offline_replay_breakdown"])
        self.assertIn("winner_selection_policy", evaluated.json()["optimization_report"])
        self.assertIn("auto_activation", evaluated.json()["optimization_report"]["winner_selection_policy"])

    async def test_strategy_profile_auto_rollback_policy_can_be_persisted_by_scope(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
            strategy_profile_auto_rollback_enabled=False,
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            updated = client.post(
                "/strategy-profiles/auto-rollback-policy",
                json={
                    "enabled": True,
                    "review_required_only": False,
                    "min_trade_count": 2,
                    "cooldown_seconds": 60.0,
                    "matrix_allowed_symbols": [runtime.settings.default_symbol],
                    "matrix_allowed_regimes": ["range"],
                    "matrix_allowed_profiles": ["range_defensive"],
                    "reason": "persist_policy",
                },
            )
            fetched = client.get("/strategy-profiles/auto-rollback-policy")
            snapshot = client.get("/strategy-profiles")

        self.assertEqual(updated.status_code, 200)
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json()["policy_status"], "settings_fallback")
        self.assertFalse(fetched.json()["enabled"])

        with TestClient(app) as client:
            client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            approved = client.post(
                "/strategy-profiles/auto-rollback-policy/approve",
                json={"reason": "approve_policy"},
            )
            history = client.get("/strategy-profiles/auto-rollback-policy/history?limit=10&offset=0")
            frozen = client.post(
                "/strategy-profiles/auto-rollback-policy/freeze",
                json={"frozen": True, "reason": "freeze_policy"},
            )
            unfrozen = client.post(
                "/strategy-profiles/auto-rollback-policy/freeze",
                json={"frozen": False, "reason": "unfreeze_policy"},
            )
            fetched_after = client.get("/strategy-profiles/auto-rollback-policy")
            snapshot_after = client.get("/strategy-profiles")

        self.assertEqual(approved.status_code, 200)
        self.assertEqual(frozen.status_code, 200)
        self.assertEqual(unfrozen.status_code, 200)
        self.assertTrue(fetched_after.json()["enabled"])
        self.assertEqual(fetched_after.json()["matrix_allowed_profiles"], ["range_defensive"])
        self.assertEqual(history.json()["history"][0]["policy_status"], "approved")
        self.assertTrue(snapshot_after.json()["auto_rollback_policy"]["enabled"])
        self.assertEqual(snapshot_after.json()["auto_rollback_policy"]["matrix_allowed_regimes"], ["range"])
        self.assertFalse(snapshot_after.json()["auto_rollback_policy"]["frozen"])

    async def test_strategy_profile_winner_selection_policy_exposes_blocked_auto_activation_thresholds(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
            strategy_profile_auto_activation_min_composite_score=9999.0,
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            evaluated = client.post("/strategy-profiles/auto-tuning/evaluate-now")

        self.assertEqual(evaluated.status_code, 200)
        self.assertIn(
            "winner_composite_score_below_threshold",
            evaluated.json()["optimization_report"]["winner_selection_policy"]["auto_activation"]["blocked_reasons"],
        )

    async def test_strategy_profile_activation_policy_can_be_persisted_by_scope(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
            strategy_profile_activation_policy_enabled=False,
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            updated = client.post(
                "/strategy-profiles/activation-policy",
                json={
                    "enabled": True,
                    "min_composite_score": 0.1,
                    "min_offline_replay_score": -0.5,
                    "min_recommendation_strength": 0.2,
                    "require_positive_replay_consensus": True,
                    "disallow_when_shadow_review_required": True,
                    "matrix_allowed_symbols": [runtime.settings.default_symbol],
                    "matrix_allowed_regimes": ["trend"],
                    "matrix_allowed_profiles": ["trend_normal"],
                    "reason": "persist_activation_policy",
                },
            )
            fetched = client.get("/strategy-profiles/activation-policy")
            snapshot = client.get("/strategy-profiles")

        self.assertEqual(updated.status_code, 200)
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json()["policy_status"], "settings_fallback")
        self.assertFalse(fetched.json()["enabled"])

        with TestClient(app) as client:
            client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            approved = client.post(
                "/strategy-profiles/activation-policy/approve",
                json={"reason": "approve_activation_policy"},
            )
            history = client.get("/strategy-profiles/activation-policy/history?limit=10&offset=0")
            frozen = client.post(
                "/strategy-profiles/activation-policy/freeze",
                json={"frozen": True, "reason": "freeze_activation_policy"},
            )
            unfrozen = client.post(
                "/strategy-profiles/activation-policy/freeze",
                json={"frozen": False, "reason": "unfreeze_activation_policy"},
            )
            fetched_after = client.get("/strategy-profiles/activation-policy")
            snapshot_after = client.get("/strategy-profiles")

        self.assertEqual(approved.status_code, 200)
        self.assertEqual(frozen.status_code, 200)
        self.assertEqual(unfrozen.status_code, 200)
        self.assertTrue(fetched_after.json()["enabled"])
        self.assertEqual(fetched_after.json()["matrix_allowed_profiles"], ["trend_normal"])
        self.assertEqual(history.json()["history"][0]["policy_status"], "approved")
        self.assertTrue(snapshot_after.json()["activation_policy"]["enabled"])
        self.assertEqual(snapshot_after.json()["activation_policy"]["matrix_allowed_regimes"], ["trend"])
        self.assertFalse(snapshot_after.json()["activation_policy"]["frozen"])

    async def test_strategy_profile_activation_policy_can_execute_winner_auto_activation_independently(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
            strategy_profile_activation_policy_enabled=True,
        )
        conservative = runtime.strategy_profile_repo.list_revisions(
            product_type=runtime.settings.trading_product_type,
            margin_mode=runtime.settings.margin_mode,
            profile_id="range_defensive",
        )[0]
        state = runtime.strategy_profile_repo.activation_state(
            product_type=runtime.settings.trading_product_type,
            margin_mode=runtime.settings.margin_mode,
            allowed_symbols=runtime.settings.allowed_symbols,
        )
        runtime.strategy_profile_repo.save_activation_state(
            state.model_copy(
                update={
                    "active_revision_id": conservative.revision_id,
                    "active_profile_id": conservative.profile_id,
                    "auto_switch_enabled": False,
                }
            )
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            client.post(
                "/strategy-profiles/activation-policy",
                json={
                    "enabled": True,
                    "min_composite_score": -999.0,
                    "min_offline_replay_score": -999.0,
                    "min_recommendation_strength": -999.0,
                    "require_positive_replay_consensus": False,
                    "disallow_when_shadow_review_required": False,
                    "matrix_allowed_symbols": [runtime.settings.default_symbol],
                    "matrix_allowed_regimes": ["trend", "breakout", "range", "high_volatility", "execution_degraded", "unknown"],
                    "matrix_allowed_profiles": ["trend_normal", "trend_strict", "range_defensive", "high_volatility_defensive", "execution_degraded_safe"],
                    "reason": "allow_independent_activation",
                },
            )
            client.post(
                "/strategy-profiles/activation-policy/approve",
                json={"reason": "approve_independent_activation"},
            )
            evaluated = client.post("/strategy-profiles/auto-tuning/evaluate-now")
            snapshot = client.get("/strategy-profiles")

        self.assertEqual(evaluated.status_code, 200)
        self.assertFalse(evaluated.json()["validation"]["auto_apply_allowed"])
        self.assertIn("profile_activation_policy", evaluated.json())
        self.assertEqual(
            evaluated.json()["profile_activation_policy"]["status"],
            "winner_policy_auto_activation_executed",
        )
        self.assertIn(
            evaluated.json()["selection_decision"]["decision_status"],
            {"winner_policy_auto_activation_executed", "execution_outcome_recorded"},
        )
        self.assertNotEqual(snapshot.json()["activation"]["active_profile_id"], "range_defensive")

    async def test_strategy_profile_auto_rollback_matrix_can_block_by_symbol(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
            strategy_profile_auto_rollback_enabled=True,
            strategy_profile_auto_rollback_review_required_only=False,
            strategy_profile_auto_rollback_min_trade_count=1,
            strategy_profile_auto_rollback_cooldown_seconds=0.0,
            strategy_profile_auto_rollback_allowed_symbols=("ETH-USDT",),
        )
        recommendation = StrategyProfileRecommendation(
            product_type=runtime.settings.trading_product_type,
            margin_mode=runtime.settings.margin_mode,
            allowed_symbols=runtime.settings.allowed_symbols,
            active_profile_id="trend_normal",
            recommended_profile_id="range_defensive",
            confidence=0.85,
            market_regime_assessment=StrategyProfileMarketRegimeAssessment(
                regime="range",
                volatility_state="medium",
                execution_condition="normal",
            ),
            reason_codes=["manual_activation_for_matrix_block"],
            human_summary="activate defensive profile",
            risk_notes=[],
            valid_for_minutes=120,
            generated_by="test",
            input_digest="digest_matrix_block",
            input_snapshot={"source": "test"},
            expires_at=utc_now() + timedelta(minutes=120),
        )
        runtime.strategy_profile_repo.save_recommendation(recommendation)
        runtime.execution_repo.save_fill(
            FillEvent(
                fill_id="fill_matrix_block",
                decision_id="decision_matrix_block",
                intent_id="intent_matrix_block",
                client_order_id="clord_matrix_block",
                exchange_order_id="ex_matrix_block",
                symbol=runtime.settings.default_symbol,
                venue="PAPER",
                side="buy",
                fill_qty=0.001,
                fill_price=100_000.0,
                fee_amount=1.0,
                fee_currency="USDT",
                liquidity_role="taker",
                exchange_timestamp=utc_now(),
                ingestion_timestamp=utc_now(),
                order_status_after_fill="FILLED",
            )
        )
        runtime.recovery_status = runtime.recovery_status.model_copy(
            update={"safe_to_trade": False, "review_required": True}
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            client.post(
                f"/strategy-profiles/recommendations/{recommendation.recommendation_id}/accept",
                json={"reason": "activate_now", "activation_mode": "manual_now"},
            )
            evaluated = client.post("/strategy-profiles/auto-tuning/evaluate-now")

        self.assertEqual(evaluated.status_code, 200)
        self.assertNotIn("auto_rollback", evaluated.json())
        self.assertEqual(evaluated.json()["selection_decision"]["decision_status"], "auto_rollback_recommended")

    async def test_strategy_profile_auto_apply_is_blocked_when_review_required(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
        )
        runtime.recovery_status = runtime.recovery_status.model_copy(
            update={
                "safe_to_trade": False,
                "review_required": True,
                "resume_blocked_reasons": ["operator_rebaseline_required"],
            }
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            evaluated = client.post("/strategy-profiles/auto-tuning/evaluate-now")
            snapshot = client.get("/strategy-profiles")
            evaluations = client.get("/strategy-profiles/evaluations?limit=5&offset=0")

        self.assertEqual(evaluated.status_code, 200)
        self.assertEqual(snapshot.status_code, 200)
        self.assertEqual(evaluations.status_code, 200)
        self.assertFalse(evaluated.json()["validation"]["auto_apply_allowed"])
        self.assertIn("strategy_profile_review_required", evaluated.json()["validation"]["blocked_reasons"])
        self.assertIn("strategy_profile_runtime_not_safe_to_trade", evaluated.json()["validation"]["blocked_reasons"])
        self.assertGreaterEqual(evaluations.json()["total_available"], 1)
        self.assertEqual(snapshot.json()["activation"]["active_profile_id"], "trend_normal")

    async def test_strategy_profile_auto_apply_allows_same_risk_target_when_confidence_high(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
        )
        app = self._app(runtime)
        recommendation = StrategyProfileRecommendation(
            product_type=runtime.settings.trading_product_type,
            margin_mode=runtime.settings.margin_mode,
            allowed_symbols=runtime.settings.allowed_symbols,
            active_profile_id="trend_normal",
            recommended_profile_id="trend_strict",
            confidence=0.92,
            market_regime_assessment=StrategyProfileMarketRegimeAssessment(
                regime="trend",
                volatility_state="medium",
                execution_condition="normal",
            ),
            reason_codes=["trend_signal_moderate"],
            human_summary="keep trend but tighten entries",
            risk_notes=["test_same_risk"],
            valid_for_minutes=120,
            generated_by="test",
            input_digest="digest_trend_strict",
            input_snapshot={"source": "test"},
            expires_at=utc_now() + timedelta(minutes=120),
        )

        with patch(
            "aats.services.operator.strategy_profiles.StrategyProfileControlService._generate_recommendation",
            new=AsyncMock(return_value=recommendation),
        ):
            with TestClient(app) as client:
                client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
                evaluated = client.post("/strategy-profiles/auto-tuning/evaluate-now")
                snapshot = client.get("/strategy-profiles")

        self.assertEqual(evaluated.status_code, 200)
        self.assertTrue(evaluated.json()["validation"]["auto_apply_allowed"])
        self.assertEqual(evaluated.json()["validation"]["transition_risk_direction"], "same_risk")
        self.assertEqual(evaluated.json()["auto_activation"]["status"], "auto_applied")
        self.assertEqual(
            evaluated.json()["auto_activation"]["activation_record"]["reason_code"],
            "ai_recommended_same_risk_profile",
        )
        self.assertEqual(snapshot.json()["activation"]["active_profile_id"], "trend_strict")

    async def test_strategy_profile_auto_apply_blocks_more_aggressive_target_when_confidence_too_low(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
        )
        conservative = runtime.strategy_profile_repo.list_revisions(
            product_type=runtime.settings.trading_product_type,
            margin_mode=runtime.settings.margin_mode,
            profile_id="range_defensive",
        )[0]
        state = runtime.strategy_profile_repo.activation_state(
            product_type=runtime.settings.trading_product_type,
            margin_mode=runtime.settings.margin_mode,
            allowed_symbols=runtime.settings.allowed_symbols,
        )
        runtime.strategy_profile_repo.save_activation_state(
            state.model_copy(
                update={
                    "active_revision_id": conservative.revision_id,
                    "active_profile_id": conservative.profile_id,
                    "previous_active_revision_id": state.active_revision_id,
                }
            )
        )
        app = self._app(runtime)
        recommendation = StrategyProfileRecommendation(
            product_type=runtime.settings.trading_product_type,
            margin_mode=runtime.settings.margin_mode,
            allowed_symbols=runtime.settings.allowed_symbols,
            active_profile_id="range_defensive",
            recommended_profile_id="trend_normal",
            confidence=0.86,
            market_regime_assessment=StrategyProfileMarketRegimeAssessment(
                regime="trend",
                volatility_state="medium",
                execution_condition="normal",
            ),
            reason_codes=["trend_recovery_detected"],
            human_summary="trend recovery supports normal profile",
            risk_notes=["test_more_aggressive"],
            valid_for_minutes=120,
            generated_by="test",
            input_digest="digest_trend_normal_low_conf",
            input_snapshot={"source": "test"},
            expires_at=utc_now() + timedelta(minutes=120),
        )

        with patch(
            "aats.services.operator.strategy_profiles.StrategyProfileControlService._generate_recommendation",
            new=AsyncMock(return_value=recommendation),
        ):
            with TestClient(app) as client:
                client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
                evaluated = client.post("/strategy-profiles/auto-tuning/evaluate-now")
                snapshot = client.get("/strategy-profiles")

        self.assertEqual(evaluated.status_code, 200)
        self.assertFalse(evaluated.json()["validation"]["auto_apply_allowed"])
        self.assertEqual(evaluated.json()["validation"]["transition_risk_direction"], "more_aggressive")
        self.assertIn(
            "strategy_profile_auto_switch_aggressive_confidence_too_low",
            evaluated.json()["validation"]["blocked_reasons"],
        )
        self.assertNotIn("auto_activation", evaluated.json())
        self.assertEqual(snapshot.json()["activation"]["active_profile_id"], "range_defensive")

    async def test_strategy_profile_auto_apply_allows_more_aggressive_target_when_confidence_high(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
        )
        conservative = runtime.strategy_profile_repo.list_revisions(
            product_type=runtime.settings.trading_product_type,
            margin_mode=runtime.settings.margin_mode,
            profile_id="range_defensive",
        )[0]
        state = runtime.strategy_profile_repo.activation_state(
            product_type=runtime.settings.trading_product_type,
            margin_mode=runtime.settings.margin_mode,
            allowed_symbols=runtime.settings.allowed_symbols,
        )
        runtime.strategy_profile_repo.save_activation_state(
            state.model_copy(
                update={
                    "active_revision_id": conservative.revision_id,
                    "active_profile_id": conservative.profile_id,
                    "previous_active_revision_id": state.active_revision_id,
                }
            )
        )
        app = self._app(runtime)
        recommendation = StrategyProfileRecommendation(
            product_type=runtime.settings.trading_product_type,
            margin_mode=runtime.settings.margin_mode,
            allowed_symbols=runtime.settings.allowed_symbols,
            active_profile_id="range_defensive",
            recommended_profile_id="trend_normal",
            confidence=0.93,
            market_regime_assessment=StrategyProfileMarketRegimeAssessment(
                regime="trend",
                volatility_state="medium",
                execution_condition="normal",
            ),
            reason_codes=["trend_recovery_detected", "fee_churn_normalized"],
            human_summary="trend regime recovered and supports normal posture",
            risk_notes=["test_more_aggressive_allowed"],
            valid_for_minutes=120,
            generated_by="test",
            input_digest="digest_trend_normal_high_conf",
            input_snapshot={"source": "test"},
            expires_at=utc_now() + timedelta(minutes=120),
        )

        with patch(
            "aats.services.operator.strategy_profiles.StrategyProfileControlService._generate_recommendation",
            new=AsyncMock(return_value=recommendation),
        ):
            with TestClient(app) as client:
                client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
                evaluated = client.post("/strategy-profiles/auto-tuning/evaluate-now")
                snapshot = client.get("/strategy-profiles")

        self.assertEqual(evaluated.status_code, 200)
        self.assertTrue(evaluated.json()["validation"]["auto_apply_allowed"])
        self.assertEqual(evaluated.json()["validation"]["transition_risk_direction"], "more_aggressive")
        self.assertEqual(evaluated.json()["auto_activation"]["status"], "auto_applied")
        self.assertEqual(
            evaluated.json()["auto_activation"]["activation_record"]["reason_code"],
            "ai_recommended_more_aggressive_profile",
        )
        self.assertEqual(snapshot.json()["activation"]["active_profile_id"], "trend_normal")

    async def test_strategy_profile_auto_apply_is_blocked_when_open_orders_exist(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
        )
        runtime.execution_repo.save_order_state(
            OrderState(
                decision_id="decision_strategy_profile_auto",
                intent_id="intent_strategy_profile_auto",
                symbol=runtime.settings.default_symbol,
                client_order_id="order_strategy_profile_auto",
                status="SUBMITTED",
                requested_qty=0.001,
                remaining_qty=0.001,
            )
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            evaluated = client.post("/strategy-profiles/auto-tuning/evaluate-now")
            snapshot = client.get("/strategy-profiles")

        self.assertEqual(evaluated.status_code, 200)
        self.assertFalse(evaluated.json()["validation"]["auto_apply_allowed"])
        self.assertIn("strategy_profile_open_orders_present", evaluated.json()["validation"]["blocked_reasons"])
        self.assertNotIn("auto_activation", evaluated.json())
        self.assertEqual(snapshot.json()["activation"]["active_profile_id"], "trend_normal")

    async def test_strategy_profile_accept_stage_activate_and_reject_are_audited(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
        )
        recommendation = StrategyProfileRecommendation(
            product_type=runtime.settings.trading_product_type,
            margin_mode=runtime.settings.margin_mode,
            allowed_symbols=runtime.settings.allowed_symbols,
            active_profile_id="trend_normal",
            recommended_profile_id="range_defensive",
            confidence=0.82,
            market_regime_assessment=StrategyProfileMarketRegimeAssessment(
                regime="range",
                volatility_state="medium",
                execution_condition="normal",
            ),
            reason_codes=["range_regime_detected"],
            human_summary="range market",
            risk_notes=["manual_test"],
            valid_for_minutes=120,
            generated_by="test",
            input_digest="digest",
            input_snapshot={"source": "test"},
            expires_at=utc_now() + timedelta(minutes=120),
        )
        runtime.strategy_profile_repo.save_recommendation(recommendation)
        app = self._app(runtime)

        with TestClient(app) as client:
            client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            staged = client.post(
                f"/strategy-profiles/recommendations/{recommendation.recommendation_id}/accept",
                json={"reason": "stage_now", "activation_mode": "stage_only"},
            )
            activated = client.post(
                "/strategy-profiles/pending/activate",
                json={"reason": "activate_pending"},
            )

        self.assertEqual(staged.status_code, 200)
        self.assertEqual(activated.status_code, 200)
        self.assertEqual(staged.json()["status"], "accepted_and_staged")
        self.assertEqual(activated.json()["active_revision"]["profile_id"], "range_defensive")
        selection_decisions = [item.payload for item in runtime.event_store.by_topic(topics.STRATEGY_PROFILE_SELECTION_DECISIONS)]
        self.assertTrue(any(item["decision_status"] == "staged_for_activation" for item in selection_decisions))
        self.assertTrue(any(item["decision_status"] == "pending_activation_executed" for item in selection_decisions))
        actions = [item.payload for item in runtime.event_store.by_topic(topics.OPERATOR_ACTIONS)]
        accept_action = next(item for item in reversed(actions) if item["action"] == "strategy_profile_accept")
        activate_action = next(item for item in reversed(actions) if item["action"] == "strategy_profile_activate_pending")
        self.assertEqual(accept_action["status"], "accepted_and_staged")
        self.assertEqual(activate_action["status"], "pending_profile_activated")

        second = recommendation.model_copy(
            update={
                "recommendation_id": "strp_rec_manual_reject",
                "recommended_profile_id": "trend_strict",
                "generated_at": utc_now(),
                "expires_at": utc_now() + timedelta(minutes=120),
            }
        )
        runtime.strategy_profile_repo.save_recommendation(second)

        with TestClient(app) as client:
            client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            rejected = client.post(
                f"/strategy-profiles/recommendations/{second.recommendation_id}/reject",
                json={"reason_code": "manual_reject", "reason_detail": "keep current profile"},
            )

        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(rejected.json()["status"], "rejected")
        rejections = runtime.event_store.by_topic(topics.STRATEGY_PROFILE_REJECTIONS)
        self.assertEqual(len(rejections), 1)
        selection_decisions = [item.payload for item in runtime.event_store.by_topic(topics.STRATEGY_PROFILE_SELECTION_DECISIONS)]
        self.assertTrue(any(item["decision_status"] == "recommendation_rejected" for item in selection_decisions))

    async def test_admin_can_manually_activate_registered_strategy_profile(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            activated = client.post(
                "/strategy-profiles/profiles/trend_strict/activate",
                json={"reason": "manual_switch_from_ui"},
            )
            snapshot = client.get("/strategy-profiles")

        self.assertEqual(activated.status_code, 200)
        self.assertEqual(activated.json()["status"], "manually_activated")
        self.assertEqual(activated.json()["active_revision"]["profile_id"], "trend_strict")
        self.assertEqual(snapshot.status_code, 200)
        self.assertEqual(snapshot.json()["activation"]["active_profile_id"], "trend_strict")
        selection_decisions = [item.payload for item in runtime.event_store.by_topic(topics.STRATEGY_PROFILE_SELECTION_DECISIONS)]
        self.assertTrue(any(item["decision_status"] == "manual_profile_activation_executed" for item in selection_decisions))
        actions = [item.payload for item in runtime.event_store.by_topic(topics.OPERATOR_ACTIONS)]
        activate_action = next(item for item in reversed(actions) if item["action"] == "strategy_profile_manual_activate")
        self.assertEqual(activate_action["status"], "profile_manually_activated")
        self.assertEqual(activate_action["details"]["requested_profile_id"], "trend_strict")
        self.assertEqual(activate_action["details"]["active_profile_id"], "trend_strict")

    async def test_strategy_profile_activation_is_blocked_when_open_orders_exist(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
        )
        runtime.execution_repo.save_order_state(
            OrderState(
                decision_id="decision_strategy_profile",
                intent_id="intent_strategy_profile",
                symbol=runtime.settings.default_symbol,
                client_order_id="order_strategy_profile",
                status="SUBMITTED",
                requested_qty=0.001,
                remaining_qty=0.001,
            )
        )
        recommendation = StrategyProfileRecommendation(
            product_type=runtime.settings.trading_product_type,
            margin_mode=runtime.settings.margin_mode,
            allowed_symbols=runtime.settings.allowed_symbols,
            active_profile_id="trend_normal",
            recommended_profile_id="range_defensive",
            confidence=0.82,
            market_regime_assessment=StrategyProfileMarketRegimeAssessment(
                regime="range",
                volatility_state="medium",
                execution_condition="normal",
            ),
            reason_codes=["range_regime_detected"],
            human_summary="range market",
            risk_notes=["manual_test"],
            valid_for_minutes=120,
            generated_by="test",
            input_digest="digest2",
            input_snapshot={"source": "test"},
            expires_at=utc_now() + timedelta(minutes=120),
        )
        runtime.strategy_profile_repo.save_recommendation(recommendation)
        app = self._app(runtime)

        with TestClient(app) as client:
            client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            blocked = client.post(
                f"/strategy-profiles/recommendations/{recommendation.recommendation_id}/accept",
                json={"reason": "activate_now", "activation_mode": "manual_now"},
            )

        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.json()["detail"], "strategy_profile_open_orders_present")

    async def test_operator_user_management_requires_admin_and_preserves_last_admin(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass"), ("operator", "operator-pass")],
            operator_write_api_key="write-key",
        )
        app = self._app(runtime)

        with TestClient(app) as operator_client:
            login = operator_client.post("/auth/login", json={"username": "operator", "password": "operator-pass"})
            denied = operator_client.get("/auth/users")

        with TestClient(app) as admin_client:
            admin_client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            self_disable = admin_client.patch("/auth/users/admin", json={"enabled": False})
            self_delete = admin_client.delete("/auth/users/admin")

        with TestClient(app) as api_key_client:
            write_allowed = api_key_client.get("/auth/users", headers={"X-AATS-API-Key": "write-key"})
            last_admin_disable = api_key_client.patch(
                "/auth/users/admin",
                json={"enabled": False},
                headers={"X-AATS-API-Key": "write-key"},
            )

        self.assertEqual(login.status_code, 200)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["detail"], "operator_admin_access_required")
        self.assertEqual(self_disable.status_code, 409)
        self.assertEqual(self_disable.json()["detail"], "operator_self_disable_forbidden")
        self.assertEqual(self_delete.status_code, 409)
        self.assertEqual(self_delete.json()["detail"], "operator_self_delete_forbidden")
        self.assertEqual(write_allowed.status_code, 200)
        self.assertEqual(last_admin_disable.status_code, 409)
        self.assertEqual(last_admin_disable.json()["detail"], "operator_last_admin_required")

    async def test_session_is_revoked_immediately_when_database_user_is_disabled(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass"), ("operator", "operator-pass")],
            operator_write_api_key="write-key",
        )
        operator_user = runtime.operator_repo.get_by_username("operator")
        self.assertIsNotNone(operator_user)
        runtime.operator_repo.save_user(operator_user.model_copy(update={"role": "admin"}))

        app = self._app(runtime)
        with TestClient(app) as session_client:
            login = session_client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            self.assertEqual(login.status_code, 200)
            before_disable = session_client.get("/auth/whoami")
            with TestClient(app) as api_key_client:
                disabled = api_key_client.patch(
                    "/auth/users/admin",
                    json={"enabled": False},
                    headers={"X-AATS-API-Key": "write-key"},
                )
            after_disable = session_client.get("/auth/whoami")

        self.assertEqual(before_disable.status_code, 200)
        self.assertEqual(disabled.status_code, 200)
        self.assertEqual(after_disable.status_code, 401)
        self.assertEqual(after_disable.json()["detail"], "operator_auth_required")

    async def test_operator_write_is_denied_without_auth_by_default(self) -> None:
        runtime = await self._runtime(operator_unsafe_write_without_auth=False)
        app = self._app(runtime)
        with TestClient(app) as client:
            denied = client.post("/system/halt", json={"reason": "unauthenticated_write"})

        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["detail"], "operator_write_auth_required")

    async def test_unauthenticated_session_remains_anonymous_when_browser_auth_is_disabled(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=False,
            operator_users=[("admin", "solo-pass")],
        )
        app = self._app(runtime)
        with TestClient(app) as client:
            session = client.get("/auth/session")

        self.assertEqual(session.status_code, 200)
        payload = session.json()
        self.assertFalse(payload["authenticated"])
        self.assertIsNone(payload["identity"])
        self.assertEqual(payload["role"], "anonymous")
        self.assertEqual(payload["auth_source"], "anonymous")

    async def test_auth_providers_no_longer_expose_runtime_bootstrap_state(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
        )
        app = self._app(runtime)
        with TestClient(app) as client:
            providers = client.get("/auth/providers")

        self.assertEqual(providers.status_code, 200)
        payload = providers.json()
        self.assertEqual(payload["configured_roles"], [])
        self.assertFalse(payload["runtime_profile_control_enabled"])
        self.assertNotIn("bootstrap_pending", payload)

    async def test_unauthenticated_session_is_anonymous_without_stored_operator_users(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=False,
        )
        app = self._app(runtime)
        with TestClient(app) as client:
            session = client.get("/auth/session")

        self.assertEqual(session.status_code, 200)
        payload = session.json()
        self.assertIsNone(payload["identity"])
        self.assertEqual(payload["role"], "anonymous")
        self.assertEqual(payload["auth_source"], "anonymous")

    async def test_sqlite_backed_operator_account_persists_and_allows_login(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = (Path(temp_dir) / "aats_auth.db").resolve().as_posix()
            seed_settings = AATSSettings.model_validate(
                {
                    "config_profile": "local_demo",
                    "mode": "paper_live",
                    "market_data_backend": "demo",
                    "execution_backend": "paper",
                    "account_backend": "disabled",
                    "account_read_enabled": False,
                    "storage_mode": "postgres",
                    "database_url": f"sqlite+pysqlite:///{database_path}",
                    "database_auto_create_schema": True,
                    "event_persistence_mode": "strict",
                    "enabled_decision_timeframes": ("15m",),
                    "operator_auth_enabled": False,
                }
            )
            runtime = await build_runtime(seed_settings)
            runtime.operator_repo.save_user(
                OperatorUserRecord(
                    username="admin",
                    password_hash=hash_password("correct-pass"),
                    role="admin",
                )
            )
            await runtime.market_gateway.run_local_publisher(
                symbol=seed_settings.default_symbol,
                iterations=2,
                interval_seconds=0.0,
            )
            self.assertEqual(runtime.operator_repo.count(), 1)
            if runtime.database_runtime is not None:
                runtime.database_runtime.dispose()

            settings = seed_settings.model_copy(
                update={
                    "operator_auth_enabled": True,
                    "operator_session_secret": "session-secret",
                }
            )
            recovered_runtime = await build_runtime(settings)
            app = self._app(recovered_runtime)
            with TestClient(app) as client:
                providers = client.get("/auth/providers")
                login = client.post("/auth/login", json={"username": "admin", "password": "correct-pass"})

            self.assertEqual(providers.status_code, 200)
            self.assertEqual(providers.json()["stored_user_count"], 1)
            self.assertFalse(providers.json()["runtime_profile_control_enabled"])
            self.assertEqual(login.status_code, 200)
            self.assertEqual(login.json()["identity"], "admin")
            if recovered_runtime.database_runtime is not None:
                recovered_runtime.database_runtime.dispose()

    async def test_database_backed_session_auth_requires_preexisting_admin_user(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = (Path(temp_dir) / "aats_auth.db").resolve().as_posix()
            settings = AATSSettings.model_validate(
                {
                    "config_profile": "local_demo",
                    "mode": "paper_live",
                    "market_data_backend": "demo",
                    "execution_backend": "paper",
                    "account_backend": "disabled",
                    "account_read_enabled": False,
                    "storage_mode": "postgres",
                    "database_url": f"sqlite+pysqlite:///{database_path}",
                    "database_auto_create_schema": True,
                    "event_persistence_mode": "strict",
                    "enabled_decision_timeframes": ("15m",),
                    "operator_auth_enabled": True,
                    "operator_session_secret": "session-secret",
                }
            )

            with self.assertRaisesRegex(ValueError, "operator_session_auth_requires_enabled_admin_user"):
                await build_runtime(settings)

    async def test_mode_hot_swap_is_rejected_and_cancel_is_operator_audited(self) -> None:
        runtime = await self._runtime(
            operator_users=[("admin", "solo-pass")],
        )
        app = self._app(runtime)
        with TestClient(app) as client:
            mode_change = client.post("/system/mode", json={"mode": "guarded_live"})
            latest_order_id = client.get("/orders/latest").json()["order"]["client_order_id"]
            cancel = client.post(
                f"/orders/{latest_order_id}/cancel",
                json={"reason": "ui_cancel_test"},
            )

        self.assertEqual(mode_change.status_code, 409)
        self.assertEqual(
            mode_change.json()["detail"],
            "runtime_mode_hot_swap_not_supported_restart_required",
        )
        self.assertEqual(cancel.status_code, 200)
        actions = [item.payload for item in runtime.event_store.by_topic(topics.OPERATOR_ACTIONS)]
        cancel_action = next(item for item in reversed(actions) if item["action"] == "cancel_order")
        self.assertIsNone(cancel_action["actor_identity"])
        self.assertEqual(cancel_action["actor_role"], "anonymous")
        self.assertEqual(cancel_action["auth_source"], "anonymous")
        self.assertEqual(cancel_action["reason"], "ui_cancel_test")
        self.assertEqual(cancel_action["order_id"], latest_order_id)

    async def test_stuck_submission_can_be_resolved_after_restart_when_exchange_confirms_absence(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "config_profile": "guarded_simulated_submit_dry_run",
                "mode": "guarded_live",
                "market_data_backend": "demo",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": True,
                "live_submit_enabled": False,
                "guarded_execution_dry_run": False,
                "bootstrap_portfolio_from_exchange": True,
                "storage_mode": "memory",
                "event_persistence_mode": "strict",
                "operator_unsafe_write_without_auth": True,
            }
        )
        FakeOperatorAccountService.SNAPSHOT = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            balances=[ExchangeBalance(currency="USDT", total=1000.0, available=1000.0, frozen=0.0)],
            positions=[],
            open_orders=[],
            fills=[],
            instruments=[],
            account_mode="cross",
        )
        with patch("aats.bootstrap.config.OKXAccountService", FakeOperatorAccountService):
            runtime = await build_runtime(settings)
        await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=2,
            interval_seconds=0.0,
        )
        stale_ts = utc_now() - timedelta(minutes=10)
        runtime.execution_repo.save_order_state(
            OrderState(
                decision_id="decision_restart_stuck",
                intent_id="intent_restart_stuck",
                symbol=settings.default_symbol,
                client_order_id="cl_restart_stuck",
                venue="OKX",
                exchange_order_id=None,
                status="SUBMITTING",
                submission_mode="local_order_manager",
                submitted_ts=None,
                last_update_ts=stale_ts,
                requested_qty=0.1,
                filled_qty=0.0,
                remaining_qty=0.1,
                product_type="spot",
                margin_mode="cash",
                submission_payload={},
            )
        )
        runtime.started_at = utc_now()
        app = self._app(runtime)

        with TestClient(app) as client:
            response = client.post(
                "/orders/cl_restart_stuck/resolve-stuck-submission",
                json={"reason": "ui_resolve_stuck_submission"},
            )
            detail = client.get("/orders/cl_restart_stuck")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["order"]["status"], "FAILED")
        self.assertEqual(detail.status_code, 200)
        self.assertFalse(detail.json()["stuck_submission_resolution"]["eligible"])
        actions = [item.payload for item in runtime.event_store.by_topic(topics.OPERATOR_ACTIONS)]
        resolution_action = next(item for item in reversed(actions) if item["action"] == "resolve_stuck_submission")
        self.assertEqual(resolution_action["reason"], "ui_resolve_stuck_submission")
        self.assertEqual(resolution_action["order_id"], "cl_restart_stuck")

    async def test_stuck_submission_resolution_updates_audit_record_when_decision_audit_exists(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "config_profile": "guarded_simulated_submit_dry_run",
                "mode": "guarded_live",
                "market_data_backend": "demo",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": True,
                "live_submit_enabled": False,
                "guarded_execution_dry_run": False,
                "bootstrap_portfolio_from_exchange": True,
                "storage_mode": "memory",
                "event_persistence_mode": "strict",
                "operator_unsafe_write_without_auth": True,
            }
        )
        FakeOperatorAccountService.SNAPSHOT = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            balances=[ExchangeBalance(currency="USDT", total=1000.0, available=1000.0, frozen=0.0)],
            positions=[],
            open_orders=[],
            fills=[],
            instruments=[],
            account_mode="cash",
        )
        with patch("aats.bootstrap.config.OKXAccountService", FakeOperatorAccountService):
            runtime = await build_runtime(settings)
        await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=2,
            interval_seconds=0.0,
        )
        stale_ts = utc_now() - timedelta(minutes=10)
        runtime.execution_repo.save_order_state(
            OrderState(
                decision_id="decision_restart_stuck_audit",
                intent_id="intent_restart_stuck_audit",
                symbol=settings.default_symbol,
                client_order_id="cl_restart_stuck_audit",
                venue="OKX",
                exchange_order_id=None,
                status="SUBMITTING",
                submission_mode="local_order_manager",
                submitted_ts=None,
                last_update_ts=stale_ts,
                requested_qty=0.1,
                filled_qty=0.0,
                remaining_qty=0.1,
                product_type="spot",
                margin_mode="cash",
                submission_payload={},
            )
        )
        runtime.audit_repo.upsert(
            DecisionAuditRecord(
                decision_id="decision_restart_stuck_audit",
                decision_context_ref="evt_decision_context_seed",
            )
        )
        runtime.started_at = utc_now()
        app = self._app(runtime)

        with TestClient(app) as client:
            response = client.post(
                "/orders/cl_restart_stuck_audit/resolve-stuck-submission",
                json={"reason": "ui_resolve_stuck_submission"},
            )

        self.assertEqual(response.status_code, 200)
        audit = runtime.audit_repo.get("decision_restart_stuck_audit")
        self.assertIsNotNone(audit)
        self.assertEqual(len(audit.order_state_refs), 1)
        order_update = runtime.event_store.get(audit.order_state_refs[0])
        self.assertIsNotNone(order_update)
        self.assertEqual(order_update.payload["status"], "FAILED")

    async def test_stuck_submission_resolution_is_rejected_when_exchange_still_has_order(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "config_profile": "guarded_simulated_submit_dry_run",
                "mode": "guarded_live",
                "market_data_backend": "demo",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": True,
                "live_submit_enabled": False,
                "guarded_execution_dry_run": False,
                "bootstrap_portfolio_from_exchange": True,
                "storage_mode": "memory",
                "event_persistence_mode": "strict",
                "operator_unsafe_write_without_auth": True,
            }
        )
        FakeOperatorAccountService.SNAPSHOT = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            balances=[ExchangeBalance(currency="USDT", total=1000.0, available=1000.0, frozen=0.0)],
            positions=[],
            open_orders=[
                ExchangeOpenOrder(
                    instrument_id="BTC-USDT",
                    client_order_id="cl_restart_live",
                    exchange_order_id="ord_live",
                    side="buy",
                    order_type="market",
                    status="live",
                    quantity=0.1,
                )
            ],
            fills=[],
            instruments=[],
            account_mode="cross",
        )
        with patch("aats.bootstrap.config.OKXAccountService", FakeOperatorAccountService):
            runtime = await build_runtime(settings)
        await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=2,
            interval_seconds=0.0,
        )
        runtime.execution_repo.save_order_state(
            OrderState(
                decision_id="decision_restart_live",
                intent_id="intent_restart_live",
                symbol=settings.default_symbol,
                client_order_id="cl_restart_live",
                venue="OKX",
                exchange_order_id=None,
                status="SUBMITTING",
                submission_mode="local_order_manager",
                submitted_ts=None,
                last_update_ts=utc_now() - timedelta(minutes=10),
                requested_qty=0.1,
                filled_qty=0.0,
                remaining_qty=0.1,
                product_type="spot",
                margin_mode="cash",
                submission_payload={},
            )
        )
        runtime.started_at = utc_now()
        app = self._app(runtime)

        with TestClient(app) as client:
            response = client.post(
                "/orders/cl_restart_live/resolve-stuck-submission",
                json={"reason": "ui_resolve_stuck_submission"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["detail"],
            "stuck_submission_resolution_blocked:exchange_order_still_open",
        )

    async def test_stuck_submission_resolution_is_rejected_when_private_ws_saw_order(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "config_profile": "guarded_simulated_submit_dry_run",
                "mode": "guarded_live",
                "market_data_backend": "demo",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": True,
                "live_submit_enabled": False,
                "guarded_execution_dry_run": False,
                "bootstrap_portfolio_from_exchange": True,
                "storage_mode": "memory",
                "event_persistence_mode": "strict",
                "operator_unsafe_write_without_auth": True,
            }
        )
        FakeOperatorAccountService.SNAPSHOT = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            balances=[ExchangeBalance(currency="USDT", total=1000.0, available=1000.0, frozen=0.0)],
            positions=[],
            open_orders=[],
            fills=[],
            instruments=[],
            account_mode="cross",
        )
        FakeOperatorAccountService.PRIVATE_ORDER_ROW = {
            "instId": settings.default_symbol,
            "ordId": "ord_ws_seen",
            "clOrdId": "cl_restart_ws_seen",
            "state": "live",
            "side": "buy",
            "ordType": "limit",
            "sz": "0.1",
            "accFillSz": "0",
            "uTime": "1700000002000",
            "cTime": "1700000001000",
        }
        FakeOperatorAccountService.PRIVATE_ORDER_FILLS = []
        with patch("aats.bootstrap.config.OKXAccountService", FakeOperatorAccountService):
            runtime = await build_runtime(settings)
        runtime.execution_repo.save_order_state(
            OrderState(
                decision_id="decision_restart_ws_seen",
                intent_id="intent_restart_ws_seen",
                symbol=settings.default_symbol,
                client_order_id="cl_restart_ws_seen",
                venue="OKX",
                exchange_order_id=None,
                status="SUBMITTING",
                submission_mode="local_order_manager",
                submitted_ts=None,
                last_update_ts=utc_now() - timedelta(minutes=10),
                requested_qty=0.1,
                filled_qty=0.0,
                remaining_qty=0.1,
                product_type="spot",
                margin_mode="cash",
                submission_payload={},
            )
        )
        runtime.started_at = utc_now()
        app = self._app(runtime)

        with TestClient(app) as client:
            response = client.post(
                "/orders/cl_restart_ws_seen/resolve-stuck-submission",
                json={"reason": "ui_resolve_stuck_submission"},
            )
            detail = client.get("/orders/cl_restart_ws_seen").json()

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["detail"],
            "stuck_submission_resolution_blocked:exchange_order_seen_via_private_ws",
        )
        self.assertTrue(detail["stuck_submission_resolution"]["private_ws_order_present"])
        FakeOperatorAccountService.PRIVATE_ORDER_ROW = None
        FakeOperatorAccountService.PRIVATE_ORDER_FILLS = None

    async def test_orders_recent_returns_latest_orders_first(self) -> None:
        runtime = await self._runtime()
        runtime.execution_repo.save_order_state(
            OrderState(
                decision_id="decision_old_recent",
                intent_id="intent_old_recent",
                symbol=runtime.settings.default_symbol,
                client_order_id="order_old_recent",
                venue="PAPER",
                status="FAILED",
                submission_mode="paper_local",
                submitted_ts=None,
                last_update_ts=utc_now() - timedelta(minutes=5),
                requested_qty=1.0,
                filled_qty=0.0,
                remaining_qty=1.0,
                product_type="spot",
                margin_mode="cash",
                submission_payload={},
            )
        )
        runtime.execution_repo.save_order_state(
            OrderState(
                decision_id="decision_new_recent",
                intent_id="intent_new_recent",
                symbol=runtime.settings.default_symbol,
                client_order_id="order_new_recent",
                venue="PAPER",
                status="SUBMITTING",
                submission_mode="paper_local",
                submitted_ts=None,
                last_update_ts=utc_now(),
                requested_qty=1.0,
                filled_qty=0.0,
                remaining_qty=1.0,
                product_type="spot",
                margin_mode="cash",
                submission_payload={},
            )
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            recent = client.get("/orders/recent?limit=1")
            full_list = client.get("/orders/recent?limit=200").json()
            order_ids = [item["client_order_id"] for item in full_list["orders"]]
            old_index = order_ids.index("order_old_recent")
            next_page = client.get(f"/orders/recent?limit=1&offset={old_index}")

        self.assertEqual(recent.status_code, 200)
        self.assertEqual(recent.json()["orders"][0]["client_order_id"], "order_new_recent")
        self.assertEqual(recent.json()["offset"], 0)
        self.assertEqual(recent.json()["limit"], 1)
        self.assertTrue(recent.json()["has_more"])
        self.assertLess(order_ids.index("order_new_recent"), order_ids.index("order_old_recent"))
        self.assertEqual(next_page.status_code, 200)
        self.assertEqual(next_page.json()["orders"][0]["client_order_id"], "order_old_recent")
        self.assertEqual(next_page.json()["offset"], old_index)

    async def test_execution_latest_uses_normalized_recovery_view(self) -> None:
        runtime = await self._runtime()
        runtime.kill_switch.halt(reason="operator_test_halt")
        runtime.recovery_status = runtime.recovery_status.model_copy(update={"recovery_state": "normal_operation"})
        app = self._app(runtime)

        with TestClient(app) as client:
            execution = client.get("/execution/latest")
            recovery = client.get("/system/recovery")

        self.assertEqual(execution.status_code, 200)
        self.assertEqual(recovery.status_code, 200)
        self.assertEqual(
            execution.json()["recovery"]["recovery_state"],
            recovery.json()["recovery"]["recovery_state"],
        )
        self.assertEqual(execution.json()["recovery"]["recovery_state"], "resume_blocked")

    async def test_execution_errors_hide_stale_failures_from_previous_runtime(self) -> None:
        runtime = await self._runtime()
        stale_error = ExecutionErrorSummary(
            subsystem="execution_engine",
            severity="error",
            message="stale_failure",
            decision_id="decision_old",
            order_id="order_old",
            status="FAILED",
            observed_at=utc_now() - timedelta(hours=2),
        )
        runtime.event_store.append(
            build_envelope(
                topic=topics.EXECUTION_ERROR_SUMMARIES,
                key=runtime.settings.default_symbol,
                payload_model=stale_error,
                source_component="execution_engine",
            )
        )
        runtime.started_at = utc_now()
        app = self._app(runtime)

        with TestClient(app) as client:
            execution_errors = client.get("/execution/errors")

        self.assertEqual(execution_errors.status_code, 200)
        self.assertEqual(execution_errors.json()["errors"], [])

    async def test_operator_histories_are_persisted_for_blockers_and_replay(self) -> None:
        runtime = await self._runtime()
        app = self._app(runtime)
        with TestClient(app) as client:
            client.get("/system/health")
            blockers = client.get("/system/blockers").json()
            decision_id = client.get("/decision/latest").json()["decision_id"]
            replay_validation = client.post(f"/replay/validate/{decision_id}").json()
            replay_recent = client.get("/replay/recent-validations").json()

        self.assertTrue(blockers["recent_history"])
        self.assertEqual(replay_validation["decision_id"], decision_id)
        self.assertTrue(replay_recent["validations"])

    async def test_system_recovery_and_rebaseline_endpoints_expose_operator_recovery_flow(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "config_profile": "guarded_simulated_submit_dry_run",
                "mode": "guarded_live",
                "market_data_backend": "demo",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": True,
                "live_submit_enabled": False,
                "guarded_execution_dry_run": True,
                "bootstrap_portfolio_from_exchange": True,
                "storage_mode": "memory",
                "event_persistence_mode": "strict",
                "operator_unsafe_write_without_auth": True,
            }
        )
        FakeOperatorAccountService.SNAPSHOT = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            balances=[ExchangeBalance(currency="USDT", total=1000.0, available=1000.0, frozen=0.0)],
            positions=[],
            open_orders=[],
            fills=[],
            instruments=[],
            account_mode="cash",
        )
        with patch("aats.bootstrap.config.OKXAccountService", FakeOperatorAccountService):
            runtime = await build_runtime(settings)
        await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=2,
            interval_seconds=0.0,
        )

        app = self._app(runtime)
        with TestClient(app) as client:
            recovery_before = client.get("/system/recovery").json()
            rebaseline = client.post("/system/rebaseline", json={"reason": "accept_exchange_state"}).json()
            recovery_after = client.get("/system/recovery").json()

        self.assertIn("recovery_state", recovery_before["recovery"])
        self.assertEqual(rebaseline["status"], "rebaseline_completed")
        self.assertEqual(recovery_after["recovery"]["recovery_state"], "rebaseline_completed")
        self.assertTrue(recovery_after["recovery"]["resume_eligible"])
        self.assertIsNotNone(recovery_after["recovery"]["last_rebaseline_action"])

    async def test_recovery_view_uses_latest_account_baseline_for_current_scope(self) -> None:
        runtime = await self._runtime()
        runtime.event_store.append(
            build_envelope(
                topic=topics.ACCOUNT_BASELINES,
                key="okx",
                payload_model=AccountBaselineSnapshot(
                    account_source="okx",
                    exchange_snapshot_ts=utc_now(),
                    imported_at=utc_now(),
                    product_type="spot",
                    margin_mode="cash",
                    allowed_symbols=["BTC-USDT"],
                    baseline_status="baseline_imported",
                ),
                source_component="test",
            )
        )
        runtime.event_store.append(
            build_envelope(
                topic=topics.ACCOUNT_BASELINES,
                key="okx",
                payload_model=AccountBaselineSnapshot(
                    account_source="okx",
                    exchange_snapshot_ts=utc_now(),
                    imported_at=utc_now(),
                    product_type="spot",
                    margin_mode="cash",
                    allowed_symbols=["ETH-USDT"],
                    baseline_status="baseline_imported",
                ),
                source_component="test",
            )
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            recovery = client.get("/system/recovery").json()

        self.assertEqual(recovery["latest_account_baseline"]["allowed_symbols"], ["BTC-USDT"])

    async def test_rebaseline_uses_previous_baseline_from_current_scope(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "config_profile": "guarded_simulated_submit_dry_run",
                "mode": "guarded_live",
                "market_data_backend": "demo",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": True,
                "live_submit_enabled": False,
                "guarded_execution_dry_run": True,
                "bootstrap_portfolio_from_exchange": True,
                "storage_mode": "memory",
                "event_persistence_mode": "strict",
                "operator_unsafe_write_without_auth": True,
            }
        )
        FakeOperatorAccountService.SNAPSHOT = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            balances=[ExchangeBalance(currency="USDT", total=1000.0, available=1000.0, frozen=0.0)],
            positions=[],
            open_orders=[],
            fills=[],
            instruments=[],
            account_mode="cash",
        )
        with patch("aats.bootstrap.config.OKXAccountService", FakeOperatorAccountService):
            runtime = await build_runtime(settings)

        initial_baseline = runtime.event_store.latest(topics.ACCOUNT_BASELINES)
        self.assertIsNotNone(initial_baseline)
        runtime.event_store.append(
            build_envelope(
                topic=topics.ACCOUNT_BASELINES,
                key="okx",
                payload_model=AccountBaselineSnapshot(
                    account_source="okx",
                    exchange_snapshot_ts=utc_now(),
                    imported_at=utc_now(),
                    product_type="spot",
                    margin_mode="cash",
                    allowed_symbols=["ETH-USDT"],
                    baseline_status="baseline_imported",
                ),
                source_component="test",
            )
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            response = client.post("/system/rebaseline", json={"reason": "accept_exchange_state"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["baseline"]["previous_baseline_ref"],
            initial_baseline.event_id,
        )

    async def test_system_rebaseline_is_rejected_for_paper_local_profile(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "config_profile": "local_demo",
                "mode": "paper_live",
                "market_data_backend": "demo",
                "execution_backend": "paper",
                "account_backend": "okx",
                "account_read_enabled": True,
                "bootstrap_portfolio_from_exchange": True,
                "storage_mode": "memory",
                "event_persistence_mode": "strict",
                "operator_unsafe_write_without_auth": True,
            }
        )
        FakeOperatorAccountService.SNAPSHOT = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            balances=[ExchangeBalance(currency="USDT", total=1000.0, available=1000.0, frozen=0.0)],
            positions=[],
            open_orders=[],
            fills=[],
            instruments=[],
            account_mode="cash",
        )
        with patch("aats.bootstrap.config.OKXAccountService", FakeOperatorAccountService):
            runtime = await build_runtime(settings)

        app = self._app(runtime)
        with TestClient(app) as client:
            response = client.post("/system/rebaseline", json={"reason": "paper_profile_rebaseline"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "rebaseline_not_supported_for_runtime_profile")

    async def _runtime(self, operator_users: list[tuple[str, str]] | None = None, **overrides):
        settings = AATSSettings.model_validate(
            {
                "config_profile": "local_demo",
                "mode": "paper_live",
                "market_data_backend": "demo",
                "execution_backend": "paper",
                "account_backend": "disabled",
                "account_read_enabled": False,
                "storage_mode": "memory",
                "event_persistence_mode": "strict",
                "enabled_decision_timeframes": ("15m",),
                "operator_unsafe_write_without_auth": True,
                **overrides,
            }
        )
        runtime = await build_runtime(settings)
        for username, password in operator_users or []:
            role = "admin" if username == "admin" else "operator" if username == "operator" else "viewer"
            runtime.operator_repo.save_user(
                OperatorUserRecord(
                    username=username,
                    password_hash=hash_password(password),
                    role=role,
                )
            )
        await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=4,
            interval_seconds=0.0,
        )
        return runtime

    @staticmethod
    def _app(runtime) -> FastAPI:
        app = FastAPI()
        app.include_router(auth_router)
        app.include_router(router)
        app.state.runtime = runtime
        return app


if __name__ == "__main__":
    unittest.main()
