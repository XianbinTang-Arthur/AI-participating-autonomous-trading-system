from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from aats.bootstrap.logging import correlation_fields, get_logger, log_event
from aats.bootstrap.settings import AATSSettings
from aats.bus.base import EventBus
from aats.events import topics
from aats.events.envelopes import build_envelope, publish_model
from aats.schemas.ai_brief import AIDecisionBrief
from aats.schemas.ai_shadow import AIDegradationEvent, AIShadowEvaluation
from aats.schemas.common import utc_now
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
        bus: EventBus | None = None,
        provider: AIProvider | None = None,
        evaluator: AIEvaluationTracker | None = None,
    ) -> None:
        self.settings = settings
        self.event_store = event_store
        self.bus = bus
        self.prompt_builder = prompt_builder
        self.validator = validator
        self.provider = provider or self._default_provider()
        self.evaluator = evaluator or AIEvaluationTracker()
        self.logger = get_logger("aats.ai_service")
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._degraded = False
        self._degradation_reason = ""
        self._recovery_probe_after: datetime | None = None

    async def assess(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
    ) -> AIMarketAssessment:
        feature_snapshot = self._feature_snapshot(context)
        probe_attempt = self._degraded and self._recovery_probe_ready()
        brief = self.prompt_builder.build_brief(
            context=context,
            baseline=baseline,
            feature_snapshot=feature_snapshot,
            margin_mode=self.settings.margin_mode,
            fee_bps=self.settings.paper_taker_fee_bps,
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
        override_count = sum(1 for item in shadow_rows if item.would_override_baseline)
        agreement_count = sum(1 for item in shadow_rows if item.shadow_action_type == "same_as_baseline")
        disagreement_count = len(shadow_rows) - agreement_count
        fallback_count = 0
        for item in shadow_rows:
            assessment = self.latest_shadow_assessment(item.decision_id)
            if assessment is not None and assessment.fallback_used:
                fallback_count += 1
        baseline_replay = self._replay_shadow_path(shadow_rows=shadow_rows, use_shadow_targets=False)
        shadow_replay = self._replay_shadow_path(shadow_rows=shadow_rows, use_shadow_targets=True)
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
            },
        )
        self.evaluator.record_shadow_evaluation(evaluation)
        return evaluation, True

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
        return {
            "configured_operating_mode": self.settings.ai_operating_mode,
            "effective_operating_mode": self.effective_operating_mode(),
            "provider": self.settings.ai_provider,
            "configured": self.settings.ai_provider_configured,
            "degraded": self._degraded,
            "auto_downgrade_active": self._degraded and self.settings.ai_auto_downgrade_enabled,
            "degradation_reason": self._degradation_reason or None,
            "recovery_probe_after": self._recovery_probe_after,
            "recovery_probe_ready": self._recovery_probe_ready(),
            "consecutive_failures": self._consecutive_failures,
            "consecutive_successes": self._consecutive_successes,
            "shadow_mode_enabled": self.settings.ai_shadow_mode_enabled,
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
            operating_mode="ai_primary_shadow",
        )
        try:
            response = await asyncio.wait_for(
                self.provider.generate_assessment(
                    prompt=prompt,
                    response_schema=self.validator.output_schema(),
                ),
                timeout=self.settings.ai_timeout_seconds,
            )
            shadow_assessment = self.validator.validate_provider_output(
                raw_output=response.payload,
                brief=brief,
                context=context,
                baseline=baseline,
                operating_mode="ai_primary",
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
                operating_mode="ai_primary",
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
                await self._publish_degradation_event()

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._consecutive_successes += 1
        if self._degraded and self._consecutive_successes >= self.settings.ai_recover_after_successes:
            self._degraded = False
            self._degradation_reason = ""
            self._consecutive_successes = 0
            self._recovery_probe_after = None
        elif self._degraded:
            self._recovery_probe_after = utc_now() + timedelta(
                seconds=max(self.settings.ai_recovery_probe_interval_seconds, 0.0)
            )

    def _default_provider(self) -> AIProvider | None:
        if self.settings.ai_provider == "openai" and self.settings.ai_provider_configured:
            return OpenAIProvider(settings=self.settings)
        return None

    def effective_operating_mode(self) -> str:
        if self._degraded and self.settings.ai_auto_downgrade_enabled and not self._recovery_probe_ready():
            return "baseline_only"
        return self.settings.ai_operating_mode

    def should_attempt_assessment(self) -> bool:
        if self.settings.ai_operating_mode == "baseline_only":
            return False
        return self.effective_operating_mode() != "baseline_only"

    def _expected_slippage_proxy_bps(self) -> float:
        return max(self.settings.max_slippage_tolerance_bps, 0) * max(
            self.settings.strategy_expected_slippage_bps_fraction,
            0.0,
        )

    def _recovery_probe_ready(self) -> bool:
        if not self._degraded or not self.settings.ai_auto_downgrade_enabled:
            return False
        if self._recovery_probe_after is None:
            return True
        return utc_now() >= self._recovery_probe_after

    async def _publish_degradation_event(self) -> None:
        event = AIDegradationEvent(
            symbol=self.settings.default_symbol,
            timeframe=self.settings.primary_timeframe,
            product_type=self.settings.trading_product_type,
            margin_mode=self.settings.margin_mode,
            allowed_symbols=tuple(self.settings.allowed_symbols),
            configured_operating_mode=self.settings.ai_operating_mode,
            effective_operating_mode=self.effective_operating_mode(),
            degraded=True,
            auto_downgrade_active=self._degraded and self.settings.ai_auto_downgrade_enabled,
            reason_code=self._degradation_reason or "ai_provider_failed",
            consecutive_failures=self._consecutive_failures,
            consecutive_successes=self._consecutive_successes,
            recovery_probe_after=self._recovery_probe_after,
        )
        if self.bus is not None:
            await publish_model(
                bus=self.bus,
                topic=topics.AI_DEGRADATION_EVENTS,
                key=event.symbol,
                payload_model=event,
                source_component="ai_service",
            )
            return
        self.event_store.append(
            build_envelope(
                topic=topics.AI_DEGRADATION_EVENTS,
                key=event.symbol,
                payload_model=event,
                source_component="ai_service",
            )
        )

    def _replay_shadow_path(
        self,
        *,
        shadow_rows: list,
        use_shadow_targets: bool,
    ) -> dict[str, float]:
        current_qty = 0.0
        avg_entry_price = 0.0
        realized_gross_pnl = 0.0
        fee_total = 0.0
        trade_count = 0
        low_edge_trade_count = 0
        last_price = 0.0

        for row in shadow_rows:
            brief = self.latest_brief(row.decision_id)
            price = brief.last_price if brief is not None and brief.last_price > 0.0 else last_price
            if price <= 0.0:
                continue
            last_price = price
            target_qty = row.ai_shadow_target_qty if use_shadow_targets else row.baseline_target_qty
            delta_qty = target_qty - current_qty
            if abs(delta_qty) <= 1e-12:
                continue

            trade_count += 1
            fee = abs(delta_qty) * price * (self.settings.paper_taker_fee_bps / 10_000.0)
            fee_total += fee

            realized_trade_pnl, next_qty, next_avg_entry = self._apply_shadow_fill(
                current_qty=current_qty,
                avg_entry_price=avg_entry_price,
                target_qty=target_qty,
                execution_price=price,
            )
            realized_gross_pnl += realized_trade_pnl
            if abs(realized_trade_pnl) <= fee * 1.25:
                low_edge_trade_count += 1
            current_qty = next_qty
            avg_entry_price = next_avg_entry

        unrealized_pnl = current_qty * (last_price - avg_entry_price) if last_price > 0.0 else 0.0
        gross_pnl = realized_gross_pnl + unrealized_pnl
        net_pnl = gross_pnl - fee_total
        fee_ratio = abs(fee_total / gross_pnl) if abs(gross_pnl) > 1e-12 else None
        churn_ratio = (low_edge_trade_count / trade_count) if trade_count else 0.0
        return {
            "trade_count": float(trade_count),
            "gross_pnl": gross_pnl,
            "net_pnl": net_pnl,
            "fee_total": fee_total,
            "fee_ratio": fee_ratio,
            "churn_ratio": churn_ratio,
            "final_position_qty": current_qty,
        }

    @staticmethod
    def _apply_shadow_fill(
        *,
        current_qty: float,
        avg_entry_price: float,
        target_qty: float,
        execution_price: float,
    ) -> tuple[float, float, float]:
        delta_qty = target_qty - current_qty
        realized_trade_pnl = 0.0
        next_qty = current_qty
        next_avg = avg_entry_price

        if abs(current_qty) <= 1e-12:
            return 0.0, target_qty, execution_price if abs(target_qty) > 1e-12 else 0.0

        same_direction = current_qty * delta_qty > 0
        if same_direction:
            combined_qty = current_qty + delta_qty
            if abs(combined_qty) <= 1e-12:
                return 0.0, 0.0, 0.0
            weighted_notional = (current_qty * avg_entry_price) + (delta_qty * execution_price)
            next_avg = weighted_notional / combined_qty
            return 0.0, combined_qty, next_avg

        close_qty = min(abs(delta_qty), abs(current_qty))
        realized_trade_pnl += close_qty * (execution_price - avg_entry_price) * (1.0 if current_qty > 0 else -1.0)
        remaining_qty = current_qty + delta_qty
        if abs(remaining_qty) <= 1e-12:
            return realized_trade_pnl, 0.0, 0.0
        if current_qty * remaining_qty > 0:
            return realized_trade_pnl, remaining_qty, avg_entry_price
        return realized_trade_pnl, remaining_qty, execution_price
