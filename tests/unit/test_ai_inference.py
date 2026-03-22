from __future__ import annotations

import asyncio
import unittest
from datetime import timedelta
from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.ai_brief import AIDecisionBrief
from aats.schemas.ai_shadow import AIShadowDecision, AIShadowEvaluation
from aats.schemas.common import utc_now
from aats.schemas.decision import AIMarketAssessment, BaselineAssessment, DecisionContext
from aats.schemas.execution import FillEvent
from aats.schemas.features import FeatureSnapshot
from aats.services.ai_service.inference import AIInferenceService
from aats.services.ai_service.provider import AIProviderResponse
from aats.services.ai_service.prompt_builder import PromptBuilder
from aats.services.ai_service.validator import AssessmentValidator
from aats.services.decision_engine.target_position import TargetPositionEngine
from aats.storage.execution_repo import InMemoryExecutionRepository
from aats.storage.event_store import InMemoryEventStore


class FakeProvider:
    def __init__(self, *, payload: dict | None = None, delay_seconds: float = 0.0) -> None:
        self.payload = payload or {
            "regime": "trend",
            "directional_edge": 0.4,
            "expected_volatility": 0.08,
            "confidence": 0.75,
            "uncertainty": 0.2,
            "expected_holding_horizon": "15m",
            "invalidation_conditions": ["trend_break", "book_flip"],
            "risk_tags": ["provider_ok"],
            "rationale_summary": "valid_output",
        }
        self.delay_seconds = delay_seconds
        self.calls = 0

    async def generate_assessment(self, *, prompt: str, response_schema: dict[str, object]) -> AIProviderResponse:
        self.calls += 1
        _ = prompt
        _ = response_schema
        if self.delay_seconds > 0:
            await asyncio.sleep(self.delay_seconds)
        return AIProviderResponse(
            provider_name="fake_provider",
            request_id="req_1",
            latency_ms=12.0,
            payload=self.payload,
        )


class FlakyProvider(FakeProvider):
    def __init__(self) -> None:
        super().__init__()
        self._attempts = 0

    async def generate_assessment(self, *, prompt: str, response_schema: dict[str, object]) -> AIProviderResponse:
        self._attempts += 1
        if self._attempts == 1:
            raise ValueError("temporary_failure")
        return await super().generate_assessment(prompt=prompt, response_schema=response_schema)


class SequenceProvider(FakeProvider):
    def __init__(self, payloads: list[dict]) -> None:
        super().__init__(payload=payloads[0] if payloads else None)
        self._payloads = list(payloads)

    async def generate_assessment(self, *, prompt: str, response_schema: dict[str, object]) -> AIProviderResponse:
        self.calls += 1
        _ = prompt
        _ = response_schema
        payload = self._payloads.pop(0)
        return AIProviderResponse(
            provider_name="sequence_provider",
            request_id=f"seq_{self.calls}",
            latency_ms=10.0,
            payload=payload,
        )


class _FixedFeeResolver:
    def __init__(self, fee_bps: str) -> None:
        self.fee_bps = Decimal(fee_bps)

    def taker_fee_bps_decimal(self, *, symbol: str | None = None) -> Decimal:
        _ = symbol
        return self.fee_bps

    def taker_fee_bps(self, *, symbol: str | None = None) -> float:
        _ = symbol
        return float(self.fee_bps)

    def estimated_execution_fee_bps_decimal(self, *, symbol: str | None = None, **kwargs) -> Decimal:
        _ = symbol
        _ = kwargs
        return self.fee_bps

    def funding_fee_bps_decimal(self, *, symbol: str | None = None) -> Decimal:
        _ = symbol
        return Decimal("0")


