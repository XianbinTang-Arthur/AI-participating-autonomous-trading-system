from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from aats.bootstrap.logging import get_logger
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.common import EventEnvelope, utc_now
from aats.schemas.blocker_control import BlockerControlSnapshot
from aats.schemas.decision import AIDecisionIntent, BaselineReference, DecisionOutcome, normalize_ai_operating_mode
from aats.schemas.execution import FillEvent, OrderState, execution_action_from_position_intent
from aats.schemas.portfolio import SleevePnLRecord
from aats.schemas.strategy_runtime import (
    PortfolioAllocationDecision,
    StrategyCoordinatorSnapshot,
    StrategyExecutionBundle,
    StrategySleeveIntent,
)
from aats.schemas.operator import (
    AuthSource,
    BlockerSnapshotRecord,
    OperatorActionRecord,
    OperatorRole,
    OperatorUserRecord,
)
from aats.services.blocker_control import BlockerControlService
from aats.services.blocker_control.actions import BlockerActionService
from aats.services.execution_engine.fill_ordering import fill_processing_sort_key
from aats.services.governance_engine.recovery_posture import RecoveryPostureEvaluator
from aats.services.operator.account_queries import AccountQueryFacade
from aats.services.operator.audit_replay_queries import AuditReplayQueryFacade
from aats.services.operator.blocker_queries import BlockerQueryFacade
from aats.services.operator.accounts import (
    create_operator_user as create_managed_operator_user,
    delete_operator_user as delete_managed_operator_user,
    enabled_admin_count,
    update_operator_user as update_managed_operator_user,
)
from aats.services.operator.report_queries import ReportQueryFacade
from aats.services.operator.recovery_queries import RecoveryQueryFacade
from aats.services.operator.runtime_profiles import readonly_runtime_profile_snapshot
from aats.services.operator.runtime_queries import RuntimeQueryFacade
from aats.services.operator.reconciliation_system_queries import ReconciliationSystemQueryFacade
from aats.services.operator.strategy_profile_queries import StrategyProfileQueryFacade
from aats.services.operator.strategy_queries import StrategyQueryFacade
from aats.services.operator.strategy_profiles import StrategyProfileControlService
from aats.services.ledger.lot_projection import LotBasedProjectionBuilder
from aats.services.portfolio_service.position_keys import build_position_key, position_key_for_snapshot_position
from aats.services.runtime_scope import (
    fill_outcomes_for_scope,
    fills_for_scope,
    funding_fee_records_for_scope,
    latest_reconciliation_for_scope,
    latest_snapshot_for_scope,
    sleeve_pnl_records_for_scope,
    order_states_for_scope,
    snapshots_for_scope,
    runtime_state_scope,
    latest_topic_event_for_scope,
)
from aats.services.strategy_engines.smart_arbitrage.pair_registry import load_pair_definitions
from aats.services.strategy_execution_health import compute_strategy_execution_health

if TYPE_CHECKING:
    from aats.bootstrap.config import ApplicationRuntime


