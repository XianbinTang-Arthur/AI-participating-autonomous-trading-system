#!/usr/bin/env python3
"""验证 RDP 与 live production DB 的连接和表可读性.

阶段 A 交付物：确保 RDP 能稳定读取主交易系统的 live 事实数据。

用法:
    python scripts/rdp_check_live_db.py
    python scripts/rdp_check_live_db.py --table execution_orders --sample 5
    python scripts/rdp_check_live_db.py --verbose

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
    p = argparse.ArgumentParser(
        description="验证 RDP live DB 只读连接",
    )
    p.add_argument(
        "--table",
        default=None,
        help="只检查指定表（默认检查全部 7 张表）",
    )
    p.add_argument(
        "--sample",
        type=int,
        default=0,
        help="每张表输出 N 条样本行（默认不输出）",
    )
    p.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="输出详细信息",
    )
    p.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="JSON 格式输出",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()

    from aats.data_platform.config import get_settings
    from aats.data_platform.live_query_adapter import (
        LIVE_TABLES,
        TABLE_KEY_COLUMNS,
        check_live_db_health,
        get_live_session,
    )

    settings = get_settings()

    # 检查配置
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

    # 健康检查
    print("=" * 60)
    print("RDP Live DB 连接验证")
    print("=" * 60)
    print(f"时间: {datetime.now(timezone.utc).isoformat()}")
    print(f"Readonly 模式: {settings.live_db_readonly}")
    print()

    health = check_live_db_health(settings)

    if args.json_output:
        print(json.dumps(health, ensure_ascii=False, indent=2, default=str))
        return 0 if health["healthy"] else 1

    # 连接状态
    conn_icon = "[OK]" if health["connection_ok"] else "[FAIL]"
    print(f"  {conn_icon} 数据库连接")

    # 各表状态
    tables_to_check = [args.table] if args.table else LIVE_TABLES
    passed = 0
    failed = 0

    for table in tables_to_check:
        info = health.get("tables_checked", {}).get(table, {})
        if info.get("readable"):
            count = info.get("row_count_sample", "?")
            print(f"  [OK] {table} (rows >= {count})")
            passed += 1
        else:
            err = info.get("error", "未知错误")
            print(f"  [FAIL] {table}: {err}")
            failed += 1

    print()
    print(f"结果: {passed} 通过, {failed} 失败")

    # 样本输出
    if args.sample > 0 and health["connection_ok"]:
        from sqlalchemy import text

        print()
        print("-" * 60)
        print(f"样本数据（每表最多 {args.sample} 行）")
        print("-" * 60)

        with get_live_session(settings) as session:
            for table in tables_to_check:
                meta = TABLE_KEY_COLUMNS.get(table)
                if meta is None:
                    continue
                time_col = meta["time_col"]
                try:
                    rows = session.execute(
                        text(f"SELECT * FROM {table} ORDER BY {time_col} DESC LIMIT :n"),  # noqa: S608
                        {"n": args.sample},
                    ).mappings().all()
                    print(f"\n  [{table}] ({len(rows)} rows)")
                    for i, row in enumerate(rows):
                        row_dict = dict(row)
                        if args.verbose:
                            print(f"    #{i+1}: {json.dumps(row_dict, default=str, ensure_ascii=False)}")
                        else:
                            # 仅输出 PK + time
                            pk = meta["pk"]
                            print(f"    #{i+1}: {pk}={row_dict.get(pk)}, {time_col}={row_dict.get(time_col)}")
                except Exception as exc:
                    print(f"\n  [{table}] ERROR: {exc}")

    # 总结
    overall = "[HEALTHY]" if health["healthy"] else "[UNHEALTHY]"
    print()
    print(f"整体状态: {overall}")

    if health.get("errors"):
        print()
        print("错误详情:")
        for err in health["errors"]:
            print(f"  - {err}")

    return 0 if health["healthy"] else 1


if __name__ == "__main__":
    sys.exit(main())
