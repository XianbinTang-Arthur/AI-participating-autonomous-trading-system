from __future__ import annotations

from aats.bootstrap.settings import AATSSettings
from aats.bus.base import EventBus
from aats.events import topics
from aats.events.envelopes import publish_model
from aats.schemas.decision import PositionTarget
from aats.schemas.governance import PolicyDecision
from aats.services.governance_engine.health import SystemHealthService
from aats.services.governance_engine.kill_switch import KillSwitch
from aats.services.governance_engine.mode import RuntimeModeController


class PolicyEngine:
    def __init__(
        self,
        *,
        settings: AATSSettings,
        kill_switch: KillSwitch,
        mode_controller: RuntimeModeController,
        health_service: SystemHealthService,
    ) -> None:
        self.settings = settings
        self.kill_switch = kill_switch
        self.mode_controller = mode_controller
        self.health_service = health_service

    def evaluate(self, target: PositionTarget) -> PolicyDecision:
        mode = self.mode_controller.mode
        rejection_reasons: list[str] = []
        if target.symbol not in self.settings.allowed_symbols:
            rejection_reasons.append("symbol_not_allowed")
        if self.kill_switch.halted:
            rejection_reasons.append("kill_switch_active")
        if mode == "autonomous_live":
            rejection_reasons.append("autonomous_live_not_supported")

        health_blockers = []
        if self.settings.execution_backend == "okx":
            health_blockers = self.health_service.execution_blockers()
            rejection_reasons.extend(health_blockers)

        allowed = not rejection_reasons
        execution_allowed = allowed
        submission_allowed = (
            execution_allowed
            and self.settings.execution_backend == "okx"
            and mode == "guarded_live"
            and self.settings.live_submit_enabled
            and not self.settings.guarded_execution_dry_run
        )
        dry_run_only = (
            execution_allowed
            and self.settings.execution_backend == "okx"
            and not submission_allowed
        )

        return PolicyDecision(
            decision_id=target.decision_id,
            mode=mode,
            allowed=allowed,
            execution_allowed=execution_allowed,
            submission_allowed=submission_allowed,
            dry_run_only=dry_run_only,
            requires_human_approval=self.settings.execution_backend == "okx" and not submission_allowed,
            allowed_symbols=list(self.settings.allowed_symbols),
            allowed_execution_styles=["market", "limit"],
            max_notional_override=self.settings.max_notional_per_symbol,
            forced_degrade_mode="paper_live" if dry_run_only else None,
            rejection_reasons=rejection_reasons,
        )

    async def publish_decision(
        self,
        *,
        bus: EventBus,
        target: PositionTarget,
        decision: PolicyDecision,
    ) -> None:
        await publish_model(
            bus=bus,
            topic=topics.POLICY_DECISIONS,
            key=target.symbol,
            payload_model=decision,
            source_component="governance_engine",
        )
