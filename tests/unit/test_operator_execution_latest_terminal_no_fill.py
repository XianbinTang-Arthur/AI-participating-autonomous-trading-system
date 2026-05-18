from __future__ import annotations

from types import SimpleNamespace

from aats.services.operator.account_queries import AccountQueryFacade
from aats.services.operator.query_service import OperatorQueryService
from aats.services.runtime_scope import RuntimeStateScope


class _FakeOwner:
    def __init__(
        self,
        *,
        orders: list[dict],
        fills: list[dict] | None = None,
        order_count: int | None = None,
        fill_count: int | None = None,
        current_runtime_timestamps: set[str] | None = None,
        readiness_raises: bool = False,
        reconciliation_raises: bool = False,
    ) -> None:
        self.orders = orders
        self.fills = fills or []
        self.order_count = len(orders) if order_count is None else order_count
        self.fill_count = len(self.fills) if fill_count is None else fill_count
        self.order_row_calls: list[dict] = []
        self.fill_row_calls: list[dict] = []
        self.order_count_calls = 0
        self.fill_count_calls = 0
        self.current_runtime_timestamps = current_runtime_timestamps
        self.dashboard_recovery_calls = 0
        self.dashboard_mode_calls = 0
        self.execution_error_calls = 0
        self.execution_readiness_calls = 0
        self.reconciliation_calls = 0
        self.readiness_raises = readiness_raises
        self.reconciliation_raises = reconciliation_raises
        self.runtime = SimpleNamespace(
            settings=SimpleNamespace(
                mode="paper",
                live_submit_enabled=False,
                guarded_execution_dry_run=True,
                okx_simulated_trading=True,
            ),
            execution_adapter=SimpleNamespace(readiness=self.execution_readiness),
            execution_order_repo=SimpleNamespace(count_orders=self._unscoped_order_count),
            execution_fill_repo_v2=SimpleNamespace(count_fills=self._unscoped_fill_count),
        )

    def _unscoped_order_count(self):
        raise AssertionError("phase5 order pagination must not use unscoped count_orders")

    def _unscoped_fill_count(self):
        raise AssertionError("phase5 fill pagination must not use unscoped count_fills")

    def execution_readiness(self):
        self.execution_readiness_calls += 1
        if self.readiness_raises:
            raise AssertionError("dashboard executionLatest must not call full execution readiness")
        return {"ready": True}

    def latest_order(self):
        return self.orders[0] if self.orders else None

    def latest_fill(self):
        return self.fills[0] if self.fills else None

    def latest_exchange_snapshot(self):
        return SimpleNamespace(open_orders=[], fills=[])

    def _latest_scoped_reconciliation(self):
        self.reconciliation_calls += 1
        if self.reconciliation_raises:
            raise AssertionError("dashboard executionLatest must not call latest reconciliation")
        return None

    def recovery_view(self):
        return {"recovery_state": "normal_operation"}

    def recovery_view_dashboard(self):
        self.dashboard_recovery_calls += 1
        return {"recovery_state": "dashboard_normal_operation"}

    def system_mode(self):
        return {"execution_route": "derivatives_live"}

    def system_mode_dashboard(self):
        self.dashboard_mode_calls += 1
        return {
            "execution_route": "derivatives_live_dashboard",
            "mode": "derivatives-live",
            "execution_blocked": False,
            "exchange_submit_allowed": True,
            "submit_blocked": False,
            "submit_blocked_reasons": [],
        }

    def _execution_record_payload(self, record):
        payload = dict(record)
        if "state" in payload and "status" not in payload:
            payload["status"] = payload["state"]
        return payload

    def execution_errors(self):
        self.execution_error_calls += 1
        return {"errors": []}

    def _is_current_runtime_timestamp(self, value):
        if self.current_runtime_timestamps is None:
            return True
        return str(value) in self.current_runtime_timestamps

    def _phase5_control_plane_enabled(self):
        return True

    def _phase5_order_rows(self, *, limit=None, offset=0, open_only=False):
        call = {"limit": limit, "offset": offset}
        if open_only:
            call["open_only"] = True
        self.order_row_calls.append(call)
        rows = self.orders[offset:]
        if open_only:
            rows = [
                row
                for row in rows
                if str(row.get("state") or row.get("status") or "").upper()
                not in {"FILLED", "CANCELED", "REJECTED", "FAILED", "BLOCKED", "DRY_RUN", "EXPIRED"}
            ]
        return rows[:limit] if limit is not None else rows

    def _phase5_order_count(self, *, open_only=False):
        self.order_count_calls += 1
        if open_only:
            return len(
                [
                    row
                    for row in self.orders
                    if str(row.get("state") or row.get("status") or "").upper()
                    not in {"FILLED", "CANCELED", "REJECTED", "FAILED", "BLOCKED", "DRY_RUN", "EXPIRED"}
                ]
            )
        return self.order_count

    def _phase5_fill_rows(self, *, limit=None, offset=0):
        self.fill_row_calls.append({"limit": limit, "offset": offset})
        rows = self.fills[offset:]
        return rows[:limit] if limit is not None else rows

    def _phase5_fill_count(self):
        self.fill_count_calls += 1
        return self.fill_count


