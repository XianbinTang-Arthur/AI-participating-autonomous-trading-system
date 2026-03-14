from __future__ import annotations

from typing import Callable

from aats.bootstrap.settings import AATSSettings
from aats.bus.base import EventBus
from aats.events import topics
from aats.events.envelopes import publish_model
from aats.schemas.decision import PositionTarget
from aats.schemas.governance import RiskDecision
from aats.services.decision_engine.trigger_policy import DecisionTriggerPolicy
from aats.services.execution_engine.okx_account import OKXAccountService
from aats.services.governance_engine.health import SystemHealthService


class RiskEngine:
    def __init__(
        self,
        *,
        settings: AATSSettings,
        account_service: OKXAccountService,
        health_service: SystemHealthService,
        trigger_policy: DecisionTriggerPolicy,
        price_provider: Callable[[str], float],
    ) -> None:
        self.settings = settings
        self.account_service = account_service
        self.health_service = health_service
        self.trigger_policy = trigger_policy
        self.price_provider = price_provider

    def evaluate(self, target: PositionTarget) -> RiskDecision:
        max_abs_qty = self.settings.max_abs_position_qty
        max_notional = self.settings.max_notional_per_symbol
        capped_qty = max(min(target.target_position_qty, max_abs_qty), -max_abs_qty)
        constraints_applied: list[str] = []
        if capped_qty != target.target_position_qty:
            constraints_applied.append("max_abs_qty")

        mark_price = self.price_provider(target.symbol)
        target_notional = abs(capped_qty) * mark_price
        if target_notional > max_notional and abs(target.target_position_qty) > 1e-12:
            notional_scale = max_notional / target_notional
            capped_qty *= notional_scale
            target_notional = max_notional
            constraints_applied.append("max_notional_per_symbol")

        current_open_order_count = self.account_service.open_order_count(symbol=target.symbol)
        approved = True
        rejection_reasons: list[str] = []
        if current_open_order_count >= self.settings.max_open_orders:
            approved = False
            rejection_reasons.append("max_open_orders_reached")

        if self.trigger_policy.decision_count_last_minute(
            symbol=target.symbol,
            timeframe=self.settings.primary_timeframe,
        ) >= self.settings.max_decisions_per_minute:
            approved = False
            rejection_reasons.append("max_decision_frequency_reached")

        if self.settings.execution_backend == "okx":
            health_blockers = self.health_service.execution_blockers()
            if health_blockers:
                approved = False
                rejection_reasons.extend(health_blockers)

        modified = bool(constraints_applied)
        risk_score = min(abs(capped_qty) / max_abs_qty, 1.0) if max_abs_qty else 0.0
        halt_required = any(reason.endswith("_halt_required") for reason in rejection_reasons)
        return RiskDecision(
            decision_id=target.decision_id,
            approved=approved,
            modified=modified,
            capped_target_position_qty=capped_qty,
            capped_target_notional=target_notional,
            current_open_order_count=current_open_order_count,
            constraints_applied=constraints_applied,
            risk_score=risk_score,
            flatten_required=False,
            halt_required=halt_required,
            rejection_reasons=rejection_reasons,
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
