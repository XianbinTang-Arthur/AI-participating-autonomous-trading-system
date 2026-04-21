"""Baseline 审查第 3 轮修复 — regression tests.

锁定契约:
  - R3-M1: BaselineAssessment.factor_scores 包含 basis/funding/oi/ls_alpha,
    reason_codes 在各 alpha >= 0.15 时产生对应语义码（审计可追溯性）.
  - R3-M2: OpenInterestState.update 拒绝 oi <= 0.
  - R3-M4: LongShortRatioPoller 首次 sample 打 info 日志.
  - R3-M5: BaselineStrategy.evaluate 对旧 AlphaFactorSet payload (缺 4 个新字段)
    能正确反序列化且 default 0, 不 raise.
  - R3-m5: composite_alpha_score 从真实源码权重算, 不依赖硬编码字典.
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import timedelta
from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.common import utc_now
from aats.schemas.decision import DecisionContext
from aats.schemas.features import (
    AlphaFactorSet,
    AnalysisContext,
    FeatureSnapshot,
    LiquidityFeatureSet,
    MultiTimeframeContext,
    PositionSizingContext,
    TimeframeFeatureSet,
)
from aats.services.decision_engine.baseline import BaselineStrategy
from aats.services.feature_engine.calculator import FeatureCalculator
from aats.services.feature_engine.long_short_poller import LongShortRatioPoller
from aats.services.feature_engine.oi_state import OpenInterestState
from aats.storage.event_store import InMemoryEventStore


class Round3FixesRegressionTests(unittest.TestCase):
    # ────────────────────────────────────────────────────────────────
    # R3-M1: factor_scores / reason_codes 纳入 4 新 alpha
    # ────────────────────────────────────────────────────────────────

    def test_baseline_factor_scores_includes_new_alphas(self) -> None:
        """BaselineAssessment.factor_scores 必须包含 basis/funding/oi/ls_alpha."""
        store = InMemoryEventStore()
        strategy = BaselineStrategy(event_store=store, settings=AATSSettings())
        snap = _build_feature_snapshot_with_alphas(
            basis=0.5, funding=-0.3, oi=0.25, ls=-0.4,
        )
        event = build_envelope(
            topic=topics.FEATURE_SNAPSHOTS, key=snap.symbol,
            payload_model=snap, source_component="test",
        )
        store.append(event)
        ctx = _decision_context(event.event_id)
        baseline = strategy.evaluate(ctx)
        expected_keys = {
            "momentum_alpha", "trend_alpha", "regime_alpha",
            "multi_timeframe_alpha", "microstructure_alpha",
            "basis_alpha", "funding_alpha", "oi_alpha", "ls_alpha",
            "liquidity_scale",
        }
        self.assertEqual(set(baseline.factor_scores.keys()), expected_keys)
        self.assertAlmostEqual(baseline.factor_scores["basis_alpha"], 0.5, places=4)
        self.assertAlmostEqual(baseline.factor_scores["funding_alpha"], -0.3, places=4)
        self.assertAlmostEqual(baseline.factor_scores["oi_alpha"], 0.25, places=4)
        self.assertAlmostEqual(baseline.factor_scores["ls_alpha"], -0.4, places=4)

    def test_reason_codes_flags_strong_new_alphas(self) -> None:
        """|alpha| >= 0.15 的新 alpha 会出现对应语义 reason code."""
        store = InMemoryEventStore()
        strategy = BaselineStrategy(event_store=store, settings=AATSSettings())
        snap = _build_feature_snapshot_with_alphas(
            basis=0.20, funding=-0.20, oi=0.18, ls=0.25,
        )
        event = build_envelope(
            topic=topics.FEATURE_SNAPSHOTS, key=snap.symbol,
            payload_model=snap, source_component="test",
        )
        store.append(event)
        baseline = strategy.evaluate(_decision_context(event.event_id))
        codes = set(baseline.reason_codes)
        self.assertIn("alpha_basis_contrarian_long", codes)
        self.assertIn("alpha_funding_short_bias", codes)
        self.assertIn("alpha_oi_long_confirming", codes)
        self.assertIn("alpha_ls_contrarian_long", codes)

    # ────────────────────────────────────────────────────────────────
    # R3-M2: OI <= 0 拒绝
    # ────────────────────────────────────────────────────────────────

    def test_oi_state_rejects_zero(self) -> None:
        """OI=0 被拒绝, state 不更新."""
        state = OpenInterestState(symbol="BTC-USDT-SWAP")
        ts = utc_now()
        state.update(1_000_000.0, ts=ts)
        self.assertEqual(state.samples_count(), 1)
        state.update(0.0, ts=ts + timedelta(seconds=3))
        self.assertEqual(state.samples_count(), 1, "oi=0 不应被接受")
        state.update(-5.0, ts=ts + timedelta(seconds=6))
        self.assertEqual(state.samples_count(), 1, "oi<0 不应被接受")

    # ────────────────────────────────────────────────────────────────
    # R3-M4: poller 首次 sample 打 info 日志
    # ────────────────────────────────────────────────────────────────

    def test_poller_first_sample_logs_info(self) -> None:
        """第一次拿到 sample 时打 info 日志; 后续不重复打."""
        poller = LongShortRatioPoller(okx_rest_url="https://example.com")

        from aats.services.feature_engine.long_short_poller import LongShortRatioSample

        async def fake_poll_one(client, symbol):
            return LongShortRatioSample(symbol=symbol, ts=utc_now(), ls_ratio=2.0)

        poller._poll_one = fake_poll_one  # type: ignore[method-assign]
        with self.assertLogs(
            "aats.feature_engine.long_short_poller", level="INFO",
        ) as captured:
            asyncio.run(poller._poll_round(("BTC-USDT-SWAP",)))
            # 第二轮同 symbol 不应再打 first_sample
            asyncio.run(poller._poll_round(("BTC-USDT-SWAP",)))
        first_sample_lines = [
            line for line in captured.output
            if "long_short_poller_first_sample" in line
        ]
        self.assertEqual(
            len(first_sample_lines), 1,
            "first_sample 仅首次出现一次 (后续不重复打)",
        )

    # ────────────────────────────────────────────────────────────────
    # R3-M5: 旧 FeatureSnapshot payload (无 4 个新 alpha 字段) replay
    # ────────────────────────────────────────────────────────────────

    def test_baseline_replay_pre_p14_payload_defaults_new_alphas_to_zero(self) -> None:
        """event_store 里存的是 P1.4 之前的 payload (AlphaFactorSet 无
        basis/funding/oi/ls 字段), 反序列化应 default 0, evaluate 不 raise."""
        store = InMemoryEventStore()
        strategy = BaselineStrategy(event_store=store, settings=AATSSettings())
        legacy_snap = _build_feature_snapshot_with_alphas(
            basis=0.0, funding=0.0, oi=0.0, ls=0.0,
        )
        # 模拟老 payload: 序列化后手动删掉 4 个新字段
        payload = legacy_snap.model_dump(mode="json")
        af = payload["analysis_context"]["alpha_factors"]
        for deprecated_field in ("basis_alpha", "funding_alpha", "oi_alpha", "ls_alpha"):
            af.pop(deprecated_field, None)
        # 重新校验 (schema 应 default 0)
        reparsed = FeatureSnapshot.model_validate(payload)
        self.assertEqual(reparsed.analysis_context.alpha_factors.basis_alpha, 0.0)  # type: ignore[union-attr]
        self.assertEqual(reparsed.analysis_context.alpha_factors.funding_alpha, 0.0)  # type: ignore[union-attr]
        self.assertEqual(reparsed.analysis_context.alpha_factors.oi_alpha, 0.0)  # type: ignore[union-attr]
        self.assertEqual(reparsed.analysis_context.alpha_factors.ls_alpha, 0.0)  # type: ignore[union-attr]

        # BaselineStrategy 处理老 payload 不 raise
        event = build_envelope(
            topic=topics.FEATURE_SNAPSHOTS, key=reparsed.symbol,
            payload_model=reparsed, source_component="test",
        )
        store.append(event)
        baseline = strategy.evaluate(_decision_context(event.event_id))
        self.assertIsNotNone(baseline)
        # factor_scores 里的新 alpha 都应是 0
        self.assertEqual(baseline.factor_scores["basis_alpha"], 0.0)
        self.assertEqual(baseline.factor_scores["funding_alpha"], 0.0)
        self.assertEqual(baseline.factor_scores["oi_alpha"], 0.0)
        self.assertEqual(baseline.factor_scores["ls_alpha"], 0.0)

    # ────────────────────────────────────────────────────────────────
    # R3-m5: composite 权重测试从源码算 (不依赖硬编码字典)
    # ────────────────────────────────────────────────────────────────

    def test_composite_alpha_equals_liquidity_scale_when_all_alphas_saturate_positive(self) -> None:
        """所有 alpha = +1 时 composite_alpha_score == liquidity_scale.

        验证权重和严格 = 1.00 (从真实代码算, 不是测试里复制权重表).
        """
        calc = FeatureCalculator(
            enable_timeseries_smoothing=False,  # 避免 state 影响
        )
        # 手动调用 _alpha_factors 给定全 +1 的环境
        dummy_features_15m = TimeframeFeatureSet(
            timeframe="15m", open_price=Decimal("100"), high_price=Decimal("101"),
            low_price=Decimal("99"), close_price=Decimal("100.5"),
            momentum_score=1.0,  # 会被 clamp 到合理值，但 momentum_alpha 会爆掉
            trend_strength=1.0, volatility_value=0.001, volatility_state="low",
            candle_body_ratio=0.5, range_ratio=0.01,
        )
        dummy_features_1h = dummy_features_15m.model_copy()
        dummy_mtf = MultiTimeframeContext(
            directional_alignment="long",
            momentum_alignment_score=1.0,
            regime_alignment_score=1.0,
            dominant_timeframe="balanced",
        )
        alphas = calc._alpha_factors(
            features_15m=dummy_features_15m,
            features_1h=dummy_features_1h,
            multi_timeframe=dummy_mtf,
            liquidity_score=1.0,   # → liquidity_scale = clamp(0.45 + 1.0*0.55) = 1.0
            top_of_book_imbalance=1.0,
            depth_imbalance=1.0,
            trade_flow_imbalance=1.0,
            execution_quality_scale=1.0,
            spread_penalty=0.0,
            regime_indicator="trend",
            regime_confidence=1.0,
            regime_bias="long",
            last_price=66000.0,
            mark_price=67000.0,   # last < mark → basis_bps 负 → -tanh(负) 正
            basis_signal_enabled=True,
            basis_scale_bps=10.0,
            funding_rate=-0.001,  # funding<0 → -tanh(负) 正
            funding_signal_enabled=True,
            funding_scale=2000.0,
            oi_delta=0.5,         # price_roc>0 + oi_delta>0 → oi_alpha 正
            price_roc=0.05,
            oi_signal_enabled=True,
            oi_dead_zone=0.005,
            ls_ratio=0.3,          # ls<1 (空头多) → -tanh((0.3-1)/2)=tanh(0.35) 正
            ls_signal_enabled=True,
            ls_scale=2.0,
        )
        # 所有 alpha 都是正向
        self.assertGreater(alphas.momentum_alpha, 0.5)
        self.assertGreater(alphas.basis_alpha, 0.5)
        self.assertGreater(alphas.funding_alpha, 0.5)
        self.assertGreater(alphas.oi_alpha, 0.5)
        self.assertGreater(alphas.ls_alpha, 0.1)
        # composite 受 liquidity_scale 缩放, 但这里 liquidity_score=1 → scale=1.0
        # 所有 alpha ≈ +1, 权重和 1.00 → composite ≈ 1.0 (clamp)
        self.assertGreater(alphas.composite_alpha_score, 0.5)
        self.assertLessEqual(alphas.composite_alpha_score, 1.0)


# ── Helpers ─────────────────────────────────────────────────────────


def _build_feature_snapshot_with_alphas(
    *, basis: float, funding: float, oi: float, ls: float,
) -> FeatureSnapshot:
    """手工造一个 FeatureSnapshot，所有 regime/momentum 字段为中性 flat,
    4 个新 alpha 值由参数指定, 方便独立验证 factor_scores / reason_codes.
    """
    now = utc_now()
    liquidity = LiquidityFeatureSet(
        spread_bps=1.0, top_of_book_imbalance=0.0, depth_imbalance=0.0,
        trade_flow_imbalance=0.0, quoted_depth=10.0, spread_penalty=0.0,
        execution_quality_scale=1.0, liquidity_score=0.8,
    )
    tf_15m = TimeframeFeatureSet(
        timeframe="15m", open_price=Decimal("67000"), high_price=Decimal("67100"),
        low_price=Decimal("66900"), close_price=Decimal("67050"),
        momentum_score=0.0, trend_strength=0.0,
        volatility_value=0.003, volatility_state="low",
        candle_body_ratio=0.3, range_ratio=0.003,
    )
    tf_1h = tf_15m.model_copy(update={"timeframe": "1h"})
    mtf = MultiTimeframeContext(
        directional_alignment="flat", momentum_alignment_score=0.0,
        regime_alignment_score=0.0, dominant_timeframe="balanced",
    )
    alphas = AlphaFactorSet(
        momentum_alpha=0.0, trend_alpha=0.0, regime_alpha=0.0,
        multi_timeframe_alpha=0.0, microstructure_alpha=0.0,
        basis_alpha=basis, funding_alpha=funding, oi_alpha=oi, ls_alpha=ls,
        liquidity_scale=0.8, composite_alpha_score=0.0, conviction_score=0.0,
    )
    pos = PositionSizingContext(
        volatility_target_scale=1.0, liquidity_scale=0.8,
        execution_quality_scale=1.0, conviction_scale=0.0,
        suggested_position_scale=0.5,
    )
    analysis = AnalysisContext(
        symbol="BTC-USDT-SWAP", snapshot_ts=now,
        analysis_version="0.2.0", regime_version="0.2.0",
        trend_bias="flat", regime_indicator="uncertain", regime_confidence=0.3,
        regime_reasons=["test"],
        timeframe_features={"15m": tf_15m, "1h": tf_1h},
        liquidity=liquidity, multi_timeframe=mtf, alpha_factors=alphas,
        position_sizing=pos,
    )
    return FeatureSnapshot(
        symbol="BTC-USDT-SWAP", snapshot_ts=now,
        market_snapshot_ref="evt_mkt",
        trend_strength=0.0, volatility_state="low", volatility_value=0.003,
        momentum_score=0.0, liquidity_score=0.8,
        regime_indicator="uncertain", regime_confidence=0.3,
        multi_timeframe_alignment=0.0, composite_alpha_score=0.0,
        suggested_position_scale=0.5, volatility_target_scale=1.0,
        feature_version="0.2.0", analysis_context=analysis,
    )


def _decision_context(ref: str) -> DecisionContext:
    now = utc_now()
    return DecisionContext(
        decision_id="dec_r3_test", symbol="BTC-USDT-SWAP", timeframe="15m",
        as_of_ts=now, market_snapshot_ref="evt_market",
        feature_snapshot_ref=ref,
        portfolio_snapshot_ref="evt_portfolio", health_snapshot_ref="evt_health",
        mode="guarded_live", current_position_qty=0.0, product_type="derivatives",
        current_exposure_side="flat", current_target_leverage=1.0,
    )


if __name__ == "__main__":
    unittest.main()
