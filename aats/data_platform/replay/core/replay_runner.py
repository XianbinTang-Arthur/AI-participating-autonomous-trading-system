"""Replay runner: bar-by-bar replay engine.

Phase 2 核心模块。从 Gold replay bars 按时间顺序逐 bar 重放，
调用 family adapter 做策略评估，输出 replay decisions。

职责边界（§8.4）：
- 读取 Gold replay bars  -> YES
- 调用 adapter           -> YES
- 输出决策 artifact      -> YES
- 完整撮合仿真          -> NO (Phase 4)
- PnL accounting         -> NO (Phase 4)
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from aats.data_platform.replay.adapters.base_adapter import BaseReplayAdapter
from aats.data_platform.replay.core.replay_context import (
    ReplayBar,
    ReplayBarContext,
    ReplayDecision,
    ReplayParameterOverrides,
)
from aats.domain.instrument_scope import (
    INSTRUMENT_SCOPE_UNSUPPORTED_REASON,
    classify_instrument_scope,
)

log = logging.getLogger(__name__)

# 表名映射
_GOLD_TABLE_MAP: dict[tuple[str, str], str] = {}
for _inst in ("spot", "swap"):
    for _tf in ("1m", "5m", "15m", "1h"):
        _GOLD_TABLE_MAP[(_inst, _tf)] = f"gold.market_{_inst}_replay_bars_{_tf}"


def _resolve_gold_table(symbol: str, timeframe: str) -> str:
    """根据 symbol 和 timeframe 确定 Gold 表名。"""
    inst = classify_instrument_scope(symbol)
    if inst == "unsupported":
        raise ValueError(INSTRUMENT_SCOPE_UNSUPPORTED_REASON)
    tf = timeframe.lower()
    key = (inst, tf)
    table = _GOLD_TABLE_MAP.get(key)
    if table is None:
        raise ValueError(f"No Gold table for instrument={inst}, timeframe={timeframe}")
    return table


def load_gold_bars(
    session: Session,
    *,
    symbol: str,
    timeframe: str,
    start_ts: datetime,
    end_ts: datetime,
    dataset_version: str | None = None,
) -> list[ReplayBar]:
    """从 Gold 表加载 replay bars，按 ts 升序排列。"""
    instrument_scope = classify_instrument_scope(symbol)
    if instrument_scope == "unsupported":
        raise ValueError(INSTRUMENT_SCOPE_UNSUPPORTED_REASON)
    if instrument_scope == "swap":
        raise ValueError("legacy_derivative_replay_contract_lineage_required")
    table = _resolve_gold_table(symbol, timeframe)

    where_clauses = ["symbol = :symbol", "ts >= :start_ts", "ts < :end_ts"]
    params: dict[str, Any] = {
        "symbol": symbol,
        "start_ts": start_ts,
        "end_ts": end_ts,
    }
    if dataset_version:
        where_clauses.append("source_candle_dataset_version = :dv")
        params["dv"] = dataset_version

    sql = f"""
        SELECT symbol, ts, open, high, low, close,
               volume, quote_volume, is_closed,
               aligned_funding_rate, funding_source_ts
        FROM {table}
        WHERE {' AND '.join(where_clauses)}
        ORDER BY ts ASC
    """
    rows = session.execute(text(sql), params).fetchall()

    bars: list[ReplayBar] = []
    for r in rows:
        bars.append(ReplayBar(
            symbol=r[0],
            ts=r[1],
            open=Decimal(str(r[2])),
            high=Decimal(str(r[3])),
            low=Decimal(str(r[4])),
            close=Decimal(str(r[5])),
            volume=Decimal(str(r[6])) if r[6] is not None else None,
            quote_volume=Decimal(str(r[7])) if r[7] is not None else None,
            is_closed=r[8],
            aligned_funding_rate=Decimal(str(r[9])) if r[9] is not None else None,
            funding_source_ts=r[10],
        ))
    return bars


def run_replay(
    session: Session,
    *,
    adapter: BaseReplayAdapter,
    symbol: str,
    timeframe: str,
    dataset_version: str,
    start_ts: datetime,
    end_ts: datetime,
    params: ReplayParameterOverrides | None = None,
) -> list[ReplayDecision]:
    """执行一次完整 replay，返回逐 bar 决策列表。

    流程：
    1. 加载 Gold bars
    2. 重置 adapter 状态
    3. 逐 bar 调用 adapter.evaluate_bar()
    4. 返回所有决策
    """
    if params is None:
        params = ReplayParameterOverrides()

    log.info(
        "Starting replay: family=%s symbol=%s tf=%s window=[%s, %s) params=%s",
        adapter.family_name, symbol, timeframe,
        start_ts.isoformat(), end_ts.isoformat(),
        params.to_dict(),
    )

    # 1. 加载 Gold bars
    bars = load_gold_bars(
        session,
        symbol=symbol,
        timeframe=timeframe,
        start_ts=start_ts,
        end_ts=end_ts,
        dataset_version=dataset_version,
    )
    log.info("Loaded %d Gold bars for replay.", len(bars))

    if not bars:
        log.warning("No Gold bars found for replay window. Returning empty decisions.")
        return []

    # 2. 重置 adapter 状态
    state = adapter.reset_state()

    # 3. 逐 bar 评估
    decisions: list[ReplayDecision] = []
    for i, bar in enumerate(bars):
        state.bar_index = i
        ctx = ReplayBarContext(
            bar=bar,
            bar_index=i,
            state=state,
            params=params,
            family=adapter.family_name,
            symbol=symbol,
            timeframe=timeframe,
            dataset_version=dataset_version,
        )
        decision = adapter.evaluate_bar(ctx)
        decisions.append(decision)
        state.score_history.append(max(decision.long_score, decision.short_score))

    log.info(
        "Replay completed: %d bars, %d decisions, final_state=%s",
        len(bars), len(decisions),
        decisions[-1].state if decisions else "N/A",
    )
    return decisions
