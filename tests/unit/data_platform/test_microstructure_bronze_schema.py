"""P1-D Phase 1A Stage 1 单元测试 — Bronze + staging microstructure schema。

对齐 docs/design/p1d_phase1a_implementation_design_2026_04_20.md §6 的四张表:
  - bronze.market_trades           (symbol, ts, trade_id) PK
  - bronze.market_orderbook_bbo    (symbol, ts) PK + generated mid/spread/imbalance
  - bronze.market_orderbook_books5 (symbol, ts) PK + 20 档展平列
  - staging.market_oi_funding_ticks BIGSERIAL id PK + tick_type discriminator

测试策略(§8.2 Bronze 写入测试的 Stage 1 子集):
  1. 4 张表的 ORM round-trip 基本正确 (insert → select → 字段 match)
  2. PK 冲突检测: 同 (symbol, ts, trade_id) 二次 insert 报 IntegrityError
  3. bbo 的 generated columns (mid / spread / imbalance) 由 DB 自动计算
  4. books5 level 2-5 列允许 NULL (OKX 有时不足 5 档)
  5. staging.market_oi_funding_ticks 的 tick_type CHECK 约束生效
  6. batch_b_05_rollback.sql 可跑 + 表真的 drop

方言无关设计:
  - 用 in-memory SQLite + ATTACH DATABASE schema 模拟 Postgres bronze/staging
  - @compiles override 把 JSONB/UUID/ARRAY/BigInteger 映射到 SQLite 能懂的类型
  - SQLite create_function('now', ...) 补 PostgreSQL now() 函数
  - 不强校验 NUMERIC 小数精度(SQLite Decimal 用 REAL 背后),只验证符号/数量级/NULL
"""
from __future__ import annotations

import datetime as _dt
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from sqlalchemy import BigInteger, create_engine, event, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

# ─────────────────────────────────────────────────────────────────────
# 方言无关 compile overrides: 让 PG 类型在 SQLite 下编译成等价原始类型。
# 必须在 ORM metadata 被编译前注册(import rdp_models 之前)。
# ─────────────────────────────────────────────────────────────────────


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(_type, _compiler, **_kw):  # type: ignore[unused-argument]
    return "TEXT"


@compiles(UUID, "sqlite")
def _compile_uuid_sqlite(_type, _compiler, **_kw):  # type: ignore[unused-argument]
    return "TEXT"


@compiles(ARRAY, "sqlite")
def _compile_array_sqlite(_type, _compiler, **_kw):  # type: ignore[unused-argument]
    return "TEXT"


@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(_type, _compiler, **_kw):  # type: ignore[unused-argument]
    # SQLite 只认 INTEGER PRIMARY KEY 作为 rowid alias (支持 autoincrement)。
    # 把 BIGINT 在 SQLite 下编译为 INTEGER,让 BIGSERIAL id 能 auto-increment。
    return "INTEGER"


# 需要在 compile overrides 注册之后才 import ORM metadata
# (否则 metadata 已经 bake 好 PG-only type 信息)
from aats.data_platform.rdp_models import (  # noqa: E402
    BronzeMarketOrderbookBboModel,
    BronzeMarketOrderbookBooks5Model,
    BronzeMarketTradesModel,
    RdpBase,
    StagingMarketOiFundingTicksModel,
)


_SQLITE_SCHEMAS = ("bronze", "staging")


def _make_sqlite_engine():
    """in-memory SQLite with:
    - ATTACH DATABASE 模拟 bronze/staging schema
    - now() 函数 polyfill (PostgreSQL 默认值)
    """
    engine = create_engine("sqlite:///:memory:", future=True)

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _):  # type: ignore[unused-argument]
        # PG now() polyfill
        dbapi_conn.create_function(
            "now",
            0,
            lambda: _dt.datetime.now(_dt.timezone.utc).isoformat(
                sep=" ", timespec="microseconds"
            ),
        )
        # 模拟 schema
        cur = dbapi_conn.cursor()
        for schema in _SQLITE_SCHEMAS:
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    tables = [
        BronzeMarketTradesModel.__table__,
        BronzeMarketOrderbookBboModel.__table__,
        BronzeMarketOrderbookBooks5Model.__table__,
        StagingMarketOiFundingTicksModel.__table__,
    ]
    RdpBase.metadata.create_all(engine, tables=tables)
    return engine


