"""Bounded, source-aware RDP data-governance read model."""

from __future__ import annotations

import json
import logging
import shutil
import time
from copy import deepcopy
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from aats.data_platform.db import get_engine
from aats.domain.instrument_scope import (
    INSTRUMENT_SCOPE_UNSUPPORTED_REASON,
    SUPPORTED_SYMBOLS_SPOT,
    SUPPORTED_SYMBOLS_SWAP,
)


log = logging.getLogger(__name__)
SCHEMA_VERSION = "rdp.data_governance.v1"
_SNAPSHOT_CACHE_SECONDS = 30
_CONTRACT_BINDING_POLICY_VERSION = "instrument-contract-binding-v1"
_CONTRACT_ELIGIBILITY_SQL = """
WITH bundle_contract_state AS (
    SELECT
        status,
        eligibility_report,
        component_sources,
        eligibility_report -> 'instrument_contract_binding' AS binding,
        CASE
            WHEN jsonb_typeof(component_sources) = 'array' THEN
                jsonb_array_length(component_sources) > 0
                AND NOT EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements(component_sources) AS component(value)
                    WHERE jsonb_typeof(component.value) <> 'object'
                       OR NULLIF(BTRIM(component.value ->> 'symbol'), '') IS NULL
                )
            ELSE FALSE
        END AS components_well_formed,
        CASE
            WHEN jsonb_typeof(component_sources) = 'array' THEN
                jsonb_array_length(component_sources) > 0
                AND NOT EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements(component_sources) AS component(value)
                    WHERE UPPER(BTRIM(COALESCE(component.value ->> 'symbol', '')))
                          <> ALL(CAST(:supported_spot_symbols AS TEXT[]))
                )
            ELSE FALSE
        END AS all_spot_symbols,
        CASE
            WHEN jsonb_typeof(component_sources) = 'array' THEN
                jsonb_array_length(component_sources) > 0
                AND NOT EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements(component_sources) AS component(value)
                    WHERE UPPER(BTRIM(COALESCE(component.value ->> 'symbol', '')))
                          <> ALL(CAST(:supported_swap_symbols AS TEXT[]))
                )
            ELSE FALSE
        END AS all_swap_symbols
    FROM meta.dataset_bundles
), classified AS (
    SELECT
        *,
        COALESCE(
            status = 'ELIGIBLE'
            AND components_well_formed
            AND jsonb_typeof(binding) = 'object'
            AND binding ->> 'policy_version' = :binding_policy_version
            AND eligibility_report -> 'eligible' = 'true'::jsonb
            AND eligibility_report -> 'reason_codes' = '[]'::jsonb
            AND binding -> 'eligible' = 'true'::jsonb
            AND binding -> 'reason_codes' = '[]'::jsonb
            AND binding ->> 'evidence_fingerprint' ~ '^[0-9a-f]{64}$'
            AND (
                (
                    binding -> 'required' = 'false'::jsonb
                    AND all_spot_symbols
                )
                OR (
                    binding -> 'required' = 'true'::jsonb
                    AND all_swap_symbols
                    AND binding ->> 'snapshot_digest' ~ '^[0-9a-f]{64}$'
                    AND NOT EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements(component_sources) AS component(value)
                        WHERE (component.value ->> 'instrument_snapshot_digest')
                                  IS DISTINCT FROM (binding ->> 'snapshot_digest')
                           OR COALESCE(
                                  component.value ->> 'instrument_snapshot_source_id',
                                  ''
                              ) !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
                           OR jsonb_typeof(
                                  component.value
                                  -> 'provenance'
                                  -> 'instrument_contract_snapshot'
                              ) <> 'object'
                           OR (
                                  component.value
                                  -> 'provenance'
                                  -> 'instrument_contract_snapshot'
                                  ->> 'snapshot_digest'
                              ) IS DISTINCT FROM (binding ->> 'snapshot_digest')
                           OR (
                                  component.value
                                  -> 'provenance'
                                  -> 'instrument_contract_snapshot'
                                  -> 'evidence'
                                  ->> 'kind'
                              ) IS DISTINCT FROM 'observed_forward'
                           OR (
                                  component.value
                                  -> 'provenance'
                                  -> 'instrument_contract_snapshot'
                                  ->> 'schema'
                              ) IS DISTINCT FROM
                                  'aats.instrument_contract_snapshot.v1'
                           OR (
                                  component.value
                                  -> 'provenance'
                                  -> 'instrument_contract_snapshot'
                                  ->> 'arithmetic_policy_id'
                              ) IS DISTINCT FROM 'instrument-arithmetic/v1'
                           OR (
                                  component.value
                                  -> 'provenance'
                                  -> 'instrument_contract_snapshot'
                                  ->> 'venue'
                              ) IS DISTINCT FROM 'OKX'
                           OR (
                                  component.value
                                  -> 'provenance'
                                  -> 'instrument_contract_snapshot'
                                  -> 'instrument'
                                  ->> 'symbol'
                              ) IS DISTINCT FROM (component.value ->> 'symbol')
                           OR NOT EXISTS (
                                SELECT 1
                                FROM meta.data_source_registry AS registry
                                WHERE registry.source_id = CASE
                                          WHEN COALESCE(
                                              component.value
                                              ->> 'instrument_snapshot_source_id',
                                              ''
                                          ) !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
                                              THEN NULL
                                          ELSE (
                                              component.value
                                              ->> 'instrument_snapshot_source_id'
                                          )::uuid
                                      END
                                  AND registry.source_kind = 'okx_rest'
                                  AND registry.provider = 'OKX'
                                  AND registry.truth_tier = 'authoritative_external'
                                  AND registry.source_locator = (
                                      component.value
                                      -> 'provenance'
                                      -> 'instrument_contract_snapshot'
                                      -> 'evidence'
                                      ->> 'source_locator'
                                  )
                                  AND registry.schema_version = (
                                      component.value
                                      -> 'provenance'
                                      -> 'instrument_contract_snapshot'
                                      -> 'evidence'
                                      ->> 'source_schema'
                                  )
                                  AND registry.source_metadata ->> 'record_type' =
                                      'instrument_contract_snapshot_v1'
                                  AND registry.source_metadata -> 'snapshot' =
                                      component.value
                                      -> 'provenance'
                                      -> 'instrument_contract_snapshot'
                            )
                    )
                )
            ),
            FALSE
        ) AS contract_aware_eligible
    FROM bundle_contract_state
)
SELECT
    COUNT(*) AS bundle_total,
    COUNT(*) FILTER (WHERE status = 'ELIGIBLE') AS raw_source_eligible,
    COUNT(*) FILTER (WHERE contract_aware_eligible) AS contract_aware_eligible,
    COUNT(*) FILTER (
        WHERE status = 'ELIGIBLE'
          AND jsonb_typeof(binding) IS DISTINCT FROM 'object'
    ) AS legacy_unbound,
    COUNT(*) FILTER (
        WHERE status = 'ELIGIBLE'
          AND all_swap_symbols
          AND jsonb_typeof(binding) IS DISTINCT FROM 'object'
    ) AS legacy_derivative_unbound,
    COUNT(*) FILTER (
        WHERE status = 'ELIGIBLE'
          AND NOT all_spot_symbols
          AND NOT all_swap_symbols
    ) AS unsupported_instrument_scope,
    COUNT(*) FILTER (
        WHERE status = 'ELIGIBLE'
          AND jsonb_typeof(binding) = 'object'
          AND NOT contract_aware_eligible
    ) AS contract_binding_failed
FROM classified
"""


