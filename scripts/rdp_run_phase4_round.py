#!/usr/bin/env python3
"""Phase 4 Round Runner — 批量 execution realism.

对 4 个 family × timeframe 组合批量运行 execution realism 分析，
生成统一汇总产物和结论文档。

固定范围：
  symbol     = BTC-USDT-SWAP
  families   = independent, directional
  timeframes = 15m, 1H

Usage:
    python scripts/rdp_run_phase4_round.py \
        --start 2026-03-31 --end 2026-04-02

    python scripts/rdp_run_phase4_round.py \
        --start 2026-03-31 --end 2026-04-02 \
        --taker-fee-bps 3.0

    python scripts/rdp_run_phase4_round.py \
        --start 2026-03-31 --end 2026-04-02 \
        --ensure-schema

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
from datetime import datetime, timedelta, timezone
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
    ROUND_PHASE_PHASE4,
    save_research_round_snapshot,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rdp_phase4_round")

_SYMBOL = "BTC-USDT-SWAP"

_COMBOS: list[dict[str, str]] = [
    {"key": "independent_15m", "family": "independent", "timeframe": "15m"},
    {"key": "independent_1h", "family": "independent", "timeframe": "1H"},
    {"key": "directional_15m", "family": "directional", "timeframe": "15m"},
    {"key": "directional_1h", "family": "directional", "timeframe": "1H"},
]

_DEFAULT_ARTIFACT_ROOT = pathlib.Path("artifacts/research/execution_rounds")
_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
_CHILD_RESULT_SCHEMA = "aats.execution_realism_result.v1"
_CHILD_RESULT_MARKER = "RDP_EXECUTION_REALISM_RESULT_JSON="
_ROUND_RESULT_SCHEMA = "aats.phase4_result.v1"
_ROUND_RESULT_MARKER = "RDP_PHASE4_RESULT_JSON="
_CHILD_OUTPUT_FILES = {
    "execution_alignment": "execution_alignment.csv",
    "fill_feasibility_summary": "fill_feasibility_summary.csv",
    "slippage_summary": "slippage_summary.csv",
    "execution_cost_summary": "execution_cost_summary.json",
    "replay_params_used": "replay_params_used.json",
    "live_execution_realism_report": "live_execution_realism_report.md",
}
_EXECUTION_ALIGNMENT_FIELDS = (
    "family", "symbol", "timeframe",
    "candidate_ts", "candidate_source", "candidate_side",
    "candidate_qty", "candidate_notional_usd", "candidate_action",
    "snapshot_ts", "trades_window_start", "trades_window_end",
    "alignment_status",
    "bar_open", "bar_high", "bar_low", "bar_close",
    "bar_volume", "bar_quote_volume", "bar_range_bps",
    "aligned_funding_rate",
    "signal_edge_proxy_bps", "funding_adjustment_bps",
    "cost_bps", "expected_net_edge_bps",
)
_FILL_FEASIBILITY_FIELDS = (
    "candidate_id", "family", "timeframe",
    "candidate_ts", "candidate_side", "candidate_qty",
    "book_depth_available_qty", "fillable_qty", "fillable_ratio",
    "volume_ratio", "levels_consumed",
    "full_fill_possible", "partial_fill_possible",
    "feasibility_category",
)
_SLIPPAGE_FIELDS = (
    "candidate_id", "family", "timeframe",
    "candidate_ts", "candidate_side", "candidate_qty",
    "candidate_action",
    "feasibility_category",
    "arrival_mid_px", "estimated_fill_vwap_px",
    "half_spread_bps", "volume_impact_bps",
    "estimated_slippage_bps", "estimated_fee_bps",
    "estimated_total_execution_cost_bps",
    "cost_vs_assumed_bps", "cost_adjusted_edge_bps",
    "slippage_model", "slippage_data_quality",
    "bar_range_bps", "bar_volume",
    "signal_edge_proxy_bps", "expected_net_edge_bps",
)
_EXECUTION_EVIDENCE_IDENTITY_FIELDS = {
    "schema_version",
    "source_run_id",
    "symbol",
    "timeframe",
    "window_start",
    "window_end",
}


# =========================================================================
# 子进程调用 one-shot execution realism
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
    def _parse(value: str) -> datetime:
        raw = value.strip()
        if len(raw) == 10:
            raw = f"{raw}T00:00:00+00:00"
        if raw.endswith("Z"):
            raw = f"{raw[:-1]}+00:00"
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    try:
        start_utc = _parse(start)
        end_utc = _parse(end)
    except (AttributeError, ValueError) as exc:
        raise ValueError("requested_window_invalid") from exc
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


def _finite_number(value: Any, *, label: str) -> float:
    if value in (None, "") or isinstance(value, bool):
        raise ValueError(f"{label}_number_required")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}_number_invalid") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{label}_number_non_finite")
    return parsed


def _csv_cell(value: Any) -> str:
    return "" if value is None else str(value)


def _assert_csv_rows_equal(
    actual: list[dict[str, str]],
    expected: list[dict[str, Any]],
    *,
    fields: tuple[str, ...],
    label: str,
) -> None:
    projected = [
        {field: _csv_cell(row.get(field)) for field in fields}
        for row in expected
    ]
    if actual != projected:
        raise ValueError(f"{label}_detail_mismatch")


def _validate_execution_business_artifacts(
    *,
    output_bytes: dict[str, bytes],
    child_result: dict[str, Any],
    family: str,
    symbol: str,
    timeframe: str,
    start: str,
    end: str,
    taker_fee_bps: float,
    replay_parameters: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, int]]:
    alignment_rows = _read_strict_csv(
        output_bytes["execution_alignment"],
        expected_fields=_EXECUTION_ALIGNMENT_FIELDS,
        label="execution_alignment",
    )
    feasibility_rows = _read_strict_csv(
        output_bytes["fill_feasibility_summary"],
        expected_fields=_FILL_FEASIBILITY_FIELDS,
        label="fill_feasibility_summary",
    )
    slippage_rows = _read_strict_csv(
        output_bytes["slippage_summary"],
        expected_fields=_SLIPPAGE_FIELDS,
        label="slippage_summary",
    )
    start_utc, end_utc = _requested_window_bounds(start, end)
    identities: set[str] = set()
    alignment_stats = {"total": 0, "matched": 0, "no_bar_data": 0}
    nullable_numeric_fields = {
        "bar_volume",
        "bar_quote_volume",
        "aligned_funding_rate",
    }
    required_numeric_fields = {
        "candidate_qty",
        "candidate_notional_usd",
        "signal_edge_proxy_bps",
        "funding_adjustment_bps",
        "cost_bps",
        "expected_net_edge_bps",
    }
    matched_numeric_fields = {
        "bar_open",
        "bar_high",
        "bar_low",
        "bar_close",
        "bar_range_bps",
    }
    noise_buffer_bps = max(
        _finite_number(
            replay_parameters.get("noise_buffer_bps"),
            label="execution_noise_buffer_bps",
        ),
        0.0,
    )

    for row in alignment_rows:
        if (
            row.get("family") != family
            or row.get("symbol") != symbol
            or row.get("timeframe") != timeframe
        ):
            raise ValueError("execution_alignment_scope_mismatch")
        if row.get("candidate_source") != "replay":
            raise ValueError("execution_candidate_source_invalid")
        if row.get("candidate_side") not in {"buy", "sell"}:
            raise ValueError("execution_candidate_side_invalid")
        if row.get("candidate_action") not in {"open", "close"}:
            raise ValueError("execution_candidate_action_invalid")
        status = row.get("alignment_status")
        if status not in {"matched", "no_bar_data"}:
            raise ValueError("execution_alignment_status_invalid")

        candidate_ts = row.get("candidate_ts") or ""
        candidate_utc = _parse_explicit_timestamp(
            candidate_ts,
            label="execution_candidate",
        )
        if candidate_ts != candidate_utc.isoformat():
            raise ValueError("execution_candidate_timestamp_not_canonical")
        if not start_utc <= candidate_utc < end_utc:
            raise ValueError("execution_candidate_outside_window")
        candidate_id = f"{candidate_utc.isoformat()}|{family}|{timeframe}"
        if candidate_id in identities:
            raise ValueError("execution_candidate_duplicate_identity")
        identities.add(candidate_id)

        required_numbers = {
            field: _finite_number(row.get(field), label=f"execution_{field}")
            for field in required_numeric_fields
        }
        if required_numbers["candidate_qty"] <= 0:
            raise ValueError("execution_candidate_qty_invalid")
        if any(
            required_numbers[field] < 0
            for field in {"candidate_notional_usd", "cost_bps"}
        ):
            raise ValueError("execution_nonnegative_value_invalid")
        expected_net_edge_bps = round(
            required_numbers["signal_edge_proxy_bps"]
            + required_numbers["funding_adjustment_bps"]
            - required_numbers["cost_bps"]
            - noise_buffer_bps,
            4,
        )
        if not math.isclose(
            required_numbers["expected_net_edge_bps"],
            expected_net_edge_bps,
            rel_tol=0,
            abs_tol=3e-4,
        ):
            raise ValueError("execution_expected_net_edge_mismatch")
        for field in nullable_numeric_fields:
            if row.get(field) not in (None, ""):
                nullable_value = _finite_number(
                    row[field],
                    label=f"execution_{field}",
                )
                if field in {"bar_volume", "bar_quote_volume"} and nullable_value < 0:
                    raise ValueError(f"execution_{field}_negative")

        if status == "matched":
            matched_timestamps = {
                field: _parse_explicit_timestamp(
                    row.get(field),
                    label=f"execution_{field}",
                )
                for field in (
                    "snapshot_ts",
                    "trades_window_start",
                    "trades_window_end",
                )
            }
            if any(
                row.get(field) != parsed.isoformat()
                for field, parsed in matched_timestamps.items()
            ):
                raise ValueError("execution_market_timestamp_not_canonical")
            from aats.data_platform.attribution.taxonomy import TF_SECONDS

            if (
                matched_timestamps["snapshot_ts"] != candidate_utc
                or matched_timestamps["trades_window_start"] != candidate_utc
                or matched_timestamps["trades_window_end"]
                != candidate_utc + timedelta(seconds=TF_SECONDS[timeframe])
            ):
                raise ValueError("execution_market_alignment_time_mismatch")
            matched_values = {
                field: _finite_number(row.get(field), label=f"execution_{field}")
                for field in matched_numeric_fields
            }
            if any(matched_values[field] <= 0 for field in (
                "bar_open", "bar_high", "bar_low", "bar_close",
            )):
                raise ValueError("execution_bar_price_invalid")
            if matched_values["bar_high"] < max(
                matched_values["bar_open"],
                matched_values["bar_close"],
                matched_values["bar_low"],
            ) or matched_values["bar_low"] > min(
                matched_values["bar_open"],
                matched_values["bar_close"],
                matched_values["bar_high"],
            ):
                raise ValueError("execution_bar_ohlc_invalid")
            if matched_values["bar_range_bps"] < 0:
                raise ValueError("execution_bar_range_invalid")
            expected_range = round(
                (matched_values["bar_high"] - matched_values["bar_low"])
                / matched_values["bar_close"]
                * 10000,
                2,
            )
            if not math.isclose(
                matched_values["bar_range_bps"],
                expected_range,
                rel_tol=0,
                abs_tol=1e-9,
            ):
                raise ValueError("execution_bar_range_mismatch")
            expected_notional = (
                required_numbers["candidate_qty"]
                * 0.01
                * matched_values["bar_close"]
            )
            if not math.isclose(
                required_numbers["candidate_notional_usd"],
                expected_notional,
                rel_tol=1e-12,
                abs_tol=1e-9,
            ):
                raise ValueError("execution_candidate_notional_mismatch")
        elif any(
            row.get(field) not in (None, "")
            for field in (
                "snapshot_ts", "trades_window_start", "trades_window_end",
                "bar_open", "bar_high", "bar_low", "bar_close",
                "bar_volume", "bar_quote_volume", "bar_range_bps",
                "aligned_funding_rate",
            )
        ):
            raise ValueError("execution_no_bar_data_payload_invalid")
        elif required_numbers["candidate_notional_usd"] != 0:
            raise ValueError("execution_no_bar_data_notional_invalid")

        alignment_stats["total"] += 1
        alignment_stats[str(status)] += 1

    from aats.data_platform.execution_realism.execution_cost_model import (
        build_execution_cost_summary,
    )
    from aats.data_platform.execution_realism.fill_feasibility import (
        evaluate_fill_feasibility,
    )
    from aats.data_platform.execution_realism.slippage_estimator import (
        estimate_slippage,
    )

    recomputed_feasibility = evaluate_fill_feasibility(alignment_rows)
    _assert_csv_rows_equal(
        feasibility_rows,
        recomputed_feasibility,
        fields=_FILL_FEASIBILITY_FIELDS,
        label="fill_feasibility_summary",
    )
    recomputed_slippage = estimate_slippage(
        recomputed_feasibility,
        taker_fee_bps=taker_fee_bps,
    )
    _assert_csv_rows_equal(
        slippage_rows,
        recomputed_slippage,
        fields=_SLIPPAGE_FIELDS,
        label="slippage_summary",
    )
    recomputed_summary = build_execution_cost_summary(recomputed_slippage)
    cost_summary = _decode_json(
        output_bytes["execution_cost_summary"],
        label="execution_cost_summary",
    )
    if type(cost_summary) is not dict:
        raise ValueError("execution_cost_summary_contract_invalid")
    if set(cost_summary) != set(recomputed_summary) | _EXECUTION_EVIDENCE_IDENTITY_FIELDS:
        raise ValueError("execution_cost_summary_keys_invalid")
    if any(cost_summary.get(key) != value for key, value in recomputed_summary.items()):
        raise ValueError("execution_cost_summary_detail_mismatch")
    expected_identity = {
        "schema_version": "execution_cost_summary_v1",
        "source_run_id": child_result["run_id"],
        "symbol": symbol,
        "timeframe": timeframe,
        "window_start": start_utc.isoformat(),
        "window_end": end_utc.isoformat(),
    }
    if any(cost_summary.get(key) != value for key, value in expected_identity.items()):
        raise ValueError("execution_cost_summary_identity_mismatch")
    expected_exit_code = (
        2
        if alignment_stats["total"] > 0 and alignment_stats["matched"] == 0
        else 0
    )
    expected_status = "partial_success" if expected_exit_code == 2 else "succeeded"
    if (
        child_result.get("exit_code") != expected_exit_code
        or child_result.get("status") != expected_status
    ):
        raise ValueError("execution_result_status_business_mismatch")
    return cost_summary, recomputed_slippage, alignment_stats


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
        raise ValueError("phase4_per_combo_root_symlink_invalid")
    per_combo_root.mkdir(parents=True, exist_ok=True)
    trusted_root = per_combo_root.resolve(strict=True)
    combo_root = trusted_root / combo_key
    if combo_root.is_symlink():
        raise ValueError("phase4_combo_root_symlink_invalid")
    combo_root.mkdir(parents=False, exist_ok=True)
    resolved_combo_root = combo_root.resolve(strict=True)
    if resolved_combo_root.parent != trusted_root:
        raise ValueError("phase4_combo_root_outside_round")
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
    taker_fee_bps: float,
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
        "taker_fee_bps", "resolved_parameter_values_fingerprint", "finished_at",
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
    result_fee = result.get("taker_fee_bps")
    if (
        isinstance(result_fee, bool)
        or type(result_fee) not in {int, float}
        or not math.isfinite(float(result_fee))
    ):
        raise ValueError("result_taker_fee_type_invalid")
    expected_scope = {
        "family": family,
        "symbol": symbol,
        "timeframe": timeframe,
        "dataset_version": dataset_version,
        "window": {"start": start, "end": end},
        "taker_fee_bps": taker_fee_bps,
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


def _run_single_execution_realism(
    family: str,
    timeframe: str,
    *,
    symbol: str,
    start: str,
    end: str,
    artifact_root: pathlib.Path,
    taker_fee_bps: float,
    ensure_schema: bool,
    dataset_version: str,
    params_json: str | None = None,
    parameter_lineage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """通过子进程调用 rdp_run_execution_realism.py。"""
    combo_key = f"{family}_{timeframe.lower()}"
    try:
        combo_root = _prepare_combo_root(artifact_root, combo_key=combo_key)
    except (OSError, ValueError) as exc:
        return {
            "family": family,
            "timeframe": timeframe,
            "status": "failed",
            "run_dir": None,
            "cost_summary": None,
            "slippage_rows": None,
            "error": f"phase4_combo_root_invalid:{exc}",
        }
    result_path = combo_root / f"result_{uuid4().hex}.json"

    cmd = [
        sys.executable, "scripts/rdp_run_execution_realism.py",
        "--family", family,
        "--symbol", symbol,
        "--timeframe", timeframe,
        "--start", start,
        "--end", end,
        "--dataset-version", dataset_version,
        "--taker-fee-bps", str(taker_fee_bps),
        "--artifact-root", str(combo_root),
        "--result-json", str(result_path),
    ]
    if ensure_schema:
        cmd.append("--ensure-schema")
    # P0: 参数闭环 — 传递 Phase 2 推荐参数
    if params_json:
        ft_key = f"{family}_{timeframe.lower()}"
        cmd.extend(["--params-json", params_json, "--parameter-set", ft_key])

    log.info("  CMD: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True)

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
            taker_fee_bps=taker_fee_bps,
            expected_parameter_fingerprint=expected_parameter_fingerprint,
        )
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "family": family,
            "timeframe": timeframe,
            "status": "failed",
            "run_dir": None,
            "cost_summary": None,
            "slippage_rows": None,
            "error": f"phase4_child_result_invalid:{exc}",
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
            "cost_summary": None,
            "slippage_rows": None,
            "error": f"phase4_parameter_evidence_invalid:{type(exc).__name__}",
        }
    if used_parameters_fingerprint != child_result["resolved_parameter_values_fingerprint"]:
        return {
            "family": family,
            "timeframe": timeframe,
            "status": "failed",
            "run_dir": str(run_dir),
            "cost_summary": None,
            "slippage_rows": None,
            "error": "phase4_sidecar_parameter_values_fingerprint_mismatch",
        }

    try:
        cost_summary, slippage_rows, alignment_stats = (
            _validate_execution_business_artifacts(
                output_bytes=output_bytes,
                child_result=child_result,
                family=family,
                symbol=symbol,
                timeframe=timeframe,
                start=start,
                end=end,
                taker_fee_bps=taker_fee_bps,
                replay_parameters=used_parameters,
            )
        )
    except (UnicodeDecodeError, csv.Error, ValueError) as exc:
        return {
            "family": family,
            "timeframe": timeframe,
            "status": "failed",
            "run_dir": str(run_dir),
            "cost_summary": None,
            "slippage_rows": None,
            "error": f"phase4_child_artifact_invalid:{exc}",
        }

    return {
        "family": family,
        "timeframe": timeframe,
        # P1b: 保留 partial_success 语义（exit=2 表示 replay 正常但无 bar 匹配）
        "status": child_result["status"],
        "run_dir": str(run_dir),
        "child_result_ref": child_result_ref,
        "cost_summary": cost_summary,
        "slippage_rows": slippage_rows,
        "alignment_stats": alignment_stats,
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
# 主流程
# =========================================================================


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
    parameter_lineage: dict[str, Any],
) -> dict[str, Any]:
    payload = {
        "schema_version": _ROUND_RESULT_SCHEMA,
        "phase": "phase4",
        "round_id": round_id,
        "round_dir": str(round_dir.resolve(strict=True)),
        "manifest_path": str(manifest_path.resolve(strict=True)),
        "manifest_sha256": manifest_sha256,
        "manifest_size_bytes": manifest_path.stat().st_size,
        "status": status,
        "exit_code": _round_exit_code(status),
        "symbol": _SYMBOL,
        "dataset_version": dataset_version,
        "window": {"start": start, "end": end},
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Phase 4 Round Runner: 批量 execution realism 分析",
    )
    parser.add_argument("--start", required=True, help="YYYY-MM-DD (UTC)")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD (UTC)")
    parser.add_argument("--dataset-version", default="v1.0")
    parser.add_argument(
        "--taker-fee-bps", type=float, default=5.0,
        help="Taker fee in bps (default: 5.0)",
    )
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
        log.error("Phase 4 parameter lineage validation failed: %s", exc)
        return 2
    if parameter_lineage.get("status") == "bound" and (
        parameter_lineage.get("symbol") != _SYMBOL
        or parameter_lineage.get("dataset_version") != args.dataset_version
        or parameter_lineage.get("window")
        != {"start": args.start, "end": args.end}
    ):
        log.error("Phase 4 parameter lineage scope mismatch")
        return 2

    started_at = datetime.now(timezone.utc).isoformat()
    round_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:8]
    artifact_root = pathlib.Path(args.artifact_root).resolve()
    round_dir = artifact_root / round_id
    per_combo_root = round_dir / "per_combo"

    log.info("=" * 60)
    log.info("Phase 4 Round Runner (Execution Realism)")
    log.info("  Round ID    : %s", round_id)
    log.info("  Symbol      : %s", _SYMBOL)
    log.info("  Window      : %s ~ %s", args.start, args.end)
    log.info("  Taker fee   : %.1f bps", args.taker_fee_bps)
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

        result = _run_single_execution_realism(
            combo["family"], combo["timeframe"],
            symbol=_SYMBOL,
            start=args.start,
            end=args.end,
            artifact_root=per_combo_root,
            taker_fee_bps=args.taker_fee_bps,
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

    all_cost_summaries: dict[str, dict[str, Any]] = {}
    all_slippage_rows: dict[str, list[dict[str, Any]]] = {}

    for r in results:
        ft_key = r["key"]
        if r.get("cost_summary"):
            all_cost_summaries[ft_key] = r["cost_summary"]
        if r.get("slippage_rows"):
            all_slippage_rows[ft_key] = r["slippage_rows"]

    # 比较表
    from aats.data_platform.execution_realism.aggregation import (
        build_execution_realism_comparison,
        generate_cross_comparison_findings,
    )

    comparison_input = {
        ft_key: {
            "cost_summary": all_cost_summaries.get(ft_key, {}),
            "slippage_rows": all_slippage_rows.get(ft_key, []),
        }
        for ft_key in [c["key"] for c in _COMBOS]
        if ft_key in all_cost_summaries
    }

    comparison_rows = build_execution_realism_comparison(comparison_input)
    cross_findings = generate_cross_comparison_findings(comparison_rows)

    # 写入比较 CSV
    if comparison_rows:
        comp_csv_path = round_dir / "execution_realism_comparison.csv"
        comp_csv_path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = list(comparison_rows[0].keys())
        with comp_csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in comparison_rows:
                writer.writerow(row)
        log.info("Wrote comparison CSV -> %s", comp_csv_path)

    # ---- 结论文档 ----
    log.info("Building conclusion document...")
    from aats.data_platform.execution_realism.report_builder import build_phase4_conclusion

    conclusion_path = round_dir / "phase4_execution_realism_conclusion.md"
    build_phase4_conclusion(
        symbol=_SYMBOL,
        start=args.start,
        end=args.end,
        all_cost_summaries=all_cost_summaries,
        comparison_rows=comparison_rows,
        cross_findings=cross_findings,
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
        "phase": "phase4",
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
        "taker_fee_bps": args.taker_fee_bps,
        "model_version": "v1_bar_proxy",
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
            "summary_path": "execution_realism_comparison.csv",
            "report_path": "phase4_execution_realism_conclusion.md",
        },
        "combos": [
            {
                "key": r["key"],
                "family": r["family"],
                "timeframe": r["timeframe"],
                "status": r["status"],
                "run_dir": r.get("run_dir"),
                "child_result_ref": r.get("child_result_ref"),
                "candidates": (r.get("cost_summary") or {}).get("total_candidates", 0),
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
            "cost_summary": r.get("cost_summary"),
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
    snapshot_saved = save_research_round_snapshot(
        round_id=round_id,
        phase=ROUND_PHASE_PHASE4,
        status=manifest["overall_status"],
        round_path=str(round_dir),
        started_at=started_at,
        finished_at=finished_at,
        replay_only=False,
        manifest_payload=manifest,
        summary_payload={
            "all_cost_summaries": all_cost_summaries,
            "comparison_rows": comparison_rows,
            "cross_findings": cross_findings,
            "combos": combo_payload,
        },
        conclusion_payload={
            "report_markdown_path": str(conclusion_path),
        },
        artifacts_payload={
            "round_dir": str(round_dir),
            "manifest_path": str(manifest_path),
            "conclusion_path": str(conclusion_path),
            "comparison_csv_path": str(round_dir / "execution_realism_comparison.csv"),
        },
    )
    if not snapshot_saved:
        if has_explicit_governance_db_configuration(_PROJECT_ROOT):
            log.error(
                "Managed Phase4 snapshot publication failed; refusing result marker "
                "for round %s",
                round_id,
            )
            return 3
        log.warning(
            "Phase4 round snapshot DB upsert unavailable; continuing in explicit "
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
        parameter_lineage=parameter_lineage,
    )

    # ---- 最终汇总 ----

    log.info("")
    log.info("=" * 60)
    log.info("Phase 4 round completed: %d succeeded, %d partial, %d failed",
             n_ok, n_partial, n_fail)
    log.info("Round dir: %s", round_dir)
    log.info("=" * 60)

    if not args.no_print_summary:
        print("")
        print(f"=== Phase 4 Execution Realism Round: {round_id} ===")
        print(f"Symbol: {_SYMBOL}")
        print(f"Window: {args.start} ~ {args.end}")
        print(f"Combos: {n_ok} succeeded, {n_partial} partial, {n_fail} failed")
        print("")

        for r in results:
            status_icon = {"succeeded": "OK", "partial_success": "PART", "failed": "FAIL"}.get(r["status"], "??")
            cs = r.get("cost_summary", {})
            candidates = cs.get("total_candidates", 0)
            full_fill = cs.get("full_fill_ratio", 0)
            mean_slip = cs.get("slippage", {}).get("mean", 0)
            print(f"  [{status_icon}] {r['key']:<25s} "
                  f"candidates={candidates}, full_fill={full_fill:.1%}, "
                  f"mean_slip={mean_slip:.2f}bps")

        print("")
        print(f"Comparison: {round_dir / 'execution_realism_comparison.csv'}")
        print(f"Conclusion: {round_dir / 'phase4_execution_realism_conclusion.md'}")
        print(f"Artifacts : {round_dir}")

    return _round_exit_code(overall_status)


if __name__ == "__main__":
    sys.exit(main())
