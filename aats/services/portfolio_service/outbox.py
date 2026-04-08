from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from aats.bootstrap.logging import get_logger, log_event
from aats.bus.base import EventBus
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.common import EventEnvelope
from aats.schemas.portfolio import FillOutcomeRecord, PortfolioBalanceDelta, PortfolioSnapshot
from aats.services.portfolio_service.snapshot_cache import PortfolioSnapshotCache
from aats.storage.event_store_postgres import PostgresEventStore
from aats.storage.outbox_repo_postgres import PostgresOutboxRepository
from aats.storage.fill_outcome_repo_postgres import PostgresFillOutcomeRepository
from aats.storage.portfolio_repo_postgres import PostgresPortfolioRepository


@dataclass(slots=True)
class PostgresPortfolioOutboxPublisher:
    _MAX_PUBLISH_ATTEMPTS = 3
    session_factory: sessionmaker[Session]
    event_store: PostgresEventStore
    outbox_repo: PostgresOutboxRepository
    bus: EventBus
    portfolio_repo: PostgresPortfolioRepository
    fill_outcome_repo: PostgresFillOutcomeRepository
    # Stage 6 Slice 6.3：可选 portfolio_snapshot 跨进程 cache。在 build_runtime
    # 由 PortfolioSnapshotCache 实例注入。本地 execution 进程 commit 之后立刻
    # 把 snapshot 写入 cache（同步本地 dict + best-effort Redis），让 4 进程拓
    # 扑里的 dashboard / query 路径无须等 NATS 路径就能看到最新值。设计文档：
    # docs/task/stage_6_slice_6_3_portfolio_snapshot_design.md §4.2 D5
    snapshot_cache: PortfolioSnapshotCache | None = None
    logger: Any = field(init=False)

    def __post_init__(self) -> None:
        self.logger = get_logger("aats.portfolio_outbox")

    async def persist_bootstrap_snapshot(
        self,
        *,
        snapshot: PortfolioSnapshot,
        source_component: str,
    ) -> None:
        # bootstrap 在进程启动期跑一次，但 persist_fill_projection 是热路径
        # 每笔成交一次，两条路径都包含完整的 SQLAlchemy sync session 事务
        # （save_snapshot_in_session / append_in_session / enqueue_in_session
        # / commit）。全部丢到 asyncio.to_thread 让 event loop 专心调度协程。
        await asyncio.to_thread(
            self._persist_bootstrap_snapshot_sync,
            snapshot=snapshot,
            source_component=source_component,
        )
        # Stage 6 Slice 6.3：commit 成功后把最新 snapshot 注入跨进程 cache。
        # publish 内部 best-effort Redis + 同步本地 dict，不抛。
        await self._publish_to_cache(snapshot)
        await self.flush_pending()

    def _persist_bootstrap_snapshot_sync(
        self,
        *,
        snapshot: PortfolioSnapshot,
        source_component: str,
    ) -> None:
        with self.session_factory() as session:
            self.portfolio_repo.save_snapshot_in_session(session, snapshot)
            envelope = self._portfolio_snapshot_envelope(snapshot=snapshot, source_component=source_component)
            self.event_store.append_in_session(session, envelope)
            self.outbox_repo.enqueue_in_session(session, envelope)
            session.commit()

    async def persist_fill_projection(
        self,
        *,
        snapshot: PortfolioSnapshot,
        balance_delta: PortfolioBalanceDelta,
        outcome: FillOutcomeRecord,
        source_component: str,
        pre_commit_actions: Sequence[Callable[[Session], None]] = (),
    ) -> None:
        await asyncio.to_thread(
            self._persist_fill_projection_sync,
            snapshot=snapshot,
            balance_delta=balance_delta,
            outcome=outcome,
            source_component=source_component,
            pre_commit_actions=pre_commit_actions,
        )
        # Stage 6 Slice 6.3：commit 成功后把最新 snapshot 注入跨进程 cache。
        # 调用顺序：DB commit → cache publish (本地 dict + Redis) → flush_pending
        # (NATS 广播)。这样本地 execution 进程的 query 路径不必等 NATS roundtrip
        # 就能命中 cache，远端 3 个进程通过 NATS 接收。
        await self._publish_to_cache(snapshot)
        await self.flush_pending()

    def _persist_fill_projection_sync(
        self,
        *,
        snapshot: PortfolioSnapshot,
        balance_delta: PortfolioBalanceDelta,
        outcome: FillOutcomeRecord,
        source_component: str,
        pre_commit_actions: Sequence[Callable[[Session], None]],
    ) -> None:
        with self.session_factory() as session:
            self.persist_fill_projection_in_session(
                session=session,
                snapshot=snapshot,
                balance_delta=balance_delta,
                outcome=outcome,
                source_component=source_component,
                pre_commit_actions=pre_commit_actions,
            )
            session.commit()

    def persist_fill_projection_in_session(
        self,
        *,
        session: Session,
        snapshot: PortfolioSnapshot,
        balance_delta: PortfolioBalanceDelta,
        outcome: FillOutcomeRecord,
        source_component: str,
        pre_commit_actions: Sequence[Callable[[Session], None]] = (),
    ) -> None:
        for action in pre_commit_actions:
            action(session)
        self.portfolio_repo.save_snapshot_in_session(session, snapshot)
        self.fill_outcome_repo.save_outcome_in_session(session, outcome)
        envelopes = (
            self._balance_delta_envelope(balance_delta=balance_delta, source_component=source_component),
            self._portfolio_snapshot_envelope(snapshot=snapshot, source_component=source_component),
        )
        for envelope in envelopes:
            self.event_store.append_in_session(session, envelope)
            self.outbox_repo.enqueue_in_session(session, envelope)

    async def flush_pending(self, *, limit: int = 100) -> None:
        pending = await asyncio.to_thread(self.outbox_repo.pending, limit=limit)
        published_ids: list[str] = []
        for envelope in pending:
            try:
                await self.bus.publish_envelope(envelope, persist=False)
                published_ids.append(envelope.event_id)
            except Exception as exc:
                if published_ids:
                    await asyncio.to_thread(self.outbox_repo.mark_published_batch, published_ids)
                    published_ids = []
                status = await asyncio.to_thread(
                    self.outbox_repo.record_failure_with_threshold,
                    envelope.event_id,
                    str(exc),
                    max_attempts=self._MAX_PUBLISH_ATTEMPTS,
                )
                log_event(
                    self.logger,
                    "portfolio_outbox_publish_failed",
                    level="error",
                    topic=envelope.topic,
                    key=envelope.key,
                    event_id=envelope.event_id,
                    outbox_status=status,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                if status != "FAILED":
                    break
        if published_ids:
            await asyncio.to_thread(self.outbox_repo.mark_published_batch, published_ids)

    async def _publish_to_cache(self, snapshot: PortfolioSnapshot) -> None:
        """Stage 6 Slice 6.3：commit hook，把最新 snapshot 注入跨进程 cache。

        - 当 ``snapshot_cache`` 为 None（构造时未注入），整个调用 noop。
        - cache.publish 内部 best-effort，理论不抛；但仍然 try/except 兜底，
          保护 outbox 主路径不受 cache 子系统拖累。
        """
        if self.snapshot_cache is None:
            return
        try:
            await self.snapshot_cache.publish(snapshot)
        except Exception as exc:
            log_event(
                self.logger,
                "portfolio_outbox_cache_publish_failed",
                level="warning",
                error_type=type(exc).__name__,
                error=str(exc),
            )

    @staticmethod
    def _portfolio_snapshot_envelope(
        *,
        snapshot: PortfolioSnapshot,
        source_component: str,
    ) -> EventEnvelope:
        return build_envelope(
            topic=topics.PORTFOLIO_SNAPSHOTS,
            key="portfolio",
            payload_model=snapshot,
            source_component=source_component,
        )

    @staticmethod
    def _balance_delta_envelope(
        *,
        balance_delta: PortfolioBalanceDelta,
        source_component: str,
    ) -> EventEnvelope:
        return build_envelope(
            topic=topics.PORTFOLIO_BALANCE_DELTAS,
            key=balance_delta.symbol,
            payload_model=balance_delta,
            source_component=source_component,
        )
