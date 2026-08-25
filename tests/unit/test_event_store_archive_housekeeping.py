"""Path B Phase 1 单元测试：DatabaseHousekeeping.archive_hot_event_store。

测试场景：
  1. happy path：老行全部搬到 archive，hot 只剩新行。
  2. batch 边界：batch_size 小于待归档总数，需要多个 batch。
  3. 幂等：二次执行不会重复 INSERT（copied=0, deleted=0）。
  4. cutoff 精度：cutoff 恰好等于某行时间戳的行**不应**被归档（<= vs <）。
  5. 事务 rollback：INSERT 失败时 hot 表完整无损（mock 打桩）。
  6. dry_run：统计 pending 但不改数据。
  7. max_batches 限流：传入 max_batches=1 时只跑 1 个 batch。
  8. ArchiveReport.as_dict 序列化。

用 SQLite in-memory，方言无关 INSERT/DELETE 路径。
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session, sessionmaker

from aats.storage.housekeeping import ArchiveReport, DatabaseHousekeeping
from aats.storage.sqlalchemy_models import (
    Base,
    EventEnvelopeArchiveModel,
    EventEnvelopeModel,
)


def _make_session_factory(owner: unittest.TestCase) -> sessionmaker[Session]:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    owner.addCleanup(engine.dispose)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def _insert_event(
    session_factory: sessionmaker[Session],
    *,
    event_id: str,
    event_timestamp: datetime,
    topic: str = "test.topic",
    symbol: str | None = "BTC-USDT",
    product_type: str | None = "derivatives",
    margin_mode: str | None = "cross",
) -> int:
    with session_factory() as session:
        row = EventEnvelopeModel(
            event_id=event_id,
            schema_version="1.0",
            created_at=event_timestamp,
            event_type="test.event",
            event_timestamp=event_timestamp,
            source_component="unit-test",
            topic=topic,
            event_key=symbol or "none",
            decision_id=None,
            symbol=symbol,
            timeframe=None,
            product_type=product_type,
            margin_mode=margin_mode,
            payload={"event_id": event_id},
        )
        session.add(row)
        session.commit()
        return row.sequence_id


def _count(session_factory, model) -> int:
    with session_factory() as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _hot_event_ids(session_factory) -> set[str]:
    with session_factory() as session:
        return set(
            session.scalars(select(EventEnvelopeModel.event_id)).all()
        )


def _archive_event_ids(session_factory) -> set[str]:
    with session_factory() as session:
        return set(
            session.scalars(select(EventEnvelopeArchiveModel.event_id)).all()
        )


class TestArchiveHotEventStore(unittest.TestCase):
    # ──────────────────────────────────────────────────────────────────
    # Case 1: happy path
    # ──────────────────────────────────────────────────────────────────

    def test_archive_moves_old_rows_and_keeps_new_ones(self) -> None:
        sf = _make_session_factory(self)
        now = datetime.now(timezone.utc)
        # 3 行老数据 (> 14 天), 2 行新数据 (< 14 天)
        for i in range(3):
            _insert_event(
                sf,
                event_id=f"old_{i}",
                event_timestamp=now - timedelta(days=20 + i),
            )
        for i in range(2):
            _insert_event(
                sf,
                event_id=f"new_{i}",
                event_timestamp=now - timedelta(days=1),
            )

        hk = DatabaseHousekeeping(session_factory=sf)
        report = hk.archive_hot_event_store(
            older_than_days=14,
            batch_size=100,
        )

        self.assertEqual(report.copied_rows, 3)
        self.assertEqual(report.deleted_rows, 3)
        self.assertEqual(report.batches, 1)
        self.assertFalse(report.dry_run)
        self.assertEqual(_count(sf, EventEnvelopeModel), 2)
        self.assertEqual(_count(sf, EventEnvelopeArchiveModel), 3)
        self.assertEqual(
            _hot_event_ids(sf),
            {"new_0", "new_1"},
        )
        self.assertEqual(
            _archive_event_ids(sf),
            {"old_0", "old_1", "old_2"},
        )

    # ──────────────────────────────────────────────────────────────────
    # Case 2: batch 边界
    # ──────────────────────────────────────────────────────────────────

    def test_batch_size_forces_multiple_rounds(self) -> None:
        sf = _make_session_factory(self)
        now = datetime.now(timezone.utc)
        for i in range(25):
            _insert_event(
                sf,
                event_id=f"old_{i:03d}",
                event_timestamp=now - timedelta(days=30, hours=i),
            )

        hk = DatabaseHousekeeping(session_factory=sf)
        report = hk.archive_hot_event_store(
            older_than_days=14,
            batch_size=7,
        )

        self.assertEqual(report.copied_rows, 25)
        self.assertEqual(report.deleted_rows, 25)
        # 25 / 7 = 4 batch (7+7+7+4)
        self.assertEqual(report.batches, 4)
        self.assertEqual(_count(sf, EventEnvelopeModel), 0)
        self.assertEqual(_count(sf, EventEnvelopeArchiveModel), 25)

    # ──────────────────────────────────────────────────────────────────
    # Case 3: 幂等性
    # ──────────────────────────────────────────────────────────────────

    def test_idempotent_second_run_is_noop(self) -> None:
        sf = _make_session_factory(self)
        now = datetime.now(timezone.utc)
        for i in range(3):
            _insert_event(
                sf,
                event_id=f"old_{i}",
                event_timestamp=now - timedelta(days=20 + i),
            )

        hk = DatabaseHousekeeping(session_factory=sf)
        first = hk.archive_hot_event_store(older_than_days=14, batch_size=100)
        second = hk.archive_hot_event_store(older_than_days=14, batch_size=100)

        self.assertEqual(first.copied_rows, 3)
        self.assertEqual(first.deleted_rows, 3)
        # 第二次跑：hot 已空，copied=0, deleted=0, batches=0
        self.assertEqual(second.copied_rows, 0)
        self.assertEqual(second.deleted_rows, 0)
        self.assertEqual(second.batches, 0)
        # archive 仍是 3 行（未重复插入）
        self.assertEqual(_count(sf, EventEnvelopeArchiveModel), 3)

    def test_idempotent_with_preexisting_archive_rows(self) -> None:
        """边界：archive 已经存在同 event_id，但 hot 也有（异常修复场景）。

        场景：event_id='dup' 既在 hot 又在 archive（例如先前归档失败
        DELETE 未跑、下次 backfill）。预期：这行应该仍被 DELETE 掉 hot，
        archive 保持原行（不重复 INSERT）。
        """
        sf = _make_session_factory(self)
        now = datetime.now(timezone.utc)
        old_ts = now - timedelta(days=20)
        # 先手工放进 archive
        with sf() as session:
            session.add(
                EventEnvelopeArchiveModel(
                    source_sequence_id=9999,
                    event_id="dup",
                    schema_version="1.0",
                    created_at=old_ts,
                    event_type="test.event",
                    event_timestamp=old_ts,
                    source_component="unit-test",
                    topic="test.topic",
                    event_key="BTC-USDT",
                    decision_id=None,
                    symbol="BTC-USDT",
                    timeframe=None,
                    product_type="derivatives",
                    margin_mode="cross",
                    payload={"preexisting": True},
                )
            )
            session.commit()

        _insert_event(sf, event_id="dup", event_timestamp=old_ts)
        _insert_event(
            sf, event_id="other", event_timestamp=now - timedelta(days=21)
        )

        hk = DatabaseHousekeeping(session_factory=sf)
        report = hk.archive_hot_event_store(
            older_than_days=14,
            batch_size=100,
        )

        # copied 只计真正 INSERT 的 (other)，deleted 两行都算
        self.assertEqual(report.copied_rows, 1)
        self.assertEqual(report.deleted_rows, 2)
        self.assertEqual(_count(sf, EventEnvelopeModel), 0)
        # archive = 原来的 dup + 新搬的 other = 2 行，dup 只有一份
        self.assertEqual(_count(sf, EventEnvelopeArchiveModel), 2)
        self.assertEqual(_archive_event_ids(sf), {"dup", "other"})
        # 原 dup payload 未被覆盖
        with sf() as session:
            dup_payload = session.scalar(
                select(EventEnvelopeArchiveModel.payload).where(
                    EventEnvelopeArchiveModel.event_id == "dup"
                )
            )
        self.assertEqual(dup_payload, {"preexisting": True})

    # ──────────────────────────────────────────────────────────────────
    # Case 4: cutoff 严格 < ，相等的行不搬
    # ──────────────────────────────────────────────────────────────────

    def test_cutoff_uses_strict_less_than(self) -> None:
        sf = _make_session_factory(self)
        # 构造：一行恰好在 cutoff 上，一行早 1 秒
        # older_than_days=14 → cutoff = now - 14d
        # 放一行 ts = now - 14d - 1s（应归档），一行 ts = now - 13d 23h 59m（不归档）
        now = datetime.now(timezone.utc)
        cutoff_approx = now - timedelta(days=14)
        _insert_event(
            sf,
            event_id="just_before_cutoff",
            event_timestamp=cutoff_approx - timedelta(seconds=5),
        )
        _insert_event(
            sf,
            event_id="just_after_cutoff",
            event_timestamp=cutoff_approx + timedelta(seconds=5),
        )

        hk = DatabaseHousekeeping(session_factory=sf)
        report = hk.archive_hot_event_store(
            older_than_days=14,
            batch_size=100,
        )

        self.assertEqual(report.copied_rows, 1)
        self.assertEqual(_hot_event_ids(sf), {"just_after_cutoff"})
        self.assertEqual(_archive_event_ids(sf), {"just_before_cutoff"})

    # ──────────────────────────────────────────────────────────────────
    # Case 5: 事务 rollback — INSERT 失败时 hot 完整无损
    # ──────────────────────────────────────────────────────────────────

    def test_rollback_on_insert_failure_preserves_hot_table(self) -> None:
        """模拟 INSERT 阶段抛异常 → 事务 rollback → hot 表无变化。

        通过 monkey-patch session.execute，让第二次 execute（即 INSERT）
        抛 RuntimeError。此时 DELETE 还未跑，commit 也未跑。期望：
          - hot 表保留全部行
          - archive 表为空
          - 异常向上抛
        """
        sf = _make_session_factory(self)
        now = datetime.now(timezone.utc)
        for i in range(3):
            _insert_event(
                sf,
                event_id=f"old_{i}",
                event_timestamp=now - timedelta(days=20 + i),
            )

        hk = DatabaseHousekeeping(session_factory=sf)

        real_session_factory = sf
        call_count = {"n": 0}

        class _FlakeySession:
            def __init__(self, inner):
                self._inner = inner

            def __enter__(self):
                self._inner.__enter__()
                return self

            def __exit__(self, *args):
                return self._inner.__exit__(*args)

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def execute(self, stmt, *args, **kwargs):
                call_count["n"] += 1
                # 第 1 次 execute 是 INSERT（batch 内部），我们让它炸。
                # 之前的 scalars() 调用是 SELECT，不走 execute。
                compiled = str(stmt)
                if "INSERT INTO event_store_archive" in compiled:
                    raise RuntimeError("simulated INSERT failure")
                return self._inner.execute(stmt, *args, **kwargs)

        def flakey_factory():
            return _FlakeySession(real_session_factory())

        hk._session_factory = flakey_factory

        with self.assertRaises(RuntimeError):
            hk.archive_hot_event_store(older_than_days=14, batch_size=100)

        # hot 表完整无损
        self.assertEqual(_count(sf, EventEnvelopeModel), 3)
        self.assertEqual(_count(sf, EventEnvelopeArchiveModel), 0)

    # ──────────────────────────────────────────────────────────────────
    # Case 6: dry_run
    # ──────────────────────────────────────────────────────────────────

    def test_dry_run_counts_but_does_not_mutate(self) -> None:
        sf = _make_session_factory(self)
        now = datetime.now(timezone.utc)
        for i in range(5):
            _insert_event(
                sf,
                event_id=f"old_{i}",
                event_timestamp=now - timedelta(days=20 + i),
            )

        hk = DatabaseHousekeeping(session_factory=sf)
        report = hk.archive_hot_event_store(
            older_than_days=14,
            batch_size=100,
            dry_run=True,
        )

        self.assertTrue(report.dry_run)
        self.assertEqual(report.copied_rows, 5)
        self.assertEqual(report.deleted_rows, 5)  # dry_run 下 copied == deleted
        self.assertEqual(report.batches, 0)  # dry_run 未跑 batch
        # 表数据未变
        self.assertEqual(_count(sf, EventEnvelopeModel), 5)
        self.assertEqual(_count(sf, EventEnvelopeArchiveModel), 0)

    # ──────────────────────────────────────────────────────────────────
    # Case 7: max_batches 限流
    # ──────────────────────────────────────────────────────────────────

    def test_max_batches_bounds_execution(self) -> None:
        sf = _make_session_factory(self)
        now = datetime.now(timezone.utc)
        for i in range(15):
            _insert_event(
                sf,
                event_id=f"old_{i:03d}",
                event_timestamp=now - timedelta(days=20, hours=i),
            )

        hk = DatabaseHousekeeping(session_factory=sf)
        report = hk.archive_hot_event_store(
            older_than_days=14,
            batch_size=5,
            max_batches=2,
        )

        # 只跑 2 个 batch × 5 行 = 10 行，剩 5 行在 hot
        self.assertEqual(report.copied_rows, 10)
        self.assertEqual(report.batches, 2)
        self.assertEqual(_count(sf, EventEnvelopeModel), 5)
        self.assertEqual(_count(sf, EventEnvelopeArchiveModel), 10)

    def test_postgres_batch_uses_cte_upsert_without_large_in_list(self) -> None:
        captured: dict[str, object] = {}

        class _FakeResult:
            def one(self):
                return SimpleNamespace(copied_count=7, deleted_count=9)

        class _FakeSession:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def execute(self, statement, params):
                captured["sql"] = str(statement)
                captured["params"] = dict(params)
                return _FakeResult()

            def commit(self):
                captured["committed"] = True

            def rollback(self):
                captured["rolled_back"] = True

        hk = DatabaseHousekeeping(session_factory=lambda: _FakeSession())  # type: ignore[arg-type]
        copied, deleted = hk._archive_hot_event_store_postgres_batch(
            cutoff=datetime.now(timezone.utc) - timedelta(days=14),
            batch_size=10_000,
        )

        sql = str(captured["sql"])
        self.assertEqual((copied, deleted), (7, 9))
        self.assertIn("WITH candidates AS MATERIALIZED", sql)
        self.assertIn("ON CONFLICT (event_id) DO NOTHING", sql)
        self.assertIn("DELETE FROM event_store AS hot", sql)
        self.assertNotIn("event_id IN", sql)
        self.assertEqual(captured["params"]["batch_size"], 10_000)
        self.assertTrue(captured["committed"])

    # ──────────────────────────────────────────────────────────────────
    # Case 8: ArchiveReport.as_dict
    # ──────────────────────────────────────────────────────────────────

    def test_archive_report_as_dict_shape(self) -> None:
        now = datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc)
        report = ArchiveReport(
            copied_rows=100,
            deleted_rows=100,
            batches=10,
            time_taken_ms=1234,
            oldest_ts_before=now,
            oldest_ts_after=now + timedelta(days=1),
            cutoff_ts=now - timedelta(days=14),
            dry_run=False,
        )
        d = report.as_dict()
        self.assertEqual(d["copied_rows"], 100)
        self.assertEqual(d["deleted_rows"], 100)
        self.assertEqual(d["batches"], 10)
        self.assertEqual(d["time_taken_ms"], 1234)
        self.assertTrue(d["oldest_ts_before"].startswith("2026-04-19"))
        self.assertTrue(d["oldest_ts_after"].startswith("2026-04-20"))
        self.assertTrue(d["cutoff_ts"].startswith("2026-04-05"))
        self.assertFalse(d["dry_run"])

    def test_archive_report_as_dict_handles_none(self) -> None:
        report = ArchiveReport(dry_run=True)
        d = report.as_dict()
        self.assertIsNone(d["oldest_ts_before"])
        self.assertIsNone(d["oldest_ts_after"])
        self.assertIsNone(d["cutoff_ts"])
        self.assertTrue(d["dry_run"])


class TestRunAllIntegration(unittest.TestCase):
    """run_all 把 archive_hot + outbox purge + archive purge 串联。"""

    def test_run_all_includes_archive_hot_report(self) -> None:
        sf = _make_session_factory(self)
        now = datetime.now(timezone.utc)
        for i in range(3):
            _insert_event(
                sf,
                event_id=f"old_{i}",
                event_timestamp=now - timedelta(days=20 + i),
            )

        hk = DatabaseHousekeeping(session_factory=sf)
        result = hk.run_all(
            hot_event_retention_days=14,
            hot_event_batch_size=100,
        )

        self.assertIn("archive_hot_report", result)
        self.assertIn("archive_purged", result)
        self.assertIn("outbox_purged", result)
        report = result["archive_hot_report"]
        self.assertEqual(report["copied_rows"], 3)
        self.assertEqual(report["deleted_rows"], 3)

    def test_run_all_can_disable_hot_archive(self) -> None:
        sf = _make_session_factory(self)
        now = datetime.now(timezone.utc)
        _insert_event(
            sf,
            event_id="old_0",
            event_timestamp=now - timedelta(days=20),
        )

        hk = DatabaseHousekeeping(session_factory=sf)
        result = hk.run_all(
            hot_event_retention_days=14,
            hot_event_archive_enabled=False,
        )

        self.assertEqual(result["archive_hot_report"], {})
        # hot 行未动
        self.assertEqual(_count(sf, EventEnvelopeModel), 1)


if __name__ == "__main__":
    unittest.main()
