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
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

from aats.data_platform.replay.adapters.base_adapter import BaseReplayAdapter
from aats.data_platform.replay.adapters.independent_adapter import (
    IndependentReplayAdapter,
)
from aats.data_platform.replay.backtest.fill_simulator import FillSimulator
from aats.data_platform.replay.backtest.harness import (
    BacktestConfig,
    BacktestResult,
    run_backtest,
    validate_backtest_result_units,
)
from aats.data_platform.replay.core.replay_context import (
    ReplayBar,
    ReplayBarContext,
    ReplayDecision,
    ReplayParameterOverrides,
    ReplayState,
)
from tests.unit.replay_contract_fixtures import LINEAR_SWAP_CONTRACT, SPOT_CONTRACT


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_BASE_TS = datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc)


def _config(**overrides: Any) -> BacktestConfig:
    values = {
        "symbol": SPOT_CONTRACT.symbol,
        "instrument_contract": SPOT_CONTRACT,
        "spot_buy_fee_asset": "quote",
    }
    values.update(overrides)
    return BacktestConfig(**values)


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
        symbol="BTC-USDT",
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
    cost_bps_is_explicit: bool = True,
    signal_edge_bps: float | None = None,
    selectable: bool = True,
    execution_compatible: bool = True,
    score_stable: bool = True,
    blocking_reasons: list[str] | None = None,
) -> ReplayDecision:
    """Build a minimal ReplayDecision tied to a specific bar timestamp."""
    return ReplayDecision(
        ts=ts,
        family="independent",
        symbol="BTC-USDT",
        timeframe="1h",
        state="holding" if action in ("open", "hold") and target > 0 else "flat",
        selectable=selectable,
        execution_compatible=execution_compatible,
        long_score=long_score,
        short_score=short_score,
        blocking_reasons=[] if blocking_reasons is None else blocking_reasons,
        expected_net_edge_bps=net_edge_bps,
        target_position_qty=target,
        delta_position_qty=delta,
        signal_edge_proxy_bps=(
            net_edge_bps + cost_bps + 2.0
            if signal_edge_bps is None
            else signal_edge_bps
        ),
        funding_adjustment_bps=0.0,
        cost_bps=cost_bps,
        cost_bps_is_explicit=cost_bps_is_explicit,
        noise_buffer_bps=2.0,
        action=action,
        close_reason="",
        score_stable=score_stable,
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

    @property
    def algorithm_version(self) -> str:
        return "fake-independent-replay/v1"

    @property
    def accepted_parameter_keys(self) -> frozenset[str]:
        return frozenset(ReplayParameterOverrides.from_dict({}).to_dict()) | {
            "taker_fee_bps",
            "slippage_bps",
            "maker_fee_bps",
            "execution_style",
            "passive_bias",
            "maker_taker_bias",
        }

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
            cost_bps_is_explicit=entry.get("cost_bps_is_explicit", True),
            signal_edge_bps=entry.get("signal_edge_bps"),
            selectable=entry.get("selectable", True),
            execution_compatible=entry.get("execution_compatible", True),
            score_stable=entry.get("score_stable", True),
            blocking_reasons=entry.get("blocking_reasons"),
        )


class _RecordingAdapter(_FakeAdapter):
    """Captures the ``ReplayParameterOverrides`` seen during evaluate_bar."""

    def __init__(self, script: list[dict[str, Any]]) -> None:
        super().__init__(script)
        self.seen_params: list[ReplayParameterOverrides] = []

    def evaluate_bar(self, ctx: ReplayBarContext) -> ReplayDecision:
        self.seen_params.append(ctx.params)
        return super().evaluate_bar(ctx)


class _OpenThenCloseIndependentAdapter(IndependentReplayAdapter):
    """Use the real adapter state machine with one open signal then closes."""

    def _compute_book_score(self, bar: ReplayBar, *, leg: str) -> float:
        del bar, leg
        return 0.9 if len(self._bar_history) == 1 else 0.0


# ---------------------------------------------------------------------------
# 1. Bar input edges
# ---------------------------------------------------------------------------


class TestRunBacktestBars(unittest.TestCase):
    def setUp(self) -> None:
        self.session = MagicMock()
        self.config = _config()

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
                parameter_overrides={
                    "strategy_short_bias_enabled": False,
                    **(parameter_overrides or {}),
                },
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

    def test_non_tick_aligned_ohlc_is_rejected_before_adapter(self) -> None:
        bar = _make_bar(0, close=Decimal("50000.05"))
        with self.assertRaisesRegex(
            ValueError,
            "replay_bar_open_tick_misaligned",
        ):
            self._run(bars=[bar])


# ---------------------------------------------------------------------------
# 2. Trade flow
# ---------------------------------------------------------------------------


