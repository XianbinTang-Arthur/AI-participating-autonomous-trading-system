from __future__ import annotations

from collections.abc import Sequence

from aats.bootstrap.settings import AATSSettings
from aats.schemas.decision import AIMarketAssessment, BaselineAssessment

from aats.services.portfolio_service.decimals import clamp_float as _clamp
from .models import IndependentLeg, ScoreStabilityMetrics


def effective_score_drawdown_threshold_bps(*, settings: AATSSettings) -> float:
    configured = settings.strategy_hedge_independent_min_score_drawdown_bps
    if configured is not None:
        return float(configured)
    return float(settings.strategy_hedge_independent_min_score_stability_bps)


def compute_raw_book_score(
    *,
    settings: AATSSettings,
    leg: IndependentLeg,
    baseline: BaselineAssessment,
    ai_assessment: AIMarketAssessment | None,
) -> float:
    if leg == "short" and not bool(settings.strategy_short_bias_enabled):
        return 0.0
    side_sign = 1.0 if leg == "long" else -1.0
    momentum_alpha = float(baseline.factor_scores.get("momentum_alpha", 0.0))
    trend_alpha = float(baseline.factor_scores.get("trend_alpha", 0.0))
    microstructure_alpha = float(baseline.factor_scores.get("microstructure_alpha", 0.0))
    alpha_component = _clamp(max(0.0, side_sign * float(baseline.composite_alpha_score)), 0.0, 1.0)
    ai_component = _clamp(max(0.0, side_sign * _ai_directional_edge(ai_assessment)), 0.0, 1.0)
    momentum_component = _clamp(max(0.0, side_sign * momentum_alpha), 0.0, 1.0)
    trend_component = _clamp(max(0.0, side_sign * trend_alpha), 0.0, 1.0)
    microstructure_component = _clamp(max(0.0, side_sign * microstructure_alpha), 0.0, 1.0)
    confidence = _clamp(float(baseline.confidence), 0.0, 1.0)
    score = (
        (alpha_component * 0.28)
        + (ai_component * 0.26)
        + (momentum_component * 0.16)
        + (trend_component * 0.12)
        + (microstructure_component * 0.08)
        + (confidence * 0.10)
    )
    if baseline.regime in {"range", "uncertain"}:
        score += 0.04
    if baseline.direction_bias == leg:
        score += 0.06
    if baseline.volatility_state == "high":
        score += 0.03
    return _clamp(score, 0.0, 1.0)


def compute_signal_edge_bps(
    *,
    settings: AATSSettings,
    baseline: BaselineAssessment,
    ai_assessment: AIMarketAssessment | None,
    leg: IndependentLeg,
) -> float:
    """计算 independent 家族的 signal_edge_proxy_bps。

    优先走 RDP score-based 路径 (与 replay 对齐)，仅在 RDP 未启用时
    退回到 legacy component_edge 路径。

    生产 vs replay 已知差异 (P2-8 修复后仍存在):
        生产 `compute_raw_book_score` 在 leg=="short" 且 short_bias_enabled=False
        时直接返回 0，导致 short leg signal_edge=0。
        replay `independent_adapter._compute_edge_layers` 不区分 short_bias，
        而是 dominant_leg = max(long_score, short_score)。
        当 short_bias_enabled=False 但 short_score > long_score 时，
        生产会跳过 short leg，replay 可能会进。
        当前仅 P2-8 修复 component vs score_based 公式偏差，short_bias gating
        差异属于另一个独立 issue (待 P2-9 跟进)。
    """
    side_sign = 1.0 if leg == "long" else -1.0
    directional_alpha = max(0.0, side_sign * float(baseline.composite_alpha_score))
    directional_microstructure = max(0.0, side_sign * float(baseline.factor_scores.get("microstructure_alpha", 0.0)))
    directional_momentum = max(0.0, side_sign * float(baseline.factor_scores.get("momentum_alpha", 0.0)))
    directional_trend = max(0.0, side_sign * float(baseline.factor_scores.get("trend_alpha", 0.0)))
    directional_ai = max(0.0, side_sign * _ai_directional_edge(ai_assessment))
    alpha_edge = directional_alpha * max(float(settings.strategy_alpha_edge_bps_scale), 0.0)
    microstructure_bonus = max(directional_microstructure - 0.08, 0.0) * 25.0
    momentum_bonus = max(directional_momentum - 0.08, 0.0) * 15.0
    trend_bonus = max(directional_trend - 0.08, 0.0) * 12.0
    ai_bonus = max(directional_ai - 0.1, 0.0) * 20.0
    component_edge = alpha_edge + microstructure_bonus + momentum_bonus + trend_bonus + ai_bonus

    # ── RDP score-based 信号边际路径 (P2-8: 单路径与 replay 对齐) ──────
    #
    # 当 strategy_signal_edge_scale_bps 由 active_parameters 注入时，
    # 切换到 score_based_edge = composite_score × scale 的单路径。
    #
    # P2-8 修复:
    #   RDP replay 独立家族在 independent_adapter._compute_edge_layers 中
    #   采用单一公式 `signal_edge_proxy_bps = dominant_score * signal_edge_scale_bps`。
    #   旧实现在生产端取 max(component_edge, score_based_edge)，导致生产端
    #   signal_edge >= 回测值，entry 行为系统性地偏离 replay 验证结论
    #   (120 天 BTC-USDT-SWAP 回测 scale=20 → pos_ratio 97-98%，但生产端
    #   component_edge 经常在 score_based_edge 之上，形成不可归因的行为差)。
    #
    # 新策略:
    #   - rdp_scale > 0 (已被 RDP calibration 钉住)    → 使用 score_based_edge
    #   - rdp_scale is None / 0 (legacy / 未校准)      → 保留 component_edge 旧路径
    #   这样生产与 replay 的 entry 决策基于同一 signal_edge 公式，
    #   RDP 推荐的 scale 才真正可跨环境复现。
    rdp_scale = settings.strategy_signal_edge_scale_bps
    if rdp_scale is not None and float(rdp_scale) > 0:
        composite_score = compute_raw_book_score(
            settings=settings,
            leg=leg,
            baseline=baseline,
            ai_assessment=ai_assessment,
        )
        score_based_edge = composite_score * float(rdp_scale)
        return score_based_edge

    # ── Legacy component_edge fallback (DEPRECATED, FALLBACK ONLY) ─────
    #
    # 此分支仅在以下场景触发:
    #   1. RDP coverage 失效 (active_parameter_sets 缺 signal_edge_scale_bps)
    #   2. 纯本地 sandbox / 单元测试无 RDP 推荐注入
    #   3. derivatives_live 之外的 profile 未启用 RDP pipeline
    #
    # 当前 production deployment (configs/active_parameter_sets/) 已全量
    # 钉住 signal_edge_scale_bps=20，意味着生产环境**永远不走此分支**。
    # 此处保留是为了:
    #   - sandbox / unit test 兼容
    #   - 如果 RDP pipeline 临时失效，仍有可工作的 fallback
    #
    # 后续 cleanup PR 可考虑:
    #   - 在 `__post_init__` 等启动校验中检查 strategy_signal_edge_scale_bps
    #     必须 > 0，缺失时直接 raise，并彻底删除此分支
    #   - 删除前需确认所有 sandbox / replay-disabled profile 都已迁移
    return component_edge


