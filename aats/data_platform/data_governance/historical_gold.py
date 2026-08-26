"""Deterministic, bundle-scoped historical Gold replay artifacts.

This builder is intentionally separate from the legacy Gold tables.  Every
output row is versioned by an immutable artifact and carries the exact input
bundle fingerprints used to build it.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable

from sqlalchemy import text

from aats.data_platform.data_governance.contracts import canonical_json_bytes
from aats.data_platform.gold.funding_aligner import align_funding_to_bars
from aats.data_platform.models import candle_table_name, instrument_type_for_symbol


TRANSFORM_VERSION = "rdp-historical-gold-v1"
_GIT_COMMIT = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_SUPPORTED_TIMEFRAMES = {"15m": timedelta(minutes=15), "1H": timedelta(hours=1)}
_PURPOSE_ROLES = {
    "ohlcv_research": "candles",
    "funding_research": "funding",
    "mark_price_research": "mark_price_bar",
    "trade_flow_research": "trades",
    "l2_replay": "l2_event_history",
}


@dataclass(frozen=True)
class HistoricalGoldInput:
    bundle_id: str
    purpose: str
    role: str
    fingerprint: str
    dataset_version: str
    coverage_start: datetime
    coverage_end: datetime
    source_id: str
    source_key: str
    source_row_count: int
    raw_partition_sha256: tuple[str, ...]
    rebuild_output_fingerprint: str | None = None


@dataclass(frozen=True)
class HistoricalGoldPlan:
    operation_key: str
    symbol: str
    timeframe: str
    coverage_start: datetime
    coverage_end: datetime
    candle: HistoricalGoldInput
    funding: HistoricalGoldInput | None
    auxiliary: tuple[HistoricalGoldInput, ...]
    input_fingerprint: str
    transform_version: str
    git_commit: str

    @property
    def inputs(self) -> tuple[HistoricalGoldInput, ...]:
        return (self.candle,) + (() if self.funding is None else (self.funding,)) + self.auxiliary


@dataclass(frozen=True)
class HistoricalGoldResult:
    artifact_id: str
    operation_key: str
    rows_written: int
    output_fingerprint: str
    quality_report: dict[str, Any]
    artifact_index: dict[str, Any]


def plan_historical_gold(
    session,
    *,
    symbol: str,
    timeframe: str,
    candle_bundle_id: str,
    funding_bundle_id: str | None,
    auxiliary_bundle_ids: Iterable[str] = (),
    coverage_start: datetime | None = None,
    coverage_end: datetime | None = None,
    git_commit: str,
) -> HistoricalGoldPlan:
    """Build a fail-closed plan from currently ELIGIBLE historical bundles."""

    normalized_symbol = str(symbol).upper().strip()
    if not normalized_symbol:
        raise ValueError("historical_gold_symbol_missing")
    if timeframe not in _SUPPORTED_TIMEFRAMES:
        raise ValueError("historical_gold_timeframe_unsupported")
    if not _GIT_COMMIT.fullmatch(git_commit):
        raise ValueError("historical_gold_git_commit_invalid")

    candle = _load_input(session, candle_bundle_id, normalized_symbol)
    if candle.purpose != "ohlcv_research":
        raise ValueError("historical_gold_candle_bundle_purpose_invalid")
    funding = None
    if funding_bundle_id is not None:
        funding = _load_input(session, funding_bundle_id, normalized_symbol)
        if funding.purpose != "funding_research":
            raise ValueError("historical_gold_funding_bundle_purpose_invalid")
    if instrument_type_for_symbol(normalized_symbol) == "swap" and funding is None:
        raise ValueError("historical_gold_swap_requires_funding_bundle")

    start = coverage_start or candle.coverage_start
    end = coverage_end or candle.coverage_end
    _validate_window(start, end, timeframe)
    all_inputs: list[HistoricalGoldInput] = [candle]
    if funding is not None:
        all_inputs.append(funding)
    seen = {candle.bundle_id, *(() if funding is None else (funding.bundle_id,))}
    auxiliaries: list[HistoricalGoldInput] = []
    for bundle_id in auxiliary_bundle_ids:
        if bundle_id in seen:
            raise ValueError("historical_gold_duplicate_input_bundle")
        item = _load_input(session, bundle_id, normalized_symbol)
        if item.purpose in {"ohlcv_research", "funding_research"}:
            raise ValueError("historical_gold_auxiliary_role_invalid")
        item = _with_rebuild_evidence(session, item)
        auxiliaries.append(item)
        all_inputs.append(item)
        seen.add(item.bundle_id)

    for item in (candle,) + (() if funding is None else (funding,)):
        if item.coverage_start > start or item.coverage_end < end:
            raise ValueError(f"historical_gold_bundle_coverage_insufficient:{item.role}")
    _validate_auxiliary_coverage(auxiliaries, start=start, end=end)

    input_payload = {
        "schema": "aats.historical_gold.inputs.v1",
        "symbol": normalized_symbol,
        "timeframe": timeframe,
        "coverage_start": start,
        "coverage_end": end,
        "inputs": [_input_identity(item) for item in all_inputs],
    }
    input_fingerprint = hashlib.sha256(canonical_json_bytes(input_payload)).hexdigest()
    operation_payload = {
        "input_fingerprint": input_fingerprint,
        "transform_version": TRANSFORM_VERSION,
        "git_commit": git_commit,
    }
    operation_key = "hist-gold-" + hashlib.sha256(
        canonical_json_bytes(operation_payload)
    ).hexdigest()
    return HistoricalGoldPlan(
        operation_key=operation_key,
        symbol=normalized_symbol,
        timeframe=timeframe,
        coverage_start=start,
        coverage_end=end,
        candle=candle,
        funding=funding,
        auxiliary=tuple(auxiliaries),
        input_fingerprint=input_fingerprint,
        transform_version=TRANSFORM_VERSION,
        git_commit=git_commit,
    )


def start_historical_gold(session, plan: HistoricalGoldPlan) -> tuple[str, str]:
    inserted = session.execute(
        text(
            """
            INSERT INTO meta.historical_research_artifacts (
                operation_key, artifact_type, primary_bundle_id, symbol,
                timeframe, coverage_start, coverage_end, input_bundles,
                input_fingerprint, transform_version, git_commit, status,
                started_at
            ) VALUES (
                :operation_key, 'gold_replay_bars', CAST(:primary_bundle_id AS UUID),
                :symbol, :timeframe, :coverage_start, :coverage_end,
                CAST(:input_bundles AS jsonb), :input_fingerprint,
                :transform_version, :git_commit, 'RUNNING', NOW()
            ) ON CONFLICT (operation_key) DO NOTHING
            RETURNING artifact_id
            """
        ),
        {
            "operation_key": plan.operation_key,
            "primary_bundle_id": plan.candle.bundle_id,
            "symbol": plan.symbol,
            "timeframe": plan.timeframe,
            "coverage_start": plan.coverage_start,
            "coverage_end": plan.coverage_end,
            "input_bundles": json.dumps(
                [_input_identity(item) for item in plan.inputs],
                sort_keys=True,
                default=str,
            ),
            "input_fingerprint": plan.input_fingerprint,
            "transform_version": plan.transform_version,
            "git_commit": plan.git_commit,
        },
    ).scalar_one_or_none()
    if inserted is not None:
        return "started", str(inserted)

    existing = session.execute(
        text(
            "SELECT artifact_id, status, input_fingerprint "
            "FROM meta.historical_research_artifacts "
            "WHERE operation_key = :operation_key FOR UPDATE"
        ),
        {"operation_key": plan.operation_key},
    ).one()
    if str(existing.input_fingerprint) != plan.input_fingerprint:
        raise RuntimeError("historical_gold_operation_identity_conflict")
    artifact_id = str(existing.artifact_id)
    if existing.status == "SUCCEEDED":
        _verify_succeeded_artifact(session, artifact_id, plan)
        return "already_succeeded", artifact_id
    if existing.status == "RUNNING":
        raise RuntimeError("historical_gold_already_running")
    session.execute(
        text(
            "DELETE FROM gold.historical_replay_bars "
            "WHERE artifact_id = CAST(:artifact_id AS UUID)"
        ),
        {"artifact_id": artifact_id},
    )
    session.execute(
        text(
            "UPDATE meta.historical_research_artifacts SET status = 'RUNNING', "
            "row_count = 0, output_fingerprint = NULL, quality_report = NULL, "
            "artifact_index = NULL, started_at = NOW(), ended_at = NULL, "
            "error_message = NULL, updated_at = NOW() "
            "WHERE artifact_id = CAST(:artifact_id AS UUID)"
        ),
        {"artifact_id": artifact_id},
    )
    return "started", artifact_id


def execute_historical_gold(
    session,
    plan: HistoricalGoldPlan,
    *,
    artifact_id: str,
) -> HistoricalGoldResult:
    _verify_plan_current(session, plan)
    _verify_auxiliary_material(session, plan)
    candle_table = candle_table_name("silver", plan.symbol, plan.timeframe)
    candles = session.execute(
        text(
            f"SELECT symbol, ts, open, high, low, close, vol, vol_quote, "
            f"confirm, dataset_version FROM {candle_table} "
            "WHERE symbol = :symbol AND ts >= :start AND ts < :end "
            "AND dataset_version = :dataset_version ORDER BY ts"
        ),
        {
            "symbol": plan.symbol,
            "start": plan.coverage_start,
            "end": plan.coverage_end,
            "dataset_version": plan.candle.dataset_version,
        },
    ).fetchall()
    expected_rows = _expected_rows(plan.coverage_start, plan.coverage_end, plan.timeframe)
    if len(candles) != expected_rows or len(candles) != plan.candle.source_row_count:
        raise RuntimeError("historical_gold_candle_material_count_mismatch")
    if any(not bool(row.confirm) for row in candles):
        raise RuntimeError("historical_gold_contains_unconfirmed_candle")

    funding_events: list[dict[str, Any]] = []
    if plan.funding is not None:
        funding_rows = session.execute(
            text(
                "SELECT ts, funding_rate, dataset_version "
                "FROM silver.market_swap_funding WHERE symbol = :symbol "
                "AND ts >= :start AND ts < :end "
                "AND dataset_version = :dataset_version ORDER BY ts"
            ),
            {
                "symbol": plan.symbol,
                "start": plan.coverage_start,
                "end": plan.coverage_end,
                "dataset_version": plan.funding.dataset_version,
            },
        ).fetchall()
        if len(funding_rows) != plan.funding.source_row_count:
            raise RuntimeError("historical_gold_funding_material_count_mismatch")
        funding_events = [
            {
                "ts": row.ts,
                "funding_rate": row.funding_rate,
                "dataset_version": row.dataset_version,
            }
            for row in funding_rows
        ]

    funding_map = align_funding_to_bars(
        [row.ts for row in candles],
        funding_events,
    )
    lineage = [_input_identity(item) for item in plan.inputs]
    lineage_json = json.dumps(lineage, sort_keys=True, default=str)
    session.execute(
        text(
            "DELETE FROM gold.historical_replay_bars "
            "WHERE artifact_id = CAST(:artifact_id AS UUID)"
        ),
        {"artifact_id": artifact_id},
    )

    values: list[dict[str, Any]] = []
    row_fingerprints: list[str] = []
    for row in candles:
        funding_rate, funding_ts, _ = funding_map.get(row.ts, (None, None, None))
        material_payload = {
            "symbol": plan.symbol,
            "timeframe": plan.timeframe,
            "ts": row.ts,
            "open": row.open,
            "high": row.high,
            "low": row.low,
            "close": row.close,
            "volume": row.vol,
            "quote_volume": row.vol_quote,
            "is_closed": bool(row.confirm),
            "aligned_funding_rate": funding_rate,
            "funding_source_ts": funding_ts,
            "source_candle_bundle_id": plan.candle.bundle_id,
            "source_funding_bundle_id": (
                None if plan.funding is None else plan.funding.bundle_id
            ),
            "source_lineage": lineage,
            "transform_version": plan.transform_version,
        }
        # The generated database artifact_id is deliberately excluded: two
        # clean databases must produce identical content fingerprints from the
        # same immutable inputs and transform version.
        fingerprint = hashlib.sha256(canonical_json_bytes(material_payload)).hexdigest()
        row_fingerprints.append(fingerprint)
        values.append(
            {
                "artifact_id": artifact_id,
                **material_payload,
                "source_lineage": lineage_json,
                "output_fingerprint": fingerprint,
            }
        )

    insert = text(
        """
        INSERT INTO gold.historical_replay_bars (
            artifact_id, symbol, timeframe, ts, open, high, low, close,
            volume, quote_volume, is_closed, aligned_funding_rate,
            funding_source_ts, source_candle_bundle_id,
            source_funding_bundle_id, source_lineage, transform_version,
            output_fingerprint
        ) VALUES (
            CAST(:artifact_id AS UUID), :symbol, :timeframe, :ts,
            :open, :high, :low, :close, :volume, :quote_volume, :is_closed,
            :aligned_funding_rate, :funding_source_ts,
            CAST(:source_candle_bundle_id AS UUID),
            CAST(:source_funding_bundle_id AS UUID),
            CAST(:source_lineage AS jsonb), :transform_version,
            :output_fingerprint
        )
        """
    )
    for offset in range(0, len(values), 2000):
        session.execute(insert, values[offset : offset + 2000])

    output_fingerprint = _aggregate_output_fingerprint(
        plan.input_fingerprint,
        row_fingerprints,
    )
    quality = _quality_report(plan, rows=len(values), funding_events=funding_events)
    quality_fingerprint = hashlib.sha256(canonical_json_bytes(quality)).hexdigest()
    quality["quality_fingerprint"] = quality_fingerprint
    artifact_index = {
        "schema": "aats.historical_research_artifact_index.v1",
        "artifact_id": artifact_id,
        "artifact_type": "gold_replay_bars",
        "relation": "gold.historical_replay_bars",
        "symbol": plan.symbol,
        "timeframe": plan.timeframe,
        "coverage_start": plan.coverage_start,
        "coverage_end": plan.coverage_end,
        "row_count": len(values),
        "input_fingerprint": plan.input_fingerprint,
        "output_fingerprint": output_fingerprint,
        "quality_fingerprint": quality_fingerprint,
        "input_bundles": lineage,
        "input_bundle_count": len(lineage),
        "transform_version": plan.transform_version,
        "git_commit": plan.git_commit,
    }
    terminal = session.execute(
        text(
            "UPDATE meta.historical_research_artifacts SET status = 'SUCCEEDED', "
            "row_count = :row_count, output_fingerprint = :output_fingerprint, "
            "quality_report = CAST(:quality_report AS jsonb), "
            "artifact_index = CAST(:artifact_index AS jsonb), ended_at = NOW(), "
            "updated_at = NOW() WHERE artifact_id = CAST(:artifact_id AS UUID) "
            "AND status = 'RUNNING'"
        ),
        {
            "artifact_id": artifact_id,
            "row_count": len(values),
            "output_fingerprint": output_fingerprint,
            "quality_report": json.dumps(quality, sort_keys=True, default=str),
            "artifact_index": json.dumps(artifact_index, sort_keys=True, default=str),
        },
    )
    if int(terminal.rowcount or 0) != 1:
        raise RuntimeError("historical_gold_terminal_transition_conflict")
    return HistoricalGoldResult(
        artifact_id=artifact_id,
        operation_key=plan.operation_key,
        rows_written=len(values),
        output_fingerprint=output_fingerprint,
        quality_report=quality,
        artifact_index=artifact_index,
    )


def fail_historical_gold(session, artifact_id: str, error_type: str) -> None:
    result = session.execute(
        text(
            "UPDATE meta.historical_research_artifacts SET status = 'FAILED', "
            "error_message = :error_type, ended_at = NOW(), updated_at = NOW() "
            "WHERE artifact_id = CAST(:artifact_id AS UUID) AND status = 'RUNNING'"
        ),
        {"artifact_id": artifact_id, "error_type": error_type},
    )
    if int(result.rowcount or 0) != 1:
        raise RuntimeError("historical_gold_failure_transition_conflict")


def _load_input(session, bundle_id: str, symbol: str) -> HistoricalGoldInput:
    row = session.execute(
        text(
            "SELECT bundle_id, purpose, eligibility_mode, status, fingerprint, "
            "dataset_version, coverage_start, coverage_end, component_sources "
            "FROM meta.dataset_bundles WHERE bundle_id = CAST(:bundle_id AS UUID)"
        ),
        {"bundle_id": bundle_id},
    ).mappings().one_or_none()
    if row is None:
        raise ValueError("historical_gold_bundle_not_found")
    if row["eligibility_mode"] != "historical_research" or row["status"] != "ELIGIBLE":
        raise ValueError("historical_gold_bundle_not_eligible")
    purpose = str(row["purpose"])
    role = _PURPOSE_ROLES.get(purpose)
    if role is None:
        raise ValueError("historical_gold_bundle_purpose_unsupported")
    components = row["component_sources"]
    if isinstance(components, str):
        components = json.loads(components)
    if not isinstance(components, list) or len(components) != 1:
        raise ValueError("historical_gold_bundle_component_shape_invalid")
    component = components[0]
    if not isinstance(component, dict) or str(component.get("symbol")) != symbol:
        raise ValueError("historical_gold_bundle_symbol_mismatch")
    if str(component.get("role")) != role:
        raise ValueError("historical_gold_bundle_role_mismatch")
    provenance = component.get("provenance")
    if not isinstance(provenance, dict):
        raise ValueError("historical_gold_bundle_provenance_missing")
    try:
        source_id = str(component["source_id"])
        source_key = str(provenance["source_key"])
        row_count = int(provenance["row_count"])
        hashes = tuple(
            sorted(str(item) for item in provenance["gap_manifest"]["raw_partition_sha256"])
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("historical_gold_bundle_provenance_invalid") from exc
    if row_count <= 0 or not hashes or any(not re.fullmatch(r"[0-9a-f]{64}", item) for item in hashes):
        raise ValueError("historical_gold_bundle_material_invalid")
    return HistoricalGoldInput(
        bundle_id=str(row["bundle_id"]),
        purpose=purpose,
        role=role,
        fingerprint=str(row["fingerprint"]),
        dataset_version=str(row["dataset_version"]),
        coverage_start=row["coverage_start"],
        coverage_end=row["coverage_end"],
        source_id=source_id,
        source_key=source_key,
        source_row_count=row_count,
        raw_partition_sha256=hashes,
    )


def _with_rebuild_evidence(session, item: HistoricalGoldInput) -> HistoricalGoldInput:
    if item.purpose not in {"trade_flow_research", "l2_replay"}:
        return item
    fingerprint = session.execute(
        text(
            "SELECT output_fingerprint FROM meta.data_rebuild_runs "
            "WHERE bundle_id = CAST(:bundle_id AS UUID) AND status = 'SUCCEEDED' "
            "AND input_fingerprint = :input_fingerprint "
            "ORDER BY ended_at DESC LIMIT 1"
        ),
        {"bundle_id": item.bundle_id, "input_fingerprint": item.fingerprint},
    ).scalar_one_or_none()
    if fingerprint is None:
        raise ValueError(f"historical_gold_auxiliary_rebuild_missing:{item.role}")
    return HistoricalGoldInput(**{**asdict(item), "rebuild_output_fingerprint": str(fingerprint)})


def _verify_plan_current(session, plan: HistoricalGoldPlan) -> None:
    for item in plan.inputs:
        row = session.execute(
            text(
                "SELECT fingerprint, status, eligibility_mode FROM meta.dataset_bundles "
                "WHERE bundle_id = CAST(:bundle_id AS UUID) FOR SHARE"
            ),
            {"bundle_id": item.bundle_id},
        ).one()
        if (
            str(row.fingerprint) != item.fingerprint
            or row.status != "ELIGIBLE"
            or row.eligibility_mode != "historical_research"
        ):
            raise RuntimeError("historical_gold_bundle_changed_or_ineligible")


def _verify_auxiliary_material(session, plan: HistoricalGoldPlan) -> None:
    for item in plan.auxiliary:
        if item.purpose == "mark_price_research":
            if ":15m:" in item.source_key:
                table = "bronze.market_mark_price_candles_15m"
            elif ":1H:" in item.source_key:
                table = "bronze.market_mark_price_candles_1h"
            else:
                raise RuntimeError("historical_gold_mark_timeframe_unknown")
            observed = int(
                session.execute(
                    text(
                        f"SELECT COUNT(*) FROM {table} "
                        "WHERE source_id = CAST(:source_id AS UUID) "
                        "AND symbol = :symbol AND ts >= :start AND ts < :end"
                    ),
                    {
                        "source_id": item.source_id,
                        "symbol": plan.symbol,
                        "start": item.coverage_start,
                        "end": item.coverage_end,
                    },
                ).scalar_one()
            )
            if observed != item.source_row_count:
                raise RuntimeError("historical_gold_mark_material_count_mismatch")
            continue

        table = {
            "trade_flow_research": "silver.historical_trade_flow_15m",
            "l2_replay": "silver.historical_orderbook_metrics_15m",
        }.get(item.purpose)
        if table is None or item.rebuild_output_fingerprint is None:
            raise RuntimeError("historical_gold_auxiliary_material_unsupported")
        run = session.execute(
            text(
                "SELECT transform_version, git_commit, output_fingerprint "
                "FROM meta.data_rebuild_runs WHERE bundle_id = CAST(:bundle_id AS UUID) "
                "AND status = 'SUCCEEDED' AND input_fingerprint = :input_fingerprint "
                "AND output_fingerprint = :output_fingerprint "
                "ORDER BY ended_at DESC LIMIT 1"
            ),
            {
                "bundle_id": item.bundle_id,
                "input_fingerprint": item.fingerprint,
                "output_fingerprint": item.rebuild_output_fingerprint,
            },
        ).mappings().one_or_none()
        if run is None:
            raise RuntimeError("historical_gold_auxiliary_rebuild_changed")
        rows = session.execute(
            text(
                f"SELECT output_fingerprint FROM {table} "
                "WHERE bundle_id = CAST(:bundle_id AS UUID) AND symbol = :symbol "
                "AND ts >= :start AND ts < :end ORDER BY ts"
            ),
            {
                "bundle_id": item.bundle_id,
                "symbol": plan.symbol,
                "start": item.coverage_start,
                "end": item.coverage_end,
            },
        ).mappings().all()
        observed_fingerprint = hashlib.sha256(
            canonical_json_bytes(
                {
                    "git_commit": str(run["git_commit"]),
                    "transform_version": str(run["transform_version"]),
                    "row_fingerprints": [row["output_fingerprint"] for row in rows],
                }
            )
        ).hexdigest()
        if not rows or observed_fingerprint != item.rebuild_output_fingerprint:
            raise RuntimeError("historical_gold_auxiliary_output_fingerprint_mismatch")


def _verify_succeeded_artifact(session, artifact_id: str, plan: HistoricalGoldPlan) -> None:
    row = session.execute(
        text(
            "SELECT a.row_count, a.input_fingerprint, a.output_fingerprint, "
            "COUNT(g.ts) AS actual_rows "
            "FROM meta.historical_research_artifacts a "
            "LEFT JOIN gold.historical_replay_bars g ON g.artifact_id = a.artifact_id "
            "WHERE a.artifact_id = CAST(:artifact_id AS UUID) "
            "GROUP BY a.row_count, a.input_fingerprint, a.output_fingerprint"
        ),
        {"artifact_id": artifact_id},
    ).one()
    expected = _expected_rows(plan.coverage_start, plan.coverage_end, plan.timeframe)
    if int(row.row_count) != expected or int(row.actual_rows) != expected:
        raise RuntimeError("historical_gold_succeeded_artifact_incomplete")
    if not re.fullmatch(r"[0-9a-f]{64}", str(row.output_fingerprint)):
        raise RuntimeError("historical_gold_succeeded_artifact_fingerprint_invalid")
    row_fingerprints = session.execute(
        text(
            "SELECT output_fingerprint FROM gold.historical_replay_bars "
            "WHERE artifact_id = CAST(:artifact_id AS UUID) ORDER BY ts"
        ),
        {"artifact_id": artifact_id},
    ).scalars().all()
    if any(not re.fullmatch(r"[0-9a-f]{64}", str(item)) for item in row_fingerprints):
        raise RuntimeError("historical_gold_succeeded_row_fingerprint_invalid")
    observed = _aggregate_output_fingerprint(
        str(row.input_fingerprint),
        [str(item) for item in row_fingerprints],
    )
    if observed != str(row.output_fingerprint):
        raise RuntimeError("historical_gold_succeeded_artifact_fingerprint_mismatch")


def _aggregate_output_fingerprint(
    input_fingerprint: str,
    row_fingerprints: list[str],
) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "schema": "aats.historical_gold.output.v1",
                "input_fingerprint": input_fingerprint,
                "row_fingerprints": row_fingerprints,
            }
        )
    ).hexdigest()


def _quality_report(
    plan: HistoricalGoldPlan,
    *,
    rows: int,
    funding_events: list[dict[str, Any]],
) -> dict[str, Any]:
    expected = _expected_rows(plan.coverage_start, plan.coverage_end, plan.timeframe)
    reasons: list[str] = []
    if rows != expected:
        reasons.append("bar_count_mismatch")
    if plan.funding is not None and len(funding_events) != plan.funding.source_row_count:
        reasons.append("funding_count_mismatch")
    return {
        "schema": "aats.historical_gold.quality.v1",
        "eligible": not reasons,
        "reason_codes": reasons,
        "expected_rows": expected,
        "actual_rows": rows,
        "funding_events": len(funding_events),
        "coverage_ratio": 0.0 if expected == 0 else rows / expected,
        "no_future_funding": True,
        "no_implicit_interpolation": True,
        "confirmed_candles_only": True,
        "input_bundle_count": len(plan.inputs),
        "input_fingerprint": plan.input_fingerprint,
    }


def _input_identity(item: HistoricalGoldInput) -> dict[str, Any]:
    return {
        "bundle_id": item.bundle_id,
        "purpose": item.purpose,
        "role": item.role,
        "fingerprint": item.fingerprint,
        "dataset_version": item.dataset_version,
        "coverage_start": item.coverage_start,
        "coverage_end": item.coverage_end,
        "source_id": item.source_id,
        "source_key": item.source_key,
        "source_row_count": item.source_row_count,
        "raw_partition_sha256": item.raw_partition_sha256,
        "rebuild_output_fingerprint": item.rebuild_output_fingerprint,
    }


def _validate_window(start: datetime, end: datetime, timeframe: str) -> None:
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise ValueError("historical_gold_window_invalid")
    seconds = (end - start).total_seconds()
    interval = _SUPPORTED_TIMEFRAMES[timeframe].total_seconds()
    if seconds % interval:
        raise ValueError("historical_gold_window_not_timeframe_aligned")


def _validate_auxiliary_coverage(
    inputs: Iterable[HistoricalGoldInput],
    *,
    start: datetime,
    end: datetime,
) -> None:
    """Require each supplied auxiliary role to cover the window without gaps.

    Bulk trade/L2 inputs are intentionally partitioned by day, so requiring one
    bundle to span a multi-day campaign would make the provenance model lie.
    """

    by_role: dict[str, list[HistoricalGoldInput]] = {}
    for item in inputs:
        by_role.setdefault(item.role, []).append(item)
    for role, role_inputs in by_role.items():
        cursor = start
        for item in sorted(role_inputs, key=lambda candidate: candidate.coverage_start):
            scoped_start = max(item.coverage_start, start)
            scoped_end = min(item.coverage_end, end)
            if scoped_end <= scoped_start:
                continue
            if scoped_start > cursor:
                raise ValueError(f"historical_gold_auxiliary_coverage_gap:{role}")
            cursor = max(cursor, scoped_end)
            if cursor >= end:
                break
        if cursor < end:
            raise ValueError(f"historical_gold_auxiliary_coverage_insufficient:{role}")


def _expected_rows(start: datetime, end: datetime, timeframe: str) -> int:
    return int((end - start) / _SUPPORTED_TIMEFRAMES[timeframe])


__all__ = [
    "HistoricalGoldInput",
    "HistoricalGoldPlan",
    "HistoricalGoldResult",
    "TRANSFORM_VERSION",
    "execute_historical_gold",
    "fail_historical_gold",
    "plan_historical_gold",
    "start_historical_gold",
]
