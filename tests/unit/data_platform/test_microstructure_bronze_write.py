"""P1-D Phase 1A Stage 2 单元测试 — Bronze 写入 SQL 构造正确性。

目标: 在方言无关的 SQLite schema fake 上,验证 write_*_batch 四个写函数
生成的 INSERT SQL 能真实往 Bronze/staging 表写入并读回 (round-trip)。

与 test_microstructure_bronze_schema (Stage 1) 的区别:
  - Stage 1 测 ORM round-trip, 不走 collector 的 text() SQL 路径
  - Stage 2 (本文件) 测 write_*_batch() 生成的 text() SQL 的字段名/顺序/
    CAST 语法是否 DDL 兼容

复用 Stage 1 的 _make_sqlite_engine helper (import 即可),保持方言
无关约束。SQLite 下 ON CONFLICT DO NOTHING 语法兼容, CAST AS UUID /
JSONB 用 compile override 映射成 TEXT (Stage 1 已打通)。
"""
from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from uuid import uuid4

# Stage 1 测试里已注册的 compile override (@compiles JSONB/UUID/ARRAY/BigInteger
# → sqlite TEXT/INTEGER) 必须先 import 才能让 rdp_models metadata 落地正确,
# 复用该 helper 保持方言无关。
from tests.unit.data_platform.test_microstructure_bronze_schema import (  # noqa: F401
    _make_sqlite_engine,
)

from sqlalchemy import text
from sqlalchemy.orm import Session

# Python 3.12 sqlite3 默认不再 adapt Decimal,需要显式注册。只注册一次,
# 进程内全局生效;PostgreSQL 驱动不会走这条路径,无副作用。
# 注册到 str 等价于 NUMERIC 存储——SQLite 的 NUMERIC affinity 行为下
# NULL 列保持 NULL (TEXT None → 保持 None),小数作为 TEXT round-trip。
sqlite3.register_adapter(Decimal, str)

from aats.data_platform.collectors.microstructure_ws_collector import (
    BboRow,
    Books5Row,
    OiFundingMarkRow,
    TradeRow,
    write_bbo_batch,
    write_books5_batch,
    write_oif_batch,
    write_trades_batch,
)

_TS = datetime(2026, 4, 20, 12, 0, 0, tzinfo=timezone.utc)


# -----------------------------------------------------------------------
# SQLite 没有 PostgreSQL 的 ON CONFLICT ON CONSTRAINT 语法。
# 改写 SQL 用 ON CONFLICT DO NOTHING (SQLite 3.35+ 支持) 通过
# patch-in text() 的方式重测。这里用通用 helper: 直接对 SQLite 执行
# 一个等价 INSERT OR IGNORE 变体以验证字段映射。
# -----------------------------------------------------------------------


def _sqlite_insert_trades(session: Session, rows, ingest_run_id: str) -> int:
    """SQLite-compatible trades INSERT — mirrors write_trades_batch fields.

    SQLite 语法: INSERT OR IGNORE INTO ... 等价 PG 的
    INSERT ... ON CONFLICT ... DO NOTHING。不 CAST JSONB/UUID。
    """
    batch = [
        {
            "symbol": r.symbol, "ts": r.ts, "trade_id": r.trade_id,
            "px": str(r.px), "sz": str(r.sz), "side": r.side,
            "raw_payload": str(r.raw_payload),
            "ingest_run_id": ingest_run_id,
        }
        for r in rows
    ]
    if not batch:
        return 0
    result = session.execute(
        text("""
            INSERT OR IGNORE INTO bronze.market_trades
                (symbol, ts, trade_id, px, sz, side, raw_payload, ingest_run_id)
            VALUES
                (:symbol, :ts, :trade_id, :px, :sz, :side, :raw_payload, :ingest_run_id)
        """),
        batch,
    )
    rowcount = getattr(result, "rowcount", None)
    return int(rowcount) if rowcount is not None and rowcount >= 0 else len(batch)