def compute_score_stability(
    *,
    settings: AATSSettings,
    leg: IndependentLeg,
    score: float,
    entry_threshold: float,
    baseline: BaselineAssessment,
    ai_assessment: AIMarketAssessment | None,
    recent_score_history: Sequence[float],
    min_confirm_ticks: int | None = None,
) -> ScoreStabilityMetrics:
    history = [float(item) for item in recent_score_history if item is not None]
    effective_min_confirm_ticks = max(
        int(settings.strategy_hedge_independent_min_confirm_ticks)
        if min_confirm_ticks is None
        else int(min_confirm_ticks),
        1,
    )
    if history:
        window_size = max(effective_min_confirm_ticks, 2)
        window = [*history[-window_size:], float(score)]
        support_count = sum(1 for item in window if item + 1e-9 >= entry_threshold)
        min_score = min(window)
        max_score = max(window)
        mean_score = sum(window) / max(len(window), 1)
        score_slope = 0.0 if len(window) < 2 else (window[-1] - window[0]) / max(len(window) - 1, 1)
        variance = sum((item - mean_score) ** 2 for item in window) / max(len(window), 1)
        score_volatility_bps = (variance**0.5) * 100.0
        upward_excursion_bps = max(float(score) - min_score, 0.0) * 100.0
        downward_drawdown_bps = max(max_score - float(score), 0.0) * 100.0
        stable = (
            support_count >= effective_min_confirm_ticks
            and downward_drawdown_bps <= effective_score_drawdown_threshold_bps(settings=settings) + 1e-9
        )
        return ScoreStabilityMetrics(
            support_count=support_count,
            min_score=min_score,
            max_score=max_score,
            mean_score=mean_score,
            stable=stable,
            source="recent_target_history",
            score_slope=score_slope,
            score_volatility_bps=score_volatility_bps,
            upward_excursion_bps=upward_excursion_bps,
            downward_drawdown_bps=downward_drawdown_bps,
        )
    support_count = _signal_confirmation_count(
        leg=leg,
        baseline=baseline,
        ai_assessment=ai_assessment,
    )
    return ScoreStabilityMetrics(
        support_count=support_count,
        min_score=float(score),
        max_score=float(score),
        mean_score=float(score),
        stable=support_count >= effective_min_confirm_ticks,
        source="current_signal_confirmation",
        score_slope=0.0,
        score_volatility_bps=0.0,
        upward_excursion_bps=0.0,
        downward_drawdown_bps=0.0,
    )


def compute_candidate_confidence(score: float) -> float:
    return min(0.95, 0.30 + max(score, 0.0) * 0.55)


def _signal_confirmation_count(
    *,
    leg: IndependentLeg,
    baseline: BaselineAssessment,
    ai_assessment: AIMarketAssessment | None,
) -> int:
    side_sign = 1.0 if leg == "long" else -1.0
    confirmations = (
        max(0.0, side_sign * float(baseline.composite_alpha_score)) >= 0.08,
        max(0.0, side_sign * float(baseline.factor_scores.get("momentum_alpha", 0.0))) >= 0.08,
        max(0.0, side_sign * float(baseline.factor_scores.get("trend_alpha", 0.0))) >= 0.08,
        max(0.0, side_sign * float(baseline.factor_scores.get("microstructure_alpha", 0.0))) >= 0.08,
        max(0.0, side_sign * _ai_directional_edge(ai_assessment)) >= 0.10,
    )
    return sum(1 for item in confirmations if item)


def _ai_directional_edge(ai_assessment: AIMarketAssessment | None) -> float:
    return 0.0 if ai_assessment is None else ai_assessment.directional_edge
