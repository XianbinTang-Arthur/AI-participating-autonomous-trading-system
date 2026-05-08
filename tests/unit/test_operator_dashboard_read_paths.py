from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import threading
from types import SimpleNamespace

from aats.services.blocker_control import BlockerControlService
from aats.services.operator.query_service import OperatorQueryService
from aats.services.operator.runtime_queries import RuntimeQueryFacade
from aats.services.operator.strategy_queries import StrategyQueryFacade


class _StrategyDashboardOwner:
    _DECIMAL_EPSILON = Decimal("0.000000000001")

    def __init__(self) -> None:
        now = datetime(2026, 5, 8, tzinfo=timezone.utc)
        self.recent_sleeve_limit: int | None = None
        self.recent_outcome_limit: int | None = None
        self.sleeve_record = SimpleNamespace(
            event_timestamp=now,
            created_at=now,
            realized_pnl=Decimal("1.25"),
            funding_fee_amount=Decimal("-0.05"),
        )
        self.outcome = SimpleNamespace(ingestion_timestamp=now, created_at=now)

    def _scope_cache_fragment(self) -> str:
        return "derivatives:cross:BTC-USDT-SWAP"

    def _cached_ttl(self, _key: str, _ttl_seconds: int, loader):
        return loader()

    def _scoped_sleeve_pnl_records_recent(self, *, limit: int):
        self.recent_sleeve_limit = limit
        return [self.sleeve_record]

    def _scoped_fill_outcomes_recent(self, *, limit: int):
        self.recent_outcome_limit = limit
        return [self.outcome]

    def _scoped_sleeve_pnl_records(self):
        raise AssertionError("dashboard attribution must not rebuild full sleeve PnL records")

    def _scoped_fill_outcomes(self):
        raise AssertionError("dashboard attribution must not hydrate all fill outcomes")

    def _strategy_sleeve_inventory_summary(self):
        return []

    def _execution_quality_row(self, _item):
        return {
            "realized_pnl_delta": Decimal("1.25"),
            "gross_realized_pnl": Decimal("1.25"),
            "fill_notional": Decimal("100"),
            "risk_protection_active": False,
            "market_regime": "trend",
            "active_profile_id": "trend_normal",
        }

    def _strategy_pnl_bucket_rows(self, *, records, key_name: str, fallback: str):
        return [{key_name: fallback, "record_count": len(records)}]

    @staticmethod
    def _to_decimal(value):
        if value is None:
            return None
        return value if isinstance(value, Decimal) else Decimal(str(value))


class _TrialReviewSummaryOwner:
    _DECIMAL_EPSILON = Decimal("0.000000000001")

    def __init__(self) -> None:
        self.dashboard_packet_calls = 0

    def forward_validation_report(self, *, window_days: int, period_count: int):
        return {
            "window_days": window_days,
            "period_count": period_count,
            "summary": {"verdict": "observe", "summary": "observe"},
            "periods": [
                {
                    "closed_fill_count": 0,
                    "net_realized_pnl": Decimal("0"),
                    "funding_fee_net_pnl": Decimal("0"),
                    "combined_net_realized_pnl": Decimal("0"),
                    "win_rate": None,
                    "fee_to_notional_ratio": None,
                    "high_slippage_count": 0,
                    "slow_submit_to_fill_count": 0,
                }
            ],
        }

    def scaling_readiness_report(self, *, window_days: int, period_count: int, forward_validation):
        return {
            "readiness": "continue_small_capital",
            "summary": "summary",
            "reasons": ["forward_validation_still_observing"],
            "recovery": {"safe_to_trade": True, "review_required": False},
            "active_blockers": [],
            "trial_guard": {"status": "monitoring"},
            "trial_guard_hard_stop": {"active": False},
            "runtime_constraints": {"can_continue_runtime": True},
        }

    def strategy_segment_report(self, *, limit: int):
        return {"group_by": ["symbol"], "segments": []}

    def recovery_view(self):
        raise AssertionError("summary must reuse scaling recovery")

    def blockers(self):
        raise AssertionError("summary must reuse scaling blockers")

    def _latest_trial_review_action(self):
        return None

    def _trial_review_action_items(self, **_kwargs):
        return ["continue"]

    def _trial_review_workbench_payload(self, **_kwargs):
        return {"latest_action": None}

    def guarded_live_run_packet(self):
        raise AssertionError("summary must not build the full guarded-live packet")

    def guarded_live_run_packet_dashboard(self):
        raise AssertionError("summary must reuse scaling context instead of run-packet loaders")

    @staticmethod
    def _to_decimal(value):
        if value is None:
            return None
        return value if isinstance(value, Decimal) else Decimal(str(value))


