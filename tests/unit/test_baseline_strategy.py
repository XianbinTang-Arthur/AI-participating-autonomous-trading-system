from __future__ import annotations

import unittest
from aats.bootstrap.settings import AATSSettings
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.common import utc_now
from aats.schemas.decision import DecisionContext
from aats.schemas.features import FeatureSnapshot
from aats.services.decision_engine.baseline import BaselineStrategy
from aats.services.decision_engine.feature_resolver import FeatureSnapshotResolver
from aats.services.feature_engine.calculator import FeatureCalculator
from aats.storage.event_store import InMemoryEventStore
from aats.storage.stream_snapshot_cache import StreamSnapshotCache
from aats.schemas.market import MarketSnapshot


class TestBaselineStrategy(unittest.TestCase):
    def test_range_signal_uses_configured_threshold_when_relaxed(self) -> None:
        event_store = InMemoryEventStore()
        settings = AATSSettings.model_validate({"strategy_baseline_range_alpha_threshold": 0.12})
        strategy = BaselineStrategy(event_store=event_store, settings=settings)
        seed = self._feature_snapshot()
        feature_snapshot = seed.model_copy(
            update={
                "regime_indicator": "range",
                "composite_alpha_score": 0.13,
                "analysis_context": seed.analysis_context.model_copy(  # type: ignore[union-attr]
                    update={
                        "alpha_factors": seed.analysis_context.alpha_factors.model_copy(  # type: ignore[union-attr]
                            update={"microstructure_alpha": 0.04}
                        ),
                    }
                ),
            }
        )
        event = build_envelope(
            topic=topics.FEATURE_SNAPSHOTS,
            key=feature_snapshot.symbol,
            payload_model=feature_snapshot,
            source_component="test",
        )
        event_store.append(event)

        baseline = strategy.evaluate(self._context(feature_snapshot_ref=event.event_id))

        self.assertEqual(baseline.direction_bias, "long")
        self.assertEqual(baseline.direction_rule, "baseline_regime_range_threshold_crossed")
        self.assertEqual(baseline.direction_threshold, 0.12)

    def test_impulse_override_promotes_range_signal_to_long(self) -> None:
        event_store = InMemoryEventStore()
        settings = AATSSettings.model_validate(
            {
                "strategy_baseline_range_alpha_threshold": 0.20,
                "strategy_baseline_impulse_override_enabled": True,
                "strategy_baseline_impulse_alpha_min": 0.10,
                "strategy_baseline_impulse_microstructure_min": 0.25,
                "strategy_baseline_impulse_momentum_min": 0.00035,
                "strategy_baseline_impulse_range_ratio_min": 0.003,
                "strategy_baseline_impulse_body_ratio_min": 0.10,
            }
        )
        strategy = BaselineStrategy(event_store=event_store, settings=settings)
        seed = self._feature_snapshot()
        feature_snapshot = seed.model_copy(
            update={
                "regime_indicator": "range",
                "composite_alpha_score": 0.11,
                "analysis_context": seed.analysis_context.model_copy(  # type: ignore[union-attr]
                    update={
                        "multi_timeframe": seed.analysis_context.multi_timeframe.model_copy(  # type: ignore[union-attr]
                            update={"directional_alignment": "long"}
                        ),
                        "alpha_factors": seed.analysis_context.alpha_factors.model_copy(  # type: ignore[union-attr]
                            update={"microstructure_alpha": 0.32}
                        ),
                        "timeframe_features": {
                            **seed.analysis_context.timeframe_features,  # type: ignore[union-attr]
                            "15m": seed.analysis_context.timeframe_features["15m"].model_copy(  # type: ignore[union-attr]
                                update={
                                    "momentum_score": 0.0006,
                                    "range_ratio": 0.0045,
                                    "candle_body_ratio": 0.22,
                                }
                            ),
                        },
                    }
                ),
            }
        )
        event = build_envelope(
            topic=topics.FEATURE_SNAPSHOTS,
            key=feature_snapshot.symbol,
            payload_model=feature_snapshot,
            source_component="test",
        )
        event_store.append(event)

        baseline = strategy.evaluate(self._context(feature_snapshot_ref=event.event_id))

        self.assertEqual(baseline.direction_bias, "long")
        self.assertEqual(baseline.direction_rule, "baseline_impulse_override_long")
        self.assertIsNone(baseline.direction_threshold)

    def test_trend_signal_uses_microstructure_confirmation_to_commit(self) -> None:
        event_store = InMemoryEventStore()
        strategy = BaselineStrategy(event_store=event_store)
        feature_snapshot = self._feature_snapshot().model_copy(
            update={
                "regime_indicator": "trend",
                "composite_alpha_score": 0.17,
                "analysis_context": self._feature_snapshot().analysis_context.model_copy(  # type: ignore[union-attr]
                    update={
                        "multi_timeframe": self._feature_snapshot().analysis_context.multi_timeframe.model_copy(  # type: ignore[union-attr]
                            update={"directional_alignment": "long"}
                        ),
                        "alpha_factors": self._feature_snapshot().analysis_context.alpha_factors.model_copy(  # type: ignore[union-attr]
                            update={"microstructure_alpha": 0.12}
                        ),
                    }
                ),
            }
        )
        event = build_envelope(
            topic=topics.FEATURE_SNAPSHOTS,
            key=feature_snapshot.symbol,
            payload_model=feature_snapshot,
            source_component="test",
        )
        event_store.append(event)

        baseline = strategy.evaluate(self._context(feature_snapshot_ref=event.event_id))

        self.assertEqual(baseline.direction_bias, "long")
        self.assertIn("microstructure_confirms_long", baseline.reason_codes)

    def test_uncertain_signal_stays_flat_when_microstructure_conflicts(self) -> None:
        event_store = InMemoryEventStore()
        strategy = BaselineStrategy(event_store=event_store)
        seed = self._feature_snapshot()
        feature_snapshot = seed.model_copy(
            update={
                "regime_indicator": "uncertain",
                "composite_alpha_score": 0.34,
                "analysis_context": seed.analysis_context.model_copy(  # type: ignore[union-attr]
                    update={
                        "multi_timeframe": seed.analysis_context.multi_timeframe.model_copy(  # type: ignore[union-attr]
                            update={"directional_alignment": "mixed"}
                        ),
                        "alpha_factors": seed.analysis_context.alpha_factors.model_copy(  # type: ignore[union-attr]
                            update={"microstructure_alpha": -0.14}
                        ),
                    }
                ),
            }
        )
        event = build_envelope(
            topic=topics.FEATURE_SNAPSHOTS,
            key=feature_snapshot.symbol,
            payload_model=feature_snapshot,
            source_component="test",
        )
        event_store.append(event)

        baseline = strategy.evaluate(self._context(feature_snapshot_ref=event.event_id))

        self.assertEqual(baseline.direction_bias, "flat")
        self.assertIn("microstructure_not_strong_enough", baseline.reason_codes)

    def test_resolver_reads_from_stream_cache_by_event_id(self) -> None:
        """验证 FeatureSnapshotResolver 按 event_id 从 StreamSnapshotCache 精确命中。

        模拟实盘场景：feature snapshot 只进 StreamSnapshotCache，不落 Postgres。
        baseline 通过 resolver → stream_cache.get(event_id) 精确取回，不走 latest()。
        """
        event_store = InMemoryEventStore()
        stream_cache = StreamSnapshotCache()
        resolver = FeatureSnapshotResolver(
            event_store=event_store, stream_snapshot_cache=stream_cache,
        )
        strategy = BaselineStrategy(event_store=event_store, feature_resolver=resolver)
        feature_snapshot = self._feature_snapshot()
        event = build_envelope(
            topic=topics.FEATURE_SNAPSHOTS,
            key=feature_snapshot.symbol,
            payload_model=feature_snapshot,
            source_component="test",
        )
        # 只写入 stream_cache（模拟 nats_bus 路由），不写入 event_store
        stream_cache.update(event)

        baseline = strategy.evaluate(self._context(feature_snapshot_ref=event.event_id))
        self.assertEqual(baseline.symbol, "BTC-USDT-SWAP")
        self.assertIsNotNone(baseline.direction_bias)

    def test_resolver_exact_match_not_latest(self) -> None:
        """验证 resolver 按 ref 精确匹配，不会返回更晚到达的 latest snapshot。

        P1 一致性场景：context 建好后新的 feature snapshot 更新了 cache，
        baseline 必须仍然使用 context 引用的那条旧 snapshot。
        """
        event_store = InMemoryEventStore()
        stream_cache = StreamSnapshotCache()
        resolver = FeatureSnapshotResolver(
            event_store=event_store, stream_snapshot_cache=stream_cache,
        )
        strategy = BaselineStrategy(event_store=event_store, feature_resolver=resolver)
        # 第一条 feature snapshot（context 引用）
        snap_v1 = self._feature_snapshot().model_copy(
            update={"composite_alpha_score": 0.05},
        )
        event_v1 = build_envelope(
            topic=topics.FEATURE_SNAPSHOTS,
            key=snap_v1.symbol,
            payload_model=snap_v1,
            source_component="test",
        )
        stream_cache.update(event_v1)
        # 第二条更新的 snapshot（latest 已变）
        snap_v2 = self._feature_snapshot().model_copy(
            update={"composite_alpha_score": 0.99},
        )
        event_v2 = build_envelope(
            topic=topics.FEATURE_SNAPSHOTS,
            key=snap_v2.symbol,
            payload_model=snap_v2,
            source_component="test",
        )
        stream_cache.update(event_v2)
        # latest 现在指向 v2，但 context 引用的是 v1
        self.assertEqual(
            stream_cache.latest(topics.FEATURE_SNAPSHOTS, key=snap_v1.symbol).event_id,  # type: ignore[union-attr]
            event_v2.event_id,
        )
        baseline = strategy.evaluate(self._context(feature_snapshot_ref=event_v1.event_id))
        # baseline 用的必须是 v1 的 alpha，不是 v2 的 0.99
        self.assertAlmostEqual(baseline.composite_alpha_score, 0.05, places=4)

    @staticmethod
    def _context(*, feature_snapshot_ref: str) -> DecisionContext:
        now = utc_now()
        return DecisionContext(
            decision_id="decision_baseline_test",
            symbol="BTC-USDT-SWAP",
            timeframe="15m",
            as_of_ts=now,
            market_snapshot_ref="evt_market",
            feature_snapshot_ref=feature_snapshot_ref,
            portfolio_snapshot_ref="evt_portfolio",
            health_snapshot_ref="evt_health",
            mode="guarded_live",
            current_position_qty=0.0,
            product_type="derivatives",
            current_exposure_side="flat",
            current_target_leverage=1.0,
        )

    @staticmethod
    def _feature_snapshot() -> FeatureSnapshot:
        now = utc_now()
        market_snapshot = MarketSnapshot(
            created_at=now,
            symbol="BTC-USDT-SWAP",
            exchange="OKX",
            snapshot_ts=now,
            best_bid=67000.0,
            best_ask=67001.0,
            last_price=67000.5,
            bid_size=4.0,
            ask_size=2.2,
            volume_24h=1000.0,
            kline_15m={"open": 66800.0, "high": 67200.0, "low": 66700.0, "close": 67100.0},
            kline_1h={"open": 66000.0, "high": 67300.0, "low": 65900.0, "close": 67100.0},
            orderbook_depth={
                "bids": [{"price": 67000.0, "size": 7.0}, {"price": 66999.0, "size": 5.0}],
                "asks": [{"price": 67001.0, "size": 4.0}, {"price": 67002.0, "size": 4.5}],
            },
            recent_trades=[
                {"side": "buy", "size": 1.2},
                {"side": "buy", "size": 0.8},
                {"side": "sell", "size": 0.3},
            ],
        )
        return FeatureCalculator().calculate(market_snapshot, market_snapshot_ref="evt_market")


if __name__ == "__main__":
    unittest.main()
