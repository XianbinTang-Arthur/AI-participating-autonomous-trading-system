from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aats.services.runtime_scope import snapshots_for_scope

if TYPE_CHECKING:
    from aats.services.operator.query_service import OperatorQueryService


class AccountQueryFacade:
    def __init__(self, owner: "OperatorQueryService") -> None:
        self.owner = owner

    def portfolio_latest(self) -> dict[str, Any]:
        cache_key = f"portfolio_latest:{self.owner._scope_cache_fragment()}"
        return self.owner._cached_ttl(cache_key, 10, self.owner._build_portfolio_latest)

    def portfolio_history(self, *, limit: int = 20) -> dict[str, Any]:
        history = snapshots_for_scope(self.owner.runtime.portfolio_repo, self.owner.state_scope, limit=limit)
        return {
            "snapshots": [snapshot.model_dump(mode="json") for snapshot in history],
            "total_available": len(snapshots_for_scope(self.owner.runtime.portfolio_repo, self.owner.state_scope)),
        }

    def balances(self) -> dict[str, Any]:
        snapshot = self.owner._latest_scoped_snapshot()
        exchange = self.owner.runtime.account_service.latest_snapshot()
        return {
            "local_balances": self.owner._phase5_balance_view(),
            "exchange_balances": [item.model_dump(mode="json") for item in exchange.balances] if exchange is not None else [],
            "truth_source": "ledger_accounts" if self.owner._phase5_control_plane_enabled() else "legacy_portfolio_snapshot",
            "snapshot_balances": snapshot.balances if snapshot is not None else {},
        }

    def positions(self) -> dict[str, Any]:
        snapshot = self.owner._latest_scoped_snapshot()
        exchange = self.owner.runtime.account_service.latest_snapshot()
        reconciliation = self.owner._latest_scoped_reconciliation()
        return {
            "local_positions": [item.model_dump(mode="json") for item in snapshot.positions] if snapshot is not None else [],
            "local_net_positions": self.owner._aggregate_local_positions(snapshot),
            "local_margin_summary": self.owner._local_position_margin_summary(snapshot),
            "exchange_positions": [item.model_dump(mode="json") for item in exchange.positions] if exchange is not None else [],
            "exchange_net_positions": self.owner._aggregate_exchange_positions(exchange),
            "exchange_margin_summary": self.owner._exchange_position_margin_summary(exchange),
            "margin_reconciliation": self.owner._margin_reconciliation_summary(reconciliation),
            "margin_buffer_overview": self.owner.margin_buffer_risk(),
            "truth_source": "ledger_backed_snapshot" if self.owner._phase5_control_plane_enabled() else "legacy_portfolio_snapshot",
        }

    def account_state(self) -> dict[str, Any]:
        cache_key = f"account_state:{self.owner._scope_cache_fragment()}"
        return self.owner._cached_ttl(cache_key, 10, self.owner._build_account_state)

    async def account_recent_bills(self, *, limit: int = 50) -> dict[str, Any]:
        rows = await self.owner.runtime.account_service.recent_bills(
            symbol=self.owner.runtime.settings.default_symbol,
            limit=limit,
        )
        return {
            "bills": rows[:limit],
            "total_available": len(rows),
            "limit": limit,
            "latest_bill": rows[0] if rows else None,
        }

    def account_recent_funding_fees(self, *, limit: int = 50) -> dict[str, Any]:
        normalized_limit = max(int(limit), 1)
        cache_key = f"account_recent_funding_fees:{self.owner._scope_cache_fragment()}:{normalized_limit}"
        return self.owner._cached_ttl(
            cache_key,
            10,
            lambda: self.owner._build_account_recent_funding_fees(limit=normalized_limit),
        )

    def account_open_orders(self) -> dict[str, Any]:
        exchange = self.owner.runtime.account_service.latest_snapshot()
        local_open_orders = (
            [self.owner._execution_record_payload(order) for order in self.owner.runtime.execution_order_repo.open_orders()]
            if self.owner._phase5_control_plane_enabled()
            else [order.model_dump(mode="json") for order in self.owner._scoped_open_order_states()]
        )
        return {
            "local_open_orders": local_open_orders,
            "exchange_open_orders": [order.model_dump(mode="json") for order in exchange.open_orders] if exchange is not None else [],
        }

    def account_recent_fills(self) -> dict[str, Any]:
        exchange = self.owner.runtime.account_service.latest_snapshot()
        local_fills = (
            [self.owner._execution_record_payload(fill) for fill in self.owner._phase5_fill_rows(limit=50)]
            if self.owner._phase5_control_plane_enabled()
            else [fill.model_dump(mode="json") for fill in self.owner.recent_fills(limit=50)]
        )
        return {
            "local_fills": local_fills,
            "exchange_fills": [fill.model_dump(mode="json") for fill in exchange.fills[:50]] if exchange is not None else [],
        }

    def orders_open(self) -> dict[str, Any]:
        return self.account_open_orders()

    def orders_recent(self, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        normalized_limit = max(int(limit), 1)
        normalized_offset = max(int(offset), 0)
        cache_key = f"orders_recent:{self.owner._scope_cache_fragment()}:{normalized_limit}:{normalized_offset}"
        return self.owner._cached_ttl(
            cache_key,
            10,
            lambda: self.owner._build_orders_recent(limit=normalized_limit, offset=normalized_offset),
        )

    def order_detail(self, client_order_id: str) -> dict[str, Any]:
        if self.owner._phase5_control_plane_enabled():
            order = self.owner.runtime.execution_order_repo.get_order_by_client_order_id(client_order_id)
            if order is None:
                raise KeyError(f"order_not_found:{client_order_id}")
            fills = self.owner.runtime.execution_fill_repo_v2.fills_for_order(client_order_id)
            control_order = self.owner._control_plane_order_state(client_order_id)
            return {
                "order": self.owner._execution_record_payload(order),
                "fills": [self.owner._execution_record_payload(fill) for fill in fills],
                "stuck_submission_resolution": (
                    self.owner._stuck_submission_resolution(
                        order=control_order,
                        fills=self.owner._control_plane_fills_for_order(client_order_id),
                        exchange_snapshot=self.owner.runtime.account_service.latest_snapshot(),
                    )
                    if control_order is not None
                    else {
                        "eligible": False,
                        "summary": "当前订单缺少可操作的本地执行状态，暂时不能执行卡单恢复。",
                        "reason_code": "phase5_order_state_unavailable",
                    }
                ),
                "truth_source": "execution_order_repo",
            }
        order = next(
            (item for item in self.owner._scoped_order_states() if item.client_order_id == client_order_id),
            None,
        )
        if order is None:
            raise KeyError(f"order_not_found:{client_order_id}")
        fills = self.owner._scoped_fills_for_order(client_order_id)
        return {
            "order": self.owner._execution_record_payload(order),
            "fills": [self.owner._execution_record_payload(fill) for fill in fills],
            "stuck_submission_resolution": self.owner._stuck_submission_resolution(
                order=order,
                fills=fills,
                exchange_snapshot=self.owner.runtime.account_service.latest_snapshot(),
            ),
            "truth_source": "execution_repo",
        }

    def fills_recent(self, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        normalized_limit = max(int(limit), 1)
        normalized_offset = max(int(offset), 0)
        cache_key = f"fills_recent:{self.owner._scope_cache_fragment()}:{normalized_limit}:{normalized_offset}"
        return self.owner._cached_ttl(
            cache_key,
            10,
            lambda: self.owner._build_fills_recent(limit=normalized_limit, offset=normalized_offset),
        )

    def fill_detail(self, fill_id: str) -> dict[str, Any]:
        if self.owner._phase5_control_plane_enabled():
            fill = self.owner.runtime.execution_fill_repo_v2.get_fill(fill_id)
            if fill is None:
                raise KeyError(f"fill_not_found:{fill_id}")
            outcome = self.owner._fill_outcome_map().get(fill_id)
            return {
                "fill": self.owner._execution_record_payload(fill),
                "fill_outcome": None if outcome is None else outcome.model_dump(mode="json"),
                "truth_source": "execution_fill_repo_v2",
            }
        fill = next((item for item in self.owner._scoped_fills() if item.fill_id == fill_id), None)
        if fill is None:
            raise KeyError(f"fill_not_found:{fill_id}")
        outcome = self.owner._fill_outcome_map().get(fill_id)
        return {
            "fill": self.owner._execution_record_payload(fill),
            "fill_outcome": None if outcome is None else outcome.model_dump(mode="json"),
            "truth_source": "execution_repo",
        }

    def execution_latest(self) -> dict[str, Any]:
        cache_key = f"execution_latest:{self.owner._scope_cache_fragment()}"
        return self.owner._cached_ttl(cache_key, 10, self.owner._build_execution_latest)
