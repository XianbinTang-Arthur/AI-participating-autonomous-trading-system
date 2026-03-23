from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.engine import make_url


DEFAULT_KEEP_TABLES = ("operator_users",)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="清理项目运行时环境：删除历史日志，并清空 PostgreSQL 当前 schema 中除保留表外的所有表数据。"
    )
    parser.add_argument(
        "--database-url",
        default=os.getenv("AATS_DATABASE_URL"),
        help="PostgreSQL 连接串。默认读取环境变量 AATS_DATABASE_URL。",
    )
    parser.add_argument(
        "--log-dir",
        default="logs",
        help="日志目录。默认值为项目根目录下的 logs。",
    )
    parser.add_argument(
        "--keep-table",
        action="append",
        dest="keep_tables",
        default=[],
        help="额外保留不清空的表，可重复传入。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将执行的清理动作，不真正删除日志或清空表。",
    )
    return parser.parse_args()


def require_postgres_url(database_url: str | None) -> str:
    if not database_url:
        raise SystemExit("缺少 PostgreSQL 连接串：请设置 AATS_DATABASE_URL 或通过 --database-url 传入。")
    parsed = make_url(database_url)
    if parsed.get_backend_name() != "postgresql":
        raise SystemExit("只支持 PostgreSQL，当前 database_url 不是 postgresql。")
    return database_url


def create_pg_engine(database_url: str) -> Engine:
    return create_engine(database_url, future=True, pool_pre_ping=True)


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
        print("数据库表清理：没有需要清空的表。")
        return
    qualified = ", ".join(quote_table(engine, schema, table) for table in tables)
    sql = f"TRUNCATE TABLE {qualified} RESTART IDENTITY CASCADE"
    print(f"数据库表清理：将清空 {len(tables)} 张表。")
    for table in tables:
        print(f"  - {schema}.{table}")
    if dry_run:
        print(f"[dry-run] {sql}")
        return
    with engine.begin() as connection:
        connection.execute(text(sql))
    print("数据库表清理：已完成。")


def clean_log_directory(log_dir: Path, dry_run: bool) -> None:
    resolved = log_dir.resolve()
    if not resolved.exists():
        print(f"日志清理：目录不存在，跳过 -> {resolved}")
        return
    if not resolved.is_dir():
        raise SystemExit(f"日志路径不是目录：{resolved}")

    items = sorted(resolved.iterdir(), key=lambda item: item.name)
    if not items:
        print(f"日志清理：目录已经为空 -> {resolved}")
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
    print("日志清理：已完成。")


def main() -> int:
    args = parse_args()
    project_root = Path(__file__).resolve().parent
    log_dir = Path(args.log_dir)
    if not log_dir.is_absolute():
        log_dir = project_root / log_dir

    database_url = require_postgres_url(args.database_url)
    keep_tables = sorted(set(DEFAULT_KEEP_TABLES) | {item.strip() for item in args.keep_tables if item.strip()})

    print("开始清理运行时环境。")
    print(f"项目根目录：{project_root}")
    print(f"日志目录：{log_dir.resolve()}")
    print(f"保留数据表：{', '.join(keep_tables)}")
    if args.dry_run:
        print("当前为 dry-run，仅展示动作，不真正执行。")

    clean_log_directory(log_dir, dry_run=args.dry_run)

    engine = create_pg_engine(database_url)
    try:
        schema, tables = discover_tables(engine)
        truncate_targets = [table for table in tables if table not in keep_tables]
        truncate_tables(engine, schema, truncate_targets, dry_run=args.dry_run)
    finally:
        engine.dispose()

    print("运行时环境清理完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
