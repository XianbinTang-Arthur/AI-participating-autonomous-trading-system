from __future__ import annotations

from types import SimpleNamespace
from unittest import TestCase

from aats.schemas.operator import ReplayValidationSummary
from aats.schemas.common import utc_now
from aats.services.operator.audit_replay_queries import AuditReplayQueryFacade
from aats.services.reconciliation_service.replay import ReplayResult


class TestAuditReplayQueries(TestCase):
    def test_replay_summary_includes_independent_version_diagnostics(self) -> None:
        owner = SimpleNamespace(
            runtime=SimpleNamespace(
                settings=SimpleNamespace(
                    trading_product_type="derivatives",
                    margin_mode="cross",
                    allowed_symbols=("BTC-USDT-SWAP",),
                )
            ),
            _independent_expected_vs_realized_summary=lambda **_: None,
            _independent_version_summary=lambda **_: {
                "state_version": 2,
                "score_stability_semantics_version": 2,
            },
        )

        facade = AuditReplayQueryFacade(owner)
        summary = facade._replay_summary(
            ReplayResult(
                replayed_event_count=1,
                stored_snapshot_count=1,
                divergence_count=0,
                selected_decision_id="decision_replay_versions",
            ),
            symbol="BTC-USDT-SWAP",
            regime="trend",
            active_profile_id="derivatives_live",
            margin_mode="cross",
        )

        self.assertEqual(summary["independent_state_version"], 2)
        self.assertEqual(summary["independent_score_stability_semantics_version"], 2)

    def test_replay_status_enriches_legacy_validation_rows_with_independent_versions(self) -> None:
        validation = {
            "validated_at": utc_now(),
            "decision_id": "decision_replay_versions",
            "symbol": "BTC-USDT-SWAP",
            "replayed_event_count": 1,
            "stored_snapshot_count": 1,
            "divergence_count": 0,
            "healthy": True,
        }
        event_store = SimpleNamespace(
            recent_by_topic=lambda topic, limit=10: [],
            by_topic=lambda topic: [],
            latest_replay_offset=lambda **_: None,
            archive_summary=lambda: {},
        )
        owner = SimpleNamespace(
            runtime=SimpleNamespace(
                settings=SimpleNamespace(
                    trading_product_type="derivatives",
                    margin_mode="cross",
                    allowed_symbols=("BTC-USDT-SWAP",),
                ),
                replay_validation_history=[validation],
                event_store=event_store,
            ),
            state_scope=None,
            _independent_expected_vs_realized_summary=lambda **_: None,
            _independent_version_summary=lambda **_: {
                "state_version": 2,
                "score_stability_semantics_version": 2,
            },
        )

        facade = AuditReplayQueryFacade(owner)
        status = facade.replay_status()

        latest = status["last_validation"]
        assert latest is not None
        self.assertEqual(latest["independent_state_version"], 2)
        self.assertEqual(latest["independent_score_stability_semantics_version"], 2)
        self.assertEqual(status["recent_validations"][0]["independent_state_version"], 2)
        self.assertEqual(status["recent_validations"][0]["independent_score_stability_semantics_version"], 2)

    def test_replay_summary_prefers_replayed_target_margin_mode(self) -> None:
        owner = SimpleNamespace(
            runtime=SimpleNamespace(
                settings=SimpleNamespace(
                    trading_product_type="derivatives",
                    margin_mode="cross",
                    allowed_symbols=("BTC-USDT-SWAP",),
                )
            ),
            _independent_expected_vs_realized_summary=lambda **_: None,
            _independent_version_summary=lambda **_: None,
        )

        facade = AuditReplayQueryFacade(owner)
        summary = facade._replay_summary(
            ReplayResult(
                replayed_event_count=1,
                stored_snapshot_count=1,
                divergence_count=0,
                selected_decision_id="decision_replay_margin_mode",
            ),
            symbol="BTC-USDT-SWAP",
            regime="trend",
            active_profile_id="derivatives_live",
            margin_mode="isolated",
        )

        self.assertEqual(summary["margin_mode"], "isolated")

    def test_replay_summary_includes_overlay_parent_exposure_summary(self) -> None:
        owner = SimpleNamespace(
            runtime=SimpleNamespace(
                settings=SimpleNamespace(
                    trading_product_type="derivatives",
                    margin_mode="cross",
                    allowed_symbols=("BTC-USDT-SWAP",),
                )
            ),
            _independent_expected_vs_realized_summary=lambda **_: None,
            _independent_version_summary=lambda **_: None,
        )

        facade = AuditReplayQueryFacade(owner)
        summary = facade._replay_summary(
            ReplayResult(
                replayed_event_count=1,
                stored_snapshot_count=1,
                divergence_count=0,
                selected_decision_id="decision_overlay_parent",
            ),
            symbol="BTC-USDT-SWAP",
            regime="trend",
            active_profile_id="derivatives_live",
            margin_mode="cross",
            overlay_parent_exposure_summary={
                "parent_family": "directional",
                "symbol": "BTC-USDT-SWAP",
                "margin_mode": "cross",
                "source_of_truth": "inventory",
                "lifecycle_state": "inventory_only",
                "target_signal": "flat",
                "current_signal": "long",
                "effective_signal": "long",
                "target_qty": "0",
                "current_qty": "0.03",
                "effective_qty": "0.03",
                "target_active": False,
                "inventory_active": True,
            },
        )

        self.assertIsNotNone(summary["overlay_parent_exposure_summary"])
        self.assertEqual(summary["overlay_parent_exposure_summary"]["source_of_truth"], "inventory")
        self.assertEqual(summary["overlay_parent_exposure_summary"]["effective_signal"], "long")

    def test_replay_validation_model_preserves_overlay_parent_entity_fields(self) -> None:
        payload = ReplayValidationSummary(
            validated_at=utc_now(),
            decision_id="decision_overlay_parent_entity",
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            replayed_event_count=1,
            stored_snapshot_count=1,
            divergence_count=0,
            healthy=True,
            overlay_parent_exposure_summary={
                "overlay_parent_exposure_id": "ovlpexp_entity",
                "decision_id": "decision_overlay_parent_entity",
                "source_stage": "decision_outcome",
                "source_ref": "evt_decision_outcome",
                "captured_at": utc_now(),
                "strategy_family": "protective",
                "strategy_sleeve_id": "protective_overlay_entity",
                "allocation_id": "alloc_overlay_entity",
                "parent_family": "directional",
                "symbol": "BTC-USDT-SWAP",
                "margin_mode": "cross",
                "source_of_truth": "inventory",
                "lifecycle_state": "inventory_only",
                "effective_signal": "long",
                "effective_qty": "0.03",
            },
        ).model_dump(mode="json")

        summary = payload["overlay_parent_exposure_summary"]
        assert summary is not None
        self.assertEqual(summary["overlay_parent_exposure_id"], "ovlpexp_entity")
        self.assertEqual(summary["source_stage"], "decision_outcome")
        self.assertEqual(summary["source_ref"], "evt_decision_outcome")
        self.assertEqual(summary["strategy_sleeve_id"], "protective_overlay_entity")
        self.assertEqual(summary["allocation_id"], "alloc_overlay_entity")

    def test_replay_summary_includes_transition_exception_summary(self) -> None:
        owner = SimpleNamespace(
            runtime=SimpleNamespace(
                settings=SimpleNamespace(
                    trading_product_type="derivatives",
                    margin_mode="cross",
                    allowed_symbols=("BTC-USDT-SWAP",),
                )
            ),
            _independent_expected_vs_realized_summary=lambda **_: None,
            _independent_version_summary=lambda **_: None,
        )

        facade = AuditReplayQueryFacade(owner)
        summary = facade._replay_summary(
            ReplayResult(
                replayed_event_count=1,
                stored_snapshot_count=1,
                divergence_count=0,
                selected_decision_id="decision_transition_exception",
            ),
            symbol="BTC-USDT-SWAP",
            regime="trend",
            active_profile_id="derivatives_live",
            margin_mode="cross",
            independent_transition_exception_summary={
                "family": "independent",
                "total_books": 2,
                "invalid_transition_count": 1,
                "affected_legs": ["long"],
                "violation_reasons": ["independent_transition_invalid:cooldown->building"],
                "blocking": True,
                "items": [
                    {
                        "leg": "long",
                        "state": "blocked",
                        "book_state": "building",
                        "prior_book_state": "cooldown",
                        "book_action": "blocked",
                        "transition_valid": False,
                        "transition_violation_reason": "independent_transition_invalid:cooldown->building",
                    }
                ],
            },
        )

        self.assertIsNotNone(summary["independent_transition_exception_summary"])
        self.assertEqual(summary["independent_transition_exception_summary"]["invalid_transition_count"], 1)
        self.assertTrue(summary["independent_transition_exception_summary"]["blocking"])
