from __future__ import annotations

import os
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from dotenv import dotenv_values
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, URL, make_url

from aats.bootstrap.env_profiles import PROFILE_STARTUP_PROFILES, resolve_profile_dotenv_path
from aats.storage.session import DatabaseRuntime, apply_current_migrations, create_database_runtime, create_schema


def bootstrap_postgres_test_env(
    *,
    project_root: Path | None = None,
    profile: str | None = None,
) -> str | None:
    configured = os.getenv("AATS_DATABASE_URL")
    if configured:
        return configured
    explicit_test_url = str(os.getenv("AATS_TEST_DATABASE_URL") or "").strip()
    if explicit_test_url:
        os.environ["AATS_DATABASE_URL"] = explicit_test_url
        return explicit_test_url

    root = project_root or Path(__file__).resolve().parents[2]
    test_dotenv_path = root / ".env.test.postgres"
    if test_dotenv_path.exists():
        database_url = str(dotenv_values(test_dotenv_path).get("AATS_DATABASE_URL") or "").strip()
        if database_url:
            os.environ["AATS_DATABASE_URL"] = database_url
            return database_url

    selected_profile = str(
        profile
        or os.getenv("AATS_TEST_ENV_TEMPLATE_PROFILE")
        or "derivatives_live"
    ).strip()
    if not selected_profile:
        return None
    try:
        dotenv_path = resolve_profile_dotenv_path(root, selected_profile)  # type: ignore[arg-type]
    except (FileNotFoundError, KeyError):
        return None
    if not dotenv_path.exists():
        return None

    database_url = str(dotenv_values(dotenv_path).get("AATS_DATABASE_URL") or "").strip()
    if not database_url:
        return None
    os.environ["AATS_DATABASE_URL"] = database_url
    os.environ.setdefault("AATS_ENV_TEMPLATE_PROFILE", selected_profile)
    startup_profile = PROFILE_STARTUP_PROFILES.get(selected_profile)  # type: ignore[arg-type]
    if startup_profile:
        os.environ.setdefault("AATS_STARTUP_PROFILE", startup_profile)
    return database_url


def postgres_test_url() -> str:
    database_url = bootstrap_postgres_test_env()
    if not database_url:
        raise unittest.SkipTest("AATS_DATABASE_URL is required for PostgreSQL-backed tests")
    return database_url


def postgres_example_url(*, database_name: str = "aats") -> str:
    return URL.create(
        drivername="postgresql+psycopg",
        username="example_user",
        password="example_password",
        host="example-host",
        port=5432,
        database=database_name,
    ).render_as_string(hide_password=False)


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
