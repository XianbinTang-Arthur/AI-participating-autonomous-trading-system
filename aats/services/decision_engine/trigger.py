from __future__ import annotations

from aats.events.envelopes import parse_payload
from aats.schemas.features import FeatureSnapshot
from aats.services.decision_engine.orchestrator import DecisionOrchestrator


class DecisionCycleTrigger:
    def __init__(self, *, orchestrator: DecisionOrchestrator, timeframe: str) -> None:
        self.orchestrator = orchestrator
        self.timeframe = timeframe

    async def handle_feature_snapshot(self, message: dict) -> None:
        snapshot = parse_payload(message, FeatureSnapshot)
        await self.orchestrator.run_cycle(symbol=snapshot.symbol, timeframe=self.timeframe)

