"""Path B 单测：``POST /rdp/recommendations/{id}/approve-and-release``.

合并 approve + gate + release + apply 的原子链端点。本文件覆盖以下契约：

1. Happy path：draft → approved → release 成功 → apply 成功；响应把四段合在一起返回
2. Step2 integrity 阻断：返回 ok=False, integrity_blocked=True；recommendation 未变
3. Recommendation 不存在：404
4. Recommendation 已 approved：409（预检阶段就拦下）
5. Approve 阶段 CAS race：409 with reason=cas_race（预检通过但底层 helper 返回 None）
6. Gate 阻断：approve 已落库，release.apply_result=blocked_by_gate，返回 ok=False
7. Apply 失败：approve 已落库，release.apply_result=failed，返回 ok=False
8. skip_apply=True：release 创建但 apply 不触发，approve 已落库
9. Permissions：require_write_access 被强制（FastAPI 依赖栈走到位）

测试通过两层 patch 隔离：
- ``try_governance_db`` → ``(None, False)`` 让 registry 读写走 JSON 副本，不依赖 DB
- ``create_parameter_release`` 被替换成 fake，返回不同 shape 以覆盖 gate/apply 分支
"""

from __future__ import annotations

import json
import pathlib
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(rdp_router)
    app.state.runtime = _build_runtime()
    return app


def _draft_rec(rec_id: str = "rec_draft") -> dict:
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


def _approved_rec(rec_id: str = "rec_approved") -> dict:
    rec = _draft_rec(rec_id)
    rec["status"] = "approved"
    rec["approved_by"] = "operator"
    rec["approved_at"] = "2026-04-17T00:02:00Z"
    return rec


def _write_rec_registry(root: pathlib.Path, recs: list[dict]) -> None:
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


def _read_rec_registry(root: pathlib.Path) -> dict:
    path = root / "artifacts" / "decision_system" / "recommendation_registry.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _patch_stack(
    root: pathlib.Path,
    *,
    integrity_block: str | None = None,
    release_result: dict | None = None,
    db_cas_conflict: bool = False,
):
    """所有 test 共用的 patch 组合。

    - ``integrity_block``：非 None 时 Step2 gate 直接拒绝（让我们测 integrity 分支）
    - ``release_result``：``create_parameter_release`` 的返回值；None 表示不 patch
    - ``db_cas_conflict``：``True`` 时模拟 DB 层 CAS 竞态——``_db_update_rec_status``
      返回 False，approve helper 回滚内存态并返回 None；handler 映射成 409

    注意 ``try_governance_db`` 被 patch 成"DB 可达但返回 fake engine"，再搭配
    ``_db_update_rec_status`` 的 patch，既让读路径命中 DB-first（其实是 stub，
    会 fallback 到 JSON load），也让写路径避开真实 SQL。
    """
    stack = ExitStack()
    stack.enter_context(patch(
        "aats.api.rdp_routes._project_root", lambda _request: root,
    ))
    # Registry 读路径：让 load_recommendation_registry 走 JSON 副本
    stack.enter_context(patch(
        "aats.data_platform.decision_system.recommendation_registry.try_governance_db",
        lambda: (None, False),
    ))
    # Step2 integrity gate
    stack.enter_context(patch(
        "aats.api.rdp_routes._step2_integrity_blocking_reason",
        lambda _root: integrity_block,
    ))
    # Registry 写路径：_db_update_rec_status 默认 True（DB CAS 通过），
    # 除非测试显式要求 CAS 冲突。
    db_return_value = False if db_cas_conflict else True
    stack.enter_context(patch(
        "aats.data_platform.decision_system.recommendation_registry._db_update_rec_status",
        lambda *_a, **_kw: db_return_value,
    ))
    if release_result is not None:
        stack.enter_context(patch(
            "aats.data_platform.production_workflow.release_registry.create_parameter_release",
            lambda *_a, **_kw: release_result,
        ))
    return stack


# ── 1. Happy path ──────────────────────────────────────────────────────────


