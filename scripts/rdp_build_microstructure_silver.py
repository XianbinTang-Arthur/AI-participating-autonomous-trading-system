#!/usr/bin/env python3
"""P1-D Phase 1A — Microstructure Silver 15m ETL 手动触发入口.

参考: docs/design/p1d_phase1a_implementation_design_2026_04_20.md §7.3 / §9 W2 Day 1-3

用法:
    # 默认 dry-run (不 commit, 只看本 bar 数据规模)
    python scripts/rdp_build_microstructure_silver.py --symbol BTC-USDT-SWAP

    # 明确指定 bar (UTC ISO)
    python scripts/rdp_build_microstructure_silver.py --symbol BTC-USDT-SWAP \
        --bar-start 2026-04-20T00:00:00+00:00

    # 正式执行 (双层保护: 必须 --apply 和 --confirm 同时指定)
    python scripts/rdp_build_microstructure_silver.py --symbol BTC-USDT-SWAP \
        --apply --confirm

    # 批量补跑 N 个最近 bar (运维场景)
    python scripts/rdp_build_microstructure_silver.py --symbol BTC-USDT-SWAP \
        --backfill-bars 4 --apply --confirm

触发语义:
    - scheduler 走 rdp_task_daemon → workflow `microstructure_silver_15m` →
      这个脚本以 `--apply --confirm` 模式跑 (configs/rdp_workflows/
      microstructure_silver_15m.json)
    - 运维手动触发同样用这个 CLI

双层保护:
    - 默认 dry-run: 只打日志不 commit, 安全看数据
    - --apply 切开 dry-run, 但仍需 --confirm 才真正 commit
    - 走 workflow 时 configs 里两个都带, 守护进程直接跑
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rdp_microstructure_silver")


def _parse_iso_utc(value: str) -> datetime:
    """Parse ISO 8601 to UTC-aware datetime.

    接受 "2026-04-20T00:00:00+00:00" / "2026-04-20T00:00:00Z" /
    "2026-04-20 00:00:00+00:00"。
    """
    # Python 3.11+ fromisoformat 支持 Z 后缀; 3.10- 需要手动替换
    normalized = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        raise argparse.ArgumentTypeError(
            f"Timestamp must be UTC-aware (e.g. '2026-04-20T00:00:00+00:00'), "
            f"got naive: {value!r}"
        )
    return dt.astimezone(timezone.utc)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="P1-D Phase 1A microstructure Silver 15m ETL builder.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--symbol",
        type=str,
        required=True,
        help="Instrument id, e.g. BTC-USDT-SWAP",
    )
    parser.add_argument(
        "--bar-start",
        type=_parse_iso_utc,
        default=None,
        help=(
            "Bar 起点 (UTC ISO 8601, 必须对齐 15m). 默认取最近一个已完成的 bar."
        ),
    )
    parser.add_argument(
        "--bar-end",
        type=_parse_iso_utc,
        default=None,
        help="Bar 终点,默认 = bar_start + 15min;通常无需手动指定。",
    )
    parser.add_argument(
        "--dataset-version",
        type=str,
        default=None,
        help="Silver 写入的 dataset_version,默认 p1d_microstructure_v1.0",
    )
    parser.add_argument(
        "--backfill-bars",
        type=int,
        default=1,
        help=(
            "补跑 N 个最近 bar (>=1), 仅当未指定 --bar-start 时生效。"
            "默认 1 = 只跑最近一个已关的 bar。"
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="切开 dry-run, 真正执行 UPSERT。仍需 --confirm 才 commit。",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="配合 --apply 使用, 确认 commit。",
    )
    return parser.parse_args()


def _run_one_bar(
    *, symbol: str, bar_start: datetime, bar_end: datetime,
    dataset_version: str | None, apply: bool, confirm: bool,
) -> dict[str, Any]:
    """Run ETL for a single bar, return summary dict."""
    from aats.data_platform.db import get_session
    from aats.data_platform.jobs.run_registry import (
        create_ingest_run,
        finish_ingest_run,
    )
    from aats.data_platform.merge.microstructure_silver_merger import (
        DEFAULT_DATASET_VERSION,
        build_silver_microstructure_15m,
    )

    version = dataset_version or DEFAULT_DATASET_VERSION
    mode = "apply" if (apply and confirm) else "dry-run"
    log.info(
        "start symbol=%s bar=[%s, %s) version=%s mode=%s",
        symbol, bar_start.isoformat(), bar_end.isoformat(), version, mode,
    )

    with get_session() as session:
        run_id = create_ingest_run(
            session,
            run_type="rolling",
            dataset_domain="microstructure",
            instrument_type="swap",
            symbol=symbol,
            timeframe="15m",
            trigger_mode="scheduler" if (apply and confirm) else "manual",
        )
        # Stage 3: ingest_run 的 create 先 commit 出去让 meta.ingest_runs
        # 能立刻被 observability 看到 (即便 ETL 本身 dry-run 不 commit)
        session.commit()

    # 切新 session 跑 ETL 本体
    with get_session() as session:
        try:
            result = build_silver_microstructure_15m(
                session=session,
                symbol=symbol,
                bar_start_ts=bar_start,
                bar_end_ts=bar_end,
                ingest_run_id=run_id,
                dataset_version=version,
            )
            if apply and confirm:
                session.commit()
                log.info(
                    "COMMITTED symbol=%s bar_start=%s tables_written=%s flags=%s "
                    "duration=%.3fs",
                    symbol, bar_start.isoformat(), result.tables_written,
                    result.quality_flags, result.duration_seconds,
                )
            else:
                session.rollback()
                log.info(
                    "DRY-RUN (no commit) symbol=%s bar_start=%s tables_written=%s "
                    "flags=%s duration=%.3fs",
                    symbol, bar_start.isoformat(), result.tables_written,
                    result.quality_flags, result.duration_seconds,
                )
        except Exception as exc:
            session.rollback()
            log.exception("build_silver_microstructure_15m failed")
            # 写 meta.ingest_runs 的 failed 状态
            with get_session() as run_session:
                finish_ingest_run(
                    run_session, run_id, status="failed",
                    error_message=repr(exc)[:500],
                )
                run_session.commit()
            raise

    # 写 meta.ingest_runs 的 succeeded 状态 (独立 session)
    if apply and confirm:
        with get_session() as run_session:
            finish_ingest_run(run_session, run_id, status="succeeded")
            run_session.commit()

    return {
        "symbol": symbol,
        "bar_start": bar_start.isoformat(),
        "bar_end": bar_end.isoformat(),
        "tables_written": dict(result.tables_written),
        "quality_flags": list(result.quality_flags),
        "duration_seconds": result.duration_seconds,
        "ingest_run_id": run_id,
        "mode": mode,
        "error": result.error,
    }


def main() -> int:
    args = parse_args()

    if args.backfill_bars < 1:
        log.error("--backfill-bars must be >= 1, got %d", args.backfill_bars)
        return 2

    if args.apply and not args.confirm:
        log.error(
            "--apply requires --confirm for safety; rerun with both or drop --apply"
        )
        return 2
    if args.confirm and not args.apply:
        log.error("--confirm has no effect without --apply")
        return 2

    from aats.data_platform.merge.microstructure_silver_merger import (
        BAR_SECONDS,
        latest_complete_bar,
    )
    from datetime import timedelta

    # 决定 bar 窗口
    bars: list[tuple[datetime, datetime]] = []
    if args.bar_start is not None:
        bar_start = args.bar_start
        bar_end = args.bar_end or (bar_start + timedelta(seconds=BAR_SECONDS))
        bars.append((bar_start, bar_end))
    else:
        # 补跑 N 个最近 bar: lookback_bars=1..N
        for i in range(1, args.backfill_bars + 1):
            bs, be = latest_complete_bar(lookback_bars=i)
            bars.append((bs, be))

    # 从最早 bar 开始跑 (让 EMA 递归 / baseline 按时间顺序建立)
    bars.sort(key=lambda pair: pair[0])

    summaries: list[dict[str, Any]] = []
    had_error = False
    for bar_start, bar_end in bars:
        try:
            summary = _run_one_bar(
                symbol=args.symbol,
                bar_start=bar_start,
                bar_end=bar_end,
                dataset_version=args.dataset_version,
                apply=args.apply,
                confirm=args.confirm,
            )
            summaries.append(summary)
        except Exception as exc:
            had_error = True
            log.error("bar %s failed: %r", bar_start.isoformat(), exc)
            # 后续 bar 仍然继续尝试 (batch 内独立失败)
            summaries.append({
                "symbol": args.symbol,
                "bar_start": bar_start.isoformat(),
                "bar_end": bar_end.isoformat(),
                "error": repr(exc),
                "mode": "apply" if (args.apply and args.confirm) else "dry-run",
            })

    log.info("=== SUMMARY ===")
    for s in summaries:
        log.info("%s", s)

    return 1 if had_error else 0


if __name__ == "__main__":
    sys.exit(main())
