from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aats.bootstrap.config import build_runtime, build_storage_backends, load_settings
from aats.bootstrap.settings import AATSSettings
from aats.events import topics
from aats.events.envelopes import build_envelope, publish_model
from aats.schemas.audit import DecisionAuditRecord
from aats.schemas.common import utc_now
from aats.schemas.decision import BaselineAssessment, DecisionContext, PositionTarget
from aats.schemas.execution import OrderIntent
from aats.schemas.governance import PolicyDecision, RiskDecision
from aats.schemas.system import HealthSnapshot
from aats.services.portfolio_service.pnl import PortfolioPnLCalculator
from aats.services.portfolio_service.reconstruction import PortfolioReconstructionService
from aats.services.portfolio_service.snapshots import PortfolioSnapshotBuilder
from aats.services.reconciliation_service.replay import ReplayEngine
from aats.storage.audit_repo import InMemoryAuditRepository
from aats.storage.event_store import InMemoryEventStore


class TestPersistenceAndReplay(unittest.IsolatedAsyncioTestCase):
    async def test_sqlalchemy_event_store_persists_and_queries_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = self._sqlite_settings(Path(temp_dir))
            runtime = await build_runtime(settings)
            fresh_storage = None
            try:
                await runtime.market_gateway.run_local_publisher(
                    symbol=settings.default_symbol,
                    iterations=4,
                    interval_seconds=0.0,
                )

                audit_records = runtime.audit_repo.all()
                self.assertTrue(audit_records)

                event_store = runtime.event_store
                self.assertGreater(event_store.count(), 0)
                self.assertEqual(event_store.count(topic=topics.MARKET_SNAPSHOTS), 4)
                self.assertTrue(event_store.by_decision(audit_records[0].decision_id))

                fresh_storage = build_storage_backends(settings)
                self.assertEqual(fresh_storage.event_store.count(), event_store.count())
            finally:
                if runtime.database_runtime is not None:
                    runtime.database_runtime.dispose()
                if fresh_storage is not None and fresh_storage.database_runtime is not None:
                    fresh_storage.database_runtime.dispose()

    async def test_replay_reconstructs_same_portfolio_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = self._sqlite_settings(Path(temp_dir))
            runtime = await build_runtime(settings)
            storage = None
            try:
                await runtime.market_gateway.run_local_publisher(
                    symbol=settings.default_symbol,
                    iterations=4,
                    interval_seconds=0.0,
                )

                storage = build_storage_backends(settings)
                replay = ReplayEngine(
                    event_store=storage.event_store,
                    reconstruction_service=PortfolioReconstructionService(
                        initial_usdt_balance=settings.initial_usdt_balance,
                        snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
                    ),
                    audit_repo=storage.audit_repo,
                    portfolio_repo=storage.portfolio_repo,
                ).replay()

                self.assertEqual(replay.divergence_count, 0)
                self.assertEqual(replay.portfolio_issues, [])
                self.assertEqual(replay.decision_chain_issues, [])
                self.assertEqual(replay.execution_chain_issues, [])
                self.assertEqual(replay.audit_issues, [])
                self.assertIsNotNone(replay.final_reconstructed_snapshot)
                self.assertIsNotNone(replay.final_stored_snapshot)
                self.assertEqual(
                    self._snapshot_signature(replay.final_stored_snapshot),
                    self._snapshot_signature(runtime.portfolio_repo.latest()),
                )
            finally:
                if runtime.database_runtime is not None:
                    runtime.database_runtime.dispose()
                if storage is not None and storage.database_runtime is not None:
                    storage.database_runtime.dispose()

    async def test_audit_records_reference_persisted_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = self._sqlite_settings(Path(temp_dir))
            runtime = await build_runtime(settings)
            storage = None
            try:
                await runtime.market_gateway.run_local_publisher(
                    symbol=settings.default_symbol,
                    iterations=4,
                    interval_seconds=0.0,
                )

                storage = build_storage_backends(settings)
                event_store = storage.event_store
                for record in storage.audit_repo.all():
                    context_event = event_store.get(record.decision_context_ref)
                    self.assertIsNotNone(context_event)
                    if context_event is not None:
                        health_snapshot_ref = context_event.payload.get("health_snapshot_ref")
                        self.assertIsInstance(health_snapshot_ref, str)
                        health_event = event_store.get(health_snapshot_ref)
                        self.assertIsNotNone(health_event)
                        if health_event is not None:
                            self.assertEqual(health_event.topic, topics.HEALTH_SNAPSHOTS)
                            self.assertEqual(health_event.payload.get("decision_id"), record.decision_id)
                    if record.baseline_assessment_ref is not None:
                        self.assertIsNotNone(event_store.get(record.baseline_assessment_ref))
                    if record.ai_market_assessment_ref is not None:
                        self.assertIsNotNone(event_store.get(record.ai_market_assessment_ref))
                    if record.position_target_ref is not None:
                        self.assertIsNotNone(event_store.get(record.position_target_ref))
                    if record.policy_decision_ref is not None:
                        self.assertIsNotNone(event_store.get(record.policy_decision_ref))
                    if record.risk_decision_ref is not None:
                        self.assertIsNotNone(event_store.get(record.risk_decision_ref))
                    if record.execution_plan_ref is not None:
                        execution_plan_event = event_store.get(record.execution_plan_ref)
                        self.assertIsNotNone(execution_plan_event)
                        if execution_plan_event is not None:
                            self.assertEqual(execution_plan_event.topic, topics.EXECUTION_PLANS)
                    if record.portfolio_delta_ref is not None:
                        snapshot_event = event_store.get(record.portfolio_delta_ref)
                        self.assertIsNotNone(snapshot_event)
                        self.assertEqual(snapshot_event.payload.get("decision_id"), record.decision_id)
                    for ref in (
                        record.order_intent_refs
                        + record.order_state_refs
                        + record.fill_event_refs
                        + record.reconciliation_refs
                    ):
                        event = event_store.get(ref)
                        self.assertIsNotNone(event)
                        if event is not None and "decision_id" in event.payload:
                            self.assertEqual(event.payload.get("decision_id"), record.decision_id)
                    if record.order_intent_refs:
                        self.assertIsNotNone(record.execution_plan_ref)
                        self.assertTrue(record.order_state_refs)
            finally:
                if runtime.database_runtime is not None:
                    runtime.database_runtime.dispose()
                if storage is not None and storage.database_runtime is not None:
                    storage.database_runtime.dispose()

    async def test_replay_detects_missing_chain_reference(self) -> None:
        event_store = InMemoryEventStore()
        audit_repo = InMemoryAuditRepository()
        decision_id = "decision_missing_baseline"
        context = DecisionContext(
            decision_id=decision_id,
            symbol="BTC-USDT",
            timeframe="15m",
            as_of_ts=utc_now(),
            market_snapshot_ref="evt_market_1",
            feature_snapshot_ref="evt_feature_1",
            portfolio_snapshot_ref="evt_portfolio_1",
            health_snapshot_ref="TODO_health_snapshot_ref",
            mode="paper_live",
            current_position_qty=0.0,
        )
        target = PositionTarget(
            decision_id=decision_id,
            symbol="BTC-USDT",
            current_position_qty=0.0,
            target_position_qty=0.001,
            delta_position_qty=0.001,
            current_notional=0.0,
            target_notional=100.0,
            rebalance_reason="test",
            urgency="medium",
            max_slippage_tolerance_bps=20,
            source_mix={"baseline": 1.0},
            decision_expiry_ts=utc_now(),
        )
        policy = PolicyDecision(
            decision_id=decision_id,
            mode="paper_live",
            allowed=True,
            requires_human_approval=False,
            allowed_symbols=["BTC-USDT"],
            allowed_execution_styles=["market"],
        )
        risk = RiskDecision(
            decision_id=decision_id,
            approved=True,
            modified=False,
            capped_target_position_qty=0.001,
            risk_score=0.1,
        )
        context_event = build_envelope(
            topic=topics.DECISION_CONTEXTS,
            key="BTC-USDT",
            payload_model=context,
            source_component="test",
        )
        target_event = build_envelope(
            topic=topics.POSITION_TARGETS,
            key="BTC-USDT",
            payload_model=target,
            source_component="test",
        )
        policy_event = build_envelope(
            topic=topics.POLICY_DECISIONS,
            key="BTC-USDT",
            payload_model=policy,
            source_component="test",
        )
        risk_event = build_envelope(
            topic=topics.RISK_DECISIONS,
            key="BTC-USDT",
            payload_model=risk,
            source_component="test",
        )
        for envelope in (context_event, target_event, policy_event, risk_event):
            event_store.append(envelope)

        record = DecisionAuditRecord(
            decision_id=decision_id,
            decision_context_ref=context_event.event_id,
            baseline_assessment_ref="evt_missing_baseline",
            position_target_ref=target_event.event_id,
            policy_decision_ref=policy_event.event_id,
            risk_decision_ref=risk_event.event_id,
        )
        audit_repo.upsert(record)
        event_store.append(
            build_envelope(
                topic=topics.AUDIT_RECORDS,
                key=decision_id,
                payload_model=record,
                source_component="test",
            )
        )

        replay = ReplayEngine(
            event_store=event_store,
            reconstruction_service=PortfolioReconstructionService(
                initial_usdt_balance=10_000.0,
                snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
            ),
            audit_repo=audit_repo,
        ).replay()

        self.assertGreater(replay.divergence_count, 0)
        self.assertTrue(
            any("baseline_assessment_ref" in issue for issue in replay.decision_chain_issues)
        )
        self.assertTrue(
            any("missing_health_snapshot" in issue for issue in replay.decision_chain_issues)
        )

    async def test_duplicate_fill_does_not_mutate_portfolio_twice_and_replay_stays_reconstructable(self) -> None:
        settings = load_settings()
        runtime = await build_runtime(settings)

        await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=2,
            interval_seconds=0.0,
        )

        original_snapshot = runtime.portfolio_repo.latest()
        self.assertIsNotNone(original_snapshot)
        original_fill = runtime.execution_repo.fills()[0]

        await publish_model(
            bus=runtime.bus,
            topic=topics.FILL_EVENTS,
            key=original_fill.symbol,
            payload_model=original_fill,
            source_component="test_duplicate_fill",
        )

        duplicate_snapshot = runtime.portfolio_repo.latest()
        self.assertIsNotNone(duplicate_snapshot)
        self.assertEqual(
            self._snapshot_signature(duplicate_snapshot),
            self._snapshot_signature(original_snapshot),
        )

        replay = ReplayEngine(
            event_store=runtime.event_store,
            reconstruction_service=PortfolioReconstructionService(
                initial_usdt_balance=settings.initial_usdt_balance,
                snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
            ),
            audit_repo=runtime.audit_repo,
            portfolio_repo=runtime.portfolio_repo,
        ).replay()

        self.assertEqual(
            self._snapshot_signature(replay.final_stored_snapshot),
            self._snapshot_signature(duplicate_snapshot),
        )
        self.assertTrue(
            any("execution_chain_duplicate_fill_events" in issue for issue in replay.execution_chain_issues)
        )

    async def test_replay_detects_missing_execution_plan_for_emitted_intent(self) -> None:
        event_store = InMemoryEventStore()
        audit_repo = InMemoryAuditRepository()
        decision_id = "decision_missing_execution_plan"
        health_snapshot = HealthSnapshot(
            decision_id=decision_id,
            mode="paper_live",
            operating_state="real_market_paper",
            status="ok",
            halted=False,
            blockers=[],
            components=[],
        )
        health_event = build_envelope(
            topic=topics.HEALTH_SNAPSHOTS,
            key="BTC-USDT",
            payload_model=health_snapshot,
            source_component="test",
        )
        context = DecisionContext(
            decision_id=decision_id,
            symbol="BTC-USDT",
            timeframe="15m",
            as_of_ts=utc_now(),
            market_snapshot_ref="evt_market_1",
            feature_snapshot_ref="evt_feature_1",
            portfolio_snapshot_ref="evt_portfolio_1",
            health_snapshot_ref=health_event.event_id,
            mode="paper_live",
            current_position_qty=0.0,
        )
        baseline = BaselineAssessment(
            decision_id=decision_id,
            symbol="BTC-USDT",
            regime="trend",
            direction_bias="long",
            trend_strength=0.8,
            volatility_state="medium",
            confidence=0.7,
            holding_horizon="15m",
            invalidation_conditions=[],
            reason_codes=["test"],
            engine_version="test",
        )
        target = PositionTarget(
            decision_id=decision_id,
            symbol="BTC-USDT",
            current_position_qty=0.0,
            target_position_qty=0.001,
            delta_position_qty=0.001,
            current_notional=0.0,
            target_notional=100.0,
            rebalance_reason="test",
            urgency="medium",
            max_slippage_tolerance_bps=20,
            source_mix={"baseline": 1.0},
            decision_expiry_ts=utc_now(),
        )
        policy = PolicyDecision(
            decision_id=decision_id,
            mode="paper_live",
            allowed=True,
            execution_allowed=True,
            submission_allowed=False,
            dry_run_only=False,
            requires_human_approval=False,
            allowed_symbols=["BTC-USDT"],
            allowed_execution_styles=["market"],
        )
        risk = RiskDecision(
            decision_id=decision_id,
            approved=True,
            modified=False,
            capped_target_position_qty=0.001,
            risk_score=0.1,
        )
        intent = OrderIntent(
            intent_id="intent_missing_plan",
            decision_id=decision_id,
            symbol="BTC-USDT",
            side="buy",
            quantity=0.001,
            execution_style="taker",
            order_type="market",
            urgency="medium",
            time_in_force="IOC",
            idempotency_key="intent_missing_plan",
        )
        context_event = build_envelope(
            topic=topics.DECISION_CONTEXTS,
            key="BTC-USDT",
            payload_model=context,
            source_component="test",
        )
        baseline_event = build_envelope(
            topic=topics.BASELINE_ASSESSMENTS,
            key="BTC-USDT",
            payload_model=baseline,
            source_component="test",
        )
        target_event = build_envelope(
            topic=topics.POSITION_TARGETS,
            key="BTC-USDT",
            payload_model=target,
            source_component="test",
        )
        policy_event = build_envelope(
            topic=topics.POLICY_DECISIONS,
            key="BTC-USDT",
            payload_model=policy,
            source_component="test",
        )
        risk_event = build_envelope(
            topic=topics.RISK_DECISIONS,
            key="BTC-USDT",
            payload_model=risk,
            source_component="test",
        )
        intent_event = build_envelope(
            topic=topics.ORDER_INTENTS,
            key="BTC-USDT",
            payload_model=intent,
            source_component="test",
        )
        for envelope in (
            health_event,
            context_event,
            baseline_event,
            target_event,
            policy_event,
            risk_event,
            intent_event,
        ):
            event_store.append(envelope)

        record = DecisionAuditRecord(
            decision_id=decision_id,
            decision_context_ref=context_event.event_id,
            baseline_assessment_ref=baseline_event.event_id,
            position_target_ref=target_event.event_id,
            policy_decision_ref=policy_event.event_id,
            risk_decision_ref=risk_event.event_id,
            order_intent_refs=[intent_event.event_id],
        )
        audit_repo.upsert(record)
        event_store.append(
            build_envelope(
                topic=topics.AUDIT_RECORDS,
                key=decision_id,
                payload_model=record,
                source_component="test",
            )
        )

        replay = ReplayEngine(
            event_store=event_store,
            reconstruction_service=PortfolioReconstructionService(
                initial_usdt_balance=10_000.0,
                snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
            ),
            audit_repo=audit_repo,
        ).replay()

        self.assertGreater(replay.divergence_count, 0)
        self.assertTrue(
            any("missing_execution_plan" in issue for issue in replay.decision_chain_issues)
        )

    @staticmethod
    def _sqlite_settings(temp_dir: Path) -> AATSSettings:
        database_path = (temp_dir / "aats.db").resolve().as_posix()
        return AATSSettings.model_validate(
            {
                "storage_mode": "postgres",
                "database_url": f"sqlite+pysqlite:///{database_path}",
                "database_auto_create_schema": True,
                "local_publish_iterations": 4,
                "local_publish_interval_seconds": 0.0,
            }
        )

    @staticmethod
    def _snapshot_signature(snapshot) -> dict:
        return {
            "balances": snapshot.balances,
            "positions": {position.symbol: position.position_qty for position in snapshot.positions},
            "cost_basis": snapshot.cost_basis,
            "realized_pnl": snapshot.realized_pnl,
            "unrealized_pnl": snapshot.unrealized_pnl,
            "total_equity": snapshot.total_equity,
            "gross_exposure": snapshot.gross_exposure,
            "net_exposure": snapshot.net_exposure,
        }


if __name__ == "__main__":
    unittest.main()
