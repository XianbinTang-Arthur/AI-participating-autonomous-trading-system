"""Unit tests for ``aats.data_platform.replay.backtest.evidence_scorecard``.

全部基于确定性合成 fixture 构造 ``BacktestResult``, 不依赖 DB / adapter。

覆盖面:
    * 顶层 key 齐全, 且无 verdict/go/no-go/pass/fail 文案
    * OOS 时间中点切分, train/test 两段 + UTC 边界
    * cross_window 只细分 OOS test, 至少 3 片且覆盖闭合
    * cost_adjusted 5 个字段从 diagnostics 正确聚合
    * regime_slice.vol 低/高波 bucket, ir + fills
    * 空 curve / 空 diagnostics 不抛异常
    * 恒定 equity 下 IR == 0
"""

from __future__ import annotations

import json
import re
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from aats.data_platform.replay.backtest.cost_validator import (
    CostDiagnostic,
    CostValidator,
)
from aats.data_platform.replay.backtest.equity_builder import (
    BacktestSummary,
    EquityPoint,
    recompute_equity_curve_metrics,
)
from aats.data_platform.replay.backtest.fill_simulator import (
    FillRequest,
    FillSimulator,
)
from aats.data_platform.replay.backtest.evidence_scorecard import (
    SCORECARD_FILL_MODEL_VERSION,
    SCORECARD_RESOLVED_PARAMETER_KEYS,
    build_scorecard,
)
from aats.data_platform.replay.backtest.harness import (
    BacktestConfig,
    BacktestResult,
    ExecutionTimingRecord,
)
from aats.data_platform.replay.backtest.position_tracker import (
    Fill,
    PositionTracker,
)
from aats.data_platform.replay.core.replay_context import ReplayParameterOverrides
from tests.unit.replay_contract_fixtures import LINEAR_SWAP_CONTRACT, SPOT_CONTRACT


# ---------------------------------------------------------------------------
# Synthetic fixture builders
# ---------------------------------------------------------------------------


_BASE_TS = datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc)
_BASE_MS = int(_BASE_TS.timestamp() * 1000)
_HOUR_MS = 60 * 60 * 1000
_TOP_LEVEL_KEYS = {
    "artifact_kind",
    "artifact_schema_version",
    "meta",
    "oos",
    "cross_window",
    "cost_adjusted",
    "regime_slice",
}
# Scorecard fixtures deliberately preserve arbitrary synthetic PnL paths.
# Give that synthetic venue an explicit fine tick instead of emitting marks
# that contradict its declared InstrumentContract.
_SCORECARD_SPOT_CONTRACT = replace(
    SPOT_CONTRACT,
    tick_size=Decimal("1E-27"),
)


def test_scorecard_v2_freezes_fill_and_parameter_schema() -> None:
    assert SCORECARD_FILL_MODEL_VERSION == "ohlcv_participation_cap_contract_v3"
    assert SCORECARD_RESOLVED_PARAMETER_KEYS == frozenset(
        ReplayParameterOverrides.__dataclass_fields__
    )


def _mk_point(index: int, equity: str) -> EquityPoint:
    return EquityPoint(
        ts_ms=_BASE_MS + index * _HOUR_MS,
        equity=Decimal(equity),
        cumulative_pnl=Decimal(equity),
        drawdown_bps=Decimal("0"),
        daily_return_bps=Decimal("0"),
        settlement_currency=_SCORECARD_SPOT_CONTRACT.settle_currency,
        instrument_symbol=_SCORECARD_SPOT_CONTRACT.symbol,
        instrument_contract_fingerprint=_SCORECARD_SPOT_CONTRACT.fingerprint,
    )


def _mk_diagnostic(
    index: int,
    *,
    assumed_cost: float = 6.0,
    actual_cost: float = 5.0,
    assumed_net: float = 10.0,
) -> CostDiagnostic:
    ts = _BASE_TS + timedelta(hours=index)
    diff = actual_cost - assumed_cost
    actual_net = assumed_net - diff
    return CostDiagnostic(
        decision_id=ts.isoformat(),
        assumed_cost_bps=assumed_cost,
        actual_cost_bps=actual_cost,
        cost_diff_bps=diff,
        assumed_net_edge_bps=assumed_net,
        actual_net_edge_bps=actual_net,
        edge_flipped_negative=(assumed_net > 0 and actual_net <= 0),
    )


