from __future__ import annotations

import os
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url

from aats.storage.session import DatabaseRuntime, apply_current_migrations, create_database_runtime, create_schema


def postgres_test_url() -> str:
    database_url = os.getenv("AATS_DATABASE_URL")
    if not database_url:
        raise unittest.SkipTest("AATS_DATABASE_URL is required for PostgreSQL-backed tests")
    return database_url


def postgres_example_url(*, database_name: str = "aats") -> str:
    return f"postgresql+psycopg://aats:aats@localhost:5432/{database_name}"


@contextmanager
def temporary_postgres_url() -> Iterator[tuple[str, Engine, str]]:
    base_url = make_url(postgres_test_url())
    schema_name = f"aats_test_{uuid.uuid4().hex[:12]}"
    admin_engine = create_engine(base_url.render_as_string(hide_password=False), future=True)
    try:
        with admin_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
        query = dict(base_url.query)
        existing_options = query.get("options")
        search_path_option = f"-csearch_path={schema_name}"
        query["options"] = f"{existing_options} {search_path_option}".strip() if existing_options else search_path_option
        scoped_url = base_url.set(query=query).render_as_string(hide_password=False)
        yield scoped_url, admin_engine, schema_name
    finally:
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        admin_engine.dispose()


@contextmanager
def temporary_postgres_runtime(*, use_migrations: bool = False) -> Iterator[tuple[DatabaseRuntime, Engine, str]]:
    with temporary_postgres_url() as (scoped_url, admin_engine, schema_name):
        runtime = create_database_runtime(scoped_url)
        try:
            if use_migrations:
                _apply_migrations(runtime)
            else:
                create_schema(runtime)
            yield runtime, admin_engine, schema_name
        finally:
            runtime.dispose()


def _apply_migrations(runtime: DatabaseRuntime) -> None:
    apply_current_migrations(runtime)
