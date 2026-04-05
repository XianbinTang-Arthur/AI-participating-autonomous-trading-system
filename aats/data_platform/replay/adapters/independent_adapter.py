"""Independent family replay adapter.

Phase 2 首批优先打通的 adapter。从 Gold replay bars 构建简化的评估上下文，
复用 independent 策略的核心决策逻辑（评分、稳定性、资格门槛），输出逐 bar 决策。

简化说明（对比生产系统）：
- 不依赖 AI assessment（ai_component 置零）
- 不依赖 orderbook depth（流动性分数置 1.0）
- 不依赖真实 execution state（用简化状态机追踪）
- 基于 OHLCV + funding rate 构造因子输入
- 参数通过 ReplayParameterOverrides 注入，支持扫描

上述简化是 Phase 2 设计决策 §8.4 明确的边界：
  > Replay Core 在 Phase 2 不负责完整撮合仿真、PnL accounting、滑点模型、orderbook realism。

Edge Contract（P0-3）：
  expected_net_edge_bps = signal_edge_proxy_bps + funding_adjustment_bps - cost_bps

  signal_edge_proxy_bps:   从 score/momentum/trend/alpha 等因子派生
  funding_adjustment_bps:  funding rate 作为附加项（不是全部）
  cost_bps:               由 ReplayCostConfig 控制（默认 taker 5 bps + slippage 2 bps）
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


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 评分权重（与生产 scoring.py 对齐，去掉 ai_component）
_W_ALPHA = 0.34          # 原 0.28, AI 的 0.26 按比例分配到其他因子
_W_MOMENTUM = 0.24       # 原 0.16
_W_TREND = 0.18          # 原 0.12
_W_MICRO = 0.12          # 原 0.08
_W_CONFIDENCE = 0.12     # 原 0.10

# 默认阈值已迁移到 ReplayParameterOverrides 中，不再在此硬编码。
# 以下常量仅保留为文档参考，实际运行时从 params 读取：
#   entry_threshold  = 0.40  → params.entry_threshold
#   close_threshold  = 0.15  → params.close_threshold
#   scale_in_threshold = 0.60 → params.scale_in_threshold

_SCORE_HISTORY_WINDOW = 20         # 评分历史保留窗口


class IndependentReplayAdapter(BaseReplayAdapter):
    """Independent 策略的 replay adapter。

    核心流程（每根 bar）：
    1. 从 OHLCV 计算简化因子 -> 原始评分
    2. 检查评分稳定性（min_confirm_ticks + score_stability_threshold）
    3. 计算预期净边际（funding rate - cost）
    4. 通过资格门槛（min_safe_net_edge_bps）
    5. 更新状态机 -> 输出 ReplayDecision
    """

    def __init__(self) -> None:
        self._bar_history: deque[ReplayBar] = deque(maxlen=50)
        self._long_score_history: deque[float] = deque(maxlen=_SCORE_HISTORY_WINDOW)
        self._short_score_history: deque[float] = deque(maxlen=_SCORE_HISTORY_WINDOW)

    @property
    def family_name(self) -> str:
        return "independent"

    def reset_state(self) -> ReplayState:
        self._bar_history.clear()
        self._long_score_history.clear()
        self._short_score_history.clear()
        return ReplayState()

    # ------------------------------------------------------------------
    # 主评估入口
    # ------------------------------------------------------------------

    def evaluate_bar(self, ctx: ReplayBarContext) -> ReplayDecision:
        bar = ctx.bar
        params = ctx.params
        state = ctx.state

        self._bar_history.append(bar)

        # 1) 计算 long / short 原始评分
        long_score = self._compute_book_score(bar, leg="long")
        short_score = self._compute_book_score(bar, leg="short")

        self._long_score_history.append(long_score)
        self._short_score_history.append(short_score)

        # 2) 选择较强方向
        dominant_leg = "long" if long_score >= short_score else "short"
        dominant_score = max(long_score, short_score)
        score_history = (
            list(self._long_score_history) if dominant_leg == "long"
            else list(self._short_score_history)
        )

        # 3) 评分稳定性
        entry_thresh = params.get_entry_threshold(dominant_leg)
        score_stable = self._check_score_stability(
            score=dominant_score,
            history=score_history,
            min_confirm_ticks=params.min_confirm_ticks,
            threshold_bps=params.score_stability_threshold,
            entry_threshold=entry_thresh,
            min_score_drawdown_bps=params.min_score_drawdown_bps,
        )

        # 4) 计算预期净边际（统一 edge contract: signal + funding - cost）
        edge = self._compute_edge_breakdown(bar, dominant_leg, dominant_score, params)
        signal_edge_proxy_bps = edge["signal"]
        funding_adjustment_bps = edge["funding"]
        cost_bps = edge["cost"]
        expected_net_edge_bps = edge["net"]

        # 5) 资格评估（阈值从 params 读取，不再使用硬编码常量）
        blocking_reasons: list[str] = []

        if dominant_score < entry_thresh:
            blocking_reasons.append("score_below_entry_threshold")
        if not score_stable:
            blocking_reasons.append("score_not_stable")
        if expected_net_edge_bps < params.min_safe_net_edge_bps:
            blocking_reasons.append("net_edge_below_safe_minimum")
        if cost_bps > params.max_acceptable_cost_bps:
            blocking_reasons.append("cost_exceeds_max_acceptable")

        selectable = dominant_score >= entry_thresh
        execution_compatible = selectable and score_stable and (
            expected_net_edge_bps >= params.min_safe_net_edge_bps
        ) and cost_bps <= params.max_acceptable_cost_bps

        # 6) 状态机推进（注意: _advance_state 可能 append blocking_reasons — 副作用传参）
        action, new_state_label, close_reason = self._advance_state(
            state=state,
            bar=bar,
            params=params,
            dominant_leg=dominant_leg,
            dominant_score=dominant_score,
            execution_compatible=execution_compatible,
            blocking_reasons=blocking_reasons,
            expected_net_edge_bps=expected_net_edge_bps,
        )

        # 7) 持仓变动
        target_qty, delta_qty = self._compute_position_delta(
            state=state,
            action=action,
        )

        return ReplayDecision(
            ts=bar.ts,
            family="independent",
            symbol=ctx.symbol,
            timeframe=ctx.timeframe,
            state=new_state_label,
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

    # ------------------------------------------------------------------
    # 简化评分（对齐生产 scoring.py 逻辑，基于 OHLCV 派生因子）
    # ------------------------------------------------------------------

    def _compute_book_score(self, bar: ReplayBar, *, leg: str) -> float:
        """从 OHLCV 派生因子，计算简化的 book 评分。

        因子来源：
        - alpha:         (close - open) / open 的方向性
        - momentum:      最近 N 根 bar 的累积动量
        - trend:         SMA 趋势方向
        - microstructure: bar 实体占比（body / range）
        - confidence:    volume 加权置信度
        """
        if len(self._bar_history) < 2:
            return 0.0

        close_f = float(bar.close)
        open_f = float(bar.open)
        high_f = float(bar.high)
        low_f = float(bar.low)

        # --- alpha 因子：本根 bar 的方向性收益 ---
        bar_return = (close_f - open_f) / open_f if open_f > 0 else 0.0
        if leg == "short":
            bar_return = -bar_return
        alpha_raw = _sigmoid(bar_return * 500)  # 缩放到 [0, 1]

        # --- momentum 因子：最近 5 根 bar 的累积收益 ---
        lookback = min(5, len(self._bar_history))
        bars = list(self._bar_history)[-lookback:]
        if len(bars) >= 2:
            momentum_return = (float(bars[-1].close) - float(bars[0].open)) / float(bars[0].open)
        else:
            momentum_return = bar_return
        if leg == "short":
            momentum_return = -momentum_return
        momentum_raw = _sigmoid(momentum_return * 300)

        # --- trend 因子：SMA(10) 方向 ---
        sma_lookback = min(10, len(self._bar_history))
        sma_bars = list(self._bar_history)[-sma_lookback:]
        sma = sum(float(b.close) for b in sma_bars) / len(sma_bars)
        trend_dir = (close_f - sma) / sma if sma > 0 else 0.0
        if leg == "short":
            trend_dir = -trend_dir
        trend_raw = _sigmoid(trend_dir * 400)

        # --- microstructure 因子：bar 实体占比 ---
        bar_range = high_f - low_f
        body = abs(close_f - open_f)
        micro_raw = (body / bar_range) if bar_range > 0 else 0.5

        # --- confidence 因子：volume 相对强度 ---
        vol_lookback = min(10, len(self._bar_history))
        vol_bars = list(self._bar_history)[-vol_lookback:]
        avg_vol = sum(float(b.volume or 0) for b in vol_bars) / len(vol_bars) if vol_bars else 1.0
        current_vol = float(bar.volume or 0)
        vol_ratio = (current_vol / avg_vol) if avg_vol > 0 else 1.0
        confidence_raw = min(_sigmoid((vol_ratio - 1.0) * 200), 1.0)

        # --- 加权合成 ---
        score = (
            _W_ALPHA * alpha_raw
            + _W_MOMENTUM * momentum_raw
            + _W_TREND * trend_raw
            + _W_MICRO * micro_raw
            + _W_CONFIDENCE * confidence_raw
        )
        return max(0.0, min(1.0, score))

    # ------------------------------------------------------------------
    # 评分稳定性（对齐生产 scoring.py: compute_score_stability）
    # ------------------------------------------------------------------

    def _check_score_stability(
        self,
        *,
        score: float,
        history: list[float],
        min_confirm_ticks: int,
        threshold_bps: float,
        entry_threshold: float,
        min_score_drawdown_bps: float | None = None,
    ) -> bool:
        """检查评分是否稳定。

        逻辑：最近 min_confirm_ticks 根 bar 的评分都 >= entry_threshold，
        且最大回撤 <= threshold_bps（以分数变化的 bps 衡量）。

        新增：如果 min_score_drawdown_bps 不为 None，则同时检查回撤
        是否超过该 bps 阈值（与生产端 min_score_drawdown_bps 对齐）。
        """
        if len(history) < min_confirm_ticks:
            return False

        recent = history[-min_confirm_ticks:]

        # 条件 1：所有 recent 评��� >= entry_threshold（参数化）
        support_count = sum(1 for s in recent if s >= entry_threshold)
        if support_count < min_confirm_ticks:
            return False

        # 条件 2：最大回撤（从峰值到当前）不超过阈值
        peak = max(recent)
        drawdown_bps = (peak - score) * 100.0  # 评分差异 -> bps (与生产端 ×100 对齐)
        if drawdown_bps > threshold_bps:
            return False

        # ���件 3（可选）：如果启用了 min_score_drawdown_bps，额外检查
        if min_score_drawdown_bps is not None and drawdown_bps > min_score_drawdown_bps:
            return False

        return True

    # ------------------------------------------------------------------
    # Edge 分解（P0-3 统一 contract）
    # ------------------------------------------------------------------

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
           从评分因子派生。independent 的信号价值来自 score/momentum/trend/alpha
           的综合评估，不应被简化为只看 funding。
           公式: dominant_score * params.signal_edge_scale_bps
           说明: score=0.6 * 10 = 6 bps 信号代理; score=0.4 * 10 = 4 bps
           缩放系数 signal_edge_scale_bps 可通过参数覆盖（默认 10.0），支持 calibration run。

        2) funding_adjustment_bps:
           funding rate 作为附加项。对 short leg 正 funding 有利，对 long leg 负 funding 有利。
           这是 independent 特有的 funding 附加收益，但不再是 edge 的全部。

        3) cost_bps:
           来自 ReplayCostConfig（默认 taker 5 bps + slippage 2 bps = 7 bps）

        4) net = signal + funding - cost
        """
        cost = params.cost_config

        # --- 1) signal edge proxy: 来自策略评分的机会代理 ---
        # 评分越高，信号越强，代理 edge 越大
        # 将 score 映射到 bps: score * scale（scale 来自 params，可校准）
        signal_edge_proxy_bps = dominant_score * params.signal_edge_scale_bps

        # --- 2) funding adjustment: 附加项，不是全部 ---
        funding_adjustment_bps = 0.0
        if bar.aligned_funding_rate is not None:
            fr = float(bar.aligned_funding_rate)
            funding_bps = fr * 10000  # rate -> bps
            # 方向调整: short 收取正 funding, long 收取负 funding
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

    # ------------------------------------------------------------------
    # 状态机推进
    # ------------------------------------------------------------------

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

        状态转移（简化版）：
        flat -> probing (score >= entry_threshold)
        probing -> holding (execution_compatible && confirmed)
        holding -> holding (继续持有)
        holding -> flat (score < close_threshold or 方向反转)
        * -> blocked (有阻断原因)

        Phase 1 扩展：
        - 持仓未达 min_hold_seconds 前不平仓
        - 上次平仓后 rebalance_cooldown_seconds 内不开新仓
        - thesis 超龄（max_thesis_age_seconds）触发 stale 退出
        - 净边际 < failed_thesis_net_edge_bps → thesis 失效退出
        - 净边际 < de_risk_net_edge_bps → 降风险退出

        close_reason 值域:
          thesis_failed / de_risk / score_below_close /
          direction_reversal / thesis_stale / ""（非 close 时）
        """
        current_state = state.position_side
        close_thresh = params.get_close_threshold(current_state)
        entry_thresh = params.get_entry_threshold(dominant_leg)

        # --- 已持仓 ---
        if current_state in ("long", "short"):
            # 计算持仓时长
            held_seconds = 0.0
            if state.entry_ts is not None:
                held_seconds = (bar.ts - state.entry_ts).total_seconds()

            # min_hold_seconds 保护：持仓不够久则继续持有
            if held_seconds < params.min_hold_seconds:
                return "hold", "holding", ""

            # 平仓条件（按优先级从高到低排列）
            should_close = False
            close_reason = ""

            # thesis 失效（最紧急 — 净边际大幅为负）
            if expected_net_edge_bps < params.failed_thesis_net_edge_bps:
                should_close = True
                close_reason = "thesis_failed"
            # 降风险（净边际变薄但未到失效）
            elif expected_net_edge_bps < params.de_risk_net_edge_bps:
                should_close = True
                close_reason = "de_risk"
            elif dominant_score < close_thresh:
                should_close = True
                close_reason = "score_below_close"
            elif current_state == "long" and dominant_leg == "short" and dominant_score > entry_thresh:
                should_close = True
                close_reason = "direction_reversal"
            elif current_state == "short" and dominant_leg == "long" and dominant_score > entry_thresh:
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
            else:
                return "hold", "holding", ""

        # --- 无持仓 ---
        # rebalance_cooldown 保护：平仓后冷却期内不开新仓
        if state.last_close_ts is not None:
            cooldown_elapsed = (bar.ts - state.last_close_ts).total_seconds()
            if cooldown_elapsed < params.rebalance_cooldown_seconds:
                if dominant_score >= entry_thresh:
                    blocking_reasons.append("rebalance_cooldown")
                    return "blocked", "probing", ""
                return "hold", "flat", ""

        if execution_compatible:
            # 开仓
            state.position_side = dominant_leg  # type: ignore[assignment]
            state.position_qty = Decimal("1")
            state.entry_price = bar.close
            state.entry_ts = bar.ts
            return "open", "probing", ""

        if dominant_score >= entry_thresh:
            return "blocked", "probing", ""

        return "hold", "flat", ""

    # ------------------------------------------------------------------
    # 持仓计算
    # ------------------------------------------------------------------

    def _compute_position_delta(
        self,
        *,
        state: ReplayState,
        action: str,
    ) -> tuple[Decimal, Decimal]:
        """计算目标持仓和持仓变化。"""
        if action == "open":
            target = Decimal("1")
            delta = Decimal("1")
        elif action == "close":
            target = Decimal("0")
            delta = Decimal("-1")
        else:
            target = state.position_qty
            delta = Decimal("0")
        return target, delta


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _sigmoid(x: float) -> float:
    """Sigmoid 函数，将任意实数映射到 (0, 1)。"""
    try:
        return 1.0 / (1.0 + math.exp(-x))
    except OverflowError:
        return 0.0 if x < 0 else 1.0
