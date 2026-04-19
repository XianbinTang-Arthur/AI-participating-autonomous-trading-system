from __future__ import annotations

import math
from typing import Literal

from aats.bus.base import EventBus
from aats.events import topics
from aats.events.envelopes import parse_envelope, publish_model
from aats.schemas.features import (
    AlphaFactorSet,
    AnalysisContext,
    DirectionalBias,
    FeatureSnapshot,
    MultiTimeframeContext,
    PositionSizingContext,
    TimeframeFeatureSet,
)
from aats.schemas.market import KlineBar, MarketSnapshot
from aats.services.feature_engine.liquidity import LiquidityAnalyzer
from aats.services.feature_engine.regime import RegimeClassifier
from aats.services.feature_engine.timeseries import (
    DEFAULT_ATR_WINDOW,
    DEFAULT_MAX_BARS,
    DEFAULT_ROC_WINDOW,
    RollingCandleState,
)
from aats.services.feature_engine.trend import TrendCalculator
from aats.services.feature_engine.volatility import VolatilityAnalyzer


class FeatureCalculator:
    def __init__(
        self,
        *,
        trend: TrendCalculator | None = None,
        volatility: VolatilityAnalyzer | None = None,
        liquidity: LiquidityAnalyzer | None = None,
        regime: RegimeClassifier | None = None,
        enable_timeseries_smoothing: bool = True,
        rolling_max_bars: int = DEFAULT_MAX_BARS,
        rolling_roc_window: int = DEFAULT_ROC_WINDOW,
        rolling_atr_window: int = DEFAULT_ATR_WINDOW,
        enable_basis_signal: bool = True,
        basis_scale_bps: float = 10.0,
    ) -> None:
        self.trend = trend or TrendCalculator()
        self.volatility = volatility or VolatilityAnalyzer()
        self.liquidity = liquidity or LiquidityAnalyzer()
        self.regime = regime or RegimeClassifier()
        # Bug-1 时序平滑开关。默认开启；关闭后所有 timeframe 走单 K 线瞬时路径
        # （紧急回滚）。
        self.enable_timeseries_smoothing = enable_timeseries_smoothing
        self._rolling_max_bars = rolling_max_bars
        self._rolling_roc_window = rolling_roc_window
        self._rolling_atr_window = rolling_atr_window
        # P1.4 Mark price basis 信号开关 + 灵敏度参数。关闭后 basis_alpha 恒为 0,
        # composite 权重仍按新公式（含 0 贡献 basis）—— 等价于 "basis 不参与"。
        self.enable_basis_signal = enable_basis_signal
        self.basis_scale_bps = basis_scale_bps
        # Per (symbol, timeframe) 的滚动状态。跨 calculate() 调用累积历史。
        # 同 ts 的 update 幂等 → 同 snapshot 多次 calculate 结果一致
        # （守 test_feature_calculation_is_deterministic_for_same_snapshot 契约）。
        self._rolling_states: dict[tuple[str, str], RollingCandleState] = {}

    def _get_rolling_state(self, symbol: str, timeframe: str) -> RollingCandleState:
        key = (symbol, timeframe)
        state = self._rolling_states.get(key)
        if state is None:
            state = RollingCandleState(
                symbol=symbol,
                timeframe=timeframe,
                max_bars=self._rolling_max_bars,
                roc_window=self._rolling_roc_window,
                atr_window=self._rolling_atr_window,
            )
            self._rolling_states[key] = state
        return state

    def rolling_state_snapshot(self) -> dict[tuple[str, str], RollingCandleState]:
        """Observability helper — 暴露当前 state 引用供 warmup 和诊断使用.

        返回字典的 values 与内部共享引用（非拷贝）；warmup.py 通过它直接
        prewarm 已注册的 state，避免重复构造。
        """
        return self._rolling_states

    def register_rolling_state(
        self,
        *,
        symbol: str,
        timeframe: str,
    ) -> RollingCandleState:
        """显式注册 (symbol, timeframe) 对应 state，供 warmup 批量预热调用.

        这样 warmup 可以在任何 market snapshot 到来前就提前构造好 state。
        如已存在则返回既有实例。
        """
        return self._get_rolling_state(symbol, timeframe)

    def calculate(self, snapshot: MarketSnapshot, *, market_snapshot_ref: str | None = None) -> FeatureSnapshot:
        features_15m = self._timeframe_features(snapshot=snapshot, timeframe="15m", kline=snapshot.kline_15m)
        features_1h = self._timeframe_features(snapshot=snapshot, timeframe="1h", kline=snapshot.kline_1h)
        liquidity = self.liquidity.calculate(snapshot)
        regime = self.regime.classify(
            momentum_15m=features_15m.momentum_score,
            momentum_1h=features_1h.momentum_score,
            trend_strength_15m=features_15m.trend_strength,
            trend_strength_1h=features_1h.trend_strength,
            volatility_state_15m=features_15m.volatility_state,
            volatility_state_1h=features_1h.volatility_state,
            liquidity_score=liquidity.liquidity_score,
        )
        multi_timeframe_context = self._multi_timeframe_context(
            snapshot_ts=snapshot.snapshot_ts,
            features_15m=features_15m,
            features_1h=features_1h,
            regime_alignment_score=regime.regime_alignment_score,
        )
        alpha_factors = self._alpha_factors(
            features_15m=features_15m,
            features_1h=features_1h,
            multi_timeframe=multi_timeframe_context,
            liquidity_score=liquidity.liquidity_score,
            top_of_book_imbalance=liquidity.top_of_book_imbalance,
            depth_imbalance=liquidity.depth_imbalance,
            trade_flow_imbalance=liquidity.trade_flow_imbalance,
            execution_quality_scale=liquidity.execution_quality_scale,
            spread_penalty=liquidity.spread_penalty,
            regime_indicator=regime.regime_indicator,
            regime_confidence=regime.regime_confidence,
            regime_bias=regime.trend_bias,
            last_price=float(snapshot.last_price),
            mark_price=float(snapshot.mark_price) if snapshot.mark_price is not None else None,
            basis_signal_enabled=self.enable_basis_signal,
            basis_scale_bps=self.basis_scale_bps,
        )
        position_sizing = self._position_sizing_context(
            alpha_factors=alpha_factors,
            execution_quality_scale=liquidity.execution_quality_scale,
            volatility_state=features_15m.volatility_state,
            volatility_value=features_15m.volatility_value,
        )
        analysis_context = AnalysisContext(
            created_at=snapshot.snapshot_ts,
            symbol=snapshot.symbol,
            snapshot_ts=snapshot.snapshot_ts,
            analysis_version="0.2.0",
            regime_version="0.2.0",
            trend_bias=regime.trend_bias,
            regime_indicator=regime.regime_indicator,
            regime_confidence=regime.regime_confidence,
            regime_reasons=list(regime.reasons),
            timeframe_features={
                "15m": features_15m,
                "1h": features_1h,
            },
            liquidity=liquidity,
            multi_timeframe=multi_timeframe_context,
            alpha_factors=alpha_factors,
            position_sizing=position_sizing,
        )
        return FeatureSnapshot(
            created_at=snapshot.snapshot_ts,
            symbol=snapshot.symbol,
            snapshot_ts=snapshot.snapshot_ts,
            market_snapshot_ref=market_snapshot_ref,
            trend_strength=features_15m.trend_strength,
            volatility_state=features_15m.volatility_state,
            volatility_value=features_15m.volatility_value,
            momentum_score=features_15m.momentum_score,
            liquidity_score=liquidity.liquidity_score,
            regime_indicator=regime.regime_indicator,
            regime_confidence=regime.regime_confidence,
            multi_timeframe_alignment=multi_timeframe_context.regime_alignment_score,
            composite_alpha_score=alpha_factors.composite_alpha_score,
            suggested_position_scale=position_sizing.suggested_position_scale,
            volatility_target_scale=position_sizing.volatility_target_scale,
            feature_version="0.2.0",
            analysis_context=analysis_context,
        )

    def _timeframe_features(
        self,
        *,
        snapshot: MarketSnapshot,
        timeframe: Literal["15m", "1h"],
        kline: KlineBar,
    ) -> TimeframeFeatureSet:
        if self.enable_timeseries_smoothing:
            state = self._get_rolling_state(snapshot.symbol, timeframe)
            # 幂等：同 snapshot_ts 反复 update 覆盖末尾 bar，不推进 EMA/窗口。
            # test_feature_calculation_is_deterministic_for_same_snapshot 依赖此契约。
            state.update(kline, ts=snapshot.snapshot_ts)
            trend_metrics = self.trend.analyze_with_state(state, kline)
            volatility_metrics = self.volatility.analyze_with_state(state, kline)
        else:
            # 退化路径（feature flag off）：保留旧单 K 线行为
            trend_metrics = self.trend.analyze_kline(kline)
            volatility_metrics = self.volatility.analyze_kline(kline)
        return TimeframeFeatureSet(
            created_at=snapshot.snapshot_ts,
            timeframe=timeframe,
            open_price=kline.open,
            high_price=kline.high,
            low_price=kline.low,
            close_price=kline.close,
            momentum_score=trend_metrics.momentum_score,
            trend_strength=trend_metrics.trend_strength,
            volatility_value=volatility_metrics.volatility_value,
            volatility_state=volatility_metrics.volatility_state,
            candle_body_ratio=trend_metrics.candle_body_ratio,
            range_ratio=volatility_metrics.range_ratio,
        )

    @staticmethod
    def _multi_timeframe_context(
        *,
        snapshot_ts,
        features_15m: TimeframeFeatureSet,
        features_1h: TimeframeFeatureSet,
        regime_alignment_score: float,
    ) -> MultiTimeframeContext:
        direction_15m = FeatureCalculator._direction(features_15m.momentum_score)
        direction_1h = FeatureCalculator._direction(features_1h.momentum_score)
        if direction_15m == direction_1h:
            directional_alignment = direction_15m
        else:
            directional_alignment = "mixed"
        # Scale the raw momentum difference by 50x so that typical crypto
        # divergences (0.001–0.02) map to a meaningful 0–1 range instead of
        # being perpetually stuck near 1.0.
        momentum_alignment_score = max(
            1.0 - abs(features_15m.momentum_score - features_1h.momentum_score) * 50.0,
            0.0,
        )
        if abs(features_15m.trend_strength) > abs(features_1h.trend_strength) + 0.1:
            dominant_timeframe = "15m"
        elif abs(features_1h.trend_strength) > abs(features_15m.trend_strength) + 0.1:
            dominant_timeframe = "1h"
        else:
            dominant_timeframe = "balanced"
        return MultiTimeframeContext(
            created_at=snapshot_ts,
            directional_alignment=directional_alignment,
            momentum_alignment_score=max(min(momentum_alignment_score, 1.0), -1.0),
            regime_alignment_score=max(min(regime_alignment_score, 1.0), 0.0),
            dominant_timeframe=dominant_timeframe,
        )

    @staticmethod
    def _direction(momentum: float) -> DirectionalBias:
        if momentum > 0.0:
            return "long"
        if momentum < 0.0:
            return "short"
        return "flat"

    @staticmethod
    def _alpha_factors(
        *,
        features_15m: TimeframeFeatureSet,
        features_1h: TimeframeFeatureSet,
        multi_timeframe: MultiTimeframeContext,
        liquidity_score: float,
        top_of_book_imbalance: float,
        depth_imbalance: float,
        trade_flow_imbalance: float,
        execution_quality_scale: float,
        spread_penalty: float,
        regime_indicator: str,
        regime_confidence: float,
        regime_bias: str,
        last_price: float,
        mark_price: float | None,
        basis_signal_enabled: bool,
        basis_scale_bps: float,
    ) -> AlphaFactorSet:
        momentum_alpha = FeatureCalculator._clamp(
            (features_15m.momentum_score * 140.0 * 0.65) + (features_1h.momentum_score * 90.0 * 0.35),
            -1.0,
            1.0,
        )
        trend_alpha = FeatureCalculator._clamp(
            (
                FeatureCalculator._direction_sign(features_15m.momentum_score) * features_15m.trend_strength * 0.65
                + FeatureCalculator._direction_sign(features_1h.momentum_score) * features_1h.trend_strength * 0.35
            ),
            -1.0,
            1.0,
        )
        regime_weight = 1.0 if regime_indicator in {"trend", "breakout"} else 0.35 if regime_indicator == "uncertain" else 0.0
        regime_alpha = FeatureCalculator._clamp(
            FeatureCalculator._direction_sign_from_bias(regime_bias) * regime_confidence * regime_weight,
            -1.0,
            1.0,
        )
        multi_timeframe_alpha = FeatureCalculator._clamp(
            FeatureCalculator._direction_sign_from_bias(multi_timeframe.directional_alignment)
            * multi_timeframe.momentum_alignment_score
            * multi_timeframe.regime_alignment_score,
            -1.0,
            1.0,
        )
        microstructure_direction = FeatureCalculator._clamp(
            (top_of_book_imbalance * 0.25)
            + (depth_imbalance * 0.4)
            + (trade_flow_imbalance * 0.35),
            -1.0,
            1.0,
        )
        microstructure_alpha = FeatureCalculator._clamp(
            microstructure_direction * execution_quality_scale * (1.0 - min(spread_penalty * 0.5, 0.45)),
            -1.0,
            1.0,
        )
        # P1.4 mark-price basis alpha：last vs mark 偏离度 → 超买/超卖反转倾向.
        # basis_bps > 0 (last 高于 mark) → 短期超买 → basis_alpha 负
        # basis_bps < 0 (last 低于 mark) → 短期超卖 → basis_alpha 正
        # 用 tanh 做 S 型饱和，basis_scale_bps 定义 "达到 ±0.76 饱和" 的 bps。
        basis_alpha = 0.0
        if (
            basis_signal_enabled
            and mark_price is not None
            and mark_price > 0.0
            and last_price > 0.0
            and basis_scale_bps > 0.0
        ):
            basis_bps = (last_price - mark_price) / mark_price * 10_000.0
            basis_alpha = FeatureCalculator._clamp(
                -math.tanh(basis_bps / basis_scale_bps),
                -1.0,
                1.0,
            )
        liquidity_scale = FeatureCalculator._clamp(0.45 + (liquidity_score * 0.55), 0.25, 1.0)
        # Composite 权重重分配（P1.4）：basis 引入 0.12 权重，其他因子按比例让出。
        # 权重总和严格为 1.00: momentum 0.30 + trend 0.20 + regime 0.15 +
        # multi_tf 0.11 + micro 0.12 + basis 0.12 = 1.00。
        composite_alpha_score = FeatureCalculator._clamp(
            (
                momentum_alpha * 0.30
                + trend_alpha * 0.20
                + regime_alpha * 0.15
                + multi_timeframe_alpha * 0.11
                + microstructure_alpha * 0.12
                + basis_alpha * 0.12
            )
            * liquidity_scale,
            -1.0,
            1.0,
        )
        conviction_score = FeatureCalculator._clamp(
            (abs(composite_alpha_score) * 0.7)
            + (regime_confidence * 0.15)
            + (multi_timeframe.regime_alignment_score * 0.08)
            + (execution_quality_scale * 0.07),
            0.0,
            1.0,
        )
        return AlphaFactorSet(
            created_at=features_15m.created_at,
            momentum_alpha=momentum_alpha,
            trend_alpha=trend_alpha,
            regime_alpha=regime_alpha,
            multi_timeframe_alpha=multi_timeframe_alpha,
            microstructure_alpha=microstructure_alpha,
            basis_alpha=basis_alpha,
            liquidity_scale=liquidity_scale,
            composite_alpha_score=composite_alpha_score,
            conviction_score=conviction_score,
        )

    @staticmethod
    def _position_sizing_context(
        *,
        alpha_factors: AlphaFactorSet,
        execution_quality_scale: float,
        volatility_state: str,
        volatility_value: float,
    ) -> PositionSizingContext:
        volatility_target_scale = {
            "low": 1.1,
            "medium": 1.0,
            "high": 0.65,
        }.get(volatility_state, 0.85)
        if volatility_value > 0.04:
            volatility_target_scale *= 0.85
        elif volatility_value < 0.01:
            volatility_target_scale *= 1.05
        volatility_target_scale = FeatureCalculator._clamp(volatility_target_scale, 0.45, 1.2)
        suggested_position_scale = FeatureCalculator._clamp(
            alpha_factors.conviction_score
            * alpha_factors.liquidity_scale
            * execution_quality_scale
            * volatility_target_scale,
            0.0,
            1.0,
        )
        if abs(alpha_factors.composite_alpha_score) >= 0.18:
            suggested_position_scale = max(
                suggested_position_scale,
                0.2 * max(min(execution_quality_scale, 1.0), 0.1),
            )
        return PositionSizingContext(
            created_at=alpha_factors.created_at,
            volatility_target_scale=volatility_target_scale,
            liquidity_scale=alpha_factors.liquidity_scale,
            execution_quality_scale=execution_quality_scale,
            conviction_scale=alpha_factors.conviction_score,
            suggested_position_scale=suggested_position_scale,
        )

    @staticmethod
    def _direction_sign(momentum: float) -> float:
        if momentum > 0.0:
            return 1.0
        if momentum < 0.0:
            return -1.0
        return 0.0

    @staticmethod
    def _direction_sign_from_bias(direction_bias: str) -> float:
        if direction_bias == "long":
            return 1.0
        if direction_bias == "short":
            return -1.0
        return 0.0

    @staticmethod
    def _clamp(value: float, lower: float, upper: float) -> float:
        return max(lower, min(value, upper))


class FeatureEngine:
    def __init__(self, *, bus: EventBus, calculator: FeatureCalculator) -> None:
        self.bus = bus
        self.calculator = calculator
        self._latest_snapshots: dict[str, FeatureSnapshot] = {}

    def latest_snapshot(self, symbol: str) -> FeatureSnapshot | None:
        return self._latest_snapshots.get(symbol)

    async def handle_market_snapshot(self, message: dict) -> None:
        envelope = parse_envelope(message)
        market_snapshot = MarketSnapshot.model_validate(envelope.payload)
        feature_snapshot = self.calculator.calculate(
            market_snapshot,
            market_snapshot_ref=envelope.event_id,
        )
        self._latest_snapshots[feature_snapshot.symbol] = feature_snapshot
        await publish_model(
            bus=self.bus,
            topic=topics.FEATURE_SNAPSHOTS,
            key=feature_snapshot.symbol,
            payload_model=feature_snapshot,
            source_component="feature_engine",
        )
