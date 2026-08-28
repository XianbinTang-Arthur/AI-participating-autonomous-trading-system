"""Batch B migration runner — Phase 1-3 schema evolution (scope + feature flag + saga).

参考: docs/task/rdp_scope_expansion_detailed_design_v3.md §7

Batch B 按顺序执行版本化 schema 演进:
  - batch_b_01_core_schema.sql: scope 列 + system_config + saga + heartbeat
  - batch_b_02_profile_research.sql: profile_research_runs + streak
  - batch_b_03_cost_calibration.sql: cost_calibration_runs
  - batch_b_04_sleeve_advice.sql: vw_sleeve_advice_recent 视图
  - batch_b_05_microstructure.sql: P1-D Phase 1A bronze + staging microstructure 表
  - batch_b_06_silver_microstructure.sql: P1-D Phase 1A Silver 15m 聚合表 (5 张)
  - batch_b_07_ingest_runs_domain_extension.sql: 扩 domain='microstructure' 白名单
  - batch_b_08_oi_history.sql: P1-D Stage 5 OKX REST OI history bronze 表
  - batch_b_09_mark_ls_history.sql: P1-D Stage 5 OKX REST mark + LS history bronze 表
  - batch_b_11_silver_numeric_widen.sql: P0-a Silver vol_weighted_tfi 精度扩展
  - batch_b_12_orderbook_payloads.sql: execution science orderbook payload sidecar
  - batch_b_13_rdp_collection_modeling_hygiene.sql: collection/modeling hygiene
  - batch_b_14_ls_ratio_1h_schedule.sql: official 1H long-short ratio bronze table
  - batch_b_15_recommendation_source_round.sql: recommendation source round column + active uniqueness
  - batch_b_16_profit_readiness_governance.sql: holdout + parameter activation audit ledgers
  - batch_b_17_rdp_run_observability.sql: logical runs + attempts/steps/events
  - batch_b_18_data_governance.sql: provenance + archive/gap/bundle/rebuild/continuity ledgers
  - batch_b_19_historical_research_artifacts.sql: source-aware Gold + quality/index + campaign ledger
  - batch_b_20_typed_json_identity.sql: type-sensitive immutable JSON identity anchors

每个 stage 对应一个 rollback SQL,逆序回滚。

使用模式(与 _batch_a 一致):
    >>> from aats.data_platform.migrations._batch_b import run_batch_b_migrations
    >>> report = run_batch_b_migrations(engine)
    >>> if not report.ok:
    ...     raise RuntimeError(report.error_message)
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from sqlalchemy import text
from sqlalchemy.engine import Engine


MIGRATIONS_DIR = Path(__file__).parent
RDP_SCHEMA_MIGRATIONS_TABLE = "governance.rdp_schema_migrations"
_RDP_SCHEMA_MIGRATION_LOCK_KEY = 7_150_281_924_009_006


BATCH_B_STAGES: tuple[str, ...] = (
    "batch_b_01_core_schema",
    "batch_b_02_profile_research",
    "batch_b_03_cost_calibration",
    "batch_b_04_sleeve_advice",
    "batch_b_05_microstructure",
    "batch_b_06_silver_microstructure",
    "batch_b_07_ingest_runs_domain_extension",
    "batch_b_08_oi_history",
    "batch_b_09_mark_ls_history",
    "batch_b_11_silver_numeric_widen",
    "batch_b_12_orderbook_payloads",
    "batch_b_13_rdp_collection_modeling_hygiene",
    "batch_b_14_ls_ratio_1h_schedule",
    "batch_b_15_recommendation_source_round",
    "batch_b_16_profit_readiness_governance",
    "batch_b_17_rdp_run_observability",
    "batch_b_18_data_governance",
    "batch_b_19_historical_research_artifacts",
    "batch_b_20_typed_json_identity",
)


@dataclass
class BatchBStageResult:
    stage: str
    ok: bool
    applied: bool = False
    error_type: str | None = None
    error_message: str | None = None


@dataclass
class BatchBReport:
    stages: list[BatchBStageResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.stages)

    @property
    def error_message(self) -> str | None:
        for s in self.stages:
            if not s.ok:
                return f"{s.stage}: {s.error_message}"
        return None


def _load_sql(stage: str, *, rollback: bool = False) -> str:
    suffix = "_rollback.sql" if rollback else ".sql"
    filename = f"{stage}{suffix}"
    path = MIGRATIONS_DIR / filename
    if rollback and not path.exists():
        # Early Batch B rollback files used `batch_b_05_rollback.sql`
        # instead of `batch_b_05_microstructure_rollback.sql`. Keep the
        # runner backward-compatible so mixed old/new stage names remain
        # reversible without renaming deployed migration files.
        parts = stage.split("_")
        if len(parts) >= 3:
            legacy_filename = f"{'_'.join(parts[:3])}_rollback.sql"
            legacy_path = MIGRATIONS_DIR / legacy_filename
            if legacy_path.exists():
                path = legacy_path
    if not path.exists():
        raise FileNotFoundError(f"migration SQL not found: {path}")
    return path.read_text(encoding="utf-8")


def _stage_checksum(stage: str) -> str:
    sql = _load_sql(stage)
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def _without_outer_transaction(sql: str, *, stage: str) -> str:
    """Remove the SQL file's legacy BEGIN/COMMIT wrapper.

    The runner owns the transaction so the schema change and its ledger row
    commit atomically.  Keeping an inner COMMIT would allow DDL to become
    visible before the checksum ledger is durable.
    """
    lines = sql.splitlines()
    begin_indexes = [
        index for index, line in enumerate(lines) if line.strip().upper() == "BEGIN;"
    ]
    commit_indexes = [
        index for index, line in enumerate(lines) if line.strip().upper() == "COMMIT;"
    ]
    if len(begin_indexes) != 1 or len(commit_indexes) != 1:
        raise RuntimeError(
            "rdp_schema_migration_transaction_wrapper_invalid:"
            f"{stage}:begin={len(begin_indexes)};commit={len(commit_indexes)}"
        )
    begin_index = begin_indexes[0]
    commit_index = commit_indexes[0]
    if begin_index >= commit_index:
        raise RuntimeError(
            f"rdp_schema_migration_transaction_wrapper_invalid:{stage}:order"
        )
    return "\n".join(
        line
        for index, line in enumerate(lines)
        if index not in {begin_index, commit_index}
    )


@contextmanager
def _migration_lock(engine: Engine) -> Iterator[None]:
    """Serialize RDP schema changes across Gateway/daemon/operator jobs."""
    if engine.dialect.name != "postgresql":
        yield
        return

    connection = engine.connect()
    try:
        connection.execute(
            text("SELECT pg_advisory_lock(:lock_key)"),
            {"lock_key": _RDP_SCHEMA_MIGRATION_LOCK_KEY},
        )
        yield
    finally:
        try:
            connection.execute(
                text("SELECT pg_advisory_unlock(:lock_key)"),
                {"lock_key": _RDP_SCHEMA_MIGRATION_LOCK_KEY},
            )
        finally:
            connection.close()


def _ensure_migration_ledger(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS governance"))
        conn.execute(
            text(
                f"""
                CREATE TABLE IF NOT EXISTS {RDP_SCHEMA_MIGRATIONS_TABLE} (
                    version VARCHAR(256) PRIMARY KEY,
                    checksum VARCHAR(128) NOT NULL,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
        )


