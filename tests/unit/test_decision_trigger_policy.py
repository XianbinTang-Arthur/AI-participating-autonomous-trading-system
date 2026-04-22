from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
import unittest

from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.features import FeatureSnapshot
from aats.schemas.market import MarketSnapshot
from aats.services.decision_engine.trigger import DecisionCycleTrigger
from aats.services.decision_engine.trigger_policy import DecisionTriggerPolicy


def _feature(*, snapshot_ts, momentum: float, regime: str, symbol: str = "BTC-USDT") -> FeatureSnapshot:
    return FeatureSnapshot(
        symbol=symbol,
        snapshot_ts=snapshot_ts,
        trend_strength=0.6,
        volatility_state="medium",
        volatility_value=1.0,
        momentum_score=momentum,
        liquidity_score=0.9,
        regime_indicator=regime,  # type: ignore[arg-type]
        feature_version="test",
    )


def _market(*, snapshot_ts, last_price: Decimal | float, symbol: str = "BTC-USDT") -> MarketSnapshot:
    return MarketSnapshot(
        symbol=symbol,
        exchange="OKX",
        snapshot_ts=snapshot_ts,
        best_bid=last_price - 1.0,
        best_ask=last_price + 1.0,
        last_price=last_price,
        bid_size=1.0,
        ask_size=1.0,
        volume_24h=1000.0,
        kline_15m={"open": last_price, "high": last_price, "low": last_price, "close": last_price},
        kline_1h={"open": last_price, "high": last_price, "low": last_price, "close": last_price},
    )


class TestDecisionTriggerPolicy(unittest.TestCase):
    def test_duplicate_suppression_and_material_change_gating(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "decision_min_interval_seconds_15m": 60.0,
                "decision_min_price_move_bps": 5.0,
                "decision_min_momentum_delta": 0.2,
            }
        )
        policy = DecisionTriggerPolicy(settings=settings)
        base_ts = utc_now()
        feature = _feature(snapshot_ts=base_ts, momentum=0.1, regime="trend")
        market = _market(snapshot_ts=base_ts, last_price=67_000.0)

        allowed, reason = policy.should_trigger(
            feature_snapshot=feature,
            market_snapshot=market,
            timeframe="15m",
        )
        self.assertTrue(allowed)
        self.assertEqual(reason, "initial_decision")
        policy.record_trigger(feature_snapshot=feature, market_snapshot=market, timeframe="15m")

        # R3-P1-U-B：同 snapshot_ts + 完全相同内容 → 不再 early-reject 为
        # "duplicate_market_snapshot"，而是 fall-through 到 material_change
        # 评估。内容完全相同时 material_change=False + cadence 未到 → 仍被
        # 拒绝，但 reason 变为 suppressed_duplicate。真正重复的消息仍然被拦。
        duplicate_allowed, duplicate_reason = policy.should_trigger(
            feature_snapshot=feature,
            market_snapshot=market,
            timeframe="15m",
        )
        self.assertFalse(duplicate_allowed)
        self.assertEqual(duplicate_reason, "suppressed_duplicate")

        next_ts = base_ts + timedelta(seconds=10)
        small_move_market = _market(snapshot_ts=next_ts, last_price=67_001.0)
        small_move_feature = _feature(snapshot_ts=next_ts, momentum=0.11, regime="trend")
        suppressed, suppressed_reason = policy.should_trigger(
            feature_snapshot=small_move_feature,
            market_snapshot=small_move_market,
            timeframe="15m",
        )
        self.assertFalse(suppressed)
        self.assertEqual(suppressed_reason, "suppressed_duplicate")

        big_move_market = _market(snapshot_ts=next_ts, last_price=67_100.0)
        material, material_reason = policy.should_trigger(
            feature_snapshot=small_move_feature.model_copy(update={"momentum_score": 0.45}),
            market_snapshot=big_move_market,
            timeframe="15m",
        )
        self.assertTrue(material)
        self.assertEqual(material_reason, "material_change")

    def test_frequency_cap_is_enforced_in_trigger_policy(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "max_decisions_per_minute": 2,
                "decision_min_interval_seconds_15m": 0.0,
                "decision_min_price_move_bps": 0.0,
                "decision_min_momentum_delta": 0.0,
            }
        )
        policy = DecisionTriggerPolicy(settings=settings)
        base_ts = utc_now()

        first_feature = _feature(snapshot_ts=base_ts, momentum=0.1, regime="trend")
        first_market = _market(snapshot_ts=base_ts, last_price=67_000.0)
        self.assertEqual(
            policy.should_trigger(feature_snapshot=first_feature, market_snapshot=first_market, timeframe="15m"),
            (True, "initial_decision"),
        )
        policy.record_trigger(feature_snapshot=first_feature, market_snapshot=first_market, timeframe="15m")

        second_ts = base_ts + timedelta(seconds=10)
        second_feature = _feature(snapshot_ts=second_ts, momentum=0.2, regime="trend")
        second_market = _market(snapshot_ts=second_ts, last_price=67_010.0)
        second_allowed, second_reason = policy.should_trigger(
            feature_snapshot=second_feature,
            market_snapshot=second_market,
            timeframe="15m",
        )
        self.assertTrue(second_allowed)
        self.assertIn(second_reason, {"cadence_elapsed", "material_change"})
        policy.record_trigger(feature_snapshot=second_feature, market_snapshot=second_market, timeframe="15m")

        third_ts = base_ts + timedelta(seconds=20)
        third_feature = _feature(snapshot_ts=third_ts, momentum=0.3, regime="trend")
        third_market = _market(snapshot_ts=third_ts, last_price=67_020.0)
        third_allowed, third_reason = policy.should_trigger(
            feature_snapshot=third_feature,
            market_snapshot=third_market,
            timeframe="15m",
        )
        self.assertFalse(third_allowed)
        self.assertEqual(third_reason, "max_decision_frequency_reached")

    def test_decision_cycle_symbols_exclude_smart_arbitrage_companion_symbols(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "smart_arbitrage_enabled": True,
                "smart_arbitrage_pair_definitions": (
                    {
                        "pair_id": "btc_usdt_swap",
                        "spot_symbol": "BTC-USDT",
                        "hedge_symbol": "BTC-USDT-SWAP",
                    },
                ),
            }
        )

        self.assertEqual(settings.decision_cycle_symbols(), ("BTC-USDT-SWAP",))
        self.assertEqual(settings.expanded_allowed_symbols(), ("BTC-USDT-SWAP", "BTC-USDT"))
        self.assertFalse(settings.symbol_allowed_for_decision_cycle("BTC-USDT"))
        self.assertTrue(settings.symbol_allowed_for_decision_cycle("BTC-USDT-SWAP"))


