from __future__ import annotations

import unittest
from decimal import Decimal

from sqlalchemy import func, select

from aats.bus.memory_bus import InMemoryEventBus
from aats.events import topics
from aats.schemas.common import utc_now
from aats.schemas.portfolio import FillOutcomeRecord, PortfolioBalanceDelta, PortfolioSnapshot
from aats.services.portfolio_service.outbox import PostgresPortfolioOutboxPublisher
from aats.storage.event_store_postgres import PostgresEventStore
from aats.storage.fill_outcome_repo_postgres import PostgresFillOutcomeRepository
from aats.storage.outbox_repo_postgres import PostgresOutboxRepository
from aats.storage.portfolio_repo_postgres import PostgresPortfolioRepository
from aats.storage.sqlalchemy_models import FillOutcomeModel, OutboxEventModel, PortfolioSnapshotModel
from tests.support.postgres import temporary_postgres_runtime


class _ExplodingFillOutcomeRepository(PostgresFillOutcomeRepository):
    def save_outcome_in_session(self, session, outcome):  # type: ignore[override]
        _ = (session, outcome)
        raise RuntimeError("fill_outcome_boom")


class TestPortfolioOutboxPostgres(unittest.IsolatedAsyncioTestCase):
    async def test_persist_snapshot_writes_snapshot_event_and_outbox(self) -> None:
        with temporary_postgres_runtime() as (runtime, _admin_engine, _schema_name):
            bus = InMemoryEventBus()
            event_store = PostgresEventStore(runtime.session_factory)
            outbox_repo = PostgresOutboxRepository(runtime.session_factory)
            publisher = PostgresPortfolioOutboxPublisher(
                session_factory=runtime.session_factory,
                event_store=event_store,
                outbox_repo=outbox_repo,
                bus=bus,
                portfolio_repo=PostgresPortfolioRepository(runtime.session_factory),
                fill_outcome_repo=PostgresFillOutcomeRepository(runtime.session_factory),
            )

            await publisher.persist_snapshot(
                snapshot=_snapshot(),
                source_component="test_recovery",
            )

            with runtime.session_factory() as session:
                self.assertEqual(session.scalar(select(func.count()).select_from(PortfolioSnapshotModel)), 1)
                self.assertEqual(session.scalar(select(func.count()).select_from(FillOutcomeModel)), 0)
            self.assertEqual(event_store.count(topic=topics.PORTFOLIO_SNAPSHOTS), 1)
            self.assertEqual(outbox_repo.counts(), {"pending": 0, "published": 1, "failed": 0})

    async def test_persist_fill_projection_keeps_snapshot_and_outcome_atomic(self) -> None:
        with temporary_postgres_runtime() as (runtime, _admin_engine, _schema_name):
            bus = InMemoryEventBus()
            publisher = PostgresPortfolioOutboxPublisher(
                session_factory=runtime.session_factory,
                event_store=PostgresEventStore(runtime.session_factory),
                outbox_repo=PostgresOutboxRepository(runtime.session_factory),
                bus=bus,
                portfolio_repo=PostgresPortfolioRepository(runtime.session_factory),
                fill_outcome_repo=_ExplodingFillOutcomeRepository(runtime.session_factory),
            )

            with self.assertRaisesRegex(RuntimeError, "fill_outcome_boom"):
                await publisher.persist_fill_projection(
                    snapshot=_snapshot(),
                    balance_delta=_balance_delta(),
                    outcome=_outcome(),
                    source_component="test",
                )

            with runtime.session_factory() as session:
                self.assertEqual(session.scalar(select(func.count()).select_from(PortfolioSnapshotModel)), 0)
                self.assertEqual(session.scalar(select(func.count()).select_from(FillOutcomeModel)), 0)
                self.assertEqual(session.scalar(select(func.count()).select_from(OutboxEventModel)), 0)

    async def test_persist_fill_projection_leaves_pending_outbox_when_subscriber_fails(self) -> None:
        with temporary_postgres_runtime() as (runtime, _admin_engine, _schema_name):
            bus = InMemoryEventBus()

            async def exploding_handler(message: dict) -> None:
                _ = message
                raise RuntimeError("portfolio_subscriber_boom")

            await bus.subscribe(topics.PORTFOLIO_SNAPSHOTS, exploding_handler)
            publisher = PostgresPortfolioOutboxPublisher(
                session_factory=runtime.session_factory,
                event_store=PostgresEventStore(runtime.session_factory),
                outbox_repo=PostgresOutboxRepository(runtime.session_factory),
                bus=bus,
                portfolio_repo=PostgresPortfolioRepository(runtime.session_factory),
                fill_outcome_repo=PostgresFillOutcomeRepository(runtime.session_factory),
            )

            await publisher.persist_fill_projection(
                snapshot=_snapshot(),
                balance_delta=_balance_delta(),
                outcome=_outcome(),
                source_component="test",
            )

            with runtime.session_factory() as session:
                self.assertEqual(session.scalar(select(func.count()).select_from(PortfolioSnapshotModel)), 1)
                self.assertEqual(session.scalar(select(func.count()).select_from(FillOutcomeModel)), 1)
                self.assertEqual(
                    session.scalar(
                        select(func.count()).select_from(OutboxEventModel).where(OutboxEventModel.status == "PENDING")
                    ),
                    1,
                )

    async def test_persist_fill_projection_rolls_back_pre_commit_actions_atomically(self) -> None:
        with temporary_postgres_runtime() as (runtime, _admin_engine, _schema_name):
            bus = InMemoryEventBus()
            publisher = PostgresPortfolioOutboxPublisher(
                session_factory=runtime.session_factory,
                event_store=PostgresEventStore(runtime.session_factory),
                outbox_repo=PostgresOutboxRepository(runtime.session_factory),
                bus=bus,
                portfolio_repo=PostgresPortfolioRepository(runtime.session_factory),
                fill_outcome_repo=PostgresFillOutcomeRepository(runtime.session_factory),
            )

            with self.assertRaisesRegex(RuntimeError, "pre_commit_boom"):
                await publisher.persist_fill_projection(
                    snapshot=_snapshot(),
                    balance_delta=_balance_delta(),
                    outcome=_outcome(),
                    source_component="test",
                    pre_commit_actions=(lambda session: (_ for _ in ()).throw(RuntimeError("pre_commit_boom")),),
                )

            with runtime.session_factory() as session:
                self.assertEqual(session.scalar(select(func.count()).select_from(PortfolioSnapshotModel)), 0)
                self.assertEqual(session.scalar(select(func.count()).select_from(FillOutcomeModel)), 0)
                self.assertEqual(session.scalar(select(func.count()).select_from(OutboxEventModel)), 0)