def _applied_stage_checksums(engine: Engine) -> dict[str, str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                f"SELECT version, checksum FROM {RDP_SCHEMA_MIGRATIONS_TABLE}"
            )
        ).mappings().all()
    return {str(row["version"]): str(row["checksum"]) for row in rows}


def validate_batch_b_migrations(engine: Engine) -> tuple[str, ...]:
    """Read-only validation of the complete Batch B ledger/checksum contract."""
    if engine.dialect.name == "postgresql":
        with engine.connect() as conn:
            exists = conn.execute(
                text("SELECT to_regclass(:table_name)"),
                {"table_name": RDP_SCHEMA_MIGRATIONS_TABLE},
            ).scalar_one_or_none()
        if exists is None:
            raise RuntimeError("rdp_schema_migrations_ledger_missing")

    applied = _applied_stage_checksums(engine)
    expected = {stage: _stage_checksum(stage) for stage in BATCH_B_STAGES}
    missing = sorted(set(expected) - set(applied))
    unknown = sorted(set(applied) - set(expected))
    mismatched = sorted(
        stage
        for stage in set(expected) & set(applied)
        if expected[stage] != applied[stage]
    )
    if missing or unknown or mismatched:
        raise RuntimeError(
            "rdp_schema_migration_contract_failed:"
            f"missing={missing};unknown={unknown};mismatched={mismatched}"
        )
    return BATCH_B_STAGES


def _validate_target_stages(stages: Iterable[str] | None) -> tuple[str, ...]:
    target = tuple(stages) if stages is not None else BATCH_B_STAGES
    unknown = [stage for stage in target if stage not in BATCH_B_STAGES]
    if unknown:
        raise ValueError(f"unknown Batch B stages: {unknown}")
    if len(set(target)) != len(target):
        raise ValueError("duplicate Batch B stage requested")
    return target


