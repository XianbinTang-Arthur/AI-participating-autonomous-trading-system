from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from aats.storage.sqlalchemy_models import Base


@dataclass(slots=True)
class DatabaseRuntime:
    engine: Engine
    session_factory: sessionmaker[Session]
    runtime_lock_key: int | None = None
    runtime_lock_connection: Connection | None = None

    def acquire_single_runtime_lock(self, lock_key: int) -> None:
        if self.engine.dialect.name != "postgresql":
            return
        if self.runtime_lock_connection is not None and self.runtime_lock_key == lock_key:
            return
        connection = self.engine.connect()
        acquired = connection.execute(
            text("SELECT pg_try_advisory_lock(:lock_key)"),
            {"lock_key": lock_key},
        ).scalar()
        if not bool(acquired):
            connection.close()
            raise RuntimeError("database_single_runtime_lock_not_acquired")
        self.runtime_lock_key = lock_key
        self.runtime_lock_connection = connection

    def dispose(self) -> None:
        if self.runtime_lock_connection is not None and self.runtime_lock_key is not None:
            try:
                self.runtime_lock_connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": self.runtime_lock_key},
                )
            finally:
                self.runtime_lock_connection.close()
                self.runtime_lock_connection = None
                self.runtime_lock_key = None
        self.engine.dispose()


def create_database_runtime(database_url: str) -> DatabaseRuntime:
    connect_args: dict[str, object] = {}
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False

    engine = create_engine(
        database_url,
        future=True,
        pool_pre_ping=True,
        connect_args=connect_args,
    )
    return DatabaseRuntime(
        engine=engine,
        session_factory=sessionmaker(bind=engine, expire_on_commit=False, future=True),
    )


def create_schema(runtime: DatabaseRuntime) -> None:
    Base.metadata.create_all(runtime.engine)
