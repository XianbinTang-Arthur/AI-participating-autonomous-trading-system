from __future__ import annotations

import asyncio
from collections.abc import Callable

from aats.bootstrap.logging import get_logger, log_event
from aats.events.envelopes import parse_envelope, parse_payload
from aats.schemas.features import FeatureSnapshot
from aats.services.decision_engine.trigger_policy import DecisionTriggerPolicy
from aats.services.decision_engine.orchestrator import DecisionOrchestrator
from aats.services.market_gateway.gateway import MarketDataGateway

CanTriggerCheck = Callable[..., tuple[bool, str]]


class DecisionCycleTrigger:
    # 连续失败后退避，避免堵死 asyncio 事件循环（冷启动时 feature store 为空）
    _BACKOFF_INITIAL_S = 2.0
    _BACKOFF_MAX_S = 30.0

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
        self._consecutive_failures: dict[tuple[str, str], int] = {}

    async def handle_feature_snapshot(self, message: dict) -> None:
        # R3-P1-U-A：同时保留触发本次 cycle 的 feature envelope（parse_envelope 得到
        # 完整 EventEnvelope，含 event_id / event_timestamp），向下游 run_cycle 透传。
        # 保证 DecisionContext.feature_snapshot_ref = 本 envelope.event_id，与
        # trigger_policy 评估依据的 snapshot 严格一致，消除触发与构建之间新
        # snapshot 抢跑导致的 ref 漂移。
        feature_envelope = parse_envelope(message)
        snapshot = FeatureSnapshot.model_validate(feature_envelope.payload)
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
                fail_key = (snapshot.symbol, timeframe)
                try:
                    await self.orchestrator.run_cycle(
                        symbol=snapshot.symbol,
                        timeframe=timeframe,
                        feature_snapshot_hint=feature_envelope,
                    )
                except Exception as exc:
                    n = self._consecutive_failures.get(fail_key, 0) + 1
                    self._consecutive_failures[fail_key] = n
                    backoff = min(self._BACKOFF_INITIAL_S * n, self._BACKOFF_MAX_S)
                    log_event(
                        self.logger,
                        "decision_cycle_failed",
                        level="warning" if n > 1 else "error",
                        symbol=snapshot.symbol,
                        timeframe=timeframe,
                        error_type=type(exc).__name__,
                        error=str(exc),
                        consecutive_failures=n,
                        backoff_s=backoff,
                    )
                    await asyncio.sleep(backoff)
                    continue
                # 成功后重置退避计数
                self._consecutive_failures.pop(fail_key, None)
                self.policy.record_trigger(
                    feature_snapshot=snapshot,
                    market_snapshot=current_market_snapshot,
                    timeframe=timeframe,
                )
