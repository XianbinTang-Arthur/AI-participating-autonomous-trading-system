from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone
from decimal import Decimal
import inspect
import threading
from types import SimpleNamespace

from aats.api import auth_routes
from aats.services.blocker_control import BlockerControlService
from aats.services.operator import query_service as query_service_module
from aats.services.operator.account_queries import AccountQueryFacade
from aats.services.operator.query_service import OperatorQueryService
from aats.services.operator.reconciliation_system_queries import ReconciliationSystemQueryFacade
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


class _RuntimeDict:
    def __init__(self, **payload) -> None:
        self.payload = dict(payload)

    def to_dict(self):
        return dict(self.payload)


class _RuntimeDashboardOwner:
    def __init__(self) -> None:
        now = datetime(2026, 5, 8, tzinfo=timezone.utc)
        self.strategy_runtime_dashboard_calls = 0
        self.runtime = SimpleNamespace(
            event_store=SimpleNamespace(
                latest=lambda _topic: SimpleNamespace(event_timestamp=now),
            ),
            mode_controller=SimpleNamespace(
                snapshot=lambda: {
                    "submit_blocked_reasons": ["exchange_not_ready"],
                    "exchange_submit_allowed": True,
                },
            ),
            execution_adapter=SimpleNamespace(
                readiness=lambda: (_ for _ in ()).throw(
                    AssertionError("dashboard runtime must synthesize execution readiness")
                ),
            ),
            runtime_profile=_RuntimeDict(profile="derivatives-live"),
            environment_capabilities=_RuntimeDict(okx=True),
            policy_profile=_RuntimeDict(real_money_submission_structurally_blocked=False),
            recovery_policy=_RuntimeDict(policy="standard"),
            runtime_profile_resolution=SimpleNamespace(profile_source="test"),
            settings=SimpleNamespace(
                mode="guarded_live",
                trading_product_type="derivatives",
                startup_profile="derivatives",
                env_template_profile="derivatives-live",
                config_profile="derivatives_live",
                default_symbol="BTC-USDT-SWAP",
                enabled_decision_timeframes=("5m",),
                decision_min_interval_seconds_15m=60,
                decision_min_interval_seconds_1h=300,
                decision_min_price_move_bps=5,
                decision_min_momentum_delta=0.1,
                max_decisions_per_minute=4,
                strategy_family_active="directional",
                storage_mode="postgres",
                operator_auth_enabled=True,
                operator_session_configured=True,
                operator_read_api_key=None,
                operator_write_api_key=None,
                operator_unsafe_write_without_auth=False,
                operator_control_plane_execution_ledger_enabled=True,
                financial_convergence_mode_enabled=True,
                portfolio_ledger_truth_enabled=True,
            ),
            database_runtime=object(),
            operator_repo=SimpleNamespace(count=lambda: 1),
            started_at=now,
            recovery_status=SimpleNamespace(
                baseline_status="ready",
                baseline_imported=True,
                baseline_imported_at=now,
                baseline_source="exchange_snapshot",
                baseline_requires_operator_review=False,
                baseline_safe_for_automatic_continuation=True,
                baseline_balance_count=1,
                baseline_position_count=1,
                baseline_open_order_count=0,
                baseline_fill_count=0,
                baseline_event_ref="evt_baseline",
                last_rebaseline_event_ref=None,
                last_rebaseline_at=None,
            ),
        )
        self.blocker_control_service = SimpleNamespace(
            execution_blocker_summary=lambda *, recovery, submit_blocked_reasons: {
                "blockers": [
                    {
                        "blocker": reason,
                        "affects_execution": True,
                    }
                    for reason in submit_blocked_reasons
                ]
            }
        )

    def _phase5_control_plane_enabled(self):
        return True

    def latest_fill(self):
        return None

    def _latest_scoped_reconciliation(self):
        raise AssertionError("dashboard runtime must defer latest reconciliation")

    def latest_account_baseline(self):
        return {"baseline_kind": "exchange"}

    def latest_exchange_snapshot(self):
        return SimpleNamespace(account_configuration=None, risk_snapshot=None, instruments=[])

    def recovery_view_dashboard(self):
        return {
            "recovery_state": "running",
            "review_required": False,
            "rebaseline_available": False,
            "resume_eligible": True,
            "safe_to_trade": True,
        }

    def guarded_live_preflight_dashboard(self):
        raise AssertionError("dashboard runtime must build preflight from existing runtime context")

    def _submit_blocked_reasons_dashboard(self):
        raise AssertionError("dashboard runtime must merge submit blockers from existing runtime context")

    def account_service_status(self):
        return {"position_mode_contract": {"exchange_position_mode": "long_short_mode"}}

    def runtime_profile_snapshot(self):
        return {"activation": {}}

    def strategy_runtime_dashboard(self, *, limit: int):
        self.strategy_runtime_dashboard_calls += 1
        raise AssertionError("dashboard runtime must not build strategyRuntime panel payload")

    def trial_guard(self):
        return {"status": "monitoring"}

    def margin_buffer_risk(self):
        return {"status": "healthy", "current": {}, "liquidation": {}}

    def derivatives_live_guard(self):
        return {"auto_halt_required": False, "only_reduce_required": False}

    def strategy_runtime(self, *, limit: int):
        raise AssertionError("dashboard runtime must not build full strategy runtime")

    def recovery_view(self):
        raise AssertionError("dashboard runtime must not build full recovery")

    def guarded_live_preflight(self):
        raise AssertionError("dashboard runtime must not build full preflight")

    def blocker_control(self):
        raise AssertionError("dashboard runtime must not build full blocker control")