class _SQLiteEngineTestCase(unittest.TestCase):
    """Own and deterministically dispose each test's in-memory database."""

    def setUp(self) -> None:
        self.engine = _make_sqlite_engine()

    def tearDown(self) -> None:
        self.engine.dispose()


_SAMPLE_TS = datetime(2026, 4, 20, 12, 0, 0, tzinfo=timezone.utc)


# =====================================================================
# Case 1: 4 张表 ORM round-trip 基本正确
# =====================================================================


class TestMicrostructureSchemaRoundtrip(_SQLiteEngineTestCase):
    """§6 的 4 张表每张插入一行后能按字段 round-trip 读回。

    不对 NUMERIC 精度做字符串严格对比(SQLite 用 REAL 背后存 Decimal),
    而用近似 Decimal 比较 or 只断言 non-NULL/符号。
    """

    def test_all_four_tables_insert_and_read(self) -> None:
        run_id = str(uuid4())

        with Session(self.engine) as session:
            session.add(BronzeMarketTradesModel(
                symbol="BTC-USDT-SWAP",
                ts=_SAMPLE_TS,
                trade_id="T-ABC-1",
                px=Decimal("95000.12345"),
                sz=Decimal("1.5"),
                side="buy",
                raw_payload={"source": "okx", "instType": "SWAP"},
                ingest_run_id=run_id,
            ))
            session.add(BronzeMarketOrderbookBboModel(
                symbol="BTC-USDT-SWAP",
                ts=_SAMPLE_TS,
                source_ts=_SAMPLE_TS - timedelta(milliseconds=5),
                bid_px=Decimal("95000"),
                bid_sz=Decimal("1"),
                ask_px=Decimal("95010"),
                ask_sz=Decimal("2"),
                ingest_run_id=run_id,
            ))
            session.add(BronzeMarketOrderbookBooks5Model(
                symbol="BTC-USDT-SWAP",
                ts=_SAMPLE_TS,
                source_ts=_SAMPLE_TS - timedelta(milliseconds=100),
                bid_px_1=Decimal("95000"),
                bid_sz_1=Decimal("1"),
                ask_px_1=Decimal("95010"),
                ask_sz_1=Decimal("2"),
                # 其它 level 故意 NULL
                ingest_run_id=run_id,
            ))
            session.add(StagingMarketOiFundingTicksModel(
                ts=_SAMPLE_TS,
                symbol="BTC-USDT-SWAP",
                tick_type="funding",
                funding_rate=Decimal("0.000150000000"),
                next_funding_rate=Decimal("0.000200000000"),
                next_funding_time=_SAMPLE_TS + timedelta(hours=8),
            ))
            session.commit()

            trade = session.query(BronzeMarketTradesModel).one()
            self.assertEqual(trade.symbol, "BTC-USDT-SWAP")
            self.assertEqual(trade.trade_id, "T-ABC-1")
            self.assertEqual(trade.side, "buy")
            self.assertEqual(str(trade.ingest_run_id), run_id)
            # raw_payload: SQLite 下会 round-trip 成 JSON string,
            # PostgreSQL 下是 dict。只断言 non-None。
            self.assertIsNotNone(trade.raw_payload)

            bbo = session.query(BronzeMarketOrderbookBboModel).one()
            self.assertEqual(bbo.bid_px, Decimal("95000"))
            self.assertEqual(bbo.ask_px, Decimal("95010"))
            # generated columns 由 DB 自动算,非 NULL
            self.assertIsNotNone(bbo.mid)
            self.assertIsNotNone(bbo.spread)

            books5 = session.query(BronzeMarketOrderbookBooks5Model).one()
            self.assertEqual(books5.bid_px_1, Decimal("95000"))
            self.assertIsNone(books5.bid_px_2)     # level 2 为 NULL
            self.assertIsNone(books5.ask_sz_5)

            oif = session.query(StagingMarketOiFundingTicksModel).one()
            self.assertEqual(oif.tick_type, "funding")
            self.assertIsNotNone(oif.id)           # BIGSERIAL autoincrement
            self.assertIsNone(oif.oi)              # non-funding 列 NULL


