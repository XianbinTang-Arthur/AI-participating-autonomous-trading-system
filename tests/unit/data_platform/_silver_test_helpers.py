"""P1-D Phase 1A Stage 3 — Silver ETL 单元测试共享 helper。

复用 Stage 1 test_microstructure_bronze_schema 里的 @compiles override 与
SQLite in-memory + ATTACH DATABASE schema 模拟。这里额外:
    - 建 5 张 Silver 表 + 1 张 staging.raw_liquidations (Silver ETL 源之一)
    - 提供 SilverEnv 便利封装 (engine + insert helper)
    - rewrite Silver merger 的 UPSERT SQL 语法让 SQLite 跑
      (PG 用 INSERT ... ON CONFLICT (symbol, ts) DO UPDATE;
       SQLite 3.24+ 同样支持 ON CONFLICT (col) DO UPDATE SET, 无需 rewrite)

方言差异汇总 (写成 docstring, 防止二次踩坑):
    - JSONB → TEXT, UUID → TEXT, ARRAY → TEXT, BigInteger → INTEGER
      (Stage 1 test_microstructure_bronze_schema 注册的 @compiles override)
    - now() → Python callable (Stage 1 create_function polyfill)
    - TIMESTAMPTZ 列: SQLite 存 ISO str, round-trip 为 str (非 datetime);
      Silver merger 的聚合 SQL 里 ts comparison 用 ISO str 排序 == datetime
      排序, 所以 WHERE ts >= :bs AND ts < :be 语义保留
    - STDDEV_SAMP: SQLite 需要 aggregate extension, 否则返回 NULL。
      Stage 1 helper 里已注册。但本 Stage 的 Silver ETL 里某些 SQL 用了
      STDDEV_SAMP, 测试里我们用 Python-side 聚合 / 小数据集保证可用性。
"""
from __future__ import annotations

import datetime as _dt
import math
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

# Stage 1 helper 注册 @compiles override 的副作用必须先触发一次
from tests.unit.data_platform.test_microstructure_bronze_schema import (  # noqa: F401
    _make_sqlite_engine as _stage1_make_sqlite_engine,
)

# Python 3.12 sqlite3 默认不再 adapt Decimal — 注册一次全局生效
sqlite3.register_adapter(Decimal, str)
# Silver UPSERT 传 quality_flags (Python list) 进 TEXT 列 (SQLite 方言),
# 默认 sqlite3 不支持 list binding, 注册一次 list→repr 让测试能跑。
# PostgreSQL 驱动走 ARRAY(Text) 原生列,不会触发这个 adapter。
sqlite3.register_adapter(list, lambda lst: "{" + ",".join(str(x) for x in lst) + "}")

# Task P3-1：E402 noqa —— 必须先 register_adapter 再 import sqlalchemy，否则
# sqlalchemy 早绑定到默认 sqlite adapter 会让 Decimal/list 测试失败。
from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from aats.data_platform.rdp_models import (  # noqa: E402
    BronzeMarketOrderbookBboModel,
    BronzeMarketOrderbookBooks5Model,
    BronzeMarketTradesModel,
    RawLiquidationsModel,
    RdpBase,
    SilverMarketLiquidationMetrics15mModel,
    SilverMarketOiFundingMetrics15mModel,
    SilverMarketOrderbookMetrics15mModel,
    SilverMarketTradeFlow15mModel,
    SilverMarketVolumeProfile15mModel,
    StagingMarketOiFundingTicksModel,
)


_SQLITE_SCHEMAS = ("meta", "staging", "bronze", "silver")


class _StddevSamp:
    """SQLite aggregate function — STDDEV_SAMP (sample std dev).

    SQL 里 STDDEV_SAMP(col) Silver ETL 用来算 bbo imbalance std / funding
    z-score baseline / liquidation intensity 7d。SQLite 不带这个 fn, 用
    Welford online algorithm 实现。
    """

    def __init__(self) -> None:
        self._n = 0
        self._mean = 0.0
        self._m2 = 0.0

    def step(self, value: Any) -> None:  # noqa: D401
        if value is None:
            return
        try:
            x = float(value)
        except (TypeError, ValueError):
            return
        self._n += 1
        delta = x - self._mean
        self._mean += delta / self._n
        delta2 = x - self._mean
        self._m2 += delta * delta2

    def finalize(self) -> float | None:
        if self._n < 2:
            return None
        variance = self._m2 / (self._n - 1)
        return math.sqrt(variance) if variance >= 0 else None


