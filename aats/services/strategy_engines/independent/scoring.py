from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from aats.bootstrap.settings import AATSSettings
from aats.schemas.decision import AIMarketAssessment, BaselineAssessment, normalize_ai_operating_mode

from aats.services.portfolio_service.decimals import clamp_float as _clamp
from .models import IndependentLeg, ScoreStabilityMetrics


# =========================================================================
# AI Operating Mode Scoring Tiers
# =========================================================================
# Independent family 评分在三档 AI 运行模式下使用不同权重：
#
#   Mode A  (baseline_only / ai_assessment is None):
#       AI 完全退出 composite score，权重按 RDP replay adapter 的设计
#       重分配给其他因子。保持与 replay 一致以便回测 ↔ 生产对齐。
#       ai_assessment is None 时无条件落入此档（防御性设计，
#       防止 AI 服务超时/降级返回 None 时仍按 AI 在场公式算分）。
#
#   Mode B  (ai_assisted):
#       AI 有一定参考价值但不主导决策。AI 权重降至 0.10（原 0.26），
#       节省的 0.16 按 Mode A vs Mode C 的增量比例线性插值分配：
#           Mode B[i] = Mode C[i] + (Mode A[i] - Mode C[i]) × 16/26
#
#   Mode C  (ai_decision_maker):
#       原始公式，AI 权重 0.26。
#
# 修改任一档权重时必须：
#   (1) 同步更新 aats/data_platform/replay/adapters/independent_adapter.py
#       中 Mode A 的权重（否则 replay 与生产 drift）
#   (2) 验证三档 Σ ≈ 1.0
#   (3) 跑 test_independent_scoring_ai_fallback.py 确认锁定测试通过

AIScoringMode = Literal["MODE_A", "MODE_B", "MODE_C"]


# ── Composite score 权重 (Mode A: baseline_only / AI=None) ──
# 与 aats/data_platform/replay/adapters/independent_adapter.py 保持同步
_MODE_A_W_ALPHA: float = 0.34
_MODE_A_W_AI: float = 0.0
_MODE_A_W_MOMENTUM: float = 0.24
_MODE_A_W_TREND: float = 0.18
_MODE_A_W_MICRO: float = 0.12
_MODE_A_W_CONFIDENCE: float = 0.12

# ── Composite score 权重 (Mode B: ai_assisted) ──
# AI 权重 0.10；其他 = Mode C + (Mode A - Mode C) × 16/26，精确到 4 位小数。
# alpha 补 +0.0001 消除累积 round-down 误差，保证 Σ = 1.0000 严格成立。
_MODE_B_W_ALPHA: float = 0.3170
_MODE_B_W_AI: float = 0.10
_MODE_B_W_MOMENTUM: float = 0.2092
_MODE_B_W_TREND: float = 0.1569
_MODE_B_W_MICRO: float = 0.1046
_MODE_B_W_CONFIDENCE: float = 0.1123

# ── Composite score 权重 (Mode C: ai_decision_maker) ──
_MODE_C_W_ALPHA: float = 0.28
_MODE_C_W_AI: float = 0.26
_MODE_C_W_MOMENTUM: float = 0.16
_MODE_C_W_TREND: float = 0.12
_MODE_C_W_MICRO: float = 0.08
_MODE_C_W_CONFIDENCE: float = 0.10


# ── Legacy component_edge bonus 系数 (三档) ──
# 原系数 (Mode C): micro=25, momentum=15, trend=12, ai=20
# 三档策略:
#   - ai_coef 按 (AI权重/0.26) 缩放：Mode A=0, Mode B=20×10/26≈7.692, Mode C=20
#   - "节省 pool" = 20 - ai_coef，按 micro:momentum:trend = 25:15:12 (合计 52) 比例
#     补偿给三个 bonus
#   - alpha_edge_scale 使用 settings.strategy_alpha_edge_bps_scale，不在此重分配
#     (对齐 composite 设计精神：alpha 已是主力信号，不再补偿)

_MODE_A_BONUS_MICRO: float = 34.6154
_MODE_A_BONUS_MOMENTUM: float = 20.7692
_MODE_A_BONUS_TREND: float = 16.6154
_MODE_A_BONUS_AI: float = 0.0

_MODE_B_BONUS_MICRO: float = 30.9172
_MODE_B_BONUS_MOMENTUM: float = 18.5503
_MODE_B_BONUS_TREND: float = 14.8402
_MODE_B_BONUS_AI: float = 7.6923

_MODE_C_BONUS_MICRO: float = 25.0
_MODE_C_BONUS_MOMENTUM: float = 15.0
_MODE_C_BONUS_TREND: float = 12.0
_MODE_C_BONUS_AI: float = 20.0