def test_system_runtime_dashboard_uses_summary_loaders_without_full_runtime() -> None:
    owner = _RuntimeDashboardOwner()

    payload = RuntimeQueryFacade(owner).build_system_runtime(dashboard_summary_only=True)

    assert owner.strategy_runtime_dashboard_calls == 0
    assert payload["dashboard_summary_only"] is True
    assert payload["truth_source"] == "system_runtime_dashboard_summary"
    assert payload["strategy_runtime_summary"]["deferred_from_dashboard_summary"] is True
    assert payload["guarded_live_preflight"]["truth_source"] == "runtime_context_guarded_live_preflight_summary"
    assert payload["guarded_live_run_packet_summary"]["summary_source"] == "runtime_lightweight"
    assert payload["guarded_live_run_packet_summary"]["summary_metrics"]["execution_blocker_count"] == 1
    assert "execution_adapter.readiness" in payload["deferred_sections"]
    assert "latest_reconciliation" in payload["deferred_sections"]


class _StrategyRuntimeDashboardFacadeOwner:
    def __init__(self) -> None:
        self.cache_key: str | None = None
        self.ttl_seconds: int | None = None
        self.build_limit: int | None = None
        self.dashboard_summary_only: bool | None = None

    def _scope_cache_fragment(self) -> str:
        return "derivatives:cross:BTC-USDT-SWAP"

    def _cached_ttl(self, key: str, ttl_seconds: int, loader):
        self.cache_key = key
        self.ttl_seconds = ttl_seconds
        return loader()

    def _build_strategy_runtime(self, *, limit: int, dashboard_summary_only: bool = False):
        self.build_limit = limit
        self.dashboard_summary_only = dashboard_summary_only
        return {
            "dashboard_summary_only": dashboard_summary_only,
            "limit": limit,
        }


def test_strategy_runtime_dashboard_facade_uses_dashboard_summary_builder() -> None:
    owner = _StrategyRuntimeDashboardFacadeOwner()

    payload = StrategyQueryFacade(owner).strategy_runtime_dashboard(limit=0)

    assert payload["dashboard_summary_only"] is True
    assert payload["limit"] == 1
    assert owner.build_limit == 1
    assert owner.dashboard_summary_only is True
    assert owner.ttl_seconds == 30
    assert owner.cache_key == "strategy_runtime_dashboard:derivatives:cross:BTC-USDT-SWAP:1"


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


