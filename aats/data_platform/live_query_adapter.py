"""RDP 只读访问主交易系统 live 事实数据的统一适配器.

所有对 production DB 的读取操作都应通过此模块，
而不是在各 Phase 脚本中直接写 SQL。

设计原则：
  1. RDP 不导入主系统 ORM 模型，保持解耦
  2. 所有查询使用 raw SQL text
  3. 强制只读（readonly 开关 + 不提供写入方法）
  4. 返回 list[dict] 方便 JSON 序列化
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from aats.data_platform.config import ResearchPlatformSettings, get_settings

log = logging.getLogger(__name__)

# ── RDP 需要只读访问的 live 表 ──────────────────────────────────────

LIVE_TABLES: list[str] = [
    "strategy_sleeve_intents",
    "portfolio_allocation_decisions",
    "allocator_budget_snapshots",
    "reconciliation_state_snapshots",
    "strategy_execution_bundles",
    "execution_orders",
    "execution_fills",
]

# ── 表的关键字段契约 ────────────────────────────────────────────────

TABLE_KEY_COLUMNS: dict[str, dict[str, str]] = {
    "strategy_sleeve_intents": {
        "pk": "sleeve_intent_id",
        "time_col": "created_at",
        "symbol_col": "symbol",
    },
    "portfolio_allocation_decisions": {
        "pk": "allocation_id",
        "time_col": "created_at",
        "symbol_col": "symbol",
    },
    "allocator_budget_snapshots": {
        "pk": "budget_snapshot_id",
        "time_col": "created_at",
        "symbol_col": "symbol",
    },
    "reconciliation_state_snapshots": {
        "pk": "snapshot_id",
        "time_col": "created_at",
        "symbol_col": "primary_symbol",
    },
    "strategy_execution_bundles": {
        "pk": "bundle_id",
        "time_col": "created_at",
        "symbol_col": "selected_symbol",
    },
    "execution_orders": {
        "pk": "order_id",
        "time_col": "created_at",
        "symbol_col": "symbol",
    },
    "execution_fills": {
        "pk": "fill_id",
        "time_col": "ingestion_ts",
        "symbol_col": "symbol",
    },
}

# ── 引擎缓存 ───────────────────────────────────────────────────────

_live_engine: Engine | None = None
_live_session_factory: sessionmaker[Session] | None = None


def _get_live_engine(settings: ResearchPlatformSettings | None = None) -> Engine:
    global _live_engine
    if _live_engine is not None:
        return _live_engine

    settings = settings or get_settings()
    if not settings.live_database_url:
        raise RuntimeError(
            "RDP_LIVE_DATABASE_URL 未配置。"
            "请在 .env.research 中设置 production DB 只读连接。"
        )

    _live_engine = create_engine(
        settings.live_database_url,
        pool_size=3,
        max_overflow=5,
        pool_pre_ping=True,
        # 只读标记（非 DB 级强制，逻辑层强制）
        echo=False,
    )
    log.info("已创建 live DB 只读引擎: %s",
             settings.live_database_url.split("@")[-1] if "@" in settings.live_database_url else "***")
    return _live_engine


def _get_live_session_factory(
    settings: ResearchPlatformSettings | None = None,
) -> sessionmaker[Session]:
    global _live_session_factory
    if _live_session_factory is None:
        engine = _get_live_engine(settings)
        _live_session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    return _live_session_factory


@contextlib.contextmanager
def get_live_session(
    settings: ResearchPlatformSettings | None = None,
) -> Iterator[Session]:
    """获取 live DB 只读 session.

    用法:
        with get_live_session() as session:
            rows = fetch_execution_orders(session, symbol="BTC-USDT-SWAP")
    """
    factory = _get_live_session_factory(settings)
    session = factory()
    try:
        yield session
        # 只读：强制 rollback 而非 commit
        session.rollback()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_live_engine() -> None:
    """重置缓存引擎（用于测试）."""
    global _live_engine, _live_session_factory
    if _live_engine is not None:
        _live_engine.dispose()
    _live_engine = None
    _live_session_factory = None


# ── 健康检查 ───────────────────────────────────────────────────────


def check_live_db_health(
    settings: ResearchPlatformSettings | None = None,
) -> dict[str, Any]:
    """检查 live DB 连接和关键表可读性.

    Returns
    -------
    dict  包含 healthy (bool), connection_ok, tables_checked, errors
    """
    result: dict[str, Any] = {
        "healthy": False,
        "connection_ok": False,
        "tables_checked": {},
        "errors": [],
    }

    try:
        engine = _get_live_engine(settings)
    except RuntimeError as exc:
        result["errors"].append(str(exc))
        return result

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            result["connection_ok"] = True
    except Exception as exc:
        result["errors"].append(f"连接失败: {exc}")
        return result

    # 检查每张表是否可读
    with engine.connect() as conn:
        for table in LIVE_TABLES:
            try:
                row = conn.execute(
                    text(f"SELECT COUNT(*) AS cnt FROM {table} LIMIT 1")  # noqa: S608
                ).fetchone()
                count = row[0] if row else 0
                result["tables_checked"][table] = {
                    "readable": True,
                    "row_count_sample": count,
                }
            except Exception as exc:
                result["tables_checked"][table] = {
                    "readable": False,
                    "error": str(exc),
                }
                result["errors"].append(f"表 {table} 不可读: {exc}")

    all_readable = all(
        v.get("readable", False) for v in result["tables_checked"].values()
    )
    result["healthy"] = result["connection_ok"] and all_readable
    return result


# ── 通用时间窗口查询 ───────────────────────────────────────────────


def _build_time_window_query(
    table: str,
    *,
    symbol: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 1000,
    order_desc: bool = True,
) -> tuple[str, dict[str, Any]]:
    """构建时间窗口查询 SQL + 参数."""
    meta = TABLE_KEY_COLUMNS.get(table)
    if meta is None:
        raise ValueError(f"未知表: {table}")

    time_col = meta["time_col"]
    symbol_col = meta["symbol_col"]

    conditions: list[str] = []
    params: dict[str, Any] = {}

    if symbol:
        conditions.append(f"{symbol_col} = :symbol")
        params["symbol"] = symbol

    if start:
        conditions.append(f"{time_col} >= :start_time")
        params["start_time"] = start

    if end:
        conditions.append(f"{time_col} <= :end_time")
        params["end_time"] = end

    where_clause = " AND ".join(conditions) if conditions else "TRUE"
    direction = "DESC" if order_desc else "ASC"

    sql = f"SELECT * FROM {table} WHERE {where_clause} ORDER BY {time_col} {direction} LIMIT :limit"  # noqa: S608
    params["limit"] = limit

    return sql, params


def _execute_query(
    session: Session,
    table: str,
    *,
    symbol: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """执行查询并返回 list[dict]."""
    sql, params = _build_time_window_query(
        table, symbol=symbol, start=start, end=end, limit=limit,
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
    return _execute_query(
        session, "strategy_sleeve_intents",
        symbol=symbol, start=start, end=end, limit=limit,
    )


def fetch_allocation_decisions(
    session: Session,
    *,
    symbol: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """查询 portfolio_allocation_decisions."""
    return _execute_query(
        session, "portfolio_allocation_decisions",
        symbol=symbol, start=start, end=end, limit=limit,
    )


def fetch_budget_snapshots(
    session: Session,
    *,
    symbol: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """查询 allocator_budget_snapshots."""
    return _execute_query(
        session, "allocator_budget_snapshots",
        symbol=symbol, start=start, end=end, limit=limit,
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
    return _execute_query(
        session, "reconciliation_state_snapshots",
        symbol=symbol, start=start, end=end, limit=limit,
    )


def fetch_execution_bundles(
    session: Session,
    *,
    symbol: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """查询 strategy_execution_bundles."""
    return _execute_query(
        session, "strategy_execution_bundles",
        symbol=symbol, start=start, end=end, limit=limit,
    )


def fetch_execution_orders(
    session: Session,
    *,
    symbol: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """查询 execution_orders."""
    return _execute_query(
        session, "execution_orders",
        symbol=symbol, start=start, end=end, limit=limit,
    )


def fetch_execution_fills(
    session: Session,
    *,
    symbol: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """查询 execution_fills."""
    return _execute_query(
        session, "execution_fills",
        symbol=symbol, start=start, end=end, limit=limit,
    )


# ── 聚合查询 ───────────────────────────────────────────────────────


def fetch_recent_fill_stats(
    session: Session,
    *,
    symbol: str | None = None,
    hours: int = 24,
) -> dict[str, Any]:
    """查询最近 N 小时的成交统计.

    用于 Phase 4 execution realism 分析。

    注意: 时间窗口通过 Python 端计算 start_time 并作为绑定参数传入，
    而非在 SQL INTERVAL 字符串中嵌入参数占位符（后者无法被正确参数化）。
    """
    from datetime import timedelta

    start_time = datetime.utcnow() - timedelta(hours=hours)

    conditions = ["ingestion_ts >= :start_time"]
    params: dict[str, Any] = {"start_time": start_time}

    if symbol:
        conditions.append("symbol = :symbol")
        params["symbol"] = symbol

    where = " AND ".join(conditions)
    sql = f"""
        SELECT
            symbol,
            side,
            COUNT(*)                   AS fill_count,
            SUM(fill_qty)              AS total_qty,
            SUM(fill_qty * fill_price) AS total_notional,
            SUM(fee_amount)            AS total_fees,
            AVG(fill_price)            AS avg_price
        FROM execution_fills
        WHERE {where}
        GROUP BY symbol, side
        ORDER BY symbol, side
    """  # noqa: S608

    rows = session.execute(text(sql), params).mappings().all()
    return {"stats": [dict(r) for r in rows], "hours": hours}


def fetch_recent_order_states(
    session: Session,
    *,
    symbol: str | None = None,
    hours: int = 24,
) -> dict[str, Any]:
    """查询最近 N 小时的订单状态分布.

    用于 Phase 3 attribution 分析。

    注意: 时间窗口通过 Python 端计算 start_time 并作为绑定参数传入，
    而非在 SQL INTERVAL 字符串中嵌入参数占位符（后者无法被正确参数化）。
    """
    from datetime import timedelta

    start_time = datetime.utcnow() - timedelta(hours=hours)

    conditions = ["created_at >= :start_time"]
    params: dict[str, Any] = {"start_time": start_time}

    if symbol:
        conditions.append("symbol = :symbol")
        params["symbol"] = symbol

    where = " AND ".join(conditions)
    sql = f"""
        SELECT
            symbol,
            strategy_family,
            state,
            COUNT(*) AS order_count
        FROM execution_orders
        WHERE {where}
        GROUP BY symbol, strategy_family, state
        ORDER BY symbol, strategy_family, state
    """  # noqa: S608

    rows = session.execute(text(sql), params).mappings().all()
    return {"states": [dict(r) for r in rows], "hours": hours}