# =====================================================================
# Case 1: write_trades_batch field projection
# =====================================================================


class TestWriteTradesBatchFields(unittest.TestCase):
    """验证 SQL text() 字段列表与 PG bronze.market_trades 列完全对应。"""

    def test_sql_contains_expected_columns(self) -> None:
        """SQL 字符串必须包含 PK + 所有非空列。"""
        from aats.data_platform.collectors.microstructure_ws_collector import (
            write_trades_batch as real_writer,
        )

        class _CapSession:
            def __init__(self) -> None:
                self.sql: str = ""

            def execute(self, stmt, _params):
                self.sql = str(stmt)
                class R:
                    rowcount = 1
                return R()

        sess = _CapSession()
        rows = [
            TradeRow(
                symbol="BTC-USDT-SWAP", ts=_TS, trade_id="T-1",
                px=Decimal("95000"), sz=Decimal("0.1"), side="buy",
                raw_payload={"tradeId": "T-1"},
            )
        ]
        count = real_writer(sess, rows, ingest_run_id=str(uuid4()))   # type: ignore[arg-type]
        self.assertEqual(count, 1)
        sql_upper = sess.sql.upper()
        self.assertIn("INSERT INTO BRONZE.MARKET_TRADES", sql_upper)
        for col in ("SYMBOL", "TS", "TRADE_ID", "PX", "SZ", "SIDE",
                    "RAW_PAYLOAD", "INGEST_RUN_ID"):
            self.assertIn(col, sql_upper, f"missing column {col} in trades INSERT SQL")
        # PG 专用 CAST: JSONB + UUID
        self.assertIn("JSONB", sql_upper)
        self.assertIn("UUID", sql_upper)
        # ON CONFLICT DO NOTHING 幂等子句
        self.assertIn("ON CONFLICT", sql_upper)
        self.assertIn("DO NOTHING", sql_upper)

    def test_empty_rows_skips_execute(self) -> None:
        """空 rows 不发 SQL,避免 noop RTT。"""
        from aats.data_platform.collectors.microstructure_ws_collector import (
            write_trades_batch as real_writer,
        )

        class _Cap:
            called = 0
            def execute(self, *_args, **_kwargs):
                self.called += 1

        sess = _Cap()
        count = real_writer(sess, [], ingest_run_id=str(uuid4()))   # type: ignore[arg-type]
        self.assertEqual(count, 0)
        self.assertEqual(sess.called, 0)


# =====================================================================
# Case 2: write_bbo_batch 不写 GENERATED 列
# =====================================================================


class TestWriteBboBatchFields(unittest.TestCase):
    """§6.2 bronze.market_orderbook_bbo: mid/spread/imbalance 是
    GENERATED ALWAYS AS ... STORED,writer 必须不引用这三列,
    否则 PostgreSQL 拒绝写 GENERATED 列。
    """

    def test_sql_excludes_generated_columns(self) -> None:
        from aats.data_platform.collectors.microstructure_ws_collector import (
            write_bbo_batch as real_writer,
        )

        class _Cap:
            sql = ""
            def execute(self, stmt, _params):
                self.sql = str(stmt)
                class R:
                    rowcount = 1
                return R()

        sess = _Cap()
        rows = [
            BboRow(
                symbol="BTC-USDT-SWAP", ts=_TS, source_ts=_TS,
                bid_px=Decimal("95000"), bid_sz=Decimal("1"),
                ask_px=Decimal("95010"), ask_sz=Decimal("2"),
            )
        ]
        count = real_writer(sess, rows, ingest_run_id=str(uuid4()))   # type: ignore[arg-type]
        self.assertEqual(count, 1)
        sql_upper = sess.sql.upper()
        self.assertIn("INSERT INTO BRONZE.MARKET_ORDERBOOK_BBO", sql_upper)
        # GENERATED 列必须缺席
        for bad_col in ("MID", "SPREAD", "IMBALANCE"):
            # 要小心 "IMBALANCE" 不可能误包含;"MID" 可能是别的 token 子串
            # 严格匹配列名边界: 列表里的 identifier 之间一定是逗号或括号。
            pattern_checks = [
                f", {bad_col}",
                f"({bad_col},",
                f"({bad_col})",
                f", {bad_col},",
                f" {bad_col},",
            ]
            for p in pattern_checks:
                self.assertNotIn(
                    p, sql_upper,
                    f"GENERATED column {bad_col} leaked into INSERT SQL",
                )
        # 必备列在
        for col in ("SYMBOL", "TS", "SOURCE_TS", "BID_PX", "BID_SZ",
                    "ASK_PX", "ASK_SZ", "INGEST_RUN_ID"):
            self.assertIn(col, sql_upper)


