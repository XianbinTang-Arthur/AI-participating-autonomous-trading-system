"""市场数据对齐层 (Phase 4-A).

将 replay/live candidate 对齐到对应时间的市场快照（Gold bar）。

V1 实现：
  - 使用 Gold OHLCV bars 作为市场快照
  - 通过 bar timestamp 精确匹配 replay decision
  - 输出 execution_alignment.csv 的行数据
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from aats.data_platform.attribution.taxonomy import TF_SECONDS

log = logging.getLogger(__name__)

# OKX BTC-USDT-SWAP: 1 contract = 0.01 BTC
_CONTRACT_MULTIPLIER_BTC = Decimal("0.01")

# 对齐状态
ALIGNMENT_MATCHED = "matched"
ALIGNMENT_NO_BAR_DATA = "no_bar_data"


_VALID_TIMEFRAMES = frozenset({"15m", "1h", "4h", "1d"})


def _gold_table_name(timeframe: str) -> str:
    """根据 timeframe 返回 Gold swap bar 表名。

    通过白名单校验防止 SQL 注入。
    """
    tf_lower = timeframe.lower()
    if tf_lower not in _VALID_TIMEFRAMES:
        raise ValueError(
            f"Invalid timeframe: {timeframe!r}. "
            f"Must be one of {sorted(_VALID_TIMEFRAMES)}"
        )
    return f"gold.market_swap_replay_bars_{tf_lower}"


def query_gold_bars_for_window(
    session: Session,
    *,
    symbol: str,
    timeframe: str,
    start_ts: datetime,
    end_ts: datetime,
    dataset_version: str | None = None,
) -> dict[str, dict[str, Any]]:
    """查询 Gold bars 并按 timestamp ISO 索引返回。

    Returns:
        dict[ts_iso -> bar_dict]，每条包含 OHLCV + funding 数据。
    """
    table = _gold_table_name(timeframe)

    sql = f"""
        SELECT
            symbol, ts,
            open, high, low, close,
            volume, quote_volume,
            is_closed, aligned_funding_rate
        FROM {table}
        WHERE symbol = :symbol
          AND ts >= :start_ts
          AND ts < :end_ts
    """
    params: dict[str, Any] = {
        "symbol": symbol,
        "start_ts": start_ts,
        "end_ts": end_ts,
    }
    if dataset_version:
        sql += " AND source_candle_dataset_version = :dv"
        params["dv"] = dataset_version
    sql += " ORDER BY ts"

    rows = session.execute(text(sql), params).fetchall()
    bar_map: dict[str, dict[str, Any]] = {}
    for row in rows:
        ts_iso = row.ts.isoformat() if hasattr(row.ts, "isoformat") else str(row.ts)
        bar_map[ts_iso] = {
            "symbol": row.symbol,
            "ts": ts_iso,
            "open": float(row.open),
            "high": float(row.high),
            "low": float(row.low),
            "close": float(row.close),
            "volume": float(row.volume) if row.volume is not None else None,
            "quote_volume": float(row.quote_volume) if row.quote_volume is not None else None,
            "is_closed": row.is_closed,
            "aligned_funding_rate": float(row.aligned_funding_rate) if row.aligned_funding_rate is not None else None,
        }

    log.info("Loaded %d Gold bars from %s [%s ~ %s]",
             len(bar_map), table, start_ts, end_ts)
    return bar_map


def build_execution_alignment(
    replay_decisions: list[dict[str, Any]],
    gold_bars: dict[str, dict[str, Any]],
    *,
    family: str,
    symbol: str,
    timeframe: str,
) -> list[dict[str, Any]]:
    """将 replay decisions（openings + closes）对齐到 Gold bars。

    只处理有仓位变动的决策 (action in ("open", "close"))。

    Returns:
        对齐行列表，每条包含候选订单信息 + 市场快照数据。
    """
    bar_seconds = TF_SECONDS.get(timeframe, 900)
    aligned_rows: list[dict[str, Any]] = []

    candidates = [
        d for d in replay_decisions
        if d.get("action") in ("open", "close")
    ]

    log.info("Aligning %d candidates (from %d total decisions) with %d Gold bars",
             len(candidates), len(replay_decisions), len(gold_bars))

    for dec in candidates:
        ts_str = dec["ts"]
        delta_qty = float(dec.get("delta_position_qty", 0))
        if delta_qty == 0:
            continue

        # 确定 side
        candidate_side = "buy" if delta_qty > 0 else "sell"
        candidate_qty = abs(delta_qty)

        # 尝试匹配 Gold bar（同一 timestamp）
        bar = gold_bars.get(ts_str)
        if bar is None:
            # 尝试其他格式匹配
            bar = _try_match_bar(ts_str, gold_bars)

        if bar is not None and bar.get("close") and bar["close"] > 0:
            mid_price = bar["close"]
            bar_range_bps = (bar["high"] - bar["low"]) / mid_price * 10000 if mid_price > 0 else 0
            bar_volume = bar.get("volume")
            alignment_status = ALIGNMENT_MATCHED

            # 预估 notional（USDT）
            candidate_notional_usd = Decimal(str(candidate_qty)) * _CONTRACT_MULTIPLIER_BTC * Decimal(str(mid_price))

            row = {
                "family": family,
                "symbol": symbol,
                "timeframe": timeframe,
                "candidate_ts": ts_str,
                "candidate_source": "replay",
                "candidate_side": candidate_side,
                "candidate_qty": candidate_qty,
                "candidate_notional_usd": float(candidate_notional_usd),
                "candidate_action": dec.get("action", ""),
                "snapshot_ts": bar["ts"],
                "trades_window_start": ts_str,
                "trades_window_end": _offset_ts(ts_str, bar_seconds),
                "alignment_status": alignment_status,
                # 市场快照
                "bar_open": bar["open"],
                "bar_high": bar["high"],
                "bar_low": bar["low"],
                "bar_close": bar["close"],
                "bar_volume": bar_volume,
                "bar_quote_volume": bar.get("quote_volume"),
                "bar_range_bps": round(bar_range_bps, 2),
                "aligned_funding_rate": bar.get("aligned_funding_rate"),
                # 策略 edge 信息（从 replay decision 携带）
                "signal_edge_proxy_bps": dec.get("signal_edge_proxy_bps", 0),
                "funding_adjustment_bps": dec.get("funding_adjustment_bps", 0),
                "cost_bps": dec.get("cost_bps", 0),
                "expected_net_edge_bps": dec.get("expected_net_edge_bps", 0),
                "close_price": dec.get("close_price"),
            }
        else:
            row = {
                "family": family,
                "symbol": symbol,
                "timeframe": timeframe,
                "candidate_ts": ts_str,
                "candidate_source": "replay",
                "candidate_side": candidate_side,
                "candidate_qty": candidate_qty,
                "candidate_notional_usd": 0,
                "candidate_action": dec.get("action", ""),
                "snapshot_ts": None,
                "trades_window_start": None,
                "trades_window_end": None,
                "alignment_status": ALIGNMENT_NO_BAR_DATA,
                "bar_open": None,
                "bar_high": None,
                "bar_low": None,
                "bar_close": None,
                "bar_volume": None,
                "bar_quote_volume": None,
                "bar_range_bps": None,
                "aligned_funding_rate": None,
                "signal_edge_proxy_bps": dec.get("signal_edge_proxy_bps", 0),
                "funding_adjustment_bps": dec.get("funding_adjustment_bps", 0),
                "cost_bps": dec.get("cost_bps", 0),
                "expected_net_edge_bps": dec.get("expected_net_edge_bps", 0),
                "close_price": dec.get("close_price"),
            }

        aligned_rows.append(row)

    n_matched = sum(1 for r in aligned_rows if r["alignment_status"] == ALIGNMENT_MATCHED)
    log.info("Alignment complete: %d total, %d matched, %d no_bar_data",
             len(aligned_rows), n_matched, len(aligned_rows) - n_matched)

    return aligned_rows


def _try_match_bar(
    ts_str: str,
    gold_bars: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """尝试多种 ISO 格式匹配 Gold bar。"""
    # 尝试去掉 +00:00 或添加 +00:00
    variants = []
    if "+00:00" in ts_str:
        variants.append(ts_str.replace("+00:00", ""))
    else:
        variants.append(ts_str + "+00:00")

    for v in variants:
        if v in gold_bars:
            return gold_bars[v]
    return None


def _offset_ts(ts_str: str, seconds: int) -> str:
    """将 ISO timestamp 字符串偏移指定秒数。"""
    try:
        dt = datetime.fromisoformat(ts_str)
        return (dt + timedelta(seconds=seconds)).isoformat()
    except (ValueError, TypeError):
        return ts_str
