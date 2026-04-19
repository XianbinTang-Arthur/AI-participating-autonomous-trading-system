from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from aats.bootstrap.settings import AATSSettings
from aats.events import topics
from aats.schemas.decision import BaselineAssessment, DecisionContext
from aats.schemas.features import AnalysisContext, DirectionalBias, FeatureSnapshot, RegimeIndicator
from aats.storage.base import EventStore

if TYPE_CHECKING:
    from aats.services.decision_engine.feature_resolver import FeatureSnapshotResolver

_LOGGER = logging.getLogger("aats.decision_engine.baseline")


def _parse_snapshot_ts(raw: Any) -> datetime | None:
    """容错解析 FeatureSnapshot.payload['snapshot_ts']，失败返回 None.

    payload 来自 event_store，可能是 ISO 字符串（JSON 持久化后）或 datetime
    （内存路径）。解析失败不 raise——Bug-3 检查本身是保护性的，解析失败时
    降级为 "无法判断是否过期"，照常 warning + fallback（旧行为）。
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo is not None else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    return None


class BaselineStrategy:
    def __init__(
        self,
        *,
        event_store: EventStore,
        feature_resolver: "FeatureSnapshotResolver | None" = None,
        settings: AATSSettings | None = None,
    ) -> None:
        self.event_store = event_store
        self._feature_resolver = feature_resolver
        self.settings = settings or AATSSettings()

    def evaluate(self, context: DecisionContext) -> BaselineAssessment:
        # 按 feature_snapshot_ref(event_id) 精确读取，保证同一 context 行情基准一致。
        # resolver 路径: stream_cache.get(ref) -> event_store.get(ref)
        if self._feature_resolver is not None:
            feature_event = self._feature_resolver.resolve(context.feature_snapshot_ref)
        else:
            feature_event = self.event_store.get(context.feature_snapshot_ref)
        if feature_event is None:
            # R4-D3：ref 查询 miss。stream_cache 的 LRU 可能淘汰了这条 event，
            # event_store 异步落盘可能还没完成。做一次 latest(topic, symbol) 兜底：
            # 同 symbol 的最近一条 feature snapshot 大概率就是 ref 指向的那条或
            # 一个略新版本。hard-raise 会让本轮决策彻底丢失；这里 WARNING + fallback
            # 优先提供服务连续性，同时记录偏差以便 ops 审计。若 latest 也 miss
            # （event_store 完全没有该 symbol 的 feature 记录，如冷启动）才 raise。
            fallback_event = self.event_store.latest(topics.FEATURE_SNAPSHOTS, context.symbol)
            if fallback_event is None:
                raise RuntimeError("Feature snapshot reference is missing from the event store")

            # Bug-3 修复：fallback 可能是"很久以前的 snapshot"（event_store 长期
            # 持有历史）。如果 fallback 的 snapshot_ts 距 decision 时间 >
            # max_stale_seconds，读旧行情做决策比 raise 更危险 → 拒绝。
            # 阈值与开关可 settings 配置，紧急回滚关 flag 即回旧行为。
            if self.settings.strategy_baseline_fallback_ts_check_enabled:
                max_stale = float(self.settings.strategy_baseline_fallback_max_stale_seconds)
                raw_ts = fallback_event.payload.get("snapshot_ts") if isinstance(fallback_event.payload, dict) else None
                fallback_dt = _parse_snapshot_ts(raw_ts)
                decision_dt = context.created_at
                if decision_dt is not None and decision_dt.tzinfo is None:
                    decision_dt = decision_dt.replace(tzinfo=timezone.utc)
                if fallback_dt is not None and decision_dt is not None:
                    # R2-M1 审查修复: 用 abs() 防 clock skew 漏检。若本地时钟
                    # 早于 event_store 里的 snapshot_ts (容器时钟不同步 / NATS
                    # 延迟 + OKX 服务器时间超前本地), signed age < 0, 原比较
                    # age > max_stale 恒 False → 真陈旧的 fallback 被放行做决策。
                    # abs 兼顾"时钟偏差异常"(负大) 和"fallback 真过期"(正大) 两种
                    # 都拒绝交易。若确实是 skew 而非 stale，ops 应看到
                    # baseline_feature_fallback_clock_skew 告警去修 NTP.
                    signed_age = (decision_dt - fallback_dt).total_seconds()
                    age_seconds = abs(signed_age)
                    if signed_age < -max_stale:
                        _LOGGER.warning(
                            "baseline_feature_fallback_clock_skew",
                            extra={
                                "decision_id": context.decision_id,
                                "symbol": context.symbol,
                                "fallback_event_id": fallback_event.event_id,
                                "signed_age_seconds": signed_age,
                                "max_stale_seconds": max_stale,
                            },
                        )
                    if age_seconds > max_stale:
                        _LOGGER.error(
                            "baseline_feature_fallback_stale_refused",
                            extra={
                                "decision_id": context.decision_id,
                                "symbol": context.symbol,
                                "fallback_event_id": fallback_event.event_id,
                                "fallback_age_seconds": age_seconds,
                                "signed_age_seconds": signed_age,
                                "max_stale_seconds": max_stale,
                            },
                        )
                        raise RuntimeError(
                            "Feature snapshot fallback is stale by "
                            f"{age_seconds:.1f}s |abs| (signed={signed_age:.1f}s, "
                            f"limit={max_stale:.1f}s); refusing baseline decision "
                            "to avoid trading on outdated or clock-skewed market state"
                        )

            _LOGGER.warning(
                "baseline_feature_ref_miss_fallback",
                extra={
                    "decision_id": context.decision_id,
                    "symbol": context.symbol,
                    "requested_ref": context.feature_snapshot_ref,
                    "fallback_event_id": fallback_event.event_id,
                    "fallback_topic": fallback_event.topic,
                },
            )
            feature_event = fallback_event

        features = FeatureSnapshot.model_validate(feature_event.payload)
        analysis = features.analysis_context
        alpha_score = features.composite_alpha_score
        microstructure_alpha = analysis.alpha_factors.microstructure_alpha if analysis is not None else 0.0
        direction_bias, direction_rule, direction_threshold = self._direction_bias(
            alpha_score=alpha_score,
            regime_indicator=features.regime_indicator,
            microstructure_alpha=microstructure_alpha,
            directional_alignment=analysis.multi_timeframe.directional_alignment if analysis is not None else "flat",
            analysis=analysis,
        )
        position_scale = features.suggested_position_scale
        volatility_scale = features.volatility_target_scale
        confidence = min(
            max(
                0.35
                + (abs(alpha_score) * 0.35)
                + (features.regime_confidence * 0.2)
                + (position_scale * 0.1),
                0.4,
            ),
            0.96,
        )
        reason_codes = ["baseline_multi_factor_alpha", f"regime_{features.regime_indicator}"]
        if direction_rule:
            reason_codes.append(direction_rule)
        if direction_threshold is not None:
            reason_codes.append(
                f"baseline_direction_threshold_{features.regime_indicator}_{direction_threshold:.3f}".replace(".", "_")
            )
        factor_scores: dict[str, float] = {}
        if analysis is not None:
            reason_codes.append(f"mtf_alignment_{analysis.multi_timeframe.directional_alignment}")
            # R3-M1 审查修复: 4 个新 alpha (basis/funding/oi/ls) 已按 P1.4/1.5/1.6/
            # 2.7 进入 composite_alpha_score 权重合成, 但之前未纳入 factor_scores
            # 和 reason_codes → BaselineAssessment 审计日志看不到它们的贡献,
            # 真金白银决策失败归因和回测对齐都不可能. 现在完整纳入.
            factor_scores = {
                "momentum_alpha": analysis.alpha_factors.momentum_alpha,
                "trend_alpha": analysis.alpha_factors.trend_alpha,
                "regime_alpha": analysis.alpha_factors.regime_alpha,
                "multi_timeframe_alpha": analysis.alpha_factors.multi_timeframe_alpha,
                "microstructure_alpha": analysis.alpha_factors.microstructure_alpha,
                "basis_alpha": analysis.alpha_factors.basis_alpha,
                "funding_alpha": analysis.alpha_factors.funding_alpha,
                "oi_alpha": analysis.alpha_factors.oi_alpha,
                "ls_alpha": analysis.alpha_factors.ls_alpha,
                "liquidity_scale": analysis.alpha_factors.liquidity_scale,
            }
            reason_codes.extend(self._factor_reason_codes(analysis))
            reason_codes.extend(
                self._microstructure_reason_codes(
                    direction_bias=direction_bias,
                    microstructure_alpha=analysis.alpha_factors.microstructure_alpha,
                )
            )
            if features.liquidity_score < 0.3:
                reason_codes.append("liquidity_thin")
        return BaselineAssessment(
            decision_id=context.decision_id,
            symbol=context.symbol,
            regime=features.regime_indicator,
            direction_bias=direction_bias,
            trend_strength=features.trend_strength,
            volatility_state=features.volatility_state,
            confidence=confidence,
            composite_alpha_score=alpha_score,
            suggested_position_scale=position_scale,
            volatility_target_scale=volatility_scale,
            direction_threshold=direction_threshold,
            direction_rule=direction_rule,
            factor_scores=factor_scores,
            holding_horizon=context.timeframe,
            invalidation_conditions=["feature_regime_flip"],
            reason_codes=reason_codes,
            engine_version="0.2.0",
        )

    def _direction_bias(
        self,
        *,
        alpha_score: float,
        regime_indicator: RegimeIndicator,
        microstructure_alpha: float,
        directional_alignment: DirectionalBias,
        analysis: AnalysisContext | None,
    ) -> tuple[DirectionalBias, str | None, float | None]:
        impulse_bias = self._impulse_override_bias(
            alpha_score=alpha_score,
            regime_indicator=regime_indicator,
            microstructure_alpha=microstructure_alpha,
            directional_alignment=directional_alignment,
            analysis=analysis,
        )
        if impulse_bias != "flat":
            return impulse_bias, f"baseline_impulse_override_{impulse_bias}", None

        breakout_threshold = float(self.settings.strategy_baseline_breakout_alpha_threshold)
        trend_threshold = float(self.settings.strategy_baseline_trend_alpha_threshold)
        range_threshold = float(self.settings.strategy_baseline_range_alpha_threshold)
        uncertain_threshold = float(self.settings.strategy_baseline_uncertain_alpha_threshold)
        alignment_bonus = (
            float(self.settings.strategy_baseline_alignment_bonus)
            if directional_alignment in {"long", "short"}
            else 0.0
        )

        # Bug-2 修复（去除 microstructure double-dipping）:
        #   原实现让 microstructure_alpha 既进入 composite_alpha_score（在
        #   FeatureCalculator._alpha_factors 里权重 0.15），又在 adjusted_threshold
        #   里以 "support / conflict" 两种方式改动决策阈值 —— 同一信号源两处影响
        #   决策，隐式权重远大于设计值，conflicts 下双重惩罚。
        #
        #   修复：alpha_score 已经吃过 micro 的 15% 权重，决策门槛不再二次读 micro。
        #   alignment_bonus 保留，但它来自 directional_alignment（multi_timeframe 对
        #   齐信号，与 micro 独立），语义正交，不属于 double-dipping。
        #
        #   range_regime 分支里仍有 microstructure_alpha 的独立使用（见下文），
        #   那是 "方向确认" 语义（range 内只允许强 micro 支持的方向翻转），
        #   与 "阈值调整" 是两件事，保留。
        def adjusted_threshold(value: float) -> float:
            return max(
                value - alignment_bonus,
                float(self.settings.strategy_baseline_min_threshold_floor),
            )

        if regime_indicator == "breakout":
            threshold = adjusted_threshold(breakout_threshold)
            if alpha_score >= threshold:
                return "long", "baseline_regime_breakout_threshold_crossed", threshold
            if alpha_score <= -threshold:
                return "short", "baseline_regime_breakout_threshold_crossed", threshold
            return "flat", "baseline_regime_breakout_threshold_not_met", threshold
        if regime_indicator == "trend":
            threshold = adjusted_threshold(trend_threshold)
            if alpha_score >= threshold:
                return "long", "baseline_regime_trend_threshold_crossed", threshold
            if alpha_score <= -threshold:
                return "short", "baseline_regime_trend_threshold_crossed", threshold
            return "flat", "baseline_regime_trend_threshold_not_met", threshold
        if regime_indicator == "range":
            threshold = adjusted_threshold(range_threshold)
            range_micro_floor = float(self.settings.strategy_baseline_range_microstructure_floor)
            if alpha_score >= threshold and microstructure_alpha >= range_micro_floor:
                return "long", "baseline_regime_range_threshold_crossed", threshold
            if alpha_score <= -threshold and microstructure_alpha <= -range_micro_floor:
                return "short", "baseline_regime_range_threshold_crossed", threshold
            return "flat", "baseline_regime_range_threshold_not_met", threshold
        if regime_indicator == "uncertain":
            threshold = adjusted_threshold(uncertain_threshold)
            uncertain_micro = float(self.settings.strategy_baseline_uncertain_microstructure_threshold)
            if alpha_score >= threshold and microstructure_alpha > uncertain_micro:
                return "long", "baseline_regime_uncertain_threshold_crossed", threshold
            if alpha_score <= -threshold and microstructure_alpha < -uncertain_micro:
                return "short", "baseline_regime_uncertain_threshold_crossed", threshold
            return "flat", "baseline_regime_uncertain_threshold_not_met", threshold
        return "flat", "baseline_direction_bias_flat", None

    def _impulse_override_bias(
        self,
        *,
        alpha_score: float,
        regime_indicator: RegimeIndicator,
        microstructure_alpha: float,
        directional_alignment: DirectionalBias,
        analysis: AnalysisContext | None,
    ) -> DirectionalBias:
        if (
            not self.settings.strategy_baseline_impulse_override_enabled
            or analysis is None
            or regime_indicator not in set(self.settings.strategy_baseline_impulse_allowed_regimes)
        ):
            return "flat"
        primary = analysis.timeframe_features.get("15m")
        if primary is None:
            return "flat"
        body_ratio = float(primary.candle_body_ratio)
        range_ratio = float(primary.range_ratio)
        momentum_score = float(primary.momentum_score)
        alpha_min = float(self.settings.strategy_baseline_impulse_alpha_min)
        micro_min = float(self.settings.strategy_baseline_impulse_microstructure_min)
        momentum_min = float(self.settings.strategy_baseline_impulse_momentum_min)
        range_min = float(self.settings.strategy_baseline_impulse_range_ratio_min)
        body_min = float(self.settings.strategy_baseline_impulse_body_ratio_min)

        def aligned(direction: DirectionalBias) -> bool:
            if not self.settings.strategy_baseline_impulse_require_mtf_alignment:
                return True
            return directional_alignment == direction

        if (
            alpha_score >= alpha_min
            and microstructure_alpha >= micro_min
            and momentum_score >= momentum_min
            and range_ratio >= range_min
            and body_ratio >= body_min
            and aligned("long")
        ):
            return "long"
        if (
            alpha_score <= -alpha_min
            and microstructure_alpha <= -micro_min
            and momentum_score <= -momentum_min
            and range_ratio >= range_min
            and body_ratio >= body_min
            and aligned("short")
        ):
            return "short"
        return "flat"

    @staticmethod
    def _factor_reason_codes(analysis: AnalysisContext) -> list[str]:
        reason_codes: list[str] = []
        factors = analysis.alpha_factors
        if abs(factors.momentum_alpha) >= 0.2:
            reason_codes.append("alpha_momentum_support")
        if abs(factors.trend_alpha) >= 0.15:
            reason_codes.append("alpha_trend_support")
        if abs(factors.regime_alpha) >= 0.15:
            reason_codes.append("alpha_regime_support")
        if abs(factors.multi_timeframe_alpha) >= 0.15:
            reason_codes.append("alpha_multi_timeframe_support")
        # R3-M1 审查修复: 新 alpha (basis/funding/oi/ls) 的贡献反映在 reason_codes,
        # 审计日志里可以看到"basis 在超买时抑制 long / funding 显示多头拥挤"等
        # 语义可读信号, 方便实盘失败归因. 阈值 0.15 与既有 alpha 一致.
        if abs(factors.basis_alpha) >= 0.15:
            side = "contrarian_long" if factors.basis_alpha > 0 else "contrarian_short"
            reason_codes.append(f"alpha_basis_{side}")
        if abs(factors.funding_alpha) >= 0.15:
            side = "funding_long_bias" if factors.funding_alpha > 0 else "funding_short_bias"
            reason_codes.append(f"alpha_{side}")
        if abs(factors.oi_alpha) >= 0.15:
            side = "oi_long_confirming" if factors.oi_alpha > 0 else "oi_short_confirming"
            reason_codes.append(f"alpha_{side}")
        if abs(factors.ls_alpha) >= 0.15:
            side = "ls_contrarian_long" if factors.ls_alpha > 0 else "ls_contrarian_short"
            reason_codes.append(f"alpha_{side}")
        if analysis.position_sizing.volatility_target_scale < 0.8:
            reason_codes.append("volatility_targeting_reduced_size")
        elif analysis.position_sizing.volatility_target_scale > 1.05:
            reason_codes.append("volatility_targeting_expanded_size")
        return reason_codes

    @staticmethod
    def _microstructure_reason_codes(
        *,
        direction_bias: DirectionalBias,
        microstructure_alpha: float,
    ) -> list[str]:
        if abs(microstructure_alpha) < 0.08:
            return ["microstructure_neutral"]
        if direction_bias == "long" and microstructure_alpha > 0.0:
            return ["microstructure_confirms_long"]
        if direction_bias == "short" and microstructure_alpha < 0.0:
            return ["microstructure_confirms_short"]
        if direction_bias == "flat":
            return ["microstructure_not_strong_enough"]
        return ["microstructure_conflicts_with_direction"]
