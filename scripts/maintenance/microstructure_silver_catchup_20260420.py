"""P0-a Silver ETL 一次性回填 — 2026-04-20 微观结构 Silver catch-up.

起因 (docs/review/p0a_silver_etl_truth_layer_fix_2026_04_20.md):
    Silver 5 张表全部停在 2026-04-20 05:30:00 UTC+08 (= 2026-04-19 21:30:00 UTC)
    每张 3 行;原因是 vol_weighted_tfi NUMERIC(14,8) 在 volume 超 10^6 USDT
    时 overflow。Batch B Stage 11 修完后, live DB 仍缺 2026-04-20 05:45 ~ 现在
    的所有 15m bar 数据。

本脚本做的事:
    1. 读 `silver.market_orderbook_metrics_15m` 找最新 ts (= last_good)
    2. 以 last_good + 15min 为起点, 到 latest_complete_bar 为止, 列出所有
       缺的 15m bar
    3. dry-run: 只打 "将回填 N 个 bar", 不 commit
    4. --apply --confirm: 对每个缺失 bar 逐个调用
       `build_silver_microstructure_15m(session, symbol=BTC-USDT-SWAP, bar)`,
       按时间顺序跑让 EMA 递归有上一 bar seed, UPSERT 幂等

用法
====

Dry-run (默认, 只打缺口和计划):
    python scripts/maintenance/microstructure_silver_catchup_20260420.py

实际执行 (必须 --apply --confirm):
    python scripts/maintenance/microstructure_silver_catchup_20260420.py \
        --apply --confirm

限定单个 symbol / 指定时间范围:
    python scripts/maintenance/microstructure_silver_catchup_20260420.py \
        --symbol BTC-USDT-SWAP \
        --from 2026-04-19T21:45:00+00:00 \
        --to 2026-04-20T12:00:00+00:00 \
        --apply --confirm

Exit codes:
    0 = 所有目标 bar 全部 commit 成功 (dry-run 无缺口也返回 0)
    1 = 有 bar 抛异常 (主 try/except)
    2 = 有 bar partial fail (某表 written=0, etl_failed flag)
    3 = 有 bar full fail (全表 written=0)
    4 = 参数错误 / DB 不可达
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("microstructure_silver_catchup")


BAR_MINUTES = 15


def _parse_iso_utc(value: str) -> datetime:
    """Parse ISO 8601 to UTC-aware datetime (同 rdp_build_microstructure_silver)."""
    normalized = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        raise argparse.ArgumentTypeError(
            f"Timestamp must be UTC-aware (e.g. '2026-04-20T00:00:00+00:00'), "
            f"got naive: {value!r}"
        )
    return dt.astimezone(timezone.utc)


def _align_up(ts: datetime) -> datetime:
    """对齐到下一个 15m 边界 (若已对齐返回自身)。"""
    if ts.minute % BAR_MINUTES == 0 and ts.second == 0 and ts.microsecond == 0:
        return ts
    # round up
    delta = BAR_MINUTES - (ts.minute % BAR_MINUTES)
    new = ts.replace(second=0, microsecond=0) + timedelta(minutes=delta)
    return new


def _align_down(ts: datetime) -> datetime:
    """对齐到当前 15m 边界起点。"""
    return ts.replace(
        minute=(ts.minute // BAR_MINUTES) * BAR_MINUTES,
        second=0, microsecond=0,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="2026-04-20 microstructure Silver 15m catch-up backfill.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--symbol",
        type=str,
        default="BTC-USDT-SWAP",
        help="Symbol 白名单, 默认只跑 BTC-USDT-SWAP",
    )
    parser.add_argument(
        "--from",
        dest="from_ts",
        type=_parse_iso_utc,
        default=None,
        help=(
            "起始 bar_start (UTC ISO, inclusive)。默认自动探测 Silver "
            "orderbook_metrics_15m 最大 ts + 15min。"
        ),
    )
    parser.add_argument(
        "--to",
        dest="to_ts",
        type=_parse_iso_utc,
        default=None,
        help=(
            "结束 bar_start (UTC ISO, exclusive)。默认 latest_complete_bar "
            "(= now 向前对齐 15min 边界)。"
        ),
    )
    parser.add_argument(
        "--dataset-version",
        type=str,
        default=None,
        help="覆盖 DEFAULT_DATASET_VERSION",
    )
    parser.add_argument(
        "--max-bars",
        type=int,
        default=100,
        help="安全上限, 单次回填超过此数 bars 直接拒绝 (默认 100 = 25h)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="切开 dry-run, 真正 commit UPSERT。仍需 --confirm。",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="配合 --apply 使用, 确认 commit。",
    )
    return parser.parse_args()


def _detect_gap(session, symbol: str) -> datetime | None:
    """返回 silver.market_orderbook_metrics_15m 的最大 ts (UTC-aware)。

    找 orderbook 表(而非 5 张任一) 的原因: orderbook 是 Stage 1 产出, 不
    依赖其他 Silver 表, 是 "最早写的一张" — 它推进, 说明 5 张全 OK 那一 bar。
    """
    from sqlalchemy import text

    row = session.execute(
        text(
            "SELECT MAX(ts) AS max_ts FROM silver.market_orderbook_metrics_15m "
            "WHERE symbol = :sym"
        ),
        {"sym": symbol},
    ).fetchone()
    if row is None or row.max_ts is None:
        return None
    # psycopg 返回 aware datetime
    max_ts = row.max_ts
    if max_ts.tzinfo is None:
        max_ts = max_ts.replace(tzinfo=timezone.utc)
    return max_ts


def _enumerate_bars(
    *, from_ts: datetime, to_ts: datetime,
) -> list[tuple[datetime, datetime]]:
    """列出 [from_ts, to_ts) 内所有 15m bar 起点, 要求 from/to 已对齐。"""
    bars: list[tuple[datetime, datetime]] = []
    cursor = from_ts
    while cursor < to_ts:
        end = cursor + timedelta(minutes=BAR_MINUTES)
        bars.append((cursor, end))
        cursor = end
    return bars


def main() -> int:
    args = parse_args()
    if args.apply and not args.confirm:
        log.error("--apply requires --confirm for safety")
        return 4
    if args.confirm and not args.apply:
        log.error("--confirm has no effect without --apply")
        return 4

    mode = "apply" if (args.apply and args.confirm) else "dry-run"
    log.info("catchup start mode=%s symbol=%s", mode, args.symbol)

    try:
        from aats.data_platform.db import get_session
        from aats.data_platform.jobs.run_registry import (
            create_ingest_run,
            finish_ingest_run,
        )
        from aats.data_platform.merge.microstructure_silver_merger import (
            DEFAULT_DATASET_VERSION,
            build_silver_microstructure_15m,
            latest_complete_bar,
        )
    except Exception as exc:
        log.exception("failed to import RDP modules: %r", exc)
        return 4

    version = args.dataset_version or DEFAULT_DATASET_VERSION

    # 1. 决定 from / to
    if args.to_ts is None:
        # 用 latest_complete_bar: 确保目标 bar 已完整关闭
        _, to_ts = latest_complete_bar()
    else:
        aligned = _align_down(args.to_ts)
        if aligned != args.to_ts:
            log.warning(
                "--to=%s 未对齐到 15min 边界, 已对齐 (align_down) 到 %s",
                args.to_ts.isoformat(), aligned.isoformat(),
            )
        to_ts = aligned

    if args.from_ts is not None:
        from_ts = _align_up(args.from_ts)
        if from_ts != args.from_ts:
            log.warning(
                "--from=%s 未对齐到 15min 边界, 已对齐 (align_up) 到 %s",
                args.from_ts.isoformat(), from_ts.isoformat(),
            )
    else:
        try:
            with get_session() as sess:
                max_ts = _detect_gap(sess, args.symbol)
        except Exception as exc:
            log.exception("failed to detect current Silver max ts: %r", exc)
            return 4
        if max_ts is None:
            log.error(
                "Silver orderbook_metrics_15m for symbol=%s 无任何 row, "
                "请显式传 --from; catchup 脚本不做完全冷启动", args.symbol,
            )
            return 4
        # 从 max_ts + 15min 开始 (max_ts 本身已存在)
        from_ts = max_ts + timedelta(minutes=BAR_MINUTES)

    if from_ts >= to_ts:
        log.info(
            "no gap to fill: from_ts=%s >= to_ts=%s (Silver up to date)",
            from_ts.isoformat(), to_ts.isoformat(),
        )
        return 0

    bars = _enumerate_bars(from_ts=from_ts, to_ts=to_ts)
    log.info(
        "gap detected: from=%s to=%s bars=%d (每 bar 15min)",
        from_ts.isoformat(), to_ts.isoformat(), len(bars),
    )

    if len(bars) > args.max_bars:
        log.error(
            "bars=%d exceeds --max-bars=%d; 请拆分多次执行或提高上限",
            len(bars), args.max_bars,
        )
        return 4

    if mode == "dry-run":
        log.info("DRY-RUN: 将回填 %d bars, 未 commit 任何数据", len(bars))
        for bs, be in bars[:5]:
            log.info("  planned bar: [%s, %s)", bs.isoformat(), be.isoformat())
        if len(bars) > 5:
            log.info("  ... %d more bars elided", len(bars) - 5)
        return 0

    # 2. apply: 按时间顺序逐个 bar UPSERT
    any_uncaught = False
    any_partial_fail = False
    any_full_fail = False
    success = 0
    for idx, (bar_start, bar_end) in enumerate(bars):
        log.info(
            "[%d/%d] symbol=%s bar=[%s, %s)",
            idx + 1, len(bars), args.symbol,
            bar_start.isoformat(), bar_end.isoformat(),
        )
        # 独立 session per bar (和 rdp_build_microstructure_silver 一致)
        run_id = None
        try:
            with get_session() as session:
                run_id = create_ingest_run(
                    session,
                    # meta.ingest_runs.chk_ir_type 只允许
                    #   {backfill, rolling, gap_repair, gold_build}
                    # 历史空洞回填在语义上是 gap_repair.
                    # (2026-04-20 P0-a catchup apply 时发现 chk violation, 现场修)
                    run_type="gap_repair",
                    dataset_domain="microstructure",
                    instrument_type="swap",
                    symbol=args.symbol,
                    timeframe="15m",
                    trigger_mode="manual",
                )
                session.commit()

            with get_session() as session:
                result = build_silver_microstructure_15m(
                    session=session, symbol=args.symbol,
                    bar_start_ts=bar_start, bar_end_ts=bar_end,
                    ingest_run_id=run_id, dataset_version=version,
                )
                session.commit()

            # 判定状态
            if result.tables_failed:
                if result.error is not None and all(
                    rc == 0 for rc in result.tables_written.values()
                ):
                    any_full_fail = True
                    log.error(
                        "bar %s FULL FAIL: tables_failed=%s error=%s",
                        bar_start.isoformat(), result.tables_failed, result.error,
                    )
                else:
                    any_partial_fail = True
                    log.warning(
                        "bar %s PARTIAL FAIL: tables_failed=%s written=%s",
                        bar_start.isoformat(), result.tables_failed,
                        result.tables_written,
                    )
            else:
                success += 1
                log.info(
                    "bar %s OK: tables_written=%s flags=%s duration=%.3fs",
                    bar_start.isoformat(), result.tables_written,
                    result.quality_flags, result.duration_seconds,
                )

            # 标注 ingest_run 状态
            with get_session() as run_session:
                if result.tables_failed:
                    finish_ingest_run(
                        run_session, run_id, status="failed",
                        error_message=(
                            f"tables_failed={result.tables_failed!r} "
                            f"error={result.error!r}"
                        )[:500],
                    )
                else:
                    finish_ingest_run(run_session, run_id, status="succeeded")
                run_session.commit()
        except Exception as exc:
            any_uncaught = True
            log.exception("bar %s uncaught exception: %r", bar_start.isoformat(), exc)
            if run_id is not None:
                try:
                    with get_session() as run_session:
                        finish_ingest_run(
                            run_session, run_id, status="failed",
                            error_message=repr(exc)[:500],
                        )
                        run_session.commit()
                except Exception:
                    log.exception("failed to mark ingest_run failed on %s", run_id)

    log.info(
        "catchup complete: %d/%d bars OK, partial=%s full=%s uncaught=%s",
        success, len(bars), any_partial_fail, any_full_fail, any_uncaught,
    )

    if any_uncaught:
        return 1
    if any_full_fail:
        return 3
    if any_partial_fail:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
