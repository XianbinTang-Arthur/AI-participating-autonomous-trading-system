"""Round 3 Phase 1.3 · DecisionOrchestrator paper trading shadow hook 单测。

覆盖:
- service=None → hook 零开销 skip
- service.enabled()=False → hook skip
- service.evaluate_candidates 抛异常 → hook swallow + log warning, 不 propagate
- publish_model 失败 → 继续下一个 candidate，不 propagate
- 正常路径 → 每个 decision publish 到 STRATEGY_FAMILY_SHADOW_DECISIONS
"""
from __future__ import annotations

import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aats.bootstrap.settings import AATSSettings
from aats.events import topics
from aats.services.decision_engine.orchestrator import DecisionOrchestrator


def _make_orch(*, shadow_service=None) -> DecisionOrchestrator:
    orch = DecisionOrchestrator.__new__(DecisionOrchestrator)
    orch.bus = MagicMock()
    orch.logger = MagicMock()
    orch.paper_trading_shadow_service = shadow_service
    return orch


def _make_target():
    t = MagicMock()
    t.decision_id = "decision_test"
    t.symbol = "BTC-USDT-SWAP"
    t.target_position_qty = Decimal("0")
    t.current_position_qty = Decimal("0")
    t.strategy_family = "independent"
    return t


class _CountingAccountService:
    def __init__(self) -> None:
        self.refresh_kwargs: list[dict[str, object]] = []

    async def refresh(self, **kwargs):
        self.refresh_kwargs.append(dict(kwargs))
        return None


class TestPaperTradingHookSkipPaths(unittest.IsolatedAsyncioTestCase):
    """关键不变性：service None / 未 enabled → 零开销 skip。"""

    async def test_service_is_none_returns_empty_list(self) -> None:
        orch = _make_orch(shadow_service=None)
        result = await orch._maybe_record_paper_trading_shadows(
            context=MagicMock(),
            baseline=MagicMock(),
            live_target=_make_target(),
            symbol="BTC-USDT-SWAP",
        )
        self.assertEqual(result, [])

    async def test_service_not_enabled_returns_empty_list(self) -> None:
        svc = MagicMock()
        svc.enabled = MagicMock(return_value=False)
        orch = _make_orch(shadow_service=svc)
        result = await orch._maybe_record_paper_trading_shadows(
            context=MagicMock(),
            baseline=MagicMock(),
            live_target=_make_target(),
            symbol="BTC-USDT-SWAP",
        )
        self.assertEqual(result, [])
        # enabled() 被 check，evaluate_candidates 不应被调
        svc.evaluate_candidates.assert_not_called()


class TestDecisionAccountRefreshScope(unittest.IsolatedAsyncioTestCase):
    async def test_real_market_paper_does_not_force_account_refresh_before_decision(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "paper_live",
                "market_data_backend": "okx",
                "execution_backend": "paper",
                "account_backend": "okx",
                "account_read_enabled": True,
            }
        )
        account_service = _CountingAccountService()
        orch = DecisionOrchestrator.__new__(DecisionOrchestrator)
        orch.context_builder = SimpleNamespace(
            settings=settings,
            account_service=account_service,
            mode_controller=SimpleNamespace(
                environment_capabilities=SimpleNamespace(exchange_coupled=False)
            ),
        )

        await orch._refresh_account_state_for_decision()

        self.assertEqual(account_service.refresh_kwargs, [])

    async def test_exchange_coupled_decision_forces_account_state_refresh(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "guarded_live",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
            }
        )
        account_service = _CountingAccountService()
        orch = DecisionOrchestrator.__new__(DecisionOrchestrator)
        orch.context_builder = SimpleNamespace(
            settings=settings,
            account_service=account_service,
            mode_controller=SimpleNamespace(
                environment_capabilities=SimpleNamespace(exchange_coupled=True)
            ),
        )

        await orch._refresh_account_state_for_decision()

        self.assertEqual(account_service.refresh_kwargs, [{"force_account_state": True}])


