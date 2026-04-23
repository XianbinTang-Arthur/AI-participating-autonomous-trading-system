"""Unit tests for ``aats.data_platform.replay.backtest.evidence_scorecard``.

全部基于确定性合成 fixture 构造 ``BacktestResult``, 不依赖 DB / adapter。

覆盖面:
    * 顶层 key 齐全, 且无 verdict/go/no-go/pass/fail 文案
    * OOS 时间中点切分, train/test 两段 + UTC 边界
    * cross_window 至少 3 片, 每片含 ir/hit_rate/fills/max_drawdown_bps
    * cost_adjusted 5 个字段从 diagnostics 正确聚合
    * regime_slice.vol 低/高波 bucket, ir + fills
    * 空 curve / 空 diagnostics 不抛异常
    * 恒定 equity 下 IR == 0
"""

from __future__ import annotations

import json
import re
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from aats.data_platform.replay.backtest.cost_validator import (
    CostDiagnostic,
    CostValidationSummary,
)
from aats.data_platform.replay.backtest.equity_builder import (
    BacktestSummary,
    EquityPoint,
)
from aats.data_platform.replay.backtest.evidence_scorecard import build_scorecard
from aats.data_platform.replay.backtest.harness import (
    BacktestConfig,
    BacktestResult,
)


# ---------------------------------------------------------------------------
# Synthetic fixture builders
# ---------------------------------------------------------------------------


_BASE_TS = datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc)
_BASE_MS = int(_BASE_TS.timestamp() * 1000)
_HOUR_MS = 60 * 60 * 1000


