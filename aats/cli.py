"""Top-level CLI entry for AATS developer utilities.

Usage::

    python -m aats.cli backtest \
        --symbol BTC-USDT-SWAP \
        --timeframe 1h \
        --start 2026-03-01 \
        --end 2026-03-08 \
        --family independent \
        --dataset-version v1.0 \
        --rdp-db-name aats_research \
        --output-dir ./backtest_results

本模块只暴露开发/研究用的 CLI 工具，绝不被 live service 进程引用。

Design notes
------------
* DB URL 通过 Pydantic ``ResearchPlatformSettings`` 获取，**绝不**读取
  ``.env.*`` 文件原文 —— settings 内部已经处理 env 合并。
* ``--rdp-db-name`` 允许覆盖 URL 末端数据库名（rsplit 替换），便于把
  同一份 Postgres 凭证切到不同研究库上。
* 运行结果写 4 个文件 (``summary.json`` / ``equity_curve.csv`` /
  ``cost_validation.json`` / ``execution_timeline.json``) 到 ``--output-dir``。
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from aats.data_platform.replay.backtest.evidence_scorecard import build_scorecard
from aats.data_platform.replay.backtest.harness import (
    BacktestConfig,
    BacktestResult,
    run_backtest,
)
from aats.data_platform.replay.backtest.route_a_evidence_scaffold import (
    DEFAULT_OUTPUT_ROOT as _DEFAULT_SCAFFOLD_OUTPUT_ROOT,
    ScaffoldError,
    ScaffoldInputs,
    create_scaffold,
)

log = logging.getLogger("aats.cli.backtest")


# ---------------------------------------------------------------------------
# Argparse wiring
# ---------------------------------------------------------------------------


def _add_backtest_parser(subparsers: argparse._SubParsersAction) -> None:
    p: argparse.ArgumentParser = subparsers.add_parser(
        "backtest",
        help="Run end-to-end backtest over Gold replay bars (Phase 3 MVP).",
        description=(
            "End-to-end backtest harness: loads Gold bars, runs adapter → "
            "fill simulator → position tracker → equity curve → cost validator."
        ),
    )
    p.add_argument("--symbol", default="BTC-USDT-SWAP", help="Trading symbol.")
    p.add_argument("--timeframe", default="1h", help="Candle timeframe, e.g. 1h/15m.")
    p.add_argument(
        "--start",
        required=True,
        help="Start timestamp (inclusive). ISO-8601 date or datetime, UTC assumed.",
    )
    p.add_argument(
        "--end",
        required=True,
        help="End timestamp (exclusive). ISO-8601 date or datetime, UTC assumed.",
    )
    p.add_argument("--family", default="independent", help="Strategy family.")
    p.add_argument(
        "--dataset-version",
        default="v1.0",
        help="Gold dataset version filter.",
    )
    p.add_argument(
        "--order-type",
        default="ioc",
        choices=["ioc", "post_only", "bounded_limit"],
        help="Fill simulator order type.",
    )
    p.add_argument(
        "--rdp-db-name",
        default=None,
        help=(
            "Override the database name inside the RDP URL (last path segment). "
            "The rest of the DSN (host/user/password) is resolved from Pydantic "
            "settings, never read from disk directly."
        ),
    )
    p.add_argument(
        "--output-dir",
        required=True,
        help=(
            "Directory where summary.json / equity_curve.csv / "
            "cost_validation.json / execution_timeline.json will be written."
        ),
    )
    p.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Override adapter parameter; can be repeated. Values parsed as JSON "
             "when possible, else str.",
    )
    p.add_argument(
        "--contract-multiplier",
        default="0.01",
        help="Contract multiplier (Decimal) used by PositionTracker.",
    )
    p.add_argument(
        "--maker-fee-bps",
        type=float,
        default=2.0,
        help="FillSimulator maker fee (bps).",
    )
    p.add_argument(
        "--taker-fee-bps",
        type=float,
        default=5.0,
        help="FillSimulator taker fee (bps).",
    )
    p.add_argument(
        "--ioc-slippage-bps",
        type=float,
        default=1.0,
        help="FillSimulator IOC slippage (bps).",
    )
    p.add_argument(
        "--max-volume-participation",
        type=Decimal,
        default=Decimal("0.01"),
        help=(
            "Maximum fraction of the causal OHLCV bar volume fillable by one "
            "order; Decimal in (0, 1], default 0.01."
        ),
    )
    p.add_argument(
        "--assumed-cost-bps",
        type=float,
        default=6.0,
        help="Decision-side assumed cost for CostValidator (bps).",
    )
    p.add_argument(
        "--scorecard-out",
        default=None,
        help=(
            "Optional path to write an evidence scorecard JSON "
            "(see docs/governance/alpha_evidence_gate.md). "
            "Outputs numeric stats only; no verdict fields."
        ),
    )
    p.add_argument(
        "--scorecard-split-ts",
        default=None,
        help=(
            "Optional ISO-8601 UTC timestamp used as the OOS train/test split "
            "boundary. When provided, scorecard.oos.split_method == 'explicit'; "
            "otherwise falls back to time midpoint."
        ),
    )
    p.set_defaults(func=_run_backtest_cmd)


def _add_route_a_scaffold_parser(subparsers: argparse._SubParsersAction) -> None:
    p: argparse.ArgumentParser = subparsers.add_parser(
        "route-a-evidence-scaffold",
        help=(
            "Scaffold a Route A phase 0 evidence bundle directory from existing "
            "scorecard + observation-window JSONs."
        ),
        description=(
            "本地 research/governance 工具：把已有 scorecard JSON 和 observation-"
            "window JSON summary 拼成 evidence bundle 骨架，落在 "
            "<output-root>/<proposal-id>/ 下。不输出 verdict / go-no-go / archive "
            "判定，也不触及 live runtime / configs / deploy。"
        ),
    )
    p.add_argument("--proposal-id", required=True, help="Proposal identifier.")
    p.add_argument(
        "--feature", required=True, help="Feature name (e.g. OFI / TFI)."
    )
    p.add_argument(
        "--horizon", required=True, help="Horizon label (e.g. 5s / 15min)."
    )
    p.add_argument(
        "--scorecard-json",
        required=True,
        help="Path to an existing backtest evidence scorecard JSON.",
    )
    p.add_argument(
        "--observation-window-json",
        required=True,
        help="Path to an existing observation-window daily-check JSON summary.",
    )
    p.add_argument(
        "--proposer",
        default=None,
        help="Optional proposer name; written into proposal.md metadata.",
    )
    p.add_argument(
        "--output-root",
        default=str(_DEFAULT_SCAFFOLD_OUTPUT_ROOT),
        help=(
            "Directory under which <proposal-id>/ is created "
            f"(default: {_DEFAULT_SCAFFOLD_OUTPUT_ROOT})."
        ),
    )
    p.set_defaults(func=_run_route_a_scaffold_cmd)


def _run_route_a_scaffold_cmd(args: argparse.Namespace) -> int:
    inputs = ScaffoldInputs(
        proposal_id=args.proposal_id,
        feature=args.feature,
        horizon=args.horizon,
        scorecard_json=Path(args.scorecard_json),
        observation_window_json=Path(args.observation_window_json),
        proposer=args.proposer,
        output_root=Path(args.output_root),
    )
    try:
        result = create_scaffold(inputs)
    except ScaffoldError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Route A evidence bundle scaffold created: {result.proposal_dir}")
    print(f"  manifest : {result.manifest_path}")
    print(f"  scorecard: {result.scorecard_path}")
    print(f"  observation_window: {result.observation_window_summary_path}")
    print(f"  proposal.md: {result.proposal_md_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argparse parser.

    Exposed separately so unit tests can call ``parse_args(...)`` without
    hitting the ``main()`` side effects.
    """
    parser = argparse.ArgumentParser(
        prog="aats.cli",
        description="AATS developer CLI (research tools).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    _add_backtest_parser(subparsers)
    _add_route_a_scaffold_parser(subparsers)
    return parser


# ---------------------------------------------------------------------------
# Backtest command implementation
# ---------------------------------------------------------------------------


def _parse_iso(value: str, *, field: str) -> datetime:
    """Parse ISO-8601 date/datetime; naive values are treated as UTC."""
    try:
        # Python <3.11 fromisoformat doesn't accept Z suffix; normalize first.
        cleaned = value.replace("Z", "+00:00")
        ts = datetime.fromisoformat(cleaned)
    except ValueError as exc:
        raise SystemExit(f"--{field} 必须是 ISO-8601 格式: {value!r} ({exc})") from exc
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _parse_param_overrides(raw: list[str]) -> dict[str, Any]:
    """Parse ``--param key=value`` pairs into a dict.

    Values try JSON first (so ``foo=3.14`` / ``foo=true`` / ``foo=[1,2]``
    work), then fall back to string.
    """
    out: dict[str, Any] = {}
    for item in raw:
        if "=" not in item:
            raise SystemExit(f"--param 必须是 key=value 形式: {item!r}")
        key, _, value = item.partition("=")
        key = key.strip()
        value = value.strip()
        try:
            out[key] = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            out[key] = value
    return out


def _resolve_database_url(rdp_db_name: str | None) -> str:
    """Get DB URL from Pydantic settings, optionally swapping the DB name.

    ``.env.*`` 文件绝不直接读取 —— ``ResearchPlatformSettings`` 内部会去消化
    env 变量。密码只留在进程内存，不 print / log。
    """
    # Import deferred so unit tests can monkeypatch without side effects.
    from aats.data_platform.config import get_settings

    settings = get_settings()
    url: str = settings.database_url
    if rdp_db_name:
        # DSN 格式: postgresql+psycopg://user:pass@host:port/dbname?...
        # 安全做法：只替换最后一个 "/" 后的 path segment，保留 query string。
        base, _, tail = url.rpartition("/")
        if not base:
            raise SystemExit(
                "RDP database_url 格式异常（无 '/' 分隔 dbname），无法安全切换。"
            )
        # 保留 query string（如果有）
        if "?" in tail:
            _, _, query = tail.partition("?")
            new_tail = f"{rdp_db_name}?{query}"
        else:
            new_tail = rdp_db_name
        url = f"{base}/{new_tail}"
    return url


def _build_session(url: str) -> Session:
    """Build a short-lived SQLAlchemy session bound to ``url``."""
    engine = create_engine(url, pool_pre_ping=True, future=True, poolclass=NullPool)
    maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    return maker()


def _run_backtest_cmd(args: argparse.Namespace) -> int:
    """Top-level handler for the ``backtest`` subcommand."""
    start_ts = _parse_iso(args.start, field="start")
    end_ts = _parse_iso(args.end, field="end")
    if end_ts <= start_ts:
        raise SystemExit("--end 必须晚于 --start")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    param_overrides = _parse_param_overrides(args.param)

    config = BacktestConfig(
        symbol=args.symbol,
        timeframe=args.timeframe,
        dataset_version=args.dataset_version,
        family=args.family,
        order_type=args.order_type,
        contract_multiplier=Decimal(args.contract_multiplier),
        maker_fee_bps=args.maker_fee_bps,
        taker_fee_bps=args.taker_fee_bps,
        ioc_slippage_bps=args.ioc_slippage_bps,
        max_volume_participation=args.max_volume_participation,
        assumed_cost_bps=args.assumed_cost_bps,
    )

    url = _resolve_database_url(args.rdp_db_name)
    session = _build_session(url)
    try:
        result = run_backtest(
            session,
            config=config,
            start_ts=start_ts,
            end_ts=end_ts,
            parameter_overrides=param_overrides or None,
        )
    finally:
        session.close()

    _write_outputs(output_dir, result)
    if args.scorecard_out:
        split_ts = (
            _parse_iso(args.scorecard_split_ts, field="scorecard-split-ts")
            if args.scorecard_split_ts
            else None
        )
        _write_scorecard(Path(args.scorecard_out), result, split_ts=split_ts)
    log.info(
        "Backtest done: %d bars / %d fills, final_equity=%s",
        result.summary.bar_count,
        result.fills_count,
        result.summary.final_equity,
    )
    return 0


# ---------------------------------------------------------------------------
# Output serialization
# ---------------------------------------------------------------------------


class _DecimalJSONEncoder(json.JSONEncoder):
    """Serialize Decimal as str to keep full precision and avoid float rounding."""

    def default(self, o: Any) -> Any:
        if isinstance(o, Decimal):
            return str(o)
        if isinstance(o, datetime):
            return o.isoformat()
        if is_dataclass(o) and not isinstance(o, type):
            return asdict(o)
        return super().default(o)


def _dataclass_to_dict(obj: Any) -> dict[str, Any]:
    """Wrap asdict + Decimal coercion for frozen dataclasses."""
    return json.loads(json.dumps(asdict(obj), cls=_DecimalJSONEncoder))


def _write_outputs(output_dir: Path, result: BacktestResult) -> None:
    """Write summary/equity/cost and causal execution-timeline artifacts."""
    summary_path = output_dir / "summary.json"
    equity_path = output_dir / "equity_curve.csv"
    cost_path = output_dir / "cost_validation.json"
    timeline_path = output_dir / "execution_timeline.json"

    # summary.json := BacktestSummary + config + run window + counts
    summary_payload = {
        "config": _dataclass_to_dict(result.config),
        "summary": _dataclass_to_dict(result.summary),
        "decisions_count": result.decisions_count,
        "fills_count": result.fills_count,
        "start_ts": result.start_ts.isoformat(),
        "end_ts": result.end_ts.isoformat(),
    }
    summary_path.write_text(
        json.dumps(summary_payload, indent=2, cls=_DecimalJSONEncoder),
        encoding="utf-8",
    )

    # equity_curve.csv
    with equity_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["ts_ms", "equity", "cumulative_pnl", "drawdown_bps", "daily_return_bps"]
        )
        for pt in result.equity_curve:
            writer.writerow(
                [
                    pt.ts_ms,
                    str(pt.equity),
                    str(pt.cumulative_pnl),
                    str(pt.drawdown_bps),
                    str(pt.daily_return_bps),
                ]
            )

    # cost_validation.json
    cost_path.write_text(
        json.dumps(_dataclass_to_dict(result.cost_summary), indent=2),
        encoding="utf-8",
    )

    # FS-003: 时间契约必须随研究产物持久化，不能只靠进程内对象或日志证明。
    timeline_path.write_text(
        json.dumps(
            [_dataclass_to_dict(record) for record in result.execution_timeline],
            indent=2,
            cls=_DecimalJSONEncoder,
        ),
        encoding="utf-8",
    )


def _write_scorecard(
    path: Path,
    result: BacktestResult,
    *,
    split_ts: datetime | None = None,
) -> None:
    """Serialize the evidence scorecard to a JSON file (numeric-only)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    scorecard = build_scorecard(result, split_ts=split_ts)
    path.write_text(
        json.dumps(scorecard, indent=2, cls=_DecimalJSONEncoder),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns process exit code."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 2
    return func(args)


if __name__ == "__main__":
    sys.exit(main())
