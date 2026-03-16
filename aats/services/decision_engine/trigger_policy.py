from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime

from aats.bootstrap.settings import AATSSettings
from aats.schemas.features import FeatureSnapshot
from aats.schemas.market import MarketSnapshot
from aats.schemas.common import utc_now


@dataclass(slots=True)
class TriggerState:
    last_trigger_ts: datetime | None = None
    last_market_snapshot_ts: datetime | None = None
    last_price: float | None = None
    last_momentum_score: float | None = None
    last_regime: str | None = None


class DecisionTriggerPolicy:
    def __init__(self, *, settings: AATSSettings) -> None:
        self.settings = settings
        self._state: dict[tuple[str, str], TriggerState] = {}
        self._decision_times: dict[tuple[str, str], deque] = defaultdict(deque)

    def enabled_timeframes(self) -> tuple[str, ...]:
        return tuple(self.settings.enabled_decision_timeframes)

    def should_trigger(
        self,
        *,
        feature_snapshot: FeatureSnapshot,
        market_snapshot: MarketSnapshot | None,
        timeframe: str,
    ) -> tuple[bool, str]:
        if market_snapshot is None:
            return False, "missing_market_snapshot"
        if (utc_now() - market_snapshot.snapshot_ts).total_seconds() > self.settings.market_data_stale_after_seconds:
            return False, "market_stale"

        state_key = (feature_snapshot.symbol, timeframe)
        state = self._state.get(state_key)
        if state is None:
            return True, "initial_decision"

        if state.last_market_snapshot_ts == market_snapshot.snapshot_ts:
            return False, "duplicate_market_snapshot"

        decision_times = self._decision_times[state_key]
        self._prune_decision_times(decision_times=decision_times, reference_ts=market_snapshot.snapshot_ts)
        if len(decision_times) >= self.settings.max_decisions_per_minute:
            return False, "max_decision_frequency_reached"

        min_interval = self._min_interval_seconds(timeframe)
        seconds_since_trigger = (
            (market_snapshot.snapshot_ts - state.last_trigger_ts).total_seconds()
            if state.last_trigger_ts is not None
            else None
        )
        material_change = self._material_change(
            state=state,
            feature_snapshot=feature_snapshot,
            market_snapshot=market_snapshot,
        )
        if seconds_since_trigger is None:
            return True, "first_trigger"
        if seconds_since_trigger >= min_interval:
            return True, "cadence_elapsed"
        if material_change:
            return True, "material_change"
        return False, "suppressed_duplicate"

    def record_trigger(
        self,
        *,
        feature_snapshot: FeatureSnapshot,
        market_snapshot: MarketSnapshot,
        timeframe: str,
    ) -> None:
        state_key = (feature_snapshot.symbol, timeframe)
        self._state[state_key] = TriggerState(
            last_trigger_ts=market_snapshot.snapshot_ts,
            last_market_snapshot_ts=market_snapshot.snapshot_ts,
            last_price=market_snapshot.last_price,
            last_momentum_score=feature_snapshot.momentum_score,
            last_regime=feature_snapshot.regime_indicator,
        )
        decision_times = self._decision_times[state_key]
        self._prune_decision_times(decision_times=decision_times, reference_ts=market_snapshot.snapshot_ts)
        decision_times.append(market_snapshot.snapshot_ts)

    def decision_count_last_minute(self, *, symbol: str, timeframe: str) -> int:
        state_key = (symbol, timeframe)
        decision_times = self._decision_times.get(state_key)
        return len(decision_times or ())

    def latest_reasonable_market_ts(self, *, symbol: str, timeframe: str):
        state = self._state.get((symbol, timeframe))
        return state.last_market_snapshot_ts if state is not None else None

    def _material_change(
        self,
        *,
        state: TriggerState,
        feature_snapshot: FeatureSnapshot,
        market_snapshot: MarketSnapshot,
    ) -> bool:
        if state.last_price is None:
            return True
        if state.last_regime != feature_snapshot.regime_indicator:
            return True
        if state.last_momentum_score is not None and (
            abs(feature_snapshot.momentum_score - state.last_momentum_score)
            >= self.settings.decision_min_momentum_delta
        ):
            return True
        if state.last_price == 0.0:
            return False
        price_move_bps = abs((market_snapshot.last_price - state.last_price) / state.last_price) * 10_000.0
        return price_move_bps >= self.settings.decision_min_price_move_bps

    def _min_interval_seconds(self, timeframe: str) -> float:
        if timeframe == "15m":
            return self.settings.decision_min_interval_seconds_15m
        if timeframe == "1h":
            return self.settings.decision_min_interval_seconds_1h
        return 0.0

    @staticmethod
    def _prune_decision_times(*, decision_times: deque, reference_ts: datetime) -> None:
        while decision_times and (reference_ts - decision_times[0]).total_seconds() > 60.0:
            decision_times.popleft()
