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
