"""Read-only, bounded RDP data coverage audit."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import Engine, PrimaryKeyConstraint, UniqueConstraint, inspect, text

from aats.data_platform.rdp_models import RdpBase


ALGORITHM_VERSION = "rdp-coverage-v4"
_TIME_COLUMNS = (
    "ts",
    "event_ts",
    "bar_start_ts",
    "window_start_ts",
    "coverage_start",
    "gap_start",
    "started_at",
    "occurred_at",
    "detected_at",
    "accessed_at",
    "applied_at",
    "generated_at",
    "created_at",
)
_TIMEFRAME_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}


@dataclass(frozen=True)
class TableCoverage:
    table: str
    status: str
    time_column: str | None
    window_start: str
    window_end: str
    row_count: int | None
    earliest_ts: str | None
    latest_ts: str | None
    symbols: int | None
    dataset_versions: int | None
    ingest_runs: int | None
    duplicate_natural_keys: int | None
    missing_intervals: int | None
    null_symbol_rows: int | None
    unconfirmed_rows: int | None
    quality_flagged_rows: int | None
    estimated_total_rows: int | None
    symbol_windows: tuple[dict[str, Any], ...] | None
    symbol_windows_truncated: bool | None
    error_code: str | None = None


def database_fingerprint(engine: Engine) -> str:
    url = engine.url
    identity = {
        "driver": url.drivername,
        "host": url.host or "",
        "port": url.port,
        "database": url.database or "",
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def git_commit(project_root: str = ".") -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", project_root, "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def audit_coverage(
    engine: Engine,
    *,
    window_end: datetime | None = None,
    window_days: int = 90,
    statement_timeout_ms: int = 15_000,
    project_root: str = ".",
) -> dict[str, Any]:
    """Inspect every modeled RDP table without DDL or data mutation.

    Exact counts are bounded to ``[window_start, window_end)``. Tables without
    a modeled timestamp use PostgreSQL planner estimates instead of unbounded
    ``COUNT(*)`` scans.
    """

    if window_days <= 0:
        raise ValueError("window_days_must_be_positive")
    if statement_timeout_ms <= 0:
        raise ValueError("statement_timeout_ms_must_be_positive")
    end = (window_end or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start = end - timedelta(days=window_days)
    inspector = inspect(engine)
    results: list[TableCoverage] = []

    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            if engine.dialect.name == "postgresql":
                connection.execute(
                    text(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                    )
                )
                connection.execute(
                    text("SELECT set_config('statement_timeout', :timeout, true)"),
                    {"timeout": f"{statement_timeout_ms}ms"},
                )
            for table in sorted(RdpBase.metadata.tables.values(), key=lambda item: item.fullname):
                savepoint = (
                    connection.begin_nested()
                    if engine.dialect.name == "postgresql"
                    else None
                )
                result = _audit_table(
                    connection,
                    inspector,
                    table.schema or "public",
                    table.name,
                    start,
                    end,
                )
                if savepoint is not None and result.status == "audit_failed":
                    savepoint.rollback()
                elif savepoint is not None:
                    savepoint.commit()
                results.append(result)
        finally:
            transaction.rollback()

    measured = [asdict(item) for item in results]
    result_fingerprint = hashlib.sha256(
        json.dumps(measured, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    report = {
        "schema_version": "rdp-coverage-report-v1",
        "algorithm_version": ALGORITHM_VERSION,
        "queried_at": datetime.now(timezone.utc).isoformat(),
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "database_fingerprint_sha256": database_fingerprint(engine),
        "git_commit": git_commit(project_root),
        "read_only": True,
        "result_fingerprint_sha256": result_fingerprint,
        "tables": measured,
        "summary": _summarize(results),
    }
    report["recovery_matrix"] = build_recovery_matrix(measured)
    return report


def build_recovery_matrix(tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Classify observed deficits without claiming data has been recovered."""

    matrix = []
    for item in tables:
        status = str(item.get("status") or "unknown")
        if status in {
            "observed",
            "present_unbounded_not_scanned",
            "zero_event_with_healthy_collector",
        }:
            continue
        table = str(item.get("table") or "")
        classification, owner, next_action = _recovery_route(table, status)
        matrix.append(
            {
                "dataset": table,
                "observed_status": status,
                "classification": classification,
                "owner": owner,
                "priority": "P0" if status in {"audit_failed", "collector_unknown"} else "P1",
                "next_action": next_action,
                "capacity_status": (
                    "not_applicable"
                    if classification in {"deterministic_rebuild", "cannot_recover"}
                    else "unknown_requires_one_utc_day_sample"
                ),
                "stop_condition": (
                    "禁止下载 30/90 日数据，直到 1 个 UTC 日样本的字节数、"
                    "行数、解析耗时和数据库增长均已测量"
                    if classification in {"official_backfill", "prospective_only"}
                    else "输入来源或精确 lineage 不可验证时保持失败关闭"
                ),
            }
        )
    return matrix


