"""Database connection management for the Research Data Platform."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from aats.data_platform.config import ResearchPlatformSettings, get_settings

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine(settings: ResearchPlatformSettings | None = None) -> Engine:
    global _engine
    if _engine is None:
        settings = settings or get_settings()
        _engine = create_engine(
            settings.database_url,
            pool_size=5,
            max_overflow=10,
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


def run_migrations(settings: ResearchPlatformSettings | None = None) -> None:
    """使用 ORM create_all() 初始化研究数据库全部 schema 和表。

    替代旧的 migrations/research/*.sql 逐文件执行方式。
    幂等——已存在的 schema/表不会被破坏。
    """
    from aats.data_platform.rdp_models import create_rdp_schema

    engine = get_engine(settings)
    create_rdp_schema(engine)


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
    run_migrations()