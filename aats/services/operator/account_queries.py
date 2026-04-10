from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aats.services.execution_engine.okx_account import derivatives_position_mode_contract
from aats.services.operator._parallel import parallel_fetch
from aats.services.runtime_scope import (
    funding_fee_records_for_scope,
    order_states_for_scope,
    snapshots_for_scope,
)

if TYPE_CHECKING:
    from aats.services.operator.query_service import OperatorQueryService


class AccountQueryFacade:
    def __init__(self, owner: "OperatorQueryService") -> None:
        self.owner = owner

    def portfolio_latest(self) -> dict[str, Any]:
        cache_key = f"portfolio_latest:{self.owner._scope_cache_fragment()}"
        return self.owner._cached_ttl(cache_key, 10, self.build_portfolio_latest)

    def build_portfolio_latest(self) -> dict[str, Any]:
        snapshot = self.owner._latest_scoped_snapshot()
        return {
            "portfolio": snapshot.model_dump(mode="json") if snapshot is not None else None,
            "latest_update_timestamp": snapshot.snapshot_ts if snapshot is not None else None,
            "truth_source": "ledger_backed_snapshot" if self.owner._phase5_control_plane_enabled() else "legacy_portfolio_snapshot",
        }

    def portfolio_history(self, *, limit: int = 20) -> dict[str, Any]:
        history = snapshots_for_scope(self.owner.runtime.portfolio_repo, self.owner.state_scope, limit=limit)
        return {
            "snapshots": [snapshot.model_dump(mode="json") for snapshot in history],
            "total_available": len(snapshots_for_scope(self.owner.runtime.portfolio_repo, self.owner.state_scope)),
        }

    def balances(self) -> dict[str, Any]:
        snapshot = self.owner._latest_scoped_snapshot()
        exchange = self.owner.latest_exchange_snapshot()
        return {
            "local_balances": self.owner._phase5_balance_view(),
            "exchange_balances": [item.model_dump(mode="json") for item in exchange.balances] if exchange is not None else [],
            "truth_source": "ledger_accounts" if self.owner._phase5_control_plane_enabled() else "legacy_portfolio_snapshot",
            "snapshot_balances": snapshot.balances if snapshot is not None else {},
        }

    def positions(self) -> dict[str, Any]:
        snapshot = self.owner._latest_scoped_snapshot()
        exchange = self.owner.latest_exchange_snapshot()
        reconciliation = self.owner._latest_scoped_reconciliation()
        local_instrument_positions = self.owner._aggregate_local_positions(snapshot)
        exchange_instrument_positions = self.owner._aggregate_exchange_positions(exchange)
        return {
            "local_positions": [item.model_dump(mode="json") for item in snapshot.positions] if snapshot is not None else [],
            "local_instrument_positions": local_instrument_positions,
            "local_net_positions": local_instrument_positions,
            "local_margin_summary": self.owner._local_position_margin_summary(snapshot),
            "exchange_positions": [item.model_dump(mode="json") for item in exchange.positions] if exchange is not None else [],
            "exchange_instrument_positions": exchange_instrument_positions,
            "exchange_net_positions": exchange_instrument_positions,
            "exchange_margin_summary": self.owner._exchange_position_margin_summary(exchange),
            "margin_reconciliation": self.owner._margin_reconciliation_summary(reconciliation),
            "margin_buffer_overview": self.owner.margin_buffer_risk(),
            "truth_source": "ledger_backed_snapshot" if self.owner._phase5_control_plane_enabled() else "legacy_portfolio_snapshot",
        }

    def account_state(self) -> dict[str, Any]:
        # 外层 query_service.account_state() 已经用同 key 做了 _cached_ttl(35s)，
        # 这里不能再用同 key 做 _cached_ttl，否则 singleflight 的 inflight Event
        # 会自死锁（内层等外层完成，外层等内层返回 → 60s 超时）。
        return self.build_account_state()

    def build_account_state(self) -> dict[str, Any]:
        # ── Phase 1：并行获取所有互相独立的子查询 ──
        phase1_queries: dict[str, Any] = {
            "status": self.owner.account_service_status,
            "snapshot": self.owner.latest_exchange_snapshot,
            "local_snapshot": self.owner._latest_scoped_snapshot,
            "recovery": self.owner.recovery_view,
            "reconciliation": self.owner._latest_scoped_reconciliation,
            "derivatives_live_guard": self.owner.derivatives_live_guard,
            "persisted_funding_fee_summary": lambda: self._recent_persisted_funding_fee_summary(limit=200),
            "margin_buffer_risk": self.owner.margin_buffer_risk,
        }
        if hasattr(self.owner.runtime.account_service, "recent_funding_fee_summary"):
            phase1_queries["exchange_funding_fee_summary"] = (
                lambda: self.owner.runtime.account_service.recent_funding_fee_summary(
                    symbol=self.owner.runtime.settings.default_symbol
                )
            )
        r = parallel_fetch(phase1_queries)

        status = r["status"]
        snapshot = r["snapshot"]
        local_snapshot = r["local_snapshot"]
        recovery = r["recovery"]
        reconciliation = r["reconciliation"]
        tracked_symbols = set(self.owner.runtime.settings.allowed_symbols) | {self.owner.runtime.settings.default_symbol}
        exchange_funding_fee_summary = r.get("exchange_funding_fee_summary")
        position_mode_contract = status.get("position_mode_contract") or derivatives_position_mode_contract(
            settings=self.owner.runtime.settings,
            snapshot=snapshot,
        )
        derivatives_live_guard = r["derivatives_live_guard"]
        return {
            "backend": self.owner.runtime.settings.account_backend,
            "read_enabled": self.owner.runtime.settings.account_read_enabled,
            "last_refresh_timestamp": status.get("last_update_ts"),
            "fresh": status.get("fresh", False),
            "connected": status.get("connected", False),
            "ready": status.get("ready", False),
            "last_error": status.get("last_error"),
            "private_ws_connected": status.get("private_ws_connected", False),
            "private_ws_last_message_ts": status.get("private_ws_last_message_ts"),
            "private_ws_last_error": status.get("private_ws_last_error"),
            "private_ws_fresh": status.get("private_ws_fresh", False),
            "maker_fee_rate": status.get("maker_fee_rate"),
            "taker_fee_rate": status.get("taker_fee_rate"),
            "fee_rates_source": status.get("fee_rates_source"),
            "position_mode_contract": position_mode_contract,
            "configured_derivatives_position_mode": position_mode_contract.get("configured_derivatives_position_mode"),
            "required_exchange_position_mode": position_mode_contract.get("required_exchange_position_mode"),
            "exchange_position_mode": position_mode_contract.get("exchange_position_mode"),
            "exchange_position_mode_label": position_mode_contract.get("exchange_position_mode_label"),
            "exchange_position_mode_matches_configured": position_mode_contract.get(
                "exchange_position_mode_matches_configured"
            ),
            "position_mode_match_required": position_mode_contract.get("position_mode_match_required"),
            "account_configuration": (
                snapshot.account_configuration.model_dump(mode="json")
                if snapshot is not None and snapshot.account_configuration is not None
                else status.get("account_configuration")
            ),
            "fee_schedule": (
                snapshot.fee_schedule.model_dump(mode="json")
                if snapshot is not None and snapshot.fee_schedule is not None
                else status.get("fee_schedule")
            ),
            "risk_snapshot": (
                snapshot.risk_snapshot.model_dump(mode="json")
                if snapshot is not None and snapshot.risk_snapshot is not None
                else status.get("risk_snapshot")
            ),
            "system_status_items": (
                [item.model_dump(mode="json") for item in snapshot.system_status_items]
                if snapshot is not None
                else status.get("system_status_items", [])
            ),
            "tracked_instrument_rules": (
                [item.model_dump(mode="json") for item in snapshot.instruments if item.symbol in tracked_symbols]
                if snapshot is not None
                else []
            ),
            "recent_bills_count": status.get("recent_bills_count", 0),
            "last_bills_error": status.get("last_bills_error"),
            "exchange_funding_fee_summary": exchange_funding_fee_summary,
            "persisted_funding_fee_summary": r["persisted_funding_fee_summary"],
            "local_position_margin_summary": self.owner._local_position_margin_summary(local_snapshot),
            "exchange_position_margin_summary": self.owner._exchange_position_margin_summary(snapshot),
            "margin_reconciliation": self.owner._margin_reconciliation_summary(reconciliation),
            "margin_buffer_overview": r["margin_buffer_risk"],
            "derivatives_live_guard": derivatives_live_guard,
            "blockers": status.get("blockers", []),
            "current_blocking_reason": next(iter(status.get("blockers", [])), None),
            "detail": status.get("detail"),
            "recovery": {
                "recovery_state": recovery["recovery_state"],
                "review_required": recovery["review_required"],
                "rebaseline_available": recovery["rebaseline_available"],
                "resume_eligible": recovery["resume_eligible"],
                "safe_to_trade": recovery["safe_to_trade"],
            },
            "baseline_takeover": {
                "status": self.owner.runtime.recovery_status.baseline_status,
                "baseline_imported": self.owner.runtime.recovery_status.baseline_imported,
                "baseline_imported_at": self.owner.runtime.recovery_status.baseline_imported_at,
                "baseline_source": self.owner.runtime.recovery_status.baseline_source,
                "requires_operator_review": self.owner.runtime.recovery_status.baseline_requires_operator_review,
                "safe_for_automatic_continuation": self.owner.runtime.recovery_status.baseline_safe_for_automatic_continuation,
                "balance_count": self.owner.runtime.recovery_status.baseline_balance_count,
                "position_count": self.owner.runtime.recovery_status.baseline_position_count,
                "open_order_count": self.owner.runtime.recovery_status.baseline_open_order_count,
                "fill_count": self.owner.runtime.recovery_status.baseline_fill_count,
                "event_ref": self.owner.runtime.recovery_status.baseline_event_ref,
            },
            "control_plane": {
                "phase5_enabled": self.owner._phase5_control_plane_enabled(),
                "order_truth_source": "execution_order_repo" if self.owner._phase5_control_plane_enabled() else "execution_repo",
                "fill_truth_source": "execution_fill_repo_v2" if self.owner._phase5_control_plane_enabled() else "execution_repo",
                "balance_truth_source": "ledger_accounts" if self.owner._phase5_control_plane_enabled() else "portfolio_snapshot",
                "legacy_execution_views_authoritative": not self.owner._phase5_control_plane_enabled(),
            },
        }

    async def account_recent_bills(self, *, limit: int = 50) -> dict[str, Any]:
        # 非 execution 角色不应直接打 OKX REST 拉账单——WS/refresh 已收敛到
        # execution，本地 account_service._latest_recent_bills 由
        # AccountSnapshotCache 跨进程同步填充。只在 execution / monolith 角色
        # 下走 REST 拉取最新账单。
        _role = getattr(self.owner.runtime, "process_role", None)
        _is_execution_role = _role in {None, "monolith", "execution"}
        if _is_execution_role:
            rows = await self.owner.runtime.account_service.recent_bills(
                symbol=self.owner.runtime.settings.default_symbol,
                limit=limit,
            )
        else:
            _default_symbol = self.owner.runtime.settings.default_symbol
            rows = [
                row for row in self.owner.runtime.account_service.latest_recent_bills()
                if _default_symbol is None or str(row.get("instId") or "") == _default_symbol
            ]
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
            lambda: self.build_account_recent_funding_fees(limit=normalized_limit),
        )

    def build_account_recent_funding_fees(self, *, limit: int) -> dict[str, Any]:
        rows = (
            funding_fee_records_for_scope(
                getattr(self.owner.runtime, "funding_fee_repo", None),
                self.owner.state_scope,
                limit=limit,
            )
            if getattr(self.owner.runtime, "funding_fee_repo", None) is not None
            else []
        )
        return {
            "funding_fees": [row.model_dump(mode="json") for row in rows],
            "total_available": len(
                funding_fee_records_for_scope(
                    getattr(self.owner.runtime, "funding_fee_repo", None),
                    self.owner.state_scope,
                )
            ) if getattr(self.owner.runtime, "funding_fee_repo", None) is not None else 0,
            "limit": limit,
            "latest_funding_fee": rows[-1].model_dump(mode="json") if rows else None,
            "summary": self._recent_persisted_funding_fee_summary(limit=max(limit, 200)),
        }

    def account_open_orders(self) -> dict[str, Any]:
        exchange = self.owner.latest_exchange_snapshot()
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
        exchange = self.owner.latest_exchange_snapshot()
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
            lambda: self.build_orders_recent(limit=normalized_limit, offset=normalized_offset),
        )

    def build_orders_recent(self, *, limit: int, offset: int) -> dict[str, Any]:
        if self.owner._phase5_control_plane_enabled():
            all_orders = self.owner._phase5_order_rows()
            orders = all_orders[offset : offset + limit]
            return {
                "orders": [self.owner._execution_record_payload(order) for order in orders],
                "limit": limit,
                "offset": offset,
                "total_available": len(all_orders),
                "has_more": offset + len(orders) < len(all_orders),
                "truth_source": "execution_order_repo",
            }
        all_orders = sorted(
            order_states_for_scope(self.owner.runtime.execution_repo, self.owner.state_scope),
            key=lambda item: (item.last_update_ts or item.created_at, item.client_order_id),
            reverse=True,
        )
        orders = all_orders[offset : offset + limit]
        return {
            "orders": [self.owner._execution_record_payload(order) for order in orders],
            "limit": limit,
            "offset": offset,
            "total_available": len(all_orders),
            "has_more": offset + len(orders) < len(all_orders),
            "truth_source": "execution_repo",
        }

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
            lambda: self.build_fills_recent(limit=normalized_limit, offset=normalized_offset),
        )

    def build_fills_recent(self, *, limit: int, offset: int) -> dict[str, Any]:
        if self.owner._phase5_control_plane_enabled():
            all_fills = self.owner._phase5_fill_rows()
            fills = all_fills[offset : offset + limit]
            return {
                "fills": [self.owner._execution_record_payload(fill) for fill in fills],
                "limit": limit,
                "offset": offset,
                "total_available": len(all_fills),
                "has_more": offset + len(fills) < len(all_fills),
                "truth_source": "execution_fill_repo_v2",
            }
        all_fills = self.owner.recent_fills(limit=limit + offset)
        fills = all_fills[offset : offset + limit]
        total_available = len(self.owner._scoped_fills())
        return {
            "fills": [self.owner._execution_record_payload(fill) for fill in fills],
            "limit": limit,
            "offset": offset,
            "total_available": total_available,
            "has_more": offset + len(fills) < total_available,
            "truth_source": "execution_repo",
        }

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
        # 外层 query_service.execution_latest() 已经用同 key 做了 _cached_ttl(35s)，
        # 这里不能再用同 key 做 _cached_ttl，否则 singleflight 的 inflight Event
        # 会自死锁（内层等外层完成，外层等内层返回 → 60s 超时）。
        return self.build_execution_latest()

    def build_execution_latest(self) -> dict[str, Any]:
        latest_order = self.owner.latest_order()
        latest_fill = self.owner.latest_fill()
        latest_reconciliation = self.owner._latest_scoped_reconciliation()
        recovery = self.owner.recovery_view()
        return {
            "mode": self.owner.system_mode(),
            "execution": self.owner.runtime.execution_adapter.readiness(),
            "latest_order": self.owner._execution_record_payload(latest_order) if latest_order is not None else None,
            "latest_fill": self.owner._execution_record_payload(latest_fill) if latest_fill is not None else None,
            "latest_reconciliation": latest_reconciliation.model_dump(mode="json") if latest_reconciliation is not None else None,
            "recent_failures": self.owner.execution_errors()["errors"],
            "recovery": recovery,
            "truth_source": {
                "orders": "execution_order_repo" if self.owner._phase5_control_plane_enabled() else "execution_repo",
                "fills": "execution_fill_repo_v2" if self.owner._phase5_control_plane_enabled() else "execution_repo",
                "balances": "ledger_accounts" if self.owner._phase5_control_plane_enabled() else "portfolio_snapshot",
            },
        }

    def _recent_persisted_funding_fee_summary(self, *, limit: int = 200) -> dict[str, Any]:
        return self.owner._recent_persisted_funding_fee_summary(limit=limit)