class _ScopedOrderRepo:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.scoped_calls: list[dict] = []
        self.count_calls: list[dict] = []

    def list_orders_for_scope(
        self,
        *,
        product_type: str,
        margin_mode: str,
        symbols: tuple[str, ...] = (),
        limit: int | None = None,
        offset: int = 0,
        open_only: bool = False,
    ) -> list[dict]:
        self.scoped_calls.append(
            {
                "product_type": product_type,
                "margin_mode": margin_mode,
                "symbols": symbols,
                "limit": limit,
                "offset": offset,
                "open_only": open_only,
            }
        )
        return self.rows

    def list_orders(self, *, limit=None, offset=0):
        raise AssertionError("scoped dashboard order reads must not fall back to global list_orders")

    def count_orders_for_scope(
        self,
        *,
        product_type: str,
        margin_mode: str,
        symbols: tuple[str, ...] = (),
        open_only: bool = False,
    ) -> int:
        self.count_calls.append(
            {
                "product_type": product_type,
                "margin_mode": margin_mode,
                "symbols": symbols,
                "open_only": open_only,
            }
        )
        return len(self.rows)

    def count_orders(self):
        raise AssertionError("scoped dashboard order counts must not fall back to global count_orders")


class _ScopedFillRepo:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.scoped_calls: list[dict] = []
        self.count_calls: list[dict] = []

    def recent_fills_for_scope(
        self,
        *,
        product_type: str,
        margin_mode: str,
        symbols: tuple[str, ...] = (),
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict]:
        self.scoped_calls.append(
            {
                "product_type": product_type,
                "margin_mode": margin_mode,
                "symbols": symbols,
                "limit": limit,
                "offset": offset,
            }
        )
        return self.rows

    def count_fills_for_scope(
        self,
        *,
        product_type: str,
        margin_mode: str,
        symbols: tuple[str, ...] = (),
    ) -> int:
        self.count_calls.append(
            {
                "product_type": product_type,
                "margin_mode": margin_mode,
                "symbols": symbols,
            }
        )
        return len(self.rows)

    def recent_fills(self, *, limit, offset=0):
        raise AssertionError("scoped dashboard fill reads must not fall back to global recent_fills")

    def count_fills(self):
        raise AssertionError("scoped dashboard fill counts must not fall back to global count_fills")


class _DetailOrderRepo:
    def __init__(self, row: dict | None) -> None:
        self.row = row

    def get_order_by_client_order_id(self, client_order_id: str):
        return self.row


class _DetailFillRepo:
    def __init__(self, row: dict | None) -> None:
        self.row = row

    def get_fill(self, fill_id: str):
        return self.row


class _DetailOwner:
    def __init__(self, *, order: dict | None = None, fill: dict | None = None) -> None:
        self.state_scope = RuntimeStateScope(
            product_type="derivatives",
            margin_mode="cross",
            allowed_symbols=("BTC-USDT-SWAP",),
            default_symbol="BTC-USDT-SWAP",
        )
        self.runtime = SimpleNamespace(
            execution_order_repo=_DetailOrderRepo(order),
            execution_fill_repo_v2=_DetailFillRepo(fill),
        )

    def _phase5_control_plane_enabled(self):
        return True

    def _fill_outcome_map(self):
        return {}


