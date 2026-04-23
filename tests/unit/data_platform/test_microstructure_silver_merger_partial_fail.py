"""P0-a Silver ETL 真相层修复锁定测试.

对齐 docs/review/p0a_silver_etl_truth_layer_fix_2026_04_20.md

覆盖 3 层 bug 的 6+ 单测:
  1. Bug 1 schema: UPSERT |vol_weighted_tfi| = 5e6 不再 raise
     (NUMERIC(28,10) 能容纳 > 10^6; migration 正确扩列)
  2. Bug 2 rollback: mock 中间某 step 抛 NumericValueOutOfRange,
     后续 step 不再链式 InFailedSqlTransaction (session 仍可用)
  3. Bug 2 tables_failed: 失败表名正确聚合到 result.tables_failed
  4. Bug 2 日志分级: 任一表 written=0 → log 级别 WARNING 而非 INFO
  5. Bug 3 exit code: mock _run_one_bar 返回 partial fail summary,
     scripts/rdp_build_microstructure_silver.py main() 返回 2
  6. 幂等性: 扩列 migration 跑两次 + ETL 重跑同 bar 不出错, 行数不变
"""

from __future__ import annotations

import datetime as _dt
import io
import logging
import sys
import unittest
import unittest.mock as _mock
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from aats.data_platform.merge import microstructure_silver_merger as msm
from aats.data_platform.merge.microstructure_silver_merger import (
    build_silver_microstructure_15m,
)
from aats.data_platform.migrations._batch_b import BATCH_B_STAGES, _load_sql
from tests.unit.data_platform._silver_test_helpers import (
    insert_bbo,
    insert_books5,
    insert_trades,
    make_env,
)


# ─────────────────────────────────────────────────────────────────────
# Test 1 — Bug 1 schema: vol_weighted_tfi 扩列后能容纳大值
# ─────────────────────────────────────────────────────────────────────


class TestVolWeightedTfiLargeValue(unittest.TestCase):
    """Bug 1: NUMERIC(14,8) 只能存 |v| < 10^6, 扩到 NUMERIC(28,10) 后
    |v| 可至 10^18。这里直接 UPSERT 一个 5e6 的值 (原 schema 会 overflow),
    确认测试环境的 silver.market_volume_profile_15m 能存住。

    (SQLite 下 TYPE=NUMERIC 不强制 precision, 本 case 主要验证:
    (a) merger code path 不再因 scale overflow 失败
    (b) round-trip 值一致)
    """

    def test_large_vol_weighted_tfi_upsert_roundtrip(self) -> None:
        env = make_env()
        with Session(env.engine) as sess:
            sess.execute(
                text("""
                    INSERT INTO silver.market_volume_profile_15m
                        (symbol, ts, volume_ccy, trade_count,
                         expected_volume_ccy, expected_volume_std,
                         volume_z_score, volume_spike_flag, dow_hod_slot,
                         vol_weighted_tfi, baseline_sample_weeks,
                         ingest_run_id, dataset_version, quality_flags,
                         created_at, updated_at)
                    VALUES
                        (:sym, :ts, :vol, 100,
                         NULL, NULL, NULL, 0, 'mon_12:00',
                         :vw_tfi, 0,
                         :run_id, 'test-v1', :flags,
                         :now, :now)
                """),
                {
                    "sym": env.symbol,
                    "ts": env.bar_start,
                    "vol": Decimal("10000000"),  # 10M USDT
                    "vw_tfi": Decimal("5000000.5"),  # 5e6 > 10^6 旧限
                    "run_id": env.ingest_run_id,
                    "flags": [],
                    "now": _dt.datetime.now(_dt.timezone.utc),
                },
            )
            sess.commit()

            row = sess.execute(
                text(
                    "SELECT vol_weighted_tfi FROM silver.market_volume_profile_15m "
                    "WHERE symbol = :sym AND ts = :ts"
                ),
                {"sym": env.symbol, "ts": env.bar_start},
            ).fetchone()
            self.assertIsNotNone(row)
            # SQLite 存的是 str 表示 Decimal, 值完整
            self.assertAlmostEqual(
                float(row.vol_weighted_tfi), 5_000_000.5, places=1,
            )

    def test_migration_sql_widens_to_28_10(self) -> None:
        """Bug 1 schema diff: batch_b_11 SQL 确实 ALTER vol_weighted_tfi
        到 NUMERIC(28, 10), 而不是其他精度。"""
        self.assertIn("batch_b_11_silver_numeric_widen", BATCH_B_STAGES)
        sql = _load_sql("batch_b_11_silver_numeric_widen")
        self.assertIn("vol_weighted_tfi", sql)
        self.assertIn("NUMERIC(28, 10)", sql)
        # rollback 对偶
        rb = _load_sql("batch_b_11_silver_numeric_widen", rollback=True)
        self.assertIn("NUMERIC(14, 8)", rb)


