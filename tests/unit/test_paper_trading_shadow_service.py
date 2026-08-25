"""Round 3 Phase 1.2 · PaperTradingShadowService 单测。

覆盖:
- `enabled()` 按 enabled flag + candidates 非空判断
- `evaluate_candidates()` 返回每个 candidate 的 decision
- 候选异常不影响其他候选 (绝不 re-raise)
- settings 不被 mutate (Pydantic 不可变)
- _classify_action_type 5 种 ShadowActionType
- _action_label 基础场景
"""
from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import MagicMock

from aats.bootstrap.settings import AATSSettings
from aats.services.strategy_engines.paper_trading_shadow import (
    PaperTradingShadowService,
    _action_label,
    _classify_action_type,
)


class TestEnabledGate(unittest.TestCase):
    def _make(self, *, enabled: bool, candidates: tuple[dict, ...]) -> PaperTradingShadowService:
        s = AATSSettings.model_validate(
            {
                "default_symbol": "BTC-USDT",
                "allowed_symbols": ("BTC-USDT",),
                "paper_trading_shadow_enabled": enabled,
                "paper_trading_shadow_candidates": candidates,
            }
        )
        return PaperTradingShadowService(base_settings=s, metrics=None, logger=MagicMock())

    def test_disabled_by_default(self) -> None:
        svc = self._make(enabled=False, candidates=())
        self.assertFalse(svc.enabled())

    def test_enabled_but_no_candidates_still_disabled(self) -> None:
        """Guard: enabled=True but empty list → 不启动（白白开销）。"""
        svc = self._make(enabled=True, candidates=())
        self.assertFalse(svc.enabled())

    def test_enabled_with_candidates(self) -> None:
        svc = self._make(
            enabled=True,
            candidates=(
                {"candidate_id": "low", "family": "independent", "overrides": {}},
            ),
        )
        self.assertTrue(svc.enabled())


class TestClassifyActionType(unittest.TestCase):
    def test_same_as_baseline(self) -> None:
        self.assertEqual(
            _classify_action_type(
                baseline_qty=Decimal("0.01"),
                shadow_qty=Decimal("0.01"),
                current_qty=Decimal("0"),
            ),
            "same_as_baseline",
        )

    def test_shadow_holds_instead(self) -> None:
        """baseline 想开仓 / shadow 不动 → hold_instead。"""
        self.assertEqual(
            _classify_action_type(
                baseline_qty=Decimal("0.01"),
                shadow_qty=Decimal("0"),
                current_qty=Decimal("0"),
            ),
            "hold_instead",
        )

    def test_shadow_enters_when_baseline_holds(self) -> None:
        """baseline 不动 / shadow 要开仓 → entry_override。"""
        self.assertEqual(
            _classify_action_type(
                baseline_qty=Decimal("0"),
                shadow_qty=Decimal("0.01"),
                current_qty=Decimal("0"),
            ),
            "entry_override",
        )

    def test_shadow_exits_when_baseline_holds(self) -> None:
        """baseline 持仓 hold / shadow 想 reduce → exit_override。"""
        self.assertEqual(
            _classify_action_type(
                baseline_qty=Decimal("0.02"),  # hold current
                shadow_qty=Decimal("0.01"),  # wants to reduce
                current_qty=Decimal("0.02"),
            ),
            "exit_override",
        )

    def test_reverse_override(self) -> None:
        """baseline long, shadow short → reverse_override。"""
        self.assertEqual(
            _classify_action_type(
                baseline_qty=Decimal("0.01"),
                shadow_qty=Decimal("-0.01"),
                current_qty=Decimal("0"),
            ),
            "reverse_override",
        )


class TestActionLabel(unittest.TestCase):
    def test_hold_on_small_diff(self) -> None:
        self.assertEqual(_action_label(Decimal("0.0001"), Decimal("0")), "hold")

    def test_close(self) -> None:
        self.assertEqual(_action_label(Decimal("0"), Decimal("0.01")), "close")

    def test_open_long(self) -> None:
        self.assertEqual(_action_label(Decimal("0.01"), Decimal("0")), "open_long")

    def test_open_short(self) -> None:
        self.assertEqual(_action_label(Decimal("-0.01"), Decimal("0")), "open_short")

    def test_reduce(self) -> None:
        """同方向但减仓。"""
        self.assertEqual(_action_label(Decimal("0.005"), Decimal("0.01")), "reduce")


