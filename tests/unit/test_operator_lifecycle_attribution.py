from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from aats.schemas.audit import DecisionAuditRecord
from aats.schemas.portfolio import FillOutcomeRecord
from aats.services.operator.lifecycle_attribution import LifecycleAttributionFacade


class _FakeOwner:
    def __init__(self) -> None:
        now = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
        self._now = now
        self._payloads = {
            "ctx_open": {
                "symbol": "BTC-USDT-SWAP",
                "timeframe": "15m",
                "as_of_ts": now - timedelta(minutes=21),
                "market_last_price": Decimal("100"),
                "leg_strategy_health": {
                    "long": {
                        "recent_fee_drag_ratio": 0.2,
                        "recent_guard_eligible_fee_drag_ratio": 0.1,
                        "recent_churn_ratio": 0.3,
                        "recent_guard_eligible_churn_ratio": 0.15,
                        "recent_low_edge_trade_streak": 1,
                        "recent_guard_eligible_low_edge_trade_streak": 1,
                    }
                },
            },
            "target_open": {
                "strategy_family": "independent",
                "strategy_family_action": "open_independent_book",
                "book_runtime_states": [
                    {
                        "leg": "long",
                        "current_qty": Decimal("0"),
                        "target_qty": Decimal("2"),
                        "book_state": "opening",
                        "book_action": "open",
                        "execution_chain_id": "independent:decision_open:long:open",
                    }
                ],
                "book_expectancy_summary": {
                    "books": [
                        {
                            "leg": "long",
                            "expected_signal_edge_bps": 18.0,
                            "expected_cost_bps": 6.0,
                            "expected_net_edge_bps": 12.0,
                        }
                    ]
                },
            },
            "ctx_failed": {
                "symbol": "BTC-USDT-SWAP",
                "timeframe": "15m",
                "as_of_ts": now - timedelta(minutes=11),
                "market_last_price": Decimal("102"),
                "leg_strategy_health": {
                    "long": {
                        "recent_fee_drag_ratio": 0.25,
                        "recent_guard_eligible_fee_drag_ratio": 0.18,
                        "recent_churn_ratio": 0.35,
                        "recent_guard_eligible_churn_ratio": 0.22,
                        "recent_low_edge_trade_streak": 2,
                        "recent_guard_eligible_low_edge_trade_streak": 2,
                    }
                },
            },
            "target_failed": {
                "strategy_family": "independent",
                "strategy_family_action": "close_failed_thesis_independent_book",
                "book_runtime_states": [
                    {
                        "leg": "long",
                        "current_qty": Decimal("2"),
                        "target_qty": Decimal("1"),
                        "book_state": "closing",
                        "book_action": "close_failed_thesis",
                        "close_reason": "failed_thesis",
                        "policy_reason": "independent_failed_thesis_force_exit",
                        "execution_chain_id": "independent:decision_failed:long:close_failed_thesis",
                    }
                ],
                "book_expectancy_summary": {
                    "books": [
                        {
                            "leg": "long",
                            "expected_signal_edge_bps": 3.0,
                            "expected_cost_bps": 6.0,
                            "expected_net_edge_bps": -1.0,
                        }
                    ]
                },
            },
            "ctx_health": {
                "symbol": "BTC-USDT-SWAP",
                "timeframe": "15m",
                "as_of_ts": now - timedelta(minutes=5),
                "market_last_price": Decimal("101"),
                "leg_strategy_health": {
                    "long": {
                        "recent_fee_drag_ratio": 0.9,
                        "recent_guard_eligible_fee_drag_ratio": 0.7,
                        "recent_churn_ratio": 0.8,
                        "recent_guard_eligible_churn_ratio": 0.65,
                        "recent_low_edge_trade_streak": 3,
                        "recent_guard_eligible_low_edge_trade_streak": 2,
                    }
                },
            },
            "target_health": {
                "strategy_family": "independent",
                "strategy_family_action": "de_risk_independent_book",
                "book_runtime_states": [
                    {
                        "leg": "long",
                        "current_qty": Decimal("1"),
                        "target_qty": Decimal("0"),
                        "book_state": "de_risking",
                        "book_action": "de_risk",
                        "close_reason": "execution_health_degraded",
                        "policy_reason": "independent_execution_health_urgent_exit",
                        "execution_chain_id": "independent:decision_health:long:de_risk:execution_health_degraded",
                    }
                ],
                "book_expectancy_summary": {
                    "books": [
                        {
                            "leg": "long",
                            "expected_signal_edge_bps": 1.5,
                            "expected_cost_bps": 6.0,
                            "expected_net_edge_bps": -2.5,
                        }
                    ]
                },
            },
            "ctx_shadow": {
                "symbol": "BTC-USDT-SWAP",
                "timeframe": "15m",
                "as_of_ts": now - timedelta(minutes=6),
                "market_last_price": Decimal("101"),
                "leg_strategy_health": {
                    "long": {
                        "recent_fee_drag_ratio": 0.4,
                        "recent_guard_eligible_fee_drag_ratio": 0.25,
                        "recent_churn_ratio": 0.45,
                        "recent_guard_eligible_churn_ratio": 0.25,
                        "recent_low_edge_trade_streak": 1,
                        "recent_guard_eligible_low_edge_trade_streak": 1,
                    }
                },
            },
            "target_shadow": {
                "strategy_family": "independent",
                "strategy_family_action": "de_risk_independent_book",
                "book_runtime_states": [
                    {
                        "leg": "long",
                        "current_qty": Decimal("1"),
                        "target_qty": Decimal("0.5"),
                        "book_state": "de_risking",
                        "book_action": "de_risk",
                        "close_reason": "weak_edge_de_risk",
                        "policy_reason": "independent_weak_edge_guarded_reduce",
                        "execution_chain_id": "independent:decision_shadow:long:de_risk:weak_edge",
                    }
                ],
                "book_expectancy_summary": {
                    "books": [
                        {
                            "leg": "long",
                            "expected_signal_edge_bps": 4.0,
                            "expected_cost_bps": 6.0,
                            "expected_net_edge_bps": -0.5,
                        }
                    ]
                },
            },
        }
        self._outcomes = [
            FillOutcomeRecord(
                fill_id="fill_open",
                decision_id="decision_open",
                execution_chain_id="independent:decision_open:long:open",
                order_id="order_open",
                symbol="BTC-USDT-SWAP",
                position_key="BTC-USDT-SWAP:long",
                side="buy",
                fill_qty=Decimal("2"),
                fill_price=Decimal("100"),
                fill_notional=Decimal("200"),
                fee_delta=Decimal("-0.10"),
                position_intent="open_long",
                execution_action="open_long",
                pos_side="long",
                starting_position_qty=Decimal("0"),
                ending_position_qty=Decimal("2"),
                realized_pnl_delta=Decimal("0"),
                strategy_family="independent",
                ingestion_timestamp=now - timedelta(minutes=20),
                created_at=now - timedelta(minutes=20),
            ),
            FillOutcomeRecord(
                fill_id="fill_reduce",
                decision_id="decision_failed",
                execution_chain_id="independent:decision_failed:long:close_failed_thesis",
                order_id="order_reduce",
                symbol="BTC-USDT-SWAP",
                position_key="BTC-USDT-SWAP:long",
                side="sell",
                fill_qty=Decimal("1"),
                fill_price=Decimal("102"),
                fill_notional=Decimal("102"),
                fee_delta=Decimal("-0.05"),
                position_intent="reduce_long",
                execution_action="reduce_long",
                pos_side="long",
                starting_position_qty=Decimal("2"),
                ending_position_qty=Decimal("1"),
                realized_pnl_delta=Decimal("1.20"),
                strategy_family="independent",
                ingestion_timestamp=now - timedelta(minutes=10),
                created_at=now - timedelta(minutes=10),
            ),
            FillOutcomeRecord(
                fill_id="fill_close",
                decision_id="decision_health",
                execution_chain_id="independent:decision_health:long:de_risk:execution_health_degraded",
                order_id="order_close",
                symbol="BTC-USDT-SWAP",
                position_key="BTC-USDT-SWAP:long",
                side="sell",
                fill_qty=Decimal("1"),
                fill_price=Decimal("101"),
                fill_notional=Decimal("101"),
                fee_delta=Decimal("-0.07"),
                position_intent="close_long",
                execution_action="close_long",
                pos_side="long",
                starting_position_qty=Decimal("1"),
                ending_position_qty=Decimal("0"),
                realized_pnl_delta=Decimal("0.80"),
                strategy_family="independent",
                ingestion_timestamp=now - timedelta(minutes=4),
                created_at=now - timedelta(minutes=4),
            ),
        ]
        self._lifecycle = {
            "lifecycle_id": "lifecycle:BTC-USDT-SWAP:fill_open",
            "symbol": "BTC-USDT-SWAP",
            "position_key": "BTC-USDT-SWAP:long",
            "pos_side": "long",
            "status": "closed",
            "opened_at": now - timedelta(minutes=20),
            "closed_at": now - timedelta(minutes=4),
            "fill_count": 3,
            "fill_ids": ["fill_open", "fill_reduce", "fill_close"],
            "funding_fee_event_count": 0,
            "funding_fee_bill_ids": [],
            "trading_net_realized_pnl": Decimal("2.0"),
            "funding_fee_total": Decimal("-0.05"),
            "combined_net_realized_pnl": Decimal("1.95"),
        }
        self.runtime = SimpleNamespace(
            audit_repo=SimpleNamespace(
                all=lambda: [
                    DecisionAuditRecord(
                        decision_id="decision_open",
                        decision_context_ref="ctx_open",
                        position_target_ref="target_open",
                    ),
                    DecisionAuditRecord(
                        decision_id="decision_failed",
                        decision_context_ref="ctx_failed",
                        position_target_ref="target_failed",
                    ),
                    DecisionAuditRecord(
                        decision_id="decision_health",
                        decision_context_ref="ctx_health",
                        position_target_ref="target_health",
                    ),
                    DecisionAuditRecord(
                        decision_id="decision_shadow",
                        decision_context_ref="ctx_shadow",
                        position_target_ref="target_shadow",
                    ),
                ]
            )
        )
        self.state_scope = SimpleNamespace(product_type="derivatives", margin_mode="cross", allowed_symbols=("BTC-USDT-SWAP",))

    def _scope_cache_fragment(self) -> str:
        return "unit"

    def _cached_ttl(self, _key: str, _ttl: int, loader):
        return loader()

    def _scoped_fill_outcomes(self):
        return list(self._outcomes)

    def _scoped_funding_fee_records(self):
        return []

    def _build_position_lifecycle_rows(self, *, outcomes, funding_records):
        return [dict(self._lifecycle)], []

    def payload_by_ref(self, ref: str | None):
        return None if ref is None else self._payloads.get(ref)

    def payloads_by_ref_map(self, refs):
        return {ref: self._payloads[ref] for ref in refs if ref in self._payloads}

    def _position_target_payload(self, payload):
        return payload

    def _resolved_position_target_payload(self, **kwargs):
        return kwargs.get("position_target") or kwargs.get("finalized_decision_outcome")

    def _risk_decision_payload(self, payload):
        return payload

    def _book_runtime_states_from_payload(self, payload):
        return [] if payload is None else list(payload.get("book_runtime_states") or [])

    def _book_expectancy_summary_from_payload(self, payload):
        return None if payload is None else payload.get("book_expectancy_summary")

    def _fill_outcome_event_timestamp(self, record):
        return record.ingestion_timestamp or record.created_at

    def _funding_fee_event_timestamp(self, record):
        return getattr(record, "bill_ts", None)

    def _fee_cost_in_quote(self, record):
        return abs(self._to_decimal(getattr(record, "fee_delta", None)) or Decimal("0"))

    def _as_datetime(self, value):
        if isinstance(value, datetime):
            return value
        return None

    @staticmethod
    def _to_decimal(value):
        if isinstance(value, Decimal):
            return value
        if value is None:
            return None
        return Decimal(str(value))


