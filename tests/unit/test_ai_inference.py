from __future__ import annotations

import asyncio
import unittest

from aats.bootstrap.settings import AATSSettings
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.common import utc_now
from aats.schemas.decision import AIMarketAssessment, BaselineAssessment, DecisionContext
from aats.schemas.features import FeatureSnapshot
from aats.services.ai_service.inference import AIInferenceService
from aats.services.ai_service.provider import AIProviderResponse
from aats.services.ai_service.prompt_builder import PromptBuilder
from aats.services.ai_service.validator import AssessmentValidator
from aats.services.decision_engine.target_position import TargetPositionEngine
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
            "invalidation_conditions": ["trend_break"],
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

    @staticmethod
    def _service(
        *,
        ai_operating_mode: str,
        provider: FakeProvider,
        ai_timeout_seconds: float = 5.0,
        ai_max_retries: int = 0,
    ) -> tuple[AIInferenceService, DecisionContext, BaselineAssessment, FakeProvider]:
        settings = AATSSettings.model_validate(
            {
                "ai_operating_mode": ai_operating_mode,
                "ai_provider": "openai",
                "ai_timeout_seconds": ai_timeout_seconds,
                "ai_max_retries": ai_max_retries,
                "ai_degrade_after_failures": 1,
                "ai_recover_after_successes": 1,
            }
        )
        event_store = InMemoryEventStore()
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
            prompt_builder=PromptBuilder(),
            validator=AssessmentValidator(),
            provider=provider,
        )
        return service, context, baseline, provider


class TestAIOperatingModes(unittest.TestCase):
    def test_target_engine_respects_modes(self) -> None:
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
            operating_mode="ai_blended",
            provider_name="fake",
            output_valid=True,
            fallback_used=False,
            degraded=False,
            calibrated_confidence=0.7,
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

        advisory_settings = AATSSettings.model_validate({"ai_operating_mode": "ai_advisory", "default_order_qty": 0.001})
        blended_settings = AATSSettings.model_validate({"ai_operating_mode": "ai_blended", "default_order_qty": 0.001})
        primary_settings = AATSSettings.model_validate(
            {"ai_operating_mode": "ai_primary", "default_order_qty": 0.001, "ai_primary_min_confidence": 0.6}
        )

        advisory_target = TargetPositionEngine(settings=advisory_settings).build(context, baseline, ai_short)
        blended_target = TargetPositionEngine(settings=blended_settings).build(context, baseline, ai_short)
        primary_target = TargetPositionEngine(settings=primary_settings).build(context, baseline, ai_short)
        primary_derivatives_target = TargetPositionEngine(
            settings=AATSSettings.model_validate(
                {
                    "ai_operating_mode": "ai_primary",
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

        self.assertGreater(advisory_target.target_position_qty, 0.0)
        self.assertEqual(blended_target.target_position_qty, 0.0)
        self.assertEqual(primary_target.target_position_qty, 0.0)
        self.assertLess(primary_derivatives_target.target_position_qty, 0.0)


if __name__ == "__main__":
    unittest.main()
