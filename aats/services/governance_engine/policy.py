from __future__ import annotations

from aats.bootstrap.settings import AATSSettings
from aats.bus.base import EventBus
from aats.events import topics
from aats.events.envelopes import publish_model
from aats.schemas.decision import PositionTarget
from aats.schemas.governance import PolicyDecision
from aats.services.governance_engine.kill_switch import KillSwitch


class PolicyEngine:
    def __init__(self, *, settings: AATSSettings, kill_switch: KillSwitch) -> None:
        self.settings = settings
        self.kill_switch = kill_switch

    def evaluate(self, mode: str, target: PositionTarget) -> PolicyDecision:
        allowed = target.symbol in self.settings.allowed_symbols and not self.kill_switch.halted
        rejection_reasons: list[str] = []
        if target.symbol not in self.settings.allowed_symbols:
            rejection_reasons.append("symbol_not_allowed")
        if self.kill_switch.halted:
            rejection_reasons.append("kill_switch_active")

        return PolicyDecision(
            decision_id=target.decision_id,
            mode=mode,
            allowed=allowed,
            requires_human_approval=False,
            allowed_symbols=list(self.settings.allowed_symbols),
            allowed_execution_styles=["market", "limit"],
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

