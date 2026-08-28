#!/usr/bin/env python3
"""Phase 3 Round Runner — 批量 attribution.

对 4 个 family × timeframe 组合批量运行 live attribution，
生成统一汇总产物和结论文档。

固定范围：
  symbol     = BTC-USDT-SWAP
  families   = independent, directional
  timeframes = 15m, 1H

Usage:
    python scripts/rdp_run_phase3_round.py \
        --start 2026-03-31 --end 2026-04-02

    python scripts/rdp_run_phase3_round.py \
        --start 2026-03-31 --end 2026-04-02 \
        --live-db-url "postgresql+psycopg://localhost:5432/aats_derivatives"

    python scripts/rdp_run_phase3_round.py \
        --start 2026-03-31 --end 2026-04-02 \
        --replay-only

Exit codes:
    0 = 全部成功
    2 = 部分成功
    3 = 全部失败
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import logging
import math
import os
import pathlib
import stat
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from aats.data_platform.governance._atomic_io import immutable_json_write
from aats.data_platform.governance._db_util import (
    has_explicit_governance_db_configuration,
)
from aats.data_platform.governance.parameter_candidate_lineage import (
    load_parameter_candidate_lineage,
)
from aats.data_platform.governance.parameter_identity import (
    parameter_values_fingerprint,
)
from aats.data_platform.governance.snapshot_db import (
    ROUND_PHASE_PHASE3,
    save_research_round_snapshot,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rdp_phase3_round")

_SYMBOL = "BTC-USDT-SWAP"

_COMBOS: list[dict[str, str]] = [
    {"key": "independent_15m", "family": "independent", "timeframe": "15m"},
    {"key": "independent_1h", "family": "independent", "timeframe": "1H"},
    {"key": "directional_15m", "family": "directional", "timeframe": "15m"},
    {"key": "directional_1h", "family": "directional", "timeframe": "1H"},
]

_DEFAULT_ARTIFACT_ROOT = pathlib.Path("artifacts/research/attribution_rounds")
_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
_CHILD_RESULT_SCHEMA = "aats.live_attribution_result.v1"
_CHILD_RESULT_MARKER = "RDP_LIVE_ATTRIBUTION_RESULT_JSON="
_ROUND_RESULT_SCHEMA = "aats.phase3_result.v1"
_ROUND_RESULT_MARKER = "RDP_PHASE3_RESULT_JSON="
_CHILD_OUTPUT_FILES = {
    "replay_live_alignment": "replay_live_alignment.csv",
    "attribution_summary": "attribution_summary.json",
    "top_failure_modes": "top_failure_modes.json",
    "replay_params_used": "replay_params_used.json",
    "live_attribution_report": "live_attribution_report.md",
}
_ATTRIBUTION_ALIGNMENT_FIELDS = (
    "family", "symbol", "timeframe",
    "replay_ts", "live_ts", "alignment_status",
    "lineage_error", "replay_opening", "live_opening",
    "final_attribution_category", "final_attribution_reason",
    "strategy_reason", "permission_reason", "allocator_reason",
    "budget_reason", "risk_reason", "execution_reason",
    "order_status", "fill_status", "replay_action", "replay_selectable",
    "replay_execution_compatible", "replay_blocking_reasons",
    "replay_expected_net_edge_bps", "live_state", "live_route_action",
    "live_automatic_enabled", "live_intent_id", "live_decision_id",
    "live_allocation_id", "live_parameter_set_id",
    "live_runtime_generation", "live_code_version",
    "live_market_snapshot_ref", "live_feature_snapshot_ref",
)
_ATTRIBUTION_ALIGNMENT_STATUSES = {
    "aligned", "replay_only", "live_only", "unattributable",
}
_ALIGNED_REQUIRED_LINEAGE_FIELDS = (
    "live_intent_id",
    "live_parameter_set_id",
    "live_runtime_generation",
    "live_code_version",
    "live_market_snapshot_ref",
    "live_feature_snapshot_ref",
)
_ATTRIBUTION_LAYER_FIELDS = (
    "strategy_reason",
    "permission_reason",
    "allocator_reason",
    "budget_reason",
    "risk_reason",
    "execution_reason",
    "order_status",
    "fill_status",
)
_ORDER_LIFECYCLE_STATES = {
    "CREATED",
    "SUBMITTING",
    "SUBMITTED",
    "PARTIALLY_FILLED",
    "FILLED",
    "CANCEL_PENDING",
    "CANCELED",
    "CANCELLED",
    "REJECTED",
    "FAILED",
    "BLOCKED",
    "DRY_RUN",
    "EXPIRED",
}
_NONFAILED_ORDER_STATES = {
    "CREATED",
    "SUBMITTING",
    "SUBMITTED",
    "PARTIALLY_FILLED",
    "FILLED",
    "CANCEL_PENDING",
}


# =========================================================================
# 子进程调用 one-shot attribution
# =========================================================================


def _reject_non_finite_json(value: str) -> None:
    raise ValueError(f"non_finite_json:{value}")


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate_json_key:{key}")
        result[key] = value
    return result


def _decode_json(payload: bytes, *, label: str) -> Any:
    if len(payload) > 4 * 1024 * 1024:
        raise ValueError(f"{label}_too_large")
    try:
        return json.loads(
            payload.decode("utf-8"),
            parse_constant=_reject_non_finite_json,
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label}_json_invalid") from exc


def _parse_explicit_timestamp(value: Any, *, label: str) -> datetime:
    if type(value) is not str or value != value.strip() or not value:
        raise ValueError(f"{label}_timestamp_invalid")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{label}_timestamp_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label}_timestamp_naive")
    return parsed.astimezone(timezone.utc)


def _requested_window_bounds(start: str, end: str) -> tuple[datetime, datetime]:
    try:
        start_value = datetime.fromisoformat(start)
        end_value = datetime.fromisoformat(end)
    except ValueError as exc:
        raise ValueError("requested_window_invalid") from exc
    start_utc = (
        start_value.replace(tzinfo=timezone.utc)
        if start_value.tzinfo is None
        else start_value.astimezone(timezone.utc)
    )
    end_utc = (
        end_value.replace(tzinfo=timezone.utc)
        if end_value.tzinfo is None
        else end_value.astimezone(timezone.utc)
    )
    if end_utc <= start_utc:
        raise ValueError("requested_window_invalid")
    return start_utc, end_utc


def _read_strict_csv(
    payload: bytes,
    *,
    expected_fields: tuple[str, ...],
    label: str,
) -> list[dict[str, str]]:
    if not payload:
        raise ValueError(f"{label}_empty")
    text_value = payload.decode("utf-8")
    reader = csv.DictReader(io.StringIO(text_value), strict=True)
    if tuple(reader.fieldnames or ()) != expected_fields:
        raise ValueError(f"{label}_header_invalid")
    rows = list(reader)
    if any(None in row for row in rows):
        raise ValueError(f"{label}_column_count_invalid")
    return rows


def _parse_csv_bool(
    row: dict[str, str],
    field: str,
    *,
    allow_blank: bool = False,
) -> bool | None:
    value = row.get(field)
    if value == "True":
        return True
    if value == "False":
        return False
    if allow_blank and value in {None, ""}:
        return None
    raise ValueError(f"attribution_{field}_boolean_invalid")


def _require_empty_fields(
    row: dict[str, str],
    fields: tuple[str, ...],
    *,
    label: str,
) -> None:
    if any(row.get(field) not in {None, ""} for field in fields):
        raise ValueError(f"attribution_{label}_waterfall_invalid")


def _require_layer_values(
    row: dict[str, str],
    expected: dict[str, str],
    *,
    label: str,
) -> None:
    for field in _ATTRIBUTION_LAYER_FIELDS:
        actual = row.get(field) or ""
        if actual != expected.get(field, ""):
            raise ValueError(f"attribution_{label}_waterfall_invalid")


def _first_replay_blocking_reason(row: dict[str, str]) -> str | None:
    raw = row.get("replay_blocking_reasons") or ""
    if not raw:
        return None
    reasons = raw.split("|")
    if any(not reason or reason != reason.strip() for reason in reasons):
        raise ValueError("attribution_replay_blocking_reasons_invalid")
    return reasons[0]


def _validate_attribution_waterfall(
    row: dict[str, str],
    *,
    status: str,
    category: str,
    reason: str,
    replay_opening: bool | None,
    replay_selectable: bool | None,
    live_opening: bool,
    live_automatic_enabled: bool | None,
) -> None:
    """Re-derive the classifier's first-failing-layer contract from CSV facts."""

    live_route = row.get("live_route_action") or ""
    first_blocking_reason = _first_replay_blocking_reason(row)

    if category == "not_applicable":
        if status == "live_only":
            expected_reason = "live_only_no_replay_bar"
            expected_layers: dict[str, str] = {}
        elif status == "unattributable":
            expected_reason = row.get("lineage_error") or ""
            expected_layers = {}
        else:
            if replay_selectable is not False or replay_opening is not False:
                raise ValueError("attribution_not_applicable_replay_state_invalid")
            expected_reason = "replay_not_selectable"
            expected_layers = (
                {"strategy_reason": first_blocking_reason}
                if first_blocking_reason is not None
                else {}
            )
        if reason != expected_reason:
            raise ValueError("attribution_not_applicable_reason_invalid")
        _require_layer_values(row, expected_layers, label="not_applicable")
        return

    if status in {"live_only", "unattributable"}:
        raise ValueError("attribution_live_without_replay_category_invalid")

    if category == "strategy_blocked":
        if replay_selectable is True and replay_opening is False:
            expected_reason = first_blocking_reason or "score_not_stable"
        elif status == "replay_only" and replay_opening is True:
            expected_reason = "no_intent_in_window"
        elif status == "aligned" and replay_opening is True:
            expected_reason = f"intent_route_action_{live_route or 'missing'}"
            if live_route == "override_target" or live_opening:
                raise ValueError("attribution_strategy_route_state_invalid")
        else:
            raise ValueError("attribution_strategy_state_invalid")
        if reason != expected_reason:
            raise ValueError("attribution_strategy_reason_invalid")
        _require_layer_values(
            row,
            {"strategy_reason": expected_reason},
            label="strategy",
        )
        return

    if status != "aligned" or replay_opening is not True:
        raise ValueError("attribution_downstream_alignment_invalid")
    if live_route != "override_target" or live_opening is not True:
        raise ValueError("attribution_downstream_route_invalid")

    if category == "permission_disabled":
        expected_reason = (
            "automatic_enabled_false"
            if live_automatic_enabled is False
            else "automatic_enabled_missing"
            if live_automatic_enabled is None
            else ""
        )
        if not expected_reason or reason != expected_reason:
            raise ValueError("attribution_permission_reason_invalid")
        _require_layer_values(
            row,
            {
                "strategy_reason": "passed",
                "permission_reason": expected_reason,
            },
            label="permission",
        )
        return

    if live_automatic_enabled is not True:
        raise ValueError("attribution_automatic_permission_invalid")

    layer_contracts = {
        "allocator_rejected": (
            "allocator_reason",
            ("strategy_reason", "permission_reason"),
        ),
        "budget_rejected": (
            "budget_reason",
            ("strategy_reason", "permission_reason", "allocator_reason"),
        ),
        "risk_rejected": (
            "risk_reason",
            (
                "strategy_reason",
                "permission_reason",
                "allocator_reason",
                "budget_reason",
            ),
        ),
        "execution_blocked": (
            "execution_reason",
            (
                "strategy_reason",
                "permission_reason",
                "allocator_reason",
                "budget_reason",
                "risk_reason",
            ),
        ),
    }
    if category in layer_contracts:
        failing_field, passed_fields = layer_contracts[category]
        expected = {field: "passed" for field in passed_fields}
        expected[failing_field] = reason
        _require_layer_values(row, expected, label=category)
        if category != "allocator_rejected" and not row.get("live_allocation_id"):
            raise ValueError("attribution_allocation_lineage_missing")
        return

    passed_layers = {
        "strategy_reason": "passed",
        "permission_reason": "passed",
        "allocator_reason": "passed",
        "budget_reason": "passed",
        "risk_reason": "passed",
        "execution_reason": "passed",
    }
    if not row.get("live_allocation_id") or not row.get("live_decision_id"):
        raise ValueError("attribution_execution_lineage_missing")

    if category == "order_not_created":
        expected_order_status = (
            "not_found"
            if reason == "no_order_found"
            else reason.removeprefix("order_state_").upper()
        )
        if (
            expected_order_status != "not_found"
            and expected_order_status not in _ORDER_LIFECYCLE_STATES
        ):
            raise ValueError("attribution_order_state_invalid")
        _require_layer_values(
            row,
            {**passed_layers, "order_status": expected_order_status},
            label="order",
        )
        return

    order_status = row.get("order_status") or ""
    if order_status not in _NONFAILED_ORDER_STATES:
        raise ValueError("attribution_success_order_state_invalid")
    if category == "fill_not_observed":
        expected_fill_status = {
            "no_fill_found": "not_found",
            "partial_fill_only": "partial",
        }.get(reason)
        if expected_fill_status is None:
            raise ValueError("attribution_fill_reason_invalid")
        _require_layer_values(
            row,
            {
                **passed_layers,
                "order_status": order_status,
                "fill_status": expected_fill_status,
            },
            label="fill",
        )
        return

    if category == "live_traded" and reason == "all_layers_passed":
        _require_layer_values(
            row,
            {
                **passed_layers,
                "order_status": order_status,
                "fill_status": "filled",
            },
            label="success",
        )
        return

    raise ValueError("attribution_category_unreachable")