class _ReconReport:
    def __init__(self, reconciliation_id: str, *, index: int = 0) -> None:
        self.reconciliation_id = reconciliation_id
        self.product_type = "derivatives"
        self.margin_mode = "cross"
        self.allowed_symbols: list[str] = []
        self.exchange_bills_summary = {"bill_count": index}
        self.exchange_bills_explanations = [{"bill": index}]

    def model_dump(self, *, mode: str = "json") -> dict:
        return {
            "reconciliation_id": self.reconciliation_id,
            "product_type": self.product_type,
            "margin_mode": self.margin_mode,
            "exchange_bills_summary": self.exchange_bills_summary,
        }


class _ReconRepo:
    def __init__(self, rows: list[_ReconReport]) -> None:
        self.rows = rows
        self.history_limits: list[int | None] = []
        self.get_report_calls: list[str] = []
        self.history_called = False

    def history_for_scope(self, *, scope, limit: int | None = None):
        self.history_limits.append(limit)
        return self.rows if limit is None else self.rows[-limit:]

    def count_for_scope(self, *, scope) -> int:
        return len(self.rows)

    def get_report(self, reconciliation_id: str):
        self.get_report_calls.append(reconciliation_id)
        return next((row for row in self.rows if row.reconciliation_id == reconciliation_id), None)

    def history(self):
        self.history_called = True
        raise AssertionError("reconciliation detail must use direct repo lookup")


def _reconciliation_owner(repo: _ReconRepo):
    return SimpleNamespace(
        runtime=SimpleNamespace(
            reconciliation_repo=repo,
            event_store=SimpleNamespace(latest=lambda _topic: None),
        ),
        state_scope=SimpleNamespace(product_type="derivatives", margin_mode="cross"),
        _exchange_bills_summary=lambda: {"fallback": True},
        _reconciliation_mismatch_summary=lambda _report: {},
        recovery_view=lambda: (_ for _ in ()).throw(
            AssertionError("dashboard reconciliation latest must not build full recovery")
        ),
        recovery_view_dashboard=lambda: {
            "latest_baseline_generation": None,
            "latest_exchange_ack_watermark": None,
            "latest_state_snapshot": None,
        },
    )


def test_reconciliation_latest_dashboard_uses_summary_recovery() -> None:
    report = _ReconReport("recon_1", index=1)
    owner = _reconciliation_owner(_ReconRepo([report]))
    owner._latest_scoped_reconciliation = lambda: report

    payload = ReconciliationSystemQueryFacade(owner).reconciliation_latest_dashboard()

    assert payload["reconciliation"]["reconciliation_id"] == "recon_1"
    assert payload["exchange_bills_summary"] == {"bill_count": 1}
    assert payload["dashboard_summary_only"] is True
    assert payload["truth_source"] == "latest_reconciliation_plus_recovery_dashboard_summary"


def test_reconciliation_recent_fetches_only_bounded_window() -> None:
    rows = [_ReconReport(f"recon_{index}", index=index) for index in range(10)]
    repo = _ReconRepo(rows)
    owner = _reconciliation_owner(repo)

    payload = ReconciliationSystemQueryFacade(owner).reconciliation_recent(limit=2, offset=3)

    assert repo.history_limits == [6]
    assert [row["reconciliation_id"] for row in payload["reconciliations"]] == ["recon_6", "recon_5"]
    assert payload["total_available"] == 10
    assert payload["has_more"] is True
    assert payload["read_window_limit"] == 6


def test_reconciliation_detail_uses_direct_repo_lookup_without_history_scan() -> None:
    rows = [_ReconReport("recon_1", index=1)]
    repo = _ReconRepo(rows)
    owner = _reconciliation_owner(repo)
    owner.recovery_view = lambda: {"latest_state_snapshot": {"state": "ok"}}

    payload = ReconciliationSystemQueryFacade(owner).reconciliation_detail("recon_1")

    assert repo.get_report_calls == ["recon_1"]
    assert repo.history_called is False
    assert payload["reconciliation"]["reconciliation_id"] == "recon_1"


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


