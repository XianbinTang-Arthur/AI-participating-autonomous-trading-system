from __future__ import annotations

from aats.bootstrap.logging import correlation_fields, get_logger, log_event
from aats.bootstrap.metrics import MetricsRegistry
from aats.bus.base import EventBus
from aats.events import topics
from aats.events.envelopes import publish_model
from aats.schemas.ai_shadow import AITakeoverDecision
from aats.schemas.common import new_id
from aats.schemas.decision import PositionTarget
from aats.services.ai_service.inference import AIInferenceService
from aats.services.decision_engine.baseline import BaselineStrategy
from aats.services.decision_engine.context_builder import DecisionContextBuilder
from aats.services.decision_engine.target_position import TargetPositionEngine


class DecisionOrchestrator:
    def __init__(
        self,
        *,
        bus: EventBus,
        context_builder: DecisionContextBuilder,
        baseline_strategy: BaselineStrategy,
        ai_service: AIInferenceService,
        target_engine: TargetPositionEngine,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        self.bus = bus
        self.context_builder = context_builder
        self.baseline_strategy = baseline_strategy
        self.ai_service = ai_service
        self.target_engine = target_engine
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
        operating_mode = self.ai_service.effective_operating_mode()
        ai_assessment = None
        if self.ai_service.should_attempt_assessment():
            ai_assessment = await self.ai_service.assess(context=context, baseline=baseline)
            operating_mode = self.ai_service.effective_operating_mode()
        target = self.target_engine.build(
            context,
            baseline,
            ai_assessment,
            operating_mode=operating_mode,
        )
        brief = None if ai_assessment is None else self.ai_service.latest_brief(context.decision_id)
        shadow_assessment = None if ai_assessment is None else self.ai_service.latest_shadow_assessment(context.decision_id)

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
            await publish_model(
                bus=self.bus,
                topic=topics.AI_TAKEOVER_DECISIONS,
                key=symbol,
                payload_model=AITakeoverDecision(
                    decision_id=context.decision_id,
                    symbol=symbol,
                    timeframe=timeframe,
                    ai_takeover_allowed=target.ai_takeover_allowed,
                    ai_takeover_applied=target.ai_takeover_applied,
                    ai_takeover_blockers=list(target.ai_takeover_blockers),
                    baseline_direction=baseline.direction_bias,
                    ai_direction="long" if ai_assessment.directional_edge > 0 else "short" if ai_assessment.directional_edge < 0 else "flat",
                    final_direction=target.target_exposure_side,
                ),
                source_component="decision_engine",
            )
        if shadow_assessment is not None:
            shadow_decision = self.target_engine.build_shadow(
                context=context,
                baseline=baseline,
                ai_assessment=shadow_assessment,
                actual_target=target,
            )
            self.ai_service.record_shadow_decision(shadow_decision)
            await publish_model(
                bus=self.bus,
                topic=topics.AI_SHADOW_DECISIONS,
                key=symbol,
                payload_model=shadow_decision,
                source_component="decision_engine",
            )
        await publish_model(
            bus=self.bus,
            topic=topics.POSITION_TARGETS,
            key=symbol,
            payload_model=target,
            source_component="decision_engine",
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