def test_phase5_order_rows_uses_scope_aware_repo_reader() -> None:
    repo = _ScopedOrderRepo(rows=[{"order_id": "order-scope"}])
    service = object.__new__(OperatorQueryService)
    service.runtime = SimpleNamespace(
        settings=SimpleNamespace(operator_control_plane_execution_ledger_enabled=True),
        execution_order_repo=repo,
        execution_fill_repo_v2=object(),
        ledger_account_repo=object(),
        ledger_entry_repo=object(),
    )
    service.state_scope = SimpleNamespace(
        product_type="derivatives",
        margin_mode="cross",
        allowed_symbols=("BTC-USDT-SWAP",),
    )

    rows = service._phase5_order_rows(limit=1, offset=2)

    assert rows == [{"order_id": "order-scope"}]
    assert repo.scoped_calls == [
        {
            "product_type": "derivatives",
            "margin_mode": "cross",
            "symbols": ("BTC-USDT-SWAP",),
            "limit": 1,
            "offset": 2,
            "open_only": False,
        }
    ]


def test_phase5_order_count_uses_scope_aware_repo_counter() -> None:
    repo = _ScopedOrderRepo(rows=[{"order_id": "order-scope"}])
    service = object.__new__(OperatorQueryService)
    service.runtime = SimpleNamespace(
        settings=SimpleNamespace(operator_control_plane_execution_ledger_enabled=True),
        execution_order_repo=repo,
        execution_fill_repo_v2=object(),
        ledger_account_repo=object(),
        ledger_entry_repo=object(),
    )
    service.state_scope = SimpleNamespace(
        product_type="derivatives",
        margin_mode="cross",
        allowed_symbols=("BTC-USDT-SWAP",),
    )

    count = service._phase5_order_count(open_only=True)

    assert count == 1
    assert repo.count_calls == [
        {
            "product_type": "derivatives",
            "margin_mode": "cross",
            "symbols": ("BTC-USDT-SWAP",),
            "open_only": True,
        }
    ]


def test_phase5_fill_rows_and_count_use_scope_aware_repo_methods() -> None:
    repo = _ScopedFillRepo(rows=[{"fill_id": "fill-scope"}])
    service = object.__new__(OperatorQueryService)
    service.runtime = SimpleNamespace(
        settings=SimpleNamespace(operator_control_plane_execution_ledger_enabled=True),
        execution_order_repo=object(),
        execution_fill_repo_v2=repo,
        ledger_account_repo=object(),
        ledger_entry_repo=object(),
    )
    service.state_scope = SimpleNamespace(
        product_type="derivatives",
        margin_mode="cross",
        allowed_symbols=("BTC-USDT-SWAP",),
    )

    rows = service._phase5_fill_rows(limit=3, offset=2)
    count = service._phase5_fill_count()

    assert rows == [{"fill_id": "fill-scope"}]
    assert count == 1
    assert repo.scoped_calls == [
        {
            "product_type": "derivatives",
            "margin_mode": "cross",
            "symbols": ("BTC-USDT-SWAP",),
            "limit": 3,
            "offset": 2,
        }
    ]
    assert repo.count_calls == [
        {
            "product_type": "derivatives",
            "margin_mode": "cross",
            "symbols": ("BTC-USDT-SWAP",),
        }
    ]


def test_phase5_order_detail_rejects_order_outside_current_scope() -> None:
    owner = _DetailOwner(
        order={
            "order_id": "order-spot",
            "client_order_id": "client-spot",
            "product_type": "spot",
            "margin_mode": "cash",
            "symbol": "BTC-USDT",
            "state": "SUBMITTED",
        }
    )

    try:
        AccountQueryFacade(owner).order_detail("client-spot")
    except KeyError as exc:
        assert str(exc).strip("'") == "order_not_found:client-spot"
    else:
        raise AssertionError("cross-scope phase5 order detail must be hidden")