def _pick_ai_mode(
    *,
    settings: AATSSettings,
    ai_assessment: AIMarketAssessment | None,
) -> AIScoringMode:
    """根据 AI 运行模式和 assessment 是否在场选择评分档位。

    ai_assessment is None 时无条件返回 Mode A（防御性兜底），
    即使配置的 ai_operating_mode 是 ai_decision_maker——因为此时 AI 已经
    实质性失效（超时 / 降级 / 未构造），不能按 AI 在场公式算分。
    """
    if ai_assessment is None:
        return "MODE_A"
    mode = normalize_ai_operating_mode(settings.ai_operating_mode)
    if mode == "baseline_only":
        return "MODE_A"
    if mode == "ai_assisted":
        return "MODE_B"
    if mode == "ai_decision_maker":
        return "MODE_C"
    return "MODE_A"  # 未来未知值兜底到最保守的档位


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
    # H4 修复 (2026-04-19): confidence 仅在该 leg 与 baseline.direction_bias 对齐时计入。
    # 旧实现 confidence = clamp(baseline.confidence) 两腿同加，而
    # baseline.confidence = 0.35 + 0.35·|alpha_score| + ... 方向无关 → 在多头 drift
    # 市场中系统性污染 short leg 的 score-realized 相关性（short slope 翻负、R²≈0）。
    # 参见 docs/design/h4_confidence_direction_gating_2026_04_19.md
    # 与 docs/review/short_leg_asymmetry_root_cause_2026_04_19.md §5 方案 A.1
    leg_aligned = baseline.direction_bias == leg
    confidence = _clamp(
        float(baseline.confidence) if leg_aligned else 0.0,
        0.0,
        1.0,
    )

    mode = _pick_ai_mode(settings=settings, ai_assessment=ai_assessment)
    if mode == "MODE_A":
        # AI fallback：AI 权重 0，对齐 RDP replay adapter 的权重分配
        score = (
            (alpha_component * _MODE_A_W_ALPHA)
            + (momentum_component * _MODE_A_W_MOMENTUM)
            + (trend_component * _MODE_A_W_TREND)
            + (microstructure_component * _MODE_A_W_MICRO)
            + (confidence * _MODE_A_W_CONFIDENCE)
        )
    elif mode == "MODE_B":
        # AI 低权重：Mode A 和 Mode C 之间的线性插值
        score = (
            (alpha_component * _MODE_B_W_ALPHA)
            + (ai_component * _MODE_B_W_AI)
            + (momentum_component * _MODE_B_W_MOMENTUM)
            + (trend_component * _MODE_B_W_TREND)
            + (microstructure_component * _MODE_B_W_MICRO)
            + (confidence * _MODE_B_W_CONFIDENCE)
        )
    else:  # MODE_C
        # AI 全权重（原始公式）
        score = (
            (alpha_component * _MODE_C_W_ALPHA)
            + (ai_component * _MODE_C_W_AI)
            + (momentum_component * _MODE_C_W_MOMENTUM)
            + (trend_component * _MODE_C_W_TREND)
            + (microstructure_component * _MODE_C_W_MICRO)
            + (confidence * _MODE_C_W_CONFIDENCE)
        )

    # H4 修复 (2026-04-19): 方向无关 bonus 仅在 leg 与 direction_bias 对齐时计入。
    # 旧实现 regime_bonus 和 volatility_bonus 两腿同加，稀释 short slope 信号。
    if baseline.regime in {"range", "uncertain"} and leg_aligned:
        score += 0.04
    if baseline.direction_bias == leg:
        score += 0.06
    if baseline.volatility_state == "high" and leg_aligned:
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

    # ── Legacy bonus 系数按三档 AI 运行模式选取 ──
    # 与 compute_raw_book_score 的 mode 判断保持一致。
    # ai_assessment is None 时走 Mode A（ai_coef=0），其他 bonus 系数放大补偿。
    mode = _pick_ai_mode(settings=settings, ai_assessment=ai_assessment)
    if mode == "MODE_A":
        micro_coef = _MODE_A_BONUS_MICRO
        momentum_coef = _MODE_A_BONUS_MOMENTUM
        trend_coef = _MODE_A_BONUS_TREND
        ai_coef = _MODE_A_BONUS_AI
    elif mode == "MODE_B":
        micro_coef = _MODE_B_BONUS_MICRO
        momentum_coef = _MODE_B_BONUS_MOMENTUM
        trend_coef = _MODE_B_BONUS_TREND
        ai_coef = _MODE_B_BONUS_AI
    else:  # MODE_C
        micro_coef = _MODE_C_BONUS_MICRO
        momentum_coef = _MODE_C_BONUS_MOMENTUM
        trend_coef = _MODE_C_BONUS_TREND
        ai_coef = _MODE_C_BONUS_AI

    microstructure_bonus = max(directional_microstructure - 0.08, 0.0) * micro_coef
    momentum_bonus = max(directional_momentum - 0.08, 0.0) * momentum_coef
    trend_bonus = max(directional_trend - 0.08, 0.0) * trend_coef
    ai_bonus = max(directional_ai - 0.1, 0.0) * ai_coef
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
        settings=settings,
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
    settings: AATSSettings,
    leg: IndependentLeg,
    baseline: BaselineAssessment,
    ai_assessment: AIMarketAssessment | None,
) -> int:
    """AI 运行模式三档下的 signal confirmation 统计。

    Mode A (baseline_only / ai_assessment is None):
        AI 彻底退出 confirmation 投票，仅统计 4 项特征派生信号。
    Mode B / Mode C:
        保留原 5 项投票（AI 以 ai_edge >= 0.10 作为独立确认项）。
    """
    side_sign = 1.0 if leg == "long" else -1.0
    base_confirmations = (
        max(0.0, side_sign * float(baseline.composite_alpha_score)) >= 0.08,
        max(0.0, side_sign * float(baseline.factor_scores.get("momentum_alpha", 0.0))) >= 0.08,
        max(0.0, side_sign * float(baseline.factor_scores.get("trend_alpha", 0.0))) >= 0.08,
        max(0.0, side_sign * float(baseline.factor_scores.get("microstructure_alpha", 0.0))) >= 0.08,
    )
    mode = _pick_ai_mode(settings=settings, ai_assessment=ai_assessment)
    if mode == "MODE_A":
        return sum(1 for item in base_confirmations if item)
    ai_confirmation = max(0.0, side_sign * _ai_directional_edge(ai_assessment)) >= 0.10
    return sum(1 for item in (*base_confirmations, ai_confirmation) if item)


def _ai_directional_edge(ai_assessment: AIMarketAssessment | None) -> float:
    return 0.0 if ai_assessment is None else ai_assessment.directional_edge