class TestPaperTradingHookErrorHandling(unittest.IsolatedAsyncioTestCase):
    """关键不变性：任何 shadow 异常绝不 propagate 到 run_cycle。"""

    async def test_evaluate_raises_swallowed(self) -> None:
        svc = MagicMock()
        svc.enabled = MagicMock(return_value=True)
        svc.evaluate_candidates = MagicMock(side_effect=RuntimeError("boom"))
        orch = _make_orch(shadow_service=svc)

        # 不 raise
        result = await orch._maybe_record_paper_trading_shadows(
            context=MagicMock(),
            baseline=MagicMock(),
            live_target=_make_target(),
            symbol="BTC-USDT-SWAP",
        )
        self.assertEqual(result, [])

    async def test_publish_raises_does_not_propagate(self) -> None:
        """即使单个 publish_model 失败, 其他 candidate 仍然继续 publish."""
        from aats.schemas.strategy_shadow import StrategyFamilyShadowDecision

        def _mk_decision(cid: str) -> StrategyFamilyShadowDecision:
            return StrategyFamilyShadowDecision(
                decision_id="d1",
                symbol="BTC-USDT-SWAP",
                timeframe="1m",
                candidate_id=cid,
                candidate_family="independent",
                candidate_config_version="v1",
                baseline_family="independent",
                baseline_target_qty=Decimal("0"),
                baseline_action="hold",
                shadow_target_qty=Decimal("0.001"),
                shadow_action="open_long",
                would_override_baseline=True,
                shadow_action_type="entry_override",
            )

        svc = MagicMock()
        svc.enabled = MagicMock(return_value=True)
        svc.evaluate_candidates = MagicMock(
            return_value=[_mk_decision("c1"), _mk_decision("c2")]
        )
        orch = _make_orch(shadow_service=svc)

        # 让 publish 第一次抛、第二次成功
        publish_calls = [0]

        async def _flaky_publish(**kwargs):
            publish_calls[0] += 1
            if publish_calls[0] == 1:
                raise RuntimeError("nats down")
            env = MagicMock()
            env.event_id = f"event_{publish_calls[0]}"
            return env

        import aats.services.decision_engine.orchestrator as orch_mod

        original_publish = orch_mod.publish_model
        orch_mod.publish_model = _flaky_publish
        try:
            result = await orch._maybe_record_paper_trading_shadows(
                context=MagicMock(),
                baseline=MagicMock(),
                live_target=_make_target(),
                symbol="BTC-USDT-SWAP",
            )
        finally:
            orch_mod.publish_model = original_publish

        # 第一条失败不影响第二条，返回 1 个成功 event_id
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0], "event_2")


class TestPaperTradingHookHappyPath(unittest.IsolatedAsyncioTestCase):
    async def test_publishes_to_correct_topic(self) -> None:
        from aats.schemas.strategy_shadow import StrategyFamilyShadowDecision

        decision = StrategyFamilyShadowDecision(
            decision_id="d1",
            symbol="BTC-USDT-SWAP",
            timeframe="1m",
            candidate_id="c1",
            candidate_family="independent",
            candidate_config_version="v1",
            baseline_family="independent",
            baseline_target_qty=Decimal("0"),
            baseline_action="hold",
            shadow_target_qty=Decimal("0"),
            shadow_action="hold",
            would_override_baseline=False,
            shadow_action_type="same_as_baseline",
        )
        svc = MagicMock()
        svc.enabled = MagicMock(return_value=True)
        svc.evaluate_candidates = MagicMock(return_value=[decision])
        orch = _make_orch(shadow_service=svc)

        observed_topics: list[str] = []

        async def _capture_publish(*, bus, topic, key, payload_model, source_component):
            observed_topics.append(topic)
            env = MagicMock()
            env.event_id = "event_1"
            return env

        import aats.services.decision_engine.orchestrator as orch_mod

        original_publish = orch_mod.publish_model
        orch_mod.publish_model = _capture_publish
        try:
            result = await orch._maybe_record_paper_trading_shadows(
                context=MagicMock(),
                baseline=MagicMock(),
                live_target=_make_target(),
                symbol="BTC-USDT-SWAP",
            )
        finally:
            orch_mod.publish_model = original_publish

        self.assertEqual(result, ["event_1"])
        self.assertEqual(
            observed_topics,
            [topics.STRATEGY_FAMILY_SHADOW_DECISIONS],
            "必须 publish 到 STRATEGY_FAMILY_SHADOW_DECISIONS topic (Grafana 依赖)",
        )


if __name__ == "__main__":
    unittest.main()
