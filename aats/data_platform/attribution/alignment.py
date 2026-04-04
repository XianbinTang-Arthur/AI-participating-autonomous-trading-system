"""Replay / Live 对齐模块.

将 replay decisions 与 live intents 按时间窗口近邻匹配。

匹配规则（v1）：
  主键 = family + symbol
  时间窗口 = replay bar [ts, ts + bar_duration + tolerance)
  结果 = aligned / replay_only / live_only
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from aats.data_platform.attribution.taxonomy import (
    ALIGNMENT_STATUS_ALIGNED,
    ALIGNMENT_STATUS_LIVE_ONLY,
    ALIGNMENT_STATUS_REPLAY_ONLY,
    TF_SECONDS,
)

log = logging.getLogger(__name__)

# 对齐容差：bar 关闭后额外容纳 live intent 的秒数
_DEFAULT_TOLERANCE_SECONDS = 120


# =========================================================================
# Live 数据查询
# =========================================================================


def query_live_intents(
    live_session: Any,
    *,
    family: str,
    symbol: str,
    start_ts: datetime,
    end_ts: datetime,
) -> list[dict[str, Any]]:
    """查询 live strategy_sleeve_intents。"""
    from sqlalchemy import text

    rows = live_session.execute(
        text("""
            SELECT sleeve_intent_id, decision_id, family, symbol,
                   product_type, margin_mode, route_action,
                   automatic_enabled, budget_multiplier, state,
                   allocation_id, strategy_sleeve_id, created_at
            FROM strategy_sleeve_intents
            WHERE family = :family
              AND symbol = :symbol
              AND created_at >= :start
              AND created_at < :end
            ORDER BY created_at
        """),
        {"family": family, "symbol": symbol, "start": start_ts, "end": end_ts},
    )
    return [dict(r._mapping) for r in rows]


def query_live_allocations(
    live_session: Any,
    *,
    allocation_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """按 allocation_id 批量查询 portfolio_allocation_decisions。"""
    if not allocation_ids:
        return {}
    from sqlalchemy import text

    rows = live_session.execute(
        text("""
            SELECT allocation_id, decision_id, symbol, automatic_enabled,
                   route_action, primary_family,
                   portfolio_requested_notional, portfolio_approved_notional,
                   portfolio_budget_cut_notional, created_at
            FROM portfolio_allocation_decisions
            WHERE allocation_id = ANY(:ids)
        """),
        {"ids": allocation_ids},
    )
    return {str(r._mapping["allocation_id"]): dict(r._mapping) for r in rows}


def query_live_budget_snapshots(
    live_session: Any,
    *,
    allocation_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """按 allocation_id 查询 allocator_budget_snapshots（取最新一条）。"""
    if not allocation_ids:
        return {}
    from sqlalchemy import text

    rows = live_session.execute(
        text("""
            SELECT DISTINCT ON (allocation_id)
                   budget_snapshot_id, allocation_id, family, symbol,
                   requested_notional, approved_notional,
                   budget_multiplier, clamped,
                   portfolio_approved_notional, portfolio_budget_cut_notional,
                   created_at
            FROM allocator_budget_snapshots
            WHERE allocation_id = ANY(:ids)
            ORDER BY allocation_id, created_at DESC
        """),
        {"ids": allocation_ids},
    )
    return {str(r._mapping["allocation_id"]): dict(r._mapping) for r in rows}


def query_live_bundles(
    live_session: Any,
    *,
    decision_ids: list[str],
) -> dict[str, list[dict[str, Any]]]:
    """按 decision_id 查询 strategy_execution_bundles。"""
    if not decision_ids:
        return {}
    from sqlalchemy import text

    rows = live_session.execute(
        text("""
            SELECT bundle_id, decision_id, family, status,
                   selected_symbol, route_action,
                   gross_requested_exposure, net_approved_exposure,
                   portfolio_risk_budget_state, created_at
            FROM strategy_execution_bundles
            WHERE decision_id = ANY(:ids)
            ORDER BY created_at
        """),
        {"ids": decision_ids},
    )
    result: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        d = dict(r._mapping)
        did = str(d["decision_id"])
        result.setdefault(did, []).append(d)
    return result


def query_live_orders(
    live_session: Any,
    *,
    decision_ids: list[str],
) -> dict[str, list[dict[str, Any]]]:
    """按 decision_id 查询 execution_orders。"""
    if not decision_ids:
        return {}
    from sqlalchemy import text

    rows = live_session.execute(
        text("""
            SELECT order_id, decision_id, symbol, side, order_type,
                   requested_qty, state, strategy_family,
                   strategy_bundle_id, reduce_only, reduce_only_reason,
                   position_intent, created_at
            FROM execution_orders
            WHERE decision_id = ANY(:ids)
            ORDER BY created_at
        """),
        {"ids": decision_ids},
    )
    result: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        d = dict(r._mapping)
        did = str(d["decision_id"])
        result.setdefault(did, []).append(d)
    return result


def query_live_fills(
    live_session: Any,
    *,
    order_ids: list[str],
) -> dict[str, list[dict[str, Any]]]:
    """按 order_id 查询 execution_fills。"""
    if not order_ids:
        return {}
    from sqlalchemy import text

    rows = live_session.execute(
        text("""
            SELECT fill_id, order_id, symbol, side,
                   fill_qty, fill_price, fee_amount,
                   strategy_family, exchange_ts, created_at
            FROM execution_fills
            WHERE order_id = ANY(:ids)
            ORDER BY created_at
        """),
        {"ids": order_ids},
    )
    result: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        d = dict(r._mapping)
        oid = str(d["order_id"])
        result.setdefault(oid, []).append(d)
    return result


def query_reconciliation_snapshots(
    live_session: Any,
    *,
    symbol: str,
    start_ts: datetime,
    end_ts: datetime,
) -> list[dict[str, Any]]:
    """查询时间窗口内的 reconciliation_state_snapshots。"""
    from sqlalchemy import text

    rows = live_session.execute(
        text("""
            SELECT snapshot_id, recovery_state, resume_eligible,
                   safe_to_trade, review_required,
                   only_reduce_required, halt_required,
                   bundle_recovery_required, created_at
            FROM reconciliation_state_snapshots
            WHERE (primary_symbol = :sym OR primary_symbol IS NULL)
              AND created_at >= :start
              AND created_at < :end
            ORDER BY created_at DESC
            LIMIT 50
        """),
        {"sym": symbol, "start": start_ts, "end": end_ts},
    )
    return [dict(r._mapping) for r in rows]


# =========================================================================
# 对齐逻辑
# =========================================================================


def _parse_ts(ts: Any) -> datetime:
    """将各种 ts 格式统一为 timezone-aware datetime。"""
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts
    if isinstance(ts, str):
        # 尝试 ISO 格式
        ts_str = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    raise ValueError(f"Cannot parse timestamp: {ts!r}")


def align_replay_with_live(
    replay_decisions: list[dict[str, Any]],
    live_intents: list[dict[str, Any]],
    *,
    timeframe: str,
    tolerance_seconds: int = _DEFAULT_TOLERANCE_SECONDS,
) -> list[dict[str, Any]]:
    """将 replay decisions 与 live intents 按 bar 时间窗口对齐.

    对每个 replay bar：
      - 在 [ts, ts + bar_duration + tolerance) 内查找 live intents
      - 找到 → aligned；未找到 → replay_only

    对未匹配的 live intents → live_only

    返回 alignment rows 列表。
    """
    bar_seconds = TF_SECONDS.get(timeframe, 900)
    window = timedelta(seconds=bar_seconds + tolerance_seconds)

    # 索引 live intents
    matched_intent_ids: set[str] = set()
    alignment_rows: list[dict[str, Any]] = []

    for rd in replay_decisions:
        replay_ts = _parse_ts(rd["ts"])

        # 只关注有信号的 bar（selectable=True 或 action=open）
        selectable = rd.get("selectable", False)
        action = rd.get("action", "hold")
        is_replay_opening = (action == "open")
        is_replay_selectable = selectable

        # 查找时间窗口内的 live intents
        matched_intents = []
        for li in live_intents:
            li_ts = _parse_ts(li["created_at"])
            if replay_ts <= li_ts < replay_ts + window:
                matched_intents.append(li)

        if matched_intents:
            # 取最近的一个作为主匹配
            best = min(
                matched_intents,
                key=lambda li: abs((_parse_ts(li["created_at"]) - replay_ts).total_seconds()),
            )
            matched_intent_ids.add(str(best["sleeve_intent_id"]))

            alignment_rows.append({
                "replay_ts": replay_ts.isoformat(),
                "live_ts": _parse_ts(best["created_at"]).isoformat(),
                "alignment_status": ALIGNMENT_STATUS_ALIGNED,
                "replay_action": action,
                "replay_selectable": is_replay_selectable,
                "replay_opening": is_replay_opening,
                "replay_state": rd.get("state", ""),
                "replay_execution_compatible": rd.get("execution_compatible", False),
                "replay_blocking_reasons": rd.get("blocking_reasons", ""),
                "replay_expected_net_edge_bps": rd.get("expected_net_edge_bps"),
                "live_opening": best.get("route_action") in ("buy", "sell"),
                "live_state": best.get("state", ""),
                "live_route_action": best.get("route_action", ""),
                "live_automatic_enabled": best.get("automatic_enabled"),
                "live_intent_id": str(best["sleeve_intent_id"]),
                "live_decision_id": str(best.get("decision_id", "")),
                "live_allocation_id": str(best.get("allocation_id") or ""),
                "bar_index": rd.get("bar_index"),
            })
        else:
            alignment_rows.append({
                "replay_ts": replay_ts.isoformat(),
                "live_ts": None,
                "alignment_status": ALIGNMENT_STATUS_REPLAY_ONLY,
                "replay_action": action,
                "replay_selectable": is_replay_selectable,
                "replay_opening": is_replay_opening,
                "replay_state": rd.get("state", ""),
                "replay_execution_compatible": rd.get("execution_compatible", False),
                "replay_blocking_reasons": rd.get("blocking_reasons", ""),
                "replay_expected_net_edge_bps": rd.get("expected_net_edge_bps"),
                "live_opening": False,
                "live_state": None,
                "live_route_action": None,
                "live_automatic_enabled": None,
                "live_intent_id": None,
                "live_decision_id": None,
                "live_allocation_id": None,
                "bar_index": rd.get("bar_index"),
            })

    # 补充 live_only（未被匹配的 live intents）
    for li in live_intents:
        iid = str(li["sleeve_intent_id"])
        if iid not in matched_intent_ids:
            alignment_rows.append({
                "replay_ts": None,
                "live_ts": _parse_ts(li["created_at"]).isoformat(),
                "alignment_status": ALIGNMENT_STATUS_LIVE_ONLY,
                "replay_action": None,
                "replay_selectable": None,
                "replay_opening": None,
                "replay_state": None,
                "replay_execution_compatible": None,
                "replay_blocking_reasons": None,
                "replay_expected_net_edge_bps": None,
                "live_opening": li.get("route_action") in ("buy", "sell"),
                "live_state": li.get("state", ""),
                "live_route_action": li.get("route_action", ""),
                "live_automatic_enabled": li.get("automatic_enabled"),
                "live_intent_id": iid,
                "live_decision_id": str(li.get("decision_id", "")),
                "live_allocation_id": str(li.get("allocation_id") or ""),
                "bar_index": None,
            })

    log.info(
        "Alignment: %d total rows (%d aligned, %d replay_only, %d live_only)",
        len(alignment_rows),
        sum(1 for r in alignment_rows if r["alignment_status"] == ALIGNMENT_STATUS_ALIGNED),
        sum(1 for r in alignment_rows if r["alignment_status"] == ALIGNMENT_STATUS_REPLAY_ONLY),
        sum(1 for r in alignment_rows if r["alignment_status"] == ALIGNMENT_STATUS_LIVE_ONLY),
    )
    return alignment_rows
