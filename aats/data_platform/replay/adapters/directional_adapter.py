"""Directional family replay adapter (minimal).

Phase 2 设计决策 §9.4：
- directional 接口必须能接，尽快适配
- 架构上两个 family 都要能接

本 adapter 实现 directional 策略的最小 replay 能力：
- 基于趋势方向做多/做空
- 使用 SMA crossover 作为简化信号
- 同样受 min_confirm_ticks / min_safe_net_edge_bps 约束

Edge Contract（P0-3）：
  expected_net_edge_bps = signal_edge_proxy_bps + funding_adjustment_bps - cost_bps

  signal_edge_proxy_bps:   directional 使用 bar return + trend strength 派生
  funding_adjustment_bps:  funding rate 作为附加项
  cost_bps:               由 ReplayCostConfig 控制
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
    ReplayParameterOverrides,
    ReplayState,
)

_FAST_PERIOD = 5
_SLOW_PERIOD = 20
# 默认阈值已迁移到 ReplayParameterOverrides 中，不再在此硬编码。
# 以下常量仅保留为文档参考：
#   entry_threshold  = 0.45  → params.entry_threshold
#   close_threshold  = 0.20  → params.close_threshold


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
        entry_thresh_for_stability = params.get_entry_threshold(dominant_leg)
        score_stable = self._check_stability(
            params.min_confirm_ticks, params.score_stability_threshold,
            entry_thresh_for_stability, params.min_score_drawdown_bps,
        )

        # 预期边际（统一 edge contract: signal + funding - cost）
        edge = self._compute_edge_breakdown(bar, dominant_leg, dominant_score, params)
        signal_edge_proxy_bps = edge["signal"]
        funding_adjustment_bps = edge["funding"]
        cost_bps = edge["cost"]
        expected_net_edge_bps = edge["net"]

        # 资格评估（阈值从 params 读取）
        entry_thresh = params.get_entry_threshold(dominant_leg)
        blocking_reasons: list[str] = []
        if dominant_score < entry_thresh:
            blocking_reasons.append("score_below_entry_threshold")
        if not score_stable:
            blocking_reasons.append("score_not_stable")
        if expected_net_edge_bps < params.min_safe_net_edge_bps:
            blocking_reasons.append("net_edge_below_safe_minimum")
        if edge["cost"] > params.max_acceptable_cost_bps:
            blocking_reasons.append("cost_exceeds_max_acceptable")

        selectable = dominant_score >= entry_thresh
        execution_compatible = selectable and score_stable and (
            expected_net_edge_bps >= params.min_safe_net_edge_bps
        ) and edge["cost"] <= params.max_acceptable_cost_bps

        # 状态机（注意: _advance_state 可能 append blocking_reasons — 副作用传参）
        action, new_state, close_reason = self._advance_state(
            state=state,
            bar=bar,
            params=params,
            dominant_leg=dominant_leg,
            dominant_score=dominant_score,
            execution_compatible=execution_compatible,
            blocking_reasons=blocking_reasons,
            expected_net_edge_bps=expected_net_edge_bps,
        )

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
            signal_edge_proxy_bps=round(signal_edge_proxy_bps, 4),
            funding_adjustment_bps=round(funding_adjustment_bps, 4),
            cost_bps=round(cost_bps, 4),
            expected_net_edge_bps=round(expected_net_edge_bps, 4),
            target_position_qty=target_qty,
            delta_position_qty=delta_qty,
            action=action,
            close_reason=close_reason,
            score_stable=score_stable,
            funding_rate=float(bar.aligned_funding_rate) if bar.aligned_funding_rate is not None else None,
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

    def _check_stability(
        self, min_ticks: int, threshold_bps: float,
        entry_threshold: float, min_score_drawdown_bps: float | None = None,
    ) -> bool:
        history = list(self._score_history)
        if len(history) < min_ticks:
            return False
        recent = history[-min_ticks:]
        if not all(s >= entry_threshold for s in recent):
            return False
        peak = max(recent)
        current = recent[-1]
        drawdown_bps = (peak - current) * 10000
        if drawdown_bps > threshold_bps:
            return False
        if min_score_drawdown_bps is not None and drawdown_bps > min_score_drawdown_bps:
            return False
        return True

    def _compute_edge_breakdown(
        self,
        bar: ReplayBar,
        leg: str,
        dominant_score: float,
        params: ReplayParameterOverrides,
    ) -> dict[str, float]:
        """计算 edge 的 4 层分解（bps）。

        统一 Edge Contract:
          expected_net_edge_bps = signal_edge_proxy_bps + funding_adjustment_bps - cost_bps

        1) signal_edge_proxy_bps:
           directional 的信号代理包含两部分：
           - 趋势强度：dominant_score * params.signal_edge_scale_bps
           - bar return 修正：最近一根 bar 的方向收益提供短期确认
           二者加权合成（权重、限幅均来自 params，可校准），使信号代理不只依赖单一来源。

        2) funding_adjustment_bps:
           与 independent 相同语义，funding rate 作为附加项。

        3) cost_bps:
           来自 ReplayCostConfig
        """
        cost = params.cost_config
        scale = params.signal_edge_scale_bps
        trend_w = params.directional_trend_weight
        clamp = params.directional_return_clamp_bps

        # --- 1) signal edge proxy ---
        # 趋势强度分量（scale 来自 params，可校准）
        trend_signal = dominant_score * scale

        # bar return 修正分量（短期方向确认）
        closes = list(self._close_history)
        bar_return_bps = 0.0
        if len(closes) >= 2 and closes[-2] != 0:
            bar_return = (closes[-1] - closes[-2]) / closes[-2]
            bar_return_bps = bar_return * 10000 if leg == "long" else -bar_return * 10000

        # 加权（权重和限幅均来自 params，可校准）
        bar_return_clamped = max(-clamp, min(clamp, bar_return_bps))
        signal_edge_proxy_bps = trend_w * trend_signal + (1.0 - trend_w) * bar_return_clamped

        # --- 2) funding adjustment ---
        funding_adjustment_bps = 0.0
        if bar.aligned_funding_rate is not None:
            fr = float(bar.aligned_funding_rate)
            funding_bps = fr * 10000
            if leg == "short":
                funding_adjustment_bps = funding_bps
            else:
                funding_adjustment_bps = -funding_bps

        # --- 3) cost: 基础成本 + 缓冲 ---
        cost_bps = (
            cost.total_cost_bps
            + params.expected_slippage_buffer_bps
            + params.expected_execution_buffer_bps
        )

        # --- 4) net ---
        net_edge = signal_edge_proxy_bps + funding_adjustment_bps - cost_bps

        return {
            "signal": signal_edge_proxy_bps,
            "funding": funding_adjustment_bps,
            "cost": cost_bps,
            "net": net_edge,
        }

    def _advance_state(
        self,
        *,
        state: ReplayState,
        bar: ReplayBar,
        params: ReplayParameterOverrides,
        dominant_leg: str,
        dominant_score: float,
        execution_compatible: bool,
        blocking_reasons: list[str],
        expected_net_edge_bps: float = 0.0,
    ) -> tuple[str, str, str]:
        """推进简化状态机，返回 (action, state_label, close_reason)。

        注意: 本方法会直接 append blocking_reasons 列表（副作用传参）。

        close_reason 值域:
          thesis_failed / catastrophic_thesis_failed / de_risk / score_below_close /
          direction_reversal / thesis_stale / ""（非 close 时）
        """
        close_thresh = params.get_close_threshold(state.position_side)
        entry_thresh = params.get_entry_threshold(dominant_leg)

        if state.position_side in ("long", "short"):
            # min_hold_seconds 保护
            held_seconds = 0.0
            if state.entry_ts is not None:
                held_seconds = (bar.ts - state.entry_ts).total_seconds()

            # 灾难性 failed_thesis 判定（whipsaw 防护）
            # 只有当 net_edge 深度跌破 failed_thesis 阈值（跨越 catastrophic buffer）
            # 才豁免 min_hold 立即止损；否则遵守 min_hold 冷却。
            catastrophic_threshold = (
                params.failed_thesis_net_edge_bps
                - max(params.catastrophic_failed_thesis_buffer_bps, 0.0)
            )
            is_catastrophic = expected_net_edge_bps <= catastrophic_threshold + 1e-9

            if held_seconds < params.min_hold_seconds and not is_catastrophic:
                return "hold", "holding", ""

            should_close = False
            close_reason = ""

            # 灾难性 thesis 失效（最紧急 — 深度亏损，豁免 min_hold）
            if is_catastrophic:
                should_close = True
                close_reason = "catastrophic_thesis_failed"
            # 标准 thesis 失效（净边际小幅为负）
            elif expected_net_edge_bps < params.failed_thesis_net_edge_bps:
                should_close = True
                close_reason = "thesis_failed"
            # 降风险（净边际变薄但未到失效）
            elif expected_net_edge_bps < params.de_risk_net_edge_bps:
                should_close = True
                close_reason = "de_risk"
            elif dominant_score < close_thresh:
                should_close = True
                close_reason = "score_below_close"
            elif state.position_side != dominant_leg and dominant_score > entry_thresh:
                should_close = True
                close_reason = "direction_reversal"
            elif held_seconds >= params.max_thesis_age_seconds:
                should_close = True
                close_reason = "thesis_stale"

            if should_close:
                state.position_qty = Decimal("0")
                state.position_side = "flat"
                state.entry_price = None
                state.entry_ts = None
                state.last_close_ts = bar.ts
                return "close", "flat", close_reason
            return "hold", "holding", ""

        # rebalance_cooldown 保护
        if state.last_close_ts is not None:
            cooldown_elapsed = (bar.ts - state.last_close_ts).total_seconds()
            if cooldown_elapsed < params.rebalance_cooldown_seconds:
                if dominant_score >= entry_thresh:
                    blocking_reasons.append("rebalance_cooldown")
                    return "blocked", "probing", ""
                return "hold", "flat", ""

        if execution_compatible:
            state.position_side = dominant_leg  # type: ignore[assignment]
            state.position_qty = Decimal("1")
            state.entry_price = bar.close
            state.entry_ts = bar.ts
            return "open", "probing", ""

        if dominant_score >= entry_thresh:
            return "blocked", "probing", ""
        return "hold", "flat", ""


def _sigmoid(x: float) -> float:
    try:
        return 1.0 / (1.0 + math.exp(-x))
    except OverflowError:
        return 0.0 if x < 0 else 1.0
