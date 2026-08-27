#!/usr/bin/env python3
"""受控导入 OKX 官方历史成交、L2 或 mark-price bar proxy。"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

_ROOT = Path(__file__).resolve().parent.parent
_SAFE_ERROR_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_:=-]{0,159}$")
_L2_TRANSFORM_VERSION = "okx-bulk-l2-causal-resample-v2"
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from aats.data_platform.collectors.backfill.official_history_importers import (
    causal_resample_l2_ordered,
    import_l2_file,
    import_mark_price_rest,
    import_trade_files,
    import_trade_rest,
    iter_l2_history,
    persist_resampled_l2,
    register_official_source,
)
from aats.data_platform.config import get_settings
from aats.data_platform.data_governance.coverage import git_commit
from aats.data_platform.data_governance.gaps import (
    official_backfill_gap,
    record_data_gaps,
)
from aats.data_platform.data_governance.registry import (
    finalize_historical_bundle,
    import_source_record,
    persist_historical_bundle,
    reserve_historical_bundle,
)
from aats.data_platform.db import get_session
from aats.data_platform.jobs.run_registry import create_ingest_run, finish_ingest_run


def _utc(raw: str) -> datetime:
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise argparse.ArgumentTypeError("时间必须包含 UTC offset")
    return value.astimezone(timezone.utc)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("trade-rest", "trade-file", "l2-file", "mark-rest"))
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--start", required=True, type=_utc)
    parser.add_argument("--end", required=True, type=_utc)
    parser.add_argument("--input", type=Path)
    parser.add_argument(
        "--additional-input",
        action="append",
        default=[],
        type=Path,
        help="trade-file 可追加相邻官方文件，以完整覆盖同一 UTC 窗口",
    )
    parser.add_argument("--raw-archive-dir", type=Path)
    parser.add_argument("--timeframe", choices=("15m", "1H"))
    parser.add_argument(
        "--bundle-id",
        help="兼容校验参数；bundle 由来源证据确定性创建，传入值必须与生成值一致",
    )
    parser.add_argument("--max-pages", type=int, default=10_000)
    parser.add_argument("--max-staleness-ms", type=int, default=2_000)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.apply != args.confirm:
        print("--apply 与 --confirm 必须同时使用", file=sys.stderr)
        return 4
    requirements = {
        "trade-rest": (args.raw_archive_dir is not None),
        "trade-file": (args.input is not None and args.raw_archive_dir is not None),
        "l2-file": (
            args.input is not None
            and args.raw_archive_dir is not None
        ),
        "mark-rest": (args.raw_archive_dir is not None and args.timeframe is not None),
    }
    if not requirements[args.mode]:
        print("所选模式缺少 input/raw-archive-dir/timeframe", file=sys.stderr)
        return 4
    symbol = args.symbol.strip().upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9-]{0,63}", symbol):
        print("--symbol 格式无效", file=sys.stderr)
        return 4
    if args.end <= args.start:
        print("--end 必须晚于 --start", file=sys.stderr)
        return 4
    if args.max_pages <= 0 or args.max_staleness_ms < 0:
        print("分页或最大陈旧度参数无效", file=sys.stderr)
        return 4
    if args.raw_archive_dir is not None and not args.raw_archive_dir.expanduser().is_absolute():
        print("--raw-archive-dir 必须是绝对路径", file=sys.stderr)
        return 4
    if args.input is not None:
        if not args.input.expanduser().is_absolute():
            print("--input 必须是绝对路径", file=sys.stderr)
            return 4
        if not args.input.expanduser().resolve().is_file():
            print("--input 文件不存在", file=sys.stderr)
            return 4
    for additional in args.additional_input:
        if not additional.expanduser().is_absolute():
            print("--additional-input 必须是绝对路径", file=sys.stderr)
            return 4
        if not additional.expanduser().resolve().is_file():
            print("--additional-input 文件不存在", file=sys.stderr)
            return 4
    if args.additional_input and args.mode != "trade-file":
        print("--additional-input 只允许用于 trade-file", file=sys.stderr)
        return 4
    if args.mode == "l2-file" and args.end - args.start > timedelta(days=1):
        print("L2 单次导入最多 1 个 UTC 日；请按日分区运行", file=sys.stderr)
        return 4
    plan = {
        "mode": args.mode,
        "symbol": symbol,
        "start": args.start.isoformat(),
        "end": args.end.isoformat(),
        "input": str(args.input.expanduser().resolve()) if args.input else None,
        "additional_input_count": len(args.additional_input),
        "raw_archive_dir": (
            str(args.raw_archive_dir.expanduser().resolve())
            if args.raw_archive_dir
            else None
        ),
        "timeframe": args.timeframe,
        "bundle_id_present": args.bundle_id is not None,
        "network": args.mode.endswith("rest"),
        "live_side_effects": False,
    }
    if not args.apply:
        print(json.dumps({"dry_run": True, "plan": plan}, indent=2, ensure_ascii=False))
        return 2

    run_id: str | None = None
    try:
        with get_session() as session:
            run_id = create_ingest_run(
                session,
                run_type="backfill",
                dataset_domain="microstructure",
                instrument_type="SWAP",
                symbol=symbol,
                trigger_mode="manual",
            )
        with get_session() as session:
            if args.mode == "trade-rest":
                source_key = "okx-rest:market-history-trades:v5"
                source_kind = "okx_rest"
                source_locator = "/api/v5/market/history-trades"
                timestamp_semantics = "exchange trade timestamp in milliseconds"
                source_id = register_official_source(
                    session,
                    source_key=source_key,
                    source_kind=source_kind,
                    source_locator=source_locator,
                    timestamp_semantics=timestamp_semantics,
                )
                with httpx.Client() as client:
                    stats = import_trade_rest(
                        session,
                        client=client,
                        base_url=get_settings().okx_rest_url,
                        symbol=symbol,
                        start=args.start,
                        end=args.end,
                        source_id=source_id,
                        ingest_run_id=run_id,
                        raw_archive_dir=args.raw_archive_dir,
                        max_pages=args.max_pages,
                    )
                result: dict[str, object] = asdict(stats)
            elif args.mode == "trade-file":
                source_key = "okx-bulk:trade-history:v1"
                source_kind = "okx_bulk"
                source_locator = "operator-supplied OKX trade history file"
                timestamp_semantics = "exchange trade timestamp from official bulk file"
                source_id = register_official_source(
                    session,
                    source_key=source_key,
                    source_kind=source_kind,
                    source_locator=source_locator,
                    timestamp_semantics=timestamp_semantics,
                )
                stats = import_trade_files(
                    session,
                    paths=(args.input, *args.additional_input),
                    symbol=symbol,
                    start=args.start,
                    end=args.end,
                    source_id=source_id,
                    ingest_run_id=run_id,
                    raw_archive_dir=args.raw_archive_dir,
                )
                result = asdict(stats)
            elif args.mode == "mark-rest":
                source_key = f"okx-rest:mark-price:{args.timeframe}:proxy-v1"
                source_kind = "proxy"
                source_locator = "/api/v5/market/history-mark-price-candles"
                timestamp_semantics = (
                    "confirmed mark-price bar opening time; bar proxy only"
                )
                source_id = register_official_source(
                    session,
                    source_key=source_key,
                    source_kind=source_kind,
                    source_locator=source_locator,
                    timestamp_semantics=timestamp_semantics,
                )
                with httpx.Client() as client:
                    stats = import_mark_price_rest(
                        session,
                        client=client,
                        base_url=get_settings().okx_rest_url,
                        symbol=symbol,
                        timeframe=args.timeframe,
                        start=args.start,
                        end=args.end,
                        source_id=source_id,
                        ingest_run_id=run_id,
                        raw_archive_dir=args.raw_archive_dir,
                        max_pages=args.max_pages,
                    )
                result = asdict(stats)
            else:
                source_key = "okx-bulk:l2-history:v1"
                source_kind = "okx_bulk"
                source_locator = "operator-supplied OKX L2 history file"
                timestamp_semantics = (
                    "exchange order-book event timestamp from official bulk file"
                )
                source_id = register_official_source(
                    session,
                    source_key=source_key,
                    source_kind=source_kind,
                    source_locator=source_locator,
                    timestamp_semantics=timestamp_semantics,
                )
                stats = import_l2_file(
                    session,
                    path=args.input,
                    symbol=symbol,
                    start=args.start,
                    end=args.end,
                    source_id=source_id,
                    ingest_run_id=run_id,
                    raw_archive_dir=args.raw_archive_dir,
                )
                source = _source_record(
                    stats,
                    source_key=source_key,
                    source_locator=source_locator,
                    timestamp_semantics=timestamp_semantics,
                    transform_version=_L2_TRANSFORM_VERSION,
                )
                bundle_id, reservation_fingerprint = reserve_historical_bundle(
                    session,
                    source_id=source_id,
                    source=source,
                    symbol=symbol,
                    role="l2_event_history",
                    purpose="l2_replay",
                )
                if args.bundle_id is not None and args.bundle_id != bundle_id:
                    raise RuntimeError("operator_bundle_id_does_not_match_provenance")
                # Raw/staging facts and the RESERVED bundle are a durable checkpoint.
                # A later resample failure may be retried idempotently without losing a
                # multi-gigabyte import transaction.
                session.commit()
                books5, books5_gaps = causal_resample_l2_ordered(
                    iter_l2_history(
                        session,
                        source_id=source_id,
                        symbol=symbol,
                        start=args.start,
                        end=args.end,
                    ),
                    start=args.start,
                    end=args.end,
                    interval_ms=500,
                    max_staleness_ms=args.max_staleness_ms,
                )
                bbo = [
                    row
                    for row in books5
                    if _is_one_second_sample(row.ts, args.start)
                ]
                bbo_gap_times = {
                    gap["sample_ts"]
                    for gap in books5_gaps
                    if _is_one_second_sample(
                        datetime.fromisoformat(str(gap["sample_ts"])),
                        args.start,
                    )
                }
                bbo_gaps = [
                    gap for gap in books5_gaps if gap["sample_ts"] in bbo_gap_times
                ]
                bbo_written, books5_written = persist_resampled_l2(
                    session,
                    bundle_id=bundle_id,
                    bbo_rows=bbo,
                    books5_rows=books5,
                    transform_version=_L2_TRANSFORM_VERSION,
                )
                bbo_sample_count = len(bbo)
                books5_sample_count = len(books5)
                result = {
                    "import": asdict(stats),
                    "resampled": {
                        "source_label": "okx_bulk_l2_resampled",
                        "bbo_1hz_written": bbo_written,
                        "books5_2hz_written": books5_written,
                        "bbo_gaps": bbo_gaps,
                        "books5_gaps": books5_gaps,
                    },
                }
                # BBO is a strict subset of the 2 Hz books5 samples. Recording both
                # gap lists in source provenance counts the same unavailable source
                # state twice, so retain the raw-import gaps plus the finest sampled
                # gap evidence only. The BBO-specific view remains in the result.
                combined_gaps = _deduplicate_gaps(stats.gaps, books5_gaps)
                source = _source_record(
                    stats,
                    source_key=source_key,
                    source_locator=source_locator,
                    timestamp_semantics=timestamp_semantics,
                    transform_version=_L2_TRANSFORM_VERSION,
                    gaps=combined_gaps,
                )
                expected_bbo = int((args.end - args.start).total_seconds())
                expected_books5 = expected_bbo * 2
                coverage_ratio = min(
                    bbo_sample_count / expected_bbo if expected_bbo else 0.0,
                    books5_sample_count / expected_books5 if expected_books5 else 0.0,
                )
                causal_time_check = _l2_samples_are_causal(
                    books5,
                    source_rows_read=stats.rows_read,
                )
                if reservation_fingerprint is None:
                    bundle_id, eligibility = persist_historical_bundle(
                        session,
                        source_id=source_id,
                        source=source,
                        symbol=symbol,
                        role="l2_event_history",
                        purpose="l2_replay",
                        coverage_ratio=coverage_ratio,
                        causal_time_check=causal_time_check,
                    )
                else:
                    bundle_id, eligibility = finalize_historical_bundle(
                        session,
                        bundle_id=bundle_id,
                        reservation_fingerprint=reservation_fingerprint,
                        source_id=source_id,
                        source=source,
                        symbol=symbol,
                        role="l2_event_history",
                        purpose="l2_replay",
                        coverage_ratio=coverage_ratio,
                        causal_time_check=causal_time_check,
                    )
                result["bundle"] = _bundle_result(bundle_id, eligibility)

            if args.mode != "l2-file":
                purpose, role = {
                    "trade-rest": ("trade_flow_research", "trades"),
                    "trade-file": ("trade_flow_research", "trades"),
                    "mark-rest": ("mark_price_research", "mark_price_bar"),
                }[args.mode]
                source = _source_record(
                    stats,
                    source_key=source_key,
                    source_locator=source_locator,
                    timestamp_semantics=timestamp_semantics,
                )
                coverage_ratio = _coverage_ratio(args.mode, stats, args)
                bundle_id, eligibility = persist_historical_bundle(
                    session,
                    source_id=source_id,
                    source=source,
                    symbol=symbol,
                    role=role,
                    purpose=purpose,
                    coverage_ratio=coverage_ratio,
                    causal_time_check=not stats.gaps and stats.rows_read > 0,
                )
                result["bundle"] = _bundle_result(bundle_id, eligibility)

            _persist_import_gaps(
                session,
                mode=args.mode,
                source_id=source_id,
                symbol=symbol,
                default_start=args.start,
                default_end=args.end,
                gaps=(
                    combined_gaps
                    if args.mode == "l2-file"
                    else stats.gaps
                ),
            )
            finish_ingest_run(session, run_id, status="succeeded")
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 0
    except Exception as exc:
        error_code = _safe_error_code(exc)
        if run_id is not None:
            try:
                with get_session() as session:
                    finish_ingest_run(
                        session,
                        run_id,
                        status="failed",
                        error_message=error_code,
                    )
            except Exception as status_exc:
                print(
                    "ERROR: failed to persist import failure state: "
                    f"{type(status_exc).__name__}",
                    file=sys.stderr,
                )
        print(f"ERROR: {error_code}", file=sys.stderr)
        return 3


def _safe_error_code(exc: Exception) -> str:
    """Expose an actionable internal code without leaking paths or responses."""

    detail = str(exc).strip()
    if isinstance(exc, (RuntimeError, ValueError)) and _SAFE_ERROR_CODE.fullmatch(
        detail
    ):
        return detail
    return type(exc).__name__


def _is_one_second_sample(sample: datetime, start: datetime) -> bool:
    delta = sample - start
    return delta.days >= 0 and delta.microseconds == 0


def _l2_samples_are_causal(rows, *, source_rows_read: int) -> bool:
    """Prove every emitted sample uses only an already-observed source state."""

    if source_rows_read <= 0 or not rows:
        return False
    return all(
        row.source_state_ts <= row.ts
        and row.staleness_ms
        == int((row.ts - row.source_state_ts).total_seconds() * 1_000)
        for row in rows
    )


def _deduplicate_gaps(*groups) -> tuple[dict[str, object], ...]:
    """Preserve gap order while removing duplicate evidence records."""

    unique: list[dict[str, object]] = []
    fingerprints: set[str] = set()
    for group in groups:
        for item in group:
            normalized = dict(item)
            fingerprint = json.dumps(
                normalized,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                default=str,
            )
            if fingerprint in fingerprints:
                continue
            fingerprints.add(fingerprint)
            unique.append(normalized)
    return tuple(unique)


def _source_record(
    stats,
    *,
    source_key: str,
    source_locator: str,
    timestamp_semantics: str,
    transform_version: str | None = None,
    gaps=None,
):
    return import_source_record(
        source_key=source_key,
        source_kind=stats.source_kind,
        provider="OKX",
        source_locator=source_locator,
        coverage_start=stats.start,
        coverage_end=stats.end,
        timestamp_semantics=timestamp_semantics,
        schema_version="okx-v5",
        dataset_version="rdp-official-history-v1",
        transform_version=transform_version,
        git_commit=git_commit(str(_ROOT)),
        raw_partition_sha256=stats.raw_sha256,
        row_count=stats.rows_read,
        gaps=tuple(stats.gaps if gaps is None else gaps),
    )


def _coverage_ratio(mode: str, stats, args: argparse.Namespace) -> float:
    if stats.rows_read == 0:
        return 0.0
    if mode != "mark-rest":
        return 1.0 if not stats.gaps else 0.0
    interval = 900 if args.timeframe == "15m" else 3600
    expected = int((args.end - args.start).total_seconds() / interval)
    return min(1.0, stats.rows_read / expected) if expected else 0.0


def _bundle_result(bundle_id: str, report) -> dict[str, object]:
    return {
        "bundle_id": bundle_id,
        "eligible": report.eligible,
        "reason_codes": list(report.reason_codes),
        "evidence_fingerprint": report.evidence_fingerprint,
    }


def _persist_import_gaps(
    session,
    *,
    mode: str,
    source_id: str,
    symbol: str,
    default_start: datetime,
    default_end: datetime,
    gaps,
) -> int:
    dataset, channel = {
        "trade-rest": ("staging.official_trade_history", "history-trades"),
        "trade-file": ("staging.official_trade_history", "official-trade-file"),
        "l2-file": ("staging.official_l2_history", "official-l2-file"),
        "mark-rest": ("bronze.market_mark_price_candles", "history-mark-price-candles"),
    }[mode]
    records = []
    for item in gaps:
        start = _gap_time(item, "gap_start", "sample_ts", "at") or default_start
        end = _gap_time(item, "gap_end")
        if end is None or end <= start:
            end = start + timedelta(milliseconds=1)
        start = max(start, default_start)
        end = min(end, default_end)
        if end <= start:
            continue
        records.append(
            official_backfill_gap(
                source_id=source_id,
                dataset_name=dataset,
                symbol=symbol,
                channel=channel,
                gap_start=start,
                gap_end=end,
                reason_code=str(item.get("reason") or "official_source_gap"),
                evidence=dict(item),
            )
        )
    return record_data_gaps(session, records)


def _gap_time(item, *keys: str) -> datetime | None:
    for key in keys:
        value = item.get(key)
        if not isinstance(value, str):
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is not None and parsed.utcoffset() is not None:
            return parsed.astimezone(timezone.utc)
    return None


if __name__ == "__main__":
    raise SystemExit(main())
