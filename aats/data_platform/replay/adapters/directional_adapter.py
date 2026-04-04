"""Directional family replay adapter (minimal).

Phase 2 设计决策 §9.4：
- directional 接口必须能接，尽快适配
- 架构上两个 family 都要能接

本 adapter 实现 directional 策略的最小 replay 能力：
- 基于趋势方向做多/做空
- 使用 SMA crossover 作为简化信号
- 同样受 min_confirm_ticks / min_safe_net_edge_bps 约束

后续阶段可替换为更复杂的 directional 评估逻辑。
"""

from __future__ import annotations

import math
from collections import deque
from decimal import Decimal

from aats.data_platform.replay.adapters.base_adapter import BaseReplayAdapter
from aats.data_platform.replay.core.replay_context import (
    ReplayBar,
    ReplayBarContext,
    ReplayDecision,
    ReplayState,
)

_FAST_PERIOD = 5
_SLOW_PERIOD = 20
_ENTRY_THRESHOLD = 0.45
_CLOSE_THRESHOLD = 0.20
_DEFAULT_COST_BPS = 0.8


class DirectionalReplayAdapter(BaseReplayAdapter):
    """Directional 策略的最小 replay adapter。

    评估逻辑：
    - 快慢 SMA 交叉确定方向
    - bar return 确认强度
    - 应用 min_confirm_ticks 和 min_safe_net_edge_bps 门槛
    """

    def __init__(self) -> None:
        self._close_history: deque[float] = deque(maxlen=_SLOW_PERIOD + 5)
        self._score_history: deque[float] = deque(maxlen=20)

    @property
    def family_name(self) -> str:
        return "directional"

    def reset_state(self) -> ReplayState:
        self._close_history.clear()
        self._score_history.clear()
        return ReplayState()

    def evaluate_bar(self, ctx: ReplayBarContext) -> ReplayDecision:
        bar = ctx.bar
        params = ctx.params
        state = ctx.state

        close_f = float(bar.close)
        self._close_history.append(close_f)

        # 计算分数
        long_score, short_score = self._compute_scores()
        dominant_leg = "long" if long_score >= short_score else "short"
        dominant_score = max(long_score, short_score)
        self._score_history.append(dominant_score)

        # 稳定性检查
        score_stable = self._check_stability(params.min_confirm_ticks, params.score_stability_threshold)

        # 预期边际
        expected_net_edge_bps = self._compute_edge(bar, dominant_leg)

        # 资格评估
        blocking_reasons: list[str] = []
        if dominant_score < _ENTRY_THRESHOLD:
            blocking_reasons.append("score_below_entry_threshold")
        if not score_stable:
            blocking_reasons.append("score_not_stable")
        if expected_net_edge_bps < params.min_safe_net_edge_bps:
            blocking_reasons.append("net_edge_below_safe_minimum")

        selectable = dominant_score >= _ENTRY_THRESHOLD
        execution_compatible = selectable and score_stable and (
            expected_net_edge_bps >= params.min_safe_net_edge_bps
        )

        # 状态机
        action, new_state = self._advance_state(state, bar, dominant_leg, dominant_score, execution_compatible)

        target_qty = state.position_qty
        delta_qty = Decimal("0")
        if action == "open":
            target_qty = Decimal("1")
            delta_qty = Decimal("1")
        elif action == "close":
            target_qty = Decimal("0")
            delta_qty = Decimal("-1")

        return ReplayDecision(
            ts=bar.ts,
            family="directional",
            symbol=ctx.symbol,
            timeframe=ctx.timeframe,
            state=new_state,
            selectable=selectable,
            execution_compatible=execution_compatible,
            long_score=round(long_score, 6),
            short_score=round(short_score, 6),
            blocking_reasons=blocking_reasons,
            expected_net_edge_bps=round(expected_net_edge_bps, 4),
            target_position_qty=target_qty,
            delta_position_qty=delta_qty,
            action=action,
            score_stable=score_stable,
            funding_rate=float(bar.aligned_funding_rate) if bar.aligned_funding_rate else None,
            close_price=float(bar.close),
            bar_index=ctx.bar_index,
        )

    def _compute_scores(self) -> tuple[float, float]:
        """基于 SMA 交叉计算方向性分数。"""
        closes = list(self._close_history)
        if len(closes) < _SLOW_PERIOD:
            return 0.0, 0.0

        fast_sma = sum(closes[-_FAST_PERIOD:]) / _FAST_PERIOD
        slow_sma = sum(closes[-_SLOW_PERIOD:]) / _SLOW_PERIOD

        if slow_sma == 0:
            return 0.0, 0.0

        spread = (fast_sma - slow_sma) / slow_sma
        signal_strength = _sigmoid(spread * 800)

        if spread > 0:
            return signal_strength, 1.0 - signal_strength
        else:
            return 1.0 - signal_strength, signal_strength

    def _check_stability(self, min_ticks: int, threshold_bps: float) -> bool:
        history = list(self._score_history)
        if len(history) < min_ticks:
            return False
        recent = history[-min_ticks:]
        if not all(s >= _ENTRY_THRESHOLD for s in recent):
            return False
        peak = max(recent)
        current = recent[-1]
        drawdown_bps = (peak - current) * 10000
        return drawdown_bps <= threshold_bps

    def _compute_edge(self, bar: ReplayBar, leg: str) -> float:
        closes = list(self._close_history)
        if len(closes) < 2:
            return 0.0
        bar_return = (closes[-1] - closes[-2]) / closes[-2] if closes[-2] != 0 else 0.0
        dir_edge = bar_return * 10000 if leg == "long" else -bar_return * 10000
        return dir_edge - _DEFAULT_COST_BPS

    def _advance_state(self, state: ReplayState, bar: ReplayBar,
                       dominant_leg: str, score: float, exec_ok: bool) -> tuple[str, str]:
        if state.position_side in ("long", "short"):
            if score < _CLOSE_THRESHOLD:
                state.position_qty = Decimal("0")
                state.position_side = "flat"
                state.entry_price = None
                state.last_close_ts = bar.ts
                return "close", "flat"
            if state.position_side != dominant_leg and score > _ENTRY_THRESHOLD:
                state.position_qty = Decimal("0")
                state.position_side = "flat"
                state.entry_price = None
                state.last_close_ts = bar.ts
                return "close", "flat"
            return "hold", "holding"

        if exec_ok:
            state.position_side = dominant_leg  # type: ignore[assignment]
            state.position_qty = Decimal("1")
            state.entry_price = bar.close
            state.entry_ts = bar.ts
            return "open", "probing"

        if score >= _ENTRY_THRESHOLD:
            return "blocked", "probing"
        return "hold", "flat"


def _sigmoid(x: float) -> float:
    try:
        return 1.0 / (1.0 + math.exp(-x))
    except OverflowError:
        return 0.0 if x < 0 else 1.0