def _mk_point(index: int, equity: str) -> EquityPoint:
    return EquityPoint(
        ts_ms=_BASE_MS + index * _HOUR_MS,
        equity=Decimal(equity),
        cumulative_pnl=Decimal(equity),
        drawdown_bps=Decimal("0"),
        daily_return_bps=Decimal("0"),
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
    cfg = config or BacktestConfig(ioc_slippage_bps=1.0, assumed_cost_bps=6.0)
    start_ts = _BASE_TS
    end_ts = _BASE_TS + timedelta(hours=window_hours)
    summary = BacktestSummary(
        bar_count=len(curve),
        fill_count=len(diagnostics),
        start_ts_ms=curve[0].ts_ms if curve else 0,
        end_ts_ms=curve[-1].ts_ms if curve else 0,
        final_equity=curve[-1].equity if curve else Decimal("0"),
        cumulative_pnl=curve[-1].equity if curve else Decimal("0"),
    )
    return BacktestResult(
        config=cfg,
        summary=summary,
        cost_summary=CostValidationSummary(total_decisions=len(diagnostics)),
        equity_curve=curve,
        decisions_count=len(curve),
        fills_count=len(diagnostics),
        start_ts=start_ts,
        end_ts=end_ts,
        cost_diagnostics=diagnostics,
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
            {"meta", "oos", "cross_window", "cost_adjusted", "regime_slice"},
        )

    def test_meta_includes_symbol_and_window(self) -> None:
        sc = self._build_standard()
        meta = sc["meta"]
        self.assertEqual(meta["symbol"], "BTC-USDT-SWAP")
        self.assertEqual(meta["timeframe"], "1h")
        self.assertEqual(meta["total_bars"], 8)
        self.assertEqual(meta["total_fills"], 4)
        # UTC ISO boundaries
        self.assertTrue(meta["start_ts"].endswith("+00:00"))
        self.assertTrue(meta["end_ts"].endswith("+00:00"))
        self.assertEqual(meta["generated_at"], "2026-04-23T00:00:00+00:00")

    def test_no_verdict_fields_or_text(self) -> None:
        """整份 scorecard 不得含 go / no-go / pass / fail / verdict / archive。"""
        sc = self._build_standard()
        blob = json.dumps(sc).lower()
        forbidden = ["verdict", "go/no-go", "no_go", "pass", "fail", "archive"]
        for token in forbidden:
            self.assertNotIn(
                token,
                blob,
                f"verdict-style token {token!r} must not appear in scorecard",
            )
        # 也不得出现裸 "go" (完整 word)
        self.assertNotRegex(
            blob, r"\bgo\b", "bare verdict word 'go' must not appear"
        )

    def test_scorecard_is_json_serializable(self) -> None:
        sc = self._build_standard()
        # 应可直接 json.dumps (默认 encoder)
        dumped = json.dumps(sc)
        self.assertIsInstance(dumped, str)
        reloaded = json.loads(dumped)
        self.assertEqual(
            set(reloaded.keys()),
            {"meta", "oos", "cross_window", "cost_adjusted", "regime_slice"},
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
                {"start", "end", "ir", "hit_rate", "fills", "max_drawdown_bps"},
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

    def test_cross_window_with_drawdown(self) -> None:
        """明显回撤段 -> max_drawdown_bps > 0。"""
        # 人造三段: 升 / 降 / 升
        values = ["0", "100", "200", "300", "100", "50", "80", "160", "240"]
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
            for i in range(4)
        )
        config = BacktestConfig(ioc_slippage_bps=1.5, assumed_cost_bps=6.0)
        result = _mk_result(curve=curve, diagnostics=diagnostics, config=config)
        sc = build_scorecard(result)
        ca = sc["cost_adjusted"]
        self.assertEqual(
            set(ca.keys()),
            {
                "realized_edge_bps",
                "fee_bps",
                "slip_bps",
                "exec_buffer_bps",
                "net_edge_bps",
            },
        )
        self.assertAlmostEqual(ca["fee_bps"], 5.0, places=6)
        self.assertAlmostEqual(ca["slip_bps"], 1.5, places=6)
        # exec_buffer = assumed_cost(6) - fee(5) - slip(1.5) = -0.5
        self.assertAlmostEqual(ca["exec_buffer_bps"], -0.5, places=6)
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
            order_type="ioc", ioc_slippage_bps=1.5, assumed_cost_bps=6.0
        )
        diagnostics = (_mk_diagnostic(0, assumed_cost=6.0, actual_cost=5.0),)
        result = _mk_result(curve=curve, diagnostics=diagnostics, config=config)
        sc = build_scorecard(result)
        self.assertAlmostEqual(sc["cost_adjusted"]["slip_bps"], 1.5, places=6)
        # exec_buffer = assumed_cost(6) - fee(5) - slip(1.5) = -0.5
        self.assertAlmostEqual(
            sc["cost_adjusted"]["exec_buffer_bps"], -0.5, places=6
        )

    def test_slip_bps_post_only_is_zero(self) -> None:
        """post_only 无 IOC slippage — slip_bps 必须为 0.0。"""
        curve = tuple(_mk_point(i, str(i)) for i in range(3))
        config = BacktestConfig(
            order_type="post_only",
            ioc_slippage_bps=1.5,  # present in config but must not leak
            assumed_cost_bps=6.0,
        )
        diagnostics = (_mk_diagnostic(0, assumed_cost=6.0, actual_cost=5.0),)
        result = _mk_result(curve=curve, diagnostics=diagnostics, config=config)
        sc = build_scorecard(result)
        self.assertEqual(sc["cost_adjusted"]["slip_bps"], 0.0)
        # exec_buffer = assumed_cost(6) - fee(5) - slip(0) = 1.0
        self.assertAlmostEqual(
            sc["cost_adjusted"]["exec_buffer_bps"], 1.0, places=6
        )

    def test_slip_bps_bounded_limit_is_zero(self) -> None:
        """bounded_limit 当前模拟无独立 slippage — slip_bps 必须为 0.0。"""
        curve = tuple(_mk_point(i, str(i)) for i in range(3))
        config = BacktestConfig(
            order_type="bounded_limit",
            ioc_slippage_bps=1.5,
            assumed_cost_bps=6.0,
        )
        diagnostics = (_mk_diagnostic(0, assumed_cost=6.0, actual_cost=5.0),)
        result = _mk_result(curve=curve, diagnostics=diagnostics, config=config)
        sc = build_scorecard(result)
        self.assertEqual(sc["cost_adjusted"]["slip_bps"], 0.0)

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
        curve = tuple(_mk_point(i, "100") for i in range(8))
        result = _mk_result(curve=curve, diagnostics=())
        sc = build_scorecard(result)
        self.assertEqual(sc["oos"]["train"]["ir"], 0.0)
        self.assertEqual(sc["oos"]["test"]["ir"], 0.0)
        for slot in sc["cross_window"]:
            self.assertEqual(slot["ir"], 0.0)
        self.assertEqual(sc["regime_slice"]["vol"]["low"]["ir"], 0.0)
        self.assertEqual(sc["regime_slice"]["vol"]["high"]["ir"], 0.0)

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


if __name__ == "__main__":
    unittest.main()