class TestRunBacktestTrades(unittest.TestCase):
    def setUp(self) -> None:
        self.session = MagicMock()
        self.config = _config()

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
                parameter_overrides={"strategy_short_bias_enabled": False},
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

    def test_smallest_valid_lot_is_not_swallowed_by_epsilon(self) -> None:
        tiny_contract = replace(
            SPOT_CONTRACT,
            lot_size=Decimal("0.00000001"),
            min_size=Decimal("0.00000001"),
        )
        tiny_qty = Decimal("0.00000001")
        result = self._run(
            bars=[_make_bar(0), _make_bar(1)],
            script=[
                {"action": "open", "delta": tiny_qty, "target": tiny_qty},
                {"action": "hold", "delta": Decimal("0"), "target": tiny_qty},
            ],
            config=_config(
                instrument_contract=tiny_contract,
                maker_fee_bps=0.0,
                taker_fee_bps=0.0,
                ioc_slippage_bps=0.0,
                max_volume_participation=Decimal("1"),
            ),
        )

        self.assertEqual(result.fills_count, 1)
        self.assertEqual(result.execution_timeline[0].status, "filled")
        self.assertEqual(
            result.cost_diagnostics[0].filled_exchange_quantity,
            tiny_qty,
        )

    def test_spot_base_fee_inventory_and_equity_are_bound_end_to_end(self) -> None:
        execution_bar = replace(
            _make_bar(
                1,
                close=Decimal("200"),
                open_price=Decimal("100"),
                volume=Decimal("10"),
            ),
            low=Decimal("100"),
        )
        result = self._run(
            bars=[
                _make_bar(0, close=Decimal("100"), volume=Decimal("10")),
                execution_bar,
            ],
            script=[
                {"action": "open", "delta": Decimal("1"), "target": Decimal("1")},
                {
                    "action": "hold",
                    "delta": Decimal("0"),
                    "target": Decimal("0.999"),
                },
            ],
            config=_config(
                maker_fee_bps=10.0,
                taker_fee_bps=10.0,
                ioc_slippage_bps=0.0,
                max_volume_participation=Decimal("1"),
                spot_buy_fee_asset="base",
            ),
        )

        self.assertEqual(result.summary.final_equity, Decimal("99.8"))
        self.assertEqual(result.summary.fee_total, Decimal("0.1"))
        diagnostic = result.cost_diagnostics[0]
        self.assertEqual(diagnostic.fee_currency, "USDT")
        self.assertEqual(diagnostic.fee_asset, "BTC")
        self.assertEqual(diagnostic.fee_asset_quantity, Decimal("0.001"))
        validate_backtest_result_units(result, require_complete_artifact=True)

    def test_base_fee_partial_fill_preserves_dust_through_hold_and_close(
        self,
    ) -> None:
        gross_fill = Decimal("0.1234")
        base_fee = Decimal("0.0000617")
        net_inventory = gross_fill - base_fee
        tradable_close = Decimal("0.1233")
        residual_dust = net_inventory - tradable_close
        result = self._run(
            bars=[
                _make_bar(
                    0,
                    close=Decimal("100"),
                    volume=gross_fill,
                ),
                _make_bar(1, close=Decimal("100"), volume=Decimal("10")),
                _make_bar(2, close=Decimal("100"), volume=Decimal("10")),
                _make_bar(3, close=Decimal("100"), volume=Decimal("10")),
                _make_bar(4, close=Decimal("100"), volume=Decimal("10")),
            ],
            script=[
                {"action": "open", "delta": Decimal("1"), "target": Decimal("1")},
                {
                    "action": "hold",
                    "delta": Decimal("0"),
                    "target": net_inventory,
                },
                {
                    "action": "close",
                    "delta": -net_inventory,
                    "target": Decimal("0"),
                },
                {
                    "action": "hold",
                    "delta": Decimal("0"),
                    "target": residual_dust,
                },
                {
                    "action": "close",
                    "delta": -residual_dust,
                    "target": Decimal("0"),
                },
            ],
            config=_config(
                maker_fee_bps=5.0,
                taker_fee_bps=5.0,
                ioc_slippage_bps=0.0,
                max_volume_participation=Decimal("1"),
                spot_buy_fee_asset="base",
            ),
        )

        self.assertEqual(result.fills_count, 2)
        self.assertEqual(
            tuple(record.status for record in result.execution_timeline),
            (
                "partial_fill",
                "no_order",
                "partial_fill",
                "no_order",
                "no_order",
            ),
        )
        self.assertEqual(
            tuple(
                diagnostic.filled_exchange_quantity
                for diagnostic in result.cost_diagnostics
            ),
            (gross_fill, tradable_close),
        )
        self.assertEqual(result.cost_diagnostics[0].fee_asset, "BTC")
        self.assertEqual(
            result.cost_diagnostics[0].fee_asset_quantity,
            base_fee,
        )
        self.assertEqual(result.cost_diagnostics[1].fee_asset, "USDT")
        self.assertEqual(result.summary.final_equity, Decimal("-0.012335"))
        validate_backtest_result_units(result, require_complete_artifact=True)
        tampered_timeline = list(result.execution_timeline)
        tampered_timeline[0] = replace(
            tampered_timeline[0],
            status="filled",
        )
        with self.assertRaisesRegex(
            ValueError,
            "backtest_execution_fill_status_mismatch",
        ):
            validate_backtest_result_units(
                replace(
                    result,
                    execution_timeline=tuple(tampered_timeline),
                ),
                require_complete_artifact=True,
            )
        dust_timeline = list(result.execution_timeline)
        dust_index = next(
            index
            for index, record in enumerate(dust_timeline)
            if record.status == "no_order" and record.action == "close"
        )
        dust_timeline[dust_index] = replace(
            dust_timeline[dust_index],
            requested_exchange_quantity=0,
        )
        with self.assertRaisesRegex(
            ValueError,
            "backtest_no_order_dust_close_invalid",
        ):
            validate_backtest_result_units(
                replace(result, execution_timeline=tuple(dust_timeline)),
                require_complete_artifact=True,
            )


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
        config = _config(order_type="post_only")
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
                parameter_overrides={"strategy_short_bias_enabled": False},
                adapter=adapter,
            )

        # fills_count ∈ {0, 1}；fee 不会高于 taker 费率
        self.assertEqual(result.fills_count, 1)
        # post-only 在下一根完整 bar 的 close/volume 解析。
        self.assertAlmostEqual(
            float(result.summary.fee_total),
            float(
                Decimal("5")
                * Decimal("50100")
                * Decimal("0.0002")
            ),
            places=6,
        )
        validate_backtest_result_units(result, require_complete_artifact=True)


# ---------------------------------------------------------------------------
# 4. Cost validation + result shape
# ---------------------------------------------------------------------------