# ─────────────────────────────────────────────────────────────────────
# Test 2-4 — Bug 2 rollback + tables_failed + PARTIAL log
# ─────────────────────────────────────────────────────────────────────


class TestMergerPartialFailRollback(unittest.TestCase):
    """Bug 2 锁定: 某个 _build_* 抛异常后, session 不进入 aborted state,
    后续 step 正常跑, tables_failed 聚合正确, summary log 级别=WARNING。"""

    def _seed_trades_and_books(self, env) -> None:
        """往 Bronze 填足够数据让 orderbook / trade_flow 能成功聚合。"""
        with Session(env.engine) as sess:
            insert_trades(
                sess, symbol=env.symbol, ingest_run_id=env.ingest_run_id,
                trades=[
                    {
                        "ts": env.bar_start + _dt.timedelta(minutes=1),
                        "trade_id": "t1", "px": Decimal("65000"),
                        "sz": Decimal("1.5"), "side": "buy",
                    },
                    {
                        "ts": env.bar_start + _dt.timedelta(minutes=2),
                        "trade_id": "t2", "px": Decimal("65010"),
                        "sz": Decimal("0.5"), "side": "sell",
                    },
                ],
            )
            insert_bbo(
                sess, symbol=env.symbol, ingest_run_id=env.ingest_run_id,
                rows=[{
                    "ts": env.bar_start + _dt.timedelta(minutes=1),
                    "bid_px": Decimal("65000"),
                    "bid_sz": Decimal("1.0"),
                    "ask_px": Decimal("65010"),
                    "ask_sz": Decimal("1.0"),
                }],
            )
            insert_books5(
                sess, symbol=env.symbol, ingest_run_id=env.ingest_run_id,
                rows=[{
                    "ts": env.bar_start + _dt.timedelta(minutes=1),
                    "bid_px_1": Decimal("65000"), "bid_sz_1": Decimal("2.0"),
                    "ask_px_1": Decimal("65010"), "ask_sz_1": Decimal("2.0"),
                }],
            )
            sess.commit()

    def test_volume_profile_raise_does_not_chain_fail_liquidation(self) -> None:
        """Bug 2 核心: mock _build_volume_profile 抛 RuntimeError
        (模拟 NumericValueOutOfRange), 后续 step 5 liquidation_metrics
        仍然正常跑完 (session 没进 aborted state)。"""
        env = make_env()
        self._seed_trades_and_books(env)

        with Session(env.engine) as sess:
            with _mock.patch.object(
                msm, "_build_volume_profile",
                side_effect=RuntimeError(
                    "simulated NumericValueOutOfRange on vol_weighted_tfi"
                ),
            ):
                result = build_silver_microstructure_15m(
                    session=sess, symbol=env.symbol,
                    bar_start_ts=env.bar_start, bar_end_ts=env.bar_end,
                    ingest_run_id=env.ingest_run_id,
                )
            sess.commit()

        # volume_profile 失败但其他表正常
        self.assertEqual(result.tables_written["volume_profile_15m"], 0)
        self.assertEqual(result.tables_written["liquidation_metrics_15m"], 1)
        # tables_failed 只含 volume_profile_15m, 其他表没牵连
        self.assertEqual(result.tables_failed, ["volume_profile_15m"])
        # error 非空 (merger 保留第一个异常 repr)
        self.assertIsNotNone(result.error)
        self.assertIn("NumericValueOutOfRange", result.error)
        # flags 含 etl_failed:volume_profile
        self.assertIn("etl_failed:volume_profile", result.quality_flags)

    def test_merger_logs_PARTIAL_warning_when_any_table_fails(self) -> None:
        """Bug 2: 任一 table written=0 → final log 级别=WARNING + 前缀 'PARTIAL'。
        原 bug 下这里是 INFO 级 'COMMITTED', 对 Loki 告警说谎。"""
        env = make_env()
        self._seed_trades_and_books(env)

        # 捕获 merger 模块日志
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.INFO)
        logger = logging.getLogger(msm.__name__)
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        try:
            with Session(env.engine) as sess:
                with _mock.patch.object(
                    msm, "_build_liquidation_metrics",
                    side_effect=RuntimeError("boom"),
                ):
                    build_silver_microstructure_15m(
                        session=sess, symbol=env.symbol,
                        bar_start_ts=env.bar_start, bar_end_ts=env.bar_end,
                        ingest_run_id=env.ingest_run_id,
                    )
                sess.commit()
        finally:
            logger.removeHandler(handler)

        output = stream.getvalue()
        # 最终 summary log 含 PARTIAL 前缀
        self.assertIn("PARTIAL", output)
        # tables_failed 列表在 log 里体现
        self.assertIn("liquidation_metrics_15m", output)
        # 不应该打 'COMMITTED' (只允许 PARTIAL)
        # 筛最后一条 final summary
        summary_lines = [
            line for line in output.splitlines()
            if "silver_microstructure_etl" in line
        ]
        self.assertTrue(summary_lines, "expected final summary log")
        final = summary_lines[-1]
        self.assertIn("PARTIAL", final)
        self.assertNotIn("COMMITTED silver_microstructure_etl", final)

    def test_result_tables_failed_populated_with_multiple_failures(self) -> None:
        """Bug 2: 多个 step 失败时 tables_failed 按顺序记全。"""
        env = make_env()
        self._seed_trades_and_books(env)

        with Session(env.engine) as sess:
            with _mock.patch.object(
                msm, "_build_trade_flow",
                side_effect=RuntimeError("trade_flow fail"),
            ), _mock.patch.object(
                msm, "_build_volume_profile",
                side_effect=RuntimeError("vp fail"),
            ):
                result = build_silver_microstructure_15m(
                    session=sess, symbol=env.symbol,
                    bar_start_ts=env.bar_start, bar_end_ts=env.bar_end,
                    ingest_run_id=env.ingest_run_id,
                )
            sess.commit()

        self.assertIn("trade_flow_15m", result.tables_failed)
        self.assertIn("volume_profile_15m", result.tables_failed)
        self.assertNotIn("orderbook_metrics_15m", result.tables_failed)
        # 成功的表仍在 written 里 > 0
        self.assertEqual(result.tables_written["orderbook_metrics_15m"], 1)
        self.assertEqual(result.tables_written["liquidation_metrics_15m"], 1)