class TestSafetyInvariants(unittest.TestCase):
    """关键不变性：service 绝不抛异常到 live path。"""

    def test_settings_not_mutated_by_service(self) -> None:
        """验证 service 对 settings 做 model_copy 而不是直接改。"""
        base = AATSSettings.model_validate(
            {
                "default_symbol": "BTC-USDT",
                "allowed_symbols": ("BTC-USDT",),
                "paper_trading_shadow_enabled": True,
                "paper_trading_shadow_candidates": (
                    {
                        "candidate_id": "test",
                        "family": "independent",
                        "overrides": {"max_notional_per_symbol": 999},
                    },
                ),
            }
        )
        original_max = base.max_notional_per_symbol
        svc = PaperTradingShadowService(base_settings=base, logger=MagicMock())
        # 构造 candidate engine (触发 model_copy)
        cached = svc._get_or_build_engine({"max_notional_per_symbol": 999}, "v1")
        # live settings 不能被改
        self.assertEqual(base.max_notional_per_symbol, original_max)
        # candidate engine 里的 settings 应该有 override
        self.assertEqual(cached.settings.max_notional_per_symbol, 999)

    def test_engine_cache_reuses_instances(self) -> None:
        base = AATSSettings.model_validate(
            {
                "default_symbol": "BTC-USDT",
                "allowed_symbols": ("BTC-USDT",),
            }
        )
        svc = PaperTradingShadowService(base_settings=base, logger=MagicMock())
        e1 = svc._get_or_build_engine({"max_notional_per_symbol": 500}, "v1")
        e2 = svc._get_or_build_engine({"max_notional_per_symbol": 500}, "v1")
        self.assertIs(e1, e2, "同 config_version 应缓存复用")
        e3 = svc._get_or_build_engine({"max_notional_per_symbol": 500}, "v2")
        self.assertIsNot(e1, e3, "不同 config_version 应新建")

    def test_candidate_exception_does_not_propagate(self) -> None:
        """某个 candidate 失败 → 返回部分结果，不抛。"""
        base = AATSSettings.model_validate(
            {
                "default_symbol": "BTC-USDT",
                "allowed_symbols": ("BTC-USDT",),
                "paper_trading_shadow_enabled": True,
                "paper_trading_shadow_candidates": (
                    # 故意用无效 family 触发错误（candidate payload missing id）
                    {},  # 缺 candidate_id → _evaluate_one returns None
                ),
            }
        )
        logger = MagicMock()
        svc = PaperTradingShadowService(base_settings=base, logger=logger)
        # 构造 minimal mock context/baseline/target (不真实触发 engine.build)

        context = MagicMock()
        context.timeframe = "1m"
        baseline = MagicMock()
        target = MagicMock()
        target.decision_id = "decision_test"
        target.symbol = "BTC-USDT-SWAP"
        target.target_position_qty = Decimal("0")
        target.current_position_qty = Decimal("0")
        target.strategy_family = "independent"

        # 不抛
        result = svc.evaluate_candidates(context=context, baseline=baseline, live_target=target)
        # 空候选 id → 该 candidate 被 skip，返回空列表
        self.assertEqual(result, [])


