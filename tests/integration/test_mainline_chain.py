from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from aats.bootstrap.config import build_runtime
from aats.bootstrap.settings import AATSSettings
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.decision import HedgeOverlayDecision, PositionTarget
from aats.schemas.execution import FillEvent
from aats.schemas.features import FeatureSnapshot
from aats.schemas.governance import (
    DerivativesExposureLimits,
    DerivativesExposureMetrics,
    PolicyDecision,
    RiskDecision,
)
from aats.schemas.market import MarketSnapshot
from aats.schemas.portfolio import PortfolioSnapshot, Position
from aats.schemas.reconciliation import ReconciliationReport
from aats.schemas.strategy_runtime import StrategyExecutionBundle, StrategyLegIntent


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

    async def test_protective_overlay_mainline_chain_persists_bundle_and_executes_leg_orders(self) -> None:
        runtime = await build_runtime(self._derivatives_overlay_settings(mode="protective"))
        self._seed_overlay_cycle_inputs(
            runtime,
            snapshot=self._portfolio_snapshot(
                symbol=runtime.settings.default_symbol,
                positions=[
                    Position(
                        symbol=runtime.settings.default_symbol,
                        position_key=f"{runtime.settings.default_symbol}:long",
                        position_qty=Decimal("0.05"),
                        position_notional=Decimal("4000"),
                        avg_entry_price=Decimal("80000"),
                        unrealized_pnl=Decimal("0"),
                        product_type="derivatives",
                        exposure_side="long",
                        target_leverage=2.0,
                        margin_mode="cross",
                        position_mode="long_short_mode",
                        pos_side="long",
                    )
                ],
            ),
        )

        with patch.object(
            runtime.decision_engine.target_engine,
            "build",
            side_effect=lambda context, *_args, **_kwargs: self._overlay_target(context, mode="protective"),
        ):
            target = await runtime.decision_engine.run_cycle(
                runtime.settings.default_symbol,
                runtime.settings.primary_timeframe,
            )

        self._assert_overlay_execution_chain(
            runtime=runtime,
            target=target,
            expected_leg_modes={"protective_overlay"},
            expected_leg_count=1,
        )

    async def test_opportunistic_overlay_mainline_chain_persists_bundle_and_executes_leg_orders(self) -> None:
        runtime = await build_runtime(self._derivatives_overlay_settings(mode="opportunistic"))
        self._seed_overlay_cycle_inputs(
            runtime,
            snapshot=self._portfolio_snapshot(
                symbol=runtime.settings.default_symbol,
                positions=[
                    Position(
                        symbol=runtime.settings.default_symbol,
                        position_key=f"{runtime.settings.default_symbol}:long",
                        position_qty=Decimal("0.05"),
                        position_notional=Decimal("4000"),
                        avg_entry_price=Decimal("80000"),
                        unrealized_pnl=Decimal("0"),
                        product_type="derivatives",
                        exposure_side="long",
                        target_leverage=2.0,
                        margin_mode="cross",
                        position_mode="long_short_mode",
                        pos_side="long",
                    )
                ],
            ),
        )

        with patch.object(
            runtime.decision_engine.target_engine,
            "build",
            side_effect=lambda context, *_args, **_kwargs: self._overlay_target(context, mode="opportunistic"),
        ):
            target = await runtime.decision_engine.run_cycle(
                runtime.settings.default_symbol,
                runtime.settings.primary_timeframe,
            )

        self._assert_overlay_execution_chain(
            runtime=runtime,
            target=target,
            expected_leg_modes={"opportunistic_overlay"},
            expected_leg_count=1,
        )

    async def test_directional_overlay_cycle_preserves_execution_metadata_after_coordinator_selection(self) -> None:
        runtime = await build_runtime(self._derivatives_overlay_settings(mode="protective"))
        self._seed_overlay_cycle_inputs(
            runtime,
            snapshot=self._portfolio_snapshot(
                symbol=runtime.settings.default_symbol,
                positions=[
                    Position(
                        symbol=runtime.settings.default_symbol,
                        position_key=f"{runtime.settings.default_symbol}:long",
                        position_qty=Decimal("0.05"),
                        position_notional=Decimal("4000"),
                        avg_entry_price=Decimal("80000"),
                        unrealized_pnl=Decimal("0"),
                        product_type="derivatives",
                        exposure_side="long",
                        target_leverage=2.0,
                        margin_mode="cross",
                        position_mode="long_short_mode",
                        pos_side="long",
                    )
                ],
            ),
        )

        with patch.object(
            runtime.decision_engine.target_engine,
            "build",
            side_effect=lambda context, *_args, **_kwargs: self._overlay_target(context, mode="protective"),
        ):
            target = await runtime.decision_engine.run_cycle(
                runtime.settings.default_symbol,
                runtime.settings.primary_timeframe,
            )

        self.assertEqual(target.strategy_family, "directional")
        self.assertEqual(target.strategy_execution_mode, "protective_overlay")
        self.assertEqual(target.strategy_state_phase, "active")
        self.assertIn(
            "protective_overlay_signal_above_open_threshold",
            target.strategy_reason_codes,
        )
        self.assertEqual(len(target.strategy_execution_legs), 1)
        self.assertEqual(target.strategy_execution_legs[0].execution_mode, "protective_overlay")

    async def test_independent_overlay_mainline_chain_persists_bundle_and_executes_leg_orders(self) -> None:
        runtime = await build_runtime(self._derivatives_overlay_settings(mode="independent"))
        self._seed_overlay_cycle_inputs(
            runtime,
            snapshot=self._portfolio_snapshot(symbol=runtime.settings.default_symbol, positions=[]),
        )

        with (
            patch.object(
                runtime.decision_engine.target_engine,
                "build",
                side_effect=lambda context, *_args, **_kwargs: self._overlay_target(context, mode="independent"),
            ),
            patch.object(
                runtime.risk_engine,
                "evaluate_leg_order",
                side_effect=lambda leg_intent: self._approved_leg_risk_decision(
                    decision_id=leg_intent.decision_id,
                    projected_qty=Decimal("0.02") if leg_intent.pos_side == "long" else Decimal("-0.01"),
                ),
            ),
            patch.object(
                runtime.order_manager,
                "leg_risk_evaluator",
                new=lambda leg_intent: self._approved_leg_risk_decision(
                    decision_id=leg_intent.decision_id,
                    projected_qty=Decimal("0.02") if leg_intent.pos_side == "long" else Decimal("-0.01"),
                ),
            ),
            patch.object(
                runtime.risk_engine,
                "evaluate_leg_order_bundle",
                return_value=self._bundle_risk_decision(
                    decision_id="bundle_independent_ok",
                    approved=True,
                    capped_target_qty=Decimal("0.01"),
                    current_exposure=DerivativesExposureMetrics(),
                    projected_exposure=DerivativesExposureMetrics(
                        long_position_qty=Decimal("0.02"),
                        short_position_qty=Decimal("0.01"),
                        net_position_qty=Decimal("0.01"),
                        gross_position_qty=Decimal("0.03"),
                        long_notional=Decimal("1600"),
                        short_notional=Decimal("800"),
                        net_notional=Decimal("800"),
                        gross_notional=Decimal("2400"),
                        net_exposure_side="long",
                    ),
                ),
            ),
        ):
            target = await runtime.decision_engine.run_cycle(
                runtime.settings.default_symbol,
                runtime.settings.primary_timeframe,
            )

        self._assert_overlay_execution_chain(
            runtime=runtime,
            target=target,
            expected_leg_modes={"independent_long_book", "independent_short_book"},
            expected_leg_count=2,
        )

    async def test_independent_overlay_mainline_chain_allows_partial_leg_execution(self) -> None:
        runtime = await build_runtime(self._derivatives_overlay_settings(mode="independent"))
        self._seed_overlay_cycle_inputs(
            runtime,
            snapshot=self._portfolio_snapshot(symbol=runtime.settings.default_symbol, positions=[]),
        )
        def _leg_risk_with_blocked_short(leg_intent):
            decision = self._approved_leg_risk_decision(
                decision_id=leg_intent.decision_id,
                projected_qty=Decimal("0.02") if leg_intent.pos_side == "long" else Decimal("-0.01"),
            )
            if leg_intent.pos_side == "short":
                return decision.model_copy(
                    update={
                        "approved": False,
                        "modified": True,
                        "risk_limit_breached": True,
                        "rejection_reasons": ["independent_short_book_cooldown_active"],
                        "constraints_applied": ["independent_short_book_cooldown_active"],
                    }
                )
            return decision

        with (
            patch.object(
                runtime.decision_engine.target_engine,
                "build",
                side_effect=lambda context, *_args, **_kwargs: self._overlay_target(context, mode="independent"),
            ),
            patch.object(
                runtime.risk_engine,
                "evaluate_leg_order",
                side_effect=_leg_risk_with_blocked_short,
            ),
            patch.object(
                runtime.order_manager,
                "leg_risk_evaluator",
                new=_leg_risk_with_blocked_short,
            ),
            patch.object(
                runtime.risk_engine,
                "evaluate_leg_order_bundle",
                return_value=self._bundle_risk_decision(
                    decision_id="bundle_independent_partial",
                    approved=True,
                    capped_target_qty=Decimal("0.02"),
                    current_exposure=DerivativesExposureMetrics(),
                    projected_exposure=DerivativesExposureMetrics(
                        long_position_qty=Decimal("0.02"),
                        short_position_qty=Decimal("0"),
                        net_position_qty=Decimal("0.02"),
                        gross_position_qty=Decimal("0.02"),
                        long_notional=Decimal("1600"),
                        short_notional=Decimal("0"),
                        net_notional=Decimal("1600"),
                        gross_notional=Decimal("1600"),
                        net_exposure_side="long",
                    ),
                ),
            ),
        ):
            target = await runtime.decision_engine.run_cycle(
                runtime.settings.default_symbol,
                runtime.settings.primary_timeframe,
            )

        self._assert_overlay_execution_chain(
            runtime=runtime,
            target=target,
            expected_leg_modes={"independent_long_book", "independent_short_book"},
            expected_leg_count=2,
            expected_order_intent_count=1,
            expected_submitted_leg_modes={"independent_long_book"},
        )
        record = runtime.audit_repo.get(target.decision_id)
        self.assertIsNotNone(record)
        assert record is not None
        bundle_event = {
            event.event_id: event for event in runtime.event_store.all()
        }[record.strategy_execution_bundle_ref]
        bundle = StrategyExecutionBundle.model_validate(bundle_event.payload)
        self.assertIn("strategy_bundle_partial_leg_execution", bundle.reason_codes)
        blocked_short_legs = [
            leg
            for leg in bundle.legs
            if str(leg.execution_mode) == "independent_short_book"
        ]
        self.assertEqual(len(blocked_short_legs), 1)
        self.assertEqual(blocked_short_legs[0].policy_rejection_reasons, [])
        self.assertIn("independent_short_book_cooldown_active", blocked_short_legs[0].risk_rejection_reasons)
        self.assertIsNone(blocked_short_legs[0].order_intent_ref)

    async def test_independent_overlay_mainline_chain_keeps_safe_subset_when_combined_gross_risk_fails(self) -> None:
        runtime = await build_runtime(self._derivatives_overlay_settings(mode="independent"))
        self._seed_overlay_cycle_inputs(
            runtime,
            snapshot=self._portfolio_snapshot(symbol=runtime.settings.default_symbol, positions=[]),
        )

        with (
            patch.object(
                runtime.decision_engine.target_engine,
                "build",
                side_effect=lambda context, *_args, **_kwargs: self._overlay_target(context, mode="independent"),
            ),
            patch.object(
                runtime.risk_engine,
                "evaluate_leg_order",
                side_effect=lambda leg_intent: self._approved_leg_risk_decision(
                    decision_id=leg_intent.decision_id,
                    projected_qty=Decimal("0.02") if leg_intent.pos_side == "long" else Decimal("-0.01"),
                ),
            ),
            patch.object(
                runtime.order_manager,
                "leg_risk_evaluator",
                new=lambda leg_intent: self._approved_leg_risk_decision(
                    decision_id=leg_intent.decision_id,
                    projected_qty=Decimal("0.02") if leg_intent.pos_side == "long" else Decimal("-0.01"),
                ),
            ),
            patch.object(
                runtime.risk_engine,
                "evaluate_leg_order_bundle",
                side_effect=[
                    self._bundle_risk_decision(
                        decision_id="bundle_independent_gross_blocked",
                        approved=False,
                        capped_target_qty=Decimal("0.01"),
                        rejection_reasons=["risk_max_gross_notional_exceeded"],
                        current_exposure=DerivativesExposureMetrics(),
                        projected_exposure=DerivativesExposureMetrics(
                            long_position_qty=Decimal("0.02"),
                            short_position_qty=Decimal("0.01"),
                            net_position_qty=Decimal("0.01"),
                            gross_position_qty=Decimal("0.03"),
                            long_notional=Decimal("1600"),
                            short_notional=Decimal("800"),
                            net_notional=Decimal("800"),
                            gross_notional=Decimal("2400"),
                            net_exposure_side="long",
                        ),
                    ),
                    self._bundle_risk_decision(
                        decision_id="bundle_independent_long_only",
                        approved=True,
                        capped_target_qty=Decimal("0.02"),
                        current_exposure=DerivativesExposureMetrics(),
                        projected_exposure=DerivativesExposureMetrics(
                            long_position_qty=Decimal("0.02"),
                            short_position_qty=Decimal("0"),
                            net_position_qty=Decimal("0.02"),
                            gross_position_qty=Decimal("0.02"),
                            long_notional=Decimal("1600"),
                            short_notional=Decimal("0"),
                            net_notional=Decimal("1600"),
                            gross_notional=Decimal("1600"),
                            net_exposure_side="long",
                        ),
                    ),
                    self._bundle_risk_decision(
                        decision_id="bundle_independent_short_after_long_blocked",
                        approved=False,
                        capped_target_qty=Decimal("0.01"),
                        rejection_reasons=["risk_max_gross_notional_exceeded"],
                        current_exposure=DerivativesExposureMetrics(),
                        projected_exposure=DerivativesExposureMetrics(
                            long_position_qty=Decimal("0.02"),
                            short_position_qty=Decimal("0.01"),
                            net_position_qty=Decimal("0.01"),
                            gross_position_qty=Decimal("0.03"),
                            long_notional=Decimal("1600"),
                            short_notional=Decimal("800"),
                            net_notional=Decimal("800"),
                            gross_notional=Decimal("2400"),
                            net_exposure_side="long",
                        ),
                    ),
                ],
            ),
        ):
            target = await runtime.decision_engine.run_cycle(
                runtime.settings.default_symbol,
                runtime.settings.primary_timeframe,
            )

        self._assert_overlay_execution_chain(
            runtime=runtime,
            target=target,
            expected_leg_modes={"independent_long_book", "independent_short_book"},
            expected_leg_count=2,
            expected_order_intent_count=1,
            expected_submitted_leg_modes={"independent_long_book"},
        )
        record = runtime.audit_repo.get(target.decision_id)
        self.assertIsNotNone(record)
        assert record is not None
        bundle_event = {
            event.event_id: event for event in runtime.event_store.all()
        }[record.strategy_execution_bundle_ref]
        bundle = StrategyExecutionBundle.model_validate(bundle_event.payload)
        self.assertEqual(bundle.status, "submitted")
        self.assertIn("risk_max_gross_notional_exceeded", bundle.reason_codes)
        self.assertIn("strategy_bundle_partial_leg_execution", bundle.reason_codes)
        blocked_short_legs = [
            leg
            for leg in bundle.legs
            if str(leg.execution_mode) == "independent_short_book"
        ]
        self.assertEqual(len(blocked_short_legs), 1)
        self.assertIn("risk_max_gross_notional_exceeded", blocked_short_legs[0].risk_rejection_reasons)
        self.assertIsNone(blocked_short_legs[0].order_intent_ref)

    async def test_independent_overlay_mainline_chain_preserves_scale_in_long_intent_for_same_side_expansion(self) -> None:
        runtime = await build_runtime(self._derivatives_overlay_settings(mode="independent"))
        self._seed_overlay_cycle_inputs(
            runtime,
            snapshot=self._portfolio_snapshot(
                symbol=runtime.settings.default_symbol,
                positions=[
                    Position(
                        symbol=runtime.settings.default_symbol,
                        position_qty=Decimal("0.01"),
                        position_notional=Decimal("800"),
                        avg_entry_price=Decimal("80000"),
                        unrealized_pnl=Decimal("0"),
                        product_type="derivatives",
                        exposure_side="long",
                        target_leverage=2.0,
                        margin_mode="cross",
                        position_mode="long_short_mode",
                        pos_side="long",
                        instrument_family="BTC-USDT",
                        settle_currency="USDT",
                    )
                ],
            ),
        )

        def _scale_in_target(context, *_args, **_kwargs):
            target = self._overlay_target(context, mode="independent")
            scaled_long_leg = target.strategy_execution_legs[0].model_copy(
                update={
                    "current_position_qty": Decimal("0.01"),
                    "target_position_qty": Decimal("0.02"),
                    "delta_position_qty": Decimal("0.01"),
                }
            )
            return target.model_copy(
                update={
                    "current_position_qty": Decimal("0.01"),
                    "target_position_qty": Decimal("0.02"),
                    "delta_position_qty": Decimal("0.01"),
                    "position_intent": "scale_in_long",
                    "strategy_execution_legs": [scaled_long_leg, target.strategy_execution_legs[1]],
                }
            )

        with (
            patch.object(
                runtime.decision_engine.target_engine,
                "build",
                side_effect=_scale_in_target,
            ),
            patch.object(
                runtime.risk_engine,
                "evaluate_leg_order",
                side_effect=lambda leg_intent: self._approved_leg_risk_decision(
                    decision_id=leg_intent.decision_id,
                    projected_qty=Decimal("0.02") if leg_intent.pos_side == "long" else Decimal("-0.01"),
                ),
            ),
            patch.object(
                runtime.order_manager,
                "leg_risk_evaluator",
                new=lambda leg_intent: self._approved_leg_risk_decision(
                    decision_id=leg_intent.decision_id,
                    projected_qty=Decimal("0.02") if leg_intent.pos_side == "long" else Decimal("-0.01"),
                ),
            ),
            patch.object(
                runtime.risk_engine,
                "evaluate_leg_order_bundle",
                return_value=self._bundle_risk_decision(
                    decision_id="bundle_independent_scale_in",
                    approved=True,
                    capped_target_qty=Decimal("0.02"),
                    current_exposure=DerivativesExposureMetrics(
                        long_position_qty=Decimal("0.01"),
                        short_position_qty=Decimal("0"),
                        net_position_qty=Decimal("0.01"),
                        gross_position_qty=Decimal("0.01"),
                        long_notional=Decimal("800"),
                        short_notional=Decimal("0"),
                        net_notional=Decimal("800"),
                        gross_notional=Decimal("800"),
                        net_exposure_side="long",
                    ),
                    projected_exposure=DerivativesExposureMetrics(
                        long_position_qty=Decimal("0.02"),
                        short_position_qty=Decimal("0.01"),
                        net_position_qty=Decimal("0.01"),
                        gross_position_qty=Decimal("0.03"),
                        long_notional=Decimal("1600"),
                        short_notional=Decimal("800"),
                        net_notional=Decimal("800"),
                        gross_notional=Decimal("2400"),
                        net_exposure_side="long",
                    ),
                ),
            ),
        ):
            target = await runtime.decision_engine.run_cycle(
                runtime.settings.default_symbol,
                runtime.settings.primary_timeframe,
            )

        self._assert_overlay_execution_chain(
            runtime=runtime,
            target=target,
            expected_leg_modes={"independent_long_book", "independent_short_book"},
            expected_leg_count=2,
        )
        record = runtime.audit_repo.get(target.decision_id)
        assert record is not None
        events_by_id = {event.event_id: event for event in runtime.event_store.all()}
        order_intents = [
            events_by_id[ref].payload
            for ref in record.order_intent_refs
            if ref in events_by_id
        ]
        self.assertTrue(
            any(
                str(item.get("strategy_execution_mode") or "") == "independent_long_book"
                and str(item.get("position_intent") or "") == "scale_in_long"
                for item in order_intents
            )
        )

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
            self.assertTrue(record.portfolio_delta_refs)
            self.assertIn(record.portfolio_delta_ref, record.portfolio_delta_refs)
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
                self.assertIn(report.portfolio_snapshot_ref, record.portfolio_delta_refs)

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

    @staticmethod
    def _derivatives_overlay_settings(*, mode: str) -> AATSSettings:
        payload = {
            "mode": "guarded_live",
            "market_data_backend": "demo",
            "execution_backend": "paper",
            "account_backend": "disabled",
            "account_read_enabled": False,
            "storage_mode": "memory",
            "event_persistence_mode": "strict",
            "enabled_decision_timeframes": ("15m",),
            "live_submit_enabled": True,
            "guarded_execution_dry_run": False,
            "trading_product_type": "derivatives",
            "margin_mode": "cross",
            "default_symbol": "BTC-USDT-SWAP",
            "allowed_symbols": ("BTC-USDT-SWAP",),
            "derivatives_position_mode": "hedge",
            "strategy_short_bias_enabled": True,
            "strategy_hedge_overlay_enabled": True,
            "strategy_hedge_overlay_mode": mode,
            "strategy_hedge_opportunistic_enabled": mode == "opportunistic",
            "strategy_hedge_opportunistic_rollout_stage": "live",
            "strategy_hedge_independent_enabled": mode == "independent",
            "strategy_hedge_independent_rollout_stage": "live",
            "strategy_cost_guard_enabled": False,
            "strategy_entry_min_signal_edge_bps": 0.0,
            "strategy_entry_alpha_min": 0.0,
            "strategy_entry_confidence_min": 0.0,
            "strategy_short_entry_min_signal_edge_bps": 0.0,
            "strategy_short_entry_alpha_min": 0.0,
            "strategy_short_entry_confidence_min": 0.0,
            "max_abs_position_qty": 1.0,
            "default_order_qty": 0.01,
            "max_notional_per_symbol": 100_000.0,
            "max_pending_notional_per_symbol": 5_000.0,
            "max_target_leverage": 2.0,
        }
        return AATSSettings.model_validate(payload)

    def _seed_overlay_cycle_inputs(self, runtime, *, snapshot: PortfolioSnapshot) -> None:
        runtime.portfolio_repo.save_snapshot(snapshot)
        runtime.portfolio_service.state.load_portfolio_snapshot(snapshot)
        market_snapshot = MarketSnapshot(
            symbol=runtime.settings.default_symbol,
            exchange="OKX",
            snapshot_ts=datetime.now(timezone.utc),
            best_bid=80_000.0,
            best_ask=80_001.0,
            last_price=80_000.5,
            bid_size=1.0,
            ask_size=1.0,
            volume_24h=10_000_000.0,
            kline_15m={"open": 79_900.0, "high": 80_100.0, "low": 79_800.0, "close": 80_000.5},
            kline_1h={"open": 79_700.0, "high": 80_200.0, "low": 79_600.0, "close": 80_000.5},
        )
        runtime.market_gateway._latest_snapshots[runtime.settings.default_symbol] = market_snapshot
        runtime.event_store.append(
            build_envelope(
                topic=topics.MARKET_SNAPSHOTS,
                key=runtime.settings.default_symbol,
                payload_model=market_snapshot,
                source_component="test",
            )
        )
        runtime.event_store.append(
            build_envelope(
                topic=topics.FEATURE_SNAPSHOTS,
                key=runtime.settings.default_symbol,
                payload_model=FeatureSnapshot(
                    symbol=runtime.settings.default_symbol,
                    snapshot_ts=datetime.now(timezone.utc),
                    market_snapshot_ref="evt_market_overlay_mainline",
                    trend_strength=0.7,
                    volatility_state="medium",
                    volatility_value=0.18,
                    momentum_score=10.0,
                    liquidity_score=0.9,
                    regime_indicator="trend",
                    regime_confidence=0.8,
                    multi_timeframe_alignment=0.72,
                    composite_alpha_score=0.34,
                    suggested_position_scale=1.0,
                    volatility_target_scale=1.0,
                    feature_version="test",
                ),
                source_component="test",
            )
        )

    @staticmethod
    def _portfolio_snapshot(*, symbol: str, positions: list[Position]) -> PortfolioSnapshot:
        return PortfolioSnapshot(
            snapshot_ts=datetime.now(timezone.utc),
            balances={"USDT": Decimal("75000")},
            positions=positions,
            cost_basis={symbol: Decimal("4000")} if positions else {},
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            total_equity=Decimal("75000"),
            gross_exposure=sum((abs(position.position_notional) for position in positions), start=Decimal("0")),
            net_exposure=sum((position.position_notional for position in positions), start=Decimal("0")),
            risk_budget_usage={},
            product_type="derivatives",
            margin_mode="cross",
        )

    def _overlay_target(self, context, *, mode: str) -> PositionTarget:
        symbol = context.symbol
        decision_id = context.decision_id
        if mode == "independent":
            return PositionTarget(
                decision_id=decision_id,
                symbol=symbol,
                current_position_qty=Decimal("0"),
                target_position_qty=Decimal("0.01"),
                delta_position_qty=Decimal("0.01"),
                current_notional=Decimal("0"),
                target_notional=Decimal("800"),
                rebalance_reason="independent_overlay_mainline_test",
                urgency="medium",
                max_slippage_tolerance_bps=5_000,
                source_mix={"directional": 1.0},
                decision_expiry_ts=datetime.now(timezone.utc) + timedelta(minutes=5),
                product_type="derivatives",
                current_exposure_side="flat",
                target_exposure_side="long",
                position_intent="open_long",
                target_leverage=2.0,
                margin_mode="cross",
                strategy_family="directional",
                strategy_sleeve_id="sleeve_directional_primary",
                strategy_route_action="override_target",
                strategy_execution_mode="independent_books",
                strategy_state_phase="active",
                strategy_reason_codes=["independent_books_active"],
                strategy_headline="独立双书主链测试。",
                allocation_id=f"alloc_{decision_id}",
                strategy_bundle_id=f"bundle_{decision_id}",
                strategy_execution_legs=[
                    StrategyLegIntent(
                        symbol=symbol,
                        product_type="derivatives",
                        side="buy",
                        position_mode="long_short_mode",
                        pos_side="long",
                        action="open",
                        family="directional",
                        role="primary",
                        strategy_sleeve_id="sleeve_independent_long",
                        allocation_id=f"alloc_{decision_id}",
                        margin_mode="cross",
                        target_leverage=2.0,
                        current_position_qty=Decimal("0"),
                        target_position_qty=Decimal("0.02"),
                        delta_position_qty=Decimal("0.02"),
                        reference_price=Decimal("80000"),
                        execution_compatible=True,
                        execution_mode="independent_long_book",
                        state_phase="active",
                        overlay_mode="independent",
                        trigger_reason_codes=["independent_long_book_signal_above_entry_threshold"],
                    ),
                    StrategyLegIntent(
                        symbol=symbol,
                        product_type="derivatives",
                        side="sell",
                        position_mode="long_short_mode",
                        pos_side="short",
                        action="open",
                        family="directional",
                        role="primary",
                        strategy_sleeve_id="sleeve_independent_short",
                        allocation_id=f"alloc_{decision_id}",
                        margin_mode="cross",
                        target_leverage=2.0,
                        current_position_qty=Decimal("0"),
                        target_position_qty=Decimal("-0.01"),
                        delta_position_qty=Decimal("-0.01"),
                        reference_price=Decimal("80000"),
                        execution_compatible=True,
                        execution_mode="independent_short_book",
                        state_phase="active",
                        overlay_mode="independent",
                        trigger_reason_codes=["independent_short_book_signal_above_entry_threshold"],
                    ),
                ],
                hedge_overlay_decision=HedgeOverlayDecision(
                    enabled=True,
                    runtime_supported=True,
                    configured_mode="independent",
                    effective_mode="independent",
                    overlay_source="independent_books",
                    active=True,
                    state="opening",
                    main_leg_signal="long",
                    hedge_leg_signal="short",
                    long_leg_score=0.76,
                    short_leg_score=0.69,
                    long_leg_reason_codes=["independent_long_book_signal_above_entry_threshold"],
                    short_leg_reason_codes=["independent_short_book_signal_above_entry_threshold"],
                    reason_codes=[
                        "independent_long_book_signal_above_entry_threshold",
                        "independent_short_book_signal_above_entry_threshold",
                    ],
                    rollout_stage="live",
                    runtime_rollout_stage="live",
                ),
            )
        execution_mode = "protective_overlay" if mode == "protective" else "opportunistic_overlay"
        overlay_source = "protective" if mode == "protective" else "opportunistic"
        reason_code = (
            "protective_overlay_signal_above_open_threshold"
            if mode == "protective"
            else "opportunistic_overlay_signal_above_open_threshold"
        )
        return PositionTarget(
            decision_id=decision_id,
            symbol=symbol,
            current_position_qty=Decimal("0.05"),
            target_position_qty=Decimal("0.05"),
            delta_position_qty=Decimal("0"),
            current_notional=Decimal("4000"),
            target_notional=Decimal("4000"),
            rebalance_reason=f"{mode}_overlay_mainline_test",
            urgency="medium",
            max_slippage_tolerance_bps=5_000,
            source_mix={"directional": 1.0},
            decision_expiry_ts=datetime.now(timezone.utc) + timedelta(minutes=5),
            product_type="derivatives",
            current_exposure_side="long",
            target_exposure_side="long",
            position_intent="hold",
            target_leverage=2.0,
            margin_mode="cross",
            strategy_family="directional",
            strategy_sleeve_id="sleeve_directional_primary",
            strategy_route_action="override_target",
            strategy_execution_mode=execution_mode,
            strategy_state_phase="active",
            strategy_reason_codes=[reason_code],
            strategy_headline=f"{mode} overlay 主链测试。",
            allocation_id=f"alloc_{decision_id}",
            strategy_bundle_id=f"bundle_{decision_id}",
            strategy_execution_legs=[
                StrategyLegIntent(
                    symbol=symbol,
                    product_type="derivatives",
                    side="sell",
                    position_mode="long_short_mode",
                    pos_side="short",
                    action="open",
                    family="directional",
                    role="hedge",
                    strategy_sleeve_id=f"sleeve_{mode}_short",
                    allocation_id=f"alloc_{decision_id}",
                    margin_mode="cross",
                    target_leverage=2.0,
                    current_position_qty=Decimal("0"),
                    target_position_qty=Decimal("-0.02"),
                    delta_position_qty=Decimal("-0.02"),
                    reference_price=Decimal("80000"),
                    execution_compatible=True,
                    execution_mode=execution_mode,
                    state_phase="active",
                    overlay_mode=mode,
                    trigger_reason_codes=[reason_code],
                )
            ],
            hedge_overlay_decision=HedgeOverlayDecision(
                enabled=True,
                runtime_supported=True,
                configured_mode=mode,
                effective_mode=mode,
                overlay_source=overlay_source,
                active=True,
                state="opening",
                main_leg_signal="long",
                hedge_leg_signal="short",
                main_leg_current_qty=Decimal("0.05"),
                main_leg_target_qty=Decimal("0.05"),
                hedge_leg_current_qty=Decimal("0"),
                hedge_leg_target_qty=Decimal("-0.02"),
                hedge_ratio=Decimal("0.40"),
                max_ratio=Decimal("0.50"),
                pressure_score=0.84,
                open_threshold=0.58 if mode == "protective" else 0.62,
                close_threshold=0.42 if mode == "protective" else 0.46,
                open_condition="overlay_signal_above_open_threshold",
                reason_codes=[reason_code],
                rollout_stage="live",
                runtime_rollout_stage="live",
            ),
        )

    def _assert_overlay_execution_chain(
        self,
        *,
        runtime,
        target: PositionTarget,
        expected_leg_modes: set[str],
        expected_leg_count: int,
        expected_order_intent_count: int | None = None,
        expected_submitted_leg_modes: set[str] | None = None,
    ) -> None:
        record = runtime.audit_repo.get(target.decision_id)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertIsNotNone(record.strategy_execution_bundle_ref)
        expected_order_intent_count = (
            expected_leg_count if expected_order_intent_count is None else expected_order_intent_count
        )
        expected_submitted_leg_modes = (
            expected_leg_modes if expected_submitted_leg_modes is None else expected_submitted_leg_modes
        )
        self.assertEqual(len(record.order_intent_refs), expected_order_intent_count)

        events_by_id = {event.event_id: event for event in runtime.event_store.all()}
        order_states = [
            events_by_id[ref].payload
            for ref in record.order_state_refs
            if ref in events_by_id
        ]
        self.assertEqual(
            len(
                {
                    str(item.get("client_order_id") or "")
                    for item in order_states
                    if str(item.get("client_order_id") or "")
                }
            ),
            expected_order_intent_count,
        )
        bundle_event = events_by_id.get(record.strategy_execution_bundle_ref)
        self.assertIsNotNone(bundle_event)
        bundle = StrategyExecutionBundle.model_validate(bundle_event.payload)
        self.assertEqual(bundle.decision_id, target.decision_id)
        self.assertEqual(bundle.status, "submitted")
        self.assertEqual(len(bundle.legs), expected_leg_count)
        self.assertEqual({str(leg.execution_mode) for leg in bundle.legs}, expected_leg_modes)
        self.assertEqual(len(bundle.order_intent_refs), expected_order_intent_count)

        runtime_bundle = runtime.strategy_runtime_repo.get_execution_bundle(bundle.bundle_id)
        self.assertIsNotNone(runtime_bundle)
        assert runtime_bundle is not None
        self.assertEqual(runtime_bundle.status, "submitted")
        self.assertEqual(
            {str(leg.execution_mode) for leg in runtime_bundle.legs},
            expected_leg_modes,
        )
        persisted_order_states = [
            state
            for state in runtime.execution_repo.order_states()
            if str(state.strategy_bundle_id or "") == bundle.bundle_id
        ]
        self.assertEqual(
            {str(state.strategy_execution_mode) for state in persisted_order_states},
            expected_submitted_leg_modes,
        )
        self.assertEqual(
            len({state.client_order_id for state in persisted_order_states}),
            expected_order_intent_count,
        )

    @staticmethod
    def _approved_leg_risk_decision(*, decision_id: str, projected_qty: Decimal) -> RiskDecision:
        return RiskDecision(
            decision_id=decision_id,
            approved=True,
            modified=False,
            capped_target_position_qty=projected_qty,
            projected_notional=abs(projected_qty) * Decimal("80000"),
            current_open_order_count=0,
            risk_budget_multiplier=Decimal("1"),
            execution_aggressiveness_multiplier=Decimal("1"),
            risk_score=0.1,
            rejection_reasons=[],
            constraints_applied=[],
        )

    @staticmethod
    def _bundle_risk_decision(
        *,
        decision_id: str,
        approved: bool,
        capped_target_qty: Decimal,
        current_exposure: DerivativesExposureMetrics,
        projected_exposure: DerivativesExposureMetrics,
        rejection_reasons: list[str] | None = None,
    ) -> RiskDecision:
        return RiskDecision(
            decision_id=decision_id,
            approved=approved,
            modified=not approved,
            capped_target_position_qty=capped_target_qty,
            capped_target_notional=projected_exposure.net_notional,
            projected_notional=projected_exposure.gross_notional,
            current_open_order_count=0,
            risk_budget_multiplier=Decimal("1"),
            execution_aggressiveness_multiplier=Decimal("1"),
            risk_score=0.2,
            risk_limit_breached=not approved,
            current_derivatives_exposure=current_exposure,
            projected_derivatives_exposure=projected_exposure,
            derivatives_exposure_limits=DerivativesExposureLimits(
                risk_max_long_notional=Decimal("5000"),
                risk_max_short_notional=Decimal("5000"),
                risk_max_gross_notional=Decimal("5000"),
                risk_max_net_notional=Decimal("5000"),
            ),
            rejection_reasons=list(rejection_reasons or []),
        )


if __name__ == "__main__":
    unittest.main()
