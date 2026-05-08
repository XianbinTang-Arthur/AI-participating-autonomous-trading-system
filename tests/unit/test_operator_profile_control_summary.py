from __future__ import annotations

from types import SimpleNamespace

import pytest

from aats.services.operator.query_service import OperatorQueryService
from aats.services.operator.strategy_profile_queries import StrategyProfileQueryFacade


class _State:
    active_profile_id = "balanced"
    active_revision_id = "rev-balanced"

    def model_dump(self, *, mode: str):
        assert mode == "json"
        return {
            "active_profile_id": self.active_profile_id,
            "active_revision_id": self.active_revision_id,
            "auto_switch_enabled": True,
        }


class _StrategyProfiles:
    def __init__(self, *, latest_optimization: dict | None) -> None:
        self.latest_optimization = latest_optimization
        self.seed_calls = 0
        self.tuning_context_calls = 0

    def snapshot(self):
        raise AssertionError("profileControlSummary must not build full strategy profile snapshot")

    def ensure_seed_profiles(self) -> None:
        self.seed_calls += 1

    def _activation_state(self):
        return _State()

    def _revision(self, revision_id: str | None):
        return SimpleNamespace(revision_id=revision_id, profile_id="balanced", profile_label="Balanced")

    def _revision_view(self, revision):
        return {
            "revision_id": revision.revision_id,
            "profile_id": revision.profile_id,
            "profile_label": revision.profile_label,
        }

    def _latest_optimization_report_payload(self):
        return self.latest_optimization

    def _latest_selection_decision_payload(self):
        return {
            "transition_class": "stable_keep_active",
            "gating_state": "allowed",
            "operator_summary": "保持当前档位。",
        }

    def _tuning_context(self):
        self.tuning_context_calls += 1
        return {"context": "fallback"}

    @staticmethod
    def _context_payload(snapshot):
        return snapshot

    def _profile_control_summary(self, *, context, replay_summary, active_profile_id):
        return {
            "active_profile_id": active_profile_id,
            "evidence": {"cold_start_active": True},
            "safety_profile_required": False,
            "adaptive_controls": {},
            "entry_execution_guard": {},
        }


class _Owner:
    def __init__(self, strategy_profiles) -> None:
        self.runtime = SimpleNamespace()
        self.strategy_profiles = strategy_profiles


def test_profile_control_summary_snapshot_reuses_latest_control_summary_without_full_snapshot() -> None:
    profiles = _StrategyProfiles(
        latest_optimization={
            "recommended_profile_id": "balanced",
            "score_delta_vs_active": 0.0,
            "control_summary": {
                "active_profile_id": "balanced",
                "evidence": {"cold_start_active": False},
                "safety_profile_required": False,
                "adaptive_controls": {"risk_budget": {"multiplier": 1.0}},
                "entry_execution_guard": {},
            },
            "winner_selection_policy": {"mode": "registered_profile_only"},
            "notes": ["latest report already has control summary"],
            "expensive_field": "kept out by profile_control_summary_report",
        }
    )

    payload = StrategyProfileQueryFacade(_Owner(profiles)).summary_snapshot()

    assert profiles.seed_calls == 1
    assert profiles.tuning_context_calls == 0
    assert payload["control_summary"]["evidence"]["cold_start_active"] is False
    assert payload["activation"]["active_profile_id"] == "balanced"
    assert payload["active_revision"]["revision_id"] == "rev-balanced"
    assert payload["latest_selection_decision"]["transition_class"] == "stable_keep_active"


def test_profile_control_summary_report_filters_heavy_optimization_payload() -> None:
    service = object.__new__(OperatorQueryService)
    service.strategy_profile_queries = SimpleNamespace(
        snapshot=lambda: pytest.fail("profileControlSummary must use summary_snapshot"),
        summary_snapshot=lambda: {
            "control_summary": {"evidence": {"cold_start_active": False}},
            "activation": {"active_profile_id": "balanced"},
            "active_revision": {"revision_id": "rev-balanced"},
            "latest_selection_decision": {"transition_class": "stable_keep_active"},
            "latest_optimization_report": {
                "recommended_profile_id": "balanced",
                "score_delta_vs_active": 0.0,
                "control_summary": {"evidence": {"cold_start_active": False}},
                "winner_selection_policy": {"mode": "registered_profile_only"},
                "notes": ["summary"],
                "candidates": [{"profile_id": "heavy"}],
            },
        },
    )

    payload = service._build_profile_control_summary_report()

    assert payload["control_summary"]["evidence"]["cold_start_active"] is False
    assert payload["activation"]["active_profile_id"] == "balanced"
    assert payload["active_revision"]["revision_id"] == "rev-balanced"
    assert payload["latest_selection_decision"]["transition_class"] == "stable_keep_active"
    assert payload["latest_optimization_report"]["recommended_profile_id"] == "balanced"
    assert "candidates" not in payload["latest_optimization_report"]
