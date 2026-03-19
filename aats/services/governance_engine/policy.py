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
from aats.services.governance_engine.runtime_layers import EnvironmentCapabilities, PolicyProfile


class PolicyEngine:
    def __init__(
        self,
        *,
        settings: AATSSettings,
        kill_switch: KillSwitch,
        mode_controller: RuntimeModeController,
        health_service: SystemHealthService,
        environment_capabilities: EnvironmentCapabilities | None = None,
        policy_profile: PolicyProfile | None = None,
    ) -> None:
        self.settings = settings
        self.kill_switch = kill_switch
        self.mode_controller = mode_controller
        self.health_service = health_service
        self.environment_capabilities = environment_capabilities or mode_controller.environment_capabilities
        self.policy_profile = policy_profile or mode_controller.policy_profile

    def evaluate(self, target: PositionTarget) -> PolicyDecision:
        mode = self.mode_controller.mode
        rejection_reasons: list[str] = []
        if target.symbol not in self.settings.allowed_symbols:
            rejection_reasons.append("symbol_not_allowed")
        if target.target_exposure_side == "short" and not self.policy_profile.shorting_allowed:
            rejection_reasons.append("shorting_not_supported")
        if target.target_leverage > 1.0 and not self.policy_profile.leverage_allowed:
            rejection_reasons.append("leverage_not_supported")
        if self.kill_switch.halted:
            rejection_reasons.append("kill_switch_active")
        if mode == "autonomous_live":
            rejection_reasons.append("autonomous_live_not_supported")

        if self.policy_profile.enforce_health_blockers:
            health_blockers = [
                *self.health_service.execution_blockers(),
                *self.health_service.submission_blockers(),
            ]
            rejection_reasons.extend(health_blockers)
        if (
            self.policy_profile.real_money_submission_structurally_blocked
            and self.environment_capabilities.exchange_submission_enabled
        ):
            rejection_reasons.append("real_money_live_not_supported")
        rejection_reasons = list(dict.fromkeys(rejection_reasons))

        allowed = not rejection_reasons
        execution_allowed = allowed
        submission_allowed = (
            execution_allowed
            and self.policy_profile.exchange_submission_allowed_in_principle
            and self.environment_capabilities.exchange_submission_enabled
            and not self.policy_profile.real_money_submission_structurally_blocked
        )
        dry_run_only = (
            execution_allowed
            and self.environment_capabilities.exchange_submission_possible
            and not submission_allowed
        )
        requires_human_approval = self.policy_profile.requires_human_approval and not submission_allowed

        return PolicyDecision(
            decision_id=target.decision_id,
            mode=mode,
            allowed=allowed,
            execution_allowed=execution_allowed,
            submission_allowed=submission_allowed,
            dry_run_only=dry_run_only,
            requires_human_approval=requires_human_approval,
            allowed_symbols=list(self.settings.allowed_symbols),
            allowed_execution_styles=["market", "limit"],
            max_notional_override=self.settings.max_notional_per_symbol,
            forced_degrade_mode="paper_live" if dry_run_only and self.environment_capabilities.exchange_coupled else None,
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
