from __future__ import annotations

from aats.events.envelopes import parse_payload
from aats.schemas.features import FeatureSnapshot
from aats.services.decision_engine.trigger_policy import DecisionTriggerPolicy
from aats.services.decision_engine.orchestrator import DecisionOrchestrator
from aats.services.market_gateway.gateway import MarketDataGateway


class DecisionCycleTrigger:
    def __init__(
        self,
        *,
        orchestrator: DecisionOrchestrator,
        market_gateway: MarketDataGateway,
        policy: DecisionTriggerPolicy,
    ) -> None:
        self.orchestrator = orchestrator
        self.market_gateway = market_gateway
        self.policy = policy

    async def handle_feature_snapshot(self, message: dict) -> None:
        snapshot = parse_payload(message, FeatureSnapshot)
        market_snapshot = self.market_gateway.latest_snapshot(snapshot.symbol)
        for timeframe in self.policy.enabled_timeframes():
            should_trigger, _reason = self.policy.should_trigger(
                feature_snapshot=snapshot,
                market_snapshot=market_snapshot,
                timeframe=timeframe,
            )
            if not should_trigger or market_snapshot is None:
                continue
            await self.orchestrator.run_cycle(symbol=snapshot.symbol, timeframe=timeframe)
            self.policy.record_trigger(
                feature_snapshot=snapshot,
                market_snapshot=market_snapshot,
                timeframe=timeframe,
            )
