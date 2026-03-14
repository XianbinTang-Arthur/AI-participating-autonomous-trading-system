from __future__ import annotations

import asyncio

from aats.bootstrap.logging import correlation_fields, get_logger, log_event
from aats.bootstrap.settings import AATSSettings
from aats.schemas.decision import AIMarketAssessment, BaselineAssessment, DecisionContext
from aats.schemas.features import FeatureSnapshot
from aats.services.ai_service.evaluator import AIEvaluationTracker
from aats.services.ai_service.openai_provider import OpenAIProvider
from aats.services.ai_service.provider import AIProvider, AIProviderError, AIProviderTimeoutError
from aats.services.ai_service.prompt_builder import PromptBuilder
from aats.services.ai_service.validator import AIOutputValidationError, AssessmentValidator
from aats.storage.base import EventStore


class AIInferenceService:
    def __init__(
        self,
        *,
        settings: AATSSettings,
        event_store: EventStore,
        prompt_builder: PromptBuilder,
        validator: AssessmentValidator,
        provider: AIProvider | None = None,
        evaluator: AIEvaluationTracker | None = None,
    ) -> None:
        self.settings = settings
        self.event_store = event_store
        self.prompt_builder = prompt_builder
        self.validator = validator
        self.provider = provider or self._default_provider()
        self.evaluator = evaluator or AIEvaluationTracker()
        self.logger = get_logger("aats.ai_service")
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._degraded = False

    async def assess(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
    ) -> AIMarketAssessment:
        feature_snapshot = self._feature_snapshot(context)
        prompt = self.prompt_builder.build(
            context=context,
            baseline=baseline,
            feature_snapshot=feature_snapshot,
            operating_mode=self.settings.ai_operating_mode,
        )
        if self.settings.ai_operating_mode == "baseline_only":
            assessment = self.validator.fallback_assessment(
                context=context,
                baseline=baseline,
                operating_mode=self.settings.ai_operating_mode,
                fallback_reason="baseline_only_mode",
                degraded=self._degraded,
                output_valid=True,
                model_name=self.settings.ai_model_name,
                model_version=self.settings.ai_model_version,
                prompt_version=self.settings.ai_prompt_version,
            )
            self.evaluator.record_assessment(assessment)
            return assessment

        if self.provider is None:
            return self._fallback(
                context=context,
                baseline=baseline,
                reason="ai_provider_not_configured",
                output_valid=False,
            )

        attempts = max(1, self.settings.ai_max_retries + 1)
        last_reason = "ai_provider_failed"
        for attempt in range(1, attempts + 1):
            try:
                response = await asyncio.wait_for(
                    self.provider.generate_assessment(
                        prompt=prompt,
                        response_schema=self.validator.output_schema(),
                    ),
                    timeout=self.settings.ai_timeout_seconds,
                )
                assessment = self.validator.validate_provider_output(
                    raw_output=response.payload,
                    context=context,
                    baseline=baseline,
                    operating_mode=self.settings.ai_operating_mode,
                    provider_name=response.provider_name,
                    provider_request_id=response.request_id,
                    provider_latency_ms=response.latency_ms,
                    model_name=self.settings.ai_model_name,
                    model_version=self.settings.ai_model_version,
                    prompt_version=self.settings.ai_prompt_version,
                    degraded=self._degraded,
                )
                self._record_success()
                self.evaluator.record_assessment(assessment)
                log_event(
                    self.logger,
                    "ai_assessment_generated",
                    **correlation_fields(
                        decision_id=context.decision_id,
                        provider=response.provider_name,
                        operating_mode=self.settings.ai_operating_mode,
                        calibrated_confidence=assessment.calibrated_confidence,
                        degraded=self._degraded,
                        attempt=attempt,
                    ),
                )
                return assessment
            except (asyncio.TimeoutError, AIProviderTimeoutError):
                last_reason = "ai_timeout"
            except (AIProviderError, AIOutputValidationError) as exc:
                last_reason = str(exc)
            except Exception as exc:
                last_reason = f"ai_unexpected_error:{type(exc).__name__}"
            if attempt < attempts:
                log_event(
                    self.logger,
                    "ai_assessment_retry",
                    level="warning",
                    **correlation_fields(
                        decision_id=context.decision_id,
                        provider=self.settings.ai_provider,
                        operating_mode=self.settings.ai_operating_mode,
                        fallback_reason=last_reason,
                        attempt=attempt,
                        retrying=True,
                    ),
                )

        return self._fallback(
            context=context,
            baseline=baseline,
            reason=last_reason,
            output_valid=False,
        )

    def latest_evaluation(self, decision_id: str):
        return self.evaluator.latest(decision_id)

    async def handle_portfolio_snapshot(self, message: dict) -> None:
        await self.evaluator.handle_portfolio_snapshot(message)

    async def handle_reconciliation_report(self, message: dict) -> None:
        await self.evaluator.handle_reconciliation_report(message)

    def status(self) -> dict[str, object]:
        return {
            "operating_mode": self.settings.ai_operating_mode,
            "provider": self.settings.ai_provider,
            "configured": self.settings.ai_provider_configured,
            "degraded": self._degraded,
            "consecutive_failures": self._consecutive_failures,
            "consecutive_successes": self._consecutive_successes,
        }

    def _fallback(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
        reason: str,
        output_valid: bool,
    ) -> AIMarketAssessment:
        self._record_failure()
        assessment = self.validator.fallback_assessment(
            context=context,
            baseline=baseline,
            operating_mode=self.settings.ai_operating_mode,
            fallback_reason=reason,
            degraded=self._degraded,
            output_valid=output_valid,
            model_name=self.settings.ai_model_name,
            model_version=self.settings.ai_model_version,
            prompt_version=self.settings.ai_prompt_version,
        )
        self.evaluator.record_assessment(assessment)
        log_event(
            self.logger,
            "ai_assessment_fallback",
            level="warning",
            **correlation_fields(
                decision_id=context.decision_id,
                provider=self.settings.ai_provider,
                operating_mode=self.settings.ai_operating_mode,
                fallback_reason=reason,
                degraded=self._degraded,
            ),
        )
        return assessment

    def _feature_snapshot(self, context: DecisionContext) -> FeatureSnapshot | None:
        event = self.event_store.get(context.feature_snapshot_ref)
        if event is None:
            return None
        return FeatureSnapshot.model_validate(event.payload)

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        self._consecutive_successes = 0
        if self._consecutive_failures >= self.settings.ai_degrade_after_failures:
            self._degraded = True

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._consecutive_successes += 1
        if self._degraded and self._consecutive_successes >= self.settings.ai_recover_after_successes:
            self._degraded = False
            self._consecutive_successes = 0

    def _default_provider(self) -> AIProvider | None:
        if self.settings.ai_provider == "openai" and self.settings.ai_provider_configured:
            return OpenAIProvider(settings=self.settings)
        return None