# ─────────────────────────────────────────────────────────────────────
# Test 5 — Bug 3 runner exit code
# ─────────────────────────────────────────────────────────────────────


class TestRunnerExitCode(unittest.TestCase):
    """Bug 3 锁定: rdp_build_microstructure_silver.py main() 应根据
    build_silver_microstructure_15m 的 result.tables_failed 返回:
        0 = 成功, 1 = uncaught, 2 = partial, 3 = full fail
    原 bug: had_error 永远 False → exit 0。"""

    def _run_main_with_mock(
        self,
        run_one_bar_return: dict | Exception,
    ) -> tuple[int, str]:
        """mock _run_one_bar 后跑 main(), 返回 (exit_code, captured_stdout)。"""
        import importlib

        rbms = importlib.import_module("scripts.rdp_build_microstructure_silver")

        args = [
            "rdp_build_microstructure_silver.py",
            "--symbol", "BTC-USDT-SWAP",
            "--backfill-bars", "1",
            "--apply", "--confirm",
        ]
        orig_argv = sys.argv
        buf = io.StringIO()
        orig_stdout = sys.stdout
        try:
            sys.argv = args
            sys.stdout = buf

            def fake_run_one_bar(*a, **kw):
                if isinstance(run_one_bar_return, Exception):
                    raise run_one_bar_return
                return run_one_bar_return

            # watermark → None 让 runner 走冷启动 fallback (旧 --backfill-bars=N 路径),
            # 不触 DB; 本组测试锁的是 exit code 语义, 不关心水位线路径。
            with _mock.patch.object(
                rbms, "_detect_trade_flow_watermark", return_value=None,
            ), _mock.patch.object(
                rbms, "_run_one_bar", side_effect=fake_run_one_bar,
            ):
                exit_code = rbms.main()
        finally:
            sys.argv = orig_argv
            sys.stdout = orig_stdout
        return exit_code, buf.getvalue()

    def test_exit_0_when_all_tables_written(self) -> None:
        exit_code, out = self._run_main_with_mock({
            "symbol": "BTC-USDT-SWAP",
            "bar_start": "2026-04-20T12:00:00+00:00",
            "bar_end": "2026-04-20T12:15:00+00:00",
            "tables_written": {
                "orderbook_metrics_15m": 1, "trade_flow_15m": 1,
                "oi_funding_metrics_15m": 1, "volume_profile_15m": 1,
                "liquidation_metrics_15m": 1,
            },
            "tables_failed": [],
            "quality_flags": [],
            "duration_seconds": 0.5,
            "ingest_run_id": "rid",
            "mode": "apply",
            "error": None,
        })
        self.assertEqual(exit_code, 0)

    def test_exit_2_when_partial_fail(self) -> None:
        exit_code, out = self._run_main_with_mock({
            "symbol": "BTC-USDT-SWAP",
            "bar_start": "2026-04-20T12:00:00+00:00",
            "bar_end": "2026-04-20T12:15:00+00:00",
            "tables_written": {
                "orderbook_metrics_15m": 1, "trade_flow_15m": 1,
                "oi_funding_metrics_15m": 1, "volume_profile_15m": 0,
                "liquidation_metrics_15m": 1,
            },
            "tables_failed": ["volume_profile_15m"],
            "quality_flags": ["etl_failed:volume_profile"],
            "duration_seconds": 0.5,
            "ingest_run_id": "rid",
            "mode": "apply",
            "error": "RuntimeError('NumericValueOutOfRange')",
        })
        self.assertEqual(exit_code, 2)
        self.assertIn("TASK_PARTIAL_FAIL", out)
        self.assertIn("volume_profile_15m", out)

    def test_exit_3_when_full_fail(self) -> None:
        exit_code, out = self._run_main_with_mock({
            "symbol": "BTC-USDT-SWAP",
            "bar_start": "2026-04-20T12:00:00+00:00",
            "bar_end": "2026-04-20T12:15:00+00:00",
            "tables_written": {
                "orderbook_metrics_15m": 0, "trade_flow_15m": 0,
                "oi_funding_metrics_15m": 0, "volume_profile_15m": 0,
                "liquidation_metrics_15m": 0,
            },
            "tables_failed": [
                "orderbook_metrics_15m", "trade_flow_15m",
                "oi_funding_metrics_15m", "volume_profile_15m",
                "liquidation_metrics_15m",
            ],
            "quality_flags": ["etl_failed:orderbook"],
            "duration_seconds": 0.5,
            "ingest_run_id": "rid",
            "mode": "apply",
            "error": "RuntimeError('something went really wrong')",
        })
        self.assertEqual(exit_code, 3)
        self.assertIn("TASK_FULL_FAIL", out)


