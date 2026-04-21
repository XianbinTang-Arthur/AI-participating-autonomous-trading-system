from __future__ import annotations

import asyncio
import logging
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
from aats.services.feature_engine.long_short_poller import LongShortRatioPoller
from aats.services.feature_engine.oi_state import (
    DEFAULT_OI_DEAD_ZONE,
    DEFAULT_OI_EMA_PERIOD,
    DEFAULT_OI_MAX_SNAPSHOTS,
    OpenInterestState,
)
from aats.services.feature_engine.regime import RegimeClassifier
from aats.services.feature_engine.timeseries import (
    DEFAULT_ATR_WINDOW,
    DEFAULT_MAX_BARS,
    DEFAULT_ROC_WINDOW,
    RollingCandleState,
)
from aats.services.feature_engine.trend import TrendCalculator
from aats.services.feature_engine.volatility import VolatilityAnalyzer

# Task P3-1：module-level logger 从 imports 中间挪到 imports 后，消除 E402。
_LOGGER_CALC = logging.getLogger("aats.feature_engine.calculator")


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
        enable_funding_signal: bool = True,
        funding_scale: float = 2000.0,
        enable_oi_signal: bool = True,
        oi_max_snapshots: int = DEFAULT_OI_MAX_SNAPSHOTS,
        oi_ema_period: int = DEFAULT_OI_EMA_PERIOD,
        oi_dead_zone: float = DEFAULT_OI_DEAD_ZONE,
        enable_regime_adx: bool = True,
        long_short_poller: LongShortRatioPoller | None = None,
        enable_ls_ratio_signal: bool = False,
        ls_ratio_scale: float = 2.0,
        ls_ratio_max_staleness_seconds: float = 900.0,
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
        # P1.5 Funding rate 拥挤度信号。funding_scale=2000 → funding=0.0005 对
        # 应 funding_alpha ≈ -tanh(1.0) ≈ -0.76。关闭等价 0 贡献.
        self.enable_funding_signal = enable_funding_signal
        self.funding_scale = funding_scale
        # P1.6 Open Interest 方向性信号。per-symbol OpenInterestState 持有滚动
        # 历史+EMA。oi_dead_zone：|delta|<0.5% 视为噪声 oi_alpha=0，避免窄幅
        # 震荡的 OI 呼吸 false-trigger。关闭时 oi_alpha 恒 0.
        self.enable_oi_signal = enable_oi_signal
        self._oi_max_snapshots = oi_max_snapshots
        self._oi_ema_period = oi_ema_period
        self.oi_dead_zone = oi_dead_zone
        self._oi_states: dict[str, OpenInterestState] = {}
        # P2.9 ADX-driven regime classification 开关. 关闭 → 走旧 classify()
        # 即硬编码 momentum / trend_strength 阈值 (紧急回滚).
        self.enable_regime_adx = enable_regime_adx
        # P2.7 Long-Short ratio 情绪极值信号. 由独立 poller 拉取；FeatureCalculator
        # 仅读缓存. 默认关 (flag=False)，因为数据 5min 聚合与 tick 决策时间分辨率
        # 差 100 倍，需上线观察后决定是否打开. enable 时 poller 必须非 None.
        self._long_short_poller = long_short_poller
        self.enable_ls_ratio_signal = enable_ls_ratio_signal
        self.ls_ratio_scale = ls_ratio_scale
        self.ls_ratio_max_staleness_seconds = ls_ratio_max_staleness_seconds
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

    def _get_oi_state(self, symbol: str) -> OpenInterestState:
        state = self._oi_states.get(symbol)
        if state is None:
            state = OpenInterestState(
                symbol=symbol,
                max_snapshots=self._oi_max_snapshots,
                ema_period=self._oi_ema_period,
            )
            self._oi_states[symbol] = state
        return state

    def oi_state_snapshot(self) -> dict[str, OpenInterestState]:
        """Observability helper for OI states (symmetric with rolling_state_snapshot)."""
        return self._oi_states

    def _extract_price_roc(self, symbol: str) -> float | None:
        """Read ROC(5) from 15m rolling state for oi_alpha composition.

        OI 方向性信号需要与价格方向联合判断。优先用 15m 的 ROC (primary
        timeframe). state 未 ready 或未启用 smoothing 时返回 None →
        oi_alpha=0 (退化).
        """
        if not self.enable_timeseries_smoothing:
            return None
        key = (symbol, "15m")
        state = self._rolling_states.get(key)
        if state is None:
            return None
        ind = state.indicators()
        if not ind.ready:
            return None
        return ind.roc

    def _extract_ls_ratio(
        self, symbol: str, as_of_ts,
    ) -> float | None:
        """Read most recent ls_ratio from poller cache with staleness check.

        缓存过期 (> max_staleness_seconds) 或 poller 不可用 → None →
        ls_alpha = 0 (退化).
        """
        if self._long_short_poller is None:
            return None
        sample = self._long_short_poller.latest(symbol)
        if sample is None:
            return None
        try:
            cache_ts = sample.ts
            if cache_ts.tzinfo is None:
                # cache_ts 由 datetime.fromtimestamp(ms, tz=utc) 构造,必 aware.
                # 走到这里说明源码被改坏 — 拒绝使用而不是隐式补 UTC.
                _LOGGER_CALC.warning(
                    "ls_ratio_cache_ts_naive_rejected symbol=%s", symbol,
                )
                return None
            if as_of_ts is not None:
                if as_of_ts.tzinfo is None:
                    # R2-M3 审查修复: 原代码把 naive as_of_ts 强加 UTC，会把
                    # 来自脏数据 (MarketSnapshot.snapshot_ts 缺 tz) 的时刻当 UTC
                    # 时钟值，staleness 比较结果不可信 → 拒绝而不是静默假设.
                    _LOGGER_CALC.warning(
                        "ls_ratio_as_of_ts_naive_rejected symbol=%s", symbol,
                    )
                    return None
                age = abs((as_of_ts - cache_ts).total_seconds())
                if age > self.ls_ratio_max_staleness_seconds:
                    return None
        except Exception as exc:
            _LOGGER_CALC.warning(
                "ls_ratio_staleness_check_error symbol=%s error=%s",
                symbol, exc,
            )
            return None
        return sample.ls_ratio

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
        # P1.6 — 若本 tick 的 MarketSnapshot 带 open_interest，更新 per-symbol
        # OpenInterestState。幂等：同 snapshot_ts 反复调用结果一致
        # （同 RollingCandleState 契约）。
        oi_delta: float | None = None
        if self.enable_oi_signal and snapshot.open_interest is not None:
            oi_state = self._get_oi_state(snapshot.symbol)
            oi_state.update(float(snapshot.open_interest), ts=snapshot.snapshot_ts)
            oi_ind = oi_state.indicators()
            if oi_ind.ready:
                oi_delta = oi_ind.oi_delta
        liquidity = self.liquidity.calculate(snapshot)
        # P2.9 — ADX 驱动的 regime 分类 (开关 enable_regime_adx). state 未 ready
        # 或 ADX None → classify_with_adx 内部自动退化到 classify(). flag 关则
        # 完全走旧 classify 路径 (紧急回滚).
        adx_value: float | None = None
        plus_di_value: float | None = None
        minus_di_value: float | None = None
        if self.enable_regime_adx and self.enable_timeseries_smoothing:
            key_15m = (snapshot.symbol, "15m")
            primary_state = self._rolling_states.get(key_15m)
            if primary_state is not None:
                primary_ind = primary_state.indicators()
                if primary_ind.ready:
                    adx_value = primary_ind.adx
                    plus_di_value = primary_ind.plus_di
                    minus_di_value = primary_ind.minus_di
        if self.enable_regime_adx:
            regime = self.regime.classify_with_adx(
                adx=adx_value,
                plus_di=plus_di_value,
                minus_di=minus_di_value,
                momentum_15m=features_15m.momentum_score,
                momentum_1h=features_1h.momentum_score,
                trend_strength_15m=features_15m.trend_strength,
                trend_strength_1h=features_1h.trend_strength,
                volatility_state_15m=features_15m.volatility_state,
                volatility_state_1h=features_1h.volatility_state,
                liquidity_score=liquidity.liquidity_score,
            )
        else:
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
            funding_rate=float(snapshot.funding_rate) if snapshot.funding_rate is not None else None,
            funding_signal_enabled=self.enable_funding_signal,
            funding_scale=self.funding_scale,
            oi_delta=oi_delta,
            price_roc=self._extract_price_roc(snapshot.symbol),
            oi_signal_enabled=self.enable_oi_signal,
            oi_dead_zone=self.oi_dead_zone,
            ls_ratio=self._extract_ls_ratio(snapshot.symbol, snapshot.snapshot_ts),
            ls_signal_enabled=self.enable_ls_ratio_signal,
            ls_scale=self.ls_ratio_scale,
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
            # P0 Bug-1 follow-up: 优先用 kline.ts (K 线自己的时刻)，只有在 ts 缺失
            # (旧 payload / dict 构造) 时才 fallback 到 snapshot.snapshot_ts. 不能
            # 直接用 snapshot.snapshot_ts —— 它取所有数据源 max，会被 mark-price /
            # funding 推送拉到更新，导致同一根未闭合 K 线被当作"新 ts"反复 append
            # 到 deque、推进 EMA、破坏幂等契约 (M-4 审查发现).
            if kline.ts is None:
                # R2-N3 审查修复: fallback 路径静默会重新暴露 M-4 问题 (未闭合 K
                # 线被 mark/funding 拉 ts → 反复 append). Warning 让 replay/schema
                # evolution 场景可见; 希望长期 schema 升级到必填 ts.
                _LOGGER_CALC.warning(
                    "kline_ts_missing_fallback_to_snapshot_ts symbol=%s timeframe=%s",
                    snapshot.symbol, timeframe,
                )
                kline_ts = snapshot.snapshot_ts
            else:
                kline_ts = kline.ts
            state.update(kline, ts=kline_ts)
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
        funding_rate: float | None,
        funding_signal_enabled: bool,
        funding_scale: float,
        oi_delta: float | None,
        price_roc: float | None,
        oi_signal_enabled: bool,
        oi_dead_zone: float,
        ls_ratio: float | None,
        ls_signal_enabled: bool,
        ls_scale: float,
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
        # P1.5 funding_rate 拥挤度 alpha：funding 高 (多头付费重) → 多头过度拥挤
        # → funding_alpha 负抑制 long。funding_scale=2000 默认，让 fr=0.0005
        # 对应 ≈ -0.76。
        funding_alpha = 0.0
        if (
            funding_signal_enabled
            and funding_rate is not None
            and funding_scale > 0.0
        ):
            funding_alpha = FeatureCalculator._clamp(
                -math.tanh(funding_rate * funding_scale),
                -1.0,
                1.0,
            )
        # P1.6 Open Interest 方向性 alpha. 与 15m ROC 联合判断:
        #   价 ↑ OI ↑ → 新多头入场 (正 alpha，趋势确认)
        #   价 ↑ OI ↓ → 多头平仓反弹 (弱负 alpha, 短期谨慎)
        #   价 ↓ OI ↑ → 新空头入场 (负 alpha，趋势确认)
        #   价 ↓ OI ↓ → 多头平仓回调 (弱正 alpha)
        # dead zone: |oi_delta| < threshold 或 |roc| = 0 → oi_alpha=0 避免噪声
        oi_alpha = 0.0
        if (
            oi_signal_enabled
            and oi_delta is not None
            and price_roc is not None
            and abs(oi_delta) >= oi_dead_zone
        ):
            price_dir = 1.0 if price_roc > 0 else (-1.0 if price_roc < 0 else 0.0)
            oi_dir = 1.0 if oi_delta > 0 else -1.0
            if price_dir != 0.0:
                alignment = price_dir * oi_dir   # +1 同向 / -1 反向
                # magnitude: |oi_delta| × 10 缩放 (0.5% → 5%，饱和 1.0)
                magnitude = min(abs(oi_delta) * 10.0, 1.0)
                # 同向 (alignment=+1) → oi_alpha 与 price_dir 同号 (趋势确认)
                # 反向 (alignment=-1) → oi_alpha 与 price_dir 反号 (弱反转，magnitude 减半)
                if alignment > 0:
                    oi_alpha = FeatureCalculator._clamp(
                        price_dir * magnitude, -1.0, 1.0,
                    )
                else:
                    oi_alpha = FeatureCalculator._clamp(
                        -price_dir * magnitude * 0.5, -1.0, 1.0,
                    )
        # P2.7 Long-Short ratio 情绪极值 alpha.
        #   ls_ratio > 1 = 多头占优, >> 1 (如 3-5) 多头拥挤 → 反转 alpha 负
        #   ls_ratio < 1 = 空头占优, << 1 (如 0.2-0.33) 空头拥挤 → 反转 alpha 正
        # 公式用 (ls_ratio - 1) / scale 的 tanh 做 S 型饱和:
        #   ls_scale=2 → ls=3 (+2 from neutral 1) 对应 -tanh(1) ≈ -0.76
        # stale / 未获取 / flag off → ls_ratio = None → ls_alpha = 0.
        ls_alpha = 0.0
        if ls_signal_enabled and ls_ratio is not None and ls_ratio > 0 and ls_scale > 0:
            ls_alpha = FeatureCalculator._clamp(
                -math.tanh((ls_ratio - 1.0) / ls_scale),
                -1.0,
                1.0,
            )
        liquidity_scale = FeatureCalculator._clamp(0.45 + (liquidity_score * 0.55), 0.25, 1.0)
        # Composite 权重最终版（P2.7）：ls 引入 0.06, 其他按比例让出。严格归一 1.00:
        # momentum 0.24 + trend 0.17 + regime 0.12 + multi_tf 0.08 + micro 0.09
        # + basis 0.10 + funding 0.07 + oi 0.07 + ls 0.06 = 1.00.
        composite_alpha_score = FeatureCalculator._clamp(
            (
                momentum_alpha * 0.24
                + trend_alpha * 0.17
                + regime_alpha * 0.12
                + multi_timeframe_alpha * 0.08
                + microstructure_alpha * 0.09
                + basis_alpha * 0.10
                + funding_alpha * 0.07
                + oi_alpha * 0.07
                + ls_alpha * 0.06
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
            funding_alpha=funding_alpha,
            oi_alpha=oi_alpha,
            ls_alpha=ls_alpha,
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
        # R2-B2 审查修复: NATS push subscription 在 max_ack_pending > 1 下会
        # 并发调度 handler task. 同 symbol 两条 MARKET_SNAPSHOTS 同时到达时,
        # FeatureCalculator._get_rolling_state / _get_oi_state 的 setdefault
        # 式 get-or-create 在 await 切换点存在 double-create 竞态 → 一条 handler
        # 持有孤儿 state 写入丢失; RollingCandleState.update 的 deque.append /
        # EMA 读改写也非原子. Per-symbol lock 保证同 symbol 串行 + 跨 symbol 并行,
        # latency 几乎无影响 (calculate() 是 CPU 毫秒级).
        self._symbol_locks: dict[str, asyncio.Lock] = {}

    def latest_snapshot(self, symbol: str) -> FeatureSnapshot | None:
        return self._latest_snapshots.get(symbol)

    def _lock_for_symbol(self, symbol: str) -> asyncio.Lock:
        lock = self._symbol_locks.get(symbol)
        if lock is None:
            # asyncio.Lock 本身构造也有 loop-binding 风险 (Python 3.10+),
            # 但此方法只在 running event loop 中被 handle_market_snapshot 调用,
            # 所以没有和 B-1 同类的启动期绑错问题.
            lock = asyncio.Lock()
            self._symbol_locks[symbol] = lock
        return lock

    async def handle_market_snapshot(self, message: dict) -> None:
        envelope = parse_envelope(message)
        market_snapshot = MarketSnapshot.model_validate(envelope.payload)
        # R2-B2 同 symbol 串行化. lock 只覆盖 state 写入 + snapshot 发布,
        # 不影响 envelope 解析和 pydantic validation 的并发性.
        lock = self._lock_for_symbol(market_snapshot.symbol)
        async with lock:
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
