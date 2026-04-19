"""P1-D Phase 1A Stage 3 单元测试 — Silver ETL 主入口 + 幂等性。

对齐设计 §7 / §8 的 pipeline-level cases:
  - bar alignment 校验 (15m boundary + UTC-aware)
  - 所有 Bronze/staging 全空时 5 张 silver row 都写入 (gap-filled NULL 路径)
  - 同 (symbol, bar_start_ts) 跑两次 → silver 表每张仍只有 1 行 (UPSERT 幂等)
  - quality_flags 累积正确: 缺 Bronze 数据 → 对应 *_no_data flag
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from aats.data_platform.merge.microstructure_silver_merger import (
    DEFAULT_DATASET_VERSION,
    build_silver_microstructure_15m,
    latest_complete_bar,
)
from tests.unit.data_platform._silver_test_helpers import (
    insert_bbo,
    make_env,
)


class TestBarAlignmentValidation(unittest.TestCase):
    """§7 总入口进来必须做 15m 对齐校验, 防止误用随意 ts。"""

    def test_naive_timestamp_rejected(self) -> None:
        env = make_env()
        naive = datetime(2026, 4, 20, 12, 0, 0)  # no tzinfo
        with Session(env.engine) as sess:
            with self.assertRaises(ValueError) as cm:
                build_silver_microstructure_15m(
                    session=sess, symbol=env.symbol,
                    bar_start_ts=naive,
                    bar_end_ts=naive + timedelta(minutes=15),
                    ingest_run_id=env.ingest_run_id,
                )
            self.assertIn("timezone", str(cm.exception).lower())

    def test_unaligned_minute_rejected(self) -> None:
        env = make_env()
        off = datetime(2026, 4, 20, 12, 7, 0, tzinfo=timezone.utc)
        with Session(env.engine) as sess:
            with self.assertRaises(ValueError) as cm:
                build_silver_microstructure_15m(
                    session=sess, symbol=env.symbol,
                    bar_start_ts=off,
                    bar_end_ts=off + timedelta(minutes=15),
                    ingest_run_id=env.ingest_run_id,
                )
            self.assertIn("multiple of 15", str(cm.exception))

    def test_end_minus_start_not_15min_rejected(self) -> None:
        env = make_env()
        with Session(env.engine) as sess:
            with self.assertRaises(ValueError) as cm:
                build_silver_microstructure_15m(
                    session=sess, symbol=env.symbol,
                    bar_start_ts=env.bar_start,
                    bar_end_ts=env.bar_start + timedelta(minutes=30),
                    ingest_run_id=env.ingest_run_id,
                )
            self.assertIn("15min", str(cm.exception))


class TestEmptyBarGapFill(unittest.TestCase):
    """§4.3 + §7.4: Bronze 全空时 silver 5 张表仍写入 1 行, 所有聚合 NULL,
    quality_flags 累积 *_no_data 标记 (observability 可见)。
    """

    def test_all_sources_empty_writes_five_rows(self) -> None:
        env = make_env()
        with Session(env.engine) as sess:
            result = build_silver_microstructure_15m(
                session=sess, symbol=env.symbol,
                bar_start_ts=env.bar_start, bar_end_ts=env.bar_end,
                ingest_run_id=env.ingest_run_id,
            )
            sess.commit()

            # 5 张 silver 表每张有 1 行
            self.assertEqual(result.tables_written, {
                "orderbook_metrics_15m": 1,
                "trade_flow_15m": 1,
                "oi_funding_metrics_15m": 1,
                "volume_profile_15m": 1,
                "liquidation_metrics_15m": 1,
            })

            # 所有 _no_data flag 都在
            for flag in (
                "orderbook_bbo_no_data",
                "orderbook_books5_no_data",
                "trades_no_data",
                "oi_no_data",
                "funding_no_data",
                "mark_no_data",
                "liquidation_no_data",
            ):
                self.assertIn(
                    flag, result.quality_flags,
                    f"missing expected flag {flag!r} in {result.quality_flags}",
                )

            # DB 里查每张表 1 行
            for tbl in (
                "market_orderbook_metrics_15m",
                "market_trade_flow_15m",
                "market_oi_funding_metrics_15m",
                "market_volume_profile_15m",
                "market_liquidation_metrics_15m",
            ):
                count = sess.execute(
                    text(f"SELECT COUNT(*) AS n FROM silver.{tbl}")
                ).scalar()
                self.assertEqual(count, 1, f"expected 1 row in silver.{tbl}")

    def test_no_etl_failed_flag_on_empty_bar(self) -> None:
        """空 bar 应该是 "gap fill" (打 *_no_data), 不应该是 etl_failed。"""
        env = make_env()
        with Session(env.engine) as sess:
            result = build_silver_microstructure_15m(
                session=sess, symbol=env.symbol,
                bar_start_ts=env.bar_start, bar_end_ts=env.bar_end,
                ingest_run_id=env.ingest_run_id,
            )
            sess.commit()

            for flag in result.quality_flags:
                self.assertFalse(
                    flag.startswith("etl_failed"),
                    f"unexpected etl_failed flag on empty bar: {flag!r}",
                )
            self.assertIsNone(result.error)


class TestIdempotency(unittest.TestCase):
    """§7.4 幂等性: 同 (symbol, bar_start_ts) 二次调用 silver 表每张仍
    只有 1 行, UPSERT ON CONFLICT (symbol, ts) DO UPDATE 生效。
    """

    def test_same_bar_run_twice_no_duplicate_rows(self) -> None:
        env = make_env()
        with Session(env.engine) as sess:
            bbo_rows = [
                {
                    "ts": env.bar_start + timedelta(seconds=i * 30),
                    "bid_px": Decimal("95000"), "bid_sz": Decimal("1"),
                    "ask_px": Decimal("95010"), "ask_sz": Decimal("2"),
                }
                for i in range(5)
            ]
            insert_bbo(
                sess, symbol=env.symbol, ingest_run_id=env.ingest_run_id,
                rows=bbo_rows,
            )

            # 1st run
            r1 = build_silver_microstructure_15m(
                session=sess, symbol=env.symbol,
                bar_start_ts=env.bar_start, bar_end_ts=env.bar_end,
                ingest_run_id=env.ingest_run_id,
            )
            sess.commit()

            # 2nd run (same bar, same ingest_run_id → 幂等路径)
            r2 = build_silver_microstructure_15m(
                session=sess, symbol=env.symbol,
                bar_start_ts=env.bar_start, bar_end_ts=env.bar_end,
                ingest_run_id=env.ingest_run_id,
            )
            sess.commit()

            # 每张表仍只 1 行
            for tbl in (
                "market_orderbook_metrics_15m",
                "market_trade_flow_15m",
                "market_oi_funding_metrics_15m",
                "market_volume_profile_15m",
                "market_liquidation_metrics_15m",
            ):
                count = sess.execute(
                    text(f"SELECT COUNT(*) AS n FROM silver.{tbl}")
                ).scalar()
                self.assertEqual(
                    count, 1,
                    f"{tbl} should have exactly 1 row after 2nd run, got {count}",
                )

            # 两次 tables_written 结构应一致
            self.assertEqual(r1.tables_written, r2.tables_written)

    def test_different_ingest_run_id_still_idempotent(self) -> None:
        """换一个 ingest_run_id 跑同一 bar, silver 表仍只 1 行, 但
        ingest_run_id 列应该是最新那次的 (ON CONFLICT DO UPDATE 语义)。
        """
        env = make_env()
        with Session(env.engine) as sess:
            build_silver_microstructure_15m(
                session=sess, symbol=env.symbol,
                bar_start_ts=env.bar_start, bar_end_ts=env.bar_end,
                ingest_run_id=env.ingest_run_id,
            )
            sess.commit()

            new_run_id = str(uuid4())
            build_silver_microstructure_15m(
                session=sess, symbol=env.symbol,
                bar_start_ts=env.bar_start, bar_end_ts=env.bar_end,
                ingest_run_id=new_run_id,
            )
            sess.commit()

            row = sess.execute(
                text(
                    "SELECT ingest_run_id FROM silver.market_orderbook_metrics_15m "
                    "WHERE symbol = :sym AND ts = :ts"
                ),
                {"sym": env.symbol, "ts": env.bar_start},
            ).fetchone()
            self.assertEqual(str(row.ingest_run_id), new_run_id)


class TestLatestCompleteBar(unittest.TestCase):
    """latest_complete_bar(now, lookback_bars) 的语义校验。"""

    def test_lookback_1_gives_previous_closed_bar(self) -> None:
        # now = 12:17:00 → current bar starts 12:15, unclosed
        # lookback=1 → bar_start=12:00, bar_end=12:15
        now = datetime(2026, 4, 20, 12, 17, 0, tzinfo=timezone.utc)
        bs, be = latest_complete_bar(now, lookback_bars=1)
        self.assertEqual(bs, datetime(2026, 4, 20, 12, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(be, datetime(2026, 4, 20, 12, 15, 0, tzinfo=timezone.utc))

    def test_lookback_2_gives_bar_before_that(self) -> None:
        now = datetime(2026, 4, 20, 12, 17, 0, tzinfo=timezone.utc)
        bs, be = latest_complete_bar(now, lookback_bars=2)
        self.assertEqual(bs, datetime(2026, 4, 20, 11, 45, 0, tzinfo=timezone.utc))
        self.assertEqual(be, datetime(2026, 4, 20, 12, 0, 0, tzinfo=timezone.utc))

    def test_zero_lookback_rejected(self) -> None:
        with self.assertRaises(ValueError):
            latest_complete_bar(lookback_bars=0)

    def test_naive_now_rejected(self) -> None:
        with self.assertRaises(ValueError):
            latest_complete_bar(datetime(2026, 4, 20, 12, 17, 0))


class TestBatchB06Registration(unittest.TestCase):
    """防止 refactor 意外丢弃 stage 06 注册, 导致 deploy 时 Silver migration 漏跑。"""

    def test_batch_b_06_silver_microstructure_registered_last(self) -> None:
        """stage 6 位置验证。

        Phase 1A deploy retro (2026-04-20): batch_b_07_ingest_runs_domain_extension
        追加后, stage 6 自然不再是 tuple 末尾。原始意图 — "新 stage 以 append
        形式入 tuple, 不随意插入中间" — 仍保留: 验证 stage 6 在 stage 7 之前。
        """
        from aats.data_platform.migrations._batch_b import BATCH_B_STAGES

        self.assertIn("batch_b_06_silver_microstructure", BATCH_B_STAGES)
        idx_06 = BATCH_B_STAGES.index("batch_b_06_silver_microstructure")
        # stage 6 必须在 stage 7 之前 (严格 append 顺序)
        if "batch_b_07_ingest_runs_domain_extension" in BATCH_B_STAGES:
            idx_07 = BATCH_B_STAGES.index("batch_b_07_ingest_runs_domain_extension")
            self.assertLess(
                idx_06, idx_07,
                "stage 6 必须在 stage 7 之前, 保持严格 append 顺序",
            )
        else:
            # stage 7 尚未加入, stage 6 必须是末尾
            self.assertEqual(
                BATCH_B_STAGES[-1],
                "batch_b_06_silver_microstructure",
                "stage 6 必须是 tuple 的最后一项, 保持严格 append 顺序",
            )

    def test_batch_b_06_migration_files_exist(self) -> None:
        from pathlib import Path
        root = Path(__file__).resolve().parents[3]
        migrate_dir = root / "aats" / "data_platform" / "migrations"
        self.assertTrue(
            (migrate_dir / "batch_b_06_silver_microstructure.sql").exists(),
            "batch_b_06 migration SQL missing",
        )
        self.assertTrue(
            (migrate_dir / "batch_b_06_silver_microstructure_rollback.sql").exists(),
            "batch_b_06 rollback SQL missing",
        )


class TestBatchB06Rollback(unittest.TestCase):
    """batch_b_06_silver_microstructure_rollback.sql 在 SQLite 等价语义下
    可以 DROP 5 张 Silver 表 (不 DROP schema), 与 Stage 1 rollback 测试
    范式一致。
    """

    def test_rollback_drops_all_five_silver_tables(self) -> None:
        from pathlib import Path
        from sqlalchemy import create_engine, text, event
        import datetime as _dt

        root = Path(__file__).resolve().parents[3]
        rollback_path = (
            root / "aats" / "data_platform" / "migrations"
            / "batch_b_06_silver_microstructure_rollback.sql"
        )
        self.assertTrue(rollback_path.exists(), f"missing {rollback_path}")
        sql_text = rollback_path.read_text(encoding="utf-8")

        # 复用 silver test helper 建 engine (已含 5 张 silver 表)
        from tests.unit.data_platform._silver_test_helpers import (
            make_silver_sqlite_engine,
        )
        engine = make_silver_sqlite_engine()

        # 初始: 5 张表存在
        with engine.connect() as conn:
            for tbl in (
                "market_orderbook_metrics_15m",
                "market_trade_flow_15m",
                "market_oi_funding_metrics_15m",
                "market_volume_profile_15m",
                "market_liquidation_metrics_15m",
            ):
                row = conn.execute(
                    text(
                        f"SELECT name FROM silver.sqlite_master "
                        f"WHERE type='table' AND name='{tbl}'"
                    )
                ).fetchone()
                self.assertIsNotNone(row, f"silver.{tbl} should exist before rollback")

        # 过滤注释 + 空行, 按 ';' 切成 statement list
        # 和 Stage 1 test_microstructure_bronze_schema 里的 rollback 处理方式一致
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

        with engine.begin() as conn:
            for stmt in stmts:
                conn.execute(text(stmt))

        # 回滚后: 5 张表全部不存在
        with engine.connect() as conn:
            for tbl in (
                "market_orderbook_metrics_15m",
                "market_trade_flow_15m",
                "market_oi_funding_metrics_15m",
                "market_volume_profile_15m",
                "market_liquidation_metrics_15m",
            ):
                row = conn.execute(
                    text(
                        f"SELECT name FROM silver.sqlite_master "
                        f"WHERE type='table' AND name='{tbl}'"
                    )
                ).fetchone()
                self.assertIsNone(row, f"silver.{tbl} should be dropped")


class TestWorkflowRegistration(unittest.TestCase):
    """附录 E #7: microstructure_silver_15m workflow 必须在 VALID_WORKFLOWS
    + WORKFLOW_TIMEOUTS 都注册, 否则 rdp_task_daemon 会拒绝领取任务。
    """

    def test_workflow_in_valid_workflows(self) -> None:
        from aats.data_platform.governance.rdp_task_db import VALID_WORKFLOWS
        self.assertIn("microstructure_silver_15m", VALID_WORKFLOWS)

    def test_workflow_timeout_configured(self) -> None:
        # rdp_task_daemon 是独立 script, 要 import scripts 目录
        import importlib.util
        from pathlib import Path
        root = Path(__file__).resolve().parents[3]
        spec = importlib.util.spec_from_file_location(
            "rdp_task_daemon",
            root / "scripts" / "rdp_task_daemon.py",
        )
        self.assertIsNotNone(spec)
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        self.assertIn("microstructure_silver_15m", mod.WORKFLOW_TIMEOUTS)
        self.assertGreaterEqual(
            mod.WORKFLOW_TIMEOUTS["microstructure_silver_15m"], 60,
            "timeout too low for Silver ETL (expected >= 60s, §11 p95 < 10s with headroom)",
        )

    def test_workflow_config_file_exists(self) -> None:
        from pathlib import Path
        root = Path(__file__).resolve().parents[3]
        cfg = root / "configs" / "rdp_workflows" / "microstructure_silver_15m.json"
        self.assertTrue(cfg.exists(), f"missing workflow config at {cfg}")
        import json
        data = json.loads(cfg.read_text(encoding="utf-8"))
        self.assertEqual(data["workflow"], "microstructure_silver_15m")
        self.assertTrue(data.get("schedule", {}).get("enabled"))
        self.assertGreaterEqual(
            len(data.get("tasks", [])), 1, "workflow must have at least 1 task",
        )


class TestSilverMetricsPlumbing(unittest.TestCase):
    """Stage 4 新增: metrics_registry 可选注入, 产生 counter 增量。"""

    def test_metrics_registry_none_is_noop(self) -> None:
        """Stage 3 既有行为: metrics_registry 未传 → 零副作用,结果数据不变。"""
        env = make_env()
        with Session(env.engine) as sess:
            result = build_silver_microstructure_15m(
                session=sess, symbol=env.symbol,
                bar_start_ts=env.bar_start, bar_end_ts=env.bar_end,
                ingest_run_id=env.ingest_run_id,
            )
            sess.commit()
        self.assertEqual(sum(result.tables_written.values()), 5)
        self.assertEqual(result.error, None)

    def test_metrics_registry_accumulates_counters(self) -> None:
        """当 caller 注入 MetricsRegistry, ETL 每张表都产生 _success 与 rows_written 计数。"""
        from aats.bootstrap.metrics import MetricsRegistry
        env = make_env()
        registry = MetricsRegistry()
        with Session(env.engine) as sess:
            build_silver_microstructure_15m(
                session=sess, symbol=env.symbol,
                bar_start_ts=env.bar_start, bar_end_ts=env.bar_end,
                ingest_run_id=env.ingest_run_id,
                metrics_registry=registry,
            )
            sess.commit()
        snapshot = registry.snapshot()
        # 5 张表的 success counter 全部打点 (空 bar 也是 "成功写入 NULL 行")
        for key in (
            "microstructure_silver_etl_runs_total_orderbook_success",
            "microstructure_silver_etl_runs_total_trade_flow_success",
            "microstructure_silver_etl_runs_total_oi_funding_success",
            "microstructure_silver_etl_runs_total_volume_profile_success",
            "microstructure_silver_etl_runs_total_liquidation_success",
        ):
            self.assertGreaterEqual(
                snapshot.get(key, 0), 1,
                f"expected {key} counter >= 1, got {snapshot.get(key, 0)}",
            )
        # duration bucket 必有一个被打: <1s (空 bar 通常毫秒级)
        bucket_keys = {
            k for k in snapshot
            if k.startswith("microstructure_silver_etl_duration_bucket_")
        }
        self.assertEqual(len(bucket_keys), 1,
                         f"exactly one duration bucket should fire, got {bucket_keys}")


if __name__ == "__main__":
    unittest.main()