def _validate_attribution_business_artifacts(
    *,
    output_bytes: dict[str, bytes],
    family: str,
    symbol: str,
    timeframe: str,
    start: str,
    end: str,
    replay_only: bool,
) -> tuple[
    list[dict[str, Any]],
    dict[str, Any],
    list[dict[str, str]],
    dict[str, int],
]:
    summary = _decode_json(
        output_bytes["attribution_summary"],
        label="attribution_summary",
    )
    if type(summary) is not list or any(type(row) is not dict for row in summary):
        raise ValueError("attribution_summary_contract_invalid")
    top_failure_modes = _decode_json(
        output_bytes["top_failure_modes"],
        label="top_failure_modes",
    )
    if type(top_failure_modes) is not dict:
        raise ValueError("top_failure_modes_contract_invalid")
    rows = _read_strict_csv(
        output_bytes["replay_live_alignment"],
        expected_fields=_ATTRIBUTION_ALIGNMENT_FIELDS,
        label="replay_live_alignment",
    )
    start_utc, end_utc = _requested_window_bounds(start, end)
    from aats.data_platform.attribution.taxonomy import REASON_CODES

    allowed_categories = {
        "not_applicable",
        "strategy_blocked",
        "permission_disabled",
        "allocator_rejected",
        "budget_rejected",
        "risk_rejected",
        "execution_blocked",
        "order_not_created",
        "fill_not_observed",
        "live_traded",
    }
    aligned_identities: set[tuple[datetime, str]] = set()
    replay_only_identities: set[datetime] = set()
    live_intent_ids: set[str] = set()
    replay_alignment_statuses: dict[datetime, set[str]] = {}
    alignment_stats = {
        "total": 0,
        "aligned": 0,
        "replay_only": 0,
        "live_only": 0,
        "unattributable": 0,
    }
    for row in rows:
        if (
            row.get("family") != family
            or row.get("symbol") != symbol
            or row.get("timeframe") != timeframe
        ):
            raise ValueError("attribution_alignment_scope_mismatch")
        status = row.get("alignment_status")
        if status not in _ATTRIBUTION_ALIGNMENT_STATUSES:
            raise ValueError("attribution_alignment_status_invalid")
        if replay_only and status != "replay_only":
            raise ValueError("attribution_replay_only_mode_status_invalid")
        category = row.get("final_attribution_category")
        reason = row.get("final_attribution_reason")
        if category not in allowed_categories or not reason:
            raise ValueError("attribution_taxonomy_invalid")
        if category in REASON_CODES and reason not in REASON_CODES[category]:
            raise ValueError("attribution_reason_code_invalid")

        replay_timestamp = row.get("replay_ts") or ""
        live_timestamp = row.get("live_ts") or ""
        replay_utc: datetime | None = None
        live_utc: datetime | None = None
        if replay_timestamp:
            replay_utc = _parse_explicit_timestamp(
                replay_timestamp,
                label="attribution_replay",
            )
            if replay_timestamp != replay_utc.isoformat():
                raise ValueError("attribution_replay_timestamp_not_canonical")
            if not start_utc <= replay_utc < end_utc:
                raise ValueError("attribution_replay_outside_window")
        if live_timestamp:
            live_utc = _parse_explicit_timestamp(
                live_timestamp,
                label="attribution_live",
            )
            if live_timestamp != live_utc.isoformat():
                raise ValueError("attribution_live_timestamp_not_canonical")
            if not start_utc <= live_utc < end_utc:
                raise ValueError("attribution_live_outside_window")

        live_intent_id = row.get("live_intent_id") or ""
        if live_intent_id:
            if live_intent_id in live_intent_ids:
                raise ValueError("attribution_live_intent_duplicate_identity")
            live_intent_ids.add(live_intent_id)
        if replay_utc is not None:
            replay_alignment_statuses.setdefault(replay_utc, set()).add(str(status))
            if replay_alignment_statuses[replay_utc] == {"aligned", "replay_only"}:
                raise ValueError("attribution_replay_alignment_conflict")
        if status == "aligned" and replay_utc is not None:
            aligned_identity = (replay_utc, live_intent_id)
            if aligned_identity in aligned_identities:
                raise ValueError("attribution_alignment_duplicate_identity")
            aligned_identities.add(aligned_identity)
        if status == "replay_only" and replay_utc is not None:
            if replay_utc in replay_only_identities:
                raise ValueError("attribution_replay_duplicate_identity")
            replay_only_identities.add(replay_utc)

        if status == "aligned":
            if (
                not replay_timestamp
                or not live_timestamp
                or row.get("lineage_error") not in {None, ""}
                or any(not row.get(field) for field in _ALIGNED_REQUIRED_LINEAGE_FIELDS)
            ):
                raise ValueError("attribution_aligned_lineage_invalid")
        elif status == "replay_only":
            if not replay_timestamp or live_timestamp or live_intent_id:
                raise ValueError("attribution_replay_only_identity_invalid")
            if row.get("lineage_error") not in {None, ""}:
                raise ValueError("attribution_replay_only_lineage_invalid")
        elif status in {"live_only", "unattributable"}:
            if replay_timestamp or not live_timestamp or not live_intent_id:
                raise ValueError("attribution_live_only_identity_invalid")
            if status == "live_only" and (
                row.get("lineage_error") not in {None, ""}
                or any(not row.get(field) for field in _ALIGNED_REQUIRED_LINEAGE_FIELDS)
            ):
                raise ValueError("attribution_live_only_lineage_invalid")
        if status == "unattributable" and not str(
            row.get("lineage_error") or ""
        ).startswith("live_lineage_"):
            raise ValueError("attribution_unattributable_reason_invalid")

        replay_fields = (
            "replay_action",
            "replay_selectable",
            "replay_opening",
            "replay_execution_compatible",
            "replay_blocking_reasons",
            "replay_expected_net_edge_bps",
        )
        if replay_utc is None:
            _require_empty_fields(row, replay_fields, label="missing_replay")
            replay_opening = None
            replay_selectable = None
        else:
            replay_action = row.get("replay_action") or ""
            if replay_action not in {"hold", "open", "close"}:
                raise ValueError("attribution_replay_action_invalid")
            replay_opening = _parse_csv_bool(row, "replay_opening")
            replay_selectable = _parse_csv_bool(row, "replay_selectable")
            _parse_csv_bool(row, "replay_execution_compatible")
            if replay_opening is not (replay_action == "open"):
                raise ValueError("attribution_replay_opening_action_mismatch")
            replay_edge = row.get("replay_expected_net_edge_bps")
            if replay_edge in {None, ""}:
                raise ValueError("attribution_replay_edge_missing")
            try:
                parsed_edge = float(replay_edge)
            except (TypeError, ValueError) as exc:
                raise ValueError("attribution_replay_edge_invalid") from exc
            if not math.isfinite(parsed_edge):
                raise ValueError("attribution_replay_edge_non_finite")

        live_opening = _parse_csv_bool(row, "live_opening")
        live_automatic_enabled = _parse_csv_bool(
            row,
            "live_automatic_enabled",
            allow_blank=True,
        )
        if live_utc is None:
            _require_empty_fields(
                row,
                (
                    "live_state",
                    "live_route_action",
                    "live_automatic_enabled",
                    "live_decision_id",
                    "live_allocation_id",
                    "live_parameter_set_id",
                    "live_runtime_generation",
                    "live_code_version",
                    "live_market_snapshot_ref",
                    "live_feature_snapshot_ref",
                ),
                label="missing_live",
            )
            if live_opening is not False:
                raise ValueError("attribution_missing_live_opening_invalid")
        elif live_opening is not (
            (row.get("live_route_action") or "") == "override_target"
        ):
            raise ValueError("attribution_live_opening_route_mismatch")

        first_blocking_reason = _first_replay_blocking_reason(row)
        if first_blocking_reason is not None and first_blocking_reason not in REASON_CODES[
            "strategy_blocked"
        ]:
            raise ValueError("attribution_replay_blocking_reason_invalid")
        _validate_attribution_waterfall(
            row,
            status=str(status),
            category=str(category),
            reason=str(reason),
            replay_opening=replay_opening,
            replay_selectable=replay_selectable,
            live_opening=bool(live_opening),
            live_automatic_enabled=live_automatic_enabled,
        )

        alignment_stats["total"] += 1
        alignment_stats[str(status)] += 1

    from aats.data_platform.attribution.aggregation import (
        build_attribution_summary,
        build_top_failure_modes,
    )

    recomputed_summary = build_attribution_summary(
        rows,
        family=family,
        timeframe=timeframe,
    )
    recomputed_failure_modes = build_top_failure_modes(rows)
    if summary != recomputed_summary:
        raise ValueError("attribution_summary_detail_mismatch")
    if top_failure_modes != recomputed_failure_modes:
        raise ValueError("top_failure_modes_detail_mismatch")
    return summary, top_failure_modes, rows, alignment_stats


