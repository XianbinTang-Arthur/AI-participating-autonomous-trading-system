from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

from aats.bootstrap.logging import correlation_fields, get_logger, log_event
from aats.bootstrap.settings import AATSSettings
from aats.bus.base import EventBus
from aats.events import topics
from aats.events.envelopes import build_envelope, publish_model
from aats.schemas.ai_brief import AIDecisionBrief
from aats.schemas.ai_reports import AIPerformanceReport, AIPerformanceWindowReport
from aats.schemas.ai_shadow import AIDegradationEvent, AIShadowEvaluation
from aats.schemas.common import utc_now
from aats.schemas.decision import (
    AIMarketAssessment,
    BaselineAssessment,
    CanonicalAIOperatingMode,
    DecisionContext,
    normalize_ai_operating_mode,
)
from aats.schemas.features import FeatureSnapshot
from aats.services.ai_service.evaluator import AIEvaluationTracker
from aats.services.fee_resolver import EffectiveFeeResolver
from aats.services.portfolio_service.positions import PortfolioState
from aats.services.ai_service.openai_provider import OpenAIProvider
from aats.services.ai_service.provider import AIProvider, AIProviderError, AIProviderTimeoutError
from aats.services.ai_service.prompt_builder import PromptBuilder
from aats.services.ai_service.validator import AIOutputValidationError, AssessmentValidator
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, to_decimal
from aats.storage.base import EventStore, ExecutionRepository


