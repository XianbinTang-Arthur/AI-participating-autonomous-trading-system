from __future__ import annotations

from datetime import timedelta
from datetime import timezone
from decimal import Decimal
from unittest import TestCase

from aats.bootstrap.settings import AATSSettings
from aats.schemas.audit import DecisionAuditRecord
from aats.schemas.execution import FillEvent
from aats.schemas.common import utc_now
from aats.services.strategy_execution_guard_filters import (
    guard_excluded_fill_ids_for_independent_residual_exits,
)
from aats.services.strategy_execution_health import (
    ClosedTradeOutcome,
    _strategy_health_snapshot_from_outcomes,
)


class TestStrategyExecutionHealth(TestCase):
    def _make_fill(
        self,
        *,
        fill_id: str = "fill-1",
        fill_qty: str = "0.001",
        fill_price: str = "100000",
        position_intent: str = "reduce_long",
        strategy_family: str = "independent",
        strategy_execution_mode: str = "independent_long_book",
        reduce_only: bool = True,
        close_only: bool = False,
        decision_id: str = "dec-test",
        execution_chain_id: str | None = "independent:dec-test:long:de_risk",
    ) -> FillEvent:
        ts = utc_now().astimezone(timezone.utc)
        return FillEvent(
            fill_id=fill_id,
            decision_id=decision_id,
            execution_chain_id=execution_chain_id,
            intent_id="intent-test",
            client_order_id=f"coid-{fill_id}",
            exchange_order_id=f"exo-{fill_id}",
            symbol="BTC-USDT-SWAP",
            venue="OKX",
            side="sell",
            fill_qty=Decimal(fill_qty),
            fill_price=Decimal(fill_price),
            fee_amount=Decimal("0.01"),
            reduce_only=reduce_only,
            close_only=close_only,
            strategy_family=strategy_family,
            strategy_execution_mode=strategy_execution_mode,
            position_intent=position_intent,
            execution_action="reduce",
            leg_action="reduce",
            liquidity_role="taker",
            exchange_timestamp=ts,
            ingestion_timestamp=ts,
            product_type="derivatives",
            margin_mode="isolated",
            pos_side="long",
        )

    def test_residual_exits_do_not_raise_guard_eligible_churn_ratio(self) -> None:
        now = utc_now()
        settings = AATSSettings.model_validate(
            {
                "strategy_performance_guard_min_closed_trades": 2,
                "strategy_max_churn_ratio": 0.42,
                "strategy_low_edge_streak_limit": 10,
                "strategy_low_edge_cooldown_seconds": 0,
            }
        )
        outcomes = [
            ClosedTradeOutcome(
                timestamp=now - timedelta(minutes=4),
                fill_id="guard_1",
                net_realized_pnl=Decimal("4"),
                gross_realized_pnl=Decimal("4.1"),
                fee_cost_quote=Decimal("0.1"),
                close_notional=Decimal("100"),
                net_edge_bps=Decimal("40"),
                is_win=True,
                is_small_churn=False,
                is_low_edge=False,
                is_residual_exit=False,
            ),
            ClosedTradeOutcome(
                timestamp=now - timedelta(minutes=3),
                fill_id="guard_2",
                net_realized_pnl=Decimal("3"),
                gross_realized_pnl=Decimal("3.1"),
                fee_cost_quote=Decimal("0.1"),
                close_notional=Decimal("100"),
                net_edge_bps=Decimal("30"),
                is_win=True,
                is_small_churn=False,
                is_low_edge=False,
                is_residual_exit=False,
            ),
            ClosedTradeOutcome(
                timestamp=now - timedelta(minutes=2),
                fill_id="residual_1",
                net_realized_pnl=Decimal("0.01"),
                gross_realized_pnl=Decimal("0.06"),
                fee_cost_quote=Decimal("0.05"),
                close_notional=Decimal("10"),
                net_edge_bps=Decimal("1"),
                is_win=True,
                is_small_churn=True,
                is_low_edge=True,
                is_residual_exit=True,
            ),
            ClosedTradeOutcome(
                timestamp=now - timedelta(minutes=1),
                fill_id="residual_2",
                net_realized_pnl=Decimal("-0.01"),
                gross_realized_pnl=Decimal("0.04"),
                fee_cost_quote=Decimal("0.05"),
                close_notional=Decimal("10"),
                net_edge_bps=Decimal("-1"),
                is_win=False,
                is_small_churn=True,
                is_low_edge=True,
                is_residual_exit=True,
            ),
        ]

        snapshot = _strategy_health_snapshot_from_outcomes(
            settings=settings,
            symbol="BTC-USDT-SWAP",
            current_position_opened_at=None,
            last_position_closed_at=now - timedelta(minutes=1),
            latest_fill_timestamp=now,
            outcomes=outcomes,
        )

        self.assertEqual(snapshot.recent_closed_trade_count, 4)
        self.assertAlmostEqual(snapshot.recent_churn_ratio, 0.5)
        self.assertGreater(snapshot.recent_fee_drag_ratio, 0.0)
        self.assertEqual(snapshot.recent_guard_eligible_closed_trade_count, 2)
        self.assertEqual(snapshot.recent_guard_eligible_net_realized_pnl, Decimal("7"))
        self.assertAlmostEqual(snapshot.recent_guard_eligible_fee_drag_ratio or 0.0, 0.027777777777777776)
        self.assertAlmostEqual(snapshot.recent_guard_eligible_churn_ratio or 0.0, 0.0)

        guardrails = snapshot.active_guardrails(
            settings=settings,
            as_of=now,
            current_position_qty=Decimal("0"),
        )
        self.assertNotIn("churn_elevated", guardrails["flags"])

    def test_residual_exits_do_not_raise_guard_eligible_fee_drag_ratio(self) -> None:
        now = utc_now()
        settings = AATSSettings.model_validate(
            {
                "strategy_performance_guard_min_closed_trades": 2,
                "strategy_max_fee_drag_ratio": 0.48,
                "strategy_max_churn_ratio": 0.42,
                "strategy_low_edge_streak_limit": 10,
                "strategy_low_edge_cooldown_seconds": 0,
            }
        )
        outcomes = [
            ClosedTradeOutcome(
                timestamp=now - timedelta(minutes=8),
                fill_id="guard_1",
                net_realized_pnl=Decimal("0.24"),
                gross_realized_pnl=Decimal("0.25"),
                fee_cost_quote=Decimal("0.01"),
                close_notional=Decimal("100"),
                net_edge_bps=Decimal("24"),
                is_win=True,
                is_small_churn=False,
                is_low_edge=False,
                is_residual_exit=False,
            ),
            ClosedTradeOutcome(
                timestamp=now - timedelta(minutes=7),
                fill_id="guard_2",
                net_realized_pnl=Decimal("0.24"),
                gross_realized_pnl=Decimal("0.25"),
                fee_cost_quote=Decimal("0.01"),
                close_notional=Decimal("100"),
                net_edge_bps=Decimal("24"),
                is_win=True,
                is_small_churn=False,
                is_low_edge=False,
                is_residual_exit=False,
            ),
        ]
        for idx in range(6):
            outcomes.append(
                ClosedTradeOutcome(
                    timestamp=now - timedelta(minutes=6 - idx),
                    fill_id=f"residual_{idx}",
                    net_realized_pnl=Decimal("-0.049"),
                    gross_realized_pnl=Decimal("0.001"),
                    fee_cost_quote=Decimal("0.05"),
                    close_notional=Decimal("5"),
                    net_edge_bps=Decimal("-98"),
                    is_win=False,
                    is_small_churn=True,
                    is_low_edge=True,
                    is_residual_exit=True,
                )
            )

        snapshot = _strategy_health_snapshot_from_outcomes(
            settings=settings,
            symbol="BTC-USDT-SWAP",
            current_position_opened_at=None,
            last_position_closed_at=now - timedelta(minutes=1),
            latest_fill_timestamp=now,
            outcomes=outcomes,
        )

        self.assertGreater(snapshot.recent_fee_drag_ratio, settings.strategy_max_fee_drag_ratio)
        self.assertLess(snapshot.recent_guard_eligible_fee_drag_ratio or 0.0, settings.strategy_max_fee_drag_ratio)

        guardrails = snapshot.active_guardrails(
            settings=settings,
            as_of=now,
            current_position_qty=Decimal("0"),
        )
        self.assertNotIn("fee_drag_elevated", guardrails["flags"])

    def test_residual_exits_do_not_trigger_guard_eligible_low_edge_cooldown(self) -> None:
        now = utc_now()
        settings = AATSSettings.model_validate(
            {
                "strategy_performance_guard_min_closed_trades": 2,
                "strategy_max_fee_drag_ratio": 0.48,
                "strategy_max_churn_ratio": 0.42,
                "strategy_low_edge_streak_limit": 2,
                "strategy_low_edge_cooldown_seconds": 3600,
            }
        )
        outcomes = [
            ClosedTradeOutcome(
                timestamp=now - timedelta(minutes=4),
                fill_id="guard_1",
                net_realized_pnl=Decimal("1"),
                gross_realized_pnl=Decimal("1.01"),
                fee_cost_quote=Decimal("0.01"),
                close_notional=Decimal("100"),
                net_edge_bps=Decimal("10"),
                is_win=True,
                is_small_churn=False,
                is_low_edge=False,
                is_residual_exit=False,
            ),
            ClosedTradeOutcome(
                timestamp=now - timedelta(minutes=3),
                fill_id="guard_2",
                net_realized_pnl=Decimal("1"),
                gross_realized_pnl=Decimal("1.01"),
                fee_cost_quote=Decimal("0.01"),
                close_notional=Decimal("100"),
                net_edge_bps=Decimal("10"),
                is_win=True,
                is_small_churn=False,
                is_low_edge=False,
                is_residual_exit=False,
            ),
            ClosedTradeOutcome(
                timestamp=now - timedelta(minutes=2),
                fill_id="residual_1",
                net_realized_pnl=Decimal("0.001"),
                gross_realized_pnl=Decimal("0.011"),
                fee_cost_quote=Decimal("0.01"),
                close_notional=Decimal("5"),
                net_edge_bps=Decimal("2"),
                is_win=True,
                is_small_churn=True,
                is_low_edge=True,
                is_residual_exit=True,
            ),
            ClosedTradeOutcome(
                timestamp=now - timedelta(minutes=1),
                fill_id="residual_2",
                net_realized_pnl=Decimal("0.001"),
                gross_realized_pnl=Decimal("0.011"),
                fee_cost_quote=Decimal("0.01"),
                close_notional=Decimal("5"),
                net_edge_bps=Decimal("2"),
                is_win=True,
                is_small_churn=True,
                is_low_edge=True,
                is_residual_exit=True,
            ),
        ]

        snapshot = _strategy_health_snapshot_from_outcomes(
            settings=settings,
            symbol="BTC-USDT-SWAP",
            current_position_opened_at=None,
            last_position_closed_at=now - timedelta(minutes=1),
            latest_fill_timestamp=now,
            outcomes=outcomes,
        )

        guardrails = snapshot.active_guardrails(
            settings=settings,
            as_of=now,
            current_position_qty=Decimal("0"),
        )
        self.assertNotIn("low_edge_cooldown_active", guardrails["flags"])

    def test_guard_excluded_fill_ids_require_explicit_floor_promoted_signal(self) -> None:
        fill = self._make_fill(
            fill_id="cleanup",
            decision_id="decision_cleanup",
            execution_chain_id="independent:decision_cleanup:long:de_risk",
        )
        payloads = {
            "target_cleanup": {
                "book_runtime_states": [
                    {
                        "leg": "long",
                        "current_qty": Decimal("0.3"),
                        "target_qty": Decimal("0"),
                        "book_action": "de_risk",
                        "execution_chain_id": "independent:decision_cleanup:long:de_risk",
                        "reason_codes": ["independent_long_book_de_risk_floor_promoted_to_close"],
                    }
                ]
            }
        }
        audits = [
            DecisionAuditRecord(
                decision_id="decision_cleanup",
                decision_context_ref="ctx_cleanup",
                position_target_ref="target_cleanup",
            )
        ]

        excluded = guard_excluded_fill_ids_for_independent_residual_exits(
            fills=[fill],
            audits=audits,
            payload_by_ref=lambda ref: payloads.get(ref),
        )

        self.assertEqual(excluded, {"cleanup"})

    def test_guard_excluded_fill_ids_support_chain_only_fills(self) -> None:
        fill = self._make_fill(
            fill_id="chain-only-cleanup",
            decision_id="",
            execution_chain_id="independent:decision_chain_only:long:de_risk",
        )
        payloads = {
            "target_chain_only": {
                "book_runtime_states": [
                    {
                        "leg": "long",
                        "current_qty": Decimal("0.25"),
                        "target_qty": Decimal("0"),
                        "book_action": "de_risk",
                        "execution_chain_id": "independent:decision_chain_only:long:de_risk",
                        "reason_codes": ["independent_long_book_de_risk_floor_promoted_to_close"],
                    }
                ]
            }
        }
        audits = [
            DecisionAuditRecord(
                decision_id="decision_chain_only",
                decision_context_ref="ctx_chain_only",
                position_target_ref="target_chain_only",
            )
        ]

        excluded = guard_excluded_fill_ids_for_independent_residual_exits(
            fills=[fill],
            audits=audits,
            payload_by_ref=lambda ref: payloads.get(ref),
        )

        self.assertEqual(excluded, {"chain-only-cleanup"})

    def test_later_zero_target_qty_overrides_stale_prefloor_target(self) -> None:
        fill = self._make_fill(
            fill_id="merged-cleanup",
            decision_id="decision_merge_cleanup",
            execution_chain_id="independent:decision_merge_cleanup:long:de_risk",
        )
        payloads = {
            "target_prefloor": {
                "book_runtime_states": [
                    {
                        "leg": "long",
                        "current_qty": Decimal("0.25"),
                        "target_qty": Decimal("0.10"),
                        "book_action": "de_risk",
                        "execution_chain_id": "independent:decision_merge_cleanup:long:de_risk",
                        "reason_codes": [],
                    }
                ]
            },
            "outcome_promoted": {
                "book_runtime_states": [
                    {
                        "leg": "long",
                        "current_qty": Decimal("0.25"),
                        "target_qty": Decimal("0"),
                        "book_action": "de_risk",
                        "execution_chain_id": "independent:decision_merge_cleanup:long:de_risk",
                        "reason_codes": ["independent_long_book_de_risk_floor_promoted_to_close"],
                    }
                ]
            },
        }
        audits = [
            DecisionAuditRecord(
                decision_id="decision_merge_cleanup",
                decision_context_ref="ctx_merge_cleanup",
                position_target_ref="target_prefloor",
                decision_outcome_ref="outcome_promoted",
            )
        ]

        excluded = guard_excluded_fill_ids_for_independent_residual_exits(
            fills=[fill],
            audits=audits,
            payload_by_ref=lambda ref: payloads.get(ref),
        )

        self.assertEqual(excluded, {"merged-cleanup"})

    def test_partial_reduce_that_leaves_small_residual_is_not_excluded_without_explicit_signal(self) -> None:
        fill = self._make_fill(
            fill_id="partial-reduce",
            decision_id="decision_partial_reduce",
            execution_chain_id="independent:decision_partial_reduce:long:de_risk",
        )
        payloads = {
            "target_partial": {
                "book_runtime_states": [
                    {
                        "leg": "long",
                        "current_qty": Decimal("0.5"),
                        "target_qty": Decimal("0.2"),
                        "book_action": "de_risk",
                        "execution_chain_id": "independent:decision_partial_reduce:long:de_risk",
                        "reason_codes": ["independent_long_book_hold_above_close_threshold"],
                    }
                ]
            }
        }
        audits = [
            DecisionAuditRecord(
                decision_id="decision_partial_reduce",
                decision_context_ref="ctx_partial",
                position_target_ref="target_partial",
            )
        ]

        excluded = guard_excluded_fill_ids_for_independent_residual_exits(
            fills=[fill],
            audits=audits,
            payload_by_ref=lambda ref: payloads.get(ref),
        )

        self.assertEqual(excluded, set())