def build_data_governance_snapshot(root: Path) -> dict[str, Any]:
    """Return one bounded snapshot; never scan raw tick tables on the UI path."""

    resolved = str(root.resolve())
    bucket = int(time.monotonic() // _SNAPSHOT_CACHE_SECONDS)
    return deepcopy(_build_cached_snapshot(resolved, bucket))


@lru_cache(maxsize=16)
def _build_cached_snapshot(root: str, _bucket: int) -> dict[str, Any]:
    """Bound repeated workspace polling while retaining a short freshness SLA."""

    project_root = Path(root)
    coverage = _load_latest_coverage(project_root)
    database = _database_projection(project_root)
    status = "ready"
    reasons: list[str] = []
    if coverage.get("status") != "available":
        status = "unknown"
        reasons.append("coverage_snapshot_unavailable")
    if database.get("status") != "available":
        status = "unknown"
        reasons.append("data_governance_database_unavailable")
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cache_ttl_seconds": _SNAPSHOT_CACHE_SECONDS,
        "status": status,
        "reason_codes": reasons,
        "coverage": coverage,
        "historical_imports": database.get("historical_imports", _unknown_view()),
        "live_collection": database.get("live_collection", _unknown_view()),
        "archives": database.get("archives", _unknown_view()),
        "eligibility": database.get("eligibility", _unknown_view()),
        "rebuilds": database.get("rebuilds", _unknown_view()),
        "sources": database.get("sources", _unknown_view()),
        "gaps": database.get("gaps", _unknown_view()),
        "monitoring": database.get("monitoring", _unknown_view()),
        "safety": {
            "raw_table_scan_on_request": False,
            "contains_database_url": False,
            "live_actions_available": False,
            "parameter_apply_available": False,
        },
    }


