"""Top-level CLI entry for AATS developer utilities.

Usage::

    python -m aats.cli backtest \
        --symbol BTC-USDT \
        --timeframe 1h \
        --start 2026-03-01 \
        --end 2026-03-08 \
        --family independent \
        --dataset-version v1.0 \
        --instrument-type SPOT \
        --contract-type spot \
        --base-currency BTC --quote-currency USDT --settle-currency USDT \
        --contract-value 1 --contract-multiplier 1 \
        --contract-value-currency BTC \
        --lot-size 0.0001 --min-size 0.0001 --tick-size 0.1 \
        --param strategy_short_bias_enabled=false \
        --rdp-db-name aats_research \
        --output-dir ./backtest_results

本模块只暴露开发/研究用的 CLI 工具，绝不被 live service 进程引用。

Design notes
------------
* DB URL 通过 Pydantic ``ResearchPlatformSettings`` 获取，**绝不**读取
  ``.env.*`` 文件原文 —— settings 内部已经处理 env 合并。
* ``--rdp-db-name`` 允许覆盖 URL 末端数据库名（rsplit 替换），便于把
  同一份 Postgres 凭证切到不同研究库上。
* 运行结果把 5 个 payload (``summary.json`` / ``equity_curve.csv`` /
  ``cost_validation.json`` / ``cost_diagnostics.json`` /
  ``execution_timeline.json``) 与最后发布的 ``manifest.json`` 一起写入
  全新的 ``--output-dir``。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from aats.data_platform.replay.backtest.evidence_scorecard import build_scorecard
from aats.data_platform.replay.backtest.equity_builder import (
    REPLAY_RISK_METRIC_POLICY_ID,
)
from aats.data_platform.replay.backtest.harness import (
    BACKTEST_ARTIFACT_SCHEMA_VERSION,
    BacktestConfig,
    BacktestResult,
    run_backtest,
    validate_backtest_request,
    validate_backtest_result_units,
)
from aats.data_platform.replay.backtest.route_a_evidence_scaffold import (
    DEFAULT_OUTPUT_ROOT as _DEFAULT_SCAFFOLD_OUTPUT_ROOT,
    ScaffoldError,
    ScaffoldInputs,
    create_scaffold,
)
from aats.domain.instrument_contract import (
    INSTRUMENT_ARITHMETIC_POLICY_ID,
    InstrumentContract,
    InstrumentContractError,
    canonical_decimal_identity,
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
    p.add_argument(
        "--symbol",
        required=True,
        help="Trading symbol; must match the explicit instrument contract.",
    )
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
            "cost_validation.json / cost_diagnostics.json / "
            "execution_timeline.json will be written."
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
        "--instrument-type",
        required=True,
        choices=["SPOT", "MARGIN", "SWAP", "FUTURES"],
        help="Explicit exchange instrument type.",
    )
    p.add_argument(
        "--contract-type",
        required=True,
        choices=["spot", "linear", "inverse"],
        help="Explicit financial contract type.",
    )
    p.add_argument("--base-currency", required=True)
    p.add_argument("--quote-currency", required=True)
    p.add_argument("--settle-currency", required=True)
    p.add_argument("--contract-value", required=True, type=Decimal)
    p.add_argument("--contract-multiplier", required=True, type=Decimal)
    p.add_argument("--contract-value-currency", required=True)
    p.add_argument("--lot-size", required=True, type=Decimal)
    p.add_argument("--min-size", required=True, type=Decimal)
    p.add_argument("--tick-size", required=True, type=Decimal)
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
        "--spot-buy-fee-asset",
        choices=["base", "quote"],
        required=True,
        help=(
            "Explicit asset charged for simulated SPOT buy fees. "
            "'quote' charges settlement inventory; 'base' deducts "
            "the fee from acquired base inventory."
        ),
    )
    p.add_argument(
        "--assumed-cost-bps",
        type=float,
        default=None,
        help=(
            "Deprecated and rejected because it had no unambiguous consumer; "
            "use --param taker_fee_bps=..., --param maker_fee_bps=... and "
            "--param slippage_bps=... for the decision cost contract."
        ),
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
    """Parse ISO-8601 and normalize the public CLI boundary to UTC."""
    try:
        # Python <3.11 fromisoformat doesn't accept Z suffix; normalize first.
        cleaned = value.replace("Z", "+00:00")
        ts = datetime.fromisoformat(cleaned)
    except ValueError as exc:
        raise SystemExit(f"--{field} 必须是 ISO-8601 格式: {value!r} ({exc})") from exc
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


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

    try:
        instrument_contract = InstrumentContract(
            symbol=args.symbol,
            instrument_type=args.instrument_type,
            contract_type=args.contract_type,
            base_currency=args.base_currency,
            quote_currency=args.quote_currency,
            settle_currency=args.settle_currency,
            contract_value=args.contract_value,
            contract_multiplier=args.contract_multiplier,
            contract_value_currency=args.contract_value_currency,
            lot_size=args.lot_size,
            min_size=args.min_size,
            tick_size=args.tick_size,
        )
    except (InstrumentContractError, ValueError) as exc:
        raise SystemExit(f"instrument contract 无效: {exc}") from exc
    param_overrides = _parse_param_overrides(args.param)

    config = BacktestConfig(
        symbol=args.symbol,
        instrument_contract=instrument_contract,
        timeframe=args.timeframe,
        dataset_version=args.dataset_version,
        family=args.family,
        order_type=args.order_type,
        maker_fee_bps=args.maker_fee_bps,
        taker_fee_bps=args.taker_fee_bps,
        ioc_slippage_bps=args.ioc_slippage_bps,
        max_volume_participation=args.max_volume_participation,
        spot_buy_fee_asset=args.spot_buy_fee_asset,
        assumed_cost_bps=args.assumed_cost_bps,
    )

    split_ts = (
        _parse_iso(args.scorecard_split_ts, field="scorecard-split-ts")
        if args.scorecard_split_ts
        else None
    )
    try:
        validate_backtest_request(
            config=config,
            start_ts=start_ts,
            end_ts=end_ts,
            parameter_overrides=param_overrides or None,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    output_dir = Path(args.output_dir)
    if output_dir.exists():
        raise SystemExit(f"output-dir 必须是尚不存在的新目录: {output_dir}")
    scorecard_path = Path(args.scorecard_out) if args.scorecard_out else None
    if scorecard_path is not None and scorecard_path.exists():
        raise SystemExit(f"scorecard-out 已存在，拒绝覆盖: {scorecard_path}")
    if scorecard_path is not None:
        resolved_output = output_dir.resolve(strict=False)
        resolved_scorecard = scorecard_path.resolve(strict=False)
        if (
            resolved_scorecard == resolved_output
            or resolved_output in resolved_scorecard.parents
        ):
            raise SystemExit("scorecard-out 不得位于 output-dir 内部")

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
    if scorecard_path is not None:
        _write_scorecard(scorecard_path, result, split_ts=split_ts)
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
    return json.loads(
        json.dumps(
            asdict(obj),
            cls=_DecimalJSONEncoder,
            allow_nan=False,
        )
    )


def _semantic_value(value: Any) -> Any:
    """Canonicalize replay semantics independently from lexical Decimal input."""

    if isinstance(value, Decimal):
        return canonical_decimal_identity(value)
    if isinstance(value, datetime):
        aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return aware.astimezone(timezone.utc).isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return _semantic_value(asdict(value))
    if isinstance(value, dict):
        return {str(key): _semantic_value(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_semantic_value(child) for child in value]
    return value


def _semantic_run_fingerprint(result: BacktestResult) -> str:
    payload = _semantic_value(
        {
            "artifact_schema_version": BACKTEST_ARTIFACT_SCHEMA_VERSION,
            "config": result.config,
            "resolved_parameters": result.resolved_parameters,
            "adapter_identity": result.adapter_identity,
            "adapter_algorithm_version": result.adapter_algorithm_version,
            "summary": result.summary,
            "cost_summary": result.cost_summary,
            "equity_curve": result.equity_curve,
            "decisions_count": result.decisions_count,
            "fills_count": result.fills_count,
            "start_ts": result.start_ts,
            "end_ts": result.end_ts,
            "cost_diagnostics": result.cost_diagnostics,
            "execution_timeline": result.execution_timeline,
            "cadence_gap_count": result.cadence_gap_count,
        }
    )
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_outputs(output_dir: Path, result: BacktestResult) -> None:
    """Atomically publish one complete, manifest-last backtest run directory."""

    contract = validate_backtest_result_units(
        result,
        require_complete_artifact=True,
    )
    if output_dir.exists():
        raise FileExistsError(f"backtest output directory already exists: {output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(
        tempfile.mkdtemp(
            prefix=f".{output_dir.name}.staging-",
            dir=output_dir.parent,
        )
    )
    try:
        _write_output_payloads(staging_dir, result)
        artifact_names = (
            "summary.json",
            "equity_curve.csv",
            "cost_validation.json",
            "cost_diagnostics.json",
            "execution_timeline.json",
        )
        artifact_sha256 = {
            name: _sha256_file(staging_dir / name) for name in artifact_names
        }
        fingerprint_payload = {
            "artifact_schema_version": BACKTEST_ARTIFACT_SCHEMA_VERSION,
            "artifact_sha256": artifact_sha256,
        }
        artifact_set_fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_payload,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        manifest = {
            "artifact_kind": "backtest_run_manifest",
            "artifact_schema_version": BACKTEST_ARTIFACT_SCHEMA_VERSION,
            "complete": True,
            "run_fingerprint": _semantic_run_fingerprint(result),
            "artifact_set_fingerprint": artifact_set_fingerprint,
            "instrument_arithmetic_policy_id": INSTRUMENT_ARITHMETIC_POLICY_ID,
            "fill_model_version": result.config.fill_model_version,
            "contract_lineage_status": "calculation_contract_only_unverified",
            "settlement_currency": contract.settle_currency,
            "instrument_symbol": contract.symbol,
            "instrument_contract_fingerprint": contract.fingerprint,
            "instrument_contract": _dataclass_to_dict(contract),
            "resolved_parameters": _dataclass_to_dict(
                result.resolved_parameters
            ),
            "adapter_identity": result.adapter_identity,
            "adapter_algorithm_version": result.adapter_algorithm_version,
            "cadence_gap_count": result.cadence_gap_count,
            "risk_metric_policy_id": REPLAY_RISK_METRIC_POLICY_ID,
            "artifact_sha256": artifact_sha256,
        }
        (staging_dir / "manifest.json").write_text(
            json.dumps(
                manifest,
                indent=2,
                ensure_ascii=False,
                allow_nan=False,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(staging_dir, output_dir)
    except Exception:
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        raise


def _write_output_payloads(output_dir: Path, result: BacktestResult) -> None:
    """Write payload files into a private staging directory."""
    summary_path = output_dir / "summary.json"
    equity_path = output_dir / "equity_curve.csv"
    cost_path = output_dir / "cost_validation.json"
    cost_diagnostics_path = output_dir / "cost_diagnostics.json"
    timeline_path = output_dir / "execution_timeline.json"

    # summary.json := BacktestSummary + config + run window + counts
    summary_payload = {
        "artifact_schema_version": BACKTEST_ARTIFACT_SCHEMA_VERSION,
        "artifact_kind": "backtest_run_summary",
        "config": _dataclass_to_dict(result.config),
        "resolved_parameters": _dataclass_to_dict(result.resolved_parameters),
        "adapter_identity": result.adapter_identity,
        "adapter_algorithm_version": result.adapter_algorithm_version,
        "cadence_gap_count": result.cadence_gap_count,
        "summary": _dataclass_to_dict(result.summary),
        "decisions_count": result.decisions_count,
        "fills_count": result.fills_count,
        "start_ts": result.start_ts.isoformat(),
        "end_ts": result.end_ts.isoformat(),
    }
    summary_path.write_text(
        json.dumps(
            summary_payload,
            indent=2,
            cls=_DecimalJSONEncoder,
            allow_nan=False,
        ),
        encoding="utf-8",
        newline="\n",
    )

    # equity_curve.csv
    with equity_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "artifact_schema_version",
                "settlement_currency",
                "instrument_symbol",
                "instrument_contract_fingerprint",
                "ts_ms",
                "equity",
                "cumulative_pnl",
                "realized_pnl",
                "unrealized_pnl",
                "net_qty",
                "avg_entry_price",
                "mark_price",
                "fill_count",
                "accumulated_fees",
                "drawdown_bps",
                "daily_return_bps",
            ]
        )
        for pt in result.equity_curve:
            writer.writerow(
                [
                    BACKTEST_ARTIFACT_SCHEMA_VERSION,
                    pt.settlement_currency,
                    pt.instrument_symbol,
                    pt.instrument_contract_fingerprint,
                    pt.ts_ms,
                    str(pt.equity),
                    str(pt.cumulative_pnl),
                    str(pt.realized_pnl),
                    str(pt.unrealized_pnl),
                    str(pt.net_qty),
                    str(pt.avg_entry_price),
                    str(pt.mark_price),
                    pt.fill_count,
                    str(pt.accumulated_fees),
                    str(pt.drawdown_bps),
                    str(pt.daily_return_bps),
                ]
            )

    # cost_validation.json
    cost_path.write_text(
        json.dumps(
            _dataclass_to_dict(result.cost_summary),
            indent=2,
            allow_nan=False,
        ),
        encoding="utf-8",
        newline="\n",
    )

    # 逐笔归因是复算 semantic run fingerprint 与 scorecard 成本切片所必需
    # 的一等证据，不能只保留在进程内 BacktestResult。
    cost_diagnostics_path.write_text(
        json.dumps(
            {
                "artifact_schema_version": BACKTEST_ARTIFACT_SCHEMA_VERSION,
                "artifact_kind": "backtest_cost_diagnostics",
                "diagnostics": [
                    _dataclass_to_dict(diagnostic)
                    for diagnostic in result.cost_diagnostics
                ],
            },
            indent=2,
            allow_nan=False,
        ),
        encoding="utf-8",
        newline="\n",
    )

    # FS-003: 时间契约必须随研究产物持久化，不能只靠进程内对象或日志证明。
    timeline_path.write_text(
        json.dumps(
            [_dataclass_to_dict(record) for record in result.execution_timeline],
            indent=2,
            cls=_DecimalJSONEncoder,
            allow_nan=False,
        ),
        encoding="utf-8",
        newline="\n",
    )


def _write_scorecard(
    path: Path,
    result: BacktestResult,
    *,
    split_ts: datetime | None = None,
) -> None:
    """Atomically serialize one non-finite-free evidence scorecard."""
    validate_backtest_result_units(
        result,
        require_complete_artifact=True,
    )
    scorecard = build_scorecard(result, split_ts=split_ts)
    payload = json.dumps(
        scorecard,
        indent=2,
        cls=_DecimalJSONEncoder,
        allow_nan=False,
    )
    if path.exists():
        raise FileExistsError(f"scorecard output already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.staging-",
        dir=path.parent,
    )
    os.close(fd)
    staged_path = Path(temp_name)
    try:
        staged_path.write_text(
            payload,
            encoding="utf-8",
            newline="\n",
        )
        # Hard-link publication is atomic and no-clobber: if a concurrent
        # writer creates ``path`` after the preflight check, this raises
        # FileExistsError instead of replacing an audit artifact.
        os.link(staged_path, path)
        staged_path.unlink()
    except Exception:
        staged_path.unlink(missing_ok=True)
        raise


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
