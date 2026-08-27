"""Unit tests for ``aats.cli`` backtest subcommand.

我们避开真实 DB + FS 依赖：
    * DB session 构造通过 monkeypatch ``_build_session`` 屏蔽
    * ``run_backtest`` 通过 monkeypatch 返回 stub result
    * 输出写在 tmp_path 下，用 ``unittest`` 的 tempfile 生成
"""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from aats.cli import build_parser, main
from aats.data_platform.replay.backtest.cost_validator import (
    CostDiagnostic,
    CostValidationSummary,
)
from aats.data_platform.replay.backtest.equity_builder import (
    BacktestSummary,
    EquityPoint,
)
from aats.data_platform.replay.backtest.harness import (
    BacktestConfig,
    BacktestResult,
    ExecutionTimingRecord,
)
from aats.data_platform.replay.core.replay_context import ReplayParameterOverrides


_BASE_TS = datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc)


def _contract_args() -> list[str]:
    return [
        "--instrument-type", "SWAP",
        "--contract-type", "linear",
        "--base-currency", "BTC",
        "--quote-currency", "USDT",
        "--settle-currency", "USDT",
        "--contract-value", "0.01",
        "--contract-multiplier", "1",
        "--contract-value-currency", "BTC",
        "--lot-size", "1",
        "--min-size", "1",
        "--tick-size", "0.1",
    ]


def _spot_contract_args() -> list[str]:
    return [
        "--instrument-type", "SPOT",
        "--contract-type", "spot",
        "--base-currency", "BTC",
        "--quote-currency", "USDT",
        "--settle-currency", "USDT",
        "--contract-value", "1",
        "--contract-multiplier", "1",
        "--contract-value-currency", "BTC",
        "--lot-size", "0.0001",
        "--min-size", "0.0001",
        "--tick-size", "0.1",
        "--spot-buy-fee-asset", "quote",
        "--param", "strategy_short_bias_enabled=false",
    ]


def _make_stub_result(config: BacktestConfig) -> BacktestResult:
    """Construct a deterministic BacktestResult for output serialization tests."""
    if config.spot_buy_fee_asset is None:
        config = replace(config, spot_buy_fee_asset="quote")
    contract = config.instrument_contract
    assert contract is not None
    curve = (
        EquityPoint(
            ts_ms=int((_BASE_TS + timedelta(hours=1)).timestamp() * 1000),
            equity=Decimal("0"),
            cumulative_pnl=Decimal("0"),
            drawdown_bps=Decimal("0"),
            daily_return_bps=Decimal("0"),
            settlement_currency=contract.settle_currency,
            instrument_symbol=contract.symbol,
            instrument_contract_fingerprint=contract.fingerprint,
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            net_qty=Decimal("0"),
            avg_entry_price=Decimal("0"),
            mark_price=Decimal("1"),
            fill_count=0,
            accumulated_fees=Decimal("0"),
        ),
        EquityPoint(
            ts_ms=int((_BASE_TS + timedelta(hours=2)).timestamp() * 1000),
            equity=Decimal("0"),
            cumulative_pnl=Decimal("0"),
            drawdown_bps=Decimal("0"),
            daily_return_bps=Decimal("0"),
            settlement_currency=contract.settle_currency,
            instrument_symbol=contract.symbol,
            instrument_contract_fingerprint=contract.fingerprint,
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            net_qty=Decimal("0"),
            avg_entry_price=Decimal("0"),
            mark_price=Decimal("1"),
            fill_count=0,
            accumulated_fees=Decimal("0"),
        ),
    )
    summary = BacktestSummary(
        initial_equity=Decimal("0"),
        final_equity=Decimal("0"),
        cumulative_pnl=Decimal("0"),
        max_drawdown_bps=Decimal("0"),
        sharpe_ratio=0.0,
        fill_count=0,
        fee_total=Decimal("0"),
        bar_count=2,
        start_ts_ms=curve[0].ts_ms,
        end_ts_ms=curve[-1].ts_ms,
        settlement_currency=contract.settle_currency,
        instrument_symbol=contract.symbol,
        instrument_contract_fingerprint=contract.fingerprint,
    )
    cost_summary = CostValidationSummary()
    timeline = tuple(
        ExecutionTimingRecord(
            decision_id=(
                f"{(_BASE_TS + timedelta(hours=index)).isoformat()}_hold"
            ),
            action="hold",
            observation_bar_start_ts=_BASE_TS + timedelta(hours=index),
            observation_completed_at_ts=_BASE_TS + timedelta(hours=index + 1),
            decision_ts=_BASE_TS + timedelta(hours=index + 1),
            submitted_at_ts=None,
            next_tradable_event_ts=None,
            resolved_at_ts=_BASE_TS + timedelta(hours=index + 1),
            fill_ts=None,
            status="no_order",
            price_source=None,
        )
        for index in range(2)
    )
    return BacktestResult(
        config=config,
        resolved_parameters=replace(
            ReplayParameterOverrides.for_family(config.family),
            strategy_short_bias_enabled=False,
        ),
        adapter_identity=(
            "aats.data_platform.replay.adapters.independent_adapter."
            "IndependentReplayAdapter"
        ),
        adapter_algorithm_version="independent-replay/v2",
        summary=summary,
        cost_summary=cost_summary,
        equity_curve=curve,
        decisions_count=2,
        fills_count=0,
        start_ts=_BASE_TS,
        end_ts=_BASE_TS + timedelta(days=1),
        execution_timeline=timeline,
    )


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


