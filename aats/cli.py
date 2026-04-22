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
* 运行结果写 3 个文件 (``summary.json`` / ``equity_curve.csv`` /
  ``cost_validation.json``) 到 ``--output-dir``。
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

from aats.data_platform.replay.backtest.harness import (
    BacktestConfig,
    BacktestResult,
    run_backtest,
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
        help="Directory where summary.json / equity_curve.csv / cost_validation.json "
             "will be written.",
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
        "--assumed-cost-bps",
        type=float,
        default=6.0,
        help="Decision-side assumed cost for CostValidator (bps).",
    )
    p.set_defaults(func=_run_backtest_cmd)


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
    engine = create_engine(url, pool_pre_ping=True, future=True)
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
    """Write summary.json / equity_curve.csv / cost_validation.json."""
    summary_path = output_dir / "summary.json"
    equity_path = output_dir / "equity_curve.csv"
    cost_path = output_dir / "cost_validation.json"

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