def _recovery_route(table: str, status: str) -> tuple[str, str, str]:
    if status == "audit_failed":
        return (
            "cannot_classify_until_audit_succeeds",
            "data-platform",
            "修复只读审计错误后重新分类；当前不得补数或重建",
        )
    if table.startswith(("silver.", "gold.")):
        return (
            "deterministic_rebuild",
            "rdp-rebuild",
            "从 ELIGIBLE dataset bundle 确定性重建并核对 fingerprint",
        )
    if table.startswith("research.") or "lineage" in table or "intent" in table:
        return (
            "cannot_recover",
            "research-governance",
            "保留 unattributable/unknown；只允许未来新事实形成精确 lineage",
        )
    if table in {
        "bronze.market_trades",
        "bronze.market_orderbook_bbo",
        "bronze.market_orderbook_books5",
        "staging.market_oi_funding_ticks",
        "staging.raw_liquidations",
    }:
        return (
            "prospective_only",
            "live-collectors",
            "等待真实连续采集；不得用代理或插值冒充历史观测",
        )
    if table.startswith(("staging.", "bronze.")):
        return (
            "official_backfill",
            "historical-import",
            "先验证官方来源能力，再执行 1 日样本和分级扩展",
        )
    return (
        "cannot_classify_until_audit_succeeds",
        "data-platform",
        "核对表级语义和来源后人工分类",
    )


def _audit_table(connection, inspector, schema: str, table: str, start: datetime, end: datetime) -> TableCoverage:
    qualified = f"{schema}.{table}"
    if not inspector.has_table(table, schema=schema):
        return _empty(qualified, "missing", start, end, error_code="table_missing")
    columns = {column["name"] for column in inspector.get_columns(table, schema=schema)}
    time_column = next((candidate for candidate in _TIME_COLUMNS if candidate in columns), None)
    estimate = _estimated_rows(connection, schema, table)
    if time_column is None:
        return TableCoverage(
            table=qualified,
            status="present_unbounded_not_scanned",
            time_column=None,
            window_start=start.isoformat(),
            window_end=end.isoformat(),
            row_count=None,
            earliest_ts=None,
            latest_ts=None,
            symbols=None,
            dataset_versions=None,
            ingest_runs=None,
            duplicate_natural_keys=None,
            missing_intervals=None,
            null_symbol_rows=None,
            unconfirmed_rows=None,
            quality_flagged_rows=None,
            estimated_total_rows=estimate,
            symbol_windows=None,
            symbol_windows_truncated=None,
        )

    table_sql = _quote_qualified(schema, table)
    time_sql = _quote_identifier(time_column)
    expressions = [
        "COUNT(*) AS row_count",
        f"MIN({time_sql}) AS earliest_ts",
        f"MAX({time_sql}) AS latest_ts",
        "COUNT(DISTINCT symbol) AS symbols" if "symbol" in columns else "NULL AS symbols",
        "COUNT(DISTINCT dataset_version) AS dataset_versions" if "dataset_version" in columns else "NULL AS dataset_versions",
        "COUNT(DISTINCT ingest_run_id) AS ingest_runs" if "ingest_run_id" in columns else "NULL AS ingest_runs",
        "COUNT(*) FILTER (WHERE symbol IS NULL) AS null_symbol_rows" if "symbol" in columns else "NULL AS null_symbol_rows",
        "COUNT(*) FILTER (WHERE confirm IS FALSE) AS unconfirmed_rows" if "confirm" in columns else "NULL AS unconfirmed_rows",
        "COUNT(*) FILTER (WHERE cardinality(quality_flags) > 0) AS quality_flagged_rows" if "quality_flags" in columns else "NULL AS quality_flagged_rows",
    ]
    try:
        row = connection.execute(
            text(
                f"SELECT {', '.join(expressions)} FROM {table_sql} "
                f"WHERE {time_sql} >= :start AND {time_sql} < :end"
            ),
            {"start": start, "end": end},
        ).mappings().one()
        modeled_table = RdpBase.metadata.tables.get(qualified)
        natural_key = _natural_key_columns(modeled_table, columns, time_column)
        duplicates = (
            0
            if _database_enforces_unique_key(modeled_table, natural_key)
            else _duplicate_count(
                connection,
                table_sql,
                time_sql,
                natural_key,
                start,
                end,
            )
        )
        gaps = _gap_count(
            connection,
            table_sql,
            time_sql,
            table,
            columns,
            start,
            end,
        )
        symbol_windows, symbol_windows_truncated = _symbol_windows(
            connection,
            table_sql,
            time_sql,
            columns,
            start,
            end,
        )
    except Exception as exc:
        return _empty(
            qualified,
            "audit_failed",
            start,
            end,
            time_column=time_column,
            estimate=estimate,
            error_code=type(exc).__name__,
        )
    count = int(row["row_count"])
    status = _coverage_status(
        count=count,
        duplicates=duplicates,
        missing_intervals=gaps,
        null_symbol_rows=_optional_int(row["null_symbol_rows"]),
        unconfirmed_rows=_optional_int(row["unconfirmed_rows"]),
        quality_flagged_rows=_optional_int(row["quality_flagged_rows"]),
        zero_status=(
            _zero_status(connection, qualified, start, end)
            if count == 0
            else None
        ),
    )
    return TableCoverage(
        table=qualified,
        status=status,
        time_column=time_column,
        window_start=start.isoformat(),
        window_end=end.isoformat(),
        row_count=count,
        earliest_ts=_iso(row["earliest_ts"]),
        latest_ts=_iso(row["latest_ts"]),
        symbols=_optional_int(row["symbols"]),
        dataset_versions=_optional_int(row["dataset_versions"]),
        ingest_runs=_optional_int(row["ingest_runs"]),
        duplicate_natural_keys=duplicates,
        missing_intervals=gaps,
        null_symbol_rows=_optional_int(row["null_symbol_rows"]),
        unconfirmed_rows=_optional_int(row["unconfirmed_rows"]),
        quality_flagged_rows=_optional_int(row["quality_flagged_rows"]),
        estimated_total_rows=estimate,
        symbol_windows=symbol_windows,
        symbol_windows_truncated=symbol_windows_truncated,
    )