def _load_latest_coverage(root: Path) -> dict[str, Any]:
    directory = (root / "artifacts/data_governance/coverage").resolve()
    try:
        candidates = sorted(directory.glob("coverage_*.json"), reverse=True)
        if not candidates:
            return {
                "status": "unknown",
                "reason_code": "coverage_snapshot_missing",
                "next_action": "运行只读数据覆盖审计",
            }
        path = candidates[0].resolve()
        if directory not in path.parents or path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError("coverage_snapshot_path_or_size_invalid")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != "rdp-coverage-report-v1":
            raise ValueError("coverage_snapshot_schema_incompatible")
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        tables = payload.get("tables") if isinstance(payload.get("tables"), list) else []
        recovery_matrix = (
            payload.get("recovery_matrix")
            if isinstance(payload.get("recovery_matrix"), list)
            else []
        )
        return {
            "status": "available",
            "snapshot_id": path.stem,
            "queried_at": payload.get("queried_at"),
            "window": payload.get("window"),
            "algorithm_version": payload.get("algorithm_version"),
            "result_fingerprint_sha256": payload.get("result_fingerprint_sha256"),
            "summary": summary,
            "table_count": len(payload.get("tables") or []),
            "datasets": [_coverage_dataset(item) for item in tables[:150] if isinstance(item, dict)],
            "recovery_matrix": [
                _recovery_item(item)
                for item in recovery_matrix[:150]
                if isinstance(item, dict)
            ],
            "next_action": (
                "处理缺失或审计失败的数据集"
                if summary.get("missing")
                or summary.get("audit_failed")
                or summary.get("observed_with_quality_issues")
                else "保持覆盖审计与采集连续性"
            ),
        }
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        log.warning("RDP coverage snapshot unavailable: %s", type(exc).__name__)
        return {
            "status": "unknown",
            "reason_code": type(exc).__name__,
            "next_action": "重新生成并校验只读覆盖快照",
        }


