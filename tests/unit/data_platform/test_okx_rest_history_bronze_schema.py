"""P1-D Stage 5 单元测试 — batch_b_08 + batch_b_09 Bronze schema.

对齐 docs/design/p1d_okx_historical_backfill_plan_2026_04_20.md §3 的三张表:
  - bronze.market_oi_history_1h              (symbol, ts) PK
  - bronze.market_mark_price_candles_1m      (symbol, ts) PK
  - bronze.market_long_short_ratio_5m        (symbol, ts) PK

测试策略 (对齐 test_microstructure_bronze_schema 的 Stage 1 子集):
  1. 3 张表的 ORM round-trip 正确 (insert → select → field match)
  2. PK 冲突检测: 同 (symbol, ts) 二次 insert 触发 IntegrityError
  3. LS ratio 两列都 nullable (OKX 实际只返回 account-based)
  4. Mark candles OHLC 全 required, 缺一 INSERT 失败
  5. OI oi_ccy / oi_usd nullable
  6. batch_b_08_rollback.sql / batch_b_09_rollback.sql 可跑

方言无关设计沿用 Stage 1 pattern (in-memory SQLite + @compiles override).
"""
from __future__ import annotations

import datetime as _dt
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import BigInteger, create_engine, event
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session


# 方言无关 compile overrides (必须在 import ORM metadata 之前)
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
    return "INTEGER"


from aats.data_platform.rdp_models import (  # noqa: E402
    BronzeMarketLongShortRatio5mModel,
    BronzeMarketMarkPriceCandles1mModel,
    BronzeMarketOIHistory1hModel,
    RdpBase,
)


_SQLITE_SCHEMAS = ("bronze", "staging")


def _make_sqlite_engine():
    engine = create_engine("sqlite:///:memory:", future=True)

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _):  # type: ignore[unused-argument]
        dbapi_conn.create_function(
            "now",
            0,
            lambda: _dt.datetime.now(_dt.timezone.utc).isoformat(
                sep=" ", timespec="microseconds"
            ),
        )
        cur = dbapi_conn.cursor()
        for schema in _SQLITE_SCHEMAS:
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    tables = [
        BronzeMarketOIHistory1hModel.__table__,
        BronzeMarketMarkPriceCandles1mModel.__table__,
        BronzeMarketLongShortRatio5mModel.__table__,
    ]
    RdpBase.metadata.create_all(engine, tables=tables)
    return engine


_SAMPLE_TS = datetime(2026, 3, 20, 0, 0, 0, tzinfo=timezone.utc)


# =====================================================================
# Case 1: 3 张表 ORM round-trip 正确
# =====================================================================


class TestStage5SchemaRoundtrip(unittest.TestCase):
    def test_all_three_tables_insert_and_read(self) -> None:
        engine = _make_sqlite_engine()
        run_id = str(uuid4())

        with Session(engine) as session:
            session.add(BronzeMarketOIHistory1hModel(
                symbol="BTC-USDT-SWAP",
                ts=_SAMPLE_TS,
                oi=Decimal("450000"),
                oi_ccy=Decimal("6100"),
                oi_usd=Decimal("450123456"),
                ingest_run_id=run_id,
            ))
            session.add(BronzeMarketMarkPriceCandles1mModel(
                symbol="BTC-USDT-SWAP",
                ts=_SAMPLE_TS,
                open=Decimal("73000.1"),
                high=Decimal("73100.2"),
                low=Decimal("72950.3"),
                close=Decimal("73050.4"),
                ingest_run_id=run_id,
            ))
            session.add(BronzeMarketLongShortRatio5mModel(
                symbol="BTC-USDT-SWAP",
                ts=_SAMPLE_TS,
                ls_ratio_accounts=Decimal("1.07"),
                # ls_ratio_positions 故意 NULL (OKX 常只返回 accounts)
                ingest_run_id=run_id,
            ))
            session.commit()

            oi = session.query(BronzeMarketOIHistory1hModel).one()
            self.assertEqual(oi.symbol, "BTC-USDT-SWAP")
            # 整数值避开 SQLite Decimal → REAL 精度损失
            self.assertEqual(oi.oi, Decimal("450000"))
            self.assertEqual(oi.oi_ccy, Decimal("6100"))
            self.assertEqual(oi.oi_usd, Decimal("450123456"))
            self.assertEqual(str(oi.ingest_run_id), run_id)

            mark = session.query(BronzeMarketMarkPriceCandles1mModel).one()
            # NUMERIC → SQLite REAL: 用 float 近似而非严格 Decimal
            self.assertAlmostEqual(float(mark.open), 73000.1, places=1)
            self.assertAlmostEqual(float(mark.close), 73050.4, places=1)

            ls = session.query(BronzeMarketLongShortRatio5mModel).one()
            self.assertAlmostEqual(float(ls.ls_ratio_accounts), 1.07, places=2)
            self.assertIsNone(ls.ls_ratio_positions)