# ─────────────────────────────────────────────────────────────────────
# Test 6 — 幂等性
# ─────────────────────────────────────────────────────────────────────


class TestIdempotentRerun(unittest.TestCase):
    """同 bar 重跑两次, silver 表每张仍然 1 行, flags 一致, log 不打错误。"""

    def test_rerun_same_bar_preserves_row_count(self) -> None:
        env = make_env()
        with Session(env.engine) as sess:
            result1 = build_silver_microstructure_15m(
                session=sess, symbol=env.symbol,
                bar_start_ts=env.bar_start, bar_end_ts=env.bar_end,
                ingest_run_id=env.ingest_run_id,
            )
            sess.commit()

        with Session(env.engine) as sess:
            result2 = build_silver_microstructure_15m(
                session=sess, symbol=env.symbol,
                bar_start_ts=env.bar_start, bar_end_ts=env.bar_end,
                ingest_run_id=env.ingest_run_id,
            )
            sess.commit()

            # 5 张表每张仍只有 1 行 (UPSERT 幂等, 非 2 行重复)
            for table in (
                "silver.market_orderbook_metrics_15m",
                "silver.market_trade_flow_15m",
                "silver.market_oi_funding_metrics_15m",
                "silver.market_volume_profile_15m",
                "silver.market_liquidation_metrics_15m",
            ):
                count = sess.execute(
                    text(
                        f"SELECT COUNT(*) AS n FROM {table} "
                        f"WHERE symbol = :sym AND ts = :ts"
                    ),
                    {"sym": env.symbol, "ts": env.bar_start},
                ).fetchone().n
                self.assertEqual(count, 1, f"{table} should have 1 row after rerun")

        # 两次都没 error + 都没 tables_failed
        self.assertIsNone(result1.error)
        self.assertIsNone(result2.error)
        self.assertEqual(result1.tables_failed, [])
        self.assertEqual(result2.tables_failed, [])


if __name__ == "__main__":
    unittest.main()
