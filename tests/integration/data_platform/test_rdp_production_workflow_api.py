from __future__ import annotations

import json
import os
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aats.api.rdp_routes import rdp_router


def _build_runtime() -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(
            operator_auth_enabled=False,
            operator_control_plane_execution_ledger_enabled=False,
            operator_unsafe_write_without_auth=True,
            storage_mode="memory",
        ),
        environment_capabilities=SimpleNamespace(local_only=True),
    )


@contextmanager
def _fake_governance_session():
    yield object()


def test_create_release_api_rejects_prod_skip_gate() -> None:
    app = FastAPI()
    app.include_router(rdp_router)
    app.state.runtime = _build_runtime()

    # /releases/create 入口装了 Step2 integrity gate（见
    # test_create_release_blocked_when_step2_snapshot_incomplete）。本测试关心的
    # 是 "prod + skip_gate = 拒绝" 的业务语义，需要先让 integrity gate 判定为
    # healthy，否则请求会被前置门闸拦下，根本走不到 skip_gate 校验。
    with patch.dict(
        os.environ,
        {"RDP_ENV": "prod"},
        clear=False,
    ), patch(
        "aats.api.rdp_routes._step2_integrity_blocking_reason",
        return_value=None,
    ):
        client = TestClient(app)
        response = client.post(
            "/rdp/releases/create",
            json={
                "recommendation_id": "rec_prod_api",
                "actor": "operator",
                "skip_gate": True,
                "skip_apply": False,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload.get("integrity_blocked") is not True, (
        "本测试不应走到 integrity_blocked 分支；若走到说明 patch 失效"
    )
    assert "requires gate pass" in payload["message"]


def test_trigger_task_api_rejects_release_cycle_under_golden_path_freeze() -> None:
    app = FastAPI()
    app.include_router(rdp_router)
    app.state.runtime = _build_runtime()

    from aats.data_platform.governance.rdp_task_db import WorkflowEnqueueBlockedError

    with (
        patch("aats.api.rdp_routes._governance_session", _fake_governance_session),
        patch(
            "aats.data_platform.governance.rdp_task_db.db_create_task_if_idle",
            side_effect=WorkflowEnqueueBlockedError("golden-path freeze"),
        ),
    ):
        client = TestClient(app)
        response = client.post(
            "/rdp/tasks/trigger",
            json={"workflow": "release_cycle", "actor": "operator"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["workflow"] == "release_cycle"
    assert payload["task_id"] is None
    assert payload["blocked_by_freeze"] is True
    assert "golden-path freeze" in payload["message"]


def test_tuning_review_routes_expose_and_review_pending_proposals(tmp_path) -> None:
    app = FastAPI()
    app.include_router(rdp_router)
    app.state.runtime = _build_runtime()

    root = tmp_path
    (root / "artifacts" / "governance").mkdir(parents=True, exist_ok=True)
    (root / "artifacts" / "governance" / "strategy_tuning_proposals.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-04-16T12:00:00Z",
                "version": 1,
                "proposals": [
                    {
                        "proposal_id": "tprop_demo_1",
                        "created_at": "2026-04-16T12:00:00Z",
                        "combo_key": "directional_1h",
                        "family": "directional",
                        "timeframe": "1h",
                        "parameter": "min_safe_net_edge_bps",
                        "current_value": 2.0,
                        "proposed_value": 1.5,
                        "status": "pending_review",
                        "rationale": "Phase 4 边际为正，但安全边界阻断占主导",
                    },
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (root / "artifacts" / "governance" / "strategy_tuning_overrides.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-04-16T12:00:00Z",
                "combo_overrides": {},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    with (
        patch("aats.api.rdp_routes._project_root", lambda _request: root),
        patch("aats.api.rdp_control_summary._project_root", lambda _request: root),
        patch("aats.data_platform.operations.strategy_tuning_registry.try_governance_db", lambda: (None, False)),
    ):
        client = TestClient(app)

        overview = client.get("/rdp/tuning/overview").json()
        assert overview["pending_review_count"] == 1

        listing = client.get("/rdp/tuning/proposals").json()
        assert listing["total"] == 1
        assert listing["items"][0]["proposal_id"] == "tprop_demo_1"

        approved = client.post(
            "/rdp/tuning/proposals/tprop_demo_1/approve",
            json={"actor": "operator", "notes": "approve"},
        ).json()
        assert approved["ok"] is True

        listing_after = client.get("/rdp/tuning/proposals").json()
        assert listing_after["total"] == 0


def _seed_tuning_registry(root, proposals: list[dict[str, object]]) -> None:
    (root / "artifacts" / "governance").mkdir(parents=True, exist_ok=True)
    (root / "artifacts" / "governance" / "strategy_tuning_proposals.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-04-16T12:00:00Z",
                "version": 1,
                "proposals": proposals,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (root / "artifacts" / "governance" / "strategy_tuning_overrides.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-04-16T12:00:00Z",
                "combo_overrides": {},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def test_tuning_approve_returns_not_found_for_unknown_proposal(tmp_path) -> None:
    """审批不存在的 proposal_id 应返回 ok=False，消息包含“未找到”。"""

    app = FastAPI()
    app.include_router(rdp_router)
    app.state.runtime = _build_runtime()

    root = tmp_path
    _seed_tuning_registry(
        root,
        proposals=[
            {
                "proposal_id": "tprop_exists_1",
                "created_at": "2026-04-16T12:00:00Z",
                "combo_key": "directional_1h",
                "family": "directional",
                "timeframe": "1h",
                "parameter": "min_safe_net_edge_bps",
                "current_value": 2.0,
                "proposed_value": 1.5,
                "status": "pending_review",
                "rationale": "seed",
            },
        ],
    )

    with (
        patch("aats.api.rdp_routes._project_root", lambda _request: root),
        patch("aats.api.rdp_control_summary._project_root", lambda _request: root),
        patch(
            "aats.data_platform.operations.strategy_tuning_registry.try_governance_db",
            lambda: (None, False),
        ),
    ):
        client = TestClient(app)
        response = client.post(
            "/rdp/tuning/proposals/tprop_does_not_exist/approve",
            json={"actor": "operator", "notes": "ghost"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert "未找到" in payload["message"]
    assert payload["proposal"] is None


def test_tuning_reject_returns_not_found_for_unknown_proposal(tmp_path) -> None:
    """拒绝不存在的 proposal_id 同样应返回 ok=False，消息包含“未找到”。"""

    app = FastAPI()
    app.include_router(rdp_router)
    app.state.runtime = _build_runtime()

    root = tmp_path
    _seed_tuning_registry(root, proposals=[])

    with (
        patch("aats.api.rdp_routes._project_root", lambda _request: root),
        patch("aats.api.rdp_control_summary._project_root", lambda _request: root),
        patch(
            "aats.data_platform.operations.strategy_tuning_registry.try_governance_db",
            lambda: (None, False),
        ),
    ):
        client = TestClient(app)
        response = client.post(
            "/rdp/tuning/proposals/tprop_missing/reject",
            json={"actor": "operator", "notes": "ghost"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert "未找到" in payload["message"]
    assert payload["proposal"] is None


def test_tuning_approve_twice_blocks_second_review(tmp_path) -> None:
    """已批准的提案再次批准应返回 ok=False，消息包含“不能继续审核”。"""

    app = FastAPI()
    app.include_router(rdp_router)
    app.state.runtime = _build_runtime()

    root = tmp_path
    _seed_tuning_registry(
        root,
        proposals=[
            {
                "proposal_id": "tprop_once_1",
                "created_at": "2026-04-16T12:00:00Z",
                "combo_key": "directional_1h",
                "family": "directional",
                "timeframe": "1h",
                "parameter": "min_safe_net_edge_bps",
                "current_value": 2.0,
                "proposed_value": 1.5,
                "status": "pending_review",
                "rationale": "seed",
            },
        ],
    )

    with (
        patch("aats.api.rdp_routes._project_root", lambda _request: root),
        patch("aats.api.rdp_control_summary._project_root", lambda _request: root),
        patch(
            "aats.data_platform.operations.strategy_tuning_registry.try_governance_db",
            lambda: (None, False),
        ),
    ):
        client = TestClient(app)

        first = client.post(
            "/rdp/tuning/proposals/tprop_once_1/approve",
            json={"actor": "operator", "notes": "first"},
        ).json()
        assert first["ok"] is True
        assert first["proposal"]["status"] == "approved"

        second = client.post(
            "/rdp/tuning/proposals/tprop_once_1/approve",
            json={"actor": "operator", "notes": "second"},
        ).json()

    assert second["ok"] is False
    assert "不能继续审核" in second["message"]
    # 提案状态保持 approved，不被二次写入
    assert second["proposal"]["status"] == "approved"


def test_tuning_reject_then_approve_is_blocked(tmp_path) -> None:
    """先拒绝的提案再调用 approve 时应返回 ok=False，消息包含“不能继续审核”。"""

    app = FastAPI()
    app.include_router(rdp_router)
    app.state.runtime = _build_runtime()

    root = tmp_path
    _seed_tuning_registry(
        root,
        proposals=[
            {
                "proposal_id": "tprop_reject_1",
                "created_at": "2026-04-16T12:00:00Z",
                "combo_key": "directional_1h",
                "family": "directional",
                "timeframe": "1h",
                "parameter": "min_safe_net_edge_bps",
                "current_value": 2.0,
                "proposed_value": 1.5,
                "status": "pending_review",
                "rationale": "seed",
            },
        ],
    )

    with (
        patch("aats.api.rdp_routes._project_root", lambda _request: root),
        patch("aats.api.rdp_control_summary._project_root", lambda _request: root),
        patch(
            "aats.data_platform.operations.strategy_tuning_registry.try_governance_db",
            lambda: (None, False),
        ),
    ):
        client = TestClient(app)

        rejected = client.post(
            "/rdp/tuning/proposals/tprop_reject_1/reject",
            json={"actor": "operator", "notes": "nope"},
        ).json()
        assert rejected["ok"] is True
        assert rejected["proposal"]["status"] == "rejected"

        retry_approve = client.post(
            "/rdp/tuning/proposals/tprop_reject_1/approve",
            json={"actor": "operator", "notes": "retry"},
        ).json()

    assert retry_approve["ok"] is False
    assert "不能继续审核" in retry_approve["message"]
    assert retry_approve["proposal"]["status"] == "rejected"


def test_workbench_detail_routes_expose_evidence_and_integrity_block(tmp_path) -> None:
    app = FastAPI()
    app.include_router(rdp_router)
    app.state.runtime = _build_runtime()

    root = tmp_path

    with (
        patch("aats.api.rdp_routes._project_root", lambda _request: root),
        patch("aats.api.rdp_control_summary._project_root", lambda _request: root),
        patch("aats.api.rdp_control_summary._governance_session", _fake_governance_session),
        patch("aats.data_platform.governance.snapshot_db.load_latest_research_round_snapshot", return_value={"round_id": "step2_missing"}),
        patch("aats.data_platform.governance.snapshot_db.is_snapshot_incomplete", return_value=True),
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
                    "recommendation_id": "rec_combo_1",
                    "symbol": "BTC-USDT-SWAP",
                    "family": "directional",
                    "timeframe": "1h",
                    "recommendation_type": "keep_active",
                    "confidence": "low",
                    "reason": "研究结果尚不完整；先不要审批",
                    "status": "draft",
                    "target_parameter_set_id": "ps_candidate_1",
                    "source_round_id": "round_step2_1",
                    "created_at": "2026-04-16T12:00:00Z",
                },
            ],
        }),
        patch("aats.api.rdp_control_summary.query_active_parameter_sets", return_value={
            "generated_at": "2026-04-16T11:40:00Z",
            "governance_managed": True,
            "paused_combos": [],
            "known_combos": ["directional_1h"],
            "active_sets": {},
            "parameter_sets": [],
        }),
        patch("aats.api.rdp_control_summary.query_latest_decision_round", return_value={"available": False}),
        patch("aats.api.rdp_control_summary.query_latest_decisions", return_value={
            "available": True,
            "generated_at": "2026-04-16T11:55:00Z",
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
        patch("aats.api.rdp_control_summary.query_latest_attribution", return_value={"available": True, "round_id": "round_phase3_1", "combos": []}),
        patch("aats.api.rdp_control_summary.query_latest_execution_realism", return_value={"available": True, "round_id": "round_phase4_1", "combos": []}),
    ):
        client = TestClient(app)
        detail = client.get("/rdp/workbench/items/directional_1h").json()
        evidence = client.get("/rdp/workbench/evidence/directional_1h").json()

    assert detail["available"] is True
    assert detail["item"]["approval_enabled"] is False
    assert detail["detail_summary"]["integrity_status"] == "blocked"
    assert evidence["available"] is True
    assert evidence["integrity_status"] == "blocked"
    assert evidence["phase2"]["status"] == "blocked"


def test_rdp_route_chain_updates_control_summary_after_release_and_rollback(tmp_path) -> None:
    app = FastAPI()
    app.include_router(rdp_router)
    app.state.runtime = _build_runtime()

    root = tmp_path
    (root / "artifacts" / "decision_system").mkdir(parents=True, exist_ok=True)
    (root / "artifacts" / "governance").mkdir(parents=True, exist_ok=True)
    (root / "artifacts" / "production_workflow" / "gates" / "gate_demo_1").mkdir(parents=True, exist_ok=True)

    (root / "artifacts" / "decision_system" / "recommendation_registry.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-04-16T11:50:00Z",
                "version": 1,
                "recommendations": [
                    {
                        "recommendation_id": "rec_demo_1",
                        "created_at": "2026-04-16T11:55:00Z",
                        "family": "independent",
                        "symbol": "BTC-USDT-SWAP",
                        "timeframe": "15m",
                        "recommendation_type": "parameter_upgrade",
                        "target_parameter_set_id": "ps_candidate_1",
                        "confidence": "high",
                        "reason": "候选参数已生成，可审批",
                        "status": "draft",
                    },
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (root / "artifacts" / "governance" / "current_parameter_registry.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-04-16T11:45:00Z",
                "version": 1,
                "parameter_sets": [
                    {
                        "parameter_set_id": "ps_live_0",
                        "family": "independent",
                        "timeframe": "15m",
                        "status": "frozen",
                        "source_round_id": "round_prev",
                        "values": {"entry_threshold": 0.4},
                    },
                    {
                        "parameter_set_id": "ps_candidate_1",
                        "family": "independent",
                        "timeframe": "15m",
                        "status": "candidate",
                        "source_round_id": "round_demo",
                        "values": {"entry_threshold": 0.42},
                    },
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    active_state = {
        "active_sets": {
            "independent_15m": {
                "parameter_set_id": "ps_live_0",
                "family": "independent",
                "timeframe": "15m",
                "status": "active",
                "applied_at": "2026-04-16T11:40:00Z",
                "applied_by": "operator",
                "approval_recommendation_id": "rec_prev",
                "source_round_id": "round_prev",
                "values": {"entry_threshold": 0.4},
            },
        },
    }

    def _active_summary() -> dict[str, object]:
        active_sets = active_state["active_sets"]
        return {
            "generated_at": "2026-04-16T12:00:00Z",
            "governance_managed": True,
            "paused_combos": [],
            "known_combos": ["independent_15m"],
            "active_combos": sorted(active_sets.keys()),
            "missing_combos": [],
            "total_active_sets": len(active_sets),
            "active_sets": active_sets,
            "parameter_sets": [
                {
                    "combo_key": combo_key,
                    "family": entry["family"],
                    "timeframe": entry["timeframe"],
                    "parameter_set_id": entry["parameter_set_id"],
                    "status": entry.get("status", "active"),
                    "applied_at": entry.get("applied_at"),
                    "applied_by": entry.get("applied_by"),
                    "approval_recommendation_id": entry.get("approval_recommendation_id"),
                    "source_round_id": entry.get("source_round_id"),
                    "parameter_count": len(entry.get("values", {})),
                    "values": entry.get("values", {}),
                }
                for combo_key, entry in active_sets.items()
            ],
        }

    def _fake_gate(project_root, recommendation_id):
        result = {
            "gate_run_id": "gate_demo_1",
            "recommendation_id": recommendation_id,
            "created_at": "2026-04-16T12:04:00Z",
            "gate_status": "pass",
            "allow_apply": True,
            "blocking_reasons": [],
            "warnings": [],
            "checks": [],
        }
        gate_path = project_root / "artifacts" / "production_workflow" / "gates" / "gate_demo_1" / "pre_apply_gate_result.json"
        gate_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    def _fake_apply(project_root, *, recommendation_id, actor="operator", notes=None, dry_run=False, release_id=None, gate_result=None):
        active_state["active_sets"]["independent_15m"] = {
            "parameter_set_id": "ps_candidate_1",
            "family": "independent",
            "timeframe": "15m",
            "status": "active",
            "applied_at": "2026-04-16T12:05:00Z",
            "applied_by": actor,
            "approval_recommendation_id": recommendation_id,
            "source_round_id": "round_demo",
            "values": {"entry_threshold": 0.42},
        }
        return {
            "ok": True,
            "message": "apply success",
            "operation_type": "apply",
            "combo_key": "independent_15m",
            "family": "independent",
            "timeframe": "15m",
            "recommendation_id": recommendation_id,
            "parameter_set_id": "ps_candidate_1",
            "release_id": release_id,
        }

    def _fake_rollback(project_root, *, family, timeframe, to_parameter_set_id=None, actor="operator", notes=None, dry_run=False):
        active_state["active_sets"]["independent_15m"] = {
            "parameter_set_id": "ps_live_0",
            "family": family,
            "timeframe": timeframe,
            "status": "active",
            "applied_at": "2026-04-16T12:10:00Z",
            "applied_by": actor,
            "approval_recommendation_id": "rec_prev",
            "source_round_id": "round_prev",
            "values": {"entry_threshold": 0.4},
        }
        return {
            "ok": True,
            "message": "rollback success",
            "family": family,
            "timeframe": timeframe,
            "to_parameter_set_id": "ps_live_0",
        }

    with (
        patch("aats.api.rdp_routes._project_root", lambda _request: root),
        patch("aats.api.rdp_routes._governance_session", _fake_governance_session),
        patch("aats.api.rdp_control_summary._project_root", lambda _request: root),
        patch("aats.api.rdp_control_summary._governance_session", _fake_governance_session),
        patch("aats.api.rdp_control_summary._environment_summary", return_value={
            "name": "dev",
            "strict_environment": False,
            "description": "开发环境",
            "require_gate_pass": False,
            "require_approval": False,
            "allow_parameter_rollback": True,
            "direct_apply_allowed": True,
            "required_observation_window_hours": 24,
        }),
        patch("aats.api.rdp_control_summary.query_rdp_health", return_value={
            "overall_health": "healthy",
            "blocking_reasons": [],
            "warnings": [],
            "checks": [],
        }),
        patch("aats.api.rdp_control_summary.query_active_parameter_sets", side_effect=lambda _root: _active_summary()),
        patch("aats.api.rdp_control_summary.query_latest_decision_round", return_value={"available": False}),
        patch("aats.api.rdp_control_summary.query_latest_decisions", return_value={
            "available": True,
            "generated_at": "2026-04-16T12:00:00Z",
            "status_distribution": {"keep_active": 1},
            "decisions": [],
        }),
        patch("aats.data_platform.decision_system.recommendation_registry.try_governance_db", lambda: (None, False)),
        patch("aats.data_platform.governance.parameter_registry.try_governance_db", lambda: (None, False)),
        patch("aats.data_platform.production_workflow.release_registry.try_governance_db", lambda: (None, False)),
        patch("aats.data_platform.production_workflow.observation_window.try_governance_db", lambda: (None, False)),
        patch("aats.data_platform.production_workflow.pre_apply_gate.run_pre_apply_gate", _fake_gate),
        patch("aats.data_platform.decision_system.active_parameter_apply.apply_approved_recommendation", _fake_apply),
        patch("aats.data_platform.decision_system.active_parameter_apply.rollback_active_parameter_set", _fake_rollback),
        patch(
            "aats.data_platform.governance.rdp_task_db.db_create_task_if_idle",
            return_value=("task_demo_1", None),
        ),
        patch("aats.data_platform.governance.rdp_task_db.db_get_recent_tasks", return_value=[]),
    ):
        client = TestClient(app)

        before = client.get("/rdp/control-summary").json()
        assert before["operations_summary"]["draft_recommendation_count"] == 1
        assert before["operations_summary"]["approved_release_candidate_count"] == 0

        triggered = client.post(
            "/rdp/tasks/trigger",
            json={"workflow": "research_cycle", "actor": "operator"},
        ).json()
        assert triggered["ok"] is True

        approved = client.post(
            "/rdp/recommendations/rec_demo_1/approve",
            json={"actor": "operator", "notes": "manual approve"},
        ).json()
        assert approved["ok"] is True

        after_approve = client.get("/rdp/control-summary").json()
        assert after_approve["operations_summary"]["approved_release_candidate_count"] == 1
        assert len(after_approve["pending_recommendations"]) == 1

        released = client.post(
            "/rdp/releases/create",
            json={
                "recommendation_id": "rec_demo_1",
                "actor": "operator",
                "observation_window_hours": 24,
            },
        ).json()
        assert released["ok"] is True
        release_id = released["release"]["release_id"]

        after_release = client.get("/rdp/control-summary").json()
        assert after_release["operations_summary"]["approved_release_candidate_count"] == 0
        assert after_release["operations_summary"]["observing_release_count"] == 1
        assert after_release["pending_recommendations"] == []
        assert len(after_release["observation_queue"]) == 1
        assert after_release["observation_queue"][0]["release_id"] == release_id
        assert after_release["observation_queue"][0]["is_current_active_release"] is True

        observed = client.post(
            "/rdp/observations/run",
            json={"release_id": release_id, "window_hours": 24},
        ).json()
        assert observed["status"] == "observing"

        rolled_back = client.post(
            "/rdp/parameters/rollback",
            json={"family": "independent", "timeframe": "15m", "actor": "operator"},
        ).json()
        assert rolled_back["ok"] is True

        after_rollback = client.get("/rdp/control-summary").json()
        assert after_rollback["operations_summary"]["approved_release_candidate_count"] == 0
        assert after_rollback["operations_summary"]["observing_release_count"] == 0
        assert after_rollback["pending_recommendations"] == []
        assert after_rollback["observation_queue"] == []
        assert (
            after_rollback["active_parameters"]["independent_15m"]["parameter_set_id"]
            == "ps_live_0"
        )


# ══════════════════════════════════════════════════════════════════
#  H1 回归：Step2 快照不完整时 server-side 审批门闸（tuning / recommendation）
# ══════════════════════════════════════════════════════════════════
#
# 回归原因：之前 UI 把 action.enabled 标为 False 只能阻止按钮点击，任何绕过 UI
# 的调用（curl / 脚本 / 重放）都能把 blocked 提案批准下去。必须在 server 端
# 重新做同样的 gate，且 reject 路径不应被门闸阻断，否则运营者无法清理脏提案。


def test_tuning_approve_blocked_when_step2_snapshot_incomplete(tmp_path) -> None:
    """Step2 快照不完整时，tuning 提案 approve 必须被 server 端直接拒绝。"""

    app = FastAPI()
    app.include_router(rdp_router)
    app.state.runtime = _build_runtime()

    root = tmp_path
    _seed_tuning_registry(
        root,
        proposals=[
            {
                "proposal_id": "tprop_gated_1",
                "created_at": "2026-04-16T12:00:00Z",
                "combo_key": "directional_1h",
                "family": "directional",
                "timeframe": "1h",
                "parameter": "min_safe_net_edge_bps",
                "current_value": 2.0,
                "proposed_value": 1.5,
                "status": "pending_review",
                "rationale": "seed",
            },
        ],
    )

    with (
        patch("aats.api.rdp_routes._project_root", lambda _request: root),
        patch("aats.api.rdp_control_summary._project_root", lambda _request: root),
        patch(
            "aats.data_platform.operations.strategy_tuning_registry.try_governance_db",
            lambda: (None, False),
        ),
        # 让两条路径的 snapshot 查询都返回"不完整"
        patch(
            "aats.data_platform.governance.snapshot_db.load_latest_research_round_snapshot",
            return_value={"round_id": "round_step2_dirty", "manifest_synthesized": True},
        ),
        patch(
            "aats.data_platform.governance.snapshot_db.is_snapshot_incomplete",
            return_value=True,
        ),
    ):
        client = TestClient(app)
        response = client.post(
            "/rdp/tuning/proposals/tprop_gated_1/approve",
            json={"actor": "operator", "notes": "bypass attempt"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False, "Step2 不完整时 approve 必须被拒绝"
    assert payload.get("integrity_blocked") is True, \
        "必须通过 integrity_blocked=True 让调用方区分完整性阻断和其他失败"
    assert "不完整" in payload["message"]
    # 提案状态不能被偷偷改写
    assert payload["proposal"]["status"] == "pending_review"


def test_tuning_reject_still_allowed_when_step2_snapshot_incomplete(tmp_path) -> None:
    """reject 永远不受 integrity gate 限制——运营者必须能清理队列里过期/脏的提案。"""

    app = FastAPI()
    app.include_router(rdp_router)
    app.state.runtime = _build_runtime()

    root = tmp_path
    _seed_tuning_registry(
        root,
        proposals=[
            {
                "proposal_id": "tprop_reject_gated_1",
                "created_at": "2026-04-16T12:00:00Z",
                "combo_key": "directional_1h",
                "family": "directional",
                "timeframe": "1h",
                "parameter": "min_safe_net_edge_bps",
                "current_value": 2.0,
                "proposed_value": 1.5,
                "status": "pending_review",
                "rationale": "seed",
            },
        ],
    )

    with (
        patch("aats.api.rdp_routes._project_root", lambda _request: root),
        patch("aats.api.rdp_control_summary._project_root", lambda _request: root),
        patch(
            "aats.data_platform.operations.strategy_tuning_registry.try_governance_db",
            lambda: (None, False),
        ),
        patch(
            "aats.data_platform.governance.snapshot_db.load_latest_research_round_snapshot",
            return_value={"round_id": "round_step2_dirty", "manifest_synthesized": True},
        ),
        patch(
            "aats.data_platform.governance.snapshot_db.is_snapshot_incomplete",
            return_value=True,
        ),
    ):
        client = TestClient(app)
        response = client.post(
            "/rdp/tuning/proposals/tprop_reject_gated_1/reject",
            json={"actor": "operator", "notes": "清理过期"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True, "reject 不受 Step2 完整性门闸限制"
    assert payload["proposal"]["status"] == "rejected"


def test_recommendation_approve_blocked_when_step2_snapshot_incomplete(tmp_path) -> None:
    """Step2 快照不完整时，recommendation approve 也必须被 server 端直接拒绝。"""

    app = FastAPI()
    app.include_router(rdp_router)
    app.state.runtime = _build_runtime()

    root = tmp_path
    (root / "artifacts" / "decision_system").mkdir(parents=True, exist_ok=True)
    (root / "artifacts" / "decision_system" / "recommendation_registry.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-04-16T11:50:00Z",
                "version": 1,
                "recommendations": [
                    {
                        "recommendation_id": "rec_gated_1",
                        "created_at": "2026-04-16T11:55:00Z",
                        "family": "independent",
                        "symbol": "BTC-USDT-SWAP",
                        "timeframe": "15m",
                        "recommendation_type": "parameter_upgrade",
                        "target_parameter_set_id": "ps_candidate_1",
                        "confidence": "high",
                        "status": "draft",
                    },
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    with (
        patch("aats.api.rdp_routes._project_root", lambda _request: root),
        patch("aats.api.rdp_control_summary._project_root", lambda _request: root),
        patch(
            "aats.data_platform.governance.snapshot_db.load_latest_research_round_snapshot",
            return_value={"round_id": "round_step2_dirty", "manifest_synthesized": True},
        ),
        patch(
            "aats.data_platform.governance.snapshot_db.is_snapshot_incomplete",
            return_value=True,
        ),
    ):
        client = TestClient(app)
        response = client.post(
            "/rdp/recommendations/rec_gated_1/approve",
            json={"actor": "operator", "notes": "bypass attempt"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload.get("integrity_blocked") is True
    assert "不完整" in payload["message"]

    # 验证 recommendation_registry.json 里状态仍然是 draft，没有被写为 approved
    registry_contents = json.loads(
        (root / "artifacts/decision_system/recommendation_registry.json").read_text(
            encoding="utf-8",
        )
    )
    recs = registry_contents.get("recommendations", [])
    assert recs and recs[0]["status"] == "draft", \
        "Integrity gate 必须阻止 registry 被 approve"


def test_apply_parameter_blocked_when_step2_snapshot_incomplete(tmp_path) -> None:
    """Step2 快照不完整时，/parameters/apply 必须被 server 端直接拒绝。

    approve 时已有门闸，但 approve → apply 之间 Step2 仍可能 degrade；UI 会
    同步禁用按钮，但 curl / 脚本 / 回放请求能绕过。apply 会把已批准的参数推
    到 active_parameter_sets，影响面大于 approve 本身，server 必须再校验。
    """
    app = FastAPI()
    app.include_router(rdp_router)
    app.state.runtime = _build_runtime()

    root = tmp_path

    # 不需要写任何 recommendation_registry：gate 在调用 apply 前就应返回
    with (
        patch("aats.api.rdp_routes._project_root", lambda _request: root),
        patch(
            "aats.data_platform.governance.snapshot_db.load_latest_research_round_snapshot",
            return_value={"round_id": "round_step2_dirty", "manifest_synthesized": True},
        ),
        patch(
            "aats.data_platform.governance.snapshot_db.is_snapshot_incomplete",
            return_value=True,
        ),
        # 如果 gate 未触发、走进真实 apply 实现，必须直接炸开而不是静默走掉
        patch(
            "aats.data_platform.decision_system.active_parameter_apply."
            "apply_approved_recommendation",
            side_effect=AssertionError(
                "apply_approved_recommendation 不能在 integrity gate 命中时被调用"
            ),
        ),
    ):
        client = TestClient(app)
        response = client.post(
            "/rdp/parameters/apply",
            json={
                "recommendation_id": "rec_apply_bypass_attempt",
                "actor": "operator",
                "notes": "bypass attempt via curl",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload.get("integrity_blocked") is True, (
        "apply 必须带 integrity_blocked=True，否则前端无法区分"
        " Step2 降级 vs 业务失败"
    )
    assert "不完整" in payload["message"]


def test_create_release_blocked_when_step2_snapshot_incomplete(tmp_path) -> None:
    """Step2 快照不完整时，/releases/create 必须被 server 端直接拒绝。

    /releases/create 触发 gate + apply 完整链路，影响面比单独 apply 更大
    （会生成 parameter_release 行并进入 observation 阶段），必须有门闸。
    """
    app = FastAPI()
    app.include_router(rdp_router)
    app.state.runtime = _build_runtime()

    root = tmp_path

    with (
        patch("aats.api.rdp_routes._project_root", lambda _request: root),
        patch(
            "aats.data_platform.governance.snapshot_db.load_latest_research_round_snapshot",
            return_value={"round_id": "round_step2_dirty", "manifest_synthesized": True},
        ),
        patch(
            "aats.data_platform.governance.snapshot_db.is_snapshot_incomplete",
            return_value=True,
        ),
        patch(
            "aats.data_platform.production_workflow.release_registry."
            "create_parameter_release",
            side_effect=AssertionError(
                "create_parameter_release 不能在 integrity gate 命中时被调用"
            ),
        ),
    ):
        client = TestClient(app)
        response = client.post(
            "/rdp/releases/create",
            json={
                "recommendation_id": "rec_release_bypass_attempt",
                "actor": "operator",
                "observation_window_hours": 24,
                "skip_gate": False,
                "skip_apply": False,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload.get("integrity_blocked") is True, (
        "release/create 必须带 integrity_blocked=True"
    )
    assert "不完整" in payload["message"]


def test_rollback_parameter_not_blocked_when_step2_snapshot_incomplete(tmp_path) -> None:
    """反契约：rollback 是安全回滚动作，Step2 降级时仍必须可用。

    如果哪天有人"顺手"给 /parameters/rollback 加了 integrity gate，这个测试会失败。
    防止把"应急止血"路径一起锁死。
    """
    app = FastAPI()
    app.include_router(rdp_router)
    app.state.runtime = _build_runtime()

    root = tmp_path

    with (
        patch("aats.api.rdp_routes._project_root", lambda _request: root),
        patch(
            "aats.data_platform.governance.snapshot_db.load_latest_research_round_snapshot",
            return_value={"round_id": "round_step2_dirty", "manifest_synthesized": True},
        ),
        patch(
            "aats.data_platform.governance.snapshot_db.is_snapshot_incomplete",
            return_value=True,
        ),
        patch(
            "aats.data_platform.decision_system.active_parameter_apply."
            "rollback_active_parameter_set",
            return_value={"ok": True, "rolled_back_to": "ps_prev"},
        ),
    ):
        client = TestClient(app)
        response = client.post(
            "/rdp/parameters/rollback",
            json={
                "family": "independent",
                "timeframe": "15m",
                "to_parameter_set_id": "ps_prev",
                "actor": "operator",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload.get("integrity_blocked") is not True, (
        "rollback 是安全止血动作，Step2 降级时仍必须可用，不能被 integrity gate 锁死"
    )
    assert payload["ok"] is True


# ======================================================================
# P0-1 阶段 B：approve/reject/supersede 的 404 / 409 精确化契约
# ======================================================================
#
# 背景：旧代码对"找不到 rec / 状态已改 / CAS 并发改写"一律返回 200 + ok=False，
# 前端看到的是 "200 OK but ok: false"，会把 UI 状态置为成功或模糊失败，
# 让 operator 不知道到底是"点错了"还是"别人已经点了"。
#
# 阶段 B 改造后：
# - rec 不存在 → 404
# - rec 状态不在 expected_current_status → 409（带 current_status / expected_status）
# - 预检通过但底层 CAS 失败（竞态）→ 409（reason=cas_race）
#
# 下列测试 patch ``try_governance_db`` 为 (None, False)，让 load/save 走 JSON-only
# 路径，避免在 Windows 单测里跑 testcontainers；WSL2 集成测试走真实 Postgres。
# ======================================================================


def _write_rec_registry(root, recs: list[dict]) -> None:
    """把给定 recommendation 列表写成 registry.json。"""
    (root / "artifacts" / "decision_system").mkdir(parents=True, exist_ok=True)
    (root / "artifacts" / "decision_system" / "recommendation_registry.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-04-17T00:00:00Z",
                "version": 1,
                "recommendations": recs,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _draft_rec(rec_id: str = "rec_phase_b_1") -> dict:
    return {
        "recommendation_id": rec_id,
        "created_at": "2026-04-17T00:01:00Z",
        "family": "independent",
        "symbol": "BTC-USDT-SWAP",
        "timeframe": "15m",
        "recommendation_type": "parameter_upgrade",
        "target_parameter_set_id": "ps_candidate_1",
        "confidence": "high",
        "status": "draft",
    }


def _phase_b_client(root):
    """和 Phase B 所有测试共享的 app + patch 栈。

    - ``_project_root`` 固定到 tmp_path
    - ``try_governance_db`` 强制 (None, False)，让 load/save 走 JSON
    - ``_step2_integrity_blocking_reason`` 返回 None，不让 integrity gate 拦截
    """
    app = FastAPI()
    app.include_router(rdp_router)
    app.state.runtime = _build_runtime()
    return app


_PHASE_B_PATCHES: tuple[str, ...] = (
    "aats.data_platform.decision_system.recommendation_registry.try_governance_db",
)


def _phase_b_patch_stack(root):
    from contextlib import ExitStack
    stack = ExitStack()
    stack.enter_context(patch(
        "aats.api.rdp_routes._project_root", lambda _request: root,
    ))
    stack.enter_context(patch(
        "aats.data_platform.decision_system.recommendation_registry.try_governance_db",
        lambda: (None, False),
    ))
    # A-0.3 之后 DB 不可达会抛 DBUnavailableError，这些 Phase B 测试只关心 API
    # pre-check + 业务逻辑分支（404 / 409 / cas_race / 200），不测 DB 集成，所以
    # 把底层 _db_* 辅助函数打桩成 no-op / 固定返回值，保留原测试意图。
    stack.enter_context(patch(
        "aats.data_platform.decision_system.recommendation_registry._db_sync_recommendation",
        lambda *a, **kw: None,
    ))
    stack.enter_context(patch(
        "aats.data_platform.decision_system.recommendation_registry._db_update_rec_status",
        lambda *a, **kw: True,
    ))
    stack.enter_context(patch(
        "aats.data_platform.decision_system.recommendation_registry._db_sync_active_decision",
        lambda *a, **kw: None,
    ))
    stack.enter_context(patch(
        "aats.api.rdp_routes._step2_integrity_blocking_reason",
        lambda _root: None,
    ))
    return stack


def test_approve_returns_404_when_recommendation_missing(tmp_path) -> None:
    app = _phase_b_client(tmp_path)
    _write_rec_registry(tmp_path, [])  # 空 registry
    with _phase_b_patch_stack(tmp_path):
        client = TestClient(app)
        response = client.post(
            "/rdp/recommendations/rec_does_not_exist/approve",
            json={"actor": "operator"},
        )
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["ok"] is False
    assert detail["recommendation_id"] == "rec_does_not_exist"
    assert "未找到" in detail["message"]


def test_approve_returns_409_when_rec_already_approved(tmp_path) -> None:
    app = _phase_b_client(tmp_path)
    rec = _draft_rec("rec_already_approved")
    rec["status"] = "approved"
    _write_rec_registry(tmp_path, [rec])
    with _phase_b_patch_stack(tmp_path):
        client = TestClient(app)
        response = client.post(
            "/rdp/recommendations/rec_already_approved/approve",
            json={"actor": "operator"},
        )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["ok"] is False
    assert detail["current_status"] == "approved"
    assert detail["expected_status"] == ["draft"]


def test_approve_returns_409_on_cas_race(tmp_path) -> None:
    """预检时是 draft，但底层 ``_db_update_rec_status`` 返回 False（CAS 竞态）。

    由于 ``try_governance_db`` 被 patch 成 (None, False)，默认走不到 CAS
    路径；这里换一种 patch 策略——直接把 ``approve_recommendation`` stub
    成返回 None，模拟 "rec 被并发改写" 的结局。
    """
    app = _phase_b_client(tmp_path)
    _write_rec_registry(tmp_path, [_draft_rec("rec_cas_race_approve")])
    with _phase_b_patch_stack(tmp_path), patch(
        "aats.data_platform.decision_system.recommendation_registry.approve_recommendation",
        return_value=None,
    ):
        client = TestClient(app)
        response = client.post(
            "/rdp/recommendations/rec_cas_race_approve/approve",
            json={"actor": "operator"},
        )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["ok"] is False
    assert detail["reason"] == "cas_race"
    assert detail["recommendation_id"] == "rec_cas_race_approve"


def test_reject_returns_404_when_recommendation_missing(tmp_path) -> None:
    app = _phase_b_client(tmp_path)
    _write_rec_registry(tmp_path, [])
    with _phase_b_patch_stack(tmp_path):
        client = TestClient(app)
        response = client.post(
            "/rdp/recommendations/rec_missing_reject/reject",
            json={"actor": "operator"},
        )
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["ok"] is False
    assert detail["recommendation_id"] == "rec_missing_reject"


def test_reject_returns_409_when_status_is_rejected(tmp_path) -> None:
    app = _phase_b_client(tmp_path)
    rec = _draft_rec("rec_double_reject")
    rec["status"] = "rejected"
    _write_rec_registry(tmp_path, [rec])
    with _phase_b_patch_stack(tmp_path):
        client = TestClient(app)
        response = client.post(
            "/rdp/recommendations/rec_double_reject/reject",
            json={"actor": "operator"},
        )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["current_status"] == "rejected"


def test_reject_returns_409_on_cas_race(tmp_path) -> None:
    app = _phase_b_client(tmp_path)
    _write_rec_registry(tmp_path, [_draft_rec("rec_cas_race_reject")])
    with _phase_b_patch_stack(tmp_path), patch(
        "aats.data_platform.decision_system.recommendation_registry.reject_recommendation",
        return_value=None,
    ):
        client = TestClient(app)
        response = client.post(
            "/rdp/recommendations/rec_cas_race_reject/reject",
            json={"actor": "operator"},
        )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["reason"] == "cas_race"


def test_supersede_returns_404_when_recommendation_missing(tmp_path) -> None:
    app = _phase_b_client(tmp_path)
    _write_rec_registry(tmp_path, [])
    with _phase_b_patch_stack(tmp_path):
        client = TestClient(app)
        response = client.post(
            "/rdp/recommendations/rec_missing_supersede/supersede",
            json={"actor": "operator", "superseded_by_id": "rec_new_1"},
        )
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["recommendation_id"] == "rec_missing_supersede"


def test_supersede_returns_409_when_status_is_terminal(tmp_path) -> None:
    """superseded / rejected 是终态，supersede 必须 409 拒绝。"""
    app = _phase_b_client(tmp_path)
    rec = _draft_rec("rec_terminal_superseded")
    rec["status"] = "superseded"
    _write_rec_registry(tmp_path, [rec])
    with _phase_b_patch_stack(tmp_path):
        client = TestClient(app)
        response = client.post(
            "/rdp/recommendations/rec_terminal_superseded/supersede",
            json={"actor": "operator", "superseded_by_id": "rec_new_1"},
        )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["current_status"] == "superseded"
    assert set(detail["expected_status"]) == {"draft", "approved"}


def test_supersede_allows_approved_state(tmp_path) -> None:
    """approved 是 supersede 的合法前置状态，不能被误判成 409。"""
    app = _phase_b_client(tmp_path)
    rec = _draft_rec("rec_approved_supersede")
    rec["status"] = "approved"
    _write_rec_registry(tmp_path, [rec])
    with _phase_b_patch_stack(tmp_path):
        client = TestClient(app)
        response = client.post(
            "/rdp/recommendations/rec_approved_supersede/supersede",
            json={"actor": "operator", "superseded_by_id": "rec_new_1"},
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["recommendation"]["status"] == "superseded"


def test_supersede_returns_409_on_cas_race(tmp_path) -> None:
    app = _phase_b_client(tmp_path)
    _write_rec_registry(tmp_path, [_draft_rec("rec_cas_race_supersede")])
    with _phase_b_patch_stack(tmp_path), patch(
        "aats.data_platform.decision_system.recommendation_registry.supersede_recommendation",
        return_value=None,
    ):
        client = TestClient(app)
        response = client.post(
            "/rdp/recommendations/rec_cas_race_supersede/supersede",
            json={"actor": "operator", "superseded_by_id": "rec_new_1"},
        )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["reason"] == "cas_race"
