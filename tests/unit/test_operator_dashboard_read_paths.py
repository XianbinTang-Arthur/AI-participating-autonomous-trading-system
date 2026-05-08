from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import inspect
import threading
from types import SimpleNamespace

from aats.api import auth_routes
from aats.services.blocker_control import BlockerControlService
from aats.services.operator.account_queries import AccountQueryFacade
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


def test_guarded_live_preflight_dashboard_uses_summary_paths_without_full_detail_loaders() -> None:
    service = OperatorQueryService.__new__(OperatorQueryService)
    service.runtime = SimpleNamespace(
        settings=SimpleNamespace(
            startup_profile="derivatives",
            mode="guarded_live",
            trading_product_type="derivatives",
            market_data_backend="okx",
            execution_backend="okx",
            account_backend="okx",
            account_read_enabled=True,
            storage_mode="postgres",
            database_url="postgresql://redacted",
            database_single_runtime_guard_enabled=True,
            operator_auth_enabled=True,
            operator_unsafe_write_without_auth=False,
            max_gross_notional_per_symbol=Decimal("50"),
            max_total_open_notional=Decimal("100"),
            trial_guard_max_daily_loss_usdt=Decimal("5"),
        ),
        policy_profile=SimpleNamespace(real_money_submission_structurally_blocked=False),
        health_service=SimpleNamespace(snapshot=lambda: SimpleNamespace(blockers=[])),
        kill_switch=SimpleNamespace(halted=False),
    )
    service.blocker_control_service = BlockerControlService(service)
    service.recovery_view = lambda: (_ for _ in ()).throw(
        AssertionError("dashboard preflight must not build full recovery")
    )
    service.blockers = lambda: (_ for _ in ()).throw(
        AssertionError("dashboard preflight must not build full blockers")
    )
    service.account_state = lambda: (_ for _ in ()).throw(
        AssertionError("dashboard preflight must not build full account state")
    )
    service.system_mode = lambda: (_ for _ in ()).throw(
        AssertionError("dashboard preflight must not build full system mode")
    )
    service.recovery_view_dashboard = lambda: {
        "safe_to_trade": True,
        "review_required": False,
        "resume_eligible": True,
        "resume_blocked_reasons": [],
        "recovery_state": "running",
    }
    service.account_service_status = lambda: {
        "connected": True,
        "fresh": True,
        "ready": True,
        "blockers": [],
    }
    service.margin_buffer_risk = lambda: {"status": "healthy"}
    service.derivatives_live_guard = lambda: {
        "auto_halt_required": False,
        "only_reduce_required": False,
    }
    service.trial_guard = lambda: {"status": "monitoring"}
    service._submit_blocked_reasons_dashboard = lambda: ["live_submit_disabled"]

    payload = service._build_guarded_live_preflight_dashboard()

    check_ids = {item["check_id"] for item in payload["checks"]}
    assert payload["status"] == "ready"
    assert payload["launch_ready"] is True
    assert payload["dashboard_summary_only"] is True
    assert payload["truth_source"] == "guarded_live_preflight_dashboard_summary"
    assert "small_capital_limits_present_dashboard" in check_ids
    assert "account_status_dashboard" in check_ids


