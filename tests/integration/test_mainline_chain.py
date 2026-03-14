from __future__ import annotations

import unittest

from aats.bootstrap.config import build_runtime
from aats.bootstrap.settings import AATSSettings
from aats.events import topics
from aats.schemas.decision import PositionTarget
from aats.schemas.execution import FillEvent
from aats.schemas.governance import PolicyDecision, RiskDecision
from aats.schemas.portfolio import PortfolioSnapshot
from aats.schemas.reconciliation import ReconciliationReport


class TestMainlineTradingChain(unittest.IsolatedAsyncioTestCase):
    async def test_local_demo_chain_is_complete_for_execution_eligible_decisions(self) -> None:
        runtime = await build_runtime(self._paper_settings(config_profile="local_demo", execution_backend="paper"))
        await self._assert_complete_mainline_chain(runtime=runtime, iterations=6)

    async def test_real_market_paper_chain_remains_complete_with_paper_execution(self) -> None:
        runtime = await build_runtime(
            self._paper_settings(
                config_profile="real_market_paper",
                execution_backend="okx",
                market_data_backend="demo",
            )
        )
        await self._assert_complete_mainline_chain(runtime=runtime, iterations=6)

    async def _assert_complete_mainline_chain(self, *, runtime, iterations: int) -> None:
        await runtime.market_gateway.run_local_publisher(
            symbol=runtime.settings.default_symbol,
            iterations=iterations,
            interval_seconds=0.0,
        )

        self.assertEqual(runtime.event_store.count(topic=topics.MARKET_SNAPSHOTS), iterations)
        self.assertEqual(runtime.event_store.count(topic=topics.FEATURE_SNAPSHOTS), iterations)

        decision_ids = sorted(
            {
                record.decision_id
                for record in runtime.audit_repo.all()
                if record.decision_context_ref is not None
            }
        )
        self.assertTrue(decision_ids)

        events_by_id = {event.event_id: event for event in runtime.event_store.all()}
        executed_decision_ids: set[str] = set()

        for decision_id in decision_ids:
            record = runtime.audit_repo.get(decision_id)
            self.assertIsNotNone(record)
            if record is None:
                continue

            policy_event = events_by_id.get(record.policy_decision_ref)
            target_event = events_by_id.get(record.position_target_ref)
            self.assertIsNotNone(policy_event)
            self.assertIsNotNone(target_event)
            self.assertIsNotNone(record.baseline_assessment_ref)

            policy = PolicyDecision.model_validate(policy_event.payload)
            target = PositionTarget.model_validate(target_event.payload)
            self.assertEqual(policy.decision_id, decision_id)

            if not policy.execution_allowed:
                self.assertIsNone(record.risk_decision_ref)
                self.assertEqual(record.order_intent_refs, [])
                continue

            risk_event = events_by_id.get(record.risk_decision_ref)
            self.assertIsNotNone(risk_event)
            risk = RiskDecision.model_validate(risk_event.payload)
            self.assertEqual(risk.decision_id, decision_id)

            if not risk.approved or risk.halt_required or abs(target.delta_position_qty) < 1e-12:
                self.assertEqual(record.order_intent_refs, [])
                continue

            executed_decision_ids.add(decision_id)
            self.assertIsNotNone(record.execution_plan_ref)
            self.assertTrue(record.order_intent_refs)
            self.assertTrue(record.order_state_refs)
            self.assertTrue(record.fill_event_refs)
            self.assertIsNotNone(record.portfolio_delta_ref)
            self.assertTrue(record.reconciliation_refs)

            fill_ids: set[str] = set()
            for ref in record.fill_event_refs:
                fill_event = events_by_id.get(ref)
                self.assertIsNotNone(fill_event)
                fill = FillEvent.model_validate(fill_event.payload)
                self.assertEqual(fill.decision_id, decision_id)
                fill_ids.add(fill.fill_id)

            portfolio_event = events_by_id.get(record.portfolio_delta_ref)
            self.assertIsNotNone(portfolio_event)
            snapshot = PortfolioSnapshot.model_validate(portfolio_event.payload)
            self.assertEqual(snapshot.decision_id, decision_id)
            self.assertIn(snapshot.source_fill_id, fill_ids)

            for ref in record.reconciliation_refs:
                reconciliation_event = events_by_id.get(ref)
                self.assertIsNotNone(reconciliation_event)
                report = ReconciliationReport.model_validate(reconciliation_event.payload)
                self.assertEqual(report.decision_id, decision_id)
                self.assertEqual(report.portfolio_snapshot_ref, record.portfolio_delta_ref)

        self.assertTrue(executed_decision_ids)

        fill_ids = {fill.fill_id for fill in runtime.execution_repo.fills()}
        decision_snapshots = [
            snapshot
            for snapshot in runtime.portfolio_repo.history()
            if snapshot.decision_id is not None
        ]
        self.assertTrue(decision_snapshots)
        for snapshot in decision_snapshots:
            self.assertIsNotNone(snapshot.source_fill_id)
            self.assertIn(snapshot.source_fill_id, fill_ids)

        reconciliation_history = runtime.reconciliation_repo.history()
        self.assertTrue(reconciliation_history)
        executed_reports = [report for report in reconciliation_history if report.decision_id in executed_decision_ids]
        self.assertTrue(executed_reports)
        for report in executed_reports:
            self.assertIsNotNone(report.portfolio_snapshot_ref)

    @staticmethod
    def _paper_settings(
        *,
        config_profile: str,
        execution_backend: str,
        market_data_backend: str = "demo",
    ) -> AATSSettings:
        return AATSSettings.model_validate(
            {
                "config_profile": config_profile,
                "mode": "paper_live",
                "market_data_backend": market_data_backend,
                "execution_backend": execution_backend,
                "account_backend": "disabled",
                "account_read_enabled": False,
                "storage_mode": "memory",
                "event_persistence_mode": "strict",
                "enabled_decision_timeframes": ("15m",),
            }
        )


if __name__ == "__main__":
    unittest.main()
