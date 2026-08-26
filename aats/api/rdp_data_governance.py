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


log = logging.getLogger(__name__)
SCHEMA_VERSION = "rdp.data_governance.v1"
_SNAPSHOT_CACHE_SECONDS = 30


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
                output = {
                    "status": "available",
                    "historical_imports": _historical_imports(connection),
                    "live_collection": _live_collection(connection),
                    "archives": _archives(connection),
                    "eligibility": _eligibility(connection),
                    "rebuilds": _rebuilds(connection),
                    "sources": _sources(connection),
                    "gaps": _gaps(connection),
                    "monitoring": _monitoring(connection, root),
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
    return {
        "status": "available",
        "states": [_safe_row(row) for row in rows],
        "bundle_count": sum(int(row["bundle_count"]) for row in rows),
        "next_action": "只让具备来源、覆盖、因果和校验和证据的 bundle 进入研究",
    }


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
        "next_action": "失败运行保持证据；仅从 ELIGIBLE bundle 重建",
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


def _monitoring(connection, root: Path | None) -> dict[str, Any]:
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
            "(SELECT COUNT(*) FROM meta.data_rebuild_runs WHERE status = 'FAILED') AS rebuild_failures, "
            "(SELECT COUNT(*) FROM meta.dataset_bundles) AS bundle_total, "
            "(SELECT COUNT(*) FROM meta.dataset_bundles WHERE status = 'ELIGIBLE') AS bundle_eligible"
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

    bundle_total = int(aggregate["bundle_total"] or 0)
    bundle_eligible = int(aggregate["bundle_eligible"] or 0)
    eligibility_ratio = bundle_eligible / bundle_total if bundle_total else None
    if bundle_total and bundle_eligible < bundle_total:
        alerts.append(_monitor_alert("bundle_ineligible", "warning", "部分历史 bundle 未通过资格门"))
    severities = {item["severity"] for item in alerts}
    status = "critical" if "critical" in severities else "warning" if alerts else "healthy"
    return {
        "status": status,
        "alerts": alerts[:100],
        "alert_count": len(alerts),
        "critical_count": sum(item["severity"] == "critical" for item in alerts),
        "open_gap_count": int(aggregate["open_gaps"] or 0),
        "archive_backlog_count": int(aggregate["archive_backlog"] or 0),
        "eligibility_ratio": eligibility_ratio,
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