def test_dashboard_health_payload_uses_lightweight_execution_summary_on_metrics_miss() -> None:
    class Query:
        def system_health_dashboard(self):
            return {
                "runtime_state": "healthy",
                "subsystems": {
                    "phase1_shadow": {
                        "status": "healthy",
                        "lag": {
                            "order_backlog": 1,
                            "fill_backlog": 2,
                            "obligation_backlog": 3,
                        },
                    }
                },
            }

        def metrics_if_cached(self):
            return None

        def metrics(self):
            raise AssertionError("dashboard health must not build full metrics on cache miss")

        def _scoped_order_states(self):
            raise AssertionError("dashboard health must not hydrate order history")

        def _scoped_fills(self):
            raise AssertionError("dashboard health must not hydrate fill history")

        def _scoped_open_order_states(self):
            raise AssertionError("dashboard health must not hydrate open order history")

    runtime = SimpleNamespace(
        metrics=SimpleNamespace(
            snapshot=lambda: {
                "order_intents_generated": 7,
                "fills_processed": 4,
                "processing_failures": 1,
                "portfolio_snapshot_repairs": 2,
            }
        )
    )

    payload = auth_routes._system_health_payload_for_runtime(runtime, Query())

    summary = payload["execution_summary"]
    assert summary["summary_source"] == "runtime_metrics_dashboard_summary"
    assert summary["order_intents_generated"] == 7
    assert summary["fills_processed"] == 4
    assert summary["fill_count"] is None
    assert summary["phase1_shadow_order_backlog"] == 1
    assert "order_count" in summary["deferred_sections"]
    assert "fill_count" in summary["deferred_sections"]


def test_dashboard_health_payload_reuses_cached_operator_metrics_without_rebuild() -> None:
    class Query:
        def system_health_dashboard(self):
            return {"runtime_state": "healthy", "subsystems": {}}

        def metrics_if_cached(self):
            return {
                "fill_count": 12,
                "current_open_order_count": 3,
                "processing_failure_count": 4,
                "portfolio_snapshot_repair_count": 5,
                "fill_without_snapshot_count": 6,
                "snapshot_without_reconciliation_count": 7,
                "phase1_shadow": {"status": "lagging"},
                "phase1_shadow_failure_count": 8,
                "phase1_shadow_alert_count": 9,
                "phase1_shadow_recovery_count": 10,
                "phase1_shadow_order_backlog": 11,
                "phase1_shadow_fill_backlog": 12,
                "phase1_shadow_obligation_backlog": 13,
            }

        def metrics(self):
            raise AssertionError("dashboard health should peek cached metrics, not rebuild them")

    runtime = SimpleNamespace(
        metrics=SimpleNamespace(
            snapshot=lambda: {
                "order_intents_generated": 2,
                "fills_processed": 1,
            }
        )
    )

    payload = auth_routes._system_health_payload_for_runtime(runtime, Query())

    summary = payload["execution_summary"]
    assert summary["summary_source"] == "cached_operator_metrics"
    assert summary["fill_count"] == 12
    assert summary["open_order_count"] == 3
    assert summary["phase1_shadow_status"] == "lagging"
    assert summary["order_count"] is None
    assert summary["deferred_sections"] == ["order_count"]


def test_dashboard_metrics_uses_lightweight_summary_when_full_metrics_cache_misses() -> None:
    service = OperatorQueryService.__new__(OperatorQueryService)
    service._ttl_cache = {}
    service._cache_lock = threading.RLock()
    service._inflight = {}
    service.state_scope = SimpleNamespace(
        product_type="derivatives",
        margin_mode="cross",
        allowed_symbols={"BTC-USDT-SWAP"},
    )
    service.runtime = SimpleNamespace(
        metrics=SimpleNamespace(
            snapshot=lambda: {
                "processing_failures": 2,
                "portfolio_snapshot_repairs": 3,
                "reconciliation_mismatches": 4,
                "phase1_shadow_alerts": 5,
                "phase1_shadow_recoveries": 6,
            }
        )
    )
    service.runtime_queries = SimpleNamespace(
        metrics=lambda: (_ for _ in ()).throw(
            AssertionError("dashboard metrics must not rebuild full metrics")
        )
    )
    service.phase1_shadow = lambda: {
        "lag": {"order_backlog": 1, "fill_backlog": 2, "obligation_backlog": 3},
        "execution_shadow": {"order_failure_count": 4, "fill_failure_count": 5},
        "ledger_shadow": {"sync_failure_count": 6},
    }
    service._latest_scoped_snapshot = lambda: SimpleNamespace(
        gross_exposure=Decimal("12.5"),
        net_exposure=Decimal("1.5"),
        total_equity=Decimal("100.0"),
    )
    service._scoped_open_order_states = lambda: [object(), object()]

    payload = service.metrics_dashboard()

    assert payload["dashboard_summary_only"] is True
    assert payload["summary_source"] == "dashboard_metrics_light"
    assert payload["current_open_order_count"] == 2
    assert payload["processing_failure_count"] == 2
    assert payload["portfolio_snapshot_repair_count"] == 3
    assert payload["reconciliation_mismatch_count"] == 4
    assert payload["phase1_shadow_failure_count"] == 15
    assert payload["exposure_summary"]["total_equity"] == Decimal("100.0")
    assert payload["fill_count"] is None
    assert "fill_count" in payload["deferred_sections"]
    assert "strategy_execution_health" in payload["deferred_sections"]


