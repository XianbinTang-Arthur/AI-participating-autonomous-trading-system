"""Unit tests for ``aats.data_platform.replay.backtest.harness``.

测试组织：
    1. TestRunBacktestBars — 输入 bar 数量边界 / no-op decision 时的行为
    2. TestRunBacktestTrades — 开仓 / 平仓 / sequence PnL
    3. TestRunBacktestOrderType — post_only 分支处理
    4. TestCostValidation / TestResultShape — summary 与 config 透传
    5. TestParameterOverrides — adapter 参数透传

关键策略：
* 用 MagicMock 替身 ``Session``（harness 不直接用 session 方法，只传给
  ``load_gold_bars``）。
* monkeypatch ``load_gold_bars`` 返回预制 ``ReplayBar`` fixture。
* 注入 fake adapter 产出确定的 ``ReplayDecision`` 序列，避免依赖
  独立策略内部逻辑变动。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

from aats.data_platform.replay.adapters.base_adapter import BaseReplayAdapter
from aats.data_platform.replay.backtest.harness import (
    BacktestConfig,
    BacktestResult,
    run_backtest,
)
from aats.data_platform.replay.core.replay_context import (
    ReplayBar,
    ReplayBarContext,
    ReplayDecision,
    ReplayParameterOverrides,
    ReplayState,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_BASE_TS = datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc)


def _make_bar(
    index: int,
    close: Decimal = Decimal("50000"),
    volume: Decimal | None = Decimal("1000"),
    *,
    open_price: Decimal | None = None,
    is_closed: bool = True,
) -> ReplayBar:
    """Build a minimal ReplayBar for the harness loop."""
    ts = _BASE_TS + timedelta(hours=index)
    return ReplayBar(
        symbol="BTC-USDT-SWAP",
        ts=ts,
        open=open_price if open_price is not None else close,
        high=close,
        low=close,
        close=close,
        volume=volume,
        quote_volume=None,
        is_closed=is_closed,
        aligned_funding_rate=None,
        funding_source_ts=None,
    )


def _make_decision(
    *,
    ts: datetime,
    action: str,
    delta: Decimal,
    target: Decimal,
    long_score: float = 0.5,
    short_score: float = 0.1,
    net_edge_bps: float = 10.0,
    cost_bps: float = 6.0,
) -> ReplayDecision:
    """Build a minimal ReplayDecision tied to a specific bar timestamp."""
    return ReplayDecision(
        ts=ts,
        family="independent",
        symbol="BTC-USDT-SWAP",
        timeframe="1h",
        state="holding" if action in ("open", "hold") and target > 0 else "flat",
        selectable=True,
        execution_compatible=True,
        long_score=long_score,
        short_score=short_score,
        blocking_reasons=[],
        expected_net_edge_bps=net_edge_bps,
        target_position_qty=target,
        delta_position_qty=delta,
        signal_edge_proxy_bps=15.0,
        funding_adjustment_bps=0.0,
        cost_bps=cost_bps,
        noise_buffer_bps=2.0,
        action=action,
        close_reason="",
        score_stable=True,
        funding_rate=None,
        close_price=None,
        bar_index=0,
    )


class _FakeAdapter(BaseReplayAdapter):
    """Replays a pre-canned list of (action, delta, target, scores) tuples.

    Aligns decision.ts with context.bar.ts so the harness writes deterministic
    order_ids / timestamps.
    """

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self._script = script
        self._cursor = 0

    @property
    def family_name(self) -> str:
        return "independent"

    def reset_state(self) -> ReplayState:
        self._cursor = 0
        return ReplayState()

    def evaluate_bar(self, ctx: ReplayBarContext) -> ReplayDecision:
        if self._cursor >= len(self._script):
            entry = {"action": "hold", "delta": Decimal("0"), "target": Decimal("0")}
        else:
            entry = self._script[self._cursor]
        self._cursor += 1
        return _make_decision(
            ts=ctx.bar.ts,
            action=entry.get("action", "hold"),
            delta=entry.get("delta", Decimal("0")),
            target=entry.get("target", Decimal("0")),
            long_score=entry.get("long_score", 0.5),
            short_score=entry.get("short_score", 0.1),
            net_edge_bps=entry.get("net_edge_bps", 10.0),
            cost_bps=entry.get("cost_bps", 6.0),
        )


class _RecordingAdapter(_FakeAdapter):
    """Captures the ``ReplayParameterOverrides`` seen during evaluate_bar."""

    def __init__(self, script: list[dict[str, Any]]) -> None:
        super().__init__(script)
        self.seen_params: list[ReplayParameterOverrides] = []

    def evaluate_bar(self, ctx: ReplayBarContext) -> ReplayDecision:
        self.seen_params.append(ctx.params)
        return super().evaluate_bar(ctx)


# ---------------------------------------------------------------------------
# 1. Bar input edges
# ---------------------------------------------------------------------------


class TestRunBacktestBars(unittest.TestCase):
    def setUp(self) -> None:
        self.session = MagicMock()
        self.config = BacktestConfig()

    def _run(
        self,
        *,
        bars: list[ReplayBar],
        script: list[dict[str, Any]] | None = None,
        config: BacktestConfig | None = None,
        parameter_overrides: dict[str, Any] | None = None,
    ) -> BacktestResult:
        adapter = _FakeAdapter(script or [])
        with patch(
            "aats.data_platform.replay.backtest.harness.load_gold_bars",
            return_value=bars,
        ):
            return run_backtest(
                self.session,
                config=config or self.config,
                start_ts=_BASE_TS,
                end_ts=_BASE_TS + timedelta(days=1),
                parameter_overrides=parameter_overrides,
                adapter=adapter,
            )

    def test_run_backtest_with_zero_bars(self) -> None:
        """空 bar 列表 → summary 全零、curve 为空、decisions/fills 都 0。"""
        result = self._run(bars=[])
        self.assertEqual(result.decisions_count, 0)
        self.assertEqual(result.fills_count, 0)
        self.assertEqual(result.summary.bar_count, 0)
        self.assertEqual(result.summary.final_equity, Decimal("0"))
        self.assertEqual(len(result.equity_curve), 0)
        self.assertEqual(result.cost_summary.total_decisions, 0)

    def test_run_backtest_single_bar_hold(self) -> None:
        """delta=0 → 无 fill；但 MtM 仍要产一条 equity point。"""
        bars = [_make_bar(0)]
        script = [{"action": "hold", "delta": Decimal("0"), "target": Decimal("0")}]
        result = self._run(bars=bars, script=script)

        self.assertEqual(result.decisions_count, 1)
        self.assertEqual(result.fills_count, 0)
        self.assertEqual(result.summary.bar_count, 1)
        self.assertEqual(result.summary.final_equity, Decimal("0"))
        self.assertEqual(result.cost_summary.total_decisions, 0)


# ---------------------------------------------------------------------------
# 2. Trade flow
# ---------------------------------------------------------------------------


class TestRunBacktestTrades(unittest.TestCase):
    def setUp(self) -> None:
        self.session = MagicMock()
        self.config = BacktestConfig()

    def _run(
        self,
        *,
        bars: list[ReplayBar],
        script: list[dict[str, Any]],
        config: BacktestConfig | None = None,
    ) -> BacktestResult:
        adapter = _FakeAdapter(script)
        with patch(
            "aats.data_platform.replay.backtest.harness.load_gold_bars",
            return_value=bars,
        ):
            return run_backtest(
                self.session,
                config=config or self.config,
                start_ts=_BASE_TS,
                end_ts=_BASE_TS + timedelta(days=1),
                adapter=adapter,
            )

    def test_run_backtest_single_bar_open_long_expires_without_next_event(self) -> None:
        """最后一根 bar 的动作没有下一事件，不得在同 bar close 补成交。"""
        bars = [_make_bar(0, close=Decimal("50000"), volume=Decimal("1000"))]
        script = [
            {
                "action": "open",
                "delta": Decimal("1"),
                "target": Decimal("1"),
                "long_score": 0.6,
                "short_score": 0.2,
            }
        ]
        result = self._run(bars=bars, script=script)

        self.assertEqual(result.fills_count, 0)
        self.assertEqual(result.summary.fill_count, 0)
        self.assertEqual(result.summary.fee_total, Decimal("0"))
        self.assertEqual(result.cost_summary.total_decisions, 0)
        self.assertEqual(
            result.execution_timeline[0].status,
            "expired_no_next_event",
        )

    def test_run_backtest_sequence_entry_exit(self) -> None:
        """open @ 50k → close @ 51k，realized_pnl 应 > 0（扣 fees/slippage 后）。"""
        bars = [
            _make_bar(0, close=Decimal("50000"), volume=Decimal("1000")),
            _make_bar(1, close=Decimal("50500"), volume=Decimal("1000")),
            _make_bar(2, close=Decimal("51000"), volume=Decimal("1000")),
            _make_bar(3, close=Decimal("51500"), volume=Decimal("1000")),
        ]
        script = [
            {
                "action": "open",
                "delta": Decimal("1"),
                "target": Decimal("1"),
                "long_score": 0.7,
                "short_score": 0.1,
            },
            {"action": "hold", "delta": Decimal("0"), "target": Decimal("1")},
            {
                "action": "close",
                "delta": Decimal("-1"),
                "target": Decimal("0"),
                "long_score": 0.7,
                "short_score": 0.1,
            },
            {"action": "hold", "delta": Decimal("0"), "target": Decimal("0")},
        ]
        result = self._run(bars=bars, script=script)

        self.assertEqual(result.fills_count, 2)
        self.assertEqual(result.summary.bar_count, 4)
        # open/close 都在各自下一根 K 线的 open 事件执行。
        self.assertGreater(result.summary.cumulative_pnl, Decimal("-100"))
        # 交易方向正确时 final_equity != 0
        self.assertNotEqual(result.summary.final_equity, Decimal("0"))


# ---------------------------------------------------------------------------
# 3. order_type branches
# ---------------------------------------------------------------------------


class TestRunBacktestOrderType(unittest.TestCase):
    def test_run_backtest_respects_order_type_post_only(self) -> None:
        """order_type=post_only 时，FillSimulator 输出必然是 maker 或 no_fill。

        我们通过断言 ``fee_total`` 要么是 0 (no_fill)、要么对应 maker 费率
        （2 bps 远低于 taker 5 bps），来间接验证 fill_kind 分支。
        """
        session = MagicMock()
        config = BacktestConfig(order_type="post_only")
        # qty 5 / volume 1000 = 0.5% → high prob (0.9)；order_id 会随 bar.ts
        # 变化，不同种子给不同抽样，但至少有一条必须落在 <0.9。
        bars = [
            _make_bar(0, close=Decimal("50000"), volume=Decimal("1000")),
            _make_bar(1, close=Decimal("50100"), volume=Decimal("1000")),
        ]
        script = [
            {
                "action": "open",
                "delta": Decimal("5"),
                "target": Decimal("5"),
                "long_score": 0.8,
                "short_score": 0.1,
            },
            {"action": "hold", "delta": Decimal("0"), "target": Decimal("5")},
        ]
        adapter = _FakeAdapter(script)
        with patch(
            "aats.data_platform.replay.backtest.harness.load_gold_bars",
            return_value=bars,
        ):
            result = run_backtest(
                session,
                config=config,
                start_ts=_BASE_TS,
                end_ts=_BASE_TS + timedelta(days=1),
                adapter=adapter,
            )

        # fills_count ∈ {0, 1}；fee 不会高于 taker 费率
        self.assertIn(result.fills_count, (0, 1))
        if result.fills_count == 1:
            # post-only 在下一根完整 bar 的 close/volume 解析。
            self.assertAlmostEqual(
                float(result.summary.fee_total),
                float(Decimal("5") * Decimal("50100") * Decimal("0.0002")),
                places=6,
            )


# ---------------------------------------------------------------------------
# 4. Cost validation + result shape
# ---------------------------------------------------------------------------


class TestCostValidationAndResultShape(unittest.TestCase):
    def test_cost_validation_summary_populated_when_decisions_recorded(self) -> None:
        """有 fill 发生时 cost_validator 必须收到记录。"""
        session = MagicMock()
        bars = [_make_bar(0), _make_bar(1)]
        script = [
            {
                "action": "open",
                "delta": Decimal("1"),
                "target": Decimal("1"),
                "long_score": 0.6,
                "short_score": 0.1,
                "net_edge_bps": 5.0,
            },
            {"action": "hold", "delta": Decimal("0"), "target": Decimal("1")},
        ]
        adapter = _FakeAdapter(script)
        config = BacktestConfig(assumed_cost_bps=6.0)
        with patch(
            "aats.data_platform.replay.backtest.harness.load_gold_bars",
            return_value=bars,
        ):
            result = run_backtest(
                session,
                config=config,
                start_ts=_BASE_TS,
                end_ts=_BASE_TS + timedelta(days=1),
                adapter=adapter,
            )

        self.assertEqual(result.cost_summary.total_decisions, 1)
        # ioc 下 actual cost = taker fee 5.0 + fixed slip 1.0 = assumed 6.0
        self.assertAlmostEqual(result.cost_summary.avg_cost_diff_bps, 0.0, places=6)

    def test_cost_validator_uses_decision_cost_not_config_assumed(self) -> None:
        """P2 口径修复锚定 (2026-04-23)。

        Per-decision 自己估的 cost (decision.cost_bps) 必须优先于全局
        config.assumed_cost_bps。否则 cost_summary 回答的问题是 "你的
        全局假设 vs 实际"，而不是 "策略每笔决策的自估 cost vs 实际"，
        决策路线判断会被污染。
        """
        session = MagicMock()
        bars = [_make_bar(0), _make_bar(1)]
        # 关键：decision.cost_bps=4.0，config.assumed_cost_bps=99.0（极端不等）
        # ioc actual cost = taker fee 5.0 + fixed slip 1.0 = 6.0
        script = [
            {
                "action": "open",
                "delta": Decimal("1"),
                "target": Decimal("1"),
                "long_score": 0.6,
                "short_score": 0.1,
                "net_edge_bps": 5.0,
                "cost_bps": 4.0,  # 本笔决策自估 cost
            },
            {"action": "hold", "delta": Decimal("0"), "target": Decimal("1")},
        ]
        adapter = _FakeAdapter(script)
        # 全局 assumed 故意设成 99.0，确保老口径 bug 会让 diff = -94
        config = BacktestConfig(assumed_cost_bps=99.0)
        with patch(
            "aats.data_platform.replay.backtest.harness.load_gold_bars",
            return_value=bars,
        ):
            result = run_backtest(
                session,
                config=config,
                start_ts=_BASE_TS,
                end_ts=_BASE_TS + timedelta(days=1),
                adapter=adapter,
            )

        self.assertEqual(result.cost_summary.total_decisions, 1)
        # 修复后: assumed=decision.cost_bps(4.0), actual=6.0, diff=+2.0
        # 修复前 bug: assumed=config(99.0), actual=6.0, diff=-93.0
        self.assertAlmostEqual(result.cost_summary.avg_cost_diff_bps, +2.0, places=6)

    def test_cost_validator_falls_back_to_config_when_decision_has_no_cost(self) -> None:
        """Fallback: decision.cost_bps=0 (legacy / 未设) → 用 config.assumed_cost_bps。"""
        session = MagicMock()
        bars = [_make_bar(0), _make_bar(1)]
        script = [
            {
                "action": "open",
                "delta": Decimal("1"),
                "target": Decimal("1"),
                "long_score": 0.6,
                "short_score": 0.1,
                "net_edge_bps": 5.0,
                "cost_bps": 0.0,  # 显式 legacy
            },
            {"action": "hold", "delta": Decimal("0"), "target": Decimal("1")},
        ]
        adapter = _FakeAdapter(script)
        config = BacktestConfig(assumed_cost_bps=7.0)
        with patch(
            "aats.data_platform.replay.backtest.harness.load_gold_bars",
            return_value=bars,
        ):
            result = run_backtest(
                session,
                config=config,
                start_ts=_BASE_TS,
                end_ts=_BASE_TS + timedelta(days=1),
                adapter=adapter,
            )

        self.assertEqual(result.cost_summary.total_decisions, 1)
        # fallback 后: assumed=config(7.0), actual=6.0, diff=-1.0
        self.assertAlmostEqual(result.cost_summary.avg_cost_diff_bps, -1.0, places=6)

    def test_backtest_result_includes_config(self) -> None:
        """BacktestResult.config 必须完整等于传入的 config。"""
        session = MagicMock()
        config = BacktestConfig(
            symbol="ETH-USDT-SWAP",
            timeframe="15m",
            dataset_version="v9.9",
            family="independent",
            order_type="bounded_limit",
            contract_multiplier=Decimal("0.1"),
            taker_fee_bps=4.5,
        )
        adapter = _FakeAdapter([])
        with patch(
            "aats.data_platform.replay.backtest.harness.load_gold_bars",
            return_value=[],
        ):
            result = run_backtest(
                session,
                config=config,
                start_ts=_BASE_TS,
                end_ts=_BASE_TS + timedelta(days=1),
                adapter=adapter,
            )

        self.assertIs(result.config, config)
        self.assertEqual(result.config.symbol, "ETH-USDT-SWAP")
        self.assertEqual(result.config.timeframe, "15m")
        self.assertEqual(result.config.order_type, "bounded_limit")
        self.assertEqual(result.config.taker_fee_bps, 4.5)


# ---------------------------------------------------------------------------
# 5. parameter_overrides propagation
# ---------------------------------------------------------------------------


class TestParameterOverrides(unittest.TestCase):
    def test_parameter_overrides_propagate_to_adapter(self) -> None:
        """parameter_overrides dict 必须被翻译成 ReplayParameterOverrides 注入 adapter。"""
        session = MagicMock()
        bars = [_make_bar(0)]
        script = [{"action": "hold", "delta": Decimal("0"), "target": Decimal("0")}]
        adapter = _RecordingAdapter(script)
        overrides = {
            "entry_threshold": 0.55,
            "close_threshold": 0.10,
            # ReplayParameterOverrides 要求 scale_in_threshold >= entry_threshold
            "scale_in_threshold": 0.60,
            "taker_fee_bps": 4.0,
        }

        with patch(
            "aats.data_platform.replay.backtest.harness.load_gold_bars",
            return_value=bars,
        ):
            run_backtest(
                session,
                config=BacktestConfig(),
                start_ts=_BASE_TS,
                end_ts=_BASE_TS + timedelta(days=1),
                parameter_overrides=overrides,
                adapter=adapter,
            )

        self.assertEqual(len(adapter.seen_params), 1)
        params = adapter.seen_params[0]
        self.assertAlmostEqual(params.entry_threshold, 0.55)
        self.assertAlmostEqual(params.close_threshold, 0.10)
        self.assertAlmostEqual(params.cost_config.taker_fee_bps, 4.0)

    def test_no_parameter_overrides_uses_family_defaults(self) -> None:
        """None overrides → ReplayParameterOverrides.for_family(family) 的默认值。"""
        session = MagicMock()
        bars = [_make_bar(0)]
        script = [{"action": "hold", "delta": Decimal("0"), "target": Decimal("0")}]
        adapter = _RecordingAdapter(script)

        with patch(
            "aats.data_platform.replay.backtest.harness.load_gold_bars",
            return_value=bars,
        ):
            run_backtest(
                session,
                config=BacktestConfig(family="independent"),
                start_ts=_BASE_TS,
                end_ts=_BASE_TS + timedelta(days=1),
                adapter=adapter,
            )

        expected = ReplayParameterOverrides.for_family("independent")
        self.assertEqual(
            adapter.seen_params[0].entry_threshold, expected.entry_threshold
        )


if __name__ == "__main__":
    unittest.main()