class TestCostValidationAndResultShape(unittest.TestCase):
    @staticmethod
    def _one_fill_result() -> BacktestResult:
        bars = [_make_bar(0), _make_bar(1)]
        adapter = _FakeAdapter(
            [
                {
                    "action": "open",
                    "delta": Decimal("1"),
                    "target": Decimal("1"),
                    "net_edge_bps": 5.0,
                    "cost_bps": 4.0,
                },
                {"action": "hold", "delta": Decimal("0"), "target": Decimal("1")},
            ]
        )
        with patch(
            "aats.data_platform.replay.backtest.harness.load_gold_bars",
            return_value=bars,
        ):
            return run_backtest(
                MagicMock(),
                config=_config(),
                start_ts=_BASE_TS,
                end_ts=_BASE_TS + timedelta(days=1),
                parameter_overrides={"strategy_short_bias_enabled": False},
                adapter=adapter,
            )

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
        config = _config()
        with patch(
            "aats.data_platform.replay.backtest.harness.load_gold_bars",
            return_value=bars,
        ):
            result = run_backtest(
                session,
                config=config,
                start_ts=_BASE_TS,
                end_ts=_BASE_TS + timedelta(days=1),
                parameter_overrides={"strategy_short_bias_enabled": False},
                adapter=adapter,
            )

        self.assertEqual(result.cost_summary.total_decisions, 1)
        # ioc 下 actual cost = taker fee 5.0 + fixed slip 1.0 = assumed 6.0
        self.assertAlmostEqual(result.cost_summary.avg_cost_diff_bps, 0.0, places=6)

    def test_complete_artifact_closes_cost_identity_arithmetic_and_timeline(
        self,
    ) -> None:
        result = self._one_fill_result()
        diagnostic = result.cost_diagnostics[0]
        filled_timing = next(
            record
            for record in result.execution_timeline
            if record.status in {"filled", "partial_fill"}
        )

        self.assertEqual(diagnostic.decision_id, filled_timing.decision_id)
        self.assertIs(
            validate_backtest_result_units(
                result,
                require_complete_artifact=True,
            ),
            SPOT_CONTRACT,
        )

    def test_result_validator_rejects_falsy_non_mapping_parameter_extra(
        self,
    ) -> None:
        result = self._one_fill_result()
        for invalid in (None, [], ""):
            params = replace(result.resolved_parameters)
            object.__setattr__(params, "extra", invalid)
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ValueError,
                "backtest_resolved_parameters_extra_must_be_empty",
            ):
                validate_backtest_result_units(
                    replace(result, resolved_parameters=params),
                    require_complete_artifact=True,
                )

    def test_complete_artifact_rejects_tampered_cost_evidence(self) -> None:
        result = self._one_fill_result()
        diagnostic = result.cost_diagnostics[0]
        cases = (
            (
                replace(
                    result,
                    cost_diagnostics=(
                        replace(diagnostic, actual_fee_bps=500.0),
                    ),
                ),
                "backtest_cost_diagnostic_fee_rate_config_mismatch",
            ),
            (
                replace(
                    result,
                    cost_diagnostics=(
                        replace(diagnostic, decision_id="unrelated"),
                    ),
                ),
                "backtest_cost_diagnostic_timeline_mismatch",
            ),
            (
                replace(
                    result,
                    cost_diagnostics=(
                        replace(
                            diagnostic,
                            cost_diff_bps=999.0,
                            actual_net_edge_bps=-777.0,
                            edge_flipped_negative=True,
                        ),
                    ),
                ),
                "backtest_cost_diagnostic_arithmetic_mismatch",
            ),
            (
                replace(
                    result,
                    cost_diagnostics=(
                        replace(diagnostic, actual_slippage_bps=456.0),
                    ),
                ),
                "backtest_cost_diagnostic_fill_replay_mismatch",
            ),
            (
                replace(
                    result,
                    cost_summary=replace(
                        result.cost_summary,
                        avg_cost_diff_bps=123.0,
                    ),
                ),
                "backtest_cost_summary_recalculation_mismatch",
            ),
        )
        for tampered, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(
                ValueError,
                reason,
            ):
                validate_backtest_result_units(
                    tampered,
                    require_complete_artifact=True,
                )

    def test_complete_artifact_rejects_declared_ledger_offset(self) -> None:
        result = self._one_fill_result()
        curve = list(result.equity_curve)
        last = curve[-1]
        assert last.realized_pnl is not None
        assert last.accumulated_fees is not None
        curve[-1] = replace(
            last,
            realized_pnl=last.realized_pnl + Decimal("1"),
            accumulated_fees=last.accumulated_fees + Decimal("1"),
        )
        with self.assertRaisesRegex(
            ValueError,
            "backtest_equity_position_replay_mismatch",
        ):
            validate_backtest_result_units(
                replace(result, equity_curve=tuple(curve)),
                require_complete_artifact=True,
            )

    def test_complete_artifact_rejects_noncanonical_numeric_types(self) -> None:
        result = self._one_fill_result()
        diagnostic = result.cost_diagnostics[0]
        cases = (
            (
                replace(
                    result,
                    summary=replace(result.summary, final_equity=0.0),
                ),
                "backtest_summary_final_equity_invalid",
            ),
            (
                replace(
                    result,
                    summary=replace(result.summary, sharpe_ratio=True),
                ),
                "backtest_summary_sharpe_ratio_invalid",
            ),
            (
                replace(
                    result,
                    summary=replace(result.summary, sharpe_ratio="0"),
                ),
                "backtest_summary_sharpe_ratio_invalid",
            ),
            (
                replace(
                    result,
                    summary=replace(
                        result.summary,
                        final_equity=Decimal("NaN"),
                    ),
                ),
                "backtest_artifact_non_finite",
            ),
            (
                replace(
                    result,
                    summary=replace(result.summary, sharpe_ratio=float("inf")),
                ),
                "backtest_artifact_non_finite",
            ),
            (
                replace(
                    result,
                    equity_curve=(
                        replace(result.equity_curve[0], equity=0.0),
                        *result.equity_curve[1:],
                    ),
                ),
                "backtest_equity_equity_invalid",
            ),
            (
                replace(
                    result,
                    cost_diagnostics=(
                        replace(diagnostic, actual_cost_bps=5),
                    ),
                ),
                "backtest_cost_diagnostic_actual_cost_bps_invalid",
            ),
            (
                replace(
                    result,
                    cost_diagnostics=(
                        replace(diagnostic, actual_fee_bps=5),
                    ),
                ),
                "backtest_cost_diagnostic_actual_fee_bps_invalid",
            ),
        )
        for tampered, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(
                ValueError,
                reason,
            ):
                validate_backtest_result_units(
                    tampered,
                    require_complete_artifact=True,
                )

    def test_complete_artifact_rejects_nonsense_timeline_actions(self) -> None:
        result = self._one_fill_result()
        no_order = result.execution_timeline[-1]
        cases = (
            ("nonsense", "backtest_execution_action_invalid"),
            ("open", "backtest_no_order_action_invalid"),
        )
        for action, reason in cases:
            tampered = list(result.execution_timeline)
            tampered[-1] = replace(
                no_order,
                action=action,
                decision_id=(
                    f"{no_order.observation_bar_start_ts.isoformat()}_{action}"
                ),
            )
            with self.subTest(action=action), self.assertRaisesRegex(
                ValueError,
                reason,
            ):
                validate_backtest_result_units(
                    replace(result, execution_timeline=tuple(tampered)),
                    require_complete_artifact=True,
                )

    def test_complete_artifact_rejects_past_or_unbound_fill_timeline(self) -> None:
        result = self._one_fill_result()
        filled_index = next(
            index
            for index, record in enumerate(result.execution_timeline)
            if record.status in {"filled", "partial_fill"}
        )
        filled = result.execution_timeline[filled_index]
        past = filled.decision_ts - timedelta(hours=5)
        tampered_timeline = list(result.execution_timeline)
        tampered_timeline[filled_index] = replace(
            filled,
            next_tradable_event_ts=past,
            resolved_at_ts=past,
            fill_ts=past,
        )
        with self.assertRaisesRegex(
            ValueError,
            "backtest_execution_next_event_mismatch",
        ):
            validate_backtest_result_units(
                replace(result, execution_timeline=tuple(tampered_timeline)),
                require_complete_artifact=True,
            )

    def test_complete_artifact_rejects_non_decimal_participation_basis(self) -> None:
        result = self._one_fill_result()
        filled_index = next(
            index
            for index, record in enumerate(result.execution_timeline)
            if record.status in {"filled", "partial_fill"}
        )
        tampered_timeline = list(result.execution_timeline)
        tampered_timeline[filled_index] = replace(
            tampered_timeline[filled_index],
            max_volume_participation=1,
        )

        with self.assertRaisesRegex(
            ValueError,
            "backtest_execution_order_basis_incomplete",
        ):
            validate_backtest_result_units(
                replace(result, execution_timeline=tuple(tampered_timeline)),
                require_complete_artifact=True,
            )

    def test_complete_artifact_rejects_non_lot_open_order_basis(self) -> None:
        result = self._one_fill_result()
        filled_index = next(
            index
            for index, record in enumerate(result.execution_timeline)
            if record.status in {"filled", "partial_fill"}
        )
        tampered_timeline = list(result.execution_timeline)
        tampered_timeline[filled_index] = replace(
            tampered_timeline[filled_index],
            decision_intent_exchange_quantity=Decimal("1.00001"),
            requested_exchange_quantity=Decimal("1.00001"),
        )

        with self.assertRaisesRegex(
            ValueError,
            "exchange_quantity_lot_misaligned",
        ):
            validate_backtest_result_units(
                replace(result, execution_timeline=tuple(tampered_timeline)),
                require_complete_artifact=True,
            )

    def test_complete_artifact_rejects_non_tick_execution_and_mark_prices(
        self,
    ) -> None:
        result = self._one_fill_result()
        filled_index = next(
            index
            for index, record in enumerate(result.execution_timeline)
            if record.status in {"filled", "partial_fill"}
        )
        bad_timeline = list(result.execution_timeline)
        assert bad_timeline[filled_index].reference_price is not None
        bad_timeline[filled_index] = replace(
            bad_timeline[filled_index],
            reference_price=(
                bad_timeline[filled_index].reference_price + Decimal("0.01")
            ),
        )
        bad_curve = list(result.equity_curve)
        assert bad_curve[-1].mark_price is not None
        bad_curve[-1] = replace(
            bad_curve[-1],
            mark_price=bad_curve[-1].mark_price + Decimal("0.01"),
        )

        cases = (
            (
                replace(result, execution_timeline=tuple(bad_timeline)),
                "backtest_execution_reference_price_tick_misaligned",
            ),
            (
                replace(result, equity_curve=tuple(bad_curve)),
                "backtest_equity_mark_price_tick_misaligned",
            ),
        )
        for tampered, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(
                ValueError,
                reason,
            ):
                validate_backtest_result_units(
                    tampered,
                    require_complete_artifact=True,
                )

    def test_complete_artifact_recomputes_equity_and_fee_metrics(self) -> None:
        result = self._one_fill_result()
        curve = list(result.equity_curve)
        curve[-1] = replace(curve[-1], drawdown_bps=Decimal("999999"))
        bad_drawdown = replace(
            result,
            equity_curve=tuple(curve),
            summary=replace(
                result.summary,
                max_drawdown_bps=Decimal("999999"),
            ),
        )

        curve = list(result.equity_curve)
        curve[-1] = replace(curve[-1], daily_return_bps=Decimal("999999"))
        cases = (
            (
                bad_drawdown,
                "backtest_equity_drawdown_recalculation_mismatch",
            ),
            (
                replace(result, equity_curve=tuple(curve)),
                "backtest_equity_daily_return_recalculation_mismatch",
            ),
            (
                replace(
                    result,
                    summary=replace(result.summary, sharpe_ratio=999.0),
                ),
                "backtest_summary_sharpe_recalculation_mismatch",
            ),
            (
                replace(
                    result,
                    summary=replace(
                        result.summary,
                        fee_total=result.summary.fee_total + Decimal("1"),
                    ),
                ),
                "backtest_summary_fee_total_mismatch",
            ),
        )
        for tampered, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(
                ValueError,
                reason,
            ):
                validate_backtest_result_units(
                    tampered,
                    require_complete_artifact=True,
                )

    def test_non_explicit_decision_cost_has_no_legacy_fallback(self) -> None:
        bars = [_make_bar(0), _make_bar(1)]
        for decision_cost in (0.0, 4.0):
            adapter = _FakeAdapter(
                [
                    {
                        "action": "open",
                        "delta": Decimal("1"),
                        "target": Decimal("1"),
                        "net_edge_bps": 5.0,
                        "cost_bps": decision_cost,
                        "cost_bps_is_explicit": False,
                    }
                ]
            )
            with (
                self.subTest(decision_cost=decision_cost),
                patch(
                    "aats.data_platform.replay.backtest.harness.load_gold_bars",
                    return_value=bars,
                ),
                self.assertRaisesRegex(
                    ValueError,
                    "replay_decision_cost_must_be_explicit",
                ),
            ):
                run_backtest(
                    MagicMock(),
                    config=_config(),
                    start_ts=_BASE_TS,
                    end_ts=_BASE_TS + timedelta(days=1),
                    parameter_overrides={"strategy_short_bias_enabled": False},
                    adapter=adapter,
                )

    def test_explicit_zero_decision_cost_does_not_fall_back(self) -> None:
        bars = [_make_bar(0), _make_bar(1)]
        adapter = _FakeAdapter(
            [
                {
                    "action": "open",
                    "delta": Decimal("1"),
                    "target": Decimal("1"),
                    "cost_bps": 0.0,
                    "cost_bps_is_explicit": True,
                },
                {
                    "action": "hold",
                    "delta": Decimal("0"),
                    "target": Decimal("1"),
                },
            ]
        )
        with patch(
            "aats.data_platform.replay.backtest.harness.load_gold_bars",
            return_value=bars,
        ):
            result = run_backtest(
                MagicMock(),
                config=_config(),
                start_ts=_BASE_TS,
                end_ts=_BASE_TS + timedelta(days=1),
                parameter_overrides={"strategy_short_bias_enabled": False},
                adapter=adapter,
            )

        self.assertAlmostEqual(result.cost_summary.avg_cost_diff_bps, 6.0)

    def test_backtest_result_includes_config(self) -> None:
        """BacktestResult.config 必须完整等于传入的 config。"""
        session = MagicMock()
        config = _config(
            symbol="BTC-USDT",
            timeframe="15m",
            dataset_version="v9.9",
            family="independent",
            order_type="bounded_limit",
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
                parameter_overrides={"strategy_short_bias_enabled": False},
                adapter=adapter,
            )

        self.assertIs(result.config, config)
        self.assertEqual(result.config.symbol, "BTC-USDT")
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
            "strategy_short_bias_enabled": False,
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
                config=_config(),
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

    def test_safe_spot_override_preserves_other_family_defaults(self) -> None:
        """禁用未建模裸空时，其余参数仍使用 family 默认值。"""
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
                config=_config(family="independent"),
                start_ts=_BASE_TS,
                end_ts=_BASE_TS + timedelta(days=1),
                parameter_overrides={"strategy_short_bias_enabled": False},
                adapter=adapter,
            )

        expected = ReplayParameterOverrides.for_family("independent")
        self.assertEqual(
            adapter.seen_params[0].entry_threshold, expected.entry_threshold
        )

    def test_each_flat_cost_override_preserves_every_other_default(self) -> None:
        defaults = ReplayParameterOverrides.for_family("independent")
        cases = {
            "taker_fee_bps": 4.0,
            "slippage_bps": 0.75,
            "maker_fee_bps": -0.25,
            "execution_style": " MARKET ",
            "passive_bias": 0.25,
            "maker_taker_bias": -0.4,
        }
        for key, value in cases.items():
            with self.subTest(key=key):
                resolved = ReplayParameterOverrides.from_dict(
                    {key: value},
                    base=defaults,
                )
                expected_value = "market" if key == "execution_style" else value
                self.assertEqual(
                    getattr(resolved.cost_config, key),
                    expected_value,
                )
                for other in cases:
                    if other != key:
                        self.assertEqual(
                            getattr(resolved.cost_config, other),
                            getattr(defaults.cost_config, other),
                        )

    def test_directional_single_override_preserves_family_thresholds(self) -> None:
        defaults = ReplayParameterOverrides.for_family("directional")
        resolved = ReplayParameterOverrides.from_dict(
            {"strategy_short_bias_enabled": False},
            base=defaults,
        )

        self.assertEqual(resolved.entry_threshold, 0.45)
        self.assertEqual(resolved.close_threshold, 0.20)
        self.assertEqual(resolved.scale_in_threshold, 0.55)