class TestDecisionCycleTrigger(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_same_snapshot_only_runs_one_cycle(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "enabled_decision_timeframes": ["15m"],
                "decision_min_interval_seconds_15m": 60.0,
            }
        )
        policy = DecisionTriggerPolicy(settings=settings)
        base_ts = utc_now()
        market = _market(snapshot_ts=base_ts, last_price=67_000.0)
        feature = _feature(snapshot_ts=base_ts, momentum=0.1, regime="trend")

        class _FakeOrchestrator:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            async def run_cycle(self, *, symbol: str, timeframe: str, feature_snapshot_hint=None, market_snapshot_hint=None):
                self.calls.append((symbol, timeframe))
                await asyncio.sleep(0)

        class _FakeMarketGateway:
            def latest_snapshot(self, symbol: str):
                return market if symbol == "BTC-USDT" else None

        orchestrator = _FakeOrchestrator()
        trigger = DecisionCycleTrigger(
            orchestrator=orchestrator,
            market_gateway=_FakeMarketGateway(),
            policy=policy,
        )
        # S2 后 DecisionCycleTrigger 需要先 start() 起 dispatcher task。
        # 生产路径由 bootstrap/config.py:_subscribe_critical_handlers 统一挂。
        # 测试里手工调 + addAsyncCleanup 保证 teardown。
        await trigger.start()
        self.addAsyncCleanup(trigger.stop)
        envelope = build_envelope(
            topic=topics.FEATURE_SNAPSHOTS,
            key=feature.symbol,
            payload_model=feature,
            source_component="test",
        )
        message = {"topic": topics.FEATURE_SNAPSHOTS, "key": feature.symbol, "payload": envelope.model_dump(mode="json")}

        await asyncio.gather(
            trigger.handle_feature_snapshot(message),
            trigger.handle_feature_snapshot(message),
            trigger.handle_feature_snapshot(message),
        )
        # Queue 路径下 handler 立即返回，run_cycle 由 dispatcher 异步消费。
        # 等 queue 排空 + task_done 配对再断言。enqueue 里 drain 也做了 task_done
        # 配对，所以 join 语义清晰：unfinished_tasks==0 即"dispatcher 已跑完所有
        # 实际进 queue 的 pending"。
        await trigger._trigger_queue.join()

        self.assertEqual(orchestrator.calls, [("BTC-USDT", "15m")])

    async def test_halted_gate_skips_decision_cycle(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "enabled_decision_timeframes": ["15m"],
                "decision_min_interval_seconds_15m": 0.0,
            }
        )
        policy = DecisionTriggerPolicy(settings=settings)
        base_ts = utc_now()
        market = _market(snapshot_ts=base_ts, last_price=67_000.0)
        feature = _feature(snapshot_ts=base_ts, momentum=0.1, regime="trend")

        class _FakeOrchestrator:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            async def run_cycle(self, *, symbol: str, timeframe: str, feature_snapshot_hint=None, market_snapshot_hint=None):
                self.calls.append((symbol, timeframe))

        class _FakeMarketGateway:
            def latest_snapshot(self, symbol: str):
                return market if symbol == "BTC-USDT" else None

        orchestrator = _FakeOrchestrator()
        trigger = DecisionCycleTrigger(
            orchestrator=orchestrator,
            market_gateway=_FakeMarketGateway(),
            policy=policy,
            can_trigger=lambda *, symbol: (False, "kill_switch_active"),
        )
        await trigger.start()
        self.addAsyncCleanup(trigger.stop)
        envelope = build_envelope(
            topic=topics.FEATURE_SNAPSHOTS,
            key=feature.symbol,
            payload_model=feature,
            source_component="test",
        )
        message = {"topic": topics.FEATURE_SNAPSHOTS, "key": feature.symbol, "payload": envelope.model_dump(mode="json")}

        await trigger.handle_feature_snapshot(message)

        # can_trigger=False → handler 不入队，队列空；不需 join。
        self.assertEqual(orchestrator.calls, [])

    async def test_companion_spot_symbol_is_filtered_out_of_derivatives_decision_cycle(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "enabled_decision_timeframes": ["15m"],
                "decision_min_interval_seconds_15m": 0.0,
                "smart_arbitrage_enabled": True,
                "smart_arbitrage_pair_definitions": (
                    {
                        "pair_id": "btc_usdt_swap",
                        "spot_symbol": "BTC-USDT",
                        "hedge_symbol": "BTC-USDT-SWAP",
                    },
                ),
            }
        )
        policy = DecisionTriggerPolicy(settings=settings)
        base_ts = utc_now()
        market = _market(snapshot_ts=base_ts, last_price=67_000.0, symbol="BTC-USDT")
        feature = _feature(snapshot_ts=base_ts, momentum=0.1, regime="trend", symbol="BTC-USDT")

        class _FakeOrchestrator:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            async def run_cycle(self, *, symbol: str, timeframe: str, feature_snapshot_hint=None, market_snapshot_hint=None):
                self.calls.append((symbol, timeframe))

        class _FakeMarketGateway:
            def latest_snapshot(self, symbol: str):
                return market if symbol == "BTC-USDT" else None

        orchestrator = _FakeOrchestrator()
        trigger = DecisionCycleTrigger(
            orchestrator=orchestrator,
            market_gateway=_FakeMarketGateway(),
            policy=policy,
            can_trigger=lambda *, symbol: (
                True,
                "ready",
            )
            if settings.symbol_allowed_for_decision_cycle(symbol)
            else (
                False,
                "symbol_not_enabled_for_decision_cycle",
            ),
        )
        await trigger.start()
        self.addAsyncCleanup(trigger.stop)
        envelope = build_envelope(
            topic=topics.FEATURE_SNAPSHOTS,
            key=feature.symbol,
            payload_model=feature,
            source_component="test",
        )
        message = {"topic": topics.FEATURE_SNAPSHOTS, "key": feature.symbol, "payload": envelope.model_dump(mode="json")}

        await trigger.handle_feature_snapshot(message)

        # can_trigger 把 spot companion symbol 挡住 → 不入队。
        self.assertEqual(orchestrator.calls, [])
