"""P1-D Phase 1A Silver microstructure ETL — Bronze / staging → Silver 15m.

参考: docs/design/p1d_phase1a_implementation_design_2026_04_20.md §5 / §7

总入口 `build_silver_microstructure_15m` 调用 5 个 `_build_*` 函数依次聚合
5 张 Silver 15m 表。每个子函数独立失败边界: 任一表失败只打 quality_flags
= ['etl_failed:<table>'], 其他表仍尝试写入。

幂等性:
    - bar_start_ts 对齐 15m boundary → 同一 bar 的输入 window 确定
    - UPSERT ON CONFLICT (symbol, ts) DO UPDATE → 多次跑结果一致
    - EMA 递归 / baseline 读 silver 自身上一行 → 冷启动 seed 一次后稳定
    - 所有 _build_* 读 Bronze / staging 是纯只读 → 无 side effect

失败处理:
    - Bronze 数据全空: silver row 仍写入 (NULL 指标 + quality_flags='*_no_data')
    - SQL error: log.exception + flag=['etl_failed:<table>'], 其他表继续

方言:
    - 生产走 PostgreSQL;EXTRACT, ARRAY_AGG, FILTER, PERCENTILE_CONT, STDDEV
      等用 pg 原生语法
    - 单元测试用 SQLite in-memory + @compiles override (复用 Stage 1
      _make_sqlite_engine), 避开 pg-only 聚合时退化为 Python-side computation
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Metrics protocol — duck-typed MetricsRegistry hook (Stage 4)
# ─────────────────────────────────────────────────────────────────────


class _MetricsLike(Protocol):
    """Minimal interface mirroring aats.bootstrap.metrics.MetricsRegistry.

    Declared here (not imported) so Silver ETL stays decoupled from the
    bootstrap runtime and can be unit-tested without MetricsRegistry.
    """

    def increment(self, metric_name: str, value: int = 1) -> None: ...


def _record_metric(
    registry: "_MetricsLike | None",
    name: str,
    value: int = 1,
) -> None:
    """Best-effort metric increment; never throws."""
    if registry is None:
        return
    try:
        registry.increment(name, value)
    except Exception:  # pragma: no cover — metrics never block ETL
        pass


# ─────────────────────────────────────────────────────────────────────
# 常量 / 配置
# ─────────────────────────────────────────────────────────────────────

BAR_SECONDS = 15 * 60  # 15 min

#: EMA-20 smoothing factor. α = 2/(N+1) = 2/21 ≈ 0.0952 (standard TA convention)
_EMA20_ALPHA = Decimal("2") / Decimal("21")

#: Volume profile 冷启动阈值: 不足 4 周历史不计算 z-score
_BASELINE_WEEKS_REQUIRED = 4

#: Cascade 检测: 15m 窗口内 >= 该清算笔数触发 cascade_flag (BTC 常态 ~5-20,
#: cascade 事件常 50+)。Stage 3 用固定阈值 30, Phase 2A 评估是否改为
#: 相对于 7d 历史 p95 的动态阈值。
_LIQ_CASCADE_THRESHOLD = 30

#: Whale 阈值: 若本 bar 无历史 1h rolling 基准(冷启动), 退化到固定
#: 阈值 (BTC-USDT-SWAP: contract size ≈ 0.01 BTC, p99 单笔常 > 10 contract)。
#: Phase 1 先用 2.0 (保守), Phase 2A 替换为 rolling p99。
_WHALE_SIZE_FALLBACK = Decimal("2.0")

#: 本 Silver ETL 产出的 dataset_version (与既有 candles/funding Silver 独立)
DEFAULT_DATASET_VERSION = "p1d_microstructure_v1.0"


# ─────────────────────────────────────────────────────────────────────
# 结果数据类
# ─────────────────────────────────────────────────────────────────────


@dataclass
class SilverMicrostructureResult:
    """build_silver_microstructure_15m 的聚合返回。

    Attributes
    ----------
    symbol, bar_start_ts, bar_end_ts
        识别该 bar 的三元组
    tables_written : dict[str, int]
        每张 Silver 表实际 UPSERT 的 rowcount (0 或 1, 因为一 bar 一 row)
    tables_failed : list[str]
        所有 written=0 或 flags 含 ``etl_failed:<table>`` 的表名 (不含前缀
        schema)。P0-a 之后 runner 用此字段决定 exit code: 非空 → partial
        fail 走 exit 2, total_error 非 None → full fail 走 exit 3。
    quality_flags : list[str]
        5 张表产生的 quality_flag 合并; 用于 observability 告警
    duration_seconds : float
        end-to-end 耗时 (含 commit 前的聚合 SQL)
    error : str | None
        若 total 失败, 存第一个异常 repr; 单张表失败也会填到 flags。
    """
    symbol: str
    bar_start_ts: datetime
    bar_end_ts: datetime
    tables_written: dict[str, int] = field(default_factory=dict)
    tables_failed: list[str] = field(default_factory=list)
    quality_flags: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    error: str | None = None


# ─────────────────────────────────────────────────────────────────────
# 时间工具
# ─────────────────────────────────────────────────────────────────────


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_bar_alignment(bar_start_ts: datetime, bar_end_ts: datetime) -> None:
    """校验 bar_start / bar_end 严格对齐 15m boundary + 跨度 = 15min。

    Raises
    ------
    ValueError
        bar_start 的 minute/second/microsecond 不对齐 15m, 或 bar_end
        与 bar_start 跨度不是 15min。
    """
    if bar_start_ts.tzinfo is None:
        raise ValueError(
            f"bar_start_ts must be timezone-aware UTC, got naive: {bar_start_ts!r}"
        )
    if bar_start_ts.minute % 15 != 0:
        raise ValueError(
            f"bar_start_ts.minute must be multiple of 15, got {bar_start_ts.minute}"
        )
    if bar_start_ts.second != 0 or bar_start_ts.microsecond != 0:
        raise ValueError(
            f"bar_start_ts must have second=0 microsecond=0, got {bar_start_ts!r}"
        )
    expected_end = bar_start_ts + timedelta(seconds=BAR_SECONDS)
    if bar_end_ts != expected_end:
        raise ValueError(
            f"bar_end_ts must be bar_start_ts + 15min, got "
            f"start={bar_start_ts!r} end={bar_end_ts!r} (expected {expected_end!r})"
        )


# ─────────────────────────────────────────────────────────────────────
# 工具: fetch 上一 bar silver row (for EMA 递归 / baseline 读取)
# ─────────────────────────────────────────────────────────────────────


def _fetch_previous_orderbook_row(
    session: Session, *, symbol: str, bar_start_ts: datetime,
) -> Any | None:
    prev_ts = bar_start_ts - timedelta(seconds=BAR_SECONDS)
    row = session.execute(
        text(
            "SELECT top5_imbalance_mean, top5_imbalance_ema "
            "FROM silver.market_orderbook_metrics_15m "
            "WHERE symbol = :sym AND ts = :prev_ts"
        ),
        {"sym": symbol, "prev_ts": prev_ts},
    ).fetchone()
    return row


def _fetch_previous_oi_row(
    session: Session, *, symbol: str, bar_start_ts: datetime,
) -> Any | None:
    prev_ts = bar_start_ts - timedelta(seconds=BAR_SECONDS)
    row = session.execute(
        text(
            "SELECT oi_close, oi_ema_20 "
            "FROM silver.market_oi_funding_metrics_15m "
            "WHERE symbol = :sym AND ts = :prev_ts"
        ),
        {"sym": symbol, "prev_ts": prev_ts},
    ).fetchone()
    return row


def _compute_ema(
    prev_ema: Decimal | None,
    prev_sample: Decimal | None,
    current: Decimal | None,
    *,
    alpha: Decimal = _EMA20_ALPHA,
) -> tuple[Decimal | None, bool]:
    """EMA 递归: ema_t = α * current + (1-α) * ema_{t-1}.

    冷启动 (prev_ema 为 None):
        若 prev_sample 有值, 用 (prev_sample + current) / 2 做 SMA-2 seed,
        返回 seeded=True 让 caller 打 quality_flag='ema_seed_from_sma'。
        若 prev_sample 也为 None, 退化为 current 本身作为第一个 sample。
    """
    if current is None:
        return prev_ema, False
    if prev_ema is None:
        if prev_sample is None:
            return current, True
        seed = (prev_sample + current) / Decimal("2")
        return seed, True
    ema = alpha * current + (Decimal("1") - alpha) * prev_ema
    return ema, False


# ─────────────────────────────────────────────────────────────────────
# §5.1 _build_orderbook_metrics
# ─────────────────────────────────────────────────────────────────────


def _build_orderbook_metrics(
    *,
    session: Session,
    symbol: str,
    bar_start: datetime,
    bar_end: datetime,
    ingest_run_id: str,
    dataset_version: str,
    flags: list[str],
) -> int:
    """聚合 bronze.market_orderbook_bbo + bronze.market_orderbook_books5 → silver.market_orderbook_metrics_15m.

    注意:
        - GENERATED STORED 列 bbo.imbalance / mid / spread 由 Bronze 层预算好,
          Silver 直接 SELECT 用,不重算
        - top5 depth = sum(bid_sz_1..5) / sum(ask_sz_1..5), 2-5 档 COALESCE 0
        - spread_bps 由 (spread / mid) * 10000 算, NULL-safe
        - top5_imbalance_ema 用上一 bar 的 ema + 本 bar mean 做递归
        - 空 bar 时打 flag='orderbook_bbo_no_data'/'orderbook_books5_no_data'
    """
    # ── 1. BBO 聚合 ────────────────────────────────────────────────
    bbo = session.execute(
        text("""
            SELECT
                COUNT(*) AS n,
                AVG(imbalance) AS imb_mean,
                -- pg stddev_samp needs >=2 samples; return NULL for n<2 handled in Python
                CASE WHEN COUNT(*) > 1 THEN STDDEV_SAMP(imbalance) ELSE NULL END AS imb_std,
                AVG(CASE WHEN spread > 0 AND mid > 0
                         THEN (spread / mid) * 10000
                         ELSE NULL END) AS spread_bps_mean,
                MAX(CASE WHEN spread > 0 AND mid > 0
                         THEN (spread / mid) * 10000
                         ELSE NULL END) AS spread_bps_max,
                MIN(CASE WHEN spread > 0 AND mid > 0
                         THEN (spread / mid) * 10000
                         ELSE NULL END) AS spread_bps_min
            FROM bronze.market_orderbook_bbo
            WHERE symbol = :sym AND ts >= :bs AND ts < :be
        """),
        {"sym": symbol, "bs": bar_start, "be": bar_end},
    ).fetchone()

    # 用两条 SQL 分别取 last imbalance / last mid 避免 window fn 兼容问题
    # (SQLite 的 FIRST_VALUE over 在 sqlite3 3.31+ 才稳, 用 ORDER BY LIMIT 1 更保险)
    last_row = session.execute(
        text(
            "SELECT imbalance, mid FROM bronze.market_orderbook_bbo "
            "WHERE symbol = :sym AND ts >= :bs AND ts < :be "
            "ORDER BY ts DESC LIMIT 1"
        ),
        {"sym": symbol, "bs": bar_start, "be": bar_end},
    ).fetchone()

    bbo_n = int(bbo.n or 0)
    if bbo_n == 0:
        flags.append("orderbook_bbo_no_data")

    # ── 2. Books5 聚合 ─────────────────────────────────────────────
    books5 = session.execute(
        text("""
            SELECT
                COUNT(*) AS n,
                AVG(
                    bid_sz_1 * bid_px_1 +
                    COALESCE(bid_sz_2, 0) * COALESCE(bid_px_2, 0) +
                    COALESCE(bid_sz_3, 0) * COALESCE(bid_px_3, 0) +
                    COALESCE(bid_sz_4, 0) * COALESCE(bid_px_4, 0) +
                    COALESCE(bid_sz_5, 0) * COALESCE(bid_px_5, 0)
                ) AS bid_depth_ccy,
                AVG(
                    ask_sz_1 * ask_px_1 +
                    COALESCE(ask_sz_2, 0) * COALESCE(ask_px_2, 0) +
                    COALESCE(ask_sz_3, 0) * COALESCE(ask_px_3, 0) +
                    COALESCE(ask_sz_4, 0) * COALESCE(ask_px_4, 0) +
                    COALESCE(ask_sz_5, 0) * COALESCE(ask_px_5, 0)
                ) AS ask_depth_ccy,
                AVG(
                    CASE
                        WHEN (bid_sz_1 + COALESCE(bid_sz_2,0) + COALESCE(bid_sz_3,0)
                              + COALESCE(bid_sz_4,0) + COALESCE(bid_sz_5,0)
                              + ask_sz_1 + COALESCE(ask_sz_2,0) + COALESCE(ask_sz_3,0)
                              + COALESCE(ask_sz_4,0) + COALESCE(ask_sz_5,0)) > 0
                        THEN (
                            (bid_sz_1 + COALESCE(bid_sz_2,0) + COALESCE(bid_sz_3,0)
                             + COALESCE(bid_sz_4,0) + COALESCE(bid_sz_5,0))
                            -
                            (ask_sz_1 + COALESCE(ask_sz_2,0) + COALESCE(ask_sz_3,0)
                             + COALESCE(ask_sz_4,0) + COALESCE(ask_sz_5,0))
                        ) * 1.0 / (
                            (bid_sz_1 + COALESCE(bid_sz_2,0) + COALESCE(bid_sz_3,0)
                             + COALESCE(bid_sz_4,0) + COALESCE(bid_sz_5,0))
                            +
                            (ask_sz_1 + COALESCE(ask_sz_2,0) + COALESCE(ask_sz_3,0)
                             + COALESCE(ask_sz_4,0) + COALESCE(ask_sz_5,0))
                        )
                        ELSE NULL
                    END
                ) AS imb_mean
            FROM bronze.market_orderbook_books5
            WHERE symbol = :sym AND ts >= :bs AND ts < :be
        """),
        {"sym": symbol, "bs": bar_start, "be": bar_end},
    ).fetchone()

    books5_n = int(books5.n or 0)
    if books5_n == 0:
        flags.append("orderbook_books5_no_data")

    # ── 3. top5 weighted imbalance (用 depth 加权) ─────────────────
    bid_depth = books5.bid_depth_ccy
    ask_depth = books5.ask_depth_ccy
    if (
        bid_depth is not None
        and ask_depth is not None
        and Decimal(bid_depth) + Decimal(ask_depth) > 0
    ):
        bd = Decimal(bid_depth)
        ad = Decimal(ask_depth)
        top5_weighted_imb = (bd - ad) / (bd + ad)
    else:
        top5_weighted_imb = None

    # ── 4. top5 EMA 递归 (读上一 bar 做 seed 或递归) ───────────────
    prev_row = _fetch_previous_orderbook_row(
        session, symbol=symbol, bar_start_ts=bar_start,
    )
    prev_ema = (
        Decimal(prev_row.top5_imbalance_ema)
        if prev_row is not None and prev_row.top5_imbalance_ema is not None
        else None
    )
    prev_sample = (
        Decimal(prev_row.top5_imbalance_mean)
        if prev_row is not None and prev_row.top5_imbalance_mean is not None
        else None
    )
    current = Decimal(books5.imb_mean) if books5.imb_mean is not None else None
    top5_ema, ema_seeded = _compute_ema(prev_ema, prev_sample, current)
    if ema_seeded and current is not None:
        flags.append("ema_seed_from_sma")

    # ── 5. UPSERT ──────────────────────────────────────────────────
    params = {
        "symbol": symbol,
        "ts": bar_start,
        "bbo_imbalance_mean": bbo.imb_mean,
        "bbo_imbalance_std": bbo.imb_std,
        "bbo_imbalance_last": (last_row.imbalance if last_row is not None else None),
        "bbo_samples_n": bbo_n,
        "top5_bid_depth_ccy": bid_depth,
        "top5_ask_depth_ccy": ask_depth,
        "top5_imbalance_mean": books5.imb_mean,
        "top5_imbalance_ema": top5_ema,
        "top5_weighted_imbalance": top5_weighted_imb,
        "books5_samples_n": books5_n,
        "spread_bps_mean": bbo.spread_bps_mean,
        "spread_bps_max": bbo.spread_bps_max,
        "spread_bps_min": bbo.spread_bps_min,
        "mid_price_last": (last_row.mid if last_row is not None else None),
        "ingest_run_id": ingest_run_id,
        "dataset_version": dataset_version,
        "quality_flags": _quality_flags_for_table(flags, "orderbook"),
        "now": _utc_now(),
    }
    _upsert_silver_orderbook(session, params)
    return 1


# ─────────────────────────────────────────────────────────────────────
# §5.2 _build_trade_flow
# ─────────────────────────────────────────────────────────────────────


def _build_trade_flow(
    *,
    session: Session,
    symbol: str,
    bar_start: datetime,
    bar_end: datetime,
    ingest_run_id: str,
    dataset_version: str,
    flags: list[str],
    mid_price_ref: Any = None,
) -> int:
    """聚合 bronze.market_trades → silver.market_trade_flow_15m.

    notional (ccy) = px * sz (USDT for BTC-USDT-SWAP contract layer)
    taker side: OKX 的 side 字段 = 'buy' 表示 taker buy (吃 ask),
                'sell' 表示 taker sell (吃 bid)

    Size distribution: Python-side 排序后取 p50/p95/p99 (PostgreSQL
    percentile_cont 与 SQLite 语法差异大, 用 Python 保方言无关)。

    Whale detection: 若本 bar mean_size ≥ _WHALE_SIZE_FALLBACK * 3 才
    分组;Phase 2A 改为读 1h rolling p99。
    """
    rows = session.execute(
        text(
            "SELECT ts, px, sz, side FROM bronze.market_trades "
            "WHERE symbol = :sym AND ts >= :bs AND ts < :be "
            "ORDER BY ts"
        ),
        {"sym": symbol, "bs": bar_start, "be": bar_end},
    ).fetchall()

    if not rows:
        flags.append("trades_no_data")
        params = _trade_flow_null_params(
            symbol, bar_start, ingest_run_id, dataset_version, flags, mid_price_ref,
        )
        _upsert_silver_trade_flow(session, params)
        return 1

    sizes: list[Decimal] = []
    notional_buy = Decimal("0")
    notional_sell = Decimal("0")
    notional_total = Decimal("0")
    vwap_num = Decimal("0")
    vwap_den = Decimal("0")
    count_total = 0
    for r in rows:
        px = Decimal(r.px)
        sz = Decimal(r.sz)
        notional = px * sz
        sizes.append(sz)
        vwap_num += notional * px
        vwap_den += notional
        notional_total += notional
        count_total += 1
        if r.side == "buy":
            notional_buy += notional
        elif r.side == "sell":
            notional_sell += notional

    # Size distribution (Python-side sort)
    sizes_sorted = sorted(sizes)
    mean_size = sum(sizes, Decimal("0")) / len(sizes)
    p50 = _percentile(sizes_sorted, 0.50)
    p95 = _percentile(sizes_sorted, 0.95)
    p99 = _percentile(sizes_sorted, 0.99)
    max_size = sizes_sorted[-1]

    # Whale: 本 bar 超 _WHALE_SIZE_FALLBACK 的 trades (Phase 1 保守)
    whale_threshold = _WHALE_SIZE_FALLBACK
    whale_count = 0
    whale_buy = Decimal("0")
    whale_sell = Decimal("0")
    for r in rows:
        sz = Decimal(r.sz)
        if sz >= whale_threshold:
            whale_count += 1
            n = Decimal(r.px) * sz
            if r.side == "buy":
                whale_buy += n
            elif r.side == "sell":
                whale_sell += n

    whale_total = whale_buy + whale_sell
    whale_dir = (whale_buy - whale_sell) / whale_total if whale_total > 0 else None

    # Taker ratio / TFI / log-TFI
    buy_plus_sell = notional_buy + notional_sell
    taker_buy_ratio = (
        notional_buy / buy_plus_sell if buy_plus_sell > 0 else None
    )
    tfi = (
        (notional_buy - notional_sell) / buy_plus_sell if buy_plus_sell > 0 else None
    )
    # log TFI clipped to [-5, 5]
    if notional_buy > 0 and notional_sell > 0:
        ratio = float(notional_buy / notional_sell)
        log_tfi = max(-5.0, min(5.0, math.log(ratio)))
        log_tfi_dec = Decimal(str(round(log_tfi, 8)))
    else:
        log_tfi_dec = None

    vwap = vwap_num / vwap_den if vwap_den > 0 else None
    vwap_minus_mid_bps: Decimal | None = None
    if vwap is not None and mid_price_ref is not None:
        mid = Decimal(mid_price_ref)
        if mid > 0:
            vwap_minus_mid_bps = (vwap - mid) / mid * Decimal("10000")

    params = {
        "symbol": symbol,
        "ts": bar_start,
        "total_volume_ccy": notional_total,
        "buy_volume_ccy": notional_buy,
        "sell_volume_ccy": notional_sell,
        "trade_count": count_total,
        "taker_buy_ratio": taker_buy_ratio,
        "trade_flow_imbalance": tfi,
        "log_tfi": log_tfi_dec,
        "mean_trade_size": mean_size,
        "p50_trade_size": p50,
        "p95_trade_size": p95,
        "p99_trade_size": p99,
        "max_trade_size": max_size,
        "whale_threshold_applied": whale_threshold,
        "whale_count": whale_count,
        "whale_buy_volume_ccy": whale_buy,
        "whale_sell_volume_ccy": whale_sell,
        "whale_direction": whale_dir,
        "vwap": vwap,
        "mid_price_ref": mid_price_ref,
        "vwap_minus_mid_bps": vwap_minus_mid_bps,
        "ingest_run_id": ingest_run_id,
        "dataset_version": dataset_version,
        "quality_flags": _quality_flags_for_table(flags, "trade_flow"),
        "now": _utc_now(),
    }
    _upsert_silver_trade_flow(session, params)
    return 1


def _percentile(sorted_values: list[Decimal], p: float) -> Decimal:
    """Python-side linear percentile (方言无关)。sorted_values 必须已排序。"""
    if not sorted_values:
        raise ValueError("empty sequence")
    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]
    k = (n - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_values[int(k)]
    lo = sorted_values[f]
    hi = sorted_values[c]
    return lo + (hi - lo) * Decimal(str(k - f))


def _trade_flow_null_params(
    symbol: str,
    bar_start: datetime,
    ingest_run_id: str,
    dataset_version: str,
    flags: list[str],
    mid_price_ref: Any,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "ts": bar_start,
        "total_volume_ccy": None,
        "buy_volume_ccy": None,
        "sell_volume_ccy": None,
        "trade_count": 0,
        "taker_buy_ratio": None,
        "trade_flow_imbalance": None,
        "log_tfi": None,
        "mean_trade_size": None,
        "p50_trade_size": None,
        "p95_trade_size": None,
        "p99_trade_size": None,
        "max_trade_size": None,
        "whale_threshold_applied": None,
        "whale_count": 0,
        "whale_buy_volume_ccy": None,
        "whale_sell_volume_ccy": None,
        "whale_direction": None,
        "vwap": None,
        "mid_price_ref": mid_price_ref,
        "vwap_minus_mid_bps": None,
        "ingest_run_id": ingest_run_id,
        "dataset_version": dataset_version,
        "quality_flags": _quality_flags_for_table(flags, "trade_flow"),
        "now": _utc_now(),
    }


# ─────────────────────────────────────────────────────────────────────
# §5.3 _build_oi_funding_metrics
# ─────────────────────────────────────────────────────────────────────


def _build_oi_funding_metrics(
    *,
    session: Session,
    symbol: str,
    bar_start: datetime,
    bar_end: datetime,
    ingest_run_id: str,
    dataset_version: str,
    flags: list[str],
    mid_price_ref: Any = None,
) -> int:
    """聚合 staging.market_oi_funding_ticks → silver.market_oi_funding_metrics_15m.

    OI 四价: open/close/high/low 从 bar 内 tick_type='oi' 序列取
    Funding: 最后一个 tick_type='funding' 的 funding_rate / next 值
    Mark: 最后一个 tick_type='mark' 的 mark_px
    Basis: (mark - mid) / mid * 10000
    EMA-20: 用上一 bar 的 oi_close + oi_ema_20 递归
    Price-OI regime: price_change_bps vs oi_delta 符号矩阵
    Funding z-score 7d: 本 bar funding vs 前 7d 同 symbol 的均值/std
                       (样本不足 5 个时 NULL + flag='partial_data')
    """
    # ── OI 聚合 ──────────────────────────────────────────────────
    oi_agg = session.execute(
        text("""
            SELECT
                COUNT(*) AS n,
                MAX(oi) AS oi_high,
                MIN(oi) AS oi_low
            FROM staging.market_oi_funding_ticks
            WHERE symbol = :sym AND tick_type = 'oi'
              AND ts >= :bs AND ts < :be AND oi IS NOT NULL
        """),
        {"sym": symbol, "bs": bar_start, "be": bar_end},
    ).fetchone()

    oi_first = session.execute(
        text(
            "SELECT oi FROM staging.market_oi_funding_ticks "
            "WHERE symbol = :sym AND tick_type = 'oi' "
            "AND ts >= :bs AND ts < :be AND oi IS NOT NULL "
            "ORDER BY ts ASC LIMIT 1"
        ),
        {"sym": symbol, "bs": bar_start, "be": bar_end},
    ).fetchone()
    oi_last = session.execute(
        text(
            "SELECT oi FROM staging.market_oi_funding_ticks "
            "WHERE symbol = :sym AND tick_type = 'oi' "
            "AND ts >= :bs AND ts < :be AND oi IS NOT NULL "
            "ORDER BY ts DESC LIMIT 1"
        ),
        {"sym": symbol, "bs": bar_start, "be": bar_end},
    ).fetchone()

    oi_n = int(oi_agg.n or 0)
    oi_open = oi_first.oi if oi_first is not None else None
    oi_close = oi_last.oi if oi_last is not None else None
    if oi_n == 0:
        flags.append("oi_no_data")

    oi_delta = None
    if (
        oi_open is not None
        and oi_close is not None
        and Decimal(oi_open) > 0
    ):
        oi_delta = (Decimal(oi_close) - Decimal(oi_open)) / Decimal(oi_open)

    # EMA-20 递归
    prev_oi_row = _fetch_previous_oi_row(
        session, symbol=symbol, bar_start_ts=bar_start,
    )
    prev_ema = (
        Decimal(prev_oi_row.oi_ema_20)
        if prev_oi_row is not None and prev_oi_row.oi_ema_20 is not None
        else None
    )
    prev_sample = (
        Decimal(prev_oi_row.oi_close)
        if prev_oi_row is not None and prev_oi_row.oi_close is not None
        else None
    )
    oi_close_dec = Decimal(oi_close) if oi_close is not None else None
    oi_ema, ema_seeded = _compute_ema(prev_ema, prev_sample, oi_close_dec)
    if ema_seeded and oi_close_dec is not None:
        flags.append("ema_seed_from_sma")

    oi_delta_vs_ema = None
    if oi_ema is not None and oi_ema > 0 and oi_close_dec is not None:
        oi_delta_vs_ema = (oi_close_dec - oi_ema) / oi_ema

    # ── Funding: last value per bar ─────────────────────────────
    funding_row = session.execute(
        text(
            "SELECT funding_rate, next_funding_rate, next_funding_time "
            "FROM staging.market_oi_funding_ticks "
            "WHERE symbol = :sym AND tick_type = 'funding' "
            "AND ts >= :bs AND ts < :be "
            "ORDER BY ts DESC LIMIT 1"
        ),
        {"sym": symbol, "bs": bar_start, "be": bar_end},
    ).fetchone()

    if funding_row is None:
        flags.append("funding_no_data")
        funding_rate = None
        funding_next_est = None
        next_funding_time = None
    else:
        funding_rate = funding_row.funding_rate
        funding_next_est = funding_row.next_funding_rate
        next_funding_time = funding_row.next_funding_time

    # ── Mark: last value per bar ────────────────────────────────
    mark_row = session.execute(
        text(
            "SELECT mark_px FROM staging.market_oi_funding_ticks "
            "WHERE symbol = :sym AND tick_type = 'mark' "
            "AND ts >= :bs AND ts < :be AND mark_px IS NOT NULL "
            "ORDER BY ts DESC LIMIT 1"
        ),
        {"sym": symbol, "bs": bar_start, "be": bar_end},
    ).fetchone()

    if mark_row is None:
        flags.append("mark_no_data")
        mark_price = None
    else:
        mark_price = mark_row.mark_px

    # Basis (mark vs mid)
    basis_bps = None
    if (
        mark_price is not None
        and mid_price_ref is not None
        and Decimal(mid_price_ref) > 0
    ):
        basis_bps = (
            (Decimal(mark_price) - Decimal(mid_price_ref))
            / Decimal(mid_price_ref)
            * Decimal("10000")
        )

    # Funding z-score 7d (读 self Silver 前 7d 同 symbol 的 funding_rate_current)
    funding_z = None
    funding_dev_30d = None
    if funding_rate is not None:
        z_stats = session.execute(
            text(
                "SELECT COUNT(*) AS n, AVG(funding_rate_current) AS mu, "
                "STDDEV_SAMP(funding_rate_current) AS sd "
                "FROM silver.market_oi_funding_metrics_15m "
                "WHERE symbol = :sym "
                "AND ts >= :start_7d AND ts < :bs "
                "AND funding_rate_current IS NOT NULL"
            ),
            {
                "sym": symbol,
                "start_7d": bar_start - timedelta(days=7),
                "bs": bar_start,
            },
        ).fetchone()
        if (
            z_stats is not None
            and z_stats.n is not None
            and int(z_stats.n) >= 5
            and z_stats.sd is not None
            and Decimal(z_stats.sd) > 0
        ):
            funding_z = (Decimal(funding_rate) - Decimal(z_stats.mu)) / Decimal(z_stats.sd)
        else:
            if funding_rate is not None:
                flags.append("partial_data")

    # minutes_to_next_funding
    minutes_to_next = None
    if next_funding_time is not None:
        delta = next_funding_time - bar_end
        minutes_to_next = max(0, int(delta.total_seconds() // 60))

    # Price change bps (需要 mid_price_ref 现在 + 上一 bar)
    price_change_bps = None
    if mid_price_ref is not None and prev_oi_row is not None:
        # 这里用 oi_funding 上一 bar 的 mid_price_ref (存在 oi_funding 行里) 做对比
        # Stage 3 v1: 只有 mid_price_ref_current, price_change 留 NULL,
        # Phase 2A 回填时可考虑加 mid_price_ref_prev 列。
        pass

    # Price-OI regime (简化版: 仅基于 oi_delta 符号)
    oi_price_regime = None
    if oi_delta is not None:
        if oi_delta > Decimal("0.005"):
            oi_price_regime = "trend_long"  # OI 增 + 价涨的 fallback,缺 price 用 OI 决定
        elif oi_delta < Decimal("-0.005"):
            oi_price_regime = "long_cover"
        else:
            oi_price_regime = "flat"

    params = {
        "symbol": symbol,
        "ts": bar_start,
        "oi_open": oi_open,
        "oi_close": oi_close,
        "oi_high": oi_agg.oi_high,
        "oi_low": oi_agg.oi_low,
        "oi_delta": oi_delta,
        "oi_samples_n": oi_n,
        "oi_ema_20": oi_ema,
        "oi_delta_vs_ema": oi_delta_vs_ema,
        "price_change_bps": price_change_bps,
        "oi_price_regime": oi_price_regime,
        "funding_rate_current": funding_rate,
        "funding_rate_next_est": funding_next_est,
        "funding_z_score_7d": funding_z,
        "funding_deviation_30d": funding_dev_30d,
        "minutes_to_next_funding": minutes_to_next,
        "mark_price": mark_price,
        "mid_price_ref": mid_price_ref,
        "basis_bps": basis_bps,
        "ingest_run_id": ingest_run_id,
        "dataset_version": dataset_version,
        "quality_flags": _quality_flags_for_table(flags, "oi_funding"),
        "now": _utc_now(),
    }
    _upsert_silver_oi_funding(session, params)
    return 1


# ─────────────────────────────────────────────────────────────────────
# §5.4 _build_volume_profile
# ─────────────────────────────────────────────────────────────────────


def _build_volume_profile(
    *,
    session: Session,
    symbol: str,
    bar_start: datetime,
    bar_end: datetime,
    ingest_run_id: str,
    dataset_version: str,
    flags: list[str],
) -> int:
    """聚合 bronze.market_trades → silver.market_volume_profile_15m.

    volume_ccy 由本 bar trades 聚合 (不依赖上游 trade_flow Silver)。
    baseline: 读 silver 自身前 N 周同 dow/hod/15min slot 的 rows 做 mean/std;
              样本数 < 4 则 baseline_sample_weeks < 4, z_score=NULL + flag。

    dow_hod_slot 格式: "mon_13:00" — 星期一 13:00 UTC 起点的 15min 槽位。
    """
    # ── 本 bar volume ──────────────────────────────────────────
    agg = session.execute(
        text(
            "SELECT COUNT(*) AS n, COALESCE(SUM(px * sz), 0) AS volume_ccy "
            "FROM bronze.market_trades "
            "WHERE symbol = :sym AND ts >= :bs AND ts < :be"
        ),
        {"sym": symbol, "bs": bar_start, "be": bar_end},
    ).fetchone()

    trade_count = int(agg.n or 0)
    volume_ccy = agg.volume_ccy  # 可能是 0 或 Decimal

    if trade_count == 0:
        flags.append("trades_no_data")

    # dow_hod_slot: 用 bar_start 的 UTC weekday + HH:MM (00/15/30/45)
    dow = bar_start.strftime("%a").lower()  # mon/tue/...
    hhmm = bar_start.strftime("%H:%M")
    dow_hod_slot = f"{dow}_{hhmm}"

    # baseline: 扫 silver 自身前 4 周同 slot 的 rows
    baseline_rows = session.execute(
        text(
            "SELECT volume_ccy FROM silver.market_volume_profile_15m "
            "WHERE symbol = :sym "
            "AND dow_hod_slot = :slot "
            "AND ts >= :start_4w AND ts < :bs "
            "AND volume_ccy IS NOT NULL"
        ),
        {
            "sym": symbol,
            "slot": dow_hod_slot,
            "start_4w": bar_start - timedelta(weeks=_BASELINE_WEEKS_REQUIRED),
            "bs": bar_start,
        },
    ).fetchall()

    sample_weeks = len(baseline_rows)
    expected_volume: Decimal | None = None
    expected_std: Decimal | None = None
    volume_z: Decimal | None = None
    volume_spike = False

    if sample_weeks >= _BASELINE_WEEKS_REQUIRED:
        values = [Decimal(r.volume_ccy) for r in baseline_rows]
        mu = sum(values, Decimal("0")) / len(values)
        if len(values) > 1:
            variance = sum(
                (v - mu) ** 2 for v in values
            ) / (len(values) - 1)
            # Decimal 没 sqrt, 走 float
            sigma = Decimal(str(math.sqrt(float(variance))))
        else:
            sigma = Decimal("0")
        expected_volume = mu
        expected_std = sigma
        if volume_ccy is not None and sigma > 0:
            volume_z = (Decimal(volume_ccy) - mu) / sigma
            volume_spike = volume_z >= Decimal("2.0")
    else:
        flags.append("partial_baseline")

    # Interaction: vol_weighted_tfi = read trade_flow Silver 同 bar 的 TFI
    tfi_row = session.execute(
        text(
            "SELECT trade_flow_imbalance FROM silver.market_trade_flow_15m "
            "WHERE symbol = :sym AND ts = :bs"
        ),
        {"sym": symbol, "bs": bar_start},
    ).fetchone()

    vol_weighted_tfi = None
    if (
        tfi_row is not None
        and tfi_row.trade_flow_imbalance is not None
        and volume_ccy is not None
    ):
        vol_weighted_tfi = Decimal(tfi_row.trade_flow_imbalance) * Decimal(volume_ccy)

    params = {
        "symbol": symbol,
        "ts": bar_start,
        "volume_ccy": volume_ccy,
        "trade_count": trade_count,
        "expected_volume_ccy": expected_volume,
        "expected_volume_std": expected_std,
        "volume_z_score": volume_z,
        "volume_spike_flag": volume_spike,
        "dow_hod_slot": dow_hod_slot,
        "vol_weighted_tfi": vol_weighted_tfi,
        "baseline_sample_weeks": sample_weeks,
        "ingest_run_id": ingest_run_id,
        "dataset_version": dataset_version,
        "quality_flags": _quality_flags_for_table(flags, "volume_profile"),
        "now": _utc_now(),
    }
    _upsert_silver_volume_profile(session, params)
    return 1


# ─────────────────────────────────────────────────────────────────────
# §5.5 _build_liquidation_metrics
# ─────────────────────────────────────────────────────────────────────


def _build_liquidation_metrics(
    *,
    session: Session,
    symbol: str,
    bar_start: datetime,
    bar_end: datetime,
    ingest_run_id: str,
    dataset_version: str,
    flags: list[str],
) -> int:
    """聚合 staging.raw_liquidations → silver.market_liquidation_metrics_15m.

    OKX liquidation-orders convention:
        side='sell' = 长仓被强平 (long liquidation)
        side='buy'  = 短仓被强平 (short liquidation)
    对齐 OKX API 文档: https://www.okx.com/docs-v5/en/#public-data-websocket-liquidation-orders-channel

    notional_usd = bk_px * sz (USD approx for BTC-USDT-SWAP)
    cascade_flag = count >= _LIQ_CASCADE_THRESHOLD
    intensity_z_7d = (total_notional - μ_7d) / σ_7d (自 Silver 前 7d 同 symbol)
    """
    rows = session.execute(
        text(
            "SELECT side, bk_px, sz FROM staging.raw_liquidations "
            "WHERE inst_id = :sym AND ts >= :bs AND ts < :be"
        ),
        {"sym": symbol, "bs": bar_start, "be": bar_end},
    ).fetchall()

    if not rows:
        flags.append("liquidation_no_data")

    long_count = 0
    short_count = 0
    long_notional = Decimal("0")
    short_notional = Decimal("0")
    max_single = Decimal("0")

    for r in rows:
        px = Decimal(r.bk_px)
        sz = Decimal(r.sz)
        notional = px * sz
        if notional > max_single:
            max_single = notional
        if r.side == "sell":   # long liquidation
            long_count += 1
            long_notional += notional
        elif r.side == "buy":  # short liquidation
            short_count += 1
            short_notional += notional

    total_count = long_count + short_count
    total_notional = long_notional + short_notional
    imbalance = None
    if total_notional > 0:
        imbalance = (long_notional - short_notional) / total_notional

    cascade_flag = total_count >= _LIQ_CASCADE_THRESHOLD

    # Intensity z-score 7d
    intensity_z = None
    if total_notional > 0:
        z_stats = session.execute(
            text(
                "SELECT COUNT(*) AS n, "
                "AVG(COALESCE(long_liq_notional_usd,0) + COALESCE(short_liq_notional_usd,0)) AS mu, "
                "STDDEV_SAMP(COALESCE(long_liq_notional_usd,0) + COALESCE(short_liq_notional_usd,0)) AS sd "
                "FROM silver.market_liquidation_metrics_15m "
                "WHERE symbol = :sym "
                "AND ts >= :start_7d AND ts < :bs"
            ),
            {
                "sym": symbol,
                "start_7d": bar_start - timedelta(days=7),
                "bs": bar_start,
            },
        ).fetchone()
        if (
            z_stats is not None
            and z_stats.n is not None
            and int(z_stats.n) >= 5
            and z_stats.sd is not None
            and Decimal(z_stats.sd) > 0
        ):
            intensity_z = (
                (total_notional - Decimal(z_stats.mu)) / Decimal(z_stats.sd)
            )

    # 若 max_single 没被赋值 (空 rows), 为 None 而非 0
    max_single_out = max_single if total_count > 0 else None

    params = {
        "symbol": symbol,
        "ts": bar_start,
        "long_liq_count": long_count,
        "short_liq_count": short_count,
        "long_liq_notional_usd": long_notional if long_count > 0 else None,
        "short_liq_notional_usd": short_notional if short_count > 0 else None,
        "liq_imbalance": imbalance,
        "max_single_liq_usd": max_single_out,
        "cascade_flag": cascade_flag,
        "cascade_threshold_used": _LIQ_CASCADE_THRESHOLD,
        "intensity_z_7d": intensity_z,
        "ingest_run_id": ingest_run_id,
        "dataset_version": dataset_version,
        "quality_flags": _quality_flags_for_table(flags, "liquidation"),
        "now": _utc_now(),
    }
    _upsert_silver_liquidation(session, params)
    return 1


# ─────────────────────────────────────────────────────────────────────
# UPSERT helpers — 每张表一个纯函数, 为 idempotency 保证 ON CONFLICT
# ─────────────────────────────────────────────────────────────────────


def _upsert_silver_orderbook(session: Session, p: dict[str, Any]) -> None:
    session.execute(
        text("""
            INSERT INTO silver.market_orderbook_metrics_15m
                (symbol, ts,
                 bbo_imbalance_mean, bbo_imbalance_std, bbo_imbalance_last, bbo_samples_n,
                 top5_bid_depth_ccy, top5_ask_depth_ccy, top5_imbalance_mean,
                 top5_imbalance_ema, top5_weighted_imbalance, books5_samples_n,
                 spread_bps_mean, spread_bps_max, spread_bps_min,
                 mid_price_last,
                 ingest_run_id, dataset_version, quality_flags,
                 created_at, updated_at)
            VALUES
                (:symbol, :ts,
                 :bbo_imbalance_mean, :bbo_imbalance_std, :bbo_imbalance_last, :bbo_samples_n,
                 :top5_bid_depth_ccy, :top5_ask_depth_ccy, :top5_imbalance_mean,
                 :top5_imbalance_ema, :top5_weighted_imbalance, :books5_samples_n,
                 :spread_bps_mean, :spread_bps_max, :spread_bps_min,
                 :mid_price_last,
                 :ingest_run_id, :dataset_version, :quality_flags,
                 :now, :now)
            ON CONFLICT (symbol, ts) DO UPDATE SET
                bbo_imbalance_mean = EXCLUDED.bbo_imbalance_mean,
                bbo_imbalance_std = EXCLUDED.bbo_imbalance_std,
                bbo_imbalance_last = EXCLUDED.bbo_imbalance_last,
                bbo_samples_n = EXCLUDED.bbo_samples_n,
                top5_bid_depth_ccy = EXCLUDED.top5_bid_depth_ccy,
                top5_ask_depth_ccy = EXCLUDED.top5_ask_depth_ccy,
                top5_imbalance_mean = EXCLUDED.top5_imbalance_mean,
                top5_imbalance_ema = EXCLUDED.top5_imbalance_ema,
                top5_weighted_imbalance = EXCLUDED.top5_weighted_imbalance,
                books5_samples_n = EXCLUDED.books5_samples_n,
                spread_bps_mean = EXCLUDED.spread_bps_mean,
                spread_bps_max = EXCLUDED.spread_bps_max,
                spread_bps_min = EXCLUDED.spread_bps_min,
                mid_price_last = EXCLUDED.mid_price_last,
                ingest_run_id = EXCLUDED.ingest_run_id,
                dataset_version = EXCLUDED.dataset_version,
                quality_flags = EXCLUDED.quality_flags,
                updated_at = EXCLUDED.updated_at
        """),
        p,
    )


def _upsert_silver_trade_flow(session: Session, p: dict[str, Any]) -> None:
    session.execute(
        text("""
            INSERT INTO silver.market_trade_flow_15m
                (symbol, ts,
                 total_volume_ccy, buy_volume_ccy, sell_volume_ccy, trade_count,
                 taker_buy_ratio, trade_flow_imbalance, log_tfi,
                 mean_trade_size, p50_trade_size, p95_trade_size, p99_trade_size, max_trade_size,
                 whale_threshold_applied, whale_count, whale_buy_volume_ccy,
                 whale_sell_volume_ccy, whale_direction,
                 vwap, mid_price_ref, vwap_minus_mid_bps,
                 ingest_run_id, dataset_version, quality_flags,
                 created_at, updated_at)
            VALUES
                (:symbol, :ts,
                 :total_volume_ccy, :buy_volume_ccy, :sell_volume_ccy, :trade_count,
                 :taker_buy_ratio, :trade_flow_imbalance, :log_tfi,
                 :mean_trade_size, :p50_trade_size, :p95_trade_size, :p99_trade_size, :max_trade_size,
                 :whale_threshold_applied, :whale_count, :whale_buy_volume_ccy,
                 :whale_sell_volume_ccy, :whale_direction,
                 :vwap, :mid_price_ref, :vwap_minus_mid_bps,
                 :ingest_run_id, :dataset_version, :quality_flags,
                 :now, :now)
            ON CONFLICT (symbol, ts) DO UPDATE SET
                total_volume_ccy = EXCLUDED.total_volume_ccy,
                buy_volume_ccy = EXCLUDED.buy_volume_ccy,
                sell_volume_ccy = EXCLUDED.sell_volume_ccy,
                trade_count = EXCLUDED.trade_count,
                taker_buy_ratio = EXCLUDED.taker_buy_ratio,
                trade_flow_imbalance = EXCLUDED.trade_flow_imbalance,
                log_tfi = EXCLUDED.log_tfi,
                mean_trade_size = EXCLUDED.mean_trade_size,
                p50_trade_size = EXCLUDED.p50_trade_size,
                p95_trade_size = EXCLUDED.p95_trade_size,
                p99_trade_size = EXCLUDED.p99_trade_size,
                max_trade_size = EXCLUDED.max_trade_size,
                whale_threshold_applied = EXCLUDED.whale_threshold_applied,
                whale_count = EXCLUDED.whale_count,
                whale_buy_volume_ccy = EXCLUDED.whale_buy_volume_ccy,
                whale_sell_volume_ccy = EXCLUDED.whale_sell_volume_ccy,
                whale_direction = EXCLUDED.whale_direction,
                vwap = EXCLUDED.vwap,
                mid_price_ref = EXCLUDED.mid_price_ref,
                vwap_minus_mid_bps = EXCLUDED.vwap_minus_mid_bps,
                ingest_run_id = EXCLUDED.ingest_run_id,
                dataset_version = EXCLUDED.dataset_version,
                quality_flags = EXCLUDED.quality_flags,
                updated_at = EXCLUDED.updated_at
        """),
        p,
    )


def _upsert_silver_oi_funding(session: Session, p: dict[str, Any]) -> None:
    session.execute(
        text("""
            INSERT INTO silver.market_oi_funding_metrics_15m
                (symbol, ts,
                 oi_open, oi_close, oi_high, oi_low, oi_delta, oi_samples_n,
                 oi_ema_20, oi_delta_vs_ema,
                 price_change_bps, oi_price_regime,
                 funding_rate_current, funding_rate_next_est,
                 funding_z_score_7d, funding_deviation_30d, minutes_to_next_funding,
                 mark_price, mid_price_ref, basis_bps,
                 ingest_run_id, dataset_version, quality_flags,
                 created_at, updated_at)
            VALUES
                (:symbol, :ts,
                 :oi_open, :oi_close, :oi_high, :oi_low, :oi_delta, :oi_samples_n,
                 :oi_ema_20, :oi_delta_vs_ema,
                 :price_change_bps, :oi_price_regime,
                 :funding_rate_current, :funding_rate_next_est,
                 :funding_z_score_7d, :funding_deviation_30d, :minutes_to_next_funding,
                 :mark_price, :mid_price_ref, :basis_bps,
                 :ingest_run_id, :dataset_version, :quality_flags,
                 :now, :now)
            ON CONFLICT (symbol, ts) DO UPDATE SET
                oi_open = EXCLUDED.oi_open,
                oi_close = EXCLUDED.oi_close,
                oi_high = EXCLUDED.oi_high,
                oi_low = EXCLUDED.oi_low,
                oi_delta = EXCLUDED.oi_delta,
                oi_samples_n = EXCLUDED.oi_samples_n,
                oi_ema_20 = EXCLUDED.oi_ema_20,
                oi_delta_vs_ema = EXCLUDED.oi_delta_vs_ema,
                price_change_bps = EXCLUDED.price_change_bps,
                oi_price_regime = EXCLUDED.oi_price_regime,
                funding_rate_current = EXCLUDED.funding_rate_current,
                funding_rate_next_est = EXCLUDED.funding_rate_next_est,
                funding_z_score_7d = EXCLUDED.funding_z_score_7d,
                funding_deviation_30d = EXCLUDED.funding_deviation_30d,
                minutes_to_next_funding = EXCLUDED.minutes_to_next_funding,
                mark_price = EXCLUDED.mark_price,
                mid_price_ref = EXCLUDED.mid_price_ref,
                basis_bps = EXCLUDED.basis_bps,
                ingest_run_id = EXCLUDED.ingest_run_id,
                dataset_version = EXCLUDED.dataset_version,
                quality_flags = EXCLUDED.quality_flags,
                updated_at = EXCLUDED.updated_at
        """),
        p,
    )


def _upsert_silver_volume_profile(session: Session, p: dict[str, Any]) -> None:
    session.execute(
        text("""
            INSERT INTO silver.market_volume_profile_15m
                (symbol, ts,
                 volume_ccy, trade_count,
                 expected_volume_ccy, expected_volume_std, volume_z_score,
                 volume_spike_flag, dow_hod_slot,
                 vol_weighted_tfi, baseline_sample_weeks,
                 ingest_run_id, dataset_version, quality_flags,
                 created_at, updated_at)
            VALUES
                (:symbol, :ts,
                 :volume_ccy, :trade_count,
                 :expected_volume_ccy, :expected_volume_std, :volume_z_score,
                 :volume_spike_flag, :dow_hod_slot,
                 :vol_weighted_tfi, :baseline_sample_weeks,
                 :ingest_run_id, :dataset_version, :quality_flags,
                 :now, :now)
            ON CONFLICT (symbol, ts) DO UPDATE SET
                volume_ccy = EXCLUDED.volume_ccy,
                trade_count = EXCLUDED.trade_count,
                expected_volume_ccy = EXCLUDED.expected_volume_ccy,
                expected_volume_std = EXCLUDED.expected_volume_std,
                volume_z_score = EXCLUDED.volume_z_score,
                volume_spike_flag = EXCLUDED.volume_spike_flag,
                dow_hod_slot = EXCLUDED.dow_hod_slot,
                vol_weighted_tfi = EXCLUDED.vol_weighted_tfi,
                baseline_sample_weeks = EXCLUDED.baseline_sample_weeks,
                ingest_run_id = EXCLUDED.ingest_run_id,
                dataset_version = EXCLUDED.dataset_version,
                quality_flags = EXCLUDED.quality_flags,
                updated_at = EXCLUDED.updated_at
        """),
        p,
    )


def _upsert_silver_liquidation(session: Session, p: dict[str, Any]) -> None:
    session.execute(
        text("""
            INSERT INTO silver.market_liquidation_metrics_15m
                (symbol, ts,
                 long_liq_count, short_liq_count,
                 long_liq_notional_usd, short_liq_notional_usd,
                 liq_imbalance, max_single_liq_usd,
                 cascade_flag, cascade_threshold_used, intensity_z_7d,
                 ingest_run_id, dataset_version, quality_flags,
                 created_at, updated_at)
            VALUES
                (:symbol, :ts,
                 :long_liq_count, :short_liq_count,
                 :long_liq_notional_usd, :short_liq_notional_usd,
                 :liq_imbalance, :max_single_liq_usd,
                 :cascade_flag, :cascade_threshold_used, :intensity_z_7d,
                 :ingest_run_id, :dataset_version, :quality_flags,
                 :now, :now)
            ON CONFLICT (symbol, ts) DO UPDATE SET
                long_liq_count = EXCLUDED.long_liq_count,
                short_liq_count = EXCLUDED.short_liq_count,
                long_liq_notional_usd = EXCLUDED.long_liq_notional_usd,
                short_liq_notional_usd = EXCLUDED.short_liq_notional_usd,
                liq_imbalance = EXCLUDED.liq_imbalance,
                max_single_liq_usd = EXCLUDED.max_single_liq_usd,
                cascade_flag = EXCLUDED.cascade_flag,
                cascade_threshold_used = EXCLUDED.cascade_threshold_used,
                intensity_z_7d = EXCLUDED.intensity_z_7d,
                ingest_run_id = EXCLUDED.ingest_run_id,
                dataset_version = EXCLUDED.dataset_version,
                quality_flags = EXCLUDED.quality_flags,
                updated_at = EXCLUDED.updated_at
        """),
        p,
    )


def _quality_flags_for_table(all_flags: list[str], table_key: str) -> list[str]:
    """过滤 shared all_flags 里只属于本 table 的 flag。

    Shared flags (etl_failed:*, partial_data, stale_source) 对所有 5 张表都
    有意义, 所以每张表的 quality_flags 列都带上它们 + 自己专属的 '*_no_data'
    / 'ema_seed_from_sma' / 'partial_baseline' 等。这里简化 Phase 1A:
    每张表都带上完整 all_flags (避免丢失诊断信息), 后续读者按 table 列自行
    过滤语义。
    """
    # 简化: 直接返回 shallow copy,避免 shared mutable alias
    return list(all_flags)


# ─────────────────────────────────────────────────────────────────────
# 总入口
# ─────────────────────────────────────────────────────────────────────


def build_silver_microstructure_15m(
    *,
    session: Session,
    symbol: str,
    bar_start_ts: datetime,
    bar_end_ts: datetime,
    ingest_run_id: str,
    dataset_version: str = DEFAULT_DATASET_VERSION,
    metrics_registry: _MetricsLike | None = None,
) -> SilverMicrostructureResult:
    """§7.1 总入口 — 聚合 5 张 Silver 15m 表。

    Parameters
    ----------
    session : sqlalchemy.orm.Session
        已 begin 的 session;caller 负责 commit。
    symbol : str
        如 'BTC-USDT-SWAP'。
    bar_start_ts, bar_end_ts : datetime
        15m bar 窗口 [start, end), bar_end_ts = bar_start_ts + 15min。
        必须 UTC aware 且 minute % 15 == 0, 否则 raise ValueError。
    ingest_run_id : str
        本次 ETL 所属 meta.ingest_runs UUID。caller 负责创建/关闭 run。
    dataset_version : str
        默认 'p1d_microstructure_v1.0'; 可由 CLI 覆盖测试。
    metrics_registry : MetricsRegistry-like, optional
        Stage 4 新增: 若传入, ETL 会递增如下 counter (Prometheus 名前缀
        自动添加 'aats_'):

        - ``microstructure_silver_etl_runs_total`` — 每次入口调用 +1
        - ``microstructure_silver_etl_runs_success_total`` — 无 table 失败时 +1
        - ``microstructure_silver_etl_errors_total`` — 有 table 失败时 +1
        - ``microstructure_silver_etl_errors_{table}_total`` — 按表拆分
        - ``microstructure_silver_rows_written_{table}_total`` — 每次 UPSERT 的
          rowcount (0 或 1) 累计

        Duration p95 不在 MetricsRegistry 中追踪 (Counter 不支持 histogram,
        见 §4.3); 走 log event ``silver_microstructure_etl`` 的 ``duration``
        字段 + Loki ``| json | unwrap duration`` 查询。
    """
    _validate_bar_alignment(bar_start_ts, bar_end_ts)

    start_time = time.monotonic()
    flags: list[str] = []
    written: dict[str, int] = {}
    tables_failed: list[str] = []
    total_error: str | None = None

    _record_metric(metrics_registry, "microstructure_silver_etl_runs_total")

    def _run_step(
        table_key: str,
        table_name: str,
        build_fn: Callable[[], int],
    ) -> None:
        """运行单个 _build_*, 用 SAVEPOINT 隔离失败。

        关键语义 (P0-a 修复前的 bug 场景):
            - 子事务失败后 *必须* rollback savepoint, 否则 PG session 会
              进入 aborted transaction 状态,后续所有 session.execute 都
              抛 InFailedSqlTransaction (live log_tail 看到的串链失败根源)
            - 用 begin_nested() 创建 SAVEPOINT, __exit__ 会自动根据异常
              rollback 到该 savepoint; 已成功的前置 step 的写入保留
            - SQLite 和 PostgreSQL 都支持 SAVEPOINT, 单测走 sqlite 无兼容问题
        """
        nonlocal total_error
        try:
            with session.begin_nested():
                written[table_name] = build_fn()
            _record_metric(
                metrics_registry,
                f"microstructure_silver_etl_runs_total_{table_key}_success",
            )
        except Exception as exc:
            flags.append(f"etl_failed:{table_key}")
            log.exception("%s build failed", table_key)
            written[table_name] = 0
            tables_failed.append(table_name)
            total_error = total_error or repr(exc)
            _record_metric(
                metrics_registry,
                f"microstructure_silver_etl_errors_{table_key}_total",
            )

    # Step 1: orderbook (provides mid_price_ref for downstream)
    mid_price_ref: Any = None
    _run_step(
        "orderbook",
        "orderbook_metrics_15m",
        lambda: _build_orderbook_metrics(
            session=session, symbol=symbol,
            bar_start=bar_start_ts, bar_end=bar_end_ts,
            ingest_run_id=ingest_run_id, dataset_version=dataset_version,
            flags=flags,
        ),
    )
    # 读刚写的 mid_price_last 供 trade_flow / oi_funding 用。
    # Step 1 失败时 row 可能是 None,mid_price_ref 保持 None, 下游 step 仍会
    # 尝试用 NULL mid 写入 (保留原有行为,只靠 SAVEPOINT 防止失败串链)。
    if written.get("orderbook_metrics_15m", 0) > 0:
        try:
            row = session.execute(
                text(
                    "SELECT mid_price_last FROM silver.market_orderbook_metrics_15m "
                    "WHERE symbol = :sym AND ts = :ts"
                ),
                {"sym": symbol, "ts": bar_start_ts},
            ).fetchone()
            if row is not None:
                mid_price_ref = row.mid_price_last
        except Exception:
            log.exception("mid_price_ref lookup after orderbook step failed")

    # Step 2: trade_flow
    _run_step(
        "trade_flow",
        "trade_flow_15m",
        lambda: _build_trade_flow(
            session=session, symbol=symbol,
            bar_start=bar_start_ts, bar_end=bar_end_ts,
            ingest_run_id=ingest_run_id, dataset_version=dataset_version,
            flags=flags, mid_price_ref=mid_price_ref,
        ),
    )

    # Step 3: oi_funding_metrics
    _run_step(
        "oi_funding",
        "oi_funding_metrics_15m",
        lambda: _build_oi_funding_metrics(
            session=session, symbol=symbol,
            bar_start=bar_start_ts, bar_end=bar_end_ts,
            ingest_run_id=ingest_run_id, dataset_version=dataset_version,
            flags=flags, mid_price_ref=mid_price_ref,
        ),
    )

    # Step 4: volume_profile (depends on trade_flow TFI, but 5.4 对依赖
    # 用 LEFT JOIN 读 silver.market_trade_flow_15m, 若上一步失败也不阻塞)
    _run_step(
        "volume_profile",
        "volume_profile_15m",
        lambda: _build_volume_profile(
            session=session, symbol=symbol,
            bar_start=bar_start_ts, bar_end=bar_end_ts,
            ingest_run_id=ingest_run_id, dataset_version=dataset_version,
            flags=flags,
        ),
    )

    # Step 5: liquidation_metrics
    _run_step(
        "liquidation",
        "liquidation_metrics_15m",
        lambda: _build_liquidation_metrics(
            session=session, symbol=symbol,
            bar_start=bar_start_ts, bar_end=bar_end_ts,
            ingest_run_id=ingest_run_id, dataset_version=dataset_version,
            flags=flags,
        ),
    )

    duration = time.monotonic() - start_time
    result = SilverMicrostructureResult(
        symbol=symbol,
        bar_start_ts=bar_start_ts,
        bar_end_ts=bar_end_ts,
        tables_written=written,
        tables_failed=list(tables_failed),
        quality_flags=sorted(set(flags)),
        duration_seconds=duration,
        error=total_error,
    )

    # Aggregate metrics: success/error + per-table rows_written
    if total_error is None:
        _record_metric(
            metrics_registry, "microstructure_silver_etl_runs_success_total",
        )
    else:
        _record_metric(metrics_registry, "microstructure_silver_etl_errors_total")
    for table_name, rowcount in written.items():
        _record_metric(
            metrics_registry,
            f"microstructure_silver_rows_written_{table_name}_total",
            value=rowcount,
        )

    # Duration histogram bucket (recorded into single bucket by threshold).
    # 确保某一档 (exactly one) 每次入口调用都被打点;
    # 阈值对齐 §11 Gate: p95 < 10s. 超过 30s 视为病态, 需要独立告警.
    if duration < 1.0:
        duration_bucket = "1s"
    elif duration < 5.0:
        duration_bucket = "5s"
    elif duration < 10.0:
        duration_bucket = "10s"
    elif duration < 30.0:
        duration_bucket = "30s"
    else:
        duration_bucket = "inf"
    _record_metric(
        metrics_registry,
        f"microstructure_silver_etl_duration_bucket_{duration_bucket}",
    )

    # Final summary log — 按结果分级, 让 Loki 告警和运维 tail 能区分
    # "全成功" / "部分失败" / "彻底失败" 三种状态 (P0-a 修复前都是 INFO 级
    # COMMITTED, 与 exit code 双重说谎)
    log_payload = (
        "silver_microstructure_etl symbol=%s bar=%s written=%s "
        "tables_failed=%s flags=%s duration=%.3fs error=%s"
    )
    log_args = (
        symbol, bar_start_ts.isoformat(), written, tables_failed,
        result.quality_flags, duration, total_error,
    )
    all_zero = all(rc == 0 for rc in written.values()) if written else True
    if total_error is not None and all_zero:
        log.error("FAILED " + log_payload, *log_args)
    elif tables_failed:
        log.warning("PARTIAL " + log_payload, *log_args)
    else:
        log.info("COMMITTED " + log_payload, *log_args)
    return result


# ─────────────────────────────────────────────────────────────────────
# 工具: 给定 now, 计算最近一个已完成的 15m bar (bar_start, bar_end)
# ─────────────────────────────────────────────────────────────────────


def latest_complete_bar(
    now: datetime | None = None,
    *,
    lookback_bars: int = 1,
) -> tuple[datetime, datetime]:
    """返回最近一个完整 (已关闭) 15m bar 的 (bar_start, bar_end)。

    e.g. now = 12:17:00 UTC → bar_start = 11:45, bar_end = 12:00 (不是
    12:00-12:15, 因为 12:15 bar 要到 12:30 才关). 默认 lookback_bars=1
    表示 "前一个已关的 bar", 更保险防竞态。

    Parameters
    ----------
    now : datetime, optional
        默认当前 UTC。必须是 timezone-aware。
    lookback_bars : int
        向前多少个 bar (>=1)。1 = 已关闭的最新 bar; 2 = 再往前一个 bar。
    """
    if lookback_bars < 1:
        raise ValueError(f"lookback_bars must be >= 1, got {lookback_bars}")
    now = now or _utc_now()
    if now.tzinfo is None:
        raise ValueError(f"now must be timezone-aware UTC, got naive {now!r}")
    # 对齐到 now 所在 15m bar 的起点
    current_bar_start_minute = (now.minute // 15) * 15
    current_bar_start = now.replace(
        minute=current_bar_start_minute, second=0, microsecond=0,
    )
    # current bar 未关闭, 向前取 lookback_bars 个
    bar_end = current_bar_start - timedelta(seconds=BAR_SECONDS * (lookback_bars - 1))
    bar_start = bar_end - timedelta(seconds=BAR_SECONDS)
    return bar_start, bar_end