def _snapshot() -> PortfolioSnapshot:
    now = utc_now()
    return PortfolioSnapshot(
        snapshot_ts=now,
        created_at=now,
        balances={"USDT": Decimal("950"), "BTC": Decimal("1")},
        positions=[],
        realized_pnl=Decimal("5"),
        unrealized_pnl=Decimal("0"),
        total_equity=Decimal("955"),
        gross_exposure=Decimal("100"),
        net_exposure=Decimal("100"),
        risk_budget_usage={},
        product_type="spot",
        margin_mode="cash",
    )


def _balance_delta() -> PortfolioBalanceDelta:
    now = utc_now()
    return PortfolioBalanceDelta(
        fill_id="fill_portfolio_outbox",
        decision_id="decision_portfolio_outbox",
        intent_id="intent_portfolio_outbox",
        order_id="clord_portfolio_outbox",
        symbol="BTC-USDT",
        product_type="spot",
        margin_mode="cash",
        balance_deltas={"USDT": Decimal("-100"), "BTC": Decimal("1")},
        balances_before={"USDT": Decimal("1050")},
        balances_after={"USDT": Decimal("950"), "BTC": Decimal("1")},
        realized_pnl_delta=Decimal("5"),
        fee_delta=Decimal("1"),
        created_at=now,
    )


def _outcome() -> FillOutcomeRecord:
    now = utc_now()
    return FillOutcomeRecord(
        fill_id="fill_portfolio_outbox",
        decision_id="decision_portfolio_outbox",
        intent_id="intent_portfolio_outbox",
        order_id="clord_portfolio_outbox",
        symbol="BTC-USDT",
        venue="OKX",
        side="buy",
        fill_qty=Decimal("1"),
        fill_price=Decimal("100"),
        fill_notional=Decimal("100"),
        fee_amount=Decimal("1"),
        fee_currency="USDT",
        liquidity_role="taker",
        exchange_timestamp=now,
        ingestion_timestamp=now,
        order_status_after_fill="FILLED",
        target_leverage=1.0,
        exposure_side="long",
        execution_action="enter",
        position_intent="open_long",
        starting_position_qty=Decimal("0"),
        starting_avg_entry_price=Decimal("0"),
        ending_position_qty=Decimal("1"),
        ending_avg_entry_price=Decimal("100"),
        realized_pnl_delta=Decimal("5"),
        fee_delta=Decimal("1"),
        product_type="spot",
        margin_mode="cash",
        created_at=now,
    )
