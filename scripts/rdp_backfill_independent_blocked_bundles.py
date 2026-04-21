from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote_plus

from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Task P3-1：E402 noqa —— 脚本必须先 sys.path.insert 再 from aats... 否则找不到包。
from aats.services.execution_engine.bundle_status_backfill import (  # noqa: E402
    backfill_independent_blocked_bundles,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backfill historical independent blocked bundles.")
    parser.add_argument(
        "--profile",
        choices=("spot", "derivatives", "spot_live", "derivatives_live"),
        default=None,
        help="Runtime profile template to load before connecting to the execution database.",
    )
    parser.add_argument("--bundle-id", action="append", default=[])
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _compose_database_url_from_env() -> str | None:
    user = os.environ.get("POSTGRES_USER")
    password = os.environ.get("POSTGRES_PASSWORD")
    database = os.environ.get("AATS_DB_NAME") or os.environ.get("POSTGRES_DB")
    if not user or not password or not database:
        return None
    host = os.environ.get("AATS_DB_HOST") or os.environ.get("POSTGRES_HOST") or "127.0.0.1"
    port = os.environ.get("AATS_DB_PORT") or os.environ.get("POSTGRES_PORT") or "5432"
    return (
        "postgresql+psycopg://"
        f"{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{quote_plus(database)}"
    )


def _resolve_runtime_database_url(*, profile: str | None) -> str:
    from aats.bootstrap.config import load_settings
    from aats.bootstrap.env_profiles import PROFILE_STARTUP_PROFILES, load_profiled_dotenv_into_process

    if profile:
        load_profiled_dotenv_into_process(ROOT, profile)
    elif os.environ.get("AATS_PROFILE"):
        selected_profile = os.environ["AATS_PROFILE"]
        startup_profile = PROFILE_STARTUP_PROFILES.get(selected_profile)
        if startup_profile is None:
            raise SystemExit(f"unsupported_aats_profile:{selected_profile}")
        os.environ.setdefault("AATS_STARTUP_PROFILE", startup_profile)
        os.environ.setdefault("AATS_ENV_TEMPLATE_PROFILE", selected_profile)

    os.chdir(ROOT)
    settings = load_settings()
    if settings.storage_mode != "postgres":
        raise SystemExit("storage_mode_must_be_postgres")
    resolved_database_url = settings.database_url or _compose_database_url_from_env()
    if not resolved_database_url:
        raise SystemExit("database_url_required")
    return str(resolved_database_url)


def main() -> int:
    args = build_parser().parse_args()
    engine = create_engine(_resolve_runtime_database_url(profile=args.profile), pool_pre_ping=True)
    try:
        try:
            with Session(engine) as session, session.begin():
                result = backfill_independent_blocked_bundles(
                    session,
                    bundle_ids=args.bundle_id or None,
                    limit=args.limit,
                    dry_run=args.dry_run,
                )
        except SQLAlchemyError as exc:
            print(json.dumps({
                "ok": False,
                "error": f"database_unavailable: {exc.__class__.__name__}",
            }, ensure_ascii=False, indent=2))
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