def _symbol_windows(
    connection,
    table_sql: str,
    time_sql: str,
    columns: set[str],
    start: datetime,
    end: datetime,
    *,
    limit: int = 100,
) -> tuple[tuple[dict[str, Any], ...] | None, bool | None]:
    identity = "symbol" if "symbol" in columns else "inst_id" if "inst_id" in columns else None
    if identity is None:
        return None, None
    identity_sql = _quote_identifier(identity)
    utc_day_sql = (
        f"({time_sql} AT TIME ZONE 'UTC')::date"
        if connection.dialect.name == "postgresql"
        else f"DATE({time_sql})"
    )
    rows = connection.execute(
        text(
            f"SELECT {identity_sql} AS symbol, COUNT(*) AS row_count, "
            f"MIN({time_sql}) AS earliest_ts, MAX({time_sql}) AS latest_ts, "
            f"COUNT(DISTINCT {utc_day_sql}) AS utc_days_observed "
            f"FROM {table_sql} WHERE {time_sql} >= :start AND {time_sql} < :end "
            f"GROUP BY {identity_sql} ORDER BY {identity_sql} LIMIT :limit"
        ),
        {"start": start, "end": end, "limit": limit + 1},
    ).mappings().all()
    truncated = len(rows) > limit
    measured = tuple(
        {
            "symbol": None if row["symbol"] is None else str(row["symbol"]),
            "row_count": int(row["row_count"]),
            "earliest_ts": _iso(row["earliest_ts"]),
            "latest_ts": _iso(row["latest_ts"]),
            "utc_days_observed": int(row["utc_days_observed"]),
        }
        for row in rows[:limit]
    )
    return measured, truncated


def _natural_key_columns(modeled_table, columns: set[str], time_column: str) -> tuple[str, ...] | None:
    """Return the modeled business key used to identify duplicate facts.

    A trade timestamp is not unique: bursts routinely contain several trades
    in one millisecond.  Prefer composite modeled primary/unique constraints
    containing the event-time column, then fall back to ``symbol + time`` for
    staging tables whose surrogate key deliberately preserves duplicate input.
    """

    if modeled_table is not None:
        candidates: list[tuple[str, ...]] = []
        for constraint in modeled_table.constraints:
            if not isinstance(constraint, (PrimaryKeyConstraint, UniqueConstraint)):
                continue
            names = tuple(column.name for column in constraint.columns)
            if time_column in names and len(names) > 1:
                candidates.append(names)
        if candidates:
            return min(
                candidates,
                key=lambda item: (
                    0 if "symbol" in item or "inst_id" in item else 1,
                    len(item),
                    item,
                ),
            )
    if "symbol" in columns:
        return ("symbol", time_column)
    if "inst_id" in columns:
        return ("inst_id", time_column)
    return None