# =====================================================================
# Case 2: (symbol, ts, trade_id) 复合 PK 幂等冲突
# =====================================================================


class TestMarketTradesPrimaryKey(_SQLiteEngineTestCase):
    """§6.1 的 natural PK (symbol, ts, trade_id) 在重连重发下由 DB 级
    约束做幂等: 同 PK 二次 insert 触发 IntegrityError,对应 ON CONFLICT
    DO NOTHING 的业务语义。
    """

    def test_duplicate_primary_key_raises(self) -> None:
        run_id = str(uuid4())

        with Session(self.engine) as session:
            session.add(BronzeMarketTradesModel(
                symbol="BTC-USDT-SWAP",
                ts=_SAMPLE_TS,
                trade_id="T-DUP",
                px=Decimal("95000"),
                sz=Decimal("1"),
                side="buy",
                ingest_run_id=run_id,
            ))
            session.commit()

            session.add(BronzeMarketTradesModel(
                symbol="BTC-USDT-SWAP",
                ts=_SAMPLE_TS,
                trade_id="T-DUP",    # 完全相同 PK
                px=Decimal("95001"),  # 价格不同不影响 PK 冲突
                sz=Decimal("2"),
                side="sell",
                ingest_run_id=run_id,
            ))
            with self.assertRaises(IntegrityError):
                session.commit()
            session.rollback()

    def test_same_ts_different_trade_id_allowed(self) -> None:
        """OKX 同一 ts 可能有多笔 trade (liquidation cascade)。
        (symbol, ts, trade_id) 复合 PK 允许同 ts 但 trade_id 不同。
        """
        run_id = str(uuid4())

        # 逐条 flush 避开 SA 2.0 insert-many-values sentinel 在 SQLite 上
        # 对 TEXT 型时间戳列无法 match sentinel 的已知问题(PostgreSQL 没有
        # 这个问题,因为 TIMESTAMPTZ 原生类型能 round-trip)。
        with Session(self.engine) as session:
            for tid in ("T-1", "T-2", "T-3"):
                session.add(BronzeMarketTradesModel(
                    symbol="BTC-USDT-SWAP",
                    ts=_SAMPLE_TS,
                    trade_id=tid,
                    px=Decimal("95000"),
                    sz=Decimal("1"),
                    side="buy",
                    ingest_run_id=run_id,
                ))
                session.flush()
            session.commit()

            count = session.query(BronzeMarketTradesModel).count()
            self.assertEqual(count, 3)


# =====================================================================
# Case 3: bbo generated columns 由 DB 层自动计算
# =====================================================================