def test_dashboard_metrics_reuses_cached_full_metrics_without_downgrading() -> None:
    service = OperatorQueryService.__new__(OperatorQueryService)
    service._ttl_cache = {}
    service._cache_lock = threading.RLock()
    service._inflight = {}
    service.state_scope = SimpleNamespace(
        product_type="derivatives",
        margin_mode="cross",
        allowed_symbols={"BTC-USDT-SWAP"},
    )
    cache_key = f"metrics:{service._scope_cache_fragment()}"
    service._ttl_cache[cache_key] = (
        datetime(2030, 1, 1, tzinfo=timezone.utc),
        {
            "fill_count": 12,
            "current_open_order_count": 1,
            "strategy_execution_health": {"status": "healthy"},
        },
    )

    payload = service.metrics_dashboard()

    assert payload["fill_count"] == 12
    assert payload["current_open_order_count"] == 1
    assert payload["dashboard_summary_only"] is False
    assert payload["summary_source"] == "cached_operator_metrics"
    assert payload["deferred_sections"] == []


def test_direct_blocker_history_does_not_use_short_ttl_cache() -> None:
    service = OperatorQueryService.__new__(OperatorQueryService)
    service._ttl_cache = {}
    service._cache_lock = threading.RLock()
    service._inflight = {}
    service.state_scope = SimpleNamespace(
        product_type="derivatives",
        margin_mode="cross",
        allowed_symbols={"BTC-USDT-SWAP"},
    )
    calls = []

    def _history(*, limit: int, offset: int):
        calls.append((limit, offset))
        return {"history": [{"blocker": "one"}], "limit": limit, "offset": offset}

    service.blocker_queries = SimpleNamespace(blocker_history=_history)

    first = service.blocker_history(limit=20, offset=0)
    second = service.blocker_history(limit=20, offset=0)

    assert first == second
    assert calls == [(20, 0), (20, 0)]


def test_dashboard_blocker_history_uses_short_ttl_cache() -> None:
    service = OperatorQueryService.__new__(OperatorQueryService)
    service._ttl_cache = {}
    service._cache_lock = threading.RLock()
    service._inflight = {}
    service.state_scope = SimpleNamespace(
        product_type="derivatives",
        margin_mode="cross",
        allowed_symbols={"BTC-USDT-SWAP"},
    )
    calls = []

    def _history(*, limit: int, offset: int):
        calls.append((limit, offset))
        return {"history": [{"blocker": "one"}], "limit": limit, "offset": offset}

    service.blocker_queries = SimpleNamespace(blocker_history=_history)

    first = service.blocker_history_dashboard(limit=20, offset=0)
    second = service.blocker_history_dashboard(limit=20, offset=0)

    assert first == second
    assert calls == [(20, 0)]


def test_dashboard_blockers_panel_prefers_cached_dashboard_history() -> None:
    class Query:
        def __init__(self) -> None:
            self.dashboard_calls = 0
            self.direct_calls = 0

        def blocker_history_dashboard(self, *, limit: int, offset: int):
            self.dashboard_calls += 1
            assert limit == 20
            assert offset == 0
            return {"history": [{"blocker": "cached"}]}

        def blocker_history(self, *, limit: int, offset: int):
            self.direct_calls += 1
            return {"history": [{"blocker": "fresh"}]}

    query = Query()
    payload = auth_routes._blockers_panel_payload_from_blocker_control_for_runtime(
        runtime=SimpleNamespace(kill_switch=SimpleNamespace(halted=False)),
        query=query,
        blocker_control={"blockers": [{"blocker": "operator_rebaseline_required"}]},
    )

    assert payload["recent_history"] == [{"blocker": "cached"}]
    assert query.dashboard_calls == 1
    assert query.direct_calls == 0


