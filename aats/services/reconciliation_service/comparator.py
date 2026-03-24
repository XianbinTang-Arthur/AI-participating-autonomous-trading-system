from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from aats.schemas.common import new_id, utc_now
from aats.schemas.execution import FillEvent, OrderState
from aats.schemas.exchange import ExchangeAccountSnapshot, ExchangeFill, ExchangeOpenOrder
from aats.schemas.portfolio import PortfolioSnapshot
from aats.schemas.reconciliation import ReconciliationFinding, ReconciliationReport
from aats.services.execution_engine.okx_bills import explain_okx_bills_for_reconciliation
from aats.services.execution_engine.state_machine import OrderStateMachine
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, EPSILON_DECIMAL_9, to_decimal
from aats.services.portfolio_service.position_keys import (
    position_key_for_exchange_position,
    position_key_for_snapshot_position,
    signed_quantity_for_position_side,
    symbol_from_position_key,
)


@dataclass(slots=True)
class DerivativesUnknownStateAssessment:
    mismatch_categories: list[str] = field(default_factory=list)
    mismatch_reasons: list[str] = field(default_factory=list)
    safety_impacts: list[str] = field(default_factory=list)
    only_reduce_required: bool = False
    only_reduce_reasons: list[str] = field(default_factory=list)
    unknown_state_details: list[dict[str, object]] = field(default_factory=list)
    preferred_operator_action: str | None = None


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
        reconciliation_id = new_id("recon")
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
        derivatives_assessment = self._derivatives_unknown_state_assessment(
            product_type=product_type,
            order_states=order_states,
            fills=fills,
            order_diff=order_diff,
            fill_diff=fill_diff,
            position_diff=position_diff,
            exchange_snapshot=exchange_snapshot,
        )
        mismatch_categories = self._dedupe_codes(
            [*mismatch_categories, *derivatives_assessment.mismatch_categories]
        )
        mismatch_reasons = self._dedupe_codes(
            [
                *self._mismatch_reasons(
                    order_diff=order_diff,
                    fill_diff=fill_diff,
                    balance_diff=balance_diff,
                    position_diff=position_diff,
                    mismatch_categories=mismatch_categories,
                    exchange_bills_summary=exchange_bills_summary or {},
                ),
                *derivatives_assessment.mismatch_reasons,
            ]
        )
        findings = self._build_findings(
            reconciliation_id=reconciliation_id,
            product_type=product_type,
            margin_mode=margin_mode,
            allowed_symbols=allowed_symbols,
            order_states=order_states,
            fills=fills,
            order_diff=order_diff,
            fill_diff=fill_diff,
            balance_diff=balance_diff,
            position_diff=position_diff,
            mismatch_categories=mismatch_categories,
            mismatch_reasons=mismatch_reasons,
            exchange_bills_summary=exchange_bills_summary or {},
            derivatives_assessment=derivatives_assessment,
        )
        severity = self._severity(
            findings=findings,
            mismatch_categories=mismatch_categories,
            only_reduce_required=derivatives_assessment.only_reduce_required,
        )
        review_required = any(finding.review_required for finding in findings)
        halt_required = any(finding.halt_required for finding in findings)
        structural_review_required = any(
            finding.structural and (finding.review_required or finding.halt_required)
            for finding in findings
        )
        financial_review_required = any(
            finding.financial and (finding.review_required or finding.halt_required)
            for finding in findings
        )
        observational_only = bool(findings) and all(finding.observational for finding in findings) and not (
            review_required or halt_required or derivatives_assessment.only_reduce_required
        )
        exchange_bills_explanations = explain_okx_bills_for_reconciliation(
            summary=dict(exchange_bills_summary or {}),
            mismatch_categories=mismatch_categories,
            mismatch_reasons=mismatch_reasons,
        )
        safety_impacts = self._dedupe_codes(
            [
                *self._safety_impacts(
                    severity=severity,
                    mismatch_categories=mismatch_categories,
                    order_diff=order_diff,
                    fill_diff=fill_diff,
                    balance_diff=balance_diff,
                    position_diff=position_diff,
                    exchange_bills_summary=exchange_bills_summary or {},
                    only_reduce_required=derivatives_assessment.only_reduce_required,
                ),
                *derivatives_assessment.safety_impacts,
            ]
        )
        recommended_operator_action = (
            derivatives_assessment.preferred_operator_action
            or self._recommended_operator_action(
                severity=severity,
                mismatch_categories=mismatch_categories,
                findings=findings,
                exchange_bills_summary=exchange_bills_summary or {},
                only_reduce_required=derivatives_assessment.only_reduce_required,
            )
        )
        remediation_action = recommended_operator_action
        return ReconciliationReport(
            reconciliation_id=reconciliation_id,
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
            findings=findings,
            finding_summary=self._finding_summary(findings),
            mismatch_categories=mismatch_categories,
            mismatch_reasons=mismatch_reasons,
            safety_impacts=safety_impacts,
            severity=severity,
            review_required=review_required,
            only_reduce_required=derivatives_assessment.only_reduce_required,
            only_reduce_reasons=derivatives_assessment.only_reduce_reasons,
            unknown_state_details=derivatives_assessment.unknown_state_details,
            recommended_operator_action=recommended_operator_action,
            remediation_action=remediation_action,
            halt_required=halt_required,
            structural_review_required=structural_review_required,
            financial_review_required=financial_review_required,
            observational_only=observational_only,
        )

    @staticmethod
    def _dedupe_codes(values: list[str]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for value in values:
            if not value or value in seen:
                continue
            seen.add(value)
            ordered.append(value)
        return ordered

    @staticmethod
    def _derivatives_unknown_state_assessment(
        *,
        product_type: str | None,
        order_states: list[OrderState],
        fills: list[FillEvent],
        order_diff: dict[str, object],
        fill_diff: dict[str, object],
        position_diff: dict[str, object],
        exchange_snapshot: ExchangeAccountSnapshot | None,
    ) -> DerivativesUnknownStateAssessment:
        assessment = DerivativesUnknownStateAssessment()
        if product_type != "derivatives" or exchange_snapshot is None:
            return assessment

        open_local_orders = [
            order
            for order in order_states
            if order.product_type == "derivatives"
            and order.venue == "OKX"
            and OrderStateMachine.is_open(order.status)
        ]
        exchange_order_view = order_diff.get("exchange") if isinstance(order_diff.get("exchange"), dict) else {}
        missing_on_exchange = list(exchange_order_view.get("missing_on_exchange") or [])
        status_mismatches = dict(exchange_order_view.get("status_mismatches") or {})
        if missing_on_exchange or status_mismatches:
            assessment.mismatch_categories.append("derivatives_order_state_unknown_on_exchange")
            if missing_on_exchange:
                assessment.mismatch_reasons.append("derivatives_local_order_missing_from_exchange_open_order_view")
                assessment.unknown_state_details.extend(
                    {
                        "kind": "order_state_unknown_on_exchange",
                        "symbol": next(
                            (order.symbol for order in open_local_orders if StateComparator._order_key(order) == order_key),
                            None,
                        ),
                        "order_key": order_key,
                    }
                    for order_key in missing_on_exchange
                )
            if status_mismatches:
                assessment.mismatch_reasons.append("derivatives_exchange_order_status_conflicts_with_local_open_state")
                assessment.unknown_state_details.extend(
                    {
                        "kind": "order_status_mismatch",
                        "symbol": next(
                            (order.symbol for order in open_local_orders if StateComparator._order_key(order) == order_key),
                            None,
                        ),
                        "order_key": order_key,
                        "local_status": details.get("local"),
                        "exchange_status": details.get("exchange"),
                    }
                    for order_key, details in status_mismatches.items()
                )
            assessment.safety_impacts.append("derivatives_open_order_state_is_not_confirmed")
            assessment.preferred_operator_action = "halt_execution_and_investigate_state_divergence"

        exchange_fill_view = fill_diff.get("exchange") if isinstance(fill_diff.get("exchange"), dict) else {}
        unexpected_exchange_fills = list(exchange_fill_view.get("unexpected_on_exchange") or [])
        if unexpected_exchange_fills:
            assessment.mismatch_categories.append("derivatives_fill_observed_not_booked")
            assessment.mismatch_reasons.append("derivatives_exchange_fill_observed_without_local_booking")
            assessment.safety_impacts.append("derivatives_fill_reconciliation_is_incomplete")
            assessment.unknown_state_details.append(
                {
                    "kind": "exchange_fill_without_local_booking",
                    "fill_ids": unexpected_exchange_fills,
                }
            )
            assessment.preferred_operator_action = "halt_execution_and_investigate_state_divergence"

        account_position_mode = StateComparator._exchange_position_mode(exchange_snapshot)
        if account_position_mode:
            position_mode_conflicts = [
                order
                for order in open_local_orders
                if order.position_mode is not None and order.position_mode != account_position_mode
            ]
            if position_mode_conflicts:
                assessment.mismatch_categories.append("derivatives_position_mode_mismatch")
                assessment.mismatch_reasons.append(
                    "derivatives_local_position_mode_differs_from_exchange_account_configuration"
                )
                assessment.safety_impacts.append("derivatives_order_semantics_do_not_match_exchange_account_mode")
                assessment.unknown_state_details.extend(
                    {
                        "kind": "position_mode_mismatch",
                        "symbol": order.symbol,
                        "client_order_id": order.client_order_id,
                        "local_position_mode": order.position_mode,
                        "exchange_position_mode": account_position_mode,
                    }
                    for order in position_mode_conflicts
                )
                assessment.preferred_operator_action = "halt_execution_and_investigate_state_divergence"

            pos_side_conflicts: list[OrderState] = []
            if account_position_mode == "net_mode":
                pos_side_conflicts = [
                    order for order in open_local_orders if order.pos_side not in {None, "net"}
                ]
            elif account_position_mode == "long_short_mode":
                pos_side_conflicts = [
                    order for order in open_local_orders if order.pos_side not in {"long", "short"}
                ]
            if pos_side_conflicts:
                assessment.mismatch_categories.append("derivatives_pos_side_mismatch")
                assessment.mismatch_reasons.append(
                    "derivatives_local_pos_side_conflicts_with_exchange_position_mode"
                )
                assessment.safety_impacts.append("derivatives_order_semantics_do_not_match_exchange_account_mode")
                assessment.unknown_state_details.extend(
                    {
                        "kind": "pos_side_mismatch",
                        "symbol": order.symbol,
                        "client_order_id": order.client_order_id,
                        "local_pos_side": order.pos_side,
                        "exchange_position_mode": account_position_mode,
                    }
                    for order in pos_side_conflicts
                )
                assessment.preferred_operator_action = "halt_execution_and_investigate_state_divergence"

        exchange_position_mismatches = (
            position_diff.get("exchange_mismatches")
            if isinstance(position_diff.get("exchange_mismatches"), dict)
            else {}
        )
        local_execution_symbols = {
            order.symbol for order in order_states if order.product_type == "derivatives"
        } | {
            fill.symbol for fill in fills if fill.product_type == "derivatives"
        }
        exchange_positions = StateComparator._exchange_position_quantity_map(exchange_snapshot)
        stored_positions = position_diff.get("stored") if isinstance(position_diff.get("stored"), dict) else {}
        missing_execution_chain_details: list[dict[str, object]] = []
        for position_key, mismatch in exchange_position_mismatches.items():
            exchange_qty = to_decimal(exchange_positions.get(position_key, Decimal("0")))
            symbol = symbol_from_position_key(position_key)
            if abs(exchange_qty) <= EPSILON_DECIMAL_12 or symbol in local_execution_symbols:
                continue
            missing_execution_chain_details.append(
                {
                    "kind": "exchange_position_without_local_execution_chain",
                    "position_key": position_key,
                    "symbol": symbol,
                    "stored_qty": to_decimal(stored_positions.get(position_key, Decimal("0"))),
                    "exchange_qty": exchange_qty,
                    "exchange_side_breakdown": [
                        {
                            "side": str(position.side or "net"),
                            "quantity": str(to_decimal(position.quantity)),
                        }
                        for position in exchange_snapshot.positions
                        if position.symbol == symbol and abs(to_decimal(position.quantity)) > EPSILON_DECIMAL_12
                    ],
                    "mismatch": mismatch,
                }
            )
        if missing_execution_chain_details:
            assessment.mismatch_categories.append("derivatives_exchange_position_without_local_execution_chain")
            assessment.mismatch_reasons.append("derivatives_exchange_position_not_replayed_locally")
            assessment.safety_impacts.append("derivatives_new_open_orders_blocked_until_position_reconciled")
            assessment.only_reduce_required = True
            assessment.only_reduce_reasons.append("derivatives_exchange_position_without_local_execution_chain")
            assessment.unknown_state_details.extend(missing_execution_chain_details)
            if assessment.preferred_operator_action is None:
                assessment.preferred_operator_action = "go_close_position_on_exchange"

        assessment.mismatch_categories = StateComparator._dedupe_codes(assessment.mismatch_categories)
        assessment.mismatch_reasons = StateComparator._dedupe_codes(assessment.mismatch_reasons)
        assessment.safety_impacts = StateComparator._dedupe_codes(assessment.safety_impacts)
        assessment.only_reduce_reasons = StateComparator._dedupe_codes(assessment.only_reduce_reasons)
        return assessment

    @staticmethod
    def _exchange_position_mode(exchange_snapshot: ExchangeAccountSnapshot) -> str | None:
        account_configuration = exchange_snapshot.account_configuration
        configured = None if account_configuration is None else account_configuration.position_mode
        value = str(configured or exchange_snapshot.position_mode or "").strip()
        return value or None

    @staticmethod
    def _exchange_position_net_by_symbol(positions) -> dict[str, Decimal]:
        net_by_symbol: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        for position in positions:
            quantity = to_decimal(position.quantity)
            if abs(quantity) <= EPSILON_DECIMAL_12:
                continue
            side = str(getattr(position, "side", "net") or "net").lower()
            signed_quantity = -quantity if side == "short" and quantity > 0 else quantity
            net_by_symbol[position.symbol] += signed_quantity
        return {
            symbol: quantity
            for symbol, quantity in net_by_symbol.items()
            if abs(quantity) > EPSILON_DECIMAL_12
        }

    @staticmethod
    def _position_quantity_map(positions) -> dict[str, Decimal]:
        return {
            position_key_for_snapshot_position(position): to_decimal(position.position_qty)
            for position in positions
            if abs(to_decimal(position.position_qty)) > EPSILON_DECIMAL_12
        }

    @staticmethod
    def _snapshot_position_margin_map(positions) -> dict[str, dict[str, object]]:
        rows: dict[str, dict[str, object]] = {}
        for position in positions:
            quantity = to_decimal(position.position_qty)
            if abs(quantity) <= EPSILON_DECIMAL_12:
                continue
            rows[position_key_for_snapshot_position(position)] = {
                "margin_mode": getattr(position, "margin_mode", None),
                "margin_allocated": to_decimal(getattr(position, "margin_allocated", 0) or 0),
                "maintenance_margin": to_decimal(getattr(position, "maintenance_margin", 0) or 0),
                "margin_ratio": (
                    None
                    if getattr(position, "margin_ratio", None) in {None, ""}
                    else to_decimal(getattr(position, "margin_ratio"))
                ),
                "liquidation_price": (
                    None
                    if getattr(position, "liquidation_price", None) in {None, ""}
                    else to_decimal(getattr(position, "liquidation_price"))
                ),
                "margin_source": str(getattr(position, "margin_source", "estimated") or "estimated"),
            }
        return rows

    @staticmethod
    def _exchange_position_margin_map(exchange_snapshot: ExchangeAccountSnapshot) -> dict[str, dict[str, object]]:
        position_mode = StateComparator._exchange_position_mode(exchange_snapshot)
        rows: dict[str, dict[str, object]] = {}
        for position in exchange_snapshot.positions:
            quantity = signed_quantity_for_position_side(
                position.quantity,
                pos_side=getattr(position, "side", None),
                position_mode=position_mode,
            )
            if abs(quantity) <= EPSILON_DECIMAL_12:
                continue
            rows[position_key_for_exchange_position(position, position_mode=position_mode)] = {
                "margin_mode": getattr(position, "margin_mode", None),
                "margin_allocated": (
                    None
                    if getattr(position, "margin_allocated", None) in {None, ""}
                    else to_decimal(getattr(position, "margin_allocated"))
                ),
                "maintenance_margin": (
                    None
                    if getattr(position, "maintenance_margin", None) in {None, ""}
                    else to_decimal(getattr(position, "maintenance_margin"))
                ),
                "margin_ratio": (
                    None
                    if getattr(position, "margin_ratio", None) in {None, ""}
                    else to_decimal(getattr(position, "margin_ratio"))
                ),
                "liquidation_price": (
                    None
                    if getattr(position, "liquidation_price", None) in {None, ""}
                    else to_decimal(getattr(position, "liquidation_price"))
                ),
            }
        return rows

    @staticmethod
    def _position_margin_metric_mismatches(
        local: dict[str, object],
        exchange: dict[str, object],
    ) -> dict[str, dict[str, Decimal | None]]:
        mismatches: dict[str, dict[str, Decimal | None]] = {}
        for field in ("margin_allocated", "maintenance_margin", "margin_ratio", "liquidation_price"):
            local_value = None if local.get(field) in {None, ""} else to_decimal(local.get(field))
            exchange_value = None if exchange.get(field) in {None, ""} else to_decimal(exchange.get(field))
            if local_value is None and exchange_value is None:
                continue
            if (
                local_value is None
                or exchange_value is None
                or abs(local_value - exchange_value) > EPSILON_DECIMAL_9
            ):
                mismatches[field] = {"stored": local_value, "exchange": exchange_value}
        return mismatches

    @staticmethod
    def _exchange_position_quantity_map(exchange_snapshot: ExchangeAccountSnapshot) -> dict[str, Decimal]:
        position_mode = StateComparator._exchange_position_mode(exchange_snapshot)
        quantities: dict[str, Decimal] = {}
        for position in exchange_snapshot.positions:
            quantity = signed_quantity_for_position_side(
                position.quantity,
                pos_side=getattr(position, "side", None),
                position_mode=position_mode,
            )
            if abs(quantity) <= EPSILON_DECIMAL_12:
                continue
            quantities[position_key_for_exchange_position(position, position_mode=position_mode)] = quantity
        return quantities

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
        exchange_margin_diff = bool(position_diff.get("exchange_margin_mismatches"))
        exchange_margin_profile_diff = bool(position_diff.get("exchange_margin_mode_mismatches"))
        exchange_order_diff = bool(order_diff.get("exchange"))
        local_execution_diff = bool(order_diff.get("reconstructed")) or bool(fill_diff.get("replayed"))
        local_portfolio_diff = bool(balance_diff.get("reconstructed")) or bool(
            position_diff.get("reconstructed_mismatches")
        )

        if (
            not has_local_execution_state
            and (
                exchange_fill_diff
                or exchange_balance_diff
                or exchange_position_diff
                or exchange_margin_diff
                or exchange_margin_profile_diff
            )
            and not trusted_exchange_portfolio_baseline
        ):
            return ["historical_state_only"]
        if (
            (
                unexpected_exchange_fills
                or exchange_balance_diff
                or exchange_position_diff
            )
            and not exchange_order_diff
            and not local_execution_diff
            and not local_portfolio_diff
        ):
            categories.append("external_manual_activity_detected")
        if (
            exchange_bills_summary.get("available")
            and (
                unexpected_exchange_fills
                or exchange_balance_diff
                or exchange_position_diff
                or exchange_margin_diff
                or exchange_margin_profile_diff
                or exchange_order_diff
            )
        ):
            categories.append("exchange_bills_activity_available")

        if fill_diff.get("replayed"):
            categories.append("local_fill_missing")
        if balance_diff.get("exchange"):
            categories.append("local_balance_divergence")
        if position_diff.get("exchange_mismatches"):
            categories.append("local_position_divergence")
        if position_diff.get("exchange_margin_mismatches"):
            categories.append("local_position_margin_divergence")
        if position_diff.get("exchange_margin_mode_mismatches"):
            categories.append("local_position_margin_profile_divergence")
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
        stored_positions = StateComparator._position_quantity_map(stored_snapshot.positions)
        replayed_positions = StateComparator._position_quantity_map(reconstructed_snapshot.positions)
        stored_margin = StateComparator._snapshot_position_margin_map(stored_snapshot.positions)
        stored_exchange_positions = StateComparator._position_quantity_map(
            [position for position in stored_snapshot.positions if position.product_type == "derivatives"]
        )
        stored_exchange_margin = StateComparator._snapshot_position_margin_map(
            [position for position in stored_snapshot.positions if position.product_type == "derivatives"]
        )
        reconstructed_mismatches: dict[str, dict[str, Decimal]] = {}
        for symbol in sorted(set(stored_positions) | set(replayed_positions)):
            stored = to_decimal(stored_positions.get(symbol, 0))
            replayed = to_decimal(replayed_positions.get(symbol, 0))
            if abs(stored - replayed) > EPSILON_DECIMAL_12:
                reconstructed_mismatches[symbol] = {"stored": stored, "reconstructed": replayed}

        exchange_positions: dict[str, Decimal] = {}
        exchange_mismatches: dict[str, dict[str, Decimal]] = {}
        exchange_margin: dict[str, dict[str, object]] = {}
        exchange_margin_mismatches: dict[str, dict[str, dict[str, Decimal | None]]] = {}
        exchange_margin_mode_mismatches: dict[str, dict[str, str | None]] = {}
        if compare_exchange_portfolio and exchange_snapshot is not None and exchange_snapshot.positions:
            exchange_positions = StateComparator._exchange_position_quantity_map(exchange_snapshot)
            exchange_margin = StateComparator._exchange_position_margin_map(exchange_snapshot)
            for symbol in sorted(set(stored_exchange_positions) | set(exchange_positions)):
                stored = to_decimal(stored_exchange_positions.get(symbol, 0))
                exchange = to_decimal(exchange_positions.get(symbol, 0))
                if abs(stored - exchange) > EPSILON_DECIMAL_12:
                    exchange_mismatches[symbol] = {"stored": stored, "exchange": exchange}
            for position_key in sorted(set(stored_exchange_margin) | set(exchange_margin)):
                local_margin = stored_exchange_margin.get(position_key)
                exchange_margin_row = exchange_margin.get(position_key)
                if local_margin is None or exchange_margin_row is None:
                    continue
                local_margin_mode = str(local_margin.get("margin_mode") or "").strip().lower() or None
                exchange_margin_mode = str(exchange_margin_row.get("margin_mode") or "").strip().lower() or None
                if (
                    local_margin_mode is not None
                    and exchange_margin_mode is not None
                    and local_margin_mode != exchange_margin_mode
                ):
                    exchange_margin_mode_mismatches[position_key] = {
                        "stored": local_margin_mode,
                        "exchange": exchange_margin_mode,
                    }
                if str(local_margin.get("margin_source") or "").lower() != "exchange":
                    continue
                metric_mismatches = StateComparator._position_margin_metric_mismatches(
                    local_margin,
                    exchange_margin_row,
                )
                if metric_mismatches:
                    exchange_margin_mismatches[position_key] = metric_mismatches

        return {
            "stored": stored_positions,
            "reconstructed": replayed_positions,
            "reconstructed_mismatches": reconstructed_mismatches,
            "exchange": exchange_positions,
            "exchange_mismatches": exchange_mismatches,
            "stored_margin": stored_margin,
            "exchange_margin": exchange_margin,
            "exchange_margin_mismatches": exchange_margin_mismatches,
            "exchange_margin_mode_mismatches": exchange_margin_mode_mismatches,
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
        findings: list[ReconciliationFinding],
        mismatch_categories: list[str],
        only_reduce_required: bool = False,
    ) -> str:
        if mismatch_categories == ["historical_state_only"]:
            return "INFO"
        if any(finding.halt_required or finding.severity_class == "halt" for finding in findings):
            return "HARD_MISMATCH"
        if any(finding.review_required or finding.severity_class == "review" for finding in findings):
            return "REVIEW_REQUIRED"
        if only_reduce_required:
            return "SOFT_MISMATCH"
        if not mismatch_categories and not findings:
            return "CLEAN"
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
        if position_diff.get("exchange_margin_mismatches"):
            reasons.append("local_position_margin_differs_from_exchange_position_margin")
        if position_diff.get("exchange_margin_mode_mismatches"):
            reasons.append("local_position_margin_mode_differs_from_exchange_position_margin_mode")
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
        only_reduce_required: bool = False,
    ) -> list[str]:
        impacts: list[str] = []
        if severity == "HARD_MISMATCH":
            impacts.append("portfolio_state_may_be_unsafe_for_trading")
        if severity == "REVIEW_REQUIRED":
            impacts.append("operator_review_required_before_trading")
        if only_reduce_required:
            impacts.append("derivatives_only_reduce_until_position_reconciled")
        if "historical_state_only" in mismatch_categories:
            impacts.append("historical_account_state_is_not_locally_replayed")
        if order_diff.get("exchange"):
            impacts.append("open_order_visibility_is_incomplete")
        if fill_diff.get("exchange"):
            impacts.append("fill_history_visibility_is_incomplete")
        if balance_diff.get("exchange") or position_diff.get("exchange_mismatches"):
            impacts.append("exchange_account_state_differs_from_local_state")
        if position_diff.get("exchange_margin_mismatches"):
            impacts.append("exchange_margin_state_differs_from_local_snapshot")
        if position_diff.get("exchange_margin_mode_mismatches"):
            impacts.append("cross_isolated_margin_mode_is_not_confirmed")
        if "exchange_bills_activity_available" in mismatch_categories and exchange_bills_summary.get("count"):
            impacts.append("review_exchange_bills_before_rebaselining")
        return impacts

    @staticmethod
    def _recommended_operator_action(
        *,
        severity: str,
        mismatch_categories: list[str],
        findings: list[ReconciliationFinding],
        exchange_bills_summary: dict[str, object],
        only_reduce_required: bool = False,
    ) -> str | None:
        if severity == "CLEAN":
            return None
        if severity == "INFO":
            return "observe_only"
        if only_reduce_required:
            return "go_close_position_on_exchange"
        if findings and all(finding.observational for finding in findings):
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
    def _finding_summary(findings: list[ReconciliationFinding]) -> dict[str, object]:
        summary: dict[str, object] = {
            "total_count": len(findings),
            "structural_count": 0,
            "financial_count": 0,
            "observational_count": 0,
            "review_required_count": 0,
            "halt_required_count": 0,
            "blocks_resume_count": 0,
            "severity_counts": {"info": 0, "soft": 0, "review": 0, "halt": 0},
        }
        for finding in findings:
            if finding.structural:
                summary["structural_count"] = int(summary["structural_count"]) + 1
            if finding.financial:
                summary["financial_count"] = int(summary["financial_count"]) + 1
            if finding.observational:
                summary["observational_count"] = int(summary["observational_count"]) + 1
            if finding.review_required:
                summary["review_required_count"] = int(summary["review_required_count"]) + 1
            if finding.halt_required:
                summary["halt_required_count"] = int(summary["halt_required_count"]) + 1
            if finding.blocks_resume:
                summary["blocks_resume_count"] = int(summary["blocks_resume_count"]) + 1
            severity_counts = dict(summary["severity_counts"])
            severity_counts[finding.severity_class] = int(severity_counts.get(finding.severity_class, 0)) + 1
            summary["severity_counts"] = severity_counts
        return summary

    @staticmethod
    def _build_findings(
        *,
        reconciliation_id: str,
        product_type: str | None,
        margin_mode: str | None,
        allowed_symbols: list[str] | None,
        order_states: list[OrderState],
        fills: list[FillEvent],
        order_diff: dict[str, object],
        fill_diff: dict[str, object],
        balance_diff: dict[str, object],
        position_diff: dict[str, object],
        mismatch_categories: list[str],
        mismatch_reasons: list[str],
        exchange_bills_summary: dict[str, object],
        derivatives_assessment: DerivativesUnknownStateAssessment,
    ) -> list[ReconciliationFinding]:
        findings: list[ReconciliationFinding] = []
        primary_symbol = next(iter(allowed_symbols or []), None)
        local_orders_by_key = {
            StateComparator._order_key(order): order
            for order in order_states
            if StateComparator._order_key(order)
        }
        local_fills_by_id = {fill.fill_id: fill for fill in fills}

        def add_finding(
            *,
            layer: str,
            finding_type: str,
            severity_class: str,
            reason_code: str,
            scope_kind: str = "account",
            scope_ref: str | None = None,
            primary_symbol_override: str | None = None,
            structural: bool | None = None,
            financial: bool | None = None,
            observational: bool | None = None,
            review_required: bool | None = None,
            only_reduce_required: bool = False,
            halt_required: bool | None = None,
            blocks_resume: bool | None = None,
            strategy_sleeve_id: str | None = None,
            allocation_id: str | None = None,
            strategy_bundle_id: str | None = None,
            details_json: dict[str, object] | None = None,
        ) -> None:
            layer_name = str(layer)
            finding = ReconciliationFinding(
                reconciliation_id=reconciliation_id,
                scope_kind=scope_kind,  # type: ignore[arg-type]
                scope_ref=scope_ref,
                product_type=product_type,  # type: ignore[arg-type]
                margin_mode=margin_mode,  # type: ignore[arg-type]
                primary_symbol=primary_symbol_override or primary_symbol,
                strategy_sleeve_id=strategy_sleeve_id,
                allocation_id=allocation_id,
                strategy_bundle_id=strategy_bundle_id,
                layer=layer_name,  # type: ignore[arg-type]
                finding_type=finding_type,
                severity_class=severity_class,  # type: ignore[arg-type]
                structural=structural if structural is not None else layer_name == "structural",
                financial=financial if financial is not None else layer_name == "financial",
                observational=observational if observational is not None else layer_name == "observational",
                review_required=(
                    review_required
                    if review_required is not None
                    else severity_class in {"review", "halt"}
                ),
                only_reduce_required=only_reduce_required,
                halt_required=halt_required if halt_required is not None else severity_class == "halt",
                blocks_resume=blocks_resume if blocks_resume is not None else severity_class in {"review", "halt"},
                reason_code=reason_code,
                details_json=details_json or {},
            )
            findings.append(finding)

        if mismatch_categories == ["historical_state_only"]:
            add_finding(
                layer="structural",
                finding_type="historical_state_only",
                severity_class="info",
                reason_code="historical_state_only",
                blocks_resume=False,
            )
            return findings

        for order_key, details in dict(order_diff.get("reconstructed") or {}).items():
            order = local_orders_by_key.get(str(order_key))
            add_finding(
                layer="structural",
                finding_type="local_execution_reconstruction_mismatch",
                severity_class="soft",
                reason_code="local_order_state_differs_from_fill_reconstruction",
                scope_kind="order",
                scope_ref=str(order_key),
                blocks_resume=False,
                strategy_sleeve_id=None if order is None else order.strategy_sleeve_id,
                allocation_id=None if order is None else order.allocation_id,
                strategy_bundle_id=None if order is None else order.strategy_bundle_id,
                details_json=dict(details or {}),
            )
        exchange_order_view = dict(order_diff.get("exchange") or {})
        for order_key in list(exchange_order_view.get("missing_on_exchange") or []):
            order = local_orders_by_key.get(str(order_key))
            add_finding(
                layer="structural",
                finding_type="exchange_open_order_missing",
                severity_class="review",
                reason_code="local_open_orders_diverge_from_exchange_open_orders",
                scope_kind="order",
                scope_ref=str(order_key),
                strategy_sleeve_id=None if order is None else order.strategy_sleeve_id,
                allocation_id=None if order is None else order.allocation_id,
                strategy_bundle_id=None if order is None else order.strategy_bundle_id,
            )
        for order_key in list(exchange_order_view.get("unexpected_on_exchange") or []):
            add_finding(
                layer="structural",
                finding_type="unexpected_exchange_open_order",
                severity_class="review",
                reason_code="local_open_orders_diverge_from_exchange_open_orders",
                scope_kind="order",
                scope_ref=str(order_key),
            )
        for order_key, details in dict(exchange_order_view.get("status_mismatches") or {}).items():
            order = local_orders_by_key.get(str(order_key))
            add_finding(
                layer="structural",
                finding_type="exchange_open_order_status_mismatch",
                severity_class="review",
                reason_code="local_open_orders_diverge_from_exchange_open_orders",
                scope_kind="order",
                scope_ref=str(order_key),
                strategy_sleeve_id=None if order is None else order.strategy_sleeve_id,
                allocation_id=None if order is None else order.allocation_id,
                strategy_bundle_id=None if order is None else order.strategy_bundle_id,
                details_json=dict(details or {}),
            )

        for fill_id, details in dict(fill_diff.get("replayed") or {}).items():
            fill = local_fills_by_id.get(str(fill_id))
            add_finding(
                layer="structural",
                finding_type="orphan_or_incomplete_local_fill",
                severity_class="soft",
                reason_code="orphan_or_incomplete_local_fill_chain_detected",
                scope_kind="fill",
                scope_ref=str(fill_id),
                blocks_resume=False,
                strategy_sleeve_id=None if fill is None else fill.strategy_sleeve_id,
                allocation_id=None if fill is None else fill.allocation_id,
                strategy_bundle_id=None if fill is None else fill.strategy_bundle_id,
                details_json=dict(details or {}),
            )
        exchange_fill_view = dict(fill_diff.get("exchange") or {})
        for fill_id in list(exchange_fill_view.get("missing_on_exchange") or []):
            fill = local_fills_by_id.get(str(fill_id))
            add_finding(
                layer="structural",
                finding_type="local_fill_missing_on_exchange",
                severity_class="soft",
                reason_code="local_exchange_fill_set_diverges_from_exchange_fill_set",
                scope_kind="fill",
                scope_ref=str(fill_id),
                blocks_resume=False,
                strategy_sleeve_id=None if fill is None else fill.strategy_sleeve_id,
                allocation_id=None if fill is None else fill.allocation_id,
                strategy_bundle_id=None if fill is None else fill.strategy_bundle_id,
            )
        for fill_id in list(exchange_fill_view.get("unexpected_on_exchange") or []):
            add_finding(
                layer="structural",
                finding_type="unexpected_exchange_fill",
                severity_class="review",
                reason_code="local_exchange_fill_set_diverges_from_exchange_fill_set",
                scope_kind="fill",
                scope_ref=str(fill_id),
            )

        for currency, details in dict(balance_diff.get("reconstructed") or {}).items():
            add_finding(
                layer="financial",
                finding_type="reconstructed_balance_mismatch",
                severity_class="soft",
                reason_code="stored_balance_differs_from_replayed_balance",
                scope_kind="account",
                scope_ref=str(currency),
                blocks_resume=False,
                details_json=dict(details or {}),
            )
        for currency, details in dict(balance_diff.get("exchange") or {}).items():
            add_finding(
                layer="financial",
                finding_type="exchange_balance_mismatch",
                severity_class="soft",
                reason_code="local_balance_differs_from_exchange_balance",
                scope_kind="account",
                scope_ref=str(currency),
                blocks_resume=False,
                details_json=dict(details or {}),
            )

        for position_key, details in dict(position_diff.get("reconstructed_mismatches") or {}).items():
            add_finding(
                layer="structural",
                finding_type="reconstructed_position_mismatch",
                severity_class="soft",
                reason_code="stored_position_differs_from_replayed_position",
                scope_kind="position",
                scope_ref=str(position_key),
                primary_symbol_override=symbol_from_position_key(str(position_key)),
                blocks_resume=False,
                details_json=dict(details or {}),
            )
        for position_key, details in dict(position_diff.get("exchange_mismatches") or {}).items():
            add_finding(
                layer="structural",
                finding_type="exchange_position_quantity_mismatch",
                severity_class="soft" if derivatives_assessment.only_reduce_required else "review",
                reason_code="local_position_differs_from_exchange_position",
                scope_kind="position",
                scope_ref=str(position_key),
                primary_symbol_override=symbol_from_position_key(str(position_key)),
                blocks_resume=not derivatives_assessment.only_reduce_required,
                details_json=dict(details or {}),
            )
        for position_key, details in dict(position_diff.get("exchange_margin_mismatches") or {}).items():
            add_finding(
                layer="observational",
                finding_type="exchange_position_margin_drift",
                severity_class="soft",
                reason_code="local_position_margin_differs_from_exchange_position_margin",
                scope_kind="position",
                scope_ref=str(position_key),
                primary_symbol_override=symbol_from_position_key(str(position_key)),
                blocks_resume=False,
                details_json=dict(details or {}),
            )
        for position_key, details in dict(position_diff.get("exchange_margin_mode_mismatches") or {}).items():
            add_finding(
                layer="structural",
                finding_type="exchange_position_margin_mode_mismatch",
                severity_class="halt",
                reason_code="local_position_margin_mode_differs_from_exchange_position_margin_mode",
                scope_kind="position",
                scope_ref=str(position_key),
                primary_symbol_override=symbol_from_position_key(str(position_key)),
                details_json=dict(details or {}),
            )

        if "external_manual_activity_detected" in mismatch_categories:
            add_finding(
                layer="structural",
                finding_type="external_manual_activity_detected",
                severity_class="soft" if derivatives_assessment.only_reduce_required else "review",
                reason_code="external_manual_activity_detected",
                blocks_resume=not derivatives_assessment.only_reduce_required,
            )
        if "exchange_bills_activity_available" in mismatch_categories and exchange_bills_summary.get("count"):
            add_finding(
                layer="financial",
                finding_type="exchange_bills_activity_available",
                severity_class="soft",
                reason_code="recent_exchange_bills_may_explain_exchange_side_balance_activity",
                blocks_resume=False,
                details_json=dict(exchange_bills_summary),
            )

        hard_categories = {
            "unsafe_unknown_state": "unsafe_unknown_state",
            "derivatives_order_state_unknown_on_exchange": "derivatives_order_state_unknown_on_exchange",
            "derivatives_fill_observed_not_booked": "derivatives_fill_observed_not_booked",
            "derivatives_position_mode_mismatch": "derivatives_position_mode_mismatch",
            "derivatives_pos_side_mismatch": "derivatives_pos_side_mismatch",
        }
        for category, reason_code in hard_categories.items():
            if category in mismatch_categories:
                add_finding(
                    layer="structural",
                    finding_type=category,
                    severity_class="halt",
                    reason_code=reason_code,
                )
        if "derivatives_exchange_position_without_local_execution_chain" in mismatch_categories:
            add_finding(
                layer="structural",
                finding_type="derivatives_exchange_position_without_local_execution_chain",
                severity_class="soft",
                reason_code="derivatives_exchange_position_not_replayed_locally",
                only_reduce_required=derivatives_assessment.only_reduce_required,
                blocks_resume=False,
            )

        for detail in derivatives_assessment.unknown_state_details:
            kind = str(detail.get("kind") or "unknown_state_detail")
            if any(
                finding.finding_type == kind and finding.scope_ref == str(detail.get("order_key") or detail.get("position_key") or "")
                for finding in findings
            ):
                continue
            soft_only_reduce_kind = kind == "exchange_position_without_local_execution_chain"
            add_finding(
                layer="structural",
                finding_type=kind,
                severity_class="soft" if soft_only_reduce_kind else "halt",
                reason_code=kind,
                scope_kind="position" if "position" in kind else "order",
                scope_ref=str(detail.get("order_key") or detail.get("position_key") or detail.get("symbol") or ""),
                primary_symbol_override=str(detail.get("symbol") or primary_symbol or ""),
                only_reduce_required=derivatives_assessment.only_reduce_required,
                blocks_resume=not soft_only_reduce_kind,
                details_json={str(key): value for key, value in detail.items()},
            )

        if not findings and mismatch_reasons:
            for reason in mismatch_reasons:
                add_finding(
                    layer="observational",
                    finding_type="generic_mismatch_reason",
                    severity_class="soft",
                    reason_code=reason,
                    blocks_resume=False,
                )
        return findings

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