def test_strategy_attribution_dashboard_uses_recent_records_without_full_rebuild() -> None:
    owner = _StrategyDashboardOwner()
    payload = StrategyQueryFacade(owner).strategy_attribution_dashboard(limit=5)

    assert owner.recent_sleeve_limit == 5
    assert owner.recent_outcome_limit == 5
    assert payload["dashboard_summary_only"] is True
    assert payload["truth_source"] == "sleeve_pnl_records_recent_plus_fill_outcomes_recent_dashboard_summary"
    assert payload["summary"]["fill_count"] == 1


def test_trial_review_summary_reuses_scaling_context_and_lightweight_run_packet() -> None:
    owner = _TrialReviewSummaryOwner()

    payload = OperatorQueryService._build_trial_review_summary(
        owner,
        segment_limit=100,
        window_days=7,
        period_count=4,
    )

    assert owner.dashboard_packet_calls == 0
    assert payload["sections"]["guarded_live_run_packet"]["dashboard_summary_only"] is True
    assert payload["sections"]["guarded_live_run_packet"]["summary_source"] == "trial_review_scaling_context"
    assert payload["sections"]["workbench"] == {"latest_action": None}


def test_guarded_live_dashboard_uses_minimal_blocker_summary_without_full_blocker_control() -> None:
    service = OperatorQueryService.__new__(OperatorQueryService)
    service._cache_lock = threading.RLock()
    service._ttl_cache = {}
    service._inflight = {}
    service._scope_cache_fragment = lambda: "derivatives:cross:BTC-USDT-SWAP"
    service.runtime = SimpleNamespace(
        mode_controller=SimpleNamespace(
            snapshot=lambda: {"submit_blocked_reasons": ["live_submit_disabled"]}
        ),
        execution_adapter=SimpleNamespace(
            readiness=lambda: {"submit_blocked_reasons": ["exchange_not_ready"]}
        ),
        health_service=SimpleNamespace(snapshot=lambda: SimpleNamespace(blockers=["db_unavailable"])),
        kill_switch=SimpleNamespace(halted=False),
    )
    service.runtime_queries = RuntimeQueryFacade(service)
    service.blocker_control_service = BlockerControlService(service)
    service.guarded_live_preflight = lambda: {"status": "pass", "launch_ready": True}
    service.derivatives_live_guard = lambda: {
        "auto_halt_required": False,
        "only_reduce_required": False,
    }
    service.trial_guard = lambda: {"status": "monitoring"}
    service.margin_buffer_risk = lambda: {"status": "healthy", "current": {}, "liquidation": {}}
    service.recovery_view = lambda: {
        "safe_to_trade": True,
        "review_required": False,
        "resume_eligible": True,
        "resume_blocked_reasons": [],
    }
    service.blocker_control = lambda: (_ for _ in ()).throw(
        AssertionError("dashboard run packet must not build full blockerControl")
    )

    payload = service._build_guarded_live_run_packet_dashboard()

    assert payload["status"] == "critical"
    assert payload["summary_metrics"]["execution_blocker_count"] == 1
    assert payload["dashboard_summary_only"] is True