class TestGatewayTriggerSnapshotTsParity(unittest.TestCase):
    """R3-P1-U-B 回归：market_gateway.apply_remote_snapshot 用 `<` 接收
    （同 ms 新 tick 合法接受），trigger_policy.should_trigger 也必须用 `<`
    拒绝，而不是 `==` 就早早抹掉合法更新。同 ts 不同内容必须能触发 decision。"""

    def test_same_snapshot_ts_with_material_change_triggers_decision(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "decision_min_interval_seconds_15m": 60.0,
                "decision_min_price_move_bps": 5.0,
                "decision_min_momentum_delta": 0.2,
            }
        )
        policy = DecisionTriggerPolicy(settings=settings)
        base_ts = utc_now()
        feature = _feature(snapshot_ts=base_ts, momentum=0.1, regime="trend")
        market = _market(snapshot_ts=base_ts, last_price=67_000.0)
        # 记录 initial decision
        allowed, _ = policy.should_trigger(
            feature_snapshot=feature, market_snapshot=market, timeframe="15m",
        )
        self.assertTrue(allowed)
        policy.record_trigger(feature_snapshot=feature, market_snapshot=market, timeframe="15m")

        # 同 snapshot_ts 新内容（price 大幅移动）→ 必须 fall-through 到
        # material_change 并触发
        bigger_move_market = _market(snapshot_ts=base_ts, last_price=67_500.0)  # +74 bps
        allowed2, reason2 = policy.should_trigger(
            feature_snapshot=feature,
            market_snapshot=bigger_move_market,
            timeframe="15m",
        )
        self.assertTrue(allowed2, f"same-ts material change must trigger (got reason={reason2})")
        self.assertEqual(reason2, "material_change")

    def test_strictly_older_snapshot_ts_is_rejected(self) -> None:
        """严格更旧的 ts 仍然要被拒（reorder / replay 防御）。"""
        settings = AATSSettings.model_validate(
            {"decision_min_interval_seconds_15m": 60.0}
        )
        policy = DecisionTriggerPolicy(settings=settings)
        base_ts = utc_now()
        feature = _feature(snapshot_ts=base_ts, momentum=0.1, regime="trend")
        market = _market(snapshot_ts=base_ts, last_price=67_000.0)
        policy.should_trigger(feature_snapshot=feature, market_snapshot=market, timeframe="15m")
        policy.record_trigger(feature_snapshot=feature, market_snapshot=market, timeframe="15m")

        older_ts = base_ts - timedelta(seconds=5)
        older_feature = _feature(snapshot_ts=older_ts, momentum=0.5, regime="trend")
        older_market = _market(snapshot_ts=older_ts, last_price=67_999.0)  # 即使内容变化也应拒
        allowed, reason = policy.should_trigger(
            feature_snapshot=older_feature,
            market_snapshot=older_market,
            timeframe="15m",
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, "out_of_order_market_snapshot")


