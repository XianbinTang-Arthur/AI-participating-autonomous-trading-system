"""Replay / Live 对齐模块.

将 replay decisions 与 live intents 按显式信号 lineage 精确匹配。

匹配规则（v2）：
  主键 = family + symbol + timeframe + signal_bar_start
  缺失 lineage 的旧 live intent = unattributable，禁止按 created_at 猜测
  结果 = aligned / replay_only / live_only / unattributable
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from aats.data_platform.attribution.taxonomy import (
    ALIGNMENT_STATUS_ALIGNED,
    ALIGNMENT_STATUS_LIVE_ONLY,
    ALIGNMENT_STATUS_REPLAY_ONLY,
    ALIGNMENT_STATUS_UNATTRIBUTABLE,
    TF_SECONDS,
)
from aats.data_platform.governance._time_util import parse_iso_datetime_utc

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
    timeframe: str,
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
                   allocation_id, strategy_sleeve_id, timeframe,
                   signal_bar_start, signal_bar_end, market_data_asof,
                   parameter_set_id, runtime_generation, code_version,
                   market_snapshot_ref, feature_snapshot_ref, created_at
            FROM strategy_sleeve_intents
            WHERE family = :family
              AND symbol = :symbol
              AND (timeframe = :timeframe OR timeframe IS NULL)
              AND (
                    (signal_bar_start >= :start AND signal_bar_start < :end)
                    OR (
                        signal_bar_start IS NULL
                        AND created_at >= :start
                        AND created_at < :end
                    )
              )
            ORDER BY created_at
        """),
        {
            "family": family,
            "symbol": symbol,
            "timeframe": timeframe.lower(),
            "start": start_ts,
            "end": end_ts,
        },
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
    event_times: list[datetime],
) -> list[dict[str, Any]]:
    """为每个 live intent 查询其发生时刻可见的最近 reconciliation 状态。"""
    if not event_times:
        return []
    from sqlalchemy import text

    unique_event_times = sorted(set(event_times))
    values_sql = ", ".join(
        f"(:event_ts_{index})" for index in range(len(unique_event_times))
    )
    params: dict[str, Any] = {
        f"event_ts_{index}": event_ts
        for index, event_ts in enumerate(unique_event_times)
    }
    params["sym"] = symbol
    rows = live_session.execute(
        text(
            f"""
            WITH attribution_events(event_ts) AS (
                VALUES {values_sql}
            )
            SELECT events.event_ts AS attribution_event_ts,
                   snapshot.snapshot_id, snapshot.recovery_state,
                   snapshot.resume_eligible, snapshot.safe_to_trade,
                   snapshot.review_required, snapshot.only_reduce_required,
                   snapshot.halt_required, snapshot.bundle_recovery_required,
                   snapshot.created_at
            FROM attribution_events AS events
            LEFT JOIN LATERAL (
                SELECT snapshot_id, recovery_state, resume_eligible,
                       safe_to_trade, review_required,
                       only_reduce_required, halt_required,
                       bundle_recovery_required, created_at
                FROM reconciliation_state_snapshots
                WHERE (primary_symbol = :sym OR primary_symbol IS NULL)
                  AND created_at <= events.event_ts
                ORDER BY created_at DESC
                LIMIT 1
            ) AS snapshot ON TRUE
            ORDER BY events.event_ts
            """
        ),
        params,
    )
    return [
        dict(row._mapping)
        for row in rows
        if row._mapping["snapshot_id"] is not None
    ]


# =========================================================================
# 对齐逻辑
# =========================================================================


def _parse_ts(ts: Any) -> datetime:
    """将各种 ts 格式统一为 timezone-aware datetime。"""
    if not isinstance(ts, (str, datetime)):
        raise ValueError(f"Cannot parse timestamp: {ts!r}")
    parsed = parse_iso_datetime_utc(ts, context="attribution.alignment._parse_ts")
    if parsed is None:
        raise ValueError(f"Cannot parse timestamp: {ts!r}")
    return parsed


def _live_route_requests_target(intent: dict[str, Any]) -> bool:
    """StrategyRouteAction 的唯一正常目标执行路径。"""
    return intent.get("route_action") == "override_target"