class TestAIInferenceService(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_ai_output_falls_back_and_is_marked_invalid(self) -> None:
        service, context, baseline, _ = self._service(
            ai_operating_mode="ai_blended",
            provider=FakeProvider(payload={"bad": "payload"}),
        )

        assessment = await service.assess(context=context, baseline=baseline)

        self.assertTrue(assessment.fallback_used)
        self.assertFalse(assessment.output_valid)
        self.assertEqual(assessment.fallback_reason, "ai_output_schema_validation_failed")

    async def test_timeout_falls_back(self) -> None:
        service, context, baseline, provider = self._service(
            ai_operating_mode="ai_blended",
            ai_timeout_seconds=0.01,
            provider=FakeProvider(delay_seconds=0.05),
        )

        assessment = await service.assess(context=context, baseline=baseline)

        self.assertEqual(provider.calls, 1)
        self.assertTrue(assessment.fallback_used)
        self.assertEqual(assessment.fallback_reason, "ai_timeout")

    async def test_baseline_only_mode_skips_provider(self) -> None:
        service, context, baseline, provider = self._service(
            ai_operating_mode="baseline_only",
            provider=FakeProvider(),
        )

        assessment = await service.assess(context=context, baseline=baseline)

        self.assertEqual(provider.calls, 0)
        self.assertTrue(assessment.fallback_used)
        self.assertTrue(assessment.output_valid)
        self.assertEqual(assessment.fallback_reason, "baseline_only_mode")

    async def test_baseline_only_shadow_mode_does_not_call_provider(self) -> None:
        service, context, baseline, provider = self._service(
            ai_operating_mode="baseline_only",
            provider=FakeProvider(),
            ai_shadow_mode_enabled=True,
        )

        assessment = await service.assess(context=context, baseline=baseline)
        shadow = service.latest_shadow_assessment(context.decision_id)

        self.assertEqual(provider.calls, 0)
        self.assertTrue(assessment.fallback_used)
        self.assertIsNone(shadow)

    async def test_valid_provider_output_enforces_schema_and_records_calibration(self) -> None:
        service, context, baseline, _ = self._service(
            ai_operating_mode="ai_blended",
            provider=FakeProvider(),
        )

        assessment = await service.assess(context=context, baseline=baseline)

        self.assertFalse(assessment.fallback_used)
        self.assertTrue(assessment.output_valid)
        self.assertGreater(assessment.calibrated_confidence, 0.0)
        self.assertEqual(assessment.provider_name, "fake_provider")

    async def test_brief_prefers_dynamic_taker_fee_when_resolver_available(self) -> None:
        service, context, baseline, _ = self._service(
            ai_operating_mode="ai_blended",
            provider=FakeProvider(),
            fee_resolver=_FixedFeeResolver("12.5"),
        )

        assessment = await service.assess(context=context, baseline=baseline)
        brief = service.latest_brief(context.decision_id)

        self.assertFalse(assessment.fallback_used)
        self.assertIsNotNone(brief)
        self.assertEqual(brief.fee_bps, Decimal("12.5"))

    async def test_provider_output_can_include_restricted_execution_suggestion(self) -> None:
        service, context, baseline, _ = self._service(
            ai_operating_mode="ai_primary",
            provider=FakeProvider(
                payload={
                    "regime": "trend",
                    "directional_edge": 0.45,
                    "expected_volatility": 0.08,
                    "confidence": 0.8,
                    "uncertainty": 0.2,
                    "expected_holding_horizon": "15m",
                    "invalidation_conditions": ["trend_break", "book_flip"],
                    "risk_tags": ["provider_ok"],
                    "rationale_summary": "valid_output_with_execution_suggestion",
                    "baseline_override_recommended": True,
                    "override_reason_codes": ["ai_trend_override"],
                    "execution_parameter_suggestion": {
                        "passive_bias": 0.75,
                        "maker_taker_bias": -0.4,
                        "max_cross_spread_bps": 3.5,
                        "slice_count": 3,
                    },
                }
            ),
        )

        assessment = await service.assess(context=context, baseline=baseline)

        self.assertFalse(assessment.fallback_used)
        self.assertIsNotNone(assessment.ai_execution_parameter_suggestion)
        self.assertEqual(assessment.ai_execution_parameter_suggestion.status, "diagnostic_only")
        self.assertEqual(assessment.ai_execution_parameter_suggestion.suggestion.slice_count, 3)
        self.assertIn("execution_suggestion_present", assessment.validation_flags)

    async def test_semantically_invalid_provider_output_is_marked_non_actionable(self) -> None:
        service, context, baseline, _ = self._service(
            ai_operating_mode="ai_primary",
            provider=FakeProvider(
                payload={
                    "regime": "trend",
                    "directional_edge": 0.45,
                    "expected_volatility": 0.08,
                    "confidence": 0.85,
                    "uncertainty": 0.2,
                    "expected_holding_horizon": "15m",
                    "invalidation_conditions": ["one_only"],
                    "risk_tags": ["provider_ok"],
                    "rationale_summary": "invalid_override_contract",
                    "baseline_override_recommended": True,
                    "override_reason_codes": [],
                }
            ),
        )

        assessment = await service.assess(context=context, baseline=baseline)

        self.assertFalse(assessment.fallback_used)
        self.assertFalse(assessment.output_valid)
        self.assertIn("override_requires_reason_codes", assessment.rejection_flags)
        self.assertFalse(assessment.economically_actionable)

    async def test_retry_recovers_from_transient_provider_failure(self) -> None:
        provider = FlakyProvider()
        service, context, baseline, _ = self._service(
            ai_operating_mode="ai_blended",
            provider=provider,
            ai_max_retries=1,
        )

        assessment = await service.assess(context=context, baseline=baseline)

        self.assertFalse(assessment.fallback_used)
        self.assertTrue(assessment.output_valid)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(provider._attempts, 2)

    async def test_degraded_auto_downgrades_effective_mode_and_skips_next_provider_call(self) -> None:
        service, context, baseline, provider = self._service(
            ai_operating_mode="ai_primary",
            provider=FakeProvider(payload={"bad": "payload"}),
        )

        first = await service.assess(context=context, baseline=baseline)
        second_context = context.model_copy(update={"decision_id": "decision_ai_test_2"})
        second_baseline = baseline.model_copy(update={"decision_id": "decision_ai_test_2"})
        second = await service.assess(context=second_context, baseline=second_baseline)

        self.assertTrue(first.fallback_used)
        self.assertEqual(first.fallback_reason, "ai_output_schema_validation_failed")
        self.assertTrue(service.status()["degraded"])
        self.assertEqual(service.status()["effective_operating_mode"], "baseline_only")
        self.assertTrue(service.status()["auto_downgrade_active"])
        self.assertEqual(service.status()["provider_state"], "degraded")
        self.assertIsNotNone(service.status()["last_provider_degraded_at"])
        self.assertEqual(provider.calls, 1)
        self.assertTrue(second.fallback_used)
        self.assertEqual(second.fallback_reason, "ai_auto_downgraded")

    async def test_degraded_auto_downgrade_recovers_via_probe_attempts(self) -> None:
        provider = SequenceProvider(
            payloads=[
                {"bad": "payload"},
                {
                    "regime": "trend",
                    "directional_edge": 0.4,
                    "expected_volatility": 0.08,
                    "confidence": 0.8,
                    "uncertainty": 0.2,
                    "expected_holding_horizon": "15m",
                    "invalidation_conditions": ["trend_break", "book_flip"],
                    "risk_tags": ["provider_ok"],
                    "rationale_summary": "probe_recovery",
                    "baseline_override_recommended": True,
                    "override_reason_codes": ["ai_trend_override"],
                },
            ]
        )
        service, context, baseline, _ = self._service(
            ai_operating_mode="ai_primary",
            provider=provider,
            ai_recover_after_successes=1,
            ai_recovery_probe_interval_seconds=0.0,
        )

        first = await service.assess(context=context, baseline=baseline)
        second_context = context.model_copy(update={"decision_id": "decision_ai_probe_2"})
        second_baseline = baseline.model_copy(update={"decision_id": "decision_ai_probe_2"})
        second = await service.assess(context=second_context, baseline=second_baseline)

        self.assertTrue(first.fallback_used)
        self.assertTrue(first.degraded)
        self.assertFalse(second.fallback_used)
        self.assertFalse(second.degraded)
        self.assertEqual(service.status()["effective_operating_mode"], "ai_primary")
        self.assertFalse(service.status()["degraded"])
        self.assertEqual(service.status()["provider_state"], "healthy")
        self.assertIsNotNone(service.status()["last_provider_recovered_at"])
        self.assertEqual(provider.calls, 2)

    async def test_degradation_event_uses_dedicated_payload(self) -> None:
        service, context, baseline, _ = self._service(
            ai_operating_mode="ai_primary",
            provider=FakeProvider(payload={"bad": "payload"}),
        )

        await service.assess(context=context, baseline=baseline)

        event = service.event_store.latest(topics.AI_DEGRADATION_EVENTS)
        self.assertIsNotNone(event)
        self.assertEqual(event.payload["reason_code"], "ai_output_schema_validation_failed")
        self.assertIn("recovery_probe_after", event.payload)
        self.assertEqual(event.payload["configured_operating_mode"], "ai_decision_maker")

    async def test_shadow_outcome_can_trigger_ai_primary_review_and_auto_downgrade(self) -> None:
        service, _context, _baseline, _provider = self._service(
            ai_operating_mode="ai_primary",
            provider=FakeProvider(),
            ai_auto_downgrade_enabled=True,
            ai_outcome_review_bad_window_threshold=2,
            ai_outcome_review_warmup_evaluations=0,
            ai_outcome_review_min_trade_count=0,
        )

        bad_window = AIShadowEvaluation(
            decision_ids=["decision_shadow_bad_1", "decision_shadow_bad_2"],
            window_start=utc_now(),
            window_end=utc_now(),
            symbol="BTC-USDT",
            timeframe="15m",
            baseline_trade_count=2,
            shadow_trade_count=3,
            override_count=2,
            agreement_count=0,
            disagreement_count=2,
            fallback_count=0,
            baseline_gross_pnl=Decimal("10"),
            baseline_net_pnl=Decimal("9"),
            baseline_fee_total=Decimal("1"),
            baseline_fee_ratio=0.1,
            baseline_churn_ratio=0.2,
            shadow_gross_pnl=Decimal("7"),
            shadow_net_pnl=Decimal("5"),
            shadow_fee_total=Decimal("2"),
            shadow_fee_ratio=0.18,
            shadow_churn_ratio=0.32,
            shadow_outperformed=False,
        )

        service._record_shadow_outcome(bad_window)
        self.assertEqual(service.status()["effective_operating_mode"], "ai_primary")
        self.assertFalse(service.status()["outcome_review_required"])

        service._record_shadow_outcome(
            bad_window.model_copy(update={"decision_ids": ["decision_shadow_bad_3", "decision_shadow_bad_4"]})
        )

        status = service.status()
        self.assertTrue(status["outcome_review_required"])
        self.assertTrue(status["outcome_auto_downgrade_active"])
        self.assertEqual(status["effective_operating_mode"], "baseline_only")
        self.assertEqual(status["outcome_degradation_reason"], "ai_shadow_underperformed_baseline")
        self.assertEqual(status["outcome_state"], "auto_downgraded")
        self.assertIsNotNone(status["last_outcome_degraded_at"])

    async def test_shadow_outcome_review_requires_warmup_and_min_trade_count(self) -> None:
        service, _context, _baseline, _provider = self._service(
            ai_operating_mode="ai_primary",
            provider=FakeProvider(),
            ai_auto_downgrade_enabled=True,
            ai_outcome_review_bad_window_threshold=2,
            ai_outcome_review_warmup_evaluations=5,
            ai_outcome_review_min_trade_count=3,
        )

        bad_window = AIShadowEvaluation(
            decision_ids=["decision_shadow_bad_1", "decision_shadow_bad_2"],
            window_start=utc_now(),
            window_end=utc_now(),
            symbol="BTC-USDT",
            timeframe="15m",
            baseline_trade_count=2,
            shadow_trade_count=2,
            override_count=2,
            agreement_count=0,
            disagreement_count=2,
            fallback_count=0,
            baseline_gross_pnl=Decimal("10"),
            baseline_net_pnl=Decimal("9"),
            baseline_fee_total=Decimal("1"),
            baseline_fee_ratio=0.1,
            baseline_churn_ratio=0.2,
            shadow_gross_pnl=Decimal("7"),
            shadow_net_pnl=Decimal("5"),
            shadow_fee_total=Decimal("2"),
            shadow_fee_ratio=0.18,
            shadow_churn_ratio=0.32,
            shadow_outperformed=False,
        )

        for index in range(4):
            evaluation = bad_window.model_copy(update={"decision_ids": [f"prewarm_{index}_a", f"prewarm_{index}_b"]})
            service.evaluator.record_shadow_evaluation(evaluation)
            service._record_shadow_outcome(evaluation)

        self.assertFalse(service.status()["outcome_review_required"])
        self.assertEqual(service.status()["outcome_bad_window_streak"], 0)

        evaluation = bad_window.model_copy(update={"decision_ids": ["warmup_met_a", "warmup_met_b"], "shadow_trade_count": 3})
        service.evaluator.record_shadow_evaluation(evaluation)
        service._record_shadow_outcome(evaluation)
        self.assertFalse(service.status()["outcome_review_required"])
        self.assertEqual(service.status()["outcome_bad_window_streak"], 1)

        evaluation = bad_window.model_copy(update={"decision_ids": ["warmup_trigger_a", "warmup_trigger_b"], "shadow_trade_count": 3})
        service.evaluator.record_shadow_evaluation(evaluation)
        service._record_shadow_outcome(evaluation)
        self.assertTrue(service.status()["outcome_review_required"])

    async def test_shadow_outcome_tie_with_no_trades_does_not_trigger_review(self) -> None:
        service, _context, _baseline, _provider = self._service(
            ai_operating_mode="ai_primary",
            provider=FakeProvider(),
            ai_auto_downgrade_enabled=True,
            ai_outcome_review_bad_window_threshold=1,
            ai_outcome_review_warmup_evaluations=0,
            ai_outcome_review_min_trade_count=3,
        )

        neutral_window = AIShadowEvaluation(
            decision_ids=["decision_shadow_tie_1", "decision_shadow_tie_2"],
            window_start=utc_now(),
            window_end=utc_now(),
            symbol="BTC-USDT",
            timeframe="15m",
            baseline_trade_count=0,
            shadow_trade_count=0,
            override_count=0,
            agreement_count=2,
            disagreement_count=0,
            fallback_count=0,
            baseline_gross_pnl=Decimal("0"),
            baseline_net_pnl=Decimal("0"),
            baseline_fee_total=Decimal("0"),
            baseline_fee_ratio=0.0,
            baseline_churn_ratio=0.0,
            shadow_gross_pnl=Decimal("0"),
            shadow_net_pnl=Decimal("0"),
            shadow_fee_total=Decimal("0"),
            shadow_fee_ratio=0.0,
            shadow_churn_ratio=0.0,
            shadow_outperformed=None,
        )

        service.evaluator.record_shadow_evaluation(neutral_window)
        service._record_shadow_outcome(neutral_window)

        status = service.status()
        self.assertFalse(status["outcome_review_required"])
        self.assertEqual(status["outcome_bad_window_streak"], 0)

    async def test_shadow_evaluation_prefers_real_fills_for_price_and_fee_replay(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "ai_operating_mode": "ai_primary",
                "ai_provider": "openai",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
            }
        )
        event_store = InMemoryEventStore()
        execution_repo = InMemoryExecutionRepository()
        service = AIInferenceService(
            settings=settings,
            event_store=event_store,
            execution_repo=execution_repo,
            prompt_builder=PromptBuilder(),
            validator=AssessmentValidator(),
            provider=FakeProvider(),
        )
        decision_id = "decision_shadow_fill_backed"
        service.evaluator.record_brief(
            AIDecisionBrief(
                decision_id=decision_id,
                symbol="BTC-USDT-SWAP",
                timeframe="15m",
                product_type="derivatives",
                margin_mode="cross",
                last_price=110.0,
                regime_indicator="trend",
                regime_confidence=0.8,
                composite_alpha_score=0.4,
                momentum_score=0.02,
                volatility_state="medium",
                volatility_value=0.01,
                current_position_qty=0.0,
                current_exposure_side="flat",
                current_open_order_count=0,
                baseline_direction_bias="long",
                baseline_confidence=0.7,
                fee_bps=settings.paper_taker_fee_bps,
                max_slippage_tolerance_bps=float(settings.max_slippage_tolerance_bps),
                expected_slippage_proxy_bps=2.0,
                min_net_edge_bps=settings.strategy_min_net_edge_bps,
                safe_to_trade=True,
                review_required=False,
                halted=False,
                reconciliation_severity="CLEAN",
                reconciliation_halt_required=False,
                market_snapshot_fresh=True,
                account_snapshot_fresh=True,
                execution_condition="normal",
            )
        )
        service.record_shadow_decision(
            AIShadowDecision(
                decision_id=decision_id,
                symbol="BTC-USDT-SWAP",
                timeframe="15m",
                baseline_target_qty=0.5,
                baseline_action="open_long",
                ai_shadow_target_qty=0.5,
                ai_shadow_action="open_long",
                would_override_baseline=False,
                shadow_action_type="same_as_baseline",
            )
        )
        execution_repo.save_fill(
            FillEvent(
                fill_id="fill_shadow_1",
                decision_id=decision_id,
                intent_id="intent_shadow_1",
                client_order_id="order_shadow_1",
                exchange_order_id="ex_shadow_1",
                symbol="BTC-USDT-SWAP",
                venue="OKX",
                side="buy",
                fill_qty=0.5,
                fill_price=100.0,
                fee_amount=0.2,
                fee_currency="USDT",
                product_type="derivatives",
                target_leverage=1.0,
                margin_mode="cross",
                exposure_side="long",
                position_intent="open_long",
                liquidity_role="taker",
                exchange_timestamp=utc_now(),
                ingestion_timestamp=utc_now(),
                order_status_after_fill="FILLED",
            )
        )

        evaluation, created = service.evaluate_shadow_window(limit=10)

        self.assertTrue(created)
        self.assertIsNotNone(evaluation)
        self.assertEqual(evaluation.baseline_fee_total, Decimal("0.2"))
        self.assertEqual(evaluation.shadow_fee_total, Decimal("0.2"))
        self.assertEqual(evaluation.baseline_gross_pnl, Decimal("5.0"))
        self.assertEqual(evaluation.shadow_gross_pnl, Decimal("5.0"))
        self.assertEqual(evaluation.summary["baseline_fill_backed_decision_count"], 1.0)
        self.assertEqual(evaluation.summary["shadow_fill_backed_decision_count"], 1.0)
        service.publish_shadow_performance_report(
            evaluation=evaluation,
            latest_evaluation_ref="evt_shadow_eval",
        )
        performance_report = service.event_store.latest(topics.AI_PERFORMANCE_REPORTS)
        self.assertIsNotNone(performance_report)
        self.assertEqual(performance_report.payload["symbol"], "BTC-USDT-SWAP")
        self.assertEqual(performance_report.payload["product_type"], "derivatives")
        self.assertEqual(performance_report.payload["latest_evaluation_ref"], "evt_shadow_eval")
        self.assertIn("short", performance_report.payload["windows"])
        self.assertEqual(performance_report.payload["summary"]["outperformed_count"], 0)

    @staticmethod
    def _service(
        *,
        ai_operating_mode: str,
        provider: FakeProvider,
        ai_timeout_seconds: float = 5.0,
        ai_max_retries: int = 0,
        ai_shadow_mode_enabled: bool = False,
        ai_recover_after_successes: int = 1,
        ai_recovery_probe_interval_seconds: float = 300.0,
        fee_resolver=None,
        **settings_overrides,
    ) -> tuple[AIInferenceService, DecisionContext, BaselineAssessment, FakeProvider]:
        settings_payload = {
            "ai_operating_mode": ai_operating_mode,
            "ai_provider": "openai",
            "ai_timeout_seconds": ai_timeout_seconds,
            "ai_max_retries": ai_max_retries,
            "ai_degrade_after_failures": 1,
            "ai_recover_after_successes": ai_recover_after_successes,
            "ai_recovery_probe_interval_seconds": ai_recovery_probe_interval_seconds,
            "ai_shadow_mode_enabled": ai_shadow_mode_enabled,
        }
        settings_payload.update(settings_overrides)
        settings = AATSSettings.model_validate(settings_payload)
        event_store = InMemoryEventStore()
        execution_repo = InMemoryExecutionRepository()
        feature = FeatureSnapshot(
            symbol="BTC-USDT",
            snapshot_ts=utc_now(),
            market_snapshot_ref="evt_market",
            trend_strength=0.6,
            volatility_state="medium",
            volatility_value=0.01,
            momentum_score=0.004,
            liquidity_score=0.8,
            regime_indicator="trend",
            regime_confidence=0.7,
            multi_timeframe_alignment=0.6,
            feature_version="test",
        )
        feature_event = build_envelope(
            topic=topics.FEATURE_SNAPSHOTS,
            key="BTC-USDT",
            payload_model=feature,
            source_component="test",
        )
        event_store.append(feature_event)
        context = DecisionContext(
            decision_id="decision_ai_test",
            symbol="BTC-USDT",
            timeframe="15m",
            as_of_ts=utc_now(),
            market_snapshot_ref="evt_market",
            feature_snapshot_ref=feature_event.event_id,
            portfolio_snapshot_ref="evt_portfolio",
            health_snapshot_ref="evt_health",
            mode="paper_live",
            current_position_qty=0.0,
        )
        baseline = BaselineAssessment(
            decision_id=context.decision_id,
            symbol=context.symbol,
            regime="trend",
            direction_bias="long",
            trend_strength=0.6,
            volatility_state="medium",
            confidence=0.7,
            composite_alpha_score=0.45,
            suggested_position_scale=0.8,
            volatility_target_scale=1.0,
            factor_scores={"momentum_alpha": 0.4},
            holding_horizon=context.timeframe,
            invalidation_conditions=[],
            reason_codes=["test"],
            engine_version="test",
        )
        service = AIInferenceService(
            settings=settings,
            event_store=event_store,
            execution_repo=execution_repo,
            prompt_builder=PromptBuilder(),
            validator=AssessmentValidator(),
            provider=provider,
            fee_resolver=fee_resolver,
        )
        return service, context, baseline, provider


