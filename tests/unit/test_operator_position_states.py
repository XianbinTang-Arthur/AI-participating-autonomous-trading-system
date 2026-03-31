from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from aats.schemas.exchange import ExchangeAccountConfiguration, ExchangeAccountSnapshot, ExchangePosition
from aats.schemas.portfolio import FillOutcomeRecord, PortfolioSnapshot, Position
from aats.services.operator.query_service import OperatorQueryService


class TestOperatorPositionStates(unittest.TestCase):
    def test_strategy_runtime_operator_summary_uses_overlay_close_copy(self) -> None:
        self.assertEqual(
            OperatorQueryService._strategy_runtime_operator_summary(
                latest_snapshot_present=True,
                route_action="override_target",
                family_action="close_protection_leg",
            ),
            "当前选中的策略家族正在收回保护腿。",
        )
        self.assertEqual(
            OperatorQueryService._strategy_runtime_operator_summary(
                latest_snapshot_present=True,
                route_action="override_target",
                family_action="close_opportunity_leg",
            ),
            "当前选中的策略家族正在收回机会腿。",
        )
        self.assertEqual(
            OperatorQueryService._strategy_runtime_operator_summary(
                latest_snapshot_present=True,
                route_action="override_target",
                family_action="de_risk_independent_book",
            ),
            "当前选中的策略家族正在降低独立双书风险暴露。",
        )
        self.assertEqual(
            OperatorQueryService._strategy_runtime_operator_summary(
                latest_snapshot_present=True,
                route_action="override_target",
                family_action="close_failed_thesis_independent_book",
            ),
            "当前选中的策略家族正在按 thesis 失效关闭独立双书。",
        )
        self.assertEqual(
            OperatorQueryService._strategy_runtime_operator_summary(
                latest_snapshot_present=True,
                route_action="override_target",
                family_action="close_stale_thesis_independent_book",
            ),
            "当前选中的策略家族正在按 thesis 过期关闭独立双书。",
        )

    def test_smart_arbitrage_runtime_pair_configuration_prefers_snapshot_candidate_metrics(self) -> None:
        query = OperatorQueryService.__new__(OperatorQueryService)
        query.runtime = SimpleNamespace(settings=SimpleNamespace(default_symbol="BTC-USDT-SWAP"))

        latest_snapshot = {
            "candidates": [
                {
                    "family": "smart_arbitrage",
                    "metrics": {
                        "pair_definitions": [
                            {
                                "pair_id": "btc_usdt_swap",
                                "spot_symbol": "BTC-USDT",
                                "hedge_symbol": "BTC-USDT-SWAP",
                                "metadata": {
                                    "configuration_warning_codes": ["pair_warn"],
                                    "configuration_error_codes": ["pair_err"],
                                },
                            }
                        ],
                        "pair_registry_warning_codes": ["pair_warn"],
                        "pair_registry_error_codes": ["pair_err"],
                        "pair_registry_source": "coordinator_resolved",
                    },
                }
            ]
        }

        with patch(
            "aats.services.operator.query_service.load_pair_definitions",
            side_effect=AssertionError("query_service should reuse coordinator-resolved pair definitions"),
        ):
            pair_definitions, warning_codes, error_codes, source = query._smart_arbitrage_runtime_pair_configuration(
                latest_snapshot=latest_snapshot
            )

        self.assertEqual(len(pair_definitions), 1)
        self.assertEqual(pair_definitions[0]["pair_id"], "btc_usdt_swap")
        self.assertEqual(warning_codes, ["pair_warn"])
        self.assertEqual(error_codes, ["pair_err"])
        self.assertEqual(source, "coordinator_resolved")

    def test_execution_action_summary_prefers_directional_position_intent(self) -> None:
        query = OperatorQueryService.__new__(OperatorQueryService)

        action = query._action_from_execution_fields(
            execution_action="reduce",
            position_intent="reduce_long",
        )

        self.assertEqual(action, "reduce_long")

    def test_abstract_action_from_position_intent_keeps_final_action_semantics(self) -> None:
        self.assertEqual(
            OperatorQueryService._abstract_action_from_position_intent("open_short"),
            "enter",
        )
        self.assertEqual(
            OperatorQueryService._abstract_action_from_position_intent("scale_in_long"),
            "scale_in",
        )

    def test_decision_outcome_payload_prefers_native_outcome_and_backfills_family_summary(self) -> None:
        query = OperatorQueryService.__new__(OperatorQueryService)
        query.runtime = SimpleNamespace(settings=SimpleNamespace(ai_operating_mode="baseline_only"))
        query.strategy_profile_snapshot = lambda: {"activation": {}}  # type: ignore[method-assign]

        payload = query._decision_outcome_payload(
            finalized_decision_outcome={
                "decision_id": "dec-1",
                "symbol": "BTC-USDT-SWAP",
                "decision_source": "baseline",
                "decision_authority": "reference_only",
                "final_action": "enter",
                "final_direction": "short",
            },
            decision_context=None,
            baseline_assessment=None,
            ai_assessment=None,
            position_target={
                "position_intent": "reduce_long",
                "target_exposure_side": "long",
                "family_execution_summary": {
                    "summary_mode": "single_leg",
                    "family": "protective",
                    "route_action": "override_target",
                    "family_action": "protect",
                    "leg_count": 1,
                    "position_intents": ["open_short"],
                    "directions": ["short"],
                    "leg_actions": ["open"],
                    "execution_modes": ["protective_overlay"],
                    "diagnostic_metric_flags": {
                        "emit_expected_vs_realized_metrics": True,
                    },
                    "parent_target_signal": "flat",
                    "parent_current_signal": "long",
                    "parent_effective_signal": "long",
                    "signal_source": "inventory",
                    "book_expectancy_summary": {
                        "source": "independent_book",
                        "books": [
                            {
                                "leg": "long",
                                "expected_gross_edge_bps": 18.0,
                                "expected_signal_edge_bps": 18.0,
                                "expected_slippage_bps": 1.5,
                                "expected_cost_bps": 6.0,
                                "expected_net_edge_bps": 12.0,
                            }
                        ],
                    },
                },
            },
            policy_decision=None,
            risk_decision=None,
        )

        assert payload is not None
        self.assertTrue(payload["finalized"])
        self.assertEqual(payload["final_action"], "enter")
        self.assertEqual(payload["final_direction"], "short")
        self.assertEqual(payload["family_execution_summary"]["position_intents"], ["open_short"])
        self.assertEqual(payload["parent_target_signal"], "flat")
        self.assertEqual(payload["parent_current_signal"], "long")
        self.assertEqual(payload["parent_effective_signal"], "long")
        self.assertEqual(payload["signal_source"], "inventory")
        self.assertEqual(payload["diagnostic_metric_flags"]["emit_expected_vs_realized_metrics"], True)
        self.assertEqual(payload["book_expectancy_summary"]["source"], "independent_book")
        self.assertEqual(
            payload["family_execution_summary"]["book_expectancy_summary"]["books"][0]["expected_net_edge_bps"],
            12.0,
        )
        self.assertEqual(payload["book_expectancy_summary"]["books"][0]["expected_net_edge_bps"], 12.0)
        self.assertEqual(payload["book_runtime_states"], [])

    def test_independent_diagnostics_flags_prefer_persisted_payload_flags(self) -> None:
        query = OperatorQueryService.__new__(OperatorQueryService)
        query.runtime = SimpleNamespace(
            settings=SimpleNamespace(
                strategy_hedge_independent_emit_book_level_metrics=False,
                strategy_hedge_independent_emit_expected_vs_realized_metrics=False,
                strategy_hedge_independent_emit_close_reason_metrics=False,
                strategy_hedge_independent_emit_execution_policy_metrics=False,
            )
        )

        payload = {
            "strategy_family": "independent",
            "family_execution_summary": {
                "family": "independent",
                "diagnostic_metric_flags": {
                    "emit_book_level_metrics": True,
                    "emit_expected_vs_realized_metrics": True,
                    "emit_close_reason_metrics": True,
                    "emit_execution_policy_metrics": True,
                },
            },
        }

        metric_flags = query._independent_diagnostics_flags(payloads=[payload])
        normalized = query._position_target_payload(payload)

        self.assertEqual(
            metric_flags,
            {
                "emit_book_level_metrics": True,
                "emit_expected_vs_realized_metrics": True,
                "emit_close_reason_metrics": True,
                "emit_execution_policy_metrics": True,
            },
        )
        self.assertEqual(normalized["diagnostic_metric_flags"], metric_flags)

    def test_independent_expected_vs_realized_summary_respects_payload_specific_metric_flags(self) -> None:
        query = OperatorQueryService.__new__(OperatorQueryService)
        query.runtime = SimpleNamespace(
            settings=SimpleNamespace(
                strategy_hedge_independent_emit_book_level_metrics=False,
                strategy_hedge_independent_emit_expected_vs_realized_metrics=False,
                strategy_hedge_independent_emit_close_reason_metrics=False,
                strategy_hedge_independent_emit_execution_policy_metrics=False,
            )
        )
        query._recent_independent_target_payloads = lambda **_: [  # type: ignore[method-assign]
            {
                "decision_id": "decision_emit",
                "strategy_family": "independent",
                "family_execution_summary": {
                    "family": "independent",
                    "diagnostic_metric_flags": {
                        "emit_book_level_metrics": False,
                        "emit_expected_vs_realized_metrics": True,
                        "emit_close_reason_metrics": False,
                        "emit_execution_policy_metrics": True,
                    },
                    "book_runtime_states": [
                        {
                            "leg": "long",
                            "book_action": "open",
                            "close_reason": None,
                        }
                    ],
                    "book_expectancy_summary": {
                        "source": "independent_book",
                        "books": [
                            {
                                "leg": "long",
                                "expected_net_edge_bps": 8.0,
                                "weak_edge_report_only": True,
                                "passive_first_required": True,
                            }
                        ],
                    },
                },
            },
            {
                "decision_id": "decision_close_reason_only",
                "strategy_family": "independent",
                "family_execution_summary": {
                    "family": "independent",
                    "diagnostic_metric_flags": {
                        "emit_book_level_metrics": False,
                        "emit_expected_vs_realized_metrics": False,
                        "emit_close_reason_metrics": True,
                        "emit_execution_policy_metrics": False,
                    },
                    "book_runtime_states": [
                        {
                            "leg": "short",
                            "book_action": "close_stale_thesis",
                            "close_reason": "stale_thesis",
                        }
                    ],
                    "book_expectancy_summary": {
                        "source": "independent_book",
                        "books": [
                            {
                                "leg": "short",
                                "expected_net_edge_bps": -3.0,
                                "close_reason": "stale_thesis",
                            }
                        ],
                    },
                },
            },
            {
                "decision_id": "decision_book_only",
                "strategy_family": "independent",
                "family_execution_summary": {
                    "family": "independent",
                    "diagnostic_metric_flags": {
                        "emit_book_level_metrics": True,
                        "emit_expected_vs_realized_metrics": False,
                        "emit_close_reason_metrics": False,
                        "emit_execution_policy_metrics": False,
                    },
                    "book_runtime_states": [
                        {
                            "leg": "short",
                            "book_action": "close_stale_thesis",
                            "close_reason": "stale_thesis",
                        }
                    ],
                    "book_expectancy_summary": {
                        "source": "independent_book",
                        "books": [
                            {
                                "leg": "short",
                                "expected_net_edge_bps": -3.0,
                                "close_reason": "stale_thesis",
                            }
                        ],
                    },
                },
            },
        ]
        query._scoped_fill_outcomes = lambda: []  # type: ignore[method-assign]

        summary = query._independent_expected_vs_realized_summary()

        assert summary is not None
        self.assertEqual(summary["sample_count"], 1)
        self.assertEqual(summary["entry_count"], 1)
        self.assertEqual(summary["close_count"], 0)
        self.assertEqual(summary["avg_expected_net_edge_bps"], 8.0)
        self.assertEqual(summary["weak_edge_entry_count"], 1)
        self.assertEqual(summary["passive_first_usage_ratio"], 1.0)
        self.assertEqual(summary["close_reason_distribution"], [{"reason": "stale_thesis", "count": 1}])
        self.assertEqual(len(summary["book_breakdown"]), 2)
        short_row = next(item for item in summary["book_breakdown"] if item["leg"] == "short")
        self.assertEqual(short_row["sample_count"], 1)
        self.assertEqual(short_row["close_count"], 1)
        self.assertEqual(short_row["avg_expected_net_edge_bps"], -3.0)
        self.assertIsNone(short_row["avg_realized_net_bps"])

    def test_independent_expected_vs_realized_summary_counts_partial_fills_as_single_realized_sample(self) -> None:
        query = OperatorQueryService.__new__(OperatorQueryService)
        query.runtime = SimpleNamespace(
            settings=SimpleNamespace(
                strategy_hedge_independent_emit_book_level_metrics=True,
                strategy_hedge_independent_emit_expected_vs_realized_metrics=True,
                strategy_hedge_independent_emit_close_reason_metrics=True,
                strategy_hedge_independent_emit_execution_policy_metrics=True,
            )
        )
        query._recent_independent_target_payloads = lambda **_: [  # type: ignore[method-assign]
            {
                "decision_id": "decision_partial_fill",
                "strategy_family": "independent",
                "family_execution_summary": {
                    "family": "independent",
                    "diagnostic_metric_flags": {
                        "emit_book_level_metrics": True,
                        "emit_expected_vs_realized_metrics": True,
                        "emit_close_reason_metrics": True,
                        "emit_execution_policy_metrics": True,
                    },
                    "book_runtime_states": [
                        {
                            "leg": "long",
                            "book_action": "open",
                            "close_reason": None,
                        }
                    ],
                    "book_expectancy_summary": {
                        "source": "independent_book",
                        "books": [
                            {
                                "leg": "long",
                                "expected_net_edge_bps": 9.0,
                                "weak_edge_report_only": False,
                                "passive_first_required": False,
                            }
                        ],
                    },
                },
            }
        ]
        query._scoped_fill_outcomes = lambda: [  # type: ignore[method-assign]
            FillOutcomeRecord(
                fill_id="fill_partial_1",
                decision_id="decision_partial_fill",
                symbol="BTC-USDT-SWAP",
                strategy_family="independent",
            ),
            FillOutcomeRecord(
                fill_id="fill_partial_2",
                decision_id="decision_partial_fill",
                symbol="BTC-USDT-SWAP",
                strategy_family="independent",
            ),
        ]
        query._execution_quality_row = lambda outcome: {  # type: ignore[method-assign]
            "decision_id": outcome.decision_id,
            "fill_notional": Decimal("100") if outcome.fill_id == "fill_partial_1" else Decimal("900"),
            "realized_pnl_delta": Decimal("1") if outcome.fill_id == "fill_partial_1" else Decimal("9"),
            "gross_realized_pnl": Decimal("0.9") if outcome.fill_id == "fill_partial_1" else Decimal("8.1"),
            "adverse_slippage_bps": Decimal("10") if outcome.fill_id == "fill_partial_1" else Decimal("1"),
            "pos_side": "long",
            "position_intent": "open_long",
        }

        summary = query._independent_expected_vs_realized_summary()

        assert summary is not None
        self.assertEqual(summary["expected_sample_count"], 1)
        self.assertEqual(summary["realized_sample_count"], 1)
        self.assertEqual(summary["overlap_sample_count"], 1)
        self.assertAlmostEqual(summary["avg_realized_net_bps"], 100.0)
        self.assertAlmostEqual(summary["avg_realized_slippage_bps"], 1.9)
        long_row = next(item for item in summary["book_breakdown"] if item["leg"] == "long")
        self.assertEqual(long_row["sample_count"], 1)

    def test_independent_expected_vs_realized_gap_uses_overlap_samples_only(self) -> None:
        query = OperatorQueryService.__new__(OperatorQueryService)
        query.runtime = SimpleNamespace(
            settings=SimpleNamespace(
                strategy_hedge_independent_emit_book_level_metrics=True,
                strategy_hedge_independent_emit_expected_vs_realized_metrics=True,
                strategy_hedge_independent_emit_close_reason_metrics=True,
                strategy_hedge_independent_emit_execution_policy_metrics=True,
            )
        )
        query._recent_independent_target_payloads = lambda **_: [  # type: ignore[method-assign]
            {
                "decision_id": "decision_overlap_gap",
                "strategy_family": "independent",
                "family_execution_summary": {
                    "family": "independent",
                    "diagnostic_metric_flags": {
                        "emit_book_level_metrics": True,
                        "emit_expected_vs_realized_metrics": True,
                        "emit_close_reason_metrics": True,
                        "emit_execution_policy_metrics": True,
                    },
                    "book_runtime_states": [
                        {"leg": "long", "book_action": "open", "close_reason": None},
                        {"leg": "short", "book_action": "close_stale_thesis", "close_reason": "stale_thesis"},
                    ],
                    "book_expectancy_summary": {
                        "source": "independent_book",
                        "books": [
                            {"leg": "long", "expected_net_edge_bps": 10.0},
                            {"leg": "short", "expected_net_edge_bps": -2.0, "close_reason": "stale_thesis"},
                        ],
                    },
                },
            }
        ]
        query._scoped_fill_outcomes = lambda: [  # type: ignore[method-assign]
            FillOutcomeRecord(
                fill_id="fill_overlap_long",
                decision_id="decision_overlap_gap",
                symbol="BTC-USDT-SWAP",
                strategy_family="independent",
            )
        ]
        query._execution_quality_row = lambda outcome: {  # type: ignore[method-assign]
            "decision_id": outcome.decision_id,
            "fill_notional": Decimal("100"),
            "realized_pnl_delta": Decimal("6"),
            "gross_realized_pnl": Decimal("5.5"),
            "adverse_slippage_bps": Decimal("1.0"),
            "pos_side": "long",
            "position_intent": "open_long",
        }

        summary = query._independent_expected_vs_realized_summary()

        assert summary is not None
        self.assertEqual(summary["expected_sample_count"], 2)
        self.assertEqual(summary["realized_sample_count"], 1)
        self.assertEqual(summary["overlap_sample_count"], 1)
        self.assertAlmostEqual(summary["avg_expected_net_edge_bps"], 4.0)
        self.assertAlmostEqual(summary["avg_realized_net_bps"], 600.0)
        self.assertAlmostEqual(summary["expected_realized_net_gap_bps"], 590.0)

    def test_independent_book_breakdown_ignores_unmatched_realized_leg_rows(self) -> None:
        query = OperatorQueryService.__new__(OperatorQueryService)
        query.runtime = SimpleNamespace(
            settings=SimpleNamespace(
                strategy_hedge_independent_emit_book_level_metrics=True,
                strategy_hedge_independent_emit_expected_vs_realized_metrics=True,
                strategy_hedge_independent_emit_close_reason_metrics=True,
                strategy_hedge_independent_emit_execution_policy_metrics=True,
            )
        )
        query._recent_independent_target_payloads = lambda **_: [  # type: ignore[method-assign]
            {
                "decision_id": "decision_book_alignment",
                "strategy_family": "independent",
                "family_execution_summary": {
                    "family": "independent",
                    "diagnostic_metric_flags": {
                        "emit_book_level_metrics": True,
                        "emit_expected_vs_realized_metrics": True,
                        "emit_close_reason_metrics": True,
                        "emit_execution_policy_metrics": True,
                    },
                    "book_runtime_states": [
                        {"leg": "long", "book_action": "open", "close_reason": None},
                    ],
                    "book_expectancy_summary": {
                        "source": "independent_book",
                        "books": [
                            {"leg": "long", "expected_net_edge_bps": 7.0},
                        ],
                    },
                },
            }
        ]
        query._scoped_fill_outcomes = lambda: [  # type: ignore[method-assign]
            FillOutcomeRecord(
                fill_id="fill_book_alignment_long",
                decision_id="decision_book_alignment",
                symbol="BTC-USDT-SWAP",
                strategy_family="independent",
            ),
            FillOutcomeRecord(
                fill_id="fill_book_alignment_short",
                decision_id="decision_book_alignment",
                symbol="BTC-USDT-SWAP",
                strategy_family="independent",
            ),
        ]
        query._execution_quality_row = lambda outcome: {  # type: ignore[method-assign]
            "decision_id": outcome.decision_id,
            "fill_notional": Decimal("100"),
            "realized_pnl_delta": Decimal("5"),
            "gross_realized_pnl": Decimal("4.5"),
            "adverse_slippage_bps": Decimal("1.0"),
            "pos_side": "long" if outcome.fill_id.endswith("_long") else "short",
            "position_intent": "open_long" if outcome.fill_id.endswith("_long") else "close_short",
        }

        summary = query._independent_expected_vs_realized_summary()

        assert summary is not None
        long_row = next(item for item in summary["book_breakdown"] if item["leg"] == "long")
        short_row = next(item for item in summary["book_breakdown"] if item["leg"] == "short")
        self.assertEqual(long_row["sample_count"], 1)
        self.assertAlmostEqual(long_row["avg_realized_net_bps"], 500.0)
        self.assertEqual(short_row["sample_count"], 0)
        self.assertIsNone(short_row["avg_realized_net_bps"])

    def test_independent_realized_summary_ignores_unmatched_realized_leg_rows(self) -> None:
        query = OperatorQueryService.__new__(OperatorQueryService)
        query.runtime = SimpleNamespace(
            settings=SimpleNamespace(
                strategy_hedge_independent_emit_book_level_metrics=True,
                strategy_hedge_independent_emit_expected_vs_realized_metrics=True,
                strategy_hedge_independent_emit_close_reason_metrics=True,
                strategy_hedge_independent_emit_execution_policy_metrics=True,
            )
        )
        query._recent_independent_target_payloads = lambda **_: [  # type: ignore[method-assign]
            {
                "decision_id": "decision_realized_alignment",
                "strategy_family": "independent",
                "family_execution_summary": {
                    "family": "independent",
                    "diagnostic_metric_flags": {
                        "emit_book_level_metrics": True,
                        "emit_expected_vs_realized_metrics": True,
                        "emit_close_reason_metrics": True,
                        "emit_execution_policy_metrics": True,
                    },
                    "book_runtime_states": [
                        {"leg": "long", "book_action": "open", "close_reason": None},
                    ],
                    "book_expectancy_summary": {
                        "source": "independent_book",
                        "books": [
                            {"leg": "long", "expected_net_edge_bps": 7.0},
                        ],
                    },
                },
            }
        ]
        query._scoped_fill_outcomes = lambda: [  # type: ignore[method-assign]
            FillOutcomeRecord(
                fill_id="fill_realized_alignment_long",
                decision_id="decision_realized_alignment",
                symbol="BTC-USDT-SWAP",
                strategy_family="independent",
            ),
            FillOutcomeRecord(
                fill_id="fill_realized_alignment_short",
                decision_id="decision_realized_alignment",
                symbol="BTC-USDT-SWAP",
                strategy_family="independent",
            ),
        ]
        query._execution_quality_row = lambda outcome: {  # type: ignore[method-assign]
            "decision_id": outcome.decision_id,
            "fill_notional": Decimal("100") if outcome.fill_id.endswith("_long") else Decimal("900"),
            "realized_pnl_delta": Decimal("5") if outcome.fill_id.endswith("_long") else Decimal("-45"),
            "gross_realized_pnl": Decimal("4.5") if outcome.fill_id.endswith("_long") else Decimal("-40"),
            "adverse_slippage_bps": Decimal("1.0") if outcome.fill_id.endswith("_long") else Decimal("9.0"),
            "pos_side": "long" if outcome.fill_id.endswith("_long") else "short",
            "position_intent": "open_long" if outcome.fill_id.endswith("_long") else "close_short",
        }

        summary = query._independent_expected_vs_realized_summary()

        assert summary is not None
        self.assertEqual(summary["expected_sample_count"], 1)
        self.assertEqual(summary["realized_sample_count"], 1)
        self.assertEqual(summary["overlap_sample_count"], 1)
        self.assertAlmostEqual(summary["avg_realized_net_bps"], 500.0)
        self.assertAlmostEqual(summary["avg_realized_slippage_bps"], 1.0)
        self.assertAlmostEqual(summary["expected_realized_net_gap_bps"], 493.0)

    def test_independent_realized_summary_prefers_execution_chain_id_over_decision_leg(self) -> None:
        query = OperatorQueryService.__new__(OperatorQueryService)
        query.runtime = SimpleNamespace(
            settings=SimpleNamespace(
                strategy_hedge_independent_emit_book_level_metrics=True,
                strategy_hedge_independent_emit_expected_vs_realized_metrics=True,
                strategy_hedge_independent_emit_close_reason_metrics=True,
                strategy_hedge_independent_emit_execution_policy_metrics=True,
            )
        )
        query._recent_independent_target_payloads = lambda **_: [  # type: ignore[method-assign]
            {
                "decision_id": "decision_chain_alignment",
                "strategy_family": "independent",
                "family_execution_summary": {
                    "family": "independent",
                    "diagnostic_metric_flags": {
                        "emit_book_level_metrics": True,
                        "emit_expected_vs_realized_metrics": True,
                        "emit_close_reason_metrics": True,
                        "emit_execution_policy_metrics": True,
                    },
                    "book_runtime_states": [
                        {
                            "leg": "long",
                            "execution_chain_id": "independent:decision_chain_alignment:long:open",
                            "book_action": "open",
                            "close_reason": None,
                        },
                    ],
                    "book_expectancy_summary": {
                        "source": "independent_book",
                        "books": [
                            {
                                "leg": "long",
                                "expected_net_edge_bps": 7.0,
                            },
                        ],
                    },
                },
            }
        ]
        query._scoped_fill_outcomes = lambda: [  # type: ignore[method-assign]
            FillOutcomeRecord(
                fill_id="fill_chain_match",
                decision_id="decision_chain_alignment",
                execution_chain_id="independent:decision_chain_alignment:long:open",
                symbol="BTC-USDT-SWAP",
                strategy_family="independent",
            ),
            FillOutcomeRecord(
                fill_id="fill_chain_stray",
                decision_id="decision_chain_alignment",
                execution_chain_id="independent:decision_chain_alignment:long:scale_in",
                symbol="BTC-USDT-SWAP",
                strategy_family="independent",
            ),
        ]
        query._execution_quality_row = lambda outcome: {  # type: ignore[method-assign]
            "decision_id": outcome.decision_id,
            "execution_chain_id": outcome.execution_chain_id,
            "fill_notional": Decimal("100") if outcome.fill_id == "fill_chain_match" else Decimal("900"),
            "realized_pnl_delta": Decimal("5") if outcome.fill_id == "fill_chain_match" else Decimal("-45"),
            "gross_realized_pnl": Decimal("4.5") if outcome.fill_id == "fill_chain_match" else Decimal("-40"),
            "adverse_slippage_bps": Decimal("1.0") if outcome.fill_id == "fill_chain_match" else Decimal("9.0"),
            "pos_side": "long",
            "position_intent": "open_long",
        }

        summary = query._independent_expected_vs_realized_summary()

        assert summary is not None
        self.assertEqual(summary["expected_sample_count"], 1)
        self.assertEqual(summary["realized_sample_count"], 1)
        self.assertEqual(summary["overlap_sample_count"], 1)
        self.assertAlmostEqual(summary["avg_realized_net_bps"], 500.0)
        self.assertAlmostEqual(summary["avg_realized_slippage_bps"], 1.0)
        self.assertAlmostEqual(summary["expected_realized_net_gap_bps"], 493.0)

    def test_independent_attempt_diagnostics_split_multi_attempt_chain_from_chain_level_evr(self) -> None:
        query = OperatorQueryService.__new__(OperatorQueryService)
        query.runtime = SimpleNamespace(
            settings=SimpleNamespace(
                strategy_hedge_independent_emit_book_level_metrics=True,
                strategy_hedge_independent_emit_expected_vs_realized_metrics=True,
                strategy_hedge_independent_emit_close_reason_metrics=True,
                strategy_hedge_independent_emit_execution_policy_metrics=True,
            )
        )
        query._recent_independent_target_payloads = lambda **_: [  # type: ignore[method-assign]
            {
                "decision_id": "decision_multi_attempt",
                "strategy_family": "independent",
                "family_execution_summary": {
                    "family": "independent",
                    "diagnostic_metric_flags": {
                        "emit_book_level_metrics": True,
                        "emit_expected_vs_realized_metrics": True,
                        "emit_close_reason_metrics": True,
                        "emit_execution_policy_metrics": True,
                    },
                    "book_runtime_states": [
                        {
                            "leg": "long",
                            "execution_chain_id": "independent:decision_multi_attempt:long:open",
                            "book_action": "open",
                            "close_reason": None,
                        },
                    ],
                    "book_expectancy_summary": {
                        "source": "independent_book",
                        "books": [
                            {
                                "leg": "long",
                                "expected_net_edge_bps": 8.0,
                            },
                        ],
                    },
                },
            }
        ]
        query._scoped_fill_outcomes = lambda: [  # type: ignore[method-assign]
            FillOutcomeRecord(
                fill_id="fill_attempt_one",
                decision_id="decision_multi_attempt",
                execution_chain_id="independent:decision_multi_attempt:long:open",
                execution_attempt_id="execution_attempt:clord_attempt_one",
                symbol="BTC-USDT-SWAP",
                strategy_family="independent",
            ),
            FillOutcomeRecord(
                fill_id="fill_attempt_two",
                decision_id="decision_multi_attempt",
                execution_chain_id="independent:decision_multi_attempt:long:open",
                execution_attempt_id="execution_attempt:clord_attempt_two",
                symbol="BTC-USDT-SWAP",
                strategy_family="independent",
            ),
        ]
        query._execution_quality_row = lambda outcome: {  # type: ignore[method-assign]
            "decision_id": outcome.decision_id,
            "execution_chain_id": outcome.execution_chain_id,
            "execution_attempt_id": outcome.execution_attempt_id,
            "client_order_id": (
                "clord_attempt_one" if outcome.fill_id == "fill_attempt_one" else "clord_attempt_two"
            ),
            "fill_notional": Decimal("100"),
            "realized_pnl_delta": Decimal("4") if outcome.fill_id == "fill_attempt_one" else Decimal("6"),
            "gross_realized_pnl": Decimal("4.3") if outcome.fill_id == "fill_attempt_one" else Decimal("6.5"),
            "adverse_slippage_bps": Decimal("1.0") if outcome.fill_id == "fill_attempt_one" else Decimal("2.0"),
            "pos_side": "long",
            "position_intent": "open_long",
        }

        summary = query._independent_expected_vs_realized_summary()

        assert summary is not None
        self.assertEqual(summary["expected_sample_count"], 1)
        self.assertEqual(summary["realized_sample_count"], 1)
        self.assertEqual(summary["overlap_sample_count"], 1)
        self.assertIsNotNone(summary["attempt_diagnostics"])
        diagnostics = summary["attempt_diagnostics"]
        self.assertEqual(diagnostics["attempt_count"], 2)
        self.assertEqual(diagnostics["matched_attempt_count"], 2)
        self.assertEqual(diagnostics["unmatched_attempt_count"], 0)
        self.assertEqual(diagnostics["filled_attempt_count"], 2)
        self.assertEqual(diagnostics["multi_attempt_chain_count"], 1)
        self.assertEqual(diagnostics["avg_attempts_per_chain"], 2.0)
        self.assertAlmostEqual(diagnostics["avg_realized_net_bps_per_attempt"], 500.0)
        self.assertAlmostEqual(diagnostics["avg_realized_slippage_bps_per_attempt"], 1.5)

    def test_independent_attempt_diagnostics_include_unmatched_filled_attempts_in_attempt_averages(self) -> None:
        query = OperatorQueryService.__new__(OperatorQueryService)
        query.runtime = SimpleNamespace(
            settings=SimpleNamespace(
                strategy_hedge_independent_emit_book_level_metrics=True,
                strategy_hedge_independent_emit_expected_vs_realized_metrics=True,
                strategy_hedge_independent_emit_close_reason_metrics=True,
                strategy_hedge_independent_emit_execution_policy_metrics=True,
            )
        )
        query._recent_independent_target_payloads = lambda **_: [  # type: ignore[method-assign]
            {
                "decision_id": "decision_attempt_unmatched",
                "strategy_family": "independent",
                "family_execution_summary": {
                    "family": "independent",
                    "diagnostic_metric_flags": {
                        "emit_book_level_metrics": True,
                        "emit_expected_vs_realized_metrics": True,
                        "emit_close_reason_metrics": True,
                        "emit_execution_policy_metrics": True,
                    },
                    "book_runtime_states": [
                        {
                            "leg": "long",
                            "execution_chain_id": "independent:decision_attempt_unmatched:long:open",
                            "book_action": "open",
                            "close_reason": None,
                        },
                    ],
                    "book_expectancy_summary": {
                        "source": "independent_book",
                        "books": [
                            {
                                "leg": "long",
                                "expected_net_edge_bps": 8.0,
                            },
                        ],
                    },
                },
            }
        ]
        query._scoped_fill_outcomes = lambda: [  # type: ignore[method-assign]
            FillOutcomeRecord(
                fill_id="fill_attempt_match",
                decision_id="decision_attempt_unmatched",
                execution_chain_id="independent:decision_attempt_unmatched:long:open",
                execution_attempt_id="execution_attempt:clord_attempt_match",
                symbol="BTC-USDT-SWAP",
                strategy_family="independent",
            ),
            FillOutcomeRecord(
                fill_id="fill_attempt_stray",
                decision_id="decision_attempt_unmatched",
                execution_chain_id="independent:decision_attempt_unmatched:stray",
                execution_attempt_id="execution_attempt:clord_attempt_stray",
                symbol="BTC-USDT-SWAP",
                strategy_family="independent",
            ),
        ]
        query._execution_quality_row = lambda outcome: {  # type: ignore[method-assign]
            "decision_id": outcome.decision_id,
            "execution_chain_id": outcome.execution_chain_id,
            "execution_attempt_id": outcome.execution_attempt_id,
            "client_order_id": (
                "clord_attempt_match" if outcome.fill_id == "fill_attempt_match" else "clord_attempt_stray"
            ),
            "fill_notional": Decimal("100"),
            "realized_pnl_delta": Decimal("4") if outcome.fill_id == "fill_attempt_match" else Decimal("-2"),
            "gross_realized_pnl": Decimal("4.5") if outcome.fill_id == "fill_attempt_match" else Decimal("-1.5"),
            "adverse_slippage_bps": Decimal("1.0") if outcome.fill_id == "fill_attempt_match" else Decimal("3.0"),
            "pos_side": "long",
            "position_intent": "open_long",
        }

        summary = query._independent_expected_vs_realized_summary()

        assert summary is not None
        self.assertIsNotNone(summary["attempt_diagnostics"])
        diagnostics = summary["attempt_diagnostics"]
        self.assertEqual(diagnostics["attempt_count"], 2)
        self.assertEqual(diagnostics["matched_attempt_count"], 1)
        self.assertEqual(diagnostics["unmatched_attempt_count"], 1)
        self.assertEqual(diagnostics["filled_attempt_count"], 2)
        self.assertAlmostEqual(diagnostics["avg_realized_net_bps_per_attempt"], 100.0)
        self.assertAlmostEqual(diagnostics["avg_realized_slippage_bps_per_attempt"], 2.0)

    def test_ai_decision_audit_prefers_native_outcome_over_target_fields(self) -> None:
        query = OperatorQueryService.__new__(OperatorQueryService)
        query.runtime = SimpleNamespace(settings=SimpleNamespace(ai_operating_mode="baseline_only"))

        audit = query._ai_decision_audit(
            audit=SimpleNamespace(
                order_intent_refs=[],
                order_state_refs=[],
                fill_event_refs=[],
                reconciliation_refs=[],
                ai_shadow_decision_refs=[],
                ai_shadow_evaluation_refs=[],
            ),
            decision_context=None,
            ai_decision_brief=None,
            baseline_assessment={"direction_bias": "long"},
            ai_assessment=None,
            position_target={
                "position_intent": "reduce_long",
                "target_exposure_side": "long",
                "family_execution_summary": {
                    "summary_mode": "multi_leg",
                    "family": "independent",
                    "route_action": "override_target",
                    "family_action": "open_independent_book",
                    "leg_count": 2,
                    "position_intents": ["open_long", "open_short"],
                    "directions": ["long", "short"],
                    "leg_actions": ["open"],
                    "execution_modes": ["independent_long_book", "independent_short_book"],
                    "book_runtime_states": [
                        {
                            "leg": "long",
                            "current_qty": "0",
                            "target_qty": "0.01",
                            "state": "opening",
                            "book_action": "open",
                        },
                        {
                            "leg": "short",
                            "current_qty": "0.01",
                            "target_qty": "0",
                            "state": "holding",
                            "book_action": "hold",
                        },
                    ],
                    "book_expectancy_summary": {
                        "source": "independent_book",
                        "books": [
                            {
                                "leg": "long",
                                "expected_gross_edge_bps": 18.0,
                                "expected_signal_edge_bps": 18.0,
                                "expected_slippage_bps": 1.5,
                                "expected_cost_bps": 6.0,
                                "expected_net_edge_bps": 12.0,
                            },
                            {
                                "leg": "short",
                                "expected_gross_edge_bps": 4.0,
                                "expected_signal_edge_bps": 4.0,
                                "expected_slippage_bps": 1.5,
                                "expected_cost_bps": 6.0,
                                "expected_net_edge_bps": -2.0,
                            },
                        ],
                    },
                },
            },
            finalized_decision_outcome={
                "decision_source": "baseline",
                "decision_authority": "reference_only",
                "profile_control_source": "system",
                "final_action": "enter",
                "final_direction": "flat",
                "family_execution_summary": {
                    "summary_mode": "multi_leg",
                    "family": "independent",
                    "route_action": "override_target",
                    "family_action": "open_independent_book",
                    "leg_count": 2,
                    "position_intents": ["open_long", "open_short"],
                    "directions": ["long", "short"],
                    "leg_actions": ["open"],
                    "execution_modes": ["independent_long_book", "independent_short_book"],
                    "book_runtime_states": [
                        {
                            "leg": "long",
                            "current_qty": "0",
                            "target_qty": "0.01",
                            "state": "opening",
                            "book_action": "open",
                        },
                        {
                            "leg": "short",
                            "current_qty": "0.01",
                            "target_qty": "0",
                            "state": "holding",
                            "book_action": "hold",
                        },
                    ],
                    "book_expectancy_summary": {
                        "source": "independent_book",
                        "books": [
                            {
                                "leg": "long",
                                "expected_gross_edge_bps": 18.0,
                                "expected_signal_edge_bps": 18.0,
                                "expected_slippage_bps": 1.5,
                                "expected_cost_bps": 6.0,
                                "expected_net_edge_bps": 12.0,
                            },
                            {
                                "leg": "short",
                                "expected_gross_edge_bps": 4.0,
                                "expected_signal_edge_bps": 4.0,
                                "expected_slippage_bps": 1.5,
                                "expected_cost_bps": 6.0,
                                "expected_net_edge_bps": -2.0,
                            },
                        ],
                    },
                },
            },
            strategy_execution_health=None,
        )

        assert audit is not None
        self.assertTrue(audit["finalized"])
        self.assertEqual(audit["final_action"], "enter")
        self.assertEqual(audit["final_direction"], "flat")
        self.assertEqual(audit["family_execution_summary"]["position_intents"], ["open_long", "open_short"])
        self.assertEqual(audit["book_expectancy_summary"]["source"], "independent_book")
        self.assertEqual(
            audit["family_execution_summary"]["book_expectancy_summary"]["books"][1]["expected_net_edge_bps"],
            -2.0,
        )
        self.assertEqual(audit["book_expectancy_summary"]["books"][1]["expected_net_edge_bps"], -2.0)
        self.assertEqual([item["leg"] for item in audit["book_runtime_states"]], ["long", "short"])

    def test_position_target_payload_backfills_top_level_book_expectancy_summary(self) -> None:
        query = OperatorQueryService.__new__(OperatorQueryService)

        payload = query._position_target_payload(
            {
                "position_intent": "hold",
                "family_execution_summary": {
                    "summary_mode": "multi_leg",
                    "family": "independent",
                    "parent_target_signal": "flat",
                    "parent_current_signal": "long",
                    "parent_effective_signal": "long",
                    "signal_source": "inventory",
                    "book_runtime_states": [
                        {
                            "leg": "long",
                            "current_qty": "0.01",
                            "target_qty": "0.01",
                            "state": "holding",
                            "book_action": "hold",
                        }
                    ],
                    "book_expectancy_summary": {
                        "source": "independent_book",
                        "books": [
                            {
                                "leg": "long",
                                "expected_gross_edge_bps": 18.0,
                                "expected_signal_edge_bps": 18.0,
                                "expected_slippage_bps": 1.5,
                                "expected_cost_bps": 6.0,
                                "expected_net_edge_bps": 12.0,
                            }
                        ],
                    },
                },
            }
        )

        assert payload is not None
        self.assertEqual(payload["book_expectancy_summary"]["source"], "independent_book")
        self.assertEqual(payload["book_expectancy_summary"]["books"][0]["expected_net_edge_bps"], 12.0)
        self.assertEqual([item["leg"] for item in payload["book_runtime_states"]], ["long"])
        self.assertEqual(payload["parent_target_signal"], "flat")
        self.assertEqual(payload["parent_current_signal"], "long")
        self.assertEqual(payload["parent_effective_signal"], "long")
        self.assertEqual(payload["signal_source"], "inventory")

    def test_overlay_audit_summary_exposes_parent_exposure_signals(self) -> None:
        query = OperatorQueryService.__new__(OperatorQueryService)

        audit = query._overlay_audit_summary(
            position_target={
                "hedge_overlay_decision": {
                    "configured_mode": "protective",
                    "effective_mode": "protective",
                    "overlay_source": "protective",
                    "active": True,
                    "state": "holding",
                    "main_leg_signal": "long",
                    "hedge_leg_signal": "short",
                    "parent_target_signal": "flat",
                    "parent_current_signal": "long",
                    "parent_effective_signal": "long",
                    "signal_source": "inventory",
                    "close_reason": "failed_thesis",
                    "long_leg_close_reason": "failed_thesis",
                },
                "strategy_execution_legs": [],
            }
        )

        self.assertEqual(audit["parent_target_signal"], "flat")
        self.assertEqual(audit["parent_current_signal"], "long")
        self.assertEqual(audit["parent_effective_signal"], "long")
        self.assertEqual(audit["signal_source"], "inventory")
        self.assertEqual(audit["close_reason"], "failed_thesis")
        self.assertEqual(audit["long_leg_close_reason"], "failed_thesis")

    def test_ai_economic_actionability_prefers_single_book_expectancy_summary_over_directional_target_edge(self) -> None:
        query = OperatorQueryService.__new__(OperatorQueryService)
        query.runtime = SimpleNamespace(
            settings=SimpleNamespace(
                trading_product_type="derivatives",
                max_slippage_tolerance_bps=20,
                strategy_expected_slippage_bps_fraction=0.5,
                strategy_edge_noise_buffer_bps=1.0,
                strategy_min_net_edge_bps=2.0,
                paper_taker_fee_bps=5.0,
            ),
            account_service=SimpleNamespace(),
        )

        payload = query._ai_economic_actionability(
            ai_assessment={
                "economically_actionable": True,
                "estimated_edge_bps": 15.0,
                "estimated_cost_bps": 5.0,
                "estimated_net_edge_bps": 10.0,
                "validation_flags": [],
                "rejection_flags": [],
            },
            position_target={
                "symbol": "BTC-USDT-SWAP",
                "expected_signal_edge_bps": 30.0,
                "expected_cost_bps": 9.0,
                "expected_net_edge_bps": 21.0,
                "book_expectancy_summary": {
                    "source": "opportunistic_overlay",
                    "books": [
                        {
                            "leg": "short",
                            "expected_gross_edge_bps": 8.0,
                            "expected_signal_edge_bps": 8.0,
                            "expected_slippage_bps": 1.0,
                            "expected_cost_bps": 4.0,
                            "expected_net_edge_bps": 4.0,
                            "required_safe_net_edge_bps": 6.0,
                            "max_acceptable_cost_bps": 7.5,
                            "weak_edge_execution_mode": "report_only",
                            "weak_edge_report_only": True,
                            "passive_first_required": True,
                            "book_action": "open",
                            "policy_reason": "independent_weak_edge_passive_first_required",
                            "execution_policy_urgency": "low",
                            "execution_style_preference": "bounded_limit_ioc",
                            "order_type_preference": "limit",
                            "time_in_force_preference": "IOC",
                            "limit_offset_bps_preference": 1.0,
                            "liquidity_quality_score": 0.72,
                            "execution_health_state": "ok",
                            "edge_strength": "weak",
                        }
                    ],
                },
            },
            ai_decision_brief=None,
            strategy_execution_health=None,
        )

        assert payload is not None
        self.assertEqual(payload["target_expected_signal_edge_bps"], 8.0)
        self.assertEqual(payload["target_expected_cost_bps"], 4.0)
        self.assertEqual(payload["target_expected_net_edge_bps"], 4.0)
        self.assertEqual(payload["target_required_safe_net_edge_bps"], 6.0)
        self.assertEqual(payload["target_max_acceptable_cost_bps"], 7.5)
        self.assertEqual(payload["target_weak_edge_execution_mode"], "report_only")
        self.assertTrue(payload["target_weak_edge_report_only"])
        self.assertTrue(payload["target_passive_first_required"])
        self.assertEqual(payload["target_book_action"], "open")
        self.assertEqual(payload["target_policy_reason"], "independent_weak_edge_passive_first_required")
        self.assertEqual(payload["target_execution_policy_urgency"], "low")
        self.assertEqual(payload["target_execution_style_preference"], "bounded_limit_ioc")

    def test_aggregate_local_positions_exposes_dual_leg_state(self) -> None:
        snapshot = PortfolioSnapshot(
            snapshot_ts=datetime.now(timezone.utc),
            balances={"USDT": 75_000.0},
            positions=[
                Position(
                    symbol="BTC-USDT-SWAP",
                    position_key="BTC-USDT-SWAP:long",
                    position_qty=Decimal("0.02"),
                    position_notional=Decimal("1400"),
                    avg_entry_price=Decimal("70000"),
                    unrealized_pnl=Decimal("15"),
                    product_type="derivatives",
                    margin_mode="cross",
                    position_mode="long_short_mode",
                    pos_side="long",
                ),
                Position(
                    symbol="BTC-USDT-SWAP",
                    position_key="BTC-USDT-SWAP:short",
                    position_qty=Decimal("-0.01"),
                    position_notional=Decimal("-700"),
                    avg_entry_price=Decimal("70500"),
                    unrealized_pnl=Decimal("-3"),
                    product_type="derivatives",
                    margin_mode="cross",
                    position_mode="long_short_mode",
                    pos_side="short",
                ),
            ],
            cost_basis={},
            realized_pnl=0.0,
            unrealized_pnl=12.0,
            total_equity=75_012.0,
            gross_exposure=2100.0,
            net_exposure=700.0,
            risk_budget_usage={},
            product_type="derivatives",
            margin_mode="cross",
        )

        rows = OperatorQueryService._aggregate_local_positions(snapshot)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(Decimal(str(row["position_qty"])), Decimal("0.01"))
        self.assertEqual(Decimal(str(row["net_position_qty"])), Decimal("0.01"))
        self.assertEqual(Decimal(str(row["gross_position_qty"])), Decimal("0.03"))
        self.assertEqual(Decimal(str(row["long_position_qty"])), Decimal("0.02"))
        self.assertEqual(Decimal(str(row["short_position_qty"])), Decimal("0.01"))
        self.assertEqual(Decimal(str(row["net_position_notional"])), Decimal("700"))
        self.assertEqual(Decimal(str(row["gross_position_notional"])), Decimal("2100"))
        self.assertTrue(row["dual_legged"])
        self.assertEqual(len(row["legs"]), 2)

    def test_aggregate_exchange_positions_exposes_dual_leg_state(self) -> None:
        exchange = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=datetime.now(timezone.utc),
            balances=[],
            positions=[
                ExchangePosition(
                    instrument_id="BTC-USDT-SWAP",
                    symbol="BTC-USDT-SWAP",
                    quantity=Decimal("0.02"),
                    average_entry_price=Decimal("70000"),
                    notional_usd=Decimal("1400"),
                    side="long",
                    margin_mode="cross",
                    unrealized_pnl=Decimal("15"),
                ),
                ExchangePosition(
                    instrument_id="BTC-USDT-SWAP",
                    symbol="BTC-USDT-SWAP",
                    quantity=Decimal("0.01"),
                    average_entry_price=Decimal("70500"),
                    notional_usd=Decimal("700"),
                    side="short",
                    margin_mode="cross",
                    unrealized_pnl=Decimal("-3"),
                ),
            ],
            open_orders=[],
            fills=[],
            instruments=[],
            account_mode="cross",
            position_mode="long_short_mode",
            account_configuration=ExchangeAccountConfiguration(position_mode="long_short_mode"),
        )

        rows = OperatorQueryService._aggregate_exchange_positions(exchange)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(Decimal(str(row["position_qty"])), Decimal("0.01"))
        self.assertEqual(Decimal(str(row["net_position_qty"])), Decimal("0.01"))
        self.assertEqual(Decimal(str(row["gross_position_qty"])), Decimal("0.03"))
        self.assertEqual(Decimal(str(row["long_position_qty"])), Decimal("0.02"))
        self.assertEqual(Decimal(str(row["short_position_qty"])), Decimal("0.01"))
        self.assertEqual(Decimal(str(row["net_position_notional"])), Decimal("700"))
        self.assertEqual(Decimal(str(row["gross_position_notional"])), Decimal("2100"))
        self.assertTrue(row["dual_legged"])
        self.assertEqual(len(row["legs"]), 2)

    def test_position_mode_audit_summary_collects_modes_and_pos_sides(self) -> None:
        summary = OperatorQueryService._position_mode_audit_summary(
            position_mode_contract={
                "configured_derivatives_position_mode": "hedge",
                "required_exchange_position_mode": "long_short_mode",
                "exchange_position_mode": "long_short_mode",
                "exchange_position_mode_matches_configured": True,
                "position_mode_match_required": True,
            },
            order_intents=[
                {
                    "position_mode": "long_short_mode",
                    "pos_side": "long",
                }
            ],
            order_updates=[
                {
                    "position_mode": "long_short_mode",
                    "pos_side": "short",
                }
            ],
            fills=[],
            reconciliations=[],
        )

        self.assertTrue(summary["hedge_mode_active"])
        self.assertEqual(summary["observed_position_modes"], ["long_short_mode"])
        self.assertEqual(summary["observed_pos_sides"], ["long", "short"])
        self.assertFalse(summary["mode_change_detected"])

    def test_leg_order_audit_summary_collects_leg_actions(self) -> None:
        summary = OperatorQueryService._leg_order_audit_summary(
            order_intents=[
                {
                    "symbol": "BTC-USDT-SWAP",
                    "position_mode": "long_short_mode",
                    "pos_side": "long",
                    "leg_action": "open",
                    "quantity": "0.02",
                    "client_order_id": "clord_long_open",
                    "intent_id": "intent_long_open",
                    "leg_intent_id": "leg_long_open",
                },
                {
                    "symbol": "BTC-USDT-SWAP",
                    "position_mode": "long_short_mode",
                    "pos_side": "short",
                    "leg_action": "close",
                    "quantity": "0.01",
                    "client_order_id": "clord_short_close",
                    "intent_id": "intent_short_close",
                    "leg_intent_id": "leg_short_close",
                },
            ],
            order_updates=[
                {
                    "client_order_id": "clord_long_open",
                    "intent_id": "intent_long_open",
                    "leg_intent_id": "leg_long_open",
                    "status": "FILLED",
                }
            ],
            fills=[
                {
                    "client_order_id": "clord_long_open",
                    "intent_id": "intent_long_open",
                    "leg_intent_id": "leg_long_open",
                }
            ],
        )

        self.assertEqual(summary["total_count"], 2)
        self.assertEqual(summary["open_count"], 1)
        self.assertEqual(summary["close_count"], 1)
        self.assertEqual(summary["items"][0]["fill_count"], 1)
        self.assertIn("BTC-USDT-SWAP", summary["symbols"])

    def test_leg_reconciliation_audit_summary_collects_leg_mismatches(self) -> None:
        summary = OperatorQueryService._leg_reconciliation_audit_summary(
            [
                {
                    "reconciliation_id": "recon_leg_1",
                    "position_diff": {
                        "exchange_leg_mismatches": {
                            "BTC-USDT-SWAP:short": {
                                "symbol": "BTC-USDT-SWAP",
                                "leg_side": "short",
                                "stored_qty": "0",
                                "exchange_qty": "-0.01",
                            }
                        },
                        "exchange_instrument_mismatches": {},
                    },
                    "unknown_state_details": [
                        {
                            "kind": "exchange_position_without_local_execution_chain",
                            "position_key": "BTC-USDT-SWAP:short",
                        }
                    ],
                }
            ]
        )

        self.assertEqual(summary["total_count"], 1)
        self.assertEqual(summary["missing_execution_chain_count"], 1)
        self.assertEqual(summary["items"][0]["reconciliation_id"], "recon_leg_1")
        self.assertEqual(summary["items"][0]["kind"], "missing_execution_chain")


if __name__ == "__main__":
    unittest.main()
