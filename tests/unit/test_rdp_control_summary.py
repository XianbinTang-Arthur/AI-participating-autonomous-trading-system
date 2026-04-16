from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import aats.api.rdp_control_summary as rdp_control_summary
from aats.api.rdp_control_summary import build_rdp_control_summary
from aats.bootstrap.active_parameters import get_active_parameter_summary


@contextmanager
def _dummy_session():
    yield object()


def _fake_request() -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(runtime=None)))


class TestRdpControlSummary(TestCase):
    def test_control_summary_splits_running_and_pending_tasks_by_workflow(self) -> None:
        request = _fake_request()

        with (
            patch("aats.api.rdp_control_summary._governance_session", _dummy_session),
            patch(
                "aats.data_platform.governance.rdp_task_db.db_get_recent_tasks",
                return_value=[
                    {
                        "task_id": "task_pending_data",
                        "workflow": "data_maintenance",
                        "status": "pending",
                        "requested_by": "scheduler",
                        "requested_at": "2026-04-10T12:05:00Z",
                        "started_at": None,
                        "finished_at": None,
                        "exit_code": None,
                        "error_message": None,
                        "log_tail": None,
                    },
                    {
                        "task_id": "task_running_research",
                        "workflow": "research_cycle",
                        "status": "running",
                        "requested_by": "scheduler",
                        "requested_at": "2026-04-10T12:00:00Z",
                        "started_at": "2026-04-10T12:01:00Z",
                        "finished_at": None,
                        "exit_code": None,
                        "error_message": None,
                        "log_tail": None,
                    },
                    {
                        "task_id": "task_done_data",
                        "workflow": "data_maintenance",
                        "status": "done",
                        "requested_by": "scheduler",
                        "requested_at": "2026-04-10T11:30:00Z",
                        "started_at": "2026-04-10T11:31:00Z",
                        "finished_at": "2026-04-10T11:40:00Z",
                        "exit_code": 0,
                        "error_message": None,
                        "log_tail": None,
                    },
                ],
            ),
            patch("aats.api.rdp_control_summary._environment_summary", return_value={"name": "staging"}),
            patch("aats.api.rdp_control_summary.query_rdp_health", return_value={
                "overall_health": "healthy",
                "blocking_reasons": [],
                "warnings": [],
                "checks": [],
            }),
            patch("aats.api.rdp_control_summary._load_recent_gate_results", return_value=[]),
            patch("aats.api.rdp_control_summary._load_recent_releases", return_value=[]),
            patch("aats.api.rdp_control_summary._build_observation_queue", return_value=[]),
            patch("aats.api.rdp_control_summary.query_latest_recommendations", return_value={"recommendations": []}),
            patch("aats.api.rdp_control_summary.query_active_parameter_sets", return_value={
                "generated_at": "2026-04-10T11:40:00Z",
                "governance_managed": True,
                "paused_combos": [],
                "known_combos": [],
                "active_sets": {},
                "parameter_sets": [],
            }),
            patch("aats.api.rdp_control_summary.query_latest_decision_round", return_value={"available": False}),
            patch("aats.api.rdp_control_summary.query_latest_decisions", return_value={
                "available": False,
                "generated_at": None,
                "status_distribution": {},
                "decisions": [],
            }),
            patch("aats.api.rdp_control_summary.query_parameter_registry", return_value={
                "available": True,
                "parameter_sets": [],
            }),
            patch(
                "aats.data_platform.production_workflow.release_registry.load_release_history",
                return_value={"releases": []},
            ),
        ):
            payload = build_rdp_control_summary(request)

        data_task = payload["tasks"]["data_maintenance"]
        research_task = payload["tasks"]["research_cycle"]
        self.assertEqual(data_task["pending_task"]["task_id"], "task_pending_data")
        self.assertEqual(data_task["latest_task"]["task_id"], "task_pending_data")
        self.assertIsNone(data_task["running_task"])
        self.assertEqual(research_task["running_task"]["task_id"], "task_running_research")
        self.assertEqual(research_task["display_task"]["task_id"], "task_running_research")

    def test_control_summary_excludes_already_applied_approved_recommendations(self) -> None:
        request = _fake_request()

        with (
            patch("aats.api.rdp_control_summary._governance_session", _dummy_session),
            patch("aats.data_platform.governance.rdp_task_db.db_get_recent_tasks", return_value=[]),
            patch("aats.api.rdp_control_summary._environment_summary", return_value={"name": "staging"}),
            patch("aats.api.rdp_control_summary.query_rdp_health", return_value={
                "overall_health": "healthy",
                "blocking_reasons": [],
                "warnings": [],
                "checks": [],
            }),
            patch("aats.api.rdp_control_summary._load_recent_gate_results", return_value=[]),
            patch("aats.api.rdp_control_summary._load_recent_releases", return_value=[]),
            patch("aats.api.rdp_control_summary._build_observation_queue", return_value=[]),
            patch("aats.api.rdp_control_summary.query_latest_recommendations", return_value={
                "recommendations": [
                    {
                        "recommendation_id": "rec_applied_1",
                        "symbol": "BTC-USDT-SWAP",
                        "family": "independent",
                        "timeframe": "15m",
                        "recommendation_type": "parameter_upgrade",
                        "confidence": "high",
                        "reason": "已发布并正在运行",
                        "status": "approved",
                        "target_parameter_set_id": "ps_live_2",
                        "created_at": "2026-04-10T11:50:00Z",
                    },
                ],
            }),
            patch("aats.api.rdp_control_summary.query_active_parameter_sets", return_value={
                "generated_at": "2026-04-10T11:40:00Z",
                "governance_managed": True,
                "paused_combos": [],
                "known_combos": ["independent_15m"],
                "active_sets": {
                    "independent_15m": {
                        "parameter_set_id": "ps_live_2",
                        "family": "independent",
                        "timeframe": "15m",
                        "approval_recommendation_id": "rec_applied_1",
                    },
                },
                "parameter_sets": [],
            }),
            patch("aats.api.rdp_control_summary.query_latest_decision_round", return_value={"available": False}),
            patch("aats.api.rdp_control_summary.query_latest_decisions", return_value={
                "available": True,
                "generated_at": "2026-04-10T11:55:00Z",
                "status_distribution": {"keep_active": 1},
                "decisions": [],
            }),
            patch("aats.api.rdp_control_summary.query_parameter_registry", return_value={
                "available": True,
                "parameter_sets": [],
            }),
            patch(
                "aats.data_platform.production_workflow.release_registry.load_release_history",
                return_value={"releases": []},
            ),
        ):
            payload = build_rdp_control_summary(request)

        self.assertEqual(payload["operations_summary"]["approved_release_candidate_count"], 0)
        self.assertEqual(payload["pending_recommendations"], [])

    def test_control_summary_excludes_recommendations_that_already_succeeded_once(self) -> None:
        request = _fake_request()

        with (
            patch("aats.api.rdp_control_summary._governance_session", _dummy_session),
            patch("aats.data_platform.governance.rdp_task_db.db_get_recent_tasks", return_value=[]),
            patch("aats.api.rdp_control_summary._environment_summary", return_value={"name": "staging"}),
            patch("aats.api.rdp_control_summary.query_rdp_health", return_value={
                "overall_health": "healthy",
                "blocking_reasons": [],
                "warnings": [],
                "checks": [],
            }),
            patch("aats.api.rdp_control_summary._load_recent_gate_results", return_value=[]),
            patch("aats.api.rdp_control_summary._load_recent_releases", return_value=[]),
            patch("aats.api.rdp_control_summary._build_observation_queue", return_value=[]),
            patch("aats.api.rdp_control_summary.query_latest_recommendations", return_value={
                "recommendations": [
                    {
                        "recommendation_id": "rec_released_1",
                        "symbol": "BTC-USDT-SWAP",
                        "family": "independent",
                        "timeframe": "15m",
                        "recommendation_type": "parameter_upgrade",
                        "confidence": "high",
                        "reason": "曾经成功发布，后续被回滚",
                        "status": "approved",
                        "target_parameter_set_id": "ps_candidate_1",
                        "created_at": "2026-04-10T11:50:00Z",
                    },
                ],
            }),
            patch("aats.api.rdp_control_summary.query_active_parameter_sets", return_value={
                "generated_at": "2026-04-10T11:40:00Z",
                "governance_managed": True,
                "paused_combos": [],
                "known_combos": ["independent_15m"],
                "active_sets": {
                    "independent_15m": {
                        "parameter_set_id": "ps_live_0",
                        "family": "independent",
                        "timeframe": "15m",
                        "approval_recommendation_id": "rec_prev",
                    },
                },
                "parameter_sets": [],
            }),
            patch("aats.api.rdp_control_summary.query_latest_decision_round", return_value={"available": False}),
            patch("aats.api.rdp_control_summary.query_latest_decisions", return_value={
                "available": True,
                "generated_at": "2026-04-10T11:55:00Z",
                "status_distribution": {"keep_active": 1},
                "decisions": [],
            }),
            patch("aats.api.rdp_control_summary.query_parameter_registry", return_value={
                "available": True,
                "parameter_sets": [],
            }),
            patch(
                "aats.data_platform.production_workflow.release_registry.load_release_history",
                return_value={
                    "releases": [
                        {
                            "release_id": "rel_released_1",
                            "recommendation_id": "rec_released_1",
                            "apply_result": "success",
                        },
                    ],
                },
            ),
        ):
            payload = build_rdp_control_summary(request)

        self.assertEqual(payload["operations_summary"]["approved_release_candidate_count"], 0)
        self.assertEqual(payload["pending_recommendations"], [])

    def test_observation_queue_skips_releases_that_are_no_longer_current_active(self) -> None:
        with (
            patch(
                "aats.data_platform.metrics.release_effectiveness.load_effectiveness_registry",
                return_value={"evaluations": []},
            ),
            patch(
                "aats.data_platform.production_workflow.observation_window.load_observation_result",
                return_value={},
            ),
        ):
            queue = rdp_control_summary._build_observation_queue(
                Path("."),
                releases=[
                    {
                        "release_id": "rel_old_1",
                        "family": "independent",
                        "timeframe": "15m",
                        "combo_key": "independent_15m",
                        "parameter_set_id": "ps_candidate_1",
                        "previous_parameter_set_id": "ps_live_0",
                        "created_at": "2026-04-10T12:10:00Z",
                        "apply_result": "success",
                        "observation_status": "observing",
                    },
                ],
                active_parameters={
                    "independent_15m": {
                        "parameter_set_id": "ps_live_0",
                    },
                },
            )

        self.assertEqual(queue, [])

    def test_control_summary_builds_observation_queue_from_full_release_history(self) -> None:
        request = _fake_request()
        full_releases = [
            {
                "release_id": f"rel_new_{index}",
                "created_at": f"2026-04-10T12:{59 - index:02d}:00Z",
                "family": "independent",
                "timeframe": "15m",
                "combo_key": "independent_15m",
                "recommendation_id": f"rec_new_{index}",
                "parameter_set_id": f"ps_other_{index}",
                "previous_parameter_set_id": "ps_prev",
                "actor": "operator",
                "gate_status": "pass",
                "apply_result": "success",
                "observation_status": "completed",
                "observation_window_hours": 24,
            }
            for index in range(10)
        ]
        full_releases.append(
            {
                "release_id": "rel_active_old",
                "created_at": "2026-04-10T11:00:00Z",
                "family": "independent",
                "timeframe": "15m",
                "combo_key": "independent_15m",
                "recommendation_id": "rec_active_old",
                "parameter_set_id": "ps_active_old",
                "previous_parameter_set_id": "ps_prev",
                "actor": "operator",
                "gate_status": "pass",
                "apply_result": "success",
                "observation_status": "observing",
                "observation_window_hours": 24,
            },
        )

        def _load_releases(_project_root: Path, *, limit: int | None = 10) -> list[dict[str, object]]:
            if limit is None:
                return list(full_releases)
            return list(full_releases[:limit])

        with (
            patch("aats.api.rdp_control_summary._governance_session", _dummy_session),
            patch("aats.data_platform.governance.rdp_task_db.db_get_recent_tasks", return_value=[]),
            patch("aats.api.rdp_control_summary._environment_summary", return_value={"name": "staging"}),
            patch("aats.api.rdp_control_summary.query_rdp_health", return_value={
                "overall_health": "healthy",
                "blocking_reasons": [],
                "warnings": [],
                "checks": [],
            }),
            patch("aats.api.rdp_control_summary._load_recent_gate_results", return_value=[]),
            patch("aats.api.rdp_control_summary._load_recent_releases", side_effect=_load_releases),
            patch("aats.api.rdp_control_summary.query_latest_recommendations", return_value={"recommendations": []}),
            patch("aats.api.rdp_control_summary.query_active_parameter_sets", return_value={
                "generated_at": "2026-04-10T11:40:00Z",
                "governance_managed": True,
                "paused_combos": [],
                "known_combos": ["independent_15m"],
                "active_sets": {
                    "independent_15m": {
                        "parameter_set_id": "ps_active_old",
                        "family": "independent",
                        "timeframe": "15m",
                    },
                },
                "parameter_sets": [],
            }),
            patch("aats.api.rdp_control_summary.query_latest_decision_round", return_value={"available": False}),
            patch("aats.api.rdp_control_summary.query_latest_decisions", return_value={
                "available": True,
                "generated_at": "2026-04-10T11:55:00Z",
                "status_distribution": {"keep_active": 1},
                "decisions": [],
            }),
            patch("aats.api.rdp_control_summary.query_parameter_registry", return_value={
                "available": True,
                "parameter_sets": [],
            }),
            patch(
                "aats.data_platform.production_workflow.release_registry.load_release_history",
                return_value={"releases": []},
            ),
            patch(
                "aats.data_platform.metrics.release_effectiveness.load_effectiveness_registry",
                return_value={"evaluations": []},
            ),
            patch(
                "aats.data_platform.production_workflow.observation_window.load_observation_result",
                return_value={},
            ),
        ):
            payload = build_rdp_control_summary(request)

        self.assertEqual(len(payload["observation_queue"]), 1)
        self.assertEqual(payload["observation_queue"][0]["release_id"], "rel_active_old")
        self.assertEqual(payload["operations_summary"]["observing_release_count"], 1)

    def test_control_summary_includes_release_workflow_context(self) -> None:
        request = _fake_request()

        with (
            patch("aats.api.rdp_control_summary._governance_session", _dummy_session),
            patch("aats.data_platform.governance.rdp_task_db.db_get_recent_tasks", return_value=[]),
            patch("aats.api.rdp_control_summary._environment_summary", return_value={
                "name": "staging",
                "strict_environment": True,
                "description": "预发布环境",
                "require_gate_pass": True,
                "require_approval": False,
                "allow_parameter_rollback": True,
                "direct_apply_allowed": True,
                "production_apply_enabled": True,
                "required_observation_window_hours": 24,
            }),
            patch("aats.api.rdp_control_summary.query_rdp_health", return_value={
                "overall_health": "degraded",
                "blocking_reasons": [],
                "warnings": ["workflow_runs_incomplete"],
                "checks": [
                    {
                        "category": "workflow_runs",
                        "name": "freshness",
                        "status": "warn",
                        "detail": "decision_cycle stale",
                    },
                ],
            }),
            patch("aats.api.rdp_control_summary._load_recent_gate_results", return_value=[
                {
                    "gate_run_id": "gate_1",
                    "recommendation_id": "rec_release_1",
                    "created_at": "2026-04-10T12:00:00Z",
                    "gate_status": "warn",
                    "allow_apply": True,
                    "blocking_reasons": [],
                    "warnings": ["workflow stale"],
                    "checks": [],
                },
            ]),
            patch("aats.api.rdp_control_summary._load_recent_releases", return_value=[
                {
                    "release_id": "rel_1",
                    "created_at": "2026-04-10T12:10:00Z",
                    "family": "independent",
                    "timeframe": "15m",
                    "combo_key": "independent_15m",
                    "recommendation_id": "rec_release_1",
                    "parameter_set_id": "ps_live_2",
                    "previous_parameter_set_id": "ps_live_1",
                    "actor": "operator",
                    "gate_status": "warn",
                    "apply_result": "success",
                    "observation_status": "observing",
                    "observation_window_hours": 24,
                },
            ]),
            patch("aats.api.rdp_control_summary._build_observation_queue", return_value=[
                {
                    "release_id": "rel_1",
                    "family": "independent",
                    "timeframe": "15m",
                    "observation_status": "observing",
                    "apply_result": "success",
                },
            ]),
            patch("aats.api.rdp_control_summary.query_latest_recommendations", return_value={
                "recommendations": [
                    {
                        "recommendation_id": "rec_release_1",
                        "symbol": "BTC-USDT-SWAP",
                        "family": "independent",
                        "timeframe": "15m",
                        "recommendation_type": "parameter_upgrade",
                        "confidence": "high",
                        "reason": "建议推进发布",
                        "status": "approved",
                        "target_parameter_set_id": "ps_live_2",
                        "created_at": "2026-04-10T11:50:00Z",
                    },
                ],
            }),
            patch("aats.api.rdp_control_summary.query_active_parameter_sets", return_value={
                "generated_at": "2026-04-10T11:40:00Z",
                "governance_managed": True,
                "paused_combos": [],
                "known_combos": ["independent_15m"],
                "active_sets": {
                    "independent_15m": {
                        "parameter_set_id": "ps_live_1",
                        "family": "independent",
                        "timeframe": "15m",
                    },
                },
                "parameter_sets": [],
            }),
            patch("aats.api.rdp_control_summary.query_latest_decision_round", return_value={"available": False}),
            patch("aats.api.rdp_control_summary.query_latest_decisions", return_value={
                "available": True,
                "generated_at": "2026-04-10T11:55:00Z",
                "status_distribution": {"keep_active": 1},
                "decisions": [
                    {
                        "combo_key": "independent_15m",
                        "family": "independent",
                        "timeframe": "15m",
                        "current_status": "keep_active",
                        "active_parameter_set_id": "ps_live_1",
                        "last_updated_at": "2026-04-10T11:55:00Z",
                    },
                ],
            }),
            patch("aats.api.rdp_control_summary.query_parameter_registry", return_value={
                "available": True,
                "parameter_sets": [
                    {
                        "parameter_set_id": "ps_live_1",
                        "family": "independent",
                        "timeframe": "15m",
                        "status": "active",
                    },
                    {
                        "parameter_set_id": "ps_live_2",
                        "family": "independent",
                        "timeframe": "15m",
                        "status": "candidate",
                    },
                ],
            }),
            patch(
                "aats.data_platform.production_workflow.release_registry.load_release_history",
                return_value={"releases": []},
            ),
        ):
            payload = build_rdp_control_summary(request)

        self.assertEqual(payload["environment"]["name"], "staging")
        self.assertEqual(payload["health"]["overall_health"], "degraded")
        self.assertEqual(payload["operations_summary"]["approved_release_candidate_count"], 1)
        self.assertEqual(payload["operations_summary"]["draft_recommendation_count"], 0)
        self.assertEqual(payload["operations_summary"]["latest_gate_status"], "warn")
        self.assertEqual(payload["recent_gate_results"][0]["gate_run_id"], "gate_1")
        self.assertEqual(payload["observation_queue"][0]["observation_status"], "observing")
        self.assertEqual(payload["pending_recommendations"][0]["recommendation_id"], "rec_release_1")

    def test_runtime_source_distinguishes_governance_pause_from_profile_defaults(self) -> None:
        request = _fake_request()

        with (
            patch("aats.api.rdp_control_summary._governance_session", _dummy_session),
            patch("aats.data_platform.governance.rdp_task_db.db_get_recent_tasks", return_value=[]),
            patch("aats.api.rdp_control_summary._environment_summary", return_value={"name": "dev"}),
            patch("aats.api.rdp_control_summary.query_rdp_health", return_value={"overall_health": "healthy", "checks": [], "blocking_reasons": [], "warnings": []}),
            patch("aats.api.rdp_control_summary._load_recent_gate_results", return_value=[]),
            patch("aats.api.rdp_control_summary._load_recent_releases", return_value=[]),
            patch("aats.api.rdp_control_summary._build_observation_queue", return_value=[]),
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

        self.assertEqual(payload["governance_state"]["parameter_source_mode"], "governance_pause")
        combo_state = next(
            item for item in payload["governance_state"]["combo_states"]
            if item["combo_key"] == "independent_15m"
        )
        self.assertEqual(combo_state["runtime_source"], "governance_pause")
        self.assertEqual(combo_state["decision_target_parameter_set_id"], "ps_candidate_1")
        self.assertEqual(combo_state["candidate_parameter_set_id"], "ps_candidate_1")
        self.assertEqual(combo_state["candidate_parameter_status"], "candidate")
        self.assertTrue(combo_state["pending_operator_action"])
        self.assertEqual(payload["operations_summary"]["draft_recommendation_count"], 1)

    def test_runtime_source_reports_mixed_when_active_and_paused_combos_coexist(self) -> None:
        request = _fake_request()

        with (
            patch("aats.api.rdp_control_summary._governance_session", _dummy_session),
            patch("aats.data_platform.governance.rdp_task_db.db_get_recent_tasks", return_value=[]),
            patch("aats.api.rdp_control_summary._environment_summary", return_value={"name": "dev"}),
            patch("aats.api.rdp_control_summary.query_rdp_health", return_value={"overall_health": "healthy", "checks": [], "blocking_reasons": [], "warnings": []}),
            patch("aats.api.rdp_control_summary._load_recent_gate_results", return_value=[]),
            patch("aats.api.rdp_control_summary._load_recent_releases", return_value=[]),
            patch("aats.api.rdp_control_summary._build_observation_queue", return_value=[]),
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

        self.assertEqual(payload["governance_state"]["parameter_source_mode"], "mixed")
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