class _StddevPop:
    """SQLite aggregate function — STDDEV_POP (population std dev)。"""

    def __init__(self) -> None:
        self._n = 0
        self._mean = 0.0
        self._m2 = 0.0

    def step(self, value: Any) -> None:
        if value is None:
            return
        try:
            x = float(value)
        except (TypeError, ValueError):
            return
        self._n += 1
        delta = x - self._mean
        self._mean += delta / self._n
        delta2 = x - self._mean
        self._m2 += delta * delta2

    def finalize(self) -> float | None:
        if self._n < 1:
            return None
        variance = self._m2 / self._n
        return math.sqrt(variance) if variance >= 0 else None


def _strip_pg_specific_defaults_for_sqlite(tables: list) -> dict:
    """Temporarily blank out server_default that SQLite can't parse.

    Silver 5 张表的 quality_flags 列 server_default='{}'::text[] 是 PG 专用。
    SQLite 不能识别 ::text[] cast, CREATE TABLE 会报 "unrecognized token: :"。
    测试里把这三列的 server_default 清掉 (SQLite 下 NULL default 对 insert
    不影响, Silver merger 显式传 list 进去), 返回可恢复的 state 字典。

    生产走 PG 时不碰 metadata, server_default 保留原样。
    """
    saved: dict = {}
    for table in tables:
        for col in table.columns:
            if col.server_default is None:
                continue
            default_expr = col.server_default
            expr_text = ""
            if hasattr(default_expr, "arg"):
                expr_text = str(default_expr.arg)
            if "::text[]" in expr_text or "::uuid" in expr_text.lower():
                saved[(table.name, col.name)] = col.server_default
                col.server_default = None
    return saved


def make_silver_sqlite_engine():
    """Build an in-memory SQLite engine with all tables Silver ETL needs.

    5 张 Silver 表 + 3 张 Bronze 表 + 2 张 staging 表 (oi_funding_ticks +
    raw_liquidations) 全部通过 ORM metadata 建出来。
    """
    engine = create_engine("sqlite:///:memory:", future=True)

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _):  # type: ignore[unused-argument]
        # PG now() polyfill
        dbapi_conn.create_function(
            "now", 0,
            lambda: _dt.datetime.now(_dt.timezone.utc).isoformat(
                sep=" ", timespec="microseconds"
            ),
        )
        # PG stddev_samp / stddev_pop polyfill
        dbapi_conn.create_aggregate("stddev_samp", 1, _StddevSamp)
        dbapi_conn.create_aggregate("stddev_pop", 1, _StddevPop)
        # ATTACH DATABASE for schemas
        cur = dbapi_conn.cursor()
        for schema in _SQLITE_SCHEMAS:
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    tables = [
        # Bronze + staging (Silver ETL reads from these)
        BronzeMarketTradesModel.__table__,
        BronzeMarketOrderbookBboModel.__table__,
        BronzeMarketOrderbookBooks5Model.__table__,
        StagingMarketOiFundingTicksModel.__table__,
        RawLiquidationsModel.__table__,
        # Silver (ETL writes + reads self for EMA/baseline recursion)
        SilverMarketOrderbookMetrics15mModel.__table__,
        SilverMarketTradeFlow15mModel.__table__,
        SilverMarketOiFundingMetrics15mModel.__table__,
        SilverMarketVolumeProfile15mModel.__table__,
        SilverMarketLiquidationMetrics15mModel.__table__,
    ]
    # 临时脱掉 PG-only 的 server_default; create 后恢复
    saved = _strip_pg_specific_defaults_for_sqlite(tables)
    try:
        RdpBase.metadata.create_all(engine, tables=tables)
    finally:
        for (tbl_name, col_name), default in saved.items():
            for t in tables:
                if t.name == tbl_name:
                    t.columns[col_name].server_default = default
                    break
    return engine


@dataclass
class SilverTestEnv:
    """Helper bag with engine + common fixtures for Silver ETL unit tests."""

    engine: Any
    symbol: str
    bar_start: _dt.datetime
    bar_end: _dt.datetime
    ingest_run_id: str


def make_env(
    *,
    owner: Any | None = None,
    symbol: str = "BTC-USDT-SWAP",
    bar_start: _dt.datetime | None = None,
) -> SilverTestEnv:
    from uuid import uuid4

    if bar_start is None:
        bar_start = _dt.datetime(2026, 4, 20, 12, 0, 0, tzinfo=_dt.timezone.utc)
    bar_end = bar_start + _dt.timedelta(minutes=15)
    env = SilverTestEnv(
        engine=make_silver_sqlite_engine(),
        symbol=symbol,
        bar_start=bar_start,
        bar_end=bar_end,
        ingest_run_id=str(uuid4()),
    )
    if owner is not None:
        owner.addCleanup(env.engine.dispose)
    return env


