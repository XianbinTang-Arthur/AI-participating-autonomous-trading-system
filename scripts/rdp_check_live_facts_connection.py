#!/usr/bin/env python3
"""验证 RDP 与 live production DB 的连接和表可读性.

工作包 A 交付物：确保 RDP 能稳定读取主交易系统的 live 事实数据。

检查内容:
  1. live DB 连接是否可用
  2. 7 张关键表是否存在
  3. 最小必需列是否存在
  4. 每张表的 row count 和最近时间戳

用法:
    python scripts/rdp_check_live_facts_connection.py
    python scripts/rdp_check_live_facts_connection.py --verbose
    python scripts/rdp_check_live_facts_connection.py --json
    python scripts/rdp_check_live_facts_connection.py --table execution_fills --sample 3

退出码:
    0 = 所有检查通过
    1 = 有检查失败
    2 = 配置缺失
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="验证 RDP live facts 连接")
    p.add_argument("--table", default=None, help="只检查指定表")
    p.add_argument("--sample", type=int, default=0, help="输出每表 N 条样本行")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--json", action="store_true", dest="json_output")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    from aats.data_platform.config import get_settings
    from aats.data_platform.live_facts.contracts import ALL_TABLE_CONTRACTS, LIVE_TABLE_NAMES
    from aats.data_platform.live_facts.db import get_live_engine, get_live_session, test_connection
    from aats.data_platform.live_facts.query_adapter import (
        check_tables_health,
        fetch_latest_timestamps,
    )

    settings = get_settings()

    # ── 配置检查 ────────────────────────────────────────────────
    if not settings.live_database_url:
        msg = (
            "RDP_LIVE_DATABASE_URL 未配置。\n"
            "请在 .env.research 中添加:\n"
            "  RDP_LIVE_DATABASE_URL=postgresql+psycopg://user:pass@host:port/dbname\n"
        )
        if args.json_output:
            print(json.dumps({"error": msg.strip(), "exit_code": 2}, ensure_ascii=False))
        else:
            print(f"[ERROR] {msg}")
        return 2

    report: dict = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "live_db_readonly": settings.live_db_readonly,
        "live_db_schema": settings.live_db_schema,
    }

    print("=" * 65)
    print("RDP Live Facts Connection Check")
    print("=" * 65)
    print(f"Time: {report['timestamp']}")
    print(f"Readonly: {settings.live_db_readonly}")
    print(f"Schema: {settings.live_db_schema or 'public'}")
    print(f"Timeout: {settings.live_db_connect_timeout_seconds}s")
    print()

    # ── 连接测试 ────────────────────────────────────────────────
    conn_ok = test_connection(settings)
    report["connection_ok"] = conn_ok
    icon = "[OK]" if conn_ok else "[FAIL]"
    print(f"  {icon} Database connection")
    if not conn_ok:
        report["healthy"] = False
        if args.json_output:
            print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 1

    # ── 表健康检查 ──────────────────────────────────────────────
    with get_live_session(settings) as session:
        health = check_tables_health(session)
        timestamps = fetch_latest_timestamps(session)

    report["tables_health"] = health
    report["latest_timestamps"] = timestamps

    tables_to_check = [args.table] if args.table else LIVE_TABLE_NAMES
    passed = 0
    failed = 0

    print()
    for table_name in tables_to_check:
        table_info = health["tables_checked"].get(table_name, {})
        col_info = health.get("column_checks", {}).get(table_name, {})
        latest_ts = timestamps.get(table_name)

        if table_info.get("readable"):
            row_count = table_info.get("row_count", "?")
            missing_cols = col_info.get("missing", [])
            if missing_cols:
                print(f"  [WARN] {table_name}: readable (rows={row_count}), "
                      f"missing cols: {missing_cols}")
            else:
                print(f"  [OK]   {table_name}: rows={row_count}, "
                      f"latest={latest_ts or 'N/A'}")
                if args.verbose:
                    contract = ALL_TABLE_CONTRACTS[table_name]
                    print(f"         pk={contract.primary_key}, "
                          f"time={contract.time_column}, "
                          f"symbol={contract.symbol_column}")
                    print(f"         used_by: {', '.join(contract.used_by_phases)}")
            passed += 1
        else:
            err = table_info.get("error", "unknown")
            print(f"  [FAIL] {table_name}: {err}")
            failed += 1

    # ── 样本输出 ────────────────────────────────────────────────
    if args.sample > 0:
        from sqlalchemy import text

        print()
        print("-" * 65)
        print(f"Sample data (max {args.sample} rows per table)")
        print("-" * 65)

        with get_live_session(settings) as session:
            for table_name in tables_to_check:
                contract = ALL_TABLE_CONTRACTS.get(table_name)
                if contract is None:
                    continue
                try:
                    rows = session.execute(
                        text(
                            f"SELECT * FROM {table_name} "  # noqa: S608
                            f"ORDER BY {contract.time_column} DESC LIMIT :n"
                        ),
                        {"n": args.sample},
                    ).mappings().all()
                    print(f"\n  [{table_name}] ({len(rows)} rows)")
                    for i, row in enumerate(rows):
                        pk_val = dict(row).get(contract.primary_key, "?")
                        ts_val = dict(row).get(contract.time_column, "?")
                        sym_val = dict(row).get(contract.symbol_column, "?")
                        print(f"    #{i+1}: pk={pk_val}, ts={ts_val}, symbol={sym_val}")
                except Exception as exc:
                    print(f"\n  [{table_name}] ERROR: {exc}")

    # ── 总结 ────────────────────────────────────────────────────
    report["passed"] = passed
    report["failed"] = failed
    report["healthy"] = failed == 0 and conn_ok

    print()
    print(f"Result: {passed} passed, {failed} failed")
    overall = "[HEALTHY]" if report["healthy"] else "[UNHEALTHY]"
    print(f"Overall: {overall}")

    if health.get("errors"):
        print()
        print("Errors:")
        for err in health["errors"]:
            print(f"  - {err}")

    if args.json_output:
        print()
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))

    return 0 if report["healthy"] else 1


if __name__ == "__main__":
    sys.exit(main())