def test_recent_decisions_dashboard_batches_page_payload_refs() -> None:
    service = OperatorQueryService.__new__(OperatorQueryService)
    records = [
        SimpleNamespace(
            decision_id="decision_1",
            decision_context_ref="ctx_1",
            position_target_ref="target_1",
            policy_decision_ref="policy_1",
            risk_decision_ref="risk_1",
            decision_outcome_ref="outcome_1",
            strategy_sleeve_intent_refs=["sleeve_1", "sleeve_2"],
            order_intent_refs=[],
            fill_event_refs=[],
            reconciliation_refs=[],
        ),
        SimpleNamespace(
            decision_id="decision_2",
            decision_context_ref="ctx_2",
            position_target_ref="target_2",
            policy_decision_ref="policy_2",
            risk_decision_ref="risk_2",
            decision_outcome_ref="outcome_2",
            strategy_sleeve_intent_refs=["sleeve_3"],
            order_intent_refs=[],
            fill_event_refs=[],
            reconciliation_refs=[],
        ),
    ]
    service.runtime = SimpleNamespace(
        audit_repo=SimpleNamespace(
            recent=lambda *, limit: records[:limit],
        )
    )
    captured_refs = []
    payload_map = {
        "ctx_1": {"symbol": "BTC-USDT-SWAP", "timeframe": "5m", "as_of_ts": "2026-05-08T01:00:00Z"},
        "ctx_2": {"symbol": "BTC-USDT-SWAP", "timeframe": "5m", "as_of_ts": "2026-05-08T01:05:00Z"},
        "target_1": {"position_intent": "hold", "delta_position_qty": "0"},
        "target_2": {"position_intent": "open_long", "delta_position_qty": "0.01"},
        "policy_1": {"execution_allowed": False},
        "policy_2": {"execution_allowed": True},
        "risk_1": {"approved": False},
        "risk_2": {"approved": True},
        "outcome_1": {"final_action": "hold"},
        "outcome_2": {"final_action": "enter"},
        "sleeve_1": {"intent": "observe"},
        "sleeve_2": {"intent": "hold"},
        "sleeve_3": {"intent": "enter"},
    }

    def _payloads_by_ref_map(refs):
        captured_refs.append(list(refs))
        return {ref: payload_map[ref] for ref in refs if ref in payload_map}

    service.payloads_by_ref_map = _payloads_by_ref_map
    service._position_target_payload = lambda payload: payload
    service._risk_decision_payload = lambda payload: payload
    service._resolved_position_target_payload = lambda **kwargs: kwargs["position_target"]
    service._no_trade_classification_payload = lambda **_kwargs: {"classification": "test"}
    service._book_runtime_states_from_payload = lambda _payload: []
    service._independent_adaptive_summary_from_payload = lambda _payload: None
    service._independent_transition_exception_summary_from_payload = lambda _payload: None
    service._effective_diagnostic_metric_flags = lambda _payload: {}
    service._resolved_overlay_parent_exposure = lambda _payload: None
    service._resolved_overlay_parent_exposure_summary = lambda _payload: None
    service._resolved_overlay_parent_signal_fields = lambda _payload: None

    payload = service._build_recent_decisions(limit=2, offset=0, include_total=False)

    assert captured_refs == [
        [
            "ctx_1",
            "target_1",
            "policy_1",
            "risk_1",
            "outcome_1",
            "sleeve_1",
            "sleeve_2",
            "ctx_2",
            "target_2",
            "policy_2",
            "risk_2",
            "outcome_2",
            "sleeve_3",
        ]
    ]
    assert [row["decision_id"] for row in payload["decisions"]] == ["decision_1", "decision_2"]
    assert payload["decisions"][1]["position_intent"] == "open_long"
    assert payload["has_more"] is False