class TestBacktestPreflight(unittest.TestCase):
    def test_fee_and_slippage_config_requires_canonical_finite_numbers(self) -> None:
        for field_name in (
            "maker_fee_bps",
            "taker_fee_bps",
            "ioc_slippage_bps",
        ):
            for invalid in ("1", True, float("nan"), float("inf")):
                with self.subTest(field_name=field_name, invalid=invalid):
                    with self.assertRaisesRegex(ValueError, field_name):
                        _config(**{field_name: invalid})

    def test_legacy_global_assumed_cost_is_rejected_before_gold_query(self) -> None:
        for legacy_value in (1.0, 999.0):
            with (
                self.subTest(legacy_value=legacy_value),
                patch(
                    "aats.data_platform.replay.backtest.harness.load_gold_bars"
                ) as load_bars,
                self.assertRaisesRegex(
                    ValueError,
                    "legacy_assumed_cost_bps_unsupported_use_param_cost_config",
                ),
            ):
                run_backtest(
                    MagicMock(),
                    config=_config(assumed_cost_bps=legacy_value),
                    start_ts=_BASE_TS,
                    end_ts=_BASE_TS + timedelta(days=1),
                    parameter_overrides={"strategy_short_bias_enabled": False},
                )
            load_bars.assert_not_called()

    def test_missing_or_mismatched_contract_fails_before_gold_query(self) -> None:
        cases = (
            (
                BacktestConfig(symbol="BTC-USDT", instrument_contract=None),
                "replay_instrument_contract_required",
            ),
            (
                BacktestConfig(
                    symbol="ETH-USDT",
                    instrument_contract=SPOT_CONTRACT,
                ),
                "replay_instrument_contract_symbol_mismatch",
            ),
        )
        for config, reason in cases:
            with self.subTest(reason=reason):
                with (
                    patch(
                        "aats.data_platform.replay.backtest.harness.load_gold_bars"
                    ) as load_bars,
                    self.assertRaisesRegex(ValueError, reason),
                ):
                    run_backtest(
                        MagicMock(),
                        config=config,
                        start_ts=_BASE_TS,
                        end_ts=_BASE_TS + timedelta(days=1),
                        parameter_overrides={
                            "strategy_short_bias_enabled": False
                        },
                    )
                load_bars.assert_not_called()

    def test_malformed_or_short_spot_decision_fails_before_order(self) -> None:
        cases = (
            (
                {
                    "action": "open",
                    "delta": Decimal("1"),
                    "target": Decimal("1"),
                    "long_score": True,
                    "short_score": 0.0,
                },
                "replay_decision_long_score_invalid",
            ),
            (
                {
                    "action": "open",
                    "delta": Decimal("1"),
                    "target": Decimal("1"),
                    "long_score": 0.1,
                    "short_score": 0.9,
                },
                "spot_replay_short_open_unavailable",
            ),
            (
                {
                    "action": "hold",
                    "delta": Decimal("1"),
                    "target": Decimal("1"),
                },
                "replay_non_order_action_position_mismatch",
            ),
            (
                {
                    "action": "blocked",
                    "delta": Decimal("0"),
                    "target": Decimal("1"),
                },
                "replay_non_order_action_position_mismatch",
            ),
        )
        for entry, reason in cases:
            with self.subTest(reason=reason), patch(
                "aats.data_platform.replay.backtest.harness.load_gold_bars",
                return_value=[_make_bar(0)],
            ), self.assertRaisesRegex(ValueError, reason):
                run_backtest(
                    MagicMock(),
                    config=_config(),
                    start_ts=_BASE_TS,
                    end_ts=_BASE_TS + timedelta(days=1),
                    parameter_overrides={
                        "strategy_short_bias_enabled": False,
                    },
                    adapter=_FakeAdapter([entry]),
                )

    def test_inflated_reported_edge_fails_closed_before_order(self) -> None:
        with (
            patch(
                "aats.data_platform.replay.backtest.harness.load_gold_bars",
                return_value=[_make_bar(0)],
            ),
            self.assertRaisesRegex(
                ValueError,
                "replay_decision_edge_identity_mismatch",
            ),
        ):
            run_backtest(
                MagicMock(),
                config=_config(),
                start_ts=_BASE_TS,
                end_ts=_BASE_TS + timedelta(days=1),
                parameter_overrides={"strategy_short_bias_enabled": False},
                adapter=_FakeAdapter(
                    [
                        {
                            "action": "open",
                            "delta": Decimal("1"),
                            "target": Decimal("1"),
                            "net_edge_bps": 1_000_000.0,
                            "signal_edge_bps": 0.0,
                            "cost_bps": 0.0,
                        }
                    ]
                ),
            )

    def test_claimed_flat_close_must_close_the_entire_position(self) -> None:
        adapter = _FakeAdapter(
            [
                {"action": "open", "delta": Decimal("1"), "target": Decimal("1")},
                {
                    "action": "close",
                    "delta": Decimal("-0.5"),
                    "target": Decimal("0"),
                },
            ]
        )
        with (
            patch(
                "aats.data_platform.replay.backtest.harness.load_gold_bars",
                return_value=[_make_bar(0), _make_bar(1)],
            ),
            self.assertRaisesRegex(
                ValueError,
                "replay_decision_position_identity_mismatch",
            ),
        ):
            run_backtest(
                MagicMock(),
                config=_config(
                    maker_fee_bps=0.0,
                    taker_fee_bps=0.0,
                    ioc_slippage_bps=0.0,
                ),
                start_ts=_BASE_TS,
                end_ts=_BASE_TS + timedelta(days=1),
                parameter_overrides={"strategy_short_bias_enabled": False},
                adapter=adapter,
            )

    def test_ineligible_open_cannot_enter_the_order_queue(self) -> None:
        for gate_overrides in (
            {"selectable": False},
            {"execution_compatible": False},
            {"score_stable": False},
            {"blocking_reasons": ["net_edge_below_safe_minimum"]},
        ):
            with (
                self.subTest(gate_overrides=gate_overrides),
                patch(
                    "aats.data_platform.replay.backtest.harness.load_gold_bars",
                    return_value=[_make_bar(0)],
                ),
                self.assertRaisesRegex(
                    ValueError,
                    "replay_open_not_execution_eligible",
                ),
            ):
                run_backtest(
                    MagicMock(),
                    config=_config(),
                    start_ts=_BASE_TS,
                    end_ts=_BASE_TS + timedelta(days=1),
                    parameter_overrides={"strategy_short_bias_enabled": False},
                    adapter=_FakeAdapter(
                        [
                            {
                                "action": "open",
                                "delta": Decimal("1"),
                                "target": Decimal("1"),
                                **gate_overrides,
                            }
                        ]
                    ),
                )

    def test_dataset_and_utc_window_fail_before_gold_query(self) -> None:
        cases = (
            (
                _config(dataset_version="  "),
                _BASE_TS,
                _BASE_TS + timedelta(days=1),
                "replay_dataset_version_required",
            ),
            (
                _config(),
                _BASE_TS.replace(tzinfo=None),
                (_BASE_TS + timedelta(days=1)).replace(tzinfo=None),
                "replay_start_must_be_utc",
            ),
            (
                _config(),
                _BASE_TS.astimezone(timezone(timedelta(hours=8))),
                (_BASE_TS + timedelta(days=1)).astimezone(
                    timezone(timedelta(hours=8))
                ),
                "replay_start_must_be_utc",
            ),
        )
        for config, start_ts, end_ts, reason in cases:
            with (
                self.subTest(reason=reason),
                patch(
                    "aats.data_platform.replay.backtest.harness.load_gold_bars"
                ) as load_bars,
                self.assertRaisesRegex(ValueError, reason),
            ):
                run_backtest(
                    MagicMock(),
                    config=config,
                    start_ts=start_ts,
                    end_ts=end_ts,
                    parameter_overrides={
                        "strategy_short_bias_enabled": False,
                    },
                )
            load_bars.assert_not_called()

    def test_invalid_gold_bar_contract_fails_before_adapter_observation(self) -> None:
        bad_bars = (
            replace(_make_bar(0), symbol="ETH-USDT"),
            replace(_make_bar(0), aligned_funding_rate=Decimal("NaN")),
            replace(_make_bar(0), low=Decimal("60000")),
            replace(_make_bar(0), volume=Decimal("-1")),
            replace(_make_bar(0), ts=_BASE_TS - timedelta(hours=1)),
            replace(_make_bar(0), ts=_BASE_TS.replace(tzinfo=None)),
            replace(
                _make_bar(0),
                ts=_BASE_TS.astimezone(timezone(timedelta(hours=8))),
            ),
        )
        reasons = (
            "replay_bar_symbol_mismatch",
            "replay_bar_funding_rate_invalid",
            "replay_bar_ohlc_inconsistent",
            "replay_bar_volume_invalid",
            "replay_bar_outside_requested_window",
            "replay_bar_timestamp_must_be_utc",
            "replay_bar_timestamp_must_be_utc",
        )
        for bar, reason in zip(bad_bars, reasons, strict=True):
            adapter = _RecordingAdapter([])
            with self.subTest(reason=reason), patch(
                "aats.data_platform.replay.backtest.harness.load_gold_bars",
                return_value=[bar],
            ), self.assertRaisesRegex(ValueError, reason):
                run_backtest(
                    MagicMock(),
                    config=_config(),
                    start_ts=_BASE_TS,
                    end_ts=_BASE_TS + timedelta(days=1),
                    parameter_overrides={
                        "strategy_short_bias_enabled": False,
                    },
                    adapter=adapter,
                )
            self.assertEqual(adapter.seen_params, [])

    def test_spot_gold_bar_cannot_inject_funding_or_future_lineage(self) -> None:
        bad_bars = (
            replace(
                _make_bar(0),
                aligned_funding_rate=Decimal("0.01"),
                funding_source_ts=_BASE_TS + timedelta(days=10),
            ),
            replace(
                _make_bar(0),
                funding_source_ts=_BASE_TS,
            ),
        )
        for bar in bad_bars:
            adapter = _RecordingAdapter([])
            with (
                self.subTest(bar=bar),
                patch(
                    "aats.data_platform.replay.backtest.harness.load_gold_bars",
                    return_value=[bar],
                ),
                self.assertRaisesRegex(
                    ValueError,
                    "spot_replay_funding_must_be_absent",
                ),
            ):
                run_backtest(
                    MagicMock(),
                    config=_config(),
                    start_ts=_BASE_TS,
                    end_ts=_BASE_TS + timedelta(days=1),
                    parameter_overrides={"strategy_short_bias_enabled": False},
                    adapter=adapter,
                )
            self.assertEqual(adapter.seen_params, [])

    def test_gold_bar_cadence_gap_is_explicit_in_result(self) -> None:
        adapter = _RecordingAdapter([])
        with patch(
            "aats.data_platform.replay.backtest.harness.load_gold_bars",
            return_value=[_make_bar(0), _make_bar(2)],
        ):
            result = run_backtest(
                MagicMock(),
                config=_config(),
                start_ts=_BASE_TS,
                end_ts=_BASE_TS + timedelta(days=1),
                parameter_overrides={"strategy_short_bias_enabled": False},
                adapter=adapter,
            )
        self.assertEqual(result.cadence_gap_count, 1)
        self.assertEqual(len(adapter.seen_params), 2)


    def test_unknown_or_unconsumed_override_fails_before_gold_query(self) -> None:
        for key in (
            "typo_threshold",
            "directional_trend_weight",
            "scale_in_threshold",
        ):
            with self.subTest(key=key):
                with (
                    patch(
                        "aats.data_platform.replay.backtest.harness.load_gold_bars"
                    ) as load_bars,
                    self.assertRaisesRegex(
                        ValueError,
                        "unconsumed_replay_parameter_keys",
                    ),
                ):
                    run_backtest(
                        MagicMock(),
                        config=_config(),
                        start_ts=_BASE_TS,
                        end_ts=_BASE_TS + timedelta(days=1),
                        parameter_overrides={
                            "strategy_short_bias_enabled": False,
                            key: 1,
                        },
                    )
                load_bars.assert_not_called()

    def test_invalid_override_types_and_nulls_fail_before_gold_query(self) -> None:
        cases = (
            ({"entry_threshold": True}, "entry_threshold must be numeric"),
            ({"min_confirm_ticks": 2.9}, "min_confirm_ticks must be an integer"),
            ({"taker_fee_bps": True}, "taker_fee_bps must be numeric"),
            (
                {"cost_config": {"slippage_bps": True}},
                "slippage_bps must be numeric",
            ),
            ({"entry_threshold": None}, "entry_threshold must not be null"),
            ({"cost_config": None}, "replay_cost_config_must_be_string_mapping"),
        )
        for bad_override, reason in cases:
            with self.subTest(bad_override=bad_override):
                with (
                    patch(
                        "aats.data_platform.replay.backtest.harness.load_gold_bars"
                    ) as load_bars,
                    self.assertRaisesRegex(ValueError, reason),
                ):
                    run_backtest(
                        MagicMock(),
                        config=_config(),
                        start_ts=_BASE_TS,
                        end_ts=_BASE_TS + timedelta(days=1),
                        parameter_overrides={
                            "strategy_short_bias_enabled": False,
                            **bad_override,
                        },
                    )
                load_bars.assert_not_called()

    def test_spot_short_only_overrides_fail_before_gold_query(self) -> None:
        for key in ("short_entry_threshold", "short_close_threshold"):
            with self.subTest(key=key):
                with (
                    patch(
                        "aats.data_platform.replay.backtest.harness.load_gold_bars"
                    ) as load_bars,
                    self.assertRaisesRegex(
                        ValueError,
                        "inactive_spot_short_parameter_keys",
                    ),
                ):
                    run_backtest(
                        MagicMock(),
                        config=_config(),
                        start_ts=_BASE_TS,
                        end_ts=_BASE_TS + timedelta(days=1),
                        parameter_overrides={
                            "strategy_short_bias_enabled": False,
                            key: 0.2,
                        },
                    )
                load_bars.assert_not_called()

    def test_noncanonical_symbol_fails_before_gold_query(self) -> None:
        for symbol in ("btc-usdt", " BTC-USDT "):
            with self.subTest(symbol=symbol):
                with (
                    patch(
                        "aats.data_platform.replay.backtest.harness.load_gold_bars"
                    ) as load_bars,
                    self.assertRaisesRegex(
                        ValueError,
                        "replay_symbol_must_be_canonical",
                    ),
                ):
                    run_backtest(
                        MagicMock(),
                        config=BacktestConfig(
                            symbol=symbol,
                            instrument_contract=SPOT_CONTRACT,
                        ),
                        start_ts=_BASE_TS,
                        end_ts=_BASE_TS + timedelta(days=1),
                        parameter_overrides={
                            "strategy_short_bias_enabled": False,
                        },
                    )
                load_bars.assert_not_called()

    def test_invalid_replay_cost_contract_fails_before_gold_query(self) -> None:
        cases = (
            ({"taker_fee_bps": -1}, "replay_taker_fee_bps_out_of_range"),
            ({"slippage_bps": -1}, "replay_slippage_bps_out_of_range"),
            ({"maker_fee_bps": -10000}, "replay_maker_fee_bps_out_of_range"),
            ({"passive_bias": 2}, "replay_passive_bias_out_of_range"),
            ({"maker_taker_bias": -2}, "replay_maker_taker_bias_out_of_range"),
            ({"execution_style": "unknown"}, "replay_execution_style_unsupported"),
        )
        for cost_override, reason in cases:
            overrides = {
                "strategy_short_bias_enabled": False,
                **cost_override,
            }
            with self.subTest(reason=reason):
                with (
                    patch(
                        "aats.data_platform.replay.backtest.harness.load_gold_bars"
                    ) as load_bars,
                    self.assertRaisesRegex(ValueError, reason),
                ):
                    run_backtest(
                        MagicMock(),
                        config=_config(),
                        start_ts=_BASE_TS,
                        end_ts=_BASE_TS + timedelta(days=1),
                        parameter_overrides=overrides,
                    )
                load_bars.assert_not_called()

    def test_legacy_custom_adapter_is_instantiable_but_evidence_fails_closed(
        self,
    ) -> None:
        class LegacyAdapter(BaseReplayAdapter):
            @property
            def family_name(self) -> str:
                return "independent"

            def reset_state(self) -> ReplayState:
                return ReplayState()

            def evaluate_bar(self, ctx: ReplayBarContext) -> ReplayDecision:
                return _make_decision(
                    ts=ctx.bar.ts,
                    action="hold",
                    delta=Decimal("0"),
                    target=Decimal("0"),
                )

        adapter = LegacyAdapter()
        self.assertEqual(adapter.algorithm_version, "")
        self.assertEqual(adapter.accepted_parameter_keys, frozenset())
        with (
            patch(
                "aats.data_platform.replay.backtest.harness.load_gold_bars"
            ) as load_bars,
            self.assertRaisesRegex(
                ValueError,
                "replay_adapter_algorithm_version_required",
            ),
        ):
            run_backtest(
                MagicMock(),
                config=_config(),
                start_ts=_BASE_TS,
                end_ts=_BASE_TS + timedelta(days=1),
                adapter=adapter,
            )
        load_bars.assert_not_called()

    def test_derivative_lineage_guard_precedes_gold_query(self) -> None:
        adapter = _FakeAdapter([])
        config = BacktestConfig(
            symbol=LINEAR_SWAP_CONTRACT.symbol,
            instrument_contract=LINEAR_SWAP_CONTRACT,
        )
        with (
            patch(
                "aats.data_platform.replay.backtest.harness.load_gold_bars"
            ) as load_bars,
            self.assertRaisesRegex(
                ValueError,
                "legacy_derivative_replay_contract_lineage_required",
            ),
        ):
            run_backtest(
                MagicMock(),
                config=config,
                start_ts=_BASE_TS,
                end_ts=_BASE_TS + timedelta(days=1),
                parameter_overrides={"strategy_short_bias_enabled": False},
                adapter=adapter,
            )
        load_bars.assert_not_called()

    def test_margin_and_spot_short_bias_fail_before_gold_query(self) -> None:
        margin_contract = replace(SPOT_CONTRACT, instrument_type="MARGIN")
        cases = (
            (
                BacktestConfig(
                    symbol=margin_contract.symbol,
                    instrument_contract=margin_contract,
                ),
                {"strategy_short_bias_enabled": False},
                "legacy_margin_replay_borrow_model_required",
            ),
            (
                _config(),
                None,
                "spot_replay_short_bias_must_be_disabled",
            ),
        )
        for config, overrides, reason in cases:
            with self.subTest(reason=reason):
                with (
                    patch(
                        "aats.data_platform.replay.backtest.harness.load_gold_bars"
                    ) as load_bars,
                    self.assertRaisesRegex(ValueError, reason),
                ):
                    run_backtest(
                        MagicMock(),
                        config=config,
                        start_ts=_BASE_TS,
                        end_ts=_BASE_TS + timedelta(days=1),
                        parameter_overrides=overrides,
                        adapter=_FakeAdapter([]),
                    )
                load_bars.assert_not_called()