class OperatorQueryService:
    _STUCK_SUBMISSION_STATUSES = {"CREATED", "SUBMITTING"}
    _DECIMAL_EPSILON = Decimal("1e-12")

    def __init__(self, runtime: ApplicationRuntime) -> None:
        self.runtime = runtime
        self.logger = get_logger("aats.operator_api")
        self.recovery_posture = RecoveryPostureEvaluator(runtime)
        self.state_scope = runtime_state_scope(runtime.settings)
        self._cache: dict[str, Any] = {}
        self._ttl_cache: dict[str, tuple[datetime, Any]] = {}
        self.strategy_profiles = StrategyProfileControlService(runtime)
        self.blocker_control_service = BlockerControlService(self)
        self.blocker_action_service = BlockerActionService(self)
        self.runtime_queries = RuntimeQueryFacade(self)
        self.recovery_queries = RecoveryQueryFacade(self)
        self.reconciliation_system_queries = ReconciliationSystemQueryFacade(self)
        self.strategy_profile_queries = StrategyProfileQueryFacade(self)
        self.audit_replay_queries = AuditReplayQueryFacade(self)
        self.account_queries = AccountQueryFacade(self)
        self.strategy_queries = StrategyQueryFacade(self)
        self.report_queries = ReportQueryFacade(self)
        self.blocker_queries = BlockerQueryFacade(self)

    def _cached(self, key: str, loader):
        if key not in self._cache:
            self._cache[key] = loader()
        return self._cache[key]

    def _cached_ttl(self, key: str, ttl_seconds: int, loader):
        now = utc_now()
        cached = self._ttl_cache.get(key)
        if cached is not None:
            expires_at, value = cached
            if expires_at > now:
                return value
        value = loader()
        self._ttl_cache[key] = (now + timedelta(seconds=max(int(ttl_seconds), 1)), value)
        return value

    def _invalidate_cache(self) -> None:
        self._cache.clear()
        self._ttl_cache.clear()

    def _scope_cache_fragment(self) -> str:
        return (
            f"{self.state_scope.product_type}:"
            f"{self.state_scope.margin_mode}:"
            f"{','.join(sorted(self.state_scope.allowed_symbols))}"
        )

    def _scoped_order_states(self):
        return self._cached(
            "scoped_order_states",
            lambda: order_states_for_scope(self.runtime.execution_repo, self.state_scope),
        )

    def _scoped_open_order_states(self):
        return self._cached(
            "scoped_open_order_states",
            lambda: order_states_for_scope(self.runtime.execution_repo, self.state_scope, open_only=True),
        )

    def _scoped_fills(self):
        return self._cached(
            "scoped_fills",
            lambda: fills_for_scope(self.runtime.execution_repo, self.state_scope),
        )

    def _scoped_fill_outcomes(self):
        return self._cached(
            "scoped_fill_outcomes",
            lambda: fill_outcomes_for_scope(self.runtime.fill_outcome_repo, self.state_scope),
        )

    def _refresh_sleeve_pnl_projection(self) -> list[SleevePnLRecord]:
        service = getattr(self.runtime, "sleeve_pnl_projection_service", None)
        if service is None:
            return []
        cache_key = f"refresh_sleeve_pnl_projection:{self._scope_cache_fragment()}"
        return self._cached_ttl(
            cache_key,
            5,
            lambda: service.rebuild_scope(scope=self.state_scope),
        )

    def _scoped_funding_fee_records(self):
        return self._cached(
            "scoped_funding_fee_records",
            lambda: (
                funding_fee_records_for_scope(getattr(self.runtime, "funding_fee_repo", None), self.state_scope)
                if getattr(self.runtime, "funding_fee_repo", None) is not None
                else []
            ),
        )

    def _scoped_sleeve_pnl_records(self):
        self._refresh_sleeve_pnl_projection()
        repo = getattr(self.runtime, "sleeve_pnl_repo", None)
        if repo is None:
            return []
        return self._cached(
            "scoped_sleeve_pnl_records",
            lambda: sleeve_pnl_records_for_scope(repo, self.state_scope),
        )

    def _fill_outcome_map(self) -> dict[str, Any]:
        return self._cached(
            "fill_outcome_map",
            lambda: {item.fill_id: item for item in self._scoped_fill_outcomes()},
        )

    def _latest_scoped_snapshot(self):
        return self._cached(
            "latest_scoped_snapshot",
            lambda: latest_snapshot_for_scope(self.runtime.portfolio_repo, self.state_scope),
        )

    def _phase5_control_plane_enabled(self) -> bool:
        return bool(
            self.runtime.settings.operator_control_plane_execution_ledger_enabled
            and self.runtime.execution_order_repo is not None
            and self.runtime.execution_fill_repo_v2 is not None
            and self.runtime.ledger_account_repo is not None
            and self.runtime.ledger_entry_repo is not None
        )

    def _phase5_order_rows(self, *, limit: int | None = None, offset: int = 0) -> list[dict[str, Any]]:
        if not self._phase5_control_plane_enabled():
            return []
        rows = self.runtime.execution_order_repo.list_orders(limit=limit, offset=offset)
        allowed_symbols = set(self.state_scope.allowed_symbols)
        return [
            row
            for row in rows
            if row.get("product_type") == self.state_scope.product_type
            and row.get("margin_mode") == self.state_scope.margin_mode
            and (not allowed_symbols or row.get("symbol") in allowed_symbols)
        ]

    def _phase5_fill_rows(self, *, limit: int | None = None, offset: int = 0) -> list[dict[str, Any]]:
        if not self._phase5_control_plane_enabled():
            return []
        rows = list(reversed(self.runtime.execution_fill_repo_v2.fills_since(limit=None)))
        allowed_symbols = set(self.state_scope.allowed_symbols)
        scoped = [
            row
            for row in rows
            if row.get("raw_payload", {}).get("product_type", self.state_scope.product_type) == self.state_scope.product_type
            and row.get("raw_payload", {}).get("margin_mode", self.state_scope.margin_mode) == self.state_scope.margin_mode
            and (not allowed_symbols or row.get("symbol") in allowed_symbols)
        ]
        if offset:
            scoped = scoped[offset:]
        if limit is not None:
            scoped = scoped[:limit]
        return scoped

    def _phase5_balance_view(self) -> dict[str, Decimal]:
        if not self._phase5_control_plane_enabled():
            snapshot = self._latest_scoped_snapshot()
            return {} if snapshot is None else dict(snapshot.balances)
        balances: dict[str, Decimal] = {}
        for account in self.runtime.ledger_account_repo.list_accounts(
            account_type="cash_available",
            product_type=self.state_scope.product_type,
            margin_mode=self.state_scope.margin_mode,
        ):
            currency = str(account["currency"])
            amount = self.runtime.ledger_entry_repo.balance_by_account(str(account["account_id"]))
            balances[currency] = amount
        return balances

    def _control_plane_order_state(self, client_order_id: str) -> OrderState | None:
        resolver = getattr(self.runtime.order_manager, "resolve_order_state_for_control", None)
        if callable(resolver):
            return resolver(client_order_id)
        return next(
            (item for item in self._scoped_order_states() if item.client_order_id == client_order_id),
            None,
        )

    def _control_plane_fills_for_order(self, client_order_id: str) -> list[Any]:
        if not self._phase5_control_plane_enabled():
            return self._scoped_fills_for_order(client_order_id)
        rows = self.runtime.execution_fill_repo_v2.fills_for_order(client_order_id)
        hydrated: list[Any] = []
        for row in rows:
            raw_payload = dict(row.get("raw_payload") or {})
            fill_payload = raw_payload.get("fill_event")
            if isinstance(fill_payload, dict):
                hydrated.append(FillEvent.model_validate(fill_payload))
                continue
            hydrated.append(
                FillEvent(
                    fill_id=str(row.get("fill_id") or ""),
                    decision_id=str(row.get("decision_id") or ""),
                    intent_id=str(row.get("intent_id") or ""),
                    client_order_id=str(row.get("client_order_id") or client_order_id),
                    exchange_order_id=str(row.get("venue_order_id") or ""),
                    symbol=str(row.get("symbol") or self.runtime.settings.default_symbol),
                    venue=str(raw_payload.get("venue") or "PAPER"),
                    side=str(row.get("side") or "buy"),
                    fill_qty=row.get("fill_qty"),
                    fill_price=row.get("fill_price"),
                    fee_amount=row.get("fee_amount") or Decimal("0"),
                    fee_currency=row.get("fee_currency"),
                    product_type=raw_payload.get("product_type", self.state_scope.product_type),
                    target_leverage=float(raw_payload.get("target_leverage") or 1.0),
                    margin_mode=raw_payload.get("margin_mode", self.state_scope.margin_mode),
                    exposure_side=str(raw_payload.get("exposure_side") or "flat"),
                    execution_action=raw_payload.get("execution_action"),
                    position_intent=str(raw_payload.get("position_intent") or "open_long"),
                    liquidity_role=str(row.get("liquidity_role") or "taker"),
                    exchange_timestamp=row.get("exchange_ts"),
                    ingestion_timestamp=row.get("ingestion_ts"),
                    order_status_after_fill=raw_payload.get("order_status_after_fill"),
                )
            )
        return hydrated

    def _sync_execution_order_truth(self, order_state: OrderState) -> None:
        shadow_service = self.runtime.phase1_execution_shadow_service
        if shadow_service is not None:
            shadow_service.shadow_order_state(order_state=order_state)
            return
        if self.runtime.execution_order_repo is None:
            return
        existing = self.runtime.execution_order_repo.get_order_by_client_order_id(order_state.client_order_id)
        if existing is None:
            return
        previous_state = str(existing["state"])
        self.runtime.execution_order_repo.update_order_state(
            order_id=str(existing["order_id"]),
            expected_state_version=int(existing["state_version"]),
            next_state=order_state.status,
            venue_order_id=order_state.exchange_order_id,
            last_exchange_ts=order_state.last_exchange_update_ts,
            updated_at=order_state.last_update_ts or order_state.created_at,
            raw_payload=order_state.model_dump(mode="python"),
        )
        if (
            self.runtime.execution_order_history_repo is not None
            and previous_state != order_state.status
        ):
            self.runtime.execution_order_history_repo.append_transition(
                order_id=str(existing["order_id"]),
                from_state=previous_state,
                to_state=order_state.status,
                reason_code="operator_state_sync",
                source="operator_control_plane",
                source_message_id=order_state.intent_id,
                payload=order_state.model_dump(mode="python"),
                created_at=order_state.last_update_ts or order_state.created_at,
            )

    def _current_symbol_position_qty(self, symbol: str) -> Any:
        snapshot = self._latest_scoped_snapshot()
        if snapshot is None:
            return Decimal("0")
        quantity = sum(
            (
                Decimal(str(position.position_qty))
                for position in snapshot.positions
                if position.symbol == symbol
            ),
            start=Decimal("0"),
        )
        if abs(quantity) > self._DECIMAL_EPSILON:
            return quantity
        if snapshot.product_type == "spot" and "-" in symbol:
            base_currency, _quote_currency = symbol.split("-", 1)
            return snapshot.balances.get(base_currency, Decimal("0"))
        return Decimal("0")

    @staticmethod
    def _aggregate_local_positions(snapshot) -> list[dict[str, Any]]:
        if snapshot is None:
            return []
        aggregated: dict[str, dict[str, Any]] = {}
        for position in snapshot.positions:
            entry = aggregated.setdefault(
                position.symbol,
                {
                    "symbol": position.symbol,
                    "position_qty": Decimal("0"),
                    "position_notional": Decimal("0"),
                    "avg_entry_price": Decimal("0"),
                    "unrealized_pnl": Decimal("0"),
                    "product_type": position.product_type,
                    "margin_mode": position.margin_mode,
                    "position_mode": position.position_mode,
                    "target_leverage": position.target_leverage,
                    "legs": [],
                },
            )
            entry["position_qty"] = Decimal(str(entry["position_qty"])) + Decimal(str(position.position_qty))
            entry["position_notional"] = Decimal(str(entry["position_notional"])) + Decimal(str(position.position_notional))
            entry["unrealized_pnl"] = Decimal(str(entry["unrealized_pnl"])) + Decimal(str(position.unrealized_pnl))
            entry["target_leverage"] = max(float(entry["target_leverage"]), float(position.target_leverage))
            entry["legs"].append(
                {
                    "position_key": position_key_for_snapshot_position(position),
                    "pos_side": position.pos_side,
                    "position_qty": position.position_qty,
                    "avg_entry_price": position.avg_entry_price,
                    "unrealized_pnl": position.unrealized_pnl,
                    "settle_currency": position.settle_currency,
                    "margin_mode": position.margin_mode,
                    "margin_allocated": position.margin_allocated,
                    "maintenance_margin": position.maintenance_margin,
                    "margin_ratio": position.margin_ratio,
                    "liquidation_price": position.liquidation_price,
                    "margin_source": position.margin_source,
                }
            )
        rows: list[dict[str, Any]] = []
        for item in aggregated.values():
            total_qty = Decimal(str(item["position_qty"]))
            total_notional = Decimal(str(item["position_notional"]))
            item["avg_entry_price"] = (
                total_notional / total_qty
                if abs(total_qty) > Decimal("1e-12")
                else Decimal("0")
            )
            rows.append(item)
        rows.sort(key=lambda item: str(item["symbol"]))
        return rows

    @staticmethod
    def _aggregate_exchange_positions(exchange) -> list[dict[str, Any]]:
        if exchange is None:
            return []
        aggregated: dict[str, dict[str, Any]] = {}
        for position in exchange.positions:
            quantity = Decimal(str(position.quantity))
            if str(getattr(position, "side", "net") or "net").lower() == "short" and quantity > 0:
                quantity = -quantity
            entry = aggregated.setdefault(
                position.symbol,
                {
                    "symbol": position.symbol,
                    "position_qty": Decimal("0"),
                    "legs": [],
                },
            )
            entry["position_qty"] = Decimal(str(entry["position_qty"])) + quantity
            entry["legs"].append(
                {
                    "side": getattr(position, "side", "net"),
                    "position_qty": quantity,
                    "avg_entry_price": position.average_entry_price,
                    "notional_usd": position.notional_usd,
                    "margin_mode": getattr(position, "margin_mode", None),
                    "margin_allocated": getattr(position, "margin_allocated", None),
                    "maintenance_margin": getattr(position, "maintenance_margin", None),
                    "margin_ratio": getattr(position, "margin_ratio", None),
                    "liquidation_price": getattr(position, "liquidation_price", None),
                    "settle_currency": getattr(position, "settle_currency", None),
                }
            )
        rows = list(aggregated.values())
        rows.sort(key=lambda item: str(item["symbol"]))
        return rows

    @staticmethod
    def _empty_position_margin_summary() -> dict[str, Any]:
        return {
            "available": False,
            "position_count": 0,
            "position_count_by_margin_mode": {},
            "margin_allocated_total": Decimal("0"),
            "maintenance_margin_total": Decimal("0"),
            "positions_with_liquidation_price": 0,
            "settle_currencies": [],
            "margin_source_counts": {},
        }

    def _local_position_margin_summary(self, snapshot) -> dict[str, Any]:
        if snapshot is None:
            return self._empty_position_margin_summary()
        summary = self._empty_position_margin_summary()
        margin_mode_counts: dict[str, int] = {}
        margin_source_counts: dict[str, int] = {}
        settle_currencies: set[str] = set()
        margin_allocated_total = Decimal("0")
        maintenance_margin_total = Decimal("0")
        positions_with_liquidation_price = 0
        for position in snapshot.positions:
            margin_mode = str(position.margin_mode or "unknown")
            margin_mode_counts[margin_mode] = margin_mode_counts.get(margin_mode, 0) + 1
            margin_source = str(getattr(position, "margin_source", "estimated") or "estimated")
            margin_source_counts[margin_source] = margin_source_counts.get(margin_source, 0) + 1
            margin_allocated_total += self._to_decimal(getattr(position, "margin_allocated", None)) or Decimal("0")
            maintenance_margin_total += self._to_decimal(getattr(position, "maintenance_margin", None)) or Decimal("0")
            if getattr(position, "liquidation_price", None) not in {None, ""}:
                positions_with_liquidation_price += 1
            if getattr(position, "settle_currency", None):
                settle_currencies.add(str(position.settle_currency))
        return {
            "available": bool(snapshot.positions),
            "position_count": len(snapshot.positions),
            "position_count_by_margin_mode": margin_mode_counts,
            "margin_allocated_total": margin_allocated_total,
            "maintenance_margin_total": maintenance_margin_total,
            "positions_with_liquidation_price": positions_with_liquidation_price,
            "settle_currencies": sorted(settle_currencies),
            "margin_source_counts": margin_source_counts,
        }

    def _exchange_position_margin_summary(self, exchange) -> dict[str, Any]:
        if exchange is None:
            return self._empty_position_margin_summary()
        summary = self._empty_position_margin_summary()
        margin_mode_counts: dict[str, int] = {}
        settle_currencies: set[str] = set()
        margin_allocated_total = Decimal("0")
        maintenance_margin_total = Decimal("0")
        positions_with_liquidation_price = 0
        for position in exchange.positions:
            margin_mode = str(getattr(position, "margin_mode", None) or "unknown")
            margin_mode_counts[margin_mode] = margin_mode_counts.get(margin_mode, 0) + 1
            margin_allocated_total += self._to_decimal(getattr(position, "margin_allocated", None)) or Decimal("0")
            maintenance_margin_total += self._to_decimal(getattr(position, "maintenance_margin", None)) or Decimal("0")
            if getattr(position, "liquidation_price", None) not in {None, ""}:
                positions_with_liquidation_price += 1
            if getattr(position, "settle_currency", None):
                settle_currencies.add(str(position.settle_currency))
        return {
            "available": bool(exchange.positions),
            "position_count": len(exchange.positions),
            "position_count_by_margin_mode": margin_mode_counts,
            "margin_allocated_total": margin_allocated_total,
            "maintenance_margin_total": maintenance_margin_total,
            "positions_with_liquidation_price": positions_with_liquidation_price,
            "settle_currencies": sorted(settle_currencies),
            "margin_source_counts": {"exchange": len(exchange.positions)} if exchange.positions else {},
        }

    def _margin_reconciliation_summary(self, report) -> dict[str, Any] | None:
        if report is None:
            return None
        position_diff = report.position_diff if isinstance(report.position_diff, dict) else {}
        margin_mode_mismatches = (
            position_diff.get("exchange_margin_mode_mismatches")
            if isinstance(position_diff.get("exchange_margin_mode_mismatches"), dict)
            else {}
        )
        margin_metric_mismatches = (
            position_diff.get("exchange_margin_mismatches")
            if isinstance(position_diff.get("exchange_margin_mismatches"), dict)
            else {}
        )
        return {
            "position_margin_mode_mismatch_count": len(margin_mode_mismatches),
            "position_margin_metric_mismatch_count": len(margin_metric_mismatches),
            "position_margin_mode_mismatch_keys": sorted(margin_mode_mismatches.keys()),
            "position_margin_metric_mismatch_keys": sorted(margin_metric_mismatches.keys()),
            "has_margin_reconciliation_findings": bool(margin_mode_mismatches or margin_metric_mismatches),
        }

    def _position_liquidation_gap_ratio(self, position: Any) -> Decimal | None:
        mark_price = self._to_decimal(getattr(position, "mark_price", None))
        liquidation_price = self._to_decimal(getattr(position, "liquidation_price", None))
        if (
            mark_price is None
            or liquidation_price is None
            or abs(mark_price) <= self._DECIMAL_EPSILON
        ):
            return None
        side = str(getattr(position, "side", None) or getattr(position, "pos_side", None) or "net").lower()
        if side == "short":
            return (liquidation_price - mark_price) / mark_price
        return (mark_price - liquidation_price) / mark_price

    def _exchange_liquidation_risk_summary(self, exchange) -> dict[str, Any]:
        if exchange is None:
            return {
                "available": False,
                "position_count": 0,
                "positions_with_liquidation_price": 0,
                "nearest_liquidation_gap_ratio": None,
                "buffer_threshold_ratio": Decimal(str(self.runtime.settings.liquidation_buffer_fraction)),
                "positions_inside_buffer": 0,
                "closest_position": None,
            }
        buffer_threshold = Decimal(str(self.runtime.settings.liquidation_buffer_fraction))
        positions_with_liquidation_price = 0
        positions_inside_buffer = 0
        closest_position: dict[str, Any] | None = None
        closest_gap: Decimal | None = None
        for position in exchange.positions:
            gap_ratio = self._position_liquidation_gap_ratio(position)
            if gap_ratio is None:
                continue
            positions_with_liquidation_price += 1
            if gap_ratio <= buffer_threshold + self._DECIMAL_EPSILON:
                positions_inside_buffer += 1
            if closest_gap is None or gap_ratio < closest_gap:
                closest_gap = gap_ratio
                closest_position = {
                    "symbol": getattr(position, "symbol", None),
                    "pos_side": getattr(position, "side", None),
                    "mark_price": getattr(position, "mark_price", None),
                    "liquidation_price": getattr(position, "liquidation_price", None),
                    "margin_mode": getattr(position, "margin_mode", None),
                    "margin_ratio": getattr(position, "margin_ratio", None),
                    "margin_allocated": getattr(position, "margin_allocated", None),
                    "maintenance_margin": getattr(position, "maintenance_margin", None),
                    "gap_ratio": gap_ratio,
                    "buffer_gap_ratio": gap_ratio - buffer_threshold,
                }
        return {
            "available": bool(exchange.positions),
            "position_count": len(exchange.positions),
            "positions_with_liquidation_price": positions_with_liquidation_price,
            "nearest_liquidation_gap_ratio": closest_gap,
            "buffer_threshold_ratio": buffer_threshold,
            "positions_inside_buffer": positions_inside_buffer,
            "closest_position": closest_position,
        }

    def _risk_margin_buffer_context(self, risk_payload: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(risk_payload, dict):
            return None
        projected_margin_usage = self._to_decimal(risk_payload.get("projected_margin_usage"))
        liquidation_buffer_remaining = self._to_decimal(risk_payload.get("liquidation_buffer_remaining"))
        only_reduce_trigger = Decimal(str(self.runtime.settings.derivatives_only_reduce_trigger_margin_fraction))
        hard_limit = Decimal(str(self.runtime.settings.max_margin_usage_fraction))
        if projected_margin_usage is None and liquidation_buffer_remaining is None:
            return None
        return {
            "projected_margin_usage": projected_margin_usage,
            "projected_margin_usage_percent": (
                None if projected_margin_usage is None else self._format_fraction_percent(projected_margin_usage)
            ),
            "only_reduce_trigger_fraction": only_reduce_trigger,
            "only_reduce_trigger_percent": self._format_fraction_percent(only_reduce_trigger),
            "hard_limit_fraction": hard_limit,
            "hard_limit_percent": self._format_fraction_percent(hard_limit),
            "buffer_to_only_reduce": (
                None if projected_margin_usage is None else only_reduce_trigger - projected_margin_usage
            ),
            "buffer_to_only_reduce_percent": (
                None
                if projected_margin_usage is None
                else self._format_fraction_percent(only_reduce_trigger - projected_margin_usage)
            ),
            "buffer_to_hard_limit": (
                liquidation_buffer_remaining
                if liquidation_buffer_remaining is not None
                else (None if projected_margin_usage is None else hard_limit - projected_margin_usage)
            ),
            "buffer_to_hard_limit_percent": (
                None
                if (
                    liquidation_buffer_remaining is None
                    and projected_margin_usage is None
                )
                else self._format_fraction_percent(
                    liquidation_buffer_remaining
                    if liquidation_buffer_remaining is not None
                    else hard_limit - projected_margin_usage
                )
            ),
        }

    def margin_buffer_risk(self) -> dict[str, Any]:
        cache_key = f"margin_buffer_risk:{self._scope_cache_fragment()}"
        return self._cached_ttl(cache_key, 10, self._build_margin_buffer_risk)

    def _build_margin_buffer_risk(self) -> dict[str, Any]:
        snapshot = self.runtime.account_service.latest_snapshot()
        risk_snapshot = snapshot.risk_snapshot if snapshot is not None else None
        latest_risk_payload = self.latest_risk().get("payload")
        projected_context = self._risk_margin_buffer_context(latest_risk_payload)
        current_initial_margin_requirement = (
            None
            if risk_snapshot is None or risk_snapshot.initial_margin_requirement is None
            else self._to_decimal(risk_snapshot.initial_margin_requirement)
        )
        current_maintenance_margin_requirement = (
            None
            if risk_snapshot is None or risk_snapshot.maintenance_margin_requirement is None
            else self._to_decimal(risk_snapshot.maintenance_margin_requirement)
        )
        equity_base = None
        if risk_snapshot is not None:
            for value in (
                risk_snapshot.adjusted_equity,
                risk_snapshot.total_equity,
                risk_snapshot.available_equity,
            ):
                resolved = self._to_decimal(value)
                if resolved is not None and resolved > self._DECIMAL_EPSILON:
                    equity_base = resolved
                    break
        current_initial_margin_usage = (
            None
            if current_initial_margin_requirement is None or equity_base is None
            else current_initial_margin_requirement / equity_base
        )
        current_maintenance_margin_usage = (
            None
            if current_maintenance_margin_requirement is None or equity_base is None
            else current_maintenance_margin_requirement / equity_base
        )
        only_reduce_trigger = Decimal(str(self.runtime.settings.derivatives_only_reduce_trigger_margin_fraction))
        hard_limit = Decimal(str(self.runtime.settings.max_margin_usage_fraction))
        current_buffer_to_only_reduce = (
            None if current_initial_margin_usage is None else only_reduce_trigger - current_initial_margin_usage
        )
        current_buffer_to_hard_limit = (
            None if current_initial_margin_usage is None else hard_limit - current_initial_margin_usage
        )
        liquidation_summary = self._exchange_liquidation_risk_summary(snapshot)
        nearest_gap = self._to_decimal(liquidation_summary.get("nearest_liquidation_gap_ratio"))
        projected_margin_usage = None if projected_context is None else self._to_decimal(projected_context.get("projected_margin_usage"))
        status = "healthy"
        if (
            (nearest_gap is not None and nearest_gap <= Decimal("0"))
            or (current_buffer_to_hard_limit is not None and current_buffer_to_hard_limit <= Decimal("0"))
            or (
                projected_context is not None
                and self._to_decimal(projected_context.get("buffer_to_hard_limit")) is not None
                and self._to_decimal(projected_context.get("buffer_to_hard_limit")) <= Decimal("0")
            )
        ):
            status = "critical"
        elif (
            (nearest_gap is not None and nearest_gap <= Decimal(str(self.runtime.settings.liquidation_buffer_fraction)) + self._DECIMAL_EPSILON)
            or (current_buffer_to_only_reduce is not None and current_buffer_to_only_reduce <= Decimal("0"))
            or (
                projected_context is not None
                and self._to_decimal(projected_context.get("buffer_to_only_reduce")) is not None
                and self._to_decimal(projected_context.get("buffer_to_only_reduce")) <= Decimal("0")
            )
        ):
            status = "warning"
        summary_map = {
            "healthy": "当前保证金缓冲和强平距离都还在可接受区间内。",
            "warning": "当前保证金缓冲或强平距离已经偏紧，新的开仓和加仓需要特别谨慎。",
            "critical": "当前保证金缓冲或强平距离已经进入高风险区域，应优先减仓或暂停新增暴露。",
        }
        return {
            "available": snapshot is not None and self.runtime.settings.trading_product_type == "derivatives",
            "status": status,
            "summary": summary_map[status],
            "current": {
                "equity_base": equity_base,
                "initial_margin_requirement": current_initial_margin_requirement,
                "maintenance_margin_requirement": current_maintenance_margin_requirement,
                "initial_margin_usage_fraction": current_initial_margin_usage,
                "initial_margin_usage_percent": (
                    None if current_initial_margin_usage is None else self._format_fraction_percent(current_initial_margin_usage)
                ),
                "maintenance_margin_usage_fraction": current_maintenance_margin_usage,
                "maintenance_margin_usage_percent": (
                    None
                    if current_maintenance_margin_usage is None
                    else self._format_fraction_percent(current_maintenance_margin_usage)
                ),
                "buffer_to_only_reduce": current_buffer_to_only_reduce,
                "buffer_to_only_reduce_percent": (
                    None
                    if current_buffer_to_only_reduce is None
                    else self._format_fraction_percent(current_buffer_to_only_reduce)
                ),
                "buffer_to_hard_limit": current_buffer_to_hard_limit,
                "buffer_to_hard_limit_percent": (
                    None
                    if current_buffer_to_hard_limit is None
                    else self._format_fraction_percent(current_buffer_to_hard_limit)
                ),
            },
            "projected": projected_context,
            "liquidation": liquidation_summary,
            "thresholds": {
                "only_reduce_trigger_fraction": only_reduce_trigger,
                "only_reduce_trigger_percent": self._format_fraction_percent(only_reduce_trigger),
                "hard_limit_fraction": hard_limit,
                "hard_limit_percent": self._format_fraction_percent(hard_limit),
                "liquidation_buffer_fraction": Decimal(str(self.runtime.settings.liquidation_buffer_fraction)),
                "liquidation_buffer_percent": self._format_fraction_percent(self.runtime.settings.liquidation_buffer_fraction),
            },
            "truth_source": "exchange_risk_snapshot_plus_latest_risk_decision",
        }

    def derivatives_live_guard(self) -> dict[str, Any]:
        service = getattr(self.runtime, "derivatives_live_guard_service", None)
        if service is None:
            return {
                "enabled": False,
                "status": "not_configured",
                "summary": "当前没有启用合约实盘自动保护。",
            }
        cache_key = f"derivatives_live_guard:{self._scope_cache_fragment()}"
        return self._cached_ttl(cache_key, 5, service.snapshot)

    @staticmethod
    def _guarded_live_preflight_check(
        *,
        check_id: str,
        category: str,
        label: str,
        status: str,
        detail: str,
        required: bool = True,
        observed: Any = None,
    ) -> dict[str, Any]:
        return {
            "check_id": check_id,
            "category": category,
            "label": label,
            "status": status,
            "detail": detail,
            "required": required,
            "observed": observed,
        }

    def guarded_live_preflight(self) -> dict[str, Any]:
        cache_key = f"guarded_live_preflight:{self._scope_cache_fragment()}"
        return self._cached_ttl(cache_key, 10, self._build_guarded_live_preflight)

    def _build_guarded_live_preflight(self) -> dict[str, Any]:
        if not (
            self.runtime.settings.mode == "guarded_live"
            and self.runtime.settings.trading_product_type == "derivatives"
        ):
            return {
                "generated_at": utc_now(),
                "status": "not_applicable",
                "launch_ready": False,
                "summary": "当前不是合约 guarded_live 运行线，不需要执行这份启盘前自检。",
                "checks": [],
                "counts": {"pass": 0, "warn": 0, "fail": 0},
                "operator_actions": ["先切到合约 guarded_live 运行线，再执行启盘前自检。"],
                "truth_source": "runtime_contract_plus_operator_safety_checks",
            }

        mode_snapshot = self.system_mode()
        recovery = self.recovery_view()
        blockers = self.blockers()
        account = self.account_state()
        margin_buffer = self.margin_buffer_risk()
        live_guard = self.derivatives_live_guard()
        trial_guard = self.trial_guard()
        account_snapshot = self.runtime.account_service.latest_snapshot()
        account_configuration = (
            None if account_snapshot is None else account_snapshot.account_configuration
        )
        primary_instrument_rule = (
            None
            if account_snapshot is None
            else next(
                (
                    item
                    for item in account_snapshot.instruments
                    if item.symbol == self.runtime.settings.default_symbol
                ),
                None,
            )
        )
        active_blockers = [item for item in blockers if not item.get("submit_only")]
        submit_blocked_reasons = list(mode_snapshot.get("submit_blocked_reasons") or [])

        checks = [
            self._guarded_live_preflight_check(
                check_id="startup_profile_derivatives",
                category="runtime_contract",
                label="启动档位必须是合约",
                status="pass" if self.runtime.settings.startup_profile == "derivatives" else "fail",
                detail=(
                    "当前启动档位已经明确为 derivatives。"
                    if self.runtime.settings.startup_profile == "derivatives"
                    else "当前启动档位不是 derivatives，不能把现货配置误带进合约实盘。"
                ),
                observed=self.runtime.settings.startup_profile,
            ),
            self._guarded_live_preflight_check(
                check_id="guarded_live_mode",
                category="runtime_contract",
                label="运行模式必须是 guarded_live",
                status="pass" if self.runtime.settings.mode == "guarded_live" else "fail",
                detail=(
                    "当前运行模式已经进入 guarded_live。"
                    if self.runtime.settings.mode == "guarded_live"
                    else "当前不是 guarded_live，不能拿这份预检结果当作实盘启盘依据。"
                ),
                observed=self.runtime.settings.mode,
            ),
            self._guarded_live_preflight_check(
                check_id="exchange_backends_okx",
                category="runtime_contract",
                label="行情、账户和执行后端必须接到 OKX",
                status=(
                    "pass"
                    if (
                        self.runtime.settings.market_data_backend == "okx"
                        and self.runtime.settings.execution_backend == "okx"
                        and self.runtime.settings.account_backend == "okx"
                        and self.runtime.settings.account_read_enabled
                    )
                    else "fail"
                ),
                detail=(
                    "当前行情、账户和执行链都已接到 OKX，并且账户读取已启用。"
                    if (
                        self.runtime.settings.market_data_backend == "okx"
                        and self.runtime.settings.execution_backend == "okx"
                        and self.runtime.settings.account_backend == "okx"
                        and self.runtime.settings.account_read_enabled
                    )
                    else "当前行情、账户或执行后端还没有全部接到 OKX，或者账户读取没有启用。"
                ),
                observed={
                    "market_data_backend": self.runtime.settings.market_data_backend,
                    "execution_backend": self.runtime.settings.execution_backend,
                    "account_backend": self.runtime.settings.account_backend,
                    "account_read_enabled": self.runtime.settings.account_read_enabled,
                },
            ),
            self._guarded_live_preflight_check(
                check_id="postgres_and_runtime_lock",
                category="runtime_contract",
                label="必须启用 Postgres 和单实例运行锁",
                status=(
                    "pass"
                    if (
                        self.runtime.settings.storage_mode == "postgres"
                        and bool(self.runtime.settings.database_url)
                        and self.runtime.settings.database_single_runtime_guard_enabled
                    )
                    else "fail"
                ),
                detail=(
                    "当前已经启用 Postgres 和单实例运行锁。"
                    if (
                        self.runtime.settings.storage_mode == "postgres"
                        and bool(self.runtime.settings.database_url)
                        and self.runtime.settings.database_single_runtime_guard_enabled
                    )
                    else "当前没有完整启用 Postgres 持久化和单实例运行锁，不能把这条线当成合约实盘运行线。"
                ),
                observed={
                    "storage_mode": self.runtime.settings.storage_mode,
                    "database_single_runtime_guard_enabled": self.runtime.settings.database_single_runtime_guard_enabled,
                },
            ),
            self._guarded_live_preflight_check(
                check_id="operator_auth_hardened",
                category="operator_safety",
                label="控制面必须启用认证并禁止未认证写入",
                status=(
                    "pass"
                    if self.runtime.settings.operator_auth_enabled and not self.runtime.settings.operator_unsafe_write_without_auth
                    else "fail"
                ),
                detail=(
                    "当前控制面已经启用认证，并关闭了未认证写入。"
                    if self.runtime.settings.operator_auth_enabled and not self.runtime.settings.operator_unsafe_write_without_auth
                    else "当前控制面认证仍不够硬，必须先启用认证并关闭未认证写入。"
                ),
                observed={
                    "operator_auth_enabled": self.runtime.settings.operator_auth_enabled,
                    "operator_unsafe_write_without_auth": self.runtime.settings.operator_unsafe_write_without_auth,
                },
            ),
            self._guarded_live_preflight_check(
                check_id="real_money_route_ready",
                category="execution_route",
                label="真实资金报单路径必须不再处于结构性阻断",
                status=(
                    "pass"
                    if not self.runtime.policy_profile.real_money_submission_structurally_blocked
                    else "fail"
                ),
                detail=(
                    "当前执行线路已经不再被 real_money_live_not_supported 结构性阻断。"
                    if not self.runtime.policy_profile.real_money_submission_structurally_blocked
                    else "当前执行线路仍然被结构性阻断，系统还不会把订单真正发到真实资金线路。"
                ),
                observed={
                    "execution_route": mode_snapshot.get("execution_route"),
                    "exchange_submit_target": mode_snapshot.get("exchange_submit_target"),
                    "submit_blocked_reasons": submit_blocked_reasons,
                },
            ),
            self._guarded_live_preflight_check(
                check_id="account_snapshot_ready",
                category="account_readiness",
                label="账户快照必须可用且新鲜",
                status=(
                    "pass"
                    if account.get("connected") and account.get("fresh") and account.get("ready")
                    else "fail"
                ),
                detail=(
                    "当前账户快照已经连接、刷新并可用于交易判断。"
                    if account.get("connected") and account.get("fresh") and account.get("ready")
                    else "当前账户快照还不够可信，必须先恢复连接和新鲜度。"
                ),
                observed={
                    "connected": account.get("connected"),
                    "fresh": account.get("fresh"),
                    "ready": account.get("ready"),
                    "blockers": account.get("blockers"),
                },
            ),
            self._guarded_live_preflight_check(
                check_id="account_configuration_present",
                category="account_readiness",
                label="必须拿到结构化账户模式快照",
                status="pass" if account_configuration is not None else "fail",
                detail=(
                    "当前已经拿到结构化账户模式快照。"
                    if account_configuration is not None
                    else "当前拿不到结构化账户模式快照，不能确认持仓模式和账户模式是否正确。"
                ),
                observed=None if account_configuration is None else account_configuration.model_dump(mode="json"),
            ),
            self._guarded_live_preflight_check(
                check_id="risk_snapshot_present",
                category="account_readiness",
                label="必须拿到结构化风险快照",
                status="pass" if account.get("risk_snapshot") is not None else "fail",
                detail=(
                    "当前已经拿到结构化风险快照。"
                    if account.get("risk_snapshot") is not None
                    else "当前拿不到结构化风险快照，无法确认保证金占用和风险率。"
                ),
                observed=account.get("risk_snapshot"),
            ),
            self._guarded_live_preflight_check(
                check_id="primary_instrument_rule_present",
                category="account_readiness",
                label="默认交易标的必须有结构化产品规则",
                status="pass" if primary_instrument_rule is not None else "fail",
                detail=(
                    "当前默认交易标的已经拿到结构化产品规则。"
                    if primary_instrument_rule is not None
                    else "当前默认交易标的缺少结构化产品规则，不能安全做下单前一致性校验。"
                ),
                observed=None if primary_instrument_rule is None else primary_instrument_rule.model_dump(mode="json"),
            ),
            self._guarded_live_preflight_check(
                check_id="no_active_execution_blockers",
                category="recovery_and_blockers",
                label="当前不能存在活动中的执行阻断",
                status="pass" if not active_blockers else "fail",
                detail=(
                    "当前没有活动中的执行阻断。"
                    if not active_blockers
                    else "当前仍然有执行阻断，启盘前必须先把这些阻断处理干净。"
                ),
                observed=[item.get("blocker") for item in active_blockers],
            ),
            self._guarded_live_preflight_check(
                check_id="recovery_state_safe",
                category="recovery_and_blockers",
                label="恢复状态必须允许安全继续交易",
                status=(
                    "pass"
                    if recovery.get("safe_to_trade") and not recovery.get("review_required")
                    else "fail"
                ),
                detail=(
                    "当前恢复状态允许继续自动交易。"
                    if recovery.get("safe_to_trade") and not recovery.get("review_required")
                    else "当前恢复状态仍不允许安全继续交易，必须先处理恢复阻断或人工复核。"
                ),
                observed={
                    "recovery_state": recovery.get("recovery_state"),
                    "review_required": recovery.get("review_required"),
                    "resume_blocked_reasons": recovery.get("resume_blocked_reasons"),
                },
            ),
            self._guarded_live_preflight_check(
                check_id="margin_buffer_safe",
                category="risk_buffer",
                label="当前保证金缓冲不能处于 critical 或 only-reduce",
                status=(
                    "pass"
                    if margin_buffer.get("status") == "healthy" and not live_guard.get("only_reduce_required")
                    else "fail"
                ),
                detail=(
                    "当前保证金缓冲和强平距离都还在健康区间。"
                    if margin_buffer.get("status") == "healthy" and not live_guard.get("only_reduce_required")
                    else "当前保证金缓冲已经过紧，系统至少会进入 only-reduce，严重时会自动停机。"
                ),
                observed={
                    "margin_buffer_status": margin_buffer.get("status"),
                    "only_reduce_required": live_guard.get("only_reduce_required"),
                    "auto_halt_required": live_guard.get("auto_halt_required"),
                },
            ),
            self._guarded_live_preflight_check(
                check_id="trial_guard_status",
                category="trial_guard",
                label="试盘守护不能处于 breached",
                status=(
                    "fail"
                    if trial_guard.get("status") == "breached"
                    else "warn"
                    if trial_guard.get("status") in {"disabled", "not_configured", "warming_up"}
                    else "pass"
                ),
                detail=(
                    "当前试盘守护已经进入监控中，没有触发自动停机。"
                    if trial_guard.get("status") == "monitoring"
                    else "当前试盘守护已经触发自动停机。"
                    if trial_guard.get("status") == "breached"
                    else "当前试盘守护还没有形成稳定样本，启盘后要保持小资金和人工盯盘。 "
                ).strip(),
                observed={
                    "status": trial_guard.get("status"),
                    "fill_count": trial_guard.get("fill_count"),
                    "daily_combined_net_realized": trial_guard.get("daily_combined_net_realized"),
                },
                required=False,
            ),
            self._guarded_live_preflight_check(
                check_id="small_capital_limits_present",
                category="capital_envelope",
                label="必须配置小资金运行包的名义与试盘阈值",
                status=(
                    "pass"
                    if (
                        self.runtime.settings.max_gross_notional_per_symbol > 0
                        and self.runtime.settings.max_total_open_notional > 0
                        and self.runtime.settings.trial_guard_max_daily_loss_usdt > 0
                    )
                    else "fail"
                ),
                detail=(
                    "当前已经配置小资金运行包的名义上限和试盘止损阈值。"
                    if (
                        self.runtime.settings.max_gross_notional_per_symbol > 0
                        and self.runtime.settings.max_total_open_notional > 0
                        and self.runtime.settings.trial_guard_max_daily_loss_usdt > 0
                    )
                    else "当前没有完整配置小资金运行包的名义上限或试盘止损阈值。"
                ),
                observed={
                    "max_gross_notional_per_symbol": self.runtime.settings.max_gross_notional_per_symbol,
                    "max_total_open_notional": self.runtime.settings.max_total_open_notional,
                    "trial_guard_max_daily_loss_usdt": self.runtime.settings.trial_guard_max_daily_loss_usdt,
                },
            ),
        ]

        fail_count = sum(1 for item in checks if item["status"] == "fail")
        warn_count = sum(1 for item in checks if item["status"] == "warn")
        pass_count = sum(1 for item in checks if item["status"] == "pass")
        required_failures = [item for item in checks if item["required"] and item["status"] == "fail"]
        launch_ready = not required_failures
        status = "fail" if required_failures else "warning" if warn_count else "ready"
        summary = {
            "ready": "当前合约 guarded_live 启盘前自检已通过，可以进入小资金人工盯盘阶段。",
            "warning": "当前启盘前自检没有硬失败，但仍有需要人工确认的告警项，只适合小资金受控运行。",
            "fail": "当前启盘前自检仍有硬失败项，不能把这条线视为可启盘的合约实盘运行线。",
        }[status]
        operator_actions = [
            item["detail"]
            for item in checks
            if item["status"] in {"fail", "warn"}
        ]
        return {
            "generated_at": utc_now(),
            "status": status,
            "launch_ready": launch_ready,
            "summary": summary,
            "counts": {"pass": pass_count, "warn": warn_count, "fail": fail_count},
            "checks": checks,
            "operator_actions": list(dict.fromkeys(operator_actions)),
            "truth_source": "runtime_contract_plus_operator_safety_checks",
        }

    def guarded_live_run_packet(self) -> dict[str, Any]:
        cache_key = f"guarded_live_run_packet:{self._scope_cache_fragment()}"
        return self._cached_ttl(cache_key, 15, self._build_guarded_live_run_packet)

    def _build_guarded_live_run_packet(self) -> dict[str, Any]:
        preflight = self.guarded_live_preflight()
        live_guard = self.derivatives_live_guard()
        trial_guard = self.trial_guard()
        margin_buffer = self.margin_buffer_risk()
        recovery = self.recovery_view()
        blockers = self.blockers()
        positions = self.positions()
        account = self.account_state()
        forward_validation = self.forward_validation_report(window_days=7, period_count=4)
        latest_period = (forward_validation.get("periods") or [None])[0] or {}
        execution_blockers = [item for item in blockers if item.get("affects_execution")]

        status = "ready"
        if live_guard.get("auto_halt_required") or trial_guard.get("status") == "breached" or execution_blockers:
            status = "critical"
        elif (
            preflight.get("status") in {"warning", "fail"}
            or live_guard.get("only_reduce_required")
            or margin_buffer.get("status") in {"warning", "critical"}
            or not recovery.get("safe_to_trade")
        ):
            status = "warning"

        summary_map = {
            "ready": "当前运行包状态健康，可以继续保持小资金受控运行。",
            "warning": "当前运行包存在明显风险或约束，必须保持 only-reduce / 小资金 / 人工盯盘。",
            "critical": "当前运行包已经触发自动停机或存在硬阻断，不应继续自动运行。",
        }
        operator_actions: list[str] = []
        if preflight.get("status") == "fail":
            operator_actions.append("先处理启盘前自检里的硬失败项，再讨论继续实盘。")
        if live_guard.get("auto_halt_required"):
            operator_actions.append("当前已经进入自动停机区间，先减仓并核对交易所保证金状态。")
        elif live_guard.get("only_reduce_required"):
            operator_actions.append("当前只允许继续减仓或平仓，先把保证金缓冲拉回健康区间。")
        if trial_guard.get("status") == "breached":
            operator_actions.append("试盘守护已经触发暂停，先复盘最近收益、滑点和资金费拖累。")
        if execution_blockers:
            operator_actions.append("当前仍有执行阻断，先把阻断项处理干净。")
        if (self._to_decimal(latest_period.get("combined_net_realized_pnl")) or Decimal("0")) < Decimal("0"):
            operator_actions.append("最近一个验证周期综合净收益为负，先缩小试盘规模并继续观察。")

        return {
            "generated_at": utc_now(),
            "status": status,
            "summary": summary_map[status],
            "runtime_contract": {
                "config_profile": self.runtime.settings.config_profile,
                "startup_profile": self.runtime.settings.startup_profile,
                "env_template_profile": self.runtime.settings.env_template_profile,
                "execution_route": self.runtime.environment_capabilities.exchange_submission_target,
                "storage_mode": self.runtime.settings.storage_mode,
            },
            "capital_envelope": {
                "default_order_qty": self.runtime.settings.default_order_qty,
                "max_gross_notional_per_symbol": self.runtime.settings.max_gross_notional_per_symbol,
                "max_pending_notional_per_symbol": self.runtime.settings.max_pending_notional_per_symbol,
                "max_total_open_notional": self.runtime.settings.max_total_open_notional,
                "max_daily_realized_loss_usdt": self.runtime.settings.max_daily_realized_loss_usdt,
                "trial_guard_max_daily_loss_usdt": self.runtime.settings.trial_guard_max_daily_loss_usdt,
            },
            "summary_metrics": {
                "launch_ready": preflight.get("launch_ready"),
                "safe_to_trade": recovery.get("safe_to_trade"),
                "execution_blocker_count": len(execution_blockers),
                "current_initial_margin_usage_fraction": margin_buffer.get("current", {}).get("initial_margin_usage_fraction"),
                "nearest_liquidation_gap_ratio": margin_buffer.get("liquidation", {}).get("nearest_liquidation_gap_ratio"),
                "combined_net_realized_pnl": latest_period.get("combined_net_realized_pnl"),
                "funding_fee_net_pnl": latest_period.get("funding_fee_net_pnl"),
                "open_position_count": len(positions.get("exchange_positions") or []),
                "current_open_order_count": len(self._scoped_open_order_states()),
            },
            "preflight": preflight,
            "derivatives_live_guard": live_guard,
            "trial_guard": trial_guard,
            "margin_buffer_overview": margin_buffer,
            "forward_validation_summary": {
                "summary": forward_validation.get("summary"),
                "latest_period": latest_period,
            },
            "recovery": recovery,
            "account": {
                "maker_fee_rate": account.get("maker_fee_rate"),
                "taker_fee_rate": account.get("taker_fee_rate"),
                "account_configuration": account.get("account_configuration"),
                "risk_snapshot": account.get("risk_snapshot"),
            },
            "exposure": {
                "local_margin_summary": positions.get("local_margin_summary"),
                "exchange_margin_summary": positions.get("exchange_margin_summary"),
                "margin_reconciliation": positions.get("margin_reconciliation"),
                "exchange_positions": (positions.get("exchange_positions") or [])[:5],
            },
            "active_blockers": execution_blockers,
            "operator_actions": list(dict.fromkeys(operator_actions)),
            "truth_source": "guarded_live_operator_packet",
        }

    def strategy_execution_health(self, symbol: str | None = None) -> dict[str, Any]:
        target_symbol = symbol or self.runtime.settings.default_symbol
        snapshot = compute_strategy_execution_health(
            settings=self.runtime.settings,
            symbol=target_symbol,
            fills=self._scoped_fills(),
            snapshots=snapshots_for_scope(self.runtime.portfolio_repo, self.state_scope),
            current_position_qty=self._current_symbol_position_qty(target_symbol),
        )
        return snapshot.as_payload(
            settings=self.runtime.settings,
            as_of=utc_now(),
            current_position_qty=self._current_symbol_position_qty(target_symbol),
        )

    def _current_runtime_started_at(self) -> datetime:
        return self.runtime.started_at

    def _is_current_runtime_timestamp(self, value: datetime | str | None) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            normalized = value.replace("Z", "+00:00")
            try:
                value = datetime.fromisoformat(normalized)
            except ValueError:
                return False
        return value >= self._current_runtime_started_at()

    def _latest_scoped_reconciliation(self):
        return self._cached(
            "latest_scoped_reconciliation",
            lambda: latest_reconciliation_for_scope(self.runtime.reconciliation_repo, self.state_scope),
        )

    def _latest_scoped_portfolio_event(self):
        return latest_topic_event_for_scope(self.runtime.event_store, topics.PORTFOLIO_SNAPSHOTS, self.state_scope)

    def payload(self, envelope: EventEnvelope | None) -> dict[str, Any] | None:
        if envelope is None:
            return None
        payload = dict(envelope.payload)
        payload["_event_id"] = envelope.event_id
        payload["_topic"] = envelope.topic
        return payload

    def payload_by_ref(self, ref: str | None) -> dict[str, Any] | None:
        if ref is None:
            return None
        return self.payload(self.runtime.event_store.get(ref))

    def payloads_by_refs(self, refs: list[str]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for ref in refs:
            payload = self.payload_by_ref(ref)
            if payload is not None:
                rows.append(payload)
        return rows

    @staticmethod
    def _to_decimal(value: Any) -> Decimal | None:
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except Exception:
            return None

    @staticmethod
    def _format_fraction_percent(value: Any, *, places: int = 2) -> str:
        try:
            resolved = Decimal(str(value))
        except Exception:
            return "待确认"
        quantizer = Decimal("1") if places <= 0 else Decimal("1").scaleb(-places)
        normalized = resolved * Decimal("100")
        return f"{normalized.quantize(quantizer)}%"

    @staticmethod
    def _as_datetime(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            normalized = value.replace("Z", "+00:00")
            try:
                return datetime.fromisoformat(normalized)
            except ValueError:
                return None
        return None

    @staticmethod
    def _latency_ms(start: Any, end: Any) -> float | None:
        if not isinstance(start, datetime) or not isinstance(end, datetime):
            return None
        return round((end - start).total_seconds() * 1000.0, 3)

    def _adverse_slippage_bps(self, *, side: Any, fill_price: Any, reference_price: Any) -> Decimal | None:
        fill_price_decimal = self._to_decimal(fill_price)
        reference_price_decimal = self._to_decimal(reference_price)
        if (
            fill_price_decimal is None
            or reference_price_decimal is None
            or abs(reference_price_decimal) <= self._DECIMAL_EPSILON
        ):
            return None
        raw_bps = ((fill_price_decimal - reference_price_decimal) / reference_price_decimal) * Decimal("10000")
        if str(side).lower() == "sell":
            raw_bps = -raw_bps
        return raw_bps

    def _decision_support_payload(self, decision_id: str | None) -> dict[str, Any]:
        if not decision_id:
            return {
                "audit": None,
                "decision_context": None,
                "strategy_coordinator_snapshot": None,
                "strategy_sleeve_intents": [],
                "portfolio_allocation_decision": None,
                "baseline_assessment": None,
                "position_target": None,
                "risk_decision": None,
                "execution_plan": None,
                "execution_plans": [],
                "strategy_execution_bundle": None,
                "decision_outcome": None,
            }
        audit = self.runtime.audit_repo.get(decision_id)
        if audit is None:
            return {
                "audit": None,
                "decision_context": None,
                "strategy_coordinator_snapshot": None,
                "strategy_sleeve_intents": [],
                "portfolio_allocation_decision": None,
                "baseline_assessment": None,
                "position_target": None,
                "risk_decision": None,
                "execution_plan": None,
                "decision_outcome": None,
            }
        position_target = self.payload_by_ref(audit.position_target_ref)
        decision_outcome = self.payload_by_ref(audit.decision_outcome_ref)
        if decision_outcome is None and isinstance(position_target, dict):
            native_outcome = position_target.get("decision_outcome")
            decision_outcome = native_outcome if isinstance(native_outcome, dict) else None
        return {
            "audit": audit,
            "decision_context": self.payload_by_ref(audit.decision_context_ref),
            "strategy_coordinator_snapshot": self.payload_by_ref(audit.strategy_coordinator_snapshot_ref),
            "strategy_sleeve_intents": [
                payload
                for payload in (self.payload_by_ref(ref) for ref in audit.strategy_sleeve_intent_refs)
                if payload is not None
            ],
            "portfolio_allocation_decision": self.payload_by_ref(audit.portfolio_allocation_decision_ref),
            "baseline_assessment": self.payload_by_ref(audit.baseline_assessment_ref),
            "position_target": position_target,
            "risk_decision": self._risk_decision_payload(self.payload_by_ref(audit.risk_decision_ref)),
            "execution_plan": self._execution_plan_payload(self.payload_by_ref(audit.execution_plan_ref)),
            "execution_plans": [
                payload
                for payload in (
                    self._execution_plan_payload(self.payload_by_ref(ref))
                    for ref in audit.execution_plan_refs
                )
                if payload is not None
            ],
            "strategy_execution_bundle": self.payload_by_ref(audit.strategy_execution_bundle_ref),
            "decision_outcome": decision_outcome,
        }

    def _execution_quality_row(self, fill_record: Any) -> dict[str, Any]:
        fill_payload = self._execution_record_payload(fill_record)
        decision_support = self._decision_support_payload(fill_payload.get("decision_id"))
        decision_context = decision_support["decision_context"]
        baseline_assessment = decision_support["baseline_assessment"]
        position_target = decision_support["position_target"]
        risk_decision = decision_support["risk_decision"]
        execution_plan = decision_support["execution_plan"]
        decision_outcome = decision_support["decision_outcome"]
        order_state = self._control_plane_order_state(str(fill_payload.get("client_order_id") or ""))
        order_payload = None if order_state is None else self._execution_record_payload(order_state)

        signal_ts = None if decision_context is None else self._as_datetime(decision_context.get("as_of_ts"))
        order_created_ts = None if order_payload is None else self._as_datetime(order_payload.get("created_at"))
        submitted_ts = None if order_payload is None else self._as_datetime(order_payload.get("submitted_ts"))
        exchange_fill_ts = self._as_datetime(fill_payload.get("exchange_timestamp"))
        ingestion_ts = self._as_datetime(fill_payload.get("ingestion_timestamp"))
        reference_price = None
        if execution_plan is not None:
            reference_price = execution_plan.get("reference_price")
        if reference_price is None:
            reference_price = fill_payload.get("reference_price")
        if reference_price is None and order_payload is not None:
            submission_payload = order_payload.get("submission_payload") or {}
            reference_price = submission_payload.get("referencePrice") or submission_payload.get("reference_price")

        fill_notional = self._to_decimal(fill_payload.get("fill_notional"))
        if fill_notional is None:
            qty = self._to_decimal(fill_payload.get("fill_qty"))
            price = self._to_decimal(fill_payload.get("fill_price"))
            if qty is not None and price is not None:
                fill_notional = qty * price

        fee_amount = self._to_decimal(fill_payload.get("fee_amount"))
        fee_delta = self._to_decimal(fill_payload.get("fee_delta"))
        adverse_slippage_bps = self._adverse_slippage_bps(
            side=fill_payload.get("side"),
            fill_price=fill_payload.get("fill_price"),
            reference_price=reference_price,
        )
        market_regime = None if baseline_assessment is None else baseline_assessment.get("regime")
        volatility_state = None if baseline_assessment is None else baseline_assessment.get("volatility_state")
        baseline_reason_codes = [] if baseline_assessment is None else list(baseline_assessment.get("reason_codes") or [])
        position_management_reason_codes = (
            []
            if decision_outcome is None
            else list(decision_outcome.get("position_management_reason_codes") or [])
        )
        exit_attribution = None if decision_outcome is None else decision_outcome.get("exit_attribution")
        risk_constraints_applied = [] if risk_decision is None else list(risk_decision.get("constraints_applied") or [])
        risk_rejection_reasons = [] if risk_decision is None else list(risk_decision.get("rejection_reasons") or [])
        risk_budget_multiplier = None if risk_decision is None else self._to_decimal(risk_decision.get("risk_budget_multiplier"))
        execution_aggressiveness_multiplier = (
            None if risk_decision is None else self._to_decimal(risk_decision.get("execution_aggressiveness_multiplier"))
        )
        strategy_family = (
            None
            if decision_outcome is None
            else decision_outcome.get("selected_strategy_family")
        )
        if strategy_family is None and position_target is not None:
            strategy_family = position_target.get("strategy_family")
        strategy_route_action = (
            None
            if decision_outcome is None
            else decision_outcome.get("selected_strategy_route_action")
        )
        if strategy_route_action is None and position_target is not None:
            strategy_route_action = position_target.get("strategy_route_action")
        strategy_selection_reason_codes = (
            []
            if decision_outcome is None
            else list(decision_outcome.get("strategy_selection_reason_codes") or [])
        )
        if not strategy_selection_reason_codes and position_target is not None:
            strategy_selection_reason_codes = list(position_target.get("strategy_reason_codes") or [])
        strategy_headline = None if decision_outcome is None else decision_outcome.get("strategy_selection_headline")
        if strategy_headline is None and position_target is not None:
            strategy_headline = position_target.get("strategy_headline")
        risk_protection_active = bool(
            risk_decision is not None
            and (
                bool(risk_decision.get("only_reduce_required"))
                or "risk_budget_multiplier_applied" in risk_constraints_applied
                or "execution_aggressiveness_contracted" in risk_constraints_applied
                or (risk_budget_multiplier is not None and risk_budget_multiplier < Decimal("0.999999"))
                or (
                    execution_aggressiveness_multiplier is not None
                    and execution_aggressiveness_multiplier < Decimal("0.999999")
                )
            )
        )

        return {
            "fill_id": fill_payload.get("fill_id"),
            "decision_id": fill_payload.get("decision_id"),
            "intent_id": fill_payload.get("intent_id"),
            "order_id": fill_payload.get("client_order_id"),
            "symbol": fill_payload.get("symbol"),
            "side": fill_payload.get("side"),
            "execution_action": fill_payload.get("execution_action"),
            "position_intent": fill_payload.get("position_intent"),
            "truth_source": fill_payload.get("truth_source"),
            "signal_timestamp": signal_ts,
            "order_created_timestamp": order_created_ts,
            "submitted_timestamp": submitted_ts,
            "exchange_fill_timestamp": exchange_fill_ts,
            "ingestion_timestamp": ingestion_ts,
            "decision_to_submit_latency_ms": self._latency_ms(signal_ts, submitted_ts),
            "submit_to_exchange_fill_latency_ms": self._latency_ms(
                submitted_ts,
                exchange_fill_ts,
            ),
            "exchange_fill_to_ingestion_latency_ms": self._latency_ms(
                exchange_fill_ts,
                ingestion_ts,
            ),
            "reference_price": reference_price,
            "fill_price": fill_payload.get("fill_price"),
            "adverse_slippage_bps": None if adverse_slippage_bps is None else adverse_slippage_bps,
            "fill_qty": fill_payload.get("fill_qty"),
            "fill_notional": fill_notional,
            "fee_amount": fee_amount,
            "fee_delta": fee_delta,
            "fee_ratio": (
                None
                if fee_amount is None or fill_notional is None or abs(fill_notional) <= self._DECIMAL_EPSILON
                else fee_amount / fill_notional
            ),
            "realized_pnl_delta": self._to_decimal(fill_payload.get("realized_pnl_delta")),
            "gross_realized_pnl": self._to_decimal(fill_payload.get("gross_realized_pnl")),
            "expected_net_edge_bps": None if decision_outcome is None else decision_outcome.get("expected_net_edge_bps"),
            "market_regime": market_regime,
            "volatility_state": volatility_state,
            "baseline_reason_codes": baseline_reason_codes,
            "active_profile_id": None if decision_outcome is None else decision_outcome.get("active_profile_id"),
            "profile_control_source": None if decision_outcome is None else decision_outcome.get("profile_control_source"),
            "strategy_family": strategy_family or "directional",
            "strategy_route_action": strategy_route_action or "override_target",
            "strategy_selection_reason_codes": strategy_selection_reason_codes,
            "strategy_headline": strategy_headline,
            "position_management_reason_codes": position_management_reason_codes,
            "exit_attribution": exit_attribution,
            "guardrail_flags": [] if position_target is None else list(position_target.get("guardrail_flags") or []),
            "risk_constraints_applied": risk_constraints_applied,
            "risk_rejection_reasons": risk_rejection_reasons,
            "risk_budget_multiplier": risk_budget_multiplier,
            "execution_aggressiveness_multiplier": execution_aggressiveness_multiplier,
            "risk_protection_active": risk_protection_active,
            "risk_protection": "active" if risk_protection_active else "inactive",
            "decision_context": {
                "timeframe": None if decision_context is None else decision_context.get("timeframe"),
                "market_regime": market_regime,
                "volatility_state": volatility_state,
            },
        }

    def _profitability_fill_row(self, fill_record: Any) -> dict[str, Any]:
        row = self._execution_quality_row(fill_record)
        realized = self._to_decimal(row.get("realized_pnl_delta")) or Decimal("0")
        gross = self._to_decimal(row.get("gross_realized_pnl")) or Decimal("0")
        fee_delta = self._to_decimal(row.get("fee_delta")) or Decimal("0")
        row["event_kind"] = "fill_realization"
        row["event_id"] = row.get("fill_id")
        row["event_timestamp"] = row.get("ingestion_timestamp") or row.get("exchange_fill_timestamp")
        row["trading_net_realized_delta"] = realized
        row["trading_gross_realized_delta"] = gross
        row["funding_fee_delta"] = Decimal("0")
        row["combined_net_realized_delta"] = realized
        row["fee_delta"] = fee_delta
        return row

    @staticmethod
    def _funding_fee_event_timestamp(record: Any):
        return record.bill_ts or record.created_at

    def _funding_fee_profitability_row(self, record: Any) -> dict[str, Any]:
        amount = self._to_decimal(getattr(record, "amount", None)) or Decimal("0")
        return {
            "event_kind": "funding_fee",
            "event_id": record.bill_id,
            "bill_id": record.bill_id,
            "symbol": record.symbol,
            "currency": record.currency,
            "funding_direction": record.funding_direction,
            "type_label": record.type_label,
            "sub_type_label": record.sub_type_label,
            "event_timestamp": self._funding_fee_event_timestamp(record),
            "created_at": record.created_at,
            "trading_net_realized_delta": Decimal("0"),
            "trading_gross_realized_delta": Decimal("0"),
            "fee_delta": Decimal("0"),
            "funding_fee_delta": amount,
            "combined_net_realized_delta": amount,
            "ledger_posting_state": record.ledger_posting_state,
            "ledger_journal_id": record.ledger_journal_id,
            "truth_source": "funding_fee_records",
        }

    def _profitability_funding_fee_summary(self, records: list[Any]) -> dict[str, Any]:
        if not records:
            return {
                "count": 0,
                "income_count": 0,
                "expense_count": 0,
                "latest_bill_id": None,
                "latest_bill_ts": None,
                "net_total": Decimal("0"),
                "absolute_total": Decimal("0"),
                "net_total_by_currency": {},
                "absolute_total_by_currency": {},
                "net_total_by_symbol": {},
            }
        latest = max(records, key=lambda item: self._funding_fee_event_timestamp(item) or datetime.min)
        income_count = 0
        expense_count = 0
        net_total = Decimal("0")
        absolute_total = Decimal("0")
        net_total_by_currency: dict[str, Decimal] = {}
        absolute_total_by_currency: dict[str, Decimal] = {}
        net_total_by_symbol: dict[str, Decimal] = {}
        for record in records:
            amount = self._to_decimal(getattr(record, "amount", None)) or Decimal("0")
            currency = str(getattr(record, "currency", "") or "UNKNOWN")
            symbol = str(getattr(record, "symbol", "") or "account_level")
            net_total += amount
            absolute_total += abs(amount)
            net_total_by_currency[currency] = net_total_by_currency.get(currency, Decimal("0")) + amount
            absolute_total_by_currency[currency] = absolute_total_by_currency.get(currency, Decimal("0")) + abs(amount)
            net_total_by_symbol[symbol] = net_total_by_symbol.get(symbol, Decimal("0")) + amount
            if getattr(record, "funding_direction", None) == "income":
                income_count += 1
            elif getattr(record, "funding_direction", None) == "expense":
                expense_count += 1
        return {
            "count": len(records),
            "income_count": income_count,
            "expense_count": expense_count,
            "latest_bill_id": latest.bill_id,
            "latest_bill_ts": latest.bill_ts,
            "net_total": net_total,
            "absolute_total": absolute_total,
            "net_total_by_currency": {key: format(value, "f") for key, value in net_total_by_currency.items()},
            "absolute_total_by_currency": {key: format(value, "f") for key, value in absolute_total_by_currency.items()},
            "net_total_by_symbol": {key: format(value, "f") for key, value in net_total_by_symbol.items()},
        }

    @staticmethod
    def _profitability_event_sort_key(row: dict[str, Any]) -> tuple[datetime, str]:
        event_timestamp = row.get("event_timestamp")
        if not isinstance(event_timestamp, datetime):
            event_timestamp = datetime.min.replace(tzinfo=timezone.utc)
        elif event_timestamp.tzinfo is None:
            event_timestamp = event_timestamp.replace(tzinfo=timezone.utc)
        return (event_timestamp, str(row.get("event_id") or ""))

    @staticmethod
    def _paginate_rows(
        rows: list[Any],
        *,
        limit: int,
        offset: int,
        key: str,
        serializer=None,
    ) -> dict[str, Any]:
        total_available = len(rows)
        paged_rows = rows[offset : offset + limit]
        payloads = [serializer(item) for item in paged_rows] if serializer is not None else list(paged_rows)
        return {
            key: payloads,
            "limit": limit,
            "offset": offset,
            "total_available": total_available,
            "has_more": offset + len(paged_rows) < total_available,
        }

    def latest_order(self):
        if self._phase5_control_plane_enabled():
            rows = self._phase5_order_rows(limit=1)
            return rows[0] if rows else None
        rows = self._scoped_order_states()
        return max(rows, key=lambda item: item.last_update_ts or item.created_at, default=None)

    def latest_fill(self):
        if self._phase5_control_plane_enabled():
            rows = self._phase5_fill_rows(limit=1)
            return rows[0] if rows else None
        rows = self._scoped_fills()
        return max(rows, key=lambda item: item.ingestion_timestamp, default=None)

    def latest_account_baseline(self) -> dict[str, Any] | None:
        latest = self._cached(
            "latest_account_baseline_event",
            lambda: latest_topic_event_for_scope(
                self.runtime.event_store,
                topics.ACCOUNT_BASELINES,
                self.state_scope,
            ),
        )
        return latest.payload if latest is not None else None

    def account_service_status(self) -> dict[str, Any]:
        return self._cached("account_service_status", self.runtime.account_service.status)

    def latest_exchange_snapshot(self):
        return self._cached("latest_exchange_snapshot", self.runtime.account_service.latest_snapshot)

    def _latest_strategy_snapshot_event(self):
        return self._cached(
            "latest_strategy_snapshot_event",
            lambda: latest_topic_event_for_scope(
                self.runtime.event_store,
                topics.STRATEGY_COORDINATOR_SNAPSHOTS,
                self.state_scope,
            ),
        )

    @staticmethod
    def _strategy_snapshot_payload(event: Any | None) -> dict[str, Any] | None:
        if event is None:
            return None
        snapshot = StrategyCoordinatorSnapshot.model_validate(event.payload)
        payload = snapshot.model_dump(mode="json")
        payload["_event_id"] = event.event_id
        payload["_event_timestamp"] = event.event_timestamp
        return payload

    @staticmethod
    def _strategy_bundle_payload(event: Any | None) -> dict[str, Any] | None:
        if event is None:
            return None
        bundle = StrategyExecutionBundle.model_validate(event.payload)
        payload = bundle.model_dump(mode="json")
        payload["_event_id"] = event.event_id
        payload["_event_timestamp"] = event.event_timestamp
        return payload

    @staticmethod
    def _strategy_sleeve_intent_payload(event: Any | None) -> dict[str, Any] | None:
        if event is None:
            return None
        intent = StrategySleeveIntent.model_validate(event.payload)
        payload = intent.model_dump(mode="json")
        payload["_event_id"] = event.event_id
        payload["_event_timestamp"] = event.event_timestamp
        return payload

    @staticmethod
    def _portfolio_allocation_payload(event: Any | None) -> dict[str, Any] | None:
        if event is None:
            return None
        decision = PortfolioAllocationDecision.model_validate(event.payload)
        payload = decision.model_dump(mode="json")
        payload["_event_id"] = event.event_id
        payload["_event_timestamp"] = event.event_timestamp
        return payload

    def strategy_runtime(self, *, limit: int = 10) -> dict[str, Any]:
        return self.strategy_queries.strategy_runtime(limit=limit)

    def _build_strategy_runtime(self, *, limit: int) -> dict[str, Any]:
        latest_event = self._latest_strategy_snapshot_event()
        latest_snapshot = self._strategy_snapshot_payload(latest_event)
        selected_candidate = None
        if latest_snapshot is not None:
            selected_family = latest_snapshot.get("selected_family")
            selected_candidate = next(
                (
                    candidate
                    for candidate in latest_snapshot.get("candidates", [])
                    if candidate.get("family") == selected_family
                ),
                None,
            )
        recent_events = list(
            reversed(
                self.runtime.event_store.by_topic_scoped(
                    topics.STRATEGY_COORDINATOR_SNAPSHOTS,
                    scope=self.state_scope,
                    limit=limit,
                )
            )
        )
        recent_snapshots = [self._strategy_snapshot_payload(event) for event in recent_events]
        strategy_runtime_repo = getattr(self.runtime, "strategy_runtime_repo", None)
        if strategy_runtime_repo is not None:
            recent_sleeve_intents = [
                item.model_dump(mode="json")
                for item in strategy_runtime_repo.list_sleeve_intents(
                    product_type=self.state_scope.product_type,
                    margin_mode=self.state_scope.margin_mode,
                    limit=limit * 4,
                )
            ]
            latest_allocation = strategy_runtime_repo.latest_allocation_decision(
                product_type=self.state_scope.product_type,
                margin_mode=self.state_scope.margin_mode,
            )
            latest_allocation_decision = None if latest_allocation is None else latest_allocation.model_dump(mode="json")
            recent_budget_profiles = [
                item.model_dump(mode="json")
                for item in strategy_runtime_repo.list_budget_profiles(
                    product_type=self.state_scope.product_type,
                    margin_mode=self.state_scope.margin_mode,
                )[:limit]
            ]
            recent_budget_assignments = [
                item.model_dump(mode="json")
                for item in strategy_runtime_repo.list_budget_assignments(
                    product_type=self.state_scope.product_type,
                    margin_mode=self.state_scope.margin_mode,
                )[: limit * 4]
            ]
            if latest_allocation is not None:
                recent_budget_snapshots = [
                    item.model_dump(mode="json")
                    for item in strategy_runtime_repo.list_budget_snapshots(
                        allocation_id=latest_allocation.allocation_id,
                    )
                ]
                recent_conflict_resolutions = [
                    item.model_dump(mode="json")
                    for item in strategy_runtime_repo.list_conflict_resolutions(
                        allocation_id=latest_allocation.allocation_id,
                    )
                ]
                recent_netting_decisions = [
                    item.model_dump(mode="json")
                    for item in strategy_runtime_repo.list_netting_decisions(
                        allocation_id=latest_allocation.allocation_id,
                    )
                ]
            else:
                recent_budget_snapshots = []
                recent_conflict_resolutions = []
                recent_netting_decisions = []
            recent_bundles = [
                item.model_dump(mode="json")
                for item in strategy_runtime_repo.recent_execution_bundles(
                    product_type=self.state_scope.product_type,
                    margin_mode=self.state_scope.margin_mode,
                    limit=limit,
                )
            ]
        else:
            recent_intent_events = list(
                reversed(
                    self.runtime.event_store.by_topic_scoped(
                        topics.STRATEGY_SLEEVE_INTENTS,
                        scope=self.state_scope,
                        limit=limit * 4,
                    )
                )
            )
            recent_sleeve_intents = [self._strategy_sleeve_intent_payload(event) for event in recent_intent_events]
            latest_allocation_event = latest_topic_event_for_scope(
                self.runtime.event_store,
                topics.PORTFOLIO_ALLOCATION_DECISIONS,
                self.state_scope,
            )
            latest_allocation_decision = self._portfolio_allocation_payload(latest_allocation_event)
            recent_budget_profiles = []
            recent_budget_assignments = []
            recent_budget_snapshots = []
            recent_conflict_resolutions = []
            recent_netting_decisions = []
            recent_bundle_events = list(
                reversed(
                    self.runtime.event_store.by_topic_scoped(
                        topics.STRATEGY_EXECUTION_BUNDLES,
                        scope=self.state_scope,
                        limit=limit,
                    )
                )
            )
            recent_bundles = [self._strategy_bundle_payload(event) for event in recent_bundle_events]
        latest_bundle = recent_bundles[0] if recent_bundles else None
        latest_target_event = latest_topic_event_for_scope(
            self.runtime.event_store,
            topics.POSITION_TARGETS,
            self.state_scope,
        )
        latest_target_payload = None
        if latest_target_event is not None:
            target_payload = dict(latest_target_event.payload)
            latest_target_payload = {
                "decision_id": target_payload.get("decision_id"),
                "symbol": target_payload.get("symbol"),
                "target_position_qty": target_payload.get("target_position_qty"),
                "delta_position_qty": target_payload.get("delta_position_qty"),
                "position_intent": target_payload.get("position_intent"),
                "strategy_family": target_payload.get("strategy_family", "directional"),
                "strategy_sleeve_id": target_payload.get("strategy_sleeve_id"),
                "allocation_id": target_payload.get("allocation_id"),
                "strategy_route_action": target_payload.get("strategy_route_action", "override_target"),
                "strategy_reason_codes": list(target_payload.get("strategy_reason_codes") or []),
                "strategy_headline": target_payload.get("strategy_headline"),
                "event_timestamp": latest_target_event.event_timestamp,
            }
        sleeve_records = []
        sleeve_repo = getattr(self.runtime, "strategy_sleeve_repo", None)
        if sleeve_repo is not None and hasattr(sleeve_repo, "list_sleeves"):
            sleeve_records = [
                sleeve.model_dump(mode="json")
                for sleeve in sleeve_repo.list_sleeves()
            ]
        configured_family = self.runtime.settings.strategy_family_active
        family_enablement = {
            "directional": {
                "enabled": True,
                "runtime_supported": True,
                "execution_compatible": True,
            },
            "smart_arbitrage": {
                "enabled": bool(self.runtime.settings.smart_arbitrage_enabled),
                "runtime_supported": self.runtime.settings.trading_product_type == "derivatives",
                "execution_compatible": self.runtime.settings.trading_product_type == "derivatives",
            },
            "spot_grid": {
                "enabled": bool(self.runtime.settings.spot_grid_enabled),
                "runtime_supported": self.runtime.settings.trading_product_type == "spot",
                "execution_compatible": self.runtime.settings.trading_product_type == "spot",
            },
            "dca": {
                "enabled": bool(self.runtime.settings.dca_enabled),
                "runtime_supported": self.runtime.settings.trading_product_type == "spot",
                "execution_compatible": self.runtime.settings.trading_product_type == "spot",
            },
        }
        automation_decisions = [] if latest_snapshot is None else list(latest_snapshot.get("automation_decisions") or [])
        selected_candidate_payload = None
        if latest_snapshot is not None:
            selected_family = latest_snapshot.get("selected_family")
            for item in latest_snapshot.get("candidates") or []:
                if item.get("family") == selected_family:
                    selected_candidate_payload = item
                    break
        active_automation = [item for item in automation_decisions if item.get("automation_state") == "active"]
        contracted_automation = [
            item for item in automation_decisions if item.get("automation_state") in {"contracted", "protective_only"}
        ]
        paused_automation = [item for item in automation_decisions if item.get("automation_state") == "paused"]
        summary = {
            "configured_active_family": configured_family,
            "automatic_selection_enabled": bool(self.runtime.settings.strategy_family_auto_selection_enabled),
            "auto_parallel_enabled": bool(self.runtime.settings.strategy_sleeve_auto_parallel_enabled),
            "env_template_profile": self.runtime.settings.env_template_profile,
            "latest_selected_family": None if latest_snapshot is None else latest_snapshot.get("selected_family"),
            "latest_selected_strategy_sleeve_id": (
                None if latest_target_payload is None else latest_target_payload.get("strategy_sleeve_id")
            ),
            "latest_allocation_id": None if latest_target_payload is None else latest_target_payload.get("allocation_id"),
            "latest_selected_state": None if latest_snapshot is None else latest_snapshot.get("selected_state"),
            "latest_selected_route_action": None if latest_snapshot is None else latest_snapshot.get("selected_route_action"),
            "latest_selected_pair_id": (
                None if selected_candidate_payload is None else selected_candidate_payload.get("pair_id")
            ),
            "latest_selected_opportunity_kind": (
                None if selected_candidate_payload is None else selected_candidate_payload.get("opportunity_kind")
            ),
            "latest_selected_execution_mode": (
                None if selected_candidate_payload is None else selected_candidate_payload.get("execution_mode")
            ),
            "latest_selected_state_phase": (
                None if selected_candidate_payload is None else selected_candidate_payload.get("state_phase")
            ),
            "latest_bundle_status": None if latest_bundle is None else latest_bundle.get("status"),
            "latest_bundle_id": None if latest_bundle is None else latest_bundle.get("bundle_id"),
            "latest_bundle_type": None if latest_bundle is None else latest_bundle.get("bundle_type"),
            "latest_bundle_priority": None if latest_bundle is None else latest_bundle.get("bundle_priority"),
            "latest_bundle_gross_requested_exposure": (
                None if latest_bundle is None else latest_bundle.get("gross_requested_exposure")
            ),
            "latest_bundle_net_approved_exposure": (
                None if latest_bundle is None else latest_bundle.get("net_approved_exposure")
            ),
            "latest_allocator_version": (
                None if latest_allocation_decision is None else latest_allocation_decision.get("allocator_version")
            ),
            "latest_approved_families": (
                [] if latest_allocation_decision is None else list(latest_allocation_decision.get("approved_families") or [])
            ),
            "latest_active_families": (
                [] if latest_allocation_decision is None else list(latest_allocation_decision.get("active_families") or [])
            ),
            "latest_approved_sleeve_weights": (
                {} if latest_allocation_decision is None else dict(latest_allocation_decision.get("approved_sleeve_weights") or {})
            ),
            "latest_budget_profile_count": len(recent_budget_profiles),
            "latest_budget_assignment_count": len(recent_budget_assignments),
            "latest_budget_snapshot_count": len(recent_budget_snapshots),
            "latest_conflict_resolution_count": len(recent_conflict_resolutions),
            "latest_netting_decision_count": len(recent_netting_decisions),
            "latest_portfolio_risk_budget_state": (
                None if latest_allocation_decision is None else latest_allocation_decision.get("portfolio_risk_budget_state")
            ),
            "latest_portfolio_requested_notional": (
                None if latest_allocation_decision is None else latest_allocation_decision.get("portfolio_requested_notional")
            ),
            "latest_portfolio_approved_notional": (
                None if latest_allocation_decision is None else latest_allocation_decision.get("portfolio_approved_notional")
            ),
            "latest_portfolio_budget_cut_notional": (
                None if latest_allocation_decision is None else latest_allocation_decision.get("portfolio_budget_cut_notional")
            ),
            "latest_expected_edge_bps": (
                None if latest_allocation_decision is None else latest_allocation_decision.get("expected_edge_bps")
            ),
            "latest_expected_cost_bps": (
                None if latest_allocation_decision is None else latest_allocation_decision.get("expected_cost_bps")
            ),
            "latest_hedge_protected_notional": (
                None if latest_allocation_decision is None else latest_allocation_decision.get("hedge_protected_notional")
            ),
            "latest_directional_reduced_notional": (
                None if latest_allocation_decision is None else latest_allocation_decision.get("directional_reduced_notional")
            ),
            "latest_selection_reason_codes": []
            if latest_snapshot is None
            else list(latest_snapshot.get("selection_reason_codes") or []),
            "protective_fallback_active": bool(
                latest_snapshot is not None and latest_snapshot.get("selected_route_action") == "protective_fallback"
            ),
            "automation_active_count": len(active_automation),
            "automation_contracted_count": len(contracted_automation),
            "automation_paused_count": len(paused_automation),
            "latest_automation_states": {
                item.get("family"): item.get("automation_state")
                for item in automation_decisions
            },
            "operator_summary": (
                "当前还没有产生多策略协调快照。"
                if latest_snapshot is None
                else "当前多策略协调结果已经生成。"
            ),
        }
        route_action = summary["latest_selected_route_action"]
        if latest_snapshot is not None:
            if route_action == "override_target":
                summary["operator_summary"] = "当前选中的策略家族正在直接接管本轮目标仓位。"
            elif route_action == "protective_fallback":
                summary["operator_summary"] = "当前选中的策略家族没有直接接管仓位，系统保留了方向策略的保护性减仓或退出。"
            elif route_action == "advisory_only":
                summary["operator_summary"] = "当前选中的策略家族只提供参考，不会直接接管实盘执行。"
            else:
                summary["operator_summary"] = "当前选中的策略家族没有生成可执行目标，系统继续保持当前仓位。"
        smart_arbitrage_pairs = load_pair_definitions(
            settings=self.runtime.settings,
            primary_symbol=self.runtime.settings.default_symbol,
        )
        smart_arbitrage_pair_registry_warning_codes = list(
            dict.fromkeys(
                code
                for pair in smart_arbitrage_pairs
                for code in pair.metadata.get("configuration_warning_codes", [])
                if str(code).strip()
            )
        )
        smart_arbitrage_pair_registry_error_codes = list(
            dict.fromkeys(
                code
                for pair in smart_arbitrage_pairs
                for code in pair.metadata.get("configuration_error_codes", [])
                if str(code).strip()
            )
        )
        smart_arbitrage_cost_summary = self._smart_arbitrage_cost_summary(latest_snapshot=latest_snapshot)
        return {
            "generated_at": utc_now(),
            "summary": summary,
            "family_enablement": family_enablement,
            "configured_parameters": {
                "strategy_family_active": configured_family,
                "strategy_family_auto_selection_enabled": self.runtime.settings.strategy_family_auto_selection_enabled,
                "strategy_sleeve_auto_parallel_enabled": self.runtime.settings.strategy_sleeve_auto_parallel_enabled,
                "strategy_sleeve_auto_min_budget_multiplier": self.runtime.settings.strategy_sleeve_auto_min_budget_multiplier,
                "strategy_sleeve_auto_reconciliation_contraction_multiplier": self.runtime.settings.strategy_sleeve_auto_reconciliation_contraction_multiplier,
                "strategy_sleeve_auto_soft_loss_usdt": self.runtime.settings.strategy_sleeve_auto_soft_loss_usdt,
                "strategy_sleeve_auto_hard_loss_usdt": self.runtime.settings.strategy_sleeve_auto_hard_loss_usdt,
                "strategy_sleeve_auto_volatility_cap_enabled": self.runtime.settings.strategy_sleeve_auto_volatility_cap_enabled,
                "env_template_profile": self.runtime.settings.env_template_profile,
                "trade_costs": {
                    "rate_unit": "bps",
                    "rate_semantics": "percentage_basis_points",
                    "rate_example": "8 = 0.08%",
                    "live_fee_resolution": "account_schedule_fallback_to_configured",
                    "spot_maker_fee_bps": self.runtime.settings.trade_cost_spot_maker_fee_bps,
                    "spot_taker_fee_bps": self.runtime.settings.trade_cost_spot_taker_fee_bps,
                    "margin_maker_fee_bps": self.runtime.settings.trade_cost_margin_maker_fee_bps,
                    "margin_taker_fee_bps": self.runtime.settings.trade_cost_margin_taker_fee_bps,
                    "derivatives_maker_fee_bps": self.runtime.settings.trade_cost_derivatives_maker_fee_bps,
                    "derivatives_taker_fee_bps": self.runtime.settings.trade_cost_derivatives_taker_fee_bps,
                    "delivery_settlement_fee_bps": self.runtime.settings.trade_cost_delivery_settlement_fee_bps,
                    "spot_spread_bps": self.runtime.settings.trade_cost_spot_spread_bps,
                    "spot_slippage_bps": self.runtime.settings.trade_cost_spot_slippage_bps,
                    "margin_spread_bps": self.runtime.settings.trade_cost_margin_spread_bps,
                    "margin_slippage_bps": self.runtime.settings.trade_cost_margin_slippage_bps,
                    "derivatives_spread_bps": self.runtime.settings.trade_cost_derivatives_spread_bps,
                    "derivatives_slippage_bps": self.runtime.settings.trade_cost_derivatives_slippage_bps,
                },
                "smart_arbitrage": {
                    "enabled": self.runtime.settings.smart_arbitrage_enabled,
                    "pair_definitions": [
                        pair.model_dump(mode="json")
                        for pair in smart_arbitrage_pairs
                    ],
                    "pair_registry_warning_codes": smart_arbitrage_pair_registry_warning_codes,
                    "pair_registry_error_codes": smart_arbitrage_pair_registry_error_codes,
                    "basis_entry_bps": self.runtime.settings.smart_arbitrage_basis_entry_bps,
                    "basis_exit_bps": self.runtime.settings.smart_arbitrage_basis_exit_bps,
                    "estimated_cost_bps": self.runtime.settings.smart_arbitrage_estimated_cost_bps,
                    "uses_global_trade_costs": True,
                    "quote_budget_per_trade": self.runtime.settings.smart_arbitrage_quote_budget_per_trade,
                    "max_pair_notional": self.runtime.settings.smart_arbitrage_max_pair_notional,
                    "cost_model_enabled": self.runtime.settings.smart_arbitrage_cost_model_enabled,
                    "funding_cost_enabled": self.runtime.settings.smart_arbitrage_funding_cost_enabled,
                    "borrow_cost_enabled": self.runtime.settings.smart_arbitrage_borrow_cost_enabled,
                    "negative_basis_mode": self.runtime.settings.smart_arbitrage_negative_basis_mode,
                    "inventory_reservation_enabled": self.runtime.settings.smart_arbitrage_inventory_reservation_enabled,
                    "margin_short_enabled": self.runtime.settings.smart_arbitrage_margin_short_enabled,
                    "margin_short_execution_ready": self.runtime.settings.smart_arbitrage_margin_short_execution_ready,
                    "margin_short_spot_margin_mode": self.runtime.settings.smart_arbitrage_margin_short_spot_margin_mode,
                    "margin_short_auto_repay_enabled": self.runtime.settings.smart_arbitrage_margin_short_auto_repay_enabled,
                    "max_concurrent_pairs": self.runtime.settings.smart_arbitrage_max_concurrent_pairs,
                    "pair_priority_mode": self.runtime.settings.smart_arbitrage_pair_priority_mode,
                    "min_inventory_backed_ratio": self.runtime.settings.smart_arbitrage_min_inventory_backed_ratio,
                    "fee_source_mode": self.runtime.settings.smart_arbitrage_fee_source_mode,
                    "funding_source_mode": self.runtime.settings.smart_arbitrage_funding_source_mode,
                    "borrow_source_mode": self.runtime.settings.smart_arbitrage_borrow_source_mode,
                    "expected_hold_hours": self.runtime.settings.smart_arbitrage_expected_hold_hours,
                    "funding_interval_hours": self.runtime.settings.smart_arbitrage_funding_interval_hours,
                    "expected_funding_events": self.runtime.settings.smart_arbitrage_expected_funding_events,
                    "estimated_execution_mismatch_bps": self.runtime.settings.smart_arbitrage_estimated_execution_mismatch_bps,
                    "estimated_transfer_cost_bps": self.runtime.settings.smart_arbitrage_estimated_transfer_cost_bps,
                    "time_decay_bps_per_hour": self.runtime.settings.smart_arbitrage_time_decay_bps_per_hour,
                    "estimated_borrow_apr": self.runtime.settings.smart_arbitrage_estimated_borrow_apr,
                    "borrow_interest_free_ratio": self.runtime.settings.smart_arbitrage_borrow_interest_free_ratio,
                    "estimated_funding_bps": self.runtime.settings.smart_arbitrage_estimated_funding_bps,
                    "estimated_borrow_bps": self.runtime.settings.smart_arbitrage_estimated_borrow_bps,
                },
                "spot_grid": {
                    "enabled": self.runtime.settings.spot_grid_enabled,
                    "anchor_lookback_snapshots": self.runtime.settings.spot_grid_anchor_lookback_snapshots,
                    "band_bps": self.runtime.settings.spot_grid_band_bps,
                    "inventory_floor_fraction": self.runtime.settings.spot_grid_inventory_floor_fraction,
                    "inventory_ceiling_fraction": self.runtime.settings.spot_grid_inventory_ceiling_fraction,
                    "rebalance_min_fraction_of_max_qty": self.runtime.settings.spot_grid_rebalance_min_fraction_of_max_qty,
                    "breakout_guard_enabled": self.runtime.settings.spot_grid_breakout_guard_enabled,
                },
                "dca": {
                    "enabled": self.runtime.settings.dca_enabled,
                    "interval_seconds": self.runtime.settings.dca_interval_seconds,
                    "quote_budget_per_cycle": self.runtime.settings.dca_quote_budget_per_cycle,
                    "max_position_fraction_of_limit": self.runtime.settings.dca_max_position_fraction_of_limit,
                    "pullback_only_enabled": self.runtime.settings.dca_pullback_only_enabled,
                    "pullback_entry_bps": self.runtime.settings.dca_pullback_entry_bps,
                },
          },
            "latest_snapshot": latest_snapshot,
            "latest_allocation_decision": latest_allocation_decision,
            "latest_bundle": latest_bundle,
            "latest_applied_target": latest_target_payload,
            "strategy_sleeves": sleeve_records,
            "recent_sleeve_intents": recent_sleeve_intents,
            "recent_budget_profiles": recent_budget_profiles,
            "recent_budget_assignments": recent_budget_assignments,
            "recent_budget_snapshots": recent_budget_snapshots,
            "recent_conflict_resolutions": recent_conflict_resolutions,
            "recent_netting_decisions": recent_netting_decisions,
            "recent_snapshots": recent_snapshots,
            "recent_execution_bundles": recent_bundles,
            "smart_arbitrage_cost_summary": smart_arbitrage_cost_summary,
            "truth_source": "strategy_runtime_repo_plus_event_store" if strategy_runtime_repo is not None else "strategy_coordinator_snapshots",
        }

    def _smart_arbitrage_cost_summary(self, *, latest_snapshot: dict[str, Any] | None) -> dict[str, Any]:
        smart_candidate = None
        if latest_snapshot is not None:
            smart_candidate = next(
                (
                    candidate
                    for candidate in (latest_snapshot.get("candidates") or [])
                    if candidate.get("family") == "smart_arbitrage"
                ),
                None,
            )
        metrics = {} if smart_candidate is None else dict(smart_candidate.get("metrics") or {})
        fill_rows = [
            item
            for item in self._scoped_fill_outcomes()
            if str(getattr(item, "strategy_family", "") or "") == "smart_arbitrage"
        ]
        fill_notional = sum(
            (
                abs(self._to_decimal(getattr(item, "fill_notional", None)) or Decimal("0"))
                for item in fill_rows
            ),
            start=Decimal("0"),
        )
        realized_fee_amount = sum(
            (
                abs(
                    self._to_decimal(getattr(item, "fee_amount", None))
                    or self._to_decimal(getattr(item, "fee_delta", None))
                    or Decimal("0")
                )
                for item in fill_rows
            ),
            start=Decimal("0"),
        )
        funding_rows = list(self._scoped_funding_fee_records())
        smart_symbols = {
            str(metrics.get("spot_symbol") or "").upper(),
            str(metrics.get("derivatives_symbol") or "").upper(),
            *(
                str(item.get("spot_symbol") or "").upper()
                for item in (metrics.get("selected_pair_summaries") or [])
                if isinstance(item, dict)
            ),
            *(
                str(item.get("derivatives_symbol") or "").upper()
                for item in (metrics.get("selected_pair_summaries") or [])
                if isinstance(item, dict)
            ),
        }
        smart_symbols.discard("")
        funding_cost_amount = sum(
            (
                abs(self._to_decimal(getattr(item, "amount", None)) or Decimal("0"))
                for item in funding_rows
                if (
                    str(getattr(item, "funding_direction", "") or "").lower() == "expense"
                    and (
                        not smart_symbols
                        or str(getattr(item, "symbol", "") or "").upper() in smart_symbols
                    )
                )
            ),
            start=Decimal("0"),
        )
        realized_fee_bps = (
            None
            if fill_notional <= self._DECIMAL_EPSILON
            else (realized_fee_amount / fill_notional) * Decimal("10000")
        )
        realized_funding_bps = (
            None
            if fill_notional <= self._DECIMAL_EPSILON
            else (funding_cost_amount / fill_notional) * Decimal("10000")
        )
        realized_total_drag_bps = None
        if realized_fee_bps is not None or realized_funding_bps is not None:
            realized_total_drag_bps = (realized_fee_bps or Decimal("0")) + (realized_funding_bps or Decimal("0"))
        predicted_drag = self._to_decimal(metrics.get("executable_cost_bps"))
        return {
            "available": smart_candidate is not None,
            "pair_label": None if smart_candidate is None else (
                f"{metrics.get('spot_symbol') or '现货腿'} <-> {metrics.get('derivatives_symbol') or '合约腿'}"
                if not metrics.get("aggregate_candidate")
                else f"{len(metrics.get('selected_pair_summaries') or [])} 组套利对聚合"
            ),
            "predicted": {
                "basis_bps": metrics.get("basis_bps"),
                "ideal_cost_bps": metrics.get("ideal_cost_bps"),
                "executable_cost_bps": metrics.get("executable_cost_bps"),
                "ideal_edge_bps": metrics.get("ideal_edge_bps"),
                "executable_edge_bps": metrics.get("executable_edge_bps"),
                "breakeven_basis_bps": metrics.get("breakeven_basis_bps"),
                "ideal_total_fee_bps": metrics.get("ideal_total_fee_bps"),
                "executable_spread_bps": metrics.get("executable_spread_bps"),
                "executable_slippage_bps": metrics.get("executable_slippage_bps"),
                "execution_mismatch_bps": metrics.get("execution_mismatch_bps"),
                "funding_cost_bps": metrics.get("funding_cost_bps"),
                "borrow_cost_bps": metrics.get("borrow_cost_bps"),
                "transfer_cost_bps": metrics.get("transfer_cost_bps"),
                "time_decay_cost_bps": metrics.get("time_decay_cost_bps"),
                "expected_hold_hours": metrics.get("expected_hold_hours"),
                "expected_funding_events": metrics.get("expected_funding_events"),
                "borrow_hour_windows": metrics.get("borrow_hour_windows"),
                "cost_confidence": metrics.get("cost_confidence"),
                "cost_source_flags": list(metrics.get("cost_source_flags") or metrics.get("aggregate_cost_source_flags") or []),
            },
            "realized": {
                "fill_count": len(fill_rows),
                "fill_notional": fill_notional,
                "realized_fee_amount": realized_fee_amount,
                "realized_fee_bps": realized_fee_bps,
                "funding_fee_event_count": sum(
                    1
                    for item in funding_rows
                    if (
                        str(getattr(item, "funding_direction", "") or "").lower() == "expense"
                        and (
                            not smart_symbols
                            or str(getattr(item, "symbol", "") or "").upper() in smart_symbols
                        )
                    )
                ),
                "realized_funding_cost_amount": funding_cost_amount,
                "realized_funding_bps": realized_funding_bps,
                "realized_borrow_bps": None,
                "realized_total_drag_bps": realized_total_drag_bps,
            },
            "calibration": {
                "predicted_vs_realized_total_drag_error_bps": (
                    None
                    if predicted_drag is None or realized_total_drag_bps is None
                    else predicted_drag - realized_total_drag_bps
                ),
                "realized_source_flags": [
                    "fill_outcomes_fee_bps",
                    "funding_fee_records_expense_only",
                    "borrow_realized_unavailable",
                ],
            },
        }

    def recent_fills(self, *, limit: int = 50):
        return sorted(
            self._scoped_fills(),
            key=fill_processing_sort_key,
            reverse=True,
        )[:limit]

    def latest_decision_id(self) -> str | None:
        latest = self._cached("latest_decision_record", self.runtime.audit_repo.latest)
        return latest.decision_id if latest is not None else None

    def ai_runtime(self) -> dict[str, Any]:
        return self._cached("ai_runtime", self.runtime_queries.ai_runtime)

    def _recent_ai_shadow_evaluation_events(self, *, limit: int | None = None):
        if not self._ai_history_visible():
            return []
        rows = list(
            reversed(
                self.runtime.event_store.by_topic_scoped(
                    topics.AI_SHADOW_EVALUATIONS,
                    scope=self.state_scope,
                )
            )
        )
        if limit is None:
            return rows
        return rows[:limit]

    def _recent_ai_performance_report_events(self, *, limit: int | None = None):
        if not self._ai_history_visible():
            return []
        rows = list(
            reversed(
                self.runtime.event_store.by_topic_scoped(
                    topics.AI_PERFORMANCE_REPORTS,
                    scope=self.state_scope,
                )
            )
        )
        if limit is None:
            return rows
        return rows[:limit]

    def _recent_strategy_profile_optimization_report_events(self, *, limit: int | None = None):
        rows = list(
            reversed(
                self.runtime.event_store.by_topic_scoped(
                    topics.STRATEGY_PROFILE_OPTIMIZATION_REPORTS,
                    scope=self.state_scope,
                )
            )
        )
        if limit is None:
            return rows
        return rows[:limit]

    def _recent_strategy_profile_selection_decision_events(self, *, limit: int | None = None):
        rows = list(
            reversed(
                self.runtime.event_store.by_topic_scoped(
                    topics.STRATEGY_PROFILE_SELECTION_DECISIONS,
                    scope=self.state_scope,
                )
            )
        )
        if limit is None:
            return rows
        return rows[:limit]

    @staticmethod
    def _ratio(numerator: int, denominator: int) -> float:
        if denominator <= 0:
            return 0.0
        return round(numerator / denominator, 6)

    def _ai_shadow_summary(self, *, limit: int = 10) -> dict[str, Any]:
        payloads = [self.payload(item) for item in self._recent_ai_shadow_evaluation_events(limit=limit)]
        payloads = [item for item in payloads if item is not None]
        latest = payloads[0] if payloads else None
        ai_runtime = self.runtime.ai_service.status()
        outperformed_count = sum(1 for item in payloads if item.get("shadow_outperformed") is True)
        underperformed_count = sum(1 for item in payloads if item.get("shadow_outperformed") is False)
        if latest is None:
            return {
                "window_count": 0,
                "status": "insufficient_data",
                "review_required": False,
                "outperformed_count": 0,
                "underperformed_count": 0,
                "outperformed_rate": 0.0,
                "latest_evaluation": None,
                "latest_net_pnl_delta": None,
                "latest_fee_ratio_delta": None,
                "latest_churn_ratio_delta": None,
            }
        latest_net_pnl_delta = (
            Decimal(str(latest.get("shadow_net_pnl") or "0"))
            - Decimal(str(latest.get("baseline_net_pnl") or "0"))
        )
        latest_fee_ratio_delta = round(
            float(latest.get("shadow_fee_ratio") or 0.0) - float(latest.get("baseline_fee_ratio") or 0.0),
            6,
        )
        latest_churn_ratio_delta = round(
            float(latest.get("shadow_churn_ratio") or 0.0) - float(latest.get("baseline_churn_ratio") or 0.0),
            6,
        )
        review_required = bool(ai_runtime.get("outcome_review_required"))
        status = "healthy"
        if review_required:
            status = "review_required"
        elif latest.get("shadow_outperformed") is None:
            status = "insufficient_data"
        elif latest.get("shadow_outperformed") is False:
            status = "underperforming"
        elif outperformed_count and underperformed_count:
            status = "mixed"
        return {
            "window_count": len(payloads),
            "status": status,
            "review_required": review_required,
            "outperformed_count": outperformed_count,
            "underperformed_count": underperformed_count,
            "outperformed_rate": self._ratio(outperformed_count, len(payloads)),
            "latest_evaluation": latest,
            "latest_net_pnl_delta": latest_net_pnl_delta,
            "latest_fee_ratio_delta": latest_fee_ratio_delta,
            "latest_churn_ratio_delta": latest_churn_ratio_delta,
        }

    def _ai_shadow_performance_windows(self) -> dict[str, Any]:
        latest_report_payload = self._latest_ai_performance_report_payload()
        if latest_report_payload is not None and isinstance(latest_report_payload.get("windows"), dict):
            return latest_report_payload["windows"]
        evaluations = [self.payload(item) for item in self._recent_ai_shadow_evaluation_events(limit=40)]
        evaluations = [item for item in evaluations if item is not None]
        windows = {
            "short": {"sample_size": 3, "label": "recent_3_windows"},
            "medium": {"sample_size": 5, "label": "recent_5_windows"},
            "long": {"sample_size": 10, "label": "recent_10_windows"},
        }
        result: dict[str, Any] = {}
        for key, config in windows.items():
            sample_size = config["sample_size"]
            rows = evaluations[:sample_size]
            if not rows:
                result[key] = {
                    "label": config["label"],
                    "sample_size": 0,
                    "outperformed_rate": 0.0,
                    "baseline_net_pnl_total": None,
                    "shadow_net_pnl_total": None,
                    "net_pnl_delta_total": None,
                    "avg_fee_ratio_delta": None,
                    "avg_churn_ratio_delta": None,
                    "review_required_count": 0,
                }
                continue
            baseline_net_total = sum(Decimal(str(item.get("baseline_net_pnl") or "0")) for item in rows)
            shadow_net_total = sum(Decimal(str(item.get("shadow_net_pnl") or "0")) for item in rows)
            fee_ratio_deltas = [
                float(item.get("shadow_fee_ratio") or 0.0) - float(item.get("baseline_fee_ratio") or 0.0)
                for item in rows
            ]
            churn_ratio_deltas = [
                float(item.get("shadow_churn_ratio") or 0.0) - float(item.get("baseline_churn_ratio") or 0.0)
                for item in rows
            ]
            outperformed = sum(1 for item in rows if item.get("shadow_outperformed") is True)
            review_required_count = sum(
                1
                for item in rows
                if (
                    (float(item.get("shadow_fee_ratio") or 0.0) - float(item.get("baseline_fee_ratio") or 0.0))
                    > self.runtime.settings.ai_outcome_max_fee_ratio_delta
                )
                or (
                    (float(item.get("shadow_churn_ratio") or 0.0) - float(item.get("baseline_churn_ratio") or 0.0))
                    > self.runtime.settings.ai_outcome_max_churn_ratio_delta
                )
                or item.get("shadow_outperformed") is False
            )
            result[key] = {
                "label": config["label"],
                "sample_size": len(rows),
                "outperformed_rate": self._ratio(outperformed, len(rows)),
                "baseline_net_pnl_total": baseline_net_total,
                "shadow_net_pnl_total": shadow_net_total,
                "net_pnl_delta_total": shadow_net_total - baseline_net_total,
                "avg_fee_ratio_delta": round(sum(fee_ratio_deltas) / len(fee_ratio_deltas), 6),
                "avg_churn_ratio_delta": round(sum(churn_ratio_deltas) / len(churn_ratio_deltas), 6),
                "review_required_count": review_required_count,
            }
        return result

    def _latest_ai_performance_report_payload(self) -> dict[str, Any] | None:
        rows = [self.payload(item) for item in self._recent_ai_performance_report_events(limit=1)]
        rows = [item for item in rows if item is not None]
        return rows[0] if rows else None

    def _ai_performance_overview_impl(self) -> dict[str, Any]:
        reports = [self.payload(item) for item in self._recent_ai_performance_report_events(limit=20)]
        reports = [item for item in reports if item is not None]
        latest = reports[0] if reports else None
        recent_replay = self.replay_recent_validations(limit=10, offset=0)["validations"]
        if latest is None:
            return {
                "latest_report": None,
                "report_count": 0,
                "status_counts": {},
                "trend": {
                    "avg_short_net_pnl_delta": None,
                    "avg_medium_net_pnl_delta": None,
                    "avg_long_net_pnl_delta": None,
                    "review_required_rate": 0.0,
                },
                "recent_reports": [],
                "replay_context": {
                    "validation_count": len(recent_replay),
                    "healthy_rate": self._ratio(
                        sum(1 for item in recent_replay if item.get("healthy")),
                        len(recent_replay),
                    ),
                    "latest_validation": recent_replay[0] if recent_replay else None,
                },
            }
        status_counts: dict[str, int] = {}
        short_deltas: list[Decimal] = []
        medium_deltas: list[Decimal] = []
        long_deltas: list[Decimal] = []
        review_required_count = 0
        for report in reports:
            status = str(report.get("latest_status") or "insufficient_data")
            status_counts[status] = status_counts.get(status, 0) + 1
            if report.get("review_required"):
                review_required_count += 1
            windows = report.get("windows") or {}
            for target, bucket in (("short", short_deltas), ("medium", medium_deltas), ("long", long_deltas)):
                value = ((windows.get(target) or {}).get("net_pnl_delta_total"))
                if value is not None:
                    bucket.append(Decimal(str(value)))
        return {
            "latest_report": latest,
            "report_count": len(reports),
            "status_counts": status_counts,
            "trend": {
                "avg_short_net_pnl_delta": (sum(short_deltas, start=Decimal("0")) / Decimal(len(short_deltas))) if short_deltas else None,
                "avg_medium_net_pnl_delta": (sum(medium_deltas, start=Decimal("0")) / Decimal(len(medium_deltas))) if medium_deltas else None,
                "avg_long_net_pnl_delta": (sum(long_deltas, start=Decimal("0")) / Decimal(len(long_deltas))) if long_deltas else None,
                "review_required_rate": self._ratio(review_required_count, len(reports)),
            },
            "recent_reports": reports[:10],
            "replay_context": {
                "validation_count": len(recent_replay),
                "healthy_rate": self._ratio(
                    sum(1 for item in recent_replay if item.get("healthy")),
                    len(recent_replay),
                ),
                "latest_validation": recent_replay[0] if recent_replay else None,
            },
        }

    def ai_performance_overview(self) -> dict[str, Any]:
        return self.runtime_queries.ai_performance_overview()

    def _ai_downgrade_state(self) -> dict[str, Any]:
        runtime = self.ai_runtime()
        return {
            "configured_mode": runtime.get("configured_operating_mode"),
            "effective_mode": runtime.get("effective_operating_mode"),
            "legacy_modes": runtime.get("legacy_modes"),
            "provider_state": runtime.get("provider_state"),
            "outcome_state": runtime.get("outcome_state"),
            "degraded": runtime.get("degraded"),
            "provider_degraded": runtime.get("provider_degraded"),
            "outcome_review_required": runtime.get("outcome_review_required"),
            "outcome_auto_downgrade_active": runtime.get("outcome_auto_downgrade_active"),
            "degradation_reason": runtime.get("degradation_reason"),
            "outcome_degradation_reason": runtime.get("outcome_degradation_reason"),
            "last_provider_degraded_at": runtime.get("last_provider_degraded_at"),
            "last_provider_recovered_at": runtime.get("last_provider_recovered_at"),
            "last_outcome_degraded_at": runtime.get("last_outcome_degraded_at"),
            "last_outcome_recovered_at": runtime.get("last_outcome_recovered_at"),
            "recovery_probe_after": runtime.get("recovery_probe_after"),
            "recovery_probe_ready": runtime.get("recovery_probe_ready"),
            "failure_budget": runtime.get("failure_budget"),
            "outcome_policy": runtime.get("outcome_policy"),
        }

    def ai_overview(self) -> dict[str, Any]:
        return self._cached("ai_overview", self.runtime_queries.ai_overview)

    def ai_config_summary(self) -> dict[str, Any]:
        runtime = self.ai_runtime()
        latest_decision_id = self.latest_decision_id()
        latest_decision_detail = self.decision_view(latest_decision_id) if latest_decision_id is not None else None
        return {
            "runtime_profile": self.runtime_profile_ai_config_snapshot(),
            "strategy_profile": self.strategy_profile_ai_config_snapshot(),
            "ai": {
                "configured_operating_mode": runtime.get("configured_operating_mode"),
                "effective_operating_mode": runtime.get("effective_operating_mode"),
                "shadow_mode_enabled": runtime.get("shadow_mode_enabled", False),
                "strategy_profile_auto_control_configured": runtime.get("strategy_profile_auto_control_configured", False),
                "strategy_profile_auto_control_effective": runtime.get("strategy_profile_auto_control_effective", False),
                "strategy_profile_auto_control_reason": runtime.get("strategy_profile_auto_control_reason"),
                "shadow_summary": self._ai_shadow_summary(),
                "latest_profile_control_decision": None
                if latest_decision_detail is None
                else latest_decision_detail.get("profile_control_decision"),
            },
        }

    def _ai_history_visible(self) -> bool:
        return self.runtime.settings.canonical_ai_operating_mode != "baseline_only"

    def ai_latest(self) -> dict[str, Any]:
        return self._cached("ai_latest", self.runtime_queries.ai_latest)

    def ai_recent(self, *, limit: int, offset: int) -> dict[str, Any]:
        normalized_limit = max(int(limit), 1)
        normalized_offset = max(int(offset), 0)
        cache_key = (
            f"ai_recent:{self._scope_cache_fragment()}:"
            f"{normalized_limit}:{normalized_offset}"
        )
        return self._cached_ttl(
            cache_key,
            20,
            lambda: self.runtime_queries.ai_recent(limit=normalized_limit, offset=normalized_offset),
        )

    def ai_shadow_latest(self) -> dict[str, Any]:
        return self._cached("ai_shadow_latest", self.runtime_queries.ai_shadow_latest)

    def ai_shadow_recent(self, *, limit: int, offset: int) -> dict[str, Any]:
        normalized_limit = max(int(limit), 1)
        normalized_offset = max(int(offset), 0)
        cache_key = (
            f"ai_shadow_recent:{self._scope_cache_fragment()}:"
            f"{normalized_limit}:{normalized_offset}"
        )
        return self._cached_ttl(
            cache_key,
            20,
            lambda: self.runtime_queries.ai_shadow_recent(limit=normalized_limit, offset=normalized_offset),
        )

    def ai_shadow_evaluations(self, *, limit: int, offset: int) -> dict[str, Any]:
        normalized_limit = max(int(limit), 1)
        normalized_offset = max(int(offset), 0)
        cache_key = (
            f"ai_shadow_evaluations:{self._scope_cache_fragment()}:"
            f"{normalized_limit}:{normalized_offset}"
        )
        return self._cached_ttl(
            cache_key,
            30,
            lambda: self.runtime_queries.ai_shadow_evaluations(limit=normalized_limit, offset=normalized_offset),
        )

    def ai_performance_reports(self, *, limit: int, offset: int) -> dict[str, Any]:
        return self.runtime_queries.ai_performance_reports(limit=limit, offset=offset)

    def latest_operator_action(self, action: str) -> dict[str, Any] | None:
        actions = self._cached(
            "operator_action_events",
            lambda: self.runtime.event_store.by_topic(topics.OPERATOR_ACTIONS),
        )
        for item in reversed(actions):
            if item.payload.get("action") == action:
                return item.payload
        return None

    def recent_operator_actions(
        self,
        *,
        action: str | None = None,
        key: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        actions = self._cached(
            "operator_action_events",
            lambda: self.runtime.event_store.by_topic(topics.OPERATOR_ACTIONS),
        )
        rows: list[dict[str, Any]] = []
        for item in reversed(actions):
            if action is not None and item.payload.get("action") != action:
                continue
            if key is not None and item.key != key:
                continue
            payload = self.payload(item)
            if payload is not None:
                rows.append(payload)
        return self._paginate_rows(rows, limit=limit, offset=offset, key="actions")

    def record_operator_login(
        self,
        *,
        actor_identity: str,
        actor_role: OperatorRole,
        auth_source: AuthSource = "session",
    ) -> None:
        self._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="auth",
            payload_model=OperatorActionRecord(
                action="login",
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
                reason="operator_login",
                status="login_succeeded",
                details={"database_backed": self.runtime.database_runtime is not None},
            ),
        )

    def record_operator_login_failure(
        self,
        *,
        actor_identity: str | None,
        auth_source: AuthSource = "session",
        failure_code: str = "operator_login_failed",
    ) -> None:
        self._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="auth",
            payload_model=OperatorActionRecord(
                action="login",
                actor_role="anonymous",
                actor_identity=actor_identity,
                auth_source=auth_source,
                reason="operator_login",
                status="login_failed",
                details={
                    "database_backed": self.runtime.database_runtime is not None,
                    "failure_code": failure_code,
                },
            ),
        )

    def operator_users(self, *, actor_identity: str | None = None) -> dict[str, Any]:
        users = self.runtime.operator_repo.all_users()
        protected_last_admin = enabled_admin_count(self.runtime.operator_repo) <= 1
        return {
            "users": [
                self._operator_user_view(
                    user,
                    actor_identity=actor_identity,
                    last_admin_protected=protected_last_admin,
                )
                for user in users
            ],
            "enabled_user_count": self.runtime.operator_repo.count(enabled_only=True),
            "enabled_admin_count": enabled_admin_count(self.runtime.operator_repo),
        }

    def runtime_profile_snapshot(self) -> dict[str, Any]:
        snapshot = self._cached(
            "runtime_profile_snapshot",
            lambda: readonly_runtime_profile_snapshot(
                settings=self.runtime.settings,
                resolution=self.runtime.runtime_profile_resolution,
            ),
        )
        return snapshot

    def record_phase1_shadow_review(
        self,
        *,
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None = None,
        auth_source: AuthSource = "anonymous",
    ) -> dict[str, Any]:
        snapshot = self._build_phase1_shadow()
        recovery_before = self.recovery_view()["recovery_state"]
        action = OperatorActionRecord(
            action="phase1_shadow_review",
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
            reason=reason,
            status="review_recorded",
            recovery_state_before=recovery_before,
            recovery_state_after=recovery_before,
            details={
                "snapshot_status": snapshot.get("status"),
                "summary": snapshot.get("summary"),
                "lag": snapshot.get("lag"),
                "blockers": snapshot.get("blockers", []),
                "reviewed_at": utc_now(),
                "snapshot_generated_at": utc_now(),
            },
        )
        envelope = self._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="phase1_shadow",
            payload_model=action,
        )
        self._persist_blocker_snapshot(
            source="phase1_shadow_review",
            runtime_state=self.system_health()["runtime_state"],
            mode_snapshot=self.system_mode(),
            blockers=self.blockers(),
        )
        payload = action.model_dump(mode="json")
        payload["_event_id"] = envelope.event_id
        payload["_topic"] = envelope.topic
        return payload

    def runtime_profile_ai_config_snapshot(self) -> dict[str, Any]:
        snapshot = self.runtime_profile_snapshot()
        return {
            "profile_source": snapshot.get("profile_source"),
            "control_plane_status": snapshot.get("control_plane_status"),
            "current_runtime_payload": snapshot.get("current_runtime_payload"),
        }

    def strategy_profile_snapshot(self) -> dict[str, Any]:
        return self.strategy_profile_queries.snapshot()

    def strategy_profile_ai_config_snapshot(self) -> dict[str, Any]:
        return self.strategy_profile_queries.ai_config_snapshot()

    def strategy_profile_optimization_reports(self, *, limit: int, offset: int) -> dict[str, Any]:
        return self.strategy_profile_queries.optimization_reports(limit=limit, offset=offset)

    def strategy_profile_selection_decisions(self, *, limit: int, offset: int) -> dict[str, Any]:
        return self.strategy_profile_queries.selection_decisions(limit=limit, offset=offset)

    def strategy_profile_activation_history(self, *, limit: int, offset: int) -> dict[str, Any]:
        return self.strategy_profile_queries.activation_history(limit=limit, offset=offset)

    def profile_control_summary_report(self) -> dict[str, Any]:
        cache_key = f"profile_control_summary:{self._scope_cache_fragment()}"
        return self._cached_ttl(cache_key, 20, self._build_profile_control_summary_report)

    def _build_profile_control_summary_report(self) -> dict[str, Any]:
        snapshot = self.strategy_profile_queries.snapshot()
        latest_optimization = snapshot.get("latest_optimization_report") or {}
        latest_selection = snapshot.get("latest_selection_decision") or {}
        return {
            "control_summary": snapshot.get("control_summary") or {},
            "activation": snapshot.get("activation") or {},
            "active_revision": snapshot.get("active_revision"),
            "latest_selection_decision": latest_selection,
            "latest_optimization_report": {
                "recommended_profile_id": latest_optimization.get("recommended_profile_id"),
                "score_delta_vs_active": latest_optimization.get("score_delta_vs_active"),
                "control_summary": latest_optimization.get("control_summary") or {},
                "winner_selection_policy": latest_optimization.get("winner_selection_policy") or {},
                "notes": latest_optimization.get("notes") or [],
            },
        }

    def activate_strategy_profile(
        self,
        *,
        profile_id: str,
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
    ) -> dict[str, Any]:
        return self.strategy_profile_queries.activate_profile(
            profile_id=profile_id,
            reason=reason,
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
        )

    def restore_strategy_profile_auto(
        self,
        *,
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
    ) -> dict[str, Any]:
        return self.strategy_profile_queries.restore_auto(
            reason=reason,
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
        )

    def pause_strategy_profile_auto(
        self,
        *,
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
    ) -> dict[str, Any]:
        return self.strategy_profile_queries.pause_auto(
            reason=reason,
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
        )

    def create_operator_user(
        self,
        *,
        username: str,
        password: str,
        role: OperatorRole,
        enabled: bool,
        actor_role: OperatorRole,
        actor_identity: str | None = None,
        auth_source: AuthSource = "anonymous",
    ) -> dict[str, Any]:
        user = create_managed_operator_user(
            self.runtime.operator_repo,
            username=username,
            password=password,
            role=role,
            enabled=enabled,
        )
        self._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="auth",
            payload_model=OperatorActionRecord(
                action="user_create",
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
                reason="operator_user_create",
                status="user_created",
                details={
                    "target_username": user.username,
                    "target_role": user.role,
                    "target_enabled": user.enabled,
                },
            ),
        )
        return {"user": self._operator_user_view(user, actor_identity=actor_identity)}

    def update_operator_user(
        self,
        *,
        username: str,
        role: OperatorRole | None = None,
        enabled: bool | None = None,
        password: str | None = None,
        actor_role: OperatorRole,
        actor_identity: str | None = None,
        auth_source: AuthSource = "anonymous",
    ) -> dict[str, Any]:
        user, changes = update_managed_operator_user(
            self.runtime.operator_repo,
            username=username,
            role=role,
            enabled=enabled,
            password=password,
            actor_identity=actor_identity,
        )
        self._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="auth",
            payload_model=OperatorActionRecord(
                action="user_update",
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
                reason="operator_user_update",
                status="user_updated",
                details={
                    "target_username": user.username,
                    "changes": changes,
                },
            ),
        )
        return {
            "user": self._operator_user_view(user, actor_identity=actor_identity),
            "changes": changes,
        }

    def delete_operator_user(
        self,
        *,
        username: str,
        actor_role: OperatorRole,
        actor_identity: str | None = None,
        auth_source: AuthSource = "anonymous",
    ) -> dict[str, Any]:
        user = delete_managed_operator_user(
            self.runtime.operator_repo,
            username=username,
            actor_identity=actor_identity,
        )
        self._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="auth",
            payload_model=OperatorActionRecord(
                action="user_delete",
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
                reason="operator_user_delete",
                status="user_deleted",
                details={
                    "target_username": user.username,
                    "target_role": user.role,
                },
            ),
        )
        return {"status": "deleted", "user": self._operator_user_view(user, actor_identity=actor_identity)}

    def recovery_view(self) -> dict[str, Any]:
        return self.recovery_queries.recovery_view()

    def _build_recovery_view(self) -> dict[str, Any]:
        return self.recovery_queries.build_recovery_view()

    def system_recovery(self) -> dict[str, Any]:
        return self.recovery_queries.system_recovery()

    def system_mode(self) -> dict[str, Any]:
        return self.recovery_queries.system_mode()

    def system_health(self) -> dict[str, Any]:
        return self._cached("system_health", self.runtime_queries.system_health)

    def system_runtime(self) -> dict[str, Any]:
        return self._cached("system_runtime", self.runtime_queries.system_runtime)

    def blockers(self) -> list[dict[str, Any]]:
        return self.blocker_queries.blockers()

    def blocker_control(self) -> dict[str, Any]:
        return self.blocker_queries.blocker_control()

    async def perform_blocker_action(
        self,
        *,
        action_id: str,
        panel_version: str | None,
        blocker: str | None,
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None = None,
        auth_source: AuthSource = "anonymous",
    ) -> dict[str, Any]:
        return (
            await self.blocker_action_service.execute(
                action_id=action_id,
                panel_version=panel_version,
                blocker=blocker,
                reason=reason,
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
            )
        ).model_dump(mode="json")

    def blocker_history(self, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        return self.blocker_queries.blocker_history(limit=limit, offset=offset)

    def metrics(self) -> dict[str, Any]:
        cache_key = f"metrics:{self._scope_cache_fragment()}"
        return self._cached_ttl(cache_key, 15, self.runtime_queries.metrics)

    def phase1_shadow(self) -> dict[str, Any]:
        cache_key = f"phase1_shadow:{self._scope_cache_fragment()}"
        return self._cached_ttl(cache_key, 10, self._build_phase1_shadow)

    def phase1_shadow_history(self, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        return self.runtime_queries.phase1_shadow_history(limit=limit, offset=offset)

    def trial_guard(self) -> dict[str, Any]:
        service = getattr(self.runtime, "trial_guard_service", None)
        if service is None:
            return {
                "enabled": False,
                "enabled_for_runtime": False,
                "trial_observation_active": False,
                "trial_observation_label": None,
                "status": "not_configured",
                "summary": "试盘守护未配置。",
                "hard_stop": {
                    "active": False,
                    "halt_required": False,
                    "resume_blocked": False,
                    "breach_count": 0,
                    "summary": "试盘守护未配置。",
                    "operator_guidance": "如果这条线是小资金试盘场景，先确认是否应该启用试盘守护。",
                },
                "recovery_requirements": {
                    "resume_allowed": True,
                    "items": [],
                },
            }
        cache_key = f"trial_guard:{self._scope_cache_fragment()}"
        return self._cached_ttl(cache_key, 15, service.snapshot)

    def _build_system_mode(self) -> dict[str, Any]:
        return self.recovery_queries.build_system_mode()

    def _effective_taker_fee_bps(self, *, symbol: str | None = None) -> float:
        resolver = getattr(self.runtime, "fee_resolver", None)
        if resolver is not None and hasattr(resolver, "taker_fee_bps"):
            return float(resolver.taker_fee_bps(symbol=symbol))
        getter = getattr(self.runtime.account_service, "effective_taker_fee_bps", None)
        if callable(getter):
            try:
                resolved = getter(symbol=symbol)
            except TypeError:
                resolved = getter(symbol) if symbol is not None else getter()
            if resolved is not None:
                return max(float(resolved), 0.0)
        return max(self.runtime.settings.paper_taker_fee_bps, 0.0)

    def _effective_maker_fee_bps(self, *, symbol: str | None = None) -> float:
        resolver = getattr(self.runtime, "fee_resolver", None)
        if resolver is not None and hasattr(resolver, "maker_fee_bps"):
            return float(resolver.maker_fee_bps(symbol=symbol))
        return self._effective_taker_fee_bps(symbol=symbol)

    def _funding_fee_bps(self, *, symbol: str | None = None) -> float:
        resolver = getattr(self.runtime, "fee_resolver", None)
        if resolver is not None and hasattr(resolver, "funding_fee_bps"):
            return float(resolver.funding_fee_bps(symbol=symbol))
        return 0.0

    @staticmethod
    def _execution_suggestion_payload(ai_assessment: dict[str, Any] | None) -> dict[str, Any] | None:
        envelope = None if ai_assessment is None else ai_assessment.get("ai_execution_parameter_suggestion")
        if not isinstance(envelope, dict):
            return None
        suggestion = envelope.get("suggestion")
        return suggestion if isinstance(suggestion, dict) else None

    def _estimated_execution_fee_bps(self, *, symbol: str | None, ai_assessment: dict[str, Any] | None) -> float:
        resolver = getattr(self.runtime, "fee_resolver", None)
        if resolver is None or not hasattr(resolver, "estimated_execution_fee_bps"):
            return self._effective_taker_fee_bps(symbol=symbol)
        suggestion = self._execution_suggestion_payload(ai_assessment)
        return float(
            resolver.estimated_execution_fee_bps(
                symbol=symbol,
                execution_style="bounded_limit_ioc" if suggestion is not None else "taker",
                order_type="limit" if suggestion is not None else "market",
                passive_bias=None if suggestion is None else suggestion.get("passive_bias"),
                maker_taker_bias=None if suggestion is None else suggestion.get("maker_taker_bias"),
            )
        )

    def _ai_economic_actionability(
        self,
        *,
        ai_assessment: dict[str, Any] | None,
        position_target: dict[str, Any] | None,
        ai_decision_brief: dict[str, Any] | None,
        strategy_execution_health: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if ai_assessment is None:
            return None
        symbol = None
        if position_target is not None:
            symbol = position_target.get("symbol")
        if not symbol:
            symbol = ai_assessment.get("symbol")
        effective_taker_fee_bps = self._effective_taker_fee_bps(symbol=symbol)
        effective_maker_fee_bps = self._effective_maker_fee_bps(symbol=symbol)
        estimated_execution_fee_bps = self._estimated_execution_fee_bps(symbol=symbol, ai_assessment=ai_assessment)
        funding_fee_bps = (
            self._funding_fee_bps(symbol=symbol)
            if self.runtime.settings.trading_product_type == "derivatives"
            else 0.0
        )
        required_total_edge_bps = (
            estimated_execution_fee_bps
            + (
                max(self.runtime.settings.max_slippage_tolerance_bps, 0)
                * max(self.runtime.settings.strategy_expected_slippage_bps_fraction, 0.0)
            )
            + funding_fee_bps
            + max(self.runtime.settings.strategy_edge_noise_buffer_bps, 0.0)
            + max(self.runtime.settings.strategy_min_net_edge_bps, 0.0)
        )
        funding_fee_summary = None
        if hasattr(self.runtime.account_service, "recent_funding_fee_summary"):
            funding_fee_summary = self.runtime.account_service.recent_funding_fee_summary(symbol=symbol)
        return {
            "economically_actionable": ai_assessment.get("economically_actionable"),
            "fallback_used": ai_assessment.get("fallback_used"),
            "degraded": ai_assessment.get("degraded"),
            "estimated_edge_bps": ai_assessment.get("estimated_edge_bps"),
            "estimated_cost_bps": ai_assessment.get("estimated_cost_bps"),
            "estimated_net_edge_bps": ai_assessment.get("estimated_net_edge_bps"),
            "effective_taker_fee_bps": effective_taker_fee_bps,
            "effective_maker_fee_bps": effective_maker_fee_bps,
            "estimated_execution_fee_bps": estimated_execution_fee_bps,
            "funding_fee_bps": funding_fee_bps,
            "funding_fee_summary": funding_fee_summary,
            "min_required_net_edge_bps": self.runtime.settings.strategy_min_net_edge_bps,
            "noise_buffer_bps": self.runtime.settings.strategy_edge_noise_buffer_bps,
            "required_total_edge_bps": required_total_edge_bps,
            "target_expected_signal_edge_bps": position_target.get("expected_signal_edge_bps") if position_target else None,
            "target_expected_cost_bps": position_target.get("expected_cost_bps") if position_target else None,
            "target_expected_net_edge_bps": position_target.get("expected_net_edge_bps") if position_target else None,
            "validation_flags": list(ai_assessment.get("validation_flags") or []),
            "rejection_flags": list(ai_assessment.get("rejection_flags") or []),
            "market_snapshot_fresh": None if ai_decision_brief is None else ai_decision_brief.get("market_snapshot_fresh"),
            "account_snapshot_fresh": None if ai_decision_brief is None else ai_decision_brief.get("account_snapshot_fresh"),
            "safe_to_trade": None if ai_decision_brief is None else ai_decision_brief.get("safe_to_trade"),
            "execution_condition": ai_assessment.get("execution_condition"),
            "current_open_order_count": None if ai_decision_brief is None else ai_decision_brief.get("current_open_order_count"),
            "recent_fee_drag_ratio": None if strategy_execution_health is None else strategy_execution_health.get("recent_fee_drag_ratio"),
            "recent_churn_ratio": None if strategy_execution_health is None else strategy_execution_health.get("recent_churn_ratio"),
            "recent_low_edge_trade_streak": None if strategy_execution_health is None else strategy_execution_health.get("recent_low_edge_trade_streak"),
            "guardrail_flags": [] if position_target is None else list(position_target.get("guardrail_flags") or []),
        }

    @staticmethod
    def _direction_from_edge(edge: Any) -> str:
        try:
            numeric = float(edge or 0.0)
        except (TypeError, ValueError):
            return "flat"
        if numeric > 0:
            return "long"
        if numeric < 0:
            return "short"
        return "flat"

    @staticmethod
    def _action_from_position_intent(position_intent: Any) -> str | None:
        if position_intent is None:
            return None
        return execution_action_from_position_intent(str(position_intent)) or "hold"

    def _action_from_execution_fields(self, *, execution_action: Any, position_intent: Any) -> str | None:
        if execution_action is not None:
            normalized = str(execution_action).strip().lower()
            if normalized:
                return normalized
        return self._action_from_position_intent(position_intent)

    def _execution_record_payload(self, record: Any) -> dict[str, Any]:
        if isinstance(record, dict):
            payload = dict(record)
            raw_payload = dict(payload.get("raw_payload") or {})
            if "state" in payload and "status" not in payload:
                payload["status"] = payload.get("state")
            if "requested_qty" in payload and "quantity" not in payload:
                payload["quantity"] = payload.get("requested_qty")
            if "exchange_ts" in payload and "exchange_timestamp" not in payload:
                payload["exchange_timestamp"] = payload.get("exchange_ts")
            if "ingestion_ts" in payload and "ingestion_timestamp" not in payload:
                payload["ingestion_timestamp"] = payload.get("ingestion_ts")
            payload["truth_source"] = payload.get("truth_source") or (
                "execution_fill_repo_v2" if payload.get("fill_id") else "execution_order_repo"
            )
            payload["product_type"] = raw_payload.get("product_type", payload.get("product_type"))
            payload["margin_mode"] = raw_payload.get("margin_mode", payload.get("margin_mode"))
            payload["execution_action"] = self._action_from_execution_fields(
                execution_action=raw_payload.get("execution_action", payload.get("execution_action")),
                position_intent=raw_payload.get("position_intent", payload.get("position_intent")),
            )
            fill_id = payload.get("fill_id")
            if fill_id:
                outcome = self._fill_outcome_map().get(fill_id)
                payload["has_fill_outcome"] = outcome is not None
                if outcome is not None:
                    outcome_payload = outcome.model_dump(mode="json")
                    payload["fill_notional"] = outcome_payload.get("fill_notional")
                    payload["realized_pnl"] = outcome_payload.get("realized_pnl_delta")
                    payload["realized_pnl_delta"] = outcome_payload.get("realized_pnl_delta")
                    payload["gross_realized_pnl"] = outcome.realized_pnl_delta + outcome.fee_delta
                    payload["fee_delta"] = outcome_payload.get("fee_delta")
                    payload["starting_position_qty"] = outcome_payload.get("starting_position_qty")
                    payload["starting_avg_entry_price"] = outcome_payload.get("starting_avg_entry_price")
                    payload["ending_position_qty"] = outcome_payload.get("ending_position_qty")
                    payload["ending_avg_entry_price"] = outcome_payload.get("ending_avg_entry_price")
                    payload["balances_before"] = outcome_payload.get("balances_before") or {}
                    payload["balances_after"] = outcome_payload.get("balances_after") or {}
                    payload["balance_deltas"] = outcome_payload.get("balance_deltas") or {}
                    payload["fill_outcome_recorded_at"] = outcome_payload.get("created_at")
            return payload
        payload = record.model_dump(mode="json")
        payload["execution_action"] = self._action_from_execution_fields(
            execution_action=payload.get("execution_action"),
            position_intent=payload.get("position_intent"),
        )
        fill_id = payload.get("fill_id")
        if fill_id:
            outcome = self._fill_outcome_map().get(fill_id)
            payload["has_fill_outcome"] = outcome is not None
            if outcome is not None:
                outcome_payload = outcome.model_dump(mode="json")
                payload["fill_notional"] = outcome_payload.get("fill_notional")
                payload["realized_pnl"] = outcome_payload.get("realized_pnl_delta")
                payload["realized_pnl_delta"] = outcome_payload.get("realized_pnl_delta")
                payload["gross_realized_pnl"] = (
                    outcome.realized_pnl_delta + outcome.fee_delta
                )
                payload["fee_delta"] = outcome_payload.get("fee_delta")
                payload["starting_position_qty"] = outcome_payload.get("starting_position_qty")
                payload["starting_avg_entry_price"] = outcome_payload.get("starting_avg_entry_price")
                payload["ending_position_qty"] = outcome_payload.get("ending_position_qty")
                payload["ending_avg_entry_price"] = outcome_payload.get("ending_avg_entry_price")
                payload["balances_before"] = outcome_payload.get("balances_before") or {}
                payload["balances_after"] = outcome_payload.get("balances_after") or {}
                payload["balance_deltas"] = outcome_payload.get("balance_deltas") or {}
                payload["fill_outcome_recorded_at"] = outcome_payload.get("created_at")
        return payload

    def _execution_plan_payload(self, execution_plan: dict[str, Any] | None) -> dict[str, Any] | None:
        if execution_plan is None:
            return None
        payload = dict(execution_plan)
        payload["execution_action"] = self._action_from_execution_fields(
            execution_action=payload.get("execution_action"),
            position_intent=payload.get("position_intent"),
        )
        return payload

    def _baseline_reference_payload(
        self,
        *,
        baseline_assessment: dict[str, Any] | None,
        decision_context: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if baseline_assessment is None:
            return None
        reference = BaselineReference(
            decision_id=str(baseline_assessment.get("decision_id") or ""),
            symbol=str(baseline_assessment.get("symbol") or ""),
            timeframe=(
                None
                if decision_context is None
                else decision_context.get("timeframe")
            ) or baseline_assessment.get("holding_horizon"),
            regime=baseline_assessment.get("regime"),
            volatility_state=baseline_assessment.get("volatility_state"),
            direction_bias=baseline_assessment.get("direction_bias") or "flat",
            confidence=baseline_assessment.get("confidence"),
            composite_alpha_score=baseline_assessment.get("composite_alpha_score"),
            suggested_position_scale=baseline_assessment.get("suggested_position_scale"),
            reason_codes=list(baseline_assessment.get("reason_codes") or []),
            raw_payload=baseline_assessment,
        )
        return reference.model_dump(mode="json")

    def _ai_decision_intent_payload(
        self,
        *,
        ai_assessment: dict[str, Any] | None,
        decision_context: dict[str, Any] | None,
        position_target: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        native_intent = None if position_target is None else position_target.get("ai_decision_intent")
        if isinstance(native_intent, dict):
            return native_intent
        if ai_assessment is None:
            return None
        direction = self._direction_from_edge(ai_assessment.get("directional_edge"))
        current_side = None if decision_context is None else decision_context.get("current_exposure_side")
        current_qty = 0.0 if decision_context is None else float(decision_context.get("current_position_qty") or 0.0)
        economically_actionable = bool(ai_assessment.get("economically_actionable"))
        if direction == "flat" or not economically_actionable:
            action = "hold"
        elif current_side not in {None, "flat"} and current_side != direction:
            action = "reverse"
        elif abs(current_qty) > 1e-12 and current_side == direction:
            action = "scale_in"
        else:
            action = "enter"
        intent = AIDecisionIntent(
            decision_id=str(ai_assessment.get("decision_id") or ""),
            symbol=str(ai_assessment.get("symbol") or ""),
            timeframe=(
                None
                if decision_context is None
                else decision_context.get("timeframe")
            ) or ai_assessment.get("expected_holding_horizon"),
            direction=direction,
            action=action,
            target_qty=Decimal(str((position_target or {}).get("target_position_qty") or "0")),
            confidence=float(ai_assessment.get("calibrated_confidence") or ai_assessment.get("confidence") or 0.0),
            economically_actionable=economically_actionable,
            reason_codes=list(ai_assessment.get("override_reason_codes") or ai_assessment.get("validation_flags") or []),
            fallback_used=bool(ai_assessment.get("fallback_used")),
            degraded=bool(ai_assessment.get("degraded")),
            provider_name=ai_assessment.get("provider_name"),
            provider_request_id=ai_assessment.get("provider_request_id"),
            requested_profile_id=ai_assessment.get("requested_profile_id"),
            requested_profile_reason_codes=list(ai_assessment.get("requested_profile_reason_codes") or []),
            raw_assessment_ref=ai_assessment,
        )
        return intent.model_dump(mode="json")

    def _decision_outcome_payload(
        self,
        *,
        finalized_decision_outcome: dict[str, Any] | None,
        decision_context: dict[str, Any] | None,
        baseline_assessment: dict[str, Any] | None,
        ai_assessment: dict[str, Any] | None,
        position_target: dict[str, Any] | None,
        policy_decision: dict[str, Any] | None,
        risk_decision: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if all(item is None for item in (baseline_assessment, ai_assessment, position_target, policy_decision, risk_decision, finalized_decision_outcome)):
            return None
        if isinstance(finalized_decision_outcome, dict):
            return finalized_decision_outcome
        native_outcome = None if position_target is None else position_target.get("decision_outcome")
        native_profile_control = None if position_target is None else position_target.get("profile_control_decision")
        if isinstance(native_outcome, dict):
            payload = dict(native_outcome)
            blocked_reasons = list(payload.get("decision_blocked_reasons") or [])
            blocked_reasons.extend(list((policy_decision or {}).get("rejection_reasons") or []))
            blocked_reasons.extend(list((risk_decision or {}).get("rejection_reasons") or []))
            payload["decision_blocked_reasons"] = list(dict.fromkeys(item for item in blocked_reasons if item))
            payload["policy_blocked"] = bool(policy_decision is not None and not policy_decision.get("execution_allowed", False))
            payload["policy_blocked_reasons"] = list((policy_decision or {}).get("rejection_reasons") or [])
            payload["risk_capped"] = bool(risk_decision is not None and (
                risk_decision.get("modified")
                or risk_decision.get("rejection_reasons")
                or risk_decision.get("constraints_applied")
            ))
            payload["risk_capped_reasons"] = list((risk_decision or {}).get("rejection_reasons") or []) + list(
                (risk_decision or {}).get("constraints_applied") or []
            )
            payload["risk_capped_target_qty"] = (
                None
                if risk_decision is None or risk_decision.get("capped_target_position_qty") is None
                else risk_decision.get("capped_target_position_qty")
            )
            profile_snapshot = self.strategy_profile_snapshot()
            activation = profile_snapshot.get("activation", {})
            payload["active_profile_id"] = (
                payload.get("active_profile_id")
                or activation.get("active_profile_id")
            )
            payload["finalized"] = bool(payload.get("finalized", False))
            if payload.get("profile_control_source") is None:
                if isinstance(native_profile_control, dict):
                    payload["profile_control_source"] = (
                        "ai"
                        if native_profile_control.get("applied")
                        else "admin" if native_profile_control.get("frozen_by_admin_override") else "system"
                    )
                else:
                    payload["profile_control_source"] = "system" if activation.get("active_profile_id") else "env_default"
            if payload.get("position_management_reason_codes") is None:
                payload["position_management_reason_codes"] = [
                    code
                    for code in ("alpha_decay_exit", "alpha_decay_reduce", "risk_contraction_exit", "emergency_protective_exit")
                    if code in list(payload.get("guardrail_flags") or [])
                ]
            if payload.get("exit_attribution") is None:
                for code in ("emergency_protective_exit", "alpha_decay_exit", "alpha_decay_reduce", "risk_contraction_exit"):
                    if code in list(payload.get("position_management_reason_codes") or []):
                        payload["exit_attribution"] = code
                        break
            return payload
        mode_value = (
            None if ai_assessment is None else ai_assessment.get("operating_mode")
        ) or self.runtime.settings.ai_operating_mode
        canonical_mode = normalize_ai_operating_mode(mode_value)
        decision_authority_map = {
            "baseline_only": "reference_only",
            "ai_assisted": "advisory",
            "ai_decision_maker": "final_decision",
            "ai_decision_maker_with_profile_control": "final_decision_with_profile_control",
        }
        ai_direction = self._direction_from_edge(None if ai_assessment is None else ai_assessment.get("directional_edge"))
        if ai_assessment is not None and ai_assessment.get("fallback_used") and canonical_mode in {
            "ai_decision_maker",
            "ai_decision_maker_with_profile_control",
        }:
            decision_source = "baseline_fallback"
        elif canonical_mode in {"ai_decision_maker", "ai_decision_maker_with_profile_control"} and ai_assessment is not None and (
            bool(ai_assessment.get("economically_actionable")) or ai_direction != "flat"
        ):
            decision_source = "ai"
        else:
            decision_source = "baseline"
        final_target_qty = None
        if risk_decision is not None and risk_decision.get("capped_target_position_qty") is not None:
            final_target_qty = risk_decision.get("capped_target_position_qty")
        elif position_target is not None:
            final_target_qty = position_target.get("target_position_qty")
        policy_blocked = bool(policy_decision is not None and not policy_decision.get("execution_allowed", False))
        risk_capped = bool(risk_decision is not None and (
            risk_decision.get("modified")
            or risk_decision.get("rejection_reasons")
            or risk_decision.get("constraints_applied")
        ))
        blocked_reasons: list[str] = []
        blocked_reasons.extend(list((position_target or {}).get("guardrail_flags") or []))
        blocked_reasons.extend(list((policy_decision or {}).get("rejection_reasons") or []))
        blocked_reasons.extend(list((risk_decision or {}).get("rejection_reasons") or []))
        position_management_reason_codes = [
            code
            for code in ("alpha_decay_exit", "alpha_decay_reduce", "risk_contraction_exit", "emergency_protective_exit")
            if code in list((position_target or {}).get("guardrail_flags") or [])
        ]
        exit_attribution = None
        for code in ("emergency_protective_exit", "alpha_decay_exit", "alpha_decay_reduce", "risk_contraction_exit"):
            if code in position_management_reason_codes:
                exit_attribution = code
                break
        profile_snapshot = self.strategy_profile_snapshot()
        activation = profile_snapshot.get("activation", {})
        outcome = DecisionOutcome(
            decision_id=str(
                (position_target or {}).get("decision_id")
                or (ai_assessment or {}).get("decision_id")
                or (baseline_assessment or {}).get("decision_id")
                or ""
            ),
            symbol=str(
                (position_target or {}).get("symbol")
                or (ai_assessment or {}).get("symbol")
                or (baseline_assessment or {}).get("symbol")
                or ""
            ),
            ai_operating_mode=canonical_mode,
            finalized=False,
            decision_source=decision_source,
            decision_authority=decision_authority_map[canonical_mode],
            final_direction=(
                None if position_target is None else position_target.get("target_exposure_side")
            ) or ai_direction or (
                None if baseline_assessment is None else baseline_assessment.get("direction_bias")
            ),
            final_action=self._action_from_position_intent(None if position_target is None else position_target.get("position_intent")),
            final_target_qty=None if final_target_qty is None else Decimal(str(final_target_qty)),
            baseline_reference=self._baseline_reference_payload(
                baseline_assessment=baseline_assessment,
                decision_context=decision_context,
            ),
            baseline_disagreement=None if ai_assessment is None or baseline_assessment is None else {
                "disagreed": ai_direction != baseline_assessment.get("direction_bias"),
                "baseline_direction": baseline_assessment.get("direction_bias"),
                "ai_direction": ai_direction,
            },
            decision_blocked_reasons=list(dict.fromkeys(item for item in blocked_reasons if item)),
            guardrail_flags=list((position_target or {}).get("guardrail_flags") or []),
            policy_blocked=policy_blocked,
            policy_blocked_reasons=list((policy_decision or {}).get("rejection_reasons") or []),
            risk_capped=risk_capped,
            risk_capped_reasons=list((risk_decision or {}).get("rejection_reasons") or [])
            + list((risk_decision or {}).get("constraints_applied") or []),
            risk_capped_target_qty=None if risk_decision is None or risk_decision.get("capped_target_position_qty") is None else Decimal(str(risk_decision.get("capped_target_position_qty"))),
            position_management_reason_codes=position_management_reason_codes,
            exit_attribution=exit_attribution,
            selected_strategy_family=str((position_target or {}).get("strategy_family") or "directional"),
            selected_strategy_route_action=str((position_target or {}).get("strategy_route_action") or "override_target"),
            strategy_selection_reason_codes=list((position_target or {}).get("strategy_reason_codes") or []),
            strategy_selection_headline=(position_target or {}).get("strategy_headline"),
            active_profile_id=activation.get("active_profile_id"),
            profile_control_source="system" if activation.get("active_profile_id") else "env_default",
            ai_fallback_used=bool((ai_assessment or {}).get("fallback_used")),
            ai_degraded=bool((ai_assessment or {}).get("degraded")),
        )
        return outcome.model_dump(mode="json")

    @staticmethod
    def _profile_control_decision_payload(*, position_target: dict[str, Any] | None) -> dict[str, Any] | None:
        payload = None if position_target is None else position_target.get("profile_control_decision")
        return payload if isinstance(payload, dict) else None

    def _ai_decision_audit(
        self,
        *,
        audit,
        decision_context: dict[str, Any] | None,
        ai_decision_brief: dict[str, Any] | None,
        baseline_assessment: dict[str, Any] | None,
        ai_assessment: dict[str, Any] | None,
        position_target: dict[str, Any] | None,
        finalized_decision_outcome: dict[str, Any] | None,
        strategy_execution_health: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if ai_assessment is None and position_target is None:
            return None
        native_outcome = finalized_decision_outcome
        if not isinstance(native_outcome, dict):
            native_outcome = None if position_target is None else position_target.get("decision_outcome")
        return {
            "configured_mode": self.runtime.settings.ai_operating_mode,
            "assessment_operating_mode": None if ai_assessment is None else ai_assessment.get("operating_mode"),
            "provider_name": None if ai_assessment is None else ai_assessment.get("provider_name"),
            "provider_request_id": None if ai_assessment is None else ai_assessment.get("provider_request_id"),
            "fallback_used": None if ai_assessment is None else ai_assessment.get("fallback_used"),
            "degraded": None if ai_assessment is None else ai_assessment.get("degraded"),
            "baseline_direction": None if baseline_assessment is None else baseline_assessment.get("direction_bias"),
            "ai_direction": self._direction_from_edge(None if ai_assessment is None else ai_assessment.get("directional_edge")),
            "final_direction": None if position_target is None else position_target.get("target_exposure_side"),
            "decision_source": None if not isinstance(native_outcome, dict) else native_outcome.get("decision_source"),
            "decision_authority": None if not isinstance(native_outcome, dict) else native_outcome.get("decision_authority"),
            "profile_control_source": None if not isinstance(native_outcome, dict) else native_outcome.get("profile_control_source"),
            "market_snapshot_fresh": None if ai_decision_brief is None else ai_decision_brief.get("market_snapshot_fresh"),
            "account_snapshot_fresh": None if ai_decision_brief is None else ai_decision_brief.get("account_snapshot_fresh"),
            "safe_to_trade": None if ai_decision_brief is None else ai_decision_brief.get("safe_to_trade"),
            "execution_condition": None if ai_assessment is None else ai_assessment.get("execution_condition"),
            "current_open_order_count": None if ai_decision_brief is None else ai_decision_brief.get("current_open_order_count"),
            "guardrail_flags": [] if position_target is None else list(position_target.get("guardrail_flags") or []),
            "strategy_guardrail_flags": [] if decision_context is None else list(decision_context.get("strategy_guardrail_flags") or []),
            "recent_fee_drag_ratio": None if strategy_execution_health is None else strategy_execution_health.get("recent_fee_drag_ratio"),
            "recent_churn_ratio": None if strategy_execution_health is None else strategy_execution_health.get("recent_churn_ratio"),
            "recent_low_edge_trade_streak": None if strategy_execution_health is None else strategy_execution_health.get("recent_low_edge_trade_streak"),
            "audit_ref_counts": {
                "order_intents": len(audit.order_intent_refs),
                "order_updates": len(audit.order_state_refs),
                "fills": len(audit.fill_event_refs),
                "reconciliations": len(audit.reconciliation_refs),
                "shadow_decisions": len(audit.ai_shadow_decision_refs),
                "shadow_evaluations": len(audit.ai_shadow_evaluation_refs),
            },
        }

    def _ai_execution_suggestion_summary(
        self,
        *,
        ai_assessment: dict[str, Any] | None,
        execution_plan: dict[str, Any] | None,
        latest_order_intent: dict[str, Any] | None,
    ) -> dict[str, Any]:
        assessment_suggestion = None if ai_assessment is None else ai_assessment.get("ai_execution_parameter_suggestion")
        plan_translation = None if execution_plan is None else execution_plan.get("ai_execution_parameter_suggestion")
        intent_translation = (
            None if latest_order_intent is None else latest_order_intent.get("ai_execution_parameter_suggestion")
        )
        latest_translation = intent_translation or plan_translation or assessment_suggestion
        return {
            "configured_mode": self.runtime.settings.ai_execution_suggestion_mode,
            "assessment_suggestion": assessment_suggestion,
            "execution_plan_translation": plan_translation,
            "latest_order_intent_translation": intent_translation,
            "latest_translation": latest_translation,
            "live_order_type": None if latest_order_intent is None else latest_order_intent.get("order_type"),
            "live_time_in_force": None if latest_order_intent is None else latest_order_intent.get("time_in_force"),
            "live_limit_price": None if latest_order_intent is None else latest_order_intent.get("limit_price"),
            "live_execution_style": None if latest_order_intent is None else latest_order_intent.get("execution_style"),
            "suggestion_present": assessment_suggestion is not None,
            "translation_present": plan_translation is not None or intent_translation is not None,
            "status": "absent" if latest_translation is None else latest_translation.get("status"),
        }

    def _build_system_health(self) -> dict[str, Any]:
        return self.runtime_queries.build_system_health()

    def _build_system_runtime(self) -> dict[str, Any]:
        return self.runtime_queries.build_system_runtime()

    def decision_view(self, decision_id: str) -> dict[str, Any]:
        audit = self.runtime.audit_repo.get(decision_id)
        if audit is None:
            raise KeyError(f"decision_not_found:{decision_id}")
        decision_context = self.payload_by_ref(audit.decision_context_ref)
        health_snapshot = None
        if decision_context is not None:
            health_snapshot = self.payload_by_ref(decision_context.get("health_snapshot_ref"))
        order_intents = self.payloads_by_refs(audit.order_intent_refs)
        order_updates = self.payloads_by_refs(audit.order_state_refs)
        fills = self.payloads_by_refs(audit.fill_event_refs)
        reconciliations = self.payloads_by_refs(audit.reconciliation_refs)
        ai_visible = self._ai_history_visible()
        ai_decision_brief = self.payload_by_ref(audit.ai_decision_brief_ref) if ai_visible else None
        ai_assessment = self.payload_by_ref(audit.ai_market_assessment_ref) if ai_visible else None
        baseline_assessment = self.payload_by_ref(audit.baseline_assessment_ref)
        position_target = self.payload_by_ref(audit.position_target_ref)
        policy_decision = self.payload_by_ref(audit.policy_decision_ref)
        risk_decision = self._risk_decision_payload(self.payload_by_ref(audit.risk_decision_ref))
        finalized_decision_outcome = self.payload_by_ref(audit.decision_outcome_ref)
        execution_plan = self._execution_plan_payload(self.payload_by_ref(audit.execution_plan_ref))
        strategy_execution_health = self.strategy_execution_health(
            decision_context.get("symbol") if decision_context is not None else None
        )
        return {
            "decision_id": decision_id,
            "health_snapshot": health_snapshot,
            "decision_context": decision_context,
            "baseline_assessment": baseline_assessment,
            "baseline_reference": self._baseline_reference_payload(
                baseline_assessment=baseline_assessment,
                decision_context=decision_context,
            ),
            "ai_decision_brief": ai_decision_brief,
            "ai_assessment": ai_assessment,
            "ai_decision_intent": self._ai_decision_intent_payload(
                ai_assessment=ai_assessment,
                decision_context=decision_context,
                position_target=position_target,
            ),
            "profile_control_decision": self._profile_control_decision_payload(position_target=position_target),
            "ai_shadow_decisions": self.payloads_by_refs(audit.ai_shadow_decision_refs) if ai_visible else [],
            "ai_shadow_evaluations": self.payloads_by_refs(audit.ai_shadow_evaluation_refs) if ai_visible else [],
            "position_target": position_target,
            "policy_decision": policy_decision,
            "risk_decision": risk_decision,
            "decision_outcome": self._decision_outcome_payload(
                finalized_decision_outcome=finalized_decision_outcome,
                decision_context=decision_context,
                baseline_assessment=baseline_assessment,
                ai_assessment=ai_assessment,
                position_target=position_target,
                policy_decision=policy_decision,
                risk_decision=risk_decision,
            ),
            "execution_plan": execution_plan,
            "audit": audit.model_dump(mode="json"),
            "latest_order_intent": order_intents[-1] if order_intents else None,
            "latest_order_update": order_updates[-1] if order_updates else None,
            "latest_fill_event": fills[-1] if fills else None,
            "latest_reconciliation": reconciliations[-1] if reconciliations else None,
            "order_intents": order_intents,
            "order_updates": order_updates,
            "fills": fills,
            "portfolio_snapshot": self.payload_by_ref(audit.portfolio_delta_ref),
            "reconciliations": reconciliations,
            "strategy_execution_health": strategy_execution_health,
            "ai_decision_audit": self._ai_decision_audit(
                audit=audit,
                decision_context=decision_context,
                ai_decision_brief=ai_decision_brief,
                baseline_assessment=baseline_assessment,
                ai_assessment=ai_assessment,
                position_target=position_target,
                finalized_decision_outcome=finalized_decision_outcome,
                strategy_execution_health=strategy_execution_health,
            ),
            "ai_economic_actionability": self._ai_economic_actionability(
                ai_assessment=ai_assessment,
                position_target=position_target,
                ai_decision_brief=ai_decision_brief,
                strategy_execution_health=strategy_execution_health,
            ),
            "ai_execution_suggestion": self._ai_execution_suggestion_summary(
                ai_assessment=ai_assessment,
                execution_plan=execution_plan,
                latest_order_intent=order_intents[-1] if order_intents else None,
            ),
        }

    def recent_decisions(self, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        normalized_limit = max(int(limit), 1)
        normalized_offset = max(int(offset), 0)
        cache_key = (
            f"recent_decisions:{self._scope_cache_fragment()}:"
            f"{normalized_limit}:{normalized_offset}"
        )
        return self._cached_ttl(
            cache_key,
            10,
            lambda: self._build_recent_decisions(limit=normalized_limit, offset=normalized_offset),
        )

    def _build_recent_decisions(self, *, limit: int, offset: int) -> dict[str, Any]:
        rows = self.runtime.audit_repo.recent(limit=limit + offset)
        paged_rows = rows[offset : offset + limit]
        payloads: list[dict[str, Any]] = []
        for record in paged_rows:
            context = self.payload_by_ref(record.decision_context_ref)
            target = self.payload_by_ref(record.position_target_ref)
            policy = self.payload_by_ref(record.policy_decision_ref)
            risk = self.payload_by_ref(record.risk_decision_ref)
            payloads.append(
                {
                    "decision_id": record.decision_id,
                    "symbol": context.get("symbol") if context else None,
                    "timeframe": context.get("timeframe") if context else None,
                    "decision_time": context.get("as_of_ts") if context else None,
                    "product_type": (
                        target.get("product_type")
                        if target
                        else (context.get("product_type") if context else None)
                    ),
                    "margin_mode": (
                        target.get("margin_mode")
                        if target
                        else (context.get("margin_mode") if context else None)
                    ),
                    "position_intent": target.get("position_intent") if target else None,
                    "current_position_qty": target.get("current_position_qty") if target else None,
                    "target_position_qty": target.get("target_position_qty") if target else None,
                    "delta_position_qty": target.get("delta_position_qty") if target else None,
                    "target_delta_qty": target.get("delta_position_qty") if target else None,
                    "strategy_family": (
                        target.get("strategy_family")
                        if target
                        else None
                    ),
                    "strategy_route_action": (
                        target.get("strategy_route_action")
                        if target
                        else None
                    ),
                    "strategy_reason_codes": [] if target is None else list(target.get("strategy_reason_codes") or []),
                    "guardrail_flags": target.get("guardrail_flags") if target else [],
                    "expected_net_edge_bps": target.get("expected_net_edge_bps") if target else None,
                    "policy_result": policy.get("execution_allowed") if policy else None,
                    "risk_result": risk.get("approved") if risk else None,
                    "execution_result": {
                        "order_count": len(record.order_intent_refs),
                        "fill_count": len(record.fill_event_refs),
                        "reconciled": bool(record.reconciliation_refs),
                    },
                }
            )
        total_available = self.runtime.audit_repo.count()
        return {
            "decisions": payloads,
            "limit": limit,
            "offset": offset,
            "total_available": total_available,
            "has_more": offset + len(payloads) < total_available,
        }

    def latest_decision(self) -> dict[str, Any]:
        decision_id = self.latest_decision_id()
        if decision_id is None:
            return {
                "decision_id": None,
                "decision_context": None,
                "baseline_assessment": None,
                "ai_assessment": None,
                "position_target": None,
                "policy_decision": None,
                "risk_decision": None,
                "execution_plan": None,
                "audit": None,
                "order_intents": [],
                "order_updates": [],
                "fills": [],
                "portfolio_snapshot": None,
                "reconciliations": [],
                "summary": None,
                "strategy_execution_health": self.strategy_execution_health(),
            }
        detail = self.decision_view(decision_id)
        detail["summary"] = next((item for item in self.recent_decisions(limit=1)["decisions"] if item["decision_id"] == decision_id), None)
        detail["strategy_execution_health"] = self.strategy_execution_health(
            detail["decision_context"]["symbol"] if detail["decision_context"] else None
        )
        return detail

    def latest_risk(self) -> dict[str, Any]:
        payload = self._latest_topic_summary(topics.RISK_DECISIONS)
        return {
            **payload,
            "payload": self._risk_decision_payload(payload.get("payload")),
        }

    def recent_risks(self, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        rows = list(reversed(self.runtime.event_store.by_topic(topics.RISK_DECISIONS)))
        return self._paginate_rows(
            rows,
            limit=limit,
            offset=offset,
            key="risks",
            serializer=lambda item: self._risk_decision_payload(item.payload),
        )

    @staticmethod
    def _risk_reason_message(code: str) -> str:
        messages = {
            "max_abs_qty": "目标仓位超过单标的最大绝对仓位上限，系统已先按仓位上限裁剪。",
            "max_notional_per_symbol": "目标名义金额超过通用单标的上限，系统已先按名义金额上限裁剪。",
            "only_reduce_required": "当前风控只允许继续减仓或平仓，不再允许新增暴露。",
            "only_reduce_mode_active": "当前没有可执行的减仓空间，因此本轮不会继续发出新增暴露订单。",
            "max_target_leverage": "目标杠杆超过当前运行配置允许的上限，系统已先按最大杠杆约束处理。",
            "max_open_orders_reached": "当前活动委托数已经达到上限，不能继续新增暴露。",
            "max_target_leverage_exceeded": "请求杠杆超过当前合约运行配置允许的最大杠杆。",
            "insufficient_initial_margin": "可用保证金不足，当前不能继续新增合约暴露。",
            "liquidation_buffer_breached": "预估保证金占用已经侵蚀到强平缓冲区，系统禁止继续新增暴露。",
            "derivatives_margin_usage_requires_only_reduce": "预估保证金占用已经进入高风险区域，系统只允许减仓或平仓。",
            "derivatives_liquidation_gap_requires_only_reduce": "当前最近仓位已经进入强平缓冲区，系统只允许继续减仓或平仓。",
            "derivatives_risk_snapshot_missing_requires_only_reduce": "当前拿不到合约风险快照，系统只允许继续减仓或平仓。",
            "derivatives_risk_snapshot_missing_grace_active": "当前合约风险快照短时缺失，系统先降档、缩预算并降低执行侵略性。",
            "derivatives_risk_snapshot_missing_auto_halt": "合约风险快照持续缺失过久，系统已升级到自动停机。",
            "derivatives_margin_buffer_auto_halt": "当前保证金占用已经进入自动停机阈值，系统必须保持暂停。",
            "derivatives_liquidation_proximity_auto_halt": "当前最近仓位距离强平过近，系统已经触发自动停机。",
            "max_gross_notional_per_symbol_exceeded": "单标的总名义敞口超过上限，系统禁止继续扩大该标的仓位。",
            "max_pending_notional_per_symbol_exceeded": "单标的待成交名义金额超过上限，系统禁止继续追加该标的挂单。",
            "max_total_open_notional_exceeded": "账户总名义敞口超过上限，系统禁止继续新增整体暴露。",
            "max_daily_realized_loss_usdt_exceeded": "当日已实现亏损超过上限，系统只允许继续减仓或平仓。",
            "risk_budget_multiplier_applied": "当前风险预算已经自动收缩，目标仓位和名义金额上限会同步变小。",
            "execution_aggressiveness_contracted": "当前执行侵略性已经自动收缩，实际滑点和执行参数上限会同步变严。",
            "alpha_decay_exit": "原有持仓的优势已经明显衰减，系统将直接退出该仓位。",
            "alpha_decay_reduce": "原有持仓的优势开始衰减，系统先自动减仓。",
            "risk_contraction_exit": "当前波动率或市场状态不利于继续满仓持有，系统自动收缩现有仓位。",
            "emergency_protective_exit": "当前 adverse 信号过强，系统触发紧急保护退出。",
        }
        return messages.get(code, f"风控命中了限制：{code}")

    @classmethod
    def _risk_reason_details(
        cls,
        *,
        codes: list[str],
        category: str,
    ) -> list[dict[str, str]]:
        return [
            {
                "code": code,
                "category": category,
                "message": cls._risk_reason_message(str(code)),
            }
            for code in list(dict.fromkeys(str(code) for code in codes if code))
        ]

    def _risk_decision_payload(self, payload: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return payload
        enriched = dict(payload)
        rejection_reasons = [str(item) for item in (payload.get("rejection_reasons") or []) if item]
        constraints_applied = [str(item) for item in (payload.get("constraints_applied") or []) if item]
        only_reduce_required = bool(payload.get("only_reduce_required"))
        approved = bool(payload.get("approved"))
        risk_budget_multiplier = self._to_decimal(payload.get("risk_budget_multiplier"))
        execution_aggressiveness_multiplier = self._to_decimal(payload.get("execution_aggressiveness_multiplier"))
        enriched["rejection_reason_details"] = self._risk_reason_details(
            codes=rejection_reasons,
            category="rejection",
        )
        enriched["constraint_details"] = self._risk_reason_details(
            codes=constraints_applied,
            category="constraint",
        )
        if approved and only_reduce_required:
            enriched["operator_summary"] = "风控当前只允许减仓或平仓，不允许继续新增暴露。"
        elif approved:
            enriched["operator_summary"] = "风控当前允许继续执行。"
        else:
            enriched["operator_summary"] = "风控当前已阻断本轮目标，请先处理列出的限制。"
        margin_context = self._risk_margin_buffer_context(enriched)
        if margin_context is not None:
            enriched["margin_buffer_context"] = margin_context
            projected_margin_usage_percent = margin_context.get("projected_margin_usage_percent")
            buffer_to_only_reduce_percent = margin_context.get("buffer_to_only_reduce_percent")
            buffer_to_hard_limit_percent = margin_context.get("buffer_to_hard_limit_percent")
            enriched["operator_summary"] = (
                f"{enriched['operator_summary']} 当前投影保证金占用 {projected_margin_usage_percent}，"
                f"距离 only-reduce 还有 {buffer_to_only_reduce_percent}，"
                f"距离硬上限还有 {buffer_to_hard_limit_percent}。"
            )
        if risk_budget_multiplier is not None and risk_budget_multiplier < Decimal("0.999999"):
            enriched["operator_summary"] = (
                f"{enriched['operator_summary']} 当前风险预算乘数 {risk_budget_multiplier.normalize()}，"
                "系统会自动压低仓位和名义金额上限。"
            )
        if (
            execution_aggressiveness_multiplier is not None
            and execution_aggressiveness_multiplier < Decimal("0.999999")
        ):
            enriched["operator_summary"] = (
                f"{enriched['operator_summary']} 当前执行侵略性乘数 {execution_aggressiveness_multiplier.normalize()}，"
                "系统会自动收紧滑点和执行参数边界。"
            )
        return enriched

    def latest_policy(self) -> dict[str, Any]:
        return self._latest_topic_summary(topics.POLICY_DECISIONS)

    def recent_policies(self, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        rows = list(reversed(self.runtime.event_store.by_topic(topics.POLICY_DECISIONS)))
        return self._paginate_rows(rows, limit=limit, offset=offset, key="policies", serializer=lambda item: item.payload)

    def _build_blockers(self) -> list[dict[str, Any]]:
        snapshot = self._build_blocker_control()
        return [
            {
                "blocker": item.blocker,
                "subsystem": item.subsystem,
                "affects_execution": item.affects_execution,
                "affects_account_synchronization": item.subsystem == "account_state",
                "submit_only": item.submit_only,
                "recommended_action": item.recommended_next_step,
                "title": item.title,
                "description": item.description,
                "impact": item.impact,
                "priority": item.priority,
                "root_cause": item.root_cause,
                "derived_from": item.derived_from,
                "resolution_mode": item.resolution_mode,
                "actions": [action.model_dump(mode="json") for action in item.actions],
            }
            for item in snapshot.blockers
        ]

    def _build_blocker_control(self) -> BlockerControlSnapshot:
        return self.blocker_control_service.snapshot()

    def _build_metrics(self) -> dict[str, Any]:
        snapshot = self._latest_scoped_snapshot()
        metrics = self.runtime.metrics.snapshot()
        fills = self._scoped_fills()
        phase1_shadow = self.phase1_shadow()
        order_intent_events = list(
            self.runtime.event_store.by_topic_scoped(
                topics.ORDER_INTENTS,
                scope=self.state_scope,
            )
        )
        decision_context_events = list(
            self.runtime.event_store.by_topic_scoped(
                topics.DECISION_CONTEXTS,
                scope=self.state_scope,
            )
        )
        snapshot_events = [
            event
            for event in self.runtime.event_store.by_topic_scoped(
                topics.PORTFOLIO_SNAPSHOTS,
                scope=self.state_scope,
            )
        ]
        snapshot_fill_ids = {
            event.payload.get("source_fill_id")
            for event in snapshot_events
            if isinstance(event.payload, dict) and event.payload.get("source_fill_id")
        }
        reconciliation_refs = {
            report.portfolio_snapshot_ref
            for report in self.runtime.reconciliation_repo.history_for_scope(scope=self.state_scope)
        }
        return {
            "decision_cycle_count": len(decision_context_events),
            "order_intent_count": len(order_intent_events),
            "fill_count": len(fills),
            "rejection_count": len(
                order_states_for_scope(
                    self.runtime.execution_repo,
                    self.state_scope,
                    statuses=("FAILED", "REJECTED", "BLOCKED"),
                    limit=200,
                )
            ),
            "reconciliation_mismatch_count": metrics.get("reconciliation_mismatches", 0),
            "processing_failure_count": metrics.get("processing_failures", 0),
            "portfolio_snapshot_repair_count": metrics.get("portfolio_snapshot_repairs", 0),
            "current_open_order_count": len(self._scoped_open_order_states()),
            "portfolio_snapshot_count": len(snapshot_events),
            "fill_without_snapshot_count": sum(1 for fill in fills if fill.fill_id not in snapshot_fill_ids),
            "snapshot_without_reconciliation_count": sum(
                1 for event in snapshot_events if event.event_id not in reconciliation_refs
            ),
            "phase1_shadow": phase1_shadow,
            "phase1_shadow_order_backlog": phase1_shadow["lag"]["order_backlog"],
            "phase1_shadow_fill_backlog": phase1_shadow["lag"]["fill_backlog"],
            "phase1_shadow_obligation_backlog": phase1_shadow["lag"]["obligation_backlog"],
            "phase1_shadow_failure_count": (
                int(phase1_shadow["execution_shadow"].get("order_failure_count", 0) or 0)
                + int(phase1_shadow["execution_shadow"].get("fill_failure_count", 0) or 0)
                + int(phase1_shadow["ledger_shadow"].get("sync_failure_count", 0) or 0)
            ),
            "phase1_shadow_alert_count": metrics.get("phase1_shadow_alerts", 0),
            "phase1_shadow_recovery_count": metrics.get("phase1_shadow_recoveries", 0),
            "recent_execution_errors": self.execution_errors()["errors"][:10],
            "exposure_summary": None if snapshot is None else {
                "gross_exposure": snapshot.gross_exposure,
                "net_exposure": snapshot.net_exposure,
                "total_equity": snapshot.total_equity,
            },
            "strategy_execution_health": self.strategy_execution_health(),
        }

    def _build_phase1_shadow(self) -> dict[str, Any]:
        if getattr(self.runtime, "phase1_shadow_monitor", None) is not None:
            payload = dict(self.runtime.phase1_shadow_monitor.snapshot())
        elif getattr(self.runtime, "phase1_shadow", None) is not None:
            payload = dict(self.runtime.phase1_shadow.snapshot())
        else:
            execution_shadow = (
                self.runtime.phase1_execution_shadow_service.snapshot()
                if self.runtime.phase1_execution_shadow_service is not None
                else {
                    "configured": False,
                    "status": "not_configured",
                    "last_outcome": "idle",
                    "order_attempt_count": 0,
                    "order_success_count": 0,
                    "order_failure_count": 0,
                    "fill_attempt_count": 0,
                    "fill_success_count": 0,
                    "fill_failure_count": 0,
                    "last_order_sync_ts": None,
                    "last_fill_sync_ts": None,
                    "last_failure_ts": None,
                    "last_error": None,
                    "last_synced_order_id": None,
                    "last_synced_order_state": None,
                    "last_synced_fill_id": None,
                }
            )
            ledger_shadow = (
                self.runtime.phase1_ledger_mirror_service.snapshot()
                if self.runtime.phase1_ledger_mirror_service is not None
                else {
                    "configured": False,
                    "status": "not_configured",
                    "last_outcome": "idle",
                    "sync_attempt_count": 0,
                    "sync_success_count": 0,
                    "sync_failure_count": 0,
                    "last_sync_ts": None,
                    "last_failure_ts": None,
                    "last_reason": None,
                    "last_synced_order_id": None,
                    "last_synced_fill_id": None,
                    "last_obligation_status": None,
                    "last_error": None,
                }
            )
            order_backlog = (
                None
                if self.runtime.execution_order_repo is None
                else max(len(self._scoped_order_states()) - self.runtime.execution_order_repo.count_orders(), 0)
            )
            fill_backlog = (
                None
                if self.runtime.execution_fill_repo_v2 is None
                else max(len(self._scoped_fills()) - self.runtime.execution_fill_repo_v2.count_fills(), 0)
            )
            obligation_backlog = (
                None
                if self.runtime.reservation_repo_v2 is None
                else max(len(self.runtime.obligation_repo.all_obligations()) - self.runtime.reservation_repo_v2.count_reservations(), 0)
            )
            lag = {
                "order_backlog": order_backlog,
                "fill_backlog": fill_backlog,
                "obligation_backlog": obligation_backlog,
            }
            backlog_present = any((value or 0) > 0 for value in lag.values() if value is not None)
            if not execution_shadow["configured"] and not ledger_shadow["configured"]:
                status = "not_configured"
            elif execution_shadow["status"] == "degraded" or ledger_shadow["status"] == "degraded":
                status = "degraded"
            elif backlog_present:
                status = "lagging"
            elif execution_shadow["status"] == "healthy" or ledger_shadow["status"] == "healthy":
                status = "healthy"
            else:
                status = "idle"
            payload = {
                "configured": bool(execution_shadow["configured"] or ledger_shadow["configured"]),
                "status": status,
                "connected": bool(execution_shadow["configured"] or ledger_shadow["configured"]),
                "ready": status in {"healthy", "idle", "not_configured"},
                "fresh": status != "degraded",
                "detail": self._phase1_shadow_summary(status=status, lag=lag),
                "summary": self._phase1_shadow_summary(status=status, lag=lag),
                "blockers": ["phase1_shadow_degraded"] if status == "degraded" else ["phase1_shadow_lagging"] if status == "lagging" else [],
                "lag": lag,
                "execution_shadow": execution_shadow,
                "ledger_shadow": ledger_shadow,
            }
        latest_review_action = self.latest_operator_action("phase1_shadow_review")
        latest_alert = self.runtime.event_store.latest(topics.EXECUTION_ERROR_SUMMARIES, key="phase1_shadow")
        latest_failure = self.runtime.event_store.latest(topics.PROCESSING_FAILURES, key="phase1_shadow")
        payload["latest_review_action"] = latest_review_action
        payload["latest_alert"] = self.payload(latest_alert)
        payload["latest_failure"] = self.payload(latest_failure)
        payload["review_recommended"] = payload.get("status") in {"lagging", "degraded"}
        if "detail" not in payload:
            payload["detail"] = payload.get("summary")
        if "blockers" not in payload:
            payload["blockers"] = []
        return payload

    def _build_phase1_shadow_history(self, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []

        for item in reversed(self.runtime.event_store.by_topic(topics.OPERATOR_ACTIONS)):
            if item.payload.get("action") != "phase1_shadow_review":
                continue
            rows.append(
                {
                    "entry_type": "review",
                    "event_id": item.event_id,
                    "observed_at": item.payload.get("details", {}).get("reviewed_at") or item.created_at,
                    "status": item.payload.get("details", {}).get("snapshot_status"),
                    "summary": item.payload.get("details", {}).get("summary") or item.payload.get("reason"),
                    "actor_identity": item.payload.get("actor_identity"),
                    "actor_role": item.payload.get("actor_role"),
                    "reason": item.payload.get("reason"),
                    "details": item.payload.get("details", {}),
                }
            )

        for item in reversed(self.runtime.event_store.by_topic(topics.EXECUTION_ERROR_SUMMARIES)):
            if item.key != "phase1_shadow" and item.payload.get("subsystem") != "phase1_shadow":
                continue
            rows.append(
                {
                    "entry_type": "alert",
                    "event_id": item.event_id,
                    "observed_at": item.payload.get("observed_at") or item.created_at,
                    "status": item.payload.get("severity"),
                    "summary": item.payload.get("message"),
                    "actor_identity": None,
                    "actor_role": None,
                    "reason": None,
                    "details": item.payload,
                }
            )

        for item in reversed(self.runtime.event_store.by_topic(topics.PROCESSING_FAILURES)):
            if item.key != "phase1_shadow" and item.payload.get("subsystem") != "phase1_shadow":
                continue
            rows.append(
                {
                    "entry_type": "failure",
                    "event_id": item.event_id,
                    "observed_at": item.payload.get("observed_at") or item.created_at,
                    "status": item.payload.get("severity"),
                    "summary": item.payload.get("message"),
                    "actor_identity": None,
                    "actor_role": None,
                    "reason": item.payload.get("stage"),
                    "details": item.payload,
                }
            )

        rows.sort(key=self._history_sort_key, reverse=True)
        return self._paginate_rows(rows, limit=limit, offset=offset, key="history")

    @staticmethod
    def _history_sort_key(item: dict[str, Any]) -> str:
        value = item.get("observed_at")
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value or "")

    @staticmethod
    def _phase1_shadow_summary(*, status: str, lag: dict[str, Any]) -> str:
        if status == "not_configured":
            return "Phase 1 shadow compatibility layer is not configured in this runtime."
        if status == "degraded":
            return "Phase 1 shadow compatibility layer has recent write failures and should block automated continuation."
        if status == "lagging":
            return (
                "Phase 1 shadow compatibility layer is behind the legacy runtime. "
                f"order_backlog={lag.get('order_backlog')}, fill_backlog={lag.get('fill_backlog')}, obligation_backlog={lag.get('obligation_backlog')}"
            )
        if status == "healthy":
            return "Phase 1 shadow compatibility layer is tracking legacy execution and obligation flows."
        return "Phase 1 shadow compatibility layer is configured but has not processed shadow traffic yet."

    def portfolio_latest(self) -> dict[str, Any]:
        return self.account_queries.portfolio_latest()

    def _build_portfolio_latest(self) -> dict[str, Any]:
        return self.account_queries.build_portfolio_latest()

    def portfolio_history(self, *, limit: int = 20) -> dict[str, Any]:
        return self.account_queries.portfolio_history(limit=limit)

    def balances(self) -> dict[str, Any]:
        return self.account_queries.balances()

    def positions(self) -> dict[str, Any]:
        return self.account_queries.positions()

    def account_state(self) -> dict[str, Any]:
        return self.account_queries.account_state()

    def _build_account_state(self) -> dict[str, Any]:
        return self.account_queries.build_account_state()

    async def account_recent_bills(self, *, limit: int = 50) -> dict[str, Any]:
        return await self.account_queries.account_recent_bills(limit=limit)

    def account_recent_funding_fees(self, *, limit: int = 50) -> dict[str, Any]:
        return self.account_queries.account_recent_funding_fees(limit=limit)

    def _build_account_recent_funding_fees(self, *, limit: int) -> dict[str, Any]:
        return self.account_queries.build_account_recent_funding_fees(limit=limit)

    def _recent_persisted_funding_fee_summary(self, *, limit: int = 200) -> dict[str, Any]:
        repo = getattr(self.runtime, "funding_fee_repo", None)
        if repo is None:
            return {
                "available": False,
                "count": 0,
                "latest_bill_id": None,
                "latest_bill_ts": None,
                "currencies": [],
                "net_total_by_currency": {},
                "absolute_total_by_currency": {},
                "income_count": 0,
                "expense_count": 0,
            }
        rows = funding_fee_records_for_scope(repo, self.state_scope, limit=limit)
        if not rows:
            return {
                "available": False,
                "count": 0,
                "latest_bill_id": None,
                "latest_bill_ts": None,
                "currencies": [],
                "net_total_by_currency": {},
                "absolute_total_by_currency": {},
                "income_count": 0,
                "expense_count": 0,
            }
        latest = rows[-1]
        net_total_by_currency: dict[str, Decimal] = {}
        absolute_total_by_currency: dict[str, Decimal] = {}
        income_count = 0
        expense_count = 0
        for row in rows:
            net_total_by_currency[row.currency] = net_total_by_currency.get(row.currency, Decimal("0")) + row.amount
            absolute_total_by_currency[row.currency] = absolute_total_by_currency.get(row.currency, Decimal("0")) + abs(row.amount)
            if row.funding_direction == "income":
                income_count += 1
            elif row.funding_direction == "expense":
                expense_count += 1
        return {
            "available": True,
            "count": len(rows),
            "latest_bill_id": latest.bill_id,
            "latest_bill_ts": latest.bill_ts,
            "currencies": sorted(net_total_by_currency.keys()),
            "net_total_by_currency": {key: format(value, "f") for key, value in net_total_by_currency.items()},
            "absolute_total_by_currency": {key: format(value, "f") for key, value in absolute_total_by_currency.items()},
            "income_count": income_count,
            "expense_count": expense_count,
        }

    def account_open_orders(self) -> dict[str, Any]:
        return self.account_queries.account_open_orders()

    def account_recent_fills(self) -> dict[str, Any]:
        return self.account_queries.account_recent_fills()

    def orders_open(self) -> dict[str, Any]:
        return self.account_queries.orders_open()

    def orders_recent(self, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        return self.account_queries.orders_recent(limit=limit, offset=offset)

    def _build_orders_recent(self, *, limit: int, offset: int) -> dict[str, Any]:
        return self.account_queries.build_orders_recent(limit=limit, offset=offset)

    def order_detail(self, client_order_id: str) -> dict[str, Any]:
        return self.account_queries.order_detail(client_order_id)

    def fills_recent(self, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        return self.account_queries.fills_recent(limit=limit, offset=offset)

    def _build_fills_recent(self, *, limit: int, offset: int) -> dict[str, Any]:
        return self.account_queries.build_fills_recent(limit=limit, offset=offset)

    def fill_detail(self, fill_id: str) -> dict[str, Any]:
        return self.account_queries.fill_detail(fill_id)

    def execution_latest(self) -> dict[str, Any]:
        return self.account_queries.execution_latest()

    def _build_execution_latest(self) -> dict[str, Any]:
        return self.account_queries.build_execution_latest()

    def execution_quality_report(self, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        return self.report_queries.execution_quality_report(limit=limit, offset=offset)

    def _execution_quality_summary(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        if not rows:
            return {
                "fill_count": 0,
                "avg_decision_to_submit_latency_ms": None,
                "avg_submit_to_exchange_fill_latency_ms": None,
                "avg_exchange_fill_to_ingestion_latency_ms": None,
                "avg_adverse_slippage_bps": None,
                "avg_fee_ratio": None,
            }

        def _avg_float(key: str) -> float | None:
            values = [float(item[key]) for item in rows if item.get(key) is not None]
            if not values:
                return None
            return round(sum(values) / len(values), 3)

        def _avg_decimal(key: str) -> Decimal | None:
            values = [self._to_decimal(item.get(key)) for item in rows]
            clean = [value for value in values if value is not None]
            if not clean:
                return None
            return sum(clean, start=Decimal("0")) / Decimal(len(clean))

        return {
            "fill_count": len(rows),
            "avg_decision_to_submit_latency_ms": _avg_float("decision_to_submit_latency_ms"),
            "avg_submit_to_exchange_fill_latency_ms": _avg_float("submit_to_exchange_fill_latency_ms"),
            "avg_exchange_fill_to_ingestion_latency_ms": _avg_float("exchange_fill_to_ingestion_latency_ms"),
            "avg_adverse_slippage_bps": _avg_decimal("adverse_slippage_bps"),
            "avg_fee_ratio": _avg_decimal("fee_ratio"),
        }

    @staticmethod
    def _fill_outcome_event_timestamp(record: Any) -> datetime | None:
        return getattr(record, "ingestion_timestamp", None) or getattr(record, "exchange_timestamp", None) or getattr(record, "created_at", None)

    def _fill_outcome_position_key(self, outcome: Any) -> str:
        if getattr(outcome, "position_key", None):
            return str(outcome.position_key)
        return build_position_key(
            symbol=str(getattr(outcome, "symbol", "") or ""),
            product_type=str(getattr(outcome, "product_type", "spot") or "spot"),
            position_mode=getattr(outcome, "position_mode", None),
            pos_side=getattr(outcome, "pos_side", None),
        )

    def _new_position_lifecycle(
        self,
        *,
        outcome: Any,
        position_key: str,
        event_timestamp: datetime | None,
        starting_position_qty: Decimal,
    ) -> dict[str, Any]:
        opening_avg_entry_price = self._to_decimal(getattr(outcome, "starting_avg_entry_price", None))
        return {
            "lifecycle_id": f"lifecycle:{position_key}:{getattr(outcome, 'fill_id', 'unknown')}",
            "symbol": getattr(outcome, "symbol", None),
            "position_key": position_key,
            "position_mode": getattr(outcome, "position_mode", None),
            "pos_side": getattr(outcome, "pos_side", None),
            "product_type": getattr(outcome, "product_type", None),
            "margin_mode": getattr(outcome, "margin_mode", None),
            "instrument_family": getattr(outcome, "instrument_family", None),
            "settle_currency": getattr(outcome, "settle_currency", None),
            "status": "open",
            "opened_at": event_timestamp,
            "closed_at": None,
            "opened_by_fill_id": getattr(outcome, "fill_id", None),
            "opened_by_transition_fill_id": None,
            "closed_by_fill_id": None,
            "carry_in_position": abs(starting_position_qty) > self._DECIMAL_EPSILON,
            "contains_reversal_transition": False,
            "fill_count": 0,
            "fill_ids": [],
            "latest_fill_id": None,
            "execution_actions": [],
            "starting_position_qty": starting_position_qty,
            "current_position_qty": starting_position_qty,
            "ending_position_qty": starting_position_qty,
            "opening_avg_entry_price": opening_avg_entry_price,
            "ending_avg_entry_price": opening_avg_entry_price,
            "peak_abs_position_qty": abs(starting_position_qty),
            "entry_notional_total": Decimal("0"),
            "exit_notional_total": Decimal("0"),
            "trading_gross_realized_pnl": Decimal("0"),
            "trading_net_realized_pnl": Decimal("0"),
            "fee_total": Decimal("0"),
            "funding_fee_total": Decimal("0"),
            "combined_net_realized_pnl": Decimal("0"),
            "funding_fee_event_count": 0,
            "funding_fee_bill_ids": [],
            "funding_fee_attribution_scope": "none",
        }

    def _append_fill_to_lifecycle(
        self,
        *,
        lifecycle: dict[str, Any],
        outcome: Any,
        event_timestamp: datetime | None,
        reversal_transition: bool,
    ) -> None:
        starting_qty = self._to_decimal(getattr(outcome, "starting_position_qty", None))
        if starting_qty is None:
            starting_qty = self._to_decimal(lifecycle.get("current_position_qty")) or Decimal("0")
        ending_qty = self._to_decimal(getattr(outcome, "ending_position_qty", None)) or Decimal("0")
        fill_notional = self._to_decimal(getattr(outcome, "fill_notional", None))
        if fill_notional is None:
            fill_qty = self._to_decimal(getattr(outcome, "fill_qty", None)) or Decimal("0")
            fill_price = self._to_decimal(getattr(outcome, "fill_price", None)) or Decimal("0")
            fill_notional = fill_qty * fill_price
        fill_notional = abs(fill_notional)
        fee_amount = self._to_decimal(getattr(outcome, "fee_amount", None))
        if fee_amount is None:
            fee_amount = abs(self._to_decimal(getattr(outcome, "fee_delta", None)) or Decimal("0"))
        else:
            fee_amount = abs(fee_amount)

        start_flat = abs(starting_qty) <= self._DECIMAL_EPSILON
        end_flat = abs(ending_qty) <= self._DECIMAL_EPSILON
        if reversal_transition:
            lifecycle["exit_notional_total"] += fill_notional
        elif start_flat and not end_flat:
            lifecycle["entry_notional_total"] += fill_notional
        elif not start_flat and end_flat:
            lifecycle["exit_notional_total"] += fill_notional
        elif abs(ending_qty) > abs(starting_qty) + self._DECIMAL_EPSILON:
            lifecycle["entry_notional_total"] += fill_notional
        elif abs(ending_qty) + self._DECIMAL_EPSILON < abs(starting_qty):
            lifecycle["exit_notional_total"] += fill_notional

        lifecycle["fill_count"] += 1
        lifecycle["fill_ids"].append(getattr(outcome, "fill_id", None))
        lifecycle["latest_fill_id"] = getattr(outcome, "fill_id", None)
        lifecycle["current_position_qty"] = ending_qty
        lifecycle["ending_position_qty"] = ending_qty
        lifecycle["ending_avg_entry_price"] = self._to_decimal(getattr(outcome, "ending_avg_entry_price", None))
        lifecycle["peak_abs_position_qty"] = max(
            self._to_decimal(lifecycle.get("peak_abs_position_qty")) or Decimal("0"),
            abs(starting_qty),
            abs(ending_qty),
        )
        lifecycle["trading_gross_realized_pnl"] += self._to_decimal(getattr(outcome, "gross_realized_pnl", None)) or Decimal("0")
        lifecycle["trading_net_realized_pnl"] += self._to_decimal(getattr(outcome, "realized_pnl_delta", None)) or Decimal("0")
        lifecycle["fee_total"] += fee_amount
        lifecycle["combined_net_realized_pnl"] = lifecycle["trading_net_realized_pnl"] + lifecycle["funding_fee_total"]
        execution_action = getattr(outcome, "execution_action", None)
        if execution_action and execution_action not in lifecycle["execution_actions"]:
            lifecycle["execution_actions"].append(execution_action)
        if event_timestamp is not None and lifecycle.get("opened_at") is None:
            lifecycle["opened_at"] = event_timestamp

    def _build_position_lifecycle_rows(
        self,
        *,
        outcomes: list[Any],
        funding_records: list[Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        ordered_outcomes = sorted(
            outcomes,
            key=lambda item: (
                self._fill_outcome_event_timestamp(item) or datetime.min.replace(tzinfo=timezone.utc),
                str(getattr(item, "fill_id", "") or ""),
            ),
        )
        lifecycles: list[dict[str, Any]] = []
        active_by_position_key: dict[str, dict[str, Any]] = {}

        for outcome in ordered_outcomes:
            position_key = self._fill_outcome_position_key(outcome)
            event_timestamp = self._fill_outcome_event_timestamp(outcome)
            starting_qty = self._to_decimal(getattr(outcome, "starting_position_qty", None)) or Decimal("0")
            ending_qty = self._to_decimal(getattr(outcome, "ending_position_qty", None)) or Decimal("0")
            start_sign = 1 if starting_qty > self._DECIMAL_EPSILON else -1 if starting_qty < -self._DECIMAL_EPSILON else 0
            end_sign = 1 if ending_qty > self._DECIMAL_EPSILON else -1 if ending_qty < -self._DECIMAL_EPSILON else 0
            reversal_transition = start_sign != 0 and end_sign != 0 and start_sign != end_sign

            lifecycle = active_by_position_key.get(position_key)
            if lifecycle is None:
                lifecycle = self._new_position_lifecycle(
                    outcome=outcome,
                    position_key=position_key,
                    event_timestamp=event_timestamp,
                    starting_position_qty=starting_qty,
                )
                active_by_position_key[position_key] = lifecycle
            elif abs(starting_qty) <= self._DECIMAL_EPSILON and abs(self._to_decimal(lifecycle.get("current_position_qty")) or Decimal("0")) > self._DECIMAL_EPSILON:
                lifecycle["status"] = "closed"
                lifecycle["closed_at"] = event_timestamp
                lifecycle["closed_by_fill_id"] = getattr(outcome, "fill_id", None)
                lifecycle["contains_reversal_transition"] = True
                lifecycles.append(lifecycle)
                lifecycle = self._new_position_lifecycle(
                    outcome=outcome,
                    position_key=position_key,
                    event_timestamp=event_timestamp,
                    starting_position_qty=starting_qty,
                )
                active_by_position_key[position_key] = lifecycle

            self._append_fill_to_lifecycle(
                lifecycle=lifecycle,
                outcome=outcome,
                event_timestamp=event_timestamp,
                reversal_transition=reversal_transition,
            )
            if reversal_transition:
                lifecycle["status"] = "closed"
                lifecycle["closed_at"] = event_timestamp
                lifecycle["closed_by_fill_id"] = getattr(outcome, "fill_id", None)
                lifecycle["contains_reversal_transition"] = True
                lifecycle["current_position_qty"] = Decimal("0")
                lifecycle["ending_position_qty"] = Decimal("0")
                lifecycles.append(lifecycle)
                reopened = self._new_position_lifecycle(
                    outcome=outcome,
                    position_key=position_key,
                    event_timestamp=event_timestamp,
                    starting_position_qty=Decimal("0"),
                )
                reopened["opened_by_fill_id"] = None
                reopened["opened_by_transition_fill_id"] = getattr(outcome, "fill_id", None)
                reopened["contains_reversal_transition"] = True
                reopened["opening_avg_entry_price"] = self._to_decimal(getattr(outcome, "ending_avg_entry_price", None))
                reopened["ending_avg_entry_price"] = self._to_decimal(getattr(outcome, "ending_avg_entry_price", None))
                reopened["current_position_qty"] = ending_qty
                reopened["ending_position_qty"] = ending_qty
                reopened["peak_abs_position_qty"] = max(
                    self._to_decimal(reopened.get("peak_abs_position_qty")) or Decimal("0"),
                    abs(ending_qty),
                )
                active_by_position_key[position_key] = reopened
                continue

            if abs(ending_qty) <= self._DECIMAL_EPSILON:
                lifecycle["status"] = "closed"
                lifecycle["closed_at"] = event_timestamp
                lifecycle["closed_by_fill_id"] = getattr(outcome, "fill_id", None)
                lifecycles.append(lifecycle)
                active_by_position_key.pop(position_key, None)

        lifecycles.extend(active_by_position_key.values())
        for lifecycle in lifecycles:
            lifecycle["combined_net_realized_pnl"] = (
                self._to_decimal(lifecycle.get("trading_net_realized_pnl")) or Decimal("0")
            ) + (self._to_decimal(lifecycle.get("funding_fee_total")) or Decimal("0"))

        unassigned_funding_fees: list[dict[str, Any]] = []
        for record in sorted(
            funding_records,
            key=lambda item: (
                self._funding_fee_event_timestamp(item) or datetime.min.replace(tzinfo=timezone.utc),
                str(getattr(item, "bill_id", "") or ""),
            ),
        ):
            bill_timestamp = self._funding_fee_event_timestamp(record)
            symbol = str(getattr(record, "symbol", "") or "")
            candidates = [
                lifecycle
                for lifecycle in lifecycles
                if symbol
                and lifecycle.get("symbol") == symbol
                and lifecycle.get("opened_at") is not None
                and bill_timestamp is not None
                and lifecycle["opened_at"] <= bill_timestamp
                and (lifecycle.get("closed_at") is None or bill_timestamp <= lifecycle["closed_at"])
            ]
            if len(candidates) == 1:
                lifecycle = candidates[0]
                amount = self._to_decimal(getattr(record, "amount", None)) or Decimal("0")
                lifecycle["funding_fee_total"] += amount
                lifecycle["funding_fee_event_count"] += 1
                lifecycle["funding_fee_bill_ids"].append(getattr(record, "bill_id", None))
                lifecycle["funding_fee_attribution_scope"] = "symbol_window"
                lifecycle["combined_net_realized_pnl"] = (
                    self._to_decimal(lifecycle.get("trading_net_realized_pnl")) or Decimal("0")
                ) + lifecycle["funding_fee_total"]
                continue
            reason = "no_matching_position_window" if not candidates else "ambiguous_symbol_overlap"
            unassigned_funding_fees.append(
                {
                    **self._funding_fee_profitability_row(record),
                    "attribution_reason": reason,
                }
            )
        return lifecycles, unassigned_funding_fees

    def position_lifecycle_profitability(self, *, limit: int = 100) -> dict[str, Any]:
        normalized_limit = max(int(limit), 1)
        outcomes = list(self._scoped_fill_outcomes())
        funding_records = list(self._scoped_funding_fee_records())
        lifecycles, unassigned_funding_fees = self._build_position_lifecycle_rows(
            outcomes=outcomes,
            funding_records=funding_records,
        )
        lifecycles.sort(
            key=lambda item: (
                item.get("closed_at") or item.get("opened_at") or datetime.min.replace(tzinfo=timezone.utc),
                str(item.get("lifecycle_id") or ""),
            ),
            reverse=True,
        )
        visible_rows = lifecycles[:normalized_limit]
        closed_count = sum(1 for item in lifecycles if item.get("status") == "closed")
        open_count = sum(1 for item in lifecycles if item.get("status") != "closed")
        trading_net = sum(
            (self._to_decimal(item.get("trading_net_realized_pnl")) or Decimal("0"))
            for item in lifecycles
        )
        funding_net = sum(
            (self._to_decimal(item.get("funding_fee_total")) or Decimal("0"))
            for item in lifecycles
        )
        unassigned_funding_net = sum(
            (self._to_decimal(item.get("funding_fee_delta")) or Decimal("0"))
            for item in unassigned_funding_fees
        )
        return {
            "summary": {
                "lifecycle_count": len(lifecycles),
                "closed_lifecycle_count": closed_count,
                "open_lifecycle_count": open_count,
                "assigned_funding_fee_count": sum(int(item.get("funding_fee_event_count") or 0) for item in lifecycles),
                "unassigned_funding_fee_count": len(unassigned_funding_fees),
                "trading_net_realized_pnl": trading_net,
                "assigned_funding_fee_net_pnl": funding_net,
                "unassigned_funding_fee_net_pnl": unassigned_funding_net,
                "combined_net_realized_pnl": trading_net + funding_net + unassigned_funding_net,
            },
            "lifecycles": visible_rows,
            "unassigned_funding_fees": unassigned_funding_fees[:normalized_limit],
            "has_more": len(lifecycles) > normalized_limit,
            "truth_source": "fill_outcomes_plus_funding_fee_records",
        }

    def profitability_overview(self, *, limit: int = 100) -> dict[str, Any]:
        return self.report_queries.profitability_overview(limit=limit)

    def forward_validation_report(self, *, window_days: int = 7, period_count: int = 4) -> dict[str, Any]:
        return self.report_queries.forward_validation_report(window_days=window_days, period_count=period_count)

    def _build_forward_validation_report(self, *, window_days: int, period_count: int) -> dict[str, Any]:
        normalized_window_days = max(int(window_days), 1)
        normalized_period_count = max(int(period_count), 1)
        all_rows = [self._execution_quality_row(item) for item in self._scoped_fill_outcomes()]
        all_rows.sort(
            key=lambda item: item.get("ingestion_timestamp") or item.get("exchange_fill_timestamp") or datetime.min,
            reverse=True,
        )
        funding_rows = [
            self._funding_fee_profitability_row(item)
            for item in self._scoped_funding_fee_records()
        ]
        funding_rows.sort(
            key=lambda item: item.get("event_timestamp") or datetime.min,
            reverse=True,
        )
        now = utc_now()
        periods: list[dict[str, Any]] = []
        for index in range(normalized_period_count):
            period_end = now - timedelta(days=index * normalized_window_days)
            period_start = period_end - timedelta(days=normalized_window_days)
            period_rows = [
                row
                for row in all_rows
                if isinstance(row.get("ingestion_timestamp") or row.get("exchange_fill_timestamp"), datetime)
                and period_start <= (row.get("ingestion_timestamp") or row.get("exchange_fill_timestamp")) < period_end
            ]
            period_funding_rows = [
                row
                for row in funding_rows
                if isinstance(row.get("event_timestamp"), datetime)
                and period_start <= row.get("event_timestamp") < period_end
            ]
            periods.append(
                self._forward_validation_period(
                    rows=period_rows,
                    funding_rows=period_funding_rows,
                    period_start=period_start,
                    period_end=period_end,
                )
            )

        latest_period = periods[0] if periods else None
        recommendation = self._forward_validation_recommendation(latest_period)
        return {
            "window_days": normalized_window_days,
            "period_count": normalized_period_count,
            "generated_at": now,
            "summary": recommendation,
            "periods": periods,
            "truth_source": "fill_outcomes_plus_funding_fee_records",
        }

    def _forward_validation_period(
        self,
        *,
        rows: list[dict[str, Any]],
        funding_rows: list[dict[str, Any]],
        period_start: datetime,
        period_end: datetime,
    ) -> dict[str, Any]:
        closed_fill_count = len(rows)
        net_realized = sum((self._to_decimal(item.get("realized_pnl_delta")) or Decimal("0")) for item in rows)
        gross_realized = sum((self._to_decimal(item.get("gross_realized_pnl")) or Decimal("0")) for item in rows)
        total_fees = sum((self._to_decimal(item.get("fee_amount")) or Decimal("0")) for item in rows)
        total_notional = sum((self._to_decimal(item.get("fill_notional")) or Decimal("0")) for item in rows)
        funding_fee_net_pnl = sum((self._to_decimal(item.get("funding_fee_delta")) or Decimal("0")) for item in funding_rows)
        combined_net_realized_pnl = net_realized + funding_fee_net_pnl
        winning_fill_count = 0
        losing_fill_count = 0
        total_adverse_slippage = Decimal("0")
        slippage_observation_count = 0
        high_slippage_count = 0
        slow_submit_to_fill_count = 0
        for row in rows:
            pnl = self._to_decimal(row.get("realized_pnl_delta")) or Decimal("0")
            slippage = self._to_decimal(row.get("adverse_slippage_bps"))
            latency = row.get("submit_to_exchange_fill_latency_ms")
            if pnl > self._DECIMAL_EPSILON:
                winning_fill_count += 1
            elif pnl < -self._DECIMAL_EPSILON:
                losing_fill_count += 1
            if slippage is not None:
                total_adverse_slippage += slippage
                slippage_observation_count += 1
                if slippage > Decimal(str(max(self.runtime.settings.max_slippage_tolerance_bps * 0.5, 2))):
                    high_slippage_count += 1
            if isinstance(latency, (int, float)) and latency > 10_000:
                slow_submit_to_fill_count += 1

        fee_to_notional_ratio = (
            None if abs(total_notional) <= self._DECIMAL_EPSILON else total_fees / total_notional
        )
        avg_adverse_slippage_bps = (
            None if slippage_observation_count == 0 else total_adverse_slippage / Decimal(slippage_observation_count)
        )
        high_slippage_ratio = None if closed_fill_count == 0 else round(high_slippage_count / closed_fill_count, 6)
        slow_submit_to_fill_ratio = (
            None if closed_fill_count == 0 else round(slow_submit_to_fill_count / closed_fill_count, 6)
        )
        status = "healthy"
        if closed_fill_count < int(self.runtime.settings.trial_guard_min_closed_fills):
            status = "insufficient_data"
        elif combined_net_realized_pnl < Decimal("0"):
            status = "caution"
        if (
            fee_to_notional_ratio is not None
            and fee_to_notional_ratio >= Decimal(str(self.runtime.settings.trial_guard_max_fee_to_notional_ratio))
        ) or (
            high_slippage_ratio is not None
            and high_slippage_ratio >= float(self.runtime.settings.trial_guard_max_high_slippage_ratio)
        ) or (
            slow_submit_to_fill_ratio is not None
            and slow_submit_to_fill_ratio >= float(self.runtime.settings.trial_guard_max_slow_submit_to_fill_ratio)
        ):
            status = "failing"
        return {
            "period_start": period_start,
            "period_end": period_end,
            "closed_fill_count": closed_fill_count,
            "winning_fill_count": winning_fill_count,
            "losing_fill_count": losing_fill_count,
            "win_rate": None if closed_fill_count == 0 else round(winning_fill_count / closed_fill_count, 6),
            "gross_realized_pnl": gross_realized,
            "net_realized_pnl": net_realized,
            "trading_net_realized_pnl": net_realized,
            "funding_fee_net_pnl": funding_fee_net_pnl,
            "combined_net_realized_pnl": combined_net_realized_pnl,
            "total_fees": total_fees,
            "total_notional": total_notional,
            "fee_to_notional_ratio": fee_to_notional_ratio,
            "avg_adverse_slippage_bps": avg_adverse_slippage_bps,
            "high_slippage_count": high_slippage_count,
            "high_slippage_ratio": high_slippage_ratio,
            "slow_submit_to_fill_count": slow_submit_to_fill_count,
            "slow_submit_to_fill_ratio": slow_submit_to_fill_ratio,
            "funding_fee_event_count": len(funding_rows),
            "status": status,
        }

    def _forward_validation_recommendation(self, latest_period: dict[str, Any] | None) -> dict[str, Any]:
        if latest_period is None:
            return {
                "verdict": "insufficient_data",
                "summary": "当前还没有可用于前向验证的已完成成交样本。",
                "reasons": ["no_forward_validation_rows"],
            }
        reasons: list[str] = []
        verdict = "continue"
        if latest_period["status"] == "insufficient_data":
            verdict = "observe"
            reasons.append("insufficient_forward_validation_sample")
        if latest_period["status"] == "caution":
            verdict = "shrink"
            reasons.append("negative_net_realized_pnl")
        if latest_period["status"] == "failing":
            verdict = "pause"
            reasons.append("execution_quality_or_fee_threshold_breached")
        if (
            self._to_decimal(latest_period.get("combined_net_realized_pnl")) is not None
            and self._to_decimal(latest_period.get("combined_net_realized_pnl")) <= -Decimal(str(self.runtime.settings.trial_guard_max_daily_loss_usdt))
        ):
            verdict = "pause"
            reasons.append("forward_validation_loss_limit_breached")
        summary_map = {
            "continue": "最近一个验证周期表现稳定，可以继续保持当前小资金试盘。",
            "observe": "当前样本量仍然不足，继续观察，不要扩大风险。",
            "shrink": "最近一个验证周期边际转弱，建议缩小仓位并继续观察。",
            "pause": "最近一个验证周期已经不满足试盘要求，建议暂停并复盘策略与执行质量。",
        }
        return {
            "verdict": verdict,
            "summary": summary_map[verdict],
            "reasons": reasons,
        }

    @staticmethod
    def _trial_guard_hard_stop_payload(trial_guard: dict[str, Any]) -> dict[str, Any]:
        hard_stop = dict(trial_guard.get("hard_stop") or {})
        breaches = list(trial_guard.get("breaches") or [])
        recovery_requirements = dict(trial_guard.get("recovery_requirements") or {})
        active = bool(hard_stop.get("active")) or str(trial_guard.get("status") or "") == "breached"
        return {
            "active": active,
            "status": trial_guard.get("status"),
            "summary": hard_stop.get("summary") or trial_guard.get("summary"),
            "operator_guidance": hard_stop.get("operator_guidance") or trial_guard.get("recommended_action"),
            "breaches": breaches,
            "recovery_requirements": recovery_requirements,
            "resume_blocked": bool(hard_stop.get("resume_blocked")) or active,
        }

    def _trial_runtime_constraints(
        self,
        *,
        recovery: dict[str, Any],
        active_blockers: list[dict[str, Any]],
        trial_guard: dict[str, Any],
    ) -> dict[str, Any]:
        hard_stop = self._trial_guard_hard_stop_payload(trial_guard)
        reasons: list[str] = []
        if hard_stop.get("active"):
            reasons.append("trial_guard_hard_stop_active")
        if recovery.get("halted"):
            reasons.append("runtime_halted")
        if not recovery.get("safe_to_trade", False):
            reasons.append("recovery_not_safe_to_trade")
        if recovery.get("review_required"):
            reasons.append("manual_review_required")
        if active_blockers:
            reasons.append("active_execution_blockers_present")
        return {
            "hard_stop_active": bool(hard_stop.get("active")),
            "safe_to_trade": bool(recovery.get("safe_to_trade")),
            "review_required": bool(recovery.get("review_required")),
            "active_blocker_count": len(active_blockers),
            "can_continue_runtime": (
                not hard_stop.get("active")
                and bool(recovery.get("safe_to_trade"))
                and not bool(recovery.get("review_required"))
                and not active_blockers
            ),
            "can_scale_up": (
                not hard_stop.get("active")
                and bool(recovery.get("safe_to_trade"))
                and not bool(recovery.get("review_required"))
                and not active_blockers
            ),
            "reasons": list(dict.fromkeys(reasons)),
        }

    def scaling_readiness_report(
        self,
        *,
        window_days: int = 7,
        period_count: int = 4,
        forward_validation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.report_queries.scaling_readiness_report(
            window_days=window_days,
            period_count=period_count,
            forward_validation=forward_validation,
        )

    def _build_scaling_readiness_report(
        self,
        *,
        window_days: int,
        period_count: int,
        forward_validation: dict[str, Any] | None,
    ) -> dict[str, Any]:
        forward_validation = forward_validation or self.forward_validation_report(
            window_days=window_days,
            period_count=period_count,
        )
        periods = list(forward_validation.get("periods") or [])
        latest_period = periods[0] if periods else None
        forward_summary = dict(forward_validation.get("summary") or {})
        recovery = self.recovery_view()
        trial_guard = self.trial_guard()
        hard_stop = self._trial_guard_hard_stop_payload(trial_guard)
        active_blockers = [item for item in self.blockers() if not item.get("submit_only")]
        runtime_constraints = self._trial_runtime_constraints(
            recovery=recovery,
            active_blockers=active_blockers,
            trial_guard=trial_guard,
        )
        latest_review = self.latest_operator_action("capital_scale_review")

        consecutive_healthy_periods = 0
        for period in periods:
            if (
                period.get("status") == "healthy"
                and (self._to_decimal(period.get("combined_net_realized_pnl")) or Decimal("0")) > self._DECIMAL_EPSILON
                and int(period.get("closed_fill_count") or 0) >= int(self.runtime.settings.trial_guard_min_closed_fills)
            ):
                consecutive_healthy_periods += 1
                continue
            break

        verdict = "continue_small_capital"
        reasons: list[str] = []
        if not trial_guard.get("enabled"):
            reasons.append("trial_guard_not_enabled")
        elif not trial_guard.get("trial_observation_active"):
            reasons.append("trial_observation_flow_inactive")

        forward_verdict = str(forward_summary.get("verdict") or "")
        if forward_verdict == "pause":
            verdict = "pause_trial"
            reasons.append("forward_validation_pause")
        elif forward_verdict == "shrink":
            verdict = "shrink_trial"
            reasons.append("forward_validation_shrink")
        elif forward_verdict in {"observe", "insufficient_data"}:
            reasons.append("forward_validation_still_observing")
        elif forward_verdict == "continue":
            reasons.append("forward_validation_stable")

        healthy_period_requirement_met = consecutive_healthy_periods >= 2
        latest_period_has_sample = (
            latest_period is not None
            and int(latest_period.get("closed_fill_count") or 0) >= int(self.runtime.settings.trial_guard_min_closed_fills)
        )
        if verdict == "continue_small_capital" and (
            not healthy_period_requirement_met or not latest_period_has_sample
        ):
            reasons.append("healthy_period_requirement_not_met")

        if (
            verdict == "continue_small_capital"
            and trial_guard.get("status") == "monitoring"
            and trial_guard.get("enabled_for_runtime")
            and healthy_period_requirement_met
            and latest_period_has_sample
            and forward_verdict == "continue"
            and runtime_constraints.get("can_scale_up")
        ):
            verdict = "approve_scale_up"
            reasons.append("scale_up_requirements_met")

        summary_map = {
            "approve_scale_up": "最近多个试盘周期表现稳定，可以进入人工放量评审。",
            "continue_small_capital": "当前仍应保持小资金试盘，继续积累样本，不建议直接放量。",
            "shrink_trial": "最近试盘边际转弱，建议先缩小试盘规模，再继续观察收益与执行质量。",
            "pause_trial": "最近观察周期已经不满足继续试盘的建议条件，先暂停试盘并复盘收益与执行质量。",
        }
        if not trial_guard.get("enabled"):
            summary = "试盘守护当前未启用，这份试盘建议只能作为观察参考，不应直接拿来做放量判断。"
        elif not trial_guard.get("trial_observation_active"):
            summary = "当前运行线不在试盘观察流程里，这份试盘建议仅供参考，不应用来直接决定放量或恢复。"
        else:
            summary = summary_map[verdict]
        return {
            "window_days": int(forward_validation.get("window_days") or window_days),
            "period_count": int(forward_validation.get("period_count") or period_count),
            "generated_at": utc_now(),
            "readiness": verdict,
            "summary": summary,
            "reasons": list(dict.fromkeys(reasons)),
            "requirements": {
                "required_healthy_periods": 2,
                "required_min_closed_fills_per_period": int(self.runtime.settings.trial_guard_min_closed_fills),
                "consecutive_healthy_periods": consecutive_healthy_periods,
                "healthy_period_requirement_met": healthy_period_requirement_met,
                "latest_period_has_sample": latest_period_has_sample,
                "trial_guard_enabled": bool(trial_guard.get("enabled")),
                "trial_guard_enabled_for_runtime": bool(trial_guard.get("enabled_for_runtime")),
                "trial_observation_flow_active": bool(trial_guard.get("trial_observation_active")),
                "trial_observation_label": trial_guard.get("trial_observation_label"),
                "trial_guard_status": trial_guard.get("status"),
                "trial_guard_hard_stop_active": bool(hard_stop.get("active")),
                "trial_guard_profile_active": bool(trial_guard.get("profile_active")),
                "safe_to_trade": bool(recovery.get("safe_to_trade")),
                "review_required": bool(recovery.get("review_required")),
                "active_blocker_count": len(active_blockers),
            },
            "latest_forward_validation": latest_period,
            "forward_validation_summary": forward_summary,
            "trial_guard": trial_guard,
            "trial_guard_hard_stop": hard_stop,
            "runtime_constraints": runtime_constraints,
            "recovery": recovery,
            "active_blockers": active_blockers,
            "latest_review": latest_review,
            "periods": periods,
            "truth_source": "fill_outcomes_plus_runtime_controls",
        }

    def trial_review_packet(
        self,
        *,
        profitability_limit: int = 100,
        anomaly_limit: int = 100,
        segment_limit: int = 100,
        window_days: int = 7,
        period_count: int = 4,
    ) -> dict[str, Any]:
        return self.report_queries.trial_review_packet(
            profitability_limit=profitability_limit,
            anomaly_limit=anomaly_limit,
            segment_limit=segment_limit,
            window_days=window_days,
            period_count=period_count,
        )

    def trial_review_summary(
        self,
        *,
        segment_limit: int = 100,
        window_days: int = 7,
        period_count: int = 4,
    ) -> dict[str, Any]:
        return self.report_queries.trial_review_summary(
            segment_limit=segment_limit,
            window_days=window_days,
            period_count=period_count,
        )

    def trial_review_details(
        self,
        *,
        profitability_limit: int = 100,
        anomaly_limit: int = 100,
        segment_limit: int = 100,
        window_days: int = 7,
        period_count: int = 4,
    ) -> dict[str, Any]:
        return self.report_queries.trial_review_details(
            profitability_limit=profitability_limit,
            anomaly_limit=anomaly_limit,
            segment_limit=segment_limit,
            window_days=window_days,
            period_count=period_count,
        )

    def _trial_review_action_items(
        self,
        *,
        scaling_readiness: str,
        high_slippage_count: int,
        slow_submit_to_fill_count: int,
        trial_guard: dict[str, Any],
        recovery: dict[str, Any],
        blocker_rows: list[dict[str, Any]],
    ) -> list[str]:
        action_items: list[str] = []
        hard_stop = self._trial_guard_hard_stop_payload(trial_guard)
        if hard_stop.get("active"):
            action_items.append("当前已触发试盘守护硬停机，先确认触发阈值为什么命中，再决定是否恢复自动运行。")
            action_items.append("如果确认要重新开始采样，应先人工重置试盘守护，再手动恢复自动运行。")
        if scaling_readiness == "approve_scale_up":
            action_items.append("最近多个验证周期表现稳定，可以发起下一档资金量的人工评审，但仍要保持 guarded_live 约束。")
        elif scaling_readiness == "continue_small_capital":
            action_items.append("继续保持当前小资金试盘，先积累更多稳定样本，再考虑放量。")
        elif scaling_readiness == "shrink_trial":
            action_items.append("先缩小试盘仓位和总暴露，观察收益、滑点和资金费拖累是否收敛。")
        else:
            action_items.append("先暂停试盘并复盘最近周期的收益、执行质量、恢复状态和资金费拖累。")
        if high_slippage_count > 0:
            action_items.append("高滑点样本仍然偏多，优先复核下单保护、挂单策略和市场冲击。")
        if slow_submit_to_fill_count > 0:
            action_items.append("submit 到 fill 的耗时偏长，优先排查报单链路、价格保护和交易所回报延迟。")
        if recovery.get("review_required"):
            action_items.append("恢复状态仍要求人工复核，先完成对账、rebaseline 或恢复审批。")
        if blocker_rows:
            action_items.append("当前仍有执行阻断项，先处理阻断项再讨论继续试盘或放量。")
        return list(dict.fromkeys(action_items))

    @staticmethod
    def _trial_review_action_catalog(
        *,
        hard_stop_active: bool,
        scaling_readiness: str,
        runtime_constraints: dict[str, Any],
    ) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = [
            {
                "label": "查看风险与恢复",
                "client_action": "navigate-view",
                "value": "risk",
                "tone": "ghost",
                "category": "navigate",
            }
        ]
        if hard_stop_active:
            actions.extend(
                [
                    {
                        "label": "查看委托与成交",
                        "client_action": "navigate-view",
                        "value": "execution",
                        "tone": "ghost",
                        "category": "navigate",
                    },
                    {
                        "label": "人工重置试盘守护",
                        "client_action": "record-trial-review-action",
                        "value": "reset_trial_guard",
                        "tone": "warning",
                        "category": "record",
                    },
                    {
                        "label": "记录本次复盘",
                        "client_action": "record-trial-review-action",
                        "value": "review_snapshot",
                        "tone": "secondary",
                        "category": "record",
                    },
                    {
                        "label": "刷新当前状态",
                        "client_action": "refresh-dashboard",
                        "value": "",
                        "tone": "warning",
                        "category": "refresh",
                    },
                ]
            )
            return actions

        verdict_to_button = {
            "approve_scale_up": ("提交放量评审", "approve_scale_up", "warning"),
            "continue_small_capital": ("记为继续小资金试盘", "continue_small_capital", "secondary"),
            "shrink_trial": ("记为缩小试盘规模", "shrink_trial", "warning"),
            "pause_trial": ("记为暂停试盘并复盘", "pause_trial", "warning"),
        }
        verdict_button = verdict_to_button.get(str(scaling_readiness or ""))
        if verdict_button is not None:
            label, value, tone = verdict_button
            actions.append(
                {
                    "label": label,
                    "client_action": "record-trial-review-action",
                    "value": value,
                    "tone": tone,
                    "category": "record",
                }
            )
        actions.append(
            {
                "label": "记录本次复盘",
                "client_action": "record-trial-review-action",
                "value": "review_snapshot",
                "tone": "ghost",
                "category": "record",
            }
        )
        if not runtime_constraints.get("can_continue_runtime", False):
            actions.append(
                {
                    "label": "刷新当前状态",
                    "client_action": "refresh-dashboard",
                    "value": "",
                    "tone": "warning",
                    "category": "refresh",
                }
            )
        return actions

    @staticmethod
    def _normalize_trial_review_action_payload(payload: dict[str, Any]) -> dict[str, Any]:
        action = str(payload.get("action") or "")
        details = dict(payload.get("details") or {})
        selected_action = str(
            details.get("trial_review_action_type")
            or details.get("selected_verdict")
            or ("review_snapshot" if action == "trial_review_snapshot" else "")
        )
        label_map = {
            "review_snapshot": "记录本次复盘",
            "reset_trial_guard": "人工重置试盘守护",
            "continue_small_capital": "继续小资金试盘",
            "shrink_trial": "缩小试盘规模",
            "pause_trial": "暂停试盘并复盘",
            "approve_scale_up": "提交放量评审",
        }
        return {
            "action_id": payload.get("action_id"),
            "action": action,
            "selected_action": selected_action,
            "label": label_map.get(selected_action, selected_action or action),
            "status": payload.get("status"),
            "reason": payload.get("reason"),
            "actor_role": payload.get("actor_role"),
            "actor_identity": payload.get("actor_identity"),
            "auth_source": payload.get("auth_source"),
            "created_at": payload.get("created_at"),
            "recovery_state_before": payload.get("recovery_state_before"),
            "recovery_state_after": payload.get("recovery_state_after"),
            "details": details,
        }

    def _latest_trial_review_action(self) -> dict[str, Any] | None:
        actions = self._cached(
            "operator_action_events",
            lambda: self.runtime.event_store.by_topic(topics.OPERATOR_ACTIONS),
        )
        for item in reversed(actions):
            action = str(item.payload.get("action") or "")
            if action not in {"trial_review_snapshot", "capital_scale_review", "trial_guard_manual_reset"}:
                continue
            payload = self.payload(item)
            if payload is None:
                continue
            return self._normalize_trial_review_action_payload(payload)
        return None

    def _trial_review_history_payload(self, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        actions = self._cached(
            "operator_action_events",
            lambda: self.runtime.event_store.by_topic(topics.OPERATOR_ACTIONS),
        )
        rows: list[dict[str, Any]] = []
        for item in reversed(actions):
            action = str(item.payload.get("action") or "")
            if action not in {"trial_review_snapshot", "capital_scale_review", "trial_guard_manual_reset"}:
                continue
            payload = self.payload(item)
            if payload is None:
                continue
            rows.append(self._normalize_trial_review_action_payload(payload))
        return self._paginate_rows(rows, limit=limit, offset=offset, key="actions")

    def _trial_review_workbench_payload(
        self,
        *,
        scaling_readiness: str,
        scaling: dict[str, Any],
        trial_guard: dict[str, Any],
        hard_stop: dict[str, Any],
        runtime_constraints: dict[str, Any],
        action_items: list[str],
    ) -> dict[str, Any]:
        latest_action = self._latest_trial_review_action()
        history_preview = self._trial_review_history_payload(limit=5, offset=0)
        return {
            "hard_stop": hard_stop,
            "advisory_recommendation": {
                "verdict": scaling_readiness,
                "headline": scaling.get("summary"),
                "reasons": list(scaling.get("reasons") or []),
            },
            "runtime_constraints": runtime_constraints,
            "action_items": action_items,
            "available_actions": self._trial_review_action_catalog(
                hard_stop_active=bool(hard_stop.get("active")),
                scaling_readiness=scaling_readiness,
                runtime_constraints=runtime_constraints,
            ),
            "latest_action": latest_action,
            "recent_actions": list(history_preview.get("actions") or []),
            "trial_guard_status": trial_guard.get("status"),
        }

    def _build_trial_review_summary(
        self,
        *,
        segment_limit: int,
        window_days: int,
        period_count: int,
    ) -> dict[str, Any]:
        forward_validation = self.forward_validation_report(
            window_days=window_days,
            period_count=period_count,
        )
        scaling = self.scaling_readiness_report(
            window_days=window_days,
            period_count=period_count,
            forward_validation=forward_validation,
        )
        segments = self.strategy_segment_report(limit=segment_limit)
        recovery = self.recovery_view()
        blocker_rows = [item for item in self.blockers() if not item.get("submit_only")]
        latest_review = self._latest_trial_review_action()
        latest_period = (forward_validation.get("periods") or [None])[0] or {}
        segment_rows = list(segments.get("segments") or [])
        strongest_segment = max(
            segment_rows,
            key=lambda item: self._to_decimal(item.get("net_realized_pnl")) or Decimal("-999999999"),
            default=None,
        )
        weakest_segment = min(
            segment_rows,
            key=lambda item: self._to_decimal(item.get("net_realized_pnl")) or Decimal("999999999"),
            default=None,
        )
        scaling_readiness = str(scaling.get("readiness") or "continue_small_capital")
        high_slippage_count = int(latest_period.get("high_slippage_count") or 0)
        slow_submit_to_fill_count = int(latest_period.get("slow_submit_to_fill_count") or 0)
        trial_guard = scaling.get("trial_guard") or self.trial_guard()
        hard_stop = scaling.get("trial_guard_hard_stop") or self._trial_guard_hard_stop_payload(trial_guard)
        runtime_constraints = scaling.get("runtime_constraints") or {}
        action_items = self._trial_review_action_items(
            scaling_readiness=scaling_readiness,
            high_slippage_count=high_slippage_count,
            slow_submit_to_fill_count=slow_submit_to_fill_count,
            trial_guard=trial_guard,
            recovery=recovery,
            blocker_rows=blocker_rows,
        )
        return {
            "generated_at": utc_now(),
            "summary": {
                "readiness": scaling_readiness,
                "headline": scaling.get("summary") or forward_validation.get("summary", {}).get("summary"),
                "closed_fill_count": latest_period.get("closed_fill_count"),
                "net_realized_pnl": latest_period.get("net_realized_pnl"),
                "funding_fee_net_pnl": latest_period.get("funding_fee_net_pnl"),
                "combined_net_realized_pnl": latest_period.get("combined_net_realized_pnl"),
                "win_rate": latest_period.get("win_rate"),
                "fee_to_notional_ratio": latest_period.get("fee_to_notional_ratio"),
                "high_slippage_count": high_slippage_count,
                "slow_submit_to_fill_count": slow_submit_to_fill_count,
            },
            "recommendation": {
                "readiness": scaling_readiness,
                "reasons": scaling.get("reasons", []),
                "action_items": action_items,
            },
            "sections": {
                "forward_validation": {
                    "summary": forward_validation.get("summary"),
                    "periods": forward_validation.get("periods"),
                },
                "scaling_readiness": scaling,
                "trial_guard_hard_stop": hard_stop,
                "runtime_constraints": runtime_constraints,
                "workbench": self._trial_review_workbench_payload(
                    scaling_readiness=scaling_readiness,
                    scaling=scaling,
                    trial_guard=trial_guard,
                    hard_stop=hard_stop,
                    runtime_constraints=runtime_constraints,
                    action_items=action_items,
                ),
                "guarded_live_run_packet": {
                    "status": self.guarded_live_run_packet().get("status"),
                    "summary": self.guarded_live_run_packet().get("summary"),
                    "summary_metrics": self.guarded_live_run_packet().get("summary_metrics"),
                },
                "strategy_segments": {
                    "group_by": segments.get("group_by"),
                    "strongest_segment": strongest_segment,
                    "weakest_segment": weakest_segment,
                },
            },
            "latest_review": latest_review,
            "truth_source": "aggregated_operator_reports_summary",
        }

    def _build_trial_review_details(
        self,
        *,
        profitability_limit: int,
        anomaly_limit: int,
        segment_limit: int,
        window_days: int,
        period_count: int,
    ) -> dict[str, Any]:
        profitability = self.profitability_overview(limit=profitability_limit)
        lifecycle_profitability = self.position_lifecycle_profitability(limit=profitability_limit)
        anomalies = self.execution_anomaly_report(limit=anomaly_limit)
        segments = self.strategy_segment_report(limit=segment_limit)
        forward_validation = self.forward_validation_report(
            window_days=window_days,
            period_count=period_count,
        )
        scaling = self.scaling_readiness_report(
            window_days=window_days,
            period_count=period_count,
            forward_validation=forward_validation,
        )
        trial_guard = self.trial_guard()
        recovery = self.recovery_view()
        blocker_rows = [item for item in self.blockers() if not item.get("submit_only")]
        latest_review = self._latest_trial_review_action()
        scaling_readiness = str(scaling.get("readiness") or "continue_small_capital")
        hard_stop = scaling.get("trial_guard_hard_stop") or self._trial_guard_hard_stop_payload(trial_guard)
        runtime_constraints = scaling.get("runtime_constraints") or {}
        action_items = self._trial_review_action_items(
            scaling_readiness=scaling_readiness,
            high_slippage_count=int(((forward_validation.get("periods") or [None])[0] or {}).get("high_slippage_count") or 0),
            slow_submit_to_fill_count=int(((forward_validation.get("periods") or [None])[0] or {}).get("slow_submit_to_fill_count") or 0),
            trial_guard=trial_guard,
            recovery=recovery,
            blocker_rows=blocker_rows,
        )
        return {
            "generated_at": utc_now(),
            "sections": {
                "profitability": profitability,
                "position_lifecycle_profitability": lifecycle_profitability,
                "strategy_attribution": self.strategy_attribution_report(limit=profitability_limit),
                "execution_anomalies": anomalies,
                "strategy_segments": segments,
                "forward_validation": forward_validation,
                "scaling_readiness": scaling,
                "trial_guard": trial_guard,
                "trial_guard_hard_stop": hard_stop,
                "runtime_constraints": runtime_constraints,
                "workbench": self._trial_review_workbench_payload(
                    scaling_readiness=scaling_readiness,
                    scaling=scaling,
                    trial_guard=trial_guard,
                    hard_stop=hard_stop,
                    runtime_constraints=runtime_constraints,
                    action_items=action_items,
                ),
                "margin_buffer_overview": self.margin_buffer_risk(),
                "guarded_live_preflight": self.guarded_live_preflight(),
                "guarded_live_run_packet": self.guarded_live_run_packet(),
                "recovery": recovery,
                "active_blockers": blocker_rows,
            },
            "latest_review": latest_review,
            "truth_source": "aggregated_operator_reports_details",
        }

    def _build_trial_review_packet(
        self,
        *,
        profitability_limit: int,
        anomaly_limit: int,
        segment_limit: int,
        window_days: int,
        period_count: int,
    ) -> dict[str, Any]:
        summary = self._build_trial_review_summary(
            segment_limit=segment_limit,
            window_days=window_days,
            period_count=period_count,
        )
        details = self._build_trial_review_details(
            profitability_limit=profitability_limit,
            anomaly_limit=anomaly_limit,
            segment_limit=segment_limit,
            window_days=window_days,
            period_count=period_count,
        )
        summary_sections = summary.get("sections") or {}
        detail_sections = details.get("sections") or {}
        return {
            "generated_at": summary.get("generated_at") or details.get("generated_at") or utc_now(),
            "summary": summary.get("summary") or {},
            "recommendation": summary.get("recommendation") or {},
            "sections": {
                **detail_sections,
                "strategy_segments": summary_sections.get("strategy_segments") or detail_sections.get("strategy_segments"),
                "forward_validation": summary_sections.get("forward_validation") or detail_sections.get("forward_validation"),
                "scaling_readiness": summary_sections.get("scaling_readiness") or detail_sections.get("scaling_readiness"),
            },
            "latest_review": summary.get("latest_review") or details.get("latest_review"),
            "truth_source": "aggregated_operator_reports",
        }

    def trial_review_history(self, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        return self.report_queries.trial_review_history(limit=limit, offset=offset)

    def record_trial_review_action(
        self,
        *,
        action_type: str,
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None = None,
        auth_source: AuthSource = "anonymous",
        profitability_limit: int = 100,
        anomaly_limit: int = 100,
        segment_limit: int = 100,
        window_days: int = 7,
        period_count: int = 4,
    ) -> dict[str, Any]:
        if action_type == "review_snapshot":
            return self.record_trial_review_snapshot(
                reason=reason,
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
                profitability_limit=profitability_limit,
                anomaly_limit=anomaly_limit,
                segment_limit=segment_limit,
                window_days=window_days,
                period_count=period_count,
            )
        if action_type == "reset_trial_guard":
            return self.record_trial_guard_manual_reset(
                reason=reason,
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
            )
        return self.record_capital_scale_review(
            verdict=action_type,
            reason=reason,
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
            window_days=window_days,
            period_count=period_count,
        )

    def record_trial_guard_manual_reset(
        self,
        *,
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None = None,
        auth_source: AuthSource = "anonymous",
    ) -> dict[str, Any]:
        service = getattr(self.runtime, "trial_guard_service", None)
        if service is None or not hasattr(service, "manual_reset") or not hasattr(service, "snapshot"):
            raise ValueError("trial_guard_not_configured")
        before = service.snapshot()
        if str(before.get("status") or "").lower() != "breached":
            raise ValueError("trial_guard_reset_not_required")
        recovery_before = self.recovery_view()["recovery_state"]
        effective_after = utc_now()
        preview_reset = getattr(service, "preview_manual_reset", None)
        after_preview = (
            preview_reset(effective_after=effective_after)
            if callable(preview_reset)
            else {
                "status": "warming_up",
                "hard_stop": {"active": False},
                "breaches": [],
            }
        )
        action = OperatorActionRecord(
            action="trial_guard_manual_reset",
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
            reason=reason,
            status="reset_recorded",
            recovery_state_before=recovery_before,
            recovery_state_after=None,
            details={
                "trial_review_action_type": "reset_trial_guard",
                "summary": "人工重置试盘守护，新的试盘样本窗口会从本次操作后重新开始。",
                "effective_after": effective_after,
                "product_type": self.state_scope.product_type,
                "margin_mode": self.state_scope.margin_mode,
                "allowed_symbols": list(self.state_scope.allowed_symbols),
                "trial_guard_status_before": before.get("status"),
                "trial_guard_status_after": after_preview.get("status"),
                "trial_guard_hard_stop_before": before.get("hard_stop"),
                "trial_guard_hard_stop_after": after_preview.get("hard_stop"),
                "breaches_before": list(before.get("breaches") or []),
                "breaches_after": list(after_preview.get("breaches") or []),
                "recorded_at": utc_now(),
            },
        )
        envelope = self._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="trial_guard",
            payload_model=action,
        )
        after = service.manual_reset(effective_after=effective_after)
        self._invalidate_cache()
        recovery_after = self.recovery_view()["recovery_state"]
        payload = action.model_dump(mode="json")
        payload["recovery_state_after"] = recovery_after
        payload["details"]["trial_guard_status_after"] = after.get("status")
        payload["details"]["trial_guard_hard_stop_after"] = after.get("hard_stop")
        payload["details"]["breaches_after"] = list(after.get("breaches") or [])
        payload["_event_id"] = envelope.event_id
        payload["_topic"] = envelope.topic
        return payload

    def record_capital_scale_review(
        self,
        *,
        verdict: str,
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None = None,
        auth_source: AuthSource = "anonymous",
        window_days: int = 7,
        period_count: int = 4,
    ) -> dict[str, Any]:
        allowed_verdicts = {
            "approve_scale_up",
            "continue_small_capital",
            "shrink_trial",
            "pause_trial",
        }
        if verdict not in allowed_verdicts:
            raise ValueError(f"unsupported_scaling_review_verdict:{verdict}")
        report = self.scaling_readiness_report(window_days=window_days, period_count=period_count)
        action = OperatorActionRecord(
            action="capital_scale_review",
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
            reason=reason,
            status="review_recorded",
            recovery_state_before=report.get("recovery", {}).get("recovery_state"),
            recovery_state_after=report.get("recovery", {}).get("recovery_state"),
            details={
                "trial_review_action_type": verdict,
                "selected_verdict": verdict,
                "recommended_readiness": report.get("readiness"),
                "summary": report.get("summary"),
                "reasons": report.get("reasons", []),
                "requirements": report.get("requirements", {}),
                "latest_forward_validation": report.get("latest_forward_validation"),
                "trial_guard_status": report.get("trial_guard", {}).get("status"),
                "trial_guard_hard_stop": report.get("trial_guard_hard_stop"),
                "runtime_constraints": report.get("runtime_constraints"),
                "recorded_at": utc_now(),
            },
        )
        envelope = self._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="capital_scale",
            payload_model=action,
        )
        payload = action.model_dump(mode="json")
        payload["_event_id"] = envelope.event_id
        payload["_topic"] = envelope.topic
        return payload

    def record_trial_review_snapshot(
        self,
        *,
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None = None,
        auth_source: AuthSource = "anonymous",
        profitability_limit: int = 100,
        anomaly_limit: int = 100,
        segment_limit: int = 100,
        window_days: int = 7,
        period_count: int = 4,
    ) -> dict[str, Any]:
        packet = self.trial_review_packet(
            profitability_limit=profitability_limit,
            anomaly_limit=anomaly_limit,
            segment_limit=segment_limit,
            window_days=window_days,
            period_count=period_count,
        )
        action = OperatorActionRecord(
            action="trial_review_snapshot",
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
            reason=reason,
            status="snapshot_recorded",
            recovery_state_before=packet.get("sections", {}).get("recovery", {}).get("recovery_state"),
            recovery_state_after=packet.get("sections", {}).get("recovery", {}).get("recovery_state"),
            details={
                "trial_review_action_type": "review_snapshot",
                "summary": packet.get("summary"),
                "recommendation": packet.get("recommendation"),
                "latest_forward_validation": packet.get("sections", {}).get("forward_validation", {}).get("summary"),
                "scaling_readiness": packet.get("sections", {}).get("scaling_readiness", {}).get("readiness"),
                "trial_guard_hard_stop": packet.get("sections", {}).get("trial_guard_hard_stop"),
                "runtime_constraints": packet.get("sections", {}).get("runtime_constraints"),
                "recorded_at": utc_now(),
            },
        )
        envelope = self._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="trial_review",
            payload_model=action,
        )
        payload = action.model_dump(mode="json")
        payload["_event_id"] = envelope.event_id
        payload["_topic"] = envelope.topic
        return payload

    def strategy_segment_report(
        self,
        *,
        limit: int = 200,
        group_by: tuple[str, ...] = ("symbol", "market_regime", "side", "execution_action"),
    ) -> dict[str, Any]:
        return self.strategy_queries.strategy_segment_report(limit=limit, group_by=group_by)

    def _open_strategy_lot_rows(self) -> list[dict[str, Any]]:
        cache_key = f"open_strategy_lot_rows:{self._scope_cache_fragment()}"
        return self._cached(
            cache_key,
            self._build_open_strategy_lot_rows,
        )

    def _build_open_strategy_lot_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]]
        if getattr(self.runtime, "position_lot_repo", None) is not None:
            rows = list(
                self.runtime.position_lot_repo.lots_for_scope(
                    symbol=None,
                    product_type=self.state_scope.product_type,
                    margin_mode=self.state_scope.margin_mode,
                    open_only=True,
                )
            )
        else:
            snapshot = LotBasedProjectionBuilder().rebuild_lot_book(fills=self._scoped_fills())
            rows = [dict(item) for item in snapshot.lots if str(item.get("status") or "") == "OPEN"]
        return [
            row
            for row in rows
            if row.get("symbol") in {None, ""} or self.state_scope.symbol_allowed(str(row.get("symbol")))
        ]

    def _strategy_sleeve_inventory_summary(self) -> list[dict[str, Any]]:
        family_by_sleeve = {
            item.get("sleeve_id"): item.get("family")
            for item in (
                sleeve.model_dump(mode="json")
                for sleeve in getattr(self.runtime, "strategy_sleeve_repo", None).list_sleeves()
            )
        } if getattr(self.runtime, "strategy_sleeve_repo", None) is not None and hasattr(self.runtime.strategy_sleeve_repo, "list_sleeves") else {}
        grouped: dict[str, dict[str, Any]] = {}
        for row in self._open_strategy_lot_rows():
            sleeve_id = str(row.get("strategy_sleeve_id") or "unassigned")
            bucket = grouped.setdefault(
                sleeve_id,
                {
                    "strategy_sleeve_id": None if sleeve_id == "unassigned" else sleeve_id,
                    "strategy_family": family_by_sleeve.get(sleeve_id),
                    "open_lot_count": 0,
                    "net_inventory_qty": Decimal("0"),
                    "gross_inventory_qty": Decimal("0"),
                    "inventory_notional": Decimal("0"),
                    "symbols": set(),
                    "allocation_ids": set(),
                },
            )
            qty = self._to_decimal(row.get("signed_quantity_open")) or Decimal("0")
            entry_price = self._to_decimal(row.get("entry_price")) or Decimal("0")
            bucket["open_lot_count"] += 1
            bucket["net_inventory_qty"] += qty
            bucket["gross_inventory_qty"] += abs(qty)
            bucket["inventory_notional"] += abs(qty * entry_price)
            symbol = str(row.get("symbol") or "").strip()
            if symbol:
                bucket["symbols"].add(symbol)
            allocation_id = str(row.get("allocation_id") or "").strip()
            if allocation_id:
                bucket["allocation_ids"].add(allocation_id)
        payload = []
        for bucket in grouped.values():
            payload.append(
                {
                    "strategy_sleeve_id": bucket["strategy_sleeve_id"],
                    "strategy_family": bucket["strategy_family"],
                    "open_lot_count": bucket["open_lot_count"],
                    "net_inventory_qty": bucket["net_inventory_qty"],
                    "gross_inventory_qty": bucket["gross_inventory_qty"],
                    "inventory_notional": bucket["inventory_notional"],
                    "symbols": sorted(bucket["symbols"]),
                    "allocation_ids": sorted(bucket["allocation_ids"]),
                }
            )
        payload.sort(
            key=lambda item: (
                self._to_decimal(item.get("inventory_notional")) or Decimal("0"),
                self._to_decimal(item.get("gross_inventory_qty")) or Decimal("0"),
                str(item.get("strategy_sleeve_id") or ""),
            ),
            reverse=True,
        )
        return payload

    def _strategy_pnl_bucket_rows(
        self,
        *,
        records: list[SleevePnLRecord],
        key_name: str,
        fallback: str,
    ) -> list[dict[str, Any]]:
        buckets: dict[str, dict[str, Any]] = {}
        for record in records:
            if key_name == "strategy_sleeve_id":
                bucket_key = str(record.strategy_sleeve_id or fallback)
            elif key_name == "allocation_id":
                bucket_key = str(record.allocation_id or fallback)
            elif key_name == "strategy_bundle_id":
                bucket_key = str(record.strategy_bundle_id or fallback)
            elif key_name == "attribution_type":
                bucket_key = str(record.attribution_type or fallback)
            elif key_name == "strategy_family":
                bucket_key = str(record.strategy_family or fallback)
            else:
                bucket_key = str(getattr(record, key_name, None) or fallback)
            bucket = buckets.setdefault(
                bucket_key,
                {
                    key_name: bucket_key,
                    "record_count": 0,
                    "fill_event_count": 0,
                    "funding_fee_event_count": 0,
                    "realized_pnl": Decimal("0"),
                    "fee_amount": Decimal("0"),
                    "funding_fee_amount": Decimal("0"),
                    "combined_net_realized_pnl": Decimal("0"),
                    "inventory_move_qty": Decimal("0"),
                    "symbols": set(),
                    "families": set(),
                },
            )
            bucket["record_count"] += 1
            if record.event_type == "fill_realization":
                bucket["fill_event_count"] += 1
            elif record.event_type == "funding_fee":
                bucket["funding_fee_event_count"] += 1
            bucket["realized_pnl"] += self._to_decimal(record.realized_pnl) or Decimal("0")
            bucket["fee_amount"] += self._to_decimal(record.fee_amount) or Decimal("0")
            bucket["funding_fee_amount"] += self._to_decimal(record.funding_fee_amount) or Decimal("0")
            bucket["combined_net_realized_pnl"] += (
                (self._to_decimal(record.realized_pnl) or Decimal("0"))
                + (self._to_decimal(record.funding_fee_amount) or Decimal("0"))
            )
            bucket["inventory_move_qty"] += self._to_decimal(record.inventory_move_qty) or Decimal("0")
            if record.symbol:
                bucket["symbols"].add(record.symbol)
            if record.strategy_family:
                bucket["families"].add(record.strategy_family)
        payload = []
        for bucket in buckets.values():
            payload.append(
                {
                    **{key_name: bucket[key_name]},
                    "record_count": bucket["record_count"],
                    "fill_event_count": bucket["fill_event_count"],
                    "funding_fee_event_count": bucket["funding_fee_event_count"],
                    "realized_pnl": bucket["realized_pnl"],
                    "fee_amount": bucket["fee_amount"],
                    "funding_fee_amount": bucket["funding_fee_amount"],
                    "combined_net_realized_pnl": bucket["combined_net_realized_pnl"],
                    "inventory_move_qty": bucket["inventory_move_qty"],
                    "symbols": sorted(bucket["symbols"]),
                    "families": sorted(bucket["families"]),
                }
            )
        payload.sort(
            key=lambda item: (
                self._to_decimal(item.get("combined_net_realized_pnl")) or Decimal("0"),
                item.get("record_count") or 0,
                str(item.get(key_name) or ""),
            ),
            reverse=True,
        )
        return payload

    def strategy_attribution_report(self, *, limit: int = 200) -> dict[str, Any]:
        return self.strategy_queries.strategy_attribution_report(limit=limit)

    def execution_anomaly_report(self, *, limit: int = 100) -> dict[str, Any]:
        return self.report_queries.execution_anomaly_report(limit=limit)

    def execution_errors(self) -> dict[str, Any]:
        persisted = [
            item.payload
            for item in self.runtime.event_store.recent_by_topic(topics.EXECUTION_ERROR_SUMMARIES, limit=20)
            if self._is_current_runtime_timestamp(item.payload.get("observed_at"))
        ]
        if persisted:
            return {"errors": persisted}
        errors = []
        for order in order_states_for_scope(
            self.runtime.execution_repo,
            self.state_scope,
            statuses=("FAILED", "REJECTED", "BLOCKED"),
            limit=20,
        ):
            if not self._is_current_runtime_timestamp(order.last_update_ts or order.created_at):
                continue
            errors.append(
                {
                    "timestamp": order.last_update_ts or order.created_at,
                    "subsystem": "execution_engine",
                    "severity": "error" if order.status == "FAILED" else "warning",
                    "message": order.execution_error or order.cancel_reason or order.status,
                    "decision_id": order.decision_id,
                    "order_id": order.client_order_id,
                    "status": order.status,
                }
            )
        return {"errors": errors}

    def reconciliation_latest(self) -> dict[str, Any]:
        cache_key = f"reconciliation_latest:{self._scope_cache_fragment()}"
        return self._cached_ttl(cache_key, 15, self.reconciliation_system_queries.reconciliation_latest)

    def reconciliation_recent(self, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        return self.reconciliation_system_queries.reconciliation_recent(limit=limit, offset=offset)

    def reconciliation_mismatches(self, *, limit: int = 20) -> dict[str, Any]:
        return self.reconciliation_system_queries.reconciliation_mismatches(limit=limit)

    def reconciliation_detail(self, reconciliation_id: str) -> dict[str, Any]:
        return self.reconciliation_system_queries.reconciliation_detail(reconciliation_id)

    def audit_latest(self) -> dict[str, Any]:
        return self.audit_replay_queries.audit_latest()

    def audit_detail(self, decision_id: str) -> dict[str, Any]:
        return self.audit_replay_queries.audit_detail(decision_id)

    def replay_status(self) -> dict[str, Any]:
        cache_key = f"replay_status:{self._scope_cache_fragment()}"
        return self._cached_ttl(cache_key, 30, self.audit_replay_queries.replay_status)

    def replay_validate(self, *, decision_id: str) -> dict[str, Any]:
        return self.audit_replay_queries.replay_validate(decision_id=decision_id)

    def replay_recent_validations(self, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        return self.audit_replay_queries.replay_recent_validations(limit=limit, offset=offset)

    async def validate_reconciliation(
        self,
        *,
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None = None,
        auth_source: AuthSource = "anonymous",
    ) -> dict[str, Any]:
        return await self.reconciliation_system_queries.validate_reconciliation(
            reason=reason,
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
        )

    async def cancel_order(
        self,
        *,
        client_order_id: str,
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None = None,
        auth_source: AuthSource = "anonymous",
    ) -> dict[str, Any]:
        recovery_before = self.recovery_view()["recovery_state"]
        state = await self.runtime.order_manager.cancel_order(client_order_id)
        self._invalidate_cache()
        self._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="system",
            payload_model=OperatorActionRecord(
                action="cancel_order",
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
                reason=reason,
                status=state.status,
                decision_id=state.decision_id,
                order_id=state.client_order_id,
                recovery_state_before=recovery_before,
                recovery_state_after=self.recovery_view()["recovery_state"],
                details={"final_order_status": state.status},
            ),
        )
        return {"order": state.model_dump(mode="json")}

    async def resolve_stuck_submission(
        self,
        *,
        client_order_id: str,
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None = None,
        auth_source: AuthSource = "anonymous",
    ) -> dict[str, Any]:
        return await self.reconciliation_system_queries.resolve_stuck_submission(
            client_order_id=client_order_id,
            reason=reason,
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
        )

    async def rebaseline(
        self,
        *,
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None = None,
        auth_source: AuthSource = "anonymous",
    ) -> dict[str, Any]:
        return await self.reconciliation_system_queries.rebaseline(
            reason=reason,
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
        )

    def halt(
        self,
        *,
        reason: str,
        actor_role: OperatorRole = "anonymous",
        actor_identity: str | None = None,
        auth_source: AuthSource = "anonymous",
    ) -> dict[str, Any]:
        return self.reconciliation_system_queries.halt(
            reason=reason,
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
        )

    async def resume(
        self,
        *,
        reason: str,
        actor_role: OperatorRole = "anonymous",
        actor_identity: str | None = None,
        auth_source: AuthSource = "anonymous",
    ) -> dict[str, Any]:
        return await self.reconciliation_system_queries.resume(
            reason=reason,
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
        )

    async def refresh_exchange_state(
        self,
        *,
        blocker: str | None,
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None = None,
        auth_source: AuthSource = "anonymous",
    ) -> dict[str, Any]:
        recovery_before = self.recovery_view()["recovery_state"]
        blockers_before = self.blockers()
        market_before = self.runtime.market_gateway.status()
        account_before = self.runtime.account_service.status()
        max_attempts = max(int(self.runtime.settings.operator_exchange_refresh_max_attempts), 1)
        retry_delay_seconds = max(float(self.runtime.settings.operator_exchange_refresh_retry_delay_seconds), 0.0)
        errors: list[dict[str, Any]] = []
        market_refresh_completed = False
        account_refresh_completed = False
        attempts_executed = 0

        for attempt in range(1, max_attempts + 1):
            attempts_executed = attempt
            try:
                await self._refresh_market_snapshot_for_operator_resolution()
                market_refresh_completed = True
            except Exception as exc:
                errors.append(
                    {
                        "attempt": attempt,
                        "scope": "market_data",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
            try:
                await self._refresh_account_state_for_operator_resolution()
                account_refresh_completed = True
            except Exception as exc:
                errors.append(
                    {
                        "attempt": attempt,
                        "scope": "account_state",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
            self._invalidate_cache()
            if blocker and not self.blocker_control_service.has_active_blocker(blocker):
                break
            if attempt >= max_attempts:
                break
            if retry_delay_seconds > 0:
                await asyncio.sleep(retry_delay_seconds)

        if not market_refresh_completed and not account_refresh_completed and errors:
            raise ValueError("exchange_state_refresh_failed")

        self._invalidate_cache()
        recovery_after = self.recovery_view()["recovery_state"]
        blockers_after = self.blockers()
        market_after = self.runtime.market_gateway.status()
        account_after = self.runtime.account_service.status()
        blocker_cleared = False if blocker is None else not self.blocker_control_service.has_active_blocker(blocker)
        auto_resume: dict[str, Any] | None = None
        if (
            blocker == "derivatives_risk_snapshot_missing_auto_halt"
            and blocker_cleared
            and self.runtime.kill_switch.halted
            and self.runtime.kill_switch.status().get("reason") == "derivatives_live_risk_auto_halt"
        ):
            auto_resume = await self.reconciliation_system_queries.resume(
                reason="operator_refresh_exchange_state_for_risk_snapshot_recovered",
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
            )
            self._invalidate_cache()
            recovery_after = self.recovery_view()["recovery_state"]
            blockers_after = self.blockers()
            market_after = self.runtime.market_gateway.status()
            account_after = self.runtime.account_service.status()
            blocker_cleared = not self.blocker_control_service.has_active_blocker(blocker)
        if blocker and blocker_cleared:
            if auto_resume is not None and auto_resume.get("status") in {"resumed", "already_resumed"}:
                message = "已刷新交易所状态，风险快照阻断已解除，系统已恢复自动运行。"
            elif auto_resume is not None and auto_resume.get("status") == "resume_blocked":
                message = "已刷新交易所状态，风险快照阻断已解除，但恢复仍受其他条件限制。"
            else:
                message = "已刷新交易所状态，当前阻断已解除。"
        elif blocker:
            message = "已刷新交易所状态，但当前阻断仍然存在。"
        else:
            message = "已刷新交易所状态。"

        self._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="system",
            payload_model=OperatorActionRecord(
                action="refresh_exchange_state",
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
                reason=reason,
                status="completed",
                recovery_state_before=recovery_before,
                recovery_state_after=recovery_after,
                details={
                    "target_blocker": blocker,
                    "attempts_executed": attempts_executed,
                    "max_attempts": max_attempts,
                    "retry_delay_seconds": retry_delay_seconds,
                    "market_refresh_completed": market_refresh_completed,
                    "account_refresh_completed": account_refresh_completed,
                    "blocker_cleared": blocker_cleared,
                    "errors": errors,
                    "market_before": market_before,
                    "market_after": market_after,
                    "account_before": account_before,
                    "account_after": account_after,
                    "blockers_before": blockers_before,
                    "blockers_after": blockers_after,
                    "auto_resume": auto_resume,
                },
            ),
        )
        self._persist_blocker_snapshot(
            source="refresh_exchange_state",
            runtime_state=self.system_health()["runtime_state"],
            mode_snapshot=self.system_mode(),
            blockers=self.blockers(),
        )
        return {
            "status": "completed",
            "message": message,
            "recovery": self.recovery_view(),
            "blockers": blockers_after,
            "details": {
                "target_blocker": blocker,
                "attempts_executed": attempts_executed,
                "max_attempts": max_attempts,
                "retry_delay_seconds": retry_delay_seconds,
                "market_refresh_completed": market_refresh_completed,
                "account_refresh_completed": account_refresh_completed,
                "blocker_cleared": blocker_cleared,
                "errors": errors,
                "market_before": market_before,
                "market_after": market_after,
                "account_before": account_before,
                "account_after": account_after,
                "auto_resume": auto_resume,
            },
        }

    def ai_review_restore(
        self,
        *,
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None = None,
        auth_source: AuthSource = "anonymous",
    ) -> dict[str, Any]:
        recovery_before = self.recovery_view()["recovery_state"]
        ai_before = dict(self.runtime.ai_service.status())
        ai_after = self.runtime.ai_service.resolve_outcome_review_restore_ai()
        self._invalidate_cache()
        self.runtime.recovery_status = self.recovery_posture.finalize_status()
        self._invalidate_cache()
        recovery_after = self.recovery_view()["recovery_state"]
        self._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="system",
            payload_model=OperatorActionRecord(
                action="ai_review_restore",
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
                reason=reason,
                status="completed",
                recovery_state_before=recovery_before,
                recovery_state_after=recovery_after,
                details={
                    "ai_before": ai_before,
                    "ai_after": ai_after,
                },
            ),
        )
        self._persist_blocker_snapshot(
            source="ai_review_restore",
            runtime_state=self.system_health()["runtime_state"],
            mode_snapshot=self.system_mode(),
            blockers=self.blockers(),
        )
        return {
            "status": "completed",
            "recovery": self.recovery_view(),
            "ai_runtime": self.ai_runtime(),
        }

    def set_ai_operating_mode(
        self,
        *,
        mode: str,
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None = None,
        auth_source: AuthSource = "anonymous",
    ) -> dict[str, Any]:
        recovery_before = self.recovery_view()["recovery_state"]
        ai_before = dict(self.runtime.ai_service.status())
        configured_mode = normalize_ai_operating_mode(self.runtime.settings.ai_operating_mode)
        requested_mode = normalize_ai_operating_mode(mode)
        configured_display_mode = (
            "ai_decision_maker"
            if configured_mode == "ai_decision_maker_with_profile_control"
            else configured_mode
        )
        requested_display_mode = (
            "ai_decision_maker"
            if requested_mode == "ai_decision_maker_with_profile_control"
            else requested_mode
        )
        if requested_display_mode == configured_display_mode:
            ai_after = self.runtime.ai_service.clear_manual_operating_mode_override()
        else:
            ai_after = self.runtime.ai_service.set_manual_operating_mode_override(
                mode=requested_mode,
                freeze_seconds=0.0,
            )
        self._invalidate_cache()
        self.runtime.recovery_status = self.recovery_posture.finalize_status()
        self._invalidate_cache()
        recovery_after = self.recovery_view()["recovery_state"]
        self._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="system",
            payload_model=OperatorActionRecord(
                action="ai_operating_mode_select",
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
                reason=reason,
                status="completed",
                recovery_state_before=recovery_before,
                recovery_state_after=recovery_after,
                details={
                    "ai_before": ai_before,
                    "ai_after": ai_after,
                    "configured_mode": configured_mode,
                    "requested_mode": requested_mode,
                    "configured_display_mode": configured_display_mode,
                    "requested_display_mode": requested_display_mode,
                    "effective_mode": ai_after.get("effective_operating_mode"),
                },
            ),
        )
        self._persist_blocker_snapshot(
            source="ai_operating_mode_select",
            runtime_state=self.system_health()["runtime_state"],
            mode_snapshot=self.system_mode(),
            blockers=self.blockers(),
        )
        return {
            "status": "completed",
            "recovery": self.recovery_view(),
            "ai_runtime": self.ai_runtime(),
        }

    def ai_review_degrade_to_baseline(
        self,
        *,
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None = None,
        auth_source: AuthSource = "anonymous",
    ) -> dict[str, Any]:
        recovery_before = self.recovery_view()["recovery_state"]
        ai_before = dict(self.runtime.ai_service.status())
        ai_after = self.runtime.ai_service.resolve_outcome_review_degrade_to_baseline()
        self._invalidate_cache()
        self.runtime.recovery_status = self.recovery_posture.finalize_status()
        self._invalidate_cache()
        recovery_after = self.recovery_view()["recovery_state"]
        self._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="system",
            payload_model=OperatorActionRecord(
                action="ai_review_degrade_to_baseline",
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
                reason=reason,
                status="completed",
                recovery_state_before=recovery_before,
                recovery_state_after=recovery_after,
                details={
                    "ai_before": ai_before,
                    "ai_after": ai_after,
                    "target_mode": "baseline_only",
                },
            ),
        )
        self._persist_blocker_snapshot(
            source="ai_review_degrade_to_baseline",
            runtime_state=self.system_health()["runtime_state"],
            mode_snapshot=self.system_mode(),
            blockers=self.blockers(),
        )
        return {
            "status": "completed",
            "recovery": self.recovery_view(),
            "ai_runtime": self.ai_runtime(),
        }

    def _latest_topic_summary(self, topic: str) -> dict[str, Any]:
        envelope = self.runtime.event_store.latest(topic)
        if envelope is None:
            return {"decision_id": None, "payload": None}
        decision_id = envelope.payload.get("decision_id")
        return {
            "decision_id": decision_id if isinstance(decision_id, str) else None,
            "payload": self.payload(envelope),
        }

    def _recent_topic_summaries(self, topic: str, *, limit: int) -> list[dict[str, Any]]:
        return [self.payload(item) for item in reversed(self.runtime.event_store.recent_by_topic(topic, limit=limit))]

    @staticmethod
    def _reconciliation_mismatch_summary(report) -> dict[str, Any] | None:
        if report is None:
            return None
        return {
            "reconciliation_id": report.reconciliation_id,
            "severity": report.severity,
            "recovery_classification": report.recovery_classification,
            "review_required": report.review_required,
            "halt_required": report.halt_required,
            "only_reduce_required": report.only_reduce_required,
            "only_reduce_reasons": report.only_reduce_reasons,
            "structural_review_required": report.structural_review_required,
            "financial_review_required": report.financial_review_required,
            "observational_only": report.observational_only,
            "finding_summary": report.finding_summary,
            "findings": [item.model_dump(mode="json") for item in report.findings],
            "baseline_generation_id": report.baseline_generation_id,
            "exchange_ack_watermark_id": report.exchange_ack_watermark_id,
            "unknown_state_details": report.unknown_state_details,
            "mismatch_categories": report.mismatch_categories,
            "mismatch_reasons": report.mismatch_reasons,
            "safety_impacts": report.safety_impacts,
            "recommended_operator_action": report.recommended_operator_action,
            "exchange_comparison_enabled": report.exchange_comparison_enabled,
        }

    def _exchange_bills_summary(self) -> dict[str, Any] | None:
        report = self._latest_scoped_reconciliation()
        if report is not None and report.exchange_bills_summary:
            return report.exchange_bills_summary
        summary_getter = getattr(self.runtime.account_service, "recent_bills_summary", None)
        if not callable(summary_getter):
            return None
        summary = summary_getter()
        return summary if isinstance(summary, dict) else None

    def _append_event(self, *, topic: str, key: str, payload_model: Any) -> EventEnvelope:
        envelope = build_envelope(
            topic=topic,
            key=key,
            payload_model=payload_model,
            source_component="operator_api",
        )
        self.runtime.event_store.append(envelope)
        return envelope

    def _scoped_fills_for_order(self, client_order_id: str):
        return [
            item
            for item in self.runtime.execution_repo.fills_for_order(client_order_id)
            if item.product_type == self.state_scope.product_type
            and item.margin_mode == self.state_scope.margin_mode
            and self.state_scope.symbol_allowed(item.symbol)
        ]

    async def _refresh_exchange_snapshot_for_resolution(self):
        if self.runtime.settings.account_backend != "okx" or not self.runtime.settings.account_read_enabled:
            return None
        return await self.runtime.account_service.refresh(force=True)

    async def _refresh_market_snapshot_for_operator_resolution(self):
        refresh = getattr(self.runtime.market_gateway, "refresh_snapshot", None)
        if not callable(refresh):
            return None
        return await refresh()

    async def _refresh_account_state_for_operator_resolution(self):
        refresh = getattr(self.runtime.account_service, "refresh", None)
        if not callable(refresh):
            return None
        try:
            snapshot = await refresh(force=True)
        finally:
            sync_funding = getattr(self.runtime, "_sync_funding_fees_after_refresh", None)
            if callable(sync_funding):
                await sync_funding()
            evaluate_guard = getattr(self.runtime, "_evaluate_derivatives_live_guard_after_refresh", None)
            if callable(evaluate_guard):
                evaluate_guard()
        return snapshot

    def _stuck_submission_resolution(
        self,
        *,
        order: OrderState,
        fills: list[Any] | None = None,
        exchange_snapshot=None,
    ) -> dict[str, Any]:
        local_fills = list(fills or [])
        last_update = order.last_update_ts or order.created_at
        runtime_started_at = self._current_runtime_started_at()
        runtime_restarted_after_order = last_update is not None and last_update < runtime_started_at
        latest_reconciliation = self.runtime.reconciliation_repo.latest_for_scope(scope=self.state_scope)
        exchange_order_present: bool | None = None
        exchange_fill_present: bool | None = None
        private_ws_order_present: bool | None = None
        private_ws_fill_present: bool | None = None
        reason_code: str | None = None

        private_order_lookup = getattr(self.runtime.account_service, "latest_private_order_row", None)
        if callable(private_order_lookup):
            private_ws_order_present = (
                private_order_lookup(symbol=order.symbol, order_id=order.exchange_order_id, client_order_id=order.client_order_id)
                is not None
            )
        private_fill_lookup = getattr(self.runtime.account_service, "latest_private_order_fills", None)
        if callable(private_fill_lookup):
            private_ws_fill_present = bool(
                private_fill_lookup(symbol=order.symbol, order_id=order.exchange_order_id, client_order_id=order.client_order_id)
            )

        if order.venue != "OKX":
            reason_code = "venue_not_exchange_coupled"
        elif order.status not in self._STUCK_SUBMISSION_STATUSES:
            reason_code = "order_not_in_pre_submit_state"
        elif order.exchange_order_id is not None:
            reason_code = "exchange_order_id_present"
        elif local_fills:
            reason_code = "local_fills_present"
        elif not runtime_restarted_after_order:
            reason_code = "order_belongs_to_current_runtime"
        elif private_ws_order_present:
            reason_code = "exchange_order_seen_via_private_ws"
        elif private_ws_fill_present:
            reason_code = "exchange_fill_seen_via_private_ws"
        elif self.runtime.settings.account_backend != "okx" or not self.runtime.settings.account_read_enabled:
            reason_code = "exchange_confirmation_unavailable"
        elif exchange_snapshot is None:
            reason_code = "exchange_snapshot_unavailable"
        else:
            exchange_order_present = any(
                item.client_order_id == order.client_order_id
                or (
                    order.exchange_order_id is not None
                    and item.exchange_order_id == order.exchange_order_id
                )
                for item in exchange_snapshot.open_orders
            )
            exchange_fill_present = any(
                item.client_order_id == order.client_order_id
                or (
                    order.exchange_order_id is not None
                    and item.exchange_order_id == order.exchange_order_id
                )
                for item in exchange_snapshot.fills
            )
            if exchange_order_present:
                reason_code = "exchange_order_still_open"
            elif exchange_fill_present:
                reason_code = "exchange_fill_detected"
            elif latest_reconciliation is not None and (
                latest_reconciliation.halt_required or latest_reconciliation.review_required
            ):
                reason_code = "latest_reconciliation_not_clean"

        eligible = reason_code is None
        summary = (
            "Eligible for operator resolution: the order predates the current runtime, has no exchange order id, and is absent from the latest exchange snapshot."
            if eligible
            else self._stuck_submission_resolution_summary(reason_code)
        )
        return {
            "eligible": eligible,
            "summary": summary,
            "reason_code": reason_code,
            "order_status": order.status,
            "last_local_update_ts": last_update,
            "runtime_started_at": runtime_started_at,
            "runtime_restarted_after_order": runtime_restarted_after_order,
            "local_fill_count": len(local_fills),
            "exchange_snapshot_fetched_at": exchange_snapshot.fetched_at if exchange_snapshot is not None else None,
            "latest_reconciliation_id": (
                latest_reconciliation.reconciliation_id if latest_reconciliation is not None else None
            ),
            "latest_reconciliation_severity": (
                latest_reconciliation.severity if latest_reconciliation is not None else None
            ),
            "exchange_order_present": exchange_order_present,
            "exchange_fill_present": exchange_fill_present,
            "private_ws_order_present": private_ws_order_present,
            "private_ws_fill_present": private_ws_fill_present,
        }

    @staticmethod
    def _stuck_submission_resolution_summary(reason_code: str | None) -> str:
        messages = {
            "venue_not_exchange_coupled": "This order is not exchange-coupled, so stuck submission recovery is not applicable.",
            "order_not_in_pre_submit_state": "Only pre-submit orders can use stuck submission recovery.",
            "exchange_order_id_present": "This order already has an exchange order id. Use normal exchange refresh or cancel flows instead.",
            "local_fills_present": "Local fills already exist for this order, so manual stuck submission resolution is unsafe.",
            "order_belongs_to_current_runtime": "This order belongs to the current runtime and may still be progressing normally.",
            "exchange_order_seen_via_private_ws": "A recent private websocket order confirmation exists for this order, so it must not be force-resolved locally.",
            "exchange_fill_seen_via_private_ws": "A recent private websocket fill confirmation exists for this order, so manual stuck submission resolution is unsafe.",
            "latest_reconciliation_not_clean": "The latest reconciliation still requires review or halt. Resolve that state before force-resolving submissions.",
            "exchange_confirmation_unavailable": "Exchange confirmation is unavailable, so the runtime cannot safely resolve this submission.",
            "exchange_snapshot_unavailable": "No fresh exchange snapshot is available to confirm the order is absent.",
            "exchange_order_still_open": "The order is still visible on the exchange, so it must not be force-resolved locally.",
            "exchange_fill_detected": "Exchange fills exist for this order, so manual stuck submission resolution is unsafe.",
        }
        return messages.get(reason_code, "This order is not eligible for stuck submission resolution.")

    def _update_recovery_status_for_report(self, report) -> None:
        updated_status = self.runtime.recovery_status.model_copy(
            update={
                "latest_reconciliation_id": report.reconciliation_id,
                "latest_reconciliation_severity": report.severity,
                "recovered_reconciliation_available": True,
            }
        )
        self.runtime.recovery_status = self.recovery_posture.finalize_status(
            base_status=updated_status,
            latest_reconciliation=report,
        )

    def _persist_blocker_snapshot(
        self,
        *,
        source: str,
        runtime_state: str,
        mode_snapshot: dict[str, Any],
        blockers: list[dict[str, Any]],
    ) -> None:
        latest = self.runtime.event_store.latest(topics.BLOCKER_SNAPSHOTS)
        candidate = BlockerSnapshotRecord(
            source=source,
            runtime_state=runtime_state,
            operating_state=mode_snapshot["operating_state"],
            mode=mode_snapshot["mode"],
            halted=bool(mode_snapshot["halted"]),
            execution_blocked=bool(mode_snapshot["execution_blocked"]),
            submit_blocked=bool(mode_snapshot["submit_blocked"]),
            blockers=blockers,
        )
        if latest is not None:
            payload = latest.payload
            if (
                payload.get("runtime_state") == candidate.runtime_state
                and payload.get("operating_state") == candidate.operating_state
                and payload.get("halted") == candidate.halted
                and payload.get("execution_blocked") == candidate.execution_blocked
                and payload.get("submit_blocked") == candidate.submit_blocked
                and payload.get("blockers") == candidate.blockers
            ):
                return
        self._append_event(topic=topics.BLOCKER_SNAPSHOTS, key="system", payload_model=candidate)

    @staticmethod
    def _blocker_entry(blocker: str, *, subsystem: str, submit_only: bool | None = None) -> dict[str, Any]:
        submit_only_value = submit_only if submit_only is not None else blocker in {
            "guarded_execution_dry_run",
            "live_submit_disabled",
            "okx_simulated_trading_required",
            "local_demo_no_exchange_submission",
            "real_market_paper_uses_local_paper_execution",
        }
        affects_execution = not submit_only_value
        recommended_action = "Inspect subsystem status and operator logs before resuming execution."
        if blocker == "local_demo_no_exchange_submission":
            recommended_action = "No action required. Local demo mode intentionally never submits exchange orders."
        elif blocker == "real_market_paper_uses_local_paper_execution":
            recommended_action = "No action required. Real-market paper mode intentionally uses local paper fills."
        elif blocker in {"guarded_execution_dry_run", "live_submit_disabled"}:
            recommended_action = "Open real submission only if the current runtime profile, account state, and credentials are all aligned with your intended environment."
        elif blocker == "okx_simulated_trading_required":
            recommended_action = "Verify the simulated/live switch, the API key environment, and the selected startup profile before restarting the service."
        elif blocker == "operator_rebaseline_required":
            recommended_action = "Review the exchange/local divergence and accept the current exchange state as a new baseline only if it is expected."
        elif blocker == "rebaseline_in_progress":
            recommended_action = "Wait for the explicit operator re-baseline action to complete before attempting to resume execution."
        elif blocker == "resume_blocked":
            recommended_action = "Inspect reconciliation, freshness, and recovery state before resuming execution."
        return {
            "blocker": blocker,
            "subsystem": subsystem,
            "affects_execution": affects_execution,
            "affects_account_synchronization": subsystem == "account_state",
            "submit_only": submit_only_value,
            "recommended_action": recommended_action,
        }

    @staticmethod
    def _operator_user_view(
        user: OperatorUserRecord,
        *,
        actor_identity: str | None = None,
        last_admin_protected: bool | None = None,
    ) -> dict[str, Any]:
        payload = user.model_dump(mode="json", exclude={"password_hash"})
        payload["is_current_session_user"] = actor_identity is not None and user.username == actor_identity
        payload["protected_last_admin"] = bool(last_admin_protected and user.enabled and user.role == "admin")
        return payload