def _mk_result(
    *,
    curve: tuple[EquityPoint, ...],
    diagnostics: tuple[CostDiagnostic, ...],
    config: BacktestConfig | None = None,
    window_hours: int = 24,
) -> BacktestResult:
    cfg = config or BacktestConfig(
        ioc_slippage_bps=0.0,
        spot_buy_fee_asset="quote",
    )
    if cfg.spot_buy_fee_asset is None:
        cfg = replace(cfg, spot_buy_fee_asset="quote")
    if cfg.instrument_contract is None:
        cfg = replace(
            cfg,
            symbol=_SCORECARD_SPOT_CONTRACT.symbol,
            instrument_contract=_SCORECARD_SPOT_CONTRACT,
        )
    if not diagnostics and any(point.equity != 0 for point in curve):
        expected_fee = (
            cfg.maker_fee_bps
            if cfg.order_type == "post_only"
            else cfg.taker_fee_bps
        )
        expected_slippage = (
            0.0 if cfg.order_type == "post_only" else cfg.ioc_slippage_bps
        )
        expected_cost = expected_fee + expected_slippage
        diagnostics = (
            _mk_diagnostic(
                0,
                assumed_cost=expected_cost,
                actual_cost=expected_cost,
                assumed_net=0.0,
            ),
        )
    if diagnostics and len(diagnostics) >= len(curve):
        raise ValueError("complete fixture needs one later bar per fill")

    expected_metrics, expected_sharpe = recompute_equity_curve_metrics(
        curve,
        instrument_contract=cfg.instrument_contract,
    )
    curve = tuple(
        replace(
            point,
            drawdown_bps=expected_drawdown,
            daily_return_bps=expected_daily,
        )
        for point, (expected_drawdown, expected_daily) in zip(
            curve,
            expected_metrics,
            strict=True,
        )
    )

    point_times = tuple(
        _BASE_TS + timedelta(milliseconds=point.ts_ms - _BASE_MS)
        for point in curve
    )
    start_ts = point_times[0] - timedelta(hours=1) if point_times else _BASE_TS
    end_ts = max(
        _BASE_TS + timedelta(hours=window_hours),
        point_times[-1] if point_times else _BASE_TS + timedelta(hours=1),
    )

    validator = CostValidator()
    resolved_diagnostics: list[CostDiagnostic] = []
    timeline: list[ExecutionTimingRecord] = []
    for index, point_ts in enumerate(point_times):
        observation_ts = point_ts - timedelta(hours=1)
        if index < len(diagnostics):
            source = diagnostics[index]
            next_event_ts = point_times[index + 1] - timedelta(hours=1)
            resolution_ts = (
                next_event_ts + timedelta(hours=1)
                if cfg.order_type == "post_only"
                else next_event_ts
            )
            price_source = (
                "next_bar_close"
                if cfg.order_type == "post_only"
                else "next_bar_open"
            )
            liquidity_source = (
                "next_bar_volume"
                if cfg.order_type == "post_only"
                else "observation_bar_volume"
            )
            decision_id = f"{observation_ts.isoformat()}_open"
            reference_price = Decimal("10000")
            liquidity_reference_quantity = Decimal("1000")
            simulated = FillSimulator(
                instrument_contract=cfg.instrument_contract,
                maker_fee_bps=cfg.maker_fee_bps,
                taker_fee_bps=cfg.taker_fee_bps,
                ioc_slippage_bps=cfg.ioc_slippage_bps,
                max_volume_participation=cfg.max_volume_participation,
                spot_buy_fee_asset=cfg.spot_buy_fee_asset,
            ).simulate(
                FillRequest(
                    order_id=decision_id,
                    side="buy",
                    order_type=cfg.order_type,
                    target_qty=Decimal("1"),
                    submitted_at_ts=int(point_ts.timestamp() * 1000),
                ),
                reference_price,
                liquidity_reference_quantity,
            )
            if simulated.filled_qty == 0:
                raise ValueError("complete fixture expected deterministic fill")
            resolved_diagnostics.append(
                validator.record(
                    decision_id=decision_id,
                    assumed_cost_bps=source.assumed_cost_bps,
                    actual_cost_bps=source.actual_cost_bps,
                    assumed_net_edge_bps=source.assumed_net_edge_bps,
                    notes=source.notes,
                    actual_fee_bps=simulated.fee_bps,
                    actual_slippage_bps=simulated.slippage_bps,
                    resolved_at_ts_ms=int(resolution_ts.timestamp() * 1000),
                    fill_ts_ms=int(resolution_ts.timestamp() * 1000),
                    equity_attribution_ts_ms=curve[index + 1].ts_ms,
                    filled_exchange_quantity=simulated.filled_qty,
                    average_fill_price=simulated.avg_fill_price,
                    actual_fee_notional=simulated.fee_notional,
                    fee_currency=simulated.fee_currency,
                    fee_asset=simulated.fee_asset,
                    fee_asset_quantity=simulated.fee_asset_quantity,
                )
            )
            timeline.append(
                ExecutionTimingRecord(
                    decision_id=decision_id,
                    action="open",
                    observation_bar_start_ts=observation_ts,
                    observation_completed_at_ts=point_ts,
                    decision_ts=point_ts,
                    submitted_at_ts=point_ts,
                    next_tradable_event_ts=next_event_ts,
                    resolved_at_ts=resolution_ts,
                    fill_ts=resolution_ts,
                    status="filled",
                    price_source=price_source,
                    liquidity_source=liquidity_source,
                    fill_side="buy",
                    decision_intent_exchange_quantity=Decimal("1"),
                    requested_exchange_quantity=Decimal("1"),
                    liquidity_reference_quantity=liquidity_reference_quantity,
                    max_volume_participation=cfg.max_volume_participation,
                    reference_price=reference_price,
                )
            )
        else:
            timeline.append(
                ExecutionTimingRecord(
                    decision_id=f"{observation_ts.isoformat()}_hold",
                    action="hold",
                    observation_bar_start_ts=observation_ts,
                    observation_completed_at_ts=point_ts,
                    decision_ts=point_ts,
                    submitted_at_ts=None,
                    next_tradable_event_ts=None,
                    resolved_at_ts=point_ts,
                    fill_ts=None,
                    status="no_order",
                    price_source=None,
                )
            )
    diagnostics_by_equity_ts = {
        diagnostic.equity_attribution_ts_ms: diagnostic
        for diagnostic in resolved_diagnostics
    }
    timeline_by_decision = {
        record.decision_id: index for index, record in enumerate(timeline)
    }
    tracker = PositionTracker(cfg.instrument_contract)
    curve_with_fee_ledger: list[EquityPoint] = []
    for point in curve:
        attributed = diagnostics_by_equity_ts.get(point.ts_ms)
        if attributed is not None:
            assert attributed.filled_exchange_quantity is not None
            assert attributed.average_fill_price is not None
            assert attributed.actual_fee_notional is not None
            assert attributed.fee_currency is not None
            assert attributed.fill_ts_ms is not None
            snapshot = tracker.apply_fill(
                Fill(
                    side="buy",
                    filled_qty=attributed.filled_exchange_quantity,
                    avg_fill_price=attributed.average_fill_price,
                    fee_notional=attributed.actual_fee_notional,
                    fee_currency=attributed.fee_currency,
                    instrument_symbol=cfg.instrument_contract.symbol,
                    instrument_contract_fingerprint=(
                        cfg.instrument_contract.fingerprint
                    ),
                    ts_ms=attributed.fill_ts_ms,
                    fee_asset=attributed.fee_asset,
                    fee_asset_quantity=attributed.fee_asset_quantity,
                )
            )
            timing_index = timeline_by_decision[attributed.decision_id]
            timeline[timing_index] = replace(
                timeline[timing_index],
                post_fill_position_quantity=snapshot.net_qty.copy_abs(),
            )
        current = tracker.snapshot
        if current.net_qty == 0:
            if point.equity != 0:
                raise ValueError("flat complete fixture equity must be zero")
            mark_price = Decimal("10000")
        else:
            mark_price = cfg.instrument_contract.add_settlement_amounts(
                current.avg_entry_price,
                cfg.instrument_contract.add_settlement_amounts(
                    point.equity,
                    current.accumulated_fees,
                    current.realized_pnl.copy_negate(),
                )
                / current.net_qty,
            )
        current = tracker.mark_to_market(mark_price, point.ts_ms)
        curve_with_fee_ledger.append(
            replace(
                point,
                realized_pnl=current.realized_pnl,
                unrealized_pnl=current.unrealized_pnl,
                net_qty=current.net_qty,
                avg_entry_price=current.avg_entry_price,
                mark_price=current.last_mark_price,
                fill_count=current.fill_count,
                accumulated_fees=current.accumulated_fees,
            )
        )
    curve = tuple(curve_with_fee_ledger)
    summary = BacktestSummary(
        bar_count=len(curve),
        fill_count=len(resolved_diagnostics),
        start_ts_ms=curve[0].ts_ms if curve else 0,
        end_ts_ms=curve[-1].ts_ms if curve else 0,
        final_equity=curve[-1].equity if curve else Decimal("0"),
        cumulative_pnl=curve[-1].equity if curve else Decimal("0"),
        max_drawdown_bps=(
            max(point.drawdown_bps for point in curve)
            if curve
            else Decimal("0")
        ),
        sharpe_ratio=expected_sharpe,
        fee_total=cfg.instrument_contract.add_settlement_amounts(
            *(
                diagnostic.actual_fee_notional
                for diagnostic in resolved_diagnostics
                if diagnostic.actual_fee_notional is not None
            )
        ),
        settlement_currency=cfg.instrument_contract.settle_currency,
        instrument_symbol=cfg.instrument_contract.symbol,
        instrument_contract_fingerprint=cfg.instrument_contract.fingerprint,
    )
    return BacktestResult(
        config=cfg,
        resolved_parameters=replace(
            ReplayParameterOverrides.for_family(cfg.family),
            strategy_short_bias_enabled=False,
        ),
        adapter_identity=(
            "aats.data_platform.replay.adapters.independent_adapter."
            "IndependentReplayAdapter"
        ),
        adapter_algorithm_version="independent-replay/v2",
        summary=summary,
        cost_summary=validator.summary(),
        equity_curve=curve,
        decisions_count=len(curve),
        fills_count=len(resolved_diagnostics),
        start_ts=start_ts,
        end_ts=end_ts,
        cost_diagnostics=tuple(resolved_diagnostics),
        execution_timeline=tuple(timeline),
    )


# ---------------------------------------------------------------------------
# 1. Top-level shape + verdict-free guarantee
# ---------------------------------------------------------------------------


