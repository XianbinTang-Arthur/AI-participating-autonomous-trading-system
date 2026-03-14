from __future__ import annotations

from collections import defaultdict

from aats.schemas.common import new_id, utc_now
from aats.schemas.execution import FillEvent, OrderState
from aats.schemas.exchange import ExchangeAccountSnapshot, ExchangeFill, ExchangeOpenOrder
from aats.schemas.portfolio import PortfolioSnapshot
from aats.schemas.reconciliation import ReconciliationReport


class StateComparator:
    def compare(
        self,
        *,
        decision_id: str | None,
        portfolio_snapshot_ref: str | None,
        order_states: list[OrderState],
        fills: list[FillEvent],
        stored_snapshot: PortfolioSnapshot,
        reconstructed_snapshot: PortfolioSnapshot,
        exchange_snapshot: ExchangeAccountSnapshot | None = None,
        exchange_comparison_enabled: bool = False,
        compare_exchange_portfolio: bool = False,
    ) -> ReconciliationReport:
        order_diff = self._order_diff(
            order_states=order_states,
            fills=fills,
            exchange_snapshot=exchange_snapshot,
            exchange_comparison_enabled=exchange_comparison_enabled,
        )
        fill_diff = self._fill_diff(
            order_states=order_states,
            fills=fills,
            exchange_snapshot=exchange_snapshot,
            exchange_comparison_enabled=exchange_comparison_enabled,
        )
        balance_diff = self._balance_diff(
            stored_balances=stored_snapshot.balances,
            reconstructed_balances=reconstructed_snapshot.balances,
            exchange_snapshot=exchange_snapshot,
            compare_exchange_portfolio=compare_exchange_portfolio,
        )
        position_diff = self._position_diff(
            stored_snapshot=stored_snapshot,
            reconstructed_snapshot=reconstructed_snapshot,
            exchange_snapshot=exchange_snapshot,
            compare_exchange_portfolio=compare_exchange_portfolio,
        )

        severity = self._severity(
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
        )
        safety_impacts = self._safety_impacts(
            severity=severity,
            order_diff=order_diff,
            fill_diff=fill_diff,
            balance_diff=balance_diff,
            position_diff=position_diff,
        )
        remediation_action = None if severity == "CLEAN" else "investigate_state_divergence"
        return ReconciliationReport(
            reconciliation_id=new_id("recon"),
            decision_id=decision_id,
            portfolio_snapshot_ref=portfolio_snapshot_ref,
            as_of_ts=utc_now(),
            exchange_snapshot_ts=exchange_snapshot.fetched_at if exchange_snapshot is not None else None,
            exchange_comparison_enabled=exchange_comparison_enabled,
            order_diff=order_diff,
            fill_diff=fill_diff,
            balance_diff=balance_diff,
            position_diff=position_diff,
            mismatch_reasons=mismatch_reasons,
            safety_impacts=safety_impacts,
            severity=severity,
            remediation_action=remediation_action,
            halt_required=severity == "HARD_MISMATCH",
        )

    @staticmethod
    def _order_diff(
        *,
        order_states: list[OrderState],
        fills: list[FillEvent],
        exchange_snapshot: ExchangeAccountSnapshot | None,
        exchange_comparison_enabled: bool,
    ) -> dict[str, object]:
        fill_qty_by_intent: dict[str, float] = defaultdict(float)
        for fill in fills:
            fill_qty_by_intent[fill.intent_id] += fill.fill_qty

        replay_mismatches: dict[str, dict[str, float]] = {}
        for order_state in order_states:
            replayed_fill_qty = fill_qty_by_intent.get(order_state.intent_id, 0.0)
            if abs(replayed_fill_qty - order_state.filled_qty) > 1e-12:
                replay_mismatches[order_state.intent_id] = {
                    "stored_filled_qty": order_state.filled_qty,
                    "replayed_filled_qty": replayed_fill_qty,
                }

        exchange_view: dict[str, object] = {}
        if exchange_comparison_enabled and exchange_snapshot is not None:
            local_open_orders = {
                StateComparator._order_key(order): order
                for order in order_states
                if order.venue == "OKX" and order.status not in {"FILLED", "CANCELED", "REJECTED", "FAILED"}
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
            exchange_fills = {
                StateComparator._exchange_fill_key(fill): fill for fill in exchange_snapshot.fills
            }
            missing_on_exchange = sorted(set(local_exchange_fills) - set(exchange_fills))
            unexpected_on_exchange = sorted(set(exchange_fills) - set(local_exchange_fills))
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
        stored_balances: dict[str, float],
        reconstructed_balances: dict[str, float],
        exchange_snapshot: ExchangeAccountSnapshot | None,
        compare_exchange_portfolio: bool,
    ) -> dict[str, object]:
        reconstructed_mismatches: dict[str, dict[str, float]] = {}
        currencies = sorted(set(stored_balances) | set(reconstructed_balances))
        for currency in currencies:
            stored = stored_balances.get(currency, 0.0)
            replayed = reconstructed_balances.get(currency, 0.0)
            if abs(stored - replayed) > 1e-9:
                reconstructed_mismatches[currency] = {"stored": stored, "reconstructed": replayed}

        exchange_mismatches: dict[str, dict[str, float]] = {}
        if compare_exchange_portfolio and exchange_snapshot is not None:
            exchange_balances = {balance.currency: balance.total for balance in exchange_snapshot.balances}
            currencies = sorted(set(stored_balances) | set(exchange_balances))
            for currency in currencies:
                stored = stored_balances.get(currency, 0.0)
                exchange = exchange_balances.get(currency, 0.0)
                if abs(stored - exchange) > 1e-9:
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
        reconstructed_mismatches: dict[str, dict[str, float]] = {}
        for symbol in sorted(set(stored_positions) | set(replayed_positions)):
            stored = stored_positions.get(symbol, 0.0)
            replayed = replayed_positions.get(symbol, 0.0)
            if abs(stored - replayed) > 1e-12:
                reconstructed_mismatches[symbol] = {"stored": stored, "reconstructed": replayed}

        exchange_positions: dict[str, float] = {}
        exchange_mismatches: dict[str, dict[str, float]] = {}
        if compare_exchange_portfolio and exchange_snapshot is not None:
            exchange_positions = {
                position.symbol: position.quantity
                for position in exchange_snapshot.positions
                if abs(position.quantity) > 1e-12
            }
            for symbol in sorted(set(stored_positions) | set(exchange_positions)):
                stored = stored_positions.get(symbol, 0.0)
                exchange = exchange_positions.get(symbol, 0.0)
                if abs(stored - exchange) > 1e-12:
                    exchange_mismatches[symbol] = {"stored": stored, "exchange": exchange}

        return {
            "stored": stored_positions,
            "reconstructed": replayed_positions,
            "reconstructed_mismatches": reconstructed_mismatches,
            "exchange": exchange_positions,
            "exchange_mismatches": exchange_mismatches,
        }

    @staticmethod
    def _severity(
        *,
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
        if replay_portfolio_mismatch or exchange_portfolio_mismatch:
            return "HARD_MISMATCH"
        return "SOFT_MISMATCH"

    @staticmethod
    def _mismatch_reasons(
        *,
        order_diff: dict[str, object],
        fill_diff: dict[str, object],
        balance_diff: dict[str, object],
        position_diff: dict[str, object],
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
        return reasons

    @staticmethod
    def _safety_impacts(
        *,
        severity: str,
        order_diff: dict[str, object],
        fill_diff: dict[str, object],
        balance_diff: dict[str, object],
        position_diff: dict[str, object],
    ) -> list[str]:
        impacts: list[str] = []
        if severity == "HARD_MISMATCH":
            impacts.append("portfolio_state_may_be_unsafe_for_trading")
        if order_diff.get("exchange"):
            impacts.append("open_order_visibility_is_incomplete")
        if fill_diff.get("exchange"):
            impacts.append("fill_history_visibility_is_incomplete")
        if balance_diff.get("exchange") or position_diff.get("exchange_mismatches"):
            impacts.append("exchange_account_state_differs_from_local_state")
        return impacts

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