def test_payloads_by_ref_map_reuses_runtime_payload_cache_and_returns_copies() -> None:
    service = OperatorQueryService.__new__(OperatorQueryService)
    service._payload_ref_cache = OrderedDict()
    service._cache_lock = threading.RLock()
    calls: list[list[str]] = []
    payloads = {
        "evt_1": {"value": 1, "nested": {"stable": 1}},
        "evt_2": {"value": 2, "nested": {"stable": 2}},
        "evt_3": {"value": 3, "nested": {"stable": 3}},
    }

    def _get_many(refs: list[str]):
        calls.append(list(refs))
        return {
            ref: SimpleNamespace(event_id=ref, topic="test.topic", payload=payloads[ref])
            for ref in refs
            if ref in payloads
        }

    service.runtime = SimpleNamespace(event_store=SimpleNamespace(get_many=_get_many))

    first = service.payloads_by_ref_map(["evt_1", "evt_2"])
    first["evt_2"]["nested"]["stable"] = 99
    second = service.payloads_by_ref_map(["evt_2", "evt_3"])

    assert calls == [["evt_1", "evt_2"], ["evt_3"]]
    assert second["evt_2"]["nested"]["stable"] == 2
    assert second["evt_2"]["_event_id"] == "evt_2"
    assert second["evt_2"]["_topic"] == "test.topic"
    assert second["evt_3"]["value"] == 3


def test_payloads_by_ref_map_bounds_runtime_payload_cache(monkeypatch) -> None:
    monkeypatch.setattr(query_service_module, "_PAYLOAD_REF_CACHE_MAX_ENTRIES", 2)
    service = OperatorQueryService.__new__(OperatorQueryService)
    service._payload_ref_cache = OrderedDict()
    service._cache_lock = threading.RLock()
    calls: list[list[str]] = []

    def _get_many(refs: list[str]):
        calls.append(list(refs))
        return {
            ref: SimpleNamespace(event_id=ref, topic="test.topic", payload={"value": ref})
            for ref in refs
        }

    service.runtime = SimpleNamespace(event_store=SimpleNamespace(get_many=_get_many))

    service.payloads_by_ref_map(["evt_1", "evt_2"])
    service.payloads_by_ref_map(["evt_3"])
    assert list(service._payload_ref_cache.keys()) == ["evt_2", "evt_3"]

    payload = service.payloads_by_ref_map(["evt_1"])

    assert calls == [["evt_1", "evt_2"], ["evt_3"], ["evt_1"]]
    assert payload["evt_1"]["value"] == "evt_1"


def test_dashboard_bundle_uses_summary_recovery_and_mode_panels() -> None:
    request_loader_source = inspect.getsource(auth_routes._protected_dashboard_panel_payload)
    snapshot_loader_source = inspect.getsource(auth_routes._load_dashboard_snapshot_panel)

    assert "query.metrics_dashboard()" in request_loader_source
    assert "query.metrics_dashboard()" in snapshot_loader_source
    assert "query.system_mode_dashboard()" in request_loader_source
    assert "query.system_runtime_dashboard()" in request_loader_source
    assert "query.system_recovery_dashboard()" in request_loader_source
    assert "query.account_state_dashboard()" in request_loader_source
    assert "query.execution_latest_dashboard()" in request_loader_source
    assert "query.guarded_live_preflight_dashboard()" in request_loader_source
    assert "query.reconciliation_latest_dashboard()" in request_loader_source
    assert "query.system_mode_dashboard()" in snapshot_loader_source
    assert "query.system_runtime_dashboard()" in snapshot_loader_source
    assert "query.system_recovery_dashboard()" in snapshot_loader_source
    assert "query.account_state_dashboard()" in snapshot_loader_source
    assert "query.execution_latest_dashboard()" in snapshot_loader_source
    assert "query.guarded_live_preflight_dashboard()" in snapshot_loader_source
    assert "query.reconciliation_latest_dashboard()" in snapshot_loader_source
    assert "query.strategy_runtime_dashboard()" in request_loader_source
    assert "query.strategy_runtime_dashboard()" in snapshot_loader_source