class TestScorecardFillAttributionIntegration(unittest.TestCase):
    def test_real_harness_fills_are_partitioned_once_at_equity_attribution(
        self,
    ) -> None:
        from aats.data_platform.replay.backtest.evidence_scorecard import (
            build_scorecard,
        )

        bars = [
            _make_bar(
                index,
                close=Decimal(str(50_000 + index * 100)),
                open_price=Decimal(str(50_000 + index * 100)),
            )
            for index in range(5)
        ]
        adapter = _FakeAdapter(
            [
                {
                    "action": "open",
                    "delta": Decimal("1"),
                    "target": Decimal("1"),
                    "long_score": 0.9,
                    "short_score": 0.1,
                    "net_edge_bps": 5.0,
                    "cost_bps": 4.0,
                },
                {
                    "action": "hold",
                    "delta": Decimal("0"),
                    "target": Decimal("1"),
                },
                {
                    "action": "close",
                    "delta": Decimal("-1"),
                    "target": Decimal("0"),
                    "long_score": 0.1,
                    "short_score": 0.1,
                    "net_edge_bps": 20.0,
                    "cost_bps": 10.0,
                },
                {
                    "action": "hold",
                    "delta": Decimal("0"),
                    "target": Decimal("0"),
                },
                {
                    "action": "hold",
                    "delta": Decimal("0"),
                    "target": Decimal("0"),
                },
            ]
        )
        with patch(
            "aats.data_platform.replay.backtest.harness.load_gold_bars",
            return_value=bars,
        ):
            result = run_backtest(
                MagicMock(),
                config=_config(max_volume_participation=Decimal("1")),
                start_ts=_BASE_TS,
                end_ts=_BASE_TS + timedelta(days=1),
                parameter_overrides={"strategy_short_bias_enabled": False},
                adapter=adapter,
            )

        self.assertEqual(result.fills_count, 2)
        first, second = result.cost_diagnostics
        self.assertEqual(
            first.fill_ts_ms,
            int((_BASE_TS + timedelta(hours=1)).timestamp() * 1000),
        )
        self.assertEqual(
            first.equity_attribution_ts_ms,
            int((_BASE_TS + timedelta(hours=2)).timestamp() * 1000),
        )
        self.assertEqual(
            second.fill_ts_ms,
            int((_BASE_TS + timedelta(hours=3)).timestamp() * 1000),
        )
        self.assertEqual(
            second.equity_attribution_ts_ms,
            int((_BASE_TS + timedelta(hours=4)).timestamp() * 1000),
        )

        scorecard = build_scorecard(
            result,
            split_ts=_BASE_TS + timedelta(hours=2, minutes=30),
        )
        self.assertEqual(scorecard["oos"]["train"]["fills"], 1)
        self.assertEqual(scorecard["oos"]["test"]["fills"], 1)
        self.assertEqual(
            sum(window["fills"] for window in scorecard["cross_window"]),
            scorecard["oos"]["test"]["fills"],
        )
        self.assertEqual(
            sum(
                bucket["fills"]
                for bucket in scorecard["regime_slice"]["vol"].values()
            ),
            2,
        )
        self.assertAlmostEqual(
            scorecard["cost_adjusted"]["train"]["net_edge_bps"],
            first.actual_net_edge_bps,
        )
        self.assertAlmostEqual(
            scorecard["cost_adjusted"]["test"]["net_edge_bps"],
            second.actual_net_edge_bps,
        )


