#!/usr/bin/env python3
"""P1-D Stage 5 — OKX REST 历史数据 batch backfill CLI.

配合 `aats.data_platform.collectors.backfill.okx_rest_history_collectors` 把
3 个 OKX REST endpoint 的历史数据回填到 Bronze 表:

  1. /api/v5/rubik/stat/contracts/open-interest-history (period=1H)
     → bronze.market_oi_history_1h
  2. /api/v5/market/mark-price-candles-history (period=1m)
     → bronze.market_mark_price_candles_1m
  3. /api/v5/rubik/stat/contracts/long-short-account-ratio (period=5m)
     → bronze.market_long_short_ratio_5m

三层保护:
  --dry-run (default): 只计算预估, 不发请求不写 DB. 显示预期 pages/rows/时长.
  --apply:             真发请求, 走 ingest_run + checkpoint 流程.
  --verify:            只查 3 张 Bronze 表行数 + min/max ts, 不发请求不写 DB.

用法:
    # 1. 看看要发多少请求、拉多少行
    python scripts/rdp_backfill_okx_rest_history.py --symbol BTC-USDT-SWAP \
        --days-oi 90 --days-mark 30 --days-ls 30

    # 2. 实际跑 (用户批准后)
    python scripts/rdp_backfill_okx_rest_history.py --symbol BTC-USDT-SWAP \
        --days-oi 90 --days-mark 30 --days-ls 30 --apply

    # 3. 看结果
    python scripts/rdp_backfill_okx_rest_history.py --symbol BTC-USDT-SWAP --verify

成本保护:
  - 严守 20 req / 2s IP rate limit (默认 0.15s/req 间隔)
  - 单次 dry-run 预估 > 1 小时或 > 1 GB 会 WARN 提示
  - 429 指数 backoff, 最多 5 次重试, 失败写入 stats.errors 不 raise

退出码:
  0 = success
  1 = argument error
  2 = DB/network error
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve()
ROOT = HERE.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 加载环境 (.env.wsl2 / .env.derivatives.live)
try:
    from dotenv import load_dotenv
except ImportError:
    print("missing python-dotenv; pip install python-dotenv", file=sys.stderr)
    sys.exit(2)

_ENV_SEARCH_ROOTS: list[Path] = [ROOT]
_home = os.environ.get("HOME")
if _home:
    _ENV_SEARCH_ROOTS.append(Path(_home) / "aats")

for env_file in (".env.wsl2", ".env.derivatives.live"):
    for search_root in _ENV_SEARCH_ROOTS:
        env_path = search_root / env_file
        if env_path.is_file():
            load_dotenv(env_path, override=False)
            break

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rdp_backfill_okx_rest_history")


# ─────────────────────────────────────────────────────────────────────
# DB connection
# ─────────────────────────────────────────────────────────────────────


def resolve_db_url() -> str:
    """Resolve RDP DB URL from env (never echo VALUES, only KEYS)."""
    for key in ("RDP_DATABASE_URL",):
        val = os.environ.get(key)
        if val:
            return val
    user = os.environ.get("POSTGRES_USER", "admin")
    pw = os.environ.get("POSTGRES_PASSWORD")
    if not pw:
        raise SystemExit(
            "missing credentials: set POSTGRES_PASSWORD or RDP_DATABASE_URL "
            "in .env.wsl2 / .env.derivatives.live"
        )
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("RDP_POSTGRES_DB", "aats_research")
    return f"postgresql+psycopg://{user}:{pw}@{host}:{port}/{db}"


# ─────────────────────────────────────────────────────────────────────
# Cost preflight warning
# ─────────────────────────────────────────────────────────────────────


def warn_if_excessive(estimates: list[dict[str, Any]]) -> None:
    """如果 dry-run 预估 > 1 小时或 > 1 GB, 打 WARN."""
    total_rows = sum(e["estimated_rows"] for e in estimates)
    total_pages = sum(e["estimated_pages"] for e in estimates)
    total_seconds = sum(e["estimated_seconds_at_default_rate"] for e in estimates)
    # 估 200 B/row 平均
    est_bytes = total_rows * 200

    log.info("=" * 80)
    log.info("总预估: %d pages, %d rows, ~%.0f s, ~%.1f MB",
             total_pages, total_rows, total_seconds, est_bytes / (1024 * 1024))

    if total_seconds > 3600:
        log.warning("预估下载时长 > 1 小时 (%.1f h); 请确认是否真的需要",
                    total_seconds / 3600)
    if est_bytes > 1024 ** 3:
        log.warning("预估下载体积 > 1 GB (%.1f GB); 请确认是否真的需要",
                    est_bytes / (1024 ** 3))
    log.info("=" * 80)


# ─────────────────────────────────────────────────────────────────────
# Verify mode
# ─────────────────────────────────────────────────────────────────────


def run_verify(symbol: str) -> int:
    """查 3 张 Bronze 表 (symbol filter) 的行数 + min/max ts."""
    from sqlalchemy import create_engine, text

    engine = create_engine(resolve_db_url())
    tables = [
        ("bronze.market_oi_history_1h", symbol),
        ("bronze.market_mark_price_candles_1m", symbol),
        ("bronze.market_long_short_ratio_5m", symbol),
    ]
    print()
    print("=" * 80)
    print(f"VERIFY — Bronze 表行数 & ts 范围 (symbol={symbol})")
    print("=" * 80)
    with engine.connect() as conn:
        for table, sym in tables:
            try:
                row = conn.execute(
                    text(
                        f"SELECT COUNT(*), min(ts), max(ts) FROM {table} WHERE symbol = :s"
                    ),
                    {"s": sym},
                ).fetchone()
                n = row[0] if row else 0
                mn = row[1] if row else None
                mx = row[2] if row else None
                if n == 0:
                    print(f"  {table:<48s} (symbol={sym}): 0 rows")
                else:
                    print(f"  {table:<48s} (symbol={sym}): {n:>6d} rows "
                          f"from {mn} to {mx}")
            except Exception as exc:
                print(f"  {table:<48s}: QUERY FAILED: {exc}")
    print("=" * 80)
    return 0


# ─────────────────────────────────────────────────────────────────────
# Apply / Dry-run orchestration
# ─────────────────────────────────────────────────────────────────────


def _build_session_factory(url: str):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(url, future=True)
    return engine, sessionmaker(engine, expire_on_commit=False, future=True)


def run_oi_history(
    *,
    symbol: str,
    days: int,
    dry_run: bool,
    base_url: str,
    rate_limit_sleep: float,
) -> dict[str, Any]:
    from aats.data_platform.collectors.backfill.okx_rest_history_collectors import (
        collect_oi_history,
        estimate_oi_history_requests,
    )
    from aats.data_platform.jobs.run_registry import (
        create_ingest_run,
        finish_ingest_run,
    )
    from aats.data_platform.models import instrument_type_for_symbol

    if dry_run:
        est = estimate_oi_history_requests(days, "1H")
        log.info("[OI dry-run] %s: %d pages, %d rows, ~%.1fs",
                 symbol, est["estimated_pages"], est["estimated_rows"],
                 est["estimated_seconds_at_default_rate"])
        return est

    engine, SessionMaker = _build_session_factory(resolve_db_url())
    with SessionMaker() as session:
        run_id = create_ingest_run(
            session,
            run_type="backfill",
            dataset_domain="microstructure",
            instrument_type=instrument_type_for_symbol(symbol),
            symbol=symbol,
            timeframe="oi_1h",
            trigger_mode="manual",
        )
        session.commit()  # 独立事务创建 run, 后续写数据另起
        try:
            stats = collect_oi_history(
                session,
                symbol=symbol,
                target_days=days,
                period="1H",
                base_url=base_url,
                rate_limit_sleep=rate_limit_sleep,
                dry_run=False,
                ingest_run_id=run_id,
            )
            finish_ingest_run(session, run_id, status="succeeded",
                              checkpoint_after=stats.to_dict())
            session.commit()
            return stats.to_dict()
        except Exception as exc:
            finish_ingest_run(session, run_id, status="failed",
                              error_message=str(exc))
            session.commit()
            raise


def run_mark_candles(
    *,
    symbol: str,
    days: int,
    dry_run: bool,
    base_url: str,
    rate_limit_sleep: float,
) -> dict[str, Any]:
    from aats.data_platform.collectors.backfill.okx_rest_history_collectors import (
        collect_mark_candles_history,
        estimate_mark_candles_requests,
    )
    from aats.data_platform.jobs.run_registry import (
        create_ingest_run,
        finish_ingest_run,
    )
    from aats.data_platform.models import instrument_type_for_symbol

    if dry_run:
        est = estimate_mark_candles_requests(days, "1m")
        log.info("[MARK dry-run] %s: %d pages, %d rows, ~%.1fs",
                 symbol, est["estimated_pages"], est["estimated_rows"],
                 est["estimated_seconds_at_default_rate"])
        return est

    engine, SessionMaker = _build_session_factory(resolve_db_url())
    with SessionMaker() as session:
        run_id = create_ingest_run(
            session,
            run_type="backfill",
            dataset_domain="microstructure",
            instrument_type=instrument_type_for_symbol(symbol),
            symbol=symbol,
            timeframe="mark_1m",
            trigger_mode="manual",
        )
        session.commit()
        try:
            stats = collect_mark_candles_history(
                session,
                symbol=symbol,
                target_days=days,
                period="1m",
                base_url=base_url,
                rate_limit_sleep=rate_limit_sleep,
                dry_run=False,
                ingest_run_id=run_id,
            )
            finish_ingest_run(session, run_id, status="succeeded",
                              checkpoint_after=stats.to_dict())
            session.commit()
            return stats.to_dict()
        except Exception as exc:
            finish_ingest_run(session, run_id, status="failed",
                              error_message=str(exc))
            session.commit()
            raise


def run_ls_ratio(
    *,
    ccy: str,
    days: int,
    dry_run: bool,
    base_url: str,
    rate_limit_sleep: float,
) -> dict[str, Any]:
    from aats.data_platform.collectors.backfill.okx_rest_history_collectors import (
        collect_ls_ratio_history,
        estimate_ls_ratio_requests,
        normalize_ls_symbol,
    )
    from aats.data_platform.jobs.run_registry import (
        create_ingest_run,
        finish_ingest_run,
    )

    if dry_run:
        est = estimate_ls_ratio_requests(days, "5m")
        log.info("[LS dry-run] ccy=%s: %d pages, %d rows, ~%.1fs",
                 ccy, est["estimated_pages"], est["estimated_rows"],
                 est["estimated_seconds_at_default_rate"])
        return est

    sym = normalize_ls_symbol(ccy)
    engine, SessionMaker = _build_session_factory(resolve_db_url())
    with SessionMaker() as session:
        run_id = create_ingest_run(
            session,
            run_type="backfill",
            dataset_domain="microstructure",
            instrument_type="swap",
            symbol=sym,
            timeframe="ls_5m",
            trigger_mode="manual",
        )
        session.commit()
        try:
            stats = collect_ls_ratio_history(
                session,
                ccy=ccy,
                target_days=days,
                period="5m",
                base_url=base_url,
                rate_limit_sleep=rate_limit_sleep,
                dry_run=False,
                ingest_run_id=run_id,
            )
            finish_ingest_run(session, run_id, status="succeeded",
                              checkpoint_after=stats.to_dict())
            session.commit()
            return stats.to_dict()
        except Exception as exc:
            finish_ingest_run(session, run_id, status="failed",
                              error_message=str(exc))
            session.commit()
            raise


# ─────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(
        description="P1-D Stage 5 — OKX REST 历史数据 batch backfill",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 预览
  python scripts/rdp_backfill_okx_rest_history.py --symbol BTC-USDT-SWAP \\
      --days-oi 90 --days-mark 30 --days-ls 30

  # 实跑
  python scripts/rdp_backfill_okx_rest_history.py --symbol BTC-USDT-SWAP \\
      --days-oi 90 --days-mark 30 --days-ls 30 --apply

  # 验证
  python scripts/rdp_backfill_okx_rest_history.py --symbol BTC-USDT-SWAP --verify
        """,
    )
    ap.add_argument("--symbol", default="BTC-USDT-SWAP",
                    help="OKX SWAP instId (default BTC-USDT-SWAP); "
                         "LS endpoint 自动降级为 ccy (BTC)")
    ap.add_argument("--ccy", default=None,
                    help="LS endpoint 用的 ccy (default: 从 --symbol 前缀推断)")
    ap.add_argument("--days-oi", type=int, default=90)
    ap.add_argument("--days-mark", type=int, default=30)
    ap.add_argument("--days-ls", type=int, default=30)
    ap.add_argument("--base-url", default="https://www.okx.com")
    ap.add_argument("--rate-limit-sleep", type=float, default=0.15,
                    help="请求间隔 (秒), 默认 0.15 = 6.7 req/s, "
                         "低于 OKX 10 req/s 上限 33%%")
    ap.add_argument("--skip-oi", action="store_true")
    ap.add_argument("--skip-mark", action="store_true")
    ap.add_argument("--skip-ls", action="store_true")
    ap.add_argument("--apply", action="store_true",
                    help="实际发 OKX REST 请求 + 写 DB (默认 dry-run)")
    ap.add_argument("--verify", action="store_true",
                    help="只查 Bronze 表行数 + ts 范围 (不发请求)")
    ap.add_argument("--output", default=None,
                    help="可选 JSON output path (stats summary)")

    args = ap.parse_args()

    if args.apply and args.verify:
        log.error("--apply 和 --verify 不能同时给")
        return 1

    if args.verify:
        return run_verify(args.symbol)

    # 推断 ccy
    if args.ccy is None:
        # "BTC-USDT-SWAP" → "BTC"
        args.ccy = args.symbol.split("-")[0].upper()

    dry_run = not args.apply
    mode_label = "DRY-RUN" if dry_run else "APPLY"

    log.info("=" * 80)
    log.info("OKX REST 历史 backfill — %s mode", mode_label)
    log.info("  symbol=%s ccy=%s days_oi=%d days_mark=%d days_ls=%d",
             args.symbol, args.ccy, args.days_oi, args.days_mark, args.days_ls)
    log.info("  base_url=%s rate_limit_sleep=%.3fs",
             args.base_url, args.rate_limit_sleep)
    log.info("=" * 80)

    # 预估
    from aats.data_platform.collectors.backfill.okx_rest_history_collectors import (
        estimate_ls_ratio_requests,
        estimate_mark_candles_requests,
        estimate_oi_history_requests,
    )
    estimates = []
    if not args.skip_oi:
        estimates.append(estimate_oi_history_requests(args.days_oi, "1H"))
    if not args.skip_mark:
        estimates.append(estimate_mark_candles_requests(args.days_mark, "1m"))
    if not args.skip_ls:
        estimates.append(estimate_ls_ratio_requests(args.days_ls, "5m"))
    warn_if_excessive(estimates)

    results: dict[str, Any] = {
        "mode": mode_label,
        "symbol": args.symbol,
        "ccy": args.ccy,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }

    # 按顺序跑, 每个 endpoint 独立 try/catch — 一个失败不阻塞其他
    any_failed = False
    if not args.skip_oi:
        log.info("--- OI history (period=1H, %d days) ---", args.days_oi)
        try:
            results["oi"] = run_oi_history(
                symbol=args.symbol, days=args.days_oi, dry_run=dry_run,
                base_url=args.base_url, rate_limit_sleep=args.rate_limit_sleep,
            )
        except Exception as exc:
            log.exception("OI backfill failed: %s", exc)
            results["oi_error"] = str(exc)
            any_failed = True

    if not args.skip_mark:
        log.info("--- Mark-price candles (period=1m, %d days) ---", args.days_mark)
        try:
            results["mark"] = run_mark_candles(
                symbol=args.symbol, days=args.days_mark, dry_run=dry_run,
                base_url=args.base_url, rate_limit_sleep=args.rate_limit_sleep,
            )
        except Exception as exc:
            log.exception("MARK backfill failed: %s", exc)
            results["mark_error"] = str(exc)
            any_failed = True

    if not args.skip_ls:
        log.info("--- Long-short ratio (period=5m, %d days) ---", args.days_ls)
        try:
            results["ls"] = run_ls_ratio(
                ccy=args.ccy, days=args.days_ls, dry_run=dry_run,
                base_url=args.base_url, rate_limit_sleep=args.rate_limit_sleep,
            )
        except Exception as exc:
            log.exception("LS backfill failed: %s", exc)
            results["ls_error"] = str(exc)
            any_failed = True

    results["finished_at"] = datetime.now(timezone.utc).isoformat()

    log.info("=" * 80)
    log.info("DONE — %s mode", mode_label)
    log.info("%s", json.dumps(results, default=str, indent=2))
    log.info("=" * 80)

    if args.output:
        Path(args.output).write_text(json.dumps(results, default=str, indent=2))
        log.info("wrote stats to %s", args.output)

    return 2 if any_failed else 0


if __name__ == "__main__":
    sys.exit(main())