class TestAIOperatingModes(unittest.TestCase):
    def test_manual_operating_mode_override_expires_and_restores_automatic_logic(self) -> None:
        service, _context, _baseline, _provider = TestAIInferenceService._service(
            ai_operating_mode="ai_decision_maker_with_profile_control",
            provider=FakeProvider(),
        )

        status = service.set_manual_operating_mode_override(mode="baseline_only", freeze_seconds=60.0)

        self.assertTrue(status["manual_override_active"])
        self.assertEqual(status["effective_operating_mode"], "baseline_only")
        self.assertIsNotNone(status["manual_override_freeze_until"])

        service._manual_operating_mode_freeze_until = utc_now() - timedelta(seconds=1)
        restored = service.status()

        self.assertFalse(restored["manual_override_active"])
        self.assertIsNone(restored["manual_override_mode"])
        self.assertIsNone(restored["manual_override_freeze_until"])
        self.assertEqual(restored["effective_operating_mode"], "ai_decision_maker_with_profile_control")
        latest_event = service.event_store.latest(topics.AI_DEGRADATION_EVENTS, key=service.settings.default_symbol)
        self.assertIsNotNone(latest_event)
        self.assertEqual(latest_event.payload["reason_code"], "operator_manual_ai_mode_override_expired")

    def test_target_engine_respects_canonical_modes(self) -> None:
        baseline = BaselineAssessment(
            decision_id="decision_ai_mode",
            symbol="BTC-USDT",
            regime="trend",
            direction_bias="long",
            trend_strength=0.5,
            volatility_state="medium",
            confidence=0.7,
            composite_alpha_score=0.35,
            suggested_position_scale=0.75,
            volatility_target_scale=1.0,
            factor_scores={"momentum_alpha": 0.35},
            holding_horizon="15m",
            invalidation_conditions=[],
            reason_codes=["test"],
            engine_version="test",
        )
        ai_short = AIMarketAssessment(
            decision_id="decision_ai_mode",
            symbol="BTC-USDT",
            regime="trend",
            directional_edge=-0.5,
            expected_volatility=0.1,
            confidence=0.8,
            uncertainty=0.2,
            expected_holding_horizon="15m",
            invalidation_conditions=[],
            risk_tags=[],
            rationale_summary="test",
            operating_mode="ai_assisted",
            provider_name="fake",
            output_valid=True,
            fallback_used=False,
            degraded=False,
            calibrated_confidence=0.7,
            baseline_override_recommended=True,
            override_reason_codes=["ai_trend_override"],
            economically_actionable=True,
            estimated_edge_bps=50.0,
            estimated_cost_bps=12.0,
            estimated_net_edge_bps=38.0,
            source_mode="provider",
            execution_condition="normal",
            model_name="fake",
            model_version="1",
            prompt_version="1",
        )
        context = DecisionContext(
            decision_id="decision_ai_mode",
            symbol="BTC-USDT",
            timeframe="15m",
            as_of_ts=utc_now(),
            market_snapshot_ref="evt_market",
            feature_snapshot_ref="evt_feature",
            portfolio_snapshot_ref="evt_portfolio",
            health_snapshot_ref="evt_health",
            mode="paper_live",
            current_position_qty=0.0,
        )

        assisted_settings = AATSSettings.model_validate({"ai_operating_mode": "ai_assisted", "default_order_qty": 0.001})
        decision_maker_settings = AATSSettings.model_validate(
            {"ai_operating_mode": "ai_decision_maker", "default_order_qty": 0.001, "ai_primary_min_confidence": 0.6}
        )

        assisted_target = TargetPositionEngine(settings=assisted_settings).build(context, baseline, ai_short)
        decision_maker_target = TargetPositionEngine(settings=decision_maker_settings).build(context, baseline, ai_short)
        decision_maker_derivatives_target = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "ai_operating_mode": "ai_decision_maker",
                    "default_order_qty": 0.001,
                    "ai_primary_min_confidence": 0.6,
                    "trading_product_type": "derivatives",
                    "strategy_short_bias_enabled": True,
                }
            )
        ).build(
            context.model_copy(update={"product_type": "derivatives"}),
            baseline,
            ai_short,
        )

        self.assertGreater(assisted_target.target_position_qty, 0.0)
        self.assertEqual(decision_maker_target.target_position_qty, 0.0)
        self.assertLess(decision_maker_derivatives_target.target_position_qty, 0.0)

    def test_ai_primary_takeover_is_blocked_by_execution_cooldown(self) -> None:
        baseline = BaselineAssessment(
            decision_id="decision_ai_cooldown",
            symbol="BTC-USDT-SWAP",
            regime="trend",
            direction_bias="long",
            trend_strength=0.5,
            volatility_state="medium",
            confidence=0.8,
            composite_alpha_score=0.4,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={"momentum_alpha": 0.4},
            holding_horizon="15m",
            invalidation_conditions=[],
            reason_codes=["test"],
            engine_version="test",
        )
        ai_long = AIMarketAssessment(
            decision_id="decision_ai_cooldown",
            symbol="BTC-USDT-SWAP",
            regime="trend",
            directional_edge=0.6,
            expected_volatility=0.1,
            confidence=0.9,
            uncertainty=0.15,
            expected_holding_horizon="15m",
            invalidation_conditions=["trend_break", "book_flip"],
            risk_tags=[],
            rationale_summary="test",
            operating_mode="ai_primary",
            provider_name="fake",
            output_valid=True,
            fallback_used=False,
            degraded=False,
            calibrated_confidence=0.8,
            baseline_override_recommended=True,
            override_reason_codes=["ai_trend_override"],
            economically_actionable=True,
            estimated_edge_bps=60.0,
            estimated_cost_bps=8.0,
            estimated_net_edge_bps=52.0,
            source_mode="provider",
            execution_condition="normal",
            model_name="fake",
            model_version="1",
            prompt_version="1",
        )
        context = DecisionContext(
            decision_id="decision_ai_cooldown",
            symbol="BTC-USDT-SWAP",
            timeframe="15m",
            as_of_ts=utc_now(),
            market_snapshot_ref="evt_market",
            feature_snapshot_ref="evt_feature",
            portfolio_snapshot_ref="evt_portfolio",
            health_snapshot_ref="evt_health",
            mode="paper_live",
            current_position_qty=0.0,
            product_type="derivatives",
            last_position_closed_at=utc_now(),
            recent_closed_trade_count=5,
            recent_fee_drag_ratio=0.7,
            recent_churn_ratio=0.7,
            recent_low_edge_trade_streak=3,
            recent_low_edge_trade_at=utc_now(),
        )
        settings = AATSSettings.model_validate(
            {
                "ai_operating_mode": "ai_primary",
                "trading_product_type": "derivatives",
                "strategy_short_bias_enabled": True,
                "strategy_post_close_cooldown_seconds": 600.0,
                "strategy_low_edge_streak_limit": 3,
                "strategy_low_edge_cooldown_seconds": 1800.0,
                "strategy_performance_guard_min_closed_trades": 4,
                "strategy_max_fee_drag_ratio": 0.55,
                "strategy_max_churn_ratio": 0.6,
            }
        )

        target = TargetPositionEngine(settings=settings).build(context, baseline, ai_long)

        self.assertIsNotNone(target.decision_outcome)
        self.assertEqual(target.decision_outcome.decision_source, "baseline_fallback")
        self.assertIn("ai_post_close_cooldown_active", target.decision_outcome.decision_blocked_reasons)
        self.assertIn("ai_low_edge_cooldown_active", target.decision_outcome.decision_blocked_reasons)
        self.assertIn("ai_execution_performance_guard_active", target.decision_outcome.decision_blocked_reasons)


if __name__ == "__main__":
    unittest.main()
