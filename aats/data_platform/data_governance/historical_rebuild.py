"""Deterministic, bundle-scoped historical Silver rebuilds."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import text

from aats.data_platform.data_governance.contracts import canonical_json_bytes
from aats.data_platform.data_governance.instrument_lineage import (
    instrument_snapshot_scope_reason,
    instrument_snapshot_temporal_evidence_reason,
    load_verified_instrument_contract_snapshot,
)
from aats.domain.instrument_contract_snapshot import (
    InstrumentContractSnapshot,
)
from aats.domain.instrument_scope import (
    INSTRUMENT_SCOPE_UNSUPPORTED_REASON,
    classify_instrument_scope,
)


TRANSFORM_VERSION = "rdp-historical-silver-v2"
_SUPPORTED_PURPOSES = {"l2_replay", "trade_flow_research"}
_GIT_COMMIT = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


@dataclass(frozen=True)
class HistoricalRebuildPlan:
    operation_key: str
    bundle_id: str
    bundle_fingerprint: str
    bundle_key: str
    purpose: str
    symbol: str
    coverage_start: Any
    coverage_end: Any
    source_id: str
    source_key: str
    instrument_snapshot_digest: str | None
    instrument_snapshot_source_id: str | None
    source_row_count: int
    raw_partition_sha256: tuple[str, ...]
    transform_version: str
    git_commit: str


@dataclass(frozen=True)
class HistoricalRebuildResult:
    operation_key: str
    bundle_id: str
    purpose: str
    rows_read: int
    rows_written: int
    output_fingerprint: str
    output_table: str


def plan_historical_rebuild(
    session,
    *,
    bundle_id: str,
    git_commit: str,
) -> HistoricalRebuildPlan:
    if not _GIT_COMMIT.fullmatch(git_commit):
        raise ValueError("historical_rebuild_git_commit_invalid")
    row = session.execute(
        text(
            "SELECT bundle_id, bundle_key, fingerprint, purpose, status, coverage_start, "
            "coverage_end, component_sources FROM meta.dataset_bundles "
            "WHERE bundle_id = CAST(:bundle_id AS UUID)"
        ),
        {"bundle_id": bundle_id},
    ).mappings().one_or_none()
    if row is None:
        raise ValueError("historical_bundle_not_found")
    if str(row["status"]) != "ELIGIBLE":
        raise ValueError("historical_bundle_not_eligible")
    purpose = str(row["purpose"])
    if purpose not in _SUPPORTED_PURPOSES:
        raise ValueError("historical_bundle_purpose_not_rebuildable")
    components = row["component_sources"]
    if isinstance(components, str):
        components = json.loads(components)
    if not isinstance(components, list) or len(components) != 1:
        raise ValueError("historical_bundle_component_shape_invalid")
    component = components[0]
    if not isinstance(component, dict):
        raise ValueError("historical_bundle_component_shape_invalid")
    source_id = str(component.get("source_id") or "")
    provenance = component.get("provenance")
    source_key = str(provenance.get("source_key") or "") if isinstance(provenance, dict) else ""
    if not source_id or not source_key:
        raise ValueError("historical_bundle_component_identity_missing")
    try:
        source_row_count = int(provenance["row_count"])
        raw_partition_sha256 = tuple(
            str(value)
            for value in provenance["gap_manifest"]["raw_partition_sha256"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("historical_bundle_source_material_missing") from exc
    if source_row_count <= 0 or not raw_partition_sha256:
        raise ValueError("historical_bundle_source_material_invalid")
    if any(
        len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
        for value in raw_partition_sha256
    ):
        raise ValueError("historical_bundle_source_hash_invalid")
    symbol_value = _bundle_symbol_from_key(str(row["bundle_id"]), component)
    snapshot_digest, snapshot_source_id = _validated_snapshot_reference(
        session,
        component,
        symbol=symbol_value,
        coverage_start=row["coverage_start"],
        coverage_end=row["coverage_end"],
    )
    operation_key = _rebuild_operation_key(
        bundle_key=str(row["bundle_key"]),
        bundle_fingerprint=str(row["fingerprint"]),
        purpose=purpose,
        symbol=symbol_value,
        coverage_start=row["coverage_start"],
        coverage_end=row["coverage_end"],
        source_key=source_key,
        source_row_count=source_row_count,
        raw_partition_sha256=raw_partition_sha256,
        instrument_snapshot_digest=snapshot_digest,
        transform_version=TRANSFORM_VERSION,
        git_commit=git_commit,
    )
    return HistoricalRebuildPlan(
        operation_key=operation_key,
        bundle_id=str(row["bundle_id"]),
        bundle_fingerprint=str(row["fingerprint"]),
        bundle_key=str(row["bundle_key"]),
        purpose=purpose,
        symbol=symbol_value,
        coverage_start=row["coverage_start"],
        coverage_end=row["coverage_end"],
        source_id=source_id,
        source_key=source_key,
        instrument_snapshot_digest=snapshot_digest,
        instrument_snapshot_source_id=snapshot_source_id,
        source_row_count=source_row_count,
        raw_partition_sha256=raw_partition_sha256,
        transform_version=TRANSFORM_VERSION,
        git_commit=git_commit,
    )


def start_historical_rebuild(session, plan: HistoricalRebuildPlan) -> str:
    # Re-anchor the bundle and contract evidence before inserting or restarting
    # a RUNNING row.  The runner commits this state in a separate transaction,
    # so deferring verification to execute would leave a stale permanent run.
    _verify_plan_is_current(session, plan)
    inserted = session.execute(
        text(
            """
            INSERT INTO meta.data_rebuild_runs (
                operation_key, bundle_id, transform_version, git_commit,
                rebuild_scope, input_fingerprint, status, started_at
            ) VALUES (
                :operation_key, CAST(:bundle_id AS UUID), :transform_version,
                :git_commit, CAST(:scope AS jsonb), :input_fingerprint,
                'RUNNING', NOW()
            ) ON CONFLICT (operation_key) DO NOTHING
            RETURNING rebuild_run_id
            """
        ),
        {
            "operation_key": plan.operation_key,
            "bundle_id": plan.bundle_id,
            "transform_version": plan.transform_version,
            "git_commit": plan.git_commit,
            "scope": json.dumps(_plan_scope_payload(plan), sort_keys=True),
            "input_fingerprint": plan.bundle_fingerprint,
        },
    ).scalar_one_or_none()
    if inserted is not None:
        return "started"
    existing = session.execute(
        text(
            "SELECT bundle_id, rebuild_scope, status, input_fingerprint, "
            "transform_version, git_commit, output_fingerprint "
            "FROM meta.data_rebuild_runs "
            "WHERE operation_key = :operation_key FOR UPDATE"
        ),
        {"operation_key": plan.operation_key},
    ).mappings().one()
    existing_scope = existing["rebuild_scope"]
    if isinstance(existing_scope, str):
        existing_scope = json.loads(existing_scope)
    expected_scope = _plan_scope_payload(plan)
    if (
        str(existing["bundle_id"]) != plan.bundle_id
        or existing_scope != expected_scope
        or str(existing["input_fingerprint"]) != plan.bundle_fingerprint
        or str(existing["transform_version"]) != plan.transform_version
        or str(existing["git_commit"]) != plan.git_commit
    ):
        raise RuntimeError("historical_rebuild_operation_identity_conflict")
    if existing["status"] == "SUCCEEDED":
        _verify_succeeded_rebuild(session, plan, existing)
        return "already_succeeded"
    if existing["status"] == "RUNNING":
        raise RuntimeError("historical_rebuild_already_running")
    session.execute(
        text(
            "UPDATE meta.data_rebuild_runs SET status = 'RUNNING', "
            "started_at = NOW(), ended_at = NULL, error_message = NULL, "
            "rows_read = 0, rows_written = 0, output_fingerprint = NULL, "
            "updated_at = NOW() WHERE operation_key = :operation_key"
        ),
        {"operation_key": plan.operation_key},
    )
    return "started"


def execute_historical_rebuild(
    session,
    plan: HistoricalRebuildPlan,
) -> HistoricalRebuildResult:
    _verify_plan_is_current(session, plan)
    _verify_source_material(session, plan)
    if plan.purpose == "l2_replay":
        rows_read, rows_written, output_table = _rebuild_orderbook(session, plan)
    elif plan.purpose == "trade_flow_research":
        rows_read, rows_written, output_table = _rebuild_trade_flow(session, plan)
    else:  # pragma: no cover - the preflight verifier enforces this first
        raise RuntimeError("historical_bundle_purpose_not_rebuildable")
    output_rows = session.execute(
        text(
            f"SELECT output_fingerprint FROM {output_table} "
            "WHERE bundle_id = CAST(:bundle_id AS UUID) "
            "AND symbol = :symbol AND ts >= :start AND ts < :end "
            "ORDER BY ts"
        ),
        {
            "bundle_id": plan.bundle_id,
            "symbol": plan.symbol,
            "start": plan.coverage_start,
            "end": plan.coverage_end,
        },
    ).mappings().all()
    if not output_rows:
        raise RuntimeError("historical_rebuild_produced_no_rows")
    output_fingerprint = _aggregate_output_fingerprint(
        plan,
        [str(row["output_fingerprint"]) for row in output_rows],
    )
    terminal = session.execute(
        text(
            "UPDATE meta.data_rebuild_runs SET status = 'SUCCEEDED', "
            "rows_read = :rows_read, rows_written = :rows_written, "
            "output_fingerprint = :output_fingerprint, ended_at = NOW(), "
            "error_message = NULL, updated_at = NOW() "
            "WHERE operation_key = :operation_key AND status = 'RUNNING'"
        ),
        {
            "rows_read": rows_read,
            "rows_written": rows_written,
            "output_fingerprint": output_fingerprint,
            "operation_key": plan.operation_key,
        },
    )
    if int(terminal.rowcount or 0) != 1:
        raise RuntimeError("historical_rebuild_terminal_transition_conflict")
    return HistoricalRebuildResult(
        operation_key=plan.operation_key,
        bundle_id=plan.bundle_id,
        purpose=plan.purpose,
        rows_read=rows_read,
        rows_written=rows_written,
        output_fingerprint=output_fingerprint,
        output_table=output_table,
    )


def fail_historical_rebuild(session, operation_key: str, error_type: str) -> None:
    result = session.execute(
        text(
            "UPDATE meta.data_rebuild_runs SET status = 'FAILED', "
            "error_message = :error_type, ended_at = NOW(), updated_at = NOW() "
            "WHERE operation_key = :operation_key AND status = 'RUNNING'"
        ),
        {"operation_key": operation_key, "error_type": error_type},
    )
    if int(result.rowcount or 0) != 1:
        raise RuntimeError("historical_rebuild_failure_transition_conflict")


def _rebuild_orderbook(
    session,
    plan: HistoricalRebuildPlan,
) -> tuple[int, int, str]:
    rows_read = int(
        session.execute(
            text(
                "SELECT ("
                "(SELECT COUNT(*) FROM bronze.historical_orderbook_bbo_1hz "
                " WHERE bundle_id = CAST(:bundle_id AS UUID) AND symbol = :symbol "
                " AND ts >= :start AND ts < :end) + "
                "(SELECT COUNT(*) FROM bronze.historical_orderbook_books5_2hz "
                " WHERE bundle_id = CAST(:bundle_id AS UUID) AND symbol = :symbol "
                " AND ts >= :start AND ts < :end))"
            ),
            _scope_params(plan),
        ).scalar_one()
    )
    _clear_output_scope(
        session,
        "silver.historical_orderbook_metrics_15m",
        plan,
    )
    result = session.execute(
        text(
            """
            WITH bbo AS (
                SELECT date_bin('15 minutes', ts, TIMESTAMPTZ '1970-01-01') AS bar_ts,
                       COUNT(*)::INTEGER AS bbo_samples_n,
                       AVG((bid_px + ask_px) / 2) AS mid_price_mean,
                       AVG(CASE WHEN bid_px + ask_px > 0 THEN
                           (ask_px - bid_px) / ((bid_px + ask_px) / 2) * 10000 END
                       ) AS spread_bps_mean,
                       AVG(CASE WHEN bid_sz + ask_sz > 0 THEN
                           (bid_sz - ask_sz) / (bid_sz + ask_sz) END
                       ) AS top_imbalance_mean,
                       MAX(staleness_ms)::INTEGER AS bbo_max_staleness,
                       MAX(source_state_ts) AS bbo_source_max_ts
                FROM bronze.historical_orderbook_bbo_1hz
                WHERE bundle_id = CAST(:bundle_id AS UUID) AND symbol = :symbol
                  AND ts >= :start AND ts < :end
                GROUP BY bar_ts
            ), books AS (
                SELECT date_bin('15 minutes', ts, TIMESTAMPTZ '1970-01-01') AS bar_ts,
                       COUNT(*)::INTEGER AS books5_samples_n,
                       MAX(staleness_ms)::INTEGER AS books_max_staleness,
                       MAX(source_state_ts) AS books_source_max_ts
                FROM bronze.historical_orderbook_books5_2hz
                WHERE bundle_id = CAST(:bundle_id AS UUID) AND symbol = :symbol
                  AND ts >= :start AND ts < :end
                GROUP BY bar_ts
            ), values_to_write AS (
                SELECT CAST(:bundle_id AS UUID) AS bundle_id, :symbol AS symbol,
                       bbo.bar_ts AS ts, bbo.bbo_samples_n,
                       COALESCE(books.books5_samples_n, 0) AS books5_samples_n,
                       CAST(bbo.mid_price_mean AS NUMERIC(28, 12))
                           AS mid_price_mean,
                       CAST(bbo.spread_bps_mean AS NUMERIC(28, 12))
                           AS spread_bps_mean,
                       CAST(bbo.top_imbalance_mean AS NUMERIC(28, 12))
                           AS top_imbalance_mean,
                       GREATEST(bbo.bbo_max_staleness,
                                COALESCE(books.books_max_staleness, 0)) AS max_staleness_ms,
                       GREATEST(bbo.bbo_source_max_ts,
                                COALESCE(books.books_source_max_ts, bbo.bbo_source_max_ts))
                           AS source_max_ts,
                       :transform_version AS transform_version
                FROM bbo LEFT JOIN books ON books.bar_ts = bbo.bar_ts
            )
            INSERT INTO silver.historical_orderbook_metrics_15m (
                bundle_id, symbol, ts, bbo_samples_n, books5_samples_n,
                mid_price_mean, spread_bps_mean, top_imbalance_mean,
                max_staleness_ms, source_max_ts, transform_version,
                output_fingerprint
            )
            SELECT *, encode(sha256(convert_to(concat_ws('|',
                       :bundle_fingerprint, :instrument_snapshot_digest,
                       symbol,
                       FLOOR(EXTRACT(EPOCH FROM ts) * 1000000)::BIGINT::TEXT,
                       bbo_samples_n::TEXT, books5_samples_n::TEXT,
                       mid_price_mean::TEXT, spread_bps_mean::TEXT,
                       top_imbalance_mean::TEXT, max_staleness_ms::TEXT,
                       FLOOR(EXTRACT(EPOCH FROM source_max_ts) * 1000000)::BIGINT::TEXT,
                       transform_version), 'UTF8')), 'hex')
            FROM values_to_write
            ON CONFLICT (bundle_id, symbol, ts) DO UPDATE SET
                bbo_samples_n = EXCLUDED.bbo_samples_n,
                books5_samples_n = EXCLUDED.books5_samples_n,
                mid_price_mean = EXCLUDED.mid_price_mean,
                spread_bps_mean = EXCLUDED.spread_bps_mean,
                top_imbalance_mean = EXCLUDED.top_imbalance_mean,
                max_staleness_ms = EXCLUDED.max_staleness_ms,
                source_max_ts = EXCLUDED.source_max_ts,
                transform_version = EXCLUDED.transform_version,
                output_fingerprint = EXCLUDED.output_fingerprint,
                updated_at = NOW()
            """
        ),
        {
            **_scope_params(plan),
            "transform_version": plan.transform_version,
            "bundle_fingerprint": plan.bundle_fingerprint,
            "instrument_snapshot_digest": plan.instrument_snapshot_digest,
        },
    )
    return rows_read, max(int(result.rowcount or 0), 0), (
        "silver.historical_orderbook_metrics_15m"
    )


def _rebuild_trade_flow(
    session,
    plan: HistoricalRebuildPlan,
) -> tuple[int, int, str]:
    rows_read = int(
        session.execute(
            text(
                "SELECT COUNT(*) FROM staging.official_trade_history "
                "WHERE source_id = CAST(:source_id AS UUID) AND symbol = :symbol "
                "AND ts >= :start AND ts < :end"
            ),
            {**_scope_params(plan), "source_id": plan.source_id},
        ).scalar_one()
    )
    _clear_output_scope(
        session,
        "silver.historical_trade_flow_15m",
        plan,
    )
    result = session.execute(
        text(
            """
            WITH values_to_write AS (
                SELECT CAST(:bundle_id AS UUID) AS bundle_id, :symbol AS symbol,
                       date_bin('15 minutes', ts, TIMESTAMPTZ '1970-01-01') AS bar_ts,
                       COUNT(*)::INTEGER AS trade_count,
                       COUNT(*) FILTER (WHERE side = 'buy')::INTEGER AS buy_count,
                       COUNT(*) FILTER (WHERE side = 'sell')::INTEGER AS sell_count,
                       CAST(SUM(sz) AS NUMERIC(38, 18)) AS total_size,
                       CAST(
                           COALESCE(SUM(sz) FILTER (WHERE side = 'buy'), 0)
                           AS NUMERIC(38, 18)
                       ) AS buy_size,
                       CAST(
                           COALESCE(SUM(sz) FILTER (WHERE side = 'sell'), 0)
                           AS NUMERIC(38, 18)
                       ) AS sell_size,
                       CAST(
                           SUM(px * sz) / NULLIF(SUM(sz), 0)
                           AS NUMERIC(28, 12)
                       ) AS vwap,
                       CAST(
                           (COALESCE(SUM(sz) FILTER (WHERE side = 'buy'), 0) -
                            COALESCE(SUM(sz) FILTER (WHERE side = 'sell'), 0)) /
                               NULLIF(SUM(sz), 0)
                           AS NUMERIC(28, 12)
                       ) AS trade_flow_imbalance,
                       MAX(ts) AS source_max_ts,
                       :transform_version AS transform_version
                FROM staging.official_trade_history
                WHERE source_id = CAST(:source_id AS UUID) AND symbol = :symbol
                  AND ts >= :start AND ts < :end
                GROUP BY bar_ts
            )
            INSERT INTO silver.historical_trade_flow_15m (
                bundle_id, symbol, ts, trade_count, buy_count, sell_count,
                total_size, buy_size, sell_size, vwap, trade_flow_imbalance,
                source_max_ts, transform_version, output_fingerprint
            )
            SELECT bundle_id, symbol, bar_ts, trade_count, buy_count, sell_count,
                   total_size, buy_size, sell_size,
                   vwap, trade_flow_imbalance, source_max_ts, transform_version,
                   encode(sha256(convert_to(concat_ws('|',
                       :bundle_fingerprint, :instrument_snapshot_digest,
                       symbol,
                       FLOOR(EXTRACT(EPOCH FROM bar_ts) * 1000000)::BIGINT::TEXT,
                       trade_count::TEXT, buy_count::TEXT, sell_count::TEXT,
                       total_size::TEXT, buy_size::TEXT,
                       sell_size::TEXT, vwap::TEXT,
                       trade_flow_imbalance::TEXT,
                       FLOOR(EXTRACT(EPOCH FROM source_max_ts) * 1000000)::BIGINT::TEXT,
                       transform_version), 'UTF8')), 'hex')
            FROM values_to_write
            ON CONFLICT (bundle_id, symbol, ts) DO UPDATE SET
                trade_count = EXCLUDED.trade_count,
                buy_count = EXCLUDED.buy_count,
                sell_count = EXCLUDED.sell_count,
                total_size = EXCLUDED.total_size,
                buy_size = EXCLUDED.buy_size,
                sell_size = EXCLUDED.sell_size,
                vwap = EXCLUDED.vwap,
                trade_flow_imbalance = EXCLUDED.trade_flow_imbalance,
                source_max_ts = EXCLUDED.source_max_ts,
                transform_version = EXCLUDED.transform_version,
                output_fingerprint = EXCLUDED.output_fingerprint,
                updated_at = NOW()
            """
        ),
        {
            **_scope_params(plan),
            "source_id": plan.source_id,
            "transform_version": plan.transform_version,
            "bundle_fingerprint": plan.bundle_fingerprint,
            "instrument_snapshot_digest": plan.instrument_snapshot_digest,
        },
    )
    return rows_read, max(int(result.rowcount or 0), 0), (
        "silver.historical_trade_flow_15m"
    )


def _verify_plan_is_current(session, plan: HistoricalRebuildPlan) -> None:
    row = session.execute(
        text(
            "SELECT bundle_key, fingerprint, purpose, status, coverage_start, "
            "coverage_end, component_sources FROM meta.dataset_bundles "
            "WHERE bundle_id = CAST(:bundle_id AS UUID) FOR SHARE"
        ),
        {"bundle_id": plan.bundle_id},
    ).mappings().one()
    if (
        row["status"] != "ELIGIBLE"
        or str(row["purpose"]) not in _SUPPORTED_PURPOSES
    ):
        raise RuntimeError("historical_bundle_changed_or_ineligible")
    components = row["component_sources"]
    if isinstance(components, str):
        components = json.loads(components)
    if not isinstance(components, list) or len(components) != 1:
        raise RuntimeError("historical_bundle_changed_or_ineligible")
    component = components[0]
    if not isinstance(component, Mapping):
        raise RuntimeError("historical_bundle_changed_or_ineligible")
    provenance = component.get("provenance")
    if not isinstance(provenance, Mapping):
        raise RuntimeError("historical_bundle_changed_or_ineligible")
    try:
        observed_source_id = str(component["source_id"])
        observed_symbol = _bundle_symbol_from_key(plan.bundle_id, dict(component))
        observed_source_key = str(provenance["source_key"])
        observed_row_count = int(provenance["row_count"])
        observed_hashes = tuple(
            str(value)
            for value in provenance["gap_manifest"]["raw_partition_sha256"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("historical_bundle_changed_or_ineligible") from exc
    digest, source_id = _validated_snapshot_reference(
        session,
        component,
        symbol=observed_symbol,
        coverage_start=row["coverage_start"],
        coverage_end=row["coverage_end"],
    )
    expected_operation_key = _rebuild_operation_key(
        bundle_key=str(row["bundle_key"]),
        bundle_fingerprint=str(row["fingerprint"]),
        purpose=str(row["purpose"]),
        symbol=observed_symbol,
        coverage_start=row["coverage_start"],
        coverage_end=row["coverage_end"],
        source_key=observed_source_key,
        source_row_count=observed_row_count,
        raw_partition_sha256=observed_hashes,
        instrument_snapshot_digest=digest,
        transform_version=TRANSFORM_VERSION,
        git_commit=plan.git_commit,
    )
    if (
        str(row["bundle_key"]) != plan.bundle_key
        or str(row["fingerprint"]) != plan.bundle_fingerprint
        or str(row["purpose"]) != plan.purpose
        or observed_symbol != plan.symbol
        or _canonical_time(row["coverage_start"])
        != _canonical_time(plan.coverage_start)
        or _canonical_time(row["coverage_end"]) != _canonical_time(plan.coverage_end)
        or observed_source_id != plan.source_id
        or observed_source_key != plan.source_key
        or observed_row_count != plan.source_row_count
        or observed_hashes != plan.raw_partition_sha256
        or digest != plan.instrument_snapshot_digest
        or source_id != plan.instrument_snapshot_source_id
        or plan.transform_version != TRANSFORM_VERSION
        or not _GIT_COMMIT.fullmatch(plan.git_commit)
        or plan.operation_key != expected_operation_key
    ):
        raise RuntimeError("historical_bundle_changed_or_ineligible")


def _verify_source_material(session, plan: HistoricalRebuildPlan) -> None:
    table = (
        "staging.official_l2_history"
        if plan.purpose == "l2_replay"
        else "staging.official_trade_history"
    )
    row = session.execute(
        text(
            "SELECT COUNT(*) AS row_count, "
            "COALESCE(array_agg(DISTINCT raw_partition_sha256) "
            "FILTER (WHERE raw_partition_sha256 IS NOT NULL), ARRAY[]::VARCHAR[]) "
            f"AS raw_hashes FROM {table} "
            "WHERE source_id = CAST(:source_id AS UUID) AND symbol = :symbol "
            "AND ts >= :start AND ts < :end"
        ),
        {**_scope_params(plan), "source_id": plan.source_id},
    ).mappings().one()
    observed_count = int(row["row_count"])
    observed_hashes = {str(value) for value in row["raw_hashes"]}
    expected_hashes = set(plan.raw_partition_sha256)
    if observed_count != plan.source_row_count:
        raise RuntimeError("historical_bundle_source_row_count_mismatch")
    if observed_hashes != expected_hashes:
        raise RuntimeError("historical_bundle_source_partition_mismatch")


def _verify_succeeded_rebuild(
    session,
    plan: HistoricalRebuildPlan,
    run: Mapping[str, Any],
) -> None:
    _verify_plan_is_current(session, plan)
    _verify_source_material(session, plan)
    if (
        str(run["input_fingerprint"]) != plan.bundle_fingerprint
        or str(run["transform_version"]) != plan.transform_version
        or str(run["git_commit"]) != plan.git_commit
    ):
        raise RuntimeError("historical_rebuild_succeeded_identity_mismatch")
    row_fingerprints = verified_historical_rebuild_output_fingerprints(
        session,
        purpose=plan.purpose,
        bundle_id=plan.bundle_id,
        symbol=plan.symbol,
        coverage_start=plan.coverage_start,
        coverage_end=plan.coverage_end,
        bundle_fingerprint=plan.bundle_fingerprint,
        instrument_snapshot_digest=plan.instrument_snapshot_digest,
    )
    if not row_fingerprints:
        raise RuntimeError("historical_rebuild_succeeded_output_missing")
    observed = _aggregate_output_fingerprint(
        plan,
        row_fingerprints,
    )
    if observed != str(run["output_fingerprint"]):
        raise RuntimeError("historical_rebuild_succeeded_output_fingerprint_mismatch")


def verified_historical_rebuild_output_fingerprints(
    session,
    *,
    purpose: str,
    bundle_id: str,
    symbol: str,
    coverage_start: Any,
    coverage_end: Any,
    bundle_fingerprint: str,
    instrument_snapshot_digest: str | None,
) -> list[str]:
    """Recompute every persisted Silver row fingerprint from business columns."""

    if purpose == "l2_replay":
        table = "silver.historical_orderbook_metrics_15m"
        fingerprint_expression = """
            encode(sha256(convert_to(concat_ws('|',
                :bundle_fingerprint, :instrument_snapshot_digest, symbol,
                FLOOR(EXTRACT(EPOCH FROM ts) * 1000000)::BIGINT::TEXT,
                bbo_samples_n::TEXT, books5_samples_n::TEXT,
                mid_price_mean::TEXT, spread_bps_mean::TEXT,
                top_imbalance_mean::TEXT, max_staleness_ms::TEXT,
                FLOOR(EXTRACT(EPOCH FROM source_max_ts) * 1000000)::BIGINT::TEXT,
                transform_version), 'UTF8')), 'hex')
        """
    elif purpose == "trade_flow_research":
        table = "silver.historical_trade_flow_15m"
        fingerprint_expression = """
            encode(sha256(convert_to(concat_ws('|',
                :bundle_fingerprint, :instrument_snapshot_digest, symbol,
                FLOOR(EXTRACT(EPOCH FROM ts) * 1000000)::BIGINT::TEXT,
                trade_count::TEXT, buy_count::TEXT, sell_count::TEXT,
                total_size::TEXT, buy_size::TEXT, sell_size::TEXT, vwap::TEXT,
                trade_flow_imbalance::TEXT,
                FLOOR(EXTRACT(EPOCH FROM source_max_ts) * 1000000)::BIGINT::TEXT,
                transform_version), 'UTF8')), 'hex')
        """
    else:
        raise ValueError("historical_bundle_purpose_not_rebuildable")
    rows = session.execute(
        text(
            f"SELECT output_fingerprint, {fingerprint_expression} "
            f"AS computed_fingerprint FROM {table} "
            "WHERE bundle_id = CAST(:bundle_id AS UUID) "
            "AND symbol = :symbol AND ts >= :start AND ts < :end ORDER BY ts"
        ),
        {
            "bundle_id": bundle_id,
            "symbol": symbol,
            "start": coverage_start,
            "end": coverage_end,
            "bundle_fingerprint": bundle_fingerprint,
            "instrument_snapshot_digest": instrument_snapshot_digest,
        },
    ).mappings().all()
    fingerprints: list[str] = []
    for row in rows:
        stored = str(row["output_fingerprint"] or "")
        computed = str(row["computed_fingerprint"] or "")
        if not re.fullmatch(r"[0-9a-f]{64}", stored):
            raise RuntimeError("historical_rebuild_succeeded_row_fingerprint_invalid")
        if stored != computed:
            raise RuntimeError("historical_rebuild_succeeded_row_content_mismatch")
        fingerprints.append(computed)
    return fingerprints


def _aggregate_output_fingerprint(
    plan: HistoricalRebuildPlan,
    row_fingerprints: list[str],
) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "schema": "aats.historical_rebuild.output.v2",
                "bundle_fingerprint": plan.bundle_fingerprint,
                "instrument_snapshot_digest": plan.instrument_snapshot_digest,
                "git_commit": plan.git_commit,
                "transform_version": plan.transform_version,
                "row_fingerprints": row_fingerprints,
            }
        )
    ).hexdigest()


def _clear_output_scope(
    session,
    table: str,
    plan: HistoricalRebuildPlan,
) -> None:
    if table not in {
        "silver.historical_orderbook_metrics_15m",
        "silver.historical_trade_flow_15m",
    }:
        raise ValueError("historical_rebuild_output_table_not_allowlisted")
    session.execute(
        text(
            f"DELETE FROM {table} WHERE bundle_id = CAST(:bundle_id AS UUID) "
            "AND symbol = :symbol AND ts >= :start AND ts < :end"
        ),
        _scope_params(plan),
    )


def _scope_params(plan: HistoricalRebuildPlan) -> dict[str, Any]:
    return {
        "bundle_id": plan.bundle_id,
        "symbol": plan.symbol,
        "start": plan.coverage_start,
        "end": plan.coverage_end,
    }


def _canonical_time(value: Any) -> str:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("historical_rebuild_window_invalid") from exc
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("historical_rebuild_window_invalid")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _plan_scope_payload(plan: HistoricalRebuildPlan) -> dict[str, Any]:
    payload = asdict(plan)
    payload["coverage_start"] = _canonical_time(plan.coverage_start)
    payload["coverage_end"] = _canonical_time(plan.coverage_end)
    return payload


def _rebuild_operation_key(
    *,
    bundle_key: str,
    bundle_fingerprint: str,
    purpose: str,
    symbol: str,
    coverage_start: Any,
    coverage_end: Any,
    source_key: str,
    source_row_count: int,
    raw_partition_sha256: tuple[str, ...],
    instrument_snapshot_digest: str | None,
    transform_version: str,
    git_commit: str,
) -> str:
    identity = {
        "schema": "aats.historical_rebuild.identity.v2",
        "bundle_key": bundle_key,
        "bundle_fingerprint": bundle_fingerprint,
        "purpose": purpose,
        "symbol": symbol,
        "coverage_start": _canonical_time(coverage_start),
        "coverage_end": _canonical_time(coverage_end),
        "source_key": source_key,
        "source_row_count": source_row_count,
        "raw_partition_sha256": raw_partition_sha256,
        "instrument_snapshot_digest": instrument_snapshot_digest,
        "transform_version": transform_version,
        "git_commit": git_commit,
    }
    return "hist-rebuild-" + hashlib.sha256(
        canonical_json_bytes(identity)
    ).hexdigest()


def _bundle_symbol_from_key(bundle_id: str, component: dict[str, Any]) -> str:
    symbol = component.get("symbol")
    if isinstance(symbol, str) and symbol:
        return symbol.upper()
    # Registry v1 stores symbol in bundle_key, not the component.  The caller
    # should backfill the explicit field; fail closed rather than guess.
    raise ValueError(f"historical_bundle_symbol_missing:{bundle_id}")


def _validated_snapshot_reference(
    session,
    component: Mapping[str, Any],
    *,
    symbol: str,
    coverage_start: Any,
    coverage_end: Any,
) -> tuple[str | None, str | None]:
    scope = classify_instrument_scope(symbol)
    if scope == "unsupported":
        raise ValueError(INSTRUMENT_SCOPE_UNSUPPORTED_REASON)
    provenance = component.get("provenance")
    raw_snapshot = (
        provenance.get("instrument_contract_snapshot")
        if isinstance(provenance, Mapping)
        else None
    )
    digest = str(component.get("instrument_snapshot_digest") or "")
    source_id = str(component.get("instrument_snapshot_source_id") or "")
    snapshot_material = (raw_snapshot, digest, source_id)
    if scope == "spot" and not any(snapshot_material):
        return None, None
    if (
        not isinstance(raw_snapshot, Mapping)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
        or not source_id
    ):
        if scope == "swap":
            raise ValueError("derivative_instrument_metadata_required")
        raise ValueError("historical_rebuild_instrument_contract_binding_invalid")
    embedded_snapshot = InstrumentContractSnapshot.from_dict(raw_snapshot)
    if instrument_snapshot_scope_reason(embedded_snapshot, symbol=symbol) is not None:
        raise ValueError("historical_rebuild_instrument_contract_binding_invalid")
    registered_snapshot = load_verified_instrument_contract_snapshot(
        session,
        snapshot_source_id=source_id,
    )
    if embedded_snapshot.to_dict() != registered_snapshot.to_dict():
        raise ValueError("instrument_snapshot_source_anchor_mismatch")
    evidence_reason = instrument_snapshot_temporal_evidence_reason(
        registered_snapshot
    )
    if evidence_reason is not None:
        raise ValueError(evidence_reason)
    registered_snapshot.validate_window(
        symbol=symbol,
        start=coverage_start,
        end=coverage_end,
    )
    if registered_snapshot.digest != digest:
        raise ValueError("instrument_snapshot_digest_mismatch")
    return digest, source_id


__all__ = [
    "HistoricalRebuildPlan",
    "HistoricalRebuildResult",
    "TRANSFORM_VERSION",
    "execute_historical_rebuild",
    "fail_historical_rebuild",
    "plan_historical_rebuild",
    "start_historical_rebuild",
    "verified_historical_rebuild_output_fingerprints",
]