def _database_enforces_unique_key(
    modeled_table,
    natural_key: tuple[str, ...] | None,
) -> bool:
    """Return whether PostgreSQL already rejects duplicates for this key.

    Recounting duplicates across high-frequency payload tables is both
    redundant and capable of exhausting the audit statement timeout.  An
    exact modeled primary/unique constraint is stronger evidence than a
    periodic scan, so constrained keys are reported as zero duplicates without
    rereading the table.
    """

    if modeled_table is None or not natural_key:
        return False
    return any(
        isinstance(constraint, (PrimaryKeyConstraint, UniqueConstraint))
        and tuple(column.name for column in constraint.columns) == natural_key
        for constraint in modeled_table.constraints
    )


def _duplicate_count(
    connection,
    table_sql: str,
    time_sql: str,
    natural_key: tuple[str, ...] | None,
    start: datetime,
    end: datetime,
) -> int | None:
    if not natural_key:
        return None
    group_sql = ", ".join(_quote_identifier(column) for column in natural_key)
    row = connection.execute(
        text(
            "SELECT COUNT(*) FROM ("
            f"SELECT 1 FROM {table_sql} WHERE {time_sql} >= :start AND {time_sql} < :end "
            f"GROUP BY {group_sql} HAVING COUNT(*) > 1 LIMIT 10001"
            ") AS duplicate_keys"
        ),
        {"start": start, "end": end},
    ).scalar_one()
    return int(row)


def _gap_count(
    connection,
    table_sql: str,
    time_sql: str,
    table: str,
    columns: set[str],
    start: datetime,
    end: datetime,
) -> int | None:
    timeframe = next((value for value in _TIMEFRAME_SECONDS if table.endswith(f"_{value}")), None)
    if timeframe is None:
        return None
    expected = _TIMEFRAME_SECONDS[timeframe]
    partition_column = "symbol" if "symbol" in columns else "inst_id" if "inst_id" in columns else None
    partition_sql = _quote_identifier(partition_column) if partition_column else None
    select_prefix = f"{partition_sql}, " if partition_sql else ""
    partition_clause = f"PARTITION BY {partition_sql} " if partition_sql else ""
    distinct_columns = f"{partition_sql}, {time_sql}" if partition_sql else time_sql
    row = connection.execute(
        text(
            "SELECT COUNT(*) FROM ("
            f"SELECT {select_prefix}{time_sql}, "
            f"LAG({time_sql}) OVER ({partition_clause}ORDER BY {time_sql}) AS previous_ts "
            f"FROM (SELECT DISTINCT {distinct_columns} FROM {table_sql} "
            f"WHERE {time_sql} >= :start AND {time_sql} < :end) AS timestamps"
            ") AS ordered WHERE previous_ts IS NOT NULL "
            f"AND EXTRACT(EPOCH FROM ({time_sql} - previous_ts)) > :expected"
        ),
        {"start": start, "end": end, "expected": expected},
    ).scalar_one()
    return int(row)


def _zero_status(connection, table: str, start: datetime, end: datetime) -> str:
    if "liquidation" not in table:
        return "missing"
    exists = connection.execute(
        text("SELECT to_regclass('meta.collector_continuity_events')")
    ).scalar_one_or_none()
    if exists is None:
        return "collector_unknown"
    events = connection.execute(
        text(
            "SELECT event_type, COUNT(*) AS event_count, "
            "MIN(event_ts) AS earliest_event_ts, "
            "MAX(event_ts) AS latest_event_ts, "
            "COUNT(DISTINCT connection_generation) AS generations, "
            "COUNT(DISTINCT ingest_run_id) AS ingest_runs "
            "FROM meta.collector_continuity_events "
            "WHERE collector = 'aats-liquidations-daemon' "
            "AND channel = 'liquidation-orders' "
            "AND symbol = 'BTC-USDT-SWAP' "
            "AND event_ts >= :start AND event_ts < :end "
            "GROUP BY event_type"
        ),
        {"start": start, "end": end},
    ).mappings().all()
    counts = {
        str(row["event_type"]): int(row["event_count"])
        for row in events
    }
    message_rows = [row for row in events if str(row["event_type"]) == "MESSAGE"]
    if len(message_rows) != 1:
        return "collector_unknown"
    message = message_rows[0]
    first_message = message["earliest_event_ts"]
    last_message = message["latest_event_ts"]
    if first_message is None or last_message is None:
        return "collector_unknown"
    tolerance = timedelta(seconds=120)
    boundary_covered = (
        first_message <= start + tolerance
        and last_message >= end - tolerance
    )
    # ``liquidation-orders`` is sparse by nature. A healthy zero-event window
    # has inbound connection-frame evidence but legitimately has nothing to
    # flush, so requiring FLUSH would make every true zero look unknown.
    healthy = (
        counts.get("MESSAGE", 0) > 0
        and boundary_covered
        and int(message["generations"]) == 1
        and int(message["ingest_runs"]) == 1
    )
    broken = any(
        counts.get(event_type, 0) > 0
        for event_type in ("DROP", "DISCONNECT", "CLOCK_SKEW")
    )
    return "zero_event_with_healthy_collector" if healthy and not broken else "collector_unknown"