class TestScorecardShape(unittest.TestCase):
    def _build_standard(self) -> dict:
        curve = tuple(
            _mk_point(i, eq)
            for i, eq in enumerate(["0", "10", "20", "15", "25", "35", "30", "40"])
        )
        diagnostics = (
            _mk_diagnostic(0),
            _mk_diagnostic(2),
            _mk_diagnostic(5),
            _mk_diagnostic(7),
        )
        result = _mk_result(curve=curve, diagnostics=diagnostics)
        return build_scorecard(
            result,
            generated_at=datetime(2026, 4, 23, tzinfo=timezone.utc),
        )

    def test_top_level_keys_present(self) -> None:
        sc = self._build_standard()
        self.assertEqual(
            set(sc.keys()),
            _TOP_LEVEL_KEYS,
        )
        self.assertEqual(sc["artifact_kind"], "backtest_evidence_scorecard")
        self.assertEqual(
            sc["artifact_schema_version"],
            "backtest-evidence-scorecard/v2",
        )

    def test_meta_includes_symbol_and_window(self) -> None:
        sc = self._build_standard()
        meta = sc["meta"]
        self.assertEqual(meta["symbol"], "BTC-USDT")
        self.assertEqual(meta["timeframe"], "1h")
        self.assertEqual(meta["execution_model_version"], "next_bar_event_v2")
        self.assertEqual(
            meta["fill_model_version"],
            "ohlcv_participation_cap_contract_v3",
        )
        self.assertEqual(meta["spot_buy_fee_asset"], "quote")
        self.assertEqual(meta["market_data_granularity"], "ohlcv")
        self.assertEqual(
            meta["execution_realism_limitations"],
            [
                "no_l2_depth",
                "no_spread_or_queue_position",
                "no_market_impact_calibration",
                "fixed_slippage_bps",
                "volume_participation_proxy_only",
            ],
        )
        self.assertEqual(meta["total_bars"], 8)
        self.assertEqual(meta["total_fills"], 4)
        self.assertEqual(meta["cadence_gap_count"], 0)
        self.assertEqual(
            meta["risk_metric_policy_id"],
            "calendar-365.25-bar-pnl-increment/v1",
        )
        self.assertTrue(meta["start_ts"].endswith("+00:00"))
        self.assertTrue(meta["end_ts"].endswith("+00:00"))
        self.assertEqual(meta["generated_at"], "2026-04-23T00:00:00+00:00")

    def test_derivative_scorecard_remains_fail_closed_without_verified_lineage(self) -> None:
        result = _mk_result(
            curve=(),
            diagnostics=(),
            config=BacktestConfig(
                instrument_contract=LINEAR_SWAP_CONTRACT,
            ),
        )

        with self.assertRaisesRegex(
            ValueError,
            "legacy_derivative_replay_contract_lineage_required",
        ):
            build_scorecard(result)

    def test_result_contract_identity_mismatch_is_rejected(self) -> None:
        result = _mk_result(curve=(_mk_point(0, "0"),), diagnostics=())
        bad_summary = replace(result.summary, instrument_symbol="ETH-USDT")

        with self.assertRaisesRegex(
            ValueError,
            "backtest_summary_instrument_symbol_mismatch",
        ):
            build_scorecard(replace(result, summary=bad_summary))

    def test_scorecard_rejects_falsy_non_mapping_parameter_extra(self) -> None:
        result = _mk_result(curve=(), diagnostics=())
        for invalid in (None, [], ""):
            params = replace(result.resolved_parameters)
            object.__setattr__(params, "extra", invalid)
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ValueError,
                "backtest_resolved_parameters_extra_must_be_empty",
            ):
                build_scorecard(replace(result, resolved_parameters=params))

    def test_no_verdict_fields_or_text(self) -> None:
        """整份 scorecard 不得含自动 gate 字段或完整裁决值。"""
        sc = self._build_standard()
        forbidden = {"verdict", "go", "nogo", "pass", "fail", "archive"}

        def walk(value: object) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    normalized = "".join(
                        char for char in str(key).lower() if char.isalnum()
                    )
                    self.assertNotIn(normalized, forbidden)
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
            elif isinstance(value, str):
                normalized = "".join(
                    char for char in value.lower() if char.isalnum()
                )
                self.assertNotIn(normalized, forbidden)

        walk(sc)

    def test_scorecard_is_json_serializable(self) -> None:
        sc = self._build_standard()
        # 应可直接 json.dumps (默认 encoder)
        dumped = json.dumps(sc)
        self.assertIsInstance(dumped, str)
        reloaded = json.loads(dumped)
        self.assertEqual(
            set(reloaded.keys()),
            _TOP_LEVEL_KEYS,
        )


# ---------------------------------------------------------------------------
# 2. OOS section
# ---------------------------------------------------------------------------


class TestScorecardOOS(unittest.TestCase):
    def test_oos_splits_into_train_and_test_with_utc_boundaries(self) -> None:
        curve = tuple(
            _mk_point(i, str(i * 10)) for i in range(8)
        )
        result = _mk_result(curve=curve, diagnostics=())
        sc = build_scorecard(result)
        oos = sc["oos"]
        self.assertIn("train", oos)
        self.assertIn("test", oos)
        self.assertIsNotNone(oos["split_ts"])
        self.assertTrue(oos["train"]["start"].endswith("+00:00"))
        self.assertTrue(oos["train"]["end"].endswith("+00:00"))
        self.assertTrue(oos["test"]["start"].endswith("+00:00"))
        self.assertTrue(oos["test"]["end"].endswith("+00:00"))
        # train end < test start
        train_end = datetime.fromisoformat(oos["train"]["end"])
        test_start = datetime.fromisoformat(oos["test"]["start"])
        self.assertLess(train_end, test_start)

    def test_oos_contains_ir_hit_rate_and_fills(self) -> None:
        curve = tuple(_mk_point(i, str(i)) for i in range(6))
        diagnostics = (_mk_diagnostic(0), _mk_diagnostic(4))
        result = _mk_result(curve=curve, diagnostics=diagnostics)
        sc = build_scorecard(result)
        for key in ("ir", "hit_rate", "fills"):
            self.assertIn(key, sc["oos"]["train"])
            self.assertIn(key, sc["oos"]["test"])
        # 监测 fills 计入合理段
        total = sc["oos"]["train"]["fills"] + sc["oos"]["test"]["fills"]
        self.assertEqual(total, len(diagnostics))

    def test_oos_monotone_rising_has_positive_hit_rate(self) -> None:
        curve = tuple(_mk_point(i, str(i * 5)) for i in range(10))
        result = _mk_result(curve=curve, diagnostics=())
        sc = build_scorecard(result)
        self.assertEqual(sc["oos"]["train"]["hit_rate"], 1.0)
        self.assertEqual(sc["oos"]["test"]["hit_rate"], 1.0)

    def test_oos_default_split_method_is_time_midpoint(self) -> None:
        curve = tuple(_mk_point(i, str(i)) for i in range(8))
        result = _mk_result(curve=curve, diagnostics=())
        sc = build_scorecard(result)
        self.assertEqual(sc["oos"]["split_method"], "time_midpoint")
        self.assertIsNotNone(sc["oos"]["split_ts"])

    def test_oos_explicit_split_ts_takes_effect(self) -> None:
        """显式 split_ts 应覆盖 time-midpoint, 并以该时刻为 train/test 边界。"""
        curve = tuple(_mk_point(i, str(i * 10)) for i in range(10))
        # bars at hours 0..9; split after bar 2 → train = bars 0,1,2 ; test = bars 3..9
        split = _BASE_TS + timedelta(hours=3)
        result = _mk_result(curve=curve, diagnostics=())
        sc = build_scorecard(result, split_ts=split)
        self.assertEqual(sc["oos"]["split_method"], "explicit")
        # split_ts 必须原样以 UTC ISO 写回
        self.assertEqual(
            sc["oos"]["split_ts"], split.isoformat()
        )
        # train 末 bar 应严格早于 split
        train_end = datetime.fromisoformat(sc["oos"]["train"]["end"])
        self.assertLess(train_end, split)
        # test 起始 bar 应 >= split
        test_start = datetime.fromisoformat(sc["oos"]["test"]["start"])
        self.assertGreaterEqual(test_start, split)

    def test_oos_explicit_split_ts_differs_from_midpoint(self) -> None:
        """explicit 与 time-midpoint 语义不同 — 至少一个边界应不同。"""
        curve = tuple(_mk_point(i, str(i)) for i in range(10))
        result = _mk_result(curve=curve, diagnostics=())
        default_sc = build_scorecard(result)
        custom_split = _BASE_TS + timedelta(hours=2)
        explicit_sc = build_scorecard(result, split_ts=custom_split)
        self.assertNotEqual(
            default_sc["oos"]["split_ts"], explicit_sc["oos"]["split_ts"]
        )
        self.assertEqual(default_sc["oos"]["split_method"], "time_midpoint")
        self.assertEqual(explicit_sc["oos"]["split_method"], "explicit")

    def test_oos_sample_n_and_max_drawdown_bps_present(self) -> None:
        """v0.2 模板对齐: oos.train/test 必须同时含 sample_n 与 max_drawdown_bps。"""
        # 人造一段有明显回撤的 curve: 升到 300 再下到 50
        values = ["0", "100", "200", "300", "150", "50", "120", "240"]
        curve = tuple(_mk_point(i, v) for i, v in enumerate(values))
        result = _mk_result(curve=curve, diagnostics=(), window_hours=len(values))
        sc = build_scorecard(result)
        for side in ("train", "test"):
            slot = sc["oos"][side]
            self.assertIn("sample_n", slot)
            self.assertIn("max_drawdown_bps", slot)
            self.assertIsInstance(slot["sample_n"], int)
            self.assertGreaterEqual(slot["sample_n"], 0)
            self.assertGreaterEqual(slot["max_drawdown_bps"], 0.0)
        # 至少一侧应捕捉到非零回撤 (curve 从 300 跌至 50)
        self.assertTrue(
            sc["oos"]["train"]["max_drawdown_bps"] > 0
            or sc["oos"]["test"]["max_drawdown_bps"] > 0
        )

    def test_oos_empty_curve_new_fields_are_zero(self) -> None:
        """空 curve → train/test 的 sample_n 和 max_drawdown_bps 必须为零值。"""
        result = _mk_result(curve=(), diagnostics=())
        sc = build_scorecard(result)
        for side in ("train", "test"):
            slot = sc["oos"][side]
            self.assertEqual(slot["sample_n"], 0)
            self.assertEqual(slot["max_drawdown_bps"], 0.0)

    def test_oos_explicit_split_outside_curve_leaves_one_side_empty(self) -> None:
        """explicit 模式不做 index 兜底 — 超出 curve 的 split 会让一侧为空。"""
        curve = tuple(_mk_point(i, str(i)) for i in range(4))
        far_future = _BASE_TS + timedelta(days=30)
        result = _mk_result(curve=curve, diagnostics=())
        sc = build_scorecard(result, split_ts=far_future)
        self.assertEqual(sc["oos"]["split_method"], "explicit")
        # 所有 bars 都早于 split → test 段为空
        self.assertEqual(sc["oos"]["test"]["fills"], 0)
        self.assertIsNone(sc["oos"]["test"]["start"])
        self.assertIsNotNone(sc["oos"]["train"]["start"])


