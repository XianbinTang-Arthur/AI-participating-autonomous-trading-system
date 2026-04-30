#!/usr/bin/env python3
"""Read-only runtime truth report for the AATS live stack.

This script intentionally avoids importing application settings and never prints
connection strings or secrets. Database facts are read inside the running
gateway container using its existing environment, then only aggregate counts and
non-sensitive identifiers are returned.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import ssl
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_API_BASE = "https://127.0.0.1:8011"
DEFAULT_WSL_DISTRO = "Ubuntu"
DEFAULT_WSL_PROJECT = "~/aats"
DEFAULT_GATEWAY_CONTAINER = "aats-gateway"
DEFAULT_MICROSTRUCTURE_CONTAINER = "aats-microstructure-collector"
DATABASE_TRUTH_PROBE_TIMEOUT_SECONDS = 75
MICROSTRUCTURE_HEARTBEAT_PATH = "/tmp/aats_microstructure_heartbeat"
REQUIRED_APP_CONTAINERS = (
    "aats-gateway",
    "aats-market",
    "aats-decision",
    "aats-execution",
    "aats-rdp-daemon",
)
STATIC_MARKERS = {
    "/ui/modules/views/strategy-view.js": (
        "strategyPreOrderFeasibility",
        "preOrderFeasibilitySummary",
        "terminal_no_fill_explanation",
        "无成交终局",
        "这次为什么没有成交",
    ),
    "/ui/modules/views/overview-view.js": (
        "terminal_no_fill_explanation",
        "无成交终局",
        "终端无成交解释",
        "claimedSubmitGate",
        "恢复仍被 CLAIMED 提交阻断",
        "已接受新基线不等于清除 CLAIMED 提交",
    ),
    "/ui/modules/no-trade-display.js": (
        "hasPreOrderFeasibility",
        "preOrderFeasibilitySummary",
        "执行可行性",
        "阻断维度",
    ),
}
ARTIFACT_STALE_AFTER_SECONDS = 1800
ORDERBOOK_BRONZE_STALE_AFTER_SECONDS = 180
ORDERBOOK_PAYLOAD_SEQUENCE_WINDOW_MINUTES = 30
ORDERBOOK_SILVER_STALE_AFTER_SECONDS = 3600
MICROSTRUCTURE_HEARTBEAT_STALE_AFTER_SECONDS = 60
MICROSTRUCTURE_BRONZE_GROWTH_WINDOW_MINUTES = 5
MICROSTRUCTURE_WORKFLOW_STALE_AFTER_SECONDS = 1800
MICROSTRUCTURE_BAR_MATCH_MAX_AGE_SECONDS = ORDERBOOK_SILVER_STALE_AFTER_SECONDS
ARTIFACT_COMPARE_FACTS = (
    "latest_decision_id",
    "latest_decision_route_action",
    "portfolio_allocation_decisions",
    "execution_fills",
    "shadow_benchmark",
    "ai_timeout_active_blocker",
)
SOFT_CONTRIBUTING_REASON_CODES = {
    "approved_for_non_protective_execution",
    "allocator_budget_assignment_active",
    "no_budget_contraction",
    "reconciliation_contraction_active",
}
TARGET_CONVERGENCE_GUARD_FLAG = "target_convergence_open_orders_block_exposure_increase"
IMPULSE_CHASE_GUARD_FLAGS = (
    "long_impulse_entry_post_spike_pullback_unconfirmed",
    "short_impulse_entry_post_spike_pullback_unconfirmed",
    "long_impulse_entry_extreme_chase_unconfirmed",
    "short_impulse_entry_extreme_chase_unconfirmed",
)
IMPULSE_CHASE_GUARD_CODE_MARKERS = (
    "_impulse_entry_chase_guard_reason",
    "_recent_trade_impulse_chase_reason",
    "_kline_impulse_chase_reason",
    "impulse_entry_post_spike_pullback_unconfirmed",
    "impulse_entry_extreme_chase_unconfirmed",
)
OKX_HEDGE_SCALE_IN_MISMATCH_REASON = "okx_leg_action_mismatch_with_position_intent"
CREATED_NO_COMMAND_DIRECTIONAL_ROOT_CAUSE = "execution_command_missing_for_created_order"
CLAIMED_SUBMIT_STUCK_ROOT_CAUSE = "execution_submit_command_claimed_without_terminal_order_ack"
CLAIMED_SUBMIT_RECOVERY_CONFIRMATION_PREFIX = "resolve_claimed_submit_as_failed:"
CLAIMED_SUBMIT_OPERATOR_HANDOFF_PATTERN = "claimed_submit_operator_handoff_*.json"
TERMINAL_NO_FILL_STATES = {"FAILED", "REJECTED", "CANCELED", "BLOCKED", "EXPIRED", "DRY_RUN"}
OKX_HEDGE_SCALE_IN_CODE_MARKERS = {
    "aats/schemas/execution.py": (
        "def position_intent_matches_leg_intent",
        'compatible.update({"scale_in_long", "reverse_to_long"})',
        'compatible.update({"scale_in_short", "reverse_to_short"})',
    ),
    "aats/services/execution_engine/okx_adapter.py": (
        "position_intent_matches_leg_intent(",
        "okx_leg_action_mismatch_with_position_intent",
    ),
}

SECRET_PATTERNS = (
    re.compile(r"(?i)(postgres(?:ql)?(?:\+[a-z0-9_]+)?://)[^\s'\"<>]+"),
    re.compile(r"(?i)(redis://)[^\s'\"<>]+"),
    re.compile(r"(?i)(mysql(?:\+[a-z0-9_]+)?://)[^\s'\"<>]+"),
    re.compile(
        r"(?i)\b(api[_-]?key|token|password|passwd|pwd|secret|passphrase|access[_-]?key)"
        r"\s*[:=]\s*[^,\s}\]\"']+"
    ),
    re.compile(r"(?i)://[^:/\s]+:[^@\s]+@"),
)

DB_PROBE = r"""
import json
import os
from sqlalchemy import create_engine, text

def bool_from_env(name):
    raw = os.environ.get(name)
    if raw is None:
        return None
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}

url = os.environ.get("AATS_DATABASE_URL") or os.environ.get("DATABASE_URL")
if not url:
    print(json.dumps({"ok": False, "reason": "database_url_not_available_in_container_env"}, sort_keys=True))
    raise SystemExit(0)

execution_command_flow_enabled = bool_from_env("AATS_EXECUTION_COMMAND_FLOW_ENABLED")
symbol = os.environ.get("AATS_EXECUTION_SCIENCE_SYMBOL", "BTC-USDT-SWAP")
runtime_config = {
    "execution_command_flow_enabled": bool(execution_command_flow_enabled)
    if execution_command_flow_enabled is not None
    else False,
    "execution_command_flow_flag_present": execution_command_flow_enabled is not None,
    "operator_control_plane_execution_ledger_enabled": bool_from_env(
        "AATS_OPERATOR_CONTROL_PLANE_EXECUTION_LEDGER_ENABLED"
    ),
    "financial_convergence_mode_enabled": bool_from_env("AATS_FINANCIAL_CONVERGENCE_MODE_ENABLED"),
    "recovery_reconciliation_execution_ledger_enabled": bool_from_env(
        "AATS_RECOVERY_RECONCILIATION_EXECUTION_LEDGER_ENABLED"
    ),
}

engine = create_engine(url)
with engine.connect() as conn:
    decisions = conn.execute(text("select count(*) from portfolio_allocation_decisions")).scalar()
    fills = conn.execute(text("select count(*) from execution_fills")).scalar()
    decision_select = (
        "select allocation_id, decision_id, symbol, created_at, route_action, primary_family, "
        "portfolio_requested_notional, portfolio_approved_notional, portfolio_budget_cut_notional, "
        "expected_edge_bps, expected_cost_bps, payload "
        "from portfolio_allocation_decisions "
    )
    latest = conn.execute(text(
        decision_select + "order by created_at desc limit 1"
    )).mappings().first()

    latest_executable_directional = conn.execute(text(
        decision_select
        + "where primary_family = 'directional' "
        + "and route_action not in ('advisory_only', 'hold_current') "
        + "order by created_at desc limit 1"
    )).mappings().first()

    def decision_audit_and_counts(decision):
        if not decision:
            return None, {}
        decision_id = decision["decision_id"]
        audit = conn.execute(text(
            "select decision_id, updated_at, execution_plan_ref, execution_plan_refs, "
            "order_intent_refs, order_state_refs, fill_event_refs, strategy_sleeve_intent_refs, "
            "portfolio_allocation_decision_ref, decision_outcome_ref, risk_decision_ref "
            "from decision_audit_records "
            "where decision_id = :decision_id order by audit_revision_id desc limit 1"
        ), {"decision_id": decision_id}).mappings().first()
        execution_order_terminal_no_fill_summary = conn.execute(
            text(
                "select "
                "string_agg(distinct coalesce(state, 'null'), ',' order by coalesce(state, 'null')) as states, "
                "string_agg(distinct coalesce(source_system, 'null'), ',' order by coalesce(source_system, 'null')) as source_systems, "
                "string_agg(distinct coalesce(execution_style, 'null'), ',' order by coalesce(execution_style, 'null')) as execution_styles, "
                "string_agg(distinct coalesce(position_intent, 'null'), ',' order by coalesce(position_intent, 'null')) as position_intents "
                "from execution_orders "
                "where decision_id = :decision_id "
                "and state in ('FAILED', 'REJECTED', 'CANCELED', 'BLOCKED', 'EXPIRED', 'DRY_RUN')"
            ),
            {"decision_id": decision_id},
        ).mappings().first() or {}
        order_state_terminal_no_fill_summary = conn.execute(
            text(
                "select "
                "string_agg(distinct coalesce(status, 'null'), ',' order by coalesce(status, 'null')) as statuses, "
                "string_agg(distinct coalesce(position_intent, 'null'), ',' order by coalesce(position_intent, 'null')) as position_intents "
                "from order_states "
                "where decision_id = :decision_id "
                "and status in ('FAILED', 'REJECTED', 'CANCELED', 'BLOCKED', 'EXPIRED', 'DRY_RUN')"
            ),
            {"decision_id": decision_id},
        ).mappings().first() or {}
        terminal_no_fill_order_state_drilldown = [
            dict(row)
            for row in conn.execute(
                text(
                    "select o.order_id, o.client_order_id, o.intent_id, o.allocation_id, o.decision_id, "
                    "       o.symbol, o.side, o.pos_side, o.position_intent, o.execution_action, "
                    "       o.order_type, o.time_in_force, o.source_system, o.execution_style, "
                    "       o.reduce_only, o.close_only, o.state as execution_order_state, "
                    "       o.created_at as execution_order_created_at, o.updated_at as execution_order_updated_at, "
                    "       o.raw_payload ->> 'status' as execution_order_payload_status, "
                    "       o.raw_payload -> 'order_state' ->> 'status' as nested_order_state_status, "
                    "       nullif(o.venue_order_id, '') is not null as venue_order_id_present, "
                    "       nullif(o.raw_payload ->> 'venue_order_id', '') is not null "
                    "           as raw_payload_venue_order_id_present, "
                    "       nullif(o.raw_payload ->> 'exchange_order_id', '') is not null "
                    "           as raw_payload_exchange_order_id_present, "
                    "       s.status as order_state_status, "
                    "       s.created_at as order_state_created_at, s.submitted_ts, s.last_update_ts, "
                    "       s.row_version as order_state_row_version, "
                    "       s.payload ->> 'status' as order_state_payload_status, "
                    "       nullif(s.exchange_order_id, '') is not null as order_state_exchange_order_id_present, "
                    "       nullif(s.payload ->> 'exchange_order_id', '') is not null "
                    "           as order_state_payload_exchange_order_id_present, "
                    "       c.command_id, c.command_type, c.state as command_state, "
                    "       c.attempt_count, c.last_error is not null as command_has_last_error, "
                    "       c.created_at as command_created_at, c.updated_at as command_updated_at, "
                    "       coalesce(f.execution_fill_count, 0) as execution_fill_count, "
                    "       coalesce(fe.legacy_fill_event_count, 0) as legacy_fill_event_count "
                    "from execution_orders o "
                    "left join order_states s on s.client_order_id = o.client_order_id "
                    "left join lateral ("
                    "    select command_id, command_type, state, attempt_count, last_error, created_at, updated_at "
                    "    from execution_commands "
                    "    where order_id = o.order_id and command_type = 'submit' "
                    "    order by updated_at desc, created_at desc limit 1"
                    ") c on true "
                    "left join ("
                    "    select order_id, client_order_id, count(*) as execution_fill_count "
                    "    from execution_fills group by order_id, client_order_id"
                    ") f on f.order_id = o.order_id or f.client_order_id = o.client_order_id "
                    "left join ("
                    "    select client_order_id, count(*) as legacy_fill_event_count "
                    "    from fill_events group by client_order_id"
                    ") fe on fe.client_order_id = o.client_order_id "
                    "where o.decision_id = :decision_id "
                    "and ("
                    "    o.state in ('FAILED', 'REJECTED', 'CANCELED', 'BLOCKED', 'EXPIRED', 'DRY_RUN') "
                    "    or s.status in ('FAILED', 'REJECTED', 'CANCELED', 'BLOCKED', 'EXPIRED', 'DRY_RUN')"
                    ") "
                    "order by o.created_at asc, o.order_id asc limit 8"
                ),
                {"decision_id": decision_id},
            ).mappings().all()
        ]
        counts = {
            "execution_orders": int(conn.execute(
                text("select count(*) from execution_orders where decision_id = :decision_id"),
                {"decision_id": decision_id},
            ).scalar()),
            "execution_command_flow_enabled": runtime_config["execution_command_flow_enabled"],
            "execution_command_flow_flag_present": runtime_config["execution_command_flow_flag_present"],
            "execution_orders_created_or_submitting": int(conn.execute(
                text(
                    "select count(*) from execution_orders "
                    "where decision_id = :decision_id "
                    "and state in ('CREATED', 'SUBMITTING') "
                    "and venue_order_id is null"
                ),
                {"decision_id": decision_id},
            ).scalar()),
            "execution_orders_submitted_or_later": int(conn.execute(
                text(
                    "select count(*) from execution_orders "
                    "where decision_id = :decision_id "
                    "and (state not in ('CREATED', 'SUBMITTING') or venue_order_id is not null)"
                ),
                {"decision_id": decision_id},
            ).scalar()),
            "execution_orders_terminal_no_fill": int(conn.execute(
                text(
                    "select count(*) from execution_orders "
                    "where decision_id = :decision_id "
                    "and state in ('FAILED', 'REJECTED', 'CANCELED', 'BLOCKED', 'EXPIRED', 'DRY_RUN')"
                ),
                {"decision_id": decision_id},
            ).scalar()),
            "execution_orders_terminal_no_fill_states": execution_order_terminal_no_fill_summary.get("states"),
            "execution_orders_terminal_no_fill_source_systems": execution_order_terminal_no_fill_summary.get("source_systems"),
            "execution_orders_terminal_no_fill_execution_styles": execution_order_terminal_no_fill_summary.get("execution_styles"),
            "execution_orders_terminal_no_fill_position_intents": execution_order_terminal_no_fill_summary.get("position_intents"),
            "execution_commands": int(conn.execute(
                text(
                    "select count(*) from execution_commands c "
                    "join execution_orders o on o.order_id = c.order_id "
                    "where o.decision_id = :decision_id"
                ),
                {"decision_id": decision_id},
            ).scalar()),
            "execution_submit_commands": int(conn.execute(
                text(
                    "select count(*) from execution_commands c "
                    "join execution_orders o on o.order_id = c.order_id "
                    "where o.decision_id = :decision_id and c.command_type = 'submit'"
                ),
                {"decision_id": decision_id},
            ).scalar()),
            "execution_submit_commands_pending": int(conn.execute(
                text(
                    "select count(*) from execution_commands c "
                    "join execution_orders o on o.order_id = c.order_id "
                    "where o.decision_id = :decision_id "
                    "and c.command_type = 'submit' and c.state = 'PENDING'"
                ),
                {"decision_id": decision_id},
            ).scalar()),
            "execution_submit_commands_claimed": int(conn.execute(
                text(
                    "select count(*) from execution_commands c "
                    "join execution_orders o on o.order_id = c.order_id "
                    "where o.decision_id = :decision_id "
                    "and c.command_type = 'submit' and c.state = 'CLAIMED'"
                ),
                {"decision_id": decision_id},
            ).scalar()),
            "execution_submit_commands_sent": int(conn.execute(
                text(
                    "select count(*) from execution_commands c "
                    "join execution_orders o on o.order_id = c.order_id "
                    "where o.decision_id = :decision_id "
                    "and c.command_type = 'submit' and c.state = 'SENT'"
                ),
                {"decision_id": decision_id},
            ).scalar()),
            "execution_submit_commands_failed": int(conn.execute(
                text(
                    "select count(*) from execution_commands c "
                    "join execution_orders o on o.order_id = c.order_id "
                    "where o.decision_id = :decision_id "
                    "and c.command_type = 'submit' and c.state = 'FAILED'"
                ),
                {"decision_id": decision_id},
            ).scalar()),
            "order_states": int(conn.execute(
                text("select count(*) from order_states where decision_id = :decision_id"),
                {"decision_id": decision_id},
            ).scalar()),
            "order_states_created_or_submitting": int(conn.execute(
                text(
                    "select count(*) from order_states "
                    "where decision_id = :decision_id "
                    "and status in ('CREATED', 'SUBMITTING') "
                    "and exchange_order_id is null"
                ),
                {"decision_id": decision_id},
            ).scalar()),
            "order_states_submitted_or_later": int(conn.execute(
                text(
                    "select count(*) from order_states "
                    "where decision_id = :decision_id "
                    "and (status not in ('CREATED', 'SUBMITTING') or exchange_order_id is not null)"
                ),
                {"decision_id": decision_id},
            ).scalar()),
            "order_states_terminal_no_fill": int(conn.execute(
                text(
                    "select count(*) from order_states "
                    "where decision_id = :decision_id "
                    "and status in ('FAILED', 'REJECTED', 'CANCELED', 'BLOCKED', 'EXPIRED', 'DRY_RUN')"
                ),
                {"decision_id": decision_id},
            ).scalar()),
            "order_states_terminal_no_fill_statuses": order_state_terminal_no_fill_summary.get("statuses"),
            "order_states_terminal_no_fill_position_intents": order_state_terminal_no_fill_summary.get("position_intents"),
            "terminal_no_fill_order_state_drilldown": terminal_no_fill_order_state_drilldown,
            "execution_fills": int(conn.execute(
                text("select count(*) from execution_fills where decision_id = :decision_id"),
                {"decision_id": decision_id},
            ).scalar()),
            "execution_fills_via_orders": int(conn.execute(
                text(
                    "select count(*) from execution_fills f "
                    "join execution_orders o on o.order_id = f.order_id "
                    "where o.decision_id = :decision_id"
                ),
                {"decision_id": decision_id},
            ).scalar()),
            "legacy_fill_events": int(conn.execute(
                text("select count(*) from fill_events where decision_id = :decision_id"),
                {"decision_id": decision_id},
            ).scalar()),
            "legacy_fill_events_via_orders": int(conn.execute(
                text(
                    "select count(*) from fill_events f "
                    "join execution_orders o on o.client_order_id = f.client_order_id "
                    "where o.decision_id = :decision_id"
                ),
                {"decision_id": decision_id},
            ).scalar()),
        }
        return audit, counts

    latest_audit = None
    decision_counts = {}
    if latest:
        latest_audit, decision_counts = decision_audit_and_counts(latest)
    latest_executable_directional_audit, latest_executable_directional_counts = decision_audit_and_counts(
        latest_executable_directional
    )
    target_convergence_guard_flag = "target_convergence_open_orders_block_exposure_increase"
    target_convergence_guard_coverage = dict(conn.execute(text(
        "select count(*) as directional_decisions_total, "
        "       count(*) filter (where created_at >= now() - interval '24 hour') as directional_decisions_24h, "
        "       count(*) filter (where created_at >= now() - interval '1 hour') as directional_decisions_1h, "
        "       count(*) filter (where payload::text like '%' || :guard_flag || '%') as guard_hits_total, "
        "       count(*) filter ("
        "           where created_at >= now() - interval '24 hour' "
        "           and payload::text like '%' || :guard_flag || '%'"
        "       ) as guard_hits_24h, "
        "       count(*) filter ("
        "           where created_at >= now() - interval '1 hour' "
        "           and payload::text like '%' || :guard_flag || '%'"
        "       ) as guard_hits_1h "
        "from portfolio_allocation_decisions "
        "where symbol = :symbol and primary_family = 'directional'"
    ), {"symbol": symbol, "guard_flag": target_convergence_guard_flag}).mappings().first() or {})
    latest_target_convergence_guard_hit = conn.execute(text(
        "select decision_id, created_at, route_action, expected_edge_bps, expected_cost_bps "
        "from portfolio_allocation_decisions "
        "where symbol = :symbol and primary_family = 'directional' "
        "and payload::text like '%' || :guard_flag || '%' "
        "order by created_at desc limit 1"
    ), {"symbol": symbol, "guard_flag": target_convergence_guard_flag}).mappings().first()
    execution_open_orders = dict(conn.execute(text(
        "select count(*) as open_order_count, "
        "       count(*) filter (where strategy_family = 'directional') as directional_open_order_count, "
        "       min(created_at) as oldest_open_order_created_at, "
        "       max(updated_at) as latest_open_order_updated_at, "
        "       string_agg(distinct coalesce(state, 'null'), ',' order by coalesce(state, 'null')) as states "
        "from execution_orders "
        "where symbol = :symbol "
        "and state not in ('FILLED', 'CANCELED', 'REJECTED', 'BLOCKED', 'DRY_RUN', 'FAILED', 'EXPIRED')"
    ), {"symbol": symbol}).mappings().first() or {})
    legacy_open_order_states = dict(conn.execute(text(
        "select count(*) as open_order_count, "
        "       count(*) filter (where strategy_family = 'directional') as directional_open_order_count, "
        "       min(created_at) as oldest_open_order_created_at, "
        "       max(last_update_ts) as latest_open_order_updated_at, "
        "       string_agg(distinct coalesce(status, 'null'), ',' order by coalesce(status, 'null')) as states "
        "from order_states "
        "where symbol = :symbol "
        "and status not in ('FILLED', 'CANCELED', 'REJECTED', 'BLOCKED', 'DRY_RUN', 'FAILED', 'EXPIRED')"
    ), {"symbol": symbol}).mappings().first() or {})
    target_convergence_guard = {
        "symbol": symbol,
        "guard_flag": target_convergence_guard_flag,
        "coverage": target_convergence_guard_coverage,
        "latest_guard_hit": dict(latest_target_convergence_guard_hit) if latest_target_convergence_guard_hit else None,
        "current_open_orders": {
            "execution_orders": execution_open_orders,
            "legacy_order_states": legacy_open_order_states,
        },
    }

    claimed_submit_counts = dict(conn.execute(text(
        "select count(*) as total, "
        "       count(*) filter (where o.created_at >= now() - interval '24 hour') as last_24h, "
        "       count(*) filter (where o.created_at >= now() - interval '1 hour') as last_1h, "
        "       min(o.created_at) as oldest_created_at, "
        "       max(o.updated_at) as latest_updated_at "
        "from execution_orders o "
        "join execution_commands c on c.order_id = o.order_id "
        "where o.symbol = :symbol "
        "and o.state in ('CREATED', 'SUBMITTING') "
        "and o.venue_order_id is null "
        "and c.command_type = 'submit' "
        "and c.state = 'CLAIMED' "
        "and not exists ("
        "    select 1 from execution_fills f "
        "    where f.order_id = o.order_id or f.client_order_id = o.client_order_id"
        ")"
    ), {"symbol": symbol}).mappings().first() or {})
    latest_claimed_submit = conn.execute(text(
        "select o.order_id, o.client_order_id, o.intent_id, o.decision_id, "
        "       o.symbol, o.state as execution_order_state, o.venue_order_id, "
        "       o.position_intent, o.reduce_only, o.close_only, o.product_type, o.margin_mode, "
        "       o.created_at as order_created_at, o.updated_at as order_updated_at, "
        "       o.raw_payload ->> 'status' as raw_payload_status, "
        "       o.raw_payload ->> 'venue_order_id' as raw_payload_venue_order_id, "
        "       o.raw_payload ->> 'exchange_order_id' as raw_payload_exchange_order_id, "
        "       s.status as order_state_status, s.exchange_order_id, "
        "       s.payload ->> 'status' as order_state_payload_status, "
        "       s.payload ->> 'exchange_order_id' as order_state_payload_exchange_order_id, "
        "       s.row_version as order_state_row_version, "
        "       c.command_id, c.state as command_state, c.attempt_count, c.last_error, "
        "       c.created_at as command_created_at, c.updated_at as command_updated_at, "
        "       coalesce(f.fill_count, 0) as execution_fill_count, "
        "       coalesce(fe.fill_event_count, 0) as fill_event_count, "
        "       ob.obligation_id, ob.status as obligation_status "
        "from execution_orders o "
        "join execution_commands c on c.order_id = o.order_id "
        "left join order_states s on s.client_order_id = o.client_order_id "
        "left join ("
        "    select order_id, client_order_id, count(*) as fill_count "
        "    from execution_fills group by order_id, client_order_id"
        ") f on f.order_id = o.order_id or f.client_order_id = o.client_order_id "
        "left join ("
        "    select client_order_id, count(*) as fill_event_count "
        "    from fill_events group by client_order_id"
        ") fe on fe.client_order_id = o.client_order_id "
        "left join order_obligations ob on ob.client_order_id = o.client_order_id "
        "where o.symbol = :symbol "
        "and o.state in ('CREATED', 'SUBMITTING') "
        "and o.venue_order_id is null "
        "and c.command_type = 'submit' "
        "and c.state = 'CLAIMED' "
        "and coalesce(f.fill_count, 0) = 0 "
        "order by o.updated_at desc, c.updated_at desc "
        "limit 1"
    ), {"symbol": symbol}).mappings().first()
    latest_reconciliation = conn.execute(text(
        "select reconciliation_id, decision_id, as_of_ts, created_at, severity, halt_required, "
        "       product_type, margin_mode, primary_symbol, "
        "       payload ->> 'recommended_operator_action' as recommended_operator_action, "
        "       payload ->> 'auto_repairable' as auto_repairable, "
        "       payload ->> 'safe_to_resume' as safe_to_resume "
        "from reconciliation_reports "
        "order by as_of_ts desc, created_at desc limit 1"
    )).mappings().first()
    latest_baseline = conn.execute(text(
        "select generation_id, baseline_kind, account_source, product_type, margin_mode, "
        "       allowed_symbols, exchange_snapshot_ts, imported_at, "
        "       safe_for_automatic_continuation, requires_operator_review, "
        "       operator_action_ref, trigger_reason, reason_codes, "
        "       balance_count, position_count, open_order_count, fill_count, created_at "
        "from baseline_generations "
        "order by imported_at desc, created_at desc limit 1"
    )).mappings().first()
    claimed_submit_client_order_id = (
        latest_claimed_submit["client_order_id"] if latest_claimed_submit else None
    )
    claimed_submit_findings = []
    claimed_submit_finding_counts = {}
    if latest_reconciliation and claimed_submit_client_order_id:
        claimed_submit_findings = [
            dict(row)
            for row in conn.execute(text(
                "select finding_id, scope_kind, scope_ref, layer, finding_type, severity_class, "
                "       review_required, only_reduce_required, halt_required, blocks_resume, "
                "       reason_code, created_at "
                "from reconciliation_findings "
                "where reconciliation_id = :reconciliation_id "
                "and (scope_ref = :client_order_id or details::text like '%' || :client_order_id || '%') "
                "order by created_at desc"
            ), {
                "reconciliation_id": latest_reconciliation["reconciliation_id"],
                "client_order_id": claimed_submit_client_order_id,
            }).mappings().all()
        ]
        claimed_submit_finding_counts = dict(conn.execute(text(
            "select count(*) as total, "
            "       count(*) filter (where review_required or only_reduce_required "
            "           or halt_required or blocks_resume) as blocking, "
            "       count(*) filter (where severity_class = 'info' "
            "           and reason_code = 'local_fill_older_than_exchange_lookback_window') "
            "           as historic_orphan_fill_info, "
            "       count(*) filter (where scope_ref = :client_order_id "
            "           or details::text like '%' || :client_order_id || '%') as mentions_stuck_order "
            "from reconciliation_findings "
            "where reconciliation_id = :reconciliation_id"
        ), {
            "reconciliation_id": latest_reconciliation["reconciliation_id"],
            "client_order_id": claimed_submit_client_order_id,
        }).mappings().first() or {})
    claimed_submit_operator_actions = {}
    latest_claimed_submit_operator_action = None
    if claimed_submit_client_order_id:
        claimed_submit_operator_actions = dict(conn.execute(text(
            "select count(*) filter (where payload::text ilike '%resolve_stuck_submission%') "
            "           as resolve_stuck_submission_total, "
            "       count(*) filter (where payload::text ilike '%' || :client_order_id || '%') "
            "           as mentions_stuck_order_total, "
            "       count(*) filter (where payload::text ilike '%resolve_stuck_submission%' "
            "           and payload::text ilike '%' || :client_order_id || '%') "
            "           as resolve_stuck_submission_for_order "
            "from event_store where topic = 'system.operator_actions'"
        ), {"client_order_id": claimed_submit_client_order_id}).mappings().first() or {})
        latest_claimed_submit_operator_action = conn.execute(text(
            "select event_id, event_timestamp, event_type, source_component, "
            "       payload ->> 'action' as action, payload ->> 'status' as status, "
            "       payload ->> 'reason' as reason "
            "from event_store "
            "where topic = 'system.operator_actions' "
            "and payload::text ilike '%' || :client_order_id || '%' "
            "order by event_timestamp desc, sequence_id desc limit 1"
        ), {"client_order_id": claimed_submit_client_order_id}).mappings().first()
    claimed_submit_stuck_submission = {
        "symbol": symbol,
        "root_cause": "execution_submit_command_claimed_without_terminal_order_ack",
        "required_operator_confirmation_prefix": "resolve_claimed_submit_as_failed:",
        "coverage": claimed_submit_counts,
        "latest_order": dict(latest_claimed_submit) if latest_claimed_submit else None,
        "latest_reconciliation": dict(latest_reconciliation) if latest_reconciliation else None,
        "latest_reconciliation_finding_counts": claimed_submit_finding_counts,
        "latest_reconciliation_findings_for_order": claimed_submit_findings,
        "latest_baseline": dict(latest_baseline) if latest_baseline else None,
        "operator_action_counts": claimed_submit_operator_actions,
        "latest_operator_action_for_order": (
            dict(latest_claimed_submit_operator_action)
            if latest_claimed_submit_operator_action
            else None
        ),
    }
    impulse_chase_guard_flags = (
        "long_impulse_entry_post_spike_pullback_unconfirmed",
        "short_impulse_entry_post_spike_pullback_unconfirmed",
        "long_impulse_entry_extreme_chase_unconfirmed",
        "short_impulse_entry_extreme_chase_unconfirmed",
    )
    impulse_chase_guard_params = {
        f"impulse_guard_{idx}": f"%{flag}%"
        for idx, flag in enumerate(impulse_chase_guard_flags)
    }
    impulse_chase_guard_predicate = " or ".join(
        f"payload::text like :impulse_guard_{idx}"
        for idx, _ in enumerate(impulse_chase_guard_flags)
    )
    impulse_chase_guard_coverage = dict(conn.execute(text(
        "select count(*) as directional_decisions_total, "
        "       count(*) filter (where created_at >= now() - interval '24 hour') as directional_decisions_24h, "
        "       count(*) filter (where created_at >= now() - interval '1 hour') as directional_decisions_1h, "
        f"      count(*) filter (where {impulse_chase_guard_predicate}) as guard_hits_total, "
        "       count(*) filter ("
        "           where created_at >= now() - interval '24 hour' "
        f"          and ({impulse_chase_guard_predicate})"
        "       ) as guard_hits_24h, "
        "       count(*) filter ("
        "           where created_at >= now() - interval '1 hour' "
        f"          and ({impulse_chase_guard_predicate})"
        "       ) as guard_hits_1h, "
        "       count(*) filter ("
        f"          where ({impulse_chase_guard_predicate}) "
        "           and route_action in ('hold_current', 'advisory_only')"
        "       ) as blocked_live_entry_hits_total, "
        "       count(*) filter ("
        "           where created_at >= now() - interval '24 hour' "
        f"          and ({impulse_chase_guard_predicate}) "
        "           and route_action in ('hold_current', 'advisory_only')"
        "       ) as blocked_live_entry_hits_24h, "
        "       count(*) filter ("
        "           where created_at >= now() - interval '1 hour' "
        f"          and ({impulse_chase_guard_predicate}) "
        "           and route_action in ('hold_current', 'advisory_only')"
        "       ) as blocked_live_entry_hits_1h "
        "from portfolio_allocation_decisions "
        "where symbol = :symbol and primary_family = 'directional'"
    ), {"symbol": symbol, **impulse_chase_guard_params}).mappings().first() or {})
    latest_impulse_chase_guard_hit = conn.execute(text(
        "select decision_id, created_at, route_action, expected_edge_bps, expected_cost_bps, payload "
        "from portfolio_allocation_decisions "
        "where symbol = :symbol and primary_family = 'directional' "
        f"and ({impulse_chase_guard_predicate}) "
        "order by created_at desc limit 1"
    ), {"symbol": symbol, **impulse_chase_guard_params}).mappings().first()
    impulse_chase_flag_hits_total = {}
    for idx, flag in enumerate(impulse_chase_guard_flags):
        impulse_chase_flag_hits_total[flag] = int(conn.execute(text(
            "select count(*) from portfolio_allocation_decisions "
            "where symbol = :symbol and primary_family = 'directional' "
            f"and payload::text like :impulse_guard_{idx}"
        ), {"symbol": symbol, **impulse_chase_guard_params}).scalar() or 0)

    def impulse_chase_guard_hit_summary(row):
        if not row:
            return None
        payload_text = json.dumps(row.get("payload") or {}, default=str)
        return {
            "decision_id": row.get("decision_id"),
            "created_at": row.get("created_at"),
            "route_action": row.get("route_action"),
            "expected_edge_bps": row.get("expected_edge_bps"),
            "expected_cost_bps": row.get("expected_cost_bps"),
            "matched_guard_flags": [
                flag for flag in impulse_chase_guard_flags if flag in payload_text
            ],
        }

    directional_impulse_chase_guard = {
        "symbol": symbol,
        "guard_flags": list(impulse_chase_guard_flags),
        "coverage": impulse_chase_guard_coverage,
        "flag_hits_total": impulse_chase_flag_hits_total,
        "latest_guard_hit": impulse_chase_guard_hit_summary(latest_impulse_chase_guard_hit),
    }
    okx_scale_in_mismatch_reason = "okx_leg_action_mismatch_with_position_intent"
    okx_scale_in_mismatch_needle = f"%{okx_scale_in_mismatch_reason}%"

    def count_row_with_windows(table_sql, params):
        return dict(conn.execute(text(table_sql), params).mappings().first() or {})

    created_no_command_root_cause = "execution_command_missing_for_created_order"
    execution_orders_missing_submit_command_counts = count_row_with_windows(
        "select count(*) as total, "
        "       count(*) filter (where created_at >= now() - interval '24 hour') as last_24h, "
        "       count(*) filter (where created_at >= now() - interval '1 hour') as last_1h, "
        "       max(created_at) as latest_created_at "
        "from execution_orders o "
        "where o.symbol = :symbol "
        "  and o.strategy_family = 'directional' "
        "  and o.state in ('CREATED', 'SUBMITTING') "
        "  and o.venue_order_id is null "
        "  and not exists ("
        "      select 1 from execution_commands c "
        "      where c.order_id = o.order_id and c.command_type = 'submit'"
        "  )",
        {"symbol": symbol},
    )
    order_states_missing_submit_command_counts = count_row_with_windows(
        "select count(*) as total, "
        "       count(*) filter (where s.created_at >= now() - interval '24 hour') as last_24h, "
        "       count(*) filter (where s.created_at >= now() - interval '1 hour') as last_1h, "
        "       max(s.created_at) as latest_created_at "
        "from order_states s "
        "where s.symbol = :symbol "
        "  and s.strategy_family = 'directional' "
        "  and s.status in ('CREATED', 'SUBMITTING') "
        "  and s.exchange_order_id is null "
        "  and not exists ("
        "      select 1 from execution_orders o "
        "      join execution_commands c on c.order_id = o.order_id and c.command_type = 'submit' "
        "      where o.client_order_id = s.client_order_id"
        "  )",
        {"symbol": symbol},
    )
    latest_created_no_command_execution_order_rows = conn.execute(text(
        "select o.created_at, o.updated_at, o.order_id, o.client_order_id, o.decision_id, "
        "       o.state, o.position_intent, o.side, o.pos_side, o.strategy_family, "
        "       count(c.command_id) as command_count "
        "from execution_orders o "
        "left join execution_commands c on c.order_id = o.order_id "
        "where o.symbol = :symbol "
        "  and o.strategy_family = 'directional' "
        "  and o.state in ('CREATED', 'SUBMITTING') "
        "  and o.venue_order_id is null "
        "  and not exists ("
        "      select 1 from execution_commands submit_c "
        "      where submit_c.order_id = o.order_id and submit_c.command_type = 'submit'"
        "  ) "
        "group by o.created_at, o.updated_at, o.order_id, o.client_order_id, o.decision_id, "
        "         o.state, o.position_intent, o.side, o.pos_side, o.strategy_family "
        "order by o.created_at desc limit 5"
    ), {"symbol": symbol}).mappings().all()
    latest_created_no_command_order_state_rows = conn.execute(text(
        "select s.created_at, s.last_update_ts, s.client_order_id, s.decision_id, "
        "       s.status, s.strategy_family "
        "from order_states s "
        "where s.symbol = :symbol "
        "  and s.strategy_family = 'directional' "
        "  and s.status in ('CREATED', 'SUBMITTING') "
        "  and s.exchange_order_id is null "
        "  and not exists ("
        "      select 1 from execution_orders o "
        "      join execution_commands c on c.order_id = o.order_id and c.command_type = 'submit' "
        "      where o.client_order_id = s.client_order_id"
        "  ) "
        "order by s.created_at desc limit 5"
    ), {"symbol": symbol}).mappings().all()
    created_no_command_directional_order = {
        "symbol": symbol,
        "root_cause": created_no_command_root_cause,
        "execution_order_missing_submit_command_counts": execution_orders_missing_submit_command_counts,
        "order_state_missing_submit_command_counts": order_states_missing_submit_command_counts,
        "latest_execution_order_rows": [
            dict(row)
            for row in latest_created_no_command_execution_order_rows
        ],
        "latest_order_state_rows": [
            dict(row)
            for row in latest_created_no_command_order_state_rows
        ],
    }

    okx_scale_in_history_counts = count_row_with_windows(
        "select count(*) as total, "
        "       count(*) filter (where created_at >= now() - interval '24 hour') as last_24h, "
        "       count(*) filter (where created_at >= now() - interval '1 hour') as last_1h, "
        "       max(created_at) as latest_created_at "
        "from execution_order_state_history "
        "where reason_code = :reason",
        {"reason": okx_scale_in_mismatch_reason},
    )
    okx_scale_in_execution_payload_counts = count_row_with_windows(
        "select count(*) as total, "
        "       count(*) filter (where created_at >= now() - interval '24 hour') as last_24h, "
        "       count(*) filter (where created_at >= now() - interval '1 hour') as last_1h, "
        "       max(created_at) as latest_created_at "
        "from execution_orders "
        "where raw_payload::text like :needle",
        {"needle": okx_scale_in_mismatch_needle},
    )
    okx_scale_in_order_state_payload_counts = count_row_with_windows(
        "select count(*) as total, "
        "       count(*) filter (where created_at >= now() - interval '24 hour') as last_24h, "
        "       count(*) filter (where created_at >= now() - interval '1 hour') as last_1h, "
        "       max(created_at) as latest_created_at "
        "from order_states "
        "where payload::text like :needle",
        {"needle": okx_scale_in_mismatch_needle},
    )
    okx_scale_in_open_leg_counts = count_row_with_windows(
        "select count(*) as total, "
        "       count(*) filter (where created_at >= now() - interval '24 hour') as last_24h, "
        "       count(*) filter (where created_at >= now() - interval '1 hour') as last_1h, "
        "       max(created_at) as latest_created_at "
        "from execution_orders "
        "where symbol = :symbol "
        "  and position_intent in ('scale_in_long', 'scale_in_short') "
        "  and coalesce(raw_payload ->> 'leg_action', '') = 'open'",
        {"symbol": symbol},
    )
    latest_okx_scale_in_mismatch_rows = conn.execute(text(
        "select created_at, order_id, position_intent, side, pos_side, state, "
        "       raw_payload ->> 'leg_action' as raw_leg_action, "
        "       raw_payload ->> 'status' as raw_status "
        "from execution_orders "
        "where raw_payload::text like :needle "
        "order by created_at desc limit 5"
    ), {"needle": okx_scale_in_mismatch_needle}).mappings().all()

    def okx_scale_in_mismatch_summary(row):
        return {
            "created_at": row.get("created_at"),
            "order_id": row.get("order_id"),
            "position_intent": row.get("position_intent"),
            "side": row.get("side"),
            "pos_side": row.get("pos_side"),
            "leg_action": row.get("raw_leg_action"),
            "state": row.get("state"),
            "raw_status": row.get("raw_status"),
        }

    okx_hedge_scale_in_intent = {
        "symbol": symbol,
        "mismatch_reason": okx_scale_in_mismatch_reason,
        "history_reason_counts": okx_scale_in_history_counts,
        "execution_payload_reason_counts": okx_scale_in_execution_payload_counts,
        "order_state_payload_reason_counts": okx_scale_in_order_state_payload_counts,
        "open_scale_in_leg_counts": okx_scale_in_open_leg_counts,
        "latest_mismatches": [
            okx_scale_in_mismatch_summary(row)
            for row in latest_okx_scale_in_mismatch_rows
        ],
    }
    top_level_status_mismatch_groups = [
        dict(row)
        for row in conn.execute(text(
            "select state, "
            "       coalesce(raw_payload ->> 'status', '<missing>') as raw_payload_status, "
            "       coalesce(raw_payload -> 'order_state' ->> 'status', '<missing>') "
            "           as nested_order_state_status, "
            "       count(*) as count, "
            "       max(updated_at) as latest_updated_at "
            "from execution_orders "
            "where coalesce(raw_payload ->> 'status', '') <> coalesce(state, '') "
            "group by state, "
            "         coalesce(raw_payload ->> 'status', '<missing>'), "
            "         coalesce(raw_payload -> 'order_state' ->> 'status', '<missing>') "
            "order by count desc, state, raw_payload_status"
        )).mappings().all()
    ]
    nested_status_mismatch_groups = [
        dict(row)
        for row in conn.execute(text(
            "select state, "
            "       coalesce(raw_payload -> 'order_state' ->> 'status', '<missing>') "
            "           as nested_order_state_status, "
            "       count(*) as count, "
            "       max(updated_at) as latest_updated_at "
            "from execution_orders "
            "where coalesce(raw_payload -> 'order_state' ->> 'status', '') <> coalesce(state, '') "
            "group by state, coalesce(raw_payload -> 'order_state' ->> 'status', '<missing>') "
            "order by count desc, state"
        )).mappings().all()
    ]
    latest_status_mismatch_rows = [
        dict(row)
        for row in conn.execute(text(
            "select order_id, client_order_id, symbol, state, "
            "       raw_payload ->> 'status' as raw_payload_status, "
            "       raw_payload -> 'order_state' ->> 'status' as nested_order_state_status, "
            "       created_at, updated_at "
            "from execution_orders "
            "where coalesce(raw_payload ->> 'status', '') <> coalesce(state, '') "
            "order by updated_at desc "
            "limit 10"
        )).mappings().all()
    ]
    target_payload_status_residual = conn.execute(text(
        "select order_id, client_order_id, symbol, state, "
        "       raw_payload ->> 'status' as raw_payload_status, "
        "       raw_payload -> 'order_state' ->> 'status' as nested_order_state_status, "
        "       venue_order_id, updated_at "
        "from execution_orders "
        "where client_order_id = 'cl9d7875bd332bf6fb8a5e2bd248ba21' "
        "limit 1"
    )).mappings().first()
    execution_order_payload_status_residual = {
        "symbol": symbol,
        "authority": {
            "order_status_source": "execution_orders.state",
            "order_state_status_source": "order_states.status",
            "raw_payload_top_level_status_authoritative": False,
            "notes": [
                "Open-order counts and claimed-submit recovery gates filter execution_orders.state/order_states.status.",
                "raw_payload.status is retained as diagnostic payload and may be missing or stale on historical rows.",
            ],
        },
        "coverage": {
            "top_level_status_mismatch_count": sum(
                int(row.get("count") or 0) for row in top_level_status_mismatch_groups
            ),
            "nested_status_mismatch_count": sum(
                int(row.get("count") or 0) for row in nested_status_mismatch_groups
            ),
            "terminal_column_nonterminal_top_level_count": int(conn.execute(text(
                "select count(*) from execution_orders "
                "where state in ('FILLED','CANCELED','REJECTED','BLOCKED','DRY_RUN','FAILED','EXPIRED') "
                "  and coalesce(raw_payload ->> 'status', '') "
                "      not in ('FILLED','CANCELED','REJECTED','BLOCKED','DRY_RUN','FAILED','EXPIRED')"
            )).scalar() or 0),
            "open_column_terminal_top_level_count": int(conn.execute(text(
                "select count(*) from execution_orders "
                "where state not in ('FILLED','CANCELED','REJECTED','BLOCKED','DRY_RUN','FAILED','EXPIRED') "
                "  and coalesce(raw_payload ->> 'status', '') "
                "      in ('FILLED','CANCELED','REJECTED','BLOCKED','DRY_RUN','FAILED','EXPIRED')"
            )).scalar() or 0),
            "open_by_column_count": int(conn.execute(text(
                "select count(*) from execution_orders "
                "where symbol = :symbol "
                "  and state not in ('FILLED','CANCELED','REJECTED','BLOCKED','DRY_RUN','FAILED','EXPIRED')"
            ), {"symbol": symbol}).scalar() or 0),
            "open_by_top_level_raw_payload_count": int(conn.execute(text(
                "select count(*) from execution_orders "
                "where symbol = :symbol "
                "  and coalesce(raw_payload ->> 'status', '') "
                "      not in ('FILLED','CANCELED','REJECTED','BLOCKED','DRY_RUN','FAILED','EXPIRED')"
            ), {"symbol": symbol}).scalar() or 0),
            "terminal_column_nonterminal_nested_count": int(conn.execute(text(
                "select count(*) from execution_orders "
                "where state in ('FILLED','CANCELED','REJECTED','BLOCKED','DRY_RUN','FAILED','EXPIRED') "
                "  and coalesce(raw_payload -> 'order_state' ->> 'status', '') "
                "      not in ('FILLED','CANCELED','REJECTED','BLOCKED','DRY_RUN','FAILED','EXPIRED')"
            )).scalar() or 0),
            "open_column_terminal_nested_count": int(conn.execute(text(
                "select count(*) from execution_orders "
                "where state not in ('FILLED','CANCELED','REJECTED','BLOCKED','DRY_RUN','FAILED','EXPIRED') "
                "  and coalesce(raw_payload -> 'order_state' ->> 'status', '') "
                "      in ('FILLED','CANCELED','REJECTED','BLOCKED','DRY_RUN','FAILED','EXPIRED')"
            )).scalar() or 0),
        },
        "top_level_status_mismatch_groups": top_level_status_mismatch_groups[:20],
        "nested_status_mismatch_groups": nested_status_mismatch_groups[:20],
        "latest_mismatch_rows": latest_status_mismatch_rows,
        "target_order": dict(target_payload_status_residual) if target_payload_status_residual else None,
    }
    slippage_cost_row = conn.execute(text(
        "with command_refs as ("
        "  select order_id, "
        "         max(case when command_payload #>> '{intent,limit_price}' ~ '^-?[0-9]+(\\.[0-9]+)?$' "
        "                  then (command_payload #>> '{intent,limit_price}')::numeric end) "
        "             as command_intent_limit_price, "
        "         max(case when command_payload #>> '{intent,reference_price}' ~ '^-?[0-9]+(\\.[0-9]+)?$' "
        "                  then (command_payload #>> '{intent,reference_price}')::numeric end) "
        "             as command_intent_reference_price "
        "  from execution_commands "
        "  where command_type = 'submit' "
        "  group by order_id"
        "), base as ("
        "  select f.symbol, f.side, f.fill_qty, f.fill_price, f.fee_amount, "
        "         f.fee_currency, f.liquidity_role, f.fee_rate, f.exec_type, f.ingestion_ts, "
        "         o.order_id, o.limit_price, o.order_type, o.time_in_force, o.strategy_family, "
        "         case when o.raw_payload #>> '{intent,limit_price}' ~ '^-?[0-9]+(\\.[0-9]+)?$' "
        "              then (o.raw_payload #>> '{intent,limit_price}')::numeric end "
        "             as order_intent_limit_price, "
        "         case when o.raw_payload #>> '{intent,reference_price}' ~ '^-?[0-9]+(\\.[0-9]+)?$' "
        "              then (o.raw_payload #>> '{intent,reference_price}')::numeric end "
        "             as order_intent_reference_price, "
        "         case when o.raw_payload #>> '{order_state,reference_price}' ~ '^-?[0-9]+(\\.[0-9]+)?$' "
        "              then (o.raw_payload #>> '{order_state,reference_price}')::numeric end "
        "             as order_state_reference_price, "
        "         case when o.raw_payload #>> '{order_state,submission_payload,referencePrice}' "
        "                   ~ '^-?[0-9]+(\\.[0-9]+)?$' "
        "              then (o.raw_payload #>> '{order_state,submission_payload,referencePrice}')::numeric end "
        "             as order_state_submission_reference_price, "
        "         cr.command_intent_limit_price, cr.command_intent_reference_price, "
        "         case when f.fill_qty > 0 and f.fill_price > 0 "
        "              then abs(f.fee_amount) / (f.fill_qty * f.fill_price) * 10000 end as actual_fee_bps "
        "  from execution_fills f "
        "  left join execution_orders o on o.order_id = f.order_id "
        "  left join command_refs cr on cr.order_id = f.order_id "
        "  where f.symbol = :symbol"
        "), joined as ("
        "  select base.*, "
        "         coalesce("
        "             limit_price, "
        "             order_intent_limit_price, "
        "             order_intent_reference_price, "
        "             order_state_reference_price, "
        "             order_state_submission_reference_price, "
        "             command_intent_limit_price, "
        "             command_intent_reference_price"
        "         ) as slippage_reference_price, "
        "         case "
        "             when limit_price is not null then 'execution_orders.limit_price' "
        "             when order_intent_limit_price is not null then 'execution_orders.raw_payload.intent.limit_price' "
        "             when order_intent_reference_price is not null then 'execution_orders.raw_payload.intent.reference_price' "
        "             when order_state_reference_price is not null then 'execution_orders.raw_payload.order_state.reference_price' "
        "             when order_state_submission_reference_price is not null "
        "                 then 'execution_orders.raw_payload.order_state.submission_payload.referencePrice' "
        "             when command_intent_limit_price is not null "
        "                 then 'execution_commands.command_payload.intent.limit_price' "
        "             when command_intent_reference_price is not null "
        "                 then 'execution_commands.command_payload.intent.reference_price' "
        "         end as slippage_reference_source "
        "  from base"
        "), final as ("
        "  select joined.*, "
        "         case when slippage_reference_price is not null and slippage_reference_price > 0 "
        "                   and fill_price > 0 and side = 'buy' "
        "              then (fill_price - slippage_reference_price) / slippage_reference_price * 10000 "
        "              when slippage_reference_price is not null and slippage_reference_price > 0 "
        "                   and fill_price > 0 and side = 'sell' "
        "              then (slippage_reference_price - fill_price) / slippage_reference_price * 10000 "
        "         end as reference_fill_slippage_bps "
        "  from joined"
        ") "
        "select count(*) as fills_total, "
        "       count(*) filter (where ingestion_ts >= now() - interval '24 hour') as fills_24h, "
        "       count(*) filter (where order_id is not null) as fills_with_order, "
        "       count(*) filter (where limit_price is not null) as fills_with_limit_price, "
        "       count(*) filter (where order_intent_limit_price is not null) as fills_with_order_intent_limit_price, "
        "       count(*) filter (where order_intent_reference_price is not null) "
        "           as fills_with_order_intent_reference_price, "
        "       count(*) filter (where order_state_reference_price is not null) "
        "           as fills_with_order_state_reference_price, "
        "       count(*) filter (where order_state_submission_reference_price is not null) "
        "           as fills_with_order_state_submission_reference_price, "
        "       count(*) filter (where command_intent_limit_price is not null) "
        "           as fills_with_command_intent_limit_price, "
        "       count(*) filter (where command_intent_reference_price is not null) "
        "           as fills_with_command_intent_reference_price, "
        "       count(*) filter (where slippage_reference_price is not null) as fills_with_slippage_reference_price, "
        "       count(*) filter (where actual_fee_bps is not null) as fee_bps_samples, "
        "       min(actual_fee_bps) as fee_bps_min, "
        "       avg(actual_fee_bps) as fee_bps_mean, "
        "       percentile_disc(0.95) within group (order by actual_fee_bps) as fee_bps_p95, "
        "       max(actual_fee_bps) as fee_bps_max, "
        "       count(*) filter (where reference_fill_slippage_bps is not null) as slippage_proxy_samples, "
        "       min(reference_fill_slippage_bps) as slippage_proxy_min, "
        "       avg(reference_fill_slippage_bps) as slippage_proxy_mean, "
        "       percentile_disc(0.95) within group (order by reference_fill_slippage_bps) as slippage_proxy_p95, "
        "       max(reference_fill_slippage_bps) as slippage_proxy_max, "
        "       max(ingestion_ts) as latest_fill_ts, "
        "       count(*) filter (where liquidity_role is not null) as liquidity_role_samples, "
        "       count(*) filter (where fee_rate is not null) as fee_rate_samples, "
        "       count(*) filter (where liquidity_role = 'maker') as maker_fills, "
        "       count(*) filter (where liquidity_role = 'taker') as taker_fills, "
        "       count(*) filter (where liquidity_role is null) as unknown_liquidity_fills "
        "from final"
    ), {"symbol": symbol}).mappings().first()
    slippage_cost_calibration = dict(slippage_cost_row or {})
    slippage_cost_calibration["symbol"] = symbol
    slippage_cost_calibration["by_reference_source"] = [
        dict(row)
        for row in conn.execute(text(
            "with command_refs as ("
            "  select order_id, "
            "         max(case when command_payload #>> '{intent,limit_price}' ~ '^-?[0-9]+(\\.[0-9]+)?$' "
            "                  then (command_payload #>> '{intent,limit_price}')::numeric end) "
            "             as command_intent_limit_price, "
            "         max(case when command_payload #>> '{intent,reference_price}' ~ '^-?[0-9]+(\\.[0-9]+)?$' "
            "                  then (command_payload #>> '{intent,reference_price}')::numeric end) "
            "             as command_intent_reference_price "
            "  from execution_commands where command_type = 'submit' group by order_id"
            "), joined as ("
            "  select case "
            "             when o.limit_price is not null then 'execution_orders.limit_price' "
            "             when o.raw_payload #>> '{intent,limit_price}' ~ '^-?[0-9]+(\\.[0-9]+)?$' "
            "                 then 'execution_orders.raw_payload.intent.limit_price' "
            "             when o.raw_payload #>> '{intent,reference_price}' ~ '^-?[0-9]+(\\.[0-9]+)?$' "
            "                 then 'execution_orders.raw_payload.intent.reference_price' "
            "             when o.raw_payload #>> '{order_state,reference_price}' ~ '^-?[0-9]+(\\.[0-9]+)?$' "
            "                 then 'execution_orders.raw_payload.order_state.reference_price' "
            "             when o.raw_payload #>> '{order_state,submission_payload,referencePrice}' "
            "                   ~ '^-?[0-9]+(\\.[0-9]+)?$' "
            "                 then 'execution_orders.raw_payload.order_state.submission_payload.referencePrice' "
            "             when cr.command_intent_limit_price is not null "
            "                 then 'execution_commands.command_payload.intent.limit_price' "
            "             when cr.command_intent_reference_price is not null "
            "                 then 'execution_commands.command_payload.intent.reference_price' "
            "             else 'missing' "
            "         end as reference_source "
            "  from execution_fills f "
            "  left join execution_orders o on o.order_id = f.order_id "
            "  left join command_refs cr on cr.order_id = f.order_id "
            "  where f.symbol = :symbol"
            ") "
            "select reference_source, count(*) as n "
            "from joined group by reference_source order by reference_source"
        ), {"symbol": symbol}).mappings().all()
    ]
    slippage_cost_calibration["by_reference_coverage_path"] = [
        dict(row)
        for row in conn.execute(text(
            "with command_refs as ("
            "  select order_id, "
            "         count(*) filter (where command_type = 'submit') as submit_commands, "
            "         string_agg(distinct state, ',' order by state) "
            "             filter (where command_type = 'submit') as submit_command_states, "
            "         max(case when command_type = 'submit' "
            "                    and command_payload #>> '{intent,limit_price}' ~ '^-?[0-9]+(\\.[0-9]+)?$' "
            "                  then (command_payload #>> '{intent,limit_price}')::numeric end) "
            "             as command_intent_limit_price, "
            "         max(case when command_type = 'submit' "
            "                    and command_payload #>> '{intent,reference_price}' ~ '^-?[0-9]+(\\.[0-9]+)?$' "
            "                  then (command_payload #>> '{intent,reference_price}')::numeric end) "
            "             as command_intent_reference_price "
            "  from execution_commands group by order_id"
            "), joined as ("
            "  select f.order_id, f.ingestion_ts, o.created_at as order_created_at, "
            "         o.order_type, o.time_in_force, o.source_system, o.execution_style, "
            "         o.strategy_family, o.state as order_state, o.limit_price, "
            "         case when o.raw_payload #>> '{intent,limit_price}' ~ '^-?[0-9]+(\\.[0-9]+)?$' "
            "              then (o.raw_payload #>> '{intent,limit_price}')::numeric end "
            "             as order_intent_limit_price, "
            "         case when o.raw_payload #>> '{intent,reference_price}' ~ '^-?[0-9]+(\\.[0-9]+)?$' "
            "              then (o.raw_payload #>> '{intent,reference_price}')::numeric end "
            "             as order_intent_reference_price, "
            "         case when o.raw_payload #>> '{order_state,reference_price}' ~ '^-?[0-9]+(\\.[0-9]+)?$' "
            "              then (o.raw_payload #>> '{order_state,reference_price}')::numeric end "
            "             as order_state_reference_price, "
            "         case when o.raw_payload #>> '{order_state,submission_payload,referencePrice}' "
            "                   ~ '^-?[0-9]+(\\.[0-9]+)?$' "
            "              then (o.raw_payload #>> '{order_state,submission_payload,referencePrice}')::numeric end "
            "             as order_state_submission_reference_price, "
            "         cr.submit_commands, cr.submit_command_states, "
            "         cr.command_intent_limit_price, cr.command_intent_reference_price "
            "  from execution_fills f "
            "  left join execution_orders o on o.order_id = f.order_id "
            "  left join command_refs cr on cr.order_id = f.order_id "
            "  where f.symbol = :symbol"
            "), classified as ("
            "  select *, "
            "         case "
            "             when limit_price is not null then 'execution_orders.limit_price' "
            "             when order_intent_limit_price is not null then 'execution_orders.raw_payload.intent.limit_price' "
            "             when order_intent_reference_price is not null "
            "                 then 'execution_orders.raw_payload.intent.reference_price' "
            "             when order_state_reference_price is not null "
            "                 then 'execution_orders.raw_payload.order_state.reference_price' "
            "             when order_state_submission_reference_price is not null "
            "                 then 'execution_orders.raw_payload.order_state.submission_payload.referencePrice' "
            "             when command_intent_limit_price is not null "
            "                 then 'execution_commands.command_payload.intent.limit_price' "
            "             when command_intent_reference_price is not null "
            "                 then 'execution_commands.command_payload.intent.reference_price' "
            "             else 'missing' "
            "         end as reference_source, "
            "         case when coalesce(submit_commands, 0) > 0 "
            "              then 'has_submit_command' else 'no_submit_command' end as command_presence, "
            "         case when command_intent_reference_price is not null or command_intent_limit_price is not null "
            "              then 'command_has_reference' else 'command_no_reference' end as command_reference_presence "
            "  from joined"
            ") "
            "select case when reference_source = 'missing' then 'missing' else 'covered' end as coverage, "
            "       coalesce(source_system, 'null') as source_system, "
            "       coalesce(order_type, 'null') as order_type, "
            "       coalesce(time_in_force, 'null') as time_in_force, "
            "       coalesce(execution_style, 'null') as execution_style, "
            "       coalesce(strategy_family, 'null') as strategy_family, "
            "       coalesce(order_state, 'null') as order_state, "
            "       command_presence, command_reference_presence, "
            "       coalesce(submit_command_states, 'none') as submit_command_states, "
            "       count(*) as n, count(distinct order_id) as order_count, "
            "       min(order_created_at) as first_order_created_at, "
            "       max(order_created_at) as last_order_created_at, "
            "       min(ingestion_ts) as first_fill_ingestion_ts, "
            "       max(ingestion_ts) as last_fill_ingestion_ts "
            "from classified "
            "group by coverage, source_system, order_type, time_in_force, execution_style, strategy_family, "
            "         order_state, command_presence, command_reference_presence, submit_command_states "
            "order by coverage desc, n desc, source_system, order_type, time_in_force"
        ), {"symbol": symbol}).mappings().all()
    ]
    slippage_cost_calibration["by_liquidity_role"] = [
        dict(row)
        for row in conn.execute(text(
            "select coalesce(liquidity_role, 'unknown') as liquidity_role, count(*) as n "
            "from execution_fills where symbol = :symbol "
            "group by coalesce(liquidity_role, 'unknown') order by liquidity_role"
        ), {"symbol": symbol}).mappings().all()
    ]
    directional_episode_attribution = {
        "symbol": symbol,
        "recent_decisions": [
            dict(row)
            for row in conn.execute(text(
                "with recent_decisions as ("
                "  select allocation_id, decision_id, symbol, created_at, route_action, primary_family, "
                "         portfolio_requested_notional, portfolio_approved_notional, "
                "         portfolio_budget_cut_notional, expected_edge_bps, expected_cost_bps, "
                "         jsonb_strip_nulls(jsonb_build_object("
                "           'operator_summary', payload::jsonb -> 'operator_summary', "
                "           'expected_edge_bps', payload::jsonb -> 'expected_edge_bps', "
                "           'expected_cost_bps', payload::jsonb -> 'expected_cost_bps', "
                "           'reason_codes', payload::jsonb -> 'reason_codes', "
                "           'blocked_reason_codes', payload::jsonb -> 'blocked_reason_codes', "
                "           'budget_cut_reason_codes', payload::jsonb -> 'budget_cut_reason_codes', "
                "           'strategy_sleeve_intents', ("
                "             select jsonb_agg(sleeve_compact) from ("
                "               select jsonb_strip_nulls(jsonb_build_object("
                "                 'family', sleeve -> 'family', "
                "                 'strategy_family', sleeve -> 'strategy_family', "
                "                 'strategy_sleeve_id', sleeve -> 'strategy_sleeve_id', "
                "                 'sleeve_id', sleeve -> 'sleeve_id', "
                "                 'route_action', sleeve -> 'route_action', "
                "                 'approved_for_execution', sleeve -> 'approved_for_execution', "
                "                 'permission_mode', sleeve -> 'permission_mode', "
                "                 'effective_scale', sleeve -> 'effective_scale', "
                "                 'position_intent', sleeve -> 'position_intent', "
                "                 'target_notional', sleeve -> 'target_notional', "
                "                 'target_exposure_notional', sleeve -> 'target_exposure_notional', "
                "                 'delta_notional', sleeve -> 'delta_notional', "
                "                 'net_delta_notional', sleeve -> 'net_delta_notional', "
                "                 'reason_codes', sleeve -> 'reason_codes', "
                "                 'blocked_reason_codes', sleeve -> 'blocked_reason_codes', "
                "                 'metrics', jsonb_strip_nulls(jsonb_build_object("
                "                   'expected_edge_bps', sleeve #> '{metrics,expected_edge_bps}', "
                "                   'expected_signal_edge_bps', "
                "                       sleeve #> '{metrics,expected_signal_edge_bps}', "
                "                   'expected_cost_bps', sleeve #> '{metrics,expected_cost_bps}', "
                "                   'expected_net_edge_bps', sleeve #> '{metrics,expected_net_edge_bps}'"
                "                 ))"
                "               )) as sleeve_compact "
                "               from jsonb_array_elements("
                "                 case "
                "                   when jsonb_typeof(payload::jsonb -> 'strategy_sleeve_intents') = 'array' "
                "                     then payload::jsonb -> 'strategy_sleeve_intents' "
                "                   when jsonb_typeof(payload::jsonb -> 'sleeve_intents') = 'array' "
                "                     then payload::jsonb -> 'sleeve_intents' "
                "                   when jsonb_typeof(payload::jsonb -> 'sleeve_decisions') = 'array' "
                "                     then payload::jsonb -> 'sleeve_decisions' "
                "                   else '[]'::jsonb "
                "                 end"
                "               ) with ordinality as sleeve_source(sleeve, ord) "
                "               where ord <= 8"
                "             ) compact_sleeves"
                "           )"
                "         )) as payload "
                "  from portfolio_allocation_decisions "
                "  where symbol = :symbol and primary_family = 'directional' "
                "  order by created_at desc limit 24"
                "), command_refs as ("
                "  select order_id, "
                "         max(case when command_payload #>> '{intent,limit_price}' ~ '^-?[0-9]+(\\.[0-9]+)?$' "
                "                  then (command_payload #>> '{intent,limit_price}')::numeric end) "
                "             as command_intent_limit_price, "
                "         max(case when command_payload #>> '{intent,reference_price}' ~ '^-?[0-9]+(\\.[0-9]+)?$' "
                "                  then (command_payload #>> '{intent,reference_price}')::numeric end) "
                "             as command_intent_reference_price "
                "  from execution_commands where command_type = 'submit' group by order_id"
                "), lot_event_summary as ("
                "  select fill_id, count(*) as lot_event_count, "
                "         count(*) filter (where event_type = 'open') as lot_open_event_count, "
                "         count(*) filter (where event_type = 'close') as lot_close_event_count, "
                "         sum(realized_pnl_delta) as lot_realized_pnl_delta, "
                "         string_agg(distinct coalesce(event_type, 'null'), ',' "
                "             order by coalesce(event_type, 'null')) as lot_event_types "
                "  from lot_events group by fill_id"
                "), order_summary as ("
                "  select o.decision_id, count(*) as order_count, "
                "         count(*) filter (where o.state in ('CREATED', 'SUBMITTING') "
                "                          and o.venue_order_id is null) as created_or_submitting_no_venue_count, "
                "         count(*) filter (where o.state in ('FAILED', 'REJECTED', 'CANCELED', 'BLOCKED', "
                "                          'EXPIRED', 'DRY_RUN')) as terminal_no_fill_order_count, "
                "         count(*) filter (where o.state = 'BLOCKED') as blocked_order_count, "
                "         string_agg(distinct coalesce(o.state, 'null'), ',' order by coalesce(o.state, 'null')) "
                "             as order_states, "
                "         string_agg(distinct coalesce(o.position_intent, 'null'), ',' "
                "             order by coalesce(o.position_intent, 'null')) as order_position_intents, "
                "         string_agg(distinct coalesce(o.execution_action, 'null'), ',' "
                "             order by coalesce(o.execution_action, 'null')) as order_execution_actions, "
                "         string_agg(distinct coalesce(o.strategy_bundle_id, 'null'), ',' "
                "             order by coalesce(o.strategy_bundle_id, 'null')) as order_strategy_bundle_ids, "
                "         min(o.created_at) as first_order_created_at, max(o.created_at) as last_order_created_at "
                "  from execution_orders o "
                "  join recent_decisions rd on rd.decision_id = o.decision_id "
                "  group by o.decision_id"
                "), fill_enriched as ("
                "  select coalesce(f.decision_id, o.decision_id) as decision_id, f.fill_id, f.order_id, "
                "         f.side, f.fill_qty, f.fill_price, f.fee_amount, f.liquidity_role, "
                "         f.strategy_bundle_id as fill_strategy_bundle_id, f.ingestion_ts, "
                "         o.state as order_state, o.position_intent, o.execution_action, "
                "         o.strategy_bundle_id as order_strategy_bundle_id, o.limit_price, "
                "         case when o.raw_payload #>> '{intent,limit_price}' ~ '^-?[0-9]+(\\.[0-9]+)?$' "
                "              then (o.raw_payload #>> '{intent,limit_price}')::numeric end "
                "             as order_intent_limit_price, "
                "         case when o.raw_payload #>> '{intent,reference_price}' ~ '^-?[0-9]+(\\.[0-9]+)?$' "
                "              then (o.raw_payload #>> '{intent,reference_price}')::numeric end "
                "             as order_intent_reference_price, "
                "         case when o.raw_payload #>> '{order_state,reference_price}' ~ '^-?[0-9]+(\\.[0-9]+)?$' "
                "              then (o.raw_payload #>> '{order_state,reference_price}')::numeric end "
                "             as order_state_reference_price, "
                "         case when o.raw_payload #>> '{order_state,submission_payload,referencePrice}' "
                "                   ~ '^-?[0-9]+(\\.[0-9]+)?$' "
                "              then (o.raw_payload #>> '{order_state,submission_payload,referencePrice}')::numeric end "
                "             as order_state_submission_reference_price, "
                "         cr.command_intent_limit_price, cr.command_intent_reference_price, "
                "         fo.realized_pnl_delta as fill_outcome_realized_pnl_delta, "
                "         fo.fee_delta as fill_outcome_fee_delta, "
                "         pl.lot_id as source_lot_id, pl.status as source_lot_status, "
                "         pl.signed_quantity_open as source_lot_signed_quantity_open, "
                "         pl.exposure_side as source_lot_exposure_side, "
                "         les.lot_event_count, les.lot_open_event_count, les.lot_close_event_count, "
                "         les.lot_realized_pnl_delta, les.lot_event_types, "
                "         case when f.fill_qty > 0 and f.fill_price > 0 "
                "              then abs(f.fee_amount) / (f.fill_qty * f.fill_price) * 10000 end as actual_fee_bps "
                "  from execution_fills f "
                "  left join execution_orders o on o.order_id = f.order_id "
                "  left join command_refs cr on cr.order_id = f.order_id "
                "  left join fill_outcomes fo on fo.fill_id = f.fill_id "
                "  left join position_lots pl on pl.source_fill_id = f.fill_id and pl.symbol = f.symbol "
                "  left join lot_event_summary les on les.fill_id = f.fill_id "
                "  where f.symbol = :symbol "
                "    and coalesce(f.decision_id, o.decision_id) in (select decision_id from recent_decisions)"
                "), fill_with_slippage as ("
                "  select fill_enriched.*, "
                "         coalesce(limit_price, order_intent_limit_price, order_intent_reference_price, "
                "                  order_state_reference_price, order_state_submission_reference_price, "
                "                  command_intent_limit_price, command_intent_reference_price) "
                "             as slippage_reference_price, "
                "         case "
                "             when limit_price is not null then 'execution_orders.limit_price' "
                "             when order_intent_limit_price is not null then 'execution_orders.raw_payload.intent.limit_price' "
                "             when order_intent_reference_price is not null "
                "                 then 'execution_orders.raw_payload.intent.reference_price' "
                "             when order_state_reference_price is not null "
                "                 then 'execution_orders.raw_payload.order_state.reference_price' "
                "             when order_state_submission_reference_price is not null "
                "                 then 'execution_orders.raw_payload.order_state.submission_payload.referencePrice' "
                "             when command_intent_limit_price is not null "
                "                 then 'execution_commands.command_payload.intent.limit_price' "
                "             when command_intent_reference_price is not null "
                "                 then 'execution_commands.command_payload.intent.reference_price' "
                "         end as slippage_reference_source "
                "  from fill_enriched"
                "), fill_final as ("
                "  select fill_with_slippage.*, "
                "         case when slippage_reference_price is not null and slippage_reference_price > 0 "
                "                   and fill_price > 0 and side = 'buy' "
                "              then (fill_price - slippage_reference_price) / slippage_reference_price * 10000 "
                "              when slippage_reference_price is not null and slippage_reference_price > 0 "
                "                   and fill_price > 0 and side = 'sell' "
                "              then (slippage_reference_price - fill_price) / slippage_reference_price * 10000 "
                "         end as reference_fill_slippage_bps "
                "  from fill_with_slippage"
                "), fill_summary as ("
                "  select decision_id, count(distinct fill_id) as fill_count, "
                "         count(distinct order_id) as filled_order_count, "
                "         count(fill_outcome_realized_pnl_delta) as fill_outcome_count, "
                "         min(ingestion_ts) as first_fill_ts, max(ingestion_ts) as latest_fill_ts, "
                "         sum(abs(fill_qty * fill_price)) as turnover_usdt, "
                "         sum(abs(fee_amount)) as fee_usdt, "
                "         sum(fill_outcome_realized_pnl_delta) as realized_pnl_usdt, "
                "         sum(fill_outcome_fee_delta) as fill_outcome_fee_delta_usdt, "
                "         count(actual_fee_bps) as actual_fee_bps_sample_count, "
                "         avg(actual_fee_bps) as actual_fee_bps_mean, "
                "         count(reference_fill_slippage_bps) as realized_slippage_sample_count, "
                "         avg(reference_fill_slippage_bps) as realized_slippage_bps_mean, "
                "         count(slippage_reference_price) as slippage_reference_sample_count, "
                "         count(source_lot_id) as source_lot_count, "
                "         count(source_lot_id) filter (where source_lot_status = 'OPEN') as open_source_lot_count, "
                "         count(source_lot_id) filter (where source_lot_status = 'CLOSED') as closed_source_lot_count, "
                "         coalesce(sum(abs(source_lot_signed_quantity_open)) "
                "             filter (where source_lot_status = 'OPEN'), 0) as open_source_lot_qty, "
                "         coalesce(sum(lot_event_count), 0) as lot_event_count, "
                "         coalesce(sum(lot_open_event_count), 0) as lot_open_event_count, "
                "         coalesce(sum(lot_close_event_count), 0) as lot_close_event_count, "
                "         sum(lot_realized_pnl_delta) as lot_realized_pnl_usdt, "
                "         string_agg(distinct coalesce(side, 'null'), ',' order by coalesce(side, 'null')) "
                "             as fill_sides, "
                "         string_agg(distinct coalesce(liquidity_role, 'unknown'), ',' "
                "             order by coalesce(liquidity_role, 'unknown')) as liquidity_roles, "
                "         string_agg(distinct coalesce(position_intent, 'null'), ',' "
                "             order by coalesce(position_intent, 'null')) as fill_position_intents, "
                "         string_agg(distinct coalesce(order_state, 'null'), ',' "
                "             order by coalesce(order_state, 'null')) as filled_order_states, "
                "         string_agg(distinct coalesce(fill_strategy_bundle_id, order_strategy_bundle_id, 'null'), ',' "
                "             order by coalesce(fill_strategy_bundle_id, order_strategy_bundle_id, 'null')) "
                "             as fill_strategy_bundle_ids, "
                "         string_agg(distinct coalesce(source_lot_status, 'null'), ',' "
                "             order by coalesce(source_lot_status, 'null')) as source_lot_statuses, "
                "         string_agg(distinct coalesce(source_lot_exposure_side, 'null'), ',' "
                "             order by coalesce(source_lot_exposure_side, 'null')) as source_lot_exposure_sides, "
                "         string_agg(distinct coalesce(lot_event_types, 'null'), ',' "
                "             order by coalesce(lot_event_types, 'null')) as lot_event_types "
                "  from fill_final group by decision_id"
                "), latest_fill as ("
                "  select * from ("
                "    select fill_final.*, "
                "           row_number() over (partition by decision_id order by ingestion_ts desc, fill_id desc) as rn "
                "    from fill_final"
                "  ) ranked where rn = 1"
                ") "
                "select rd.allocation_id, rd.decision_id, rd.symbol, rd.created_at, rd.route_action, "
                "       rd.primary_family, rd.portfolio_requested_notional, rd.portfolio_approved_notional, "
                "       rd.portfolio_budget_cut_notional, rd.expected_edge_bps, rd.expected_cost_bps, rd.payload, "
                "       coalesce(os.order_count, 0) as order_count, "
                "       coalesce(os.created_or_submitting_no_venue_count, 0) as created_or_submitting_no_venue_count, "
                "       coalesce(os.terminal_no_fill_order_count, 0) as terminal_no_fill_order_count, "
                "       coalesce(os.blocked_order_count, 0) as blocked_order_count, "
                "       os.order_states, os.order_position_intents, os.order_execution_actions, "
                "       os.order_strategy_bundle_ids, os.first_order_created_at, os.last_order_created_at, "
                "       coalesce(fs.fill_count, 0) as fill_count, "
                "       coalesce(fs.filled_order_count, 0) as filled_order_count, "
                "       coalesce(fs.fill_outcome_count, 0) as fill_outcome_count, "
                "       fs.first_fill_ts, fs.latest_fill_ts, fs.turnover_usdt, fs.fee_usdt, fs.realized_pnl_usdt, "
                "       fs.fill_outcome_fee_delta_usdt, coalesce(fs.actual_fee_bps_sample_count, 0) "
                "           as actual_fee_bps_sample_count, "
                "       fs.actual_fee_bps_mean, coalesce(fs.realized_slippage_sample_count, 0) "
                "           as realized_slippage_sample_count, "
                "       fs.realized_slippage_bps_mean, coalesce(fs.slippage_reference_sample_count, 0) "
                "           as slippage_reference_sample_count, "
                "       coalesce(fs.source_lot_count, 0) as source_lot_count, "
                "       coalesce(fs.open_source_lot_count, 0) as open_source_lot_count, "
                "       coalesce(fs.closed_source_lot_count, 0) as closed_source_lot_count, "
                "       fs.open_source_lot_qty, coalesce(fs.lot_event_count, 0) as lot_event_count, "
                "       coalesce(fs.lot_open_event_count, 0) as lot_open_event_count, "
                "       coalesce(fs.lot_close_event_count, 0) as lot_close_event_count, "
                "       fs.lot_realized_pnl_usdt, fs.source_lot_statuses, fs.source_lot_exposure_sides, "
                "       fs.lot_event_types, "
                "       fs.fill_sides, fs.liquidity_roles, fs.fill_position_intents, "
                "       fs.filled_order_states, fs.fill_strategy_bundle_ids, "
                "       lf.fill_id as latest_fill_id, lf.side as latest_fill_side, "
                "       lf.fill_qty as latest_fill_qty, lf.fill_price as latest_fill_price, "
                "       lf.fee_amount as latest_fill_fee_amount, lf.ingestion_ts as latest_fill_ingestion_ts, "
                "       lf.reference_fill_slippage_bps as latest_fill_slippage_bps, "
                "       lf.slippage_reference_source as latest_fill_slippage_reference_source, "
                "       lf.fill_outcome_realized_pnl_delta as latest_fill_realized_pnl_delta, "
                "       lf.source_lot_status as latest_fill_source_lot_status, "
                "       lf.source_lot_signed_quantity_open as latest_fill_source_lot_open_qty, "
                "       lf.source_lot_exposure_side as latest_fill_source_lot_exposure_side, "
                "       lf.lot_event_types as latest_fill_lot_event_types, "
                "       lf.lot_open_event_count as latest_fill_lot_open_event_count, "
                "       lf.lot_close_event_count as latest_fill_lot_close_event_count, "
                "       lf.lot_realized_pnl_delta as latest_fill_lot_realized_pnl_delta "
                "from recent_decisions rd "
                "left join order_summary os on os.decision_id = rd.decision_id "
                "left join fill_summary fs on fs.decision_id = rd.decision_id "
                "left join latest_fill lf on lf.decision_id = rd.decision_id "
                "order by rd.created_at desc"
            ), {"symbol": symbol}).mappings().all()
        ],
    }

print(json.dumps({
    "ok": True,
    "portfolio_allocation_decisions": int(decisions),
    "execution_fills": int(fills),
    "latest_decision": dict(latest) if latest else None,
    "latest_decision_audit": dict(latest_audit) if latest_audit else None,
    "latest_decision_counts": decision_counts,
    "latest_executable_directional_decision": (
        dict(latest_executable_directional) if latest_executable_directional else None
    ),
    "latest_executable_directional_decision_audit": (
        dict(latest_executable_directional_audit) if latest_executable_directional_audit else None
    ),
    "latest_executable_directional_decision_counts": latest_executable_directional_counts,
    "runtime_config": runtime_config,
    "slippage_cost_calibration": slippage_cost_calibration,
    "directional_episode_attribution": directional_episode_attribution,
    "target_convergence_guard": target_convergence_guard,
    "claimed_submit_stuck_submission": claimed_submit_stuck_submission,
    "directional_impulse_chase_guard": directional_impulse_chase_guard,
    "created_no_command_directional_order": created_no_command_directional_order,
    "okx_hedge_scale_in_intent": okx_hedge_scale_in_intent,
    "execution_order_payload_status_residual": execution_order_payload_status_residual,
}, default=str, sort_keys=True))
"""

RDP_MICROSTRUCTURE_PROBE = r"""
import json
import os
from sqlalchemy import create_engine, text

url = os.environ.get("RDP_DATABASE_URL") or os.environ.get("AATS_ACTIVE_PARAMETER_DB_URL")
if not url:
    print(json.dumps({"ok": False, "reason": "rdp_database_url_not_available_in_container_env"}, sort_keys=True))
    raise SystemExit(0)

symbol = os.environ.get("AATS_EXECUTION_SCIENCE_SYMBOL", "BTC-USDT-SWAP")
try:
    recent_silver_limit = int(os.environ.get("AATS_EXECUTION_SCIENCE_RECENT_SILVER_BARS", "672"))
except ValueError:
    recent_silver_limit = 672
recent_silver_limit = max(192, min(recent_silver_limit, 1344))
engine = create_engine(url)

def table_exists(conn, name):
    return bool(conn.execute(text("select to_regclass(:name)"), {"name": name}).scalar())

def table_stats(conn, schema, table):
    name = f"{schema}.{table}"
    exists = table_exists(conn, name)
    stats = {"exists": exists}
    if not exists:
        return stats
    row = conn.execute(
        text(
            f"select count(*) as n, max(ts) as max_ts, min(ts) as min_ts, "
            f"count(*) filter (where ts >= now() - (:recent_minutes * interval '1 minute')) "
            f"as recent_count "
            f"from {name} where symbol=:symbol"
        ),
        {"symbol": symbol, "recent_minutes": 5},
    ).mappings().first()
    stats.update(
        {
            "count": int((row or {}).get("n") or 0),
            "max_ts": (row or {}).get("max_ts"),
            "min_ts": (row or {}).get("min_ts"),
            "recent_window_minutes": 5,
            "recent_count": int((row or {}).get("recent_count") or 0),
        }
    )
    return stats

def latest_silver_orderbook(conn):
    if not table_exists(conn, "silver.market_orderbook_metrics_15m"):
        return None
    row = conn.execute(
        text(
            "select ts, bbo_samples_n, books5_samples_n, spread_bps_mean, "
            "spread_bps_max, spread_bps_min, mid_price_last, quality_flags "
            "from silver.market_orderbook_metrics_15m "
            "where symbol=:symbol order by ts desc limit 1"
        ),
        {"symbol": symbol},
    ).mappings().first()
    return dict(row) if row else None

def latest_silver_trade_flow(conn):
    if not table_exists(conn, "silver.market_trade_flow_15m"):
        return None
    row = conn.execute(
        text(
            "select ts, total_volume_ccy, trade_count, taker_buy_ratio, "
            "trade_flow_imbalance, vwap, mid_price_ref, vwap_minus_mid_bps, "
            "quality_flags "
            "from silver.market_trade_flow_15m "
            "where symbol=:symbol order by ts desc limit 1"
        ),
        {"symbol": symbol},
    ).mappings().first()
    return dict(row) if row else None

def recent_silver_orderbook(conn):
    if not table_exists(conn, "silver.market_orderbook_metrics_15m"):
        return []
    rows = conn.execute(
        text(
            "select ts, bbo_samples_n, books5_samples_n, spread_bps_mean, "
            "spread_bps_max, spread_bps_min, mid_price_last, quality_flags "
            "from silver.market_orderbook_metrics_15m "
            "where symbol=:symbol order by ts desc limit :limit"
        ),
        {"symbol": symbol, "limit": recent_silver_limit},
    ).mappings().all()
    return [dict(row) for row in rows]

def recent_silver_trade_flow(conn):
    if not table_exists(conn, "silver.market_trade_flow_15m"):
        return []
    rows = conn.execute(
        text(
            "select ts, total_volume_ccy, trade_count, taker_buy_ratio, "
            "trade_flow_imbalance, vwap, mid_price_ref, vwap_minus_mid_bps, "
            "quality_flags "
            "from silver.market_trade_flow_15m "
            "where symbol=:symbol order by ts desc limit :limit"
        ),
        {"symbol": symbol, "limit": recent_silver_limit},
    ).mappings().all()
    return [dict(row) for row in rows]

def payload_sequence(conn):
    if not table_exists(conn, "bronze.market_orderbook_payloads"):
        return {"exists": False}
    rows = conn.execute(
        text(
            "with recent as ("
            "  select collector_sequence_scope, ingest_run_id::text as ingest_run_id, "
            "         coalesce(channel, '') as channel, collector_sequence "
            "  from bronze.market_orderbook_payloads "
            "  where symbol=:symbol and ts >= now() - (:window_minutes * interval '1 minute')"
            "), agg as ("
            "  select collector_sequence_scope, ingest_run_id, channel, "
            "         count(*) n, min(collector_sequence) min_seq, "
            "         max(collector_sequence) max_seq, count(distinct collector_sequence) distinct_n "
            "  from recent group by collector_sequence_scope, ingest_run_id, channel"
            ") "
            "select collector_sequence_scope, left(ingest_run_id, 8) as ingest_run_id_prefix, "
            "       channel, n, min_seq, max_seq, distinct_n, "
            "       (max_seq - min_seq + 1 - distinct_n) as sequence_gap_count "
            "from agg order by collector_sequence_scope, ingest_run_id_prefix, channel"
        ),
        {"symbol": symbol, "window_minutes": 30},
    ).mappings().all()
    capture_rows = conn.execute(
        text(
            "select capture_status, count(*) n, max(ts) max_ts "
            "from bronze.market_orderbook_payloads "
            "where symbol=:symbol and ts >= now() - (:window_minutes * interval '1 minute') "
            "group by capture_status order by capture_status"
        ),
        {"symbol": symbol, "window_minutes": 30},
    ).mappings().all()
    return {
        "exists": True,
        "window_minutes": 30,
        "scopes": [dict(row) for row in rows],
        "capture_status_counts": [dict(row) for row in capture_rows],
    }

def latest_orderbook_payloads(conn):
    if not table_exists(conn, "bronze.market_orderbook_payloads"):
        return {"exists": False, "rows": []}
    rows = conn.execute(
        text(
            "select distinct on (coalesce(channel, '')) "
            "       coalesce(channel, '') as channel, storage_table, snapshot_table, "
            "       ts, source_ts, received_at, collector_sequence, collector_sequence_scope, "
            "       left(ingest_run_id::text, 8) as ingest_run_id_prefix, "
            "       row_checksum is not null as row_checksum_present, checksum_version, "
            "       capture_status, payload_hash is not null as payload_hash_present, "
            "       payload_schema_version, payload_kind, "
            "       exchange_sequence_id is not null as exchange_sequence_id_present, "
            "       previous_payload_hash is not null as previous_payload_hash_present "
            "from bronze.market_orderbook_payloads "
            "where symbol=:symbol "
            "order by coalesce(channel, ''), ts desc"
        ),
        {"symbol": symbol},
    ).mappings().all()
    return {"exists": True, "rows": [dict(row) for row in rows]}

def microstructure_workflow(conn):
    if not table_exists(conn, "governance.rdp_task_queue"):
        return {"exists": False}
    rows = conn.execute(
        text(
            "select workflow, status, count(*) n, "
            "       max(coalesce(finished_at, started_at, requested_at, created_at)) max_seen "
            "from governance.rdp_task_queue "
            "where workflow='microstructure_silver_15m' "
            "group by workflow, status order by status"
        )
    ).mappings().all()
    active = conn.execute(
        text(
            "select count(*) from governance.rdp_task_queue "
            "where workflow='microstructure_silver_15m' and status in ('pending', 'running')"
        )
    ).scalar()
    latest = conn.execute(
        text(
            "select task_id, workflow, status, exit_code, requested_at, started_at, finished_at, "
            "       coalesce(finished_at, started_at, requested_at, created_at) as max_seen "
            "from governance.rdp_task_queue "
            "where workflow='microstructure_silver_15m' "
            "order by coalesce(finished_at, started_at, requested_at, created_at) desc limit 1"
        )
    ).mappings().first()
    return {
        "exists": True,
        "active_count": int(active or 0),
        "status_counts": [dict(row) for row in rows],
        "latest_task": dict(latest) if latest else None,
    }

with engine.connect() as conn:
    print(
        json.dumps(
            {
                "ok": True,
                "symbol": symbol,
                "tables": {
                    "bronze.market_trades": table_stats(conn, "bronze", "market_trades"),
                    "bronze.market_orderbook_bbo": table_stats(conn, "bronze", "market_orderbook_bbo"),
                    "bronze.market_orderbook_books5": table_stats(conn, "bronze", "market_orderbook_books5"),
                    "bronze.market_orderbook_payloads": table_stats(conn, "bronze", "market_orderbook_payloads"),
                    "silver.market_liquidation_metrics_15m": table_stats(conn, "silver", "market_liquidation_metrics_15m"),
                    "silver.market_oi_funding_metrics_15m": table_stats(conn, "silver", "market_oi_funding_metrics_15m"),
                    "silver.market_orderbook_metrics_15m": table_stats(conn, "silver", "market_orderbook_metrics_15m"),
                    "silver.market_trade_flow_15m": table_stats(conn, "silver", "market_trade_flow_15m"),
                    "silver.market_volume_profile_15m": table_stats(conn, "silver", "market_volume_profile_15m"),
                },
                "latest_silver_orderbook": latest_silver_orderbook(conn),
                "latest_silver_trade_flow": latest_silver_trade_flow(conn),
                "recent_silver_limit": recent_silver_limit,
                "recent_silver_orderbook": recent_silver_orderbook(conn),
                "recent_silver_trade_flow": recent_silver_trade_flow(conn),
                "payload_sequence": payload_sequence(conn),
                "latest_orderbook_payloads": latest_orderbook_payloads(conn),
                "workflow": microstructure_workflow(conn),
            },
            default=str,
            sort_keys=True,
        )
    )
"""


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def seconds_between(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds()))


def redact_secret_text(value: str | None) -> str:
    """Redact common secret-bearing text while preserving diagnostics shape."""

    if not value:
        return ""
    text = value
    for pattern in SECRET_PATTERNS:
        if pattern.pattern.startswith("(?i)://"):
            text = pattern.sub("://<redacted-credentials>@", text)
        elif "api[_-]?key" in pattern.pattern:
            text = pattern.sub(lambda m: f"{m.group(1)}=<redacted>", text)
        else:
            text = pattern.sub(lambda m: f"{m.group(1)}<redacted-url>", text)
    return text


def truncate_text(value: Any, *, limit: int = 240) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def decimal_text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def decimal_is_positive(value: Any) -> bool:
    try:
        return float(value or 0) > 0
    except (TypeError, ValueError):
        return False


def decimal_is_zero(value: Any) -> bool:
    parsed = decimal_value(value)
    return parsed is not None and parsed == 0


def decimal_value(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def decimal_plain_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if value == value.to_integral_value():
        return format(value.quantize(Decimal("1")), "f")
    return format(value.normalize(), "f")


def decimal_subtract_text(left: Any, right: Any) -> str | None:
    left_value = decimal_value(left)
    right_value = decimal_value(right)
    if left_value is None or right_value is None:
        return None
    return decimal_plain_text(left_value - right_value)


def decimal_add_text(left: Any, right: Any) -> str | None:
    left_value = decimal_value(left)
    right_value = decimal_value(right)
    if left_value is None or right_value is None:
        return None
    return decimal_plain_text(left_value + right_value)


def decimal_bps_change_text(new: Any, old: Any) -> str | None:
    new_value = decimal_value(new)
    old_value = decimal_value(old)
    if new_value is None or old_value is None or old_value == 0:
        return None
    return decimal_plain_text((new_value - old_value) / old_value * Decimal("10000"))


def decimal_gte(left: Any, right: Any) -> bool | None:
    left_value = decimal_value(left)
    right_value = decimal_value(right)
    if left_value is None or right_value is None:
        return None
    return left_value >= right_value


def first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _allocation_payload_metric_candidate(row: dict[str, Any], field: str) -> tuple[Any, str | None]:
    payload = as_dict(row.get("payload"))
    value = payload.get(field)
    if value is not None:
        return value, f"portfolio_allocation_decisions.payload.{field}"

    candidate_keys = (
        ("expected_edge_bps", "expected_signal_edge_bps")
        if field == "expected_edge_bps"
        else ("expected_cost_bps",)
    )
    candidates = (
        payload.get("strategy_sleeve_intents")
        or payload.get("sleeve_intents")
        or payload.get("sleeve_decisions")
        or []
    )
    primary_family = row.get("primary_family")
    fallback: tuple[Any, str | None] = (None, None)
    for item in as_list(candidates):
        item_dict = as_dict(item)
        if not item_dict:
            continue
        family = item_dict.get("family") or item_dict.get("strategy_family")
        metrics = as_dict(item_dict.get("metrics"))
        for key in candidate_keys:
            metric = metrics.get(key)
            if metric is None:
                continue
            source = f"portfolio_allocation_decisions.payload.sleeve_intents[].metrics.{key}"
            if primary_family is None or family == primary_family:
                return metric, source
            if fallback[0] is None:
                fallback = (metric, source)
    return fallback


def allocation_expected_metric_value(row: dict[str, Any], field: str) -> Any:
    value = row.get(field)
    if value is not None:
        return value
    payload_value, _source = _allocation_payload_metric_candidate(row, field)
    return payload_value


def allocation_expected_metric_source(row: dict[str, Any], field: str) -> str | None:
    source = row.get(f"{field}_source")
    if source:
        return str(source)
    if row.get(field) is not None:
        return f"portfolio_allocation_decisions.{field}"
    _payload_value, payload_source = _allocation_payload_metric_candidate(row, field)
    if payload_source:
        return payload_source
    return None


def compact_unique(values: list[Any], *, limit: int = 16) -> list[str]:
    seen: set[str] = set()
    compacted: list[str] = []
    for value in values:
        text = truncate_text(value, limit=128)
        if not text or text in seen:
            continue
        seen.add(text)
        compacted.append(text)
        if len(compacted) >= limit:
            break
    return compacted


def compact_count_distribution(values: list[Any], *, limit: int = 12) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for value in values:
        text = truncate_text(value, limit=128) or "unknown"
        counts[text] = counts.get(text, 0) + 1
    return [
        {"value": key, "count": count}
        for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[
            :limit
        ]
    ]


def compact_distribution_top_value(distribution: Any) -> str | None:
    first = next((as_dict(item) for item in as_list(distribution) if as_dict(item)), {})
    value = first.get("value")
    return str(value) if value is not None else None


def split_aggregate_values(value: Any, *, limit: int = 16) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        values = str(value).split(",")
    return compact_unique([str(item).strip() for item in values], limit=limit)


def terminal_no_fill_reason_from_states(states: list[str]) -> str:
    normalized = {state.upper() for state in states}
    if "BLOCKED" in normalized:
        return "terminal_order_blocked_before_fill"
    if normalized & {"REJECTED", "FAILED"}:
        return "terminal_order_failed_or_rejected_before_fill"
    if "CANCELED" in normalized:
        return "terminal_order_canceled_before_fill"
    if "EXPIRED" in normalized:
        return "terminal_order_expired_before_fill"
    if "DRY_RUN" in normalized:
        return "terminal_order_dry_run_no_fill_expected"
    return "terminal_order_surface_without_fill"


def collect_reason_codes(payload: dict[str, Any], *, limit: int = 24) -> list[str]:
    """Collect stable reason-code strings without returning raw payload bodies."""

    collected: list[Any] = []

    def visit(value: Any, depth: int = 0) -> None:
        if len(collected) >= limit or depth > 4:
            return
        if isinstance(value, dict):
            for key, nested in value.items():
                if len(collected) >= limit:
                    break
                if str(key).endswith("reason_codes") or str(key) in {
                    "blocked_reason_codes",
                    "budget_cut_reason_codes",
                    "reason_code",
                }:
                    collected.extend(as_list(nested))
                elif isinstance(nested, (dict, list)):
                    visit(nested, depth + 1)
        elif isinstance(value, list):
            for item in value[:8]:
                if len(collected) >= limit:
                    break
                visit(item, depth + 1)

    visit(payload)
    return compact_unique(collected, limit=limit)


def summarize_sleeve_intents(payload: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = (
        payload.get("strategy_sleeve_intents")
        or payload.get("sleeve_intents")
        or payload.get("sleeve_decisions")
        or []
    )
    summaries: list[dict[str, Any]] = []
    for item in as_list(candidates)[:8]:
        item_dict = as_dict(item)
        if not item_dict:
            continue
        summaries.append(
            {
                "family": item_dict.get("family") or item_dict.get("strategy_family"),
                "strategy_sleeve_id": item_dict.get("strategy_sleeve_id") or item_dict.get("sleeve_id"),
                "route_action": item_dict.get("route_action"),
                "approved_for_execution": item_dict.get("approved_for_execution"),
                "permission_mode": item_dict.get("permission_mode"),
                "effective_scale": decimal_text(item_dict.get("effective_scale")),
                "position_intent": item_dict.get("position_intent"),
                "target_notional": decimal_text(
                    item_dict.get("target_notional") or item_dict.get("target_exposure_notional")
                ),
                "delta_notional": decimal_text(item_dict.get("delta_notional") or item_dict.get("net_delta_notional")),
                "reason_codes": compact_unique(
                    as_list(item_dict.get("reason_codes")) + as_list(item_dict.get("blocked_reason_codes")),
                    limit=6,
                ),
            }
        )
    return summaries


def collect_sleeve_reason_codes(sleeve_summaries: list[dict[str, Any]]) -> list[str]:
    collected: list[Any] = []
    for sleeve in sleeve_summaries:
        collected.extend(as_list(sleeve.get("reason_codes")))
    return compact_unique(collected, limit=24)


def classify_final_blockers(
    *,
    route_action: Any,
    requested_notional: Any,
    execution_chain: dict[str, Any],
    reason_codes: list[str],
) -> list[str]:
    final_blockers: list[str] = []
    if "candidate_execution_incompatible" in reason_codes:
        final_blockers.append("candidate_execution_incompatible")
    if any("candidate_inactive" in code for code in reason_codes):
        final_blockers.append("strategy_candidate_inactive")
    if any("signal_below_entry_threshold" in code for code in reason_codes):
        final_blockers.append("strategy_signal_below_entry_threshold")
    if any("hold_only" in code for code in reason_codes):
        final_blockers.append("strategy_hold_only")
    if "composed_as_advisory_only" in reason_codes:
        final_blockers.append("composed_as_advisory_only")
    if route_action == "advisory_only" and not decimal_is_positive(requested_notional):
        final_blockers.append("allocator_zero_notional_advisory")
    if not execution_chain.get("execution_plan_ref_present"):
        final_blockers.append("no_execution_plan_emitted")
    return compact_unique(final_blockers, limit=12)


def classify_contributing_factors(reason_codes: list[str]) -> list[str]:
    return compact_unique(
        [code for code in reason_codes if code in SOFT_CONTRIBUTING_REASON_CODES],
        limit=12,
    )


def nested_control_trace(item: dict[str, Any]) -> dict[str, Any]:
    direct = as_dict(item.get("control_trace"))
    if direct:
        return direct
    return as_dict(as_dict(item.get("metrics")).get("auto_control_trace"))


def summarize_permission_trace(item: dict[str, Any]) -> dict[str, Any]:
    trace = nested_control_trace(item)
    permission = as_dict(trace.get("permission"))
    return {
        "approved_for_execution": first_present(
            permission.get("approved_for_execution"),
            item.get("approved_for_execution"),
        ),
        "candidate_enabled": permission.get("candidate_enabled"),
        "candidate_execution_compatible": first_present(
            permission.get("candidate_execution_compatible"),
            item.get("execution_compatible"),
        ),
        "execution_prerequisites_supported": first_present(
            permission.get("execution_prerequisites_supported"),
            item.get("execution_prerequisites_supported"),
        ),
        "configured_auto_execution_enabled": permission.get("configured_auto_execution_enabled"),
        "permission_mode": first_present(permission.get("permission_mode"), item.get("permission_mode")),
        "runtime_supported": permission.get("runtime_supported"),
        "state_runtime_supported": permission.get("state_runtime_supported"),
        "reason_codes": compact_unique(as_list(permission.get("reason_codes")), limit=8),
        "human_summary": truncate_text(
            permission.get("human_summary") or item.get("control_summary"),
            limit=180,
        ),
    }


def summarize_composition_trace(item: dict[str, Any]) -> dict[str, Any]:
    composition = as_dict(nested_control_trace(item).get("composition"))
    return {
        "approved_for_execution": first_present(
            composition.get("approved_for_execution"),
            item.get("approved_for_execution"),
        ),
        "route_action": first_present(composition.get("route_action"), item.get("route_action")),
        "execution_behavior": first_present(composition.get("execution_behavior"), item.get("execution_behavior")),
        "execution_control_mode": first_present(
            composition.get("execution_control_mode"),
            item.get("execution_control_mode"),
        ),
        "requested_delta_position_qty": decimal_text(
            first_present(
                composition.get("requested_delta_position_qty"),
                item.get("requested_delta_position_qty"),
            ),
        ),
        "composed_delta_position_qty": decimal_text(composition.get("composed_delta_position_qty")),
        "budget_zero_suppressed": composition.get("budget_zero_suppressed"),
        "reason_codes": compact_unique(as_list(composition.get("reason_codes")), limit=8),
    }


def boolean_evidence(label: str, value: Any) -> str | None:
    if value is True:
        return f"{label}=true"
    if value is False:
        return f"{label}=false"
    return None


def summarize_permission_root_cause(
    *,
    permission: dict[str, Any],
    execution: dict[str, Any],
    composition: dict[str, Any],
    candidate_reason_codes: list[str],
) -> dict[str, Any]:
    permission_reasons = compact_unique(as_list(permission.get("reason_codes")), limit=8)
    composition_reasons = compact_unique(as_list(composition.get("reason_codes")), limit=8)
    all_reasons = compact_unique(
        candidate_reason_codes + permission_reasons + composition_reasons,
        limit=16,
    )

    blocking_evidence: list[str] = []
    permission_mode = first_present(permission.get("permission_mode"), execution.get("permission_mode"))
    if permission_mode == "unsupported":
        blocking_evidence.append("permission_mode=unsupported")
    for label, value in (
        ("approved_for_execution", first_present(permission.get("approved_for_execution"), execution.get("approved_for_execution"))),
        ("candidate_execution_compatible", permission.get("candidate_execution_compatible")),
        (
            "execution_prerequisites_supported",
            first_present(
                permission.get("execution_prerequisites_supported"),
                execution.get("execution_prerequisites_supported"),
            ),
        ),
        ("execution_compatible", execution.get("execution_compatible")),
    ):
        evidence = boolean_evidence(label, value)
        if evidence and evidence.endswith("=false"):
            blocking_evidence.append(evidence)
    blocking_evidence.extend(f"reason_code={code}" for code in permission_reasons)

    positive_context: list[str] = []
    for label, value in (
        ("candidate_enabled", permission.get("candidate_enabled")),
        ("configured_auto_execution_enabled", permission.get("configured_auto_execution_enabled")),
        ("runtime_supported", permission.get("runtime_supported")),
        ("state_runtime_supported", permission.get("state_runtime_supported")),
    ):
        evidence = boolean_evidence(label, value)
        if evidence and evidence.endswith("=true"):
            positive_context.append(evidence)

    composition_effect: list[str] = []
    for label in ("route_action", "execution_behavior", "execution_control_mode"):
        value = composition.get(label)
        if value:
            composition_effect.append(f"{label}={value}")
    composition_effect.extend(f"reason_code={code}" for code in composition_reasons)

    upstream_reason_codes = compact_unique(
        [
            code
            for code in all_reasons
            if code not in permission_reasons
            and code not in composition_reasons
            and (
                "candidate_inactive" in code
                or "signal_below_entry_threshold" in code
                or "family_candidate_inactive" in code
            )
        ],
        limit=8,
    )

    if "candidate_execution_incompatible" in permission_reasons:
        primary = "candidate_execution_incompatible"
        classification = "permission_denied_by_candidate_execution_compatibility"
    elif first_present(
        permission.get("execution_prerequisites_supported"),
        execution.get("execution_prerequisites_supported"),
    ) is False:
        primary = "execution_prerequisites_unsupported"
        classification = "permission_denied_by_execution_prerequisites"
    elif permission_mode == "unsupported":
        primary = "permission_mode_unsupported"
        classification = "permission_mode_unsupported"
    elif composition.get("execution_control_mode") == "permission_denied":
        primary = "composition_permission_denied"
        classification = "composition_denied_execution"
    else:
        primary = None
        classification = "insufficient_evidence"

    summary = None
    if primary:
        summary = (
            "候选已启用且运行时支持，但执行兼容性或执行前置条件未满足；"
            "因此权限模式为 unsupported，组合层只能输出 advisory_only。"
        )

    return {
        "primary": primary,
        "classification": classification,
        "blocking_evidence": compact_unique(blocking_evidence, limit=12),
        "upstream_reason_codes": upstream_reason_codes,
        "positive_context": compact_unique(positive_context, limit=8),
        "composition_effect": compact_unique(composition_effect, limit=8),
        "summary": summary,
    }


def summarize_budget_trace(item: dict[str, Any]) -> dict[str, Any]:
    budget = as_dict(nested_control_trace(item).get("budget"))
    return {
        "base_scale": decimal_text(budget.get("base_scale")),
        "effective_scale": decimal_text(first_present(budget.get("effective_scale"), item.get("effective_scale"))),
        "requested_delta_position_qty": decimal_text(
            first_present(
                budget.get("requested_delta_position_qty"),
                item.get("requested_delta_position_qty"),
            ),
        ),
        "scaled_delta_position_qty": decimal_text(budget.get("scaled_delta_position_qty")),
        "budget_zero_suppressed": first_present(
            budget.get("budget_zero_suppressed"),
            item.get("budget_zero_suppressed"),
        ),
        "reason_codes": compact_unique(as_list(budget.get("reason_codes")), limit=8),
    }


def summarize_book_runtime_states(item: dict[str, Any]) -> list[dict[str, Any]]:
    metrics = as_dict(item.get("metrics"))
    summaries: list[dict[str, Any]] = []
    for book in as_list(metrics.get("book_runtime_states"))[:2]:
        book_dict = as_dict(book)
        if not book_dict:
            continue
        threshold = as_dict(book_dict.get("threshold_snapshot"))
        score = first_present(book_dict.get("score_adjusted"), book_dict.get("score"))
        entry_threshold = first_present(threshold.get("effective_entry_threshold"), threshold.get("entry_threshold"))
        signal_edge = book_dict.get("expected_signal_edge_bps")
        expected_cost = book_dict.get("expected_cost_bps")
        summaries.append(
            {
                "leg": book_dict.get("leg"),
                "state": book_dict.get("state"),
                "book_action": book_dict.get("book_action"),
                "book_state": book_dict.get("book_state"),
                "score": decimal_text(book_dict.get("score")),
                "score_adjusted": decimal_text(book_dict.get("score_adjusted")),
                "effective_entry_threshold": decimal_text(entry_threshold),
                "expected_signal_edge_bps": decimal_text(book_dict.get("expected_signal_edge_bps")),
                "expected_cost_bps": decimal_text(book_dict.get("expected_cost_bps")),
                "expected_net_edge_bps": decimal_text(book_dict.get("expected_net_edge_bps")),
                "activation_gap": {
                    "score_gap_to_entry_threshold": decimal_subtract_text(entry_threshold, score),
                    "score_minus_entry_threshold": decimal_subtract_text(score, entry_threshold),
                    "score_meets_entry_threshold": decimal_gte(score, entry_threshold),
                    "signal_edge_minus_cost_bps": decimal_subtract_text(signal_edge, expected_cost),
                    "signal_edge_gap_to_cost_bps": decimal_subtract_text(expected_cost, signal_edge),
                    "signal_edge_covers_cost": decimal_gte(signal_edge, expected_cost),
                },
                "health_state": book_dict.get("health_state"),
                "execution_health_state": book_dict.get("execution_health_state"),
                "transition_valid": book_dict.get("transition_valid"),
                "reason_codes": compact_unique(as_list(book_dict.get("reason_codes")), limit=8),
            },
        )
    return summaries


def is_candidate_drilldown_relevant(
    *,
    item: dict[str, Any],
    family: Any,
    primary_family: Any,
    reason_codes: list[str],
) -> bool:
    if family == primary_family:
        return True
    return any(
        "candidate_inactive" in code or "signal_below_entry_threshold" in code
        for code in reason_codes
    )


def summarize_candidate_execution_drilldown(
    payload: dict[str, Any],
    *,
    primary_family: Any,
) -> list[dict[str, Any]]:
    candidates = (
        payload.get("strategy_sleeve_intents")
        or payload.get("sleeve_intents")
        or payload.get("sleeve_decisions")
        or []
    )
    summaries: list[dict[str, Any]] = []
    for item in as_list(candidates):
        item_dict = as_dict(item)
        if not item_dict:
            continue
        family = item_dict.get("family") or item_dict.get("strategy_family")
        reason_codes = compact_unique(
            as_list(item_dict.get("reason_codes"))
            + as_list(item_dict.get("blocked_reason_codes"))
            + as_list(item_dict.get("control_reason_codes"))
            + as_list(item_dict.get("blocking_reasons")),
            limit=12,
        )
        if not is_candidate_drilldown_relevant(
            item=item_dict,
            family=family,
            primary_family=primary_family,
            reason_codes=reason_codes,
        ):
            continue
        execution_summary = {
            "approved_for_execution": item_dict.get("approved_for_execution"),
            "execution_compatible": item_dict.get("execution_compatible"),
            "execution_prerequisites_supported": item_dict.get("execution_prerequisites_supported"),
            "execution_behavior": item_dict.get("execution_behavior"),
            "execution_control_mode": item_dict.get("execution_control_mode"),
            "execution_mode": item_dict.get("execution_mode"),
            "permission_mode": item_dict.get("permission_mode"),
            "automatic_enabled": item_dict.get("automatic_enabled"),
            "selectable": item_dict.get("selectable"),
            "legs_count": len(as_list(item_dict.get("legs"))),
        }
        permission_summary = summarize_permission_trace(item_dict)
        composition_summary = summarize_composition_trace(item_dict)
        summaries.append(
            {
                "family": family,
                "strategy_sleeve_id": item_dict.get("strategy_sleeve_id") or item_dict.get("sleeve_id"),
                "state": item_dict.get("state"),
                "state_phase": item_dict.get("state_phase"),
                "family_action": item_dict.get("family_action"),
                "route_action": item_dict.get("route_action"),
                "target_notional": decimal_text(
                    item_dict.get("target_notional") or item_dict.get("target_exposure_notional"),
                ),
                "reason_codes": reason_codes,
                "execution": execution_summary,
                "permission": permission_summary,
                "permission_root_cause": summarize_permission_root_cause(
                    permission=permission_summary,
                    execution=execution_summary,
                    composition=composition_summary,
                    candidate_reason_codes=reason_codes,
                ),
                "composition": composition_summary,
                "budget": summarize_budget_trace(item_dict),
                "book_runtime_states": summarize_book_runtime_states(item_dict),
            },
        )
        if len(summaries) >= 4:
            break
    return summaries


def summarize_primary_family_candidate_truth(
    *,
    latest_decision: dict[str, Any],
    candidate_drilldown: list[dict[str, Any]],
    no_trade_classification: dict[str, Any],
    execution_legs_count: int,
) -> dict[str, Any]:
    primary_family = latest_decision.get("primary_family")
    primary_candidate = next(
        (
            item
            for item in candidate_drilldown
            if item.get("family") == primary_family
        ),
        {},
    )
    if not primary_candidate:
        return {
            "status": "missing_primary_family_candidate_truth",
            "smallest_missing_field": "no_trade_attribution.candidate_execution_drilldown.primary_family",
            "primary_family": primary_family,
            "global_primary_blocker": no_trade_classification.get("primary_blocker"),
            "global_primary_blocker_applies_to_candidate": None,
            "global_primary_blocker_scope": "unknown_missing_primary_candidate",
            "order_expected_from_primary_candidate": None,
            "no_order_root_cause": "missing_primary_family_candidate_drilldown",
            "evidence": [],
        }

    permission = as_dict(primary_candidate.get("permission"))
    execution = as_dict(primary_candidate.get("execution"))
    composition = as_dict(primary_candidate.get("composition"))
    budget = as_dict(primary_candidate.get("budget"))
    permission_root_cause = as_dict(primary_candidate.get("permission_root_cause"))
    route_action = first_present(composition.get("route_action"), primary_candidate.get("route_action"))
    execution_behavior = first_present(
        composition.get("execution_behavior"),
        execution.get("execution_behavior"),
    )
    requested_delta_position_qty = first_present(
        composition.get("requested_delta_position_qty"),
        budget.get("requested_delta_position_qty"),
    )
    composed_delta_position_qty = first_present(
        composition.get("composed_delta_position_qty"),
        budget.get("scaled_delta_position_qty"),
        requested_delta_position_qty,
    )
    zero_delta = (
        decimal_is_zero(requested_delta_position_qty)
        and decimal_is_zero(composed_delta_position_qty)
    )
    root_primary = permission_root_cause.get("primary")
    global_primary_blocker = no_trade_classification.get("primary_blocker")
    candidate_execution_compatible = first_present(
        permission.get("candidate_execution_compatible"),
        execution.get("execution_compatible"),
    )
    candidate_reason_codes = compact_unique(
        as_list(primary_candidate.get("reason_codes"))
        + as_list(permission.get("reason_codes"))
        + as_list(composition.get("reason_codes"))
        + as_list(budget.get("reason_codes"))
        + ([root_primary] if root_primary else []),
        limit=16,
    )
    global_primary_blocker_applies = bool(
        global_primary_blocker and global_primary_blocker in candidate_reason_codes
    )
    advisory_suppressed_after_approval = (
        route_action == "advisory_only"
        and execution_behavior == "suppressed_after_approval"
        and decimal_is_zero(composed_delta_position_qty)
        and execution_legs_count == 0
    )
    status = "primary_family_candidate_truth_present"
    smallest_missing_field = None
    no_order_root_cause = None
    order_expected_from_primary_candidate: bool | None = None

    if root_primary:
        status = "primary_family_candidate_execution_blocked"
        no_order_root_cause = root_primary
        order_expected_from_primary_candidate = False
    elif route_action == "hold_current" and execution_behavior == "hold_current" and zero_delta:
        status = "verified_primary_candidate_hold_current_zero_delta_no_order_expected"
        no_order_root_cause = "primary_candidate_hold_current_zero_delta"
        order_expected_from_primary_candidate = False
    elif route_action == "advisory_only" and zero_delta:
        status = "verified_primary_candidate_advisory_zero_delta_no_order_expected"
        no_order_root_cause = "primary_candidate_advisory_zero_delta"
        order_expected_from_primary_candidate = False
    elif advisory_suppressed_after_approval:
        status = "verified_primary_candidate_advisory_suppressed_after_approval_no_order_expected"
        no_order_root_cause = "primary_candidate_advisory_only_suppressed_after_approval"
        order_expected_from_primary_candidate = False
    elif route_action not in {None, "advisory_only", "hold_current"} or execution_legs_count > 0:
        status = "primary_family_candidate_order_expected_or_already_surfaced"
        order_expected_from_primary_candidate = True
    elif zero_delta:
        status = "primary_family_candidate_zero_delta_no_order_expected"
        no_order_root_cause = "primary_candidate_zero_delta"
        order_expected_from_primary_candidate = False
    else:
        smallest_missing_field = "primary_candidate_order_expectation_classification"

    return {
        "status": status,
        "smallest_missing_field": smallest_missing_field,
        "primary_family": primary_family,
        "strategy_sleeve_id": primary_candidate.get("strategy_sleeve_id"),
        "candidate_state": primary_candidate.get("state"),
        "candidate_state_phase": primary_candidate.get("state_phase"),
        "candidate_route_action": route_action,
        "candidate_execution_behavior": execution_behavior,
        "candidate_execution_compatible": candidate_execution_compatible,
        "candidate_approved_for_execution": first_present(
            permission.get("approved_for_execution"),
            execution.get("approved_for_execution"),
        ),
        "candidate_selectable": execution.get("selectable"),
        "candidate_permission_mode": first_present(
            permission.get("permission_mode"),
            execution.get("permission_mode"),
        ),
        "candidate_execution_prerequisites_supported": first_present(
            permission.get("execution_prerequisites_supported"),
            execution.get("execution_prerequisites_supported"),
        ),
        "requested_delta_position_qty": decimal_text(requested_delta_position_qty),
        "composed_delta_position_qty": decimal_text(composed_delta_position_qty),
        "target_notional": decimal_text(primary_candidate.get("target_notional")),
        "effective_scale": decimal_text(budget.get("effective_scale")),
        "order_expected_from_primary_candidate": order_expected_from_primary_candidate,
        "no_order_root_cause": no_order_root_cause,
        "global_primary_blocker": global_primary_blocker,
        "global_primary_blocker_applies_to_candidate": global_primary_blocker_applies,
        "global_primary_blocker_scope": (
            "primary_family_candidate"
            if global_primary_blocker_applies
            else "other_candidate_or_portfolio_level"
        ),
        "reason_codes": candidate_reason_codes,
        "permission_root_cause": permission_root_cause,
        "evidence": compact_unique(
            [
                f"primary_family={primary_family}" if primary_family else None,
                f"candidate_route_action={route_action}" if route_action else None,
                f"candidate_execution_behavior={execution_behavior}" if execution_behavior else None,
                (
                    f"candidate_execution_compatible={str(candidate_execution_compatible).lower()}"
                    if candidate_execution_compatible is not None
                    else None
                ),
                f"requested_delta_position_qty={decimal_text(requested_delta_position_qty)}",
                f"composed_delta_position_qty={decimal_text(composed_delta_position_qty)}",
                f"execution_legs_count={execution_legs_count}",
                f"global_primary_blocker={global_primary_blocker}" if global_primary_blocker else None,
            ],
            limit=12,
        ),
    }


def classify_no_trade(
    *,
    latest_decision: dict[str, Any],
    execution_chain: dict[str, Any],
    reason_codes: list[str],
) -> dict[str, Any]:
    route_action = latest_decision.get("route_action")
    approved_notional = latest_decision.get("portfolio_approved_notional")
    requested_notional = latest_decision.get("portfolio_requested_notional")
    has_execution_activity = any(
        bool(execution_chain.get(key))
        for key in (
            "execution_plan_ref_present",
            "execution_plan_ref_count",
            "order_intent_ref_count",
            "order_state_ref_count",
            "fill_event_ref_count",
            "db_order_count",
            "db_order_state_count",
            "db_fill_count",
            "legacy_fill_event_count",
        )
    )

    if decimal_is_positive(approved_notional) or has_execution_activity:
        return {
            "classification": "execution_activity_or_positive_allocation_present",
            "primary_blocker": None,
            "final_blockers": [],
            "contributing_factors": classify_contributing_factors(reason_codes),
            "blocker_chain": [],
            "is_current_no_trade": False,
        }
    final_blockers = classify_final_blockers(
        route_action=route_action,
        requested_notional=requested_notional,
        execution_chain=execution_chain,
        reason_codes=reason_codes,
    )
    contributing_factors = classify_contributing_factors(reason_codes)
    primary = final_blockers[0] if final_blockers else (contributing_factors[0] if contributing_factors else None)
    if primary is None:
        primary = "unclassified_no_trade"
    blocker_chain: list[dict[str, Any]] = [
        {
            "stage": "allocation",
            "route_action": route_action,
            "requested_notional_positive": decimal_is_positive(requested_notional),
            "approved_notional_positive": decimal_is_positive(approved_notional),
        }
    ]
    if contributing_factors:
        blocker_chain.append({"stage": "soft_contributing_factors", "reason_codes": contributing_factors})
    if final_blockers:
        blocker_chain.append({"stage": "final_no_trade_blockers", "reason_codes": final_blockers})
    return {
        "classification": "no_order_fill_expected_for_latest_decision",
        "primary_blocker": primary,
        "final_blockers": final_blockers,
        "contributing_factors": contributing_factors,
        "blocker_chain": blocker_chain,
        "is_current_no_trade": True,
    }


def summarize_execution_truth_chain(
    *,
    latest_decision: dict[str, Any],
    execution_chain: dict[str, Any],
    execution_legs_count: int,
    candidate_drilldown: list[dict[str, Any]],
) -> dict[str, Any]:
    route_action = latest_decision.get("route_action")
    primary_family = latest_decision.get("primary_family")
    primary_candidate = next(
        (
            item
            for item in candidate_drilldown
            if item.get("family") == primary_family
        ),
        candidate_drilldown[0] if candidate_drilldown else {},
    )
    composition = as_dict(primary_candidate.get("composition"))
    budget = as_dict(primary_candidate.get("budget"))
    execution = as_dict(primary_candidate.get("execution"))
    execution_behavior = first_present(
        composition.get("execution_behavior"),
        execution.get("execution_behavior"),
    )
    requested_delta_position_qty = first_present(
        composition.get("requested_delta_position_qty"),
        budget.get("requested_delta_position_qty"),
    )
    composed_delta_position_qty = first_present(
        composition.get("composed_delta_position_qty"),
        budget.get("scaled_delta_position_qty"),
        requested_delta_position_qty,
    )
    db_order_count = int(execution_chain.get("db_order_count") or 0)
    db_order_state_count = int(execution_chain.get("db_order_state_count") or 0)
    db_fill_count = int(execution_chain.get("db_fill_count") or 0)
    db_fill_via_order_count = int(execution_chain.get("db_fill_via_order_count") or 0)
    legacy_fill_event_count = int(execution_chain.get("legacy_fill_event_count") or 0)
    legacy_fill_event_via_order_count = int(execution_chain.get("legacy_fill_event_via_order_count") or 0)
    db_execution_order_created_or_submitting_count = int(
        execution_chain.get("db_execution_order_created_or_submitting_count") or 0
    )
    db_execution_order_submitted_or_later_count = int(
        execution_chain.get("db_execution_order_submitted_or_later_count") or 0
    )
    db_execution_order_terminal_no_fill_count = int(
        execution_chain.get("db_execution_order_terminal_no_fill_count") or 0
    )
    db_order_state_created_or_submitting_count = int(
        execution_chain.get("db_order_state_created_or_submitting_count") or 0
    )
    db_order_state_submitted_or_later_count = int(
        execution_chain.get("db_order_state_submitted_or_later_count") or 0
    )
    db_order_state_terminal_no_fill_count = int(
        execution_chain.get("db_order_state_terminal_no_fill_count") or 0
    )
    db_execution_command_count = int(execution_chain.get("db_execution_command_count") or 0)
    db_execution_submit_command_count = int(execution_chain.get("db_execution_submit_command_count") or 0)
    db_execution_submit_command_pending_count = int(
        execution_chain.get("db_execution_submit_command_pending_count") or 0
    )
    db_execution_submit_command_claimed_count = int(
        execution_chain.get("db_execution_submit_command_claimed_count") or 0
    )
    db_execution_submit_command_sent_count = int(
        execution_chain.get("db_execution_submit_command_sent_count") or 0
    )
    db_execution_submit_command_failed_count = int(
        execution_chain.get("db_execution_submit_command_failed_count") or 0
    )
    execution_command_flow_enabled = execution_chain.get("execution_command_flow_enabled")
    if execution_command_flow_enabled is not None:
        execution_command_flow_enabled = bool(execution_command_flow_enabled)
    execution_plan_ref_count = int(execution_chain.get("execution_plan_ref_count") or 0)
    order_intent_ref_count = int(execution_chain.get("order_intent_ref_count") or 0)
    order_state_ref_count = int(execution_chain.get("order_state_ref_count") or 0)
    fill_event_ref_count = int(execution_chain.get("fill_event_ref_count") or 0)

    has_order_surface = any(
        (
            db_order_count,
            db_order_state_count,
            order_intent_ref_count,
            order_state_ref_count,
        ),
    )
    has_fill_surface = any(
        (
            db_fill_count,
            db_fill_via_order_count,
            legacy_fill_event_count,
            legacy_fill_event_via_order_count,
            fill_event_ref_count,
        )
    )
    has_submit_surface = any(
        (
            db_execution_command_count,
            db_execution_submit_command_count,
            db_execution_order_submitted_or_later_count,
            db_order_state_submitted_or_later_count,
            has_fill_surface,
        )
    )
    order_intent_without_order_surface = (
        order_intent_ref_count > 0
        and db_order_count == 0
        and db_order_state_count == 0
        and not has_submit_surface
        and not has_fill_surface
    )
    order_created_but_not_submitted = (
        has_order_surface
        and not has_submit_surface
        and any(
            (
                db_execution_order_created_or_submitting_count,
                db_order_state_created_or_submitting_count,
                db_order_count,
                db_order_state_count,
            )
        )
    )
    order_surface_terminal_no_fill = (
        has_order_surface
        and not has_fill_surface
        and db_execution_order_created_or_submitting_count == 0
        and db_order_state_created_or_submitting_count == 0
        and (
            db_order_count == 0
            or db_execution_order_terminal_no_fill_count >= db_order_count
        )
        and (
            db_order_state_count == 0
            or db_order_state_terminal_no_fill_count >= db_order_state_count
        )
        and any((db_order_count, db_order_state_count))
    )
    terminal_execution_order_states = split_aggregate_values(
        execution_chain.get("db_execution_order_terminal_no_fill_states"),
        limit=8,
    )
    terminal_order_state_statuses = split_aggregate_values(
        execution_chain.get("db_order_state_terminal_no_fill_statuses"),
        limit=8,
    )
    terminal_no_fill_states = compact_unique(
        terminal_execution_order_states + terminal_order_state_statuses,
        limit=8,
    )
    terminal_no_fill_explanation = None
    if order_surface_terminal_no_fill:
        terminal_no_fill_explanation = {
            "classification": "terminal_order_surface_without_fill",
            "reason": terminal_no_fill_reason_from_states(terminal_no_fill_states),
            "terminal_states": terminal_no_fill_states,
            "execution_order_terminal_states": terminal_execution_order_states,
            "order_state_terminal_statuses": terminal_order_state_statuses,
            "terminal_source_systems": split_aggregate_values(
                execution_chain.get("db_execution_order_terminal_no_fill_source_systems"),
                limit=8,
            ),
            "terminal_execution_styles": split_aggregate_values(
                execution_chain.get("db_execution_order_terminal_no_fill_execution_styles"),
                limit=8,
            ),
            "terminal_position_intents": compact_unique(
                split_aggregate_values(
                    execution_chain.get("db_execution_order_terminal_no_fill_position_intents"),
                    limit=8,
                )
                + split_aggregate_values(
                    execution_chain.get("db_order_state_terminal_no_fill_position_intents"),
                    limit=8,
                ),
                limit=8,
            ),
            "execution_order_count": db_order_count,
            "order_state_count": db_order_state_count,
            "terminal_execution_order_count": db_execution_order_terminal_no_fill_count,
            "terminal_order_state_count": db_order_state_terminal_no_fill_count,
            "created_or_submitting_execution_order_count": db_execution_order_created_or_submitting_count,
            "created_or_submitting_order_state_count": db_order_state_created_or_submitting_count,
            "fill_surface_present": has_fill_surface,
            "operator_summary": "all_visible_order_surfaces_are_terminal_no_fill",
        }
    zero_delta = (
        decimal_is_zero(requested_delta_position_qty)
        and decimal_is_zero(composed_delta_position_qty)
    )

    order_expected = route_action not in {None, "advisory_only", "hold_current"} or execution_legs_count > 0
    fill_expected = order_expected
    lifecycle_expected = order_expected
    status = "needs_manual_review"
    missing_fields: list[str] = []
    evidence: list[str] = compact_unique(
        [
            f"route_action={route_action}" if route_action else None,
            f"execution_behavior={execution_behavior}" if execution_behavior else None,
            f"execution_legs_count={execution_legs_count}",
            f"requested_delta_position_qty={decimal_text(requested_delta_position_qty)}",
            f"composed_delta_position_qty={decimal_text(composed_delta_position_qty)}",
            f"execution_plan_ref_count={execution_plan_ref_count}",
            f"order_intent_ref_count={order_intent_ref_count}",
            f"db_order_count={db_order_count}",
            f"db_order_state_count={db_order_state_count}",
            f"db_fill_count={db_fill_count}",
            f"db_fill_via_order_count={db_fill_via_order_count}",
            f"db_execution_command_count={db_execution_command_count}",
            f"db_order_submitted_or_later_count={db_execution_order_submitted_or_later_count}",
            f"db_order_terminal_no_fill_count={db_execution_order_terminal_no_fill_count}",
            f"db_submit_pending_count={db_execution_submit_command_pending_count}",
            f"db_submit_claimed_count={db_execution_submit_command_claimed_count}",
            f"db_submit_sent_count={db_execution_submit_command_sent_count}",
            f"db_submit_failed_count={db_execution_submit_command_failed_count}",
            (
                f"execution_command_flow_enabled={str(execution_command_flow_enabled).lower()}"
                if execution_command_flow_enabled is not None
                else None
            ),
        ],
        limit=16,
    )
    submission_gap_root_cause = None
    submit_command_pending_or_claimed = (
        db_execution_submit_command_pending_count
        + db_execution_submit_command_claimed_count
    ) > 0
    submit_command_sent_without_terminal_order = (
        db_execution_submit_command_sent_count > 0
        and any(
            (
                db_execution_order_created_or_submitting_count,
                db_order_state_created_or_submitting_count,
            )
        )
    )
    submit_command_failed_without_terminal_order = (
        db_execution_submit_command_failed_count > 0
        and any(
            (
                db_execution_order_created_or_submitting_count,
                db_order_state_created_or_submitting_count,
            )
        )
    )

    if route_action == "hold_current" and execution_behavior == "hold_current" and zero_delta and execution_legs_count == 0:
        order_expected = False
        fill_expected = False
        lifecycle_expected = False
        if has_order_surface or has_fill_surface:
            status = "unexpected_order_or_fill_surface_for_hold_current"
            if has_order_surface:
                missing_fields.append("explain_unexpected_order_surface")
            if has_fill_surface:
                missing_fields.append("explain_unexpected_fill_surface")
        else:
            status = "verified_no_order_expected_hold_current_zero_delta"
    elif order_expected:
        if execution_plan_ref_count == 0:
            missing_fields.append("execution_plan_refs")
        if order_intent_ref_count == 0 and db_order_count == 0:
            missing_fields.append("order_intent_refs_or_execution_orders")
        if (
            not missing_fields
            and order_intent_without_order_surface
        ):
            fill_expected = False
            submission_gap_root_cause = "execution_order_missing_for_order_intent"
            missing_fields.append("execution_order_or_order_state_from_order_intent_refs")
        elif (
            not missing_fields
            and order_created_but_not_submitted
            and not has_fill_surface
        ):
            fill_expected = False
            if execution_command_flow_enabled is False:
                submission_gap_root_cause = "execution_command_flow_disabled_direct_submit_interruption_window"
                missing_fields.append("enable_execution_command_flow_or_recover_created_order")
            else:
                submission_gap_root_cause = "execution_command_missing_for_created_order"
                missing_fields.append("execution_command_or_submitted_order_state")
        elif (
            not missing_fields
            and not has_fill_surface
            and any(
                (
                    submit_command_pending_or_claimed,
                    submit_command_sent_without_terminal_order,
                    submit_command_failed_without_terminal_order,
                )
            )
            and any(
                (
                    db_execution_order_created_or_submitting_count,
                    db_order_state_created_or_submitting_count,
                )
            )
        ):
            fill_expected = False
            lifecycle_expected = False
            if db_execution_submit_command_claimed_count > 0:
                submission_gap_root_cause = "execution_submit_command_claimed_without_terminal_order_ack"
            elif db_execution_submit_command_pending_count > 0:
                submission_gap_root_cause = "execution_submit_command_pending_without_terminal_order_ack"
            elif submit_command_sent_without_terminal_order:
                submission_gap_root_cause = "execution_submit_command_sent_without_terminal_order_ack"
            else:
                submission_gap_root_cause = "execution_submit_command_failed_without_terminal_order_ack"
            missing_fields.append("execution_command_terminal_ack_or_exchange_order_id")
        elif not missing_fields and order_surface_terminal_no_fill:
            fill_expected = False
            lifecycle_expected = False
            status = "verified_terminal_order_no_fill_expected"
        elif fill_expected and fill_event_ref_count == 0 and not has_fill_surface:
            missing_fields.append("fill_event_refs_or_execution_fills")
        if missing_fields:
            status = (
                "expected_order_submission_missing"
                if missing_fields
                in (
                    ["execution_command_or_submitted_order_state"],
                    ["enable_execution_command_flow_or_recover_created_order"],
                    ["execution_order_or_order_state_from_order_intent_refs"],
                    ["execution_command_terminal_ack_or_exchange_order_id"],
                )
                else "expected_execution_surface_missing"
            )
        elif status == "needs_manual_review":
            status = "verified_execution_surface_present"
    elif not has_order_surface and not has_fill_surface:
        status = "verified_no_order_expected"

    if not lifecycle_expected:
        lifecycle_status = "no_position_lifecycle_transition_expected"
    elif not missing_fields and lifecycle_expected:
        lifecycle_status = "position_lifecycle_transition_evidence_present"
    else:
        lifecycle_status = "position_lifecycle_transition_evidence_missing"

    return {
        "status": status,
        "order_expected": order_expected,
        "fill_expected": fill_expected,
        "position_lifecycle_transition_expected": lifecycle_expected,
        "position_lifecycle_status": lifecycle_status,
        "smallest_missing_field": missing_fields[0] if missing_fields else None,
        "missing_fields": missing_fields,
        "submission_gap_root_cause": submission_gap_root_cause,
        "terminal_no_fill_explanation": terminal_no_fill_explanation,
        "evidence": evidence,
    }


def summarize_latest_decision(
    latest: dict[str, Any] | None,
    audit: dict[str, Any] | None,
    decision_counts: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(latest, dict):
        return None
    payload = as_dict(latest.get("payload"))
    expected_edge_bps = allocation_expected_metric_value(latest, "expected_edge_bps")
    expected_cost_bps = allocation_expected_metric_value(latest, "expected_cost_bps")
    audit_payload = as_dict(audit)
    counts = as_dict(decision_counts)
    execution_plan_refs = as_list(audit_payload.get("execution_plan_refs"))
    execution_chain = {
        "execution_plan_ref_present": bool(audit_payload.get("execution_plan_ref")),
        "execution_plan_ref_count": len(execution_plan_refs),
        "order_intent_ref_count": len(as_list(audit_payload.get("order_intent_refs"))),
        "order_state_ref_count": len(as_list(audit_payload.get("order_state_refs"))),
        "fill_event_ref_count": len(as_list(audit_payload.get("fill_event_refs"))),
        "strategy_sleeve_intent_ref_count": len(as_list(audit_payload.get("strategy_sleeve_intent_refs"))),
        "db_order_count": int(counts.get("execution_orders") or 0),
        "execution_command_flow_enabled": counts.get("execution_command_flow_enabled"),
        "execution_command_flow_flag_present": counts.get("execution_command_flow_flag_present"),
        "db_execution_order_created_or_submitting_count": int(
            counts.get("execution_orders_created_or_submitting") or 0
        ),
        "db_execution_order_submitted_or_later_count": int(
            counts.get("execution_orders_submitted_or_later") or 0
        ),
        "db_execution_order_terminal_no_fill_count": int(
            counts.get("execution_orders_terminal_no_fill") or 0
        ),
        "db_execution_order_terminal_no_fill_states": counts.get(
            "execution_orders_terminal_no_fill_states"
        ),
        "db_execution_order_terminal_no_fill_source_systems": counts.get(
            "execution_orders_terminal_no_fill_source_systems"
        ),
        "db_execution_order_terminal_no_fill_execution_styles": counts.get(
            "execution_orders_terminal_no_fill_execution_styles"
        ),
        "db_execution_order_terminal_no_fill_position_intents": counts.get(
            "execution_orders_terminal_no_fill_position_intents"
        ),
        "db_execution_command_count": int(counts.get("execution_commands") or 0),
        "db_execution_submit_command_count": int(counts.get("execution_submit_commands") or 0),
        "db_execution_submit_command_pending_count": int(counts.get("execution_submit_commands_pending") or 0),
        "db_execution_submit_command_claimed_count": int(counts.get("execution_submit_commands_claimed") or 0),
        "db_execution_submit_command_sent_count": int(counts.get("execution_submit_commands_sent") or 0),
        "db_execution_submit_command_failed_count": int(counts.get("execution_submit_commands_failed") or 0),
        "db_order_state_count": int(counts.get("order_states") or 0),
        "db_order_state_created_or_submitting_count": int(
            counts.get("order_states_created_or_submitting") or 0
        ),
        "db_order_state_submitted_or_later_count": int(
            counts.get("order_states_submitted_or_later") or 0
        ),
        "db_order_state_terminal_no_fill_count": int(
            counts.get("order_states_terminal_no_fill") or 0
        ),
        "db_order_state_terminal_no_fill_statuses": counts.get(
            "order_states_terminal_no_fill_statuses"
        ),
        "db_order_state_terminal_no_fill_position_intents": counts.get(
            "order_states_terminal_no_fill_position_intents"
        ),
        "terminal_no_fill_order_state_drilldown": as_list(
            counts.get("terminal_no_fill_order_state_drilldown")
        ),
        "db_fill_count": int(counts.get("execution_fills") or 0),
        "db_fill_via_order_count": int(counts.get("execution_fills_via_orders") or 0),
        "legacy_fill_event_count": int(counts.get("legacy_fill_events") or 0),
        "legacy_fill_event_via_order_count": int(counts.get("legacy_fill_events_via_orders") or 0),
    }
    sleeve_summaries = summarize_sleeve_intents(payload)
    reason_codes = compact_unique(
        collect_reason_codes(payload) + collect_sleeve_reason_codes(sleeve_summaries),
        limit=32,
    )
    candidate_drilldown = summarize_candidate_execution_drilldown(
        payload,
        primary_family=latest.get("primary_family"),
    )
    classification = classify_no_trade(
        latest_decision=latest,
        execution_chain=execution_chain,
        reason_codes=reason_codes,
    )
    execution_legs_count = len(as_list(payload.get("execution_legs")))
    execution_truth_chain = summarize_execution_truth_chain(
        latest_decision=latest,
        execution_chain=execution_chain,
        execution_legs_count=execution_legs_count,
        candidate_drilldown=candidate_drilldown,
    )
    primary_family_candidate_truth = summarize_primary_family_candidate_truth(
        latest_decision=latest,
        candidate_drilldown=candidate_drilldown,
        no_trade_classification=classification,
        execution_legs_count=execution_legs_count,
    )
    no_trade_attribution = {
        **classification,
        "reason_codes": reason_codes,
        "operator_summary": truncate_text(payload.get("operator_summary")),
        "execution_legs_count": execution_legs_count,
        "sleeve_intent_summary": sleeve_summaries,
        "candidate_execution_drilldown": candidate_drilldown,
        "primary_family_candidate_truth": primary_family_candidate_truth,
    }
    return {
        "allocation_id": latest.get("allocation_id"),
        "decision_id": latest.get("decision_id"),
        "symbol": latest.get("symbol"),
        "created_at": latest.get("created_at"),
        "route_action": latest.get("route_action"),
        "primary_family": latest.get("primary_family"),
        "portfolio_requested_notional": decimal_text(latest.get("portfolio_requested_notional")),
        "portfolio_approved_notional": decimal_text(latest.get("portfolio_approved_notional")),
        "portfolio_budget_cut_notional": decimal_text(latest.get("portfolio_budget_cut_notional")),
        "expected_edge_bps": decimal_text(expected_edge_bps),
        "expected_cost_bps": decimal_text(expected_cost_bps),
        "expected_edge_bps_source": allocation_expected_metric_source(latest, "expected_edge_bps"),
        "expected_cost_bps_source": allocation_expected_metric_source(latest, "expected_cost_bps"),
        "audit_refs": {
            "portfolio_allocation_decision_ref": audit_payload.get("portfolio_allocation_decision_ref"),
            "decision_outcome_ref_present": bool(audit_payload.get("decision_outcome_ref")),
            "risk_decision_ref_present": bool(audit_payload.get("risk_decision_ref")),
            "updated_at": audit_payload.get("updated_at"),
        },
        "execution_chain": execution_chain,
        "execution_truth_chain": execution_truth_chain,
        "no_trade_attribution": no_trade_attribution,
    }


def _terminal_state_present(*values: Any) -> bool:
    return any(str(value).upper() in TERMINAL_NO_FILL_STATES for value in values if value)


def summarize_terminal_no_fill_order_state_drilldown(
    *,
    latest: dict[str, Any],
    terminal_surface: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not terminal_surface:
        return None

    execution_chain = as_dict(latest.get("execution_chain"))
    raw_rows = as_list(execution_chain.get("terminal_no_fill_order_state_drilldown"))
    expected_order_count = int_or_zero(
        terminal_surface.get("execution_order_count")
        or terminal_surface.get("terminal_execution_order_count")
        or execution_chain.get("db_order_count")
    )
    expected_order_state_count = int_or_zero(
        terminal_surface.get("order_state_count")
        or terminal_surface.get("terminal_order_state_count")
        or execution_chain.get("db_order_state_count")
    )

    per_order: list[dict[str, Any]] = []
    for raw in raw_rows:
        row = as_dict(raw)
        execution_order_state = row.get("execution_order_state")
        order_state_status = row.get("order_state_status")
        payload_statuses = compact_unique(
            [
                row.get("execution_order_payload_status"),
                row.get("nested_order_state_status"),
                row.get("order_state_payload_status"),
            ],
            limit=6,
        )
        exchange_ack_sources = compact_unique(
            [
                "execution_orders.venue_order_id" if row.get("venue_order_id_present") else None,
                (
                    "execution_orders.raw_payload.venue_order_id"
                    if row.get("raw_payload_venue_order_id_present")
                    else None
                ),
                (
                    "execution_orders.raw_payload.exchange_order_id"
                    if row.get("raw_payload_exchange_order_id_present")
                    else None
                ),
                "order_states.exchange_order_id" if row.get("order_state_exchange_order_id_present") else None,
                (
                    "order_states.payload.exchange_order_id"
                    if row.get("order_state_payload_exchange_order_id_present")
                    else None
                ),
            ],
            limit=6,
        )
        execution_fill_count = int_or_zero(row.get("execution_fill_count"))
        legacy_fill_event_count = int_or_zero(row.get("legacy_fill_event_count"))
        terminal_state_verified = _terminal_state_present(
            execution_order_state,
            order_state_status,
            *payload_statuses,
        )
        fill_absent = execution_fill_count == 0 and legacy_fill_event_count == 0
        command_present = bool(row.get("command_id"))
        order_intent_present = bool(row.get("intent_id"))
        exchange_ack_present = bool(exchange_ack_sources)
        if terminal_state_verified and fill_absent:
            classification = "terminal_no_fill_order_state_link_verified"
        elif not fill_absent:
            classification = "unexpected_fill_surface_for_terminal_no_fill_order"
        else:
            classification = "terminal_no_fill_order_state_link_incomplete"

        per_order.append(
            {
                "decision": {
                    "decision_id": row.get("decision_id") or latest.get("decision_id"),
                    "allocation_id": row.get("allocation_id") or latest.get("allocation_id"),
                    "symbol": row.get("symbol") or latest.get("symbol"),
                },
                "order_intent": {
                    "intent_id": row.get("intent_id"),
                    "present": order_intent_present,
                },
                "execution_command": {
                    "command_id": row.get("command_id"),
                    "command_type": row.get("command_type"),
                    "state": row.get("command_state"),
                    "attempt_count": int_or_zero(row.get("attempt_count")),
                    "has_last_error": bool(row.get("command_has_last_error")),
                    "created_at": row.get("command_created_at"),
                    "updated_at": row.get("command_updated_at"),
                    "present": command_present,
                },
                "execution_order": {
                    "order_id": row.get("order_id"),
                    "client_order_id": row.get("client_order_id"),
                    "state": execution_order_state,
                    "payload_statuses": payload_statuses,
                    "side": row.get("side"),
                    "pos_side": row.get("pos_side"),
                    "position_intent": row.get("position_intent"),
                    "execution_action": row.get("execution_action"),
                    "order_type": row.get("order_type"),
                    "time_in_force": row.get("time_in_force"),
                    "source_system": row.get("source_system"),
                    "execution_style": row.get("execution_style"),
                    "reduce_only": row.get("reduce_only"),
                    "close_only": row.get("close_only"),
                    "created_at": row.get("execution_order_created_at"),
                    "updated_at": row.get("execution_order_updated_at"),
                },
                "order_state": {
                    "status": order_state_status,
                    "payload_statuses": payload_statuses,
                    "created_at": row.get("order_state_created_at"),
                    "submitted_ts": row.get("submitted_ts"),
                    "last_update_ts": row.get("last_update_ts"),
                    "row_version": row.get("order_state_row_version"),
                    "present": bool(row.get("order_state_status")),
                },
                "exchange_ack": {
                    "present": exchange_ack_present,
                    "absent": not exchange_ack_present,
                    "sources": exchange_ack_sources,
                },
                "fill_absence": {
                    "execution_fill_count": execution_fill_count,
                    "legacy_fill_event_count": legacy_fill_event_count,
                    "verified": fill_absent,
                },
                "terminal_status_alignment": {
                    "terminal_state_verified": terminal_state_verified,
                    "states_considered": compact_unique(
                        [execution_order_state, order_state_status] + payload_statuses,
                        limit=8,
                    ),
                },
                "classification": classification,
            }
        )

    row_count = len(per_order)
    command_present_count = sum(1 for row in per_order if row["execution_command"]["present"])
    exchange_ack_absent_count = sum(1 for row in per_order if row["exchange_ack"]["absent"])
    fill_absent_count = sum(1 for row in per_order if row["fill_absence"]["verified"])
    terminal_verified_count = sum(
        1
        for row in per_order
        if row["terminal_status_alignment"]["terminal_state_verified"]
    )
    if row_count == 0:
        status = "missing_terminal_no_fill_order_state_drilldown"
        smallest_missing_field = "execution_chain.terminal_no_fill_order_state_drilldown"
    elif fill_absent_count < row_count:
        status = "inconsistent_terminal_no_fill_drilldown_has_fill_surface"
        smallest_missing_field = "terminal_no_fill_drilldown.fill_absence"
    elif terminal_verified_count < row_count:
        status = "partial_terminal_no_fill_order_state_drilldown"
        smallest_missing_field = "terminal_no_fill_drilldown.terminal_status_alignment"
    elif expected_order_count and row_count < expected_order_count:
        status = "partial_terminal_no_fill_order_state_drilldown"
        smallest_missing_field = "terminal_no_fill_drilldown.expected_execution_order_rows"
    else:
        status = "verified_terminal_no_fill_order_state_drilldown"
        smallest_missing_field = None

    return {
        "status": status,
        "smallest_missing_field": smallest_missing_field,
        "source": "live_db_execution_orders_order_states_execution_commands_fills",
        "coverage": {
            "expected_execution_order_count": expected_order_count,
            "expected_order_state_count": expected_order_state_count,
            "drilldown_order_count": row_count,
            "command_present_count": command_present_count,
            "command_absent_count": row_count - command_present_count,
            "exchange_ack_present_count": row_count - exchange_ack_absent_count,
            "exchange_ack_absent_count": exchange_ack_absent_count,
            "fill_absent_count": fill_absent_count,
            "terminal_state_verified_count": terminal_verified_count,
        },
        "per_order": per_order,
        "interpretation": {
            "not_alpha_or_profitability_evidence": True,
            "raw_payload_exposed": False,
            "exchange_ack_absence_is_diagnostic": True,
            "command_absence_can_be_expected_for_local_blocked_orders": True,
        },
    }


def summarize_directional_executable_episode_truth(db: dict[str, Any]) -> dict[str, Any]:
    latest = as_dict(db.get("latest_executable_directional_decision"))
    if not latest:
        return {
            "ok": False,
            "source": "live_db_latest_executable_directional_decision",
            "status": "missing_latest_executable_directional_decision",
            "smallest_missing_field": "database_truth.latest_executable_directional_decision",
            "latest_executable_decision": None,
            "terminal_no_fill": None,
            "terminal_no_fill_drilldown": None,
            "provenance": {},
        }

    execution_chain = as_dict(latest.get("execution_chain"))
    truth_chain = as_dict(latest.get("execution_truth_chain"))
    terminal_no_fill = as_dict(truth_chain.get("terminal_no_fill_explanation"))
    smallest_missing = truth_chain.get("smallest_missing_field")
    truth_status = truth_chain.get("status")

    terminal_no_fill_verified = (
        truth_status == "verified_terminal_order_no_fill_expected"
        and terminal_no_fill.get("classification") == "terminal_order_surface_without_fill"
        and smallest_missing is None
    )
    if terminal_no_fill_verified:
        status = "verified_executable_terminal_order_no_fill_truth"
        ok = True
        surface_missing = None
    elif truth_status == "verified_execution_surface_present" and smallest_missing is None:
        status = "verified_executable_order_fill_truth_surface"
        ok = True
        surface_missing = None
    elif truth_status:
        status = "missing_executable_directional_truth_chain_evidence"
        ok = False
        surface_missing = smallest_missing or "execution_truth_chain.terminal_state_or_fill_surface"
    else:
        status = "missing_executable_directional_truth_chain"
        ok = False
        surface_missing = "database_truth.latest_executable_directional_decision.execution_truth_chain"

    terminal_surface = None
    if terminal_no_fill:
        terminal_surface = {
            "classification": terminal_no_fill.get("classification"),
            "reason": terminal_no_fill.get("reason"),
            "terminal_states": terminal_no_fill.get("terminal_states"),
            "terminal_source_systems": terminal_no_fill.get("terminal_source_systems"),
            "terminal_execution_styles": terminal_no_fill.get("terminal_execution_styles"),
            "terminal_position_intents": terminal_no_fill.get("terminal_position_intents"),
            "execution_order_count": terminal_no_fill.get("execution_order_count"),
            "order_state_count": terminal_no_fill.get("order_state_count"),
            "terminal_execution_order_count": terminal_no_fill.get("terminal_execution_order_count"),
            "terminal_order_state_count": terminal_no_fill.get("terminal_order_state_count"),
            "operator_summary": terminal_no_fill.get("operator_summary"),
        }
    terminal_no_fill_drilldown = summarize_terminal_no_fill_order_state_drilldown(
        latest=latest,
        terminal_surface=terminal_surface,
    )

    return {
        "ok": ok,
        "source": "live_db_latest_executable_directional_decision",
        "status": status,
        "smallest_missing_field": surface_missing,
        "latest_executable_decision": {
            "decision_id": latest.get("decision_id"),
            "allocation_id": latest.get("allocation_id"),
            "created_at": latest.get("created_at"),
            "symbol": latest.get("symbol"),
            "primary_family": latest.get("primary_family"),
            "route_action": latest.get("route_action"),
            "expected_edge_bps": latest.get("expected_edge_bps"),
            "expected_cost_bps": latest.get("expected_cost_bps"),
            "portfolio_requested_notional": latest.get("portfolio_requested_notional"),
            "portfolio_approved_notional": latest.get("portfolio_approved_notional"),
            "portfolio_budget_cut_notional": latest.get("portfolio_budget_cut_notional"),
            "execution_truth_status": truth_status,
            "order_expected": truth_chain.get("order_expected"),
            "fill_expected": truth_chain.get("fill_expected"),
            "position_lifecycle_status": truth_chain.get("position_lifecycle_status"),
            "submission_gap_root_cause": truth_chain.get("submission_gap_root_cause"),
        },
        "terminal_no_fill": terminal_surface,
        "terminal_no_fill_drilldown": terminal_no_fill_drilldown,
        "provenance": {
            "execution_plan_ref_count": int_or_zero(execution_chain.get("execution_plan_ref_count")),
            "order_intent_ref_count": int_or_zero(execution_chain.get("order_intent_ref_count")),
            "order_state_ref_count": int_or_zero(execution_chain.get("order_state_ref_count")),
            "fill_event_ref_count": int_or_zero(execution_chain.get("fill_event_ref_count")),
            "db_order_count": int_or_zero(execution_chain.get("db_order_count")),
            "db_order_state_count": int_or_zero(execution_chain.get("db_order_state_count")),
            "db_fill_count": int_or_zero(execution_chain.get("db_fill_count")),
            "db_fill_via_order_count": int_or_zero(execution_chain.get("db_fill_via_order_count")),
            "db_execution_command_count": int_or_zero(execution_chain.get("db_execution_command_count")),
            "db_execution_submit_command_count": int_or_zero(
                execution_chain.get("db_execution_submit_command_count")
            ),
            "db_execution_submit_command_pending_count": int_or_zero(
                execution_chain.get("db_execution_submit_command_pending_count")
            ),
            "db_execution_submit_command_claimed_count": int_or_zero(
                execution_chain.get("db_execution_submit_command_claimed_count")
            ),
            "db_execution_submit_command_sent_count": int_or_zero(
                execution_chain.get("db_execution_submit_command_sent_count")
            ),
            "db_execution_submit_command_failed_count": int_or_zero(
                execution_chain.get("db_execution_submit_command_failed_count")
            ),
            "db_execution_order_terminal_no_fill_count": int_or_zero(
                execution_chain.get("db_execution_order_terminal_no_fill_count")
            ),
            "db_order_state_terminal_no_fill_count": int_or_zero(
                execution_chain.get("db_order_state_terminal_no_fill_count")
            ),
            "execution_command_flow_enabled": execution_chain.get("execution_command_flow_enabled"),
            "execution_command_flow_flag_present": execution_chain.get(
                "execution_command_flow_flag_present"
            ),
        },
        "interpretation": {
            "terminal_no_fill_verified": terminal_no_fill_verified,
            "terminal_no_fill_order_state_drilldown_verified": (
                as_dict(terminal_no_fill_drilldown).get("status")
                == "verified_terminal_no_fill_order_state_drilldown"
            ),
            "order_surface_present": int_or_zero(execution_chain.get("db_order_count")) > 0
            or int_or_zero(execution_chain.get("db_order_state_count")) > 0,
            "fill_surface_present": int_or_zero(execution_chain.get("db_fill_count")) > 0
            or int_or_zero(execution_chain.get("db_fill_via_order_count")) > 0
            or int_or_zero(execution_chain.get("fill_event_ref_count")) > 0,
            "not_alpha_or_profitability_evidence": True,
        },
    }


def classify_directional_episode_row(row: dict[str, Any]) -> str:
    fill_count = int_or_zero(row.get("fill_count"))
    order_count = int_or_zero(row.get("order_count"))
    outcome_count = int_or_zero(row.get("fill_outcome_count"))
    terminal_no_fill_count = int_or_zero(row.get("terminal_no_fill_order_count"))
    blocked_order_count = int_or_zero(row.get("blocked_order_count"))
    if fill_count > 0 and outcome_count > 0:
        return "filled_with_realized_pnl_outcome"
    if fill_count > 0:
        return "filled_without_realized_pnl_outcome"
    if blocked_order_count > 0:
        return "blocked_order_without_fill"
    if terminal_no_fill_count > 0:
        return "terminal_order_without_fill"
    if order_count > 0:
        return "active_order_without_fill"
    return "decision_without_order"


NO_ORDER_ROUTE_ACTIONS = {"advisory_only", "hold_current"}
VERIFIED_PRIMARY_CANDIDATE_NO_ORDER_ROOTS = {
    "primary_candidate_hold_current_zero_delta",
    "primary_candidate_advisory_zero_delta",
    "primary_candidate_advisory_only_suppressed_after_approval",
    "primary_candidate_zero_delta",
}


def classify_primary_candidate_no_order_semantics(primary_candidate: dict[str, Any]) -> dict[str, Any]:
    root_cause = primary_candidate.get("no_order_root_cause")
    order_expected = primary_candidate.get("order_expected_from_primary_candidate")
    smallest_missing = primary_candidate.get("smallest_missing_field")

    if (
        order_expected is False
        and smallest_missing is None
        and root_cause in VERIFIED_PRIMARY_CANDIDATE_NO_ORDER_ROOTS
    ):
        return {
            "status": "verified_primary_candidate_no_order_expected_semantics",
            "equivalence_class": "verified_non_executable_no_order_expected",
            "root_cause": root_cause,
            "root_cause_is_material_without_order_or_fill_change": False,
            "requires_order_or_fill_change_for_materiality": True,
            "reason": (
                "The primary candidate is verified as no-order expected; root switches inside this "
                "equivalence class are runtime semantics changes, not order/fill materiality."
            ),
        }

    if order_expected is True:
        return {
            "status": "primary_candidate_order_expected",
            "equivalence_class": "order_expected",
            "root_cause": root_cause,
            "root_cause_is_material_without_order_or_fill_change": True,
            "requires_order_or_fill_change_for_materiality": False,
            "reason": "The primary candidate expects an order, so no-order root equivalence does not apply.",
        }

    if root_cause:
        return {
            "status": "primary_candidate_no_order_root_not_semantically_equivalent",
            "equivalence_class": "root_specific_no_order",
            "root_cause": root_cause,
            "root_cause_is_material_without_order_or_fill_change": True,
            "requires_order_or_fill_change_for_materiality": False,
            "reason": "The root cause is specific and not in the verified no-order equivalence class.",
        }

    return {
        "status": "missing_primary_candidate_no_order_root_semantics",
        "equivalence_class": None,
        "root_cause": root_cause,
        "root_cause_is_material_without_order_or_fill_change": None,
        "requires_order_or_fill_change_for_materiality": None,
        "reason": "The primary candidate no-order root could not be classified from available truth.",
    }


def classify_directional_episode_order_expectation(decision: dict[str, Any]) -> dict[str, Any]:
    order = as_dict(decision.get("order"))
    fill = as_dict(decision.get("fill"))
    route_action = decision.get("route_action")
    order_count = int_or_zero(order.get("count"))
    fill_count = int_or_zero(fill.get("count"))

    if order_count > 0:
        classification = "order_surface_present"
        no_order_expected = False
        order_surface_present = True
        smallest_missing_field = None
        reason = "Execution order surface exists for this directional decision."
    elif fill_count > 0:
        classification = "fill_surface_without_order_surface"
        no_order_expected = False
        order_surface_present = False
        smallest_missing_field = "execution_orders.directional_recent_decision"
        reason = "A fill is present but no execution order surface is linked to this directional decision."
    elif route_action in NO_ORDER_ROUTE_ACTIONS:
        classification = "no_order_expected_by_route_action"
        no_order_expected = True
        order_surface_present = False
        smallest_missing_field = None
        reason = (
            "The allocation route action is advisory_only or hold_current, so no execution order is expected."
        )
    else:
        classification = "order_surface_missing_for_order_expected_decision"
        no_order_expected = False
        order_surface_present = False
        smallest_missing_field = "execution_orders.directional_recent_decision"
        reason = "The directional route action is executable or unknown, but no execution order surface exists."

    return {
        "classification": classification,
        "route_action": route_action,
        "no_order_expected": no_order_expected,
        "order_surface_present": order_surface_present,
        "smallest_missing_field": smallest_missing_field,
        "reason": reason,
    }


def classify_directional_episode_no_order_semantics(decision: dict[str, Any]) -> dict[str, Any]:
    order_expectation = as_dict(decision.get("order_expectation"))
    route_action = order_expectation.get("route_action") or decision.get("route_action")
    no_order_expected = order_expectation.get("no_order_expected")
    order_surface_present = order_expectation.get("order_surface_present")
    smallest_missing = order_expectation.get("smallest_missing_field")

    if no_order_expected is True and smallest_missing is None:
        return {
            "status": "verified_directional_decision_no_order_expected_semantics",
            "equivalence_class": "verified_non_executable_no_order_expected",
            "root_cause": f"decision_route_action_{route_action}_no_order_expected",
            "route_action": route_action,
            "root_cause_is_material_without_order_or_fill_change": False,
            "requires_order_or_fill_change_for_materiality": True,
            "reason": (
                "The recent directional decision is verified as no-order expected; route-action root "
                "switches inside this equivalence class are not order/fill materiality."
            ),
        }

    if order_surface_present is True:
        return {
            "status": "directional_decision_order_surface_present",
            "equivalence_class": "order_surface_present",
            "root_cause": None,
            "route_action": route_action,
            "root_cause_is_material_without_order_or_fill_change": True,
            "requires_order_or_fill_change_for_materiality": False,
            "reason": "Execution order surface exists, so no-order semantic equivalence does not apply.",
        }

    return {
        "status": "directional_decision_no_order_semantics_not_verified",
        "equivalence_class": None,
        "root_cause": order_expectation.get("classification"),
        "route_action": route_action,
        "root_cause_is_material_without_order_or_fill_change": True,
        "requires_order_or_fill_change_for_materiality": False,
        "reason": (
            "The recent directional decision is not verified as no-order expected and has no order "
            "surface, so this is still a material truth-chain gap."
        ),
    }


OPEN_POSITION_INTENTS = {
    "open_long",
    "open_short",
    "scale_in_long",
    "scale_in_short",
}

CLOSE_POSITION_INTENTS = {
    "close_long",
    "close_short",
    "reduce_long",
    "reduce_short",
    "flip_long_to_short",
    "flip_short_to_long",
}


def csv_tokens(value: Any) -> set[str]:
    if value is None:
        return set()
    return {part.strip() for part in str(value).split(",") if part.strip() and part.strip() != "null"}


def classify_directional_episode_pnl_lifecycle(decision: dict[str, Any]) -> dict[str, Any]:
    fill = as_dict(decision.get("fill"))
    pnl_outcome = as_dict(decision.get("pnl_outcome"))
    lifecycle = as_dict(decision.get("position_lifecycle"))
    latest_fill = as_dict(decision.get("latest_fill"))
    fill_count = int_or_zero(fill.get("count"))
    outcome_count = int_or_zero(pnl_outcome.get("fill_outcome_count"))
    open_lot_count = int_or_zero(lifecycle.get("open_source_lot_count"))
    lot_open_event_count = int_or_zero(lifecycle.get("lot_open_event_count"))
    lot_close_event_count = int_or_zero(lifecycle.get("lot_close_event_count"))
    latest_lot_status = latest_fill.get("source_lot_status")
    intent_tokens = csv_tokens(fill.get("position_intents")) | csv_tokens(as_dict(decision.get("order")).get("position_intents"))

    if fill_count <= 0:
        status = "no_fill_pnl_not_applicable"
        smallest_missing_field = None
        explanation = "No fill exists for this directional episode, so realized PnL outcome is not applicable."
    elif outcome_count >= fill_count:
        status = "realized_pnl_outcome_complete"
        smallest_missing_field = None
        explanation = "Every observed fill has a fill_outcomes realized PnL record."
    elif outcome_count > 0:
        status = "partial_fill_outcome_coverage"
        smallest_missing_field = "fill_outcomes.fill_id"
        explanation = "Some fills have realized PnL records and at least one fill is still missing fill_outcomes linkage."
    elif open_lot_count > 0 or latest_lot_status == "OPEN" or (lot_open_event_count > 0 and lot_close_event_count <= 0):
        status = "open_position_not_yet_realized"
        smallest_missing_field = None
        explanation = "Fill opened or still owns an open lot, so realized PnL is expected to remain null until close/reduce."
    elif lot_close_event_count > 0:
        status = "closed_lifecycle_missing_fill_outcome"
        smallest_missing_field = "fill_outcomes.realized_pnl_delta"
        explanation = "Lifecycle evidence shows a close/reduce path, but realized PnL is not linked through fill_outcomes."
    elif intent_tokens.intersection(CLOSE_POSITION_INTENTS):
        status = "close_intent_missing_portfolio_projection"
        smallest_missing_field = "lot_events.fill_id"
        explanation = (
            "Fill intent is close/reduce, but portfolio projection did not record a lot close event; "
            "fill_outcomes cannot be interpreted as a closed lifecycle until projection evidence exists."
        )
    elif intent_tokens.intersection(OPEN_POSITION_INTENTS):
        status = "open_position_lot_evidence_missing"
        smallest_missing_field = "position_lots.source_fill_id"
        explanation = "Intent is an opening directional fill, but open lot evidence is missing from position_lots/lot_events."
    else:
        status = "missing_fill_outcome_lifecycle_link"
        smallest_missing_field = "fill_outcomes.realized_pnl_delta_or_position_lots.source_fill_id"
        explanation = "The episode has a fill but neither realized PnL outcome nor position-lifecycle evidence explains it."

    return {
        "status": status,
        "smallest_missing_field": smallest_missing_field,
        "explanation": explanation,
        "coverage": {
            "fill_count": fill_count,
            "fill_outcome_count": outcome_count,
            "missing_fill_outcome_count": max(fill_count - outcome_count, 0),
        },
        "lifecycle_evidence": {
            "source_lot_count": int_or_zero(lifecycle.get("source_lot_count")),
            "open_source_lot_count": open_lot_count,
            "closed_source_lot_count": int_or_zero(lifecycle.get("closed_source_lot_count")),
            "open_source_lot_qty": lifecycle.get("open_source_lot_qty"),
            "source_lot_statuses": lifecycle.get("source_lot_statuses"),
            "source_lot_exposure_sides": lifecycle.get("source_lot_exposure_sides"),
            "lot_event_count": int_or_zero(lifecycle.get("lot_event_count")),
            "lot_open_event_count": lot_open_event_count,
            "lot_close_event_count": lot_close_event_count,
            "lot_event_types": lifecycle.get("lot_event_types"),
            "lot_realized_pnl_usdt": lifecycle.get("lot_realized_pnl_usdt"),
        },
        "latest_fill_evidence": {
            "fill_id": latest_fill.get("fill_id"),
            "source_lot_status": latest_lot_status,
            "source_lot_open_qty": latest_fill.get("source_lot_open_qty"),
            "source_lot_exposure_side": latest_fill.get("source_lot_exposure_side"),
            "lot_event_types": latest_fill.get("lot_event_types"),
            "lot_open_event_count": int_or_zero(latest_fill.get("lot_open_event_count")),
            "lot_close_event_count": int_or_zero(latest_fill.get("lot_close_event_count")),
            "lot_realized_pnl_delta": latest_fill.get("lot_realized_pnl_delta"),
        },
    }


def _nearest_microstructure_bar_at_or_before(
    rows: list[dict[str, Any]],
    timestamp: Any,
) -> tuple[dict[str, Any], int | None]:
    target = parse_utc_timestamp(str(timestamp)) if timestamp is not None else None
    if target is None:
        return {}, None
    selected: dict[str, Any] = {}
    selected_ts: datetime | None = None
    for row in rows:
        row_ts = parse_utc_timestamp(str(row.get("ts"))) if row.get("ts") is not None else None
        if row_ts is None or row_ts > target:
            continue
        if selected_ts is None or row_ts > selected_ts:
            selected = row
            selected_ts = row_ts
    if selected_ts is None:
        return {}, None
    age_seconds = seconds_between(selected_ts, target)
    if age_seconds is None or age_seconds > MICROSTRUCTURE_BAR_MATCH_MAX_AGE_SECONDS:
        return {}, age_seconds
    return selected, age_seconds


def _nearest_microstructure_bar_at_or_after(
    rows: list[dict[str, Any]],
    timestamp: Any,
) -> tuple[dict[str, Any], int | None]:
    target = parse_utc_timestamp(str(timestamp)) if timestamp is not None else None
    if target is None:
        return {}, None
    selected: dict[str, Any] = {}
    selected_ts: datetime | None = None
    for row in rows:
        row_ts = parse_utc_timestamp(str(row.get("ts"))) if row.get("ts") is not None else None
        if row_ts is None or row_ts < target:
            continue
        if selected_ts is None or row_ts < selected_ts:
            selected = row
            selected_ts = row_ts
    if selected_ts is None:
        return {}, None
    age_seconds = seconds_between(target, selected_ts)
    if age_seconds is None or age_seconds > MICROSTRUCTURE_BAR_MATCH_MAX_AGE_SECONDS:
        return {}, age_seconds
    return selected, age_seconds


def _microstructure_orderbook_context(row: dict[str, Any], age_seconds: int | None) -> dict[str, Any]:
    if not row:
        return {}
    return {
        "bar_ts": row.get("ts"),
        "bar_age_seconds": age_seconds,
        "spread_bps_mean": decimal_text(row.get("spread_bps_mean")),
        "spread_bps_max": decimal_text(row.get("spread_bps_max")),
        "spread_bps_min": decimal_text(row.get("spread_bps_min")),
        "mid_price_last": decimal_text(row.get("mid_price_last")),
        "bbo_samples_n": int_or_zero(row.get("bbo_samples_n")),
        "books5_samples_n": int_or_zero(row.get("books5_samples_n")),
        "quality_flags": as_list(row.get("quality_flags")),
    }


def _microstructure_trade_flow_context(row: dict[str, Any], age_seconds: int | None) -> dict[str, Any]:
    if not row:
        return {}
    return {
        "bar_ts": row.get("ts"),
        "bar_age_seconds": age_seconds,
        "trade_count": int_or_zero(row.get("trade_count")),
        "total_volume_ccy": decimal_text(row.get("total_volume_ccy")),
        "taker_buy_ratio": decimal_text(row.get("taker_buy_ratio")),
        "trade_flow_imbalance": decimal_text(row.get("trade_flow_imbalance")),
        "vwap": decimal_text(row.get("vwap")),
        "mid_price_ref": decimal_text(row.get("mid_price_ref")),
        "vwap_minus_mid_bps": decimal_text(row.get("vwap_minus_mid_bps")),
        "quality_flags": as_list(row.get("quality_flags")),
    }


def _side_adverse_fill_bps(fill_side: Any, fill_price: Any, reference_price: Any) -> str | None:
    side = str(fill_side or "").lower()
    fill = decimal_value(fill_price)
    reference = decimal_value(reference_price)
    if fill is None or reference is None or reference == 0:
        return None
    if side == "buy":
        return decimal_plain_text((fill - reference) / reference * Decimal("10000"))
    if side == "sell":
        return decimal_plain_text((reference - fill) / reference * Decimal("10000"))
    return None


def _side_post_fill_mid_move_bps(fill_side: Any, fill_price: Any, post_fill_mid: Any) -> str | None:
    side = str(fill_side or "").lower()
    fill = decimal_value(fill_price)
    mid = decimal_value(post_fill_mid)
    if fill is None or mid is None or fill == 0:
        return None
    if side == "buy":
        return decimal_plain_text((mid - fill) / fill * Decimal("10000"))
    if side == "sell":
        return decimal_plain_text((fill - mid) / fill * Decimal("10000"))
    return None


def _classify_spike_reversion_context(
    *,
    adverse_fill_vs_decision_mid_bps: Any,
    post_fill_mid_move_bps: Any,
    decision_vwap_minus_mid_bps: Any,
) -> str:
    adverse = decimal_value(adverse_fill_vs_decision_mid_bps)
    post_move = decimal_value(post_fill_mid_move_bps)
    vwap_minus_mid = decimal_value(decision_vwap_minus_mid_bps)
    adverse_10bps = adverse is not None and adverse >= Decimal("10")
    adverse_reversion_10bps = post_move is not None and post_move <= Decimal("-10")
    dislocation_10bps = vwap_minus_mid is not None and abs(vwap_minus_mid) >= Decimal("10")
    if adverse_10bps and adverse_reversion_10bps:
        return "adverse_fill_and_post_fill_reversion_observed"
    if adverse_10bps:
        return "adverse_fill_vs_decision_mid_observed"
    if adverse_reversion_10bps:
        return "post_fill_adverse_reversion_observed"
    if dislocation_10bps:
        return "decision_bar_trade_flow_dislocation_observed"
    return "no_large_spike_reversion_context_observed"


def _directional_spike_reversion_context(decision: dict[str, Any]) -> dict[str, Any]:
    latest_fill = as_dict(decision.get("latest_fill"))
    microstructure = as_dict(decision.get("pretrade_microstructure"))
    decision_context = as_dict(microstructure.get("decision_context"))
    latest_fill_context = as_dict(microstructure.get("latest_fill_context"))
    post_fill_context = as_dict(microstructure.get("post_fill_context"))
    decision_orderbook = as_dict(decision_context.get("orderbook"))
    decision_trade_flow = as_dict(decision_context.get("trade_flow"))
    fill_orderbook = as_dict(latest_fill_context.get("orderbook"))
    post_fill_orderbook = as_dict(post_fill_context.get("orderbook"))
    fill_price = latest_fill.get("fill_price")
    fill_side = latest_fill.get("side")
    decision_mid = decision_orderbook.get("mid_price_last")
    fill_bar_mid = fill_orderbook.get("mid_price_last")
    post_fill_mid = post_fill_orderbook.get("mid_price_last")
    adverse_fill_vs_decision_mid_bps = _side_adverse_fill_bps(fill_side, fill_price, decision_mid)
    adverse_fill_vs_fill_bar_mid_bps = _side_adverse_fill_bps(fill_side, fill_price, fill_bar_mid)
    post_fill_mid_move_bps = _side_post_fill_mid_move_bps(fill_side, fill_price, post_fill_mid)
    decision_mid_to_fill_bar_mid_bps = decimal_bps_change_text(fill_bar_mid, decision_mid)
    decision_vwap_minus_mid_bps = decision_trade_flow.get("vwap_minus_mid_bps")

    checks = [
        ("directional_episode.latest_fill.fill_price", fill_price is not None),
        ("directional_episode.latest_fill.side", fill_side is not None),
        ("directional_episode.pretrade_microstructure.decision_mid", decision_mid is not None),
        ("directional_episode.pretrade_microstructure.fill_bar_mid", fill_bar_mid is not None),
    ]
    smallest_missing = next((field for field, passed in checks if not passed), None)
    status = (
        "verified_spike_reversion_context_present"
        if smallest_missing is None
        else "missing_spike_reversion_context"
    )
    classification = (
        _classify_spike_reversion_context(
            adverse_fill_vs_decision_mid_bps=adverse_fill_vs_decision_mid_bps,
            post_fill_mid_move_bps=post_fill_mid_move_bps,
            decision_vwap_minus_mid_bps=decision_vwap_minus_mid_bps,
        )
        if status == "verified_spike_reversion_context_present"
        else "missing_context"
    )
    return {
        "status": status,
        "smallest_missing_field": smallest_missing,
        "classification": classification,
        "decision_id": decision.get("decision_id"),
        "route_action": decision.get("route_action"),
        "created_at": decision.get("created_at"),
        "latest_fill_ts": latest_fill.get("ingestion_ts"),
        "latest_fill_side": fill_side,
        "latest_fill_price": decimal_text(fill_price),
        "decision_mid_price": decimal_text(decision_mid),
        "fill_bar_mid_price": decimal_text(fill_bar_mid),
        "post_fill_mid_price": decimal_text(post_fill_mid),
        "adverse_fill_vs_decision_mid_bps": adverse_fill_vs_decision_mid_bps,
        "adverse_fill_vs_fill_bar_mid_bps": adverse_fill_vs_fill_bar_mid_bps,
        "decision_mid_to_fill_bar_mid_bps": decision_mid_to_fill_bar_mid_bps,
        "post_fill_mid_move_bps": post_fill_mid_move_bps,
        "decision_trade_flow_vwap_minus_mid_bps": decimal_text(decision_vwap_minus_mid_bps),
        "decision_trade_flow_imbalance": decimal_text(decision_trade_flow.get("trade_flow_imbalance")),
        "decision_taker_buy_ratio": decimal_text(decision_trade_flow.get("taker_buy_ratio")),
        "decision_spread_bps_mean": decimal_text(decision_orderbook.get("spread_bps_mean")),
        "latest_fill_slippage_bps": latest_fill.get("slippage_bps"),
    }


def _directional_episode_microstructure_context(
    decision: dict[str, Any],
    *,
    rdp_microstructure: dict[str, Any],
) -> dict[str, Any]:
    orderbook_rows = [as_dict(row) for row in as_list(rdp_microstructure.get("recent_silver_orderbook"))]
    trade_flow_rows = [as_dict(row) for row in as_list(rdp_microstructure.get("recent_silver_trade_flow"))]
    decision_ts = decision.get("created_at")
    latest_fill = as_dict(decision.get("latest_fill"))
    fill_count = int_or_zero(as_dict(decision.get("fill")).get("count"))
    latest_fill_ts = latest_fill.get("ingestion_ts")

    decision_orderbook, decision_orderbook_age = _nearest_microstructure_bar_at_or_before(
        orderbook_rows,
        decision_ts,
    )
    decision_trade_flow, decision_trade_flow_age = _nearest_microstructure_bar_at_or_before(
        trade_flow_rows,
        decision_ts,
    )
    fill_orderbook: dict[str, Any] = {}
    fill_orderbook_age: int | None = None
    fill_trade_flow: dict[str, Any] = {}
    fill_trade_flow_age: int | None = None
    post_fill_orderbook: dict[str, Any] = {}
    post_fill_orderbook_age: int | None = None
    post_fill_trade_flow: dict[str, Any] = {}
    post_fill_trade_flow_age: int | None = None
    if fill_count > 0:
        fill_orderbook, fill_orderbook_age = _nearest_microstructure_bar_at_or_before(
            orderbook_rows,
            latest_fill_ts,
        )
        fill_trade_flow, fill_trade_flow_age = _nearest_microstructure_bar_at_or_before(
            trade_flow_rows,
            latest_fill_ts,
        )
        post_fill_orderbook, post_fill_orderbook_age = _nearest_microstructure_bar_at_or_after(
            orderbook_rows,
            latest_fill_ts,
        )
        post_fill_trade_flow, post_fill_trade_flow_age = _nearest_microstructure_bar_at_or_after(
            trade_flow_rows,
            latest_fill_ts,
        )

    checks = [
        ("rdp.silver.market_orderbook_metrics_15m.decision_bar", bool(decision_orderbook)),
        ("rdp.silver.market_trade_flow_15m.decision_bar", bool(decision_trade_flow)),
    ]
    if fill_count > 0:
        checks.extend(
            [
                ("rdp.silver.market_orderbook_metrics_15m.latest_fill_bar", bool(fill_orderbook)),
                ("rdp.silver.market_trade_flow_15m.latest_fill_bar", bool(fill_trade_flow)),
            ]
        )
    smallest_missing = next((field for field, passed in checks if not passed), None)
    status = (
        "verified_pretrade_microstructure_context_present"
        if smallest_missing is None
        else "missing_pretrade_microstructure_context"
    )
    return {
        "source": "rdp_microstructure_silver_15m",
        "status": status,
        "smallest_missing_field": smallest_missing,
        "decision_context": {
            "decision_ts": decision_ts,
            "orderbook": _microstructure_orderbook_context(decision_orderbook, decision_orderbook_age),
            "trade_flow": _microstructure_trade_flow_context(decision_trade_flow, decision_trade_flow_age),
        },
        "latest_fill_context": (
            {
                "latest_fill_ts": latest_fill_ts,
                "orderbook": _microstructure_orderbook_context(fill_orderbook, fill_orderbook_age),
                "trade_flow": _microstructure_trade_flow_context(fill_trade_flow, fill_trade_flow_age),
            }
            if fill_count > 0
            else None
        ),
        "post_fill_context": (
            {
                "latest_fill_ts": latest_fill_ts,
                "orderbook": _microstructure_orderbook_context(post_fill_orderbook, post_fill_orderbook_age),
                "trade_flow": _microstructure_trade_flow_context(post_fill_trade_flow, post_fill_trade_flow_age),
            }
            if fill_count > 0 and (post_fill_orderbook or post_fill_trade_flow)
            else None
        ),
    }


def enrich_directional_episodes_with_microstructure(
    decisions: list[dict[str, Any]],
    *,
    rdp_microstructure: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rdp = as_dict(rdp_microstructure)
    if not rdp.get("ok"):
        for decision in decisions:
            decision["pretrade_microstructure"] = {
                "source": "rdp_microstructure_silver_15m",
                "status": "missing_pretrade_microstructure_context",
                "smallest_missing_field": "rdp_microstructure_truth",
                "decision_context": {"decision_ts": decision.get("created_at"), "orderbook": {}, "trade_flow": {}},
                "latest_fill_context": None,
            }
        return decisions, {
            "source": "rdp_microstructure_silver_15m",
            "status": "missing_pretrade_microstructure_context",
            "smallest_missing_field": "rdp_microstructure_truth",
            "coverage": {
                "decisions_with_pretrade_microstructure": 0,
                "filled_decisions_with_pretrade_microstructure": 0,
            },
        }

    for decision in decisions:
        decision["pretrade_microstructure"] = _directional_episode_microstructure_context(
            decision,
            rdp_microstructure=rdp,
        )

    decisions_with_context = sum(
        1
        for decision in decisions
        if as_dict(decision.get("pretrade_microstructure")).get("status")
        == "verified_pretrade_microstructure_context_present"
    )
    filled_decisions = [
        decision for decision in decisions if int_or_zero(as_dict(decision.get("fill")).get("count")) > 0
    ]
    filled_decisions_with_context = sum(
        1
        for decision in filled_decisions
        if as_dict(decision.get("pretrade_microstructure")).get("status")
        == "verified_pretrade_microstructure_context_present"
    )
    latest_filled_context = as_dict(filled_decisions[0].get("pretrade_microstructure")) if filled_decisions else {}
    smallest_missing = next(
        (
            as_dict(decision.get("pretrade_microstructure")).get("smallest_missing_field")
            for decision in decisions
            if as_dict(decision.get("pretrade_microstructure")).get("smallest_missing_field")
        ),
        None,
    )
    if not decisions:
        status = "no_recent_directional_decisions"
    elif not filled_decisions:
        status = "no_recent_filled_directional_decisions"
    elif filled_decisions_with_context > 0:
        status = "verified_filled_directional_episode_pretrade_microstructure_present"
    else:
        status = "missing_pretrade_microstructure_context"
    return decisions, {
        "source": "rdp_microstructure_silver_15m",
        "status": status,
        "smallest_missing_field": latest_filled_context.get("smallest_missing_field") or smallest_missing,
        "coverage": {
            "decisions_with_pretrade_microstructure": decisions_with_context,
            "filled_decisions_with_pretrade_microstructure": filled_decisions_with_context,
        },
        "latest_filled_decision_status": latest_filled_context.get("status"),
        "latest_filled_decision_smallest_missing_field": latest_filled_context.get("smallest_missing_field"),
    }


def sanitize_directional_episode_attribution(
    raw: dict[str, Any] | None,
) -> dict[str, Any]:
    raw = as_dict(raw)
    decisions: list[dict[str, Any]] = []
    for row in [as_dict(item) for item in as_list(raw.get("recent_decisions"))]:
        payload = as_dict(row.get("payload"))
        expected_edge_bps = allocation_expected_metric_value(row, "expected_edge_bps")
        expected_cost_bps = allocation_expected_metric_value(row, "expected_cost_bps")
        sleeve_summaries = summarize_sleeve_intents(payload)
        reason_codes = compact_unique(
            collect_reason_codes(payload) + collect_sleeve_reason_codes(sleeve_summaries),
            limit=32,
        )
        realized_cost_proxy_bps = decimal_add_text(
            row.get("actual_fee_bps_mean"),
            row.get("realized_slippage_bps_mean"),
        )
        decision = {
            "allocation_id": row.get("allocation_id"),
            "decision_id": row.get("decision_id"),
            "symbol": row.get("symbol"),
            "created_at": row.get("created_at"),
            "route_action": row.get("route_action"),
            "primary_family": row.get("primary_family"),
            "portfolio_requested_notional": decimal_text(row.get("portfolio_requested_notional")),
            "portfolio_approved_notional": decimal_text(row.get("portfolio_approved_notional")),
            "portfolio_budget_cut_notional": decimal_text(row.get("portfolio_budget_cut_notional")),
            "expected_edge_bps": decimal_text(expected_edge_bps),
            "expected_cost_bps": decimal_text(expected_cost_bps),
            "expected_edge_bps_source": allocation_expected_metric_source(row, "expected_edge_bps"),
            "expected_cost_bps_source": allocation_expected_metric_source(row, "expected_cost_bps"),
            "expected_net_edge_bps": decimal_subtract_text(
                expected_edge_bps,
                expected_cost_bps,
            ),
            "realized_cost_proxy_bps": realized_cost_proxy_bps,
            "edge_after_realized_cost_proxy_bps": decimal_subtract_text(
                expected_edge_bps,
                realized_cost_proxy_bps,
            ),
            "order": {
                "count": int_or_zero(row.get("order_count")),
                "created_or_submitting_no_venue_count": int_or_zero(
                    row.get("created_or_submitting_no_venue_count")
                ),
                "terminal_no_fill_count": int_or_zero(row.get("terminal_no_fill_order_count")),
                "blocked_count": int_or_zero(row.get("blocked_order_count")),
                "states": row.get("order_states"),
                "position_intents": row.get("order_position_intents"),
                "execution_actions": row.get("order_execution_actions"),
                "strategy_bundle_ids": row.get("order_strategy_bundle_ids"),
                "first_created_at": row.get("first_order_created_at"),
                "last_created_at": row.get("last_order_created_at"),
            },
            "fill": {
                "count": int_or_zero(row.get("fill_count")),
                "filled_order_count": int_or_zero(row.get("filled_order_count")),
                "first_fill_ts": row.get("first_fill_ts"),
                "latest_fill_ts": row.get("latest_fill_ts"),
                "turnover_usdt": decimal_text(row.get("turnover_usdt")),
                "fee_usdt": decimal_text(row.get("fee_usdt")),
                "actual_fee_bps_sample_count": int_or_zero(row.get("actual_fee_bps_sample_count")),
                "actual_fee_bps_mean": decimal_text(row.get("actual_fee_bps_mean")),
                "realized_slippage_sample_count": int_or_zero(row.get("realized_slippage_sample_count")),
                "realized_slippage_bps_mean": decimal_text(row.get("realized_slippage_bps_mean")),
                "slippage_reference_sample_count": int_or_zero(row.get("slippage_reference_sample_count")),
                "sides": row.get("fill_sides"),
                "liquidity_roles": row.get("liquidity_roles"),
                "position_intents": row.get("fill_position_intents"),
                "order_states": row.get("filled_order_states"),
                "strategy_bundle_ids": row.get("fill_strategy_bundle_ids"),
            },
            "pnl_outcome": {
                "fill_outcome_count": int_or_zero(row.get("fill_outcome_count")),
                "realized_pnl_usdt": decimal_text(row.get("realized_pnl_usdt")),
                "fill_outcome_fee_delta_usdt": decimal_text(row.get("fill_outcome_fee_delta_usdt")),
            },
            "position_lifecycle": {
                "source_lot_count": int_or_zero(row.get("source_lot_count")),
                "open_source_lot_count": int_or_zero(row.get("open_source_lot_count")),
                "closed_source_lot_count": int_or_zero(row.get("closed_source_lot_count")),
                "open_source_lot_qty": decimal_text(row.get("open_source_lot_qty")),
                "source_lot_statuses": row.get("source_lot_statuses"),
                "source_lot_exposure_sides": row.get("source_lot_exposure_sides"),
                "lot_event_count": int_or_zero(row.get("lot_event_count")),
                "lot_open_event_count": int_or_zero(row.get("lot_open_event_count")),
                "lot_close_event_count": int_or_zero(row.get("lot_close_event_count")),
                "lot_realized_pnl_usdt": decimal_text(row.get("lot_realized_pnl_usdt")),
                "lot_event_types": row.get("lot_event_types"),
            },
            "latest_fill": {
                "fill_id": row.get("latest_fill_id"),
                "side": row.get("latest_fill_side"),
                "fill_qty": decimal_text(row.get("latest_fill_qty")),
                "fill_price": decimal_text(row.get("latest_fill_price")),
                "fee_amount": decimal_text(row.get("latest_fill_fee_amount")),
                "ingestion_ts": row.get("latest_fill_ingestion_ts"),
                "slippage_bps": decimal_text(row.get("latest_fill_slippage_bps")),
                "slippage_reference_source": row.get("latest_fill_slippage_reference_source"),
                "realized_pnl_delta": decimal_text(row.get("latest_fill_realized_pnl_delta")),
                "source_lot_status": row.get("latest_fill_source_lot_status"),
                "source_lot_open_qty": decimal_text(row.get("latest_fill_source_lot_open_qty")),
                "source_lot_exposure_side": row.get("latest_fill_source_lot_exposure_side"),
                "lot_event_types": row.get("latest_fill_lot_event_types"),
                "lot_open_event_count": int_or_zero(row.get("latest_fill_lot_open_event_count")),
                "lot_close_event_count": int_or_zero(row.get("latest_fill_lot_close_event_count")),
                "lot_realized_pnl_delta": decimal_text(row.get("latest_fill_lot_realized_pnl_delta")),
            },
            "guard_decision": {
                "reason_codes": reason_codes,
                "operator_summary": truncate_text(payload.get("operator_summary")),
                "sleeve_intent_summary": sleeve_summaries,
            },
            "classification": classify_directional_episode_row(row),
        }
        decision["order_expectation"] = classify_directional_episode_order_expectation(decision)
        decision["no_order_semantics"] = classify_directional_episode_no_order_semantics(decision)
        decision["pnl_lifecycle"] = classify_directional_episode_pnl_lifecycle(decision)
        decisions.append(decision)
    return {
        "symbol": raw.get("symbol"),
        "recent_decisions": decisions,
    }


def sanitize_db_probe_payload(payload: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(payload)
    latest = summarize_latest_decision(
        sanitized.get("latest_decision"),
        sanitized.pop("latest_decision_audit", None),
        sanitized.pop("latest_decision_counts", None),
    )
    latest_executable_directional = summarize_latest_decision(
        sanitized.get("latest_executable_directional_decision"),
        sanitized.pop("latest_executable_directional_decision_audit", None),
        sanitized.pop("latest_executable_directional_decision_counts", None),
    )
    sanitized["latest_decision"] = latest
    sanitized["latest_executable_directional_decision"] = latest_executable_directional
    sanitized["directional_episode_attribution"] = sanitize_directional_episode_attribution(
        sanitized.get("directional_episode_attribution"),
    )
    return sanitized


def run_command(
    args: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 30,
    stdin: str | None = None,
) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            input=stdin,
        )
    except FileNotFoundError as exc:
        return {
            "ok": False,
            "returncode": None,
            "stdout": "",
            "stderr": redact_secret_text(str(exc)),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "returncode": None,
            "stdout": redact_secret_text(exc.stdout or ""),
            "stderr": f"command_timeout_after_{timeout}s",
        }

    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": redact_secret_text(proc.stdout.strip()),
        "stderr": redact_secret_text(proc.stderr.strip()),
    }


def parse_left_right_count(text: str) -> dict[str, int | None]:
    parts = text.strip().split()
    if len(parts) < 2:
        return {"ahead": None, "behind": None}
    try:
        return {"ahead": int(parts[0]), "behind": int(parts[1])}
    except ValueError:
        return {"ahead": None, "behind": None}


def parse_git_status_header(header: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "branch": None,
        "tracking": None,
        "ahead": 0,
        "behind": 0,
        "raw": header.strip(),
    }
    header = header.strip()
    if not header.startswith("## "):
        return result
    body = header[3:]
    branch_part, _, relation = body.partition("...")
    result["branch"] = branch_part.split()[0] if branch_part else None
    if relation:
        tracking, _, flags = relation.partition(" ")
        result["tracking"] = tracking or None
        ahead_match = re.search(r"ahead\s+(\d+)", flags)
        behind_match = re.search(r"behind\s+(\d+)", flags)
        if ahead_match:
            result["ahead"] = int(ahead_match.group(1))
        if behind_match:
            result["behind"] = int(behind_match.group(1))
    return result


def parse_docker_ps(stdout: str) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for line in stdout.splitlines():
        if not line.strip():
            continue
        name, _, status = line.partition("\t")
        if name and status:
            statuses[name.strip()] = status.strip()
    return statuses


def bash_cd_target(path: str) -> str:
    """Return a bash-safe cd target while preserving ~/ expansion."""

    if path == "~":
        return "$HOME"
    if path.startswith("~/"):
        return "$HOME/" + shlex.quote(path[2:])
    return shlex.quote(path)


def summarize_container_health(statuses: dict[str, str]) -> dict[str, Any]:
    required: dict[str, dict[str, Any]] = {}
    for name in REQUIRED_APP_CONTAINERS:
        status = statuses.get(name)
        running = bool(status and status.startswith("Up"))
        healthy = bool(status and "(healthy)" in status)
        required[name] = {
            "status": status or "missing",
            "running": running,
            "healthy": healthy,
        }
    all_healthy = all(item["running"] and item["healthy"] for item in required.values())
    return {
        "all_required_app_containers_healthy": all_healthy,
        "required": required,
        "observed_count": len(statuses),
    }


def fetch_url_text(url: str, *, timeout: int = 10, headers: dict[str, str] | None = None) -> dict[str, Any]:
    context = ssl._create_unverified_context()
    request_headers = {"User-Agent": "aats-runtime-truth-report/1.0"}
    if headers:
        request_headers.update(headers)
    request = Request(url, headers=request_headers)
    try:
        with urlopen(request, timeout=timeout, context=context) as response:
            body = response.read().decode("utf-8", errors="replace")
            return {"ok": True, "status": response.status, "body": redact_secret_text(body)}
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return {
            "ok": False,
            "status": exc.code,
            "body": redact_secret_text(body),
            "error": redact_secret_text(str(exc)),
        }
    except (URLError, TimeoutError) as exc:
        return {"ok": False, "status": None, "body": "", "error": redact_secret_text(str(exc))}


def fetch_json_url(url: str, *, timeout: int = 10, headers: dict[str, str] | None = None) -> dict[str, Any]:
    fetched = fetch_url_text(url, timeout=timeout, headers=headers)
    if not fetched["ok"]:
        return fetched | {"json": None}
    try:
        return fetched | {"json": json.loads(fetched["body"])}
    except json.JSONDecodeError as exc:
        return fetched | {"ok": False, "json": None, "error": f"invalid_json:{exc.msg}"}


def wsl_fetch_json_url(distro: str, url: str, *, timeout: int = 10) -> dict[str, Any]:
    completed = run_command(
        ["wsl", "-d", distro, "--", "bash", "-lc", f"curl -kfsS {shlex.quote(url)}"],
        timeout=timeout + 5,
    )
    if not completed["ok"]:
        return {
            "ok": False,
            "status": None,
            "body": completed.get("stdout") or "",
            "json": None,
            "error": completed.get("stderr") or f"curl_exit_{completed.get('returncode')}",
        }
    body = completed["stdout"]
    try:
        return {
            "ok": True,
            "status": 200,
            "body": redact_secret_text(body),
            "json": json.loads(body),
        }
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "status": 200,
            "body": redact_secret_text(body),
            "json": None,
            "error": f"invalid_json:{exc.msg}",
        }


def gateway_health_probe(api_base: str, distro: str) -> dict[str, Any]:
    url = f"{api_base.rstrip('/')}/healthz"
    direct = fetch_json_url(url, timeout=10)
    if direct.get("ok") and direct.get("json") is not None:
        return direct | {"probe_source": "windows_api_base"}
    fallback = wsl_fetch_json_url(distro, url, timeout=10)
    if fallback.get("ok") and fallback.get("json") is not None:
        return fallback | {
            "probe_source": "wsl_localhost_fallback",
            "fallback_from_error": direct.get("error"),
            "fallback_from_status": direct.get("status"),
        }
    return direct | {
        "probe_source": "windows_api_base",
        "wsl_fallback_error": fallback.get("error"),
        "wsl_fallback_status": fallback.get("status"),
    }


def operator_read_auth_context(env_var: str | None) -> dict[str, Any]:
    env_name = (env_var or "").strip()
    if not env_name:
        return {
            "status": "disabled",
            "method": "api_key_env",
            "env_var": None,
            "credential_present": False,
            "headers": {},
            "raw_credential_exposed": False,
        }
    credential = os.environ.get(env_name)
    if credential:
        return {
            "status": "credential_present",
            "method": "api_key_env",
            "env_var": env_name,
            "credential_present": True,
            "headers": {"X-AATS-API-Key": credential},
            "raw_credential_exposed": False,
        }
    return {
        "status": "credential_missing",
        "method": "api_key_env",
        "env_var": env_name,
        "credential_present": False,
        "headers": {},
        "raw_credential_exposed": False,
    }


def operator_read_auth_report(auth_context: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": auth_context.get("status"),
        "method": auth_context.get("method"),
        "env_var": auth_context.get("env_var"),
        "credential_present": bool(auth_context.get("credential_present")),
        "header_injected": bool(auth_context.get("headers")),
        "raw_credential_exposed": False,
    }


def dashboard_bundle_probe(api_base: str, *, headers: dict[str, str] | None = None) -> dict[str, Any]:
    query = urlencode(
        [
            ("view", "strategy"),
            ("panel", "mode"),
            ("panel", "latestDecision"),
            ("panel", "recentDecisions"),
            ("panel", "aiRuntime"),
            ("panel", "profileControlSummary"),
        ],
    )
    response = fetch_json_url(f"{api_base.rstrip('/')}/dashboard/bundle?{query}", timeout=10, headers=headers)
    if not response["ok"] or response.get("json") is None:
        return {
            "status": "request_failed",
            "http_status": response.get("status"),
            "error": response.get("error") or "dashboard_bundle_unavailable",
        }
    return summarize_dashboard_bundle(response["json"])


def parse_json_object_body(body: str | None) -> dict[str, Any]:
    if not body:
        return {}
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def ai_runtime_endpoint_probe(
    api_base: str,
    *,
    headers: dict[str, str] | None = None,
    auth_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = fetch_url_text(f"{api_base.rstrip('/')}/ai/runtime", timeout=10, headers=headers)
    body = parse_json_object_body(response.get("body"))
    auth_attempt = operator_read_auth_report(auth_context or {})
    if response.get("ok"):
        return {
            "status": "verified",
            "http_status": response.get("status"),
            "runtime": summarize_ai_runtime_endpoint_payload(body),
            "auth_attempt": auth_attempt,
            "raw_payload_exposed": False,
        }

    detail = body.get("detail")
    if response.get("status") == 401 and detail == "operator_auth_required":
        status = "auth_failed" if auth_attempt.get("credential_present") else "auth_required"
        return {
            "status": status,
            "http_status": response.get("status"),
            "error": "operator_auth_required",
            "runtime": {},
            "auth_attempt": auth_attempt,
            "raw_payload_exposed": False,
        }

    return {
        "status": "request_failed",
        "http_status": response.get("status"),
        "error": response.get("error") or body.get("detail") or "ai_runtime_unavailable",
        "runtime": {},
        "auth_attempt": auth_attempt,
        "raw_payload_exposed": False,
    }


def summarized_field(value: Any, *, missing_status: str = "missing") -> dict[str, Any]:
    return {
        "status": "verified" if value is not None else missing_status,
        "value": value,
    }


def unknown_auth_required_field() -> dict[str, Any]:
    return {"status": "unknown_auth_required", "value": None}


def summarize_ai_runtime_endpoint_payload(payload: dict[str, Any]) -> dict[str, Any]:
    manual_override_active = payload.get("manual_override_active")
    if isinstance(manual_override_active, bool):
        manual_override_status = "verified"
        manual_override_mode_status = (
            "verified"
            if manual_override_active and payload.get("manual_override_mode") is not None
            else "not_applicable_no_manual_override"
        )
    else:
        manual_override_status = "missing"
        manual_override_mode_status = "missing"

    return {
        "configured_operating_mode": summarized_field(payload.get("configured_operating_mode")),
        "effective_operating_mode": summarized_field(payload.get("effective_operating_mode")),
        "manual_override": {
            "status": manual_override_status,
            "active": manual_override_active if isinstance(manual_override_active, bool) else None,
            "mode": payload.get("manual_override_mode"),
            "mode_status": manual_override_mode_status,
            "freeze_until": payload.get("manual_override_freeze_until"),
        },
        "provider": {
            "name": payload.get("provider"),
            "state": payload.get("provider_state"),
            "configured": summarized_field(payload.get("configured")),
            "ready": summarized_field(payload.get("provider_ready")),
            "degraded": summarized_field(payload.get("provider_degraded")),
            "recent_timeout_count": payload.get("recent_timeout_count"),
        },
        "shadow_enabled": summarized_field(payload.get("shadow_mode_enabled")),
        "profile_auto_control_effective": summarized_field(
            payload.get("strategy_profile_auto_control_effective")
        ),
        "runtime_source": payload.get("ai_runtime_source"),
        "queried_from_process_role": payload.get("queried_from_process_role"),
        "ai_service_loaded": payload.get("ai_service_loaded"),
    }


def summarize_dashboard_bundle(payload: dict[str, Any]) -> dict[str, Any]:
    auth = payload.get("auth") or {}
    panels = payload.get("panels") or {}
    primary_error = auth.get("primary_error")
    access_state = auth.get("access_state")
    if primary_error == "operator_auth_required" or access_state == "auth_required":
        return {
            "status": "auth_required",
            "access_state": access_state,
            "primary_error": primary_error,
            "blocked_panel_keys": auth.get("blocked_panel_keys") or [],
            "effective_operating_mode": {
                "status": "unknown_auth_required",
                "value": None,
            },
            "profile_auto_control_effective": {
                "status": "unknown_auth_required",
                "value": None,
            },
        }

    ai_runtime = ((panels.get("aiRuntime") or {}).get("data") or {})
    mode = ((panels.get("mode") or {}).get("data") or {})
    profile = ((panels.get("profileControlSummary") or {}).get("data") or {})
    configured_mode = (
        ai_runtime.get("configured_operating_mode")
        or mode.get("configured_operating_mode")
        or mode.get("canonical_configured_operating_mode")
    )
    effective_mode = (
        ai_runtime.get("effective_operating_mode")
        or mode.get("effective_operating_mode")
        or mode.get("canonical_effective_operating_mode")
    )
    profile_effective = (
        profile.get("strategy_profile_auto_control_effective")
        if "strategy_profile_auto_control_effective" in profile
        else profile.get("auto_control_effective")
    )
    return {
        "status": "verified",
        "access_state": access_state,
        "primary_error": primary_error,
        "configured_operating_mode": {
            "status": "verified" if configured_mode else "missing",
            "value": configured_mode,
        },
        "effective_operating_mode": {
            "status": "verified" if effective_mode else "missing",
            "value": effective_mode,
        },
        "profile_auto_control_effective": {
            "status": "verified" if profile_effective is not None else "missing",
            "value": profile_effective,
        },
    }


def summarize_ai_runtime_effective_mode_truth(
    *,
    dashboard_bundle: dict[str, Any],
    ai_runtime_endpoint: dict[str, Any],
) -> dict[str, Any]:
    dashboard = as_dict(dashboard_bundle)
    endpoint = as_dict(ai_runtime_endpoint)
    endpoint_runtime = as_dict(endpoint.get("runtime"))
    endpoint_status = endpoint.get("status")
    dashboard_status = dashboard.get("status")
    auth_gated = endpoint_status == "auth_required" or dashboard_status == "auth_required"
    auth_failed = endpoint_status == "auth_failed"

    if endpoint_status == "verified":
        configured = as_dict(endpoint_runtime.get("configured_operating_mode"))
        effective = as_dict(endpoint_runtime.get("effective_operating_mode"))
        manual_override = as_dict(endpoint_runtime.get("manual_override"))
        provider = as_dict(endpoint_runtime.get("provider"))
        shadow = as_dict(endpoint_runtime.get("shadow_enabled"))
        profile_auto = as_dict(endpoint_runtime.get("profile_auto_control_effective"))
    elif auth_gated or auth_failed:
        configured = unknown_auth_required_field()
        effective = unknown_auth_required_field()
        manual_override = {
            "status": "unknown_auth_required",
            "active": None,
            "mode": None,
            "mode_status": "unknown_auth_required",
            "freeze_until": None,
        }
        provider = {
            "name": None,
            "state": None,
            "configured": unknown_auth_required_field(),
            "ready": unknown_auth_required_field(),
            "degraded": unknown_auth_required_field(),
            "recent_timeout_count": None,
        }
        shadow = unknown_auth_required_field()
        profile_auto = unknown_auth_required_field()
    else:
        configured = summarized_field(None)
        effective = summarized_field(None)
        manual_override = {
            "status": "missing",
            "active": None,
            "mode": None,
            "mode_status": "missing",
            "freeze_until": None,
        }
        provider = {
            "name": None,
            "state": None,
            "configured": summarized_field(None),
            "ready": summarized_field(None),
            "degraded": summarized_field(None),
            "recent_timeout_count": None,
        }
        shadow = summarized_field(None)
        profile_auto = summarized_field(None)

    effective_value = effective.get("value")
    provider_configured = as_dict(provider.get("configured"))
    provider_ready = as_dict(provider.get("ready"))
    provider_degraded = as_dict(provider.get("degraded"))
    provider_path_evidence_present = (
        effective.get("status") == "verified"
        and provider_configured.get("status") == "verified"
        and provider_ready.get("status") == "verified"
    )
    provider_path_active = None
    if provider_path_evidence_present:
        provider_path_active = (
            effective_value not in (None, "baseline_only")
            and bool(provider_configured.get("value"))
            and bool(provider_ready.get("value"))
        )

    recent_timeout_count = int_or_zero(provider.get("recent_timeout_count"))
    if auth_failed:
        timeout_classification = "not_active_blocker_auth_failed_provider_path_not_verified"
        ai_timeout_active_blocker: bool | str = False
    elif auth_gated:
        timeout_classification = "not_active_blocker_auth_gated_provider_path_not_verified"
        ai_timeout_active_blocker = False
    elif not provider_path_evidence_present:
        timeout_classification = "not_active_blocker_missing_provider_path_evidence"
        ai_timeout_active_blocker = False
    elif not provider_path_active:
        timeout_classification = "not_active_blocker_provider_path_inactive"
        ai_timeout_active_blocker = False
    elif recent_timeout_count > 0:
        timeout_classification = "active_provider_timeout_evidence_present"
        ai_timeout_active_blocker = True
    else:
        timeout_classification = "not_active_blocker_no_recent_timeout"
        ai_timeout_active_blocker = False

    if endpoint_status == "verified" and effective.get("status") == "verified":
        status = "verified_effective_ai_runtime_truth"
        smallest_missing = None
    elif auth_failed:
        status = "auth_failed_effective_ai_runtime_truth"
        smallest_missing = "valid_operator_read_credential"
    elif auth_gated:
        status = "auth_gated_effective_ai_runtime_truth"
        smallest_missing = "operator_authenticated_runtime_read_access"
    else:
        status = "missing_effective_ai_runtime_truth"
        smallest_missing = "ai_runtime_endpoint.effective_operating_mode"

    return {
        "source": "gateway_ai_runtime_and_dashboard_bundle",
        "ok": status != "missing_effective_ai_runtime_truth",
        "status": status,
        "smallest_missing_field": smallest_missing,
        "auth_gate": {
            "auth_required": auth_gated,
            "dashboard_status": dashboard_status,
            "ai_runtime_endpoint_status": endpoint_status,
            "ai_runtime_http_status": endpoint.get("http_status"),
            "primary_error": dashboard.get("primary_error") or endpoint.get("error"),
        },
        "operator_read_auth": endpoint.get("auth_attempt") or {},
        "configured_target": configured,
        "effective_runtime_mode": effective,
        "manual_override": manual_override,
        "provider": {
            "name": provider.get("name"),
            "state": provider.get("state"),
            "configured": provider_configured,
            "ready": provider_ready,
            "degraded": provider_degraded,
            "path_evidence_present": provider_path_evidence_present,
            "path_active": provider_path_active,
            "recent_timeout_count": provider.get("recent_timeout_count"),
        },
        "shadow_enabled": shadow,
        "profile_auto_control_effective": profile_auto,
        "runtime_source": endpoint_runtime.get("runtime_source"),
        "queried_from_process_role": endpoint_runtime.get("queried_from_process_role"),
        "ai_service_loaded": endpoint_runtime.get("ai_service_loaded"),
        "ai_timeout": {
            "classification": timeout_classification,
            "active_blocker": ai_timeout_active_blocker,
        },
        "interpretation": {
            "configured_target_is_not_effective_runtime": True,
            "auth_required_does_not_imply_baseline_only": auth_gated,
            "auth_failed_does_not_imply_provider_timeout": auth_failed,
            "ai_timeout_requires_verified_provider_path": True,
            "raw_payload_exposed": False,
        },
    }


def db_probe_command(distro: str, gateway_container: str) -> list[str]:
    return [
        "wsl",
        "-d",
        distro,
        "--",
        "docker",
        "exec",
        "-i",
        gateway_container,
        "python",
        "-",
    ]


def parse_db_probe(stdout: str, stderr: str = "") -> dict[str, Any]:
    if not stdout.strip():
        return {"ok": False, "reason": "db_probe_empty_output", "stderr": redact_secret_text(stderr)}
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "reason": f"db_probe_invalid_json:{exc.msg}",
            "stderr": redact_secret_text(stderr),
        }
    return sanitize_db_probe_payload(payload)


def database_truth_probe(distro: str, gateway_container: str) -> dict[str, Any]:
    completed = run_command(
        db_probe_command(distro, gateway_container),
        timeout=DATABASE_TRUTH_PROBE_TIMEOUT_SECONDS,
        stdin=DB_PROBE,
    )
    if not completed["ok"]:
        return {
            "ok": False,
            "reason": "db_probe_command_failed",
            "returncode": completed["returncode"],
            "stderr": completed["stderr"],
        }
    return parse_db_probe(completed["stdout"], completed["stderr"])


def rdp_microstructure_probe_command(distro: str, gateway_container: str) -> list[str]:
    return [
        "wsl",
        "-d",
        distro,
        "--",
        "docker",
        "exec",
        "-i",
        gateway_container,
        "python",
        "-",
    ]


def parse_rdp_microstructure_probe(stdout: str, stderr: str = "") -> dict[str, Any]:
    if not stdout.strip():
        return {
            "ok": False,
            "reason": "rdp_microstructure_probe_empty_output",
            "stderr": redact_secret_text(stderr),
        }
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "reason": f"rdp_microstructure_probe_invalid_json:{exc.msg}",
            "stderr": redact_secret_text(stderr),
        }
    return payload if isinstance(payload, dict) else {"ok": False, "reason": "rdp_microstructure_probe_non_object"}


def rdp_microstructure_truth_probe(distro: str, gateway_container: str) -> dict[str, Any]:
    completed = run_command(
        rdp_microstructure_probe_command(distro, gateway_container),
        timeout=45,
        stdin=RDP_MICROSTRUCTURE_PROBE,
    )
    if not completed["ok"]:
        return {
            "ok": False,
            "reason": "rdp_microstructure_probe_command_failed",
            "returncode": completed["returncode"],
            "stderr": completed["stderr"],
        }
    return parse_rdp_microstructure_probe(completed["stdout"], completed["stderr"])


def microstructure_collector_truth_probe(distro: str, container: str) -> dict[str, Any]:
    ps = run_command(
        [
            "wsl",
            "-d",
            distro,
            "--",
            "docker",
            "ps",
            "--filter",
            f"name={container}",
            "--format",
            "{{.Names}}\t{{.Status}}",
        ],
        timeout=30,
    )
    status = None
    if ps["ok"] and ps["stdout"].strip():
        statuses = parse_docker_ps(ps["stdout"])
        status = statuses.get(container) or next(iter(statuses.values()), None)
    running = bool(status and status.startswith("Up"))
    healthy = bool(status and "(healthy)" in status)

    cmdline = None
    if running:
        cmdline_result = run_command(
            [
                "wsl",
                "-d",
                distro,
                "--",
                "docker",
                "exec",
                container,
                "cat",
                "/proc/1/cmdline",
            ],
            timeout=10,
        )
        if cmdline_result["ok"]:
            cmdline = " ".join(part for part in cmdline_result["stdout"].replace("\x00", " ").split() if part)

    heartbeat: dict[str, Any] = {
        "path": MICROSTRUCTURE_HEARTBEAT_PATH,
        "exists": False,
        "fresh": False,
        "age_seconds": None,
        "stale_after_seconds": MICROSTRUCTURE_HEARTBEAT_STALE_AFTER_SECONDS,
        "mtime_utc": None,
    }
    if running:
        now_result = run_command(
            ["wsl", "-d", distro, "--", "docker", "exec", container, "date", "+%s"],
            timeout=10,
        )
        stat_result = run_command(
            [
                "wsl",
                "-d",
                distro,
                "--",
                "docker",
                "exec",
                container,
                "stat",
                "-c",
                "%Y",
                MICROSTRUCTURE_HEARTBEAT_PATH,
            ],
            timeout=10,
        )
        if now_result["ok"] and stat_result["ok"] and stat_result["stdout"].strip():
            now_epoch = int_or_zero(now_result["stdout"])
            mtime_epoch = int_or_zero(stat_result["stdout"])
            age_seconds = max(0, now_epoch - mtime_epoch) if now_epoch and mtime_epoch else None
            heartbeat.update(
                {
                    "exists": True,
                    "mtime_epoch": mtime_epoch,
                    "mtime_utc": (
                        datetime.fromtimestamp(mtime_epoch, UTC)
                        .replace(microsecond=0)
                        .isoformat()
                        .replace("+00:00", "Z")
                        if mtime_epoch
                        else None
                    ),
                    "age_seconds": age_seconds,
                    "fresh": (
                        age_seconds is not None
                        and age_seconds <= MICROSTRUCTURE_HEARTBEAT_STALE_AFTER_SECONDS
                    ),
                }
            )
        elif stat_result["returncode"] not in (None, 0):
            heartbeat["stat_error"] = redact_secret_text(stat_result["stderr"])

    return {
        "ok": bool(running and healthy),
        "container": container,
        "status": status or "missing",
        "running": running,
        "healthy": healthy,
        "cmdline": redact_secret_text(cmdline) if cmdline else None,
        "daemon_script_detected": bool(cmdline and "scripts/microstructure_ws_daemon.py" in cmdline),
        "heartbeat": heartbeat,
        "errors": {
            "docker_ps": None if ps["ok"] else ps["stderr"],
        },
    }


def int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def summarize_claimed_submit_stuck_submission_truth(
    db: dict[str, Any],
    *,
    report_generated_at: str,
) -> dict[str, Any]:
    raw = as_dict(db.get("claimed_submit_stuck_submission"))
    if not db.get("ok"):
        return {
            "status": "missing_database_truth",
            "smallest_missing_field": "database_truth",
            "root_cause": CLAIMED_SUBMIT_STUCK_ROOT_CAUSE,
            "report_generated_at": report_generated_at,
            "interpretation": "database probe did not return authoritative live facts",
        }
    if not raw:
        return {
            "status": "missing_claimed_submit_stuck_submission_probe",
            "smallest_missing_field": "database_truth.claimed_submit_stuck_submission",
            "root_cause": CLAIMED_SUBMIT_STUCK_ROOT_CAUSE,
            "report_generated_at": report_generated_at,
            "interpretation": "runtime truth report lacks claimed submit stuck-submission coverage",
        }

    coverage = as_dict(raw.get("coverage"))
    latest_order = as_dict(raw.get("latest_order"))
    latest_reconciliation = as_dict(raw.get("latest_reconciliation"))
    latest_baseline = as_dict(raw.get("latest_baseline"))
    finding_counts = as_dict(raw.get("latest_reconciliation_finding_counts"))
    operator_action_counts = as_dict(raw.get("operator_action_counts"))
    count = int_or_zero(coverage.get("total"))

    if count == 0:
        status = "verified_no_claimed_submit_stuck_submission"
        smallest_missing_field = None
        current_blocker = None
        required_confirmation = None
        interpretation = "no CREATED/SUBMITTING no-venue order has a CLAIMED submit command without fills"
    else:
        client_order_id = latest_order.get("client_order_id")
        required_confirmation = (
            f"{CLAIMED_SUBMIT_RECOVERY_CONFIRMATION_PREFIX}{client_order_id}"
            if client_order_id
            else None
        )
        action_count = int_or_zero(operator_action_counts.get("resolve_stuck_submission_for_order"))
        if action_count > 0:
            status = "recovery_action_attempted_claimed_submit_still_present"
            interpretation = (
                "a resolve_stuck_submission action mentions the order, but the claimed submit blocker still exists"
            )
        else:
            status = "blocked_external_operator_confirmation_required"
            interpretation = (
                "claimed submit outcome is unknown on exchange; protected recovery requires explicit operator "
                "confirmation that OKX has no matching order"
            )
        smallest_missing_field = "operator_confirmation"
        current_blocker = "external_operator_confirmation_required_before_resolve_stuck_submission"

    return {
        "status": status,
        "smallest_missing_field": smallest_missing_field,
        "root_cause": raw.get("root_cause") or CLAIMED_SUBMIT_STUCK_ROOT_CAUSE,
        "report_generated_at": report_generated_at,
        "symbol": raw.get("symbol"),
        "current_blocker": current_blocker,
        "required_operator_confirmation": required_confirmation,
        "coverage": {
            "claimed_submit_stuck_submission_count": count,
            "claimed_submit_stuck_submission_24h": int_or_zero(coverage.get("last_24h")),
            "claimed_submit_stuck_submission_1h": int_or_zero(coverage.get("last_1h")),
            "oldest_created_at": coverage.get("oldest_created_at"),
            "latest_updated_at": coverage.get("latest_updated_at"),
        },
        "latest_order": latest_order or None,
        "latest_reconciliation": latest_reconciliation or None,
        "latest_reconciliation_finding_counts": finding_counts,
        "latest_reconciliation_findings_for_order": as_list(
            raw.get("latest_reconciliation_findings_for_order"),
        ),
        "latest_baseline": latest_baseline or None,
        "operator_action_counts": operator_action_counts,
        "latest_operator_action_for_order": raw.get("latest_operator_action_for_order"),
        "interpretation": interpretation,
    }


def summarize_target_convergence_guard_truth(
    db: dict[str, Any],
    git: dict[str, Any],
    *,
    report_generated_at: str,
) -> dict[str, Any]:
    raw = as_dict(db.get("target_convergence_guard"))
    if not db.get("ok"):
        return {
            "status": "missing_database_truth",
            "smallest_missing_field": "database_truth",
            "guard_flag": TARGET_CONVERGENCE_GUARD_FLAG,
            "report_generated_at": report_generated_at,
            "interpretation": "database probe did not return authoritative live facts",
        }
    if not raw:
        return {
            "status": "missing_target_convergence_guard_probe",
            "smallest_missing_field": "database_truth.target_convergence_guard",
            "guard_flag": TARGET_CONVERGENCE_GUARD_FLAG,
            "report_generated_at": report_generated_at,
            "interpretation": "runtime truth report lacks target convergence guard coverage",
        }

    coverage = as_dict(raw.get("coverage"))
    current = as_dict(raw.get("current_open_orders"))
    execution_orders = as_dict(current.get("execution_orders"))
    legacy_order_states = as_dict(current.get("legacy_order_states"))
    execution_open_count = int_or_zero(execution_orders.get("open_order_count"))
    legacy_open_count = int_or_zero(legacy_order_states.get("open_order_count"))
    current_open_order_count = execution_open_count + legacy_open_count
    guard_hits_total = int_or_zero(coverage.get("guard_hits_total"))
    guard_hits_24h = int_or_zero(coverage.get("guard_hits_24h"))
    guard_hits_1h = int_or_zero(coverage.get("guard_hits_1h"))
    recent_decisions_1h = int_or_zero(coverage.get("directional_decisions_1h"))
    deployed_matches_windows = git.get("deployed_matches_windows")

    status = "pending_guard_trigger_sample"
    smallest_missing_field = None
    interpretation = "guard deployed but no qualifying trigger sample has appeared yet"
    if deployed_matches_windows is False:
        status = "deployment_mismatch_guard_truth_not_authoritative"
        smallest_missing_field = "deployed_head_matches_windows_head"
        interpretation = "runtime may not be running the local guard implementation"
    elif guard_hits_total > 0:
        status = "verified_guard_triggered"
        interpretation = "at least one directional decision carries the target convergence guard flag"
    elif current_open_order_count == 0 and recent_decisions_1h == 0:
        status = "deployed_no_trigger_no_recent_decisions_no_open_orders"
        interpretation = "no current open order condition and no recent directional decision sample to trigger the guard"
    elif current_open_order_count == 0:
        status = "deployed_no_trigger_no_current_open_orders"
        interpretation = "recent decisions did not have current open orders, so the guard condition was false"
    elif recent_decisions_1h == 0:
        status = "deployed_no_trigger_no_recent_decisions"
        interpretation = "open orders exist, but no recent directional decision sampled the guard path"
    else:
        status = "pending_open_orders_no_guard_hit"
        smallest_missing_field = TARGET_CONVERGENCE_GUARD_FLAG
        interpretation = "open orders and recent directional decisions exist, but no guard hit is visible yet"

    return {
        "status": status,
        "smallest_missing_field": smallest_missing_field,
        "guard_flag": raw.get("guard_flag") or TARGET_CONVERGENCE_GUARD_FLAG,
        "report_generated_at": report_generated_at,
        "deployed_matches_windows": deployed_matches_windows,
        "coverage": {
            "directional_decisions_total": int_or_zero(coverage.get("directional_decisions_total")),
            "directional_decisions_24h": int_or_zero(coverage.get("directional_decisions_24h")),
            "directional_decisions_1h": recent_decisions_1h,
            "guard_hits_total": guard_hits_total,
            "guard_hits_24h": guard_hits_24h,
            "guard_hits_1h": guard_hits_1h,
        },
        "current_open_orders": {
            "total_open_order_count": current_open_order_count,
            "execution_orders_open_order_count": execution_open_count,
            "execution_orders_directional_open_order_count": int_or_zero(
                execution_orders.get("directional_open_order_count")
            ),
            "execution_orders_states": execution_orders.get("states"),
            "execution_orders_oldest_open_order_created_at": execution_orders.get("oldest_open_order_created_at"),
            "execution_orders_latest_open_order_updated_at": execution_orders.get("latest_open_order_updated_at"),
            "legacy_order_states_open_order_count": legacy_open_count,
            "legacy_order_states_directional_open_order_count": int_or_zero(
                legacy_order_states.get("directional_open_order_count")
            ),
            "legacy_order_states_states": legacy_order_states.get("states"),
            "legacy_order_states_oldest_open_order_created_at": legacy_order_states.get(
                "oldest_open_order_created_at"
            ),
            "legacy_order_states_latest_open_order_updated_at": legacy_order_states.get(
                "latest_open_order_updated_at"
            ),
        },
        "latest_guard_hit": raw.get("latest_guard_hit"),
        "interpretation": interpretation,
    }


def directional_impulse_chase_guard_code_markers(repo_root: Path) -> dict[str, Any]:
    source_path = repo_root / "aats" / "services" / "decision_engine" / "target_position.py"
    if not source_path.exists():
        return {
            "source_path": str(source_path.relative_to(repo_root)) if source_path.is_absolute() else str(source_path),
            "source_file_present": False,
            "all_required_markers_present": False,
            "present_markers": [],
            "missing_markers": list(IMPULSE_CHASE_GUARD_CODE_MARKERS),
        }
    source = source_path.read_text(encoding="utf-8", errors="replace")
    present = [marker for marker in IMPULSE_CHASE_GUARD_CODE_MARKERS if marker in source]
    missing = [marker for marker in IMPULSE_CHASE_GUARD_CODE_MARKERS if marker not in source]
    return {
        "source_path": str(source_path.relative_to(repo_root)),
        "source_file_present": True,
        "all_required_markers_present": not missing,
        "present_markers": present,
        "missing_markers": missing,
    }


def summarize_directional_impulse_chase_guard_truth(
    db: dict[str, Any],
    git: dict[str, Any],
    code_markers: dict[str, Any],
    *,
    report_generated_at: str,
) -> dict[str, Any]:
    raw = as_dict(db.get("directional_impulse_chase_guard"))
    code_present = bool(code_markers.get("all_required_markers_present"))
    if not code_present:
        return {
            "status": "missing_guard_code_markers",
            "smallest_missing_field": "target_position.impulse_chase_guard_code_markers",
            "report_generated_at": report_generated_at,
            "guard_flags": list(IMPULSE_CHASE_GUARD_FLAGS),
            "code": code_markers,
            "interpretation": "source code does not contain the full deterministic impulse-chase guard marker set",
        }
    if not db.get("ok"):
        return {
            "status": "missing_database_truth",
            "smallest_missing_field": "database_truth",
            "report_generated_at": report_generated_at,
            "guard_flags": list(IMPULSE_CHASE_GUARD_FLAGS),
            "code": code_markers,
            "interpretation": "database probe did not return authoritative live guard-hit facts",
        }
    if not raw:
        return {
            "status": "missing_directional_impulse_chase_guard_probe",
            "smallest_missing_field": "database_truth.directional_impulse_chase_guard",
            "report_generated_at": report_generated_at,
            "guard_flags": list(IMPULSE_CHASE_GUARD_FLAGS),
            "code": code_markers,
            "interpretation": "runtime truth report lacks directional impulse-chase guard coverage",
        }

    coverage = as_dict(raw.get("coverage"))
    guard_hits_total = int_or_zero(coverage.get("guard_hits_total"))
    guard_hits_24h = int_or_zero(coverage.get("guard_hits_24h"))
    guard_hits_1h = int_or_zero(coverage.get("guard_hits_1h"))
    blocked_hits_total = int_or_zero(coverage.get("blocked_live_entry_hits_total"))
    blocked_hits_24h = int_or_zero(coverage.get("blocked_live_entry_hits_24h"))
    blocked_hits_1h = int_or_zero(coverage.get("blocked_live_entry_hits_1h"))
    recent_decisions_1h = int_or_zero(coverage.get("directional_decisions_1h"))
    deployed_matches_windows = git.get("deployed_matches_windows")

    status = "deployed_no_trigger_recent_directional_decisions"
    smallest_missing_field = None
    interpretation = "guard code is present, but no live directional decision has triggered the guard yet"
    if deployed_matches_windows is False:
        status = "deployment_mismatch_guard_truth_not_authoritative"
        smallest_missing_field = "deployed_head_matches_windows_head"
        interpretation = "runtime may not be running the local impulse-chase guard implementation"
    elif blocked_hits_total > 0:
        status = "verified_guard_blocked_live_directional_entry"
        interpretation = "at least one live directional decision was blocked by an impulse-chase guard flag"
    elif guard_hits_total > 0:
        status = "verified_guard_triggered"
        interpretation = "at least one live directional decision carries an impulse-chase guard flag"
    elif recent_decisions_1h == 0:
        status = "deployed_no_trigger_no_recent_directional_decisions"
        interpretation = "guard code is present, but no recent directional decision sampled the guard path"

    return {
        "status": status,
        "smallest_missing_field": smallest_missing_field,
        "report_generated_at": report_generated_at,
        "deployed_matches_windows": deployed_matches_windows,
        "guard_flags": list(raw.get("guard_flags") or IMPULSE_CHASE_GUARD_FLAGS),
        "code": code_markers,
        "coverage": {
            "directional_decisions_total": int_or_zero(coverage.get("directional_decisions_total")),
            "directional_decisions_24h": int_or_zero(coverage.get("directional_decisions_24h")),
            "directional_decisions_1h": recent_decisions_1h,
            "guard_hits_total": guard_hits_total,
            "guard_hits_24h": guard_hits_24h,
            "guard_hits_1h": guard_hits_1h,
            "blocked_live_entry_hits_total": blocked_hits_total,
            "blocked_live_entry_hits_24h": blocked_hits_24h,
            "blocked_live_entry_hits_1h": blocked_hits_1h,
        },
        "flag_hits_total": {
            str(flag): int_or_zero(count)
            for flag, count in as_dict(raw.get("flag_hits_total")).items()
        },
        "latest_guard_hit": raw.get("latest_guard_hit"),
        "interpretation": interpretation,
    }


def okx_hedge_scale_in_code_markers(repo_root: Path) -> dict[str, Any]:
    files: dict[str, dict[str, Any]] = {}
    missing_all: list[str] = []
    present_all: list[str] = []
    for relative_path, markers in OKX_HEDGE_SCALE_IN_CODE_MARKERS.items():
        source_path = repo_root / relative_path
        if not source_path.exists():
            files[relative_path] = {
                "source_file_present": False,
                "present_markers": [],
                "missing_markers": list(markers),
            }
            missing_all.extend(f"{relative_path}:{marker}" for marker in markers)
            continue
        source = source_path.read_text(encoding="utf-8", errors="replace")
        present = [marker for marker in markers if marker in source]
        missing = [marker for marker in markers if marker not in source]
        files[relative_path] = {
            "source_file_present": True,
            "present_markers": present,
            "missing_markers": missing,
        }
        present_all.extend(f"{relative_path}:{marker}" for marker in present)
        missing_all.extend(f"{relative_path}:{marker}" for marker in missing)
    return {
        "all_required_markers_present": not missing_all,
        "files": files,
        "present_markers": present_all,
        "missing_markers": missing_all,
    }


def summarize_okx_hedge_scale_in_intent_truth(
    db: dict[str, Any],
    git: dict[str, Any],
    code_markers: dict[str, Any],
    *,
    report_generated_at: str,
) -> dict[str, Any]:
    raw = as_dict(db.get("okx_hedge_scale_in_intent"))
    code_present = bool(code_markers.get("all_required_markers_present"))
    if not code_present:
        return {
            "status": "missing_okx_hedge_scale_in_code_markers",
            "smallest_missing_field": "execution_scale_in_intent_compatibility_code_markers",
            "report_generated_at": report_generated_at,
            "code": code_markers,
            "interpretation": "source code does not contain the full scale-in/open-leg compatibility marker set",
        }
    if not db.get("ok"):
        return {
            "status": "missing_database_truth",
            "smallest_missing_field": "database_truth",
            "report_generated_at": report_generated_at,
            "code": code_markers,
            "interpretation": "database probe did not return authoritative OKX hedge scale-in facts",
        }
    if not raw:
        return {
            "status": "missing_okx_hedge_scale_in_intent_probe",
            "smallest_missing_field": "database_truth.okx_hedge_scale_in_intent",
            "report_generated_at": report_generated_at,
            "code": code_markers,
            "interpretation": "runtime truth report lacks OKX hedge scale-in compatibility coverage",
        }

    history_counts = as_dict(raw.get("history_reason_counts"))
    execution_counts = as_dict(raw.get("execution_payload_reason_counts"))
    order_state_counts = as_dict(raw.get("order_state_payload_reason_counts"))
    open_scale_counts = as_dict(raw.get("open_scale_in_leg_counts"))

    mismatch_total = max(
        int_or_zero(history_counts.get("total")),
        int_or_zero(execution_counts.get("total")),
        int_or_zero(order_state_counts.get("total")),
    )
    mismatch_24h = max(
        int_or_zero(history_counts.get("last_24h")),
        int_or_zero(execution_counts.get("last_24h")),
        int_or_zero(order_state_counts.get("last_24h")),
    )
    mismatch_1h = max(
        int_or_zero(history_counts.get("last_1h")),
        int_or_zero(execution_counts.get("last_1h")),
        int_or_zero(order_state_counts.get("last_1h")),
    )
    open_scale_total = int_or_zero(open_scale_counts.get("total"))
    open_scale_24h = int_or_zero(open_scale_counts.get("last_24h"))
    open_scale_1h = int_or_zero(open_scale_counts.get("last_1h"))
    deployed_matches_windows = git.get("deployed_matches_windows")

    status = "verified_scale_in_open_leg_semantics"
    smallest_missing_field = None
    interpretation = "OKX hedge open-leg scale-in semantics are code-compatible and no mismatch is active"
    if deployed_matches_windows is False:
        status = "deployment_mismatch_scale_in_truth_not_authoritative"
        smallest_missing_field = "deployed_head_matches_windows_head"
        interpretation = "runtime may not be running the local scale-in compatibility implementation"
    elif mismatch_1h > 0:
        status = "active_scale_in_intent_mismatch_after_alignment"
        smallest_missing_field = "recent_okx_hedge_scale_in_mismatch_payload"
        interpretation = "scale-in/open-leg mismatch is still appearing in recent runtime payloads"
    elif mismatch_24h > 0 or mismatch_total > 0:
        status = "historical_scale_in_intent_mismatch_no_recent_hits"
        interpretation = "historical scale-in/open-leg mismatch rows exist, but no recent active hit is visible"
    elif open_scale_total == 0:
        status = "deployed_no_scale_in_samples"
        interpretation = "code is compatible, but no scale-in/open-leg runtime sample is present"

    latest_mismatches = as_list(raw.get("latest_mismatches"))
    latest_created_at = None
    if latest_mismatches and isinstance(latest_mismatches[0], dict):
        latest_created_at = latest_mismatches[0].get("created_at")

    return {
        "status": status,
        "smallest_missing_field": smallest_missing_field,
        "report_generated_at": report_generated_at,
        "deployed_matches_windows": deployed_matches_windows,
        "mismatch_reason": raw.get("mismatch_reason") or OKX_HEDGE_SCALE_IN_MISMATCH_REASON,
        "code": code_markers,
        "coverage": {
            "mismatch_total": mismatch_total,
            "mismatch_24h": mismatch_24h,
            "mismatch_1h": mismatch_1h,
            "open_scale_in_leg_total": open_scale_total,
            "open_scale_in_leg_24h": open_scale_24h,
            "open_scale_in_leg_1h": open_scale_1h,
            "history_reason_counts": history_counts,
            "execution_payload_reason_counts": execution_counts,
            "order_state_payload_reason_counts": order_state_counts,
            "open_scale_in_leg_counts": open_scale_counts,
        },
        "latest_mismatch_created_at": latest_created_at,
        "latest_mismatches": latest_mismatches,
        "interpretation": interpretation,
    }


def summarize_created_no_command_directional_order_truth(
    db: dict[str, Any],
    git: dict[str, Any],
    *,
    report_generated_at: str,
) -> dict[str, Any]:
    raw = as_dict(db.get("created_no_command_directional_order"))
    if not db.get("ok"):
        return {
            "status": "missing_database_truth",
            "smallest_missing_field": "database_truth",
            "root_cause": CREATED_NO_COMMAND_DIRECTIONAL_ROOT_CAUSE,
            "report_generated_at": report_generated_at,
            "interpretation": "database probe did not return authoritative created/no-command order facts",
        }
    if not raw:
        return {
            "status": "missing_created_no_command_directional_order_probe",
            "smallest_missing_field": "database_truth.created_no_command_directional_order",
            "root_cause": CREATED_NO_COMMAND_DIRECTIONAL_ROOT_CAUSE,
            "report_generated_at": report_generated_at,
            "interpretation": "runtime truth report lacks historical created/no-command order coverage",
        }

    execution_counts = as_dict(raw.get("execution_order_missing_submit_command_counts"))
    order_state_counts = as_dict(raw.get("order_state_missing_submit_command_counts"))
    execution_total = int_or_zero(execution_counts.get("total"))
    order_state_total = int_or_zero(order_state_counts.get("total"))
    missing_total = max(execution_total, order_state_total)
    missing_24h = max(
        int_or_zero(execution_counts.get("last_24h")),
        int_or_zero(order_state_counts.get("last_24h")),
    )
    missing_1h = max(
        int_or_zero(execution_counts.get("last_1h")),
        int_or_zero(order_state_counts.get("last_1h")),
    )
    deployed_matches_windows = git.get("deployed_matches_windows")

    status = "verified_no_created_no_command_directional_orders"
    smallest_missing_field = None
    interpretation = (
        "no current directional CREATED/SUBMITTING order without a submit command is present; "
        "historical recovery task is stale unless new runtime evidence appears"
    )
    if deployed_matches_windows is False:
        status = "deployment_mismatch_created_no_command_truth_not_authoritative"
        smallest_missing_field = "deployed_head_matches_windows_head"
        interpretation = "runtime may not be running the local created/no-command truth probe"
    elif missing_1h > 0:
        status = "active_created_no_command_directional_order"
        smallest_missing_field = CREATED_NO_COMMAND_DIRECTIONAL_ROOT_CAUSE
        interpretation = "a recent directional CREATED/SUBMITTING order still lacks a submit command"
    elif missing_total > 0:
        status = "historical_created_no_command_directional_order_still_present"
        smallest_missing_field = CREATED_NO_COMMAND_DIRECTIONAL_ROOT_CAUSE
        interpretation = (
            "a historical directional CREATED/SUBMITTING order without submit command remains in persisted state"
        )

    latest_created_at = first_present(
        execution_counts.get("latest_created_at"),
        order_state_counts.get("latest_created_at"),
    )
    return {
        "status": status,
        "smallest_missing_field": smallest_missing_field,
        "root_cause": raw.get("root_cause") or CREATED_NO_COMMAND_DIRECTIONAL_ROOT_CAUSE,
        "report_generated_at": report_generated_at,
        "deployed_matches_windows": deployed_matches_windows,
        "coverage": {
            "missing_total": missing_total,
            "missing_24h": missing_24h,
            "missing_1h": missing_1h,
            "execution_order_missing_total": execution_total,
            "execution_order_missing_24h": int_or_zero(execution_counts.get("last_24h")),
            "execution_order_missing_1h": int_or_zero(execution_counts.get("last_1h")),
            "order_state_missing_total": order_state_total,
            "order_state_missing_24h": int_or_zero(order_state_counts.get("last_24h")),
            "order_state_missing_1h": int_or_zero(order_state_counts.get("last_1h")),
            "latest_created_at": latest_created_at,
        },
        "latest_execution_order_rows": as_list(raw.get("latest_execution_order_rows")),
        "latest_order_state_rows": as_list(raw.get("latest_order_state_rows")),
        "interpretation": interpretation,
    }


def summarize_execution_order_payload_status_residual_truth(
    db: dict[str, Any],
    *,
    report_generated_at: str,
) -> dict[str, Any]:
    raw = as_dict(db.get("execution_order_payload_status_residual"))
    if not db.get("ok"):
        return {
            "status": "missing_database_truth",
            "smallest_missing_field": "database_truth",
            "report_generated_at": report_generated_at,
            "interpretation": "database probe did not return authoritative order status facts",
        }
    if not raw:
        return {
            "status": "missing_execution_order_payload_status_residual_probe",
            "smallest_missing_field": "database_truth.execution_order_payload_status_residual",
            "report_generated_at": report_generated_at,
            "interpretation": "runtime truth report lacks execution order payload status residual coverage",
        }

    authority = as_dict(raw.get("authority"))
    coverage = as_dict(raw.get("coverage"))
    top_level_mismatch_count = int_or_zero(coverage.get("top_level_status_mismatch_count"))
    nested_mismatch_count = int_or_zero(coverage.get("nested_status_mismatch_count"))
    terminal_column_nonterminal_top_level_count = int_or_zero(
        coverage.get("terminal_column_nonterminal_top_level_count")
    )
    open_column_terminal_top_level_count = int_or_zero(
        coverage.get("open_column_terminal_top_level_count")
    )
    open_by_column_count = int_or_zero(coverage.get("open_by_column_count"))
    open_by_top_level_raw_payload_count = int_or_zero(
        coverage.get("open_by_top_level_raw_payload_count")
    )
    terminal_column_nonterminal_nested_count = int_or_zero(
        coverage.get("terminal_column_nonterminal_nested_count")
    )
    open_column_terminal_nested_count = int_or_zero(
        coverage.get("open_column_terminal_nested_count")
    )
    target_order = as_dict(raw.get("target_order"))
    target_state = target_order.get("state")
    target_raw_status = target_order.get("raw_payload_status")
    target_nested_status = target_order.get("nested_order_state_status")
    target_top_level_mismatch = bool(target_order) and target_raw_status != target_state
    target_nested_matches_column = bool(target_order) and target_nested_status == target_state
    raw_payload_status_would_misclassify_open_orders = (
        open_by_top_level_raw_payload_count != open_by_column_count
    )

    status = "classified_non_authoritative_top_level_payload_status_residual"
    smallest_missing_field = None
    interpretation = (
        "execution_orders.state remains authoritative; top-level raw_payload.status is diagnostic "
        "and can be stale or missing on historical rows"
    )
    if open_column_terminal_top_level_count > 0:
        status = "potential_raw_payload_status_authority_conflict_requires_review"
        smallest_missing_field = "execution_orders.raw_payload.status.open_column_terminal_conflict"
        interpretation = (
            "at least one non-terminal execution_orders.state row carries terminal top-level raw payload status"
        )
    elif top_level_mismatch_count == 0 and nested_mismatch_count == 0:
        status = "verified_payload_status_layers_aligned"
        interpretation = "execution order column, top-level raw payload, and nested order_state statuses align"

    enriched_target_order: dict[str, Any] | None = None
    if target_order:
        enriched_target_order = {
            **target_order,
            "top_level_status_mismatch": target_top_level_mismatch,
            "nested_status_matches_column": target_nested_matches_column,
        }

    return {
        "status": status,
        "smallest_missing_field": smallest_missing_field,
        "report_generated_at": report_generated_at,
        "symbol": raw.get("symbol"),
        "authority": {
            "order_status_source": authority.get("order_status_source") or "execution_orders.state",
            "order_state_status_source": authority.get("order_state_status_source") or "order_states.status",
            "raw_payload_top_level_status_authoritative": False,
            "notes": as_list(authority.get("notes")),
        },
        "coverage": {
            "top_level_status_mismatch_count": top_level_mismatch_count,
            "nested_status_mismatch_count": nested_mismatch_count,
            "terminal_column_nonterminal_top_level_count": terminal_column_nonterminal_top_level_count,
            "open_column_terminal_top_level_count": open_column_terminal_top_level_count,
            "open_by_column_count": open_by_column_count,
            "open_by_top_level_raw_payload_count": open_by_top_level_raw_payload_count,
            "raw_payload_status_would_misclassify_open_orders": (
                raw_payload_status_would_misclassify_open_orders
            ),
            "terminal_column_nonterminal_nested_count": terminal_column_nonterminal_nested_count,
            "open_column_terminal_nested_count": open_column_terminal_nested_count,
        },
        "target_order": enriched_target_order,
        "latest_mismatch_rows": as_list(raw.get("latest_mismatch_rows")),
        "top_level_status_mismatch_groups": as_list(raw.get("top_level_status_mismatch_groups")),
        "nested_status_mismatch_groups": as_list(raw.get("nested_status_mismatch_groups")),
        "consumer_audit": [
            "open-order and claimed-submit recovery checks filter execution_orders.state/order_states.status",
            "exchange reconciler reads nested raw_payload.order_state before any raw payload fallback",
            "top-level raw_payload.status is exposed only as diagnostic truth-report context",
        ],
        "interpretation": interpretation,
    }


def summarize_microstructure_table(
    raw: dict[str, Any],
    *,
    report_generated_at: str,
    stale_after_seconds: int,
) -> dict[str, Any]:
    report_time = parse_utc_timestamp(report_generated_at)
    latest_ts = raw.get("max_ts")
    latest_time = parse_utc_timestamp(str(latest_ts)) if latest_ts is not None else None
    age_seconds = seconds_between(latest_time, report_time)
    exists = bool(raw.get("exists"))
    count = int_or_zero(raw.get("count"))
    if not exists:
        status = "missing_table"
    elif count <= 0:
        status = "empty_table"
    elif age_seconds is None:
        status = "latest_timestamp_unparseable"
    elif age_seconds > stale_after_seconds:
        status = "stale"
    else:
        status = "fresh"
    return {
        "exists": exists,
        "count": count,
        "latest_ts": latest_ts,
        "age_seconds": age_seconds,
        "stale_after_seconds": stale_after_seconds,
        "recent_window_minutes": raw.get("recent_window_minutes"),
        "recent_count": int_or_zero(raw.get("recent_count")),
        "status": status,
    }


def summarize_payload_sequence(
    raw: dict[str, Any],
    *,
    report_generated_at: str,
) -> dict[str, Any]:
    report_time = parse_utc_timestamp(report_generated_at)
    exists = bool(raw.get("exists"))
    scopes = [as_dict(item) for item in as_list(raw.get("scopes"))]
    capture_status_counts = [as_dict(item) for item in as_list(raw.get("capture_status_counts"))]
    total_rows = sum(int_or_zero(item.get("n")) for item in scopes)
    total_gap_count = sum(int_or_zero(item.get("sequence_gap_count")) for item in scopes)
    latest_status_time: datetime | None = None
    for item in capture_status_counts:
        max_ts = item.get("max_ts")
        candidate = parse_utc_timestamp(str(max_ts)) if max_ts is not None else None
        if candidate is not None and (latest_status_time is None or candidate > latest_status_time):
            latest_status_time = candidate
    age_seconds = seconds_between(latest_status_time, report_time)
    if not exists:
        status = "missing_table"
    elif total_rows <= 0:
        status = "no_recent_payloads"
    elif total_gap_count > 0:
        status = "sequence_gap_detected"
    elif age_seconds is None:
        status = "latest_timestamp_unparseable"
    elif age_seconds > ORDERBOOK_BRONZE_STALE_AFTER_SECONDS:
        status = "stale"
    else:
        status = "sequence_continuous"
    return {
        "exists": exists,
        "window_minutes": raw.get("window_minutes") or ORDERBOOK_PAYLOAD_SEQUENCE_WINDOW_MINUTES,
        "row_count": total_rows,
        "sequence_gap_count": total_gap_count,
        "latest_ts": latest_status_time.isoformat().replace("+00:00", "Z") if latest_status_time else None,
        "age_seconds": age_seconds,
        "status": status,
        "scopes": [
            {
                "collector_sequence_scope": item.get("collector_sequence_scope"),
                "ingest_run_id_prefix": item.get("ingest_run_id_prefix"),
                "channel": item.get("channel"),
                "row_count": int_or_zero(item.get("n")),
                "min_sequence": int_or_zero(item.get("min_seq")) if item.get("min_seq") is not None else None,
                "max_sequence": int_or_zero(item.get("max_seq")) if item.get("max_seq") is not None else None,
                "distinct_sequence_count": int_or_zero(item.get("distinct_n")),
                "sequence_gap_count": int_or_zero(item.get("sequence_gap_count")),
            }
            for item in scopes
        ],
        "capture_status_counts": [
            {
                "capture_status": item.get("capture_status"),
                "row_count": int_or_zero(item.get("n")),
                "max_ts": item.get("max_ts"),
            }
            for item in capture_status_counts
        ],
    }


def summarize_latest_silver_orderbook(
    raw: dict[str, Any] | None,
    table_summary: dict[str, Any],
) -> dict[str, Any]:
    latest = as_dict(raw)
    bbo_samples = int_or_zero(latest.get("bbo_samples_n"))
    books5_samples = int_or_zero(latest.get("books5_samples_n"))
    quality_flags = as_list(latest.get("quality_flags"))
    if table_summary.get("status") in {"missing_table", "empty_table"}:
        status = table_summary.get("status")
    elif table_summary.get("status") == "stale":
        status = "stale"
    elif bbo_samples <= 0:
        status = "missing_bbo_samples"
    elif books5_samples <= 0:
        status = "missing_books5_samples"
    elif quality_flags:
        status = "quality_flags_present"
    else:
        status = "verified_silver_orderbook_bar_present"
    return {
        "status": status,
        "latest_bar_ts": latest.get("ts") or table_summary.get("latest_ts"),
        "age_seconds": table_summary.get("age_seconds"),
        "stale_after_seconds": table_summary.get("stale_after_seconds"),
        "bbo_samples_n": bbo_samples,
        "books5_samples_n": books5_samples,
        "spread_bps_mean": decimal_text(latest.get("spread_bps_mean")),
        "spread_bps_max": decimal_text(latest.get("spread_bps_max")),
        "spread_bps_min": decimal_text(latest.get("spread_bps_min")),
        "mid_price_last": decimal_text(latest.get("mid_price_last")),
        "quality_flags": quality_flags,
    }


def summarize_latest_silver_trade_flow(
    raw: dict[str, Any] | None,
    table_summary: dict[str, Any],
) -> dict[str, Any]:
    latest = as_dict(raw)
    trade_count = int_or_zero(latest.get("trade_count"))
    quality_flags = as_list(latest.get("quality_flags"))
    if table_summary.get("status") in {"missing_table", "empty_table"}:
        status = table_summary.get("status")
    elif table_summary.get("status") == "stale":
        status = "stale"
    elif trade_count <= 0:
        status = "missing_trade_samples"
    elif quality_flags:
        status = "quality_flags_present"
    elif latest.get("vwap_minus_mid_bps") is None:
        status = "missing_vwap_minus_mid_bps"
    else:
        status = "verified_silver_trade_flow_bar_present"
    return {
        "status": status,
        "latest_bar_ts": latest.get("ts") or table_summary.get("latest_ts"),
        "age_seconds": table_summary.get("age_seconds"),
        "stale_after_seconds": table_summary.get("stale_after_seconds"),
        "trade_count": trade_count,
        "total_volume_ccy": decimal_text(latest.get("total_volume_ccy")),
        "taker_buy_ratio": decimal_text(latest.get("taker_buy_ratio")),
        "trade_flow_imbalance": decimal_text(latest.get("trade_flow_imbalance")),
        "vwap": decimal_text(latest.get("vwap")),
        "mid_price_ref": decimal_text(latest.get("mid_price_ref")),
        "vwap_minus_mid_bps": decimal_text(latest.get("vwap_minus_mid_bps")),
        "quality_flags": quality_flags,
    }


def summarize_orderbook_payload_depth_truth(
    raw: dict[str, Any],
    execution_science: dict[str, Any],
) -> dict[str, Any]:
    if not raw.get("ok"):
        return {
            "source": "rdp_microstructure.orderbook_payload_sidecar",
            "ok": False,
            "status": "rdp_microstructure_unavailable",
            "smallest_missing_field": "rdp_microstructure_probe",
            "raw_payload_exposed": False,
        }

    latest_payloads_raw = as_dict(raw.get("latest_orderbook_payloads"))
    payload_rows = [as_dict(row) for row in as_list(latest_payloads_raw.get("rows"))]
    payload_sequence = as_dict(execution_science.get("payload_sequence"))
    silver_orderbook = as_dict(execution_science.get("silver_orderbook"))
    scopes = [as_dict(item) for item in as_list(payload_sequence.get("scopes"))]
    capture_status_counts = [
        as_dict(item) for item in as_list(payload_sequence.get("capture_status_counts"))
    ]

    def _channel_row(channel: str) -> dict[str, Any]:
        return next((row for row in payload_rows if row.get("channel") == channel), {})

    def _channel_scope(channel: str) -> dict[str, Any]:
        return next((scope for scope in scopes if scope.get("channel") == channel), {})

    books5_payload = _channel_row("books5")
    bbo_payload = _channel_row("bbo-tbt") or _channel_row("bbo")
    books5_scope = _channel_scope("books5")
    bbo_scope = _channel_scope("bbo-tbt") or _channel_scope("bbo")
    diff_payload_count = sum(
        int_or_zero(item.get("row_count") or item.get("n"))
        for item in capture_status_counts
        if item.get("capture_status") == "diff_payload_persisted"
    )
    latest_payload_exists = bool(latest_payloads_raw.get("exists"))
    books5_sequence_gap_count = int_or_zero(books5_scope.get("sequence_gap_count"))
    bbo_sequence_gap_count = int_or_zero(bbo_scope.get("sequence_gap_count"))
    books5_row_count = int_or_zero(books5_scope.get("row_count"))
    bbo_row_count = int_or_zero(bbo_scope.get("row_count"))

    checks = [
        ("bronze.market_orderbook_payloads", latest_payload_exists),
        ("bronze.market_orderbook_payloads.latest_books5_payload", bool(books5_payload)),
        ("bronze.market_orderbook_payloads.books5_payload_hash", bool(books5_payload.get("payload_hash_present"))),
        ("bronze.market_orderbook_payloads.books5_row_checksum", bool(books5_payload.get("row_checksum_present"))),
        ("bronze.market_orderbook_payloads.books5_sequence", books5_row_count > 0),
        ("bronze.market_orderbook_payloads.books5_sequence_gap_count", books5_sequence_gap_count == 0),
        ("bronze.market_orderbook_payloads.diff_payload_persisted", diff_payload_count > 0),
        (
            "silver.market_orderbook_metrics_15m.books5_samples_n",
            int_or_zero(silver_orderbook.get("books5_samples_n")) > 0,
        ),
    ]
    smallest_missing = next((field for field, passed in checks if not passed), None)

    if smallest_missing is None:
        status = "verified_books5_payload_depth_evidence_present"
    elif bool(bbo_payload.get("payload_hash_present")) and bbo_row_count > 0:
        status = "top_of_book_payload_evidence_present_depth_not_verified"
    else:
        status = "missing_orderbook_payload_depth_evidence"

    return {
        "source": "rdp_microstructure.orderbook_payload_sidecar",
        "ok": True,
        "symbol": raw.get("symbol"),
        "status": status,
        "smallest_missing_field": smallest_missing,
        "raw_payload_exposed": False,
        "books5_payload": {
            "channel": books5_payload.get("channel"),
            "snapshot_table": books5_payload.get("snapshot_table"),
            "storage_table": books5_payload.get("storage_table"),
            "ts": books5_payload.get("ts"),
            "source_ts": books5_payload.get("source_ts"),
            "received_at": books5_payload.get("received_at"),
            "collector_sequence": books5_payload.get("collector_sequence"),
            "collector_sequence_scope": books5_payload.get("collector_sequence_scope"),
            "ingest_run_id_prefix": books5_payload.get("ingest_run_id_prefix"),
            "row_checksum_present": bool(books5_payload.get("row_checksum_present")),
            "checksum_version": books5_payload.get("checksum_version"),
            "capture_status": books5_payload.get("capture_status"),
            "payload_hash_present": bool(books5_payload.get("payload_hash_present")),
            "payload_schema_version": books5_payload.get("payload_schema_version"),
            "payload_kind": books5_payload.get("payload_kind"),
            "exchange_sequence_id_present": bool(books5_payload.get("exchange_sequence_id_present")),
            "previous_payload_hash_present": bool(books5_payload.get("previous_payload_hash_present")),
        },
        "bbo_payload": {
            "channel": bbo_payload.get("channel"),
            "snapshot_table": bbo_payload.get("snapshot_table"),
            "storage_table": bbo_payload.get("storage_table"),
            "ts": bbo_payload.get("ts"),
            "source_ts": bbo_payload.get("source_ts"),
            "received_at": bbo_payload.get("received_at"),
            "collector_sequence": bbo_payload.get("collector_sequence"),
            "collector_sequence_scope": bbo_payload.get("collector_sequence_scope"),
            "ingest_run_id_prefix": bbo_payload.get("ingest_run_id_prefix"),
            "row_checksum_present": bool(bbo_payload.get("row_checksum_present")),
            "checksum_version": bbo_payload.get("checksum_version"),
            "capture_status": bbo_payload.get("capture_status"),
            "payload_hash_present": bool(bbo_payload.get("payload_hash_present")),
            "payload_schema_version": bbo_payload.get("payload_schema_version"),
            "payload_kind": bbo_payload.get("payload_kind"),
            "exchange_sequence_id_present": bool(bbo_payload.get("exchange_sequence_id_present")),
            "previous_payload_hash_present": bool(bbo_payload.get("previous_payload_hash_present")),
        },
        "sequence": {
            "status": payload_sequence.get("status"),
            "window_minutes": payload_sequence.get("window_minutes"),
            "books5_row_count": books5_row_count,
            "books5_sequence_gap_count": books5_sequence_gap_count,
            "bbo_row_count": bbo_row_count,
            "bbo_sequence_gap_count": bbo_sequence_gap_count,
            "diff_payload_persisted_row_count": diff_payload_count,
        },
        "silver_orderbook": {
            "status": silver_orderbook.get("status"),
            "latest_bar_ts": silver_orderbook.get("latest_bar_ts"),
            "books5_samples_n": silver_orderbook.get("books5_samples_n"),
            "bbo_samples_n": silver_orderbook.get("bbo_samples_n"),
            "spread_bps_mean": silver_orderbook.get("spread_bps_mean"),
        },
        "interpretation": {
            "raw_payload_exposed": False,
            "depth_readiness": (
                "books5 sidecar payload hash/checksum plus continuous collector sequence can support "
                "depth-aware fill-feasibility projections"
            ),
            "not_alpha_or_profitability_evidence": True,
        },
    }


def summarize_execution_science_truth(
    raw: dict[str, Any],
    *,
    report_generated_at: str,
) -> dict[str, Any]:
    if not raw.get("ok"):
        return {
            "source": "rdp_microstructure",
            "ok": False,
            "status": "rdp_microstructure_unavailable",
            "reason": raw.get("reason"),
            "smallest_missing_field": "rdp_microstructure_probe",
        }
    tables = raw.get("tables") if isinstance(raw.get("tables"), dict) else {}
    bbo = summarize_microstructure_table(
        as_dict(tables.get("bronze.market_orderbook_bbo")),
        report_generated_at=report_generated_at,
        stale_after_seconds=ORDERBOOK_BRONZE_STALE_AFTER_SECONDS,
    )
    books5 = summarize_microstructure_table(
        as_dict(tables.get("bronze.market_orderbook_books5")),
        report_generated_at=report_generated_at,
        stale_after_seconds=ORDERBOOK_BRONZE_STALE_AFTER_SECONDS,
    )
    payload_table = summarize_microstructure_table(
        as_dict(tables.get("bronze.market_orderbook_payloads")),
        report_generated_at=report_generated_at,
        stale_after_seconds=ORDERBOOK_BRONZE_STALE_AFTER_SECONDS,
    )
    silver_orderbook_table = summarize_microstructure_table(
        as_dict(tables.get("silver.market_orderbook_metrics_15m")),
        report_generated_at=report_generated_at,
        stale_after_seconds=ORDERBOOK_SILVER_STALE_AFTER_SECONDS,
    )
    trade_flow_table = summarize_microstructure_table(
        as_dict(tables.get("silver.market_trade_flow_15m")),
        report_generated_at=report_generated_at,
        stale_after_seconds=ORDERBOOK_SILVER_STALE_AFTER_SECONDS,
    )
    sequence = summarize_payload_sequence(
        as_dict(raw.get("payload_sequence")),
        report_generated_at=report_generated_at,
    )
    silver_orderbook = summarize_latest_silver_orderbook(
        raw.get("latest_silver_orderbook") if isinstance(raw.get("latest_silver_orderbook"), dict) else None,
        silver_orderbook_table,
    )
    silver_trade_flow = summarize_latest_silver_trade_flow(
        raw.get("latest_silver_trade_flow") if isinstance(raw.get("latest_silver_trade_flow"), dict) else None,
        trade_flow_table,
    )
    missing_checks = [
        ("bronze.market_orderbook_bbo", bbo.get("status") == "fresh"),
        ("bronze.market_orderbook_books5", books5.get("status") == "fresh"),
        ("bronze.market_orderbook_payloads.collector_sequence", sequence.get("status") == "sequence_continuous"),
        (
            "silver.market_orderbook_metrics_15m",
            silver_orderbook.get("status") == "verified_silver_orderbook_bar_present",
        ),
    ]
    smallest_missing = next((field for field, passed in missing_checks if not passed), None)
    status = (
        "verified_orderbook_sequence_and_silver_bar_present"
        if smallest_missing is None
        else "missing_execution_science_evidence"
    )
    return {
        "source": "rdp_microstructure",
        "ok": True,
        "symbol": raw.get("symbol"),
        "status": status,
        "smallest_missing_field": smallest_missing,
        "bronze_orderbook": {
            "bbo": bbo,
            "books5": books5,
            "payloads": payload_table,
        },
        "payload_sequence": sequence,
        "silver_orderbook": silver_orderbook,
        "silver_trade_flow": silver_trade_flow,
        "workflow": raw.get("workflow") if isinstance(raw.get("workflow"), dict) else {},
        "fill_feasibility_truth_status": (
            "verified_preorder_orderbook_features_available"
            if smallest_missing is None
            else "blocked_missing_orderbook_truth"
        ),
    }


def summarize_microstructure_runtime_growth_truth(
    collector: dict[str, Any],
    raw: dict[str, Any],
    execution_science: dict[str, Any],
    *,
    report_generated_at: str,
) -> dict[str, Any]:
    if not raw.get("ok"):
        return {
            "source": "microstructure_collector_and_rdp",
            "ok": False,
            "status": "rdp_microstructure_unavailable",
            "smallest_missing_field": "rdp_microstructure_probe",
            "raw_payload_exposed": False,
            "reason": raw.get("reason"),
        }

    tables = raw.get("tables") if isinstance(raw.get("tables"), dict) else {}

    def _table(name: str, stale_after_seconds: int) -> dict[str, Any]:
        return summarize_microstructure_table(
            as_dict(tables.get(name)),
            report_generated_at=report_generated_at,
            stale_after_seconds=stale_after_seconds,
        )

    bronze_tables = {
        "market_trades": _table("bronze.market_trades", ORDERBOOK_BRONZE_STALE_AFTER_SECONDS),
        "market_orderbook_bbo": _table(
            "bronze.market_orderbook_bbo",
            ORDERBOOK_BRONZE_STALE_AFTER_SECONDS,
        ),
        "market_orderbook_books5": _table(
            "bronze.market_orderbook_books5",
            ORDERBOOK_BRONZE_STALE_AFTER_SECONDS,
        ),
        "market_orderbook_payloads": _table(
            "bronze.market_orderbook_payloads",
            ORDERBOOK_BRONZE_STALE_AFTER_SECONDS,
        ),
    }
    silver_tables = {
        "market_liquidation_metrics_15m": _table(
            "silver.market_liquidation_metrics_15m",
            ORDERBOOK_SILVER_STALE_AFTER_SECONDS,
        ),
        "market_oi_funding_metrics_15m": _table(
            "silver.market_oi_funding_metrics_15m",
            ORDERBOOK_SILVER_STALE_AFTER_SECONDS,
        ),
        "market_orderbook_metrics_15m": _table(
            "silver.market_orderbook_metrics_15m",
            ORDERBOOK_SILVER_STALE_AFTER_SECONDS,
        ),
        "market_trade_flow_15m": _table(
            "silver.market_trade_flow_15m",
            ORDERBOOK_SILVER_STALE_AFTER_SECONDS,
        ),
        "market_volume_profile_15m": _table(
            "silver.market_volume_profile_15m",
            ORDERBOOK_SILVER_STALE_AFTER_SECONDS,
        ),
    }

    workflow = as_dict(raw.get("workflow"))
    latest_task = as_dict(workflow.get("latest_task"))
    report_time = parse_utc_timestamp(report_generated_at)
    workflow_seen = latest_task.get("max_seen")
    workflow_seen_time = parse_utc_timestamp(str(workflow_seen)) if workflow_seen is not None else None
    workflow_age_seconds = seconds_between(workflow_seen_time, report_time)
    latest_done = latest_task.get("status") == "done" and int_or_zero(latest_task.get("exit_code")) == 0
    workflow_recent = (
        workflow_age_seconds is not None
        and workflow_age_seconds <= MICROSTRUCTURE_WORKFLOW_STALE_AFTER_SECONDS
    )
    if latest_done and workflow_recent:
        workflow_status = "latest_done_recent"
    elif latest_done:
        workflow_status = "latest_done_stale"
    elif latest_task:
        workflow_status = "latest_not_done"
    else:
        workflow_status = "missing_latest_task"

    heartbeat = as_dict(collector.get("heartbeat"))
    payload_sequence = as_dict(execution_science.get("payload_sequence"))
    bronze_growth = {
        key: {
            "fresh": value.get("status") == "fresh",
            "recent_rows": int_or_zero(value.get("recent_count")),
            "recent_window_minutes": value.get("recent_window_minutes"),
            "latest_ts": value.get("latest_ts"),
            "age_seconds": value.get("age_seconds"),
            "status": value.get("status"),
        }
        for key, value in bronze_tables.items()
    }
    silver_update = {
        key: {
            "fresh": value.get("status") == "fresh",
            "latest_ts": value.get("latest_ts"),
            "age_seconds": value.get("age_seconds"),
            "status": value.get("status"),
        }
        for key, value in silver_tables.items()
    }

    checks = [
        ("microstructure_ws_daemon.running", bool(collector.get("running"))),
        ("microstructure_ws_daemon.healthy", bool(collector.get("healthy"))),
        ("microstructure_ws_daemon.script", bool(collector.get("daemon_script_detected"))),
        ("microstructure_ws_daemon.heartbeat", bool(heartbeat.get("fresh"))),
        (
            "bronze.market_trades.recent_growth",
            bronze_growth["market_trades"]["fresh"]
            and bronze_growth["market_trades"]["recent_rows"] > 0,
        ),
        (
            "bronze.market_orderbook_bbo.recent_growth",
            bronze_growth["market_orderbook_bbo"]["fresh"]
            and bronze_growth["market_orderbook_bbo"]["recent_rows"] > 0,
        ),
        (
            "bronze.market_orderbook_books5.recent_growth",
            bronze_growth["market_orderbook_books5"]["fresh"]
            and bronze_growth["market_orderbook_books5"]["recent_rows"] > 0,
        ),
        (
            "bronze.market_orderbook_payloads.recent_growth",
            bronze_growth["market_orderbook_payloads"]["fresh"]
            and bronze_growth["market_orderbook_payloads"]["recent_rows"] > 0,
        ),
        ("bronze.market_orderbook_payloads.sequence", payload_sequence.get("status") == "sequence_continuous"),
        ("governance.microstructure_silver_15m.latest_done", latest_done),
        ("governance.microstructure_silver_15m.recent", workflow_recent),
        *[
            (f"silver.{name}.fresh", value["fresh"])
            for name, value in silver_update.items()
        ],
    ]
    smallest_missing = next((field for field, passed in checks if not passed), None)
    if smallest_missing is None:
        status = "verified_microstructure_runtime_growth"
    elif not bool(collector.get("running")) or not bool(heartbeat.get("fresh")):
        status = "collector_not_fresh"
    elif any(not item["fresh"] or item["recent_rows"] <= 0 for item in bronze_growth.values()):
        status = "bronze_not_growing"
    elif not latest_done or not workflow_recent:
        status = "silver_workflow_not_recent_done"
    elif any(not item["fresh"] for item in silver_update.values()):
        status = "silver_not_fresh"
    else:
        status = "microstructure_growth_incomplete"

    return {
        "source": "microstructure_collector_and_rdp",
        "ok": smallest_missing is None,
        "symbol": raw.get("symbol"),
        "status": status,
        "smallest_missing_field": smallest_missing,
        "raw_payload_exposed": False,
        "collector": {
            "container": collector.get("container"),
            "status": collector.get("status"),
            "running": bool(collector.get("running")),
            "healthy": bool(collector.get("healthy")),
            "daemon_script_detected": bool(collector.get("daemon_script_detected")),
            "heartbeat_exists": bool(heartbeat.get("exists")),
            "heartbeat_fresh": bool(heartbeat.get("fresh")),
            "heartbeat_age_seconds": heartbeat.get("age_seconds"),
            "heartbeat_stale_after_seconds": heartbeat.get("stale_after_seconds"),
            "heartbeat_mtime_utc": heartbeat.get("mtime_utc"),
        },
        "bronze_growth": bronze_growth,
        "payload_sequence": {
            "status": payload_sequence.get("status"),
            "window_minutes": payload_sequence.get("window_minutes"),
            "row_count": payload_sequence.get("row_count"),
            "sequence_gap_count": payload_sequence.get("sequence_gap_count"),
            "latest_ts": payload_sequence.get("latest_ts"),
            "age_seconds": payload_sequence.get("age_seconds"),
        },
        "silver_workflow": {
            "status": workflow_status,
            "latest_task_id": latest_task.get("task_id"),
            "latest_task_status": latest_task.get("status"),
            "latest_exit_code": latest_task.get("exit_code"),
            "latest_seen_at": workflow_seen,
            "latest_age_seconds": workflow_age_seconds,
            "stale_after_seconds": MICROSTRUCTURE_WORKFLOW_STALE_AFTER_SECONDS,
            "active_count": int_or_zero(workflow.get("active_count")),
        },
        "silver_update": silver_update,
        "interpretation": {
            "evidence_scope": "collector_heartbeat_plus_rdp_bronze_and_silver_freshness",
            "not_alpha_or_profitability_evidence": True,
            "raw_payload_exposed": False,
        },
    }


def summarize_slippage_cost_calibration_truth(
    db: dict[str, Any],
    execution_science: dict[str, Any],
    *,
    report_generated_at: str,
) -> dict[str, Any]:
    if not db.get("ok"):
        return {
            "source": "live_db_and_rdp_microstructure",
            "ok": False,
            "status": "live_db_unavailable",
            "smallest_missing_field": "database_truth",
        }
    raw = as_dict(db.get("slippage_cost_calibration"))
    report_time = parse_utc_timestamp(report_generated_at)
    latest_fill_ts = raw.get("latest_fill_ts")
    latest_fill_time = parse_utc_timestamp(str(latest_fill_ts)) if latest_fill_ts is not None else None
    latest_fill_age_seconds = seconds_between(latest_fill_time, report_time)
    fills_total = int_or_zero(raw.get("fills_total"))
    fee_samples = int_or_zero(raw.get("fee_bps_samples"))
    slippage_samples = int_or_zero(raw.get("slippage_proxy_samples"))
    silver_orderbook = as_dict(execution_science.get("silver_orderbook"))
    silver_trade_flow = as_dict(execution_science.get("silver_trade_flow"))
    silver_orderbook_ok = silver_orderbook.get("status") == "verified_silver_orderbook_bar_present"
    silver_trade_flow_ok = silver_trade_flow.get("status") == "verified_silver_trade_flow_bar_present"

    missing_checks = [
        ("execution_fills", fills_total > 0),
        ("execution_fills.actual_fee_bps", fee_samples > 0),
        ("order_or_command_reference_price_for_slippage_proxy", slippage_samples > 0),
        ("silver.market_orderbook_metrics_15m", silver_orderbook_ok),
        ("silver.market_trade_flow_15m", silver_trade_flow_ok),
    ]
    smallest_missing = next((field for field, passed in missing_checks if not passed), None)
    if fills_total <= 0:
        status = "no_live_fill_samples"
    elif fee_samples > 0 and slippage_samples <= 0:
        status = "partial_fee_verified_slippage_proxy_missing"
    elif smallest_missing is None:
        status = "verified_slippage_cost_calibration_evidence_present"
    else:
        status = "missing_slippage_cost_calibration_evidence"

    reference_coverage_rows = []
    for row in [as_dict(item) for item in as_list(raw.get("by_reference_coverage_path"))]:
        reference_coverage_rows.append(
            {
                "coverage": row.get("coverage"),
                "source_system": row.get("source_system"),
                "order_type": row.get("order_type"),
                "time_in_force": row.get("time_in_force"),
                "execution_style": row.get("execution_style"),
                "strategy_family": row.get("strategy_family"),
                "order_state": row.get("order_state"),
                "command_presence": row.get("command_presence"),
                "command_reference_presence": row.get("command_reference_presence"),
                "submit_command_states": row.get("submit_command_states"),
                "row_count": int_or_zero(row.get("n")),
                "order_count": int_or_zero(row.get("order_count")),
                "first_order_created_at": row.get("first_order_created_at"),
                "last_order_created_at": row.get("last_order_created_at"),
                "first_fill_ingestion_ts": row.get("first_fill_ingestion_ts"),
                "last_fill_ingestion_ts": row.get("last_fill_ingestion_ts"),
            }
        )
    missing_reference_fills = sum(
        int_or_zero(row.get("row_count")) for row in reference_coverage_rows if row.get("coverage") == "missing"
    )
    missing_reference_fills_with_submit_command = sum(
        int_or_zero(row.get("row_count"))
        for row in reference_coverage_rows
        if row.get("coverage") == "missing" and row.get("command_presence") == "has_submit_command"
    )
    missing_reference_fills_without_submit_command = sum(
        int_or_zero(row.get("row_count"))
        for row in reference_coverage_rows
        if row.get("coverage") == "missing" and row.get("command_presence") == "no_submit_command"
    )
    covered_reference_fills_with_command_reference = sum(
        int_or_zero(row.get("row_count"))
        for row in reference_coverage_rows
        if row.get("coverage") == "covered" and row.get("command_reference_presence") == "command_has_reference"
    )
    if missing_reference_fills <= 0 and slippage_samples > 0:
        reference_coverage_classification = "all_fills_have_reference_price"
    elif missing_reference_fills_with_submit_command > 0:
        reference_coverage_classification = "current_command_path_reference_gap_possible"
    elif missing_reference_fills_without_submit_command > 0 and covered_reference_fills_with_command_reference > 0:
        reference_coverage_classification = "missing_reference_price_coverage_is_no_submit_command_path"
    elif missing_reference_fills_without_submit_command > 0:
        reference_coverage_classification = "missing_reference_price_coverage_no_submit_command_no_current_coverage"
    else:
        reference_coverage_classification = "reference_price_coverage_unknown"
    deterministic_backfill_status = "not_required"
    deterministic_backfill_reason = None
    deterministic_backfill_fill_count = 0
    deterministic_backfill_mutates_database = False
    if reference_coverage_classification == "missing_reference_price_coverage_is_no_submit_command_path":
        deterministic_backfill_status = "blocked_no_persisted_pretrade_reference_price"
        deterministic_backfill_reason = (
            "historical no-submit-command fills have no persisted order, order_state, or execution_command "
            "pre-trade reference price; reference must not be inferred from fill or post-trade prices"
        )
        deterministic_backfill_fill_count = missing_reference_fills_without_submit_command
    elif reference_coverage_classification == "missing_reference_price_coverage_no_submit_command_no_current_coverage":
        deterministic_backfill_status = "blocked_no_command_path_reference_coverage"
        deterministic_backfill_reason = (
            "missing no-submit-command fills exist and no current command-reference covered fills are available "
            "to prove the forward collection path"
        )
        deterministic_backfill_fill_count = missing_reference_fills_without_submit_command
    elif reference_coverage_classification == "current_command_path_reference_gap_possible":
        deterministic_backfill_status = "blocked_current_command_path_reference_gap"
        deterministic_backfill_reason = (
            "fills with submit commands still lack persisted pre-trade reference prices; fix the live command "
            "truth path before attempting historical classification"
        )
        deterministic_backfill_fill_count = missing_reference_fills_with_submit_command

    return {
        "source": "live_db_and_rdp_microstructure",
        "ok": True,
        "symbol": raw.get("symbol"),
        "status": status,
        "smallest_missing_field": smallest_missing,
        "latest_fill_ts": latest_fill_ts,
        "latest_fill_age_seconds": latest_fill_age_seconds,
        "fills_total": fills_total,
        "fills_24h": int_or_zero(raw.get("fills_24h")),
        "fills_with_order": int_or_zero(raw.get("fills_with_order")),
        "fills_with_limit_price": int_or_zero(raw.get("fills_with_limit_price")),
        "fills_with_order_intent_limit_price": int_or_zero(raw.get("fills_with_order_intent_limit_price")),
        "fills_with_order_intent_reference_price": int_or_zero(raw.get("fills_with_order_intent_reference_price")),
        "fills_with_order_state_reference_price": int_or_zero(raw.get("fills_with_order_state_reference_price")),
        "fills_with_order_state_submission_reference_price": int_or_zero(
            raw.get("fills_with_order_state_submission_reference_price")
        ),
        "fills_with_command_intent_limit_price": int_or_zero(raw.get("fills_with_command_intent_limit_price")),
        "fills_with_command_intent_reference_price": int_or_zero(
            raw.get("fills_with_command_intent_reference_price")
        ),
        "fills_with_slippage_reference_price": int_or_zero(raw.get("fills_with_slippage_reference_price")),
        "fee": {
            "sample_count": fee_samples,
            "min_bps": decimal_text(raw.get("fee_bps_min")),
            "mean_bps": decimal_text(raw.get("fee_bps_mean")),
            "p95_bps": decimal_text(raw.get("fee_bps_p95")),
            "max_bps": decimal_text(raw.get("fee_bps_max")),
            "fee_rate_sample_count": int_or_zero(raw.get("fee_rate_samples")),
            "liquidity_role_sample_count": int_or_zero(raw.get("liquidity_role_samples")),
            "maker_fills": int_or_zero(raw.get("maker_fills")),
            "taker_fills": int_or_zero(raw.get("taker_fills")),
            "unknown_liquidity_fills": int_or_zero(raw.get("unknown_liquidity_fills")),
            "by_liquidity_role": [
                {
                    "liquidity_role": item.get("liquidity_role"),
                    "row_count": int_or_zero(item.get("n")),
                }
                for item in [as_dict(row) for row in as_list(raw.get("by_liquidity_role"))]
            ],
        },
        "slippage_proxy": {
            "sample_count": slippage_samples,
            "reference": "coalesced_order_or_command_reference_price",
            "min_bps": decimal_text(raw.get("slippage_proxy_min")),
            "mean_bps": decimal_text(raw.get("slippage_proxy_mean")),
            "p95_bps": decimal_text(raw.get("slippage_proxy_p95")),
            "max_bps": decimal_text(raw.get("slippage_proxy_max")),
            "by_reference_source": [
                {
                    "reference_source": item.get("reference_source"),
                    "row_count": int_or_zero(item.get("n")),
                }
                for item in [as_dict(row) for row in as_list(raw.get("by_reference_source"))]
            ],
            "coverage_audit": {
                "classification": reference_coverage_classification,
                "missing_reference_fills": missing_reference_fills,
                "missing_reference_fills_with_submit_command": missing_reference_fills_with_submit_command,
                "missing_reference_fills_without_submit_command": missing_reference_fills_without_submit_command,
                "covered_reference_fills_with_command_reference": covered_reference_fills_with_command_reference,
                "deterministic_backfill_status": deterministic_backfill_status,
                "deterministic_backfill_reason": deterministic_backfill_reason,
                "deterministic_backfill_fill_count": deterministic_backfill_fill_count,
                "deterministic_backfill_mutates_database": deterministic_backfill_mutates_database,
                "reference_policy": "pretrade_order_or_command_reference_only",
                "by_order_path": reference_coverage_rows,
            },
        },
        "market_context": {
            "silver_orderbook_status": silver_orderbook.get("status"),
            "silver_orderbook_spread_bps_mean": silver_orderbook.get("spread_bps_mean"),
            "silver_trade_flow_status": silver_trade_flow.get("status"),
            "silver_trade_flow_vwap_minus_mid_bps": silver_trade_flow.get("vwap_minus_mid_bps"),
            "silver_trade_flow_trade_count": silver_trade_flow.get("trade_count"),
        },
    }


def summarize_directional_command_flow_provenance_truth(
    slippage_cost_calibration: dict[str, Any],
) -> dict[str, Any]:
    slippage_proxy = as_dict(slippage_cost_calibration.get("slippage_proxy"))
    coverage_audit = as_dict(slippage_proxy.get("coverage_audit"))
    rows = [
        as_dict(row)
        for row in as_list(coverage_audit.get("by_order_path"))
        if as_dict(row).get("strategy_family") == "directional"
    ]
    if not rows:
        return {
            "source": "slippage_cost_calibration.coverage_audit",
            "status": "no_directional_fill_samples",
            "smallest_missing_field": "directional_execution_fills",
            "current_command_path_reference_gap": False,
            "coverage": {
                "directional_fill_count": 0,
                "directional_order_count": 0,
                "current_submit_command_fill_count": 0,
                "current_submit_command_reference_covered_fill_count": 0,
                "current_submit_command_reference_missing_fill_count": 0,
                "historical_no_submit_command_fill_count": 0,
                "historical_no_submit_command_reference_missing_fill_count": 0,
            },
            "by_order_path": [],
        }

    normalized_rows = []
    directional_fill_count = 0
    directional_order_count = 0
    current_submit_command_fill_count = 0
    current_submit_command_reference_covered_fill_count = 0
    current_submit_command_reference_missing_fill_count = 0
    historical_no_submit_command_fill_count = 0
    historical_no_submit_command_reference_missing_fill_count = 0
    for row in rows:
        row_count = int_or_zero(row.get("row_count"))
        order_count = int_or_zero(row.get("order_count"))
        command_presence = row.get("command_presence")
        coverage = row.get("coverage")
        directional_fill_count += row_count
        directional_order_count += order_count
        if command_presence == "has_submit_command":
            current_submit_command_fill_count += row_count
            if coverage == "missing":
                current_submit_command_reference_missing_fill_count += row_count
            elif row.get("command_reference_presence") == "command_has_reference":
                current_submit_command_reference_covered_fill_count += row_count
        elif command_presence == "no_submit_command":
            historical_no_submit_command_fill_count += row_count
            if coverage == "missing":
                historical_no_submit_command_reference_missing_fill_count += row_count
        normalized_rows.append(
            {
                "coverage": coverage,
                "source_system": row.get("source_system"),
                "order_type": row.get("order_type"),
                "time_in_force": row.get("time_in_force"),
                "execution_style": row.get("execution_style"),
                "order_state": row.get("order_state"),
                "command_presence": command_presence,
                "command_reference_presence": row.get("command_reference_presence"),
                "submit_command_states": row.get("submit_command_states"),
                "row_count": row_count,
                "order_count": order_count,
                "first_order_created_at": row.get("first_order_created_at"),
                "last_order_created_at": row.get("last_order_created_at"),
                "first_fill_ingestion_ts": row.get("first_fill_ingestion_ts"),
                "last_fill_ingestion_ts": row.get("last_fill_ingestion_ts"),
            }
        )

    current_command_path_reference_gap = current_submit_command_reference_missing_fill_count > 0
    if current_command_path_reference_gap:
        status = "current_directional_command_flow_reference_gap"
        smallest_missing = "current_directional_submit_command_reference_price"
    elif (
        current_submit_command_fill_count > 0
        and current_submit_command_reference_covered_fill_count > 0
    ):
        status = "verified_current_directional_command_flow_fill_provenance_present"
        smallest_missing = None
    elif historical_no_submit_command_fill_count > 0:
        status = "historical_directional_no_submit_command_only"
        smallest_missing = "current_directional_submit_command_fills"
    else:
        status = "missing_directional_command_flow_provenance_evidence"
        smallest_missing = "directional_submit_command_or_order_path"

    return {
        "source": "slippage_cost_calibration.coverage_audit",
        "status": status,
        "smallest_missing_field": smallest_missing,
        "current_command_path_reference_gap": current_command_path_reference_gap,
        "coverage": {
            "directional_fill_count": directional_fill_count,
            "directional_order_count": directional_order_count,
            "current_submit_command_fill_count": current_submit_command_fill_count,
            "current_submit_command_reference_covered_fill_count": (
                current_submit_command_reference_covered_fill_count
            ),
            "current_submit_command_reference_missing_fill_count": (
                current_submit_command_reference_missing_fill_count
            ),
            "historical_no_submit_command_fill_count": historical_no_submit_command_fill_count,
            "historical_no_submit_command_reference_missing_fill_count": (
                historical_no_submit_command_reference_missing_fill_count
            ),
        },
        "coverage_classification": coverage_audit.get("classification"),
        "reference_policy": coverage_audit.get("reference_policy"),
        "by_order_path": normalized_rows,
    }


def summarize_directional_episode_attribution_truth(
    db: dict[str, Any],
    rdp_microstructure: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not db.get("ok"):
        return {
            "source": "live_db",
            "ok": False,
            "status": "live_db_unavailable",
            "smallest_missing_field": "database_truth",
        }

    raw = as_dict(db.get("directional_episode_attribution"))
    recent = [as_dict(item) for item in as_list(raw.get("recent_decisions"))]
    recent, pretrade_microstructure = enrich_directional_episodes_with_microstructure(
        recent,
        rdp_microstructure=rdp_microstructure,
    )
    recent_count = len(recent)
    decisions_with_edge_cost = sum(
        1
        for item in recent
        if item.get("expected_edge_bps") is not None and item.get("expected_cost_bps") is not None
    )
    decisions_with_orders = sum(1 for item in recent if int_or_zero(as_dict(item.get("order")).get("count")) > 0)
    decisions_with_no_order_expected = sum(
        1 for item in recent if as_dict(item.get("order_expectation")).get("no_order_expected") is True
    )
    no_order_semantics_records = [
        as_dict(item.get("no_order_semantics"))
        for item in recent
        if as_dict(item.get("order_expectation")).get("no_order_expected") is True
    ]
    decisions_with_no_order_semantics = sum(
        1
        for item in no_order_semantics_records
        if item.get("status") == "verified_directional_decision_no_order_expected_semantics"
    )
    decisions_with_stable_no_order_equivalence_class = sum(
        1
        for item in no_order_semantics_records
        if item.get("equivalence_class") == "verified_non_executable_no_order_expected"
    )
    no_order_equivalence_classes = compact_unique(
        [
            item.get("equivalence_class")
            for item in no_order_semantics_records
            if item.get("equivalence_class")
        ],
        limit=8,
    )
    no_order_root_cause_count = sum(
        1 for item in no_order_semantics_records if item.get("root_cause")
    )
    no_order_root_materiality_count = sum(
        1
        for item in no_order_semantics_records
        if item.get("root_cause_is_material_without_order_or_fill_change") is not None
    )
    no_order_materiality_requirement_count = sum(
        1
        for item in no_order_semantics_records
        if item.get("requires_order_or_fill_change_for_materiality") is not None
    )
    no_order_roots_non_material = (
        decisions_with_no_order_expected > 0
        and no_order_root_materiality_count == decisions_with_no_order_expected
        and all(
            item.get("root_cause_is_material_without_order_or_fill_change") is False
            for item in no_order_semantics_records
        )
    )
    no_order_roots_require_order_or_fill_change = (
        decisions_with_no_order_expected > 0
        and no_order_materiality_requirement_count == decisions_with_no_order_expected
        and all(
            item.get("requires_order_or_fill_change_for_materiality") is True
            for item in no_order_semantics_records
        )
    )
    no_order_semantics_complete = (
        decisions_with_no_order_expected <= 0
        or decisions_with_no_order_semantics == decisions_with_no_order_expected
    )
    no_order_semantics_stable = (
        decisions_with_no_order_expected <= 0
        or decisions_with_stable_no_order_equivalence_class == decisions_with_no_order_expected
    )
    no_order_semantics_smallest_missing = (
        None
        if no_order_semantics_complete and no_order_semantics_stable
        else "directional_episode_attribution.no_order_semantics"
    )
    if decisions_with_no_order_expected <= 0:
        no_order_semantics_status = "not_applicable_no_recent_no_order_expected_decisions"
    elif no_order_semantics_complete and no_order_semantics_stable:
        no_order_semantics_status = "verified_recent_no_order_semantics_present"
    else:
        no_order_semantics_status = "missing_recent_no_order_semantics"
    decisions_with_order_surface_or_no_order_expectation = sum(
        1
        for item in recent
        if as_dict(item.get("order_expectation")).get("order_surface_present") is True
        or as_dict(item.get("order_expectation")).get("no_order_expected") is True
    )
    decisions_requiring_order_surface = recent_count - decisions_with_no_order_expected
    decisions_missing_order_surface = sum(
        1
        for item in recent
        if as_dict(item.get("order_expectation")).get("no_order_expected") is not True
        and as_dict(item.get("order_expectation")).get("order_surface_present") is not True
    )
    all_recent_decisions_no_order_expected = recent_count > 0 and decisions_with_no_order_expected == recent_count
    order_surface_or_no_order_expectation_complete = (
        recent_count > 0 and decisions_with_order_surface_or_no_order_expectation == recent_count
    )
    decisions_with_fills = sum(1 for item in recent if int_or_zero(as_dict(item.get("fill")).get("count")) > 0)
    decisions_with_pnl = sum(
        1
        for item in recent
        if int_or_zero(as_dict(item.get("pnl_outcome")).get("fill_outcome_count")) > 0
    )
    decisions_with_slippage_reference = sum(
        1
        for item in recent
        if int_or_zero(as_dict(item.get("fill")).get("slippage_reference_sample_count")) > 0
    )
    decisions_with_realized_fee = sum(
        1
        for item in recent
        if int_or_zero(as_dict(item.get("fill")).get("actual_fee_bps_sample_count")) > 0
    )
    latest_filled = next(
        (item for item in recent if int_or_zero(as_dict(item.get("fill")).get("count")) > 0),
        None,
    )
    filled_with_pnl_lifecycle = [
        item
        for item in recent
        if int_or_zero(as_dict(item.get("fill")).get("count")) > 0 and as_dict(item.get("pnl_lifecycle")).get("status")
    ]
    filled_with_resolved_pnl_lifecycle = [
        item for item in filled_with_pnl_lifecycle if as_dict(item.get("pnl_lifecycle")).get("smallest_missing_field") is None
    ]
    latest_filled_pnl_lifecycle = as_dict(latest_filled.get("pnl_lifecycle")) if latest_filled else {}
    pnl_lifecycle_smallest_missing = latest_filled_pnl_lifecycle.get("smallest_missing_field") or next(
        (
            as_dict(item.get("pnl_lifecycle")).get("smallest_missing_field")
            for item in filled_with_pnl_lifecycle
            if as_dict(item.get("pnl_lifecycle")).get("smallest_missing_field")
        ),
        None,
    )
    if decisions_with_fills <= 0:
        pnl_lifecycle_status = "no_recent_filled_directional_decisions"
    elif latest_filled_pnl_lifecycle.get("status") == "open_position_not_yet_realized":
        pnl_lifecycle_status = "latest_filled_directional_episode_open_unrealized"
    elif pnl_lifecycle_smallest_missing is None:
        pnl_lifecycle_status = "verified_directional_episode_pnl_lifecycle_explained"
    else:
        pnl_lifecycle_status = "missing_directional_episode_pnl_lifecycle_evidence"

    missing_checks = [
        ("portfolio_allocation_decisions.directional", recent_count > 0),
        ("portfolio_allocation_decisions.expected_edge_bps_or_expected_cost_bps", decisions_with_edge_cost > 0),
        (
            "directional_episode_attribution.order_surface_or_no_order_expectation",
            order_surface_or_no_order_expectation_complete,
        ),
        (
            "directional_episode_attribution.no_order_semantics",
            decisions_with_no_order_expected <= 0 or no_order_semantics_smallest_missing is None,
        ),
        ("execution_fills.directional_recent_decision", decisions_with_fills > 0 or all_recent_decisions_no_order_expected),
        ("execution_fills.actual_fee_bps", decisions_with_realized_fee > 0 or all_recent_decisions_no_order_expected),
        (
            "order_or_command_reference_price_for_recent_directional_fills",
            decisions_with_slippage_reference > 0 or all_recent_decisions_no_order_expected,
        ),
        ("fill_outcomes.realized_pnl_delta", decisions_with_pnl > 0 or all_recent_decisions_no_order_expected),
    ]
    smallest_missing = next((field for field, passed in missing_checks if not passed), None)

    if recent_count <= 0:
        status = "no_recent_directional_decisions"
    elif decisions_with_edge_cost <= 0:
        status = "missing_directional_episode_attribution_evidence"
    elif not order_surface_or_no_order_expectation_complete:
        status = "missing_directional_episode_order_surface_or_no_order_expectation"
    elif all_recent_decisions_no_order_expected:
        status = "verified_directional_episode_no_order_expected"
    elif decisions_with_fills <= 0:
        status = "partial_directional_episode_decisions_without_fills"
    elif decisions_with_pnl <= 0:
        status = "partial_directional_episode_fills_without_pnl_outcome"
    elif decisions_with_slippage_reference <= 0:
        status = "partial_directional_episode_pnl_without_slippage_proxy"
    elif smallest_missing is None:
        status = "verified_directional_episode_edge_cost_pnl_attribution_present"
    else:
        status = "missing_directional_episode_attribution_evidence"

    return {
        "source": "live_db",
        "ok": True,
        "symbol": raw.get("symbol"),
        "status": status,
        "smallest_missing_field": smallest_missing,
        "coverage": {
            "recent_decision_count": recent_count,
            "decisions_with_edge_cost": decisions_with_edge_cost,
            "decisions_with_orders": decisions_with_orders,
            "decisions_with_no_order_expected": decisions_with_no_order_expected,
            "decisions_with_no_order_semantics": decisions_with_no_order_semantics,
            "decisions_with_stable_no_order_equivalence_class": (
                decisions_with_stable_no_order_equivalence_class
            ),
            "all_no_order_expected_decisions_have_no_order_semantics": no_order_semantics_complete,
            "all_no_order_expected_decisions_stable_equivalence_class": no_order_semantics_stable,
            "decisions_with_order_surface_or_no_order_expectation": (
                decisions_with_order_surface_or_no_order_expectation
            ),
            "decisions_requiring_order_surface": decisions_requiring_order_surface,
            "decisions_missing_order_surface": decisions_missing_order_surface,
            "all_recent_decisions_no_order_expected": all_recent_decisions_no_order_expected,
            "decisions_with_fills": decisions_with_fills,
            "decisions_with_realized_fee": decisions_with_realized_fee,
            "decisions_with_slippage_reference": decisions_with_slippage_reference,
            "decisions_with_pnl_outcome": decisions_with_pnl,
            "decisions_with_pretrade_microstructure": as_dict(
                pretrade_microstructure.get("coverage")
            ).get("decisions_with_pretrade_microstructure"),
            "filled_decisions_with_pretrade_microstructure": as_dict(
                pretrade_microstructure.get("coverage")
            ).get("filled_decisions_with_pretrade_microstructure"),
            "filled_decisions_with_pnl_lifecycle_classification": len(filled_with_pnl_lifecycle),
            "filled_decisions_with_resolved_pnl_lifecycle": len(filled_with_resolved_pnl_lifecycle),
        },
        "no_order_semantics": {
            "source": "live_db_directional_route_action_order_expectation",
            "status": no_order_semantics_status,
            "smallest_missing_field": no_order_semantics_smallest_missing,
            "coverage": {
                "decisions_with_no_order_expected": decisions_with_no_order_expected,
                "decisions_with_no_order_semantics": decisions_with_no_order_semantics,
                "decisions_with_stable_no_order_equivalence_class": (
                    decisions_with_stable_no_order_equivalence_class
                ),
                "decisions_with_root_cause": no_order_root_cause_count,
                "decisions_with_root_materiality": no_order_root_materiality_count,
                "decisions_with_materiality_requirement": (
                    no_order_materiality_requirement_count
                ),
                "all_no_order_expected_decisions_have_no_order_semantics": (
                    no_order_semantics_complete
                ),
                "all_no_order_expected_decisions_stable_equivalence_class": (
                    no_order_semantics_stable
                ),
                "all_root_causes_non_material_without_order_or_fill_change": (
                    no_order_roots_non_material
                ),
                "all_root_causes_require_order_or_fill_change_for_materiality": (
                    no_order_roots_require_order_or_fill_change
                ),
            },
            "equivalence_classes": no_order_equivalence_classes,
            "distributions": {
                "root_cause": compact_count_distribution(
                    [item.get("root_cause") for item in no_order_semantics_records],
                    limit=8,
                ),
                "equivalence_class": compact_count_distribution(
                    [item.get("equivalence_class") for item in no_order_semantics_records],
                    limit=8,
                ),
                "route_action": compact_count_distribution(
                    [item.get("route_action") for item in no_order_semantics_records],
                    limit=8,
                ),
                "semantic_status": compact_count_distribution(
                    [item.get("status") for item in no_order_semantics_records],
                    limit=8,
                ),
                "root_material_without_order_or_fill_change": (
                    compact_count_distribution(
                        [
                            item.get("root_cause_is_material_without_order_or_fill_change")
                            for item in no_order_semantics_records
                        ],
                        limit=8,
                    )
                ),
                "requires_order_or_fill_change_for_materiality": (
                    compact_count_distribution(
                        [
                            item.get("requires_order_or_fill_change_for_materiality")
                            for item in no_order_semantics_records
                        ],
                        limit=8,
                    )
                ),
            },
            "root_cause_is_material_without_order_or_fill_change": (
                False if no_order_roots_non_material else None
            ),
            "requires_order_or_fill_change_for_materiality": (
                True if no_order_roots_require_order_or_fill_change else None
            ),
        },
        "pretrade_microstructure": pretrade_microstructure,
        "pnl_lifecycle": {
            "source": "live_db_fill_outcomes_position_lots_lot_events",
            "status": pnl_lifecycle_status,
            "smallest_missing_field": pnl_lifecycle_smallest_missing,
            "coverage": {
                "filled_decisions_with_pnl_lifecycle_classification": len(filled_with_pnl_lifecycle),
                "filled_decisions_with_resolved_pnl_lifecycle": len(filled_with_resolved_pnl_lifecycle),
            },
            "latest_filled_decision_status": latest_filled_pnl_lifecycle.get("status"),
            "latest_filled_decision_smallest_missing_field": latest_filled_pnl_lifecycle.get(
                "smallest_missing_field"
            ),
        },
        "latest_filled_decision": latest_filled,
        "recent_decisions": recent[:12],
    }


def summarize_depth_slippage_lifecycle_truth(
    *,
    orderbook_payload_depth: dict[str, Any],
    slippage_cost: dict[str, Any],
    directional_command_flow: dict[str, Any],
    directional_attribution: dict[str, Any],
) -> dict[str, Any]:
    depth = as_dict(orderbook_payload_depth)
    slippage = as_dict(slippage_cost)
    slippage_fee = as_dict(slippage.get("fee"))
    slippage_proxy = as_dict(slippage.get("slippage_proxy"))
    slippage_coverage = as_dict(slippage_proxy.get("coverage_audit"))
    command_coverage = as_dict(as_dict(directional_command_flow).get("coverage"))
    directional_coverage = as_dict(as_dict(directional_attribution).get("coverage"))
    pnl_lifecycle = as_dict(as_dict(directional_attribution).get("pnl_lifecycle"))

    depth_status = depth.get("status")
    depth_ready = depth_status == "verified_books5_payload_depth_evidence_present"
    slippage_status = slippage.get("status")
    slippage_verified = slippage_status == "verified_slippage_cost_calibration_evidence_present"
    fills_total = int_or_zero(slippage.get("fills_total"))
    fee_samples = int_or_zero(slippage_fee.get("sample_count"))
    slippage_samples = int_or_zero(slippage_proxy.get("sample_count"))
    current_submit_fill_count = int_or_zero(command_coverage.get("current_submit_command_fill_count"))
    current_submit_reference_covered_count = int_or_zero(
        command_coverage.get("current_submit_command_reference_covered_fill_count")
    )
    current_submit_reference_missing_count = int_or_zero(
        command_coverage.get("current_submit_command_reference_missing_fill_count")
    )
    recent_directional_decisions = int_or_zero(directional_coverage.get("recent_decision_count"))
    recent_directional_filled_decisions = int_or_zero(directional_coverage.get("decisions_with_fills"))
    recent_directional_no_order_expected = int_or_zero(
        directional_coverage.get("decisions_with_no_order_expected")
    )
    recent_directional_missing_order_surface = int_or_zero(
        directional_coverage.get("decisions_missing_order_surface")
    )
    all_recent_directional_no_order_expected = (
        directional_coverage.get("all_recent_decisions_no_order_expected") is True
    )
    no_order_expected_regime = (
        recent_directional_decisions > 0
        and all_recent_directional_no_order_expected
        and recent_directional_no_order_expected == recent_directional_decisions
        and recent_directional_missing_order_surface == 0
    )
    recent_filled_with_pretrade = int_or_zero(
        directional_coverage.get("filled_decisions_with_pretrade_microstructure")
    )
    recent_filled_with_slippage = int_or_zero(directional_coverage.get("decisions_with_slippage_reference"))
    recent_filled_with_resolved_pnl = int_or_zero(
        directional_coverage.get("filled_decisions_with_resolved_pnl_lifecycle")
    )

    checks = [
        ("orderbook_payload_depth_truth.verified_books5_depth_evidence", depth_ready),
        ("slippage_cost_calibration_truth.verified", slippage_verified),
        ("execution_fills", fills_total > 0),
        ("execution_fills.actual_fee_bps", fee_samples > 0),
        ("slippage_cost_calibration.slippage_proxy_samples", slippage_samples > 0),
        (
            "directional_command_flow.current_submit_reference_covered_fill_count",
            current_submit_reference_covered_count > 0,
        ),
        (
            "directional_episode_attribution.recent_directional_filled_decisions",
            recent_directional_filled_decisions > 0 or no_order_expected_regime,
        ),
        (
            "directional_episode_attribution.filled_decisions_with_pretrade_microstructure",
            recent_filled_with_pretrade > 0 or no_order_expected_regime,
        ),
        (
            "directional_episode_attribution.filled_decisions_with_resolved_pnl_lifecycle",
            recent_filled_with_resolved_pnl > 0 or no_order_expected_regime,
        ),
    ]
    smallest_missing = next((field for field, passed in checks if not passed), None)

    if not depth_ready:
        status = "blocked_missing_orderbook_payload_depth_evidence"
    elif not slippage_verified:
        status = "blocked_missing_slippage_cost_calibration"
    elif fills_total <= 0:
        status = "no_live_fill_samples"
    elif slippage_samples <= 0:
        status = "blocked_missing_slippage_reference_samples"
    elif current_submit_reference_covered_count <= 0:
        status = "blocked_missing_current_submit_reference_coverage"
    elif no_order_expected_regime and recent_directional_filled_decisions <= 0:
        status = "forward_depth_ready_no_order_expected_regime"
    elif recent_directional_filled_decisions <= 0:
        status = "forward_depth_ready_no_recent_directional_filled_episode"
    elif recent_filled_with_pretrade <= 0:
        status = "partial_recent_directional_fill_missing_pretrade_depth_context"
    elif recent_filled_with_resolved_pnl <= 0:
        status = "partial_recent_directional_fill_missing_resolved_pnl_lifecycle"
    elif smallest_missing is None:
        status = "verified_depth_slippage_lifecycle_coverage_present"
    else:
        status = "missing_depth_slippage_lifecycle_evidence"

    return {
        "source": "orderbook_payload_depth_slippage_cost_and_directional_lifecycle",
        "ok": True,
        "status": status,
        "smallest_missing_field": smallest_missing,
        "raw_payload_exposed": False,
        "depth_readiness": {
            "status": depth_status,
            "books5_payload_hash_present": as_dict(depth.get("books5_payload")).get("payload_hash_present"),
            "books5_row_checksum_present": as_dict(depth.get("books5_payload")).get("row_checksum_present"),
            "books5_exchange_sequence_id_present": as_dict(depth.get("books5_payload")).get(
                "exchange_sequence_id_present"
            ),
            "books5_row_count": as_dict(depth.get("sequence")).get("books5_row_count"),
            "books5_sequence_gap_count": as_dict(depth.get("sequence")).get("books5_sequence_gap_count"),
            "diff_payload_persisted_row_count": as_dict(depth.get("sequence")).get(
                "diff_payload_persisted_row_count"
            ),
            "silver_books5_samples_n": as_dict(depth.get("silver_orderbook")).get("books5_samples_n"),
        },
        "slippage_baseline": {
            "status": slippage_status,
            "fills_total": fills_total,
            "fills_24h": int_or_zero(slippage.get("fills_24h")),
            "fee_sample_count": fee_samples,
            "slippage_proxy_sample_count": slippage_samples,
            "missing_reference_fills": int_or_zero(slippage_coverage.get("missing_reference_fills")),
            "covered_reference_fills_with_command_reference": int_or_zero(
                slippage_coverage.get("covered_reference_fills_with_command_reference")
            ),
            "reference_coverage_classification": slippage_coverage.get("classification"),
            "deterministic_backfill_status": slippage_coverage.get("deterministic_backfill_status"),
            "reference_policy": slippage_coverage.get("reference_policy"),
        },
        "directional_command_coverage": {
            "status": as_dict(directional_command_flow).get("status"),
            "current_submit_command_fill_count": current_submit_fill_count,
            "current_submit_command_reference_covered_fill_count": (
                current_submit_reference_covered_count
            ),
            "current_submit_command_reference_missing_fill_count": (
                current_submit_reference_missing_count
            ),
            "historical_no_submit_command_reference_missing_fill_count": int_or_zero(
                command_coverage.get("historical_no_submit_command_reference_missing_fill_count")
            ),
        },
        "recent_directional_lifecycle_coverage": {
            "directional_episode_status": as_dict(directional_attribution).get("status"),
            "recent_decision_count": recent_directional_decisions,
            "recent_filled_decision_count": recent_directional_filled_decisions,
            "decisions_with_no_order_expected": recent_directional_no_order_expected,
            "decisions_missing_order_surface": recent_directional_missing_order_surface,
            "all_recent_decisions_no_order_expected": all_recent_directional_no_order_expected,
            "no_order_expected_regime": no_order_expected_regime,
            "recent_filled_with_pretrade_microstructure": recent_filled_with_pretrade,
            "recent_filled_with_slippage_reference": recent_filled_with_slippage,
            "recent_filled_with_resolved_pnl_lifecycle": recent_filled_with_resolved_pnl,
            "pnl_lifecycle_status": pnl_lifecycle.get("status"),
            "pnl_lifecycle_smallest_missing_field": pnl_lifecycle.get("smallest_missing_field"),
        },
        "interpretation": {
            "forward_depth_ready": depth_ready,
            "existing_fill_slippage_baseline_present": slippage_verified and slippage_samples > 0,
            "per_recent_directional_fill_depth_lifecycle_link_present": (
                recent_filled_with_pretrade > 0 and recent_filled_with_resolved_pnl > 0
            ),
            "no_order_expected_regime": no_order_expected_regime,
            "waiting_for_executable_directional_episode": (
                no_order_expected_regime and recent_directional_filled_decisions <= 0
            ),
            "does_not_claim_historical_fills_have_sidecar_payload_depth": True,
            "not_alpha_or_profitability_evidence": True,
        },
    }


def summarize_decision_lifecycle_provenance_continuity_truth(
    *,
    db: dict[str, Any],
    directional_attribution: dict[str, Any],
    directional_executable_episode: dict[str, Any],
    latest_decision_fill_feasibility: dict[str, Any],
    directional_command_flow: dict[str, Any],
    depth_slippage_lifecycle: dict[str, Any],
) -> dict[str, Any]:
    if not db.get("ok"):
        return {
            "source": "live_runtime_truth_surfaces",
            "ok": False,
            "status": "live_db_unavailable",
            "smallest_missing_field": "database_truth",
            "raw_payload_exposed": False,
        }

    latest = as_dict(db.get("latest_decision"))
    if not latest:
        return {
            "source": "live_runtime_truth_surfaces",
            "ok": False,
            "status": "missing_latest_decision_lifecycle_provenance",
            "smallest_missing_field": "database_truth.latest_decision",
            "raw_payload_exposed": False,
        }

    latest_chain = as_dict(latest.get("execution_truth_chain"))
    latest_no_trade = as_dict(latest.get("no_trade_attribution"))
    latest_execution_chain = as_dict(latest.get("execution_chain"))
    latest_fill_feasibility = as_dict(latest_decision_fill_feasibility)
    latest_fill_feasibility_no_order = as_dict(
        latest_fill_feasibility.get("no_order")
        or latest_fill_feasibility.get("no_order_context")
    )
    latest_chain_status = latest_chain.get("status")
    latest_chain_missing = latest_chain.get("smallest_missing_field")
    latest_order_expected = latest_chain.get("order_expected")
    latest_fill_expected = latest_chain.get("fill_expected")
    latest_no_order_expected = latest_order_expected is False and latest_fill_expected is False
    latest_chain_complete = (
        latest_chain_status
        in {
            "verified_no_order_expected",
            "verified_terminal_order_no_fill_expected",
            "verified_execution_surface_present",
        }
        and latest_chain_missing is None
    )
    latest_no_order_feasibility_consistent = (
        not latest_no_order_expected
        or (
            latest_fill_feasibility.get("status")
            == "verified_no_order_fill_feasibility_not_applicable_with_pretrade_context"
            and latest_fill_feasibility.get("fill_feasibility_applicable") is False
        )
    )

    executable_latest = as_dict(directional_executable_episode.get("latest_executable_decision"))
    executable_provenance = as_dict(directional_executable_episode.get("provenance"))
    executable_drilldown = as_dict(directional_executable_episode.get("terminal_no_fill_drilldown"))
    executable_status = directional_executable_episode.get("status")
    executable_missing = directional_executable_episode.get("smallest_missing_field")
    executable_terminal_no_fill_verified = (
        executable_status == "verified_executable_terminal_order_no_fill_truth"
        and executable_missing is None
        and executable_drilldown.get("status") == "verified_terminal_no_fill_order_state_drilldown"
    )
    executable_order_fill_verified = (
        executable_status == "verified_executable_order_fill_truth_surface"
        and executable_missing is None
    )
    executable_lane_complete = executable_terminal_no_fill_verified or executable_order_fill_verified

    attribution_coverage = as_dict(directional_attribution.get("coverage"))
    attribution_status = directional_attribution.get("status")
    attribution_missing = directional_attribution.get("smallest_missing_field")
    recent_batch_complete = attribution_missing is None and attribution_status in {
        "verified_directional_episode_no_order_expected",
        "verified_directional_episode_edge_cost_pnl_attribution_present",
    }

    command_coverage = as_dict(directional_command_flow.get("coverage"))
    command_flow_complete = (
        directional_command_flow.get("smallest_missing_field") is None
        and directional_command_flow.get("current_command_path_reference_gap") is not True
    )
    depth_recent = as_dict(depth_slippage_lifecycle.get("recent_directional_lifecycle_coverage"))
    depth_status = depth_slippage_lifecycle.get("status")
    depth_complete = depth_slippage_lifecycle.get("smallest_missing_field") is None and depth_status in {
        "forward_depth_ready_no_order_expected_regime",
        "forward_depth_ready_no_recent_directional_filled_episode",
        "verified_depth_slippage_lifecycle_coverage_present",
    }

    checks = [
        ("latest_decision.execution_truth_chain", latest_chain_complete),
        ("latest_decision.fill_feasibility_no_order_context", latest_no_order_feasibility_consistent),
        ("directional_executable_episode_truth", executable_lane_complete),
        ("directional_episode_attribution_truth", recent_batch_complete),
        ("directional_command_flow_provenance_truth", command_flow_complete),
        ("depth_slippage_lifecycle_truth", depth_complete),
    ]
    smallest_missing = next((field for field, passed in checks if not passed), None)

    if not latest_chain_complete:
        status = "missing_latest_decision_lifecycle_provenance"
        ok = False
    elif not latest_no_order_feasibility_consistent:
        status = "inconsistent_latest_no_order_fill_feasibility"
        ok = False
    elif not executable_lane_complete:
        status = "missing_latest_executable_directional_lifecycle_provenance"
        ok = False
    elif not recent_batch_complete:
        status = "missing_recent_directional_lifecycle_provenance"
        ok = False
    elif not command_flow_complete:
        status = "missing_directional_command_flow_provenance_continuity"
        ok = False
    elif not depth_complete:
        status = "missing_depth_slippage_lifecycle_continuity"
        ok = False
    elif latest_no_order_expected and executable_terminal_no_fill_verified:
        status = "verified_current_no_order_plus_executable_terminal_no_fill_continuity"
        ok = True
    else:
        status = "verified_decision_lifecycle_provenance_continuity"
        ok = True

    return {
        "source": "live_runtime_truth_surfaces",
        "ok": ok,
        "status": status,
        "smallest_missing_field": smallest_missing,
        "raw_payload_exposed": False,
        "current_decision": {
            "decision_id": latest.get("decision_id"),
            "created_at": latest.get("created_at"),
            "primary_family": latest.get("primary_family"),
            "route_action": latest.get("route_action"),
            "execution_truth_status": latest_chain_status,
            "order_expected": latest_order_expected,
            "fill_expected": latest_fill_expected,
            "position_lifecycle_status": latest_chain.get("position_lifecycle_status"),
            "truth_chain_smallest_missing_field": latest_chain_missing,
            "no_trade_classification": latest_no_trade.get("classification"),
            "no_trade_primary_blocker": latest_no_trade.get("primary_blocker"),
            "fill_feasibility_status": latest_fill_feasibility.get("status"),
            "fill_feasibility_applicable": latest_fill_feasibility.get(
                "fill_feasibility_applicable"
            ),
            "no_order_feasibility_classification": latest_fill_feasibility_no_order.get(
                "classification"
            ),
        },
        "current_decision_provenance_counts": {
            "execution_plan_ref_count": int_or_zero(
                latest_execution_chain.get("execution_plan_ref_count")
            ),
            "order_intent_ref_count": int_or_zero(
                latest_execution_chain.get("order_intent_ref_count")
            ),
            "order_state_ref_count": int_or_zero(
                latest_execution_chain.get("order_state_ref_count")
            ),
            "fill_event_ref_count": int_or_zero(latest_execution_chain.get("fill_event_ref_count")),
            "db_order_count": int_or_zero(latest_execution_chain.get("db_order_count")),
            "db_order_state_count": int_or_zero(latest_execution_chain.get("db_order_state_count")),
            "db_fill_count": int_or_zero(latest_execution_chain.get("db_fill_count")),
            "db_execution_command_count": int_or_zero(
                latest_execution_chain.get("db_execution_command_count")
            ),
        },
        "latest_executable_directional_episode": {
            "decision_id": executable_latest.get("decision_id"),
            "created_at": executable_latest.get("created_at"),
            "status": executable_status,
            "smallest_missing_field": executable_missing,
            "execution_truth_status": executable_latest.get("execution_truth_status"),
            "order_expected": executable_latest.get("order_expected"),
            "fill_expected": executable_latest.get("fill_expected"),
            "position_lifecycle_status": executable_latest.get("position_lifecycle_status"),
            "terminal_no_fill_drilldown_status": executable_drilldown.get("status"),
        },
        "latest_executable_directional_provenance_counts": executable_provenance,
        "recent_directional_batch": {
            "status": attribution_status,
            "smallest_missing_field": attribution_missing,
            "recent_decision_count": int_or_zero(attribution_coverage.get("recent_decision_count")),
            "decisions_with_no_order_expected": int_or_zero(
                attribution_coverage.get("decisions_with_no_order_expected")
            ),
            "decisions_with_orders": int_or_zero(attribution_coverage.get("decisions_with_orders")),
            "decisions_with_fills": int_or_zero(attribution_coverage.get("decisions_with_fills")),
            "decisions_missing_order_surface": int_or_zero(
                attribution_coverage.get("decisions_missing_order_surface")
            ),
            "all_recent_decisions_no_order_expected": attribution_coverage.get(
                "all_recent_decisions_no_order_expected"
            ),
            "filled_decisions_with_resolved_pnl_lifecycle": int_or_zero(
                attribution_coverage.get("filled_decisions_with_resolved_pnl_lifecycle")
            ),
        },
        "command_flow": {
            "status": directional_command_flow.get("status"),
            "smallest_missing_field": directional_command_flow.get("smallest_missing_field"),
            "current_command_path_reference_gap": directional_command_flow.get(
                "current_command_path_reference_gap"
            ),
            "current_submit_command_fill_count": int_or_zero(
                command_coverage.get("current_submit_command_fill_count")
            ),
            "current_submit_command_reference_covered_fill_count": int_or_zero(
                command_coverage.get("current_submit_command_reference_covered_fill_count")
            ),
            "current_submit_command_reference_missing_fill_count": int_or_zero(
                command_coverage.get("current_submit_command_reference_missing_fill_count")
            ),
        },
        "depth_slippage_lifecycle": {
            "status": depth_status,
            "smallest_missing_field": depth_slippage_lifecycle.get("smallest_missing_field"),
            "recent_filled_decision_count": depth_recent.get("recent_filled_decision_count"),
            "recent_filled_with_resolved_pnl_lifecycle": depth_recent.get(
                "recent_filled_with_resolved_pnl_lifecycle"
            ),
            "no_order_expected_regime": depth_recent.get("no_order_expected_regime"),
        },
        "interpretation": {
            "current_decision_no_order_expected": latest_no_order_expected,
            "latest_executable_terminal_no_fill_verified": executable_terminal_no_fill_verified,
            "recent_batch_lifecycle_complete": recent_batch_complete,
            "command_flow_continuity_complete": command_flow_complete,
            "depth_slippage_lifecycle_complete": depth_complete,
            "not_alpha_or_profitability_evidence": True,
        },
    }


def summarize_decision_lifecycle_execution_science_continuity_truth(
    *,
    decision_lifecycle_provenance_continuity: dict[str, Any],
    executable_terminal_pretrade_microstructure: dict[str, Any],
    orderbook_payload_depth: dict[str, Any],
    latest_decision_fill_feasibility: dict[str, Any],
    depth_slippage_lifecycle: dict[str, Any],
    execution_science: dict[str, Any],
    slippage_cost: dict[str, Any],
) -> dict[str, Any]:
    lifecycle = as_dict(decision_lifecycle_provenance_continuity)
    terminal_pretrade = as_dict(executable_terminal_pretrade_microstructure)
    orderbook_depth = as_dict(orderbook_payload_depth)
    latest_fill_feasibility = as_dict(latest_decision_fill_feasibility)
    depth_lifecycle = as_dict(depth_slippage_lifecycle)
    execution = as_dict(execution_science)
    slippage = as_dict(slippage_cost)

    lifecycle_status = lifecycle.get("status")
    terminal_status = terminal_pretrade.get("status")
    orderbook_depth_status = orderbook_depth.get("status")
    latest_fill_feasibility_status = latest_fill_feasibility.get("status")
    depth_lifecycle_status = depth_lifecycle.get("status")
    execution_status = execution.get("status")
    slippage_status = slippage.get("status")

    lifecycle_current = as_dict(lifecycle.get("current_decision"))
    lifecycle_executable = as_dict(lifecycle.get("latest_executable_directional_episode"))
    lifecycle_recent = as_dict(lifecycle.get("recent_directional_batch"))
    lifecycle_command = as_dict(lifecycle.get("command_flow"))
    terminal_sequence = as_dict(terminal_pretrade.get("snapshot_diff_sequence"))
    terminal_fill_feasibility = as_dict(terminal_pretrade.get("local_fill_feasibility"))
    terminal_slippage = as_dict(terminal_pretrade.get("slippage_baseline"))
    orderbook_depth_sequence = as_dict(orderbook_depth.get("sequence"))
    orderbook_depth_books5 = as_dict(orderbook_depth.get("books5_payload"))
    depth_lifecycle_interpretation = as_dict(depth_lifecycle.get("interpretation"))
    depth_lifecycle_slippage = as_dict(depth_lifecycle.get("slippage_baseline"))
    depth_lifecycle_recent = as_dict(depth_lifecycle.get("recent_directional_lifecycle_coverage"))
    execution_payload_sequence = as_dict(execution.get("payload_sequence"))
    slippage_proxy = as_dict(slippage.get("slippage_proxy"))
    slippage_coverage = as_dict(slippage_proxy.get("coverage_audit"))

    lifecycle_ok = (
        lifecycle.get("ok") is True
        and lifecycle.get("smallest_missing_field") is None
        and lifecycle_status
        in {
            "verified_current_no_order_plus_executable_terminal_no_fill_continuity",
            "verified_decision_lifecycle_provenance_continuity",
        }
    )
    terminal_pretrade_ok = (
        terminal_pretrade.get("smallest_missing_field") is None
        and terminal_status
        == "verified_executable_terminal_no_fill_pretrade_microstructure_drilldown"
    )
    orderbook_depth_ok = (
        orderbook_depth.get("smallest_missing_field") is None
        and orderbook_depth_status == "verified_books5_payload_depth_evidence_present"
    )
    latest_fill_feasibility_ok = (
        latest_fill_feasibility.get("smallest_missing_field") is None
        and latest_fill_feasibility_status
        in {
            "verified_no_order_fill_feasibility_not_applicable_with_pretrade_context",
            "verified_order_expected_pretrade_fill_feasibility_context_present",
        }
    )
    depth_lifecycle_ok = (
        depth_lifecycle.get("smallest_missing_field") is None
        and depth_lifecycle_status
        in {
            "forward_depth_ready_no_order_expected_regime",
            "forward_depth_ready_no_recent_directional_filled_episode",
            "verified_depth_slippage_lifecycle_coverage_present",
        }
    )
    execution_sequence_ok = (
        execution_status == "verified_orderbook_sequence_and_silver_bar_present"
        and execution_payload_sequence.get("status") == "sequence_continuous"
    )
    slippage_ok = (
        slippage.get("smallest_missing_field") is None
        and slippage_status == "verified_slippage_cost_calibration_evidence_present"
    )

    checks = [
        ("decision_lifecycle_provenance_continuity_truth", lifecycle_ok),
        (
            "directional_executable_terminal_no_fill_pretrade_microstructure_truth",
            terminal_pretrade_ok,
        ),
        ("orderbook_payload_depth_truth", orderbook_depth_ok),
        ("latest_decision_fill_feasibility_truth", latest_fill_feasibility_ok),
        ("depth_slippage_lifecycle_truth", depth_lifecycle_ok),
        ("execution_science_truth.payload_sequence", execution_sequence_ok),
        ("slippage_cost_calibration_truth", slippage_ok),
    ]
    smallest_missing = next((field for field, passed in checks if not passed), None)

    current_no_order_expected = (
        lifecycle_current.get("order_expected") is False
        and lifecycle_current.get("fill_expected") is False
    )
    terminal_no_fill_before_exchange_ack = (
        terminal_fill_feasibility.get("status") == "terminal_no_fill_before_exchange_ack"
    )
    if not lifecycle_ok:
        status = "missing_decision_lifecycle_provenance_continuity"
        ok = False
    elif not terminal_pretrade_ok:
        status = "missing_terminal_no_fill_execution_science_context"
        ok = False
    elif not orderbook_depth_ok:
        status = "missing_orderbook_payload_depth_continuity"
        ok = False
    elif not latest_fill_feasibility_ok:
        status = "missing_latest_decision_fill_feasibility_context"
        ok = False
    elif not depth_lifecycle_ok:
        status = "missing_depth_slippage_lifecycle_continuity"
        ok = False
    elif not execution_sequence_ok:
        status = "missing_execution_science_sequence_context"
        ok = False
    elif not slippage_ok:
        status = "missing_slippage_cost_calibration_context"
        ok = False
    elif current_no_order_expected and terminal_no_fill_before_exchange_ack:
        status = "verified_no_order_terminal_no_fill_execution_science_continuity"
        ok = True
    else:
        status = "verified_decision_lifecycle_execution_science_continuity"
        ok = True

    return {
        "source": "live_runtime_truth_surfaces",
        "ok": ok,
        "status": status,
        "smallest_missing_field": smallest_missing,
        "raw_payload_exposed": False,
        "lifecycle_provenance": {
            "status": lifecycle_status,
            "smallest_missing_field": lifecycle.get("smallest_missing_field"),
            "current_decision_id": lifecycle_current.get("decision_id"),
            "current_order_expected": lifecycle_current.get("order_expected"),
            "current_fill_expected": lifecycle_current.get("fill_expected"),
            "current_truth_status": lifecycle_current.get("execution_truth_status"),
            "current_fill_feasibility_status": lifecycle_current.get(
                "fill_feasibility_status"
            ),
            "executable_decision_id": lifecycle_executable.get("decision_id"),
            "executable_status": lifecycle_executable.get("status"),
            "executable_terminal_no_fill_drilldown_status": lifecycle_executable.get(
                "terminal_no_fill_drilldown_status"
            ),
            "recent_decision_count": lifecycle_recent.get("recent_decision_count"),
            "recent_filled_decisions": lifecycle_recent.get("decisions_with_fills"),
            "command_flow_status": lifecycle_command.get("status"),
        },
        "latest_decision_fill_feasibility": {
            "status": latest_fill_feasibility_status,
            "smallest_missing_field": latest_fill_feasibility.get("smallest_missing_field"),
            "decision_id": latest_fill_feasibility.get("decision_id"),
            "order_expected": latest_fill_feasibility.get("order_expected"),
            "fill_expected": latest_fill_feasibility.get("fill_expected"),
            "fill_feasibility_applicable": latest_fill_feasibility.get(
                "fill_feasibility_applicable"
            ),
        },
        "terminal_no_fill_execution_science": {
            "status": terminal_status,
            "smallest_missing_field": terminal_pretrade.get("smallest_missing_field"),
            "decision_id": as_dict(terminal_pretrade.get("decision")).get("decision_id"),
            "snapshot_diff_sequence_status": terminal_sequence.get("status"),
            "snapshot_diff_sequence_gap_count": terminal_sequence.get("sequence_gap_count"),
            "local_fill_feasibility_status": terminal_fill_feasibility.get("status"),
            "market_fill_feasibility_observable": terminal_fill_feasibility.get(
                "market_fill_feasibility_observable"
            ),
            "terminal_order_count": terminal_fill_feasibility.get("terminal_order_count"),
            "exchange_ack_absent_count": terminal_fill_feasibility.get(
                "exchange_ack_absent_count"
            ),
            "fill_absent_count": terminal_fill_feasibility.get("fill_absent_count"),
            "slippage_baseline_status": terminal_slippage.get("status"),
        },
        "orderbook_payload_depth": {
            "status": orderbook_depth_status,
            "smallest_missing_field": orderbook_depth.get("smallest_missing_field"),
            "raw_payload_exposed": orderbook_depth.get("raw_payload_exposed"),
            "books5_payload_hash_present": orderbook_depth_books5.get(
                "payload_hash_present"
            ),
            "books5_row_count": orderbook_depth_sequence.get("books5_row_count"),
            "books5_sequence_gap_count": orderbook_depth_sequence.get(
                "books5_sequence_gap_count"
            ),
            "diff_payload_persisted_row_count": orderbook_depth_sequence.get(
                "diff_payload_persisted_row_count"
            ),
        },
        "depth_slippage_lifecycle": {
            "status": depth_lifecycle_status,
            "smallest_missing_field": depth_lifecycle.get("smallest_missing_field"),
            "forward_depth_ready": depth_lifecycle_interpretation.get("forward_depth_ready"),
            "no_order_expected_regime": depth_lifecycle_interpretation.get(
                "no_order_expected_regime"
            ),
            "recent_filled_decision_count": depth_lifecycle_recent.get(
                "recent_filled_decision_count"
            ),
            "slippage_proxy_sample_count": depth_lifecycle_slippage.get(
                "slippage_proxy_sample_count"
            ),
            "fee_sample_count": depth_lifecycle_slippage.get("fee_sample_count"),
        },
        "execution_science": {
            "status": execution_status,
            "payload_sequence_status": execution_payload_sequence.get("status"),
            "payload_sequence_gap_count": execution_payload_sequence.get("sequence_gap_count"),
            "fill_feasibility_truth_status": execution.get("fill_feasibility_truth_status"),
        },
        "slippage_cost_calibration": {
            "status": slippage_status,
            "smallest_missing_field": slippage.get("smallest_missing_field"),
            "fee_sample_count": as_dict(slippage.get("fee")).get("sample_count"),
            "slippage_proxy_sample_count": slippage_proxy.get("sample_count"),
            "reference_coverage_classification": slippage_coverage.get("classification"),
            "missing_reference_fills": slippage_coverage.get("missing_reference_fills"),
            "covered_reference_fills_with_command_reference": slippage_coverage.get(
                "covered_reference_fills_with_command_reference"
            ),
        },
        "interpretation": {
            "current_no_order_expected": current_no_order_expected,
            "terminal_no_fill_before_exchange_ack": terminal_no_fill_before_exchange_ack,
            "market_fill_feasibility_observable": terminal_fill_feasibility.get(
                "market_fill_feasibility_observable"
            ),
            "execution_science_is_pretrade_observability_not_alpha": True,
            "not_alpha_or_profitability_evidence": True,
        },
    }


def summarize_recent_directional_decision_chain_density_truth(
    *,
    directional_attribution: dict[str, Any],
    decision_lifecycle_provenance_continuity: dict[str, Any],
    decision_lifecycle_execution_science_continuity: dict[str, Any],
    directional_command_flow: dict[str, Any],
) -> dict[str, Any]:
    attribution = as_dict(directional_attribution)
    lifecycle = as_dict(decision_lifecycle_provenance_continuity)
    execution_science_continuity = as_dict(decision_lifecycle_execution_science_continuity)
    command_flow = as_dict(directional_command_flow)
    coverage = as_dict(attribution.get("coverage"))
    no_order_semantics = as_dict(attribution.get("no_order_semantics"))
    no_order_semantics_coverage = as_dict(no_order_semantics.get("coverage"))
    latest_filled = as_dict(attribution.get("latest_filled_decision"))
    latest_filled_fill = as_dict(latest_filled.get("fill"))
    latest_filled_pnl = as_dict(latest_filled.get("pnl_lifecycle"))
    latest_filled_pretrade = as_dict(latest_filled.get("pretrade_microstructure"))

    recent_decision_count = int_or_zero(coverage.get("recent_decision_count"))
    decisions_with_order_surface_or_no_order_expectation = int_or_zero(
        coverage.get("decisions_with_order_surface_or_no_order_expectation")
    )
    decisions_missing_order_surface = int_or_zero(
        coverage.get("decisions_missing_order_surface")
    )
    decisions_with_no_order_expected = int_or_zero(
        coverage.get("decisions_with_no_order_expected")
    )
    decisions_with_no_order_semantics = int_or_zero(
        coverage.get("decisions_with_no_order_semantics")
    )
    decisions_with_stable_no_order_equivalence_class = int_or_zero(
        coverage.get("decisions_with_stable_no_order_equivalence_class")
    )
    decisions_with_fills = int_or_zero(coverage.get("decisions_with_fills"))
    decisions_with_pnl_outcome = int_or_zero(coverage.get("decisions_with_pnl_outcome"))
    decisions_with_pretrade_microstructure = int_or_zero(
        coverage.get("decisions_with_pretrade_microstructure")
    )
    filled_decisions_with_pretrade_microstructure = int_or_zero(
        coverage.get("filled_decisions_with_pretrade_microstructure")
    )
    filled_decisions_with_pnl_lifecycle_classification = int_or_zero(
        coverage.get("filled_decisions_with_pnl_lifecycle_classification")
    )
    filled_decisions_with_resolved_pnl_lifecycle = int_or_zero(
        coverage.get("filled_decisions_with_resolved_pnl_lifecycle")
    )
    all_recent_decisions_no_order_expected = (
        coverage.get("all_recent_decisions_no_order_expected") is True
    )
    all_no_order_expected_have_semantics = (
        coverage.get("all_no_order_expected_decisions_have_no_order_semantics") is True
        or no_order_semantics_coverage.get(
            "all_no_order_expected_decisions_have_no_order_semantics"
        )
        is True
    )
    all_no_order_expected_stable = (
        coverage.get("all_no_order_expected_decisions_stable_equivalence_class") is True
        or no_order_semantics_coverage.get(
            "all_no_order_expected_decisions_stable_equivalence_class"
        )
        is True
    )
    lifecycle_ok = (
        lifecycle.get("smallest_missing_field") is None
        and lifecycle.get("status")
        in {
            "verified_current_no_order_plus_executable_terminal_no_fill_continuity",
            "verified_decision_lifecycle_provenance_continuity",
        }
    )
    execution_science_ok = (
        execution_science_continuity.get("smallest_missing_field") is None
        and execution_science_continuity.get("status")
        in {
            "verified_no_order_terminal_no_fill_execution_science_continuity",
            "verified_decision_lifecycle_execution_science_continuity",
        }
    )
    command_flow_ok = (
        command_flow.get("smallest_missing_field") is None
        and command_flow.get("current_command_path_reference_gap") is not True
    )
    filled_decisions_with_complete_pnl_lifecycle = (
        decisions_with_fills > 0
        and decisions_with_pnl_outcome >= decisions_with_fills
        and filled_decisions_with_resolved_pnl_lifecycle >= decisions_with_fills
    )
    filled_decisions_with_complete_pretrade = (
        decisions_with_fills > 0
        and filled_decisions_with_pretrade_microstructure >= decisions_with_fills
    )

    if attribution.get("ok") is False:
        status = "directional_episode_attribution_unavailable"
        smallest_missing = (
            attribution.get("smallest_missing_field") or "directional_episode_attribution_truth"
        )
        ok = False
    elif recent_decision_count <= 0:
        status = "no_recent_directional_decisions"
        smallest_missing = "portfolio_allocation_decisions.directional"
        ok = False
    elif decisions_with_order_surface_or_no_order_expectation < recent_decision_count:
        status = "missing_recent_directional_order_surface_or_no_order_expectation"
        smallest_missing = "directional_episode_attribution.order_surface_or_no_order_expectation"
        ok = False
    elif all_recent_decisions_no_order_expected and not (
        all_no_order_expected_have_semantics and all_no_order_expected_stable
    ):
        status = "missing_recent_directional_no_order_semantics"
        smallest_missing = "directional_episode_attribution.no_order_semantics"
        ok = False
    elif not lifecycle_ok:
        status = "missing_decision_lifecycle_provenance_continuity"
        smallest_missing = "decision_lifecycle_provenance_continuity_truth"
        ok = False
    elif not execution_science_ok:
        status = "missing_decision_lifecycle_execution_science_continuity"
        smallest_missing = "decision_lifecycle_execution_science_continuity_truth"
        ok = False
    elif not command_flow_ok:
        status = "missing_directional_command_flow_provenance_continuity"
        smallest_missing = "directional_command_flow_provenance_truth"
        ok = False
    elif all_recent_decisions_no_order_expected:
        status = "verified_recent_directional_decision_chain_density_no_order_regime"
        smallest_missing = None
        ok = True
    elif decisions_with_fills <= 0:
        status = "partial_recent_directional_decisions_without_fills"
        smallest_missing = "execution_fills.directional_recent_decision"
        ok = False
    elif not filled_decisions_with_complete_pnl_lifecycle:
        status = "partial_recent_directional_fills_without_complete_pnl_lifecycle"
        smallest_missing = "directional_episode_attribution.filled_pnl_lifecycle"
        ok = False
    elif not filled_decisions_with_complete_pretrade:
        status = "partial_recent_directional_fills_without_pretrade_microstructure"
        smallest_missing = "directional_episode_attribution.filled_pretrade_microstructure"
        ok = False
    else:
        status = "verified_recent_directional_decision_chain_density_with_fills"
        smallest_missing = None
        ok = True

    recent_preview = []
    for item in [as_dict(row) for row in as_list(attribution.get("recent_decisions"))[:8]]:
        order_expectation = as_dict(item.get("order_expectation"))
        no_order = as_dict(item.get("no_order_semantics"))
        fill = as_dict(item.get("fill"))
        pnl_lifecycle = as_dict(item.get("pnl_lifecycle"))
        pretrade = as_dict(item.get("pretrade_microstructure"))
        recent_preview.append(
            {
                "decision_id": item.get("decision_id"),
                "created_at": item.get("created_at"),
                "route_action": item.get("route_action"),
                "order_surface_present": order_expectation.get("order_surface_present"),
                "no_order_expected": order_expectation.get("no_order_expected"),
                "no_order_semantics_status": no_order.get("status"),
                "fill_count": int_or_zero(fill.get("count")),
                "pnl_lifecycle_status": pnl_lifecycle.get("status"),
                "pretrade_microstructure_status": pretrade.get("status"),
            }
        )

    return {
        "source": "directional_episode_attribution_truth.recent_decisions",
        "ok": ok,
        "status": status,
        "smallest_missing_field": smallest_missing,
        "raw_payload_exposed": False,
        "coverage": {
            "recent_decision_count": recent_decision_count,
            "decisions_with_order_surface_or_no_order_expectation": (
                decisions_with_order_surface_or_no_order_expectation
            ),
            "decisions_missing_order_surface": decisions_missing_order_surface,
            "decisions_with_no_order_expected": decisions_with_no_order_expected,
            "all_recent_decisions_no_order_expected": all_recent_decisions_no_order_expected,
            "decisions_with_no_order_semantics": decisions_with_no_order_semantics,
            "decisions_with_stable_no_order_equivalence_class": (
                decisions_with_stable_no_order_equivalence_class
            ),
            "all_no_order_expected_decisions_have_no_order_semantics": (
                all_no_order_expected_have_semantics
            ),
            "all_no_order_expected_decisions_stable_equivalence_class": (
                all_no_order_expected_stable
            ),
            "decisions_with_fills": decisions_with_fills,
            "decisions_with_pnl_outcome": decisions_with_pnl_outcome,
            "filled_decisions_with_pnl_lifecycle_classification": (
                filled_decisions_with_pnl_lifecycle_classification
            ),
            "filled_decisions_with_resolved_pnl_lifecycle": (
                filled_decisions_with_resolved_pnl_lifecycle
            ),
            "decisions_with_pretrade_microstructure": (
                decisions_with_pretrade_microstructure
            ),
            "filled_decisions_with_pretrade_microstructure": (
                filled_decisions_with_pretrade_microstructure
            ),
        },
        "current_decision_continuity": {
            "status": lifecycle.get("status"),
            "smallest_missing_field": lifecycle.get("smallest_missing_field"),
        },
        "execution_science_continuity": {
            "status": execution_science_continuity.get("status"),
            "smallest_missing_field": execution_science_continuity.get("smallest_missing_field"),
        },
        "command_flow": {
            "status": command_flow.get("status"),
            "smallest_missing_field": command_flow.get("smallest_missing_field"),
            "current_command_path_reference_gap": command_flow.get(
                "current_command_path_reference_gap"
            ),
        },
        "latest_filled_decision": {
            "decision_id": latest_filled.get("decision_id"),
            "classification": latest_filled.get("classification"),
            "fill_count": int_or_zero(latest_filled_fill.get("count")),
            "pnl_lifecycle_status": latest_filled_pnl.get("status"),
            "pnl_lifecycle_smallest_missing_field": latest_filled_pnl.get(
                "smallest_missing_field"
            ),
            "pretrade_microstructure_status": latest_filled_pretrade.get("status"),
        },
        "recent_decisions": recent_preview,
        "interpretation": {
            "no_order_expected_regime": all_recent_decisions_no_order_expected,
            "waiting_for_executable_directional_episode": (
                all_recent_decisions_no_order_expected and decisions_with_fills <= 0
            ),
            "filled_decisions_have_complete_pnl_lifecycle": (
                filled_decisions_with_complete_pnl_lifecycle
            ),
            "filled_decisions_have_pretrade_microstructure": (
                filled_decisions_with_complete_pretrade
            ),
            "not_alpha_or_profitability_evidence": True,
        },
    }


def summarize_recent_directional_no_order_root_cause_density_truth(
    *,
    directional_attribution: dict[str, Any],
) -> dict[str, Any]:
    attribution = as_dict(directional_attribution)
    coverage = as_dict(attribution.get("coverage"))
    no_order_semantics = as_dict(attribution.get("no_order_semantics"))
    no_order_coverage = as_dict(no_order_semantics.get("coverage"))
    distributions = as_dict(no_order_semantics.get("distributions"))

    recent_decision_count = int_or_zero(coverage.get("recent_decision_count"))
    no_order_expected_decision_count = int_or_zero(
        coverage.get("decisions_with_no_order_expected")
    )
    decisions_with_order_surface_or_no_order_expectation = int_or_zero(
        coverage.get("decisions_with_order_surface_or_no_order_expectation")
    )
    decisions_missing_order_surface = int_or_zero(
        coverage.get("decisions_missing_order_surface")
    )
    decisions_with_no_order_semantics = int_or_zero(
        no_order_coverage.get(
            "decisions_with_no_order_semantics",
            coverage.get("decisions_with_no_order_semantics"),
        )
    )
    decisions_with_stable_no_order_equivalence_class = int_or_zero(
        no_order_coverage.get(
            "decisions_with_stable_no_order_equivalence_class",
            coverage.get("decisions_with_stable_no_order_equivalence_class"),
        )
    )
    decisions_with_root_cause = int_or_zero(
        no_order_coverage.get("decisions_with_root_cause")
    )
    decisions_with_root_materiality = int_or_zero(
        no_order_coverage.get("decisions_with_root_materiality")
    )
    decisions_with_materiality_requirement = int_or_zero(
        no_order_coverage.get("decisions_with_materiality_requirement")
    )
    recent_decision_rows = [as_dict(row) for row in as_list(attribution.get("recent_decisions"))]
    no_order_rows = [
        item
        for item in recent_decision_rows
        if as_dict(item.get("order_expectation")).get("no_order_expected") is True
    ]
    if decisions_with_root_cause <= 0 and no_order_rows:
        decisions_with_root_cause = sum(
            1 for item in no_order_rows if as_dict(item.get("no_order_semantics")).get("root_cause")
        )
    if decisions_with_root_materiality <= 0 and no_order_rows:
        decisions_with_root_materiality = sum(
            1
            for item in no_order_rows
            if as_dict(item.get("no_order_semantics")).get(
                "root_cause_is_material_without_order_or_fill_change"
            )
            is not None
        )
    if decisions_with_materiality_requirement <= 0 and no_order_rows:
        decisions_with_materiality_requirement = sum(
            1
            for item in no_order_rows
            if as_dict(item.get("no_order_semantics")).get(
                "requires_order_or_fill_change_for_materiality"
            )
            is not None
        )

    all_recent_decisions_no_order_expected = (
        recent_decision_count > 0
        and coverage.get("all_recent_decisions_no_order_expected") is True
    )
    all_no_order_expected_have_semantics = (
        no_order_coverage.get("all_no_order_expected_decisions_have_no_order_semantics")
        is True
        or coverage.get("all_no_order_expected_decisions_have_no_order_semantics") is True
    )
    all_no_order_expected_stable = (
        no_order_coverage.get("all_no_order_expected_decisions_stable_equivalence_class")
        is True
        or coverage.get("all_no_order_expected_decisions_stable_equivalence_class") is True
    )
    all_roots_non_material_without_order_or_fill_change = (
        no_order_coverage.get("all_root_causes_non_material_without_order_or_fill_change")
        is True
        or no_order_semantics.get("root_cause_is_material_without_order_or_fill_change")
        is False
    )
    all_roots_require_order_or_fill_change_for_materiality = (
        no_order_coverage.get(
            "all_root_causes_require_order_or_fill_change_for_materiality"
        )
        is True
        or no_order_semantics.get("requires_order_or_fill_change_for_materiality") is True
    )
    decisions_missing_no_order_semantics = max(
        no_order_expected_decision_count - decisions_with_no_order_semantics,
        0,
    )
    decisions_missing_stable_equivalence_class = max(
        no_order_expected_decision_count
        - decisions_with_stable_no_order_equivalence_class,
        0,
    )
    decisions_missing_root_cause = max(
        no_order_expected_decision_count - decisions_with_root_cause,
        0,
    )
    decisions_missing_root_materiality = max(
        no_order_expected_decision_count - decisions_with_root_materiality,
        0,
    )
    decisions_missing_materiality_requirement = max(
        no_order_expected_decision_count - decisions_with_materiality_requirement,
        0,
    )

    root_cause_distribution = as_list(distributions.get("root_cause"))
    if not root_cause_distribution and no_order_rows:
        root_cause_distribution = compact_count_distribution(
            [
                as_dict(item.get("no_order_semantics")).get("root_cause")
                for item in no_order_rows
            ],
            limit=8,
        )
    equivalence_class_distribution = as_list(distributions.get("equivalence_class"))
    if not equivalence_class_distribution and no_order_rows:
        equivalence_class_distribution = compact_count_distribution(
            [
                as_dict(item.get("no_order_semantics")).get("equivalence_class")
                for item in no_order_rows
            ],
            limit=8,
        )
    route_action_distribution = as_list(distributions.get("route_action"))
    if not route_action_distribution and no_order_rows:
        route_action_distribution = compact_count_distribution(
            [
                as_dict(item.get("no_order_semantics")).get("route_action")
                or item.get("route_action")
                for item in no_order_rows
            ],
            limit=8,
        )
    semantic_status_distribution = as_list(distributions.get("semantic_status"))
    if not semantic_status_distribution and no_order_rows:
        semantic_status_distribution = compact_count_distribution(
            [
                as_dict(item.get("no_order_semantics")).get("status")
                for item in no_order_rows
            ],
            limit=8,
        )

    if attribution.get("ok") is False:
        status = "directional_episode_attribution_unavailable"
        smallest_missing = (
            attribution.get("smallest_missing_field") or "directional_episode_attribution_truth"
        )
        ok = False
    elif recent_decision_count <= 0:
        status = "no_recent_directional_decisions"
        smallest_missing = "portfolio_allocation_decisions.directional"
        ok = False
    elif decisions_with_order_surface_or_no_order_expectation < recent_decision_count:
        status = "missing_recent_directional_order_surface_or_no_order_expectation"
        smallest_missing = "directional_episode_attribution.order_surface_or_no_order_expectation"
        ok = False
    elif not all_recent_decisions_no_order_expected:
        status = "recent_directional_decisions_include_order_expected_cases"
        smallest_missing = "recent_directional_no_order_regime"
        ok = False
    elif not (all_no_order_expected_have_semantics and all_no_order_expected_stable):
        status = "missing_recent_directional_no_order_root_semantics"
        smallest_missing = "directional_episode_attribution.no_order_semantics"
        ok = False
    elif decisions_missing_root_cause > 0:
        status = "missing_recent_directional_no_order_root_cause"
        smallest_missing = "directional_episode_attribution.no_order_semantics.root_cause"
        ok = False
    elif decisions_missing_root_materiality > 0:
        status = "missing_recent_directional_no_order_root_materiality"
        smallest_missing = (
            "directional_episode_attribution.no_order_semantics."
            "root_cause_is_material_without_order_or_fill_change"
        )
        ok = False
    elif decisions_missing_materiality_requirement > 0:
        status = "missing_recent_directional_no_order_materiality_requirement"
        smallest_missing = (
            "directional_episode_attribution.no_order_semantics."
            "requires_order_or_fill_change_for_materiality"
        )
        ok = False
    elif not all_roots_non_material_without_order_or_fill_change:
        status = "unresolved_recent_directional_no_order_root_materiality"
        smallest_missing = "directional_episode_attribution.no_order_semantics.root_materiality"
        ok = False
    elif not all_roots_require_order_or_fill_change_for_materiality:
        status = "unresolved_recent_directional_no_order_materiality_requirement"
        smallest_missing = (
            "directional_episode_attribution.no_order_semantics.materiality_requirement"
        )
        ok = False
    else:
        status = "verified_recent_directional_no_order_root_cause_density"
        smallest_missing = None
        ok = True

    recent_preview: list[dict[str, Any]] = []
    for item in recent_decision_rows[:8]:
        order_expectation = as_dict(item.get("order_expectation"))
        no_order = as_dict(item.get("no_order_semantics"))
        recent_preview.append(
            {
                "decision_id": item.get("decision_id"),
                "created_at": item.get("created_at"),
                "route_action": item.get("route_action"),
                "order_expectation_classification": order_expectation.get(
                    "classification"
                ),
                "no_order_expected": order_expectation.get("no_order_expected"),
                "no_order_semantics_status": no_order.get("status"),
                "no_order_equivalence_class": no_order.get("equivalence_class"),
                "no_order_root_cause": no_order.get("root_cause"),
                "root_cause_is_material_without_order_or_fill_change": no_order.get(
                    "root_cause_is_material_without_order_or_fill_change"
                ),
                "requires_order_or_fill_change_for_materiality": no_order.get(
                    "requires_order_or_fill_change_for_materiality"
                ),
            }
        )

    distribution_source = (
        "directional_episode_attribution_truth.no_order_semantics.distributions"
        if distributions
        else "directional_episode_attribution_truth.recent_decisions_preview"
    )
    return {
        "source": "directional_episode_attribution_truth.no_order_semantics",
        "ok": ok,
        "status": status,
        "smallest_missing_field": smallest_missing,
        "raw_payload_exposed": False,
        "coverage": {
            "recent_decision_count": recent_decision_count,
            "no_order_expected_decision_count": no_order_expected_decision_count,
            "all_recent_decisions_no_order_expected": all_recent_decisions_no_order_expected,
            "decisions_with_order_surface_or_no_order_expectation": (
                decisions_with_order_surface_or_no_order_expectation
            ),
            "decisions_missing_order_surface": decisions_missing_order_surface,
            "decisions_with_no_order_semantics": decisions_with_no_order_semantics,
            "decisions_missing_no_order_semantics": decisions_missing_no_order_semantics,
            "decisions_with_stable_no_order_equivalence_class": (
                decisions_with_stable_no_order_equivalence_class
            ),
            "decisions_missing_stable_no_order_equivalence_class": (
                decisions_missing_stable_equivalence_class
            ),
            "decisions_with_root_cause": decisions_with_root_cause,
            "decisions_missing_root_cause": decisions_missing_root_cause,
            "decisions_with_root_materiality": decisions_with_root_materiality,
            "decisions_missing_root_materiality": decisions_missing_root_materiality,
            "decisions_with_materiality_requirement": (
                decisions_with_materiality_requirement
            ),
            "decisions_missing_materiality_requirement": (
                decisions_missing_materiality_requirement
            ),
            "sampled_recent_decision_count": len(recent_decision_rows),
            "distribution_source": distribution_source,
        },
        "distributions": {
            "root_cause": root_cause_distribution,
            "equivalence_class": equivalence_class_distribution,
            "route_action": route_action_distribution,
            "semantic_status": semantic_status_distribution,
            "root_material_without_order_or_fill_change": as_list(
                distributions.get("root_material_without_order_or_fill_change")
            ),
            "requires_order_or_fill_change_for_materiality": as_list(
                distributions.get("requires_order_or_fill_change_for_materiality")
            ),
        },
        "top_root_cause": compact_distribution_top_value(root_cause_distribution),
        "top_equivalence_class": compact_distribution_top_value(
            equivalence_class_distribution
        ),
        "top_route_action": compact_distribution_top_value(route_action_distribution),
        "recent_decisions": recent_preview,
        "interpretation": {
            "no_order_regime_explained": ok,
            "all_roots_non_material_without_order_or_fill_change": (
                all_roots_non_material_without_order_or_fill_change
            ),
            "all_roots_require_order_or_fill_change_for_materiality": (
                all_roots_require_order_or_fill_change_for_materiality
            ),
            "root_cause_switch_without_order_or_fill_is_non_material": (
                all_roots_non_material_without_order_or_fill_change
                and all_roots_require_order_or_fill_change_for_materiality
            ),
            "not_alpha_or_profitability_evidence": True,
        },
    }


def summarize_latest_directional_no_order_primary_candidate_bridge_truth(
    *,
    db: dict[str, Any],
) -> dict[str, Any]:
    source = (
        "database_truth.latest_decision.execution_truth_chain_and_no_trade_attribution."
        "primary_family_candidate_truth"
    )
    if not db.get("ok"):
        return {
            "source": source,
            "ok": False,
            "status": "live_db_unavailable",
            "smallest_missing_field": "database_truth",
            "raw_payload_exposed": False,
        }

    latest = as_dict(db.get("latest_decision"))
    latest_decision_id = latest.get("decision_id")
    if not latest_decision_id:
        return {
            "source": source,
            "ok": False,
            "status": "missing_latest_decision_truth",
            "smallest_missing_field": "database_truth.latest_decision.decision_id",
            "raw_payload_exposed": False,
        }

    truth_chain = as_dict(latest.get("execution_truth_chain"))
    no_trade = as_dict(latest.get("no_trade_attribution"))
    primary_candidate = as_dict(no_trade.get("primary_family_candidate_truth"))
    latest_route_action = latest.get("route_action")
    if not latest_route_action:
        latest_route_action = next(
            (
                as_dict(item).get("route_action")
                for item in as_list(no_trade.get("blocker_chain"))
                if as_dict(item).get("route_action")
            ),
            None,
        )

    latest_order_expected = truth_chain.get("order_expected")
    if latest_order_expected is None and no_trade.get("classification") == (
        "no_order_fill_expected_for_latest_decision"
    ):
        latest_order_expected = False
    latest_fill_expected = truth_chain.get("fill_expected")
    if latest_fill_expected is None and no_trade.get("classification") == (
        "no_order_fill_expected_for_latest_decision"
    ):
        latest_fill_expected = False

    primary_candidate_no_order_semantics = classify_primary_candidate_no_order_semantics(
        primary_candidate
    )
    primary_candidate_route_action = primary_candidate.get("candidate_route_action")
    primary_candidate_order_expected = primary_candidate.get(
        "order_expected_from_primary_candidate"
    )
    primary_candidate_root_cause = primary_candidate_no_order_semantics.get("root_cause")
    portfolio_route_no_order_root_cause = (
        f"decision_route_action_{latest_route_action}_no_order_expected"
        if latest_route_action in NO_ORDER_ROUTE_ACTIONS
        else None
    )
    route_action_differs = (
        latest_route_action is not None
        and primary_candidate_route_action is not None
        and latest_route_action != primary_candidate_route_action
    )
    route_root_and_primary_candidate_root_distinct = (
        portfolio_route_no_order_root_cause is not None
        and primary_candidate_root_cause is not None
        and portfolio_route_no_order_root_cause != primary_candidate_root_cause
    )

    no_trade_is_current = no_trade.get("is_current_no_trade") is True
    primary_family = primary_candidate.get("primary_family")
    if latest_order_expected is True:
        status = "latest_decision_order_expected_bridge_not_applicable"
        smallest_missing = None
        ok = True
    elif not no_trade_is_current:
        status = "latest_decision_not_current_no_trade"
        smallest_missing = (
            None
            if latest_order_expected is not False
            else "database_truth.latest_decision.no_trade_attribution.is_current_no_trade"
        )
        ok = latest_order_expected is not False
    elif not primary_candidate:
        status = "missing_latest_directional_primary_candidate_truth"
        smallest_missing = (
            "database_truth.latest_decision.no_trade_attribution."
            "primary_family_candidate_truth"
        )
        ok = False
    elif primary_family != "directional":
        status = "latest_primary_candidate_not_directional"
        smallest_missing = (
            "database_truth.latest_decision.no_trade_attribution."
            "primary_family_candidate_truth.primary_family"
        )
        ok = False
    elif primary_candidate_no_order_semantics.get("status") != (
        "verified_primary_candidate_no_order_expected_semantics"
    ):
        status = "missing_latest_directional_primary_candidate_no_order_semantics"
        smallest_missing = (
            primary_candidate.get("smallest_missing_field")
            or "database_truth.latest_decision.no_trade_attribution."
            "primary_family_candidate_truth.no_order_root_cause"
        )
        ok = False
    elif latest_order_expected is not False:
        status = "missing_latest_decision_order_expectation"
        smallest_missing = "database_truth.latest_decision.execution_truth_chain.order_expected"
        ok = False
    elif primary_candidate_order_expected is not False:
        status = "latest_directional_primary_candidate_order_expected"
        smallest_missing = (
            "database_truth.latest_decision.no_trade_attribution."
            "primary_family_candidate_truth.order_expected_from_primary_candidate"
        )
        ok = False
    else:
        status = "verified_latest_directional_no_order_primary_candidate_bridge"
        smallest_missing = None
        ok = True

    return {
        "source": source,
        "ok": ok,
        "status": status,
        "smallest_missing_field": smallest_missing,
        "raw_payload_exposed": False,
        "latest_decision": {
            "decision_id": latest_decision_id,
            "created_at": latest.get("created_at"),
            "symbol": latest.get("symbol"),
            "primary_family": latest.get("primary_family"),
            "route_action": latest_route_action,
            "no_trade_classification": no_trade.get("classification"),
            "no_trade_primary_blocker": no_trade.get("primary_blocker"),
            "is_current_no_trade": no_trade.get("is_current_no_trade"),
            "order_expected": latest_order_expected,
            "fill_expected": latest_fill_expected,
            "portfolio_route_no_order_root_cause": portfolio_route_no_order_root_cause,
        },
        "primary_candidate": {
            "truth_status": primary_candidate.get("status"),
            "family": primary_family,
            "route_action": primary_candidate_route_action,
            "execution_behavior": primary_candidate.get("candidate_execution_behavior"),
            "order_expected": primary_candidate_order_expected,
            "approved_for_execution": primary_candidate.get(
                "candidate_approved_for_execution"
            ),
            "permission_mode": primary_candidate.get("candidate_permission_mode"),
            "execution_compatible": primary_candidate.get(
                "candidate_execution_compatible"
            ),
            "target_notional": primary_candidate.get("target_notional"),
            "composed_delta_position_qty": primary_candidate.get(
                "composed_delta_position_qty"
            ),
            "global_primary_blocker": primary_candidate.get("global_primary_blocker"),
            "global_blocker_applies_to_candidate": primary_candidate.get(
                "global_primary_blocker_applies_to_candidate"
            ),
            "global_blocker_scope": primary_candidate.get("global_blocker_scope"),
            "no_order_root_cause": primary_candidate_root_cause,
            "no_order_semantic_status": primary_candidate_no_order_semantics.get(
                "status"
            ),
            "no_order_equivalence_class": primary_candidate_no_order_semantics.get(
                "equivalence_class"
            ),
            "root_cause_is_material_without_order_or_fill_change": (
                primary_candidate_no_order_semantics.get(
                    "root_cause_is_material_without_order_or_fill_change"
                )
            ),
            "requires_order_or_fill_change_for_materiality": (
                primary_candidate_no_order_semantics.get(
                    "requires_order_or_fill_change_for_materiality"
                )
            ),
        },
        "bridge": {
            "latest_route_action": latest_route_action,
            "primary_candidate_route_action": primary_candidate_route_action,
            "latest_route_action_differs_from_primary_candidate_route_action": (
                route_action_differs
            ),
            "portfolio_route_no_order_root_cause": portfolio_route_no_order_root_cause,
            "primary_candidate_no_order_root_cause": primary_candidate_root_cause,
            "route_root_and_primary_candidate_root_distinct": (
                route_root_and_primary_candidate_root_distinct
            ),
            "global_blocker_scope": primary_candidate.get("global_blocker_scope"),
            "global_blocker_applies_to_candidate": primary_candidate.get(
                "global_primary_blocker_applies_to_candidate"
            ),
            "latest_decision_order_expected": latest_order_expected,
            "primary_candidate_order_expected": primary_candidate_order_expected,
        },
        "interpretation": {
            "portfolio_route_action_is_not_primary_candidate_root": (
                route_root_and_primary_candidate_root_distinct
            ),
            "hold_current_zero_delta_explains_primary_directional_no_order": (
                primary_candidate_root_cause == "primary_candidate_hold_current_zero_delta"
            ),
            "advisory_only_route_action_explains_portfolio_route_no_order": (
                portfolio_route_no_order_root_cause
                == "decision_route_action_advisory_only_no_order_expected"
            ),
            "global_blocker_is_other_candidate_or_portfolio_level": (
                primary_candidate.get("global_blocker_scope")
                == "other_candidate_or_portfolio_level"
                and primary_candidate.get("global_primary_blocker_applies_to_candidate")
                is False
            ),
            "root_switch_without_order_or_fill_is_non_material": (
                primary_candidate_no_order_semantics.get(
                    "root_cause_is_material_without_order_or_fill_change"
                )
                is False
                and primary_candidate_no_order_semantics.get(
                    "requires_order_or_fill_change_for_materiality"
                )
                is True
            ),
            "not_alpha_or_profitability_evidence": True,
        },
    }


def summarize_recent_directional_no_order_primary_candidate_bridge_density_truth(
    *,
    directional_attribution: dict[str, Any],
    recent_no_order_root_density: dict[str, Any],
    latest_bridge: dict[str, Any],
) -> dict[str, Any]:
    attribution = as_dict(directional_attribution)
    recent_root = as_dict(recent_no_order_root_density)
    latest = as_dict(latest_bridge)
    attribution_coverage = as_dict(attribution.get("coverage"))
    root_coverage = as_dict(recent_root.get("coverage"))
    root_distributions = as_dict(recent_root.get("distributions"))
    root_interpretation = as_dict(recent_root.get("interpretation"))
    latest_latest_decision = as_dict(latest.get("latest_decision"))
    latest_bridge_fields = as_dict(latest.get("bridge"))
    latest_interpretation = as_dict(latest.get("interpretation"))

    recent_decision_rows = [
        as_dict(row) for row in as_list(attribution.get("recent_decisions"))
    ]
    recent_decision_ids = [
        row.get("decision_id") for row in recent_decision_rows if row.get("decision_id")
    ]
    latest_bridge_decision_id = latest_latest_decision.get("decision_id")
    latest_bridge_decision_present_in_recent_decisions = (
        latest_bridge_decision_id in recent_decision_ids
        if latest_bridge_decision_id and recent_decision_ids
        else None
    )

    recent_decision_count = int_or_zero(
        root_coverage.get(
            "recent_decision_count",
            attribution_coverage.get("recent_decision_count"),
        )
    )
    no_order_expected_decision_count = int_or_zero(
        root_coverage.get(
            "no_order_expected_decision_count",
            attribution_coverage.get("decisions_with_no_order_expected"),
        )
    )
    decisions_with_no_order_semantics = int_or_zero(
        root_coverage.get(
            "decisions_with_no_order_semantics",
            attribution_coverage.get("decisions_with_no_order_semantics"),
        )
    )
    decisions_with_root_cause = int_or_zero(
        root_coverage.get("decisions_with_root_cause")
    )
    decisions_with_root_materiality = int_or_zero(
        root_coverage.get("decisions_with_root_materiality")
    )
    latest_bridge_verified = (
        latest.get("status")
        == "verified_latest_directional_no_order_primary_candidate_bridge"
    )
    recent_root_verified = (
        recent_root.get("status")
        == "verified_recent_directional_no_order_root_cause_density"
    )
    route_root_and_primary_candidate_root_distinct = (
        latest_bridge_fields.get("route_root_and_primary_candidate_root_distinct") is True
    )
    route_action_differs = (
        latest_bridge_fields.get(
            "latest_route_action_differs_from_primary_candidate_route_action"
        )
        is True
    )
    raw_payload_exposed = (
        recent_root.get("raw_payload_exposed") is True
        or latest.get("raw_payload_exposed") is True
    )

    if attribution.get("ok") is False:
        status = "directional_episode_attribution_unavailable"
        smallest_missing = (
            attribution.get("smallest_missing_field") or "directional_episode_attribution_truth"
        )
        ok = False
    elif recent_decision_count <= 0:
        status = "no_recent_directional_decisions"
        smallest_missing = "portfolio_allocation_decisions.directional"
        ok = False
    elif raw_payload_exposed:
        status = "recent_directional_no_order_bridge_density_raw_payload_exposed"
        smallest_missing = "raw_payload_redaction"
        ok = False
    elif not recent_root_verified:
        status = "missing_recent_directional_no_order_root_density"
        smallest_missing = (
            recent_root.get("smallest_missing_field")
            or "recent_directional_no_order_root_cause_density_truth"
        )
        ok = False
    elif not latest_bridge_verified:
        status = "missing_latest_directional_no_order_primary_candidate_bridge"
        smallest_missing = (
            latest.get("smallest_missing_field")
            or "latest_directional_no_order_primary_candidate_bridge_truth"
        )
        ok = False
    elif latest_bridge_decision_present_in_recent_decisions is False:
        status = "latest_bridge_decision_not_in_recent_directional_window"
        smallest_missing = "directional_episode_attribution.recent_decisions.latest_bridge_decision"
        ok = False
    elif not route_root_and_primary_candidate_root_distinct:
        status = "latest_bridge_roots_not_distinct"
        smallest_missing = "latest_directional_no_order_primary_candidate_bridge_truth.bridge.root_distinction"
        ok = False
    else:
        status = "verified_recent_directional_no_order_primary_candidate_bridge_density"
        smallest_missing = None
        ok = True

    return {
        "source": (
            "recent_directional_no_order_root_cause_density_truth_plus_"
            "latest_directional_no_order_primary_candidate_bridge_truth"
        ),
        "ok": ok,
        "status": status,
        "smallest_missing_field": smallest_missing,
        "raw_payload_exposed": False,
        "coverage": {
            "recent_decision_count": recent_decision_count,
            "no_order_expected_decision_count": no_order_expected_decision_count,
            "decisions_with_no_order_semantics": decisions_with_no_order_semantics,
            "decisions_with_root_cause": decisions_with_root_cause,
            "decisions_with_root_materiality": decisions_with_root_materiality,
            "recent_portfolio_route_root_density_status": recent_root.get("status"),
            "latest_primary_candidate_bridge_status": latest.get("status"),
            "latest_primary_candidate_bridge_verified": latest_bridge_verified,
            "latest_bridge_decision_id": latest_bridge_decision_id,
            "latest_bridge_decision_present_in_recent_decisions": (
                latest_bridge_decision_present_in_recent_decisions
            ),
            "historical_primary_candidate_bridge_scope": "latest_decision_only",
            "historical_primary_candidate_bridge_available_for_recent_decisions": False,
            "historical_primary_candidate_bridge_not_claimed": True,
        },
        "recent_portfolio_route_roots": {
            "top_root_cause": recent_root.get("top_root_cause"),
            "top_equivalence_class": recent_root.get("top_equivalence_class"),
            "top_route_action": recent_root.get("top_route_action"),
            "root_cause_distribution": as_list(root_distributions.get("root_cause")),
            "route_action_distribution": as_list(
                root_distributions.get("route_action")
            ),
            "equivalence_class_distribution": as_list(
                root_distributions.get("equivalence_class")
            ),
        },
        "latest_bridge": {
            "decision_id": latest_bridge_decision_id,
            "status": latest.get("status"),
            "latest_route_action": latest_bridge_fields.get("latest_route_action"),
            "primary_candidate_route_action": latest_bridge_fields.get(
                "primary_candidate_route_action"
            ),
            "route_action_differs": route_action_differs,
            "portfolio_route_no_order_root_cause": latest_bridge_fields.get(
                "portfolio_route_no_order_root_cause"
            ),
            "primary_candidate_no_order_root_cause": latest_bridge_fields.get(
                "primary_candidate_no_order_root_cause"
            ),
            "route_root_and_primary_candidate_root_distinct": (
                route_root_and_primary_candidate_root_distinct
            ),
            "latest_decision_order_expected": latest_bridge_fields.get(
                "latest_decision_order_expected"
            ),
            "primary_candidate_order_expected": latest_bridge_fields.get(
                "primary_candidate_order_expected"
            ),
        },
        "interpretation": {
            "recent_portfolio_route_roots_non_material_without_order_or_fill_change": (
                root_interpretation.get(
                    "all_roots_non_material_without_order_or_fill_change"
                )
            ),
            "recent_portfolio_route_roots_require_order_or_fill_change_for_materiality": (
                root_interpretation.get(
                    "all_roots_require_order_or_fill_change_for_materiality"
                )
            ),
            "latest_primary_candidate_root_distinct_from_portfolio_route_root": (
                route_root_and_primary_candidate_root_distinct
            ),
            "latest_portfolio_route_action_is_not_primary_candidate_root": (
                latest_interpretation.get(
                    "portfolio_route_action_is_not_primary_candidate_root"
                )
            ),
            "latest_hold_current_zero_delta_explains_primary_directional_no_order": (
                latest_interpretation.get(
                    "hold_current_zero_delta_explains_primary_directional_no_order"
                )
            ),
            "historical_primary_candidate_bridge_not_claimed": True,
            "not_alpha_or_profitability_evidence": True,
        },
    }


def summarize_latest_decision_fill_feasibility_truth(
    db: dict[str, Any],
    directional_attribution: dict[str, Any],
    execution_science: dict[str, Any],
) -> dict[str, Any]:
    if not db.get("ok"):
        return {
            "source": "live_db_directional_episode_attribution_and_rdp_microstructure",
            "ok": False,
            "status": "live_db_unavailable",
            "smallest_missing_field": "database_truth",
        }

    latest = as_dict(db.get("latest_decision"))
    latest_decision_id = latest.get("decision_id")
    if not latest_decision_id:
        return {
            "source": "live_db_directional_episode_attribution_and_rdp_microstructure",
            "ok": True,
            "status": "missing_latest_decision_truth",
            "smallest_missing_field": "database_truth.latest_decision.decision_id",
        }

    truth_chain = as_dict(latest.get("execution_truth_chain"))
    no_trade = as_dict(latest.get("no_trade_attribution"))
    primary_candidate = as_dict(no_trade.get("primary_family_candidate_truth"))
    primary_candidate_no_order_semantics = classify_primary_candidate_no_order_semantics(
        primary_candidate
    )
    recent_decision = next(
        (
            as_dict(item)
            for item in as_list(directional_attribution.get("recent_decisions"))
            if as_dict(item).get("decision_id") == latest_decision_id
        ),
        {},
    )
    pretrade = as_dict(recent_decision.get("pretrade_microstructure"))
    decision_context = as_dict(pretrade.get("decision_context"))
    orderbook = as_dict(decision_context.get("orderbook"))
    trade_flow = as_dict(decision_context.get("trade_flow"))
    pretrade_status = pretrade.get("status")
    pretrade_context_present = pretrade_status == "verified_pretrade_microstructure_context_present"
    order_expected = truth_chain.get("order_expected")
    if order_expected is None:
        order_expected = primary_candidate.get("order_expected_from_primary_candidate")
    fill_expected = truth_chain.get("fill_expected")
    fill_feasibility_applicable = bool(order_expected)

    if not recent_decision:
        smallest_missing = "directional_episode_attribution.latest_decision"
    elif pretrade_context_present:
        smallest_missing = None
    else:
        smallest_missing = pretrade.get("smallest_missing_field") or "directional_episode.pretrade_microstructure"

    if not fill_feasibility_applicable and pretrade_context_present:
        status = "verified_no_order_fill_feasibility_not_applicable_with_pretrade_context"
    elif not fill_feasibility_applicable:
        status = "no_order_fill_feasibility_not_applicable_missing_pretrade_context"
    elif pretrade_context_present:
        status = "verified_order_expected_pretrade_fill_feasibility_context_present"
    else:
        status = "missing_order_expected_pretrade_fill_feasibility_context"

    return {
        "source": "live_db_directional_episode_attribution_and_rdp_microstructure",
        "ok": True,
        "status": status,
        "smallest_missing_field": smallest_missing,
        "decision_id": latest_decision_id,
        "created_at": latest.get("created_at") or recent_decision.get("created_at"),
        "symbol": latest.get("symbol") or recent_decision.get("symbol"),
        "route_action": latest.get("route_action") or recent_decision.get("route_action"),
        "primary_family": latest.get("primary_family") or recent_decision.get("primary_family"),
        "order_expected": order_expected,
        "fill_expected": fill_expected,
        "fill_feasibility_applicable": fill_feasibility_applicable,
        "execution_truth_status": truth_chain.get("status"),
        "no_order": {
            "classification": no_trade.get("classification"),
            "primary_blocker": no_trade.get("primary_blocker"),
            "final_blockers": no_trade.get("final_blockers"),
            "primary_candidate_truth_status": primary_candidate.get("status"),
            "primary_candidate_order_expected": primary_candidate.get(
                "order_expected_from_primary_candidate"
            ),
            "primary_candidate_no_order_root_cause": primary_candidate.get("no_order_root_cause"),
            "primary_candidate_no_order_semantics": primary_candidate_no_order_semantics,
            "primary_candidate_smallest_missing_field": primary_candidate.get("smallest_missing_field"),
        },
        "pretrade_microstructure": {
            "source": pretrade.get("source"),
            "status": pretrade_status,
            "smallest_missing_field": pretrade.get("smallest_missing_field"),
            "orderbook": {
                "bar_ts": orderbook.get("bar_ts"),
                "bar_age_seconds": orderbook.get("bar_age_seconds"),
                "bbo_samples_n": orderbook.get("bbo_samples_n"),
                "books5_samples_n": orderbook.get("books5_samples_n"),
                "mid_price_last": orderbook.get("mid_price_last"),
                "spread_bps_mean": orderbook.get("spread_bps_mean"),
                "spread_bps_max": orderbook.get("spread_bps_max"),
                "spread_bps_min": orderbook.get("spread_bps_min"),
                "quality_flags": orderbook.get("quality_flags"),
            },
            "trade_flow": {
                "bar_ts": trade_flow.get("bar_ts"),
                "bar_age_seconds": trade_flow.get("bar_age_seconds"),
                "trade_count": trade_flow.get("trade_count"),
                "total_volume_ccy": trade_flow.get("total_volume_ccy"),
                "taker_buy_ratio": trade_flow.get("taker_buy_ratio"),
                "trade_flow_imbalance": trade_flow.get("trade_flow_imbalance"),
                "vwap_minus_mid_bps": trade_flow.get("vwap_minus_mid_bps"),
                "quality_flags": trade_flow.get("quality_flags"),
            },
        },
        "execution_science": {
            "status": execution_science.get("status"),
            "fill_feasibility_truth_status": execution_science.get("fill_feasibility_truth_status"),
            "orderbook_sequence_validation_status": as_dict(
                execution_science.get("payload_sequence")
            ).get("status"),
            "silver_orderbook_status": as_dict(execution_science.get("silver_orderbook")).get("status"),
            "silver_trade_flow_status": as_dict(execution_science.get("silver_trade_flow")).get("status"),
        },
        "interpretation": {
            "fill_feasibility_applicable_when_order_expected": True,
            "no_order_decision": "fill feasibility is not expected to produce an OKX order/fill",
            "pretrade_context": "silver orderbook and trade-flow context still explains the decision-time market state",
        },
    }


def summarize_directional_executable_terminal_no_fill_pretrade_microstructure_truth(
    *,
    directional_executable_episode: dict[str, Any],
    rdp_microstructure: dict[str, Any],
    execution_science: dict[str, Any],
    orderbook_payload_depth: dict[str, Any],
    depth_slippage_lifecycle: dict[str, Any],
    slippage_cost: dict[str, Any],
) -> dict[str, Any]:
    executable = as_dict(directional_executable_episode)
    latest = as_dict(executable.get("latest_executable_decision"))
    decision_id = latest.get("decision_id")
    if not decision_id:
        return {
            "source": "directional_executable_episode_truth_and_rdp_microstructure",
            "ok": False,
            "status": "missing_executable_terminal_no_fill_decision",
            "smallest_missing_field": "directional_executable_episode.latest_executable_decision",
        }

    terminal_drilldown = as_dict(executable.get("terminal_no_fill_drilldown"))
    terminal_coverage = as_dict(terminal_drilldown.get("coverage"))
    terminal_verified = (
        executable.get("status") == "verified_executable_terminal_order_no_fill_truth"
        and terminal_drilldown.get("status") == "verified_terminal_no_fill_order_state_drilldown"
    )
    decision_for_microstructure = {
        "decision_id": decision_id,
        "created_at": latest.get("created_at"),
        "symbol": latest.get("symbol"),
        "route_action": latest.get("route_action"),
        "primary_family": latest.get("primary_family"),
        "fill": {"count": 0},
        "latest_fill": {},
    }
    pretrade = _directional_episode_microstructure_context(
        decision_for_microstructure,
        rdp_microstructure=as_dict(rdp_microstructure),
    )
    sequence = as_dict(execution_science.get("payload_sequence"))
    depth = as_dict(orderbook_payload_depth)
    depth_sequence = as_dict(depth.get("sequence"))
    depth_lifecycle = as_dict(depth_slippage_lifecycle)
    depth_lifecycle_interpretation = as_dict(depth_lifecycle.get("interpretation"))
    slippage = as_dict(slippage_cost)
    slippage_proxy = as_dict(slippage.get("slippage_proxy"))
    slippage_coverage = as_dict(slippage_proxy.get("coverage_audit"))

    pretrade_present = pretrade.get("status") == "verified_pretrade_microstructure_context_present"
    sequence_continuous = sequence.get("status") == "sequence_continuous"
    depth_ready = depth.get("status") == "verified_books5_payload_depth_evidence_present"
    slippage_verified = slippage.get("status") == "verified_slippage_cost_calibration_evidence_present"
    order_count = int_or_zero(terminal_coverage.get("drilldown_order_count"))
    exchange_ack_absent_count = int_or_zero(terminal_coverage.get("exchange_ack_absent_count"))
    fill_absent_count = int_or_zero(terminal_coverage.get("fill_absent_count"))
    all_orders_lack_exchange_ack = order_count > 0 and exchange_ack_absent_count >= order_count
    all_orders_lack_fill = order_count > 0 and fill_absent_count >= order_count
    local_fill_feasibility_status = (
        "terminal_no_fill_before_exchange_ack"
        if terminal_verified and all_orders_lack_exchange_ack and all_orders_lack_fill
        else "terminal_no_fill_local_feasibility_incomplete"
    )

    checks = [
        ("directional_executable_episode.terminal_no_fill_drilldown", terminal_verified),
        ("rdp.silver.executable_decision_pretrade_microstructure", pretrade_present),
        ("execution_science.payload_sequence.sequence_continuous", sequence_continuous),
        ("orderbook_payload_depth.verified_books5_depth_evidence", depth_ready),
        ("slippage_cost_calibration_truth.verified", slippage_verified),
    ]
    smallest_missing = next((field for field, passed in checks if not passed), None)
    if not terminal_verified:
        status = "blocked_missing_terminal_no_fill_drilldown"
    elif not pretrade_present:
        status = "missing_executable_terminal_no_fill_pretrade_microstructure_context"
    elif not sequence_continuous:
        status = "blocked_missing_snapshot_diff_sequence_validation"
    elif not depth_ready:
        status = "blocked_missing_orderbook_payload_depth"
    elif not slippage_verified:
        status = "blocked_missing_slippage_cost_calibration_context"
    else:
        status = "verified_executable_terminal_no_fill_pretrade_microstructure_drilldown"

    return {
        "source": "directional_executable_episode_truth_and_rdp_microstructure",
        "ok": smallest_missing is None,
        "status": status,
        "smallest_missing_field": smallest_missing,
        "decision": {
            "decision_id": decision_id,
            "created_at": latest.get("created_at"),
            "symbol": latest.get("symbol"),
            "route_action": latest.get("route_action"),
            "primary_family": latest.get("primary_family"),
            "order_expected": latest.get("order_expected"),
            "fill_expected": latest.get("fill_expected"),
            "expected_edge_bps": latest.get("expected_edge_bps"),
            "expected_cost_bps": latest.get("expected_cost_bps"),
        },
        "pretrade_microstructure": pretrade,
        "snapshot_diff_sequence": {
            "status": sequence.get("status"),
            "window_minutes": sequence.get("window_minutes"),
            "sequence_gap_count": sequence.get("sequence_gap_count"),
            "latest_ts": sequence.get("latest_ts"),
        },
        "orderbook_payload_depth": {
            "status": depth.get("status"),
            "books5_row_count": depth_sequence.get("books5_row_count"),
            "books5_sequence_gap_count": depth_sequence.get("books5_sequence_gap_count"),
            "diff_payload_persisted_row_count": depth_sequence.get("diff_payload_persisted_row_count"),
            "raw_payload_exposed": depth.get("raw_payload_exposed"),
        },
        "local_fill_feasibility": {
            "status": local_fill_feasibility_status,
            "order_expected": latest.get("order_expected"),
            "fill_expected": latest.get("fill_expected"),
            "market_fill_feasibility_observable": not all_orders_lack_exchange_ack,
            "exchange_ack_absent_count": exchange_ack_absent_count,
            "fill_absent_count": fill_absent_count,
            "terminal_order_count": order_count,
            "terminal_states": as_dict(executable.get("terminal_no_fill")).get("terminal_states"),
            "terminal_source_systems": as_dict(executable.get("terminal_no_fill")).get(
                "terminal_source_systems"
            ),
            "terminal_execution_styles": as_dict(executable.get("terminal_no_fill")).get(
                "terminal_execution_styles"
            ),
        },
        "slippage_baseline": {
            "status": slippage.get("status"),
            "fee_sample_count": as_dict(slippage.get("fee")).get("sample_count"),
            "slippage_proxy_sample_count": slippage_proxy.get("sample_count"),
            "reference_coverage_classification": slippage_coverage.get("classification"),
            "current_submit_reference_covered_fill_count": slippage_coverage.get(
                "covered_reference_fills_with_command_reference"
            ),
            "missing_reference_fills": slippage_coverage.get("missing_reference_fills"),
            "reference_policy": slippage_coverage.get("reference_policy"),
        },
        "depth_slippage_lifecycle": {
            "status": depth_lifecycle.get("status"),
            "forward_depth_ready": depth_lifecycle_interpretation.get("forward_depth_ready"),
            "existing_fill_slippage_baseline_present": depth_lifecycle_interpretation.get(
                "existing_fill_slippage_baseline_present"
            ),
        },
        "interpretation": {
            "decision_time_pretrade_context_present": pretrade_present,
            "snapshot_diff_sequence_current": sequence_continuous,
            "market_fill_feasibility_requires_exchange_ack": True,
            "terminal_no_fill_before_exchange_ack": all_orders_lack_exchange_ack,
            "not_alpha_or_profitability_evidence": True,
            "raw_payload_exposed": False,
        },
    }


def summarize_directional_spike_reversion_truth(
    directional_attribution: dict[str, Any],
) -> dict[str, Any]:
    recent = [as_dict(item) for item in as_list(directional_attribution.get("recent_decisions"))]
    filled = [item for item in recent if int_or_zero(as_dict(item.get("fill")).get("count")) > 0]
    contexts = [_directional_spike_reversion_context(item) for item in filled]
    verified_contexts = [
        item for item in contexts if item.get("status") == "verified_spike_reversion_context_present"
    ]
    latest_context = contexts[0] if contexts else {}
    smallest_missing = latest_context.get("smallest_missing_field") or next(
        (item.get("smallest_missing_field") for item in contexts if item.get("smallest_missing_field")),
        None,
    )
    adverse_fill_10bps = sum(
        1
        for item in verified_contexts
        if (decimal_value(item.get("adverse_fill_vs_decision_mid_bps")) or Decimal("0")) >= Decimal("10")
    )
    post_fill_adverse_reversion_10bps = sum(
        1
        for item in verified_contexts
        if (decimal_value(item.get("post_fill_mid_move_bps")) or Decimal("0")) <= Decimal("-10")
    )
    trade_flow_dislocation_10bps = sum(
        1
        for item in verified_contexts
        if abs(decimal_value(item.get("decision_trade_flow_vwap_minus_mid_bps")) or Decimal("0")) >= Decimal("10")
    )
    if not filled:
        status = "no_recent_filled_directional_decisions"
    elif not verified_contexts:
        status = "missing_directional_spike_reversion_context"
    else:
        status = "verified_directional_spike_reversion_execution_context_present"
    return {
        "source": "directional_episode_attribution_truth.pretrade_microstructure",
        "status": status,
        "smallest_missing_field": None if verified_contexts else smallest_missing,
        "coverage": {
            "recent_filled_directional_decision_count": len(filled),
            "filled_decisions_with_spike_reversion_context": len(verified_contexts),
            "adverse_fill_vs_decision_mid_10bps_count": adverse_fill_10bps,
            "post_fill_adverse_reversion_10bps_count": post_fill_adverse_reversion_10bps,
            "decision_trade_flow_dislocation_10bps_count": trade_flow_dislocation_10bps,
        },
        "latest_filled_decision": latest_context,
        "recent_contexts": contexts[:6],
        "interpretation": {
            "adverse_fill_vs_decision_mid_bps": (
                "positive means buy filled above decision mid or sell filled below decision mid"
            ),
            "post_fill_mid_move_bps": (
                "positive means post-fill mid moved in fill direction; negative means immediate adverse reversion"
            ),
            "thresholds": "10 bps counters are diagnostic only and do not gate execution",
        },
    }


def git_truth(repo_root: Path, distro: str, wsl_project: str) -> dict[str, Any]:
    win_status = run_command(["git", "status", "--short", "--branch"], cwd=repo_root)
    win_head = run_command(["git", "rev-parse", "HEAD"], cwd=repo_root)
    win_divergence = run_command(
        ["git", "rev-list", "--left-right", "--count", "HEAD...origin/main"],
        cwd=repo_root,
    )
    win_log = run_command(["git", "log", "-1", "--oneline"], cwd=repo_root)

    wsl_script = (
        f"cd {bash_cd_target(wsl_project)} && "
        "git status -sb && "
        "git rev-parse HEAD && "
        "git log -1 --oneline"
    )
    wsl = run_command(["wsl", "-d", distro, "--", "bash", "-lc", wsl_script], timeout=30)
    wsl_lines = wsl["stdout"].splitlines() if wsl["stdout"] else []
    wsl_status_line = wsl_lines[0] if wsl_lines else ""
    wsl_head = wsl_lines[1] if len(wsl_lines) > 1 else None
    wsl_log = wsl_lines[2] if len(wsl_lines) > 2 else None

    win_status_lines = win_status["stdout"].splitlines() if win_status["stdout"] else []
    win_status_line = win_status_lines[0] if win_status_lines else ""
    win_dirty = any(line and not line.startswith("## ") for line in win_status_lines)

    return {
        "windows": {
            "ok": win_status["ok"] and win_head["ok"],
            "status_header": parse_git_status_header(win_status_line),
            "dirty": win_dirty,
            "head": win_head["stdout"] or None,
            "latest_commit": win_log["stdout"] or None,
            "origin_divergence": parse_left_right_count(win_divergence["stdout"]),
        },
        "wsl": {
            "ok": wsl["ok"],
            "status_header": parse_git_status_header(wsl_status_line),
            "head": wsl_head,
            "latest_commit": wsl_log,
        },
        "deployed_matches_windows": bool(win_head["stdout"] and wsl_head and win_head["stdout"] == wsl_head),
    }


def deployment_health(api_base: str, distro: str) -> dict[str, Any]:
    health = gateway_health_probe(api_base, distro)
    docker_ps = run_command(
        ["wsl", "-d", distro, "--", "docker", "ps", "--format", "{{.Names}}\t{{.Status}}"],
        timeout=30,
    )
    statuses = parse_docker_ps(docker_ps["stdout"]) if docker_ps["ok"] else {}
    return {
        "gateway_health": {
            "ok": bool(health.get("ok") and (health.get("json") or {}).get("status") == "ok"),
            "http_status": health.get("status"),
            "payload": health.get("json"),
            "error": health.get("error"),
        },
        "containers": summarize_container_health(statuses),
    }


def static_truth_surface(api_base: str) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for path, markers in STATIC_MARKERS.items():
        fetched = fetch_url_text(f"{api_base.rstrip('/')}{path}", timeout=10)
        body = fetched.get("body") or ""
        marker_status = {marker: marker in body for marker in markers}
        results[path] = {
            "ok": bool(fetched["ok"] and all(marker_status.values())),
            "http_status": fetched.get("status"),
            "markers": marker_status,
            "error": fetched.get("error"),
        }
    return results


def load_artifact_runtime_facts(repo_root: Path) -> dict[str, Any]:
    return load_artifact_runtime_projection(repo_root, utc_now_iso())["facts"]


def _merge_artifact_runtime_facts(
    *,
    facts: dict[str, Any],
    sources: list[dict[str, Any]],
    rel: Path,
    kind: str,
    payload_generated_at: Any,
    runtime_facts: Any,
) -> None:
    if not isinstance(runtime_facts, dict):
        return
    facts.update(runtime_facts)
    sources.append(
        {
            "path": rel.as_posix(),
            "kind": kind,
            "generated_at": runtime_facts.get("runtime_truth_generated_at") or payload_generated_at,
            "fact_keys": sorted(str(key) for key in runtime_facts.keys()),
        },
    )


def load_artifact_runtime_projection(repo_root: Path, report_generated_at: str) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    sources: list[dict[str, Any]] = []
    for rel in (
        Path("artifacts/automation/task_registry.json"),
        Path("artifacts/automation/current_state.json"),
    ):
        path = repo_root / rel
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        payload_generated_at = payload.get("generated_at")
        _merge_artifact_runtime_facts(
            facts=facts,
            sources=sources,
            rel=rel,
            kind="latest_runtime_facts",
            payload_generated_at=payload_generated_at,
            runtime_facts=payload.get("latest_runtime_facts"),
        )
        latest = payload.get("latest_pm_loop_check")
        if isinstance(latest, dict) and isinstance(latest.get("runtime_truth"), dict):
            _merge_artifact_runtime_facts(
                facts=facts,
                sources=sources,
                rel=rel,
                kind="latest_pm_loop_check.runtime_truth",
                payload_generated_at=latest.get("completed_at") or payload_generated_at,
                runtime_facts=latest.get("runtime_truth"),
            )
    return {
        "facts": facts,
        "sources": sources,
        "status": {
            "source": "artifact_last_known",
            "status": "pending_live_comparison",
            "may_override_live": False,
            "report_generated_at": report_generated_at,
        },
    }


def _relative_artifact_path(repo_root: Path, path: Path) -> str:
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError:
        return path.as_posix()


def load_latest_claimed_submit_operator_handoff(repo_root: Path) -> dict[str, Any] | None:
    artifact_dir = repo_root / "artifacts" / "automation"
    if not artifact_dir.exists():
        return None

    candidates: list[tuple[str, str, Path, dict[str, Any]]] = []
    for path in artifact_dir.glob(CLAIMED_SUBMIT_OPERATOR_HANDOFF_PATTERN):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("artifact_type") != "claimed_submit_operator_handoff":
            continue
        generated_key = str(payload.get("generated_from_runtime_truth_at") or "")
        candidates.append((generated_key, path.name, path, payload))

    if not candidates:
        return None

    _, _, path, payload = sorted(candidates, key=lambda item: (item[0], item[1]))[-1]
    result = dict(payload)
    result["_artifact_path"] = _relative_artifact_path(repo_root, path)
    return result


def summarize_claimed_submit_operator_handoff_truth(
    repo_root: Path,
    claimed_submit_truth: dict[str, Any],
    *,
    report_generated_at: str,
) -> dict[str, Any]:
    claimed_submit = as_dict(claimed_submit_truth)
    coverage = as_dict(claimed_submit.get("coverage"))
    count = int_or_zero(coverage.get("claimed_submit_stuck_submission_count"))
    current_order = as_dict(claimed_submit.get("latest_order"))
    current_client_order_id = current_order.get("client_order_id")
    required_confirmation = claimed_submit.get("required_operator_confirmation")

    if count == 0:
        return {
            "status": "not_required_no_claimed_submit_blocker",
            "smallest_missing_field": None,
            "report_generated_at": report_generated_at,
            "ready_for_protected_recovery": False,
            "interpretation": "no claimed-submit stuck submission currently requires an operator handoff",
        }

    handoff = load_latest_claimed_submit_operator_handoff(repo_root)
    if not handoff:
        return {
            "status": "missing_operator_handoff_artifact",
            "smallest_missing_field": "artifacts.automation.claimed_submit_operator_handoff",
            "report_generated_at": report_generated_at,
            "current_client_order_id": current_client_order_id,
            "required_operator_confirmation": required_confirmation,
            "ready_for_protected_recovery": False,
            "interpretation": "claimed-submit blocker exists, but no operator handoff artifact was found",
        }

    validation = as_dict(handoff.get("validation"))
    order = as_dict(handoff.get("order"))
    source_artifacts = as_dict(handoff.get("source_artifacts"))
    handoff_client_order_id = order.get("client_order_id")
    handoff_confirmation = order.get("exact_confirmation_required")
    matches_current_order = (
        not current_client_order_id
        or handoff_client_order_id == current_client_order_id
    )
    matches_required_confirmation = (
        not required_confirmation
        or handoff_confirmation == required_confirmation
    )
    valid = bool(validation.get("valid"))
    raw_ready = bool(validation.get("ready_for_protected_recovery"))
    ready = bool(
        valid
        and raw_ready
        and matches_current_order
        and matches_required_confirmation
    )

    if not matches_current_order:
        status = "stale_or_mismatched_operator_handoff"
        smallest_missing_field = "operator_handoff.order.client_order_id"
        current_blocker = "operator_handoff_does_not_match_current_claimed_submit_order"
    elif not matches_required_confirmation:
        status = "stale_or_mismatched_operator_handoff"
        smallest_missing_field = "operator_handoff.order.exact_confirmation_required"
        current_blocker = "operator_handoff_confirmation_does_not_match_current_required_confirmation"
    elif ready:
        status = "ready_for_protected_recovery"
        smallest_missing_field = None
        current_blocker = None
    elif valid:
        status = (
            handoff.get("handoff_status")
            or validation.get("status")
            or "awaiting_external_operator_confirmation"
        )
        smallest_missing_field = "operator_confirmation"
        current_blocker = "external_operator_confirmation_required_before_resolve_stuck_submission"
    else:
        status = "invalid_operator_handoff"
        smallest_missing_field = "operator_handoff.validation.failures"
        current_blocker = "operator_handoff_validation_failed"

    return {
        "status": status,
        "smallest_missing_field": smallest_missing_field,
        "current_blocker": current_blocker,
        "report_generated_at": report_generated_at,
        "artifact_path": handoff.get("_artifact_path"),
        "generated_from_runtime_truth_at": handoff.get("generated_from_runtime_truth_at"),
        "handoff_status": handoff.get("handoff_status"),
        "validation_status": validation.get("status"),
        "next_action": handoff.get("next_action"),
        "valid": valid,
        "ready_for_protected_recovery": ready,
        "raw_ready_for_protected_recovery": raw_ready,
        "operator_confirmation_matched": bool(validation.get("operator_confirmation_matched")),
        "matches_current_order": matches_current_order,
        "matches_required_confirmation": matches_required_confirmation,
        "current_client_order_id": current_client_order_id,
        "handoff_client_order_id": handoff_client_order_id,
        "command_id": order.get("command_id"),
        "required_operator_confirmation": required_confirmation,
        "handoff_exact_confirmation_required": handoff_confirmation,
        "source_artifacts": source_artifacts,
        "warnings": as_list(validation.get("warnings")),
        "failures": as_list(validation.get("failures")),
        "interpretation": (
            "operator handoff is current only if it matches the live claimed-submit order "
            "and the current required confirmation"
        ),
    }


def project_live_runtime_facts(report: dict[str, Any]) -> dict[str, Any]:
    db = report.get("database_truth") or {}
    execution_science = as_dict(report.get("execution_science_truth"))
    microstructure_runtime_growth = as_dict(report.get("microstructure_runtime_growth_truth"))
    microstructure_growth_collector = as_dict(microstructure_runtime_growth.get("collector"))
    microstructure_growth_bronze = as_dict(microstructure_runtime_growth.get("bronze_growth"))
    microstructure_growth_payload_sequence = as_dict(
        microstructure_runtime_growth.get("payload_sequence")
    )
    microstructure_growth_workflow = as_dict(
        microstructure_runtime_growth.get("silver_workflow")
    )
    microstructure_growth_silver = as_dict(microstructure_runtime_growth.get("silver_update"))
    orderbook_payload_depth = as_dict(report.get("orderbook_payload_depth_truth"))
    orderbook_payload_depth_books5 = as_dict(orderbook_payload_depth.get("books5_payload"))
    orderbook_payload_depth_bbo = as_dict(orderbook_payload_depth.get("bbo_payload"))
    orderbook_payload_depth_sequence = as_dict(orderbook_payload_depth.get("sequence"))
    orderbook_payload_depth_silver = as_dict(orderbook_payload_depth.get("silver_orderbook"))
    slippage_cost = as_dict(report.get("slippage_cost_calibration_truth"))
    slippage_proxy = as_dict(slippage_cost.get("slippage_proxy"))
    slippage_coverage = as_dict(slippage_proxy.get("coverage_audit"))
    directional_command_flow = as_dict(report.get("directional_command_flow_provenance_truth"))
    directional_command_flow_coverage = as_dict(directional_command_flow.get("coverage"))
    directional_attribution = as_dict(report.get("directional_episode_attribution_truth"))
    directional_executable_episode = as_dict(report.get("directional_executable_episode_truth"))
    directional_executable_latest = as_dict(
        directional_executable_episode.get("latest_executable_decision")
    )
    directional_executable_terminal = as_dict(
        directional_executable_episode.get("terminal_no_fill")
    )
    directional_executable_terminal_drilldown = as_dict(
        directional_executable_episode.get("terminal_no_fill_drilldown")
    )
    directional_executable_terminal_drilldown_coverage = as_dict(
        directional_executable_terminal_drilldown.get("coverage")
    )
    directional_executable_provenance = as_dict(
        directional_executable_episode.get("provenance")
    )
    directional_executable_interpretation = as_dict(
        directional_executable_episode.get("interpretation")
    )
    executable_terminal_pretrade = as_dict(
        report.get("directional_executable_terminal_no_fill_pretrade_microstructure_truth")
    )
    executable_terminal_pretrade_decision = as_dict(executable_terminal_pretrade.get("decision"))
    executable_terminal_pretrade_microstructure = as_dict(
        executable_terminal_pretrade.get("pretrade_microstructure")
    )
    executable_terminal_pretrade_decision_context = as_dict(
        executable_terminal_pretrade_microstructure.get("decision_context")
    )
    executable_terminal_pretrade_orderbook = as_dict(
        executable_terminal_pretrade_decision_context.get("orderbook")
    )
    executable_terminal_pretrade_trade_flow = as_dict(
        executable_terminal_pretrade_decision_context.get("trade_flow")
    )
    executable_terminal_pretrade_sequence = as_dict(
        executable_terminal_pretrade.get("snapshot_diff_sequence")
    )
    executable_terminal_pretrade_depth = as_dict(
        executable_terminal_pretrade.get("orderbook_payload_depth")
    )
    executable_terminal_pretrade_fill_feasibility = as_dict(
        executable_terminal_pretrade.get("local_fill_feasibility")
    )
    executable_terminal_pretrade_slippage = as_dict(
        executable_terminal_pretrade.get("slippage_baseline")
    )
    depth_slippage_lifecycle = as_dict(report.get("depth_slippage_lifecycle_truth"))
    depth_slippage_lifecycle_depth = as_dict(depth_slippage_lifecycle.get("depth_readiness"))
    depth_slippage_lifecycle_slippage = as_dict(depth_slippage_lifecycle.get("slippage_baseline"))
    depth_slippage_lifecycle_command = as_dict(
        depth_slippage_lifecycle.get("directional_command_coverage")
    )
    depth_slippage_lifecycle_recent = as_dict(
        depth_slippage_lifecycle.get("recent_directional_lifecycle_coverage")
    )
    depth_slippage_lifecycle_interpretation = as_dict(
        depth_slippage_lifecycle.get("interpretation")
    )
    latest_decision_fill_feasibility = as_dict(report.get("latest_decision_fill_feasibility_truth"))
    latest_decision_fill_feasibility_no_order = as_dict(
        latest_decision_fill_feasibility.get("no_order")
    )
    latest_decision_fill_feasibility_pretrade = as_dict(
        latest_decision_fill_feasibility.get("pretrade_microstructure")
    )
    latest_decision_fill_feasibility_orderbook = as_dict(
        latest_decision_fill_feasibility_pretrade.get("orderbook")
    )
    latest_decision_fill_feasibility_trade_flow = as_dict(
        latest_decision_fill_feasibility_pretrade.get("trade_flow")
    )
    decision_lifecycle_continuity = as_dict(
        report.get("decision_lifecycle_provenance_continuity_truth")
    )
    decision_lifecycle_current = as_dict(decision_lifecycle_continuity.get("current_decision"))
    decision_lifecycle_executable = as_dict(
        decision_lifecycle_continuity.get("latest_executable_directional_episode")
    )
    decision_lifecycle_recent = as_dict(
        decision_lifecycle_continuity.get("recent_directional_batch")
    )
    decision_lifecycle_command = as_dict(decision_lifecycle_continuity.get("command_flow"))
    decision_lifecycle_depth = as_dict(
        decision_lifecycle_continuity.get("depth_slippage_lifecycle")
    )
    decision_lifecycle_execution_science = as_dict(
        report.get("decision_lifecycle_execution_science_continuity_truth")
    )
    decision_lifecycle_execution_science_lifecycle = as_dict(
        decision_lifecycle_execution_science.get("lifecycle_provenance")
    )
    decision_lifecycle_execution_science_latest_fill = as_dict(
        decision_lifecycle_execution_science.get("latest_decision_fill_feasibility")
    )
    decision_lifecycle_execution_science_terminal = as_dict(
        decision_lifecycle_execution_science.get("terminal_no_fill_execution_science")
    )
    decision_lifecycle_execution_science_depth = as_dict(
        decision_lifecycle_execution_science.get("orderbook_payload_depth")
    )
    decision_lifecycle_execution_science_depth_lifecycle = as_dict(
        decision_lifecycle_execution_science.get("depth_slippage_lifecycle")
    )
    decision_lifecycle_execution_science_sequence = as_dict(
        decision_lifecycle_execution_science.get("execution_science")
    )
    decision_lifecycle_execution_science_slippage = as_dict(
        decision_lifecycle_execution_science.get("slippage_cost_calibration")
    )
    recent_directional_chain_density = as_dict(
        report.get("recent_directional_decision_chain_density_truth")
    )
    recent_directional_chain_density_coverage = as_dict(
        recent_directional_chain_density.get("coverage")
    )
    recent_directional_chain_density_interpretation = as_dict(
        recent_directional_chain_density.get("interpretation")
    )
    recent_directional_chain_density_latest_filled = as_dict(
        recent_directional_chain_density.get("latest_filled_decision")
    )
    recent_directional_no_order_root_density = as_dict(
        report.get("recent_directional_no_order_root_cause_density_truth")
    )
    recent_directional_no_order_root_density_coverage = as_dict(
        recent_directional_no_order_root_density.get("coverage")
    )
    recent_directional_no_order_root_density_interpretation = as_dict(
        recent_directional_no_order_root_density.get("interpretation")
    )
    recent_directional_no_order_root_density_distributions = as_dict(
        recent_directional_no_order_root_density.get("distributions")
    )
    latest_no_order_primary_candidate_bridge = as_dict(
        report.get("latest_directional_no_order_primary_candidate_bridge_truth")
    )
    latest_no_order_primary_candidate_bridge_latest = as_dict(
        latest_no_order_primary_candidate_bridge.get("latest_decision")
    )
    latest_no_order_primary_candidate_bridge_primary = as_dict(
        latest_no_order_primary_candidate_bridge.get("primary_candidate")
    )
    latest_no_order_primary_candidate_bridge_bridge = as_dict(
        latest_no_order_primary_candidate_bridge.get("bridge")
    )
    latest_no_order_primary_candidate_bridge_interpretation = as_dict(
        latest_no_order_primary_candidate_bridge.get("interpretation")
    )
    recent_no_order_primary_candidate_bridge_density = as_dict(
        report.get("recent_directional_no_order_primary_candidate_bridge_density_truth")
    )
    recent_no_order_primary_candidate_bridge_density_coverage = as_dict(
        recent_no_order_primary_candidate_bridge_density.get("coverage")
    )
    recent_no_order_primary_candidate_bridge_density_roots = as_dict(
        recent_no_order_primary_candidate_bridge_density.get("recent_portfolio_route_roots")
    )
    recent_no_order_primary_candidate_bridge_density_latest = as_dict(
        recent_no_order_primary_candidate_bridge_density.get("latest_bridge")
    )
    recent_no_order_primary_candidate_bridge_density_interpretation = as_dict(
        recent_no_order_primary_candidate_bridge_density.get("interpretation")
    )
    directional_spike_reversion = as_dict(report.get("directional_spike_reversion_truth"))
    directional_spike_reversion_coverage = as_dict(directional_spike_reversion.get("coverage"))
    latest_directional_spike_reversion = as_dict(directional_spike_reversion.get("latest_filled_decision"))
    target_convergence_guard = as_dict(report.get("target_convergence_guard_truth"))
    target_convergence_guard_coverage = as_dict(target_convergence_guard.get("coverage"))
    target_convergence_guard_open_orders = as_dict(target_convergence_guard.get("current_open_orders"))
    target_convergence_guard_latest_hit = as_dict(target_convergence_guard.get("latest_guard_hit"))
    directional_impulse_chase_guard = as_dict(report.get("directional_impulse_chase_guard_truth"))
    directional_impulse_chase_guard_code = as_dict(directional_impulse_chase_guard.get("code"))
    directional_impulse_chase_guard_coverage = as_dict(
        directional_impulse_chase_guard.get("coverage")
    )
    directional_impulse_chase_guard_latest_hit = as_dict(
        directional_impulse_chase_guard.get("latest_guard_hit")
    )
    okx_hedge_scale_in_intent = as_dict(report.get("okx_hedge_scale_in_intent_truth"))
    okx_hedge_scale_in_intent_code = as_dict(okx_hedge_scale_in_intent.get("code"))
    okx_hedge_scale_in_intent_coverage = as_dict(okx_hedge_scale_in_intent.get("coverage"))
    created_no_command_directional_order = as_dict(
        report.get("created_no_command_directional_order_truth")
    )
    created_no_command_directional_order_coverage = as_dict(
        created_no_command_directional_order.get("coverage")
    )
    execution_order_payload_status_residual = as_dict(
        report.get("execution_order_payload_status_residual_truth")
    )
    execution_order_payload_status_residual_authority = as_dict(
        execution_order_payload_status_residual.get("authority")
    )
    execution_order_payload_status_residual_coverage = as_dict(
        execution_order_payload_status_residual.get("coverage")
    )
    execution_order_payload_status_residual_target = as_dict(
        execution_order_payload_status_residual.get("target_order")
    )
    claimed_submit_stuck_submission = as_dict(report.get("claimed_submit_stuck_submission_truth"))
    claimed_submit_stuck_submission_coverage = as_dict(
        claimed_submit_stuck_submission.get("coverage")
    )
    claimed_submit_latest_order = as_dict(claimed_submit_stuck_submission.get("latest_order"))
    claimed_submit_latest_reconciliation = as_dict(
        claimed_submit_stuck_submission.get("latest_reconciliation")
    )
    claimed_submit_latest_baseline = as_dict(
        claimed_submit_stuck_submission.get("latest_baseline")
    )
    claimed_submit_operator_action_counts = as_dict(
        claimed_submit_stuck_submission.get("operator_action_counts")
    )
    claimed_submit_operator_handoff = as_dict(
        report.get("claimed_submit_operator_handoff_truth")
    )
    claimed_submit_operator_handoff_sources = as_dict(
        claimed_submit_operator_handoff.get("source_artifacts")
    )
    directional_attribution_coverage = as_dict(directional_attribution.get("coverage"))
    directional_no_order_semantics = as_dict(directional_attribution.get("no_order_semantics"))
    directional_no_order_semantics_coverage = as_dict(
        directional_no_order_semantics.get("coverage")
    )
    directional_pretrade_microstructure = as_dict(directional_attribution.get("pretrade_microstructure"))
    directional_pretrade_microstructure_coverage = as_dict(
        directional_pretrade_microstructure.get("coverage")
    )
    directional_pnl_lifecycle = as_dict(directional_attribution.get("pnl_lifecycle"))
    directional_pnl_lifecycle_coverage = as_dict(directional_pnl_lifecycle.get("coverage"))
    latest_directional_episode = as_dict(directional_attribution.get("latest_filled_decision"))
    latest_directional_episode_fill = as_dict(latest_directional_episode.get("fill"))
    latest_directional_episode_pnl = as_dict(latest_directional_episode.get("pnl_outcome"))
    latest_directional_episode_pnl_lifecycle = as_dict(latest_directional_episode.get("pnl_lifecycle"))
    latest_directional_episode_pretrade_microstructure = as_dict(
        latest_directional_episode.get("pretrade_microstructure")
    )
    dashboard = ((report.get("runtime") or {}).get("dashboard_bundle") or {})
    operator_read_auth = as_dict((report.get("runtime") or {}).get("operator_read_auth"))
    ai_runtime_effective = as_dict(report.get("ai_runtime_effective_mode_truth"))
    ai_runtime_configured = as_dict(ai_runtime_effective.get("configured_target"))
    ai_runtime_effective_mode = as_dict(ai_runtime_effective.get("effective_runtime_mode"))
    ai_runtime_manual_override = as_dict(ai_runtime_effective.get("manual_override"))
    ai_runtime_provider = as_dict(ai_runtime_effective.get("provider"))
    ai_runtime_provider_configured = as_dict(ai_runtime_provider.get("configured"))
    ai_runtime_provider_ready = as_dict(ai_runtime_provider.get("ready"))
    ai_runtime_provider_degraded = as_dict(ai_runtime_provider.get("degraded"))
    ai_runtime_shadow = as_dict(ai_runtime_effective.get("shadow_enabled"))
    ai_runtime_profile_auto = as_dict(ai_runtime_effective.get("profile_auto_control_effective"))
    ai_runtime_auth_gate = as_dict(ai_runtime_effective.get("auth_gate"))
    ai_runtime_timeout = as_dict(ai_runtime_effective.get("ai_timeout"))
    git = report.get("git") or {}
    health = report.get("deployment_health") or {}
    runtime_config = db.get("runtime_config") if isinstance(db.get("runtime_config"), dict) else {}
    latest = db.get("latest_decision") if isinstance(db.get("latest_decision"), dict) else {}
    latest_executable_directional = (
        db.get("latest_executable_directional_decision")
        if isinstance(db.get("latest_executable_directional_decision"), dict)
        else {}
    )
    no_trade = latest.get("no_trade_attribution") if isinstance(latest.get("no_trade_attribution"), dict) else {}
    latest_primary_candidate_truth = as_dict(no_trade.get("primary_family_candidate_truth"))
    latest_primary_candidate_no_order_semantics = classify_primary_candidate_no_order_semantics(
        latest_primary_candidate_truth
    )
    latest_truth_chain = latest.get("execution_truth_chain") or {}
    latest_terminal_no_fill = as_dict(latest_truth_chain.get("terminal_no_fill_explanation"))
    executable_truth_chain = latest_executable_directional.get("execution_truth_chain") or {}
    executable_no_trade = (
        latest_executable_directional.get("no_trade_attribution")
        if isinstance(latest_executable_directional.get("no_trade_attribution"), dict)
        else {}
    )
    executable_primary_candidate_truth = as_dict(
        executable_no_trade.get("primary_family_candidate_truth")
    )
    executable_terminal_no_fill = as_dict(executable_truth_chain.get("terminal_no_fill_explanation"))

    return {
        "source": "live_runtime",
        "authoritative": True,
        "may_be_overridden_by_artifact": False,
        "active_live_carrier": infer_live_carrier_from_database_truth(db),
        "latest_decision_id": latest.get("decision_id"),
        "latest_decision_route_action": latest.get("route_action"),
        "latest_decision_symbol": latest.get("symbol"),
        "latest_decision_primary_family": latest.get("primary_family"),
        "latest_decision_no_trade_primary_blocker": no_trade.get("primary_blocker"),
        "latest_decision_no_trade_final_blockers": no_trade.get("final_blockers"),
        "latest_decision_no_trade_contributing_factors": no_trade.get("contributing_factors"),
        "latest_decision_no_trade_candidate_drilldown_count": len(
            as_list(no_trade.get("candidate_execution_drilldown")),
        ),
        "latest_decision_no_trade_classification": no_trade.get("classification"),
        "latest_decision_is_current_no_trade": no_trade.get("is_current_no_trade"),
        "latest_decision_primary_candidate_truth_status": latest_primary_candidate_truth.get("status"),
        "latest_decision_primary_candidate_smallest_missing_field": (
            latest_primary_candidate_truth.get("smallest_missing_field")
        ),
        "latest_decision_primary_candidate_family": latest_primary_candidate_truth.get("primary_family"),
        "latest_decision_primary_candidate_route_action": latest_primary_candidate_truth.get(
            "candidate_route_action"
        ),
        "latest_decision_primary_candidate_execution_behavior": latest_primary_candidate_truth.get(
            "candidate_execution_behavior"
        ),
        "latest_decision_primary_candidate_order_expected": latest_primary_candidate_truth.get(
            "order_expected_from_primary_candidate"
        ),
        "latest_decision_primary_candidate_no_order_root_cause": latest_primary_candidate_truth.get(
            "no_order_root_cause"
        ),
        "latest_decision_primary_candidate_no_order_semantic_status": (
            latest_primary_candidate_no_order_semantics.get("status")
        ),
        "latest_decision_primary_candidate_no_order_equivalence_class": (
            latest_primary_candidate_no_order_semantics.get("equivalence_class")
        ),
        "latest_decision_primary_candidate_no_order_root_material_without_order_or_fill_change": (
            latest_primary_candidate_no_order_semantics.get(
                "root_cause_is_material_without_order_or_fill_change"
            )
        ),
        "latest_decision_primary_candidate_no_order_requires_order_or_fill_change_for_materiality": (
            latest_primary_candidate_no_order_semantics.get(
                "requires_order_or_fill_change_for_materiality"
            )
        ),
        "latest_decision_primary_candidate_execution_compatible": latest_primary_candidate_truth.get(
            "candidate_execution_compatible"
        ),
        "latest_decision_primary_candidate_approved_for_execution": latest_primary_candidate_truth.get(
            "candidate_approved_for_execution"
        ),
        "latest_decision_primary_candidate_permission_mode": latest_primary_candidate_truth.get(
            "candidate_permission_mode"
        ),
        "latest_decision_primary_candidate_composed_delta_position_qty": (
            latest_primary_candidate_truth.get("composed_delta_position_qty")
        ),
        "latest_decision_primary_candidate_target_notional": latest_primary_candidate_truth.get(
            "target_notional"
        ),
        "latest_decision_primary_candidate_global_primary_blocker": latest_primary_candidate_truth.get(
            "global_primary_blocker"
        ),
        "latest_decision_primary_candidate_global_blocker_applies": (
            latest_primary_candidate_truth.get("global_primary_blocker_applies_to_candidate")
        ),
        "latest_decision_primary_candidate_global_blocker_scope": latest_primary_candidate_truth.get(
            "global_primary_blocker_scope"
        ),
        "latest_decision_execution_truth_status": latest_truth_chain.get("status"),
        "latest_decision_order_expected": latest_truth_chain.get("order_expected"),
        "latest_decision_fill_expected": latest_truth_chain.get("fill_expected"),
        "latest_decision_position_lifecycle_status": latest_truth_chain.get("position_lifecycle_status"),
        "latest_decision_truth_chain_smallest_missing_field": latest_truth_chain.get("smallest_missing_field"),
        "latest_decision_terminal_no_fill_classification": latest_terminal_no_fill.get("classification"),
        "latest_decision_terminal_no_fill_reason": latest_terminal_no_fill.get("reason"),
        "latest_decision_terminal_no_fill_states": latest_terminal_no_fill.get("terminal_states"),
        "latest_decision_terminal_no_fill_source_systems": latest_terminal_no_fill.get("terminal_source_systems"),
        "latest_decision_terminal_no_fill_execution_styles": latest_terminal_no_fill.get("terminal_execution_styles"),
        "latest_decision_terminal_no_fill_position_intents": latest_terminal_no_fill.get("terminal_position_intents"),
        "latest_decision_terminal_no_fill_order_count": latest_terminal_no_fill.get("execution_order_count"),
        "latest_decision_terminal_no_fill_order_state_count": latest_terminal_no_fill.get("order_state_count"),
        "latest_executable_directional_decision_id": latest_executable_directional.get("decision_id"),
        "latest_executable_directional_route_action": latest_executable_directional.get("route_action"),
        "latest_executable_directional_created_at": latest_executable_directional.get("created_at"),
        "latest_executable_directional_primary_candidate_truth_status": (
            executable_primary_candidate_truth.get("status")
        ),
        "latest_executable_directional_primary_candidate_order_expected": (
            executable_primary_candidate_truth.get("order_expected_from_primary_candidate")
        ),
        "latest_executable_directional_primary_candidate_no_order_root_cause": (
            executable_primary_candidate_truth.get("no_order_root_cause")
        ),
        "latest_executable_directional_primary_candidate_execution_compatible": (
            executable_primary_candidate_truth.get("candidate_execution_compatible")
        ),
        "latest_executable_directional_primary_candidate_global_blocker_scope": (
            executable_primary_candidate_truth.get("global_primary_blocker_scope")
        ),
        "latest_executable_directional_execution_truth_status": executable_truth_chain.get("status"),
        "latest_executable_directional_order_expected": executable_truth_chain.get("order_expected"),
        "latest_executable_directional_fill_expected": executable_truth_chain.get("fill_expected"),
        "latest_executable_directional_position_lifecycle_status": executable_truth_chain.get(
            "position_lifecycle_status",
        ),
        "latest_executable_directional_truth_chain_smallest_missing_field": executable_truth_chain.get(
            "smallest_missing_field",
        ),
        "latest_executable_directional_submission_gap_root_cause": executable_truth_chain.get(
            "submission_gap_root_cause",
        ),
        "latest_executable_directional_terminal_no_fill_classification": executable_terminal_no_fill.get(
            "classification",
        ),
        "latest_executable_directional_terminal_no_fill_reason": executable_terminal_no_fill.get("reason"),
        "latest_executable_directional_terminal_no_fill_states": executable_terminal_no_fill.get(
            "terminal_states",
        ),
        "latest_executable_directional_terminal_no_fill_source_systems": executable_terminal_no_fill.get(
            "terminal_source_systems",
        ),
        "latest_executable_directional_terminal_no_fill_execution_styles": executable_terminal_no_fill.get(
            "terminal_execution_styles",
        ),
        "latest_executable_directional_terminal_no_fill_position_intents": executable_terminal_no_fill.get(
            "terminal_position_intents",
        ),
        "latest_executable_directional_terminal_no_fill_order_count": executable_terminal_no_fill.get(
            "execution_order_count",
        ),
        "latest_executable_directional_terminal_no_fill_order_state_count": executable_terminal_no_fill.get(
            "order_state_count",
        ),
        "directional_executable_episode_truth_status": directional_executable_episode.get("status"),
        "directional_executable_episode_smallest_missing_field": (
            directional_executable_episode.get("smallest_missing_field")
        ),
        "directional_executable_episode_latest_decision_id": directional_executable_latest.get(
            "decision_id"
        ),
        "directional_executable_episode_latest_route_action": directional_executable_latest.get(
            "route_action"
        ),
        "directional_executable_episode_order_expected": directional_executable_latest.get(
            "order_expected"
        ),
        "directional_executable_episode_fill_expected": directional_executable_latest.get(
            "fill_expected"
        ),
        "directional_executable_episode_execution_truth_status": directional_executable_latest.get(
            "execution_truth_status"
        ),
        "directional_executable_episode_terminal_no_fill_classification": (
            directional_executable_terminal.get("classification")
        ),
        "directional_executable_episode_terminal_no_fill_reason": (
            directional_executable_terminal.get("reason")
        ),
        "directional_executable_episode_terminal_no_fill_states": (
            directional_executable_terminal.get("terminal_states")
        ),
        "directional_executable_episode_terminal_no_fill_position_intents": (
            directional_executable_terminal.get("terminal_position_intents")
        ),
        "directional_executable_episode_terminal_no_fill_drilldown_status": (
            directional_executable_terminal_drilldown.get("status")
        ),
        "directional_executable_episode_terminal_no_fill_drilldown_smallest_missing_field": (
            directional_executable_terminal_drilldown.get("smallest_missing_field")
        ),
        "directional_executable_episode_terminal_no_fill_drilldown_order_count": (
            directional_executable_terminal_drilldown_coverage.get("drilldown_order_count")
        ),
        "directional_executable_episode_terminal_no_fill_drilldown_command_present_count": (
            directional_executable_terminal_drilldown_coverage.get("command_present_count")
        ),
        "directional_executable_episode_terminal_no_fill_drilldown_exchange_ack_absent_count": (
            directional_executable_terminal_drilldown_coverage.get("exchange_ack_absent_count")
        ),
        "directional_executable_episode_terminal_no_fill_drilldown_fill_absent_count": (
            directional_executable_terminal_drilldown_coverage.get("fill_absent_count")
        ),
        "directional_executable_episode_terminal_no_fill_drilldown_terminal_verified_count": (
            directional_executable_terminal_drilldown_coverage.get("terminal_state_verified_count")
        ),
        "directional_executable_terminal_no_fill_pretrade_microstructure_status": (
            executable_terminal_pretrade.get("status")
        ),
        "directional_executable_terminal_no_fill_pretrade_microstructure_smallest_missing_field": (
            executable_terminal_pretrade.get("smallest_missing_field")
        ),
        "directional_executable_terminal_no_fill_pretrade_microstructure_decision_id": (
            executable_terminal_pretrade_decision.get("decision_id")
        ),
        "directional_executable_terminal_no_fill_pretrade_microstructure_order_expected": (
            executable_terminal_pretrade_decision.get("order_expected")
        ),
        "directional_executable_terminal_no_fill_pretrade_microstructure_fill_expected": (
            executable_terminal_pretrade_decision.get("fill_expected")
        ),
        "directional_executable_terminal_no_fill_pretrade_microstructure_context_status": (
            executable_terminal_pretrade_microstructure.get("status")
        ),
        "directional_executable_terminal_no_fill_pretrade_microstructure_orderbook_bar_age_seconds": (
            executable_terminal_pretrade_orderbook.get("bar_age_seconds")
        ),
        "directional_executable_terminal_no_fill_pretrade_microstructure_orderbook_books5_samples_n": (
            executable_terminal_pretrade_orderbook.get("books5_samples_n")
        ),
        "directional_executable_terminal_no_fill_pretrade_microstructure_orderbook_spread_bps_mean": (
            executable_terminal_pretrade_orderbook.get("spread_bps_mean")
        ),
        "directional_executable_terminal_no_fill_pretrade_microstructure_trade_count": (
            executable_terminal_pretrade_trade_flow.get("trade_count")
        ),
        "directional_executable_terminal_no_fill_pretrade_microstructure_taker_buy_ratio": (
            executable_terminal_pretrade_trade_flow.get("taker_buy_ratio")
        ),
        "directional_executable_terminal_no_fill_pretrade_microstructure_vwap_minus_mid_bps": (
            executable_terminal_pretrade_trade_flow.get("vwap_minus_mid_bps")
        ),
        "directional_executable_terminal_no_fill_snapshot_diff_sequence_status": (
            executable_terminal_pretrade_sequence.get("status")
        ),
        "directional_executable_terminal_no_fill_snapshot_diff_sequence_gap_count": (
            executable_terminal_pretrade_sequence.get("sequence_gap_count")
        ),
        "directional_executable_terminal_no_fill_orderbook_depth_status": (
            executable_terminal_pretrade_depth.get("status")
        ),
        "directional_executable_terminal_no_fill_orderbook_depth_books5_sequence_gap_count": (
            executable_terminal_pretrade_depth.get("books5_sequence_gap_count")
        ),
        "directional_executable_terminal_no_fill_local_fill_feasibility_status": (
            executable_terminal_pretrade_fill_feasibility.get("status")
        ),
        "directional_executable_terminal_no_fill_market_fill_feasibility_observable": (
            executable_terminal_pretrade_fill_feasibility.get("market_fill_feasibility_observable")
        ),
        "directional_executable_terminal_no_fill_slippage_baseline_status": (
            executable_terminal_pretrade_slippage.get("status")
        ),
        "directional_executable_episode_provenance_order_count": (
            directional_executable_provenance.get("db_order_count")
        ),
        "directional_executable_episode_provenance_order_state_count": (
            directional_executable_provenance.get("db_order_state_count")
        ),
        "directional_executable_episode_provenance_fill_count": (
            directional_executable_provenance.get("db_fill_count")
        ),
        "directional_executable_episode_provenance_execution_command_count": (
            directional_executable_provenance.get("db_execution_command_count")
        ),
        "directional_executable_episode_terminal_no_fill_verified": (
            directional_executable_interpretation.get("terminal_no_fill_verified")
        ),
        "portfolio_allocation_decisions": db.get("portfolio_allocation_decisions") if db.get("ok") else None,
        "execution_fills": db.get("execution_fills") if db.get("ok") else None,
        "execution_command_flow_enabled": runtime_config.get("execution_command_flow_enabled"),
        "execution_command_flow_flag_present": runtime_config.get("execution_command_flow_flag_present"),
        "execution_science_truth_status": execution_science.get("status"),
        "execution_science_smallest_missing_field": execution_science.get("smallest_missing_field"),
        "microstructure_runtime_growth_status": microstructure_runtime_growth.get("status"),
        "microstructure_runtime_growth_smallest_missing_field": (
            microstructure_runtime_growth.get("smallest_missing_field")
        ),
        "microstructure_runtime_growth_raw_payload_exposed": (
            microstructure_runtime_growth.get("raw_payload_exposed")
        ),
        "microstructure_collector_container": microstructure_growth_collector.get("container"),
        "microstructure_collector_status": microstructure_growth_collector.get("status"),
        "microstructure_collector_running": microstructure_growth_collector.get("running"),
        "microstructure_collector_healthy": microstructure_growth_collector.get("healthy"),
        "microstructure_collector_daemon_script_detected": (
            microstructure_growth_collector.get("daemon_script_detected")
        ),
        "microstructure_heartbeat_exists": microstructure_growth_collector.get("heartbeat_exists"),
        "microstructure_heartbeat_fresh": microstructure_growth_collector.get("heartbeat_fresh"),
        "microstructure_heartbeat_age_seconds": (
            microstructure_growth_collector.get("heartbeat_age_seconds")
        ),
        "microstructure_heartbeat_stale_after_seconds": (
            microstructure_growth_collector.get("heartbeat_stale_after_seconds")
        ),
        "microstructure_heartbeat_mtime_utc": (
            microstructure_growth_collector.get("heartbeat_mtime_utc")
        ),
        "microstructure_bronze_market_trades_recent_rows": (
            as_dict(microstructure_growth_bronze.get("market_trades")).get("recent_rows")
        ),
        "microstructure_bronze_market_trades_latest_ts": (
            as_dict(microstructure_growth_bronze.get("market_trades")).get("latest_ts")
        ),
        "microstructure_bronze_market_orderbook_bbo_recent_rows": (
            as_dict(microstructure_growth_bronze.get("market_orderbook_bbo")).get("recent_rows")
        ),
        "microstructure_bronze_market_orderbook_bbo_latest_ts": (
            as_dict(microstructure_growth_bronze.get("market_orderbook_bbo")).get("latest_ts")
        ),
        "microstructure_bronze_market_orderbook_books5_recent_rows": (
            as_dict(microstructure_growth_bronze.get("market_orderbook_books5")).get("recent_rows")
        ),
        "microstructure_bronze_market_orderbook_books5_latest_ts": (
            as_dict(microstructure_growth_bronze.get("market_orderbook_books5")).get("latest_ts")
        ),
        "microstructure_bronze_market_orderbook_payloads_recent_rows": (
            as_dict(microstructure_growth_bronze.get("market_orderbook_payloads")).get("recent_rows")
        ),
        "microstructure_bronze_market_orderbook_payloads_latest_ts": (
            as_dict(microstructure_growth_bronze.get("market_orderbook_payloads")).get("latest_ts")
        ),
        "microstructure_payload_sequence_status": (
            microstructure_growth_payload_sequence.get("status")
        ),
        "microstructure_payload_sequence_gap_count": (
            microstructure_growth_payload_sequence.get("sequence_gap_count")
        ),
        "microstructure_payload_sequence_window_minutes": (
            microstructure_growth_payload_sequence.get("window_minutes")
        ),
        "microstructure_silver_workflow_status": microstructure_growth_workflow.get("status"),
        "microstructure_silver_workflow_latest_task_status": (
            microstructure_growth_workflow.get("latest_task_status")
        ),
        "microstructure_silver_workflow_latest_exit_code": (
            microstructure_growth_workflow.get("latest_exit_code")
        ),
        "microstructure_silver_workflow_latest_age_seconds": (
            microstructure_growth_workflow.get("latest_age_seconds")
        ),
        "microstructure_silver_market_liquidation_metrics_15m_latest_ts": (
            as_dict(microstructure_growth_silver.get("market_liquidation_metrics_15m")).get("latest_ts")
        ),
        "microstructure_silver_market_oi_funding_metrics_15m_latest_ts": (
            as_dict(microstructure_growth_silver.get("market_oi_funding_metrics_15m")).get("latest_ts")
        ),
        "microstructure_silver_market_orderbook_metrics_15m_latest_ts": (
            as_dict(microstructure_growth_silver.get("market_orderbook_metrics_15m")).get("latest_ts")
        ),
        "microstructure_silver_market_trade_flow_15m_latest_ts": (
            as_dict(microstructure_growth_silver.get("market_trade_flow_15m")).get("latest_ts")
        ),
        "microstructure_silver_market_volume_profile_15m_latest_ts": (
            as_dict(microstructure_growth_silver.get("market_volume_profile_15m")).get("latest_ts")
        ),
        "orderbook_sequence_validation_status": (
            as_dict(execution_science.get("payload_sequence")).get("status")
        ),
        "fill_feasibility_truth_status": execution_science.get("fill_feasibility_truth_status"),
        "latest_decision_fill_feasibility_truth_status": latest_decision_fill_feasibility.get("status"),
        "latest_decision_fill_feasibility_smallest_missing_field": (
            latest_decision_fill_feasibility.get("smallest_missing_field")
        ),
        "latest_decision_fill_feasibility_applicable": (
            latest_decision_fill_feasibility.get("fill_feasibility_applicable")
        ),
        "latest_decision_fill_feasibility_order_expected": (
            latest_decision_fill_feasibility.get("order_expected")
        ),
        "latest_decision_fill_feasibility_fill_expected": (
            latest_decision_fill_feasibility.get("fill_expected")
        ),
        "latest_decision_fill_feasibility_pretrade_status": (
            latest_decision_fill_feasibility_pretrade.get("status")
        ),
        "latest_decision_fill_feasibility_pretrade_smallest_missing_field": (
            latest_decision_fill_feasibility_pretrade.get("smallest_missing_field")
        ),
        "latest_decision_fill_feasibility_no_order_classification": (
            latest_decision_fill_feasibility_no_order.get("classification")
        ),
        "latest_decision_fill_feasibility_no_order_primary_blocker": (
            latest_decision_fill_feasibility_no_order.get("primary_blocker")
        ),
        "latest_decision_fill_feasibility_orderbook_bar_age_seconds": (
            latest_decision_fill_feasibility_orderbook.get("bar_age_seconds")
        ),
        "latest_decision_fill_feasibility_orderbook_bbo_samples_n": (
            latest_decision_fill_feasibility_orderbook.get("bbo_samples_n")
        ),
        "latest_decision_fill_feasibility_orderbook_books5_samples_n": (
            latest_decision_fill_feasibility_orderbook.get("books5_samples_n")
        ),
        "latest_decision_fill_feasibility_orderbook_spread_bps_mean": (
            latest_decision_fill_feasibility_orderbook.get("spread_bps_mean")
        ),
        "latest_decision_fill_feasibility_trade_flow_bar_age_seconds": (
            latest_decision_fill_feasibility_trade_flow.get("bar_age_seconds")
        ),
        "latest_decision_fill_feasibility_trade_count": (
            latest_decision_fill_feasibility_trade_flow.get("trade_count")
        ),
        "latest_decision_fill_feasibility_taker_buy_ratio": (
            latest_decision_fill_feasibility_trade_flow.get("taker_buy_ratio")
        ),
        "latest_decision_fill_feasibility_vwap_minus_mid_bps": (
            latest_decision_fill_feasibility_trade_flow.get("vwap_minus_mid_bps")
        ),
        "decision_lifecycle_provenance_continuity_status": (
            decision_lifecycle_continuity.get("status")
        ),
        "decision_lifecycle_provenance_continuity_smallest_missing_field": (
            decision_lifecycle_continuity.get("smallest_missing_field")
        ),
        "decision_lifecycle_provenance_continuity_current_decision_id": (
            decision_lifecycle_current.get("decision_id")
        ),
        "decision_lifecycle_provenance_continuity_current_order_expected": (
            decision_lifecycle_current.get("order_expected")
        ),
        "decision_lifecycle_provenance_continuity_current_fill_expected": (
            decision_lifecycle_current.get("fill_expected")
        ),
        "decision_lifecycle_provenance_continuity_current_truth_status": (
            decision_lifecycle_current.get("execution_truth_status")
        ),
        "decision_lifecycle_provenance_continuity_current_fill_feasibility_status": (
            decision_lifecycle_current.get("fill_feasibility_status")
        ),
        "decision_lifecycle_provenance_continuity_executable_decision_id": (
            decision_lifecycle_executable.get("decision_id")
        ),
        "decision_lifecycle_provenance_continuity_executable_status": (
            decision_lifecycle_executable.get("status")
        ),
        "decision_lifecycle_provenance_continuity_executable_terminal_no_fill_drilldown_status": (
            decision_lifecycle_executable.get("terminal_no_fill_drilldown_status")
        ),
        "decision_lifecycle_provenance_continuity_recent_decision_count": (
            decision_lifecycle_recent.get("recent_decision_count")
        ),
        "decision_lifecycle_provenance_continuity_recent_filled_decisions": (
            decision_lifecycle_recent.get("decisions_with_fills")
        ),
        "decision_lifecycle_provenance_continuity_command_flow_status": (
            decision_lifecycle_command.get("status")
        ),
        "decision_lifecycle_provenance_continuity_depth_slippage_status": (
            decision_lifecycle_depth.get("status")
        ),
        "decision_lifecycle_execution_science_continuity_status": (
            decision_lifecycle_execution_science.get("status")
        ),
        "decision_lifecycle_execution_science_continuity_smallest_missing_field": (
            decision_lifecycle_execution_science.get("smallest_missing_field")
        ),
        "decision_lifecycle_execution_science_continuity_current_decision_id": (
            decision_lifecycle_execution_science_lifecycle.get("current_decision_id")
        ),
        "decision_lifecycle_execution_science_continuity_current_order_expected": (
            decision_lifecycle_execution_science_lifecycle.get("current_order_expected")
        ),
        "decision_lifecycle_execution_science_continuity_current_fill_expected": (
            decision_lifecycle_execution_science_lifecycle.get("current_fill_expected")
        ),
        "decision_lifecycle_execution_science_continuity_executable_decision_id": (
            decision_lifecycle_execution_science_lifecycle.get("executable_decision_id")
        ),
        "decision_lifecycle_execution_science_continuity_terminal_pretrade_status": (
            decision_lifecycle_execution_science_terminal.get("status")
        ),
        "decision_lifecycle_execution_science_continuity_snapshot_sequence_status": (
            decision_lifecycle_execution_science_terminal.get("snapshot_diff_sequence_status")
        ),
        "decision_lifecycle_execution_science_continuity_local_fill_feasibility_status": (
            decision_lifecycle_execution_science_terminal.get("local_fill_feasibility_status")
        ),
        "decision_lifecycle_execution_science_continuity_market_fill_observable": (
            decision_lifecycle_execution_science_terminal.get("market_fill_feasibility_observable")
        ),
        "decision_lifecycle_execution_science_continuity_orderbook_depth_status": (
            decision_lifecycle_execution_science_depth.get("status")
        ),
        "decision_lifecycle_execution_science_continuity_books5_sequence_gap_count": (
            decision_lifecycle_execution_science_depth.get("books5_sequence_gap_count")
        ),
        "decision_lifecycle_execution_science_continuity_latest_fill_feasibility_status": (
            decision_lifecycle_execution_science_latest_fill.get("status")
        ),
        "decision_lifecycle_execution_science_continuity_depth_slippage_lifecycle_status": (
            decision_lifecycle_execution_science_depth_lifecycle.get("status")
        ),
        "decision_lifecycle_execution_science_continuity_execution_sequence_status": (
            decision_lifecycle_execution_science_sequence.get("payload_sequence_status")
        ),
        "decision_lifecycle_execution_science_continuity_slippage_cost_status": (
            decision_lifecycle_execution_science_slippage.get("status")
        ),
        "decision_lifecycle_execution_science_continuity_slippage_proxy_sample_count": (
            decision_lifecycle_execution_science_slippage.get("slippage_proxy_sample_count")
        ),
        "recent_directional_decision_chain_density_truth_status": (
            recent_directional_chain_density.get("status")
        ),
        "recent_directional_decision_chain_density_smallest_missing_field": (
            recent_directional_chain_density.get("smallest_missing_field")
        ),
        "recent_directional_decision_chain_density_raw_payload_exposed": (
            recent_directional_chain_density.get("raw_payload_exposed")
        ),
        "recent_directional_chain_recent_decision_count": (
            recent_directional_chain_density_coverage.get("recent_decision_count")
        ),
        "recent_directional_chain_order_surface_or_no_order_count": (
            recent_directional_chain_density_coverage.get(
                "decisions_with_order_surface_or_no_order_expectation"
            )
        ),
        "recent_directional_chain_decisions_missing_order_surface": (
            recent_directional_chain_density_coverage.get("decisions_missing_order_surface")
        ),
        "recent_directional_chain_all_recent_decisions_no_order_expected": (
            recent_directional_chain_density_coverage.get(
                "all_recent_decisions_no_order_expected"
            )
        ),
        "recent_directional_chain_decisions_with_no_order_semantics": (
            recent_directional_chain_density_coverage.get("decisions_with_no_order_semantics")
        ),
        "recent_directional_chain_decisions_with_fills": (
            recent_directional_chain_density_coverage.get("decisions_with_fills")
        ),
        "recent_directional_chain_decisions_with_pnl_outcome": (
            recent_directional_chain_density_coverage.get("decisions_with_pnl_outcome")
        ),
        "recent_directional_chain_filled_decisions_with_resolved_pnl_lifecycle": (
            recent_directional_chain_density_coverage.get(
                "filled_decisions_with_resolved_pnl_lifecycle"
            )
        ),
        "recent_directional_chain_filled_decisions_with_pretrade_microstructure": (
            recent_directional_chain_density_coverage.get(
                "filled_decisions_with_pretrade_microstructure"
            )
        ),
        "recent_directional_chain_waiting_for_executable_directional_episode": (
            recent_directional_chain_density_interpretation.get(
                "waiting_for_executable_directional_episode"
            )
        ),
        "recent_directional_chain_not_alpha_or_profitability_evidence": (
            recent_directional_chain_density_interpretation.get(
                "not_alpha_or_profitability_evidence"
            )
        ),
        "recent_directional_chain_latest_filled_decision_id": (
            recent_directional_chain_density_latest_filled.get("decision_id")
        ),
        "recent_directional_chain_latest_filled_pnl_lifecycle_status": (
            recent_directional_chain_density_latest_filled.get("pnl_lifecycle_status")
        ),
        "recent_directional_no_order_root_cause_density_truth_status": (
            recent_directional_no_order_root_density.get("status")
        ),
        "recent_directional_no_order_root_cause_density_smallest_missing_field": (
            recent_directional_no_order_root_density.get("smallest_missing_field")
        ),
        "recent_directional_no_order_root_cause_density_raw_payload_exposed": (
            recent_directional_no_order_root_density.get("raw_payload_exposed")
        ),
        "recent_directional_no_order_root_recent_decision_count": (
            recent_directional_no_order_root_density_coverage.get("recent_decision_count")
        ),
        "recent_directional_no_order_root_no_order_expected_decision_count": (
            recent_directional_no_order_root_density_coverage.get(
                "no_order_expected_decision_count"
            )
        ),
        "recent_directional_no_order_root_decisions_missing_no_order_semantics": (
            recent_directional_no_order_root_density_coverage.get(
                "decisions_missing_no_order_semantics"
            )
        ),
        "recent_directional_no_order_root_decisions_missing_root_cause": (
            recent_directional_no_order_root_density_coverage.get(
                "decisions_missing_root_cause"
            )
        ),
        "recent_directional_no_order_root_decisions_missing_root_materiality": (
            recent_directional_no_order_root_density_coverage.get(
                "decisions_missing_root_materiality"
            )
        ),
        "recent_directional_no_order_root_top_root_cause": (
            recent_directional_no_order_root_density.get("top_root_cause")
        ),
        "recent_directional_no_order_root_top_equivalence_class": (
            recent_directional_no_order_root_density.get("top_equivalence_class")
        ),
        "recent_directional_no_order_root_top_route_action": (
            recent_directional_no_order_root_density.get("top_route_action")
        ),
        "recent_directional_no_order_root_all_roots_non_material_without_order_or_fill_change": (
            recent_directional_no_order_root_density_interpretation.get(
                "all_roots_non_material_without_order_or_fill_change"
            )
        ),
        "recent_directional_no_order_root_requires_order_or_fill_change_for_materiality": (
            recent_directional_no_order_root_density_interpretation.get(
                "all_roots_require_order_or_fill_change_for_materiality"
            )
        ),
        "recent_directional_no_order_root_distribution_root_cause": as_list(
            recent_directional_no_order_root_density_distributions.get("root_cause")
        ),
        "latest_directional_no_order_primary_candidate_bridge_truth_status": (
            latest_no_order_primary_candidate_bridge.get("status")
        ),
        "latest_directional_no_order_primary_candidate_bridge_smallest_missing_field": (
            latest_no_order_primary_candidate_bridge.get("smallest_missing_field")
        ),
        "latest_directional_no_order_primary_candidate_bridge_raw_payload_exposed": (
            latest_no_order_primary_candidate_bridge.get("raw_payload_exposed")
        ),
        "latest_directional_no_order_bridge_decision_id": (
            latest_no_order_primary_candidate_bridge_latest.get("decision_id")
        ),
        "latest_directional_no_order_bridge_latest_route_action": (
            latest_no_order_primary_candidate_bridge_bridge.get("latest_route_action")
        ),
        "latest_directional_no_order_bridge_primary_candidate_route_action": (
            latest_no_order_primary_candidate_bridge_bridge.get(
                "primary_candidate_route_action"
            )
        ),
        "latest_directional_no_order_bridge_route_action_differs": (
            latest_no_order_primary_candidate_bridge_bridge.get(
                "latest_route_action_differs_from_primary_candidate_route_action"
            )
        ),
        "latest_directional_no_order_bridge_portfolio_route_no_order_root_cause": (
            latest_no_order_primary_candidate_bridge_bridge.get(
                "portfolio_route_no_order_root_cause"
            )
        ),
        "latest_directional_no_order_bridge_primary_candidate_no_order_root_cause": (
            latest_no_order_primary_candidate_bridge_bridge.get(
                "primary_candidate_no_order_root_cause"
            )
        ),
        "latest_directional_no_order_bridge_route_root_and_primary_candidate_root_distinct": (
            latest_no_order_primary_candidate_bridge_bridge.get(
                "route_root_and_primary_candidate_root_distinct"
            )
        ),
        "latest_directional_no_order_bridge_latest_decision_order_expected": (
            latest_no_order_primary_candidate_bridge_bridge.get(
                "latest_decision_order_expected"
            )
        ),
        "latest_directional_no_order_bridge_primary_candidate_order_expected": (
            latest_no_order_primary_candidate_bridge_bridge.get(
                "primary_candidate_order_expected"
            )
        ),
        "latest_directional_no_order_bridge_primary_candidate_semantic_status": (
            latest_no_order_primary_candidate_bridge_primary.get(
                "no_order_semantic_status"
            )
        ),
        "latest_directional_no_order_bridge_primary_candidate_global_blocker_scope": (
            latest_no_order_primary_candidate_bridge_primary.get(
                "global_blocker_scope"
            )
        ),
        "latest_directional_no_order_bridge_portfolio_route_action_is_not_primary_candidate_root": (
            latest_no_order_primary_candidate_bridge_interpretation.get(
                "portfolio_route_action_is_not_primary_candidate_root"
            )
        ),
        "latest_directional_no_order_bridge_hold_current_zero_delta_explains_primary_directional_no_order": (
            latest_no_order_primary_candidate_bridge_interpretation.get(
                "hold_current_zero_delta_explains_primary_directional_no_order"
            )
        ),
        "latest_directional_no_order_bridge_advisory_only_route_action_explains_portfolio_route_no_order": (
            latest_no_order_primary_candidate_bridge_interpretation.get(
                "advisory_only_route_action_explains_portfolio_route_no_order"
            )
        ),
        "latest_directional_no_order_bridge_global_blocker_is_other_candidate_or_portfolio_level": (
            latest_no_order_primary_candidate_bridge_interpretation.get(
                "global_blocker_is_other_candidate_or_portfolio_level"
            )
        ),
        "latest_directional_no_order_bridge_not_alpha_or_profitability_evidence": (
            latest_no_order_primary_candidate_bridge_interpretation.get(
                "not_alpha_or_profitability_evidence"
            )
        ),
        "recent_directional_no_order_primary_candidate_bridge_density_truth_status": (
            recent_no_order_primary_candidate_bridge_density.get("status")
        ),
        "recent_directional_no_order_primary_candidate_bridge_density_smallest_missing_field": (
            recent_no_order_primary_candidate_bridge_density.get("smallest_missing_field")
        ),
        "recent_directional_no_order_primary_candidate_bridge_density_raw_payload_exposed": (
            recent_no_order_primary_candidate_bridge_density.get("raw_payload_exposed")
        ),
        "recent_directional_no_order_bridge_density_recent_decision_count": (
            recent_no_order_primary_candidate_bridge_density_coverage.get(
                "recent_decision_count"
            )
        ),
        "recent_directional_no_order_bridge_density_no_order_expected_decision_count": (
            recent_no_order_primary_candidate_bridge_density_coverage.get(
                "no_order_expected_decision_count"
            )
        ),
        "recent_directional_no_order_bridge_density_decisions_with_no_order_semantics": (
            recent_no_order_primary_candidate_bridge_density_coverage.get(
                "decisions_with_no_order_semantics"
            )
        ),
        "recent_directional_no_order_bridge_density_recent_root_density_status": (
            recent_no_order_primary_candidate_bridge_density_coverage.get(
                "recent_portfolio_route_root_density_status"
            )
        ),
        "recent_directional_no_order_bridge_density_latest_bridge_status": (
            recent_no_order_primary_candidate_bridge_density_coverage.get(
                "latest_primary_candidate_bridge_status"
            )
        ),
        "recent_directional_no_order_bridge_density_latest_bridge_verified": (
            recent_no_order_primary_candidate_bridge_density_coverage.get(
                "latest_primary_candidate_bridge_verified"
            )
        ),
        "recent_directional_no_order_bridge_density_latest_bridge_decision_id": (
            recent_no_order_primary_candidate_bridge_density_coverage.get(
                "latest_bridge_decision_id"
            )
        ),
        "recent_directional_no_order_bridge_density_latest_bridge_decision_present_in_recent": (
            recent_no_order_primary_candidate_bridge_density_coverage.get(
                "latest_bridge_decision_present_in_recent_decisions"
            )
        ),
        "recent_directional_no_order_bridge_density_historical_primary_candidate_bridge_scope": (
            recent_no_order_primary_candidate_bridge_density_coverage.get(
                "historical_primary_candidate_bridge_scope"
            )
        ),
        "recent_directional_no_order_bridge_density_historical_primary_candidate_bridge_available": (
            recent_no_order_primary_candidate_bridge_density_coverage.get(
                "historical_primary_candidate_bridge_available_for_recent_decisions"
            )
        ),
        "recent_directional_no_order_bridge_density_historical_primary_candidate_bridge_not_claimed": (
            recent_no_order_primary_candidate_bridge_density_coverage.get(
                "historical_primary_candidate_bridge_not_claimed"
            )
        ),
        "recent_directional_no_order_bridge_density_top_portfolio_route_root_cause": (
            recent_no_order_primary_candidate_bridge_density_roots.get("top_root_cause")
        ),
        "recent_directional_no_order_bridge_density_top_portfolio_route_action": (
            recent_no_order_primary_candidate_bridge_density_roots.get("top_route_action")
        ),
        "recent_directional_no_order_bridge_density_portfolio_root_distribution": as_list(
            recent_no_order_primary_candidate_bridge_density_roots.get(
                "root_cause_distribution"
            )
        ),
        "recent_directional_no_order_bridge_density_latest_route_action": (
            recent_no_order_primary_candidate_bridge_density_latest.get(
                "latest_route_action"
            )
        ),
        "recent_directional_no_order_bridge_density_primary_candidate_route_action": (
            recent_no_order_primary_candidate_bridge_density_latest.get(
                "primary_candidate_route_action"
            )
        ),
        "recent_directional_no_order_bridge_density_route_action_differs": (
            recent_no_order_primary_candidate_bridge_density_latest.get(
                "route_action_differs"
            )
        ),
        "recent_directional_no_order_bridge_density_route_roots_distinct": (
            recent_no_order_primary_candidate_bridge_density_latest.get(
                "route_root_and_primary_candidate_root_distinct"
            )
        ),
        "recent_directional_no_order_bridge_density_primary_candidate_root_cause": (
            recent_no_order_primary_candidate_bridge_density_latest.get(
                "primary_candidate_no_order_root_cause"
            )
        ),
        "recent_directional_no_order_bridge_density_portfolio_route_root_cause": (
            recent_no_order_primary_candidate_bridge_density_latest.get(
                "portfolio_route_no_order_root_cause"
            )
        ),
        "recent_directional_no_order_bridge_density_recent_roots_non_material": (
            recent_no_order_primary_candidate_bridge_density_interpretation.get(
                "recent_portfolio_route_roots_non_material_without_order_or_fill_change"
            )
        ),
        "recent_directional_no_order_bridge_density_recent_roots_require_order_or_fill_change": (
            recent_no_order_primary_candidate_bridge_density_interpretation.get(
                "recent_portfolio_route_roots_require_order_or_fill_change_for_materiality"
            )
        ),
        "recent_directional_no_order_bridge_density_latest_root_distinct": (
            recent_no_order_primary_candidate_bridge_density_interpretation.get(
                "latest_primary_candidate_root_distinct_from_portfolio_route_root"
            )
        ),
        "recent_directional_no_order_bridge_density_not_alpha_or_profitability_evidence": (
            recent_no_order_primary_candidate_bridge_density_interpretation.get(
                "not_alpha_or_profitability_evidence"
            )
        ),
        "silver_orderbook_truth_status": (
            as_dict(execution_science.get("silver_orderbook")).get("status")
        ),
        "silver_trade_flow_truth_status": (
            as_dict(execution_science.get("silver_trade_flow")).get("status")
        ),
        "orderbook_payload_depth_truth_status": orderbook_payload_depth.get("status"),
        "orderbook_payload_depth_smallest_missing_field": (
            orderbook_payload_depth.get("smallest_missing_field")
        ),
        "orderbook_payload_depth_raw_payload_exposed": orderbook_payload_depth.get("raw_payload_exposed"),
        "orderbook_payload_depth_books5_payload_hash_present": (
            orderbook_payload_depth_books5.get("payload_hash_present")
        ),
        "orderbook_payload_depth_books5_row_checksum_present": (
            orderbook_payload_depth_books5.get("row_checksum_present")
        ),
        "orderbook_payload_depth_books5_exchange_sequence_id_present": (
            orderbook_payload_depth_books5.get("exchange_sequence_id_present")
        ),
        "orderbook_payload_depth_books5_capture_status": (
            orderbook_payload_depth_books5.get("capture_status")
        ),
        "orderbook_payload_depth_books5_collector_sequence": (
            orderbook_payload_depth_books5.get("collector_sequence")
        ),
        "orderbook_payload_depth_books5_row_count": (
            orderbook_payload_depth_sequence.get("books5_row_count")
        ),
        "orderbook_payload_depth_books5_sequence_gap_count": (
            orderbook_payload_depth_sequence.get("books5_sequence_gap_count")
        ),
        "orderbook_payload_depth_bbo_payload_hash_present": (
            orderbook_payload_depth_bbo.get("payload_hash_present")
        ),
        "orderbook_payload_depth_bbo_row_count": orderbook_payload_depth_sequence.get("bbo_row_count"),
        "orderbook_payload_depth_bbo_sequence_gap_count": (
            orderbook_payload_depth_sequence.get("bbo_sequence_gap_count")
        ),
        "orderbook_payload_depth_diff_payload_persisted_row_count": (
            orderbook_payload_depth_sequence.get("diff_payload_persisted_row_count")
        ),
        "orderbook_payload_depth_silver_books5_samples_n": (
            orderbook_payload_depth_silver.get("books5_samples_n")
        ),
        "slippage_cost_calibration_truth_status": slippage_cost.get("status"),
        "slippage_cost_calibration_smallest_missing_field": slippage_cost.get("smallest_missing_field"),
        "slippage_cost_fee_sample_count": (
            as_dict(slippage_cost.get("fee")).get("sample_count")
        ),
        "slippage_cost_slippage_proxy_sample_count": (
            slippage_proxy.get("sample_count")
        ),
        "slippage_reference_coverage_classification": slippage_coverage.get("classification"),
        "slippage_missing_reference_fills": slippage_coverage.get("missing_reference_fills"),
        "slippage_missing_reference_fills_with_submit_command": slippage_coverage.get(
            "missing_reference_fills_with_submit_command"
        ),
        "slippage_missing_reference_fills_without_submit_command": slippage_coverage.get(
            "missing_reference_fills_without_submit_command"
        ),
        "slippage_covered_reference_fills_with_command_reference": slippage_coverage.get(
            "covered_reference_fills_with_command_reference"
        ),
        "slippage_reference_deterministic_backfill_status": slippage_coverage.get(
            "deterministic_backfill_status"
        ),
        "slippage_reference_deterministic_backfill_reason": slippage_coverage.get(
            "deterministic_backfill_reason"
        ),
        "slippage_reference_deterministic_backfill_fill_count": slippage_coverage.get(
            "deterministic_backfill_fill_count"
        ),
        "slippage_reference_deterministic_backfill_mutates_database": slippage_coverage.get(
            "deterministic_backfill_mutates_database"
        ),
        "slippage_reference_policy": slippage_coverage.get("reference_policy"),
        "depth_slippage_lifecycle_truth_status": depth_slippage_lifecycle.get("status"),
        "depth_slippage_lifecycle_smallest_missing_field": (
            depth_slippage_lifecycle.get("smallest_missing_field")
        ),
        "depth_slippage_lifecycle_raw_payload_exposed": depth_slippage_lifecycle.get(
            "raw_payload_exposed"
        ),
        "depth_slippage_lifecycle_forward_depth_ready": (
            depth_slippage_lifecycle_interpretation.get("forward_depth_ready")
        ),
        "depth_slippage_lifecycle_existing_fill_slippage_baseline_present": (
            depth_slippage_lifecycle_interpretation.get("existing_fill_slippage_baseline_present")
        ),
        "depth_slippage_lifecycle_per_recent_directional_fill_link_present": (
            depth_slippage_lifecycle_interpretation.get(
                "per_recent_directional_fill_depth_lifecycle_link_present"
            )
        ),
        "depth_slippage_lifecycle_no_order_expected_regime": (
            depth_slippage_lifecycle_interpretation.get("no_order_expected_regime")
        ),
        "depth_slippage_lifecycle_waiting_for_executable_directional_episode": (
            depth_slippage_lifecycle_interpretation.get("waiting_for_executable_directional_episode")
        ),
        "depth_slippage_lifecycle_depth_books5_row_count": (
            depth_slippage_lifecycle_depth.get("books5_row_count")
        ),
        "depth_slippage_lifecycle_depth_books5_sequence_gap_count": (
            depth_slippage_lifecycle_depth.get("books5_sequence_gap_count")
        ),
        "depth_slippage_lifecycle_slippage_proxy_sample_count": (
            depth_slippage_lifecycle_slippage.get("slippage_proxy_sample_count")
        ),
        "depth_slippage_lifecycle_fee_sample_count": (
            depth_slippage_lifecycle_slippage.get("fee_sample_count")
        ),
        "depth_slippage_lifecycle_current_submit_reference_covered_fill_count": (
            depth_slippage_lifecycle_command.get(
                "current_submit_command_reference_covered_fill_count"
            )
        ),
        "depth_slippage_lifecycle_current_submit_reference_missing_fill_count": (
            depth_slippage_lifecycle_command.get(
                "current_submit_command_reference_missing_fill_count"
            )
        ),
        "depth_slippage_lifecycle_recent_filled_decision_count": (
            depth_slippage_lifecycle_recent.get("recent_filled_decision_count")
        ),
        "depth_slippage_lifecycle_recent_filled_with_pretrade_microstructure": (
            depth_slippage_lifecycle_recent.get("recent_filled_with_pretrade_microstructure")
        ),
        "depth_slippage_lifecycle_recent_filled_with_resolved_pnl_lifecycle": (
            depth_slippage_lifecycle_recent.get("recent_filled_with_resolved_pnl_lifecycle")
        ),
        "directional_command_flow_provenance_truth_status": directional_command_flow.get("status"),
        "directional_command_flow_provenance_smallest_missing_field": directional_command_flow.get(
            "smallest_missing_field"
        ),
        "directional_command_flow_current_reference_gap": directional_command_flow.get(
            "current_command_path_reference_gap"
        ),
        "directional_command_flow_current_submit_fill_count": directional_command_flow_coverage.get(
            "current_submit_command_fill_count"
        ),
        "directional_command_flow_current_reference_covered_fill_count": directional_command_flow_coverage.get(
            "current_submit_command_reference_covered_fill_count"
        ),
        "directional_command_flow_current_reference_missing_fill_count": directional_command_flow_coverage.get(
            "current_submit_command_reference_missing_fill_count"
        ),
        "directional_command_flow_historical_no_submit_fill_count": directional_command_flow_coverage.get(
            "historical_no_submit_command_fill_count"
        ),
        "directional_command_flow_historical_no_submit_reference_missing_fill_count": (
            directional_command_flow_coverage.get("historical_no_submit_command_reference_missing_fill_count")
        ),
        "directional_episode_attribution_truth_status": directional_attribution.get("status"),
        "directional_episode_attribution_smallest_missing_field": directional_attribution.get(
            "smallest_missing_field"
        ),
        "target_convergence_guard_truth_status": target_convergence_guard.get("status"),
        "target_convergence_guard_smallest_missing_field": target_convergence_guard.get("smallest_missing_field"),
        "target_convergence_guard_flag": target_convergence_guard.get("guard_flag"),
        "target_convergence_guard_directional_decisions_1h": target_convergence_guard_coverage.get(
            "directional_decisions_1h"
        ),
        "target_convergence_guard_hits_24h": target_convergence_guard_coverage.get("guard_hits_24h"),
        "target_convergence_guard_hits_1h": target_convergence_guard_coverage.get("guard_hits_1h"),
        "target_convergence_guard_current_open_order_count": target_convergence_guard_open_orders.get(
            "total_open_order_count"
        ),
        "target_convergence_guard_latest_hit_decision_id": target_convergence_guard_latest_hit.get("decision_id"),
        "target_convergence_guard_latest_hit_created_at": target_convergence_guard_latest_hit.get("created_at"),
        "directional_impulse_chase_guard_truth_status": directional_impulse_chase_guard.get("status"),
        "directional_impulse_chase_guard_smallest_missing_field": directional_impulse_chase_guard.get(
            "smallest_missing_field"
        ),
        "directional_impulse_chase_guard_code_present": directional_impulse_chase_guard_code.get(
            "all_required_markers_present"
        ),
        "directional_impulse_chase_guard_deployed_matches_windows": directional_impulse_chase_guard.get(
            "deployed_matches_windows"
        ),
        "directional_impulse_chase_guard_directional_decisions_1h": (
            directional_impulse_chase_guard_coverage.get("directional_decisions_1h")
        ),
        "directional_impulse_chase_guard_hits_24h": directional_impulse_chase_guard_coverage.get(
            "guard_hits_24h"
        ),
        "directional_impulse_chase_guard_hits_1h": directional_impulse_chase_guard_coverage.get(
            "guard_hits_1h"
        ),
        "directional_impulse_chase_guard_blocked_live_entry_hits_total": (
            directional_impulse_chase_guard_coverage.get("blocked_live_entry_hits_total")
        ),
        "directional_impulse_chase_guard_blocked_live_entry_hits_24h": (
            directional_impulse_chase_guard_coverage.get("blocked_live_entry_hits_24h")
        ),
        "directional_impulse_chase_guard_blocked_live_entry_hits_1h": (
            directional_impulse_chase_guard_coverage.get("blocked_live_entry_hits_1h")
        ),
        "directional_impulse_chase_guard_latest_hit_decision_id": (
            directional_impulse_chase_guard_latest_hit.get("decision_id")
        ),
        "directional_impulse_chase_guard_latest_hit_created_at": (
            directional_impulse_chase_guard_latest_hit.get("created_at")
        ),
        "directional_impulse_chase_guard_latest_hit_matched_flags": (
            directional_impulse_chase_guard_latest_hit.get("matched_guard_flags")
        ),
        "okx_hedge_scale_in_intent_truth_status": okx_hedge_scale_in_intent.get("status"),
        "okx_hedge_scale_in_intent_smallest_missing_field": okx_hedge_scale_in_intent.get(
            "smallest_missing_field"
        ),
        "okx_hedge_scale_in_intent_code_present": okx_hedge_scale_in_intent_code.get(
            "all_required_markers_present"
        ),
        "okx_hedge_scale_in_intent_deployed_matches_windows": okx_hedge_scale_in_intent.get(
            "deployed_matches_windows"
        ),
        "okx_hedge_scale_in_intent_mismatch_total": okx_hedge_scale_in_intent_coverage.get(
            "mismatch_total"
        ),
        "okx_hedge_scale_in_intent_mismatch_24h": okx_hedge_scale_in_intent_coverage.get(
            "mismatch_24h"
        ),
        "okx_hedge_scale_in_intent_mismatch_1h": okx_hedge_scale_in_intent_coverage.get(
            "mismatch_1h"
        ),
        "okx_hedge_scale_in_open_leg_total": okx_hedge_scale_in_intent_coverage.get(
            "open_scale_in_leg_total"
        ),
        "okx_hedge_scale_in_open_leg_24h": okx_hedge_scale_in_intent_coverage.get(
            "open_scale_in_leg_24h"
        ),
        "okx_hedge_scale_in_open_leg_1h": okx_hedge_scale_in_intent_coverage.get(
            "open_scale_in_leg_1h"
        ),
        "okx_hedge_scale_in_intent_latest_mismatch_created_at": (
            okx_hedge_scale_in_intent.get("latest_mismatch_created_at")
        ),
        "created_no_command_directional_order_truth_status": created_no_command_directional_order.get("status"),
        "created_no_command_directional_order_smallest_missing_field": (
            created_no_command_directional_order.get("smallest_missing_field")
        ),
        "created_no_command_directional_order_deployed_matches_windows": (
            created_no_command_directional_order.get("deployed_matches_windows")
        ),
        "created_no_command_directional_order_root_cause": created_no_command_directional_order.get(
            "root_cause"
        ),
        "created_no_command_directional_order_missing_total": (
            created_no_command_directional_order_coverage.get("missing_total")
        ),
        "created_no_command_directional_order_missing_24h": (
            created_no_command_directional_order_coverage.get("missing_24h")
        ),
        "created_no_command_directional_order_missing_1h": (
            created_no_command_directional_order_coverage.get("missing_1h")
        ),
        "created_no_command_directional_order_latest_created_at": (
            created_no_command_directional_order_coverage.get("latest_created_at")
        ),
        "execution_order_payload_status_residual_truth_status": (
            execution_order_payload_status_residual.get("status")
        ),
        "execution_order_payload_status_residual_smallest_missing_field": (
            execution_order_payload_status_residual.get("smallest_missing_field")
        ),
        "execution_order_payload_status_authoritative_source": (
            execution_order_payload_status_residual_authority.get("order_status_source")
        ),
        "execution_order_payload_status_top_level_authoritative": (
            execution_order_payload_status_residual_authority.get(
                "raw_payload_top_level_status_authoritative"
            )
        ),
        "execution_order_payload_status_top_level_mismatch_count": (
            execution_order_payload_status_residual_coverage.get("top_level_status_mismatch_count")
        ),
        "execution_order_payload_status_nested_mismatch_count": (
            execution_order_payload_status_residual_coverage.get("nested_status_mismatch_count")
        ),
        "execution_order_payload_status_terminal_column_nonterminal_top_level_count": (
            execution_order_payload_status_residual_coverage.get(
                "terminal_column_nonterminal_top_level_count"
            )
        ),
        "execution_order_payload_status_open_column_terminal_top_level_count": (
            execution_order_payload_status_residual_coverage.get(
                "open_column_terminal_top_level_count"
            )
        ),
        "execution_order_payload_status_open_by_column_count": (
            execution_order_payload_status_residual_coverage.get("open_by_column_count")
        ),
        "execution_order_payload_status_open_by_top_level_raw_payload_count": (
            execution_order_payload_status_residual_coverage.get(
                "open_by_top_level_raw_payload_count"
            )
        ),
        "execution_order_payload_status_raw_payload_status_would_misclassify_open_orders": (
            execution_order_payload_status_residual_coverage.get(
                "raw_payload_status_would_misclassify_open_orders"
            )
        ),
        "execution_order_payload_status_terminal_column_nonterminal_nested_count": (
            execution_order_payload_status_residual_coverage.get(
                "terminal_column_nonterminal_nested_count"
            )
        ),
        "execution_order_payload_status_open_column_terminal_nested_count": (
            execution_order_payload_status_residual_coverage.get(
                "open_column_terminal_nested_count"
            )
        ),
        "execution_order_payload_status_target_client_order_id": (
            execution_order_payload_status_residual_target.get("client_order_id")
        ),
        "execution_order_payload_status_target_state": (
            execution_order_payload_status_residual_target.get("state")
        ),
        "execution_order_payload_status_target_raw_payload_status": (
            execution_order_payload_status_residual_target.get("raw_payload_status")
        ),
        "execution_order_payload_status_target_nested_order_state_status": (
            execution_order_payload_status_residual_target.get("nested_order_state_status")
        ),
        "execution_order_payload_status_target_top_level_mismatch": (
            execution_order_payload_status_residual_target.get("top_level_status_mismatch")
        ),
        "execution_order_payload_status_target_nested_matches_column": (
            execution_order_payload_status_residual_target.get("nested_status_matches_column")
        ),
        "claimed_submit_stuck_submission_truth_status": claimed_submit_stuck_submission.get("status"),
        "claimed_submit_stuck_submission_smallest_missing_field": (
            claimed_submit_stuck_submission.get("smallest_missing_field")
        ),
        "claimed_submit_stuck_submission_root_cause": claimed_submit_stuck_submission.get("root_cause"),
        "claimed_submit_stuck_submission_count": (
            claimed_submit_stuck_submission_coverage.get("claimed_submit_stuck_submission_count")
        ),
        "claimed_submit_stuck_submission_24h": (
            claimed_submit_stuck_submission_coverage.get("claimed_submit_stuck_submission_24h")
        ),
        "claimed_submit_stuck_submission_1h": (
            claimed_submit_stuck_submission_coverage.get("claimed_submit_stuck_submission_1h")
        ),
        "claimed_submit_stuck_submission_client_order_id": claimed_submit_latest_order.get(
            "client_order_id"
        ),
        "claimed_submit_stuck_submission_command_id": claimed_submit_latest_order.get("command_id"),
        "claimed_submit_stuck_submission_execution_order_state": claimed_submit_latest_order.get(
            "execution_order_state"
        ),
        "claimed_submit_stuck_submission_command_state": claimed_submit_latest_order.get(
            "command_state"
        ),
        "claimed_submit_stuck_submission_position_intent": claimed_submit_latest_order.get(
            "position_intent"
        ),
        "claimed_submit_stuck_submission_reduce_only": claimed_submit_latest_order.get("reduce_only"),
        "claimed_submit_stuck_submission_close_only": claimed_submit_latest_order.get("close_only"),
        "claimed_submit_stuck_submission_execution_fill_count": claimed_submit_latest_order.get(
            "execution_fill_count"
        ),
        "claimed_submit_stuck_submission_fill_event_count": claimed_submit_latest_order.get(
            "fill_event_count"
        ),
        "claimed_submit_stuck_submission_required_operator_confirmation": (
            claimed_submit_stuck_submission.get("required_operator_confirmation")
        ),
        "claimed_submit_stuck_submission_current_blocker": claimed_submit_stuck_submission.get(
            "current_blocker"
        ),
        "claimed_submit_stuck_submission_latest_reconciliation_id": (
            claimed_submit_latest_reconciliation.get("reconciliation_id")
        ),
        "claimed_submit_stuck_submission_latest_reconciliation_severity": (
            claimed_submit_latest_reconciliation.get("severity")
        ),
        "claimed_submit_stuck_submission_latest_reconciliation_halt_required": (
            claimed_submit_latest_reconciliation.get("halt_required")
        ),
        "claimed_submit_stuck_submission_latest_baseline_kind": claimed_submit_latest_baseline.get(
            "baseline_kind"
        ),
        "claimed_submit_stuck_submission_latest_baseline_safe_for_automatic_continuation": (
            claimed_submit_latest_baseline.get("safe_for_automatic_continuation")
        ),
        "claimed_submit_stuck_submission_latest_baseline_requires_operator_review": (
            claimed_submit_latest_baseline.get("requires_operator_review")
        ),
        "claimed_submit_stuck_submission_resolve_action_count_for_order": (
            claimed_submit_operator_action_counts.get("resolve_stuck_submission_for_order")
        ),
        "claimed_submit_operator_handoff_truth_status": claimed_submit_operator_handoff.get("status"),
        "claimed_submit_operator_handoff_smallest_missing_field": (
            claimed_submit_operator_handoff.get("smallest_missing_field")
        ),
        "claimed_submit_operator_handoff_current_blocker": (
            claimed_submit_operator_handoff.get("current_blocker")
        ),
        "claimed_submit_operator_handoff_artifact": claimed_submit_operator_handoff.get("artifact_path"),
        "claimed_submit_operator_handoff_generated_from_runtime_truth_at": (
            claimed_submit_operator_handoff.get("generated_from_runtime_truth_at")
        ),
        "claimed_submit_operator_handoff_status": claimed_submit_operator_handoff.get("handoff_status"),
        "claimed_submit_operator_handoff_validation_status": (
            claimed_submit_operator_handoff.get("validation_status")
        ),
        "claimed_submit_operator_handoff_next_action": claimed_submit_operator_handoff.get("next_action"),
        "claimed_submit_operator_handoff_valid": claimed_submit_operator_handoff.get("valid"),
        "claimed_submit_operator_handoff_ready_for_protected_recovery": (
            claimed_submit_operator_handoff.get("ready_for_protected_recovery")
        ),
        "claimed_submit_operator_handoff_operator_confirmation_matched": (
            claimed_submit_operator_handoff.get("operator_confirmation_matched")
        ),
        "claimed_submit_operator_handoff_matches_current_order": (
            claimed_submit_operator_handoff.get("matches_current_order")
        ),
        "claimed_submit_operator_handoff_matches_required_confirmation": (
            claimed_submit_operator_handoff.get("matches_required_confirmation")
        ),
        "claimed_submit_operator_handoff_source_runtime_truth": (
            claimed_submit_operator_handoff_sources.get("runtime_truth")
        ),
        "claimed_submit_operator_handoff_source_packet": (
            claimed_submit_operator_handoff_sources.get("packet")
        ),
        "directional_episode_recent_decision_count": directional_attribution_coverage.get("recent_decision_count"),
        "directional_episode_decisions_with_edge_cost": directional_attribution_coverage.get(
            "decisions_with_edge_cost"
        ),
        "directional_episode_decisions_with_no_order_expected": directional_attribution_coverage.get(
            "decisions_with_no_order_expected"
        ),
        "directional_episode_no_order_semantics_status": directional_no_order_semantics.get("status"),
        "directional_episode_no_order_semantics_smallest_missing_field": (
            directional_no_order_semantics.get("smallest_missing_field")
        ),
        "directional_episode_decisions_with_no_order_semantics": (
            directional_no_order_semantics_coverage.get("decisions_with_no_order_semantics")
        ),
        "directional_episode_decisions_with_stable_no_order_equivalence_class": (
            directional_no_order_semantics_coverage.get(
                "decisions_with_stable_no_order_equivalence_class"
            )
        ),
        "directional_episode_all_no_order_expected_decisions_have_no_order_semantics": (
            directional_no_order_semantics_coverage.get(
                "all_no_order_expected_decisions_have_no_order_semantics"
            )
        ),
        "directional_episode_all_no_order_expected_decisions_stable_equivalence_class": (
            directional_no_order_semantics_coverage.get(
                "all_no_order_expected_decisions_stable_equivalence_class"
            )
        ),
        "directional_episode_no_order_equivalence_classes": (
            directional_no_order_semantics.get("equivalence_classes")
        ),
        "directional_episode_no_order_root_material_without_order_or_fill_change": (
            directional_no_order_semantics.get("root_cause_is_material_without_order_or_fill_change")
        ),
        "directional_episode_no_order_requires_order_or_fill_change_for_materiality": (
            directional_no_order_semantics.get("requires_order_or_fill_change_for_materiality")
        ),
        "directional_episode_decisions_with_order_surface_or_no_order_expectation": (
            directional_attribution_coverage.get("decisions_with_order_surface_or_no_order_expectation")
        ),
        "directional_episode_decisions_requiring_order_surface": directional_attribution_coverage.get(
            "decisions_requiring_order_surface"
        ),
        "directional_episode_decisions_missing_order_surface": directional_attribution_coverage.get(
            "decisions_missing_order_surface"
        ),
        "directional_episode_all_recent_decisions_no_order_expected": directional_attribution_coverage.get(
            "all_recent_decisions_no_order_expected"
        ),
        "directional_episode_decisions_with_fills": directional_attribution_coverage.get("decisions_with_fills"),
        "directional_episode_decisions_with_pnl_outcome": directional_attribution_coverage.get(
            "decisions_with_pnl_outcome"
        ),
        "directional_episode_pnl_lifecycle_status": directional_pnl_lifecycle.get("status"),
        "directional_episode_pnl_lifecycle_smallest_missing_field": directional_pnl_lifecycle.get(
            "smallest_missing_field"
        ),
        "directional_episode_filled_decisions_with_pnl_lifecycle_classification": (
            directional_pnl_lifecycle_coverage.get("filled_decisions_with_pnl_lifecycle_classification")
        ),
        "directional_episode_filled_decisions_with_resolved_pnl_lifecycle": (
            directional_pnl_lifecycle_coverage.get("filled_decisions_with_resolved_pnl_lifecycle")
        ),
        "directional_episode_pretrade_microstructure_status": directional_pretrade_microstructure.get("status"),
        "directional_episode_pretrade_microstructure_smallest_missing_field": (
            directional_pretrade_microstructure.get("smallest_missing_field")
        ),
        "directional_episode_decisions_with_pretrade_microstructure": (
            directional_pretrade_microstructure_coverage.get("decisions_with_pretrade_microstructure")
        ),
        "directional_episode_filled_decisions_with_pretrade_microstructure": (
            directional_pretrade_microstructure_coverage.get("filled_decisions_with_pretrade_microstructure")
        ),
        "latest_directional_episode_decision_id": latest_directional_episode.get("decision_id"),
        "latest_directional_episode_expected_net_edge_bps": latest_directional_episode.get("expected_net_edge_bps"),
        "latest_directional_episode_realized_cost_proxy_bps": latest_directional_episode.get(
            "realized_cost_proxy_bps"
        ),
        "latest_directional_episode_fill_count": latest_directional_episode_fill.get("count"),
        "latest_directional_episode_realized_pnl_usdt": latest_directional_episode_pnl.get("realized_pnl_usdt"),
        "latest_directional_episode_pnl_lifecycle_status": latest_directional_episode_pnl_lifecycle.get("status"),
        "latest_directional_episode_pnl_lifecycle_smallest_missing_field": (
            latest_directional_episode_pnl_lifecycle.get("smallest_missing_field")
        ),
        "latest_directional_episode_pretrade_microstructure_status": (
            latest_directional_episode_pretrade_microstructure.get("status")
        ),
        "latest_directional_episode_pretrade_microstructure_smallest_missing_field": (
            latest_directional_episode_pretrade_microstructure.get("smallest_missing_field")
        ),
        "directional_spike_reversion_truth_status": directional_spike_reversion.get("status"),
        "directional_spike_reversion_smallest_missing_field": directional_spike_reversion.get(
            "smallest_missing_field"
        ),
        "directional_spike_reversion_filled_decision_count": directional_spike_reversion_coverage.get(
            "recent_filled_directional_decision_count"
        ),
        "directional_spike_reversion_context_count": directional_spike_reversion_coverage.get(
            "filled_decisions_with_spike_reversion_context"
        ),
        "directional_spike_reversion_adverse_fill_10bps_count": directional_spike_reversion_coverage.get(
            "adverse_fill_vs_decision_mid_10bps_count"
        ),
        "directional_spike_reversion_post_fill_adverse_reversion_10bps_count": (
            directional_spike_reversion_coverage.get("post_fill_adverse_reversion_10bps_count")
        ),
        "directional_spike_reversion_trade_flow_dislocation_10bps_count": (
            directional_spike_reversion_coverage.get("decision_trade_flow_dislocation_10bps_count")
        ),
        "latest_directional_spike_reversion_classification": latest_directional_spike_reversion.get(
            "classification"
        ),
        "latest_directional_spike_reversion_adverse_fill_vs_decision_mid_bps": (
            latest_directional_spike_reversion.get("adverse_fill_vs_decision_mid_bps")
        ),
        "latest_directional_spike_reversion_post_fill_mid_move_bps": latest_directional_spike_reversion.get(
            "post_fill_mid_move_bps"
        ),
        "latest_directional_spike_reversion_decision_trade_flow_vwap_minus_mid_bps": (
            latest_directional_spike_reversion.get("decision_trade_flow_vwap_minus_mid_bps")
        ),
        "ai_runtime_effective_truth_status": ai_runtime_effective.get("status"),
        "ai_runtime_effective_truth_smallest_missing_field": ai_runtime_effective.get(
            "smallest_missing_field"
        ),
        "ai_runtime_effective_auth_required": ai_runtime_auth_gate.get("auth_required"),
        "ai_runtime_effective_dashboard_status": ai_runtime_auth_gate.get("dashboard_status"),
        "ai_runtime_endpoint_status": ai_runtime_auth_gate.get("ai_runtime_endpoint_status"),
        "ai_runtime_endpoint_http_status": ai_runtime_auth_gate.get("ai_runtime_http_status"),
        "ai_runtime_operator_read_auth_status": operator_read_auth.get("status"),
        "ai_runtime_operator_read_auth_method": operator_read_auth.get("method"),
        "ai_runtime_operator_read_auth_env_var": operator_read_auth.get("env_var"),
        "ai_runtime_operator_read_auth_credential_present": operator_read_auth.get(
            "credential_present"
        ),
        "ai_runtime_operator_read_auth_header_injected": operator_read_auth.get("header_injected"),
        "ai_runtime_configured_operating_mode": ai_runtime_configured.get("value"),
        "ai_runtime_configured_operating_mode_status": ai_runtime_configured.get("status"),
        "ai_runtime_effective_operating_mode": ai_runtime_effective_mode.get("value"),
        "ai_runtime_effective_operating_mode_status": ai_runtime_effective_mode.get("status"),
        "ai_runtime_manual_override_status": ai_runtime_manual_override.get("status"),
        "ai_runtime_manual_override_active": ai_runtime_manual_override.get("active"),
        "ai_runtime_manual_override_mode": ai_runtime_manual_override.get("mode"),
        "ai_runtime_provider_name": ai_runtime_provider.get("name"),
        "ai_runtime_provider_state": ai_runtime_provider.get("state"),
        "ai_runtime_provider_configured": ai_runtime_provider_configured.get("value"),
        "ai_runtime_provider_configured_status": ai_runtime_provider_configured.get("status"),
        "ai_runtime_provider_ready": ai_runtime_provider_ready.get("value"),
        "ai_runtime_provider_ready_status": ai_runtime_provider_ready.get("status"),
        "ai_runtime_provider_degraded": ai_runtime_provider_degraded.get("value"),
        "ai_runtime_provider_degraded_status": ai_runtime_provider_degraded.get("status"),
        "ai_runtime_provider_path_evidence_present": ai_runtime_provider.get(
            "path_evidence_present"
        ),
        "ai_runtime_provider_path_active": ai_runtime_provider.get("path_active"),
        "ai_runtime_shadow_enabled": ai_runtime_shadow.get("value"),
        "ai_runtime_shadow_enabled_status": ai_runtime_shadow.get("status"),
        "ai_runtime_profile_auto_control_effective": ai_runtime_profile_auto.get("value"),
        "ai_runtime_profile_auto_control_status": ai_runtime_profile_auto.get("status"),
        "ai_runtime_timeout_blocker_classification": ai_runtime_timeout.get("classification"),
        "effective_operating_mode": (dashboard.get("effective_operating_mode") or {}).get("value"),
        "effective_operating_mode_status": (dashboard.get("effective_operating_mode") or {}).get("status"),
        "profile_auto_control_effective": (dashboard.get("profile_auto_control_effective") or {}).get("value"),
        "profile_auto_control_status": (dashboard.get("profile_auto_control_effective") or {}).get("status"),
        "dashboard_bundle_status": dashboard.get("status"),
        "deployed_matches_windows": git.get("deployed_matches_windows"),
        "windows_dirty": ((git.get("windows") or {}).get("dirty")),
        "windows_origin_divergence": ((git.get("windows") or {}).get("origin_divergence")),
        "gateway_health_ok": ((health.get("gateway_health") or {}).get("ok")),
        "required_app_containers_healthy": ((health.get("containers") or {}).get("all_required_app_containers_healthy")),
        "shadow_benchmark": ((report.get("scope") or {}).get("shadow_benchmark")),
        "ai_timeout_active_blocker": ((report.get("runtime") or {}).get("ai_timeout_active_blocker")),
    }


def infer_live_carrier_from_database_truth(db: dict[str, Any]) -> str | None:
    latest = db.get("latest_decision") if isinstance(db.get("latest_decision"), dict) else {}
    carrier = latest.get("primary_family")
    if isinstance(carrier, str) and carrier.strip():
        return carrier.strip()

    latest_executable_directional = (
        db.get("latest_executable_directional_decision")
        if isinstance(db.get("latest_executable_directional_decision"), dict)
        else {}
    )
    fallback = latest_executable_directional.get("primary_family")
    if isinstance(fallback, str) and fallback.strip():
        return fallback.strip()
    return None


def apply_live_runtime_scope(report: dict[str, Any], live_facts: dict[str, Any]) -> None:
    scope = dict(as_dict(report.get("scope")))
    live_carrier = live_facts.get("active_live_carrier")
    if isinstance(live_carrier, str) and live_carrier.strip():
        scope["live_carrier"] = live_carrier.strip()
    report["scope"] = scope


def summarize_artifact_runtime_status(
    *,
    artifact_projection: dict[str, Any],
    live_facts: dict[str, Any],
    report_generated_at: str,
) -> dict[str, Any]:
    artifact_facts = artifact_projection.get("facts") or {}
    sources = artifact_projection.get("sources") or []
    report_time = parse_utc_timestamp(report_generated_at)

    newest_source_time: datetime | None = None
    newest_source_at: str | None = None
    for source in sources:
        source_time = parse_utc_timestamp(source.get("generated_at"))
        if source_time is not None and (newest_source_time is None or source_time > newest_source_time):
            newest_source_time = source_time
            newest_source_at = source.get("generated_at")

    mismatches: list[dict[str, Any]] = []
    compared: list[str] = []
    for fact in ARTIFACT_COMPARE_FACTS:
        if fact not in artifact_facts or fact not in live_facts:
            continue
        compared.append(fact)
        if artifact_facts.get(fact) != live_facts.get(fact):
            mismatches.append(
                {
                    "fact": fact,
                    "artifact_value": artifact_facts.get(fact),
                    "live_value": live_facts.get(fact),
                },
            )

    age_seconds = seconds_between(newest_source_time, report_time)
    age_stale = age_seconds is not None and age_seconds > ARTIFACT_STALE_AFTER_SECONDS
    if not artifact_facts:
        status = "missing_artifact"
    elif mismatches:
        status = "stale_mismatch"
    elif age_stale:
        status = "age_stale"
    elif compared:
        status = "fresh_match"
    else:
        status = "missing_live_comparison"

    return {
        "source": "artifact_last_known",
        "status": status,
        "may_override_live": False,
        "report_generated_at": report_generated_at,
        "newest_source_generated_at": newest_source_at,
        "newest_source_age_seconds": age_seconds,
        "stale_after_seconds": ARTIFACT_STALE_AFTER_SECONDS,
        "age_stale": age_stale,
        "compared_facts": compared,
        "mismatched_facts": mismatches,
        "source_count": len(sources),
    }


def summarize_runtime_fact_authority(
    *,
    live_facts: dict[str, Any],
    artifact_status: dict[str, Any],
) -> dict[str, Any]:
    return {
        "policy": "live_runtime_facts_are_authoritative",
        "authoritative_source": "runtime.live_runtime_facts",
        "fallback_reference_source": "runtime.artifact_last_known",
        "artifact_may_override_live": False,
        "artifact_status": artifact_status.get("status"),
        "artifact_stale_blocks_runtime": False,
        "authoritative_fact_keys": sorted(
            key for key, value in live_facts.items() if key not in {"source", "authoritative"} and value is not None
        ),
    }


def collect_blocking_findings(report: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    git = report.get("git", {})
    if (git.get("windows") or {}).get("dirty"):
        blockers.append("windows_worktree_dirty")
    if not git.get("deployed_matches_windows"):
        blockers.append("deployed_head_mismatch")
    win_div = ((git.get("windows") or {}).get("origin_divergence") or {})
    if win_div.get("ahead") not in (0, None) or win_div.get("behind") not in (0, None):
        blockers.append("windows_origin_divergence")

    health = report.get("deployment_health", {})
    if not ((health.get("gateway_health") or {}).get("ok")):
        blockers.append("gateway_health_failed")
    if not ((health.get("containers") or {}).get("all_required_app_containers_healthy")):
        blockers.append("required_app_container_unhealthy")

    if not ((report.get("database_truth") or {}).get("ok")):
        blockers.append("database_truth_unavailable")
    return blockers


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(args.repo_root).resolve()
    generated_at = utc_now_iso()
    artifact_projection = load_artifact_runtime_projection(repo_root, generated_at)
    operator_auth_context = operator_read_auth_context(args.operator_read_api_key_env)
    operator_auth_headers = operator_auth_context.get("headers") or None
    report: dict[str, Any] = {
        "ok": True,
        "generated_at": generated_at,
        "scope": {
            "venue": "OKX",
            "symbol": "BTC-USDT-SWAP",
            "live_carrier": "unknown_pending_database_truth",
            "shadow_benchmark": "none_verified",
        },
        "git": git_truth(repo_root, args.wsl_distro, args.wsl_project),
        "deployment_health": deployment_health(args.api_base, args.wsl_distro),
        "runtime": {
            "operator_read_auth": operator_read_auth_report(operator_auth_context),
            "dashboard_bundle": dashboard_bundle_probe(args.api_base, headers=operator_auth_headers),
            "ai_runtime_endpoint": ai_runtime_endpoint_probe(
                args.api_base,
                headers=operator_auth_headers,
                auth_context=operator_auth_context,
            ),
            "artifact_last_known": artifact_projection["facts"],
            "artifact_last_known_sources": artifact_projection["sources"],
            "artifact_last_known_status": artifact_projection["status"],
        },
        "database_truth": database_truth_probe(args.wsl_distro, args.gateway_container),
        "rdp_microstructure_truth": rdp_microstructure_truth_probe(args.wsl_distro, args.gateway_container),
        "microstructure_collector_truth": microstructure_collector_truth_probe(
            args.wsl_distro,
            args.microstructure_container,
        ),
        "static_truth_surface": static_truth_surface(args.api_base),
    }
    report["execution_science_truth"] = summarize_execution_science_truth(
        report["rdp_microstructure_truth"],
        report_generated_at=generated_at,
    )
    report["microstructure_runtime_growth_truth"] = summarize_microstructure_runtime_growth_truth(
        report["microstructure_collector_truth"],
        report["rdp_microstructure_truth"],
        report["execution_science_truth"],
        report_generated_at=generated_at,
    )
    report["orderbook_payload_depth_truth"] = summarize_orderbook_payload_depth_truth(
        report["rdp_microstructure_truth"],
        report["execution_science_truth"],
    )
    report["slippage_cost_calibration_truth"] = summarize_slippage_cost_calibration_truth(
        report["database_truth"],
        report["execution_science_truth"],
        report_generated_at=generated_at,
    )
    report["directional_command_flow_provenance_truth"] = summarize_directional_command_flow_provenance_truth(
        report["slippage_cost_calibration_truth"],
    )
    report["directional_episode_attribution_truth"] = summarize_directional_episode_attribution_truth(
        report["database_truth"],
        report["rdp_microstructure_truth"],
    )
    report["directional_executable_episode_truth"] = summarize_directional_executable_episode_truth(
        report["database_truth"],
    )
    report["depth_slippage_lifecycle_truth"] = summarize_depth_slippage_lifecycle_truth(
        orderbook_payload_depth=report["orderbook_payload_depth_truth"],
        slippage_cost=report["slippage_cost_calibration_truth"],
        directional_command_flow=report["directional_command_flow_provenance_truth"],
        directional_attribution=report["directional_episode_attribution_truth"],
    )
    report["directional_executable_terminal_no_fill_pretrade_microstructure_truth"] = (
        summarize_directional_executable_terminal_no_fill_pretrade_microstructure_truth(
            directional_executable_episode=report["directional_executable_episode_truth"],
            rdp_microstructure=report["rdp_microstructure_truth"],
            execution_science=report["execution_science_truth"],
            orderbook_payload_depth=report["orderbook_payload_depth_truth"],
            depth_slippage_lifecycle=report["depth_slippage_lifecycle_truth"],
            slippage_cost=report["slippage_cost_calibration_truth"],
        )
    )
    report["latest_decision_fill_feasibility_truth"] = summarize_latest_decision_fill_feasibility_truth(
        report["database_truth"],
        report["directional_episode_attribution_truth"],
        report["execution_science_truth"],
    )
    report["decision_lifecycle_provenance_continuity_truth"] = (
        summarize_decision_lifecycle_provenance_continuity_truth(
            db=report["database_truth"],
            directional_attribution=report["directional_episode_attribution_truth"],
            directional_executable_episode=report["directional_executable_episode_truth"],
            latest_decision_fill_feasibility=report["latest_decision_fill_feasibility_truth"],
            directional_command_flow=report["directional_command_flow_provenance_truth"],
            depth_slippage_lifecycle=report["depth_slippage_lifecycle_truth"],
        )
    )
    report["decision_lifecycle_execution_science_continuity_truth"] = (
        summarize_decision_lifecycle_execution_science_continuity_truth(
            decision_lifecycle_provenance_continuity=report[
                "decision_lifecycle_provenance_continuity_truth"
            ],
            executable_terminal_pretrade_microstructure=report[
                "directional_executable_terminal_no_fill_pretrade_microstructure_truth"
            ],
            orderbook_payload_depth=report["orderbook_payload_depth_truth"],
            latest_decision_fill_feasibility=report["latest_decision_fill_feasibility_truth"],
            depth_slippage_lifecycle=report["depth_slippage_lifecycle_truth"],
            execution_science=report["execution_science_truth"],
            slippage_cost=report["slippage_cost_calibration_truth"],
        )
    )
    report["recent_directional_decision_chain_density_truth"] = (
        summarize_recent_directional_decision_chain_density_truth(
            directional_attribution=report["directional_episode_attribution_truth"],
            decision_lifecycle_provenance_continuity=report[
                "decision_lifecycle_provenance_continuity_truth"
            ],
            decision_lifecycle_execution_science_continuity=report[
                "decision_lifecycle_execution_science_continuity_truth"
            ],
            directional_command_flow=report["directional_command_flow_provenance_truth"],
        )
    )
    report["recent_directional_no_order_root_cause_density_truth"] = (
        summarize_recent_directional_no_order_root_cause_density_truth(
            directional_attribution=report["directional_episode_attribution_truth"],
        )
    )
    report["latest_directional_no_order_primary_candidate_bridge_truth"] = (
        summarize_latest_directional_no_order_primary_candidate_bridge_truth(
            db=report["database_truth"],
        )
    )
    report["recent_directional_no_order_primary_candidate_bridge_density_truth"] = (
        summarize_recent_directional_no_order_primary_candidate_bridge_density_truth(
            directional_attribution=report["directional_episode_attribution_truth"],
            recent_no_order_root_density=report[
                "recent_directional_no_order_root_cause_density_truth"
            ],
            latest_bridge=report[
                "latest_directional_no_order_primary_candidate_bridge_truth"
            ],
        )
    )
    report["directional_spike_reversion_truth"] = summarize_directional_spike_reversion_truth(
        report["directional_episode_attribution_truth"],
    )
    report["target_convergence_guard_truth"] = summarize_target_convergence_guard_truth(
        report["database_truth"],
        report["git"],
        report_generated_at=generated_at,
    )
    report["claimed_submit_stuck_submission_truth"] = (
        summarize_claimed_submit_stuck_submission_truth(
            report["database_truth"],
            report_generated_at=generated_at,
        )
    )
    report["claimed_submit_operator_handoff_truth"] = (
        summarize_claimed_submit_operator_handoff_truth(
            repo_root,
            report["claimed_submit_stuck_submission_truth"],
            report_generated_at=generated_at,
        )
    )
    report["directional_impulse_chase_guard_truth"] = summarize_directional_impulse_chase_guard_truth(
        report["database_truth"],
        report["git"],
        directional_impulse_chase_guard_code_markers(repo_root),
        report_generated_at=generated_at,
    )
    report["okx_hedge_scale_in_intent_truth"] = summarize_okx_hedge_scale_in_intent_truth(
        report["database_truth"],
        report["git"],
        okx_hedge_scale_in_code_markers(repo_root),
        report_generated_at=generated_at,
    )
    report["created_no_command_directional_order_truth"] = summarize_created_no_command_directional_order_truth(
        report["database_truth"],
        report["git"],
        report_generated_at=generated_at,
    )
    report["execution_order_payload_status_residual_truth"] = (
        summarize_execution_order_payload_status_residual_truth(
            report["database_truth"],
            report_generated_at=generated_at,
        )
    )
    report["ai_runtime_effective_mode_truth"] = summarize_ai_runtime_effective_mode_truth(
        dashboard_bundle=report["runtime"]["dashboard_bundle"],
        ai_runtime_endpoint=report["runtime"]["ai_runtime_endpoint"],
    )
    report["runtime"]["ai_timeout_active_blocker"] = report["ai_runtime_effective_mode_truth"][
        "ai_timeout"
    ]["active_blocker"]
    live_facts = project_live_runtime_facts(report)
    apply_live_runtime_scope(report, live_facts)
    artifact_status = summarize_artifact_runtime_status(
        artifact_projection=artifact_projection,
        live_facts=live_facts,
        report_generated_at=generated_at,
    )
    report["runtime"]["live_runtime_facts"] = live_facts
    report["runtime"]["artifact_last_known_status"] = artifact_status
    report["runtime"]["fact_authority"] = summarize_runtime_fact_authority(
        live_facts=live_facts,
        artifact_status=artifact_status,
    )
    report["blocking_findings"] = collect_blocking_findings(report)
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a no-secret AATS runtime truth report.")
    parser.add_argument("--repo-root", default=str(REPO_ROOT), help="Repository root to inspect.")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE, help="Gateway base URL.")
    parser.add_argument("--wsl-distro", default=DEFAULT_WSL_DISTRO, help="WSL distribution name.")
    parser.add_argument("--wsl-project", default=DEFAULT_WSL_PROJECT, help="AATS path inside WSL.")
    parser.add_argument(
        "--gateway-container",
        default=DEFAULT_GATEWAY_CONTAINER,
        help="Gateway container name used for env-loaded DB probe.",
    )
    parser.add_argument(
        "--microstructure-container",
        default=DEFAULT_MICROSTRUCTURE_CONTAINER,
        help="Microstructure collector container used for daemon heartbeat checks.",
    )
    parser.add_argument(
        "--operator-read-api-key-env",
        default="AATS_RUNTIME_TRUTH_OPERATOR_READ_API_KEY",
        help=(
            "Optional env var containing an operator read API key for authenticated "
            "read-only probes. The value is never printed."
        ),
    )
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_report(args)
    text = json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=True)
    print(redact_secret_text(text))
    return 0


if __name__ == "__main__":
    sys.exit(main())
