"""Database connection management for the Research Data Platform."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from aats.data_platform.config import ResearchPlatformSettings, get_settings
from aats.storage.connection_budget import RDP_RESEARCH_POOL

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine(settings: ResearchPlatformSettings | None = None) -> Engine:
    global _engine
    if _engine is None:
        settings = settings or get_settings()
        _engine = create_engine(
            settings.database_url,
            pool_size=RDP_RESEARCH_POOL.pool_size,
            max_overflow=RDP_RESEARCH_POOL.max_overflow,
            pool_pre_ping=True,
        )
    return _engine


def get_session_factory(settings: ResearchPlatformSettings | None = None) -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        engine = get_engine(settings)
        _session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    return _session_factory


@contextlib.contextmanager
def get_session(settings: ResearchPlatformSettings | None = None) -> Iterator[Session]:
    factory = get_session_factory(settings)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def apply_rdp_migrations(settings: ResearchPlatformSettings | None = None):
    """显式创建 ORM baseline 并应用全部 ledgered Batch B migrations。"""
    from aats.data_platform.migrations._batch_b import run_batch_b_migrations
    from aats.data_platform.rdp_models import create_rdp_schema

    engine = get_engine(settings)
    create_rdp_schema(engine)
    report = run_batch_b_migrations(engine)
    if not report.ok:
        failed = next(stage for stage in report.stages if not stage.ok)
        raise RuntimeError(
            "rdp_schema_migration_failed:"
            f"stage={failed.stage};error_type={failed.error_type}"
        )
    validate_rdp_schema(settings)
    return report


def run_migrations(settings: ResearchPlatformSettings | None = None):
    """Backward-compatible explicit migration alias.

    Runtime services must call :func:`validate_rdp_schema`; only initialization
    and deployment migration jobs may call this mutating function.
    """
    return apply_rdp_migrations(settings)


def validate_rdp_schema(settings: ResearchPlatformSettings | None = None) -> None:
    """Read-only validation of ORM tables/columns and the Batch B ledger."""
    from aats.data_platform.migrations._batch_b import validate_batch_b_migrations
    from aats.data_platform.rdp_models import RdpBase, _RDP_SCHEMAS

    engine = get_engine(settings)
    inspector = inspect(engine)
    if engine.dialect.name == "postgresql":
        actual_schemas = set(inspector.get_schema_names())
        missing_schemas = sorted(set(_RDP_SCHEMAS) - actual_schemas)
        if missing_schemas:
            raise RuntimeError(
                f"rdp_schema_contract_missing_schemas:{missing_schemas}"
            )

    missing_tables: list[str] = []
    missing_columns: list[str] = []
    for table in RdpBase.metadata.sorted_tables:
        schema = table.schema
        qualified_name = f"{schema}.{table.name}" if schema else table.name
        if not inspector.has_table(table.name, schema=schema):
            missing_tables.append(qualified_name)
            continue
        actual_columns = {
            str(column["name"])
            for column in inspector.get_columns(table.name, schema=schema)
        }
        missing_columns.extend(
            f"{qualified_name}.{column.name}"
            for column in table.columns
            if column.name not in actual_columns
        )
    if missing_tables or missing_columns:
        raise RuntimeError(
            "rdp_schema_orm_contract_failed:"
            f"missing_tables={sorted(missing_tables)};"
            f"missing_columns={sorted(missing_columns)}"
        )

    validate_batch_b_migrations(engine)


def apply_batch_a_migrations(
    settings: ResearchPlatformSettings | None = None,
    *,
    stage: str = "orphan_report",
    dry_run: bool = False,
) -> dict:
    """Run a batch-A hardening migration stage.

    Stages:
      orphan_report — stage 4.4.1: read-only FK-gap + distribution scan.
                      Returns a structured report. Purely safe.
      fks           — stage 4.4.2: ADD 7 FOREIGN KEY constraints.
      uqs           — stage 4.4.3: ADD source_round_id column + partial unique idx.
      checks        — stage 4.4.4: ADD 9 CHECK constraints.
      rollback      — disaster: DROP all batch-A constraints (emergency only).

    Args:
      dry_run — For DDL stages (fks/uqs/checks/rollback), parses the file but
                does not execute. For orphan_report, has no effect (it is
                already read-only).

    See: docs/task/rdp_hardening_batch_a_detailed_design.md §4
    """
    from aats.data_platform.migrations._batch_a import (
        format_report_text,
        load_migration_sql,
        run_orphan_report,
    )

    engine = get_engine(settings)

    if stage == "orphan_report":
        report = run_orphan_report(engine)
        return {
            "stage": stage,
            "dry_run": dry_run,
            "report": report.summary_dict(),
            "text": format_report_text(report),
            "is_clean": report.is_clean,
        }

    if stage in ("fks", "uqs", "checks", "rollback"):
        sql = load_migration_sql(stage)
        if dry_run:
            return {
                "stage": stage,
                "dry_run": True,
                "sql_bytes": len(sql.encode("utf-8")),
                "note": "dry_run — SQL loaded but not executed",
            }
        with engine.begin() as conn:
            conn.exec_driver_sql(sql)
        return {"stage": stage, "dry_run": False, "executed": True}

    raise ValueError(f"unknown stage: {stage!r}")


def reset_engine() -> None:
    """Reset cached engine/session (for testing)."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


if __name__ == "__main__":
    apply_rdp_migrations()
