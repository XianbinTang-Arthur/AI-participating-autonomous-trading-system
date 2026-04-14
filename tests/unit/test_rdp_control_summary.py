from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from aats.api.rdp_control_summary import build_rdp_control_summary
from aats.bootstrap.active_parameters import get_active_parameter_summary


@contextmanager
def _dummy_session():
    yield object()


def _fake_request() -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(runtime=None)))


class TestRdpControlSummary(TestCase):
    def test_runtime_source_distinguishes_governance_pause_from_profile_defaults(self) -> None:
        request = _fake_request()

        with (
            patch("aats.api.rdp_control_summary._governance_session", _dummy_session),
            patch("aats.data_platform.governance.rdp_task_db.db_get_recent_tasks", return_value=[]),
            patch(
                "aats.api.rdp_control_summary.query_latest_recommendations",
                return_value={
                    "recommendations": [
                        {
                            "recommendation_id": "rec_pause_candidate",
                            "symbol": "BTC-USDT-SWAP",
                            "family": "independent",
                            "timeframe": "15m",
                            "recommendation_type": "parameter_upgrade",
                            "confidence": "high",
                            "reason": "净边际不足，需要重新应用候选参数",
                            "status": "draft",
                            "target_parameter_set_id": "ps_candidate_1",
                            "created_at": "2026-03-21T12:20:00Z",
                        },
                    ],
                },
            ),
            patch(
                "aats.api.rdp_control_summary.query_active_parameter_sets",
                return_value={
                    "generated_at": "2026-03-21T12:19:00Z",
                    "governance_managed": True,
                    "paused_combos": ["independent_15m"],
                    "known_combos": ["independent_15m"],
                    "active_sets": {},
                    "parameter_sets": [],
                },
            ),
            patch(
                "aats.api.rdp_control_summary.query_latest_decision_round",
                return_value={
                    "available": True,
                    "round_id": "round_demo",
                    "has_conclusion_report": True,
                    "promotion_readiness_assessment": {"overall_status": "blocked"},
                    "family_timeframe_decisions": [
                        {
                            "combo_key": "independent_15m",
                            "family": "independent",
                            "timeframe": "15m",
                            "decision": "pause",
                            "confidence": "high",
                            "reasons": ["净边际低于安全下限"],
                        },
                    ],
                    "parameter_upgrade_candidates": [
                        {
                            "combo_key": "independent_15m",
                            "family": "independent",
                            "timeframe": "15m",
                            "decision": "promote_candidate",
                            "parameter_set_id": "ps_candidate_1",
                            "confidence": "high",
                        },
                    ],
                },
            ),
            patch(
                "aats.api.rdp_control_summary.query_latest_decisions",
                return_value={
                    "available": True,
                    "generated_at": "2026-03-21T12:21:00Z",
                    "status_distribution": {"pause": 1},
                    "decisions": [
                        {
                            "combo_key": "independent_15m",
                            "family": "independent",
                            "timeframe": "15m",
                            "current_status": "pause",
                            "active_parameter_set_id": "ps_candidate_1",
                            "last_updated_at": "2026-03-21T12:21:00Z",
                        },
                    ],
                },
            ),
            patch(
                "aats.api.rdp_control_summary.query_parameter_registry",
                return_value={
                    "available": True,
                    "parameter_sets": [
                        {
                            "parameter_set_id": "ps_candidate_1",
                            "family": "independent",
                            "timeframe": "15m",
                            "status": "candidate",
                        },
                    ],
                },
            ),
        ):
            payload = build_rdp_control_summary(request)

        self.assertEqual(payload["runtime_parameter_source"]["mode"], "governance_pause")
        combo_state = next(
            item for item in payload["governance_state"]["combo_states"]
            if item["combo_key"] == "independent_15m"
        )
        self.assertEqual(combo_state["runtime_source"], "governance_pause")
        self.assertEqual(combo_state["decision_target_parameter_set_id"], "ps_candidate_1")
        self.assertEqual(combo_state["candidate_parameter_set_id"], "ps_candidate_1")
        self.assertEqual(combo_state["candidate_parameter_status"], "candidate")
        self.assertTrue(combo_state["pending_operator_action"])

    def test_runtime_source_reports_mixed_when_active_and_paused_combos_coexist(self) -> None:
        request = _fake_request()

        with (
            patch("aats.api.rdp_control_summary._governance_session", _dummy_session),
            patch("aats.data_platform.governance.rdp_task_db.db_get_recent_tasks", return_value=[]),
            patch("aats.api.rdp_control_summary.query_latest_recommendations", return_value={"recommendations": []}),
            patch(
                "aats.api.rdp_control_summary.query_active_parameter_sets",
                return_value={
                    "generated_at": "2026-03-21T12:30:00Z",
                    "governance_managed": True,
                    "paused_combos": ["directional_15m"],
                    "known_combos": ["independent_15m", "directional_15m"],
                    "active_sets": {
                        "independent_15m": {
                            "parameter_set_id": "ps_live_1",
                            "family": "independent",
                            "timeframe": "15m",
                            "applied_at": "2026-03-21T12:10:00Z",
                            "approval_recommendation_id": "rec_apply_1",
                            "values": {"entry_threshold": 0.42},
                        },
                    },
                    "parameter_sets": [],
                },
            ),
            patch("aats.api.rdp_control_summary.query_latest_decision_round", return_value={"available": False}),
            patch(
                "aats.api.rdp_control_summary.query_latest_decisions",
                return_value={
                    "available": True,
                    "generated_at": "2026-03-21T12:31:00Z",
                    "status_distribution": {"keep_active": 1, "pause": 1},
                    "decisions": [
                        {
                            "combo_key": "independent_15m",
                            "family": "independent",
                            "timeframe": "15m",
                            "current_status": "keep_active",
                            "active_parameter_set_id": "ps_live_1",
                            "last_updated_at": "2026-03-21T12:31:00Z",
                        },
                        {
                            "combo_key": "directional_15m",
                            "family": "directional",
                            "timeframe": "15m",
                            "current_status": "pause",
                            "active_parameter_set_id": None,
                            "last_updated_at": "2026-03-21T12:31:00Z",
                        },
                    ],
                },
            ),
            patch(
                "aats.api.rdp_control_summary.query_parameter_registry",
                return_value={
                    "available": True,
                    "parameter_sets": [
                        {
                            "parameter_set_id": "ps_live_1",
                            "family": "independent",
                            "timeframe": "15m",
                            "status": "frozen",
                        },
                    ],
                },
            ),
        ):
            payload = build_rdp_control_summary(request)

        self.assertEqual(payload["runtime_parameter_source"]["mode"], "mixed")
        combo_states = {
            item["combo_key"]: item
            for item in payload["governance_state"]["combo_states"]
        }
        self.assertEqual(combo_states["independent_15m"]["runtime_source"], "active_parameters")
        self.assertEqual(combo_states["directional_15m"]["runtime_source"], "governance_pause")

    def test_active_parameter_summary_keeps_governance_metadata(self) -> None:
        with patch(
            "aats.bootstrap.active_parameters.load_active_parameter_registry",
            return_value={
                "generated_at": "2026-03-21T12:00:00Z",
                "governance_managed": True,
                "paused_combos": ["directional_15m"],
                "active_sets": {
                    "independent_15m": {
                        "parameter_set_id": "ps_live_1",
                        "family": "independent",
                        "timeframe": "15m",
                        "applied_at": "2026-03-21T12:01:00Z",
                        "applied_by": "operator",
                        "approval_recommendation_id": "rec_apply_1",
                        "source_round_id": "round_demo",
                        "values": {"entry_threshold": 0.4},
                    },
                },
            },
        ):
            summary = get_active_parameter_summary()

        self.assertTrue(summary["governance_managed"])
        self.assertEqual(summary["paused_combos"], ["directional_15m"])
        self.assertEqual(summary["active_sets"]["independent_15m"]["parameter_set_id"], "ps_live_1")
        self.assertEqual(summary["parameter_sets"][0]["combo_key"], "independent_15m")
        self.assertEqual(summary["parameter_sets"][0]["status"], "active")