def test_phase5_fill_detail_rejects_fill_outside_current_scope() -> None:
    owner = _DetailOwner(
        fill={
            "fill_id": "fill-spot",
            "symbol": "BTC-USDT",
            "raw_payload": {"product_type": "spot", "margin_mode": "cash"},
        }
    )

    try:
        AccountQueryFacade(owner).fill_detail("fill-spot")
    except KeyError as exc:
        assert str(exc).strip("'") == "fill_not_found:fill-spot"
    else:
        raise AssertionError("cross-scope phase5 fill detail must be hidden")


def test_phase5_account_open_orders_uses_scoped_open_order_reader() -> None:
    owner = _FakeOwner(
        orders=[
            {
                "order_id": "order-open",
                "client_order_id": "client-open",
                "state": "SUBMITTED",
                "product_type": "derivatives",
                "margin_mode": "cross",
                "symbol": "BTC-USDT-SWAP",
            },
            {
                "order_id": "order-filled",
                "client_order_id": "client-filled",
                "state": "FILLED",
                "product_type": "derivatives",
                "margin_mode": "cross",
                "symbol": "BTC-USDT-SWAP",
            },
        ]
    )

    payload = AccountQueryFacade(owner).account_open_orders()

    assert owner.order_row_calls == [{"limit": None, "offset": 0, "open_only": True}]
    assert [item["order_id"] for item in payload["local_open_orders"]] == ["order-open"]


def test_execution_latest_exposes_terminal_no_fill_explanation_for_blocked_directional_decision() -> None:
    owner = _FakeOwner(
        orders=[
            {
                "order_id": "order-close-long",
                "client_order_id": "client-close-long",
                "decision_id": "decision-terminal-no-fill",
                "state": "BLOCKED",
                "position_intent": "close_long",
                "execution_style": "taker",
                "source_system": "local_order_manager",
                "updated_at": "2026-04-27T09:49:54Z",
                "product_type": "derivatives",
                "margin_mode": "cross",
                "symbol": "BTC-USDT-SWAP",
            },
            {
                "order_id": "order-open-short",
                "client_order_id": "client-open-short",
                "decision_id": "decision-terminal-no-fill",
                "state": "BLOCKED",
                "position_intent": "open_short",
                "execution_style": "semantic_dup_snapshot_blocked",
                "source_system": "semantic_dup_snapshot_blocked",
                "updated_at": "2026-04-27T09:49:54Z",
                "product_type": "derivatives",
                "margin_mode": "cross",
                "symbol": "BTC-USDT-SWAP",
            },
        ]
    )

    payload = AccountQueryFacade(owner).build_execution_latest()

    explanation = payload["terminal_no_fill_explanation"]
    assert explanation["classification"] == "terminal_order_surface_without_fill"
    assert explanation["reason"] == "terminal_order_blocked_before_fill"
    assert explanation["terminal_states"] == ["BLOCKED"]
    assert explanation["terminal_position_intents"] == ["close_long", "open_short"]
    assert explanation["terminal_execution_styles"] == ["taker", "semantic_dup_snapshot_blocked"]
    assert explanation["execution_order_count"] == 2
    assert explanation["fill_surface_present"] is False


def test_execution_latest_does_not_mark_terminal_no_fill_when_decision_has_fill() -> None:
    owner = _FakeOwner(
        orders=[
            {
                "order_id": "order-filled",
                "client_order_id": "client-filled",
                "decision_id": "decision-filled",
                "state": "FILLED",
                "position_intent": "open_long",
                "product_type": "derivatives",
                "margin_mode": "cross",
                "symbol": "BTC-USDT-SWAP",
            }
        ],
        fills=[
            {
                "fill_id": "fill-filled",
                "decision_id": "decision-filled",
                "order_id": "order-filled",
                "product_type": "derivatives",
                "margin_mode": "cross",
                "symbol": "BTC-USDT-SWAP",
            }
        ],
    )

    payload = AccountQueryFacade(owner).build_execution_latest()

    assert payload["terminal_no_fill_explanation"] is None
    assert owner.order_row_calls == []
    assert owner.fill_row_calls == []