# ---------------------------------------------------------------------------
# 3. cross_window section
# ---------------------------------------------------------------------------


class TestScorecardCrossWindow(unittest.TestCase):
    def test_cross_window_has_at_least_three_slices(self) -> None:
        curve = tuple(_mk_point(i, str(i)) for i in range(9))
        result = _mk_result(curve=curve, diagnostics=())
        sc = build_scorecard(result)
        self.assertGreaterEqual(len(sc["cross_window"]), 3)

    def test_cross_window_slice_fields(self) -> None:
        curve = tuple(_mk_point(i, str(i * 3)) for i in range(12))
        diagnostics = tuple(_mk_diagnostic(i) for i in range(0, 12, 3))
        result = _mk_result(
            curve=curve, diagnostics=diagnostics, window_hours=12
        )
        sc = build_scorecard(result)
        for slot in sc["cross_window"]:
            self.assertEqual(
                set(slot.keys()),
                {
                    "start",
                    "end",
                    "ir",
                    "ir_annualized",
                    "sharpe_ratio",
                    "hit_rate",
                    "fills",
                    "max_drawdown_bps",
                    "sample_n",
                },
            )
            if slot["start"] is not None:
                self.assertTrue(slot["start"].endswith("+00:00"))
                self.assertTrue(slot["end"].endswith("+00:00"))

    def test_cross_window_slices_are_non_overlapping_in_time(self) -> None:
        curve = tuple(_mk_point(i, str(i)) for i in range(18))
        result = _mk_result(curve=curve, diagnostics=(), window_hours=18)
        sc = build_scorecard(result)
        ends: list[datetime] = []
        starts: list[datetime] = []
        for slot in sc["cross_window"]:
            if slot["start"] is not None:
                starts.append(datetime.fromisoformat(slot["start"]))
                ends.append(datetime.fromisoformat(slot["end"]))
        for i in range(1, len(starts)):
            # 下一片的 start 不可早于上一片的 start
            self.assertGreaterEqual(starts[i], starts[i - 1])
            self.assertGreaterEqual(ends[i], ends[i - 1])

    def test_cross_window_partitions_only_oos_test(self) -> None:
        curve = tuple(_mk_point(i, str(i)) for i in range(18))
        diagnostics = tuple(_mk_diagnostic(i) for i in range(17))
        result = _mk_result(
            curve=curve,
            diagnostics=diagnostics,
            window_hours=18,
        )
        sc = build_scorecard(result)

        test = sc["oos"]["test"]
        windows = sc["cross_window"]
        self.assertEqual(windows[0]["start"], test["start"])
        self.assertEqual(windows[-1]["end"], test["end"])
        self.assertEqual(sum(slot["fills"] for slot in windows), test["fills"])
        # Return 按终点归属，每个 test return 必须且只能进入一个窗口。
        self.assertEqual(
            sum(slot["sample_n"] for slot in windows),
            test["sample_n"],
        )
        self.assertGreater(windows[0]["start"], sc["oos"]["train"]["start"])

    def test_cross_window_keeps_loss_at_oos_and_slice_boundary(self) -> None:
        values = ["0", "0", "0", *("-1000" for _ in range(7))]
        curve = tuple(_mk_point(i, value) for i, value in enumerate(values))
        result = _mk_result(curve=curve, diagnostics=(), window_hours=10)

        sc = build_scorecard(
            result,
            split_ts=_BASE_TS + timedelta(hours=3),
        )

        self.assertLess(sc["oos"]["test"]["ir"], 0)
        self.assertGreater(sc["cross_window"][0]["max_drawdown_bps"], 0)
        self.assertEqual(
            sum(slot["sample_n"] for slot in sc["cross_window"]),
            sc["oos"]["test"]["sample_n"],
        )

    def test_cross_window_sample_n_present_and_nonneg(self) -> None:
        """v0.2 模板对齐: 每个 cross_window slot 必须含整数 sample_n >= 0。"""
        curve = tuple(_mk_point(i, str(i)) for i in range(9))
        result = _mk_result(curve=curve, diagnostics=(), window_hours=9)
        sc = build_scorecard(result)
        for slot in sc["cross_window"]:
            self.assertIn("sample_n", slot)
            self.assertIsInstance(slot["sample_n"], int)
            self.assertGreaterEqual(slot["sample_n"], 0)
        # 空 curve → 每片 sample_n == 0
        empty_sc = build_scorecard(_mk_result(curve=(), diagnostics=()))
        for slot in empty_sc["cross_window"]:
            self.assertEqual(slot["sample_n"], 0)

    def test_cross_window_with_drawdown(self) -> None:
        """明显回撤段 -> max_drawdown_bps > 0。"""
        # 回撤必须发生在 OOS test 的同一个 cross-window 子段内。
        values = ["0", "100", "200", "300", "100", "200", "50", "160", "240"]
        curve = tuple(_mk_point(i, v) for i, v in enumerate(values))
        result = _mk_result(curve=curve, diagnostics=(), window_hours=9)
        sc = build_scorecard(result)
        any_dd = any(
            slot["max_drawdown_bps"] > 0 for slot in sc["cross_window"]
        )
        self.assertTrue(any_dd)


# ---------------------------------------------------------------------------
# 4. cost_adjusted section
# ---------------------------------------------------------------------------