def align_replay_with_live(
    replay_decisions: list[dict[str, Any]],
    live_intents: list[dict[str, Any]],
    *,
    timeframe: str,
    tolerance_seconds: int = _DEFAULT_TOLERANCE_SECONDS,
) -> list[dict[str, Any]]:
    """按显式 timeframe + signal_bar_start 精确对齐 replay 与 live。

    ``tolerance_seconds`` 仅保留调用兼容；v2 不再使用 created_at 宽窗。旧记录
    或缺少完整 lineage 的记录会输出 ``unattributable``，不会被猜测性匹配。
    """
    del tolerance_seconds
    normalized_timeframe = timeframe.lower()
    bar_seconds = TF_SECONDS.get(normalized_timeframe, 900)
    required_lineage_fields = (
        "timeframe",
        "signal_bar_start",
        "signal_bar_end",
        "market_data_asof",
        "parameter_set_id",
        "runtime_generation",
        "code_version",
        "market_snapshot_ref",
        "feature_snapshot_ref",
    )

    complete_by_bar: dict[datetime, list[dict[str, Any]]] = {}
    unattributable: list[tuple[dict[str, Any], str]] = []
    for live_intent in live_intents:
        missing = [
            field
            for field in required_lineage_fields
            if live_intent.get(field) in (None, "")
        ]
        if missing:
            unattributable.append(
                (live_intent, "live_lineage_missing:" + ",".join(missing))
            )
            continue
        if str(live_intent["timeframe"]).lower() != normalized_timeframe:
            continue
        try:
            bar_start = _parse_ts(live_intent["signal_bar_start"])
            bar_end = _parse_ts(live_intent["signal_bar_end"])
            market_data_asof = _parse_ts(live_intent["market_data_asof"])
        except ValueError:
            unattributable.append((live_intent, "live_lineage_invalid_timestamp"))
            continue
        if bar_end != bar_start + timedelta(seconds=bar_seconds):
            unattributable.append((live_intent, "live_lineage_invalid_bar_window"))
            continue
        if market_data_asof < bar_start:
            unattributable.append((live_intent, "live_lineage_market_data_before_bar"))
            continue
        complete_by_bar.setdefault(bar_start, []).append(live_intent)

    matched_intent_ids: set[str] = set()
    unattributable_intent_ids = {
        str(intent["sleeve_intent_id"])
        for intent, _reason in unattributable
    }
    alignment_rows: list[dict[str, Any]] = []

    for rd in replay_decisions:
        replay_ts = _parse_ts(rd["ts"])
        selectable = rd.get("selectable", False)
        action = rd.get("action", "hold")
        is_replay_opening = (action == "open")
        is_replay_selectable = selectable
        matched_intents = complete_by_bar.get(replay_ts, [])

        if matched_intents:
            for matched in matched_intents:
                matched_intent_ids.add(str(matched["sleeve_intent_id"]))
                alignment_rows.append({
                    "replay_ts": replay_ts.isoformat(),
                    "live_ts": _parse_ts(matched["created_at"]).isoformat(),
                    "alignment_status": ALIGNMENT_STATUS_ALIGNED,
                    "lineage_error": None,
                    "replay_action": action,
                    "replay_selectable": is_replay_selectable,
                    "replay_opening": is_replay_opening,
                    "replay_state": rd.get("state", ""),
                    "replay_execution_compatible": rd.get("execution_compatible", False),
                    "replay_blocking_reasons": rd.get("blocking_reasons", ""),
                    "replay_expected_net_edge_bps": rd.get("expected_net_edge_bps"),
                    "live_opening": _live_route_requests_target(matched),
                    "live_state": matched.get("state", ""),
                    "live_route_action": matched.get("route_action", ""),
                    "live_automatic_enabled": matched.get("automatic_enabled"),
                    "live_intent_id": str(matched["sleeve_intent_id"]),
                    "live_decision_id": str(matched.get("decision_id", "")),
                    "live_allocation_id": str(matched.get("allocation_id") or ""),
                    "live_parameter_set_id": matched.get("parameter_set_id"),
                    "live_runtime_generation": matched.get("runtime_generation"),
                    "live_code_version": matched.get("code_version"),
                    "live_market_snapshot_ref": matched.get("market_snapshot_ref"),
                    "live_feature_snapshot_ref": matched.get("feature_snapshot_ref"),
                    "bar_index": rd.get("bar_index"),
                })
        else:
            alignment_rows.append({
                "replay_ts": replay_ts.isoformat(),
                "live_ts": None,
                "alignment_status": ALIGNMENT_STATUS_REPLAY_ONLY,
                "lineage_error": None,
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
                "live_parameter_set_id": None,
                "live_runtime_generation": None,
                "live_code_version": None,
                "live_market_snapshot_ref": None,
                "live_feature_snapshot_ref": None,
                "bar_index": rd.get("bar_index"),
            })

    # 补充 live_only（未被匹配的 live intents）
    for li in live_intents:
        iid = str(li["sleeve_intent_id"])
        if iid in unattributable_intent_ids:
            continue
        if iid not in matched_intent_ids:
            alignment_rows.append({
                "replay_ts": None,
                "live_ts": _parse_ts(li["created_at"]).isoformat(),
                "alignment_status": ALIGNMENT_STATUS_LIVE_ONLY,
                "lineage_error": None,
                "replay_action": None,
                "replay_selectable": None,
                "replay_opening": None,
                "replay_state": None,
                "replay_execution_compatible": None,
                "replay_blocking_reasons": None,
                "replay_expected_net_edge_bps": None,
                "live_opening": _live_route_requests_target(li),
                "live_state": li.get("state", ""),
                "live_route_action": li.get("route_action", ""),
                "live_automatic_enabled": li.get("automatic_enabled"),
                "live_intent_id": iid,
                "live_decision_id": str(li.get("decision_id", "")),
                "live_allocation_id": str(li.get("allocation_id") or ""),
                "live_parameter_set_id": li.get("parameter_set_id"),
                "live_runtime_generation": li.get("runtime_generation"),
                "live_code_version": li.get("code_version"),
                "live_market_snapshot_ref": li.get("market_snapshot_ref"),
                "live_feature_snapshot_ref": li.get("feature_snapshot_ref"),
                "bar_index": None,
            })

    for li, lineage_error in unattributable:
        alignment_rows.append({
            "replay_ts": None,
            "live_ts": _parse_ts(li["created_at"]).isoformat(),
            "alignment_status": ALIGNMENT_STATUS_UNATTRIBUTABLE,
            "lineage_error": lineage_error,
            "replay_action": None,
            "replay_selectable": None,
            "replay_opening": None,
            "replay_state": None,
            "replay_execution_compatible": None,
            "replay_blocking_reasons": None,
            "replay_expected_net_edge_bps": None,
            "live_opening": _live_route_requests_target(li),
            "live_state": li.get("state", ""),
            "live_route_action": li.get("route_action", ""),
            "live_automatic_enabled": li.get("automatic_enabled"),
            "live_intent_id": str(li["sleeve_intent_id"]),
            "live_decision_id": str(li.get("decision_id", "")),
            "live_allocation_id": str(li.get("allocation_id") or ""),
            "live_parameter_set_id": li.get("parameter_set_id"),
            "live_runtime_generation": li.get("runtime_generation"),
            "live_code_version": li.get("code_version"),
            "live_market_snapshot_ref": li.get("market_snapshot_ref"),
            "live_feature_snapshot_ref": li.get("feature_snapshot_ref"),
            "bar_index": None,
        })

    log.info(
        "Alignment: %d total rows (%d aligned, %d replay_only, %d live_only, %d unattributable)",
        len(alignment_rows),
        sum(1 for r in alignment_rows if r["alignment_status"] == ALIGNMENT_STATUS_ALIGNED),
        sum(1 for r in alignment_rows if r["alignment_status"] == ALIGNMENT_STATUS_REPLAY_ONLY),
        sum(1 for r in alignment_rows if r["alignment_status"] == ALIGNMENT_STATUS_LIVE_ONLY),
        sum(1 for r in alignment_rows if r["alignment_status"] == ALIGNMENT_STATUS_UNATTRIBUTABLE),
    )
    return alignment_rows
