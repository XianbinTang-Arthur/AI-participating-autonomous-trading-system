"""P3-1 / P3-2 数据库定期清理工具。

管理两类历史数据的生命周期：

P3-1 Outbox 已发布行清理
========================
``outbox_events`` 表中 ``status='PUBLISHED'`` 的行在 NATS 广播后已无作用。
``purge_published_outbox`` 删除 ``published_at`` 早于指定天数的已发布行，
减少表膨胀。

P3-2 EventStore 归档表老化
===========================
``event_store_archive`` 表存储从 ``event_store`` 搬运过来的历史 envelope。
``purge_old_archive_events`` 删除 ``event_timestamp`` 早于指定天数的归档行，
控制归档表无限增长。

Path B Phase 1 — EventStore 热/冷分离
======================================
``archive_hot_event_store`` 把 ``event_store`` 中早于 N 天的行批量搬到
``event_store_archive``。每个 batch 一个事务：先 INSERT (幂等) 再 DELETE；
INSERT 失败时事务 rollback，热表完整无损。
读路径 (``list_envelopes`` UNION) 已经覆盖 archive，调用方透明。

调用时机
========
- 可由后台 asyncio.Task 定期执行（如 24h 一次）
- 或由运维脚本手动触发
- 或集成到现有 recovery_service 的 startup 流程

安全设计
========
- 每次删除有 ``batch_size`` 上限（默认 1000），避免长事务锁。
- 返回实际删除行数供日志记录。
- 所有操作在独立 session 中执行，不影响主业务事务。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete, insert as sa_insert, select, func, text
from sqlalchemy.orm import Session, sessionmaker

from aats.storage.sqlalchemy_models import (
    EventEnvelopeArchiveModel,
    EventEnvelopeModel,
    OutboxEventModel,
)


@dataclass
class ArchiveReport:
    """Path B Phase 1: archive_hot_event_store 返回报告。

    Attributes:
        copied_rows: 真正 INSERT 进 archive 的行数（去重后）。
        deleted_rows: 从 hot 删除的行数（幂等情况下等于 SELECT 出来的 batch 大小，
            可能 >= copied_rows：已存在于 archive 的行会 NOOP 插入但仍会删 hot）。
        batches: 实际执行的 batch 数。
        time_taken_ms: 从 start 到 end 总耗时 (毫秒)。
        oldest_ts_before: 归档前 hot 表最早 event_timestamp。
        oldest_ts_after: 归档后 hot 表最早 event_timestamp。
        cutoff_ts: 本次归档所使用的 cutoff 时间戳（绝对时刻）。
        dry_run: 是否 dry-run 模式（True = 只统计不实际搬运）。
    """

    copied_rows: int = 0
    deleted_rows: int = 0
    batches: int = 0
    time_taken_ms: int = 0
    oldest_ts_before: Optional[datetime] = None
    oldest_ts_after: Optional[datetime] = None
    cutoff_ts: Optional[datetime] = None
    dry_run: bool = False

    def as_dict(self) -> dict[str, object]:
        """转换为 dict 以便 log_event / JSON 序列化。"""
        return {
            "copied_rows": self.copied_rows,
            "deleted_rows": self.deleted_rows,
            "batches": self.batches,
            "time_taken_ms": self.time_taken_ms,
            "oldest_ts_before": self.oldest_ts_before.isoformat()
            if self.oldest_ts_before is not None
            else None,
            "oldest_ts_after": self.oldest_ts_after.isoformat()
            if self.oldest_ts_after is not None
            else None,
            "cutoff_ts": self.cutoff_ts.isoformat()
            if self.cutoff_ts is not None
            else None,
            "dry_run": self.dry_run,
        }


class DatabaseHousekeeping:
    """定期清理 outbox 已发布行 + event_store_archive 老化行。"""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    # ──────────────────────────────────────────────────────────────────
    # P3-1：Outbox 已发布行清理
    # ──────────────────────────────────────────────────────────────────

    def purge_published_outbox(
        self,
        *,
        older_than_days: int = 7,
        batch_size: int = 1000,
    ) -> int:
        """删除已发布且 published_at 早于 older_than_days 天的 outbox 行。

        Returns:
            实际删除的行数。
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
        with self._session_factory() as session:
            # 先查出要删的 event_ids（带 batch_size 限制，避免长事务）
            ids_to_delete = session.scalars(
                select(OutboxEventModel.event_id)
                .where(
                    OutboxEventModel.status == "PUBLISHED",
                    OutboxEventModel.published_at <= cutoff,
                )
                .limit(batch_size)
            ).all()
            if not ids_to_delete:
                return 0
            result = session.execute(
                delete(OutboxEventModel)
                .where(OutboxEventModel.event_id.in_(ids_to_delete))
            )
            session.commit()
            return result.rowcount  # type: ignore[return-value]

    def outbox_stats(self) -> dict[str, int]:
        """返回 outbox 各状态计数。"""
        with self._session_factory() as session:
            rows = session.execute(
                select(
                    OutboxEventModel.status,
                    func.count(OutboxEventModel.event_id),
                ).group_by(OutboxEventModel.status)
            ).all()
        return {status: count for status, count in rows}

    # ──────────────────────────────────────────────────────────────────
    # P3-2：EventStore 归档表老化
    # ──────────────────────────────────────────────────────────────────

    def purge_old_archive_events(
        self,
        *,
        older_than_days: int = 90,
        batch_size: int = 1000,
    ) -> int:
        """删除 event_timestamp 早于 older_than_days 天的归档行。

        Returns:
            实际删除的行数。
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
        with self._session_factory() as session:
            ids_to_delete = session.scalars(
                select(EventEnvelopeArchiveModel.archive_sequence_id)
                .where(EventEnvelopeArchiveModel.event_timestamp <= cutoff)
                .order_by(EventEnvelopeArchiveModel.archive_sequence_id)
                .limit(batch_size)
            ).all()
            if not ids_to_delete:
                return 0
            result = session.execute(
                delete(EventEnvelopeArchiveModel)
                .where(
                    EventEnvelopeArchiveModel.archive_sequence_id.in_(ids_to_delete)
                )
            )
            session.commit()
            return result.rowcount  # type: ignore[return-value]

    def archive_stats(self) -> dict[str, int]:
        """返回归档表统计。"""
        with self._session_factory() as session:
            total = session.scalar(
                select(func.count(EventEnvelopeArchiveModel.archive_sequence_id))
            ) or 0
        return {"total_archived": total}

    # ──────────────────────────────────────────────────────────────────
    # Path B Phase 1：EventStore 热表 → archive 搬运
    # ──────────────────────────────────────────────────────────────────

    def archive_hot_event_store(
        self,
        *,
        older_than_days: int = 14,
        batch_size: int = 10_000,
        dry_run: bool = False,
        max_batches: int | None = None,
    ) -> ArchiveReport:
        """把 event_store 中早于 older_than_days 的行批量搬运到 event_store_archive。

        事务安全策略（每个 batch 独立事务）：
          1. 读一批 (按 sequence_id 升序 + 时间戳 < cutoff) SELECT ... LIMIT batch_size
          2. 用 Postgres INSERT ... ON CONFLICT (event_id) DO NOTHING
             把本批数据写入 archive（幂等，重复 event_id 跳过）
          3. DELETE 对应 sequence_id 范围的行
          4. commit；若任一步抛异常则 rollback，热表保持无损

        InMemory fallback (``_session_factory`` 为 None 或抽象):
          不支持此方法；仅 Postgres 路径生效。运维脚本已有 CLI 入口。

        Args:
            older_than_days: 保留热表 N 天数据，归档 >= N 天前的行。默认 14。
            batch_size: 每个事务处理的最大行数。默认 10_000。
            dry_run: 仅统计需要归档的行数，不实际搬运（不改数据）。
            max_batches: 最多运行 N 个 batch 后退出（None = 不限，搬完为止）。
                用于后台 loop 场景限流，避免单次 housekeeping_loop tick 占用过长。

        Returns:
            ArchiveReport：包含 copied/deleted/batches/耗时/cutoff 等信息。
        """
        import time as _time  # 延迟 import 避免 top-level 污染

        report = ArchiveReport(dry_run=dry_run)
        start_ns = _time.perf_counter_ns()

        cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
        report.cutoff_ts = cutoff

        # 记录搬运前 oldest_ts
        with self._session_factory() as session:
            oldest_before = session.scalar(
                select(func.min(EventEnvelopeModel.event_timestamp))
            )
            report.oldest_ts_before = oldest_before

        # dry_run：只统计将要归档的行数，不改数据
        if dry_run:
            with self._session_factory() as session:
                pending = session.scalar(
                    select(func.count(EventEnvelopeModel.sequence_id)).where(
                        EventEnvelopeModel.event_timestamp < cutoff
                    )
                ) or 0
            report.copied_rows = int(pending)
            # dry_run 下 deleted_rows 与 copied_rows 概念等同（预测值）
            report.deleted_rows = int(pending)
            report.oldest_ts_after = report.oldest_ts_before
            report.time_taken_ms = int(
                (_time.perf_counter_ns() - start_ns) // 1_000_000
            )
            return report

        batches_run = 0

        with self._session_factory() as session:
            dialect_name = session.get_bind().dialect.name
        if dialect_name == "postgresql":
            while True:
                if max_batches is not None and batches_run >= max_batches:
                    break
                copied_count, deleted_count = self._archive_hot_event_store_postgres_batch(
                    cutoff=cutoff,
                    batch_size=batch_size,
                )
                if deleted_count <= 0:
                    break
                report.copied_rows += copied_count
                report.deleted_rows += deleted_count
                report.batches += 1
                batches_run += 1

            with self._session_factory() as session:
                oldest_after = session.scalar(
                    select(func.min(EventEnvelopeModel.event_timestamp))
                )
                report.oldest_ts_after = oldest_after

            report.time_taken_ms = int(
                (_time.perf_counter_ns() - start_ns) // 1_000_000
            )
            return report

        # 实际搬运
        while True:
            if max_batches is not None and batches_run >= max_batches:
                break
            with self._session_factory() as session:
                # 1. 取一批 hot 行（只读字段，不加载 ORM 对象开销）
                #    ORDER BY sequence_id 保证稳定顺序；每次都从最旧开始。
                rows = session.execute(
                    select(EventEnvelopeModel)
                    .where(EventEnvelopeModel.event_timestamp < cutoff)
                    .order_by(EventEnvelopeModel.sequence_id)
                    .limit(batch_size)
                ).scalars().all()
                if not rows:
                    break

                try:
                    # 2. 方言无关幂等 INSERT：
                    #    先查 archive 里已经存在的 event_id 集合 → 只 INSERT 未存在的行。
                    #    避免依赖 postgres ON CONFLICT 子句，同时也能在 SQLite 单元
                    #    测试中正确工作。archive.event_id 带 UNIQUE 约束，作为最终
                    #    屏障；竞态下并发 INSERT 会被 DB 层拒绝。
                    batch_event_ids = [row.event_id for row in rows]
                    existing_event_ids = set(
                        session.scalars(
                            select(EventEnvelopeArchiveModel.event_id).where(
                                EventEnvelopeArchiveModel.event_id.in_(batch_event_ids)
                            )
                        ).all()
                    )
                    new_rows = [r for r in rows if r.event_id not in existing_event_ids]

                    inserted_count = 0
                    if new_rows:
                        archive_payloads = [
                            {
                                "source_sequence_id": r.sequence_id,
                                "event_id": r.event_id,
                                "schema_version": r.schema_version,
                                "created_at": r.created_at,
                                "event_type": r.event_type,
                                "event_timestamp": r.event_timestamp,
                                "source_component": r.source_component,
                                "topic": r.topic,
                                "event_key": r.event_key,
                                "decision_id": r.decision_id,
                                "symbol": r.symbol,
                                "timeframe": r.timeframe,
                                "product_type": r.product_type,
                                "margin_mode": r.margin_mode,
                                "payload": r.payload,
                            }
                            for r in new_rows
                        ]
                        result = session.execute(
                            sa_insert(EventEnvelopeArchiveModel.__table__),
                            archive_payloads,
                        )
                        inserted_count = result.rowcount or len(new_rows)

                    # 3. DELETE 对应 sequence_id（即使 archive 已存在也要删 hot,
                    #    否则本 batch 无限循环）
                    source_ids = [row.sequence_id for row in rows]
                    del_result = session.execute(
                        delete(EventEnvelopeModel)
                        .where(EventEnvelopeModel.sequence_id.in_(source_ids))
                    )
                    deleted_count = del_result.rowcount or 0

                    # 4. 提交事务
                    session.commit()

                    report.copied_rows += int(inserted_count)
                    report.deleted_rows += int(deleted_count)
                    report.batches += 1
                    batches_run += 1
                except Exception:
                    # 任何一步失败 → rollback，hot 表完整无损
                    session.rollback()
                    raise

        # 记录搬运后 oldest_ts
        with self._session_factory() as session:
            oldest_after = session.scalar(
                select(func.min(EventEnvelopeModel.event_timestamp))
            )
            report.oldest_ts_after = oldest_after

        report.time_taken_ms = int(
            (_time.perf_counter_ns() - start_ns) // 1_000_000
        )
        return report

    def _archive_hot_event_store_postgres_batch(
        self,
        *,
        cutoff: datetime,
        batch_size: int,
    ) -> tuple[int, int]:
        """Postgres fast path: archive one batch without Python-side row copying.

        The previous dialect-neutral path loaded up to 10k ORM rows, queried
        archive with a 10k-value IN list, then built 10k archive payload dicts
        while the session transaction stayed open. Large event payloads can
        leave the connection idle in an active transaction long enough for
        Postgres to terminate it. This CTE keeps selection, idempotent insert,
        and delete inside the database in one short transaction.
        """

        if batch_size <= 0:
            raise ValueError("hot_event_batch_size_must_be_positive")
        statement = text(
            """
            WITH candidates AS MATERIALIZED (
                SELECT
                    sequence_id,
                    event_id,
                    schema_version,
                    created_at,
                    event_type,
                    event_timestamp,
                    source_component,
                    topic,
                    event_key,
                    decision_id,
                    symbol,
                    timeframe,
                    product_type,
                    margin_mode,
                    payload
                FROM event_store
                WHERE event_timestamp < :cutoff_ts
                ORDER BY sequence_id
                LIMIT :batch_size
            ),
            inserted AS (
                INSERT INTO event_store_archive (
                    source_sequence_id,
                    event_id,
                    schema_version,
                    created_at,
                    event_type,
                    event_timestamp,
                    source_component,
                    topic,
                    event_key,
                    decision_id,
                    symbol,
                    timeframe,
                    product_type,
                    margin_mode,
                    payload
                )
                SELECT
                    sequence_id,
                    event_id,
                    schema_version,
                    created_at,
                    event_type,
                    event_timestamp,
                    source_component,
                    topic,
                    event_key,
                    decision_id,
                    symbol,
                    timeframe,
                    product_type,
                    margin_mode,
                    payload
                FROM candidates
                ON CONFLICT (event_id) DO NOTHING
                RETURNING 1
            ),
            deleted AS (
                DELETE FROM event_store AS hot
                USING candidates
                WHERE hot.sequence_id = candidates.sequence_id
                RETURNING 1
            )
            SELECT
                (SELECT count(*) FROM inserted) AS copied_count,
                (SELECT count(*) FROM deleted) AS deleted_count
            """
        )
        with self._session_factory() as session:
            try:
                row = session.execute(
                    statement,
                    {"cutoff_ts": cutoff, "batch_size": int(batch_size)},
                ).one()
                session.commit()
            except Exception:
                session.rollback()
                raise
        return int(row.copied_count or 0), int(row.deleted_count or 0)

    # ──────────────────────────────────────────────────────────────────
    # 组合执行
    # ──────────────────────────────────────────────────────────────────

    def run_all(
        self,
        *,
        outbox_older_than_days: int = 7,
        archive_older_than_days: int = 90,
        hot_event_retention_days: int = 14,
        batch_size: int = 1000,
        hot_event_batch_size: int = 10_000,
        hot_event_max_batches: int | None = 6,
        hot_event_archive_enabled: bool = True,
    ) -> dict[str, object]:
        """一次性执行所有清理 + 归档任务。

        调用顺序：
          1. purge_published_outbox (outbox)
          2. archive_hot_event_store (Phase 1 新加)
          3. purge_old_archive_events (archive)

        归档放在清理 archive 表之前，保证"先搬进来再删老的"。

        Args:
            outbox_older_than_days: 已发布 outbox 行保留天数。
            archive_older_than_days: 归档表行保留天数。
            hot_event_retention_days: event_store 热表保留天数，超过的行搬到 archive。
            batch_size: outbox / archive purge 单次事务行数。
            hot_event_batch_size: archive_hot_event_store 单批行数。
            hot_event_max_batches: archive_hot_event_store 单次 tick 最大批数
                （默认 6 批 × 10k = 60k 行/tick，10 分钟内完成）。None 表示不限。
            hot_event_archive_enabled: False 时跳过热表归档（用于紧急回滚）。

        Returns:
            各项统计的 dict，字段：
              - outbox_purged: int
              - archive_hot_report: dict (ArchiveReport.as_dict())
              - archive_purged: int
        """
        outbox_purged = self.purge_published_outbox(
            older_than_days=outbox_older_than_days,
            batch_size=batch_size,
        )
        archive_hot_report_dict: dict[str, object] = {}
        if hot_event_archive_enabled:
            archive_hot_report = self.archive_hot_event_store(
                older_than_days=hot_event_retention_days,
                batch_size=hot_event_batch_size,
                max_batches=hot_event_max_batches,
            )
            archive_hot_report_dict = archive_hot_report.as_dict()
        archive_purged = self.purge_old_archive_events(
            older_than_days=archive_older_than_days,
            batch_size=batch_size,
        )
        return {
            "outbox_purged": outbox_purged,
            "archive_hot_report": archive_hot_report_dict,
            "archive_purged": archive_purged,
        }