class TestScorecardCostAdjusted(unittest.TestCase):
    def test_cost_adjusted_keys_and_values(self) -> None:
        curve = tuple(_mk_point(i, str(i)) for i in range(4))
        # assumed_cost 6.0, actual 5.0 -> diff -1.0, actual_net = 10 - (-1) = 11
        diagnostics = tuple(
            _mk_diagnostic(i, assumed_cost=6.0, actual_cost=5.0, assumed_net=10.0)
            for i in range(3)
        )
        config = BacktestConfig(taker_fee_bps=3.5, ioc_slippage_bps=1.5)
        result = _mk_result(curve=curve, diagnostics=diagnostics, config=config)
        sc = build_scorecard(result)
        ca = sc["cost_adjusted"]
        # 顶层仍保留 overall aggregate 5 字段 + 新增 train/test 子对象
        self.assertEqual(
            set(ca.keys()),
            {
                "realized_edge_bps",
                "fee_bps",
                "slip_bps",
                "exec_buffer_bps",
                "net_edge_bps",
                "train",
                "test",
                "sensitivity",
            },
        )
        self.assertAlmostEqual(ca["fee_bps"], 3.5, places=6)
        self.assertAlmostEqual(ca["slip_bps"], 1.5, places=6)
        # exec_buffer = assumed_cost(6) - fee(3.5) - slip(1.5) = 1.0
        self.assertAlmostEqual(ca["exec_buffer_bps"], 1.0, places=6)
        # realized_edge = assumed_net(10) + assumed_cost(6) = 16
        self.assertAlmostEqual(ca["realized_edge_bps"], 16.0, places=6)
        # net_edge = actual_net (10 - (-1)) = 11
        self.assertAlmostEqual(ca["net_edge_bps"], 11.0, places=6)

    def test_cost_adjusted_empty_diagnostics(self) -> None:
        curve = tuple(_mk_point(i, "0") for i in range(3))
        config = BacktestConfig(ioc_slippage_bps=2.0)
        result = _mk_result(curve=curve, diagnostics=(), config=config)
        sc = build_scorecard(result)
        ca = sc["cost_adjusted"]
        self.assertEqual(ca["fee_bps"], 0.0)
        self.assertEqual(ca["net_edge_bps"], 0.0)
        self.assertEqual(ca["realized_edge_bps"], 0.0)
        self.assertEqual(ca["exec_buffer_bps"], 0.0)
        self.assertAlmostEqual(ca["slip_bps"], 2.0, places=6)

    def test_slip_bps_ioc_uses_config_value(self) -> None:
        curve = tuple(_mk_point(i, str(i)) for i in range(3))
        config = BacktestConfig(
            order_type="ioc",
            taker_fee_bps=3.5,
            ioc_slippage_bps=1.5,
        )
        diagnostics = (_mk_diagnostic(0, assumed_cost=6.0, actual_cost=5.0),)
        result = _mk_result(curve=curve, diagnostics=diagnostics, config=config)
        sc = build_scorecard(result)
        self.assertAlmostEqual(sc["cost_adjusted"]["slip_bps"], 1.5, places=6)
        # v2 显式分项: actual_cost(5) = fee(3.5) + slip(1.5)。
        self.assertAlmostEqual(
            sc["cost_adjusted"]["exec_buffer_bps"], 1.0, places=6
        )

    def test_slip_bps_post_only_is_zero(self) -> None:
        """post_only 无 IOC slippage — slip_bps 必须为 0.0。"""
        curve = tuple(_mk_point(i, str(i)) for i in range(3))
        config = BacktestConfig(
            order_type="post_only",
            maker_fee_bps=5.0,
            ioc_slippage_bps=1.5,  # present in config but must not leak
        )
        diagnostics = (_mk_diagnostic(0, assumed_cost=6.0, actual_cost=5.0),)
        result = _mk_result(curve=curve, diagnostics=diagnostics, config=config)
        sc = build_scorecard(result)
        self.assertEqual(sc["cost_adjusted"]["slip_bps"], 0.0)
        # exec_buffer = assumed_cost(6) - fee(5) - slip(0) = 1.0
        self.assertAlmostEqual(
            sc["cost_adjusted"]["exec_buffer_bps"], 1.0, places=6
        )

    def test_slip_bps_bounded_limit_uses_taker_fallback(self) -> None:
        """历史 bounded_limit diagnostic 回退到当前固定 taker slippage。"""
        curve = tuple(_mk_point(i, str(i)) for i in range(3))
        config = BacktestConfig(
            order_type="bounded_limit",
            taker_fee_bps=3.5,
            ioc_slippage_bps=1.5,
        )
        diagnostics = (_mk_diagnostic(0, assumed_cost=6.0, actual_cost=5.0),)
        result = _mk_result(curve=curve, diagnostics=diagnostics, config=config)
        sc = build_scorecard(result)
        self.assertEqual(sc["cost_adjusted"]["slip_bps"], 1.5)

    def test_actual_cost_components_override_legacy_order_type_fallback(self) -> None:
        curve = tuple(_mk_point(i, str(i)) for i in range(3))
        diagnostic = CostDiagnostic(
            decision_id=_BASE_TS.isoformat(),
            assumed_cost_bps=8.0,
            actual_cost_bps=6.25,
            cost_diff_bps=-1.75,
            assumed_net_edge_bps=10.0,
            actual_net_edge_bps=11.75,
            edge_flipped_negative=False,
            actual_fee_bps=4.75,
            actual_slippage_bps=1.5,
        )
        config = BacktestConfig(
            order_type="ioc",
            taker_fee_bps=4.75,
            ioc_slippage_bps=1.5,
        )
        result = _mk_result(
            curve=curve,
            diagnostics=(diagnostic,),
            config=config,
        )

        cost = build_scorecard(result)["cost_adjusted"]
        self.assertEqual(cost["fee_bps"], 4.75)
        self.assertEqual(cost["slip_bps"], 1.5)
        self.assertEqual(cost["exec_buffer_bps"], 1.75)

    def test_slip_bps_post_only_empty_diagnostics(self) -> None:
        """empty diagnostics path 也应遵守 order_type 决定 slip_bps 的规则。"""
        curve = tuple(_mk_point(i, "0") for i in range(2))
        config = BacktestConfig(
            order_type="post_only", ioc_slippage_bps=2.0
        )
        result = _mk_result(curve=curve, diagnostics=(), config=config)
        sc = build_scorecard(result)
        self.assertEqual(sc["cost_adjusted"]["slip_bps"], 0.0)


# ---------------------------------------------------------------------------
# 4b. cost_adjusted train/test split alignment (v0.4)
# ---------------------------------------------------------------------------


_COST_FIELDS = {
    "realized_edge_bps",
    "fee_bps",
    "slip_bps",
    "exec_buffer_bps",
    "net_edge_bps",
}