class AIInferenceService:
    def __init__(
        self,
        *,
        settings: AATSSettings,
        event_store: EventStore,
        prompt_builder: PromptBuilder,
        validator: AssessmentValidator,
        bus: EventBus | None = None,
        execution_repo: ExecutionRepository | None = None,
        provider: AIProvider | None = None,
        evaluator: AIEvaluationTracker | None = None,
        fee_resolver: EffectiveFeeResolver | None = None,
    ) -> None:
        self.settings = settings
        self.event_store = event_store
        self.bus = bus
        self.execution_repo = execution_repo
        self.prompt_builder = prompt_builder
        self.validator = validator
        self.provider = provider or self._default_provider()
        self.evaluator = evaluator or AIEvaluationTracker()
        self.fee_resolver = fee_resolver or EffectiveFeeResolver(settings=settings)
        self.logger = get_logger("aats.ai_service")
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._degraded = False
        self._degradation_reason = ""
        self._recovery_probe_after: datetime | None = None
        self._outcome_review_required = False
        self._outcome_auto_downgraded = False
        self._outcome_degradation_reason = ""
        self._outcome_bad_window_streak = 0
        self._last_provider_degraded_at: datetime | None = None
        self._last_provider_recovered_at: datetime | None = None
        self._last_outcome_degraded_at: datetime | None = None
        self._last_outcome_recovered_at: datetime | None = None
        self._restore_runtime_state_from_events()

    async def assess(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
    ) -> AIMarketAssessment:
        feature_snapshot = self._feature_snapshot(context)
        probe_attempt = self._degraded and self._recovery_probe_ready()
        brief_fee_bps = self.fee_resolver.estimated_execution_fee_bps_decimal(
            symbol=context.symbol,
            execution_style="taker",
            order_type="market",
        )
        brief = self.prompt_builder.build_brief(
            context=context,
            baseline=baseline,
            feature_snapshot=feature_snapshot,
            margin_mode=self.settings.margin_mode,
            fee_bps=brief_fee_bps,
            funding_fee_bps=(
                self.fee_resolver.funding_fee_bps_decimal(symbol=context.symbol)
                if context.product_type == "derivatives"
                else Decimal("0")
            ),
            max_slippage_tolerance_bps=float(self.settings.max_slippage_tolerance_bps),
            expected_slippage_proxy_bps=self._expected_slippage_proxy_bps(),
            min_net_edge_bps=self.settings.strategy_min_net_edge_bps,
            degraded=self._degraded and not probe_attempt,
        )
        self.evaluator.record_brief(brief)
        operating_mode = self.effective_operating_mode()
        if self.settings.ai_operating_mode == "baseline_only":
            assessment = self.validator.fallback_assessment(
                brief=brief,
                context=context,
                baseline=baseline,
                operating_mode=operating_mode,
                fallback_reason="baseline_only_mode",
                degraded=self._degraded,
                output_valid=True,
                model_name=self.settings.ai_model_name,
                model_version=self.settings.ai_model_version,
                prompt_version=self.settings.ai_prompt_version,
            )
            self.evaluator.record_assessment(assessment)
            return assessment
        if operating_mode == "baseline_only":
            assessment = self.validator.fallback_assessment(
                brief=brief,
                context=context,
                baseline=baseline,
                operating_mode=operating_mode,
                fallback_reason="ai_auto_downgraded",
                degraded=self._degraded,
                output_valid=True,
                model_name=self.settings.ai_model_name,
                model_version=self.settings.ai_model_version,
                prompt_version=self.settings.ai_prompt_version,
            )
            self.evaluator.record_assessment(assessment)
            return assessment

        if self.provider is None:
            return await self._fallback(
                brief=brief,
                context=context,
                baseline=baseline,
                reason="ai_provider_not_configured",
                output_valid=False,
            )

        prompt = self.prompt_builder.build(
            brief=brief,
            operating_mode=operating_mode,
            include_execution_suggestion=self.settings.ai_execution_suggestion_mode != "disabled",
        )
        attempts = max(1, self.settings.ai_max_retries + 1)
        last_reason = "ai_provider_failed"
        for attempt in range(1, attempts + 1):
            try:
                response = await asyncio.wait_for(
                    self.provider.generate_assessment(
                        prompt=prompt,
                        response_schema=self.validator.output_schema(
                            include_execution_suggestion=self.settings.ai_execution_suggestion_mode != "disabled"
                        ),
                    ),
                    timeout=self.settings.ai_timeout_seconds,
                )
                assessment = self.validator.validate_provider_output(
                    raw_output=response.payload,
                    brief=brief,
                    context=context,
                    baseline=baseline,
                    operating_mode=operating_mode,
                    provider_name=response.provider_name,
                    provider_request_id=response.request_id,
                    provider_latency_ms=response.latency_ms,
                    model_name=self.settings.ai_model_name,
                    model_version=self.settings.ai_model_version,
                    prompt_version=self.settings.ai_prompt_version,
                    degraded=False if probe_attempt else self._degraded,
                    edge_bps_scale=self.settings.strategy_alpha_edge_bps_scale,
                )
                if assessment.output_valid:
                    self._record_success()
                    assessment = assessment.model_copy(
                        update={
                            "degraded": self._degraded,
                            "execution_condition": "degraded" if self._degraded else assessment.execution_condition,
                        }
                    )
                else:
                    await self._record_failure(reason="output_rejected")
                self.evaluator.record_assessment(assessment)
                log_event(
                    self.logger,
                    "ai_assessment_generated",
                    **correlation_fields(
                        decision_id=context.decision_id,
                        provider=response.provider_name,
                        operating_mode=operating_mode,
                        calibrated_confidence=assessment.calibrated_confidence,
                        degraded=self._degraded,
                        attempt=attempt,
                    ),
                )
                await self._maybe_record_shadow_assessment(
                    brief=brief,
                    context=context,
                    baseline=baseline,
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
                operating_mode=operating_mode,
                fallback_reason=last_reason,
                attempt=attempt,
                retrying=True,
                    ),
                )

        return await self._fallback(
            brief=brief,
            context=context,
            baseline=baseline,
            reason=last_reason,
            output_valid=False,
        )

    def latest_evaluation(self, decision_id: str):
        return self.evaluator.latest(decision_id)

    def latest_brief(self, decision_id: str) -> AIDecisionBrief | None:
        return self.evaluator.latest_brief(decision_id)

    def latest_shadow_assessment(self, decision_id: str) -> AIMarketAssessment | None:
        return self.evaluator.latest_shadow_assessment(decision_id)

    def latest_shadow_decision(self):
        return self.evaluator.latest_shadow_decision()

    def latest_shadow_evaluation(self):
        return self.evaluator.latest_shadow_evaluation()

    def recent_assessments(self, *, limit: int) -> list[AIMarketAssessment]:
        return self.evaluator.assessments_recent(limit=limit)

    def recent_shadow_decisions(self, *, limit: int):
        return self.evaluator.shadow_decisions_recent(limit=limit)

    def recent_shadow_evaluations(self, *, limit: int):
        return self.evaluator.shadow_evaluations_recent(limit=limit)

    def record_shadow_decision(self, shadow_decision) -> None:
        self.evaluator.record_shadow_decision(shadow_decision)

    def evaluate_shadow_window(self, *, limit: int = 50) -> tuple[AIShadowEvaluation | None, bool]:
        shadow_rows = list(reversed(self.evaluator.shadow_decisions_recent(limit=limit)))
        if not shadow_rows:
            return None, False
        decision_ids = [item.decision_id for item in shadow_rows]
        existing = self.evaluator.find_shadow_evaluation(decision_ids=decision_ids)
        if existing is not None:
            return existing, False
        first = shadow_rows[0]
        fills_by_decision = self._decision_fills(decision_ids)
        override_count = sum(1 for item in shadow_rows if item.would_override_baseline)
        agreement_count = sum(1 for item in shadow_rows if item.shadow_action_type == "same_as_baseline")
        disagreement_count = len(shadow_rows) - agreement_count
        fallback_count = 0
        for item in shadow_rows:
            assessment = self.latest_shadow_assessment(item.decision_id)
            if assessment is not None and assessment.fallback_used:
                fallback_count += 1
        baseline_replay = self._replay_baseline_path(shadow_rows=shadow_rows, fills_by_decision=fills_by_decision)
        shadow_replay = self._replay_shadow_path(shadow_rows=shadow_rows, fills_by_decision=fills_by_decision)
        evaluation = AIShadowEvaluation(
            window_start=shadow_rows[0].created_at,
            window_end=shadow_rows[-1].created_at,
            symbol=first.symbol,
            timeframe=first.timeframe,
            decision_ids=decision_ids,
            baseline_trade_count=baseline_replay["trade_count"],
            shadow_trade_count=shadow_replay["trade_count"],
            override_count=override_count,
            agreement_count=agreement_count,
            disagreement_count=disagreement_count,
            fallback_count=fallback_count,
            baseline_gross_pnl=baseline_replay["gross_pnl"],
            baseline_net_pnl=baseline_replay["net_pnl"],
            baseline_fee_total=baseline_replay["fee_total"],
            baseline_fee_ratio=baseline_replay["fee_ratio"],
            baseline_churn_ratio=baseline_replay["churn_ratio"],
            shadow_gross_pnl=shadow_replay["gross_pnl"],
            shadow_net_pnl=shadow_replay["net_pnl"],
            shadow_fee_total=shadow_replay["fee_total"],
            shadow_fee_ratio=shadow_replay["fee_ratio"],
            shadow_churn_ratio=shadow_replay["churn_ratio"],
            shadow_outperformed=shadow_replay["net_pnl"] > baseline_replay["net_pnl"],
            summary={
                "window_size": len(shadow_rows),
                "override_rate": round(override_count / len(shadow_rows), 6),
                "agreement_rate": round(agreement_count / len(shadow_rows), 6),
                "baseline_final_position_qty": baseline_replay["final_position_qty"],
                "shadow_final_position_qty": shadow_replay["final_position_qty"],
                "baseline_fill_backed_decision_count": baseline_replay["fill_backed_decision_count"],
                "shadow_fill_backed_decision_count": shadow_replay["fill_backed_decision_count"],
                "shadow_synthetic_decision_count": shadow_replay["synthetic_decision_count"],
            },
        )
        self.evaluator.record_shadow_evaluation(evaluation)
        self._record_shadow_outcome(evaluation)
        return evaluation, True

    def publish_shadow_performance_report(
        self,
        *,
        evaluation: AIShadowEvaluation,
        latest_evaluation_ref: str | None,
    ) -> None:
        self._publish_performance_report(
            evaluation=evaluation,
            latest_evaluation_ref=latest_evaluation_ref,
        )

    async def handle_portfolio_snapshot(self, message: dict) -> None:
        await self.evaluator.handle_portfolio_snapshot(message)

    async def handle_reconciliation_report(self, message: dict) -> None:
        await self.evaluator.handle_reconciliation_report(message)

    def status(self) -> dict[str, object]:
        recent_assessments = self.evaluator.assessments_recent(limit=25)
        fallback_ratio = 0.0
        if recent_assessments:
            fallback_ratio = sum(1 for item in recent_assessments if item.fallback_used) / len(recent_assessments)
        recent_timeout_count = sum(1 for item in recent_assessments if item.fallback_reason == "ai_timeout")
        recent_invalid_output_count = sum(
            1
            for item in recent_assessments
            if item.fallback_reason == "ai_output_schema_validation_failed"
            or "output_rejected" in item.evaluation_tags
            or not item.output_valid
        )
        recent_execution_suggestion_count = sum(
            1 for item in recent_assessments if item.ai_execution_parameter_suggestion is not None
        )
        degraded = self._degraded or self._outcome_review_required
        auto_downgrade_active = (
            (self._degraded and self.settings.ai_auto_downgrade_enabled)
            or self._outcome_auto_downgraded
        )
        degradation_reason = self._outcome_degradation_reason or self._degradation_reason
        failure_budget_remaining = max(
            self.settings.ai_degrade_after_failures - self._consecutive_failures,
            0,
        )
        recovery_budget_remaining = 0
        if self._degraded:
            recovery_budget_remaining = max(
                self.settings.ai_recover_after_successes - self._consecutive_successes,
                0,
            )
        outcome_budget_remaining = max(
            max(self.settings.ai_outcome_review_bad_window_threshold, 1) - self._outcome_bad_window_streak,
            0,
        )
        provider_state = "healthy"
        if self._degraded and self._recovery_probe_ready():
            provider_state = "recovery_probe"
        elif self._degraded:
            provider_state = "degraded"
        outcome_state = "healthy"
        if self._outcome_auto_downgraded:
            outcome_state = "auto_downgraded"
        elif self._outcome_review_required:
            outcome_state = "review_required"
        elif self._outcome_bad_window_streak > 0:
            outcome_state = "monitoring"
        return {
            "configured_operating_mode": self.settings.ai_operating_mode,
            "canonical_configured_operating_mode": self.settings.canonical_ai_operating_mode,
            "effective_operating_mode": self.effective_operating_mode(),
            "canonical_effective_operating_mode": self.canonical_effective_operating_mode(),
            "provider": self.settings.ai_provider,
            "configured": self.settings.ai_provider_configured,
            "provider_ready": self.provider is not None,
            "degraded": degraded,
            "provider_degraded": self._degraded,
            "outcome_review_required": self._outcome_review_required,
            "auto_downgrade_active": auto_downgrade_active,
            "outcome_auto_downgrade_active": self._outcome_auto_downgraded,
            "degradation_reason": degradation_reason or None,
            "outcome_degradation_reason": self._outcome_degradation_reason or None,
            "recovery_probe_after": self._recovery_probe_after,
            "recovery_probe_ready": self._recovery_probe_ready(),
            "consecutive_failures": self._consecutive_failures,
            "consecutive_successes": self._consecutive_successes,
            "outcome_bad_window_streak": self._outcome_bad_window_streak,
            "provider_state": provider_state,
            "outcome_state": outcome_state,
            "last_provider_degraded_at": self._last_provider_degraded_at,
            "last_provider_recovered_at": self._last_provider_recovered_at,
            "last_outcome_degraded_at": self._last_outcome_degraded_at,
            "last_outcome_recovered_at": self._last_outcome_recovered_at,
            "shadow_mode_enabled": self.settings.ai_shadow_mode_enabled,
            "execution_suggestion_mode": self.settings.ai_execution_suggestion_mode,
            "failure_budget": {
                "degrade_after_failures": self.settings.ai_degrade_after_failures,
                "recover_after_successes": self.settings.ai_recover_after_successes,
                "remaining_failures_until_degrade": failure_budget_remaining,
                "remaining_successes_until_recover": recovery_budget_remaining,
            },
            "outcome_policy": {
                "bad_window_threshold": max(self.settings.ai_outcome_review_bad_window_threshold, 1),
                "remaining_bad_windows_until_review": outcome_budget_remaining,
                "max_fee_ratio_delta": self.settings.ai_outcome_max_fee_ratio_delta,
                "max_churn_ratio_delta": self.settings.ai_outcome_max_churn_ratio_delta,
            },
            "recent_assessment_count": len(recent_assessments),
            "recent_shadow_evaluation_count": len(self.evaluator.shadow_evaluations_recent(limit=25)),
            "recent_execution_suggestion_count": recent_execution_suggestion_count,
            "recent_fallback_ratio": round(fallback_ratio, 6),
            "recent_timeout_count": recent_timeout_count,
            "recent_invalid_output_count": recent_invalid_output_count,
        }

    async def _fallback(
        self,
        *,
        brief: AIDecisionBrief,
        context: DecisionContext,
        baseline: BaselineAssessment,
        reason: str,
        output_valid: bool,
    ) -> AIMarketAssessment:
        await self._record_failure(reason=reason)
        assessment = self.validator.fallback_assessment(
            brief=brief,
            context=context,
            baseline=baseline,
            operating_mode=self.effective_operating_mode(),
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
                operating_mode=self.effective_operating_mode(),
                fallback_reason=reason,
                degraded=self._degraded,
            ),
        )
        return assessment

    async def _maybe_record_shadow_assessment(
        self,
        *,
        brief: AIDecisionBrief,
        context: DecisionContext,
        baseline: BaselineAssessment,
    ) -> None:
        if not self.settings.ai_shadow_mode_enabled or self.provider is None:
            return
        prompt = self.prompt_builder.build(
            brief=brief,
            operating_mode="ai_decision_maker",
            include_execution_suggestion=self.settings.ai_execution_suggestion_mode != "disabled",
        )
        try:
            response = await asyncio.wait_for(
                self.provider.generate_assessment(
                    prompt=prompt,
                    response_schema=self.validator.output_schema(
                        include_execution_suggestion=self.settings.ai_execution_suggestion_mode != "disabled"
                    ),
                ),
                timeout=self.settings.ai_timeout_seconds,
            )
            shadow_assessment = self.validator.validate_provider_output(
                raw_output=response.payload,
                brief=brief,
                context=context,
                baseline=baseline,
                operating_mode="ai_decision_maker",
                provider_name=response.provider_name,
                provider_request_id=response.request_id,
                provider_latency_ms=response.latency_ms,
                model_name=self.settings.ai_model_name,
                model_version=self.settings.ai_model_version,
                prompt_version=self.settings.ai_prompt_version,
                degraded=self._degraded,
                edge_bps_scale=self.settings.strategy_alpha_edge_bps_scale,
            )
            self.evaluator.record_shadow_assessment(shadow_assessment)
        except Exception:
            shadow_assessment = self.validator.fallback_assessment(
                brief=brief,
                context=context,
                baseline=baseline,
                operating_mode="ai_decision_maker",
                fallback_reason="ai_shadow_fallback",
                degraded=self._degraded,
                output_valid=False,
                model_name=self.settings.ai_model_name,
                model_version=self.settings.ai_model_version,
                prompt_version=self.settings.ai_prompt_version,
            )
            self.evaluator.record_shadow_assessment(shadow_assessment)

    def _feature_snapshot(self, context: DecisionContext) -> FeatureSnapshot | None:
        event = self.event_store.get(context.feature_snapshot_ref)
        if event is None:
            return None
        return FeatureSnapshot.model_validate(event.payload)

    async def _record_failure(self, *, reason: str) -> None:
        self._consecutive_failures += 1
        self._consecutive_successes = 0
        self._degradation_reason = reason
        if self._consecutive_failures >= self.settings.ai_degrade_after_failures:
            was_degraded = self._degraded
            self._degraded = True
            self._recovery_probe_after = utc_now() + timedelta(
                seconds=max(self.settings.ai_recovery_probe_interval_seconds, 0.0)
            )
            if not was_degraded:
                self._last_provider_degraded_at = utc_now()
                self._append_degradation_event(reason_code=self._degradation_reason or "ai_provider_failed")

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._consecutive_successes += 1
        if self._degraded and self._consecutive_successes >= self.settings.ai_recover_after_successes:
            self._degraded = False
            self._degradation_reason = ""
            self._consecutive_successes = 0
            self._recovery_probe_after = None
            self._last_provider_recovered_at = utc_now()
            self._append_degradation_event(reason_code="provider_recovered")
        elif self._degraded:
            self._recovery_probe_after = utc_now() + timedelta(
                seconds=max(self.settings.ai_recovery_probe_interval_seconds, 0.0)
            )

    def _default_provider(self) -> AIProvider | None:
        if self.settings.ai_provider == "openai" and self.settings.ai_provider_configured:
            return OpenAIProvider(settings=self.settings)
        return None

    def effective_operating_mode(self) -> str:
        if self._outcome_auto_downgraded:
            return "baseline_only"
        if self._degraded and self.settings.ai_auto_downgrade_enabled and not self._recovery_probe_ready():
            return "baseline_only"
        return self.settings.ai_operating_mode

    def canonical_effective_operating_mode(self) -> CanonicalAIOperatingMode:
        return normalize_ai_operating_mode(self.effective_operating_mode())

    def should_attempt_assessment(self) -> bool:
        if self.settings.ai_operating_mode == "baseline_only":
            return False
        return self.effective_operating_mode() != "baseline_only"

    def _expected_slippage_proxy_bps(self) -> float:
        return float(
            to_decimal(max(self.settings.max_slippage_tolerance_bps, 0))
            * max(to_decimal(self.settings.strategy_expected_slippage_bps_fraction), Decimal("0"))
        )

    def _recovery_probe_ready(self) -> bool:
        if not self._degraded or not self.settings.ai_auto_downgrade_enabled:
            return False
        if self._recovery_probe_after is None:
            return True
        return utc_now() >= self._recovery_probe_after

    def _append_degradation_event(
        self,
        *,
        reason_code: str,
        symbol: str | None = None,
        timeframe: str | None = None,
    ) -> None:
        event = AIDegradationEvent(
            symbol=symbol or self.settings.default_symbol,
            timeframe=timeframe or self.settings.primary_timeframe,
            product_type=self.settings.trading_product_type,
            margin_mode=self.settings.margin_mode,
            allowed_symbols=tuple(self.settings.allowed_symbols),
            configured_operating_mode=self.settings.canonical_ai_operating_mode,
            effective_operating_mode=self.canonical_effective_operating_mode(),
            degraded=self._degraded or self._outcome_review_required,
            provider_degraded=self._degraded,
            outcome_review_required=self._outcome_review_required,
            auto_downgrade_active=(
                (self._degraded and self.settings.ai_auto_downgrade_enabled)
                or self._outcome_auto_downgraded
            ),
            reason_code=reason_code,
            consecutive_failures=self._consecutive_failures,
            consecutive_successes=self._consecutive_successes,
            recovery_probe_after=self._recovery_probe_after,
        )
        self.event_store.append(
            build_envelope(
                topic=topics.AI_DEGRADATION_EVENTS,
                key=event.symbol,
                payload_model=event,
                source_component="ai_service",
            )
        )

    def _restore_runtime_state_from_events(self) -> None:
        latest = self.event_store.latest(topics.AI_DEGRADATION_EVENTS, key=self.settings.default_symbol)
        if latest is None:
            return
        payload = latest.payload
        self._degraded = bool(payload.get("provider_degraded", payload.get("degraded", False)))
        self._outcome_review_required = bool(payload.get("outcome_review_required", False))
        self._outcome_auto_downgraded = bool(payload.get("auto_downgrade_active", False)) and self._outcome_review_required
        self._degradation_reason = str(payload.get("reason_code") or "") if self._degraded else ""
        self._outcome_degradation_reason = (
            str(payload.get("reason_code") or "") if self._outcome_review_required else ""
        )
        self._recovery_probe_after = self._parse_event_datetime(payload.get("recovery_probe_after"))
        self._consecutive_failures = int(payload.get("consecutive_failures", 0) or 0)
        self._consecutive_successes = int(payload.get("consecutive_successes", 0) or 0)
        created_at = self._parse_event_datetime(payload.get("created_at"))
        if self._degraded:
            self._last_provider_degraded_at = created_at
        else:
            self._last_provider_recovered_at = created_at
        if self._outcome_review_required:
            self._last_outcome_degraded_at = created_at
        elif payload.get("outcome_review_required") is not None:
            self._last_outcome_recovered_at = created_at

    @staticmethod
    def _parse_event_datetime(value):
        if isinstance(value, datetime) or value is None:
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
        return None

    def _record_shadow_outcome(self, evaluation: AIShadowEvaluation) -> None:
        fee_ratio_delta = float(evaluation.shadow_fee_ratio or 0.0) - float(evaluation.baseline_fee_ratio or 0.0)
        churn_ratio_delta = float(evaluation.shadow_churn_ratio or 0.0) - float(evaluation.baseline_churn_ratio or 0.0)
        reason_codes: list[str] = []
        if evaluation.shadow_outperformed is False:
            reason_codes.append("ai_shadow_underperformed_baseline")
        if fee_ratio_delta > self.settings.ai_outcome_max_fee_ratio_delta:
            reason_codes.append("ai_shadow_fee_drag_worse")
        if churn_ratio_delta > self.settings.ai_outcome_max_churn_ratio_delta:
            reason_codes.append("ai_shadow_churn_worse")

        if reason_codes:
            self._outcome_bad_window_streak += 1
        else:
            was_review_required = self._outcome_review_required or self._outcome_auto_downgraded
            self._outcome_bad_window_streak = 0
            self._outcome_review_required = False
            self._outcome_auto_downgraded = False
            self._outcome_degradation_reason = ""
            if was_review_required:
                self._last_outcome_recovered_at = utc_now()
                self._append_degradation_event(
                    reason_code="outcome_review_recovered",
                    symbol=evaluation.symbol,
                    timeframe=evaluation.timeframe,
                )
            return

        if self._outcome_bad_window_streak < max(self.settings.ai_outcome_review_bad_window_threshold, 1):
            return

        self._outcome_review_required = True
        self._outcome_auto_downgraded = (
            self.settings.ai_auto_downgrade_enabled and self.settings.canonical_ai_operating_mode == "ai_decision_maker"
        )
        self._outcome_degradation_reason = reason_codes[0]
        self._last_outcome_degraded_at = utc_now()
        self._append_degradation_event(
            reason_code=self._outcome_degradation_reason,
            symbol=evaluation.symbol,
            timeframe=evaluation.timeframe,
        )

    def _publish_performance_report(
        self,
        *,
        evaluation: AIShadowEvaluation,
        latest_evaluation_ref: str | None,
    ) -> None:
        rows = [
            item
            for item in self.evaluator.shadow_evaluations_recent(limit=40)
            if item.symbol == evaluation.symbol and item.timeframe == evaluation.timeframe
        ]
        report = AIPerformanceReport(
            symbol=evaluation.symbol,
            timeframe=evaluation.timeframe,
            product_type=self.settings.trading_product_type,
            margin_mode=self.settings.margin_mode,
            allowed_symbols=tuple(self.settings.allowed_symbols),
            configured_operating_mode=self.settings.canonical_ai_operating_mode,
            effective_operating_mode=self.canonical_effective_operating_mode(),
            window_count=len(rows),
            latest_evaluation_ref=latest_evaluation_ref,
            latest_evaluation_id=evaluation.evaluation_id,
            latest_status="review_required" if self._outcome_review_required else ("healthy" if evaluation.shadow_outperformed else "underperforming"),
            review_required=self._outcome_review_required,
            windows=self._performance_windows(
                rows,
                max_fee_ratio_delta=self.settings.ai_outcome_max_fee_ratio_delta,
                max_churn_ratio_delta=self.settings.ai_outcome_max_churn_ratio_delta,
            ),
            summary={
                "latest_net_pnl_delta": (
                    Decimal(str(evaluation.shadow_net_pnl or "0")) - Decimal(str(evaluation.baseline_net_pnl or "0"))
                ),
                "latest_fee_ratio_delta": round(
                    float(evaluation.shadow_fee_ratio or 0.0) - float(evaluation.baseline_fee_ratio or 0.0),
                    6,
                ),
                "latest_churn_ratio_delta": round(
                    float(evaluation.shadow_churn_ratio or 0.0) - float(evaluation.baseline_churn_ratio or 0.0),
                    6,
                ),
                "outperformed_count": sum(1 for item in rows if item.shadow_outperformed is True),
                "underperformed_count": sum(1 for item in rows if item.shadow_outperformed is False),
            },
        )
        self.event_store.append(
            build_envelope(
                topic=topics.AI_PERFORMANCE_REPORTS,
                key=evaluation.symbol,
                payload_model=report,
                source_component="ai_service",
            )
        )

    @staticmethod
    def _performance_windows(
        rows: list[AIShadowEvaluation],
        *,
        max_fee_ratio_delta: float,
        max_churn_ratio_delta: float,
    ) -> dict[str, AIPerformanceWindowReport]:
        windows = {
            "short": ("recent_3_windows", 3),
            "medium": ("recent_5_windows", 5),
            "long": ("recent_10_windows", 10),
        }
        reports: dict[str, AIPerformanceWindowReport] = {}
        for key, (label, sample_size) in windows.items():
            sample = rows[:sample_size]
            if not sample:
                reports[key] = AIPerformanceWindowReport(label=label, sample_size=0)
                continue
            baseline_total = sum((Decimal(str(item.baseline_net_pnl or "0")) for item in sample), start=Decimal("0"))
            shadow_total = sum((Decimal(str(item.shadow_net_pnl or "0")) for item in sample), start=Decimal("0"))
            fee_deltas = [
                float(item.shadow_fee_ratio or 0.0) - float(item.baseline_fee_ratio or 0.0)
                for item in sample
            ]
            churn_deltas = [
                float(item.shadow_churn_ratio or 0.0) - float(item.baseline_churn_ratio or 0.0)
                for item in sample
            ]
            review_required_count = sum(
                1
                for item in sample
                if (
                    item.shadow_outperformed is False
                    or (float(item.shadow_fee_ratio or 0.0) - float(item.baseline_fee_ratio or 0.0)) > max_fee_ratio_delta
                    or (float(item.shadow_churn_ratio or 0.0) - float(item.baseline_churn_ratio or 0.0)) > max_churn_ratio_delta
                )
            )
            reports[key] = AIPerformanceWindowReport(
                label=label,
                sample_size=len(sample),
                outperformed_rate=round(
                    sum(1 for item in sample if item.shadow_outperformed is True) / len(sample),
                    6,
                ),
                baseline_net_pnl_total=baseline_total,
                shadow_net_pnl_total=shadow_total,
                net_pnl_delta_total=shadow_total - baseline_total,
                avg_fee_ratio_delta=round(sum(fee_deltas) / len(fee_deltas), 6),
                avg_churn_ratio_delta=round(sum(churn_deltas) / len(churn_deltas), 6),
                review_required_count=review_required_count,
            )
        return reports

    def _runtime_scope(self):
        from aats.services.runtime_scope import runtime_state_scope

        return runtime_state_scope(self.settings)

    def _estimated_execution_fee_bps_for_assessment(
        self,
        *,
        symbol: str,
        assessment: AIMarketAssessment | None,
    ) -> Decimal:
        envelope = None if assessment is None else assessment.ai_execution_parameter_suggestion
        suggestion = None if envelope is None else envelope.suggestion
        execution_style = "taker"
        order_type = "market"
        if suggestion is not None and self.settings.ai_execution_suggestion_mode != "disabled":
            execution_style = "bounded_limit_ioc"
            order_type = "limit"
        return self.fee_resolver.estimated_execution_fee_bps_decimal(
            symbol=symbol,
            execution_style=execution_style,
            order_type=order_type,
            passive_bias=None if suggestion is None else suggestion.passive_bias,
            maker_taker_bias=None if suggestion is None else suggestion.maker_taker_bias,
        )

    def _decision_fills(self, decision_ids: list[str]) -> dict[str, list]:
        if self.execution_repo is None or not decision_ids:
            return {}
        allowed = set(decision_ids)
        rows = [
            fill
            for fill in self.execution_repo.fills()
            if fill.decision_id in allowed
        ]
        rows.sort(key=lambda item: (item.ingestion_timestamp, item.fill_id))
        by_decision: dict[str, list] = {}
        for fill in rows:
            by_decision.setdefault(fill.decision_id, []).append(fill)
        return by_decision

    def _replay_baseline_path(
        self,
        *,
        shadow_rows: list,
        fills_by_decision: dict[str, list],
    ) -> dict[str, float]:
        if not fills_by_decision:
            return self._replay_target_path(shadow_rows=shadow_rows, use_shadow_targets=False)

        current_qty = Decimal("0")
        avg_entry_price = Decimal("0")
        realized_gross_pnl = Decimal("0")
        fee_total = Decimal("0")
        trade_count = 0
        low_edge_trade_count = 0
        last_price = Decimal("0")
        fill_backed_decision_count = 0

        for row in shadow_rows:
            decision_fills = fills_by_decision.get(row.decision_id, [])
            brief = self.latest_brief(row.decision_id)
            if not decision_fills:
                if brief is not None and brief.last_price is not None and brief.last_price > Decimal("0"):
                    last_price = to_decimal(brief.last_price)
                continue
            fill_backed_decision_count += 1
            trade_count += 1
            decision_realized = Decimal("0")
            decision_fee_total = Decimal("0")
            for fill in decision_fills:
                last_price = fill.fill_price
                signed_qty = fill.fill_qty if fill.side == "buy" else -fill.fill_qty
                realized_delta, current_qty, avg_entry_price = self._apply_signed_execution(
                    current_qty=current_qty,
                    avg_entry_price=avg_entry_price,
                    signed_qty=signed_qty,
                    execution_price=fill.fill_price,
                )
                decision_realized += realized_delta
                decision_fee_total += PortfolioState.fee_cost_in_quote(fill)
            realized_gross_pnl += decision_realized
            fee_total += decision_fee_total
            if abs(decision_realized) <= decision_fee_total * Decimal("1.25"):
                low_edge_trade_count += 1
            if brief is not None and brief.last_price is not None and brief.last_price > Decimal("0"):
                last_price = to_decimal(brief.last_price)

        unrealized_pnl = current_qty * (last_price - avg_entry_price) if last_price > 0 else Decimal("0")
        gross_pnl = realized_gross_pnl + unrealized_pnl
        net_pnl = gross_pnl - fee_total
        fee_ratio = float(abs(fee_total / gross_pnl)) if abs(gross_pnl) > EPSILON_DECIMAL_12 else None
        churn_ratio = (low_edge_trade_count / trade_count) if trade_count else 0.0
        return {
            "trade_count": float(trade_count),
            "gross_pnl": float(gross_pnl),
            "net_pnl": float(net_pnl),
            "fee_total": float(fee_total),
            "fee_ratio": fee_ratio,
            "churn_ratio": churn_ratio,
            "final_position_qty": float(current_qty),
            "fill_backed_decision_count": float(fill_backed_decision_count),
            "synthetic_decision_count": 0.0,
        }

    def _replay_shadow_path(
        self,
        *,
        shadow_rows: list,
        fills_by_decision: dict[str, list],
    ) -> dict[str, float]:
        if not fills_by_decision:
            return self._replay_target_path(shadow_rows=shadow_rows, use_shadow_targets=True)

        current_qty = Decimal("0")
        avg_entry_price = Decimal("0")
        realized_gross_pnl = Decimal("0")
        fee_total = Decimal("0")
        trade_count = 0
        low_edge_trade_count = 0
        last_price = Decimal("0")
        fill_backed_decision_count = 0
        synthetic_decision_count = 0

        for row in shadow_rows:
            brief = self.latest_brief(row.decision_id)
            price = (
                to_decimal(brief.last_price)
                if brief is not None and brief.last_price is not None and brief.last_price > Decimal("0")
                else last_price
            )
            target_qty = to_decimal(row.ai_shadow_target_qty)
            delta_qty = target_qty - current_qty
            if abs(delta_qty) <= EPSILON_DECIMAL_12:
                if price > 0:
                    last_price = price
                continue

            trade_count += 1
            decision_fills = fills_by_decision.get(row.decision_id, [])
            decision_fee = Decimal("0")
            if decision_fills:
                priced_qty = sum((max(fill.fill_qty, Decimal("0")) for fill in decision_fills), start=Decimal("0"))
                notional = sum((max(fill.fill_qty, Decimal("0")) * fill.fill_price for fill in decision_fills), start=Decimal("0"))
                actual_fee_total = sum((PortfolioState.fee_cost_in_quote(fill) for fill in decision_fills), start=Decimal("0"))
                if priced_qty > EPSILON_DECIMAL_12 and notional > EPSILON_DECIMAL_12:
                    execution_price = notional / priced_qty
                    last_price = execution_price
                    fill_backed_decision_count += 1
                    executable_qty = min(abs(delta_qty), priced_qty)
                    if executable_qty <= EPSILON_DECIMAL_12:
                        continue
                    signed_qty = executable_qty if delta_qty > 0 else -executable_qty
                    fee_bps = (actual_fee_total / notional) * Decimal("10000")
                    decision_fee = executable_qty * execution_price * (fee_bps / Decimal("10000"))
                else:
                    decision_fills = []
            if not decision_fills:
                if price <= 0:
                    continue
                last_price = price
                execution_price = price
                signed_qty = delta_qty
                synthetic_decision_count += 1
                decision_fee = abs(signed_qty) * execution_price * (
                    self._estimated_execution_fee_bps_for_assessment(
                        symbol=row.symbol,
                        assessment=self.latest_shadow_assessment(row.decision_id),
                    ) / Decimal("10000")
                )

            fee_total += decision_fee
            realized_trade_pnl, current_qty, avg_entry_price = self._apply_signed_execution(
                current_qty=current_qty,
                avg_entry_price=avg_entry_price,
                signed_qty=signed_qty,
                execution_price=execution_price,
            )
            realized_gross_pnl += realized_trade_pnl
            if abs(realized_trade_pnl) <= decision_fee * Decimal("1.25"):
                low_edge_trade_count += 1
            if brief is not None and brief.last_price is not None and brief.last_price > Decimal("0"):
                last_price = to_decimal(brief.last_price)

        unrealized_pnl = current_qty * (last_price - avg_entry_price) if last_price > 0 else Decimal("0")
        gross_pnl = realized_gross_pnl + unrealized_pnl
        net_pnl = gross_pnl - fee_total
        fee_ratio = float(abs(fee_total / gross_pnl)) if abs(gross_pnl) > EPSILON_DECIMAL_12 else None
        churn_ratio = (low_edge_trade_count / trade_count) if trade_count else 0.0
        return {
            "trade_count": float(trade_count),
            "gross_pnl": float(gross_pnl),
            "net_pnl": float(net_pnl),
            "fee_total": float(fee_total),
            "fee_ratio": fee_ratio,
            "churn_ratio": churn_ratio,
            "final_position_qty": float(current_qty),
            "fill_backed_decision_count": float(fill_backed_decision_count),
            "synthetic_decision_count": float(synthetic_decision_count),
        }

    def _replay_target_path(
        self,
        *,
        shadow_rows: list,
        use_shadow_targets: bool,
    ) -> dict[str, float]:
        current_qty = Decimal("0")
        avg_entry_price = Decimal("0")
        realized_gross_pnl = Decimal("0")
        fee_total = Decimal("0")
        trade_count = 0
        low_edge_trade_count = 0
        last_price = Decimal("0")

        for row in shadow_rows:
            brief = self.latest_brief(row.decision_id)
            price = (
                to_decimal(brief.last_price)
                if brief is not None and brief.last_price is not None and brief.last_price > Decimal("0")
                else last_price
            )
            if price <= 0:
                continue
            last_price = price
            target_qty = to_decimal(row.ai_shadow_target_qty if use_shadow_targets else row.baseline_target_qty)
            delta_qty = target_qty - current_qty
            if abs(delta_qty) <= EPSILON_DECIMAL_12:
                continue
            trade_count += 1
            assessment = self.latest_shadow_assessment(row.decision_id) if use_shadow_targets else None
            fee = abs(delta_qty) * price * (
                self._estimated_execution_fee_bps_for_assessment(
                    symbol=row.symbol,
                    assessment=assessment,
                ) / Decimal("10000")
            )
            fee_total += fee
            realized_trade_pnl, current_qty, avg_entry_price = self._apply_signed_execution(
                current_qty=current_qty,
                avg_entry_price=avg_entry_price,
                signed_qty=delta_qty,
                execution_price=price,
            )
            realized_gross_pnl += realized_trade_pnl
            if abs(realized_trade_pnl) <= fee * Decimal("1.25"):
                low_edge_trade_count += 1

        unrealized_pnl = current_qty * (last_price - avg_entry_price) if last_price > 0 else Decimal("0")
        gross_pnl = realized_gross_pnl + unrealized_pnl
        net_pnl = gross_pnl - fee_total
        fee_ratio = float(abs(fee_total / gross_pnl)) if abs(gross_pnl) > EPSILON_DECIMAL_12 else None
        churn_ratio = (low_edge_trade_count / trade_count) if trade_count else 0.0
        return {
            "trade_count": float(trade_count),
            "gross_pnl": float(gross_pnl),
            "net_pnl": float(net_pnl),
            "fee_total": float(fee_total),
            "fee_ratio": fee_ratio,
            "churn_ratio": churn_ratio,
            "final_position_qty": float(current_qty),
            "fill_backed_decision_count": 0.0,
            "synthetic_decision_count": float(trade_count),
        }

    @staticmethod
    def _apply_signed_execution(
        *,
        current_qty: Decimal,
        avg_entry_price: Decimal,
        signed_qty: Decimal,
        execution_price: Decimal,
    ) -> tuple[Decimal, Decimal, Decimal]:
        realized_trade_pnl = Decimal("0")
        next_qty = current_qty + signed_qty

        if abs(current_qty) <= EPSILON_DECIMAL_12:
            return Decimal("0"), next_qty, execution_price if abs(next_qty) > EPSILON_DECIMAL_12 else Decimal("0")

        same_direction = current_qty * signed_qty > 0
        if same_direction:
            combined_qty = next_qty
            if abs(combined_qty) <= EPSILON_DECIMAL_12:
                return Decimal("0"), Decimal("0"), Decimal("0")
            weighted_notional = (current_qty * avg_entry_price) + (signed_qty * execution_price)
            next_avg = weighted_notional / combined_qty
            return Decimal("0"), combined_qty, next_avg

        close_qty = min(abs(signed_qty), abs(current_qty))
        realized_trade_pnl += close_qty * (execution_price - avg_entry_price) * (Decimal("1") if current_qty > 0 else Decimal("-1"))
        remaining_qty = next_qty
        if abs(remaining_qty) <= EPSILON_DECIMAL_12:
            return realized_trade_pnl, Decimal("0"), Decimal("0")
        if current_qty * remaining_qty > 0:
            return realized_trade_pnl, remaining_qty, avg_entry_price
        return realized_trade_pnl, remaining_qty, execution_price