def run_batch_b_migrations(
    engine: Engine,
    *,
    stages: Iterable[str] | None = None,
) -> BatchBReport:
    """按序执行 Batch B migrations。

    每个 stage 在独立 transaction(SQL 文件内部已 BEGIN/COMMIT),
    失败即停;已跑的 stage 保留,未跑的跳过。
    """
    target = _validate_target_stages(stages)
    report = BatchBReport()

    with _migration_lock(engine):
        _ensure_migration_ledger(engine)
        applied = _applied_stage_checksums(engine)
        for stage in target:
            try:
                stage_index = BATCH_B_STAGES.index(stage)
                missing_prerequisites = [
                    prerequisite
                    for prerequisite in BATCH_B_STAGES[:stage_index]
                    if prerequisite not in applied
                ]
                if missing_prerequisites:
                    raise RuntimeError(
                        "rdp_schema_migration_prerequisite_missing:"
                        f"{stage}:{missing_prerequisites}"
                    )

                checksum = _stage_checksum(stage)
                recorded_checksum = applied.get(stage)
                if recorded_checksum is not None:
                    if recorded_checksum != checksum:
                        raise RuntimeError(
                            f"rdp_schema_migration_checksum_mismatch:{stage}"
                        )
                    report.stages.append(
                        BatchBStageResult(stage=stage, ok=True, applied=False)
                    )
                    continue

                sql = _without_outer_transaction(_load_sql(stage), stage=stage)
                with engine.begin() as conn:
                    # Migration files are trusted, versioned SQL scripts rather
                    # than SQLAlchemy statements.  ``text(sql)`` scans comments
                    # for ``:name`` bind markers (for example ``:sleeve`` in
                    # Stage 04) and can therefore fail before PostgreSQL sees
                    # otherwise-valid SQL.  Execute the script at the driver
                    # layer; keep the ledger write parameterized below.
                    conn.exec_driver_sql(
                        sql,
                        execution_options={"no_parameters": True},
                    )
                    conn.execute(
                        text(
                            f"""
                            INSERT INTO {RDP_SCHEMA_MIGRATIONS_TABLE}
                                (version, checksum, applied_at)
                            VALUES (:version, :checksum, NOW())
                            """
                        ),
                        {"version": stage, "checksum": checksum},
                    )
                applied[stage] = checksum
                report.stages.append(
                    BatchBStageResult(stage=stage, ok=True, applied=True)
                )
            except Exception as exc:
                report.stages.append(
                    BatchBStageResult(
                        stage=stage,
                        ok=False,
                        applied=False,
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                    )
                )
                break

    return report


def run_batch_b_rollback(
    engine: Engine,
    *,
    stages: Iterable[str] | None = None,
) -> BatchBReport:
    """按逆序回滚 Batch B migrations。"""
    target = (
        _validate_target_stages(stages)
        if stages is not None
        else tuple(reversed(BATCH_B_STAGES))
    )
    report = BatchBReport()

    with _migration_lock(engine):
        _ensure_migration_ledger(engine)
        applied = _applied_stage_checksums(engine)
        for stage in target:
            try:
                checksum = _stage_checksum(stage)
                recorded_checksum = applied.get(stage)
                if recorded_checksum is None:
                    report.stages.append(
                        BatchBStageResult(stage=stage, ok=True, applied=False)
                    )
                    continue
                if recorded_checksum != checksum:
                    raise RuntimeError(
                        f"rdp_schema_migration_checksum_mismatch:{stage}"
                    )

                stage_index = BATCH_B_STAGES.index(stage)
                applied_dependents = [
                    dependent
                    for dependent in BATCH_B_STAGES[stage_index + 1 :]
                    if dependent in applied
                ]
                if applied_dependents:
                    raise RuntimeError(
                        "rdp_schema_rollback_not_applied_suffix:"
                        f"{stage}:{applied_dependents}"
                    )

                sql = _without_outer_transaction(
                    _load_sql(stage, rollback=True),
                    stage=f"{stage}:rollback",
                )
                with engine.begin() as conn:
                    conn.exec_driver_sql(
                        sql,
                        execution_options={"no_parameters": True},
                    )
                    conn.execute(
                        text(
                            f"DELETE FROM {RDP_SCHEMA_MIGRATIONS_TABLE} "
                            "WHERE version = :version"
                        ),
                        {"version": stage},
                    )
                applied.pop(stage, None)
                report.stages.append(
                    BatchBStageResult(stage=stage, ok=True, applied=True)
                )
            except Exception as exc:
                report.stages.append(
                    BatchBStageResult(
                        stage=stage,
                        ok=False,
                        applied=False,
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                    )
                )
                break

    return report
