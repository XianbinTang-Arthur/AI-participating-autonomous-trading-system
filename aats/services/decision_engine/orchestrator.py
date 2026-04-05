from __future__ import annotations

from typing import TYPE_CHECKING

from aats.bootstrap.logging import correlation_fields, get_logger, log_event
from aats.bootstrap.metrics import MetricsRegistry
from aats.bus.base import EventBus
from aats.events import topics
from aats.events.envelopes import publish_model
from aats.schemas.common import new_id
from aats.schemas.decision import PositionTarget
from aats.services.ai_service.inference import AIInferenceService
from aats.services.decision_engine.baseline import BaselineStrategy
from aats.services.decision_engine.context_builder import DecisionContextBuilder
from aats.services.decision_engine.target_position import TargetPositionEngine
from aats.services.strategy_engines.overlay_parent_exposure import overlay_parent_exposure_record

if TYPE_CHECKING:
    from aats.services.operator.strategy_profiles import StrategyProfileControlService
    from aats.services.strategy_engines.coordinator import StrategyCoordinatorService


class DecisionOrchestrator:
    def __init__(
        self,
        *,
        bus: EventBus,
        context_builder: DecisionContextBuilder,
        baseline_strategy: BaselineStrategy,
        ai_service: AIInferenceService,
        target_engine: TargetPositionEngine,
        strategy_profile_service: StrategyProfileControlService | None = None,
        strategy_coordinator: StrategyCoordinatorService | None = None,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        self.bus = bus
        self.context_builder = context_builder
        self.baseline_strategy = baseline_strategy
        self.ai_service = ai_service
        self.target_engine = target_engine
        self.strategy_profile_service = strategy_profile_service
        self.strategy_coordinator = strategy_coordinator
        self.metrics = metrics
        self.logger = get_logger("aats.decision_engine")

    async def run_cycle(self, symbol: str, timeframe: str) -> PositionTarget:
        decision_id = new_id("decision")
        health_snapshot = self.context_builder.build_health_snapshot(decision_id=decision_id)
        health_envelope = await publish_model(
            bus=self.bus,
            topic=topics.HEALTH_SNAPSHOTS,
            key=symbol,
            payload_model=health_snapshot,
            source_component="governance_engine",
        )
        context = self.context_builder.build(
            symbol=symbol,
            timeframe=timeframe,
            decision_id=decision_id,
            health_snapshot_ref=health_envelope.event_id,
        )
        log_event(
            self.logger,
            "decision_cycle_started",
            **correlation_fields(
                decision_id=context.decision_id,
                symbol=symbol,
                timeframe=timeframe,
            ),
        )
        baseline = self.baseline_strategy.evaluate(context)
        await publish_model(
            bus=self.bus,
            topic=topics.DECISION_CONTEXTS,
            key=symbol,
            payload_model=context,
            source_component="decision_engine",
        )
        await publish_model(
            bus=self.bus,
            topic=topics.BASELINE_ASSESSMENTS,
            key=symbol,
            payload_model=baseline,
            source_component="decision_engine",
        )
        operating_mode = self.ai_service.effective_operating_mode()
        profile_control_decision = None
        ai_assessment = None
        if self.ai_service.should_attempt_assessment():
            ai_assessment = await self.ai_service.assess(context=context, baseline=baseline)
            operating_mode = self.ai_service.effective_operating_mode()
        canonical_mode = self.ai_service.canonical_effective_operating_mode()
        if (
            self.ai_service.settings.strategy_profile_auto_control_is_enabled_for_mode(canonical_mode)
            and self.strategy_profile_service is not None
        ):
            profile_control_decision = await self.strategy_profile_service.evaluate_mainline_profile_control(
                decision_id=context.decision_id,
            )
        ai_decision_intent = self.target_engine.build_ai_decision_intent(
            context=context,
            baseline=baseline,
            ai_assessment=ai_assessment,
            operating_mode=operating_mode,
        )
        if ai_decision_intent is not None and profile_control_decision is not None:
            ai_decision_intent = ai_decision_intent.model_copy(
                update={
                    "requested_profile_id": profile_control_decision.requested_profile_id,
                    "requested_profile_reason_codes": list(profile_control_decision.decision_reason_codes),
                }
            )
        target = self.target_engine.build(
            context,
            baseline,
            ai_assessment,
            ai_decision_intent,
            profile_control_decision=profile_control_decision,
            operating_mode=operating_mode,
        )
        if self.strategy_coordinator is not None:
            strategy_snapshot = self.strategy_coordinator.evaluate(
                context=context,
                baseline=baseline,
                directional_target=target,
                ai_assessment=ai_assessment,
            )
            strategy_envelope = await publish_model(
                bus=self.bus,
                topic=topics.STRATEGY_COORDINATOR_SNAPSHOTS,
                key=symbol,
                payload_model=strategy_snapshot,
                source_component="decision_engine",
            )
            for sleeve_intent in strategy_snapshot.sleeve_intents:
                await publish_model(
                    bus=self.bus,
                    topic=topics.STRATEGY_SLEEVE_INTENTS,
                    key=symbol,
                    payload_model=sleeve_intent,
                    source_component="decision_engine",
                )
            if strategy_snapshot.allocation_decision is not None:
                await publish_model(
                    bus=self.bus,
                    topic=topics.PORTFOLIO_ALLOCATION_DECISIONS,
                    key=symbol,
                    payload_model=strategy_snapshot.allocation_decision,
                    source_component="decision_engine",
                )
            target = self.strategy_coordinator.apply_selected_target(
                base_target=target,
                snapshot=strategy_snapshot,
                snapshot_ref=strategy_envelope.event_id,
            )
        brief = None if ai_assessment is None else self.ai_service.latest_brief(context.decision_id)
        shadow_assessment = None if ai_assessment is None else self.ai_service.latest_shadow_assessment(context.decision_id)
        if brief is not None:
            await publish_model(
                bus=self.bus,
                topic=topics.AI_DECISION_BRIEFS,
                key=symbol,
                payload_model=brief,
                source_component="ai_service",
            )
        if ai_assessment is not None:
            await publish_model(
                bus=self.bus,
                topic=topics.AI_ASSESSMENTS,
                key=symbol,
                payload_model=ai_assessment,
                source_component="ai_service",
            )
        if shadow_assessment is not None:
            shadow_decision = self.target_engine.build_shadow(
                context=context,
                baseline=baseline,
                ai_assessment=shadow_assessment,
                actual_target=target,
                operating_mode=operating_mode,
            )
            self.ai_service.record_shadow_decision(shadow_decision)
            await publish_model(
                bus=self.bus,
                topic=topics.AI_SHADOW_DECISIONS,
                key=symbol,
                payload_model=shadow_decision,
                source_component="decision_engine",
            )
        position_target_envelope = await publish_model(
            bus=self.bus,
            topic=topics.POSITION_TARGETS,
            key=symbol,
            payload_model=target,
            source_component="decision_engine",
        )
        overlay_parent_record = overlay_parent_exposure_record(
            decision_id=target.decision_id,
            product_type=target.product_type,
            strategy_family=target.strategy_family,
            strategy_sleeve_id=target.strategy_sleeve_id,
            allocation_id=target.allocation_id,
            source_stage="position_target",
            source_ref=position_target_envelope.event_id,
            parent_exposure=target.overlay_parent_exposure,
        )
        if overlay_parent_record is not None:
            await publish_model(
                bus=self.bus,
                topic=topics.OVERLAY_PARENT_EXPOSURES,
                key=symbol,
                payload_model=overlay_parent_record,
                source_component="decision_engine",
            )
        if shadow_assessment is not None:
            await self._publish_shadow_evaluation_best_effort(
                decision_id=context.decision_id,
                symbol=symbol,
            )
        if self.metrics is not None:
            self.metrics.increment("decision_cycles")
        log_event(
            self.logger,
            "decision_cycle_completed",
            **correlation_fields(
                decision_id=context.decision_id,
                symbol=symbol,
                target_position_qty=target.target_position_qty,
                delta_position_qty=target.delta_position_qty,
            ),
        )
        return target

    async def _publish_shadow_evaluation_best_effort(
        self,
        *,
        decision_id: str,
        symbol: str,
    ) -> None:
        try:
            shadow_evaluation, created = self.ai_service.evaluate_shadow_window(
                limit=self.ai_service.settings.ai_shadow_evaluation_window
            )
            if not created or shadow_evaluation is None:
                return
            envelope = await publish_model(
                bus=self.bus,
                topic=topics.AI_SHADOW_EVALUATIONS,
                key=symbol,
                payload_model=shadow_evaluation,
                source_component="decision_engine",
            )
            self.ai_service.publish_shadow_performance_report(
                evaluation=shadow_evaluation,
                latest_evaluation_ref=envelope.event_id,
            )
        except Exception as exc:
            log_event(
                self.logger,
                "shadow_evaluation_failed",
                level="warning",
                **correlation_fields(
                    decision_id=decision_id,
                    symbol=symbol,
                    error=str(exc),
                ),
            )
