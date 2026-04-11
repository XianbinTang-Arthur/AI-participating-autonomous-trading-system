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

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, func
from sqlalchemy.orm import Session, sessionmaker

from aats.storage.sqlalchemy_models import (
    EventEnvelopeArchiveModel,
    OutboxEventModel,
)


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
    # 组合执行
    # ──────────────────────────────────────────────────────────────────

    def run_all(
        self,
        *,
        outbox_older_than_days: int = 7,
        archive_older_than_days: int = 90,
        batch_size: int = 1000,
    ) -> dict[str, int]:
        """一次性执行所有清理任务。返回各项删除行数。"""
        outbox_purged = self.purge_published_outbox(
            older_than_days=outbox_older_than_days,
            batch_size=batch_size,
        )
        archive_purged = self.purge_old_archive_events(
            older_than_days=archive_older_than_days,
            batch_size=batch_size,
        )
        return {
            "outbox_purged": outbox_purged,
            "archive_purged": archive_purged,
        }