class TestLifecycleAttributionFacade(unittest.TestCase):
    def test_position_lifecycle_attribution_enriches_fee_split_and_exit_trace(self) -> None:
        owner = _FakeOwner()
        facade = LifecycleAttributionFacade(owner)

        payload = facade.position_lifecycle_attribution(limit=5)
        lifecycle = payload["lifecycles"][0]
        detail = facade.position_lifecycle_attribution_detail(lifecycle_id=lifecycle["lifecycle_id"])

        self.assertEqual(lifecycle["family"], "independent")
        self.assertEqual(lifecycle["timeframe"], "15m")
        self.assertEqual(lifecycle["direction"], "long")
        self.assertEqual(lifecycle["entry_fill_count"], 1)
        self.assertEqual(lifecycle["exit_fill_count"], 2)
        self.assertEqual(lifecycle["child_order_count"], 3)
        self.assertEqual(Decimal(str(lifecycle["entry_fee_quote"])), Decimal("0.10"))
        self.assertEqual(Decimal(str(lifecycle["exit_fee_quote"])), Decimal("0.12"))
        self.assertEqual(Decimal(str(lifecycle["total_fee_quote"])), Decimal("0.22"))
        self.assertEqual(Decimal(str(lifecycle["gross_realized_pnl"])), Decimal("2.12"))
        self.assertEqual(Decimal(str(lifecycle["combined_net_realized_pnl"])), Decimal("1.95"))
        self.assertEqual(lifecycle["trace_completeness"], "partial")
        self.assertEqual(lifecycle["unmatched_actionable_decision_count"], 1)
        self.assertEqual(lifecycle["exit_reason_breakdown"][0]["reason"], "failed_thesis")
        self.assertEqual(lifecycle["exit_reason_breakdown"][1]["transition_category"], "execution_guard_exit")
        self.assertEqual([item["intent"] for item in lifecycle["exit_intent_breakdown"]], ["reduce_long", "close_long"])

        trace = detail["decision_trace"]
        self.assertEqual([item["decision_id"] for item in trace], ["decision_open", "decision_failed", "decision_health"])
        self.assertEqual(detail["trace_completeness"], "partial")
        self.assertEqual(detail["unmatched_actionable_decision_count"], 1)
        self.assertEqual([item["decision_id"] for item in detail["candidate_decisions"]], ["decision_shadow"])
        candidate = detail["candidate_decisions"][0]
        self.assertEqual(candidate["transition_category"], "protective_exit")
        self.assertEqual(candidate["expected_cost_bps"], 6.0)
        self.assertEqual(candidate["expected_lifecycle_cost_bps"], 6.0)
        self.assertEqual(candidate["fee_drag_ratio"], 0.4)
        self.assertEqual(candidate["guard_eligible_fee_drag_ratio"], 0.25)
        self.assertEqual(candidate["position_qty_before"], Decimal("1"))
        self.assertEqual(candidate["position_qty_after"], Decimal("0.5"))
        self.assertEqual(candidate["execution_chain_id"], "independent:decision_shadow:long:de_risk:weak_edge")
        self.assertIsNone(trace[0]["transition_category"])
        self.assertEqual(trace[1]["transition_category"], "strategy_exit")
        self.assertEqual(trace[2]["transition_category"], "execution_guard_exit")
        self.assertEqual(Decimal(str(trace[1]["close_notional_quote"])), Decimal("102"))
        self.assertEqual(Decimal(str(trace[2]["residual_notional_quote"])), Decimal("0"))

    def test_decision_row_enrichment_batches_payload_reference_lookups(self) -> None:
        owner = _FakeOwner()
        batch_refs: list[str] = []
        owner.payload_by_ref = lambda _ref: (_ for _ in ()).throw(
            AssertionError("decision enrichment should use batched payload lookup")
        )

        def payloads_by_ref_map(refs):
            batch_refs.extend(refs)
            return {ref: owner._payloads[ref] for ref in refs if ref in owner._payloads}

        owner.payloads_by_ref_map = payloads_by_ref_map
        facade = LifecycleAttributionFacade(owner)

        payload = facade.position_lifecycle_attribution(limit=5)

        self.assertEqual(payload["lifecycles"][0]["decision_trace_count"], 3)
        self.assertIn("ctx_open", batch_refs)
        self.assertIn("target_health", batch_refs)

    def test_transition_category_maps_protective_and_guard_paths(self) -> None:
        self.assertEqual(
            LifecycleAttributionFacade.transition_category(
                close_reason="weak_edge_de_risk",
                book_action="de_risk",
                family_action="de_risk_independent_book",
                policy_reason="independent_weak_edge_guarded_reduce",
            ),
            "protective_exit",
        )
        self.assertEqual(
            LifecycleAttributionFacade.transition_category(
                close_reason="execution_health_degraded",
                book_action="de_risk",
                family_action="de_risk_independent_book",
                policy_reason="independent_execution_health_urgent_exit",
            ),
            "execution_guard_exit",
        )

    def test_position_lifecycle_attribution_marks_missing_linked_evidence_when_audits_are_absent(self) -> None:
        owner = _FakeOwner()
        owner.runtime = SimpleNamespace(audit_repo=SimpleNamespace(all=lambda: []))
        facade = LifecycleAttributionFacade(owner)

        payload = facade.position_lifecycle_attribution(limit=5)
        lifecycle = payload["lifecycles"][0]
        detail = facade.position_lifecycle_attribution_detail(lifecycle_id=lifecycle["lifecycle_id"])

        self.assertEqual(lifecycle["trace_completeness"], "missing_linked_evidence")
        self.assertGreater(lifecycle["missing_linked_reference_count"], 0)
        self.assertEqual(lifecycle["unmatched_actionable_decision_count"], 0)
        self.assertEqual(detail["decision_trace"], [])
        self.assertEqual(detail["candidate_decisions"], [])
        self.assertEqual(detail["trace_completeness"], "missing_linked_evidence")
        self.assertGreater(detail["missing_linked_reference_count"], 0)

    def test_position_lifecycle_attribution_reports_candidate_only_when_only_weak_candidates_remain(self) -> None:
        owner = _FakeOwner()
        owner.runtime = SimpleNamespace(
            audit_repo=SimpleNamespace(
                all=lambda: [
                    DecisionAuditRecord(
                        decision_id="decision_shadow",
                        decision_context_ref="ctx_shadow",
                        position_target_ref="target_shadow",
                    )
                ]
            )
        )
        facade = LifecycleAttributionFacade(owner)

        payload = facade.position_lifecycle_attribution(limit=5)
        lifecycle = payload["lifecycles"][0]
        detail = facade.position_lifecycle_attribution_detail(lifecycle_id=lifecycle["lifecycle_id"])

        self.assertEqual(lifecycle["trace_completeness"], "candidate_only")
        self.assertGreater(lifecycle["missing_linked_reference_count"], 0)
        self.assertEqual(lifecycle["unmatched_actionable_decision_count"], 1)
        self.assertEqual(detail["decision_trace"], [])
        self.assertEqual([item["decision_id"] for item in detail["candidate_decisions"]], ["decision_shadow"])
        self.assertEqual(detail["trace_completeness"], "candidate_only")
        self.assertGreater(detail["missing_linked_reference_count"], 0)

    def test_position_lifecycle_dashboard_uses_bounded_recent_sources(self) -> None:
        owner = _FakeOwner()
        calls: list[tuple[str, int | None]] = []
        audits = owner.runtime.audit_repo.all()

        class FillRepo:
            def outcomes_for_scope(self, *, scope, since=None, limit=None):
                _ = scope
                _ = since
                calls.append(("outcomes", limit))
                return list(owner._outcomes)

        class FundingRepo:
            def records_for_scope(self, *, scope, since=None, limit=None):
                _ = scope
                _ = since
                calls.append(("funding", limit))
                return []

        class AuditRepo:
            def all(self):
                raise AssertionError("dashboard lifecycle list should not load all audits")

            def recent(self, *, limit: int):
                calls.append(("audits", limit))
                return list(audits)

        owner.runtime = SimpleNamespace(
            fill_outcome_repo=FillRepo(),
            funding_fee_repo=FundingRepo(),
            audit_repo=AuditRepo(),
        )
        facade = LifecycleAttributionFacade(owner)

        payload = facade.position_lifecycle_attribution_dashboard(limit=5)

        self.assertEqual(payload["read_scope"], "recent_bounded")
        self.assertEqual(payload["lifecycles"][0]["lifecycle_id"], "lifecycle:BTC-USDT-SWAP:fill_open")
        self.assertIn(("outcomes", 500), calls)
        self.assertIn(("funding", 500), calls)
        self.assertIn(("audits", 1000), calls)

    def test_position_lifecycle_dashboard_skips_audits_when_no_lifecycles_exist(self) -> None:
        owner = _FakeOwner()
        owner._outcomes = []
        owner._build_position_lifecycle_rows = lambda *, outcomes, funding_records: ([], [])
        owner.runtime = SimpleNamespace(
            fill_outcome_repo=SimpleNamespace(outcomes_for_scope=lambda *, scope, limit: []),
            funding_fee_repo=SimpleNamespace(records_for_scope=lambda *, scope, limit: []),
            audit_repo=SimpleNamespace(
                recent=lambda *, limit: (_ for _ in ()).throw(
                    AssertionError("empty dashboard lifecycle list should not hydrate audits")
                )
            ),
        )
        facade = LifecycleAttributionFacade(owner)

        payload = facade.position_lifecycle_attribution_dashboard(limit=5)

        self.assertEqual(payload["read_scope"], "recent_bounded")
        self.assertEqual(payload["lifecycles"], [])
        self.assertFalse(payload["has_more"])


if __name__ == "__main__":
    unittest.main()
