#!/usr/bin/env python3
"""Archive expired microstructure partitions before retention may delete them."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import text

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from aats.data_platform.data_governance.archive import (
    ArchiveScope,
    archive_partition,
    register_local_capture_source,
)
from aats.data_platform.db import get_session, get_session_factory


RETENTION_PLAN = {
    "bronze.market_trades": 30,
    "bronze.market_orderbook_bbo": 14,
    "bronze.market_orderbook_books5": 14,
    "staging.market_oi_funding_ticks": 7,
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="归档到期的 RDP 微观结构分区")
    parser.add_argument("--archive-root", type=Path, required=True)
    parser.add_argument("--minimum-free-gib", type=int, default=5)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    return parser.parse_args(argv)


def discover(now: datetime) -> list[dict[str, object]]:
    partitions: list[dict[str, object]] = []
    with get_session() as session:
        for table, days in RETENTION_PLAN.items():
            cutoff = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
            rows = session.execute(
                text(
                    f"SELECT symbol, date_trunc('day', ts) AS day, COUNT(*) AS rows "
                    f"FROM {table} WHERE ts < :cutoff GROUP BY symbol, day ORDER BY day, symbol"
                ),
                {"cutoff": cutoff},
            ).mappings()
            for row in rows:
                partitions.append(
                    {"table": table, "days": days, "symbol": row["symbol"], "day": row["day"], "rows": int(row["rows"])}
                )
    return partitions


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.apply != args.confirm:
        print("--apply 与 --confirm 必须同时使用", file=sys.stderr)
        return 4
    if not args.archive_root.expanduser().is_absolute():
        print("--archive-root 必须是绝对路径", file=sys.stderr)
        return 4
    if args.minimum_free_gib < 0:
        print("--minimum-free-gib 不得为负数", file=sys.stderr)
        return 4
    now = datetime.now(timezone.utc)
    partitions = discover(now)
    if not args.apply:
        print(json.dumps({"mode": "dry_run", "partitions": partitions}, indent=2, default=str))
        return 2

    factory = get_session_factory()
    archived: list[dict[str, object]] = []
    for item in partitions:
        table = str(item["table"])
        day = item["day"]
        with factory() as session, session.begin():
            source_id = register_local_capture_source(
                session,
                source_key=f"aats-ws:{table}:v1",
                table=table,
                schema_version="v1",
                timestamp_semantics="exchange event/sample timestamp stored in ts; local receipt semantics are table-specific",
            )
        scope = ArchiveScope(
            source_id=source_id,
            dataset_name=table,
            table=table,
            symbol=str(item["symbol"]),
            coverage_start=day,
            coverage_end=day + timedelta(days=1),
        )
        artifact = archive_partition(
            factory,
            scope,
            args.archive_root.expanduser().resolve(),
            minimum_free_bytes=args.minimum_free_gib * 1024**3,
        )
        archived.append({"scope": item, "artifact": artifact.__dict__})
    print(json.dumps({"mode": "applied", "archived": archived}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