def _read_bound_file(
    path: pathlib.Path,
    *,
    trusted_root: pathlib.Path,
    label: str,
) -> bytes:
    if not path.is_absolute():
        raise ValueError(f"{label}_path_not_absolute")
    root = trusted_root.resolve(strict=True)
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label}_path_outside_combo") from exc
    if not relative.parts or any(part in {".", ".."} for part in relative.parts):
        raise ValueError(f"{label}_path_invalid")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"{label}_symlink_invalid")
    if path.resolve(strict=True) != path:
        raise ValueError(f"{label}_path_not_canonical")
    path_stat = path.lstat()
    if not stat.S_ISREG(path_stat.st_mode):
        raise ValueError(f"{label}_not_regular_file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"{label}_not_regular_file")
        if (path_stat.st_dev, path_stat.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError(f"{label}_changed_before_read")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    after_path = path.lstat()
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
        raise ValueError(f"{label}_changed_during_read")
    if any(getattr(after, field) != getattr(after_path, field) for field in stable_fields):
        raise ValueError(f"{label}_path_replaced_during_read")
    payload = b"".join(chunks)
    if len(payload) != after.st_size:
        raise ValueError(f"{label}_size_changed_during_read")
    return payload


def _prepare_combo_root(
    per_combo_root: pathlib.Path,
    *,
    combo_key: str,
) -> pathlib.Path:
    if per_combo_root.is_symlink():
        raise ValueError("phase3_per_combo_root_symlink_invalid")
    per_combo_root.mkdir(parents=True, exist_ok=True)
    trusted_root = per_combo_root.resolve(strict=True)
    combo_root = trusted_root / combo_key
    if combo_root.is_symlink():
        raise ValueError("phase3_combo_root_symlink_invalid")
    combo_root.mkdir(parents=False, exist_ok=True)
    resolved_combo_root = combo_root.resolve(strict=True)
    if resolved_combo_root.parent != trusted_root:
        raise ValueError("phase3_combo_root_outside_round")
    return resolved_combo_root


def _load_verified_child_result(
    *,
    result_path: pathlib.Path,
    stdout: bytes | str,
    returncode: int,
    combo_root: pathlib.Path,
    family: str,
    symbol: str,
    timeframe: str,
    dataset_version: str,
    start: str,
    end: str,
    replay_only: bool,
    expected_parameter_fingerprint: str | None,
) -> tuple[dict[str, Any], dict[str, bytes], dict[str, Any]]:
    expected_status = {0: "succeeded", 2: "partial_success"}.get(returncode)
    if expected_status is None:
        raise ValueError(f"child_exit_code_unexpected:{returncode}")

    sidecar_bytes = _read_bound_file(
        result_path,
        trusted_root=combo_root,
        label="result_sidecar",
    )
    result = _decode_json(sidecar_bytes, label="result_sidecar")
    if type(result) is not dict:
        raise ValueError("result_sidecar_object_required")

    stdout_text = (
        stdout.decode("utf-8", errors="strict")
        if isinstance(stdout, bytes)
        else stdout
    )
    marker_payloads = [
        line[len(_CHILD_RESULT_MARKER):]
        for line in stdout_text.splitlines()
        if line.startswith(_CHILD_RESULT_MARKER)
    ]
    if len(marker_payloads) != 1:
        raise ValueError("result_marker_count_invalid")
    marker_result = _decode_json(
        marker_payloads[0].encode("utf-8"),
        label="result_marker",
    )
    if marker_result != result:
        raise ValueError("result_marker_sidecar_mismatch")

    expected_keys = {
        "schema_version", "status", "exit_code", "run_id", "run_dir",
        "family", "symbol", "timeframe", "dataset_version", "window",
        "replay_only", "resolved_parameter_values_fingerprint", "finished_at",
        "outputs",
    }
    if set(result) != expected_keys:
        raise ValueError("result_sidecar_schema_keys_invalid")
    if result.get("schema_version") != _CHILD_RESULT_SCHEMA:
        raise ValueError("result_sidecar_schema_version_invalid")
    if result.get("status") != expected_status:
        raise ValueError("result_status_exit_code_mismatch")
    if type(result.get("exit_code")) is not int or result["exit_code"] != returncode:
        raise ValueError("result_exit_code_mismatch")
    if type(result.get("replay_only")) is not bool:
        raise ValueError("result_replay_only_type_invalid")
    expected_scope = {
        "family": family,
        "symbol": symbol,
        "timeframe": timeframe,
        "dataset_version": dataset_version,
        "window": {"start": start, "end": end},
        "replay_only": replay_only,
    }
    if any(result.get(key) != value for key, value in expected_scope.items()):
        raise ValueError("result_scope_mismatch")
    fingerprint = result.get("resolved_parameter_values_fingerprint")
    if (
        type(fingerprint) is not str
        or len(fingerprint) != 64
        or any(char not in "0123456789abcdef" for char in fingerprint)
    ):
        raise ValueError("result_parameter_fingerprint_invalid")
    if expected_parameter_fingerprint is not None and fingerprint != expected_parameter_fingerprint:
        raise ValueError("result_parameter_fingerprint_mismatch")
    finished_at = result.get("finished_at")
    if type(finished_at) is not str:
        raise ValueError("result_finished_at_invalid")
    try:
        parsed_finished_at = datetime.fromisoformat(
            finished_at[:-1] + "+00:00" if finished_at.endswith("Z") else finished_at
        )
    except ValueError as exc:
        raise ValueError("result_finished_at_invalid") from exc
    if (
        parsed_finished_at.tzinfo is None
        or parsed_finished_at.utcoffset() is None
        or parsed_finished_at.utcoffset() != timezone.utc.utcoffset(parsed_finished_at)
    ):
        raise ValueError("result_finished_at_not_utc")

    run_id = result.get("run_id")
    run_dir_value = result.get("run_dir")
    if type(run_id) is not str or not run_id or type(run_dir_value) is not str:
        raise ValueError("result_run_identity_invalid")
    run_dir = pathlib.Path(run_dir_value)
    if run_dir.parent != combo_root or run_dir.name != run_id:
        raise ValueError("result_run_dir_scope_mismatch")
    _read_bound_file(
        run_dir / _CHILD_OUTPUT_FILES["replay_params_used"],
        trusted_root=combo_root,
        label="run_dir_probe",
    )

    outputs = result.get("outputs")
    if type(outputs) is not dict or set(outputs) != set(_CHILD_OUTPUT_FILES):
        raise ValueError("result_outputs_contract_invalid")
    verified_outputs: dict[str, bytes] = {}
    for key, filename in _CHILD_OUTPUT_FILES.items():
        evidence = outputs.get(key)
        if type(evidence) is not dict or set(evidence) != {
            "path", "sha256", "size_bytes",
        }:
            raise ValueError(f"result_output_evidence_invalid:{key}")
        expected_path = run_dir / filename
        if evidence.get("path") != str(expected_path):
            raise ValueError(f"result_output_path_mismatch:{key}")
        output_bytes = _read_bound_file(
            expected_path,
            trusted_root=combo_root,
            label=f"result_output_{key}",
        )
        digest = hashlib.sha256(output_bytes).hexdigest()
        if evidence.get("sha256") != digest:
            raise ValueError(f"result_output_digest_mismatch:{key}")
        if type(evidence.get("size_bytes")) is not int or evidence["size_bytes"] != len(
            output_bytes
        ):
            raise ValueError(f"result_output_size_mismatch:{key}")
        verified_outputs[key] = output_bytes
    return result, verified_outputs, {
        "path": str(result_path),
        "sha256": hashlib.sha256(sidecar_bytes).hexdigest(),
        "size_bytes": len(sidecar_bytes),
    }


def _run_single_attribution(
    family: str,
    timeframe: str,
    *,
    symbol: str,
    start: str,
    end: str,
    artifact_root: pathlib.Path,
    live_db_url: str | None,
    replay_only: bool,
    ensure_schema: bool,
    dataset_version: str,
    params_json: str | None = None,
    parameter_lineage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """通过子进程调用 rdp_run_live_attribution.py。"""
    combo_key = f"{family}_{timeframe.lower()}"
    try:
        combo_root = _prepare_combo_root(artifact_root, combo_key=combo_key)
    except (OSError, ValueError) as exc:
        return {
            "family": family,
            "timeframe": timeframe,
            "status": "failed",
            "run_dir": None,
            "attribution_summary": None,
            "top_failure_modes": None,
            "error": f"phase3_combo_root_invalid:{exc}",
        }
    result_path = combo_root / f"result_{uuid4().hex}.json"

    cmd = [
        sys.executable, "scripts/rdp_run_live_attribution.py",
        "--family", family,
        "--symbol", symbol,
        "--timeframe", timeframe,
        "--start", start,
        "--end", end,
        "--dataset-version", dataset_version,
        "--artifact-root", str(combo_root),
        "--result-json", str(result_path),
    ]
    if replay_only:
        cmd.append("--replay-only")
    if ensure_schema:
        cmd.append("--ensure-schema")
    # P0: 参数闭环 — 传递 Phase 2 推荐参数
    if params_json:
        ft_key = f"{family}_{timeframe.lower()}"
        cmd.extend(["--params-json", params_json, "--parameter-set", ft_key])

    log.info("  CMD: %s", " ".join(cmd))
    child_env = os.environ.copy()
    if live_db_url:
        child_env["RDP_LIVE_DATABASE_URL"] = live_db_url
    proc = subprocess.run(cmd, capture_output=True, env=child_env)

    # 始终记录 stderr 以便调试
    if proc.stderr:
        stderr_text = proc.stderr.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0 and stderr_text:
            log.error("  subprocess stderr (last 1000 chars):\n%s", stderr_text[-1000:])
        elif stderr_text:
            log.debug("  subprocess stderr (last 500 chars):\n%s", stderr_text[-500:])

    expected_combo_lineage = (
        (parameter_lineage or {}).get("combos", {}).get(combo_key)
    )
    expected_parameter_fingerprint = (
        expected_combo_lineage.get("resolved_parameter_values_fingerprint")
        if isinstance(expected_combo_lineage, dict)
        else None
    )
    try:
        child_result, output_bytes, child_result_ref = _load_verified_child_result(
            result_path=result_path,
            stdout=proc.stdout or b"",
            returncode=proc.returncode,
            combo_root=combo_root,
            family=family,
            symbol=symbol,
            timeframe=timeframe,
            dataset_version=dataset_version,
            start=start,
            end=end,
            replay_only=replay_only,
            expected_parameter_fingerprint=expected_parameter_fingerprint,
        )
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "family": family,
            "timeframe": timeframe,
            "status": "failed",
            "run_dir": None,
            "attribution_summary": None,
            "top_failure_modes": None,
            "error": f"phase3_child_result_invalid:{exc}",
        }

    run_dir = pathlib.Path(child_result["run_dir"])
    try:
        used_parameters = _decode_json(
            output_bytes["replay_params_used"],
            label="replay_params_used",
        )
        used_parameters_fingerprint = parameter_values_fingerprint(used_parameters)
    except ValueError as exc:
        return {
            "family": family,
            "timeframe": timeframe,
            "status": "failed",
            "run_dir": str(run_dir),
            "attribution_summary": None,
            "top_failure_modes": None,
            "error": f"phase3_parameter_evidence_invalid:{type(exc).__name__}",
        }
    if used_parameters_fingerprint != child_result["resolved_parameter_values_fingerprint"]:
        return {
            "family": family,
            "timeframe": timeframe,
            "status": "failed",
            "run_dir": str(run_dir),
            "attribution_summary": None,
            "top_failure_modes": None,
            "error": "phase3_sidecar_parameter_values_fingerprint_mismatch",
        }

    try:
        summary, tfm, alignment_rows, alignment_stats = (
            _validate_attribution_business_artifacts(
                output_bytes=output_bytes,
                family=family,
                symbol=symbol,
                timeframe=timeframe,
                start=start,
                end=end,
                replay_only=replay_only,
            )
        )
    except (UnicodeDecodeError, csv.Error, ValueError) as exc:
        return {
            "family": family,
            "timeframe": timeframe,
            "status": "failed",
            "run_dir": str(run_dir),
            "attribution_summary": None,
            "top_failure_modes": None,
            "error": f"phase3_child_artifact_invalid:{exc}",
        }

    return {
        "family": family,
        "timeframe": timeframe,
        # P1b: 保留 partial_success 语义（exit=2 表示 replay 正常但 live 失败）
        "status": child_result["status"],
        "run_dir": str(run_dir),
        "child_result_ref": child_result_ref,
        "attribution_summary": summary,
        "top_failure_modes": tfm,
        "alignment_stats": alignment_stats,
        "_alignment_rows": alignment_rows,
        "live_query_succeeded": proc.returncode == 0 and not replay_only,
        "parameter_values_fingerprint": (
            expected_combo_lineage.get("parameter_values_fingerprint")
            if isinstance(expected_combo_lineage, dict)
            else None
        ),
        "resolved_parameter_values_fingerprint": used_parameters_fingerprint,
        "source_step3_round_id": (parameter_lineage or {}).get(
            "source_step3_round_id"
        ),
        "source_step3_candidate_sha256": (parameter_lineage or {}).get(
            "source_step3_candidate_sha256"
        ),
        "error": None,
    }


# =========================================================================
# 聚合
# =========================================================================


def _aggregate_summaries(
    results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """汇总所有 family/tf 的 attribution summary。"""
    all_rows: list[dict[str, Any]] = []
    for r in results:
        summary = r.get("attribution_summary")
        if not summary:
            continue
        if isinstance(summary, list):
            all_rows.extend(summary)
        elif isinstance(summary, dict) and "experiments" in summary:
            all_rows.extend(summary["experiments"])
    return all_rows


def _round_outcome_status(results: list[dict[str, Any]]) -> str:
    statuses = [result.get("status") for result in results]
    if statuses and all(status == "succeeded" for status in statuses):
        return "succeeded"
    if statuses and all(status == "failed" for status in statuses):
        return "failed"
    return "partial_success"


def _round_exit_code(status: str) -> int:
    return {"succeeded": 0, "partial_success": 2, "failed": 3}[status]


def _publish_round_result(
    *,
    round_dir: pathlib.Path,
    round_id: str,
    status: str,
    manifest_path: pathlib.Path,
    manifest_sha256: str,
    dataset_version: str,
    start: str,
    end: str,
    replay_only: bool,
    parameter_lineage: dict[str, Any],
) -> dict[str, Any]:
    manifest_size_bytes = manifest_path.stat().st_size
    payload = {
        "schema_version": _ROUND_RESULT_SCHEMA,
        "phase": "phase3",
        "round_id": round_id,
        "round_dir": str(round_dir.resolve(strict=True)),
        "manifest_path": str(manifest_path.resolve(strict=True)),
        "manifest_sha256": manifest_sha256,
        "manifest_size_bytes": manifest_size_bytes,
        "status": status,
        "exit_code": _round_exit_code(status),
        "symbol": _SYMBOL,
        "dataset_version": dataset_version,
        "window": {"start": start, "end": end},
        "replay_only": replay_only,
        "source_step3_round_id": parameter_lineage.get("source_step3_round_id"),
        "source_step3_candidate_sha256": parameter_lineage.get(
            "source_step3_candidate_sha256"
        ),
    }
    immutable_json_write(payload, round_dir / "round_result.json")
    print(
        _ROUND_RESULT_MARKER
        + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        flush=True,
    )
    return payload


# =========================================================================
# 主流程
# =========================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Phase 3 Round Runner: 批量 live attribution 归因",
    )
    parser.add_argument("--start", required=True, help="YYYY-MM-DD (UTC)")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD (UTC)")
    parser.add_argument("--dataset-version", default="v1.0")
    parser.add_argument(
        "--live-db-url", default=None,
        help="Live AATS database URL (default: env RDP_LIVE_DATABASE_URL)",
    )
    parser.add_argument("--replay-only", action="store_true")
    parser.add_argument(
        "--artifact-root", type=str, default=str(_DEFAULT_ARTIFACT_ROOT),
    )
    parser.add_argument(
        "--ensure-schema",
        action="store_true",
        help="Legacy name: validate schema before the first run; does not run DDL",
    )
    parser.add_argument("--no-print-summary", action="store_true")
    # P0: 参数闭环 — 支持从 Phase 2 parameter_candidates.json 注入参数
    parser.add_argument(
        "--params-json", default=None,
        help="Phase 2 parameter_candidates.json 路径，自动按 family_tf 分发参数",
    )
    args = parser.parse_args()

    try:
        parameter_lineage = load_parameter_candidate_lineage(args.params_json)
    except ValueError as exc:
        log.error("Phase 3 parameter lineage validation failed: %s", exc)
        return 2
    if parameter_lineage.get("status") == "bound" and (
        parameter_lineage.get("symbol") != _SYMBOL
        or parameter_lineage.get("dataset_version") != args.dataset_version
        or parameter_lineage.get("window")
        != {"start": args.start, "end": args.end}
    ):
        log.error("Phase 3 parameter lineage scope mismatch")
        return 2

    live_db_url = args.live_db_url or os.environ.get("RDP_LIVE_DATABASE_URL")
    if not live_db_url and not args.replay_only:
        log.error(
            "Phase 3 live attribution requires --live-db-url or "
            "RDP_LIVE_DATABASE_URL. Use --replay-only explicitly for replay-only analysis."
        )
        return 2
    replay_only = args.replay_only

    started_at = datetime.now(timezone.utc).isoformat()
    round_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:8]
    artifact_root = pathlib.Path(args.artifact_root).resolve()
    round_dir = artifact_root / round_id
    per_combo_root = round_dir / "per_combo"

    log.info("=" * 60)
    log.info("Phase 3 Round Runner")
    log.info("  Round ID    : %s", round_id)
    log.info("  Symbol      : %s", _SYMBOL)
    log.info("  Window      : %s ~ %s", args.start, args.end)
    log.info("  Replay-only : %s", replay_only)
    log.info("  Params JSON : %s", args.params_json or "(default)")
    log.info("  Combos      : %d", len(_COMBOS))
    log.info("  Output      : %s", round_dir)
    log.info("=" * 60)

    # ---- 运行 4 个 family/tf 组合 ----
    results: list[dict[str, Any]] = []

    for i, combo in enumerate(_COMBOS):
        log.info("")
        log.info("[%d/%d] %s / %s",
                 i + 1, len(_COMBOS), combo["family"], combo["timeframe"])

        result = _run_single_attribution(
            combo["family"], combo["timeframe"],
            symbol=_SYMBOL,
            start=args.start,
            end=args.end,
            artifact_root=per_combo_root,
            live_db_url=live_db_url,
            replay_only=replay_only,
            ensure_schema=args.ensure_schema and (i == 0),
            dataset_version=args.dataset_version,
            params_json=args.params_json,
            parameter_lineage=parameter_lineage,
        )
        result["key"] = combo["key"]
        results.append(result)

        if result["status"] == "succeeded":
            log.info("  -> SUCCEEDED: %s", result.get("run_dir"))
        elif result["status"] == "partial_success":
            log.warning("  -> PARTIAL: %s", result.get("run_dir"))
        else:
            log.error("  -> FAILED: %s", (result.get("error") or "")[:200])

    # ---- 聚合 ----
    log.info("")
    log.info("Aggregating results...")

    # 汇总 attribution summary
    all_summaries: dict[str, list[dict[str, Any]]] = {}
    all_failure_modes: dict[str, dict[str, Any]] = {}
    all_alignment_stats: dict[str, dict[str, int]] = {}

    for r in results:
        ft_key = r["key"]
        if r.get("attribution_summary"):
            all_summaries[ft_key] = r["attribution_summary"]
        if r.get("top_failure_modes"):
            all_failure_modes[ft_key] = r["top_failure_modes"]
        if r.get("alignment_stats"):
            all_alignment_stats[ft_key] = r["alignment_stats"]

    # 汇总 CSV
    all_summary_rows = _aggregate_summaries(results)
    if all_summary_rows:
        csv_path = round_dir / "family_timeframe_attribution_summary.csv"
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = list(all_summary_rows[0].keys())
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in all_summary_rows:
                writer.writerow(row)
        log.info("Wrote summary CSV -> %s", csv_path)

    # Layer analysis 使用已完成 digest 校验的同一份 bytes，避免二次读取的竞态窗口。
    all_layer_analyses: dict[str, dict[str, dict[str, int]]] = {}
    for r in results:
        ft_key = r["key"]
        classified_rows = r.get("_alignment_rows") or []
        if classified_rows:
            from aats.data_platform.attribution.aggregation import build_layer_analysis

            all_layer_analyses[ft_key] = build_layer_analysis(classified_rows)

    # ---- 结论文档 ----
    log.info("Building conclusion document...")
    from aats.data_platform.attribution.report_builder import build_phase3_conclusion

    conclusion_path = round_dir / "phase3_live_attribution_conclusion.md"
    build_phase3_conclusion(
        symbol=_SYMBOL,
        start=args.start,
        end=args.end,
        all_summaries=all_summaries,
        all_failure_modes=all_failure_modes,
        all_layer_analyses=all_layer_analyses,
        all_alignment_stats=all_alignment_stats,
        round_id=round_id,
        output_path=conclusion_path,
    )

    # ---- 统计 ----
    n_ok = sum(1 for r in results if r["status"] == "succeeded")
    n_partial = sum(1 for r in results if r["status"] == "partial_success")
    n_fail = sum(1 for r in results if r["status"] == "failed")
    overall_status = _round_outcome_status(results)

    # ---- Manifest ----
    finished_at = datetime.now(timezone.utc).isoformat()
    manifest = {
        "round_id": round_id,
        "phase": "phase3",
        "started_at": started_at,
        "finished_at": finished_at,
        "status": overall_status,
        # Compatibility projection for existing evidence readers.  ``status``
        # above is the canonical unified-manifest field.
        "overall_status": overall_status,
        "scope": {
            "symbol": _SYMBOL,
            "families": ["independent", "directional"],
            "timeframes": ["15m", "1H"],
            "window": {"start": args.start, "end": args.end},
        },
        "symbol": _SYMBOL,
        "window": {"start": args.start, "end": args.end},
        "replay_only": replay_only,
        "live_query_succeeded": (
            not replay_only
            and all(result.get("live_query_succeeded") for result in results)
        ),
        "parameter_input": {
            key: value
            for key, value in parameter_lineage.items()
            if key != "combos"
        },
        "input_refs": {
            "dataset_version": args.dataset_version,
            "parameter_input": {
                key: value
                for key, value in parameter_lineage.items()
                if key != "combos"
            },
        },
        "output_refs": {
            "summary_path": "family_timeframe_attribution_summary.csv",
            "report_path": "phase3_live_attribution_conclusion.md",
        },
        "combos": [
            {
                "key": r["key"],
                "family": r["family"],
                "timeframe": r["timeframe"],
                "status": r["status"],
                "run_dir": r.get("run_dir"),
                "child_result_ref": r.get("child_result_ref"),
                "live_query_succeeded": bool(r.get("live_query_succeeded", False)),
                "alignment_stats": r.get("alignment_stats"),
                "parameter_values_fingerprint": r.get(
                    "parameter_values_fingerprint"
                ),
                "resolved_parameter_values_fingerprint": r.get(
                    "resolved_parameter_values_fingerprint"
                ),
                "source_step3_round_id": r.get("source_step3_round_id"),
                "source_step3_candidate_sha256": r.get(
                    "source_step3_candidate_sha256"
                ),
            }
            for r in results
        ],
        "code_version": None,
        "notes": None,
    }
    manifest_path = round_dir / "round_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_sha256 = immutable_json_write(manifest, manifest_path)
    log.info("Wrote manifest -> %s", manifest_path)
    combo_payload: dict[str, Any] = {}
    for r in results:
        combo_payload[r["key"]] = {
            "family": r["family"],
            "timeframe": r["timeframe"],
            "status": r["status"],
            "run_dir": r.get("run_dir"),
            "child_result_ref": r.get("child_result_ref"),
            "attribution_summary": r.get("attribution_summary"),
            "top_failure_modes": r.get("top_failure_modes"),
            "alignment_stats": r.get("alignment_stats"),
            "live_query_succeeded": bool(r.get("live_query_succeeded", False)),
            "layer_analysis": all_layer_analyses.get(r["key"]),
            "parameter_values_fingerprint": r.get(
                "parameter_values_fingerprint"
            ),
            "resolved_parameter_values_fingerprint": r.get(
                "resolved_parameter_values_fingerprint"
            ),
            "source_step3_round_id": r.get("source_step3_round_id"),
            "source_step3_candidate_sha256": r.get(
                "source_step3_candidate_sha256"
            ),
        }
    snapshot_saved = save_research_round_snapshot(
        round_id=round_id,
        phase=ROUND_PHASE_PHASE3,
        status=manifest["overall_status"],
        round_path=str(round_dir),
        started_at=started_at,
        finished_at=finished_at,
        replay_only=replay_only,
        manifest_payload=manifest,
        summary_payload={
            "summary_rows": all_summary_rows,
            "all_summaries": all_summaries,
            "all_failure_modes": all_failure_modes,
            "all_alignment_stats": all_alignment_stats,
            "all_layer_analyses": all_layer_analyses,
            "combos": combo_payload,
        },
        conclusion_payload={
            "report_markdown_path": str(conclusion_path),
        },
        artifacts_payload={
            "round_dir": str(round_dir),
            "manifest_path": str(manifest_path),
            "conclusion_path": str(conclusion_path),
            "summary_csv_path": str(round_dir / "family_timeframe_attribution_summary.csv"),
        },
    )
    if not snapshot_saved:
        if has_explicit_governance_db_configuration(_PROJECT_ROOT):
            log.error(
                "Managed Phase3 snapshot publication failed; refusing result marker "
                "for round %s",
                round_id,
            )
            return 3
        log.warning(
            "Phase3 round snapshot DB upsert unavailable; continuing in explicit "
            "offline file mode"
        )

    _publish_round_result(
        round_dir=round_dir,
        round_id=round_id,
        status=overall_status,
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        dataset_version=args.dataset_version,
        start=args.start,
        end=args.end,
        replay_only=replay_only,
        parameter_lineage=parameter_lineage,
    )

    # ---- 最终汇总 ----

    log.info("")
    log.info("=" * 60)
    log.info("Phase 3 round completed: %d succeeded, %d partial, %d failed",
             n_ok, n_partial, n_fail)
    log.info("Round dir: %s", round_dir)
    log.info("=" * 60)

    if not args.no_print_summary:
        print("")
        print(f"=== Phase 3 Attribution Round: {round_id} ===")
        print(f"Symbol: {_SYMBOL}")
        print(f"Window: {args.start} ~ {args.end}")
        print(f"Combos: {n_ok} succeeded, {n_partial} partial, {n_fail} failed")
        print("")

        for r in results:
            status_icon = {"succeeded": "OK", "partial_success": "PART", "failed": "FAIL"}.get(r["status"], "??")
            tfm = r.get("top_failure_modes", {})
            failures = tfm.get("total_failures", 0)
            success = tfm.get("total_success", 0)
            print(f"  [{status_icon}] {r['key']:<25s} "
                  f"failures={failures}, success={success}")

        print("")
        print(f"Conclusion: {round_dir / 'phase3_live_attribution_conclusion.md'}")
        print(f"Artifacts : {round_dir}")

    return _round_exit_code(overall_status)


if __name__ == "__main__":
    sys.exit(main())