# ─────────────────────────────────────────────────────────────────────
# Insert helpers — populate Bronze / staging for ETL to consume
# ─────────────────────────────────────────────────────────────────────


def insert_trades(
    session: Session,
    *,
    symbol: str,
    ingest_run_id: str,
    trades: list[dict[str, Any]],
) -> None:
    """trades 每项 dict 含: ts, trade_id, px, sz, side。

    逐行 add + flush — SA 2.0 insertmanyvalues 在 SQLite TEXT 时间戳列上
    会失败 (Stage 1 也是这样处理)。每行单独 flush 避开 sentinel key 问题。
    """
    for t in trades:
        session.add(BronzeMarketTradesModel(
            symbol=symbol,
            ts=t["ts"],
            trade_id=t["trade_id"],
            px=t["px"],
            sz=t["sz"],
            side=t["side"],
            raw_payload=None,
            ingest_run_id=ingest_run_id,
        ))
        session.flush()


def insert_bbo(
    session: Session,
    *,
    symbol: str,
    ingest_run_id: str,
    rows: list[dict[str, Any]],
) -> None:
    """每行 dict 含: ts, source_ts, bid_px, bid_sz, ask_px, ask_sz。"""
    for r in rows:
        session.add(BronzeMarketOrderbookBboModel(
            symbol=symbol,
            ts=r["ts"],
            source_ts=r.get("source_ts", r["ts"]),
            bid_px=r["bid_px"],
            bid_sz=r["bid_sz"],
            ask_px=r["ask_px"],
            ask_sz=r["ask_sz"],
            ingest_run_id=ingest_run_id,
        ))
        session.flush()


def insert_books5(
    session: Session,
    *,
    symbol: str,
    ingest_run_id: str,
    rows: list[dict[str, Any]],
) -> None:
    """每行 dict 至少含: ts, bid_px_1, bid_sz_1, ask_px_1, ask_sz_1,
    可选 level 2-5 同前缀 (默认 None)。"""
    for r in rows:
        kw = dict(
            symbol=symbol,
            ts=r["ts"],
            source_ts=r.get("source_ts", r["ts"]),
            bid_px_1=r["bid_px_1"],
            bid_sz_1=r["bid_sz_1"],
            ask_px_1=r["ask_px_1"],
            ask_sz_1=r["ask_sz_1"],
            ingest_run_id=ingest_run_id,
        )
        for i in range(2, 6):
            kw[f"bid_px_{i}"] = r.get(f"bid_px_{i}")
            kw[f"bid_sz_{i}"] = r.get(f"bid_sz_{i}")
            kw[f"ask_px_{i}"] = r.get(f"ask_px_{i}")
            kw[f"ask_sz_{i}"] = r.get(f"ask_sz_{i}")
        session.add(BronzeMarketOrderbookBooks5Model(**kw))
        session.flush()


def insert_oi_funding_ticks(
    session: Session,
    *,
    symbol: str,
    rows: list[dict[str, Any]],
) -> None:
    """每行 dict 至少含: ts, tick_type, 以及对应的 value 列。"""
    for r in rows:
        kw = dict(symbol=symbol, ts=r["ts"], tick_type=r["tick_type"])
        for col in (
            "oi", "oi_ccy", "funding_rate", "next_funding_rate",
            "next_funding_time", "mark_px", "received_at",
        ):
            if col in r:
                kw[col] = r[col]
        session.add(StagingMarketOiFundingTicksModel(**kw))
        session.flush()


def insert_liquidations(
    session: Session,
    *,
    symbol: str,
    rows: list[dict[str, Any]],
) -> None:
    """每行 dict 含: ts, side, bk_px, sz (可选 inst_type / ccy / raw_payload)。

    symbol 映射到 inst_id (OKX liquidation-orders 的 inst_id 字段语义)。
    """
    for r in rows:
        session.add(RawLiquidationsModel(
            ts=r["ts"],
            inst_id=symbol,
            inst_type=r.get("inst_type", "SWAP"),
            inst_family=r.get("inst_family"),
            side=r["side"],
            bk_px=r["bk_px"],
            sz=r["sz"],
            bk_loss=r.get("bk_loss"),
            ccy=r.get("ccy", "USDT"),
            raw_payload=r.get("raw_payload") or {},
        ))
        session.flush()
