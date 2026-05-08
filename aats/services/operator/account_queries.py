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


_TERMINAL_NO_FILL_STATES = {"FAILED", "REJECTED", "CANCELED", "BLOCKED", "EXPIRED", "DRY_RUN"}
_CREATED_OR_SUBMITTING_STATES = {"CREATED", "SUBMITTING"}


def _compact_unique(values: list[Any], *, limit: int = 8) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= limit:
            break
    return result


def _terminal_no_fill_reason_from_states(states: list[str]) -> str:
    normalized = {state.upper() for state in states}
    if "BLOCKED" in normalized:
        return "terminal_order_blocked_before_fill"
    if normalized & {"REJECTED", "FAILED"}:
        return "terminal_order_failed_or_rejected_before_fill"
    if "CANCELED" in normalized:
        return "terminal_order_canceled_before_fill"
    if "EXPIRED" in normalized:
        return "terminal_order_expired_before_fill"
    if "DRY_RUN" in normalized:
        return "terminal_order_dry_run_no_fill_expected"
    return "terminal_order_surface_without_fill"


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

    def account_state_dashboard(self) -> dict[str, Any]:
        return self.build_account_state_dashboard()

    def build_account_state_dashboard(self) -> dict[str, Any]:
        r = parallel_fetch(
            {
                "status": self.owner.account_service_status,
                "recovery": self.owner.recovery_view_dashboard,
                "derivatives_live_guard": self.owner.derivatives_live_guard,
                "margin_buffer_risk": self.owner.margin_buffer_risk,
            }
        )
        status = r["status"] if isinstance(r.get("status"), dict) else {}
        recovery = r["recovery"] if isinstance(r.get("recovery"), dict) else {}
        position_mode_contract = status.get("position_mode_contract") or derivatives_position_mode_contract(
            settings=self.owner.runtime.settings,
            snapshot=None,
        )
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
            "account_configuration": status.get("account_configuration"),
            "fee_schedule": status.get("fee_schedule"),
            "risk_snapshot": status.get("risk_snapshot"),
            "system_status_items": status.get("system_status_items", []),
            "tracked_instrument_rules": [],
            "recent_bills_count": status.get("recent_bills_count", 0),
            "last_bills_error": status.get("last_bills_error"),
            "exchange_funding_fee_summary": None,
            "persisted_funding_fee_summary": {
                "available": False,
                "deferred_from_dashboard_summary": True,
                "detail_endpoint": "/account/recent-funding-fees",
            },
            "local_position_margin_summary": {},
            "exchange_position_margin_summary": {},
            "margin_reconciliation": None,
            "margin_buffer_overview": r["margin_buffer_risk"],
            "derivatives_live_guard": r["derivatives_live_guard"],
            "blockers": status.get("blockers", []),
            "current_blocking_reason": next(iter(status.get("blockers", []) or []), None),
            "detail": status.get("detail"),
            "recovery": {
                "recovery_state": recovery.get("recovery_state"),
                "review_required": recovery.get("review_required", False),
                "rebaseline_available": recovery.get("rebaseline_available", False),
                "resume_eligible": recovery.get("resume_eligible", False),
                "safe_to_trade": recovery.get("safe_to_trade", False),
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
                "order_truth_source": (
                    "execution_order_repo" if self.owner._phase5_control_plane_enabled() else "execution_repo"
                ),
                "fill_truth_source": (
                    "execution_fill_repo_v2" if self.owner._phase5_control_plane_enabled() else "execution_repo"
                ),
                "balance_truth_source": (
                    "ledger_accounts" if self.owner._phase5_control_plane_enabled() else "portfolio_snapshot"
                ),
                "legacy_execution_views_authoritative": not self.owner._phase5_control_plane_enabled(),
            },
            "dashboard_summary_only": True,
            "truth_source": "account_status_plus_recovery_dashboard_summary",
            "deferred_sections": [
                "local_position_margin_summary",
                "exchange_position_margin_summary",
                "margin_reconciliation",
                "persisted_funding_fee_summary",
                "tracked_instrument_rules",
            ],
        }

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
            orders = self.owner._phase5_order_rows(limit=limit, offset=offset)
            count_orders = getattr(self.owner.runtime.execution_order_repo, "count_orders", None)
            total_available = int(count_orders()) if callable(count_orders) else offset + len(orders)
            return {
                "orders": [self.owner._execution_record_payload(order) for order in orders],
                "limit": limit,
                "offset": offset,
                "total_available": total_available,
                "has_more": offset + len(orders) < total_available,
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
            fills = self.owner._phase5_fill_rows(limit=limit, offset=offset)
            count_fills = getattr(self.owner.runtime.execution_fill_repo_v2, "count_fills", None)
            total_available = int(count_fills()) if callable(count_fills) else offset + len(fills)
            return {
                "fills": [self.owner._execution_record_payload(fill) for fill in fills],
                "limit": limit,
                "offset": offset,
                "total_available": total_available,
                "has_more": offset + len(fills) < total_available,
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

    def execution_latest_dashboard(self) -> dict[str, Any]:
        return self.build_execution_latest(dashboard_summary_only=True)

    def build_execution_latest(self, *, dashboard_summary_only: bool = False) -> dict[str, Any]:
        latest_order = self.owner.latest_order()
        latest_fill = self.owner.latest_fill()
        latest_order_payload = self.owner._execution_record_payload(latest_order) if latest_order is not None else None
        latest_fill_payload = self.owner._execution_record_payload(latest_fill) if latest_fill is not None else None
        latest_reconciliation = self.owner._latest_scoped_reconciliation()
        recovery = self.owner.recovery_view_dashboard() if dashboard_summary_only else self.owner.recovery_view()
        terminal_no_fill_explanation = self._latest_terminal_no_fill_explanation(latest_order=latest_order)
        payload = {
            "mode": self.owner.system_mode_dashboard() if dashboard_summary_only else self.owner.system_mode(),
            "execution": self.owner.runtime.execution_adapter.readiness(),
            "latest_order": latest_order_payload,
            "latest_fill": latest_fill_payload,
            "latest_order_is_current_runtime": self._payload_is_current_runtime(
                latest_order_payload,
                keys=("updated_at", "last_update_ts", "created_at"),
            ),
            "latest_fill_is_current_runtime": self._payload_is_current_runtime(
                latest_fill_payload,
                keys=("ingestion_timestamp", "ingestion_ts", "created_at", "exchange_ts"),
            ),
            "latest_reconciliation": latest_reconciliation.model_dump(mode="json") if latest_reconciliation is not None else None,
            "terminal_no_fill_explanation": terminal_no_fill_explanation,
            "recent_failures": [] if dashboard_summary_only else self.owner.execution_errors()["errors"],
            "recovery": recovery,
            "truth_source": {
                "orders": "execution_order_repo" if self.owner._phase5_control_plane_enabled() else "execution_repo",
                "fills": "execution_fill_repo_v2" if self.owner._phase5_control_plane_enabled() else "execution_repo",
                "balances": "ledger_accounts" if self.owner._phase5_control_plane_enabled() else "portfolio_snapshot",
            },
        }
        if dashboard_summary_only:
            payload["dashboard_summary_only"] = True
            payload["recent_failures_deferred"] = True
            payload["deferred_sections"] = ["recent_failures"]
            payload["truth_source"]["summary"] = "execution_latest_dashboard_summary"
        return payload

    def _payload_is_current_runtime(
        self,
        payload: dict[str, Any] | None,
        *,
        keys: tuple[str, ...],
    ) -> bool | None:
        if payload is None:
            return None
        timestamp = next((payload.get(key) for key in keys if payload.get(key) is not None), None)
        if timestamp is None:
            return None
        checker = getattr(self.owner, "_is_current_runtime_timestamp", None)
        if not callable(checker):
            return True
        return bool(checker(timestamp))

    def _latest_terminal_no_fill_explanation(self, *, latest_order: Any | None) -> dict[str, Any] | None:
        if latest_order is None:
            return None
        latest_order_payload = self.owner._execution_record_payload(latest_order)
        decision_id = str(latest_order_payload.get("decision_id") or "").strip()
        if not decision_id:
            return None

        order_payloads = self._recent_order_payloads_for_decision(decision_id)
        if not order_payloads:
            order_payloads = [latest_order_payload]
        states = [
            str(payload.get("state") or payload.get("status") or "").strip().upper()
            for payload in order_payloads
        ]
        if not states or any(state in _CREATED_OR_SUBMITTING_STATES for state in states):
            return None
        if any(state not in _TERMINAL_NO_FILL_STATES for state in states):
            return None
        if self._fill_payloads_for_order_group(order_payloads, decision_id=decision_id):
            return None

        return {
            "classification": "terminal_order_surface_without_fill",
            "reason": _terminal_no_fill_reason_from_states(states),
            "decision_id": decision_id,
            "latest_order_id": latest_order_payload.get("order_id") or latest_order_payload.get("client_order_id"),
            "latest_order_updated_at": (
                latest_order_payload.get("updated_at")
                or latest_order_payload.get("last_update_ts")
                or latest_order_payload.get("created_at")
            ),
            "terminal_states": _compact_unique(states),
            "execution_order_terminal_states": _compact_unique(states),
            "order_state_terminal_statuses": [],
            "terminal_source_systems": _compact_unique([payload.get("source_system") for payload in order_payloads]),
            "terminal_execution_styles": _compact_unique([payload.get("execution_style") for payload in order_payloads]),
            "terminal_position_intents": _compact_unique([payload.get("position_intent") for payload in order_payloads]),
            "execution_order_count": len(order_payloads),
            "order_state_count": 0,
            "terminal_execution_order_count": len(order_payloads),
            "terminal_order_state_count": 0,
            "created_or_submitting_execution_order_count": 0,
            "created_or_submitting_order_state_count": 0,
            "fill_surface_present": False,
            "operator_summary": "all_visible_order_surfaces_are_terminal_no_fill",
            "truth_source": (
                "execution_order_repo"
                if self.owner._phase5_control_plane_enabled()
                else "execution_repo_order_states"
            ),
        }

    def _recent_order_payloads_for_decision(self, decision_id: str) -> list[dict[str, Any]]:
        if self.owner._phase5_control_plane_enabled():
            rows = self.owner._phase5_order_rows(limit=100)
        else:
            rows = self.owner._scoped_order_states()
        payloads: list[dict[str, Any]] = []
        for row in rows:
            payload = self.owner._execution_record_payload(row)
            if str(payload.get("decision_id") or "").strip() == decision_id:
                payloads.append(payload)
        return payloads

    def _recent_fill_payloads_for_decision(self, decision_id: str) -> list[dict[str, Any]]:
        if self.owner._phase5_control_plane_enabled():
            rows = self.owner._phase5_fill_rows(limit=500)
        else:
            rows = self.owner._scoped_fills()
        payloads: list[dict[str, Any]] = []
        for row in rows:
            payload = self.owner._execution_record_payload(row)
            if str(payload.get("decision_id") or "").strip() == decision_id:
                payloads.append(payload)
        return payloads

    def _fill_payloads_for_order_group(
        self,
        order_payloads: list[dict[str, Any]],
        *,
        decision_id: str,
    ) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        if self.owner._phase5_control_plane_enabled():
            fill_repo = getattr(self.owner.runtime, "execution_fill_repo_v2", None)
            fills_for_order = getattr(fill_repo, "fills_for_order", None)
            if callable(fills_for_order):
                for order in order_payloads:
                    order_id = str(order.get("order_id") or "").strip()
                    if not order_id:
                        continue
                    payloads.extend(self.owner._execution_record_payload(row) for row in fills_for_order(order_id))
                return [
                    payload
                    for payload in payloads
                    if str(payload.get("decision_id") or "").strip() == decision_id
                ]
        else:
            fills_for_order = getattr(self.owner, "_scoped_fills_for_order", None)
            if callable(fills_for_order):
                for order in order_payloads:
                    client_order_id = str(order.get("client_order_id") or "").strip()
                    if not client_order_id:
                        continue
                    payloads.extend(self.owner._execution_record_payload(row) for row in fills_for_order(client_order_id))
                return [
                    payload
                    for payload in payloads
                    if str(payload.get("decision_id") or "").strip() == decision_id
                ]
        return self._recent_fill_payloads_for_decision(decision_id)

    def _recent_persisted_funding_fee_summary(self, *, limit: int = 200) -> dict[str, Any]:
        return self.owner._recent_persisted_funding_fee_summary(limit=limit)
