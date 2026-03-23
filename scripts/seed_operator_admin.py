from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aats.bootstrap.config import load_settings
from aats.bootstrap.env_profiles import EnvTemplateProfile, load_profiled_dotenv_into_process
from aats.services.operator.accounts import create_operator_user
from aats.storage.operator_repo_postgres import PostgresOperatorUserRepository
from aats.storage.session import create_database_runtime, create_schema, validate_runtime_schema


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed the first operator admin user into the configured Postgres database.")
    parser.add_argument(
        "--profile",
        choices=("spot", "derivatives", "spot_live", "derivatives_live"),
        required=True,
        help="必填。选择要写入管理员用户的环境模板。",
    )
    parser.add_argument(
        "--username",
        required=True,
        help="必填。管理员用户名。",
    )
    parser.add_argument(
        "--password",
        help="可选。管理员密码；不传则交互输入。",
    )
    return parser.parse_args()


def _read_password(explicit_password: str | None) -> str:
    if explicit_password:
        return explicit_password
    first = getpass.getpass("Admin password: ")
    second = getpass.getpass("Repeat password: ")
    if not first:
        raise ValueError("operator_password_required")
    if first != second:
        raise ValueError("operator_password_confirmation_mismatch")
    return first


def main() -> None:
    args = parse_args()
    profile: EnvTemplateProfile = args.profile
    load_profiled_dotenv_into_process(ROOT, profile)
    os.chdir(ROOT)
    settings = load_settings()

    if settings.storage_mode != "postgres":
        raise SystemExit("storage_mode_must_be_postgres")
    if not settings.database_url:
        raise SystemExit("database_url_required")

    password = _read_password(args.password)
    runtime = create_database_runtime(settings.database_url)
    try:
        if settings.database_auto_create_schema:
            create_schema(runtime)
        validate_runtime_schema(runtime)
        repo = PostgresOperatorUserRepository(runtime.session_factory)
        created = create_operator_user(
            repo,
            username=args.username,
            password=password,
            role="admin",
            enabled=True,
        )
    finally:
        runtime.dispose()

    print(
        "seeded_operator_admin",
        created.username,
        created.role,
        created.enabled,
    )


if __name__ == "__main__":
    main()