class TestWindowEvaluator(unittest.TestCase):
    """Phase 2: evaluate_windows() 按 (candidate_id, config_version) 聚合。"""

    def _make_service(self, *, window: int = 3) -> PaperTradingShadowService:
        base = AATSSettings.model_validate(
            {
                "default_symbol": "BTC-USDT",
                "allowed_symbols": ("BTC-USDT",),
            }
        )
        return PaperTradingShadowService(
            base_settings=base,
            logger=MagicMock(),
            evaluation_window=window,  # 小窗口易测
        )

    def _make_decision(
        self,
        *,
        candidate_id: str = "c1",
        config_version: str = "v1",
        baseline_action: str = "hold",
        shadow_action: str = "hold",
        would_override: bool = False,
        shadow_action_type: str = "same_as_baseline",
    ):
        from aats.schemas.strategy_shadow import StrategyFamilyShadowDecision

        return StrategyFamilyShadowDecision(
            decision_id=f"d_{candidate_id}_{len(shadow_action)}",
            symbol="BTC-USDT-SWAP",
            timeframe="1m",
            candidate_id=candidate_id,
            candidate_family="independent",
            candidate_config_version=config_version,
            baseline_family="independent",
            baseline_target_qty=Decimal("0"),
            baseline_action=baseline_action,
            shadow_target_qty=Decimal("0"),
            shadow_action=shadow_action,
            would_override_baseline=would_override,
            shadow_action_type=shadow_action_type,
        )

    def test_under_window_yields_no_evaluation(self) -> None:
        svc = self._make_service(window=3)
        svc._record_for_window(self._make_decision())
        svc._record_for_window(self._make_decision())
        # 2 < 3 → 不出 evaluation
        self.assertEqual(svc.evaluate_windows(), [])

    def test_exact_window_yields_one_evaluation(self) -> None:
        svc = self._make_service(window=3)
        for _ in range(3):
            svc._record_for_window(self._make_decision())
        result = svc.evaluate_windows()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].candidate_id, "c1")
        self.assertEqual(len(result[0].decision_ids), 3)

    def test_second_call_after_window_yields_nothing(self) -> None:
        """触发后 counter 重置，再次调用（无新 decision）返回空。"""
        svc = self._make_service(window=2)
        svc._record_for_window(self._make_decision())
        svc._record_for_window(self._make_decision())
        self.assertEqual(len(svc.evaluate_windows()), 1)
        # 再调一次，counter 已重置
        self.assertEqual(svc.evaluate_windows(), [])

    def test_multiple_candidates_independent_windows(self) -> None:
        svc = self._make_service(window=2)
        svc._record_for_window(self._make_decision(candidate_id="c1"))
        svc._record_for_window(self._make_decision(candidate_id="c1"))
        svc._record_for_window(self._make_decision(candidate_id="c2"))
        # c1 窗口满，c2 还没
        result = svc.evaluate_windows()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].candidate_id, "c1")
        # 再加一个 c2 触发
        svc._record_for_window(self._make_decision(candidate_id="c2"))
        result2 = svc.evaluate_windows()
        self.assertEqual(len(result2), 1)
        self.assertEqual(result2[0].candidate_id, "c2")

    def test_evaluation_counts_trades_and_overrides(self) -> None:
        svc = self._make_service(window=3)
        svc._record_for_window(self._make_decision(baseline_action="open_long"))
        svc._record_for_window(
            self._make_decision(
                baseline_action="open_long",
                shadow_action="hold",
                would_override=True,
                shadow_action_type="hold_instead",
            )
        )
        svc._record_for_window(self._make_decision())  # hold / hold same
        result = svc.evaluate_windows()
        self.assertEqual(len(result), 1)
        e = result[0]
        # baseline_trade_count: 2 个 open_long (非 hold)
        self.assertEqual(e.baseline_trade_count, 2)
        # shadow_trade_count: 没有非 hold shadow
        self.assertEqual(e.shadow_trade_count, 0)
        self.assertEqual(e.override_count, 1)
        self.assertEqual(e.agreement_count, 2)  # 两个 same_as_baseline
        self.assertEqual(e.disagreement_count, 1)
        # Phase 2 不算 PnL
        self.assertIsNone(e.baseline_net_pnl)
        self.assertIsNone(e.shadow_net_pnl)

    def test_window_size_zero_or_negative_is_coerced_to_1(self) -> None:
        """防御：恶意 window=0/-1 不能让 tracker 永远触发或从不触发。"""
        svc = self._make_service(window=0)
        self.assertGreaterEqual(svc._evaluation_window, 1)
        svc2 = self._make_service(window=-5)
        self.assertGreaterEqual(svc2._evaluation_window, 1)


if __name__ == "__main__":
    unittest.main()
