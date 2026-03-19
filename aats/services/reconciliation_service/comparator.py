from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from aats.schemas.common import new_id, utc_now
from aats.schemas.execution import FillEvent, OrderState
from aats.schemas.exchange import ExchangeAccountSnapshot, ExchangeFill, ExchangeOpenOrder
from aats.schemas.portfolio import PortfolioSnapshot
from aats.schemas.reconciliation import ReconciliationReport
from aats.services.execution_engine.okx_bills import explain_okx_bills_for_reconciliation
from aats.services.execution_engine.state_machine import OrderStateMachine
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, EPSILON_DECIMAL_9, to_decimal


class StateComparator:
    def compare(
        self,
        *,
        decision_id: str | None,
        portfolio_snapshot_ref: str | None,
        product_type: str | None = None,
        margin_mode: str | None = None,
        allowed_symbols: list[str] | None = None,
        order_states: list[OrderState],
        fills: list[FillEvent],
        stored_snapshot: PortfolioSnapshot,
        reconstructed_snapshot: PortfolioSnapshot,
        exchange_snapshot: ExchangeAccountSnapshot | None = None,
        exchange_comparison_enabled: bool = False,
        compare_exchange_portfolio: bool = False,
        accepted_exchange_fill_ids: set[str] | None = None,
        trusted_exchange_portfolio_baseline: bool = False,
        exchange_bills_summary: dict[str, object] | None = None,
    ) -> ReconciliationReport:
        exchange_snapshot_covers_local_execution = self._exchange_snapshot_covers_local_execution(
            order_states=order_states,
            fills=fills,
            exchange_snapshot=exchange_snapshot,
        )
        effective_exchange_comparison_enabled = (
            exchange_comparison_enabled and exchange_snapshot_covers_local_execution
        )
        effective_compare_exchange_portfolio = (
            compare_exchange_portfolio and exchange_snapshot_covers_local_execution
        )
        order_diff = self._order_diff(
            order_states=order_states,
            fills=fills,
            exchange_snapshot=exchange_snapshot,
            exchange_comparison_enabled=effective_exchange_comparison_enabled,
        )
        fill_diff = self._fill_diff(
            order_states=order_states,
            fills=fills,
            exchange_snapshot=exchange_snapshot,
            exchange_comparison_enabled=effective_exchange_comparison_enabled,
            accepted_exchange_fill_ids=accepted_exchange_fill_ids,
        )
        balance_diff = self._balance_diff(
            stored_balances=stored_snapshot.balances,
            reconstructed_balances=reconstructed_snapshot.balances,
            exchange_snapshot=exchange_snapshot,
            compare_exchange_portfolio=effective_compare_exchange_portfolio,
        )
        position_diff = self._position_diff(
            stored_snapshot=stored_snapshot,
            reconstructed_snapshot=reconstructed_snapshot,
            exchange_snapshot=exchange_snapshot,
            compare_exchange_portfolio=effective_compare_exchange_portfolio,
        )

        mismatch_categories = self._mismatch_categories(
            order_states=order_states,
            fills=fills,
            order_diff=order_diff,
            fill_diff=fill_diff,
            balance_diff=balance_diff,
            position_diff=position_diff,
            trusted_exchange_portfolio_baseline=trusted_exchange_portfolio_baseline,
            exchange_bills_summary=exchange_bills_summary or {},
        )
        severity = self._severity(
            mismatch_categories=mismatch_categories,
            order_diff=order_diff,
            fill_diff=fill_diff,
            balance_diff=balance_diff,
            position_diff=position_diff,
        )
        mismatch_reasons = self._mismatch_reasons(
            order_diff=order_diff,
            fill_diff=fill_diff,
            balance_diff=balance_diff,
            position_diff=position_diff,
            mismatch_categories=mismatch_categories,
            exchange_bills_summary=exchange_bills_summary or {},
        )
        review_required = severity == "REVIEW_REQUIRED"
        exchange_bills_explanations = explain_okx_bills_for_reconciliation(
            summary=dict(exchange_bills_summary or {}),
            mismatch_categories=mismatch_categories,
            mismatch_reasons=mismatch_reasons,
        )
        safety_impacts = self._safety_impacts(
            severity=severity,
            mismatch_categories=mismatch_categories,
            order_diff=order_diff,
            fill_diff=fill_diff,
            balance_diff=balance_diff,
            position_diff=position_diff,
            exchange_bills_summary=exchange_bills_summary or {},
        )
        recommended_operator_action = self._recommended_operator_action(
            severity=severity,
            mismatch_categories=mismatch_categories,
            exchange_bills_summary=exchange_bills_summary or {},
        )
        remediation_action = recommended_operator_action
        return ReconciliationReport(
            reconciliation_id=new_id("recon"),
            decision_id=decision_id,
            portfolio_snapshot_ref=portfolio_snapshot_ref,
            as_of_ts=utc_now(),
            exchange_snapshot_ts=exchange_snapshot.fetched_at if exchange_snapshot is not None else None,
            product_type=product_type,  # type: ignore[arg-type]
            margin_mode=margin_mode,  # type: ignore[arg-type]
            allowed_symbols=list(allowed_symbols or []),
            exchange_comparison_enabled=effective_exchange_comparison_enabled,
            order_diff=order_diff,
            fill_diff=fill_diff,
            balance_diff=balance_diff,
            position_diff=position_diff,
            exchange_bills_summary=dict(exchange_bills_summary or {}),
            exchange_bills_explanations=exchange_bills_explanations,
            mismatch_categories=mismatch_categories,
            mismatch_reasons=mismatch_reasons,
            safety_impacts=safety_impacts,
            severity=severity,
            review_required=review_required,
            recommended_operator_action=recommended_operator_action,
            remediation_action=remediation_action,
            halt_required=severity == "HARD_MISMATCH",
        )

    @staticmethod
    def _mismatch_categories(
        *,
        order_states: list[OrderState],
        fills: list[FillEvent],
        order_diff: dict[str, object],
        fill_diff: dict[str, object],
        balance_diff: dict[str, object],
        position_diff: dict[str, object],
        trusted_exchange_portfolio_baseline: bool,
        exchange_bills_summary: dict[str, object],
    ) -> list[str]:
        categories: list[str] = []
        has_local_execution_state = bool(order_states or fills)
        exchange_fill_diff = bool(fill_diff.get("exchange"))
        exchange_fill_view = fill_diff.get("exchange") if isinstance(fill_diff.get("exchange"), dict) else {}
        unexpected_exchange_fills = bool(exchange_fill_view.get("unexpected_on_exchange"))
        exchange_balance_diff = bool(balance_diff.get("exchange"))
        exchange_position_diff = bool(position_diff.get("exchange_mismatches"))
        exchange_order_diff = bool(order_diff.get("exchange"))
        local_execution_diff = bool(order_diff.get("reconstructed")) or bool(fill_diff.get("replayed"))
        local_portfolio_diff = bool(balance_diff.get("reconstructed")) or bool(
            position_diff.get("reconstructed_mismatches")
        )

        if (
            not has_local_execution_state
            and (exchange_fill_diff or exchange_balance_diff or exchange_position_diff)
            and not trusted_exchange_portfolio_baseline
        ):
            return ["historical_state_only"]
        if (
            (unexpected_exchange_fills or exchange_balance_diff or exchange_position_diff)
            and not exchange_order_diff
            and not local_execution_diff
            and not local_portfolio_diff
        ):
            categories.append("external_manual_activity_detected")
        if (
            exchange_bills_summary.get("available")
            and (unexpected_exchange_fills or exchange_balance_diff or exchange_position_diff or exchange_order_diff)
        ):
            categories.append("exchange_bills_activity_available")

        if fill_diff.get("replayed"):
            categories.append("local_fill_missing")
        if balance_diff.get("exchange"):
            categories.append("local_balance_divergence")
        if position_diff.get("exchange_mismatches"):
            categories.append("local_position_divergence")
        if order_diff.get("exchange"):
            categories.append("local_open_order_divergence")
        if local_execution_diff or local_portfolio_diff:
            categories.append("unsafe_unknown_state")

        seen: set[str] = set()
        ordered: list[str] = []
        for category in categories:
            if category in seen:
                continue
            seen.add(category)
            ordered.append(category)
        return ordered

    @staticmethod
    def _order_diff(
        *,
        order_states: list[OrderState],
        fills: list[FillEvent],
        exchange_snapshot: ExchangeAccountSnapshot | None,
        exchange_comparison_enabled: bool,
    ) -> dict[str, object]:
        fill_qty_by_intent: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        for fill in fills:
            fill_qty_by_intent[fill.intent_id] += fill.fill_qty

        replay_mismatches: dict[str, dict[str, Decimal]] = {}
        for order_state in order_states:
            replayed_fill_qty = fill_qty_by_intent.get(order_state.intent_id, Decimal("0"))
            if abs(replayed_fill_qty - order_state.filled_qty) > EPSILON_DECIMAL_12:
                replay_mismatches[order_state.intent_id] = {
                    "stored_filled_qty": order_state.filled_qty,
                    "replayed_filled_qty": replayed_fill_qty,
                }

        exchange_view: dict[str, object] = {}
        if exchange_comparison_enabled and exchange_snapshot is not None:
            local_open_orders = {
                StateComparator._order_key(order): order
                for order in order_states
                if order.venue == "OKX" and OrderStateMachine.is_open(order.status)
            }
            exchange_open_orders = {
                StateComparator._exchange_order_key(order): order
                for order in exchange_snapshot.open_orders
            }
            missing_on_exchange = sorted(set(local_open_orders) - set(exchange_open_orders))
            unexpected_on_exchange = sorted(set(exchange_open_orders) - set(local_open_orders))
            status_mismatches: dict[str, dict[str, str]] = {}
            for order_key in sorted(set(local_open_orders) & set(exchange_open_orders)):
                local_order = local_open_orders[order_key]
                exchange_order = exchange_open_orders[order_key]
                local_status = local_order.exchange_status or local_order.status
                exchange_status = exchange_order.status.lower()
                if local_status.lower() != exchange_status.lower():
                    status_mismatches[order_key] = {
                        "local": local_status,
                        "exchange": exchange_order.status,
                    }
            if missing_on_exchange or unexpected_on_exchange or status_mismatches:
                exchange_view = {
                    "missing_on_exchange": missing_on_exchange,
                    "unexpected_on_exchange": unexpected_on_exchange,
                    "status_mismatches": status_mismatches,
                }

        return {
            "reconstructed": replay_mismatches,
            "exchange": exchange_view,
        }

    @staticmethod
    def _fill_diff(
        *,
        order_states: list[OrderState],
        fills: list[FillEvent],
        exchange_snapshot: ExchangeAccountSnapshot | None,
        exchange_comparison_enabled: bool,
        accepted_exchange_fill_ids: set[str] | None,
    ) -> dict[str, object]:
        known_intents = {order_state.intent_id for order_state in order_states}
        replay_mismatches: dict[str, dict[str, object]] = {}
        for fill in fills:
            if fill.intent_id not in known_intents:
                replay_mismatches[fill.fill_id] = {
                    "issue": "orphan_fill",
                    "intent_id": fill.intent_id,
                    "symbol": fill.symbol,
                }

        exchange_view: dict[str, object] = {}
        if exchange_comparison_enabled and exchange_snapshot is not None:
            local_exchange_fills = {
                StateComparator._fill_key(fill): fill for fill in fills if fill.venue == "OKX"
            }
            accepted_fill_ids = set(accepted_exchange_fill_ids or set())
            exchange_fills = {
                StateComparator._exchange_fill_key(fill): fill for fill in exchange_snapshot.fills
            }
            missing_on_exchange = sorted(
                fill_id
                for fill_id in (set(local_exchange_fills) - set(exchange_fills))
                if fill_id not in accepted_fill_ids
            )
            unexpected_on_exchange = sorted(
                fill_id
                for fill_id in (set(exchange_fills) - set(local_exchange_fills))
                if fill_id not in accepted_fill_ids
            )
            if missing_on_exchange or unexpected_on_exchange:
                exchange_view = {
                    "missing_on_exchange": missing_on_exchange,
                    "unexpected_on_exchange": unexpected_on_exchange,
                }

        return {
            "replayed": replay_mismatches,
            "exchange": exchange_view,
        }

    @staticmethod
    def _balance_diff(
        *,
        stored_balances: dict[str, Decimal],
        reconstructed_balances: dict[str, Decimal],
        exchange_snapshot: ExchangeAccountSnapshot | None,
        compare_exchange_portfolio: bool,
    ) -> dict[str, object]:
        reconstructed_mismatches: dict[str, dict[str, Decimal]] = {}
        currencies = sorted(set(stored_balances) | set(reconstructed_balances))
        for currency in currencies:
            stored = to_decimal(stored_balances.get(currency, 0))
            replayed = to_decimal(reconstructed_balances.get(currency, 0))
            if abs(stored - replayed) > EPSILON_DECIMAL_9:
                reconstructed_mismatches[currency] = {"stored": stored, "reconstructed": replayed}

        exchange_mismatches: dict[str, dict[str, Decimal]] = {}
        if compare_exchange_portfolio and exchange_snapshot is not None:
            exchange_balances = {balance.currency: to_decimal(balance.total) for balance in exchange_snapshot.balances}
            currencies = sorted(set(stored_balances) | set(exchange_balances))
            for currency in currencies:
                stored = to_decimal(stored_balances.get(currency, 0))
                exchange = to_decimal(exchange_balances.get(currency, 0))
                if abs(stored - exchange) > EPSILON_DECIMAL_9:
                    exchange_mismatches[currency] = {"stored": stored, "exchange": exchange}

        return {
            "reconstructed": reconstructed_mismatches,
            "exchange": exchange_mismatches,
        }

    @staticmethod
    def _position_diff(
        *,
        stored_snapshot: PortfolioSnapshot,
        reconstructed_snapshot: PortfolioSnapshot,
        exchange_snapshot: ExchangeAccountSnapshot | None,
        compare_exchange_portfolio: bool,
    ) -> dict[str, object]:
        stored_positions = {position.symbol: position.position_qty for position in stored_snapshot.positions}
        replayed_positions = {
            position.symbol: position.position_qty for position in reconstructed_snapshot.positions
        }
        reconstructed_mismatches: dict[str, dict[str, Decimal]] = {}
        for symbol in sorted(set(stored_positions) | set(replayed_positions)):
            stored = to_decimal(stored_positions.get(symbol, 0))
            replayed = to_decimal(replayed_positions.get(symbol, 0))
            if abs(stored - replayed) > EPSILON_DECIMAL_12:
                reconstructed_mismatches[symbol] = {"stored": stored, "reconstructed": replayed}

        exchange_positions: dict[str, Decimal] = {}
        exchange_mismatches: dict[str, dict[str, Decimal]] = {}
        if compare_exchange_portfolio and exchange_snapshot is not None and exchange_snapshot.positions:
            exchange_positions = {
                position.symbol: to_decimal(position.quantity)
                for position in exchange_snapshot.positions
                if abs(to_decimal(position.quantity)) > EPSILON_DECIMAL_12
            }
            for symbol in sorted(set(stored_positions) | set(exchange_positions)):
                stored = to_decimal(stored_positions.get(symbol, 0))
                exchange = to_decimal(exchange_positions.get(symbol, 0))
                if abs(stored - exchange) > EPSILON_DECIMAL_12:
                    exchange_mismatches[symbol] = {"stored": stored, "exchange": exchange}

        return {
            "stored": stored_positions,
            "reconstructed": replayed_positions,
            "reconstructed_mismatches": reconstructed_mismatches,
            "exchange": exchange_positions,
            "exchange_mismatches": exchange_mismatches,
        }

    @staticmethod
    def _exchange_snapshot_covers_local_execution(
        *,
        order_states: list[OrderState],
        fills: list[FillEvent],
        exchange_snapshot: ExchangeAccountSnapshot | None,
    ) -> bool:
        if exchange_snapshot is None:
            return False
        latest_local_execution_ts = exchange_snapshot.fetched_at
        saw_local_execution = False
        for order in order_states:
            if order.venue != "OKX":
                continue
            for candidate in (order.last_exchange_update_ts, order.last_update_ts, order.submitted_ts):
                if candidate is None:
                    continue
                if not saw_local_execution or candidate > latest_local_execution_ts:
                    latest_local_execution_ts = candidate
                saw_local_execution = True
        for fill in fills:
            if fill.venue != "OKX":
                continue
            for candidate in (fill.exchange_timestamp, fill.ingestion_timestamp):
                if not saw_local_execution or candidate > latest_local_execution_ts:
                    latest_local_execution_ts = candidate
                saw_local_execution = True
        if not saw_local_execution:
            return True
        return latest_local_execution_ts <= exchange_snapshot.fetched_at

    @staticmethod
    def _severity(
        *,
        mismatch_categories: list[str],
        order_diff: dict[str, object],
        fill_diff: dict[str, object],
        balance_diff: dict[str, object],
        position_diff: dict[str, object],
    ) -> str:
        replay_portfolio_mismatch = bool(balance_diff.get("reconstructed")) or bool(
            position_diff.get("reconstructed_mismatches")
        )
        exchange_portfolio_mismatch = bool(balance_diff.get("exchange")) or bool(
            position_diff.get("exchange_mismatches")
        )
        exchange_order_mismatch = bool(order_diff.get("exchange")) or bool(fill_diff.get("exchange"))
        local_execution_mismatch = bool(order_diff.get("reconstructed")) or bool(fill_diff.get("replayed"))

        if not replay_portfolio_mismatch and not exchange_portfolio_mismatch and not exchange_order_mismatch and not local_execution_mismatch:
            return "CLEAN"
        if "unsafe_unknown_state" in mismatch_categories or "local_open_order_divergence" in mismatch_categories:
            return "HARD_MISMATCH"
        if mismatch_categories == ["historical_state_only"]:
            return "INFO"
        if "external_manual_activity_detected" in mismatch_categories:
            return "REVIEW_REQUIRED"
        if exchange_portfolio_mismatch or bool(fill_diff.get("exchange")):
            return "SOFT_MISMATCH"
        return "SOFT_MISMATCH"

    @staticmethod
    def _mismatch_reasons(
        *,
        order_diff: dict[str, object],
        fill_diff: dict[str, object],
        balance_diff: dict[str, object],
        position_diff: dict[str, object],
        mismatch_categories: list[str],
        exchange_bills_summary: dict[str, object],
    ) -> list[str]:
        reasons: list[str] = []
        if order_diff.get("reconstructed"):
            reasons.append("local_order_state_differs_from_fill_reconstruction")
        if order_diff.get("exchange"):
            reasons.append("local_open_orders_diverge_from_exchange_open_orders")
        if fill_diff.get("replayed"):
            reasons.append("orphan_or_incomplete_local_fill_chain_detected")
        if fill_diff.get("exchange"):
            reasons.append("local_exchange_fill_set_diverges_from_exchange_fill_set")
        if balance_diff.get("reconstructed"):
            reasons.append("stored_balance_differs_from_replayed_balance")
        if balance_diff.get("exchange"):
            reasons.append("local_balance_differs_from_exchange_balance")
        if position_diff.get("reconstructed_mismatches"):
            reasons.append("stored_position_differs_from_replayed_position")
        if position_diff.get("exchange_mismatches"):
            reasons.append("local_position_differs_from_exchange_position")
        if "exchange_bills_activity_available" in mismatch_categories and exchange_bills_summary.get("count"):
            reasons.append("recent_exchange_bills_may_explain_exchange_side_balance_activity")
        return reasons

    @staticmethod
    def _safety_impacts(
        *,
        severity: str,
        mismatch_categories: list[str],
        order_diff: dict[str, object],
        fill_diff: dict[str, object],
        balance_diff: dict[str, object],
        position_diff: dict[str, object],
        exchange_bills_summary: dict[str, object],
    ) -> list[str]:
        impacts: list[str] = []
        if severity == "HARD_MISMATCH":
            impacts.append("portfolio_state_may_be_unsafe_for_trading")
        if severity == "REVIEW_REQUIRED":
            impacts.append("operator_review_required_before_trading")
        if "historical_state_only" in mismatch_categories:
            impacts.append("historical_account_state_is_not_locally_replayed")
        if order_diff.get("exchange"):
            impacts.append("open_order_visibility_is_incomplete")
        if fill_diff.get("exchange"):
            impacts.append("fill_history_visibility_is_incomplete")
        if balance_diff.get("exchange") or position_diff.get("exchange_mismatches"):
            impacts.append("exchange_account_state_differs_from_local_state")
        if "exchange_bills_activity_available" in mismatch_categories and exchange_bills_summary.get("count"):
            impacts.append("review_exchange_bills_before_rebaselining")
        return impacts

    @staticmethod
    def _recommended_operator_action(
        *,
        severity: str,
        mismatch_categories: list[str],
        exchange_bills_summary: dict[str, object],
    ) -> str | None:
        if severity == "CLEAN":
            return None
        if severity == "INFO":
            return "observe_only"
        if (
            severity == "REVIEW_REQUIRED"
            and "exchange_bills_activity_available" in mismatch_categories
            and exchange_bills_summary.get("count")
        ):
            return "review_exchange_bills_and_rebaseline_if_expected"
        if severity == "SOFT_MISMATCH":
            return "investigate_state_divergence"
        if severity == "REVIEW_REQUIRED":
            return "review_and_rebaseline_if_expected"
        return "halt_execution_and_investigate_state_divergence"

    @staticmethod
    def _order_key(order: OrderState) -> str:
        return order.exchange_order_id or order.client_order_id

    @staticmethod
    def _exchange_order_key(order: ExchangeOpenOrder) -> str:
        return order.exchange_order_id or (order.client_order_id or "")

    @staticmethod
    def _fill_key(fill: FillEvent) -> str:
        return fill.fill_id

    @staticmethod
    def _exchange_fill_key(fill: ExchangeFill) -> str:
        return fill.fill_id
