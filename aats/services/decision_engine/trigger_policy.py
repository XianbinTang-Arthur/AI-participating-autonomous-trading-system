from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.schemas.features import FeatureSnapshot, RegimeIndicator
from aats.schemas.market import MarketSnapshot
from aats.schemas.common import utc_now
from aats.services.portfolio_service.decimals import to_decimal


@dataclass(slots=True)
class TriggerState:
    last_trigger_ts: datetime | None = None
    last_market_snapshot_ts: datetime | None = None
    last_price: Decimal | None = None
    last_momentum_score: float | None = None
    last_regime: RegimeIndicator | None = None


class DecisionTriggerPolicy:
    def __init__(self, *, settings: AATSSettings) -> None:
        self.settings = settings
        self._state: dict[tuple[str, str], TriggerState] = {}
        _deque_maxlen = settings.max_decisions_per_minute * 2
        self._decision_times: dict[tuple[str, str], deque[datetime]] = defaultdict(
            lambda: deque(maxlen=_deque_maxlen)
        )

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

        # R3-P1-U-B：与 market_gateway.apply_remote_snapshot 的接收语义保持
        # 一致（gateway 用 `<` 判断严格更旧才拒收）。OKX 可能在同一 ms 发两笔
        # ticker（不同 bid/ask），gateway 接受并覆盖本地 snapshot——这里如果
        # 还用 `==` 把同 ts 视为重复就会把合法的新内容抹掉。改成 `<`：严格
        # 更旧（reorder/replay）才 early-reject；同 ts 走下面的 material_change
        # 分支，由内容变化判定是否需要触发新 decision。真正重复的消息内容
        # 也相同 → material_change=False → 最终走 suppressed_duplicate，结果
        # 等价；但同 ms 新内容不再被误杀。
        if (
            state.last_market_snapshot_ts is not None
            and market_snapshot.snapshot_ts < state.last_market_snapshot_ts
        ):
            return False, "out_of_order_market_snapshot"

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

    def latest_reasonable_market_ts(self, *, symbol: str, timeframe: str) -> datetime | None:
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
        if state.last_price == Decimal("0"):
            return False
        price_move_bps = abs((market_snapshot.last_price - state.last_price) / state.last_price) * Decimal("10000")
        return price_move_bps >= to_decimal(self.settings.decision_min_price_move_bps)

    def _min_interval_seconds(self, timeframe: str) -> float:
        if timeframe == "15m":
            return self.settings.decision_min_interval_seconds_15m
        if timeframe == "1h":
            return self.settings.decision_min_interval_seconds_1h
        return 0.0

    @staticmethod
    def _prune_decision_times(*, decision_times: deque[datetime], reference_ts: datetime) -> None:
        while decision_times and (reference_ts - decision_times[0]).total_seconds() > 60.0:
            decision_times.popleft()
