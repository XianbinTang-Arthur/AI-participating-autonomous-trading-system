"""Deterministic, bundle-scoped historical Silver rebuilds."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import text

from aats.data_platform.data_governance.contracts import canonical_json_bytes


TRANSFORM_VERSION = "rdp-historical-silver-v1"
_SUPPORTED_PURPOSES = {"l2_replay", "trade_flow_research"}
_GIT_COMMIT = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


@dataclass(frozen=True)
class HistoricalRebuildPlan:
    operation_key: str
    bundle_id: str
    bundle_fingerprint: str
    purpose: str
    symbol: str
    coverage_start: Any
    coverage_end: Any
    source_id: str
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
            "SELECT bundle_id, fingerprint, purpose, status, coverage_start, "
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
    identity = {
        "bundle_id": str(row["bundle_id"]),
        "bundle_fingerprint": str(row["fingerprint"]),
        "purpose": purpose,
        "symbol": symbol_value,
        "coverage_start": row["coverage_start"],
        "coverage_end": row["coverage_end"],
        "source_id": source_id,
        "source_row_count": source_row_count,
        "raw_partition_sha256": raw_partition_sha256,
        "transform_version": TRANSFORM_VERSION,
        "git_commit": git_commit,
    }
    operation_key = "hist-rebuild-" + hashlib.sha256(
        canonical_json_bytes(identity)
    ).hexdigest()
    return HistoricalRebuildPlan(
        operation_key=operation_key,
        bundle_id=str(row["bundle_id"]),
        bundle_fingerprint=str(row["fingerprint"]),
        purpose=purpose,
        symbol=symbol_value,
        coverage_start=row["coverage_start"],
        coverage_end=row["coverage_end"],
        source_id=source_id,
        source_row_count=source_row_count,
        raw_partition_sha256=raw_partition_sha256,
        transform_version=TRANSFORM_VERSION,
        git_commit=git_commit,
    )


def start_historical_rebuild(session, plan: HistoricalRebuildPlan) -> str:
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
            "scope": json.dumps(asdict(plan), sort_keys=True, default=str),
            "input_fingerprint": plan.bundle_fingerprint,
        },
    ).scalar_one_or_none()
    if inserted is not None:
        return "started"
    existing = session.execute(
        text(
            "SELECT status FROM meta.data_rebuild_runs "
            "WHERE operation_key = :operation_key FOR UPDATE"
        ),
        {"operation_key": plan.operation_key},
    ).scalar_one()
    if existing == "SUCCEEDED":
        return "already_succeeded"
    if existing == "RUNNING":
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
    else:
        rows_read, rows_written, output_table = _rebuild_trade_flow(session, plan)
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
    output_fingerprint = hashlib.sha256(
        canonical_json_bytes(
            {
                "git_commit": plan.git_commit,
                "transform_version": plan.transform_version,
                "row_fingerprints": [row["output_fingerprint"] for row in output_rows],
            }
        )
    ).hexdigest()
    if not output_rows:
        raise RuntimeError("historical_rebuild_produced_no_rows")
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
                       bbo.mid_price_mean, bbo.spread_bps_mean,
                       bbo.top_imbalance_mean,
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
            SELECT *, encode(sha256(convert_to(concat_ws('|', bundle_id::TEXT, symbol, ts::TEXT,
                       bbo_samples_n::TEXT, books5_samples_n::TEXT,
                       mid_price_mean::TEXT, spread_bps_mean::TEXT,
                       top_imbalance_mean::TEXT, max_staleness_ms::TEXT,
                       source_max_ts::TEXT, transform_version), 'UTF8')), 'hex')
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
        {**_scope_params(plan), "transform_version": plan.transform_version},
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
                       SUM(sz) AS total_size,
                       SUM(sz) FILTER (WHERE side = 'buy') AS buy_size,
                       SUM(sz) FILTER (WHERE side = 'sell') AS sell_size,
                       SUM(px * sz) / NULLIF(SUM(sz), 0) AS vwap,
                       (COALESCE(SUM(sz) FILTER (WHERE side = 'buy'), 0) -
                        COALESCE(SUM(sz) FILTER (WHERE side = 'sell'), 0)) /
                           NULLIF(SUM(sz), 0)
                           AS trade_flow_imbalance,
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
                   total_size, COALESCE(buy_size, 0), COALESCE(sell_size, 0),
                   vwap, trade_flow_imbalance, source_max_ts, transform_version,
                   encode(sha256(convert_to(concat_ws('|', bundle_id::TEXT, symbol, bar_ts::TEXT,
                       trade_count::TEXT, buy_count::TEXT, sell_count::TEXT,
                       total_size::TEXT, COALESCE(buy_size, 0)::TEXT,
                       COALESCE(sell_size, 0)::TEXT, vwap::TEXT,
                       trade_flow_imbalance::TEXT, source_max_ts::TEXT,
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
        },
    )
    return rows_read, max(int(result.rowcount or 0), 0), (
        "silver.historical_trade_flow_15m"
    )


def _verify_plan_is_current(session, plan: HistoricalRebuildPlan) -> None:
    row = session.execute(
        text(
            "SELECT fingerprint, status FROM meta.dataset_bundles "
            "WHERE bundle_id = CAST(:bundle_id AS UUID) FOR SHARE"
        ),
        {"bundle_id": plan.bundle_id},
    ).one()
    if row[0] != plan.bundle_fingerprint or row[1] != "ELIGIBLE":
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


def _bundle_symbol_from_key(bundle_id: str, component: dict[str, Any]) -> str:
    symbol = component.get("symbol")
    if isinstance(symbol, str) and symbol:
        return symbol.upper()
    # Registry v1 stores symbol in bundle_key, not the component.  The caller
    # should backfill the explicit field; fail closed rather than guess.
    raise ValueError(f"historical_bundle_symbol_missing:{bundle_id}")


__all__ = [
    "HistoricalRebuildPlan",
    "HistoricalRebuildResult",
    "TRANSFORM_VERSION",
    "execute_historical_rebuild",
    "fail_historical_rebuild",
    "plan_historical_rebuild",
    "start_historical_rebuild",
]
