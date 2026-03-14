from __future__ import annotations

from collections import defaultdict

from aats.schemas.common import new_id, utc_now
from aats.schemas.execution import FillEvent, OrderState
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
    ) -> ReconciliationReport:
        order_diff = self._order_diff(order_states=order_states, fills=fills)
        fill_diff = self._fill_diff(order_states=order_states, fills=fills)
        balance_diff = self._balance_diff(
            stored_balances=stored_snapshot.balances,
            reconstructed_balances=reconstructed_snapshot.balances,
        )
        position_diff = self._position_diff(
            stored_snapshot=stored_snapshot,
            reconstructed_snapshot=reconstructed_snapshot,
        )

        severity = self._severity(
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
            order_diff=order_diff,
            fill_diff=fill_diff,
            balance_diff=balance_diff,
            position_diff=position_diff,
            severity=severity,
            remediation_action=remediation_action,
            halt_required=severity == "HARD_MISMATCH",
        )

    @staticmethod
    def _order_diff(*, order_states: list[OrderState], fills: list[FillEvent]) -> dict[str, dict[str, float]]:
        fill_qty_by_intent: dict[str, float] = defaultdict(float)
        for fill in fills:
            fill_qty_by_intent[fill.intent_id] += fill.fill_qty

        mismatches: dict[str, dict[str, float]] = {}
        for order_state in order_states:
            replayed_fill_qty = fill_qty_by_intent.get(order_state.intent_id, 0.0)
            if abs(replayed_fill_qty - order_state.filled_qty) > 1e-12:
                mismatches[order_state.intent_id] = {
                    "stored_filled_qty": order_state.filled_qty,
                    "replayed_filled_qty": replayed_fill_qty,
                }
        return mismatches

    @staticmethod
    def _fill_diff(*, order_states: list[OrderState], fills: list[FillEvent]) -> dict[str, dict[str, object]]:
        known_intents = {order_state.intent_id for order_state in order_states}
        mismatches: dict[str, dict[str, object]] = {}
        for fill in fills:
            if fill.intent_id not in known_intents:
                mismatches[fill.fill_id] = {
                    "issue": "orphan_fill",
                    "intent_id": fill.intent_id,
                    "symbol": fill.symbol,
                }
        return mismatches

    @staticmethod
    def _balance_diff(
        *,
        stored_balances: dict[str, float],
        reconstructed_balances: dict[str, float],
    ) -> dict[str, dict[str, float]]:
        mismatches: dict[str, dict[str, float]] = {}
        currencies = sorted(set(stored_balances) | set(reconstructed_balances))
        for currency in currencies:
            stored = stored_balances.get(currency, 0.0)
            replayed = reconstructed_balances.get(currency, 0.0)
            if abs(stored - replayed) > 1e-9:
                mismatches[currency] = {"stored": stored, "reconstructed": replayed}
        return mismatches

    @staticmethod
    def _position_diff(
        *,
        stored_snapshot: PortfolioSnapshot,
        reconstructed_snapshot: PortfolioSnapshot,
    ) -> dict[str, object]:
        stored_positions = {position.symbol: position.position_qty for position in stored_snapshot.positions}
        replayed_positions = {
            position.symbol: position.position_qty for position in reconstructed_snapshot.positions
        }
        mismatches: dict[str, dict[str, float]] = {}
        for symbol in sorted(set(stored_positions) | set(replayed_positions)):
            stored = stored_positions.get(symbol, 0.0)
            replayed = replayed_positions.get(symbol, 0.0)
            if abs(stored - replayed) > 1e-12:
                mismatches[symbol] = {"stored": stored, "reconstructed": replayed}
        return {
            "stored": stored_positions,
            "reconstructed": replayed_positions,
            "mismatches": mismatches,
        }

    @staticmethod
    def _severity(
        *,
        order_diff: dict[str, object],
        fill_diff: dict[str, object],
        balance_diff: dict[str, object],
        position_diff: dict[str, object],
    ) -> str:
        has_portfolio_mismatch = bool(balance_diff) or bool(position_diff.get("mismatches"))
        if not order_diff and not fill_diff and not has_portfolio_mismatch:
            return "CLEAN"
        if has_portfolio_mismatch:
            return "HARD_MISMATCH"
        return "SOFT_MISMATCH"