class TestBboGeneratedColumns(_SQLiteEngineTestCase):
    """§6.2 bronze.market_orderbook_bbo 的 mid / spread / imbalance 是
    GENERATED ALWAYS AS ... STORED,避免 Silver ETL 每次重算。

    单测仅断言非 NULL 且方向正确(mid 在 bid 与 ask 之间、spread 为正、
    imbalance 符号与 (bid_sz - ask_sz) 同),不对 NUMERIC 精度做方言
    依赖断言。
    """

    def test_mid_and_spread_computed_by_db(self) -> None:
        """mid / spread 是整数算术(相加 / 相减),在 SQLite 与 PostgreSQL
        上结果等价 —— 只验证 mid 严格在 bid 与 ask 之间、spread 为正。
        """
        run_id = str(uuid4())

        with Session(self.engine) as session:
            session.add(BronzeMarketOrderbookBboModel(
                symbol="BTC-USDT-SWAP",
                ts=_SAMPLE_TS,
                source_ts=_SAMPLE_TS,
                bid_px=Decimal("95000"),
                bid_sz=Decimal("1"),
                ask_px=Decimal("95010"),
                ask_sz=Decimal("3"),
                ingest_run_id=run_id,
            ))
            session.commit()
            bbo = session.query(BronzeMarketOrderbookBboModel).one()
            # mid 严格在 bid 和 ask 之间 (均匀中点 95005)
            self.assertGreater(float(bbo.mid), 95000.0)
            self.assertLess(float(bbo.mid), 95010.0)
            # spread 为正 (ask > bid)
            self.assertGreater(float(bbo.spread), 0.0)

    def test_imbalance_column_computed_non_null(self) -> None:
        """imbalance = (bid_sz - ask_sz) / (bid_sz + ask_sz) 在 DB 层计算。

        PostgreSQL NUMERIC 除法走任意精度,符号正确(bid_sz > ask_sz 时 > 0);
        SQLite NUMERIC affinity 会把除法降为整数除法(见 sqlite-type-affinity
        规则),结果可能四舍五入为 0 —— 两个方言都保证 non-NULL。

        本单测只断言 imbalance 非 NULL + 属于合法范围 [-1, 1],避免方言
        精度差异引入 false-positive。实盘 PG 的精度行为另由集成测试覆盖
        (Stage 4 的 testcontainers e2e)。
        """
        run_id = str(uuid4())

        with Session(self.engine) as session:
            session.add(BronzeMarketOrderbookBboModel(
                symbol="BTC-USDT-SWAP",
                ts=_SAMPLE_TS,
                source_ts=_SAMPLE_TS,
                bid_px=Decimal("95000"),
                bid_sz=Decimal("5"),
                ask_px=Decimal("95010"),
                ask_sz=Decimal("5"),       # bid_sz == ask_sz → imbalance == 0
                ingest_run_id=run_id,
            ))
            session.commit()
            bbo = session.query(BronzeMarketOrderbookBboModel).one()
            self.assertIsNotNone(bbo.imbalance)
            imb = float(bbo.imbalance)
            self.assertGreaterEqual(imb, -1.0)
            self.assertLessEqual(imb, 1.0)
            # 对称情况下 imbalance 必然是 0 (无论 PG 还是 SQLite)
            self.assertAlmostEqual(imb, 0.0, places=6)


# =====================================================================
# Case 4: check constraint — tick_type 与 side
# =====================================================================


class TestCheckConstraints(_SQLiteEngineTestCase):
    """CHECK (tick_type IN ('oi','funding','mark')) 与 CHECK (side IN
    ('buy','sell')) 的 DB 级约束阻止非法值落库(防御不小心的 parser bug)。
    """

    def test_tick_type_check_rejects_unknown(self) -> None:
        with Session(self.engine) as session:
            session.add(StagingMarketOiFundingTicksModel(
                ts=_SAMPLE_TS,
                symbol="BTC-USDT-SWAP",
                tick_type="unknown_bogus_type",   # 不在 {oi, funding, mark}
            ))
            with self.assertRaises(IntegrityError):
                session.commit()
            session.rollback()

    def test_trade_side_check_rejects_unknown(self) -> None:
        run_id = str(uuid4())

        with Session(self.engine) as session:
            session.add(BronzeMarketTradesModel(
                symbol="BTC-USDT-SWAP",
                ts=_SAMPLE_TS,
                trade_id="T-BAD-SIDE",
                px=Decimal("95000"),
                sz=Decimal("1"),
                side="long",        # 不在 {buy, sell}
                ingest_run_id=run_id,
            ))
            with self.assertRaises(IntegrityError):
                session.commit()
            session.rollback()


# =====================================================================
# Case 5: rollback SQL 可跑 — 表真的 drop
# =====================================================================