class TestScorecardCostAdjustedSplit(unittest.TestCase):
    def test_cost_adjusted_train_test_fields_present(self) -> None:
        curve = tuple(_mk_point(i, str(i)) for i in range(6))
        diagnostics = tuple(_mk_diagnostic(i) for i in range(5))
        result = _mk_result(curve=curve, diagnostics=diagnostics)
        sc = build_scorecard(result)
        ca = sc["cost_adjusted"]
        self.assertIn("train", ca)
        self.assertIn("test", ca)
        self.assertEqual(set(ca["train"].keys()), _COST_FIELDS)
        self.assertEqual(set(ca["test"].keys()), _COST_FIELDS)

    def test_cost_adjusted_explicit_split_partitions_diagnostics(self) -> None:
        """显式 split_ts: diagnostics 按 decision ts 落入 train / test。"""
        curve = tuple(_mk_point(i, str(i * 5)) for i in range(8))
        # train side 用不同 cost 语义, test side 用另一套, 方便区分
        train_diags = tuple(
            _mk_diagnostic(i, assumed_cost=4.0, actual_cost=2.0, assumed_net=8.0)
            for i in range(3)
        )
        test_diags = tuple(
            _mk_diagnostic(i, assumed_cost=10.0, actual_cost=2.0, assumed_net=20.0)
            for i in range(4, 8)
        )
        diagnostics = train_diags + test_diags
        config = BacktestConfig(taker_fee_bps=1.0, ioc_slippage_bps=1.0)
        result = _mk_result(curve=curve, diagnostics=diagnostics, config=config)
        split = _BASE_TS + timedelta(hours=4)
        sc = build_scorecard(result, split_ts=split)
        ca = sc["cost_adjusted"]
        # v2 显式分项: actual_cost(2) = fee(1) + slip(1)。
        self.assertAlmostEqual(ca["train"]["fee_bps"], 1.0, places=6)
        self.assertAlmostEqual(ca["train"]["realized_edge_bps"], 12.0, places=6)
        # v2 同一运行必须绑定同一配置费率；两侧用 assumed edge 区分。
        self.assertAlmostEqual(ca["test"]["fee_bps"], 1.0, places=6)
        self.assertAlmostEqual(ca["test"]["realized_edge_bps"], 30.0, places=6)
        # slip_bps 遵循 order_type 规则, 与全局一致
        self.assertEqual(ca["train"]["slip_bps"], ca["slip_bps"])
        self.assertEqual(ca["test"]["slip_bps"], ca["slip_bps"])

    def test_cost_adjusted_time_midpoint_fallback_generates_both_sides(self) -> None:
        """无 split_ts: 回退到 curve time-midpoint, train/test 仍可生成。"""
        curve = tuple(_mk_point(i, str(i)) for i in range(8))
        diagnostics = tuple(_mk_diagnostic(i) for i in range(7))
        result = _mk_result(curve=curve, diagnostics=diagnostics)
        sc = build_scorecard(result)
        ca = sc["cost_adjusted"]
        # 所有 diagnostics 都能被解析, 两侧样本数之和应等于总样本
        # 不直接看样本数 (cost_adjusted 不暴露 sample_n), 但两侧 fee 必须可计算
        # (若一侧为空则 fee == 0.0, 另一侧等于 overall fee)
        self.assertIn("train", ca)
        self.assertIn("test", ca)
        # OOS 用同样的 time-midpoint; 对齐到相同 split_ms 时, 两侧 diagnostics 总数 = 8
        # 也就是说两侧 fee 必须至少一个非零 (因为所有 diag 都走了常量 cost)
        self.assertTrue(ca["train"]["fee_bps"] > 0 or ca["test"]["fee_bps"] > 0)
        # 两侧 fee 的加权平均 (按落入两侧的比例) 应等于 overall fee
        # 构造上 actual_cost=5、fee=5、实际 slip=0。
        for side in ("train", "test"):
            if ca[side]["fee_bps"] != 0.0:
                self.assertAlmostEqual(ca[side]["fee_bps"], 5.0, places=6)

    def test_cost_adjusted_split_aligned_with_oos_split(self) -> None:
        """train/test 的划分规则必须与 oos 的 split_ts 对齐。"""
        curve = tuple(_mk_point(i, str(i)) for i in range(10))
        diagnostics = tuple(_mk_diagnostic(i) for i in range(9))
        result = _mk_result(curve=curve, diagnostics=diagnostics)
        # 显式路径
        split = _BASE_TS + timedelta(hours=3)
        sc = build_scorecard(result, split_ts=split)
        self.assertEqual(sc["oos"]["split_method"], "explicit")
        # OOS fills 加和必须等于 cost_adjusted 两侧 diag 能被解析的总数。
        self.assertEqual(
            sc["oos"]["train"]["fills"] + sc["oos"]["test"]["fills"],
            len(diagnostics),
        )

    def test_cost_adjusted_empty_diagnostics_all_zero(self) -> None:
        """空 diagnostics: overall / train / test 均为零值结构。"""
        curve = tuple(_mk_point(i, "0") for i in range(4))
        config = BacktestConfig(order_type="ioc", ioc_slippage_bps=2.5)
        result = _mk_result(curve=curve, diagnostics=(), config=config)
        sc = build_scorecard(result)
        ca = sc["cost_adjusted"]
        for bucket in (ca, ca["train"], ca["test"]):
            self.assertEqual(bucket["realized_edge_bps"], 0.0)
            self.assertEqual(bucket["fee_bps"], 0.0)
            self.assertEqual(bucket["exec_buffer_bps"], 0.0)
            self.assertEqual(bucket["net_edge_bps"], 0.0)
            # slip_bps 继续沿用 order_type 语义
            self.assertAlmostEqual(bucket["slip_bps"], 2.5, places=6)

    def test_cost_adjusted_overall_unchanged_for_existing_readers(self) -> None:
        """顶层 schema + overall 5 字段保持可读 (backward compatibility)。"""
        curve = tuple(_mk_point(i, str(i)) for i in range(4))
        diagnostics = tuple(
            _mk_diagnostic(i, assumed_cost=6.0, actual_cost=5.0, assumed_net=10.0)
            for i in range(3)
        )
        config = BacktestConfig(taker_fee_bps=3.5, ioc_slippage_bps=1.5)
        result = _mk_result(curve=curve, diagnostics=diagnostics, config=config)
        sc = build_scorecard(result)
        # 顶层 schema 未变
        self.assertEqual(
            set(sc.keys()),
            _TOP_LEVEL_KEYS,
        )
        ca = sc["cost_adjusted"]
        # overall 5 字段与改造前语义一致
        self.assertAlmostEqual(ca["realized_edge_bps"], 16.0, places=6)
        self.assertAlmostEqual(ca["fee_bps"], 3.5, places=6)
        self.assertAlmostEqual(ca["slip_bps"], 1.5, places=6)
        self.assertAlmostEqual(ca["exec_buffer_bps"], 1.0, places=6)
        self.assertAlmostEqual(ca["net_edge_bps"], 11.0, places=6)


# ---------------------------------------------------------------------------
# 4c. cost_adjusted.sensitivity (v0.5)
# ---------------------------------------------------------------------------


_SENSITIVITY_FIELDS = {
    "net_edge_fee_up_20pct_bps",
    "net_edge_slip_plus_0_5bps_bps",
}