class TestPartialFillPositionContinuity(unittest.TestCase):
    @staticmethod
    def _overrides() -> dict[str, Any]:
        return {
            "strategy_short_bias_enabled": False,
            "min_confirm_ticks": 1,
            "entry_threshold": 0.10,
            "close_threshold": 0.05,
            "min_hold_seconds": 0.0,
            "min_safe_net_edge_bps": 0.0,
            "de_risk_net_edge_bps": -1.0,
            "failed_thesis_net_edge_bps": -2.0,
            "expected_slippage_buffer_bps": 0.0,
            "expected_execution_buffer_bps": 0.0,
            "noise_buffer_bps": 0.0,
            "taker_fee_bps": 0.0,
            "slippage_bps": 0.0,
            "maker_fee_bps": 0.0,
        }

    @staticmethod
    def _run(volumes: list[str], participation: str):
        bars = [
            _make_bar(index, volume=Decimal(volume))
            for index, volume in enumerate(volumes)
        ]
        requested_quantities: list[Decimal] = []
        original = FillSimulator.simulate

        def _spy(simulator, request, *args, **kwargs):
            requested_quantities.append(request.target_qty)
            return original(simulator, request, *args, **kwargs)

        with (
            patch(
                "aats.data_platform.replay.backtest.harness.load_gold_bars",
                return_value=bars,
            ),
            patch.object(FillSimulator, "simulate", new=_spy),
        ):
            result = run_backtest(
                MagicMock(),
                config=_config(
                    max_volume_participation=Decimal(participation),
                    maker_fee_bps=0.0,
                    taker_fee_bps=0.0,
                    ioc_slippage_bps=0.0,
                ),
                start_ts=_BASE_TS,
                end_ts=_BASE_TS + timedelta(days=1),
                parameter_overrides=TestPartialFillPositionContinuity._overrides(),
                adapter=_OpenThenCloseIndependentAdapter(),
            )
        return result, requested_quantities

    def test_partial_open_close_requests_only_actual_position(self) -> None:
        result, requests = self._run(["0.4", "0.4", "0.4"], "0.5")

        self.assertEqual(requests, [Decimal("1"), Decimal("0.2")])
        self.assertEqual(result.fills_count, 2)
        statuses = [
            event.status
            for event in result.execution_timeline
            if event.status != "no_order"
        ]
        self.assertEqual(statuses, ["partial_fill", "filled"])

    def test_partial_close_retries_only_remaining_position(self) -> None:
        result, requests = self._run(["10", "2", "8", "8"], "0.1")

        self.assertEqual(
            requests,
            [Decimal("1"), Decimal("1"), Decimal("0.8")],
        )
        self.assertEqual(result.fills_count, 3)
        statuses = [
            event.status
            for event in result.execution_timeline
            if event.status != "no_order"
        ]
        self.assertEqual(statuses, ["filled", "partial_fill", "filled"])


if __name__ == "__main__":
    unittest.main()