class TestRollbackSql(_SQLiteEngineTestCase):
    """batch_b_05_rollback.sql 必须 DROP 4 张表,且不 DROP schema 本身
    (schema 可能被其他 stage 共用)。

    在 Postgres 上 rollback SQL 是 DROP TABLE IF EXISTS + schema 前缀;
    在 SQLite 下用 ATTACH schema 等价语义。测试: 建表 → 跑 rollback SQL
    → 查表已不在。
    """

    _ROLLBACK_PATH = (
        Path(__file__).resolve().parents[3]
        / "aats" / "data_platform" / "migrations" / "batch_b_05_rollback.sql"
    )

    def test_rollback_drops_all_four_tables(self) -> None:
        # 生产 SQL 在 PG 有 BEGIN / COMMIT,SQLite 也支持,直接原样 split 执行
        self.assertTrue(self._ROLLBACK_PATH.exists(), f"missing {self._ROLLBACK_PATH}")
        sql_text = self._ROLLBACK_PATH.read_text(encoding="utf-8")

        # 初始确认表存在
        with self.engine.connect() as conn:
            for schema, tbl in (
                ("bronze", "market_trades"),
                ("bronze", "market_orderbook_bbo"),
                ("bronze", "market_orderbook_books5"),
                ("staging", "market_oi_funding_ticks"),
            ):
                row = conn.execute(
                    text(
                        f"SELECT name FROM {schema}.sqlite_master "
                        f"WHERE type='table' AND name='{tbl}'"
                    )
                ).fetchone()
                self.assertIsNotNone(row, f"{schema}.{tbl} should exist before rollback")

        # 执行 rollback SQL: SQLite 不支持嵌套 BEGIN,且不能整块送多行注释
        # 的 statement; 先过滤注释行 + 空行再按 `;` 切, BEGIN / COMMIT 外壳
        # 也丢掉(SA 已经在 engine.begin 里管理事务)。生产 PG 走原始文件。
        cleaned_lines: list[str] = []
        for line in sql_text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("--"):
                continue
            cleaned_lines.append(line)
        cleaned_sql = "\n".join(cleaned_lines)

        stmts: list[str] = []
        for raw in cleaned_sql.split(";"):
            stmt = raw.strip()
            if not stmt:
                continue
            upper = stmt.upper()
            if upper == "BEGIN" or upper == "COMMIT":
                continue
            stmts.append(stmt)

        with self.engine.begin() as conn:
            for stmt in stmts:
                # 逐条执行, DROP TABLE IF EXISTS 对 SQLite 同样生效
                conn.execute(text(stmt))

        with self.engine.connect() as conn:
            for schema, tbl in (
                ("bronze", "market_trades"),
                ("bronze", "market_orderbook_bbo"),
                ("bronze", "market_orderbook_books5"),
                ("staging", "market_oi_funding_ticks"),
            ):
                row = conn.execute(
                    text(
                        f"SELECT name FROM {schema}.sqlite_master "
                        f"WHERE type='table' AND name='{tbl}'"
                    )
                ).fetchone()
                self.assertIsNone(row, f"{schema}.{tbl} should be dropped")


# =====================================================================
# Case 6: BATCH_B_STAGES 注册核对 — stage 5 在 tuple 末尾
# =====================================================================


class TestBatchBRegistration(unittest.TestCase):
    """§9 Day 2 WBS 要求把 batch_b_05 append 到 BATCH_B_STAGES。

    这个轻量测试防止后续 refactor 意外丢弃 stage 5 注册,导致 deploy
    时 migration 漏跑。
    """

    def test_batch_b_05_registered_last(self) -> None:
        """P1-D Phase 1A Stage 1 注册检查。

        Stage 3 (batch_b_06_silver_microstructure) 追加后, stage 5 自然
        不再是 tuple 末尾。该断言的原始意图 — "新 stage 以 append 形式入
        tuple, 不随意插入中间" — 仍保留: 验证 stage 5 在 stage 6 之前。
        """
        from aats.data_platform.migrations._batch_b import BATCH_B_STAGES

        self.assertIn("batch_b_05_microstructure", BATCH_B_STAGES)
        idx_05 = BATCH_B_STAGES.index("batch_b_05_microstructure")
        # stage 5 必须在 stage 6 之前 (严格 append 顺序)
        # 若未来 stage 7 加入, 本断言仍成立
        if "batch_b_06_silver_microstructure" in BATCH_B_STAGES:
            idx_06 = BATCH_B_STAGES.index("batch_b_06_silver_microstructure")
            self.assertLess(
                idx_05, idx_06,
                "stage 5 必须在 stage 6 之前 (append 顺序)",
            )
        else:
            # Stage 3 未 merge 前的旧状态 — stage 5 应该是最后一项
            self.assertEqual(
                BATCH_B_STAGES[-1],
                "batch_b_05_microstructure",
                "stage 5 必须是 tuple 的最后一项,保持严格 append 顺序",
            )


if __name__ == "__main__":
    unittest.main()