class TestScorecardCostAdjustedSensitivity(unittest.TestCase):
    def test_sensitivity_structure_present(self) -> None:
        curve = tuple(_mk_point(i, str(i)) for i in range(4))
        diagnostics = tuple(
            _mk_diagnostic(i, assumed_cost=6.0, actual_cost=5.0, assumed_net=10.0)
            for i in range(3)
        )
        config = BacktestConfig(taker_fee_bps=3.5, ioc_slippage_bps=1.5)
        result = _mk_result(curve=curve, diagnostics=diagnostics, config=config)
        sc = build_scorecard(result)
        sens = sc["cost_adjusted"]["sensitivity"]
        self.assertEqual(set(sens.keys()), {"overall", "train", "test"})
        for side in ("overall", "train", "test"):
            self.assertEqual(set(sens[side].keys()), _SENSITIVITY_FIELDS)

    def test_sensitivity_formulas_match_manual_derivation(self) -> None:
        """overall bucket: fee=3.5, slip=1.5, realized=16, exec_buf=1."""
        curve = tuple(_mk_point(i, str(i)) for i in range(4))
        diagnostics = tuple(
            _mk_diagnostic(i, assumed_cost=6.0, actual_cost=5.0, assumed_net=10.0)
            for i in range(3)
        )
        config = BacktestConfig(taker_fee_bps=3.5, ioc_slippage_bps=1.5)
        result = _mk_result(curve=curve, diagnostics=diagnostics, config=config)
        sc = build_scorecard(result)
        overall_sens = sc["cost_adjusted"]["sensitivity"]["overall"]
        # fee_up_20pct = 16 - (3.5*1.2) - 1.5 - 1 = 9.3
        self.assertAlmostEqual(
            overall_sens["net_edge_fee_up_20pct_bps"], 9.3, places=6
        )
        # slip_plus_0_5bps = 16 - 3.5 - (1.5 + 0.5) - 1 = 9.5
        self.assertAlmostEqual(
            overall_sens["net_edge_slip_plus_0_5bps_bps"], 9.5, places=6
        )

    def test_negative_maker_rebate_shock_is_adverse(self) -> None:
        diagnostic = CostDiagnostic(
            decision_id=_BASE_TS.isoformat(),
            assumed_cost_bps=-2.0,
            actual_cost_bps=-2.0,
            cost_diff_bps=0.0,
            assumed_net_edge_bps=12.0,
            actual_net_edge_bps=12.0,
            edge_flipped_negative=False,
            actual_fee_bps=-2.0,
            actual_slippage_bps=0.0,
        )
        result = _mk_result(
            curve=(_mk_point(0, "0"), _mk_point(1, "1")),
            diagnostics=(diagnostic,),
            config=BacktestConfig(
                order_type="post_only",
                maker_fee_bps=-2.0,
            ),
        )
        scorecard = build_scorecard(result)
        shocked = scorecard["cost_adjusted"]["sensitivity"]["overall"][
            "net_edge_fee_up_20pct_bps"
        ]

        self.assertAlmostEqual(shocked, 11.6, places=6)
        self.assertLess(shocked, scorecard["cost_adjusted"]["net_edge_bps"])

    def test_sensitivity_train_test_formulas_independent(self) -> None:
        """train 和 test 两侧的 sensitivity 都基于本侧 bucket 数值。"""
        curve = tuple(_mk_point(i, str(i * 5)) for i in range(8))
        train_diags = tuple(
            _mk_diagnostic(i, assumed_cost=4.0, actual_cost=2.0, assumed_net=8.0)
            for i in range(3)
        )
        test_diags = tuple(
            _mk_diagnostic(i, assumed_cost=10.0, actual_cost=2.0, assumed_net=20.0)
            for i in range(4, 8)
        )
        diagnostics = train_diags + test_diags
        config = BacktestConfig(taker_fee_bps=1.0, ioc_slippage_bps=1.0)
        result = _mk_result(curve=curve, diagnostics=diagnostics, config=config)
        split = _BASE_TS + timedelta(hours=4)
        sc = build_scorecard(result, split_ts=split)
        sens = sc["cost_adjusted"]["sensitivity"]
        ca = sc["cost_adjusted"]

        # train: realized=12, fee=1, slip=1, exec_buf = 4 - 1 - 1 = 2
        # fee_up_20pct = 12 - 1.2 - 1 - 2 = 7.8
        # slip_plus_0_5bps = 12 - 1 - 1.5 - 2 = 7.5
        self.assertAlmostEqual(ca["train"]["exec_buffer_bps"], 2.0, places=6)
        self.assertAlmostEqual(
            sens["train"]["net_edge_fee_up_20pct_bps"], 7.8, places=6
        )
        self.assertAlmostEqual(
            sens["train"]["net_edge_slip_plus_0_5bps_bps"], 7.5, places=6
        )

        # test: realized=30, fee=1, slip=1, exec_buf = 10 - 1 - 1 = 8
        # fee_up_20pct = 30 - 1.2 - 1 - 8 = 19.8
        # slip_plus_0_5bps = 30 - 1 - 1.5 - 8 = 19.5
        self.assertAlmostEqual(ca["test"]["exec_buffer_bps"], 8.0, places=6)
        self.assertAlmostEqual(
            sens["test"]["net_edge_fee_up_20pct_bps"], 19.8, places=6
        )
        self.assertAlmostEqual(
            sens["test"]["net_edge_slip_plus_0_5bps_bps"], 19.5, places=6
        )

    def test_sensitivity_empty_diagnostics_all_zero(self) -> None:
        """空 diagnostics: 即使 slip_bps 非零, sensitivity 全部稳定为零值。"""
        curve = tuple(_mk_point(i, "0") for i in range(4))
        config = BacktestConfig(order_type="ioc", ioc_slippage_bps=2.5)
        result = _mk_result(curve=curve, diagnostics=(), config=config)
        sc = build_scorecard(result)
        sens = sc["cost_adjusted"]["sensitivity"]
        for side in ("overall", "train", "test"):
            self.assertEqual(sens[side]["net_edge_fee_up_20pct_bps"], 0.0)
            self.assertEqual(sens[side]["net_edge_slip_plus_0_5bps_bps"], 0.0)

    def test_sensitivity_empty_side_only_is_zero(self) -> None:
        """只有一侧为空 (explicit split 超出 curve): 该侧 sensitivity 为零。"""
        curve = tuple(_mk_point(i, str(i)) for i in range(4))
        diagnostics = tuple(
            _mk_diagnostic(i, assumed_cost=6.0, actual_cost=5.0, assumed_net=10.0)
            for i in range(3)
        )
        config = BacktestConfig(taker_fee_bps=3.5, ioc_slippage_bps=1.5)
        result = _mk_result(curve=curve, diagnostics=diagnostics, config=config)
        far_future = _BASE_TS + timedelta(days=30)
        sc = build_scorecard(result, split_ts=far_future)
        sens = sc["cost_adjusted"]["sensitivity"]
        # test 侧为空 → 稳定零
        self.assertEqual(sens["test"]["net_edge_fee_up_20pct_bps"], 0.0)
        self.assertEqual(sens["test"]["net_edge_slip_plus_0_5bps_bps"], 0.0)
        # train 侧非空 → 按公式计算, 与 overall 一致
        self.assertAlmostEqual(
            sens["train"]["net_edge_fee_up_20pct_bps"],
            sens["overall"]["net_edge_fee_up_20pct_bps"],
            places=6,
        )

    def test_sensitivity_does_not_change_top_level_schema(self) -> None:
        curve = tuple(_mk_point(i, str(i)) for i in range(4))
        diagnostics = tuple(_mk_diagnostic(i) for i in range(3))
        result = _mk_result(curve=curve, diagnostics=diagnostics)
        sc = build_scorecard(result)
        self.assertEqual(
            set(sc.keys()),
            _TOP_LEVEL_KEYS,
        )
        # 旧的 overall / train / test 5 字段仍然原样存在
        for side in ("train", "test"):
            self.assertEqual(set(sc["cost_adjusted"][side].keys()), _COST_FIELDS)


# ---------------------------------------------------------------------------
# 5. regime_slice section
# ---------------------------------------------------------------------------


class TestScorecardRegimeSlice(unittest.TestCase):
    def test_regime_slice_has_vol_low_and_high(self) -> None:
        # 低波段 (小幅 step) + 高波段 (大幅 step)
        values = ["0", "1", "2", "3", "4", "5", "105", "5", "205", "5"]
        curve = tuple(_mk_point(i, v) for i, v in enumerate(values))
        result = _mk_result(curve=curve, diagnostics=(), window_hours=10)
        sc = build_scorecard(result)
        regime = sc["regime_slice"]
        self.assertIn("vol", regime)
        self.assertIn("low", regime["vol"])
        self.assertIn("high", regime["vol"])
        for bucket_key in ("low", "high"):
            bucket = regime["vol"][bucket_key]
            self.assertIn("ir", bucket)
            self.assertIn("fills", bucket)

    def test_regime_slice_sample_n_present(self) -> None:
        """v0.2 模板对齐: regime_slice.vol.low/high 必须含 sample_n >= 0, 且总和等于 bar-return 样本数。"""
        curve = tuple(_mk_point(i, str(i * i)) for i in range(6))
        result = _mk_result(curve=curve, diagnostics=())
        sc = build_scorecard(result)
        vol = sc["regime_slice"]["vol"]
        for key in ("low", "high"):
            self.assertIn("sample_n", vol[key])
            self.assertIsInstance(vol[key]["sample_n"], int)
            self.assertGreaterEqual(vol[key]["sample_n"], 0)
        # bar-level returns 数 = len(curve) - 1 = 5
        self.assertEqual(
            vol["low"]["sample_n"] + vol["high"]["sample_n"], len(curve) - 1
        )

    def test_regime_slice_empty_curve_sample_n_zero(self) -> None:
        """空 / 单点 curve → 两个 bucket 的 sample_n 均为 0。"""
        for curve in ((), (_mk_point(0, "0"),)):
            result = _mk_result(curve=curve, diagnostics=())
            sc = build_scorecard(result)
            vol = sc["regime_slice"]["vol"]
            self.assertEqual(vol["low"]["sample_n"], 0)
            self.assertEqual(vol["high"]["sample_n"], 0)

    def test_regime_slice_fills_allocation(self) -> None:
        curve = tuple(_mk_point(i, str(i * i)) for i in range(6))
        diagnostics = tuple(_mk_diagnostic(i) for i in range(1, 6))
        result = _mk_result(curve=curve, diagnostics=diagnostics)
        sc = build_scorecard(result)
        regime = sc["regime_slice"]["vol"]
        total = regime["low"]["fills"] + regime["high"]["fills"]
        # 每个 diagnostic 的 ts 必须能归到一个 bucket
        self.assertEqual(total, len(diagnostics))


# ---------------------------------------------------------------------------
# 6. Edge cases
# ---------------------------------------------------------------------------