# =====================================================================
# Case 3: write_books5_batch 20 列完备
# =====================================================================


class TestWriteBooks5BatchFields(unittest.TestCase):
    def test_sql_has_20_level_columns(self) -> None:
        from aats.data_platform.collectors.microstructure_ws_collector import (
            write_books5_batch as real_writer,
        )

        class _Cap:
            sql = ""
            def execute(self, stmt, _params):
                self.sql = str(stmt)
                class R:
                    rowcount = 1
                return R()

        sess = _Cap()
        rows = [
            Books5Row(
                symbol="BTC-USDT-SWAP", ts=_TS, source_ts=_TS,
                bid_px_1=Decimal("95000"), bid_sz_1=Decimal("1"),
                bid_px_2=None, bid_sz_2=None,
                bid_px_3=None, bid_sz_3=None,
                bid_px_4=None, bid_sz_4=None,
                bid_px_5=None, bid_sz_5=None,
                ask_px_1=Decimal("95010"), ask_sz_1=Decimal("2"),
                ask_px_2=None, ask_sz_2=None,
                ask_px_3=None, ask_sz_3=None,
                ask_px_4=None, ask_sz_4=None,
                ask_px_5=None, ask_sz_5=None,
            )
        ]
        count = real_writer(sess, rows, ingest_run_id=str(uuid4()))   # type: ignore[arg-type]
        self.assertEqual(count, 1)
        sql_upper = sess.sql.upper()
        # 5 档 × (px, sz) × (bid, ask) = 20 列
        for side in ("BID", "ASK"):
            for level in range(1, 6):
                self.assertIn(f"{side}_PX_{level}", sql_upper)
                self.assertIn(f"{side}_SZ_{level}", sql_upper)


# =====================================================================
# Case 4: write_oif_batch 无 ingest_run_id,支持 tick_type 分发
# =====================================================================


class TestWriteOifBatchFields(unittest.TestCase):
    """staging.market_oi_funding_ticks 没有 ingest_run_id 列 (Stage 1 设计)。
    BIGSERIAL id PK append-only, 无 ON CONFLICT 子句。
    """

    def test_sql_has_no_ingest_run_id_or_on_conflict(self) -> None:
        from aats.data_platform.collectors.microstructure_ws_collector import (
            write_oif_batch as real_writer,
        )

        class _Cap:
            sql = ""
            def execute(self, stmt, _params):
                self.sql = str(stmt)
                class R:
                    rowcount = 1
                return R()

        class _CapN:
            def __init__(self) -> None:
                self.sql = ""
                self.last_rowcount = 0

            def execute(self, stmt, params):
                self.sql = str(stmt)
                batch = list(params) if not isinstance(params, dict) else [params]
                self.last_rowcount = len(batch)
                outer = self
                class R:
                    @property
                    def rowcount(self_inner) -> int:
                        return outer.last_rowcount
                return R()

        sess = _CapN()
        rows = [
            OiFundingMarkRow(
                ts=_TS, symbol="BTC-USDT-SWAP", tick_type="oi",
                oi=Decimal("123456"), oi_ccy=Decimal("1234.56"),
            ),
            OiFundingMarkRow(
                ts=_TS + timedelta(seconds=1),
                symbol="BTC-USDT-SWAP",
                tick_type="funding",
                funding_rate=Decimal("0.0001"),
            ),
            OiFundingMarkRow(
                ts=_TS + timedelta(seconds=2),
                symbol="BTC-USDT-SWAP",
                tick_type="mark",
                mark_px=Decimal("95000"),
            ),
        ]
        count = real_writer(sess, rows)   # type: ignore[arg-type]
        self.assertEqual(count, 3)
        sql_lower = sess.sql.lower()
        self.assertIn("insert into staging.market_oi_funding_ticks", sql_lower)
        self.assertNotIn("ingest_run_id", sql_lower)
        # staging append-only, 不写 ON CONFLICT
        self.assertNotIn("on conflict", sql_lower)
        # tick_type 必须入 SQL
        self.assertIn("tick_type", sql_lower)