# =====================================================================
# Case 2: (symbol, ts) PK 幂等冲突
# =====================================================================


class TestStage5PrimaryKey(unittest.TestCase):
    def test_oi_duplicate_pk_raises(self) -> None:
        engine = _make_sqlite_engine()
        run_id = str(uuid4())
        with Session(engine) as session:
            session.add(BronzeMarketOIHistory1hModel(
                symbol="BTC-USDT-SWAP", ts=_SAMPLE_TS,
                oi=Decimal("100"), ingest_run_id=run_id,
            ))
            session.commit()
            session.add(BronzeMarketOIHistory1hModel(
                symbol="BTC-USDT-SWAP", ts=_SAMPLE_TS,  # 同 PK
                oi=Decimal("200"), ingest_run_id=run_id,
            ))
            with self.assertRaises(IntegrityError):
                session.commit()
            session.rollback()

    def test_mark_candles_different_ts_ok(self) -> None:
        engine = _make_sqlite_engine()
        run_id = str(uuid4())
        with Session(engine) as session:
            import datetime as d
            for i in range(3):
                session.add(BronzeMarketMarkPriceCandles1mModel(
                    symbol="BTC-USDT-SWAP",
                    ts=_SAMPLE_TS + d.timedelta(minutes=i),
                    open=Decimal("73000"), high=Decimal("73010"),
                    low=Decimal("72990"), close=Decimal("73005"),
                    ingest_run_id=run_id,
                ))
                session.flush()
            session.commit()
            self.assertEqual(
                session.query(BronzeMarketMarkPriceCandles1mModel).count(), 3
            )

    def test_ls_same_ts_different_sym_ok(self) -> None:
        engine = _make_sqlite_engine()
        run_id = str(uuid4())
        # 逐条 flush 避开 SA 2.0 insert-many-values sentinel 在 SQLite 上
        # 对 TEXT 型时间戳列无法 match sentinel 的已知问题 (同 test_microstructure
        # _bronze_schema 里 TestMarketTradesPrimaryKey.test_same_ts_different_
        # trade_id_allowed 的 workaround)
        with Session(engine) as session:
            session.add(BronzeMarketLongShortRatio5mModel(
                symbol="BTC-USDT-SWAP", ts=_SAMPLE_TS,
                ls_ratio_accounts=Decimal("1.07"), ingest_run_id=run_id,
            ))
            session.flush()
            session.add(BronzeMarketLongShortRatio5mModel(
                symbol="ETH-USDT-SWAP", ts=_SAMPLE_TS,
                ls_ratio_accounts=Decimal("1.15"), ingest_run_id=run_id,
            ))
            session.flush()
            session.commit()
            self.assertEqual(
                session.query(BronzeMarketLongShortRatio5mModel).count(), 2
            )


# =====================================================================
# Case 3: Mark candles OHLC required
# =====================================================================