def test_approve_and_release_happy_path(tmp_path: pathlib.Path) -> None:
    """draft → approved → gate pass → release 创建 → apply 成功：单次 200 ok=True。"""
    app = _build_app()
    _write_rec_registry(tmp_path, [_draft_rec("rec_happy")])

    fake_release = {
        "ok": True,
        "release": {
            "release_id": "rel_happy_1",
            "family": "independent",
            "timeframe": "15m",
            "apply_result": "success",
            "observation_status": "observing",
        },
        "gate_result": {"gate_run_id": "gr_1", "allow_apply": True},
        "apply_result": {"ok": True, "operation_type": "apply"},
        "message": "Release rel_happy_1 created",
    }

    with _patch_stack(tmp_path, release_result=fake_release):
        client = TestClient(app)
        response = client.post(
            "/rdp/recommendations/rec_happy/approve-and-release",
            json={
                "actor": "operator",
                "approval_notes": "looks good",
                "release_notes": "auto-release",
                "observation_window_hours": 24,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["recommendation"]["status"] == "approved"
    assert payload["recommendation"]["review_notes"] == "looks good"
    assert payload["release"]["release_id"] == "rel_happy_1"
    assert payload["gate_result"]["allow_apply"] is True
    assert payload["apply_result"]["ok"] is True

    # registry JSON 被写回：状态持久化为 approved
    persisted = _read_rec_registry(tmp_path)
    assert persisted["recommendations"][0]["status"] == "approved"
    assert persisted["recommendations"][0]["review_notes"] == "looks good"


# ── 2. Integrity block ─────────────────────────────────────────────────────


def test_approve_and_release_integrity_blocked_does_not_mutate(
    tmp_path: pathlib.Path,
) -> None:
    """Step2 integrity gate 阻断 → 全链拒绝；recommendation 保持 draft。"""
    app = _build_app()
    _write_rec_registry(tmp_path, [_draft_rec("rec_integrity")])

    with _patch_stack(
        tmp_path,
        integrity_block="Step2 快照不完整：candle_stats 缺失",
    ):
        client = TestClient(app)
        response = client.post(
            "/rdp/recommendations/rec_integrity/approve-and-release",
            json={"actor": "operator"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["integrity_blocked"] is True
    assert "Step2" in payload["message"]

    # 关键：registry 未变，recommendation 仍是 draft
    persisted = _read_rec_registry(tmp_path)
    assert persisted["recommendations"][0]["status"] == "draft"
    assert "approved_by" not in persisted["recommendations"][0]


# ── 3. Recommendation 不存在 → 404 ─────────────────────────────────────────


def test_approve_and_release_missing_recommendation_404(
    tmp_path: pathlib.Path,
) -> None:
    app = _build_app()
    _write_rec_registry(tmp_path, [_draft_rec("rec_other")])

    with _patch_stack(tmp_path):
        client = TestClient(app)
        response = client.post(
            "/rdp/recommendations/rec_missing/approve-and-release",
            json={"actor": "operator"},
        )

    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["recommendation_id"] == "rec_missing"


# ── 4. 状态不是 draft → 409（预检阶段拦下）────────────────────────────────


def test_approve_and_release_wrong_status_409(tmp_path: pathlib.Path) -> None:
    """已经 approved 的 rec 不能再次 approve-and-release。"""
    app = _build_app()
    _write_rec_registry(tmp_path, [_approved_rec("rec_already_approved")])

    with _patch_stack(tmp_path):
        client = TestClient(app)
        response = client.post(
            "/rdp/recommendations/rec_already_approved/approve-and-release",
            json={"actor": "operator"},
        )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["current_status"] == "approved"
    assert detail["expected_status"] == ["draft"]


# ── 5. Approve 阶段 CAS race → 409 with reason=cas_race ────────────────────


def test_approve_and_release_cas_race_maps_to_409(
    tmp_path: pathlib.Path,
) -> None:
    """预检通过但 DB UPDATE 返回 rowcount=0 (CAS 冲突) → helper 返回 None
    → handler 把它映射成 409。
    """
    app = _build_app()
    _write_rec_registry(tmp_path, [_draft_rec("rec_cas_race")])

    with _patch_stack(tmp_path, db_cas_conflict=True):
        client = TestClient(app)
        response = client.post(
            "/rdp/recommendations/rec_cas_race/approve-and-release",
            json={"actor": "operator"},
        )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["reason"] == "cas_race"
    assert detail["recommendation_id"] == "rec_cas_race"


# ── 6. Gate 阻断 → approve 已落库，release 标记 blocked_by_gate ───────────


def test_approve_and_release_gate_blocks_keeps_approval(
    tmp_path: pathlib.Path,
) -> None:
    """Gate 阻断时，approve 已经落库；release 记录以 blocked_by_gate 标记。"""
    app = _build_app()
    _write_rec_registry(tmp_path, [_draft_rec("rec_gate_block")])

    fake_release = {
        "ok": False,
        "release": {
            "release_id": "rel_gate_block_1",
            "apply_result": "blocked_by_gate",
        },
        "gate_result": {
            "gate_run_id": "gr_blocked",
            "allow_apply": False,
            "blocking_reasons": ["attribution_missing"],
        },
        "message": "Gate blocked: ['attribution_missing']",
    }

    with _patch_stack(tmp_path, release_result=fake_release):
        client = TestClient(app)
        response = client.post(
            "/rdp/recommendations/rec_gate_block/approve-and-release",
            json={"actor": "operator"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["release"]["apply_result"] == "blocked_by_gate"
    assert payload["gate_result"]["allow_apply"] is False

    # 关键：approve 已落库，operator 可以之后单独 apply
    persisted = _read_rec_registry(tmp_path)
    assert persisted["recommendations"][0]["status"] == "approved"


# ── 7. Apply 失败 → approve 已落库，release 标记 failed ───────────────────


def test_approve_and_release_apply_failure_keeps_approval(
    tmp_path: pathlib.Path,
) -> None:
    app = _build_app()
    _write_rec_registry(tmp_path, [_draft_rec("rec_apply_fail")])

    fake_release = {
        "ok": False,
        "release": {
            "release_id": "rel_apply_fail_1",
            "apply_result": "failed",
            "observation_status": "not_started",
        },
        "gate_result": {"gate_run_id": "gr_pass", "allow_apply": True},
        "apply_result": {
            "ok": False,
            "message": "parameter_registry 未找到 target set",
        },
        "message": "Release rel_apply_fail_1 created",
    }

    with _patch_stack(tmp_path, release_result=fake_release):
        client = TestClient(app)
        response = client.post(
            "/rdp/recommendations/rec_apply_fail/approve-and-release",
            json={"actor": "operator"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["release"]["apply_result"] == "failed"
    assert payload["apply_result"]["ok"] is False

    persisted = _read_rec_registry(tmp_path)
    assert persisted["recommendations"][0]["status"] == "approved"


# ── 8. skip_apply=True → release 创建但不触发 apply ───────────────────────


def test_approve_and_release_skip_apply_creates_release_without_applying(
    tmp_path: pathlib.Path,
) -> None:
    app = _build_app()
    _write_rec_registry(tmp_path, [_draft_rec("rec_skip_apply")])

    fake_create_release = MagicMock(return_value={
        "ok": True,  # create_parameter_release 的 result["ok"] 在 run_apply=False 时是 True
        "release": {
            "release_id": "rel_skip_apply_1",
            "apply_result": None,  # 未尝试 apply
        },
        "gate_result": {"gate_run_id": "gr_skip", "allow_apply": True},
        "message": "Release rel_skip_apply_1 created",
    })

    stack = _patch_stack(tmp_path)
    with stack, patch(
        "aats.data_platform.production_workflow.release_registry.create_parameter_release",
        fake_create_release,
    ):
        client = TestClient(app)
        response = client.post(
            "/rdp/recommendations/rec_skip_apply/approve-and-release",
            json={"actor": "operator", "skip_apply": True},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["release"]["release_id"] == "rel_skip_apply_1"

    # 验证 create_parameter_release 被以 run_apply=False 调用
    assert fake_create_release.call_count == 1
    _, kwargs = fake_create_release.call_args
    assert kwargs["run_apply"] is False
    assert kwargs["run_gate"] is True  # skip_gate 默认 False


# ── 9. skip_gate=True 透传 ─────────────────────────────────────────────────


def test_approve_and_release_skip_gate_forwarded(tmp_path: pathlib.Path) -> None:
    app = _build_app()
    _write_rec_registry(tmp_path, [_draft_rec("rec_skip_gate")])

    fake_create_release = MagicMock(return_value={
        "ok": True,
        "release": {"release_id": "rel_skip_gate_1", "apply_result": "success"},
        "gate_result": None,
        "apply_result": {"ok": True},
        "message": "ok",
    })

    with _patch_stack(tmp_path), patch(
        "aats.data_platform.production_workflow.release_registry.create_parameter_release",
        fake_create_release,
    ):
        client = TestClient(app)
        response = client.post(
            "/rdp/recommendations/rec_skip_gate/approve-and-release",
            json={"actor": "operator", "skip_gate": True},
        )

    assert response.status_code == 200
    _, kwargs = fake_create_release.call_args
    assert kwargs["run_gate"] is False
    assert kwargs["run_apply"] is True


# ── 10. observation_window_hours 透传 ─────────────────────────────────────


def test_approve_and_release_observation_window_forwarded(
    tmp_path: pathlib.Path,
) -> None:
    app = _build_app()
    _write_rec_registry(tmp_path, [_draft_rec("rec_obs_window")])

    fake_create_release = MagicMock(return_value={
        "ok": True,
        "release": {"release_id": "rel_obs", "apply_result": "success"},
        "gate_result": None,
        "apply_result": {"ok": True},
        "message": "ok",
    })

    with _patch_stack(tmp_path), patch(
        "aats.data_platform.production_workflow.release_registry.create_parameter_release",
        fake_create_release,
    ):
        client = TestClient(app)
        response = client.post(
            "/rdp/recommendations/rec_obs_window/approve-and-release",
            json={"actor": "operator", "observation_window_hours": 48},
        )

    assert response.status_code == 200
    _, kwargs = fake_create_release.call_args
    assert kwargs["observation_window_hours"] == 48


# ── 11. Permissions: auth_enabled=True 时 actor 绑定 session identity ─────


def test_approve_and_release_binds_actor_to_session_when_auth_enabled(
    tmp_path: pathlib.Path,
) -> None:
    """auth 启用时审计 actor 必须是 session.identity，不能被 body 劫持。"""
    app = _build_app()
    # 覆盖 runtime 切到 auth_enabled=True
    app.state.runtime = SimpleNamespace(
        settings=SimpleNamespace(
            operator_auth_enabled=True,
            operator_control_plane_execution_ledger_enabled=False,
            operator_unsafe_write_without_auth=False,
            storage_mode="memory",
        ),
        environment_capabilities=SimpleNamespace(local_only=True),
    )

    from aats.api.auth import OperatorPrincipal, require_write_access

    def _fake_alice() -> OperatorPrincipal:
        return OperatorPrincipal(
            identity="alice",
            role="operator",
            auth_enabled=True,
            auth_source="session",
        )

    app.dependency_overrides[require_write_access] = _fake_alice

    _write_rec_registry(tmp_path, [_draft_rec("rec_actor_bind")])

    captured: dict[str, str] = {}

    def _capture_release(*_args, **kwargs):
        captured["actor"] = kwargs.get("actor", "")
        return {
            "ok": True,
            "release": {"release_id": "rel_actor_bind"},
            "gate_result": None,
            "apply_result": {"ok": True},
            "message": "ok",
        }

    with _patch_stack(tmp_path), patch(
        "aats.data_platform.production_workflow.release_registry.create_parameter_release",
        _capture_release,
    ):
        client = TestClient(app)
        response = client.post(
            "/rdp/recommendations/rec_actor_bind/approve-and-release",
            json={"actor": "mallory"},  # 尝试劫持
        )

    assert response.status_code == 200
    # 关键：即便 body 写 "mallory"，底层调用的 actor 必须是 "alice"
    assert captured["actor"] == "alice"

    # registry 里的 approved_by 也必须是 "alice"
    persisted = _read_rec_registry(tmp_path)
    assert persisted["recommendations"][0]["approved_by"] == "alice"


# ── 12. Integrity block 发生在 approve 之前（顺序保证）────────────────────


def test_approve_and_release_integrity_check_runs_before_approve(
    tmp_path: pathlib.Path,
) -> None:
    """Step2 gate 在 approve helper 之前就短路；_db_update_rec_status 不应被调用。"""
    app = _build_app()
    _write_rec_registry(tmp_path, [_draft_rec("rec_order_check")])

    db_update_spy = MagicMock(return_value=True)

    stack = ExitStack()
    with stack:
        stack.enter_context(patch(
            "aats.api.rdp_routes._project_root", lambda _request: tmp_path,
        ))
        stack.enter_context(patch(
            "aats.api.rdp_routes._step2_integrity_blocking_reason",
            lambda _root: "data frozen",
        ))
        stack.enter_context(patch(
            "aats.data_platform.decision_system.recommendation_registry._db_update_rec_status",
            db_update_spy,
        ))
        client = TestClient(app)
        response = client.post(
            "/rdp/recommendations/rec_order_check/approve-and-release",
            json={"actor": "operator"},
        )

    assert response.status_code == 200
    assert response.json()["integrity_blocked"] is True
    # DB 写路径（approve 内部的 CAS UPDATE）不应被触发
    assert db_update_spy.call_count == 0