def _estimated_rows(connection, schema: str, table: str) -> int | None:
    if connection.dialect.name != "postgresql":
        return None
    value = connection.execute(
        text(
            "SELECT GREATEST(c.reltuples, 0)::BIGINT FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = :schema AND c.relname = :table"
        ),
        {"schema": schema, "table": table},
    ).scalar_one_or_none()
    return None if value is None else int(value)


def _summarize(results: list[TableCoverage]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for item in results:
        summary[item.status] = summary.get(item.status, 0) + 1
    summary["total_tables"] = len(results)
    return dict(sorted(summary.items()))


def _empty(qualified: str, status: str, start: datetime, end: datetime, *, time_column: str | None = None, estimate: int | None = None, error_code: str | None = None) -> TableCoverage:
    return TableCoverage(
        table=qualified,
        status=status,
        time_column=time_column,
        window_start=start.isoformat(),
        window_end=end.isoformat(),
        row_count=None,
        earliest_ts=None,
        latest_ts=None,
        symbols=None,
        dataset_versions=None,
        ingest_runs=None,
        duplicate_natural_keys=None,
        missing_intervals=None,
        null_symbol_rows=None,
        unconfirmed_rows=None,
        quality_flagged_rows=None,
        estimated_total_rows=estimate,
        symbol_windows=None,
        symbol_windows_truncated=None,
        error_code=error_code,
    )


def _coverage_status(
    *,
    count: int,
    duplicates: int | None,
    missing_intervals: int | None,
    null_symbol_rows: int | None,
    unconfirmed_rows: int | None,
    quality_flagged_rows: int | None,
    zero_status: str | None,
) -> str:
    if count == 0:
        return zero_status or "missing"
    deficits = (
        duplicates,
        missing_intervals,
        null_symbol_rows,
        unconfirmed_rows,
        quality_flagged_rows,
    )
    return (
        "observed_with_quality_issues"
        if any(value is not None and value > 0 for value in deficits)
        else "observed"
    )


def _quote_identifier(value: str) -> str:
    if not value.replace("_", "").isalnum():
        raise ValueError("unsafe_sql_identifier")
    return f'"{value}"'


def _quote_qualified(schema: str, table: str) -> str:
    return f"{_quote_identifier(schema)}.{_quote_identifier(table)}"


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _iso(value: Any) -> str | None:
    return None if value is None else value.isoformat()


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# RDP 数据覆盖只读审计",
        "",
        f"> 查询时间：{report['queried_at']}",
        f"> Git：`{report['git_commit']}`",
        f"> 数据库脱敏指纹：`{report['database_fingerprint_sha256']}`",
        f"> 窗口：`{report['window']['start']}` 至 `{report['window']['end']}`（半开）",
        f"> 算法：`{report['algorithm_version']}`；只读：`{str(report['read_only']).lower()}`",
        "",
        "| 数据表 | 状态 | 窗口行数 | 最早 | 最晚 | 重复键 | 缺口 |",
        "| --- | --- | ---: | --- | --- | ---: | ---: |",
    ]
    for item in report["tables"]:
        lines.append(
            "| {table} | {status} | {row_count} | {earliest_ts} | {latest_ts} | "
            "{duplicate_natural_keys} | {missing_intervals} |".format(**item)
        )
    lines.extend(
        [
            "",
            "说明：`missing`、`collector_unknown` 与 "
            "`zero_event_with_healthy_collector` 具有不同语义；本报告不会创建表、补数或修复历史。",
            "",
        ]
    )
    lines.extend(
        [
            "## 数据恢复矩阵",
            "",
            "| 数据集 | 当前状态 | 分类 | Owner | 优先级 | 容量门禁 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for item in report.get("recovery_matrix", []):
        lines.append(
            "| {dataset} | {observed_status} | {classification} | {owner} | "
            "{priority} | {capacity_status} |".format(**item)
        )
    lines.append("")
    return "\n".join(lines)
