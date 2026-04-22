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
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from aats.cli import build_parser, main
from aats.data_platform.replay.backtest.cost_validator import CostValidationSummary
from aats.data_platform.replay.backtest.equity_builder import (
    BacktestSummary,
    EquityPoint,
)
from aats.data_platform.replay.backtest.harness import (
    BacktestConfig,
    BacktestResult,
)


_BASE_TS = datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc)


def _make_stub_result(config: BacktestConfig) -> BacktestResult:
    """Construct a deterministic BacktestResult for output serialization tests."""
    curve = (
        EquityPoint(
            ts_ms=0,
            equity=Decimal("0"),
            cumulative_pnl=Decimal("0"),
            drawdown_bps=Decimal("0"),
            daily_return_bps=Decimal("0"),
        ),
        EquityPoint(
            ts_ms=3_600_000,
            equity=Decimal("12.5"),
            cumulative_pnl=Decimal("12.5"),
            drawdown_bps=Decimal("0"),
            daily_return_bps=Decimal("0"),
        ),
    )
    summary = BacktestSummary(
        initial_equity=Decimal("0"),
        final_equity=Decimal("12.5"),
        cumulative_pnl=Decimal("12.5"),
        max_drawdown_bps=Decimal("0"),
        sharpe_ratio=0.0,
        fill_count=2,
        fee_total=Decimal("0.5"),
        bar_count=2,
        start_ts_ms=0,
        end_ts_ms=3_600_000,
    )
    cost_summary = CostValidationSummary(
        total_decisions=2,
        decisions_with_fills=2,
        avg_cost_diff_bps=-1.0,
    )
    return BacktestResult(
        config=config,
        summary=summary,
        cost_summary=cost_summary,
        equity_curve=curve,
        decisions_count=2,
        fills_count=2,
        start_ts=_BASE_TS,
        end_ts=_BASE_TS + timedelta(days=1),
    )


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


class TestCLIArgparse(unittest.TestCase):
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


# ---------------------------------------------------------------------------
# Output files end-to-end
# ---------------------------------------------------------------------------


class TestCLIOutputFiles(unittest.TestCase):
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
                        str(output_dir),
                    ]
                )
            self.assertEqual(rc, 0)

            # 3 files must exist
            summary_path = output_dir / "summary.json"
            equity_path = output_dir / "equity_curve.csv"
            cost_path = output_dir / "cost_validation.json"
            self.assertTrue(summary_path.exists())
            self.assertTrue(equity_path.exists())
            self.assertTrue(cost_path.exists())

            # summary.json parseable
            summary_data = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertIn("config", summary_data)
            self.assertIn("summary", summary_data)
            self.assertIn("decisions_count", summary_data)
            self.assertEqual(summary_data["decisions_count"], 2)
            self.assertEqual(summary_data["fills_count"], 2)
            # Decimal serialized as string
            self.assertEqual(summary_data["summary"]["final_equity"], "12.5")

            # equity_curve.csv has header + rows
            csv_lines = equity_path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(csv_lines), 3)  # header + 2 points
            self.assertIn("ts_ms", csv_lines[0])

            # cost_validation.json parseable
            cost_data = json.loads(cost_path.read_text(encoding="utf-8"))
            self.assertEqual(cost_data["total_decisions"], 2)
            self.assertEqual(cost_data["decisions_with_fills"], 2)


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