class TestCLIArgparse(unittest.TestCase):
    def test_parse_iso_normalizes_explicit_offset_to_utc(self) -> None:
        from aats.cli import _parse_iso

        self.assertEqual(
            _parse_iso("2026-03-01T08:00:00+08:00", field="start"),
            _BASE_TS,
        )

    def test_cli_argparse_required_args(self) -> None:
        """缺 --start → SystemExit (argparse 2)。"""
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "backtest",
                    "--end",
                    "2026-03-08",
                    "--output-dir",
                    "/tmp/bt",
                ]
            )

    def test_cli_parses_valid_args(self) -> None:
        """完整参数 → Namespace 包含所有字段。"""
        parser = build_parser()
        args = parser.parse_args(
            [
                "backtest",
                "--symbol",
                "BTC-USDT-SWAP",
                "--timeframe",
                "1h",
                "--start",
                "2026-03-01",
                "--end",
                "2026-03-08",
                "--family",
                "independent",
                "--dataset-version",
                "v1.0",
                "--rdp-db-name",
                "aats_research",
                "--output-dir",
                "/tmp/bt",
                "--param",
                "entry_threshold=0.55",
                "--order-type",
                "ioc",
                "--max-volume-participation",
                "0.005",
                "--spot-buy-fee-asset",
                "base",
                *_contract_args(),
            ]
        )
        self.assertEqual(args.command, "backtest")
        self.assertEqual(args.symbol, "BTC-USDT-SWAP")
        self.assertEqual(args.timeframe, "1h")
        self.assertEqual(args.start, "2026-03-01")
        self.assertEqual(args.end, "2026-03-08")
        self.assertEqual(args.family, "independent")
        self.assertEqual(args.dataset_version, "v1.0")
        self.assertEqual(args.rdp_db_name, "aats_research")
        self.assertEqual(args.output_dir, "/tmp/bt")
        self.assertEqual(args.param, ["entry_threshold=0.55"])
        self.assertEqual(args.order_type, "ioc")
        self.assertEqual(args.max_volume_participation, Decimal("0.005"))
        self.assertEqual(args.spot_buy_fee_asset, "base")

    def test_cli_requires_symbol_even_when_contract_fields_are_present(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "backtest",
                    "--start",
                    "2026-03-01",
                    "--end",
                    "2026-03-08",
                    "--output-dir",
                    "/tmp/bt",
                    *_spot_contract_args(),
                ]
            )


# ---------------------------------------------------------------------------
# Output files end-to-end
# ---------------------------------------------------------------------------


