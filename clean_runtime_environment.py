from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path
from typing import Iterable
from typing import cast

from sqlalchemy import text
from sqlalchemy.engine import Engine, make_url

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aats.bootstrap.config import load_settings
from aats.bootstrap.env_profiles import EnvTemplateProfile, load_profiled_dotenv_into_process
from aats.storage.session import create_database_runtime, create_schema, validate_runtime_schema


DEFAULT_KEEP_TABLES = ("operator_users",)
PROFILE_CHOICES: tuple[str, ...] = ("spot", "derivatives", "spot_live", "derivatives_live")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "清理当前 AATS 运行时环境。默认按当前 profile 解析日志目录和数据库配置，"
            "删除日志文件，并清空 PostgreSQL 当前 schema 中除保留表外的业务数据。"
        )
    )
    parser.add_argument(
        "--profile",
        choices=PROFILE_CHOICES,
        default=None,
        help="可选。加载与 start_api.py 相同的 .env 模板，例如 derivatives_live。",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="可选。显式覆盖数据库连接串；未传时优先使用当前 profile 解析出的 settings.database_url。",
    )
    parser.add_argument(
        "--log-dir",
        action="append",
        dest="log_dirs",
        default=[],
        help="可选。显式指定要清理的日志目录，可重复传入；未传时使用当前 settings.log_dir。",
    )
    parser.add_argument(
        "--keep-table",
        action="append",
        dest="keep_tables",
        default=[],
        help="额外保留不清空的表名，可重复传入。",
    )
    parser.add_argument(
        "--skip-logs",
        action="store_true",
        help="跳过日志目录清理。",
    )
    parser.add_argument(
        "--skip-db",
        action="store_true",
        help="跳过数据库清理。",
    )
    parser.add_argument(
        "--skip-runtime-lock-check",
        action="store_true",
        help="跳过数据库单实例锁检查。只有在确认没有运行中的实例时才应使用。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将执行的动作，不真正删除日志或清空数据库。",
    )
    return parser.parse_args()


def apply_profile(project_root: Path, profile: str | None) -> Path | None:
    if profile is None:
        return None
    return load_profiled_dotenv_into_process(project_root, profile=cast(EnvTemplateProfile, profile))


def resolve_log_directories(project_root: Path, configured_log_dirs: Iterable[str]) -> list[Path]:
    resolved: list[Path] = []
    seen: set[str] = set()
    for raw_dir in configured_log_dirs:
        if not raw_dir:
            continue
        candidate = Path(raw_dir)
        if not candidate.is_absolute():
            candidate = project_root / candidate
        key = str(candidate.resolve())
        if key in seen:
            continue
        seen.add(key)
        resolved.append(candidate)
    return resolved


def render_database_url(database_url: str | None) -> str:
    if not database_url:
        return "<unset>"
    return make_url(database_url).render_as_string(hide_password=True)


def require_postgres_url(database_url: str | None) -> str:
    if not database_url:
        raise SystemExit("缺少 PostgreSQL 连接串：请通过 --profile 加载配置，或显式传入 --database-url。")
    parsed = make_url(database_url)
    if parsed.get_backend_name() != "postgresql":
        raise SystemExit(f"只支持 PostgreSQL，当前 database_url={parsed.render_as_string(hide_password=True)}")
    return database_url


def discover_tables(engine: Engine) -> tuple[str, list[str]]:
    with engine.connect() as connection:
        schema = connection.execute(text("SELECT current_schema()")).scalar_one()
        rows = connection.execute(
            text(
                """
                SELECT tablename
                FROM pg_catalog.pg_tables
                WHERE schemaname = :schema
                ORDER BY tablename
                """
            ),
            {"schema": schema},
        ).scalars().all()
    return str(schema), [str(item) for item in rows]


def quote_table(engine: Engine, schema: str, table: str) -> str:
    preparer = engine.dialect.identifier_preparer
    return f"{preparer.quote_identifier(schema)}.{preparer.quote_identifier(table)}"