def test_execution_latest_marks_historical_order_and_fill_outside_current_runtime() -> None:
    owner = _FakeOwner(
        orders=[
            {
                "order_id": "order-stale",
                "client_order_id": "client-stale",
                "decision_id": "decision-stale",
                "state": "FAILED",
                "updated_at": "2026-04-27T09:49:54Z",
                "product_type": "derivatives",
                "margin_mode": "cross",
                "symbol": "BTC-USDT-SWAP",
            }
        ],
        fills=[
            {
                "fill_id": "fill-stale",
                "decision_id": "decision-stale",
                "order_id": "order-stale",
                "ingestion_timestamp": "2026-04-27T09:50:00Z",
                "product_type": "derivatives",
                "margin_mode": "cross",
                "symbol": "BTC-USDT-SWAP",
            }
        ],
        current_runtime_timestamps=set(),
    )

    payload = AccountQueryFacade(owner).build_execution_latest()

    assert payload["latest_order_is_current_runtime"] is False
    assert payload["latest_fill_is_current_runtime"] is False


def test_execution_latest_dashboard_uses_summary_recovery_and_defers_errors() -> None:
    owner = _FakeOwner(
        orders=[
            {
                "order_id": "order-dashboard",
                "client_order_id": "client-dashboard",
                "decision_id": "decision-dashboard",
                "state": "SUBMITTING",
                "updated_at": "2026-04-27T09:49:54Z",
                "product_type": "derivatives",
                "margin_mode": "cross",
                "symbol": "BTC-USDT-SWAP",
            }
        ],
        readiness_raises=True,
        reconciliation_raises=True,
    )

    payload = AccountQueryFacade(owner).execution_latest_dashboard()

    assert payload["dashboard_summary_only"] is True
    assert payload["recent_failures"] == []
    assert payload["recent_failures_deferred"] is True
    assert payload["latest_reconciliation"] is None
    assert payload["execution"]["truth_source"] == "system_mode_dashboard_summary"
    assert payload["execution"]["ready"] is True
    assert "execution_adapter.readiness" in payload["deferred_sections"]
    assert "latest_reconciliation" in payload["deferred_sections"]
    assert payload["recovery"]["recovery_state"] == "dashboard_normal_operation"
    assert payload["mode"]["execution_route"] == "derivatives_live_dashboard"
    assert payload["truth_source"]["summary"] == "execution_latest_dashboard_summary"
    assert owner.dashboard_recovery_calls == 1
    assert owner.dashboard_mode_calls == 1
    assert owner.execution_error_calls == 0
    assert owner.execution_readiness_calls == 0
    assert owner.reconciliation_calls == 0


def test_phase5_orders_recent_uses_bounded_page_fetch() -> None:
    owner = _FakeOwner(
        orders=[
            {
                "order_id": f"order_{index}",
                "client_order_id": f"client_{index}",
                "state": "FILLED",
                "product_type": "derivatives",
                "margin_mode": "cross",
                "symbol": "BTC-USDT-SWAP",
            }
            for index in range(8)
        ],
        order_count=123,
    )

    payload = AccountQueryFacade(owner).build_orders_recent(limit=2, offset=3)

    assert owner.order_row_calls == [{"limit": 2, "offset": 3}]
    assert owner.order_count_calls == 1
    assert [item["order_id"] for item in payload["orders"]] == ["order_3", "order_4"]
    assert payload["total_available"] == 123
    assert payload["has_more"] is True


def test_phase5_fills_recent_uses_bounded_page_fetch() -> None:
    owner = _FakeOwner(
        orders=[],
        fills=[
            {
                "fill_id": f"fill_{index}",
                "order_id": f"order_{index}",
                "product_type": "derivatives",
                "margin_mode": "cross",
                "symbol": "BTC-USDT-SWAP",
            }
            for index in range(8)
        ],
        fill_count=88,
    )

    payload = AccountQueryFacade(owner).build_fills_recent(limit=3, offset=2)

    assert owner.fill_row_calls == [{"limit": 3, "offset": 2}]
    assert owner.fill_count_calls == 1
    assert [item["fill_id"] for item in payload["fills"]] == ["fill_2", "fill_3", "fill_4"]
    assert payload["total_available"] == 88
    assert payload["has_more"] is True
