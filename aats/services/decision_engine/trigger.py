from __future__ import annotations

import asyncio
from collections.abc import Callable

from aats.bootstrap.logging import get_logger, log_event
from aats.events.envelopes import parse_payload
from aats.schemas.features import FeatureSnapshot
from aats.services.decision_engine.trigger_policy import DecisionTriggerPolicy
from aats.services.decision_engine.orchestrator import DecisionOrchestrator
from aats.services.market_gateway.gateway import MarketDataGateway

CanTriggerCheck = Callable[..., tuple[bool, str]]


class DecisionCycleTrigger:
    def __init__(
        self,
        *,
        orchestrator: DecisionOrchestrator,
        market_gateway: MarketDataGateway,
        policy: DecisionTriggerPolicy,
        can_trigger: CanTriggerCheck | None = None,
    ) -> None:
        self.orchestrator = orchestrator
        self.market_gateway = market_gateway
        self.policy = policy
        self.can_trigger = can_trigger
        self.logger = get_logger("aats.decision_trigger")
        self._timeframe_locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def handle_feature_snapshot(self, message: dict) -> None:
        snapshot = parse_payload(message, FeatureSnapshot)
        if self.can_trigger is not None:
            allowed, _reason = self.can_trigger(symbol=snapshot.symbol)
            if not allowed:
                return
        for timeframe in self.policy.enabled_timeframes():
            lock = self._timeframe_locks.setdefault((snapshot.symbol, timeframe), asyncio.Lock())
            async with lock:
                if self.can_trigger is not None:
                    allowed, _reason = self.can_trigger(symbol=snapshot.symbol)
                    if not allowed:
                        continue
                current_market_snapshot = self.market_gateway.latest_snapshot(snapshot.symbol)
                should_trigger, _reason = self.policy.should_trigger(
                    feature_snapshot=snapshot,
                    market_snapshot=current_market_snapshot,
                    timeframe=timeframe,
                )
                if not should_trigger or current_market_snapshot is None:
                    continue
                try:
                    await self.orchestrator.run_cycle(symbol=snapshot.symbol, timeframe=timeframe)
                except Exception as exc:
                    log_event(
                        self.logger,
                        "decision_cycle_failed",
                        level="error",
                        symbol=snapshot.symbol,
                        timeframe=timeframe,
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
                    continue
                self.policy.record_trigger(
                    feature_snapshot=snapshot,
                    market_snapshot=current_market_snapshot,
                    timeframe=timeframe,
                )
