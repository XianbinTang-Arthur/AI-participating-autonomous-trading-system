"""Live Facts 统一查询适配器.

将对 production DB 7 张表的读取收口到一处，
避免在 Phase 3/4 脚本中散落直接 SQL。

所有查询:
  - 支持 symbol / 时间窗口过滤
  - 返回 list[dict] 方便 JSON 序列化
  - 默认 limit=1000 防止全表扫描
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from .contracts import ALL_TABLE_CONTRACTS, TableContract

log = logging.getLogger(__name__)


# ── 通用时间窗口查询 ───────────────────────────────────────────────


def _build_query(
    contract: TableContract,
    *,
    symbol: str | None = None,
    family: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 1000,
    order_desc: bool = True,
) -> tuple[str, dict[str, Any]]:
    """根据契约构建时间窗口查询."""
    conditions: list[str] = []
    params: dict[str, Any] = {}

    if symbol:
        conditions.append(f"{contract.symbol_column} = :symbol")
        params["symbol"] = symbol

    if family and contract.family_column:
        conditions.append(f"{contract.family_column} = :family")
        params["family"] = family

    if start:
        conditions.append(f"{contract.time_column} >= :start_time")
        params["start_time"] = start

    if end:
        conditions.append(f"{contract.time_column} <= :end_time")
        params["end_time"] = end

    where = " AND ".join(conditions) if conditions else "TRUE"
    direction = "DESC" if order_desc else "ASC"

    sql = (
        f"SELECT * FROM {contract.table_name} "  # noqa: S608
        f"WHERE {where} "
        f"ORDER BY {contract.time_column} {direction} "
        f"LIMIT :limit"
    )
    params["limit"] = limit
    return sql, params


def _execute(
    session: Session,
    contract: TableContract,
    *,
    symbol: str | None = None,
    family: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """执行查询并返回 list[dict]."""
    sql, params = _build_query(
        contract, symbol=symbol, family=family,
        start=start, end=end, limit=limit,
    )
    rows = session.execute(text(sql), params).mappings().all()
    return [dict(r) for r in rows]


# ── 各表查询函数 ───────────────────────────────────────────────────


def fetch_sleeve_intents(
    session: Session,
    *,
    symbol: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """查询 strategy_sleeve_intents."""
    return _execute(
        session, ALL_TABLE_CONTRACTS["strategy_sleeve_intents"],
        symbol=symbol, start=start, end=end, limit=limit,
    )


def fetch_allocation_decisions(
    session: Session,
    *,
    symbol: str | None = None,
    family: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """查询 portfolio_allocation_decisions."""
    return _execute(
        session, ALL_TABLE_CONTRACTS["portfolio_allocation_decisions"],
        symbol=symbol, family=family, start=start, end=end, limit=limit,
    )


def fetch_budget_snapshots(
    session: Session,
    *,
    symbol: str | None = None,
    family: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """查询 allocator_budget_snapshots."""
    return _execute(
        session, ALL_TABLE_CONTRACTS["allocator_budget_snapshots"],
        symbol=symbol, family=family, start=start, end=end, limit=limit,
    )


def fetch_reconciliation_snapshots(
    session: Session,
    *,
    symbol: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """查询 reconciliation_state_snapshots."""
    return _execute(
        session, ALL_TABLE_CONTRACTS["reconciliation_state_snapshots"],
        symbol=symbol, start=start, end=end, limit=limit,
    )


def fetch_execution_bundles(
    session: Session,
    *,
    symbol: str | None = None,
    family: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """查询 strategy_execution_bundles."""
    return _execute(
        session, ALL_TABLE_CONTRACTS["strategy_execution_bundles"],
        symbol=symbol, family=family, start=start, end=end, limit=limit,
    )


def fetch_execution_orders(
    session: Session,
    *,
    symbol: str | None = None,
    family: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """查询 execution_orders."""
    return _execute(
        session, ALL_TABLE_CONTRACTS["execution_orders"],
        symbol=symbol, family=family, start=start, end=end, limit=limit,
    )


def fetch_execution_fills(
    session: Session,
    *,
    symbol: str | None = None,
    family: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """查询 execution_fills."""
    return _execute(
        session, ALL_TABLE_CONTRACTS["execution_fills"],
        symbol=symbol, family=family, start=start, end=end, limit=limit,
    )


# ── 健康检查 ───────────────────────────────────────────────────────


def check_tables_health(session: Session) -> dict[str, Any]:
    """检查所有 7 张表是否可读并校验最小必需列.

    Returns
    -------
    dict  包含 all_readable, tables_checked, column_checks, errors
    """
    result: dict[str, Any] = {
        "all_readable": True,
        "tables_checked": {},
        "column_checks": {},
        "errors": [],
    }

    for table_name, contract in ALL_TABLE_CONTRACTS.items():
        table_info: dict[str, Any] = {"readable": False, "row_count": 0}
        try:
            row = session.execute(
                text(f"SELECT COUNT(*) FROM {table_name}")  # noqa: S608
            ).scalar()
            table_info["readable"] = True
            table_info["row_count"] = row or 0
        except Exception as exc:
            table_info["error"] = str(exc)
            result["errors"].append(f"{table_name}: {exc}")
            result["all_readable"] = False

        result["tables_checked"][table_name] = table_info

        # 校验最小必需列
        if table_info["readable"]:
            try:
                cols_sql = (
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = :tbl"
                )
                existing_cols = {
                    r[0] for r in session.execute(
                        text(cols_sql), {"tbl": table_name},
                    ).fetchall()
                }
                missing = set(contract.minimum_required_columns) - existing_cols
                result["column_checks"][table_name] = {
                    "required": len(contract.minimum_required_columns),
                    "found": len(contract.minimum_required_columns) - len(missing),
                    "missing": sorted(missing) if missing else [],
                }
                if missing:
                    result["errors"].append(
                        f"{table_name}: 缺少列 {sorted(missing)}",
                    )
            except Exception as exc:
                result["column_checks"][table_name] = {"error": str(exc)}

    return result


def fetch_latest_timestamps(session: Session) -> dict[str, str | None]:
    """获取每张表最近一条记录的时间戳."""
    timestamps: dict[str, str | None] = {}
    for table_name, contract in ALL_TABLE_CONTRACTS.items():
        try:
            row = session.execute(
                text(
                    f"SELECT MAX({contract.time_column}) FROM {table_name}"  # noqa: S608
                )
            ).scalar()
            timestamps[table_name] = str(row) if row else None
        except Exception as exc:
            log.warning("Failed to fetch latest timestamp for %s: %s", table_name, exc)
            timestamps[table_name] = None
    return timestamps