class TestScorecardEdgeCases(unittest.TestCase):
    def test_empty_curve_produces_zero_values(self) -> None:
        result = _mk_result(curve=(), diagnostics=())
        sc = build_scorecard(result)
        self.assertEqual(sc["meta"]["total_bars"], 0)
        self.assertEqual(sc["oos"]["train"]["fills"], 0)
        self.assertEqual(sc["oos"]["test"]["fills"], 0)
        self.assertGreaterEqual(len(sc["cross_window"]), 3)
        for slot in sc["cross_window"]:
            self.assertEqual(slot["ir"], 0.0)
            self.assertEqual(slot["fills"], 0)
            self.assertEqual(slot["max_drawdown_bps"], 0.0)

    def test_constant_equity_produces_zero_ir(self) -> None:
        curve = tuple(_mk_point(i, "0") for i in range(8))
        result = _mk_result(curve=curve, diagnostics=())
        sc = build_scorecard(result)
        self.assertEqual(sc["oos"]["train"]["ir"], 0.0)
        self.assertEqual(sc["oos"]["test"]["ir"], 0.0)
        for slot in sc["cross_window"]:
            self.assertEqual(slot["ir"], 0.0)
        self.assertEqual(sc["regime_slice"]["vol"]["low"]["ir"], 0.0)
        self.assertEqual(sc["regime_slice"]["vol"]["high"]["ir"], 0.0)

    def test_explicit_fill_attribution_must_be_complete_and_reconcilable(
        self,
    ) -> None:
        curve = tuple(_mk_point(i, str(i)) for i in range(3))
        valid = _mk_result(curve=curve, diagnostics=(_mk_diagnostic(0),))
        baseline = valid.cost_diagnostics[0]
        cases = (
            (
                replace(
                    baseline,
                    equity_attribution_ts_ms=None,
                ),
                1,
                "scorecard_fill_attribution_incomplete",
            ),
            (
                replace(
                    baseline,
                    equity_attribution_ts_ms=curve[0].ts_ms,
                ),
                1,
                "scorecard_fill_attribution_missing_equity_interval",
            ),
        )
        for diagnostic, fills_count, reason in cases:
            with self.subTest(reason=reason):
                result = replace(
                    valid,
                    cost_diagnostics=(diagnostic,),
                    fills_count=fills_count,
                )
                result = replace(
                    result,
                    summary=replace(result.summary, fill_count=fills_count),
                )
                with self.assertRaisesRegex(ValueError, reason):
                    build_scorecard(result)

    def test_v2_scorecard_rejects_legacy_fill_without_diagnostic(self) -> None:
        curve = tuple(_mk_point(i, str(i)) for i in range(3))
        valid = _mk_result(curve=curve, diagnostics=(_mk_diagnostic(0),))

        with self.assertRaisesRegex(
            ValueError,
            "scorecard_fill_diagnostic_count_mismatch",
        ):
            build_scorecard(replace(valid, cost_diagnostics=()))

    def test_v2_scorecard_requires_complete_execution_timeline(self) -> None:
        curve = tuple(_mk_point(i, str(i)) for i in range(3))
        valid = _mk_result(curve=curve, diagnostics=(_mk_diagnostic(0),))

        with self.assertRaisesRegex(
            ValueError,
            "backtest_execution_timeline_count_mismatch",
        ):
            build_scorecard(replace(valid, execution_timeline=()))

    def test_single_bar_curve_does_not_crash(self) -> None:
        curve = (_mk_point(0, "0"),)
        result = _mk_result(curve=curve, diagnostics=())
        # 不应抛
        sc = build_scorecard(result)
        self.assertEqual(sc["meta"]["total_bars"], 1)
        # oos 至少要有 train 和 test 两键，即便其中一段空
        self.assertIn("train", sc["oos"])
        self.assertIn("test", sc["oos"])


# ---------------------------------------------------------------------------
# 6b. Annualized IR / Sharpe alignment (v0.3)
# ---------------------------------------------------------------------------


class TestScorecardAnnualizedMetrics(unittest.TestCase):
    def test_oos_and_cross_window_expose_new_fields(self) -> None:
        curve = tuple(_mk_point(i, str(i * 5)) for i in range(10))
        result = _mk_result(curve=curve, diagnostics=(), window_hours=10)
        sc = build_scorecard(result)
        for side in ("train", "test"):
            slot = sc["oos"][side]
            self.assertIn("ir_annualized", slot)
            self.assertIn("sharpe_ratio", slot)
        for slot in sc["cross_window"]:
            self.assertIn("ir_annualized", slot)
            self.assertIn("sharpe_ratio", slot)

    def test_top_level_schema_unchanged(self) -> None:
        curve = tuple(_mk_point(i, str(i)) for i in range(6))
        result = _mk_result(curve=curve, diagnostics=())
        sc = build_scorecard(result)
        self.assertEqual(
            set(sc.keys()),
            _TOP_LEVEL_KEYS,
        )

    def test_monotone_rising_ir_annualized_ge_ir(self) -> None:
        """单调上涨样本 → ir_annualized >= ir (sqrt(factor) >= 1 for hourly bars)。"""
        curve = tuple(_mk_point(i, str(i * 7)) for i in range(12))
        result = _mk_result(curve=curve, diagnostics=(), window_hours=12)
        sc = build_scorecard(result)
        for side in ("train", "test"):
            slot = sc["oos"][side]
            self.assertGreaterEqual(slot["ir_annualized"], slot["ir"])
            self.assertGreaterEqual(slot["sharpe_ratio"], slot["ir"])
        for slot in sc["cross_window"]:
            if slot["sample_n"] >= 2:
                self.assertGreaterEqual(slot["ir_annualized"], slot["ir"])
                self.assertGreaterEqual(slot["sharpe_ratio"], slot["ir"])

    def test_constant_equity_annualized_metrics_are_zero(self) -> None:
        curve = tuple(_mk_point(i, "0") for i in range(8))
        result = _mk_result(curve=curve, diagnostics=())
        sc = build_scorecard(result)
        for side in ("train", "test"):
            slot = sc["oos"][side]
            self.assertEqual(slot["ir_annualized"], 0.0)
            self.assertEqual(slot["sharpe_ratio"], 0.0)
        for slot in sc["cross_window"]:
            self.assertEqual(slot["ir_annualized"], 0.0)
            self.assertEqual(slot["sharpe_ratio"], 0.0)

    def test_empty_curve_annualized_metrics_are_zero(self) -> None:
        result = _mk_result(curve=(), diagnostics=())
        sc = build_scorecard(result)
        for side in ("train", "test"):
            slot = sc["oos"][side]
            self.assertEqual(slot["ir_annualized"], 0.0)
            self.assertEqual(slot["sharpe_ratio"], 0.0)
        for slot in sc["cross_window"]:
            self.assertEqual(slot["ir_annualized"], 0.0)
            self.assertEqual(slot["sharpe_ratio"], 0.0)


# ---------------------------------------------------------------------------
# 7. Cross-field sanity (ensure numerics are finite)
# ---------------------------------------------------------------------------


class TestScorecardNumericFinite(unittest.TestCase):
    def test_all_numeric_fields_are_finite(self) -> None:
        curve = tuple(_mk_point(i, str(i * 7)) for i in range(10))
        diagnostics = tuple(_mk_diagnostic(i) for i in range(0, 10, 2))
        result = _mk_result(curve=curve, diagnostics=diagnostics)
        sc = build_scorecard(result)

        def _walk(obj: object) -> None:
            if isinstance(obj, dict):
                for v in obj.values():
                    _walk(v)
            elif isinstance(obj, list):
                for v in obj:
                    _walk(v)
            elif isinstance(obj, float):
                self.assertFalse(
                    obj != obj or obj in (float("inf"), float("-inf")),
                    f"non-finite float in scorecard: {obj!r}",
                )

        _walk(sc)

    def test_generated_at_is_utc_iso(self) -> None:
        curve = (_mk_point(0, "0"), _mk_point(1, "1"))
        result = _mk_result(curve=curve, diagnostics=())
        sc = build_scorecard(
            result,
            generated_at=datetime(2026, 4, 23, 12, 34, 56, tzinfo=timezone.utc),
        )
        self.assertTrue(re.match(
            r"^2026-04-23T12:34:56\+00:00$",
            sc["meta"]["generated_at"],
        ))

    def test_external_scorecard_timestamps_must_be_explicit_utc(self) -> None:
        result = _mk_result(
            curve=(_mk_point(0, "0"), _mk_point(1, "1")),
            diagnostics=(),
        )
        invalid_times = (
            datetime(2026, 4, 23, 12, 34, 56),
            datetime(
                2026,
                4,
                23,
                12,
                34,
                56,
                tzinfo=timezone(timedelta(hours=8)),
            ),
        )
        for field_name in ("generated_at", "split_ts"):
            for invalid in invalid_times:
                with self.subTest(field_name=field_name, invalid=invalid):
                    kwargs = {field_name: invalid}
                    with self.assertRaisesRegex(
                        ValueError,
                        f"scorecard_{field_name}_must_be_utc",
                    ):
                        build_scorecard(result, **kwargs)

    def test_huge_finite_decimal_curve_fails_before_nan_artifact(self) -> None:
        curve = (
            _mk_point(0, "0"),
            _mk_point(1, "1e500"),
            _mk_point(2, "2e500"),
        )
        result = _mk_result(curve=curve, diagnostics=())

        with self.assertRaisesRegex(
            ValueError,
            "backtest_equity_net_fee_identity_mismatch",
        ):
            build_scorecard(result)


if __name__ == "__main__":
    unittest.main()
