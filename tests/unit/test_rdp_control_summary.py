from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import aats.api.rdp_control_summary as rdp_control_summary
from aats.api.rdp_control_summary import (
    build_rdp_control_summary,
    build_rdp_workbench_alerts,
    build_rdp_tuning_overview,
    build_rdp_tuning_proposals,
    build_rdp_workbench_item_detail,
    build_rdp_workbench_item_evidence,
    build_rdp_workbench_items,
    build_rdp_workbench_overview,
)
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

    def test_workbench_overview_uses_task_oriented_headline(self) -> None:
        request = _fake_request()

        with (
            patch("aats.api.rdp_control_summary._governance_session", _dummy_session),
            patch(
                "aats.data_platform.governance.rdp_task_db.db_get_recent_tasks",
                return_value=[
                    {
                        "task_id": "task_running_research",
                        "workflow": "research_cycle",
                        "status": "running",
                        "requested_at": "2026-04-10T12:00:00Z",
                        "started_at": "2026-04-10T12:01:00Z",
                    },
                    {
                        "task_id": "task_pending_governance",
                        "workflow": "governance_cycle",
                        "status": "pending",
                        "requested_at": "2026-04-10T12:03:00Z",
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
            patch("aats.api.rdp_control_summary.query_latest_recommendations", return_value={
                "recommendations": [
                    {
                        "recommendation_id": "rec_governance_1",
                        "symbol": "BTC-USDT-SWAP",
                        "family": "directional",
                        "timeframe": "1h",
                        "recommendation_type": "keep_active",
                        "confidence": "medium",
                        "reason": "多维度证据既无明显正面，也无必须升级的理由",
                        "status": "draft",
                        "created_at": "2026-04-10T12:05:00Z",
                    },
                ],
            }),
            patch("aats.api.rdp_control_summary.query_active_parameter_sets", return_value={
                "generated_at": "2026-04-10T11:40:00Z",
                "governance_managed": True,
                "paused_combos": [],
                "known_combos": ["directional_1h"],
                "active_sets": {},
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
                "aats.data_platform.governance.snapshot_db.load_latest_research_round_snapshot",
                return_value=None,
            ),
            patch(
                "aats.data_platform.governance.snapshot_db.is_snapshot_incomplete",
                return_value=False,
            ),
            patch("aats.api.rdp_control_summary.query_latest_attribution", return_value={"available": True}),
            patch("aats.api.rdp_control_summary.query_latest_execution_realism", return_value={"available": True}),
        ):
            payload = build_rdp_workbench_overview(request)

        self.assertEqual(payload["overall_status"], "needs_approval")
        self.assertEqual(payload["summary_counts"]["pending_items"], 1)
        self.assertEqual(payload["current_execution"]["workflow"], "research_cycle")
        self.assertEqual(payload["next_queue"]["workflow"], "governance_cycle")
        self.assertEqual(payload["primary_action"]["label"], "刷新数据")
        self.assertEqual(payload["secondary_actions"][0]["label"], "运行完整 RDP")
        self.assertEqual(len(payload["secondary_actions"]), 1)

    def test_workbench_items_disable_approval_when_integrity_alert_blocks_round(self) -> None:
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
                        "recommendation_id": "rec_upgrade_1",
                        "symbol": "BTC-USDT-SWAP",
                        "family": "independent",
                        "timeframe": "15m",
                        "recommendation_type": "parameter_upgrade",
                        "confidence": "high",
                        "reason": "候选参数已生成，等待人工审批",
                        "status": "draft",
                        "target_parameter_set_id": "ps_candidate_1",
                        "created_at": "2026-04-10T12:05:00Z",
                    },
                ],
            }),
            patch("aats.api.rdp_control_summary.query_active_parameter_sets", return_value={
                "generated_at": "2026-04-10T11:40:00Z",
                "governance_managed": True,
                "paused_combos": [],
                "known_combos": ["independent_15m"],
                "active_sets": {},
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
                "aats.data_platform.governance.snapshot_db.load_latest_research_round_snapshot",
                return_value={"round_id": "step2_incomplete"},
            ),
            patch(
                "aats.data_platform.governance.snapshot_db.is_snapshot_incomplete",
                return_value=True,
            ),
            patch("aats.api.rdp_control_summary.query_latest_attribution", return_value={"available": True}),
            patch("aats.api.rdp_control_summary.query_latest_execution_realism", return_value={"available": True}),
        ):
            payload = build_rdp_workbench_items(request)

        self.assertEqual(payload["total"], 1)
        item = payload["items"][0]
        self.assertEqual(item["integrity_status"], "blocked")
        self.assertEqual(item["actions"][0]["enabled"], False)
        self.assertEqual(item["actions"][0]["label"], "批准参数候选")
        self.assertEqual(item["approval_effect_summary"], "批准后进入待发布，下一步是运行 Gate 或创建发布。")

    def test_workbench_items_explain_keep_active_approval_as_record_only(self) -> None:
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
                        "recommendation_id": "rec_keep_1",
                        "symbol": "BTC-USDT-SWAP",
                        "family": "independent",
                        "timeframe": "1h",
                        "recommendation_type": "keep_active",
                        "confidence": "high",
                        "reason": "多维度正面且无负面",
                        "status": "draft",
                        "created_at": "2026-04-10T12:05:00Z",
                    },
                ],
            }),
            patch("aats.api.rdp_control_summary.query_active_parameter_sets", return_value={
                "generated_at": "2026-04-10T11:40:00Z",
                "governance_managed": True,
                "paused_combos": [],
                "known_combos": ["independent_1h"],
                "active_sets": {},
                "parameter_sets": [],
            }),
            patch("aats.api.rdp_control_summary.query_latest_decision_round", return_value={"available": False}),
            patch("aats.api.rdp_control_summary.query_latest_decisions", return_value={
                "available": True,
                "generated_at": "2026-04-10T11:55:00Z",
                "status_distribution": {"keep_active": 1},
                "decisions": [
                    {
                        "combo_key": "independent_1h",
                        "family": "independent",
                        "timeframe": "1h",
                        "current_status": "keep_active",
                        "active_parameter_set_id": None,
                        "last_updated_at": "2026-04-10T11:55:00Z",
                    },
                ],
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
                "aats.data_platform.governance.snapshot_db.load_latest_research_round_snapshot",
                return_value=None,
            ),
            patch(
                "aats.data_platform.governance.snapshot_db.is_snapshot_incomplete",
                return_value=False,
            ),
            patch("aats.api.rdp_control_summary.query_latest_attribution", return_value={"available": True}),
            patch("aats.api.rdp_control_summary.query_latest_execution_realism", return_value={"available": True}),
        ):
            payload = build_rdp_workbench_items(request)

        item = payload["items"][0]
        self.assertEqual(item["actions"][0]["label"], "同意保持当前")
        self.assertEqual(item["decision_summary"], "这轮先保持不动。现在还没有实盘参数，这次只记录治理结论。")
        self.assertEqual(item["approval_effect_summary"], "批准后只记录“保持当前”，不会进入发布。")

    def test_workbench_items_include_release_candidates_after_parameter_upgrade_is_approved(self) -> None:
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
            patch("aats.api.rdp_control_summary._load_recent_releases", return_value=[]),
            patch("aats.api.rdp_control_summary._build_observation_queue", return_value=[]),
            patch("aats.api.rdp_control_summary.query_latest_recommendations", return_value={
                "recommendations": [
                    {
                        "recommendation_id": "rec_release_ready_1",
                        "symbol": "BTC-USDT-SWAP",
                        "family": "independent",
                        "timeframe": "15m",
                        "recommendation_type": "parameter_upgrade",
                        "confidence": "high",
                        "reason": "已完成审批，准备创建 release",
                        "status": "approved",
                        "target_parameter_set_id": "ps_candidate_2",
                        "created_at": "2026-04-10T12:05:00Z",
                    },
                ],
            }),
            patch("aats.api.rdp_control_summary.query_active_parameter_sets", return_value={
                "generated_at": "2026-04-10T11:40:00Z",
                "governance_managed": True,
                "paused_combos": [],
                "known_combos": ["independent_15m"],
                "active_sets": {},
                "parameter_sets": [],
            }),
            patch("aats.api.rdp_control_summary.query_latest_decision_round", return_value={"available": False}),
            patch("aats.api.rdp_control_summary.query_latest_decisions", return_value={
                "available": True,
                "generated_at": "2026-04-10T11:55:00Z",
                "status_distribution": {"parameter_upgrade": 1},
                "decisions": [],
            }),
            patch("aats.api.rdp_control_summary.query_parameter_registry", return_value={
                "available": True,
                "parameter_sets": [],
            }),
            patch("aats.data_platform.production_workflow.release_registry.load_release_history", return_value={"releases": []}),
            patch("aats.api.rdp_control_summary._load_recent_gate_results", return_value=[]),
            patch("aats.data_platform.governance.snapshot_db.load_latest_research_round_snapshot", return_value=None),
            patch("aats.data_platform.governance.snapshot_db.is_snapshot_incomplete", return_value=False),
            patch("aats.api.rdp_control_summary.query_latest_attribution", return_value={"available": True}),
            patch("aats.api.rdp_control_summary.query_latest_execution_realism", return_value={"available": True}),
        ):
            payload = build_rdp_workbench_items(request)

        self.assertEqual(payload["total"], 0)
        self.assertEqual(payload["release_candidates"]["total"], 1)
        candidate = payload["release_candidates"]["items"][0]
        self.assertEqual(candidate["headline"], "已批准，待发布")
        self.assertEqual(candidate["actions"][0]["ui_action"], "rdp-run-gate")
        self.assertEqual(candidate["actions"][1]["ui_action"], "rdp-create-release")

    def test_tuning_overview_counts_pending_and_active_overrides(self) -> None:
        request = _fake_request()
        with (
            patch(
                "aats.data_platform.operations.strategy_tuning_registry.load_strategy_tuning_registry",
                return_value={
                    "version": 3,
                    "proposals": [
                        {"proposal_id": "tp_1", "status": "pending_review"},
                        {"proposal_id": "tp_2", "status": "approved"},
                    ],
                },
            ),
            patch(
                "aats.data_platform.operations.strategy_tuning_registry.load_strategy_tuning_overrides",
                return_value={
                    "combo_overrides": {
                        "directional_1h": {"min_safe_net_edge_bps": 1.5},
                    },
                },
            ),
        ):
            payload = build_rdp_tuning_overview(request)

        self.assertEqual(payload["pending_review_count"], 1)
        self.assertEqual(payload["approved_count"], 1)
        self.assertEqual(payload["active_override_count"], 1)

    def test_tuning_proposals_blocked_when_step2_snapshot_incomplete(self) -> None:
        """回归：当 Step2 最新快照缺 round_manifest 时，tuning proposals 不得当成
        可直接批准的 actionable 项返回；必须与 workbench_alerts 保持同一信号。
        """
        request = _fake_request()
        with (
            patch(
                "aats.data_platform.operations.strategy_tuning_registry.load_strategy_tuning_registry",
                return_value={
                    "version": 3,
                    "proposals": [
                        {
                            "proposal_id": "tp_1",
                            "status": "pending_review",
                            "combo_key": "independent_15m",
                            "family": "independent",
                            "timeframe": "15m",
                            "parameter": "min_safe_net_edge_bps",
                            "current_value": 2.5,
                            "proposed_value": 2.0,
                            "rationale": "安全边际占主导",
                            "created_at": "2026-04-10T11:00:00Z",
                        },
                    ],
                },
            ),
            patch(
                "aats.data_platform.governance.snapshot_db.load_latest_research_round_snapshot",
                return_value={"round_id": "step2_incomplete"},
            ),
            patch(
                "aats.data_platform.governance.snapshot_db.is_snapshot_incomplete",
                return_value=True,
            ),
        ):
            payload = build_rdp_tuning_proposals(request)

        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["step2_incomplete_reason"], "manifest_missing_on_disk")
        self.assertEqual(len(payload["items"]), 1)
        item = payload["items"][0]
        self.assertEqual(item["integrity_status"], "blocked")
        self.assertFalse(item["approval_enabled"])
        self.assertIsNotNone(item["approval_blocked_reason"])
        self.assertEqual(len(item["integrity_alerts"]), 1)
        self.assertEqual(item["integrity_alerts"][0]["code"], "step2_manifest_missing")
        # approve 按钮必须禁用（并带 disabled_reason），reject 按钮保持可用
        approve_action = next(a for a in item["actions"] if a["key"] == "approve_tuning")
        self.assertFalse(approve_action["enabled"])
        self.assertIsNotNone(approve_action["disabled_reason"])
        reject_action = next(a for a in item["actions"] if a["key"] == "reject_tuning")
        self.assertTrue(reject_action["enabled"])

    def test_tuning_proposals_actionable_when_step2_snapshot_complete(self) -> None:
        """Sanity：Step2 快照完整时 proposals 仍是 actionable（未被过度收紧）。"""
        request = _fake_request()
        with (
            patch(
                "aats.data_platform.operations.strategy_tuning_registry.load_strategy_tuning_registry",
                return_value={
                    "version": 3,
                    "proposals": [
                        {
                            "proposal_id": "tp_1",
                            "status": "pending_review",
                            "combo_key": "independent_15m",
                            "parameter": "min_safe_net_edge_bps",
                            "current_value": 2.5,
                            "proposed_value": 2.0,
                            "rationale": "安全边际占主导",
                            "created_at": "2026-04-10T11:00:00Z",
                        },
                    ],
                },
            ),
            patch(
                "aats.data_platform.governance.snapshot_db.load_latest_research_round_snapshot",
                return_value={"round_id": "step2_ok", "manifest_synthesized": False},
            ),
            patch(
                "aats.data_platform.governance.snapshot_db.is_snapshot_incomplete",
                return_value=False,
            ),
        ):
            payload = build_rdp_tuning_proposals(request)

        self.assertIsNone(payload["step2_incomplete_reason"])
        item = payload["items"][0]
        self.assertEqual(item["integrity_status"], "complete")
        self.assertTrue(item["approval_enabled"])
        self.assertEqual(item["integrity_alerts"], [])
        approve_action = next(a for a in item["actions"] if a["key"] == "approve_tuning")
        self.assertTrue(approve_action["enabled"])

    def test_tuning_overview_downgrades_headline_when_step2_incomplete(self) -> None:
        """回归：overview 的 approvable_count 与 headline 必须反映 Step2 不完整。"""
        request = _fake_request()
        with (
            patch(
                "aats.data_platform.operations.strategy_tuning_registry.load_strategy_tuning_registry",
                return_value={
                    "version": 3,
                    "proposals": [
                        {"proposal_id": "tp_1", "status": "pending_review"},
                        {"proposal_id": "tp_2", "status": "approved"},
                    ],
                },
            ),
            patch(
                "aats.data_platform.operations.strategy_tuning_registry.load_strategy_tuning_overrides",
                return_value={"combo_overrides": {}},
            ),
            patch(
                "aats.data_platform.governance.snapshot_db.load_latest_research_round_snapshot",
                return_value={"round_id": "step2_incomplete"},
            ),
            patch(
                "aats.data_platform.governance.snapshot_db.is_snapshot_incomplete",
                return_value=True,
            ),
        ):
            payload = build_rdp_tuning_overview(request)

        self.assertEqual(payload["pending_review_count"], 1)
        self.assertEqual(payload["approvable_count"], 0)
        self.assertEqual(payload["step2_incomplete_reason"], "manifest_missing_on_disk")
        self.assertIn("不完整", payload["headline"])

    def test_workbench_item_detail_and_evidence_include_drilldown_payload(self) -> None:
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
                        "recommendation_id": "rec_detail_1",
                        "symbol": "BTC-USDT-SWAP",
                        "family": "directional",
                        "timeframe": "1h",
                        "recommendation_type": "keep_active",
                        "confidence": "medium",
                        "reason": "归因失败率偏高；执行边际仍未改善",
                        "status": "draft",
                        "target_parameter_set_id": "ps_candidate_9",
                        "source_round_id": "round_step2_1",
                        "created_at": "2026-04-10T12:05:00Z",
                    },
                ],
            }),
            patch("aats.api.rdp_control_summary.query_active_parameter_sets", return_value={
                "generated_at": "2026-04-10T11:40:00Z",
                "governance_managed": True,
                "paused_combos": [],
                "known_combos": ["directional_1h"],
                "active_sets": {},
                "parameter_sets": [],
            }),
            patch("aats.api.rdp_control_summary.query_latest_decision_round", return_value={"available": False}),
            patch("aats.api.rdp_control_summary.query_latest_decisions", return_value={
                "available": True,
                "generated_at": "2026-04-10T11:55:00Z",
                "status_distribution": {"keep_active": 1},
                "decisions": [
                    {
                        "combo_key": "directional_1h",
                        "family": "directional",
                        "timeframe": "1h",
                        "current_status": "keep_active",
                        "active_parameter_set_id": "ps_live_0",
                        "last_recommendation_id": "rec_detail_1",
                        "last_updated_at": "2026-04-10T11:56:00Z",
                    },
                ],
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
                "aats.data_platform.governance.snapshot_db.load_latest_research_round_snapshot",
                return_value=None,
            ),
            patch(
                "aats.data_platform.governance.snapshot_db.is_snapshot_incomplete",
                return_value=False,
            ),
            patch("aats.api.rdp_control_summary.query_latest_attribution", return_value={
                "available": True,
                "round_id": "round_phase3_1",
                "combos": [
                    {
                        "combo_key": "directional_1h",
                        "summary": {
                            "status": "succeeded",
                            "failure_ratio": 0.84,
                            "failure_count": 2141,
                            "total_events": 2141,
                        },
                    },
                ],
            }),
            patch("aats.api.rdp_control_summary.query_latest_execution_realism", return_value={
                "available": True,
                "round_id": "round_phase4_1",
                "combos": [
                    {
                        "combo_key": "directional_1h",
                        "summary": {
                            "full_fill_ratio": 1.0,
                            "cost_adjusted_edge_proxy_bps": 0.0,
                            "mean_cost_bps": 5.6,
                        },
                    },
                ],
            }),
        ):
            detail = build_rdp_workbench_item_detail(request, "directional_1h")
            evidence = build_rdp_workbench_item_evidence(request, "directional_1h")

        self.assertTrue(detail["available"])
        self.assertEqual(detail["item"]["combo_key"], "directional_1h")
        self.assertIn("phase3_round_id", detail["source_rounds"])
        self.assertTrue(detail["detail_summary"]["risk_summary"])
        self.assertEqual(evidence["phase3"]["round_id"], "round_phase3_1")
        self.assertEqual(evidence["phase4"]["round_id"], "round_phase4_1")
        self.assertEqual(evidence["integrity_status"], "complete")

    def test_workbench_evidence_phase2_round_id_populated_from_decision_round(self) -> None:
        """M7 回归：phase2 证据的 round_id 原本硬编码 None，此处验证它会从
        query_latest_decision_round 的 round_id 补齐，即使每条结论自身没有
        source_round_id。追溯链断一环会让 evidence_bundle 在审批时丢参考。"""
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
                        "recommendation_id": "rec_m7_1",
                        "symbol": "BTC-USDT-SWAP",
                        "family": "directional",
                        "timeframe": "1h",
                        "recommendation_type": "keep_active",
                        "confidence": "medium",
                        "reason": "用于 phase2 round_id 回归",
                        "status": "draft",
                        "target_parameter_set_id": "ps_candidate_m7",
                        # 故意不填 source_round_id，确保 phase2 round_id 不是从
                        # recommendation 侧拿，而是靠 decision_round 补齐。
                        "created_at": "2026-04-10T12:05:00Z",
                    },
                ],
            }),
            patch("aats.api.rdp_control_summary.query_active_parameter_sets", return_value={
                "generated_at": "2026-04-10T11:40:00Z",
                "governance_managed": True,
                "paused_combos": [],
                "known_combos": ["directional_1h"],
                "active_sets": {},
                "parameter_sets": [],
            }),
            # 关键 mock：decision_round 整体带 round_id，但单条 family_timeframe_decisions
            # 既没有 source_round_id 也没有 round_id，必须由 M7 修复的投影逻辑补齐。
            patch("aats.api.rdp_control_summary.query_latest_decision_round", return_value={
                "available": True,
                "round_id": "round_step2_m7",
                "family_timeframe_decisions": [
                    {
                        "combo_key": "directional_1h",
                        "family": "directional",
                        "timeframe": "1h",
                        "decision": "keep_active",
                        "confidence": "medium",
                        "reasons": ["归因失败率偏高"],
                        "signal_summary": {
                            "experiments_with_openings": 3,
                            "max_opening_count": 12,
                            "mean_positive_edge_ratio": 0.41,
                        },
                        # 故意不填 source_round_id / round_id
                    },
                ],
                "parameter_upgrade_candidates": [],
            }),
            patch("aats.api.rdp_control_summary.query_latest_decisions", return_value={
                "available": True,
                "generated_at": "2026-04-10T11:55:00Z",
                "status_distribution": {"keep_active": 1},
                "decisions": [
                    {
                        "combo_key": "directional_1h",
                        "family": "directional",
                        "timeframe": "1h",
                        "current_status": "keep_active",
                        "active_parameter_set_id": "ps_live_0",
                        "last_recommendation_id": None,
                        "last_updated_at": "2026-04-10T11:56:00Z",
                    },
                ],
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
                "aats.data_platform.governance.snapshot_db.load_latest_research_round_snapshot",
                return_value=None,
            ),
            patch(
                "aats.data_platform.governance.snapshot_db.is_snapshot_incomplete",
                return_value=False,
            ),
            patch("aats.api.rdp_control_summary.query_latest_attribution", return_value={
                "available": True,
                "round_id": "round_phase3_m7",
                "combos": [
                    {
                        "combo_key": "directional_1h",
                        "summary": {"status": "succeeded", "failure_ratio": 0.2, "failure_count": 40, "total_events": 200},
                    },
                ],
            }),
            patch("aats.api.rdp_control_summary.query_latest_execution_realism", return_value={
                "available": True,
                "round_id": "round_phase4_m7",
                "combos": [
                    {
                        "combo_key": "directional_1h",
                        "summary": {"full_fill_ratio": 1.0, "cost_adjusted_edge_proxy_bps": 0.0, "mean_cost_bps": 4.0},
                    },
                ],
            }),
        ):
            evidence = build_rdp_workbench_item_evidence(request, "directional_1h")

        self.assertTrue(evidence["available"])
        phase2 = evidence["phase2"]
        self.assertIsNotNone(phase2, "phase2 evidence digest must be present when research conclusion exists")
        self.assertEqual(
            phase2["round_id"],
            "round_step2_m7",
            "phase2 round_id must fall back to decision_round.round_id when item-level keys are absent",
        )
        self.assertEqual(phase2["status"], "available")

    def test_workbench_overview_exposes_specific_disabled_reason_for_running_workflow(self) -> None:
        request = _fake_request()

        with (
            patch("aats.api.rdp_control_summary._governance_session", _dummy_session),
            patch("aats.data_platform.governance.rdp_task_db.db_get_recent_tasks", return_value=[
                {
                    "task_id": "task_running_data",
                    "workflow": "data_maintenance",
                    "status": "running",
                    "requested_by": "scheduler",
                    "requested_at": "2026-04-10T12:00:00Z",
                    "started_at": "2026-04-10T12:01:00Z",
                    "finished_at": None,
                    "exit_code": None,
                    "error_message": None,
                    "log_tail": None,
                },
            ]),
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
            patch("aats.data_platform.production_workflow.release_registry.load_release_history", return_value={"releases": []}),
            patch("aats.data_platform.governance.snapshot_db.load_latest_research_round_snapshot", return_value=None),
            patch("aats.data_platform.governance.snapshot_db.is_snapshot_incomplete", return_value=False),
            patch("aats.api.rdp_control_summary.query_latest_attribution", return_value={"available": True}),
            patch("aats.api.rdp_control_summary.query_latest_execution_realism", return_value={"available": True}),
        ):
            payload = build_rdp_workbench_overview(request)

        self.assertFalse(payload["primary_action"]["enabled"])
        self.assertEqual(
            payload["primary_action"]["disabled_reason"],
            "刷新数据正在执行，完成后才能再次点击。",
        )

    def test_workbench_alerts_dedupe_queue_noise_and_hide_missing_alert_file(self) -> None:
        request = _fake_request()

        with (
            patch("aats.api.rdp_control_summary._governance_session", _dummy_session),
            patch("aats.data_platform.governance.rdp_task_db.db_get_recent_tasks", return_value=[]),
            patch("aats.api.rdp_control_summary._environment_summary", return_value={"name": "prod"}),
            patch("aats.api.rdp_control_summary.query_rdp_health", return_value={
                "overall_health": "blocked",
                "blocking_reasons": [],
                "warnings": [
                    "rdp_task_queue_backlog_or_failures",
                    "current_alerts_missing",
                    "no_active_parameter_sets",
                ],
                "checks": [
                    {
                        "category": "task_queue",
                        "name": "queue_state",
                        "status": "warn",
                        "detail": "pending=0, running=3, failed=315",
                    },
                    {
                        "category": "alerts",
                        "name": "current_alerts",
                        "status": "warn",
                        "detail": "current_alerts.json not found",
                    },
                    {
                        "category": "parameters",
                        "name": "active_parameter_sets",
                        "status": "warn",
                        "detail": "count=0",
                    },
                ],
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
            patch("aats.data_platform.production_workflow.release_registry.load_release_history", return_value={"releases": []}),
            patch("aats.data_platform.governance.snapshot_db.load_latest_research_round_snapshot", return_value=None),
            patch("aats.data_platform.governance.snapshot_db.is_snapshot_incomplete", return_value=False),
            patch("aats.api.rdp_control_summary.query_latest_attribution", return_value={"available": True}),
            patch("aats.api.rdp_control_summary.query_latest_execution_realism", return_value={"available": True}),
        ):
            payload = build_rdp_workbench_alerts(request)

        titles = [item["title"] for item in payload["operational_alerts"]]
        messages = [item["message"] for item in payload["operational_alerts"]]
        self.assertEqual(titles.count("任务队列积压"), 1)
        self.assertTrue(any("执行中 3 条" in message and "失败 315 条" in message for message in messages))
        self.assertFalse(any("current_alerts.json not found" in message for message in messages))
        self.assertIn("当前还没有已生效的实盘参数。先完成治理结论，再决定是否发布。", messages)


# ── P0-2 阶段 D：_load_recent_gate_results 只读 DB，失败要喊出来 ────────


class TestLoadRecentGateResultsDBOnly(TestCase):
    def test_returns_rows_from_db(self) -> None:
        class _OKEngine:
            def dispose(self) -> None:
                return None

        payload = {
            "gate_run_id": "gate_demo",
            "recommendation_id": "rec_demo",
            "created_at": "2026-04-10T10:00:00Z",
            "gate_status": "pass",
            "allow_apply": True,
            "blocking_reasons": [],
            "warnings": [],
            "checks": [],
        }

        with (
            patch(
                "aats.api.rdp_control_summary.try_governance_db",
                return_value=(_OKEngine(), True),
            ),
            patch(
                "aats.data_platform.governance.operational_state_db."
                "db_list_pre_apply_gate_results",
                return_value=[payload],
            ),
        ):
            result = rdp_control_summary._load_recent_gate_results(
                Path("/tmp/nonexistent_project_root")
            )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["gate_run_id"], "gate_demo")
        self.assertTrue(result[0]["allow_apply"])

    def test_raises_when_db_unreachable(self) -> None:
        with patch(
            "aats.api.rdp_control_summary.try_governance_db",
            return_value=(None, False),
        ):
            with self.assertRaises(RuntimeError) as cm:
                rdp_control_summary._load_recent_gate_results(
                    Path("/tmp/nonexistent_project_root")
                )

        self.assertIn("governance DB", str(cm.exception))

    def test_raises_on_db_query_exception(self) -> None:
        class _OKEngine:
            def dispose(self) -> None:
                return None

        def _boom(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("connection reset")

        with (
            patch(
                "aats.api.rdp_control_summary.try_governance_db",
                return_value=(_OKEngine(), True),
            ),
            patch(
                "aats.data_platform.governance.operational_state_db."
                "db_list_pre_apply_gate_results",
                side_effect=_boom,
            ),
        ):
            with self.assertRaises(RuntimeError) as cm:
                rdp_control_summary._load_recent_gate_results(
                    Path("/tmp/nonexistent_project_root")
                )

        self.assertIn("connection reset", str(cm.exception))

    def test_does_not_scan_artifacts_directory(self, ) -> None:
        # Even if artifacts/production_workflow/gates exists on disk with JSON
        # files, they must not be surfaced — DB is the single source of truth.
        import tempfile
        import json as _json

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            gates_dir = root / "artifacts" / "production_workflow" / "gates" / "gate_old"
            gates_dir.mkdir(parents=True)
            (gates_dir / "pre_apply_gate_result.json").write_text(
                _json.dumps(
                    {
                        "gate_run_id": "gate_old",
                        "recommendation_id": "rec_old",
                        "created_at": "2025-01-01T00:00:00Z",
                        "gate_status": "pass",
                        "allow_apply": True,
                    }
                ),
                encoding="utf-8",
            )

            class _OKEngine:
                def dispose(self) -> None:
                    return None

            with (
                patch(
                    "aats.api.rdp_control_summary.try_governance_db",
                    return_value=(_OKEngine(), True),
                ),
                patch(
                    "aats.data_platform.governance.operational_state_db."
                    "db_list_pre_apply_gate_results",
                    return_value=[],
                ),
            ):
                result = rdp_control_summary._load_recent_gate_results(root)

        # DB returned empty; artifact JSON must NOT have been promoted in
        self.assertEqual(result, [])
