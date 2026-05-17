from __future__ import annotations

from decimal import Decimal
from threading import RLock
from types import SimpleNamespace

from aats.bootstrap import config as bootstrap_config
from aats.services.operator.query_service import OperatorQueryService
from aats.services.runtime_scope import RuntimeStateScope


def test_reconciliation_slice_uses_dedicated_execution_truth_repo(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeReconciliationService:
        def __init__(self, **kwargs) -> None:
            captured["reconciliation_execution_repo"] = kwargs["execution_repo"]
            self.stale_reconciliation_halt_clearer = None

    class FakeExecutionRecoveryService:
        def __init__(self, **kwargs) -> None:
            captured["recovery_execution_repo"] = kwargs["execution_repo"]

        def clear_stale_reconciliation_halt_if_resolved(self) -> bool:
            return True

    monkeypatch.setattr(bootstrap_config, "ReconciliationService", FakeReconciliationService)
    monkeypatch.setattr(bootstrap_config, "ExecutionRecoveryService", FakeExecutionRecoveryService)

    legacy_repo = object()
    execution_truth_repo = object()
    storage = SimpleNamespace(
        execution_truth_repo=execution_truth_repo,
        reconciliation_execution_repo=execution_truth_repo,
        execution_repo=legacy_repo,
        reconciliation_repo=object(),
        portfolio_repo=object(),
        event_store=object(),
        exit_execution_repo=None,
        obligation_repo=object(),
        fill_outcome_repo=object(),
        strategy_runtime_repo=object(),
        position_lot_repo=None,
        lot_event_repo=None,
        execution_order_repo=None,
        execution_command_repo=None,
    )
    slices = SimpleNamespace(
        bus=object(),
        account_service=object(),
        snapshot_builder=object(),
        market_gateway=SimpleNamespace(latest_price=lambda _symbol: Decimal("1")),
        bootstrap_from_exchange=True,
        reconciliation_classifier=object(),
        metrics=object(),
        portfolio_outbox_publisher=None,
        exit_execution_writer=None,
        kill_switch=object(),
        obligation_hot_state_cache=None,
        execution_outbox_publisher=None,
        sleeve_pnl_projection_service=None,
        execution_adapter=object(),
    )
    settings = SimpleNamespace(
        account_backend="okx",
        account_read_enabled=True,
        bootstrap_portfolio_from_exchange=True,
        initial_usdt_balance=Decimal("1000"),
        reconciliation_stale_after_seconds=60,
        recovery_reconciliation_execution_ledger_enabled=False,
        trading_product_type="derivatives",
    )
    runtime_layering = SimpleNamespace(
        environment_capabilities=SimpleNamespace(exchange_coupled=True),
        recovery_policy=object(),
    )

    bootstrap_config._build_reconciliation_slice(
        runtime_settings=settings,
        runtime_layering=runtime_layering,
        storage=storage,
        slices=slices,
        effective_process_role=None,
    )

    assert captured["reconciliation_execution_repo"] is execution_truth_repo
    assert captured["recovery_execution_repo"] is execution_truth_repo
    assert captured["reconciliation_execution_repo"] is not legacy_repo


def test_operator_scoped_execution_reads_use_execution_truth_repo() -> None:
    class FakeExecutionRepo:
        def __init__(self, label: str) -> None:
            self.label = label
            self.order_calls: list[dict[str, object]] = []
            self.fill_calls: list[dict[str, object]] = []
            self.order_fills_calls: list[str] = []

        def order_states_for_scope(self, **kwargs):
            self.order_calls.append(kwargs)
            return [SimpleNamespace(source=self.label)]

        def fills_for_scope(self, **kwargs):
            self.fill_calls.append(kwargs)
            return [SimpleNamespace(source=self.label)]

        def fills_for_order(self, client_order_id: str):
            self.order_fills_calls.append(client_order_id)
            return [
                SimpleNamespace(
                    source=self.label,
                    product_type="derivatives",
                    margin_mode="cross",
                    symbol="BTC-USDT-SWAP",
                )
            ]

    legacy_repo = FakeExecutionRepo("legacy")
    truth_repo = FakeExecutionRepo("truth")
    service = object.__new__(OperatorQueryService)
    service.runtime = SimpleNamespace(
        execution_repo=legacy_repo,
        execution_truth_repo=truth_repo,
    )
    service.state_scope = RuntimeStateScope(
        product_type="derivatives",
        margin_mode="cross",
        allowed_symbols=("BTC-USDT-SWAP",),
        default_symbol="BTC-USDT-SWAP",
    )
    service._cache = {}
    service._ttl_cache = {}
    service._cache_lock = RLock()
    service._inflight = {}

    assert service._scoped_order_states()[0].source == "truth"
    assert service._scoped_open_order_states()[0].source == "truth"
    assert service._scoped_fills()[0].source == "truth"
    assert service._scoped_fills_for_order("clord_1")[0].source == "truth"
    assert service._execution_read_truth_source() == "execution_truth_repo"
    assert len(truth_repo.order_calls) == 2
    assert len(truth_repo.fill_calls) == 1
    assert truth_repo.order_fills_calls == ["clord_1"]
    assert legacy_repo.order_calls == []
    assert legacy_repo.fill_calls == []
    assert legacy_repo.order_fills_calls == []
