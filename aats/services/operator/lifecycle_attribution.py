from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from aats.services.operator._parallel import parallel_fetch

if TYPE_CHECKING:
    from aats.services.operator.query_service import OperatorQueryService


class LifecycleAttributionFacade:
    _EPSILON = Decimal("1e-12")
    _DASHBOARD_FETCH_MIN_ROWS = 500
    _DASHBOARD_FETCH_MULTIPLIER = 50
    _DASHBOARD_AUDIT_MIN_ROWS = 1000
    _DASHBOARD_AUDIT_MULTIPLIER = 125
    _TRACEABLE_BOOK_ACTIONS = {
        "open",
        "scale_in",
        "de_risk",
        "close_failed_thesis",
        "close_stale_thesis",
        "close",
    }

    def __init__(self, owner: "OperatorQueryService") -> None:
        self.owner = owner

    def position_lifecycle_profitability(self, *, limit: int = 100) -> dict[str, Any]:
        return self._list_payload(limit=limit)

    def position_lifecycle_attribution(self, *, limit: int = 100) -> dict[str, Any]:
        return self._list_payload(limit=limit)

    def position_lifecycle_attribution_dashboard(self, *, limit: int = 100) -> dict[str, Any]:
        return self._list_payload(limit=limit, dashboard_recent=True)

    def position_lifecycle_attribution_detail(self, *, lifecycle_id: str) -> dict[str, Any]:
        compiled = self._compiled_payload()
        detail = compiled["details_by_id"].get(lifecycle_id)
        if detail is None:
            raise KeyError(f"lifecycle_not_found:{lifecycle_id}")
        return detail

    def _list_payload(self, *, limit: int, dashboard_recent: bool = False) -> dict[str, Any]:
        normalized_limit = max(int(limit), 1)
        compiled = self._compiled_payload(limit=normalized_limit if dashboard_recent else None)
        visible_rows = compiled["lifecycles"][:normalized_limit]
        has_more = len(compiled["lifecycles"]) > normalized_limit or bool(compiled.get("source_window_exhausted"))
        return {
            "summary": dict(compiled["summary"]),
            "lifecycles": visible_rows,
            "unassigned_funding_fees": compiled["unassigned_funding_fees"][:normalized_limit],
            "has_more": has_more,
            "truth_source": "fill_outcomes_plus_funding_fee_records_plus_decision_audits",
            "read_scope": compiled.get("read_scope", "full_history"),
        }

    def _compiled_payload(self, *, limit: int | None = None) -> dict[str, Any]:
        normalized_limit = None if limit is None else max(int(limit), 1)
        cache_key = f"lifecycle_attribution:{self.owner._scope_cache_fragment()}:limit={normalized_limit or 'full'}"
        return self.owner._cached_ttl(
            cache_key,
            20,
            lambda: self._build_compiled_payload(limit=normalized_limit),
        )

    def _build_compiled_payload(self, *, limit: int | None = None) -> dict[str, Any]:
        row_limit = None
        audit_limit = None
        if limit is not None:
            row_limit = max(limit * self._DASHBOARD_FETCH_MULTIPLIER, self._DASHBOARD_FETCH_MIN_ROWS)
            audit_limit = max(limit * self._DASHBOARD_AUDIT_MULTIPLIER, self._DASHBOARD_AUDIT_MIN_ROWS)
        results = parallel_fetch(
            {
                "outcomes": lambda: self._load_outcomes(limit=row_limit),
                "funding_records": lambda: self._load_funding_records(limit=row_limit),
                "audits": lambda: self._load_audits(limit=audit_limit),
            }
        )
        outcomes = list(results["outcomes"])
        funding_records = list(results["funding_records"])
        audits = list(results["audits"])
        base_lifecycles, unassigned_funding_fees = self.owner._build_position_lifecycle_rows(
            outcomes=outcomes,
            funding_records=funding_records,
        )
        base_lifecycles.sort(
            key=lambda item: (
                item.get("closed_at") or item.get("opened_at") or datetime.min.replace(tzinfo=timezone.utc),
                str(item.get("lifecycle_id") or ""),
            ),
            reverse=True,
        )
        outcomes_by_fill_id = {
            str(getattr(item, "fill_id", "") or ""): item
            for item in outcomes
            if str(getattr(item, "fill_id", "") or "").strip()
        }
        funding_by_bill_id = {
            str(getattr(item, "bill_id", "") or ""): item
            for item in funding_records
            if str(getattr(item, "bill_id", "") or "").strip()
        }
        decision_rows_by_symbol = self._decision_rows_by_symbol(audits)

        lifecycles: list[dict[str, Any]] = []
        details_by_id: dict[str, dict[str, Any]] = {}
        for lifecycle in base_lifecycles:
            summary_row, detail_payload = self._build_lifecycle_payload(
                lifecycle=lifecycle,
                outcomes_by_fill_id=outcomes_by_fill_id,
                funding_by_bill_id=funding_by_bill_id,
                decision_rows=decision_rows_by_symbol.get(str(lifecycle.get("symbol") or ""), []),
            )
            lifecycles.append(summary_row)
            details_by_id[str(summary_row.get("lifecycle_id") or "")] = detail_payload

        trading_net = sum(
            (self.owner._to_decimal(item.get("trading_net_realized_pnl")) or Decimal("0"))
            for item in base_lifecycles
        )
        funding_net = sum(
            (self.owner._to_decimal(item.get("funding_fee_total")) or Decimal("0"))
            for item in base_lifecycles
        )
        unassigned_funding_net = sum(
            (self.owner._to_decimal(item.get("funding_fee_delta")) or Decimal("0"))
            for item in unassigned_funding_fees
        )
        gross_realized = sum(
            (self.owner._to_decimal(item.get("gross_realized_pnl")) or Decimal("0"))
            for item in lifecycles
        )
        entry_fee_total = sum(
            (self.owner._to_decimal(item.get("entry_fee_quote")) or Decimal("0"))
            for item in lifecycles
        )
        exit_fee_total = sum(
            (self.owner._to_decimal(item.get("exit_fee_quote")) or Decimal("0"))
            for item in lifecycles
        )
        total_fee = sum(
            (self.owner._to_decimal(item.get("total_fee_quote")) or Decimal("0"))
            for item in lifecycles
        )
        summary = {
            "lifecycle_count": len(base_lifecycles),
            "closed_lifecycle_count": sum(1 for item in base_lifecycles if item.get("status") == "closed"),
            "open_lifecycle_count": sum(1 for item in base_lifecycles if item.get("status") != "closed"),
            "assigned_funding_fee_count": sum(int(item.get("funding_fee_event_count") or 0) for item in base_lifecycles),
            "unassigned_funding_fee_count": len(unassigned_funding_fees),
            "gross_realized_pnl": gross_realized,
            "trading_net_realized_pnl": trading_net,
            "net_realized_pnl": trading_net,
            "entry_fee_quote": entry_fee_total,
            "exit_fee_quote": exit_fee_total,
            "total_fee_quote": total_fee,
            "assigned_funding_fee_net_pnl": funding_net,
            "funding_fee_quote": funding_net,
            "unassigned_funding_fee_net_pnl": unassigned_funding_net,
            "combined_net_realized_pnl": trading_net + funding_net + unassigned_funding_net,
        }
        return {
            "summary": summary,
            "lifecycles": lifecycles,
            "details_by_id": details_by_id,
            "unassigned_funding_fees": unassigned_funding_fees,
            "read_scope": "recent_bounded" if limit is not None else "full_history",
            "source_window_exhausted": (
                limit is not None
                and (
                    (row_limit is not None and len(outcomes) >= row_limit)
                    or (row_limit is not None and len(funding_records) >= row_limit)
                    or (audit_limit is not None and len(audits) >= audit_limit)
                )
            ),
        }

    def _load_outcomes(self, *, limit: int | None) -> list[Any]:
        repo = getattr(self.owner.runtime, "fill_outcome_repo", None)
        if repo is not None and hasattr(repo, "outcomes_for_scope"):
            return list(repo.outcomes_for_scope(scope=self.owner.state_scope, limit=limit))
        return list(self.owner._scoped_fill_outcomes())[-limit:] if limit is not None else list(self.owner._scoped_fill_outcomes())

    def _load_funding_records(self, *, limit: int | None) -> list[Any]:
        repo = getattr(self.owner.runtime, "funding_fee_repo", None)
        if repo is not None and hasattr(repo, "records_for_scope"):
            return list(repo.records_for_scope(scope=self.owner.state_scope, limit=limit))
        rows = list(self.owner._scoped_funding_fee_records())
        return rows[-limit:] if limit is not None else rows

    def _load_audits(self, *, limit: int | None = None) -> list[Any]:
        if limit is not None:
            recent = getattr(self.owner.runtime.audit_repo, "recent", None)
            if callable(recent):
                return list(recent(limit=limit))
        loader = getattr(self.owner.runtime.audit_repo, "all", None)
        if callable(loader):
            return list(loader())
        count = int(getattr(self.owner.runtime.audit_repo, "count", lambda: 0)() or 0)
        recent = getattr(self.owner.runtime.audit_repo, "recent", None)
        if callable(recent):
            return list(recent(limit=max(count, 1)))
        return []

    def _decision_rows_by_symbol(self, audits: list[Any]) -> dict[str, list[dict[str, Any]]]:
        refs: list[str] = []
        for audit in audits:
            refs.extend(
                [
                    getattr(audit, "decision_context_ref", None),
                    getattr(audit, "position_target_ref", None),
                    getattr(audit, "decision_outcome_ref", None),
                    getattr(audit, "policy_decision_ref", None),
                    getattr(audit, "risk_decision_ref", None),
                ]
            )
        payloads_by_ref = self.owner.payloads_by_ref_map(refs)

        rows_by_symbol: dict[str, list[dict[str, Any]]] = {}
        for audit in audits:
            context = payloads_by_ref.get(str(getattr(audit, "decision_context_ref", "") or ""))
            if not isinstance(context, dict):
                continue
            symbol = str(context.get("symbol") or "").strip()
            if not symbol:
                continue
            decision_id = str(getattr(audit, "decision_id", "") or "").strip()
            position_target = self.owner._position_target_payload(
                payloads_by_ref.get(str(getattr(audit, "position_target_ref", "") or ""))
            )
            finalized_outcome = payloads_by_ref.get(str(getattr(audit, "decision_outcome_ref", "") or ""))
            policy_decision = payloads_by_ref.get(str(getattr(audit, "policy_decision_ref", "") or ""))
            risk_decision = self.owner._risk_decision_payload(
                payloads_by_ref.get(str(getattr(audit, "risk_decision_ref", "") or ""))
            )
            resolved_target = self.owner._resolved_position_target_payload(
                finalized_decision_outcome=finalized_outcome,
                position_target=position_target,
                policy_decision=policy_decision,
                risk_decision=risk_decision,
            )
            source_payload = resolved_target if isinstance(resolved_target, dict) else finalized_outcome
            runtime_states = self.owner._book_runtime_states_from_payload(source_payload)
            if not runtime_states:
                continue
            expectancy_summary = self.owner._book_expectancy_summary_from_payload(source_payload) or {}
            books_by_leg = {
                str(item.get("leg") or "").strip().lower(): dict(item)
                for item in list(expectancy_summary.get("books") or [])
                if isinstance(item, dict)
            }
            family = None
            if isinstance(resolved_target, dict):
                family = resolved_target.get("strategy_family")
            if family is None and isinstance(finalized_outcome, dict):
                family = finalized_outcome.get("selected_strategy_family")
            family_action = None
            if isinstance(resolved_target, dict):
                family_action = resolved_target.get("strategy_family_action")
            if family_action is None and isinstance(finalized_outcome, dict):
                family_action = finalized_outcome.get("selected_strategy_family_action")
            timestamp = self.owner._as_datetime(context.get("as_of_ts")) or getattr(audit, "created_at", None)
            for state in runtime_states:
                leg = str(state.get("leg") or "").strip().lower()
                if leg not in {"long", "short"}:
                    continue
                book = books_by_leg.get(leg, {})
                rows_by_symbol.setdefault(symbol, []).append(
                    {
                        "decision_id": decision_id,
                        "symbol": symbol,
                        "timeframe": context.get("timeframe"),
                        "timestamp": timestamp,
                        "family": family,
                        "family_action": family_action,
                        "leg": leg,
                        "current_qty": self.owner._to_decimal(state.get("current_qty")) or Decimal("0"),
                        "target_qty": self.owner._to_decimal(state.get("target_qty")) or Decimal("0"),
                        "book_state": state.get("book_state"),
                        "book_action": state.get("book_action"),
                        "close_reason": state.get("close_reason"),
                        "execution_chain_id": state.get("execution_chain_id"),
                        "policy_reason": state.get("policy_reason"),
                        "expected_signal_edge_bps": (
                            book.get("expected_signal_edge_bps")
                            if book.get("expected_signal_edge_bps") is not None
                            else state.get("expected_signal_edge_bps")
                        ),
                        "expected_cost_bps": (
                            book.get("expected_cost_bps")
                            if book.get("expected_cost_bps") is not None
                            else state.get("expected_cost_bps")
                        ),
                        "expected_lifecycle_cost_bps": (
                            book.get("expected_lifecycle_cost_bps")
                            if book.get("expected_lifecycle_cost_bps") is not None
                            else (
                                book.get("expected_cost_bps")
                                if book.get("expected_cost_bps") is not None
                                else state.get("expected_cost_bps")
                            )
                        ),
                        "expected_net_edge_bps": (
                            book.get("expected_net_edge_bps")
                            if book.get("expected_net_edge_bps") is not None
                            else state.get("expected_net_edge_bps")
                        ),
                        "expected_lifecycle_net_edge_bps": (
                            book.get("expected_lifecycle_net_edge_bps")
                            if book.get("expected_lifecycle_net_edge_bps") is not None
                            else (
                                book.get("expected_net_edge_bps")
                                if book.get("expected_net_edge_bps") is not None
                                else state.get("expected_net_edge_bps")
                            )
                        ),
                        "liquidity_quality_score": (
                            book.get("liquidity_quality_score")
                            if book.get("liquidity_quality_score") is not None
                            else state.get("liquidity_quality_score")
                        ),
                        "execution_health_state": (
                            book.get("execution_health_state")
                            if book.get("execution_health_state") is not None
                            else state.get("execution_health_state")
                        ),
                        "market_last_price": self.owner._to_decimal(context.get("market_last_price")) or Decimal("0"),
                        "leg_health": dict((context.get("leg_strategy_health") or {}).get(leg) or {}),
                        "actionable": self._is_traceable_runtime_state(state),
                    }
                )
        for rows in rows_by_symbol.values():
            rows.sort(
                key=lambda item: (
                    item.get("timestamp") or datetime.min.replace(tzinfo=timezone.utc),
                    str(item.get("decision_id") or ""),
                    str(item.get("leg") or ""),
                )
            )
        return rows_by_symbol

    def _build_lifecycle_payload(
        self,
        *,
        lifecycle: dict[str, Any],
        outcomes_by_fill_id: dict[str, Any],
        funding_by_bill_id: dict[str, Any],
        decision_rows: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        fills = [
            outcomes_by_fill_id[str(fill_id)]
            for fill_id in list(lifecycle.get("fill_ids") or [])
            if str(fill_id) in outcomes_by_fill_id
        ]
        fills.sort(
            key=lambda item: (
                self.owner._fill_outcome_event_timestamp(item) or datetime.min.replace(tzinfo=timezone.utc),
                str(getattr(item, "fill_id", "") or ""),
            )
        )
        direction = self._lifecycle_direction(lifecycle=lifecycle, fills=fills)
        child_fills: list[dict[str, Any]] = []
        fill_decision_ids: set[str] = set()
        fill_chain_ids: set[str] = set()
        families: list[str] = []
        timeframes: list[str] = []
        child_order_ids: set[str] = set()
        child_execution_ids: set[str] = set()
        entry_fill_count = 0
        exit_fill_count = 0
        entry_notional_quote = Decimal("0")
        exit_notional_quote = Decimal("0")
        entry_fee_quote = Decimal("0")
        exit_fee_quote = Decimal("0")
        gross_realized_pnl = Decimal("0")

        for outcome in fills:
            fill_id = str(getattr(outcome, "fill_id", "") or "")
            decision_id = str(getattr(outcome, "decision_id", "") or "").strip()
            execution_chain_id = str(getattr(outcome, "execution_chain_id", "") or "").strip()
            if decision_id:
                fill_decision_ids.add(decision_id)
            if execution_chain_id:
                fill_chain_ids.add(execution_chain_id)
            family = str(getattr(outcome, "strategy_family", "") or "").strip()
            if family:
                families.append(family)
            order_id = str(getattr(outcome, "order_id", "") or "").strip()
            if order_id:
                child_order_ids.add(order_id)
            elif fill_id:
                child_order_ids.add(fill_id)
            execution_attempt_id = str(getattr(outcome, "execution_attempt_id", "") or "").strip()
            if execution_attempt_id:
                child_execution_ids.add(execution_attempt_id)
            elif execution_chain_id:
                child_execution_ids.add(execution_chain_id)

            fill_bucket = self._fill_bucket(outcome)
            fee_quote = self.owner._fee_cost_in_quote(outcome) or Decimal("0")
            fill_notional_quote = abs(self.owner._to_decimal(getattr(outcome, "fill_notional", None)) or Decimal("0"))
            if fill_notional_quote <= self._EPSILON:
                fill_qty = abs(self.owner._to_decimal(getattr(outcome, "fill_qty", None)) or Decimal("0"))
                fill_price = abs(self.owner._to_decimal(getattr(outcome, "fill_price", None)) or Decimal("0"))
                fill_notional_quote = fill_qty * fill_price
            realized_pnl_delta = self.owner._to_decimal(getattr(outcome, "realized_pnl_delta", None)) or Decimal("0")
            explicit_gross = self.owner._to_decimal(getattr(outcome, "gross_realized_pnl", None))
            gross_fill_realized = (
                explicit_gross
                if explicit_gross is not None
                else realized_pnl_delta + fee_quote
                if fill_bucket == "exit"
                else realized_pnl_delta
            )
            timestamp = self.owner._fill_outcome_event_timestamp(outcome)
            if fill_bucket == "entry":
                entry_fill_count += 1
                entry_notional_quote += fill_notional_quote
                entry_fee_quote += fee_quote
            elif fill_bucket == "exit":
                exit_fill_count += 1
                exit_notional_quote += fill_notional_quote
                exit_fee_quote += fee_quote
                gross_realized_pnl += gross_fill_realized
            child_fills.append(
                {
                    "fill_id": fill_id,
                    "decision_id": decision_id or None,
                    "execution_chain_id": execution_chain_id or None,
                    "execution_attempt_id": execution_attempt_id or None,
                    "order_id": order_id or None,
                    "timestamp": timestamp,
                    "fill_bucket": fill_bucket,
                    "side": getattr(outcome, "side", None),
                    "position_intent": getattr(outcome, "position_intent", None),
                    "execution_action": getattr(outcome, "execution_action", None),
                    "liquidity_role": getattr(outcome, "liquidity_role", None),
                    "fill_qty": getattr(outcome, "fill_qty", None),
                    "fill_price": getattr(outcome, "fill_price", None),
                    "fill_notional_quote": fill_notional_quote,
                    "fee_quote": fee_quote,
                    "realized_pnl_delta": realized_pnl_delta,
                    "gross_realized_pnl": gross_fill_realized,
                    "starting_position_qty": getattr(outcome, "starting_position_qty", None),
                    "ending_position_qty": getattr(outcome, "ending_position_qty", None),
                }
            )

        match_result = self._match_decision_rows(
            lifecycle=lifecycle,
            decision_rows=decision_rows,
            direction=direction,
            fill_decision_ids=fill_decision_ids,
            fill_chain_ids=fill_chain_ids,
        )
        matched_decisions = list(match_result["strong_matches"])
        candidate_decisions = [self._candidate_decision_row(item) for item in match_result["candidate_matches"]]
        decision_trace = [self._decision_trace_row(item) for item in matched_decisions]
        for row in matched_decisions:
            timeframe = str(row.get("timeframe") or "").strip()
            if timeframe:
                timeframes.append(timeframe)
            family = str(row.get("family") or "").strip()
            if family:
                families.append(family)
        exit_reason_breakdown = self._exit_reason_breakdown(decision_trace=decision_trace)
        exit_intent_breakdown = self._exit_intent_breakdown(child_fills=child_fills)
        hold_seconds = self._hold_seconds(
            opened_at=self.owner._as_datetime(lifecycle.get("opened_at")),
            closed_at=self.owner._as_datetime(lifecycle.get("closed_at")),
        )
        total_fee_quote = entry_fee_quote + exit_fee_quote
        net_realized_pnl = self.owner._to_decimal(lifecycle.get("trading_net_realized_pnl")) or Decimal("0")
        funding_fee_quote = self.owner._to_decimal(lifecycle.get("funding_fee_total")) or Decimal("0")
        combined_net_realized_pnl = self.owner._to_decimal(lifecycle.get("combined_net_realized_pnl")) or Decimal("0")
        gross_to_net_capture_ratio = (
            None
            if abs(gross_realized_pnl) <= self._EPSILON
            else combined_net_realized_pnl / gross_realized_pnl
        )
        child_order_count = len(child_order_ids) if child_order_ids else int(lifecycle.get("fill_count") or 0)
        summary_row = {
            **dict(lifecycle),
            "family": self._first_unique(families),
            "timeframe": self._first_unique(timeframes),
            "direction": direction,
            "hold_seconds": hold_seconds,
            "entry_fill_count": entry_fill_count,
            "exit_fill_count": exit_fill_count,
            "child_order_count": child_order_count,
            "entry_notional_total": entry_notional_quote,
            "exit_notional_total": exit_notional_quote,
            "entry_notional_quote": entry_notional_quote,
            "exit_notional_quote": exit_notional_quote,
            "trading_gross_realized_pnl": gross_realized_pnl,
            "gross_realized_pnl": gross_realized_pnl,
            "entry_fee_quote": entry_fee_quote,
            "exit_fee_quote": exit_fee_quote,
            "fee_total": total_fee_quote,
            "total_fee_quote": total_fee_quote,
            "funding_fee_quote": funding_fee_quote,
            "net_realized_pnl": net_realized_pnl,
            "combined_net_realized_pnl": combined_net_realized_pnl,
            "gross_to_net_capture_ratio": gross_to_net_capture_ratio,
            "exit_reason_breakdown": exit_reason_breakdown,
            "exit_intent_breakdown": exit_intent_breakdown,
            "decision_trace_count": len(decision_trace),
            "candidate_decision_count": len(candidate_decisions),
            "trace_completeness": match_result["trace_completeness"],
            "unmatched_actionable_decision_count": match_result["unmatched_actionable_decision_count"],
            "missing_linked_reference_count": match_result["missing_linked_reference_count"],
            "child_execution_count": len(child_execution_ids),
            "detail_available": True,
        }
        detail_payload = {
            "lifecycle_id": summary_row.get("lifecycle_id"),
            "summary": summary_row,
            "child_fills": child_fills,
            "decision_trace": decision_trace,
            "candidate_decisions": candidate_decisions,
            "trace_completeness": match_result["trace_completeness"],
            "unmatched_actionable_decision_count": match_result["unmatched_actionable_decision_count"],
            "missing_linked_reference_count": match_result["missing_linked_reference_count"],
            "exit_reason_breakdown": exit_reason_breakdown,
            "exit_intent_breakdown": exit_intent_breakdown,
            "key_metrics_timeline": self._key_metrics_timeline(
                decision_trace=decision_trace,
                child_fills=child_fills,
                lifecycle=lifecycle,
                funding_by_bill_id=funding_by_bill_id,
            ),
            "truth_source": "fill_outcomes_plus_funding_fee_records_plus_decision_audits",
        }
        return summary_row, detail_payload

    def _match_decision_rows(
        self,
        *,
        lifecycle: dict[str, Any],
        decision_rows: list[dict[str, Any]],
        direction: str | None,
        fill_decision_ids: set[str],
        fill_chain_ids: set[str],
    ) -> dict[str, Any]:
        symbol = str(lifecycle.get("symbol") or "").strip()
        opened_at = self.owner._as_datetime(lifecycle.get("opened_at"))
        closed_at = self.owner._as_datetime(lifecycle.get("closed_at"))
        matched: dict[tuple[str, str, str], dict[str, Any]] = {}
        candidates: dict[tuple[str, str, str], dict[str, Any]] = {}
        matched_decision_ids: set[str] = set()
        matched_chain_ids: set[str] = set()
        for row in decision_rows:
            if str(row.get("symbol") or "").strip() != symbol:
                continue
            leg = str(row.get("leg") or "").strip().lower()
            if direction in {"long", "short"} and leg != direction:
                continue
            decision_id = str(row.get("decision_id") or "").strip()
            chain_id = str(row.get("execution_chain_id") or "").strip()
            timestamp = self.owner._as_datetime(row.get("timestamp"))
            in_lifecycle_window = False
            if opened_at is not None and timestamp is not None:
                if closed_at is None:
                    in_lifecycle_window = timestamp >= opened_at
                else:
                    in_lifecycle_window = opened_at <= timestamp <= closed_at
            key = (
                decision_id or chain_id or str(timestamp or ""),
                chain_id,
                leg,
            )
            matches_decision = bool(decision_id and decision_id in fill_decision_ids)
            matches_chain = bool(chain_id and chain_id in fill_chain_ids)
            if matches_decision or matches_chain:
                match_source = (
                    "decision_id_and_execution_chain_id"
                    if matches_decision and matches_chain
                    else "decision_id"
                    if matches_decision
                    else "execution_chain_id"
                )
                matched[key] = {
                    **row,
                    "match_source": match_source,
                }
                if matches_decision:
                    matched_decision_ids.add(decision_id)
                if matches_chain:
                    matched_chain_ids.add(chain_id)
                candidates.pop(key, None)
                continue
            if bool(row.get("actionable")) and in_lifecycle_window:
                candidates[key] = {
                    **row,
                    "match_source": "window_actionable_candidate",
                }
        ordered = sorted(
            matched.values(),
            key=lambda item: (
                item.get("timestamp") or datetime.min.replace(tzinfo=timezone.utc),
                str(item.get("decision_id") or ""),
                str(item.get("leg") or ""),
            ),
        )
        ordered_candidates = sorted(
            candidates.values(),
            key=lambda item: (
                item.get("timestamp") or datetime.min.replace(tzinfo=timezone.utc),
                str(item.get("decision_id") or ""),
                str(item.get("leg") or ""),
            ),
        )
        unmatched_count = len(ordered_candidates)
        missing_linked_reference_count = len(fill_decision_ids - matched_decision_ids) + len(fill_chain_ids - matched_chain_ids)
        trace_completeness = self._trace_completeness(
            strong_match_count=len(ordered),
            unmatched_actionable_decision_count=unmatched_count,
            missing_linked_reference_count=missing_linked_reference_count,
        )
        return {
            "strong_matches": ordered,
            "candidate_matches": ordered_candidates,
            "trace_completeness": trace_completeness,
            "unmatched_actionable_decision_count": unmatched_count,
            "missing_linked_reference_count": missing_linked_reference_count,
        }

    def _decision_trace_row(self, row: dict[str, Any]) -> dict[str, Any]:
        current_qty = self.owner._to_decimal(row.get("current_qty")) or Decimal("0")
        target_qty = self.owner._to_decimal(row.get("target_qty")) or Decimal("0")
        market_last_price = self.owner._to_decimal(row.get("market_last_price")) or Decimal("0")
        close_qty = max(abs(current_qty) - abs(target_qty), Decimal("0"))
        close_notional_quote = close_qty * market_last_price
        residual_notional_quote = abs(target_qty) * market_last_price
        leg_health = dict(row.get("leg_health") or {})
        recent_fee_drag = leg_health.get("recent_fee_drag_ratio")
        guard_fee_drag = leg_health.get("recent_guard_eligible_fee_drag_ratio")
        recent_churn = leg_health.get("recent_churn_ratio")
        guard_churn = leg_health.get("recent_guard_eligible_churn_ratio")
        recent_low_edge = leg_health.get("recent_low_edge_trade_streak")
        guard_low_edge = leg_health.get("recent_guard_eligible_low_edge_trade_streak")
        expected_cost_bps = row.get("expected_cost_bps")
        expected_lifecycle_cost_bps = row.get("expected_lifecycle_cost_bps")
        expected_net_edge_bps = row.get("expected_net_edge_bps")
        expected_lifecycle_net_edge_bps = row.get("expected_lifecycle_net_edge_bps")
        fallback_scope = (
            "lifecycle"
            if expected_lifecycle_cost_bps != expected_cost_bps or expected_lifecycle_net_edge_bps != expected_net_edge_bps
            else "decision_fallback"
        )
        return {
            "decision_id": row.get("decision_id"),
            "timestamp": row.get("timestamp"),
            "match_source": row.get("match_source"),
            "book_state": row.get("book_state"),
            "book_action": row.get("book_action"),
            "close_reason": row.get("close_reason"),
            "transition_category": self.transition_category(
                close_reason=row.get("close_reason"),
                book_action=row.get("book_action"),
                family_action=row.get("family_action"),
                policy_reason=row.get("policy_reason"),
            ),
            "expected_signal_edge_bps": row.get("expected_signal_edge_bps"),
            "expected_cost_bps": expected_cost_bps,
            "expected_lifecycle_cost_bps": expected_lifecycle_cost_bps,
            "expected_net_edge_bps": expected_net_edge_bps,
            "expected_lifecycle_net_edge_bps": expected_lifecycle_net_edge_bps,
            "expectancy_scope": fallback_scope,
            "liquidity_quality_score": row.get("liquidity_quality_score"),
            "execution_health_state": row.get("execution_health_state"),
            "fee_drag_ratio": recent_fee_drag,
            "guard_eligible_fee_drag_ratio": guard_fee_drag if guard_fee_drag is not None else recent_fee_drag,
            "churn_ratio": recent_churn,
            "guard_eligible_churn_ratio": guard_churn if guard_churn is not None else recent_churn,
            "low_edge_streak": recent_low_edge,
            "guard_eligible_low_edge_streak": guard_low_edge if guard_low_edge is not None else recent_low_edge,
            "position_qty_before": current_qty,
            "position_qty_after": target_qty,
            "close_notional_quote": close_notional_quote,
            "residual_notional_quote": residual_notional_quote,
        }

    def _candidate_decision_row(self, row: dict[str, Any]) -> dict[str, Any]:
        candidate = self._decision_trace_row(row)
        candidate.update(
            {
                "family": row.get("family"),
                "timeframe": row.get("timeframe"),
                "execution_chain_id": row.get("execution_chain_id"),
            }
        )
        return candidate

    @staticmethod
    def _trace_completeness(
        *,
        strong_match_count: int,
        unmatched_actionable_decision_count: int,
        missing_linked_reference_count: int,
    ) -> str:
        if strong_match_count <= 0:
            if unmatched_actionable_decision_count > 0:
                return "candidate_only"
            if missing_linked_reference_count > 0:
                return "missing_linked_evidence"
            return "complete"
        if missing_linked_reference_count > 0:
            return "partial"
        if unmatched_actionable_decision_count <= 0:
            return "complete"
        return "partial"

    def _key_metrics_timeline(
        self,
        *,
        decision_trace: list[dict[str, Any]],
        child_fills: list[dict[str, Any]],
        lifecycle: dict[str, Any],
        funding_by_bill_id: dict[str, Any],
    ) -> list[dict[str, Any]]:
        timeline: list[dict[str, Any]] = []
        for row in decision_trace:
            timeline.append(
                {
                    "timestamp": row.get("timestamp"),
                    "event_type": "decision",
                    "decision_id": row.get("decision_id"),
                    "book_action": row.get("book_action"),
                    "close_reason": row.get("close_reason"),
                    "transition_category": row.get("transition_category"),
                    "expected_net_edge_bps": row.get("expected_lifecycle_net_edge_bps"),
                    "execution_health_state": row.get("execution_health_state"),
                    "position_qty_before": row.get("position_qty_before"),
                    "position_qty_after": row.get("position_qty_after"),
                }
            )
        for row in child_fills:
            timeline.append(
                {
                    "timestamp": row.get("timestamp"),
                    "event_type": "fill",
                    "fill_id": row.get("fill_id"),
                    "fill_bucket": row.get("fill_bucket"),
                    "position_intent": row.get("position_intent"),
                    "fill_notional_quote": row.get("fill_notional_quote"),
                    "fee_quote": row.get("fee_quote"),
                    "realized_pnl_delta": row.get("realized_pnl_delta"),
                    "gross_realized_pnl": row.get("gross_realized_pnl"),
                }
            )
        for bill_id in list(lifecycle.get("funding_fee_bill_ids") or []):
            record = funding_by_bill_id.get(str(bill_id))
            if record is None:
                continue
            timeline.append(
                {
                    "timestamp": self.owner._funding_fee_event_timestamp(record),
                    "event_type": "funding_fee",
                    "bill_id": getattr(record, "bill_id", None),
                    "amount": self.owner._to_decimal(getattr(record, "amount", None)),
                    "direction": getattr(record, "funding_direction", None),
                }
            )
        timeline.sort(
            key=lambda item: (
                item.get("timestamp") or datetime.min.replace(tzinfo=timezone.utc),
                self._timeline_priority(item.get("event_type")),
                str(item.get("decision_id") or item.get("fill_id") or item.get("bill_id") or ""),
            )
        )
        return timeline

    @classmethod
    def transition_category(
        cls,
        *,
        close_reason: Any,
        book_action: Any,
        family_action: Any,
        policy_reason: Any,
    ) -> str | None:
        normalized_reason = str(close_reason or "").strip().lower()
        normalized_action = str(book_action or "").strip().lower()
        normalized_family_action = str(family_action or "").strip().lower()
        normalized_policy_reason = str(policy_reason or "").strip().lower()
        if normalized_reason in {"execution_health_degraded", "liquidity_degraded"}:
            return "execution_guard_exit"
        if normalized_reason in {"failed_thesis", "stale_thesis"}:
            return "strategy_exit"
        if normalized_reason == "weak_edge_de_risk":
            return "protective_exit"
        if normalized_action in {"close_failed_thesis", "close_stale_thesis", "close"}:
            return "strategy_exit"
        if normalized_action == "de_risk":
            if normalized_policy_reason.startswith("independent_execution_health_"):
                return "execution_guard_exit"
            if normalized_policy_reason.startswith("independent_liquidity_degraded_"):
                return "execution_guard_exit"
            return "protective_exit"
        if normalized_family_action == "de_risk_independent_book":
            return "protective_exit"
        if normalized_family_action in {
            "close_failed_thesis_independent_book",
            "close_stale_thesis_independent_book",
        }:
            return "strategy_exit"
        return None

    @classmethod
    def _fill_bucket(cls, outcome: Any) -> str:
        starting_qty = cls._to_decimal_static(getattr(outcome, "starting_position_qty", None)) or Decimal("0")
        ending_qty = cls._to_decimal_static(getattr(outcome, "ending_position_qty", None)) or Decimal("0")
        start_sign = 1 if starting_qty > cls._EPSILON else -1 if starting_qty < -cls._EPSILON else 0
        end_sign = 1 if ending_qty > cls._EPSILON else -1 if ending_qty < -cls._EPSILON else 0
        if start_sign != 0 and end_sign != 0 and start_sign != end_sign:
            return "exit"
        if abs(starting_qty) <= cls._EPSILON and abs(ending_qty) > cls._EPSILON:
            return "entry"
        if abs(starting_qty) > cls._EPSILON and abs(ending_qty) <= cls._EPSILON:
            return "exit"
        if abs(ending_qty) > abs(starting_qty) + cls._EPSILON:
            return "entry"
        if abs(ending_qty) + cls._EPSILON < abs(starting_qty):
            return "exit"
        position_intent = str(getattr(outcome, "position_intent", "") or "").strip().lower()
        if position_intent.startswith(("open_", "scale_in_")):
            return "entry"
        if position_intent.startswith(("reduce_", "close_", "reverse_")):
            return "exit"
        return "adjustment"

    @classmethod
    def _lifecycle_direction(cls, *, lifecycle: dict[str, Any], fills: list[Any]) -> str | None:
        pos_side = str(lifecycle.get("pos_side") or "").strip().lower()
        if pos_side in {"long", "short"}:
            return pos_side
        position_key = str(lifecycle.get("position_key") or "").strip().lower()
        if position_key.endswith(":long"):
            return "long"
        if position_key.endswith(":short"):
            return "short"
        for outcome in fills:
            fill_pos_side = str(getattr(outcome, "pos_side", "") or "").strip().lower()
            if fill_pos_side in {"long", "short"}:
                return fill_pos_side
            exposure_side = str(getattr(outcome, "exposure_side", "") or "").strip().lower()
            if exposure_side in {"long", "short"}:
                return exposure_side
            for candidate in (
                cls._to_decimal_static(getattr(outcome, "starting_position_qty", None)),
                cls._to_decimal_static(getattr(outcome, "ending_position_qty", None)),
            ):
                if candidate is None or abs(candidate) <= cls._EPSILON:
                    continue
                return "long" if candidate > 0 else "short"
        return None

    @classmethod
    def _exit_reason_breakdown(cls, *, decision_trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
        buckets: dict[tuple[str, str | None], dict[str, Any]] = {}
        for row in decision_trace:
            reason = str(row.get("close_reason") or "").strip().lower()
            if not reason:
                continue
            category = row.get("transition_category")
            key = (reason, category)
            bucket = buckets.setdefault(
                key,
                {
                    "reason": reason,
                    "transition_category": category,
                    "decision_count": 0,
                    "close_notional_quote": Decimal("0"),
                },
            )
            bucket["decision_count"] += 1
            bucket["close_notional_quote"] += cls._to_decimal_static(row.get("close_notional_quote")) or Decimal("0")
        payload = list(buckets.values())
        payload.sort(
            key=lambda item: (
                cls._to_decimal_static(item.get("close_notional_quote")) or Decimal("0"),
                int(item.get("decision_count") or 0),
                str(item.get("reason") or ""),
            ),
            reverse=True,
        )
        return payload

    @classmethod
    def _exit_intent_breakdown(cls, *, child_fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
        buckets: dict[str, dict[str, Any]] = {}
        for row in child_fills:
            if row.get("fill_bucket") != "exit":
                continue
            intent = str(row.get("position_intent") or row.get("execution_action") or "").strip().lower() or "unknown"
            bucket = buckets.setdefault(
                intent,
                {
                    "intent": intent,
                    "fill_count": 0,
                    "exit_notional_quote": Decimal("0"),
                },
            )
            bucket["fill_count"] += 1
            bucket["exit_notional_quote"] += cls._to_decimal_static(row.get("fill_notional_quote")) or Decimal("0")
        payload = list(buckets.values())
        payload.sort(
            key=lambda item: (
                cls._to_decimal_static(item.get("exit_notional_quote")) or Decimal("0"),
                int(item.get("fill_count") or 0),
                str(item.get("intent") or ""),
            ),
            reverse=True,
        )
        return payload

    @classmethod
    def _timeline_priority(cls, event_type: Any) -> int:
        normalized = str(event_type or "").strip().lower()
        if normalized == "decision":
            return 0
        if normalized == "fill":
            return 1
        if normalized == "funding_fee":
            return 2
        return 9

    @classmethod
    def _is_traceable_runtime_state(cls, state: dict[str, Any]) -> bool:
        book_action = str(state.get("book_action") or "").strip().lower()
        if book_action in cls._TRACEABLE_BOOK_ACTIONS:
            return True
        if str(state.get("close_reason") or "").strip():
            return True
        current_qty = cls._to_decimal_static(state.get("current_qty")) or Decimal("0")
        target_qty = cls._to_decimal_static(state.get("target_qty")) or Decimal("0")
        return abs(current_qty - target_qty) > cls._EPSILON

    @classmethod
    def _first_unique(cls, values: list[str]) -> str | None:
        ordered = list(dict.fromkeys(item for item in values if str(item or "").strip()))
        if not ordered:
            return None
        return ordered[0]

    @classmethod
    def _hold_seconds(cls, *, opened_at: datetime | None, closed_at: datetime | None) -> float | None:
        if opened_at is None:
            return None
        effective_closed_at = closed_at or datetime.now(timezone.utc)
        if effective_closed_at < opened_at:
            return 0.0
        return round((effective_closed_at - opened_at).total_seconds(), 6)

    @staticmethod
    def _to_decimal_static(value: Any) -> Decimal | None:
        if isinstance(value, Decimal):
            return value
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except Exception:
            return None