class TestCLIOutputFiles(unittest.TestCase):
    def test_derivative_lineage_guard_precedes_output_and_database_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "must-not-exist"
            with (
                patch("aats.cli._resolve_database_url") as resolve_url,
                patch("aats.cli._build_session") as build_session,
                self.assertRaisesRegex(
                    SystemExit,
                    "legacy_derivative_replay_contract_lineage_required",
                ),
            ):
                main(
                    [
                        "backtest",
                        "--symbol", "BTC-USDT-SWAP",
                        "--start", "2026-03-01",
                        "--end", "2026-03-08",
                        "--output-dir", str(output_dir),
                        "--spot-buy-fee-asset", "quote",
                        *_contract_args(),
                    ]
                )

            self.assertFalse(output_dir.exists())
            resolve_url.assert_not_called()
            build_session.assert_not_called()

    def test_cli_output_files_created(self) -> None:
        """运行 main() 后应写 3 个文件，内容可被 JSON/CSV 解析。"""
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"

            def _fake_run_backtest(session, **kwargs):
                config = kwargs["config"]
                return _make_stub_result(config)

            with (
                patch(
                    "aats.cli.run_backtest",
                    side_effect=_fake_run_backtest,
                ),
                patch(
                    "aats.cli._build_session",
                    return_value=MagicMock(),
                ),
                patch(
                    "aats.cli._resolve_database_url",
                    return_value="postgresql+psycopg://user:pw@host:5432/aats_research",
                ),
            ):
                rc = main(
                    [
                        "backtest",
                        "--symbol",
                        "BTC-USDT",
                        "--timeframe",
                        "1h",
                        "--start",
                        "2026-03-01",
                        "--end",
                        "2026-03-08",
                        "--family",
                        "independent",
                        "--dataset-version",
                        "v1.0",
                        "--rdp-db-name",
                        "aats_research",
                        "--output-dir",
                        str(output_dir),
                        *_spot_contract_args(),
                    ]
                )
            self.assertEqual(rc, 0)

            # 5 payloads must exist, including per-fill cost attribution and the
            # FS-003 causal execution timeline; manifest publishes last.
            summary_path = output_dir / "summary.json"
            equity_path = output_dir / "equity_curve.csv"
            cost_path = output_dir / "cost_validation.json"
            diagnostics_path = output_dir / "cost_diagnostics.json"
            timeline_path = output_dir / "execution_timeline.json"
            manifest_path = output_dir / "manifest.json"
            self.assertTrue(summary_path.exists())
            self.assertTrue(equity_path.exists())
            self.assertTrue(cost_path.exists())
            self.assertTrue(diagnostics_path.exists())
            self.assertTrue(timeline_path.exists())
            self.assertTrue(manifest_path.exists())

            # summary.json parseable
            summary_data = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertIn("config", summary_data)
            self.assertIn("summary", summary_data)
            self.assertIn("decisions_count", summary_data)
            self.assertEqual(summary_data["decisions_count"], 2)
            self.assertEqual(summary_data["fills_count"], 0)
            self.assertEqual(
                summary_data["config"]["fill_model_version"],
                "ohlcv_participation_cap_contract_v3",
            )
            self.assertEqual(
                summary_data["config"]["max_volume_participation"],
                "0.01",
            )
            self.assertIn("resolved_parameters", summary_data)
            self.assertEqual(
                summary_data["adapter_algorithm_version"],
                "independent-replay/v2",
            )
            # Decimal serialized as string
            self.assertEqual(summary_data["summary"]["final_equity"], "0")

            # equity_curve.csv has header + rows
            csv_lines = equity_path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(csv_lines), 3)  # header + 2 points
            self.assertIn("ts_ms", csv_lines[0])
            self.assertIn("settlement_currency", csv_lines[0])
            self.assertIn("instrument_contract_fingerprint", csv_lines[0])

            # cost_validation.json parseable
            cost_data = json.loads(cost_path.read_text(encoding="utf-8"))
            self.assertEqual(cost_data["total_decisions"], 0)
            self.assertEqual(cost_data["decisions_with_fills"], 0)

            diagnostics_data = json.loads(
                diagnostics_path.read_text(encoding="utf-8")
            )
            self.assertEqual(
                diagnostics_data["artifact_schema_version"],
                "backtest-run/v2",
            )
            self.assertEqual(
                diagnostics_data["artifact_kind"],
                "backtest_cost_diagnostics",
            )
            self.assertEqual(diagnostics_data["diagnostics"], [])

            timeline_data = json.loads(timeline_path.read_text(encoding="utf-8"))
            self.assertEqual(len(timeline_data), 2)
            self.assertTrue(all(row["status"] == "no_order" for row in timeline_data))

            manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertTrue(manifest_data["complete"])
            self.assertEqual(len(manifest_data["artifact_sha256"]), 5)
            self.assertIn(
                "cost_diagnostics.json",
                manifest_data["artifact_sha256"],
            )
            self.assertEqual(len(manifest_data["run_fingerprint"]), 64)
            self.assertIn("resolved_parameters", manifest_data)
            self.assertEqual(
                manifest_data["adapter_algorithm_version"],
                "independent-replay/v2",
            )
            self.assertEqual(manifest_data["cadence_gap_count"], 0)
            self.assertEqual(
                manifest_data["risk_metric_policy_id"],
                "calendar-365.25-bar-pnl-increment/v1",
            )

    def test_versioned_output_persists_each_cost_diagnostic(self) -> None:
        from aats.cli import _write_outputs
        from tests.unit.replay_contract_fixtures import SPOT_CONTRACT

        fixture_contract = replace(
            SPOT_CONTRACT,
            tick_size=Decimal("0.0001"),
        )
        baseline = _make_stub_result(
            BacktestConfig(
                symbol=fixture_contract.symbol,
                instrument_contract=fixture_contract,
            )
        )
        first_timing, second_timing = baseline.execution_timeline
        filled_timing = replace(
            first_timing,
            decision_id=f"{_BASE_TS.isoformat()}_open",
            action="open",
            submitted_at_ts=first_timing.decision_ts,
            next_tradable_event_ts=second_timing.observation_bar_start_ts,
            resolved_at_ts=second_timing.observation_bar_start_ts,
            fill_ts=second_timing.observation_bar_start_ts,
            status="filled",
            price_source="next_bar_open",
            liquidity_source="observation_bar_volume",
            fill_side="buy",
            decision_intent_exchange_quantity=Decimal("1"),
            requested_exchange_quantity=Decimal("1"),
            liquidity_reference_quantity=Decimal("1000"),
            max_volume_participation=Decimal("0.01"),
            reference_price=Decimal("10000"),
            post_fill_position_quantity=Decimal("1"),
        )
        diagnostic = CostDiagnostic(
            decision_id=filled_timing.decision_id,
            assumed_cost_bps=6.0,
            actual_cost_bps=6.0,
            cost_diff_bps=0.0,
            assumed_net_edge_bps=5.0,
            actual_net_edge_bps=5.0,
            edge_flipped_negative=False,
            actual_fee_bps=5.0,
            actual_slippage_bps=1.0,
            resolved_at_ts_ms=int(
                second_timing.observation_bar_start_ts.timestamp() * 1000
            ),
            fill_ts_ms=int(
                second_timing.observation_bar_start_ts.timestamp() * 1000
            ),
            equity_attribution_ts_ms=baseline.equity_curve[1].ts_ms,
            filled_exchange_quantity=Decimal("1"),
            average_fill_price=Decimal("10001"),
            actual_fee_notional=Decimal("5.0005"),
            fee_currency=SPOT_CONTRACT.settle_currency,
            fee_asset=SPOT_CONTRACT.settle_currency,
            fee_asset_quantity=Decimal("5.0005"),
        )
        equity_curve = list(baseline.equity_curve)
        equity_curve[1] = replace(
            equity_curve[1],
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("5.0005"),
            net_qty=Decimal("1"),
            avg_entry_price=Decimal("10001"),
            mark_price=Decimal("10006.0005"),
            fill_count=1,
            accumulated_fees=Decimal("5.0005"),
        )
        result = replace(
            baseline,
            summary=replace(
                baseline.summary,
                fill_count=1,
                fee_total=Decimal("5.0005"),
            ),
            cost_summary=CostValidationSummary(
                total_decisions=1,
                decisions_with_fills=1,
                stable_sign_count=1,
            ),
            fills_count=1,
            cost_diagnostics=(diagnostic,),
            execution_timeline=(filled_timing, second_timing),
            equity_curve=tuple(equity_curve),
        )

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "filled-run"
            _write_outputs(output_dir, result)
            payload = json.loads(
                (output_dir / "cost_diagnostics.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(payload["diagnostics"], [
                {
                    "decision_id": diagnostic.decision_id,
                    "assumed_cost_bps": 6.0,
                    "actual_cost_bps": 6.0,
                    "cost_diff_bps": 0.0,
                    "assumed_net_edge_bps": 5.0,
                    "actual_net_edge_bps": 5.0,
                    "edge_flipped_negative": False,
                    "notes": "",
                    "actual_fee_bps": 5.0,
                    "actual_slippage_bps": 1.0,
                    "resolved_at_ts_ms": diagnostic.resolved_at_ts_ms,
                    "fill_ts_ms": diagnostic.fill_ts_ms,
                    "equity_attribution_ts_ms": (
                        diagnostic.equity_attribution_ts_ms
                    ),
                    "filled_exchange_quantity": "1",
                    "average_fill_price": "10001",
                    "actual_fee_notional": "5.0005",
                    "fee_currency": SPOT_CONTRACT.settle_currency,
                    "fee_asset": SPOT_CONTRACT.settle_currency,
                    "fee_asset_quantity": "5.0005",
                }
            ])

    def test_invalid_fee_fails_before_output_and_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "must-not-exist"
            with (
                patch("aats.cli._resolve_database_url") as resolve_url,
                patch("aats.cli._build_session") as build_session,
                self.assertRaisesRegex(SystemExit, "taker_fee_bps"),
            ):
                main(
                    [
                        "backtest",
                        "--symbol", "BTC-USDT",
                        "--start", "2026-03-01",
                        "--end", "2026-03-08",
                        "--output-dir", str(output_dir),
                        "--taker-fee-bps", "-1",
                        *_spot_contract_args(),
                    ]
                )

            self.assertFalse(output_dir.exists())
            resolve_url.assert_not_called()
            build_session.assert_not_called()

    def test_legacy_assumed_cost_flag_is_rejected_before_output_and_database(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "must-not-exist"
            with (
                patch("aats.cli._resolve_database_url") as resolve_url,
                patch("aats.cli._build_session") as build_session,
                self.assertRaisesRegex(
                    SystemExit,
                    "legacy_assumed_cost_bps_unsupported_use_param_cost_config",
                ),
            ):
                main(
                    [
                        "backtest",
                        "--symbol",
                        "BTC-USDT",
                        "--start",
                        "2026-03-01",
                        "--end",
                        "2026-03-08",
                        "--output-dir",
                        str(output_dir),
                        "--assumed-cost-bps",
                        "6",
                        *_spot_contract_args(),
                    ]
                )

            self.assertFalse(output_dir.exists())
            resolve_url.assert_not_called()
            build_session.assert_not_called()

    def test_invalid_completed_result_fails_before_any_output_path_is_created(
        self,
    ) -> None:
        from aats.cli import _write_outputs
        from tests.unit.replay_contract_fixtures import SPOT_CONTRACT

        baseline = _make_stub_result(
            BacktestConfig(
                symbol=SPOT_CONTRACT.symbol,
                instrument_contract=SPOT_CONTRACT,
            )
        )
        cases = (
            (
                replace(
                    baseline,
                    config=replace(
                        baseline.config,
                        fill_model_version="legacy",  # type: ignore[arg-type]
                    ),
                ),
                "backtest_fill_model_version_unsupported",
            ),
            (
                replace(
                    baseline,
                    decisions_count=-1,
                    fills_count=42,
                    equity_curve=(),
                    execution_timeline=(),
                    summary=replace(
                        baseline.summary,
                        bar_count=99,
                        fill_count=77,
                        start_ts_ms=123,
                        end_ts_ms=1,
                    ),
                ),
                "backtest_decisions_count_invalid",
            ),
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "not-created"
            for index, (result, reason) in enumerate(cases):
                with (
                    self.subTest(reason=reason),
                    self.assertRaisesRegex(ValueError, reason),
                ):
                    _write_outputs(root / f"case-{index}", result)
            self.assertFalse(root.exists())

    def test_unknown_parameter_fails_before_output_and_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "must-not-exist"
            with (
                patch("aats.cli._resolve_database_url") as resolve_url,
                patch("aats.cli._build_session") as build_session,
                self.assertRaisesRegex(
                    SystemExit,
                    "unconsumed_replay_parameter_keys",
                ),
            ):
                main(
                    [
                        "backtest",
                        "--symbol",
                        "BTC-USDT",
                        "--start",
                        "2026-03-01",
                        "--end",
                        "2026-03-08",
                        "--output-dir",
                        str(output_dir),
                        "--param",
                        "typo_threshold=123",
                        *_spot_contract_args(),
                    ]
                )

            self.assertFalse(output_dir.exists())
            resolve_url.assert_not_called()
            build_session.assert_not_called()

    def test_output_collision_and_nested_scorecard_fail_before_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = root / "existing"
            existing.mkdir()
            cases = (
                (existing, None, "output-dir 必须是尚不存在的新目录"),
                (
                    root / "new-output",
                    root / "new-output" / "manifest.json",
                    "scorecard-out 不得位于 output-dir 内部",
                ),
            )
            for output_dir, scorecard_path, reason in cases:
                args = [
                    "backtest",
                    "--symbol", "BTC-USDT",
                    "--start", "2026-03-01",
                    "--end", "2026-03-08",
                    "--output-dir", str(output_dir),
                    *_spot_contract_args(),
                ]
                if scorecard_path is not None:
                    args.extend(["--scorecard-out", str(scorecard_path)])
                with (
                    patch("aats.cli._resolve_database_url") as resolve_url,
                    patch("aats.cli._build_session") as build_session,
                    self.assertRaisesRegex(SystemExit, reason),
                ):
                    main(args)
                resolve_url.assert_not_called()
                build_session.assert_not_called()

    def test_semantic_fingerprint_ignores_equivalent_decimal_spelling(self) -> None:
        from aats.cli import _semantic_run_fingerprint
        from tests.unit.replay_contract_fixtures import SPOT_CONTRACT

        equivalent_contract = replace(
            SPOT_CONTRACT,
            contract_value=Decimal("1.0"),
            contract_multiplier=Decimal("1.00"),
            lot_size=Decimal("0.00010"),
            min_size=Decimal("0.000100"),
            tick_size=Decimal("0.10"),
        )
        first = _make_stub_result(
            BacktestConfig(
                symbol=SPOT_CONTRACT.symbol,
                instrument_contract=SPOT_CONTRACT,
            )
        )
        second = _make_stub_result(
            BacktestConfig(
                symbol=equivalent_contract.symbol,
                instrument_contract=equivalent_contract,
            )
        )

        self.assertEqual(
            _semantic_run_fingerprint(first),
            _semantic_run_fingerprint(second),
        )

    def test_semantic_fingerprint_canonicalizes_integer_cost_config(self) -> None:
        from aats.cli import _semantic_run_fingerprint
        from tests.unit.replay_contract_fixtures import SPOT_CONTRACT

        integer_config = BacktestConfig(
            symbol=SPOT_CONTRACT.symbol,
            instrument_contract=SPOT_CONTRACT,
            maker_fee_bps=2,
            taker_fee_bps=5,
            ioc_slippage_bps=1,
            spot_buy_fee_asset="quote",
        )
        float_config = BacktestConfig(
            symbol=SPOT_CONTRACT.symbol,
            instrument_contract=SPOT_CONTRACT,
            maker_fee_bps=2.0,
            taker_fee_bps=5.0,
            ioc_slippage_bps=1.0,
            spot_buy_fee_asset="quote",
        )
        negative_zero_config = replace(
            float_config,
            maker_fee_bps=-0.0,
            taker_fee_bps=-0.0,
            ioc_slippage_bps=-0.0,
        )
        zero_config = replace(
            float_config,
            maker_fee_bps=0.0,
            taker_fee_bps=0.0,
            ioc_slippage_bps=0.0,
        )

        self.assertEqual(integer_config, float_config)
        self.assertEqual(
            _semantic_run_fingerprint(_make_stub_result(integer_config)),
            _semantic_run_fingerprint(_make_stub_result(float_config)),
        )
        self.assertEqual(negative_zero_config, zero_config)
        self.assertEqual(
            _semantic_run_fingerprint(_make_stub_result(negative_zero_config)),
            _semantic_run_fingerprint(_make_stub_result(zero_config)),
        )

    def test_semantic_fingerprint_canonicalizes_timeframe_spelling(self) -> None:
        from aats.cli import _semantic_run_fingerprint
        from tests.unit.replay_contract_fixtures import SPOT_CONTRACT

        canonical = BacktestConfig(
            symbol=SPOT_CONTRACT.symbol,
            instrument_contract=SPOT_CONTRACT,
            timeframe="1h",
        )
        uppercase = replace(canonical, timeframe="1H")
        padded = replace(canonical, timeframe=" 1h ")

        self.assertEqual(uppercase.timeframe, "1h")
        self.assertEqual(padded.timeframe, "1h")
        self.assertEqual(
            _semantic_run_fingerprint(_make_stub_result(canonical)),
            _semantic_run_fingerprint(_make_stub_result(uppercase)),
        )
        self.assertEqual(
            _semantic_run_fingerprint(_make_stub_result(canonical)),
            _semantic_run_fingerprint(_make_stub_result(padded)),
        )
        with self.assertRaisesRegex(ValueError, "Unsupported replay timeframe"):
            replace(canonical, timeframe="   ")

    def test_semantic_fingerprint_canonicalizes_diagnostic_signed_zero(
        self,
    ) -> None:
        from aats.cli import _semantic_run_fingerprint
        from tests.unit.replay_contract_fixtures import SPOT_CONTRACT

        baseline = _make_stub_result(
            BacktestConfig(
                symbol=SPOT_CONTRACT.symbol,
                instrument_contract=SPOT_CONTRACT,
            )
        )
        negative_zero = CostDiagnostic(
            decision_id="zero",
            assumed_cost_bps=-0.0,
            actual_cost_bps=-0.0,
            cost_diff_bps=-0.0,
            assumed_net_edge_bps=-0.0,
            actual_net_edge_bps=-0.0,
            edge_flipped_negative=False,
            actual_fee_bps=-0.0,
            actual_slippage_bps=-0.0,
        )
        positive_zero = replace(
            negative_zero,
            assumed_cost_bps=0.0,
            actual_cost_bps=0.0,
            cost_diff_bps=0.0,
            assumed_net_edge_bps=0.0,
            actual_net_edge_bps=0.0,
            actual_fee_bps=0.0,
            actual_slippage_bps=0.0,
        )

        self.assertEqual(negative_zero, positive_zero)
        self.assertEqual(
            _semantic_run_fingerprint(
                replace(baseline, cost_diagnostics=(negative_zero,))
            ),
            _semantic_run_fingerprint(
                replace(baseline, cost_diagnostics=(positive_zero,))
            ),
        )

    def test_semantic_fingerprint_canonicalizes_summary_signed_zero(self) -> None:
        from aats.cli import _semantic_run_fingerprint
        from tests.unit.replay_contract_fixtures import SPOT_CONTRACT

        baseline = _make_stub_result(
            BacktestConfig(
                symbol=SPOT_CONTRACT.symbol,
                instrument_contract=SPOT_CONTRACT,
            )
        )
        negative_zero = replace(
            baseline,
            summary=replace(baseline.summary, sharpe_ratio=-0.0),
            cost_summary=replace(
                baseline.cost_summary,
                avg_cost_diff_bps=-0.0,
                max_cost_diff_bps=-0.0,
                p50_cost_diff_bps=-0.0,
                p95_cost_diff_bps=-0.0,
            ),
        )

        self.assertEqual(repr(negative_zero.summary.sharpe_ratio), "0.0")
        for field_name in (
            "avg_cost_diff_bps",
            "max_cost_diff_bps",
            "p50_cost_diff_bps",
            "p95_cost_diff_bps",
        ):
            self.assertEqual(
                repr(getattr(negative_zero.cost_summary, field_name)),
                "0.0",
            )
        self.assertEqual(
            _semantic_run_fingerprint(baseline),
            _semantic_run_fingerprint(negative_zero),
        )

    def test_semantic_fingerprint_canonicalizes_replay_parameter_numbers(self) -> None:
        from aats.cli import _semantic_run_fingerprint
        from tests.unit.replay_contract_fixtures import SPOT_CONTRACT

        baseline = _make_stub_result(
            BacktestConfig(
                symbol=SPOT_CONTRACT.symbol,
                instrument_contract=SPOT_CONTRACT,
            )
        )
        integer_parameters = replace(
            baseline.resolved_parameters,
            min_hold_seconds=300,
            rebalance_cooldown_seconds=120,
            max_thesis_age_seconds=1800,
        )
        float_parameters = replace(
            baseline.resolved_parameters,
            min_hold_seconds=300.0,
            rebalance_cooldown_seconds=120.0,
            max_thesis_age_seconds=1800.0,
        )
        negative_zero_parameters = replace(
            baseline.resolved_parameters,
            rebalance_cooldown_seconds=-0.0,
        )
        zero_parameters = replace(
            baseline.resolved_parameters,
            rebalance_cooldown_seconds=0.0,
        )

        self.assertEqual(integer_parameters, float_parameters)
        self.assertIs(type(integer_parameters.min_hold_seconds), float)
        self.assertEqual(
            _semantic_run_fingerprint(
                replace(baseline, resolved_parameters=integer_parameters)
            ),
            _semantic_run_fingerprint(
                replace(baseline, resolved_parameters=float_parameters)
            ),
        )
        self.assertEqual(negative_zero_parameters, zero_parameters)
        self.assertEqual(
            _semantic_run_fingerprint(
                replace(
                    baseline,
                    resolved_parameters=negative_zero_parameters,
                )
            ),
            _semantic_run_fingerprint(
                replace(baseline, resolved_parameters=zero_parameters)
            ),
        )

    def test_semantic_fingerprint_binds_parameters_and_adapter_version(self) -> None:
        from aats.cli import _semantic_run_fingerprint
        from tests.unit.replay_contract_fixtures import SPOT_CONTRACT

        baseline = _make_stub_result(
            BacktestConfig(
                symbol=SPOT_CONTRACT.symbol,
                instrument_contract=SPOT_CONTRACT,
            )
        )
        changed_parameters = replace(
            baseline,
            resolved_parameters=replace(
                baseline.resolved_parameters,
                entry_threshold=0.31,
            ),
        )
        changed_adapter = replace(
            baseline,
            adapter_algorithm_version="independent-replay/v3",
        )

        self.assertNotEqual(
            _semantic_run_fingerprint(baseline),
            _semantic_run_fingerprint(changed_parameters),
        )
        self.assertNotEqual(
            _semantic_run_fingerprint(baseline),
            _semantic_run_fingerprint(changed_adapter),
        )


# ---------------------------------------------------------------------------
# --param parsing
# ---------------------------------------------------------------------------


class TestCLIParamParsing(unittest.TestCase):
    def test_cli_param_parses_json_and_string(self) -> None:
        """--param KEY=VALUE 支持 JSON 解析与 string fallback。"""
        from aats.cli import _parse_param_overrides

        out = _parse_param_overrides(
            [
                "entry_threshold=0.55",
                "min_confirm_ticks=3",
                "note=hello world",
            ]
        )
        self.assertEqual(out["entry_threshold"], 0.55)
        self.assertEqual(out["min_confirm_ticks"], 3)
        self.assertEqual(out["note"], "hello world")

    def test_cli_param_malformed_raises(self) -> None:
        from aats.cli import _parse_param_overrides

        with self.assertRaises(SystemExit):
            _parse_param_overrides(["no_equals_sign"])


if __name__ == "__main__":
    unittest.main()