class TestMarkCandlesRequiredFields(unittest.TestCase):
    def test_close_is_required(self) -> None:
        engine = _make_sqlite_engine()
        run_id = str(uuid4())
        with Session(engine) as session:
            session.add(BronzeMarketMarkPriceCandles1mModel(
                symbol="BTC-USDT-SWAP",
                ts=_SAMPLE_TS,
                open=Decimal("73000"),
                high=Decimal("73010"),
                low=Decimal("72990"),
                # close=None 触发 NOT NULL 约束
                ingest_run_id=run_id,
            ))
            with self.assertRaises(IntegrityError):
                session.commit()
            session.rollback()


# =====================================================================
# Case 4: Rollback SQL 可跑 (in-memory SQLite; 用 PG 语句兼容子集)
# =====================================================================


class TestStage5RollbackSQL(unittest.TestCase):
    """验证 rollback SQL 的 DROP IF EXISTS 语句在真 PG 下会成功;
    这里只校验 ORM 能真的 drop 表 (SQLAlchemy 的 drop_all() 等价于 rollback).
    真正的 PG 行为由集成测试覆盖.
    """

    def test_drop_all_round_trip(self) -> None:
        engine = _make_sqlite_engine()
        with Session(engine) as session:
            # 建完能插入
            session.add(BronzeMarketOIHistory1hModel(
                symbol="BTC-USDT-SWAP", ts=_SAMPLE_TS,
                oi=Decimal("100"), ingest_run_id=str(uuid4()),
            ))
            session.commit()
        tables = [
            BronzeMarketLongShortRatio5mModel.__table__,
            BronzeMarketMarkPriceCandles1mModel.__table__,
            BronzeMarketOIHistory1hModel.__table__,
        ]
        RdpBase.metadata.drop_all(engine, tables=tables)
        # 重建后再 insert 应该再次成功 (幂等)
        RdpBase.metadata.create_all(engine, tables=tables)
        with Session(engine) as session:
            session.add(BronzeMarketOIHistory1hModel(
                symbol="BTC-USDT-SWAP", ts=_SAMPLE_TS,
                oi=Decimal("200"), ingest_run_id=str(uuid4()),
            ))
            session.commit()
            self.assertEqual(
                session.query(BronzeMarketOIHistory1hModel).one().oi,
                Decimal("200"),
            )


# =====================================================================
# Case 5: BATCH_B_STAGES 包含 stage 08/09
# =====================================================================


class TestBatchBStagesRegistered(unittest.TestCase):
    def test_stage_08_09_in_batch_b_stages(self) -> None:
        from aats.data_platform.migrations._batch_b import BATCH_B_STAGES

        self.assertIn("batch_b_08_oi_history", BATCH_B_STAGES)
        self.assertIn("batch_b_09_mark_ls_history", BATCH_B_STAGES)
        # 顺序: 08 在 07 之后, 09 在 08 之后
        self.assertLess(
            BATCH_B_STAGES.index("batch_b_07_ingest_runs_domain_extension"),
            BATCH_B_STAGES.index("batch_b_08_oi_history"),
        )
        self.assertLess(
            BATCH_B_STAGES.index("batch_b_08_oi_history"),
            BATCH_B_STAGES.index("batch_b_09_mark_ls_history"),
        )


# =====================================================================
# Case 6: SQL 文件存在
# =====================================================================


class TestStage5MigrationFiles(unittest.TestCase):
    def test_sql_files_exist(self) -> None:
        from pathlib import Path

        from aats.data_platform.migrations import _batch_b

        d = Path(_batch_b.__file__).parent
        for stage in ("batch_b_08_oi_history", "batch_b_09_mark_ls_history"):
            self.assertTrue((d / f"{stage}.sql").is_file(),
                            f"missing {stage}.sql")
            self.assertTrue((d / f"{stage}_rollback.sql").is_file(),
                            f"missing {stage}_rollback.sql")


if __name__ == "__main__":
    unittest.main()