def test_guarded_live_dashboard_uses_summary_preflight_recovery_and_minimal_blockers() -> None:
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
    service.guarded_live_preflight = lambda: (_ for _ in ()).throw(
        AssertionError("dashboard run packet must not build full preflight")
    )
    service.guarded_live_preflight_dashboard = lambda: {"status": "pass", "launch_ready": True}
    service.derivatives_live_guard = lambda: {
        "auto_halt_required": False,
        "only_reduce_required": False,
    }
    service.trial_guard = lambda: {"status": "monitoring"}
    service.margin_buffer_risk = lambda: {"status": "healthy", "current": {}, "liquidation": {}}
    service.recovery_view = lambda: (_ for _ in ()).throw(
        AssertionError("dashboard run packet must not build full recovery")
    )
    service.recovery_view_dashboard = lambda: {
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


def test_account_state_dashboard_uses_status_summary_without_full_account_loaders() -> None:
    service = OperatorQueryService.__new__(OperatorQueryService)
    service.runtime = SimpleNamespace(
        settings=SimpleNamespace(
            account_backend="okx",
            account_read_enabled=True,
            trading_product_type="derivatives",
            derivatives_require_exchange_pos_mode_match=False,
            derivatives_position_mode="long_short",
        ),
        recovery_status=SimpleNamespace(
            baseline_status="ready",
            baseline_imported=True,
            baseline_imported_at="2026-05-08T00:00:00Z",
            baseline_source="exchange_snapshot",
            baseline_requires_operator_review=False,
            baseline_safe_for_automatic_continuation=True,
            baseline_balance_count=1,
            baseline_position_count=1,
            baseline_open_order_count=0,
            baseline_fill_count=0,
            baseline_event_ref="evt_1",
        ),
    )
    service.account_queries = AccountQueryFacade(service)
    service.account_service_status = lambda: {
        "connected": True,
        "fresh": True,
        "ready": True,
        "last_update_ts": "2026-05-08T00:01:00Z",
        "blockers": [],
        "position_mode_contract": {"exchange_position_mode": "long_short_mode"},
        "account_configuration": {"account_mode": "portfolio"},
        "risk_snapshot": {"margin_ratio": "12.5"},
    }
    service.recovery_view_dashboard = lambda: {
        "recovery_state": "running",
        "review_required": False,
        "rebaseline_available": False,
        "resume_eligible": True,
        "safe_to_trade": True,
    }
    service.derivatives_live_guard = lambda: {"auto_halt_required": False}
    service.margin_buffer_risk = lambda: {"status": "healthy"}
    service._phase5_control_plane_enabled = lambda: False
    service.recovery_view = lambda: (_ for _ in ()).throw(
        AssertionError("dashboard account state must not build full recovery")
    )
    service._latest_scoped_snapshot = lambda: (_ for _ in ()).throw(
        AssertionError("dashboard account state must not load local snapshot")
    )
    service._latest_scoped_reconciliation = lambda: (_ for _ in ()).throw(
        AssertionError("dashboard account state must not load latest reconciliation")
    )

    payload = service.account_queries.build_account_state_dashboard()

    assert payload["fresh"] is True
    assert payload["ready"] is True
    assert payload["margin_buffer_overview"] == {"status": "healthy"}
    assert payload["dashboard_summary_only"] is True
    assert payload["truth_source"] == "account_status_plus_recovery_dashboard_summary"
    assert "persisted_funding_fee_summary" in payload["deferred_sections"]


def test_legacy_blockers_reuses_cached_blocker_control_payload() -> None:
    class Owner:
        def __init__(self) -> None:
            self.blocker_control_calls = 0

        def blocker_control(self):
            self.blocker_control_calls += 1
            return {
                "blockers": [
                    {
                        "blocker": "operator_rebaseline_required",
                        "subsystem": "reconciliation",
                        "affects_execution": True,
                        "submit_only": False,
                        "recommended_next_step": "先查看最新对账。",
                        "title": "需要确认新基线",
                        "description": "账实状态需要确认。",
                        "impact": "未确认前阻断执行。",
                        "priority": 10,
                        "root_cause": True,
                        "derived_from": [],
                        "resolution_mode": "operator",
                        "actions": [{"action_id": "inspect-reconciliation"}],
                    }
                ]
            }

        def _build_blocker_control(self):
            raise AssertionError("legacy blockers must not build another blockerControl snapshot")

    owner = Owner()

    payload = OperatorQueryService._build_blockers(owner)

    assert owner.blocker_control_calls == 1
    assert payload[0]["blocker"] == "operator_rebaseline_required"
    assert payload[0]["recommended_action"] == "先查看最新对账。"
    assert payload[0]["actions"] == [{"action_id": "inspect-reconciliation"}]


def test_dashboard_bundle_uses_summary_recovery_and_mode_panels() -> None:
    request_loader_source = inspect.getsource(auth_routes._protected_dashboard_panel_payload)
    snapshot_loader_source = inspect.getsource(auth_routes._load_dashboard_snapshot_panel)

    assert "query.system_mode_dashboard()" in request_loader_source
    assert "query.system_recovery_dashboard()" in request_loader_source
    assert "query.account_state_dashboard()" in request_loader_source
    assert "query.guarded_live_preflight_dashboard()" in request_loader_source
    assert "query.system_mode_dashboard()" in snapshot_loader_source
    assert "query.system_recovery_dashboard()" in snapshot_loader_source
    assert "query.account_state_dashboard()" in snapshot_loader_source
    assert "query.guarded_live_preflight_dashboard()" in snapshot_loader_source