def truncate_tables(engine: Engine, schema: str, tables: list[str], dry_run: bool) -> None:
    if not tables:
        print("数据库清理：没有需要清空的表。")
        return

    qualified = ", ".join(quote_table(engine, schema, table) for table in tables)
    sql = f"TRUNCATE TABLE {qualified} RESTART IDENTITY CASCADE"

    print(f"数据库清理：将清空 {len(tables)} 张表。")
    for table in tables:
        print(f"  - {schema}.{table}")

    if dry_run:
        print(f"[dry-run] {sql}")
        return

    with engine.begin() as connection:
        connection.execute(text(sql))
    print("数据库清理：已完成。")


def clean_directory_contents(path: Path, dry_run: bool) -> None:
    resolved = path.resolve()
    if not resolved.exists():
        print(f"日志清理：目录不存在，跳过 -> {resolved}")
        return
    if not resolved.is_dir():
        raise SystemExit(f"日志路径不是目录：{resolved}")

    items = sorted(resolved.iterdir(), key=lambda item: item.name)
    if not items:
        print(f"日志清理：目录已为空 -> {resolved}")
        return

    print(f"日志清理：将删除 {resolved} 下的 {len(items)} 个项目。")
    for item in items:
        print(f"  - {item.name}")

    if dry_run:
        return

    for item in items:
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()
    print(f"日志清理：已完成 -> {resolved}")


def normalize_keep_tables(items: Iterable[str]) -> list[str]:
    return sorted({table.strip() for table in items if table and table.strip()})


def main() -> int:
    args = parse_args()
    dotenv_path = apply_profile(ROOT, args.profile)
    os.chdir(ROOT)

    settings = load_settings()
    explicit_log_dirs = args.log_dirs or [settings.log_dir]
    resolved_log_dirs = resolve_log_directories(ROOT, explicit_log_dirs)
    configured_database_url = args.database_url or settings.database_url
    keep_tables = normalize_keep_tables([*DEFAULT_KEEP_TABLES, *args.keep_tables])

    print("开始清理运行时环境。")
    print(f"项目根目录：{ROOT}")
    print(f"已加载 profile：{args.profile or '<none>'}")
    print(f"dotenv 文件：{dotenv_path if dotenv_path is not None else '<none>'}")
    print(f"config_profile：{settings.config_profile}")
    print(f"mode：{settings.mode}")
    print(f"storage_mode：{settings.storage_mode}")
    if resolved_log_dirs:
        print("日志目录：")
        for directory in resolved_log_dirs:
            print(f"  - {directory.resolve()}")
    else:
        print("日志目录：<none>")
    print(f"数据库：{render_database_url(configured_database_url)}")
    print(f"保留表：{', '.join(keep_tables) if keep_tables else '<none>'}")
    if args.dry_run:
        print("当前为 dry-run，仅展示动作，不执行实际删除。")

    if not args.skip_logs:
        for log_dir in resolved_log_dirs:
            clean_directory_contents(log_dir, dry_run=args.dry_run)

    if args.skip_db:
        print("数据库清理：已按参数跳过。")
        print("运行时环境清理完成。")
        return 0

    if configured_database_url is None and settings.storage_mode != "postgres":
        print("数据库清理：当前 storage_mode 不是 postgres，且未显式传入 --database-url，跳过。")
        print("运行时环境清理完成。")
        return 0

    database_url = require_postgres_url(configured_database_url)
    runtime = create_database_runtime(database_url)
    try:
        if settings.database_single_runtime_guard_enabled and not args.skip_runtime_lock_check:
            if args.dry_run:
                print(f"[dry-run] 将尝试获取数据库单实例锁 key={settings.database_runtime_lock_key}")
            runtime.acquire_single_runtime_lock(settings.database_runtime_lock_key)
            print(f"数据库锁检查：已获取 runtime lock key={settings.database_runtime_lock_key}")
        elif settings.database_single_runtime_guard_enabled:
            print("数据库锁检查：已按参数跳过。")
        else:
            print("数据库锁检查：当前配置未启用单实例保护。")

        if settings.database_auto_create_schema:
            if args.dry_run:
                print("[dry-run] 将确保数据库 schema 已创建。")
            else:
                create_schema(runtime)
        validate_runtime_schema(runtime)
        schema, tables = discover_tables(runtime.engine)
        truncate_targets = [table for table in tables if table not in keep_tables]
        truncate_tables(runtime.engine, schema, truncate_targets, dry_run=args.dry_run)
    finally:
        runtime.dispose()

    print("运行时环境清理完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
