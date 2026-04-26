#!/usr/bin/env python3
"""Read-only runtime truth report for the AATS live stack.

This script intentionally avoids importing application settings and never prints
connection strings or secrets. Database facts are read inside the running
gateway container using its existing environment, then only aggregate counts and
non-sensitive identifiers are returned.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import shlex
import ssl
import subprocess
import sys
from datetime import UTC, datetime
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
    ),
    "/ui/modules/no-trade-display.js": (
        "hasPreOrderFeasibility",
        "preOrderFeasibilitySummary",
        "执行可行性",
        "阻断维度",
    ),
}
ARTIFACT_STALE_AFTER_SECONDS = 1800
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

url = os.environ.get("AATS_DATABASE_URL") or os.environ.get("DATABASE_URL")
if not url:
    print(json.dumps({"ok": False, "reason": "database_url_not_available_in_container_env"}, sort_keys=True))
    raise SystemExit(0)

engine = create_engine(url)
with engine.connect() as conn:
    decisions = conn.execute(text("select count(*) from portfolio_allocation_decisions")).scalar()
    fills = conn.execute(text("select count(*) from execution_fills")).scalar()
    latest = conn.execute(text(
        "select allocation_id, decision_id, symbol, created_at, route_action, primary_family, "
        "portfolio_requested_notional, portfolio_approved_notional, portfolio_budget_cut_notional, "
        "expected_edge_bps, expected_cost_bps, payload "
        "from portfolio_allocation_decisions order by created_at desc limit 1"
    )).mappings().first()
    latest_audit = None
    decision_counts = {}
    if latest:
        decision_id = latest["decision_id"]
        latest_audit = conn.execute(text(
            "select decision_id, updated_at, execution_plan_ref, execution_plan_refs, "
            "order_intent_refs, order_state_refs, fill_event_refs, strategy_sleeve_intent_refs, "
            "portfolio_allocation_decision_ref, decision_outcome_ref, risk_decision_ref "
            "from decision_audit_records "
            "where decision_id = :decision_id order by audit_revision_id desc limit 1"
        ), {"decision_id": decision_id}).mappings().first()
        decision_counts = {
            "execution_orders": int(conn.execute(
                text("select count(*) from execution_orders where decision_id = :decision_id"),
                {"decision_id": decision_id},
            ).scalar()),
            "order_states": int(conn.execute(
                text("select count(*) from order_states where decision_id = :decision_id"),
                {"decision_id": decision_id},
            ).scalar()),
            "execution_fills": int(conn.execute(
                text("select count(*) from execution_fills where decision_id = :decision_id"),
                {"decision_id": decision_id},
            ).scalar()),
            "legacy_fill_events": int(conn.execute(
                text("select count(*) from fill_events where decision_id = :decision_id"),
                {"decision_id": decision_id},
            ).scalar()),
        }

print(json.dumps({
    "ok": True,
    "portfolio_allocation_decisions": int(decisions),
    "execution_fills": int(fills),
    "latest_decision": dict(latest) if latest else None,
    "latest_decision_audit": dict(latest_audit) if latest_audit else None,
    "latest_decision_counts": decision_counts,
}, default=str, sort_keys=True))
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


def first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
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
        summaries.append(
            {
                "leg": book_dict.get("leg"),
                "state": book_dict.get("state"),
                "book_action": book_dict.get("book_action"),
                "book_state": book_dict.get("book_state"),
                "score": decimal_text(book_dict.get("score")),
                "score_adjusted": decimal_text(book_dict.get("score_adjusted")),
                "effective_entry_threshold": decimal_text(
                    first_present(threshold.get("effective_entry_threshold"), threshold.get("entry_threshold")),
                ),
                "expected_signal_edge_bps": decimal_text(book_dict.get("expected_signal_edge_bps")),
                "expected_cost_bps": decimal_text(book_dict.get("expected_cost_bps")),
                "expected_net_edge_bps": decimal_text(book_dict.get("expected_net_edge_bps")),
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
                "execution": {
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
                },
                "permission": summarize_permission_trace(item_dict),
                "composition": summarize_composition_trace(item_dict),
                "budget": summarize_budget_trace(item_dict),
                "book_runtime_states": summarize_book_runtime_states(item_dict),
            },
        )
        if len(summaries) >= 4:
            break
    return summaries


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


def summarize_latest_decision(
    latest: dict[str, Any] | None,
    audit: dict[str, Any] | None,
    decision_counts: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(latest, dict):
        return None
    payload = as_dict(latest.get("payload"))
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
        "db_order_state_count": int(counts.get("order_states") or 0),
        "db_fill_count": int(counts.get("execution_fills") or 0),
        "legacy_fill_event_count": int(counts.get("legacy_fill_events") or 0),
    }
    sleeve_summaries = summarize_sleeve_intents(payload)
    reason_codes = compact_unique(
        collect_reason_codes(payload) + collect_sleeve_reason_codes(sleeve_summaries),
        limit=32,
    )
    classification = classify_no_trade(
        latest_decision=latest,
        execution_chain=execution_chain,
        reason_codes=reason_codes,
    )
    no_trade_attribution = {
        **classification,
        "reason_codes": reason_codes,
        "operator_summary": truncate_text(payload.get("operator_summary")),
        "execution_legs_count": len(as_list(payload.get("execution_legs"))),
        "sleeve_intent_summary": sleeve_summaries,
        "candidate_execution_drilldown": summarize_candidate_execution_drilldown(
            payload,
            primary_family=latest.get("primary_family"),
        ),
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
        "expected_edge_bps": decimal_text(latest.get("expected_edge_bps")),
        "expected_cost_bps": decimal_text(latest.get("expected_cost_bps")),
        "audit_refs": {
            "portfolio_allocation_decision_ref": audit_payload.get("portfolio_allocation_decision_ref"),
            "decision_outcome_ref_present": bool(audit_payload.get("decision_outcome_ref")),
            "risk_decision_ref_present": bool(audit_payload.get("risk_decision_ref")),
            "updated_at": audit_payload.get("updated_at"),
        },
        "execution_chain": execution_chain,
        "no_trade_attribution": no_trade_attribution,
    }


def sanitize_db_probe_payload(payload: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(payload)
    latest = summarize_latest_decision(
        sanitized.get("latest_decision"),
        sanitized.pop("latest_decision_audit", None),
        sanitized.pop("latest_decision_counts", None),
    )
    sanitized["latest_decision"] = latest
    return sanitized


def run_command(args: list[str], *, cwd: Path | None = None, timeout: int = 30) -> dict[str, Any]:
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


def fetch_url_text(url: str, *, timeout: int = 10) -> dict[str, Any]:
    context = ssl._create_unverified_context()
    request = Request(url, headers={"User-Agent": "aats-runtime-truth-report/1.0"})
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


def fetch_json_url(url: str, *, timeout: int = 10) -> dict[str, Any]:
    fetched = fetch_url_text(url, timeout=timeout)
    if not fetched["ok"]:
        return fetched | {"json": None}
    try:
        return fetched | {"json": json.loads(fetched["body"])}
    except json.JSONDecodeError as exc:
        return fetched | {"ok": False, "json": None, "error": f"invalid_json:{exc.msg}"}


def dashboard_bundle_probe(api_base: str) -> dict[str, Any]:
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
    response = fetch_json_url(f"{api_base.rstrip('/')}/dashboard/bundle?{query}", timeout=10)
    if not response["ok"] or response.get("json") is None:
        return {
            "status": "request_failed",
            "http_status": response.get("status"),
            "error": response.get("error") or "dashboard_bundle_unavailable",
        }
    return summarize_dashboard_bundle(response["json"])


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
        "effective_operating_mode": {
            "status": "verified" if effective_mode else "missing",
            "value": effective_mode,
        },
        "profile_auto_control_effective": {
            "status": "verified" if profile_effective is not None else "missing",
            "value": profile_effective,
        },
    }


def db_probe_command(distro: str, gateway_container: str) -> list[str]:
    encoded = base64.b64encode(DB_PROBE.encode("utf-8")).decode("ascii")
    return [
        "wsl",
        "-d",
        distro,
        "--",
        "docker",
        "exec",
        gateway_container,
        "python",
        "-c",
        f"import base64; exec(base64.b64decode('{encoded}'))",
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
    completed = run_command(db_probe_command(distro, gateway_container), timeout=45)
    if not completed["ok"]:
        return {
            "ok": False,
            "reason": "db_probe_command_failed",
            "returncode": completed["returncode"],
            "stderr": completed["stderr"],
        }
    return parse_db_probe(completed["stdout"], completed["stderr"])


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
    health = fetch_json_url(f"{api_base.rstrip('/')}/healthz", timeout=10)
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


def project_live_runtime_facts(report: dict[str, Any]) -> dict[str, Any]:
    db = report.get("database_truth") or {}
    dashboard = ((report.get("runtime") or {}).get("dashboard_bundle") or {})
    git = report.get("git") or {}
    health = report.get("deployment_health") or {}
    latest = db.get("latest_decision") if isinstance(db.get("latest_decision"), dict) else {}
    no_trade = latest.get("no_trade_attribution") if isinstance(latest.get("no_trade_attribution"), dict) else {}

    return {
        "source": "live_runtime",
        "authoritative": True,
        "may_be_overridden_by_artifact": False,
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
        "portfolio_allocation_decisions": db.get("portfolio_allocation_decisions") if db.get("ok") else None,
        "execution_fills": db.get("execution_fills") if db.get("ok") else None,
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
    report: dict[str, Any] = {
        "ok": True,
        "generated_at": generated_at,
        "scope": {
            "venue": "OKX",
            "symbol": "BTC-USDT-SWAP",
            "live_carrier": "independent",
            "shadow_benchmark": "none_verified",
        },
        "git": git_truth(repo_root, args.wsl_distro, args.wsl_project),
        "deployment_health": deployment_health(args.api_base, args.wsl_distro),
        "runtime": {
            "dashboard_bundle": dashboard_bundle_probe(args.api_base),
            "artifact_last_known": artifact_projection["facts"],
            "artifact_last_known_sources": artifact_projection["sources"],
            "artifact_last_known_status": artifact_projection["status"],
        },
        "database_truth": database_truth_probe(args.wsl_distro, args.gateway_container),
        "static_truth_surface": static_truth_surface(args.api_base),
    }
    report["runtime"]["ai_timeout_active_blocker"] = False
    runtime_mode = report["runtime"]["dashboard_bundle"].get("effective_operating_mode", {})
    if runtime_mode.get("value") not in (None, "baseline_only"):
        report["runtime"]["ai_timeout_active_blocker"] = "requires_provider_path_evidence"
    live_facts = project_live_runtime_facts(report)
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