class TestFeatureSnapshotHintPropagation(unittest.IsolatedAsyncioTestCase):
    """R3-P1-U-A 回归：trigger 收到的 feature envelope 必须向下传给 run_cycle
    成为 feature_snapshot_hint，保证 DecisionContext.feature_snapshot_ref 与
    触发 cycle 的那条 FEATURE_SNAPSHOT envelope 完全一致，消除 trigger 评估
    与 build 读取之间新 snapshot 抢跑导致的 ref 漂移。"""

    async def test_run_cycle_receives_original_feature_envelope_as_hint(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "enabled_decision_timeframes": ["15m"],
                "decision_min_interval_seconds_15m": 0.0,
            }
        )
        policy = DecisionTriggerPolicy(settings=settings)
        base_ts = utc_now()
        market = _market(snapshot_ts=base_ts, last_price=67_000.0)
        feature = _feature(snapshot_ts=base_ts, momentum=0.1, regime="trend")

        class _HintCapturingOrchestrator:
            def __init__(self) -> None:
                self.hints: list[object] = []

            async def run_cycle(self, *, symbol: str, timeframe: str, feature_snapshot_hint=None, market_snapshot_hint=None):
                self.hints.append(feature_snapshot_hint)

        class _FakeMarketGateway:
            def latest_snapshot(self, symbol: str):
                return market if symbol == "BTC-USDT" else None

        orchestrator = _HintCapturingOrchestrator()
        trigger = DecisionCycleTrigger(
            orchestrator=orchestrator,
            market_gateway=_FakeMarketGateway(),
            policy=policy,
        )
        await trigger.start()
        self.addAsyncCleanup(trigger.stop)
        envelope = build_envelope(
            topic=topics.FEATURE_SNAPSHOTS,
            key=feature.symbol,
            payload_model=feature,
            source_component="test",
        )
        message = {
            "topic": topics.FEATURE_SNAPSHOTS,
            "key": feature.symbol,
            "payload": envelope.model_dump(mode="json"),
        }

        await trigger.handle_feature_snapshot(message)
        # Queue 路径：dispatcher 异步消费，等它把 run_cycle 跑完。
        await trigger._trigger_queue.join()

        self.assertEqual(len(orchestrator.hints), 1)
        hint = orchestrator.hints[0]
        self.assertIsNotNone(hint)
        # hint 必须是触发 cycle 的那条 envelope：event_id / payload 完全一致
        self.assertEqual(hint.event_id, envelope.event_id)  # type: ignore[union-attr]
        self.assertEqual(hint.topic, topics.FEATURE_SNAPSHOTS)  # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main()