def _database_projection(root: Path | None = None) -> dict[str, Any]:
    try:
        engine = get_engine()
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                if engine.dialect.name == "postgresql":
                    connection.execute(text("SET TRANSACTION READ ONLY"))
                    connection.execute(
                        text("SELECT set_config('statement_timeout', '3000ms', true)")
                    )
                eligibility = _eligibility(connection)
                output = {
                    "status": "available",
                    "historical_imports": _historical_imports(connection),
                    "live_collection": _live_collection(connection),
                    "archives": _archives(connection),
                    "eligibility": eligibility,
                    "rebuilds": _rebuilds(connection),
                    "sources": _sources(connection),
                    "gaps": _gaps(connection),
                    "monitoring": _monitoring(
                        connection,
                        root,
                        eligibility=eligibility,
                    ),
                }
            finally:
                transaction.rollback()
        return output
    except (SQLAlchemyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        log.warning("RDP data-governance projection unavailable: %s", type(exc).__name__)
        return {"status": "unknown", "reason_code": type(exc).__name__}


def _historical_imports(connection) -> dict[str, Any]:
    rows = connection.execute(
        text(
            "SELECT ingest_run_id, dataset_domain, symbol, timeframe, status, "
            "started_at, ended_at, (error_message IS NOT NULL) AS has_error "
            "FROM meta.ingest_runs "
            "WHERE run_type = 'backfill' ORDER BY started_at DESC LIMIT 20"
        )
    ).mappings().all()
    return {
        "status": "available",
        "recent": [_safe_row(row) for row in rows],
        "total_recent": len(rows),
        "next_action": "先按 1 个 UTC 日受控导入，再评估 30/90 日扩展",
    }


def _live_collection(connection) -> dict[str, Any]:
    rows = connection.execute(
        text(
            "SELECT collector, channel, symbol, MAX(event_ts) AS latest_event_ts, "
            "COUNT(*) FILTER (WHERE event_type = 'DROP') AS drop_count, "
            "COUNT(*) FILTER (WHERE event_type = 'DISCONNECT') AS disconnect_count "
            "FROM meta.collector_continuity_events "
            "WHERE event_ts >= NOW() - interval '24 hours' "
            "GROUP BY collector, channel, symbol "
            "ORDER BY collector, channel, symbol LIMIT 100"
        )
    ).mappings().all()
    items = [_safe_row(row) for row in rows]
    return {
        "status": "available" if items else "unknown",
        "channels": items,
        "channel_count": len(items),
        "drop_count": sum(int(item.get("drop_count") or 0) for item in items),
        "next_action": "调查 drop、断连与未上报频道" if items else "等待真实采集连续性证据",
    }


def _archives(connection) -> dict[str, Any]:
    rows = connection.execute(
        text(
            "SELECT state, COUNT(*) AS partition_count, COALESCE(SUM(row_count), 0) AS row_count "
            "FROM meta.archive_partitions GROUP BY state ORDER BY state"
        )
    ).mappings().all()
    counts = {str(row["state"]): int(row["partition_count"]) for row in rows}
    return {
        "status": "available",
        "states": counts,
        "partition_count": sum(counts.values()),
        "blocked_count": counts.get("FAILED", 0) + counts.get("DISCOVERED", 0),
        "next_action": "先修复未验证归档；retention 保持阻断" if counts.get("FAILED") else "保持 archive-before-delete",
    }


def _eligibility(connection) -> dict[str, Any]:
    rows = connection.execute(
        text(
            "SELECT eligibility_mode, status, COUNT(*) AS bundle_count "
            "FROM meta.dataset_bundles GROUP BY eligibility_mode, status "
            "ORDER BY eligibility_mode, status"
        )
    ).mappings().all()
    aggregate = connection.execute(
        text(_CONTRACT_ELIGIBILITY_SQL),
        {
            "binding_policy_version": _CONTRACT_BINDING_POLICY_VERSION,
            "supported_spot_symbols": list(SUPPORTED_SYMBOLS_SPOT),
            "supported_swap_symbols": list(SUPPORTED_SYMBOLS_SWAP),
        },
    ).mappings().one()
    bundle_count = int(aggregate["bundle_total"] or 0)
    raw_eligible = int(aggregate["raw_source_eligible"] or 0)
    contract_eligible = int(aggregate["contract_aware_eligible"] or 0)
    legacy_unbound = int(aggregate["legacy_unbound"] or 0)
    derivative_unbound = int(aggregate["legacy_derivative_unbound"] or 0)
    unsupported_scope = int(aggregate.get("unsupported_instrument_scope") or 0)
    binding_failed = int(aggregate["contract_binding_failed"] or 0)
    raw_eligible_but_contract_blocked = max(raw_eligible - contract_eligible, 0)

    reason_codes: list[str] = []
    if not bundle_count:
        status = "unknown"
        reason_codes.append("dataset_bundle_evidence_missing")
    elif raw_eligible_but_contract_blocked:
        status = "blocked"
        if unsupported_scope:
            reason_codes.append(INSTRUMENT_SCOPE_UNSUPPORTED_REASON)
        if derivative_unbound:
            reason_codes.append("legacy_derivative_contract_metadata_unbound")
        if legacy_unbound:
            reason_codes.append("instrument_contract_binding_report_missing")
        if binding_failed:
            reason_codes.append("instrument_contract_binding_failed")
    elif contract_eligible:
        status = "available"
    else:
        status = "blocked"
        reason_codes.append("no_contract_aware_eligible_bundle")

    raw_ratio = raw_eligible / bundle_count if bundle_count else None
    contract_ratio = contract_eligible / bundle_count if bundle_count else None
    return {
        "status": status,
        "reason_codes": reason_codes,
        "states": [_safe_row(row) for row in rows],
        "bundle_count": bundle_count,
        "raw_source_eligible_bundle_count": raw_eligible,
        "contract_aware_eligible_bundle_count": contract_eligible,
        "research_usable": False,
        "eligibility_scope": "dataset_bundle_only",
        "raw_source": {
            "status": "available",
            "eligible_bundle_count": raw_eligible,
            "ineligible_bundle_count": max(bundle_count - raw_eligible, 0),
            "eligibility_ratio": raw_ratio,
            "supports_monetary_research": False,
            "meaning": "仅证明既有来源、覆盖、因果与校验和资格，不证明衍生品金额单位正确",
        },
        "contract_aware": {
            "status": status,
            "policy_version": _CONTRACT_BINDING_POLICY_VERSION,
            "research_usable": False,
            "contract_bound_bundle_available": (
                status == "available" and contract_eligible > 0
            ),
            "supports_monetary_research": False,
            "eligible_bundle_count": contract_eligible,
            "blocked_bundle_count": max(bundle_count - contract_eligible, 0),
            "raw_eligible_but_contract_blocked_count": (
                raw_eligible_but_contract_blocked
            ),
            "legacy_unbound_bundle_count": legacy_unbound,
            "legacy_derivative_unbound_bundle_count": derivative_unbound,
            "unsupported_instrument_scope_bundle_count": unsupported_scope,
            "binding_failed_bundle_count": binding_failed,
            "eligibility_ratio": contract_ratio,
            "reason_codes": reason_codes,
        },
        "next_action": _eligibility_next_action(
            status=status,
            derivative_unbound=derivative_unbound,
        ),
    }


def _eligibility_next_action(*, status: str, derivative_unbound: int) -> str:
    if derivative_unbound:
        return (
            "旧 ELIGIBLE 只代表来源/覆盖资格；衍生品必须绑定可校验的 "
            "instrument snapshot 后按新版本重建"
        )
    if status == "blocked":
        return "修复合约绑定或基础资格失败后，重新生成 bundle；禁止沿用旧收益结论"
    if status == "unknown":
        return "生成可验证的 bundle，并分别核对来源资格与合约感知资格"
    return (
        "contract-aware eligible bundle 仅可进入后续受控重建；"
        "完成下游金额与 artifact 门禁前仍不得形成收益结论"
    )


def _rebuilds(connection) -> dict[str, Any]:
    rows = connection.execute(
        text(
            "SELECT rebuild_run_id, bundle_id, transform_version, status, "
            "rows_read, rows_written, started_at, ended_at, "
            "(error_message IS NOT NULL) AS has_error "
            "FROM meta.data_rebuild_runs ORDER BY created_at DESC LIMIT 20"
        )
    ).mappings().all()
    return {
        "status": "available",
        "recent": [_safe_row(row) for row in rows],
        "total_recent": len(rows),
        "next_action": "失败运行保持证据；仅从 contract-aware eligible bundle 重建",
    }


def _sources(connection) -> dict[str, Any]:
    rows = connection.execute(
        text(
            "SELECT source_kind, truth_tier, COUNT(*) AS source_count "
            "FROM meta.data_source_registry GROUP BY source_kind, truth_tier "
            "ORDER BY source_kind, truth_tier"
        )
    ).mappings().all()
    return {
        "status": "available",
        "states": [_safe_row(row) for row in rows],
        "source_count": sum(int(row["source_count"]) for row in rows),
    }


def _gaps(connection) -> dict[str, Any]:
    rows = connection.execute(
        text(
            "SELECT status, classification, COUNT(*) AS gap_count "
            "FROM meta.data_gap_records GROUP BY status, classification "
            "ORDER BY status, classification"
        )
    ).mappings().all()
    return {
        "status": "available",
        "states": [_safe_row(row) for row in rows],
        "gap_count": sum(int(row["gap_count"]) for row in rows),
    }


def _monitoring(
    connection,
    root: Path | None,
    *,
    eligibility: dict[str, Any] | None = None,
) -> dict[str, Any]:
    continuity = connection.execute(
        text(
            "SELECT collector, channel, symbol, MAX(event_ts) AS latest_event_ts, "
            "EXTRACT(EPOCH FROM (NOW() - MAX(event_ts))) AS age_seconds, "
            "COUNT(*) FILTER (WHERE event_type = 'DROP') AS drop_count, "
            "COUNT(*) FILTER (WHERE event_type = 'DISCONNECT') AS disconnect_count "
            "FROM meta.collector_continuity_events "
            "WHERE event_ts >= NOW() - interval '24 hours' "
            "GROUP BY collector, channel, symbol ORDER BY collector, channel, symbol LIMIT 100"
        )
    ).mappings().all()
    aggregate = connection.execute(
        text(
            "SELECT "
            "(SELECT COUNT(*) FROM meta.data_gap_records WHERE status IN ('OPEN','CLASSIFIED')) AS open_gaps, "
            "(SELECT COUNT(*) FROM meta.archive_partitions WHERE state IN ('DISCOVERED','ARCHIVING')) AS archive_backlog, "
            "(SELECT COUNT(*) FROM meta.archive_partitions WHERE state = 'FAILED') AS archive_failures, "
            "(SELECT COUNT(*) FROM meta.data_rebuild_runs WHERE status = 'FAILED') AS rebuild_failures"
        )
    ).mappings().one()
    alerts: list[dict[str, str]] = []
    if not continuity:
        alerts.append(_monitor_alert("collector_continuity_unknown", "warning", "尚无采集连续性证据"))
    for row in continuity:
        age = float(row.get("age_seconds") or 0)
        drops = int(row.get("drop_count") or 0)
        disconnects = int(row.get("disconnect_count") or 0)
        scope = f"{row['collector']} / {row['channel']} / {row['symbol']}"
        if age > 120:
            alerts.append(_monitor_alert("collector_channel_stale", "critical", f"频道陈旧：{scope}"))
        if drops:
            alerts.append(_monitor_alert("collector_drop_detected", "critical", f"检测到丢弃：{scope}"))
        if disconnects:
            alerts.append(
                _monitor_alert(
                    "collector_disconnect_detected",
                    "critical",
                    f"检测到断连：{scope}",
                )
            )
    if int(aggregate["archive_failures"] or 0):
        alerts.append(_monitor_alert("archive_verification_failed", "critical", "存在归档失败，retention 必须保持阻断"))
    if int(aggregate["archive_backlog"] or 0):
        alerts.append(_monitor_alert("archive_backlog_present", "warning", "存在尚未验证的归档积压"))
    if int(aggregate["open_gaps"] or 0):
        alerts.append(_monitor_alert("open_data_gaps", "warning", "存在尚未关闭的数据缺口"))
    if int(aggregate["rebuild_failures"] or 0):
        alerts.append(_monitor_alert("rebuild_failed", "warning", "存在失败的历史重建运行"))

    disk: dict[str, Any] = {"status": "unknown"}
    if root is not None:
        try:
            usage = shutil.disk_usage(root.resolve())
            ratio = usage.free / usage.total if usage.total else 0.0
            disk = {
                "status": "available",
                "scope": "workspace_filesystem_proxy",
                "free_bytes": usage.free,
                "total_bytes": usage.total,
                "free_ratio": round(ratio, 6),
            }
            if usage.free < 5 * 1024**3 or ratio < 0.10:
                alerts.append(
                    _monitor_alert(
                        "disk_space_critical",
                        "critical",
                        "工作区所在文件系统空间低于安全水位；归档卷仍需独立核对",
                    )
                )
            elif ratio < 0.20:
                alerts.append(
                    _monitor_alert(
                        "disk_space_warning",
                        "warning",
                        "工作区所在文件系统空间接近安全水位；归档卷仍需独立核对",
                    )
                )
        except OSError:
            alerts.append(
                _monitor_alert(
                    "disk_space_unknown",
                    "warning",
                    "无法验证工作区文件系统空间；归档卷状态未知",
                )
            )

    eligibility_view = eligibility or _eligibility(connection)
    bundle_total = int(eligibility_view.get("bundle_count") or 0)
    raw_view = eligibility_view.get("raw_source")
    contract_view = eligibility_view.get("contract_aware")
    raw_view = raw_view if isinstance(raw_view, dict) else {}
    contract_view = contract_view if isinstance(contract_view, dict) else {}
    raw_eligible = int(raw_view.get("eligible_bundle_count") or 0)
    raw_ratio = raw_view.get("eligibility_ratio")
    contract_ratio = contract_view.get("eligibility_ratio")
    derivative_unbound = int(
        contract_view.get("legacy_derivative_unbound_bundle_count") or 0
    )
    binding_failed = int(contract_view.get("binding_failed_bundle_count") or 0)
    raw_contract_blocked = int(
        contract_view.get("raw_eligible_but_contract_blocked_count") or 0
    )
    if bundle_total and raw_eligible < bundle_total:
        alerts.append(_monitor_alert("bundle_ineligible", "warning", "部分历史 bundle 未通过资格门"))
    if derivative_unbound:
        alerts.append(
            _monitor_alert(
                "derivative_bundle_contract_metadata_unbound",
                "critical",
                "旧衍生品 bundle 缺少可校验的 instrument snapshot，禁止用于金额、损益或收益结论",
            )
        )
    elif binding_failed:
        alerts.append(
            _monitor_alert(
                "bundle_contract_binding_failed",
                "critical",
                "bundle 合约快照、摘要或来源锚点校验失败，禁止用于研究结论",
            )
        )
    elif raw_contract_blocked:
        alerts.append(
            _monitor_alert(
                "bundle_contract_binding_blocked",
                "warning",
                "部分来源资格通过的 bundle 未通过合约感知资格门",
            )
        )
    severities = {item["severity"] for item in alerts}
    status = "critical" if "critical" in severities else "warning" if alerts else "healthy"
    return {
        "status": status,
        "alerts": alerts[:100],
        "alert_count": len(alerts),
        "critical_count": sum(item["severity"] == "critical" for item in alerts),
        "open_gap_count": int(aggregate["open_gaps"] or 0),
        "archive_backlog_count": int(aggregate["archive_backlog"] or 0),
        "eligibility_ratio": raw_ratio,
        "contract_aware_eligibility_ratio": contract_ratio,
        "disk": disk,
        "next_action": "先处理 critical 告警；禁止绕过数据资格与 retention 门" if alerts else "保持连续性、归档和磁盘监控",
    }


def _monitor_alert(code: str, severity: str, message: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "message": message}


def _safe_row(row) -> dict[str, Any]:
    return {
        str(key): (
            value.astimezone(timezone.utc).isoformat()
            if isinstance(value, datetime)
            else str(value) if key.endswith("_id") and value is not None else value
        )
        for key, value in dict(row).items()
        if key not in {"database_url", "dsn", "source_locator", "source_metadata"}
    }


def _unknown_view() -> dict[str, Any]:
    return {"status": "unknown", "next_action": "等待可验证的数据治理快照"}


def _coverage_dataset(item: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "table",
        "status",
        "window_start",
        "window_end",
        "row_count",
        "earliest_ts",
        "latest_ts",
        "dataset_versions",
        "ingest_runs",
        "duplicate_natural_keys",
        "missing_intervals",
        "unconfirmed_rows",
        "quality_flagged_rows",
        "symbol_windows",
        "symbol_windows_truncated",
        "error_code",
    )
    return {key: item.get(key) for key in keys}


def _recovery_item(item: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "dataset",
        "observed_status",
        "classification",
        "owner",
        "priority",
        "next_action",
        "capacity_status",
        "stop_condition",
    )
    return {key: item.get(key) for key in keys}


__all__ = ["SCHEMA_VERSION", "build_data_governance_snapshot"]
