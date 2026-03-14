from __future__ import annotations

from aats.bootstrap.settings import AATSSettings
from aats.bus.base import EventBus
from aats.events import topics
from aats.events.envelopes import publish_model
from aats.schemas.decision import PositionTarget
from aats.schemas.governance import RiskDecision


class RiskEngine:
    def __init__(self, *, settings: AATSSettings) -> None:
        self.settings = settings

    def evaluate(self, target: PositionTarget) -> RiskDecision:
        max_abs_qty = self.settings.max_abs_position_qty
        capped_qty = max(min(target.target_position_qty, max_abs_qty), -max_abs_qty)
        modified = capped_qty != target.target_position_qty
        return RiskDecision(
            decision_id=target.decision_id,
            approved=True,
            modified=modified,
            capped_target_position_qty=capped_qty,
            constraints_applied=["max_abs_qty"] if modified else [],
            risk_score=min(abs(capped_qty) / max_abs_qty, 1.0) if max_abs_qty else 0.0,
            flatten_required=False,
            halt_required=False,
            rejection_reasons=[],
        )

    async def publish_decision(
        self,
        *,
        bus: EventBus,
        target: PositionTarget,
        decision: RiskDecision,
    ) -> None:
        await publish_model(
            bus=bus,
            topic=topics.RISK_DECISIONS,
            key=target.symbol,
            payload_model=decision,
            source_component="governance_engine",
        )

