from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.decision import PositionTarget
from aats.schemas.execution import FillEvent
from aats.schemas.portfolio import PortfolioSnapshot
from aats.services.governance_engine.risk import RiskEngine
from aats.services.operator.strategy_profile_context import StrategyProfileContextFacade
from aats.services.operator.strategy_profiles import StrategyProfileControlService
from aats.services.strategy_execution_health import ClosedTradeOutcome, compute_strategy_execution_health


def _fill(*, fee_amount: str, fee_currency: str = "USDT") -> FillEvent:
    now = utc_now()
    return FillEvent(
        fill_id="fill_task107",
        decision_id="decision_task107",
        intent_id="intent_task107",
        client_order_id="clord_task107",
        exchange_order_id="ord_task107",
        symbol="BTC-USDT",
        venue="OKX",
        side="buy",
        fill_qty=Decimal("1"),
        fill_price=Decimal("100"),
        fee_amount=Decimal(fee_amount),
        fee_currency=fee_currency,
        product_type="spot",
        margin_mode="cash",
        liquidity_role="taker",
        exchange_timestamp=now,
        ingestion_timestamp=now,
        order_status_after_fill="FILLED",
    )


def _snapshot(*, realized_pnl: str) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        snapshot_ts=utc_now(),
        balances={"USDT": Decimal("1000")},
        positions=[],
        realized_pnl=Decimal(realized_pnl),
        unrealized_pnl=Decimal("0"),
        total_equity=Decimal("1000"),
        gross_exposure=Decimal("0"),
        net_exposure=Decimal("0"),
        product_type="spot",
        margin_mode="cash",
    )


class _HealthSnapshot:
    blockers: list[str] = []


class _HealthService:
    def snapshot(self) -> _HealthSnapshot:
        return _HealthSnapshot()

    def execution_blockers(self) -> list[str]:
        return []


class _GuardProvider:
    def snapshot(self) -> dict[str, object]:
        return {
            "status": "warning",
            "current_initial_margin_usage_fraction": Decimal("0.82"),
            "nearest_liquidation_gap_ratio": Decimal("0.20"),
        }


class _TrialGuardProvider:
    def snapshot(self) -> dict[str, object]:
        return {"status": "normal"}


class TestTask107BusinessFormulaAudit(TestCase):
    @patch(
        "aats.services.strategy_execution_health._walk_symbol_fills",
        return_value=(
            None,
            None,
            [
                ClosedTradeOutcome(
                    timestamp=utc_now(),
                    fill_id="fill_task107",
                    net_realized_pnl=Decimal("-10"),
                    gross_realized_pnl=Decimal("-8"),
                    fee_cost_quote=Decimal("2"),
                    close_notional=Decimal("100"),
                    net_edge_bps=Decimal("-1000"),
                    is_win=False,
                    is_small_churn=False,
                    is_low_edge=True,
                )
            ],
        ),
    )
    def test_strategy_execution_health_uses_absolute_gross_when_recent_realized_is_negative(self, _walk) -> None:
        snapshot = compute_strategy_execution_health(
            settings=AATSSettings.model_validate({}),
            symbol="BTC-USDT",
            fills=[],
            snapshots=[],
            current_position_qty=Decimal("0"),
        )

        self.assertEqual(snapshot.recent_fee_drag_ratio, 0.25)

    @patch("aats.services.operator.strategy_profile_context.fills_for_scope")
    @patch("aats.services.operator.strategy_profile_context.snapshots_for_scope")
    def test_strategy_profile_context_uses_absolute_gross_when_recent_realized_is_negative(
        self,
        snapshots_for_scope_mock,
        fills_for_scope_mock,
    ) -> None:
        fills_for_scope_mock.return_value = [_fill(fee_amount="2")]
        snapshots_for_scope_mock.return_value = [
            _snapshot(realized_pnl="0"),
            _snapshot(realized_pnl="-10"),
        ]
        owner = SimpleNamespace(
            runtime=SimpleNamespace(execution_repo=object(), portfolio_repo=object()),
            runtime_state_scope=object(),
            evaluation_window_limit=20,
        )

        summary = StrategyProfileContextFacade(owner).performance_summary()

        self.assertEqual(summary["gross_realized_pnl"], -8.0)
        self.assertEqual(summary["fee_to_gross_pnl_ratio"], 0.25)

    def test_strategy_profile_adaptive_summary_does_not_fabricate_projected_margin_usage(self) -> None:
        service = object.__new__(StrategyProfileControlService)
        service.settings = AATSSettings.model_validate({})
        summary = StrategyProfileControlService._adaptive_control_summary(
            service,
            context={
                "safety_state": {
                    "safe_to_trade": True,
                    "review_required": False,
                    "market_snapshot_fresh": True,
                    "account_snapshot_fresh": True,
                    "reconciliation_severity": "CLEAN",
                    "only_reduce_required": False,
                    "auto_halt_required": False,
                    "trial_guard_breached": False,
                    "live_guard": {
                        "status": "warning",
                        "current_initial_margin_usage_fraction": Decimal("0.82"),
                        "nearest_liquidation_gap_ratio": Decimal("0.20"),
                    },
                    "trial_guard": {"status": "normal"},
                },
                "execution_health": {"recent_execution_error_count": 0},
            },
        )

        self.assertEqual(summary["risk_budget"]["multiplier"], 0.5)
        self.assertEqual(summary["execution_aggressiveness"]["multiplier"], 0.4)
        self.assertIn("current_margin_usage_near_hard_cap", summary["risk_budget"]["reasons"])
        self.assertNotIn("projected_margin_usage_near_hard_cap", summary["risk_budget"]["reasons"])

    def test_risk_engine_adaptive_controls_do_not_double_penalize_current_margin_usage(self) -> None:
        engine = object.__new__(RiskEngine)
        engine.settings = AATSSettings.model_validate({})
        engine.health_service = _HealthService()
        engine.live_runtime_guard_provider = _GuardProvider()
        engine.trial_guard_provider = _TrialGuardProvider()
        engine.recovery_status_provider = lambda: {}
        target = PositionTarget(
            decision_id="decision_task107_margin",
            symbol="BTC-USDT-SWAP",
            current_position_qty=Decimal("0"),
            target_position_qty=Decimal("0.05"),
            delta_position_qty=Decimal("0.05"),
            current_notional=Decimal("0"),
            target_notional=Decimal("1500"),
            rebalance_reason="task107_margin_test",
            urgency="medium",
            max_slippage_tolerance_bps=20,
            source_mix={"baseline": 1.0},
            decision_expiry_ts=utc_now(),
            product_type="derivatives",
            margin_mode="cross",
            target_leverage=3.0,
        )

        summary = RiskEngine._adaptive_control_states(engine, target=target)

        self.assertEqual(summary["risk_budget"]["multiplier"], 0.5)
        self.assertEqual(summary["execution_aggressiveness"]["multiplier"], 0.4)
        self.assertIn("current_margin_usage_near_hard_cap", summary["risk_budget"]["reasons"])
        self.assertNotIn("projected_margin_usage_near_hard_cap", summary["risk_budget"]["reasons"])