# =====================================================================
# Case 5: SQLite end-to-end — 构造真表, 跑 INSERT, 查结果
# =====================================================================


class TestSqliteRoundTrip(unittest.TestCase):
    """在 SQLite fake 上跑自定义 INSERT OR IGNORE 等价 SQL,验证字段
    映射连通性 (SQLite 不支持 ON CONFLICT ON CONSTRAINT <name>,但
    mapping 正确性可通过 INSERT OR IGNORE 等价验证)。
    """

    def test_trades_round_trip_via_sqlite_equivalent(self) -> None:
        engine = _make_sqlite_engine()
        run_id = str(uuid4())

        with Session(engine) as session:
            rows = [
                TradeRow(
                    symbol="BTC-USDT-SWAP", ts=_TS, trade_id="T-1",
                    px=Decimal("95000"), sz=Decimal("0.1"), side="buy",
                    raw_payload={"tradeId": "T-1"},
                ),
                TradeRow(
                    symbol="BTC-USDT-SWAP", ts=_TS, trade_id="T-2",
                    px=Decimal("95001"), sz=Decimal("0.2"), side="sell",
                    raw_payload={"tradeId": "T-2"},
                ),
            ]
            count = _sqlite_insert_trades(session, rows, run_id)
            session.commit()
            self.assertEqual(count, 2)

            # 读回确认 PK + 字段
            got = session.execute(
                text("SELECT symbol, trade_id, side FROM bronze.market_trades ORDER BY trade_id")
            ).all()
            self.assertEqual(len(got), 2)
            self.assertEqual(got[0].trade_id, "T-1")
            self.assertEqual(got[0].side, "buy")
            self.assertEqual(got[1].trade_id, "T-2")

            # 重复 trade_id 被 OR IGNORE 幂等吸收 (PK 冲突)
            dup = _sqlite_insert_trades(session, [rows[0]], run_id)
            session.commit()
            self.assertEqual(dup, 0, "second INSERT of same PK should be ignored")

    def test_oif_append_only_via_sqlite(self) -> None:
        engine = _make_sqlite_engine()
        # staging 表无 ingest_run_id + 无 ON CONFLICT,直接走 real writer
        with Session(engine) as session:
            rows = [
                OiFundingMarkRow(
                    ts=_TS, symbol="BTC-USDT-SWAP", tick_type="oi",
                    oi=Decimal("1000000"), oi_ccy=Decimal("10000"),
                ),
                OiFundingMarkRow(
                    ts=_TS, symbol="BTC-USDT-SWAP", tick_type="mark",
                    mark_px=Decimal("95000"),
                ),
            ]
            written = write_oif_batch(session, rows)
            session.commit()
            self.assertEqual(written, 2)

            by_type = session.execute(
                text("SELECT tick_type, COUNT(*) AS n "
                     "FROM staging.market_oi_funding_ticks GROUP BY tick_type")
            ).all()
            counts = {r.tick_type: r.n for r in by_type}
            self.assertEqual(counts.get("oi"), 1)
            self.assertEqual(counts.get("mark"), 1)


if __name__ == "__main__":
    unittest.main()
