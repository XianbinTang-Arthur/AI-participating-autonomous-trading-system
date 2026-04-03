"""Database connection management for the Research Data Platform."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Iterator
from typing import Any

from sqlalchemy import create_engine, text
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
    """Execute all research migration SQL files in order."""
    import pathlib

    migration_dir = pathlib.Path(__file__).resolve().parent.parent.parent / "migrations" / "research"
    engine = get_engine(settings)

    sql_files = sorted(migration_dir.glob("*.sql"))
    with engine.connect() as conn:
        for sql_file in sql_files:
            sql = sql_file.read_text(encoding="utf-8")
            conn.execute(text(sql))
        conn.commit()


def reset_engine() -> None:
    """Reset cached engine/session (for testing)."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
