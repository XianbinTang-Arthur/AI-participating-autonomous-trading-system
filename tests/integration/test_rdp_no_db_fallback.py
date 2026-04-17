"""A-0.3 集成测试：DB 不可达时必须抛 / 映射成 503，绝不写 JSON 审计副本。

设计动机（详见 docs/task/rdp_hardening_batch_a_detailed_design.md §5.5）：
上一次 split-brain 事故的根因是 governance 写路径（_db_sync_* / _db_update_*
/ save_release_history）在 DB 不可达时悄悄降级到仅写 JSON——下一次 loader
fallback 会把那份从未成功入真源的 JSON 重新注入系统，导致内存态、JSON、DB
三份真源长期互相矛盾。A-0.3 把 "DB 不可达 = 写路径失败" 定成硬纪律：

1. 底层 helpers 抛 :class:`DBUnavailableError`
2. API 层 exception handler 映射成 HTTP 503
3. JSON 审计副本在 DB 成功之前绝不落盘

本文件覆盖以下端到端场景（全部通过 ``try_governance_db → (None, False)``
打桩模拟 "DB 不可达"，不依赖真实 testcontainers——真机 fault injection 由
WSL2 集成测试流水线单独跑）：

- approve / reject / supersede 遇到 DB 不可达 → 503
- save_release_history DB 不可达 → 抛 DBUnavailableError，JSON 未写
- 503 响应体的 schema 稳定（错误码字符串 + 原始消息），便于运维脚本 parse
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from aats.api.rdp_routes import rdp_router
from aats.data_platform.governance._exceptions import (
    DBConstraintViolation,
    DBUnavailableError,
)


def _install_exception_handlers(app: FastAPI) -> None:
    """和 apps/api_gateway/main.py 里一致的 handler，内嵌到测试 app 避免重复构造。"""

    @app.exception_handler(DBUnavailableError)
    async def _handle_db_unavailable(_request: Request, exc: DBUnavailableError) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"ok": False, "error": "db_unavailable", "message": str(exc)},
        )

    @app.exception_handler(DBConstraintViolation)
    async def _handle_db_constraint_violation(
        _request: Request, exc: DBConstraintViolation,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"ok": False, "error": "db_constraint_violation", "message": str(exc)},
        )


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
    _install_exception_handlers(app)
    return app


def _draft_rec(rec_id: str = "rec_nodb") -> dict:
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


def _write_rec_registry(root, recs: list[dict]) -> None:
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


def _db_down_patch_stack(root):
    """所有写路径的 try_governance_db 都返回 (None, False) —— 模拟 DB 彻底不可达。

    注意必须 patch 到 module 局部引用的 ``try_governance_db``：
    ``aats.data_platform.decision_system.recommendation_registry`` 里的
    ``from ... import try_governance_db`` 会固化成该 module 的 symbol，patch
    源 module 对已经 import 的代码无效。
    """
    stack = ExitStack()
    stack.enter_context(patch(
        "aats.api.rdp_routes._project_root", lambda _request: root,
    ))
    stack.enter_context(patch(
        "aats.data_platform.decision_system.recommendation_registry.try_governance_db",
        lambda: (None, False),
    ))
    stack.enter_context(patch(
        "aats.api.rdp_routes._step2_integrity_blocking_reason",
        lambda _root: None,
    ))
    return stack


# ── API → 503 映射 ──────────────────────────────────────────────────


def test_approve_returns_503_when_db_unreachable(tmp_path) -> None:
    """approve 流程触发 _db_update_rec_status，DB 不可达时必须抛
    DBUnavailableError，由 exception handler 映射成 503。
    """
    app = _build_app()
    _write_rec_registry(tmp_path, [_draft_rec("rec_503_approve")])
    with _db_down_patch_stack(tmp_path):
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            "/rdp/recommendations/rec_503_approve/approve",
            json={"actor": "operator"},
        )
    assert response.status_code == 503
    body = response.json()
    assert body["ok"] is False
    assert body["error"] == "db_unavailable"


def test_reject_returns_503_when_db_unreachable(tmp_path) -> None:
    app = _build_app()
    _write_rec_registry(tmp_path, [_draft_rec("rec_503_reject")])
    with _db_down_patch_stack(tmp_path):
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            "/rdp/recommendations/rec_503_reject/reject",
            json={"actor": "operator"},
        )
    assert response.status_code == 503
    assert response.json()["error"] == "db_unavailable"


def test_supersede_returns_503_when_db_unreachable(tmp_path) -> None:
    app = _build_app()
    _write_rec_registry(tmp_path, [_draft_rec("rec_503_supersede")])
    with _db_down_patch_stack(tmp_path):
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            "/rdp/recommendations/rec_503_supersede/supersede",
            json={"actor": "operator", "superseded_by_id": "rec_new"},
        )
    assert response.status_code == 503
    assert response.json()["error"] == "db_unavailable"


# ── 回滚保证：DB 失败时内存态必须复原 ────────────────────────────


def test_approve_rolls_back_memory_on_db_unavailable(monkeypatch) -> None:
    """helper-level：_db_update_rec_status 抛 DBUnavailableError 时，
    approve_recommendation 必须把 rec 状态回滚成 draft，不让 JSON 落盘时
    带着"approved 但 DB 未 commit"的 ghost。
    """
    from aats.data_platform.decision_system import recommendation_registry as rr

    rec = _draft_rec("rec_rollback_approve")
    registry = {"recommendations": [rec]}

    def _fail_db(*_args, **_kwargs):
        raise DBUnavailableError("simulated DB down")

    monkeypatch.setattr(rr, "_db_update_rec_status", _fail_db)

    import pytest
    with pytest.raises(DBUnavailableError):
        rr.approve_recommendation(registry, "rec_rollback_approve", approved_by="op")

    # 关键断言：DB 失败后内存状态必须回滚到原始 draft
    assert rec["status"] == "draft"
    assert "approved_by" not in rec or rec.get("approved_by") is None
    assert "approved_at" not in rec or rec.get("approved_at") is None


def test_supersede_rolls_back_memory_on_db_unavailable(monkeypatch) -> None:
    from aats.data_platform.decision_system import recommendation_registry as rr

    rec = _draft_rec("rec_rollback_supersede")
    rec["status"] = "approved"
    rec["approved_by"] = "op"
    registry = {"recommendations": [rec]}

    def _fail_db(*_args, **_kwargs):
        raise DBUnavailableError("simulated DB down")

    monkeypatch.setattr(rr, "_db_update_rec_status", _fail_db)

    import pytest
    with pytest.raises(DBUnavailableError):
        rr.supersede_recommendation(
            registry, "rec_rollback_supersede",
            superseded_by_id="rec_new", actor="system",
        )

    assert rec["status"] == "approved"
    assert "superseded_at" not in rec or rec.get("superseded_at") is None


def test_upsert_active_decision_rolls_back_on_db_unavailable(monkeypatch) -> None:
    """upsert_active_decision 有两条路径：existing（restore dict）/ new（pop appended）。
    两条都必须在 DB 失败时回到修改前的状态。
    """
    from aats.data_platform.decision_system import recommendation_registry as rr

    def _fail_db(*_args, **_kwargs):
        raise DBUnavailableError("simulated DB down")

    monkeypatch.setattr(rr, "_db_sync_active_decision", _fail_db)

    # Path 1：new — append 新条目后 DB 失败 → 必须 pop
    import pytest
    registry_new = {"decisions": []}
    with pytest.raises(DBUnavailableError):
        rr.upsert_active_decision(
            registry_new,
            family="independent", timeframe="15m",
            current_status="promote_candidate",
        )
    assert registry_new["decisions"] == [], "new path DB 失败后必须 pop 掉刚 append 的条目"

    # Path 2：existing — 修改 existing dict 后 DB 失败 → 必须 restore 原值
    prev = {
        "family": "independent",
        "symbol": "BTC-USDT-SWAP",
        "timeframe": "15m",
        "combo_key": "independent_15m",
        "current_status": "observe",
        "active_parameter_set_id": "ps_old",
        "last_recommendation_id": "rec_old",
        "last_updated_at": "2026-04-10T00:00:00Z",
        "notes": "old",
    }
    registry_exist = {"decisions": [dict(prev)]}
    with pytest.raises(DBUnavailableError):
        rr.upsert_active_decision(
            registry_exist,
            family="independent", timeframe="15m",
            current_status="promote_candidate",
            active_parameter_set_id="ps_new",
            last_recommendation_id="rec_new",
            notes="new",
        )
    assert registry_exist["decisions"][0] == prev, (
        "existing path DB 失败后必须 restore 原字典"
    )


# ── save_release_history：DB 不可达时不写 JSON ──────────────────────


def test_save_release_history_does_not_write_json_on_db_unavailable(
    monkeypatch, tmp_path,
) -> None:
    """再次强调 §5.4.4 的硬纪律：DB 不可达必须抛，JSON 副本绝不落盘。

    与 tests/unit/test_operational_state_db.py 的
    test_save_release_history_raises_when_db_unavailable 互补：
    这里从 release 流程的业务语义角度验证，那里从基础设施语义验证。
    """
    from aats.data_platform.production_workflow import release_registry as rr

    monkeypatch.setattr(rr, "try_governance_db", lambda: (None, False))

    history = {
        "releases": [
            {
                "release_id": "rel_no_json",
                "family": "independent",
                "timeframe": "1h",
                "combo_key": "independent_1h",
                "recommendation_id": "rec_no_json",
                "parameter_set_id": "ps_no_json",
                "apply_result": "success",
                "observation_status": "observing",
                "observation_window_hours": 24,
            },
        ],
    }

    import pytest
    with pytest.raises(DBUnavailableError):
        rr.save_release_history(history, tmp_path)

    json_path = tmp_path / "artifacts/production_workflow/parameter_release_history.json"
    assert not json_path.exists(), (
        "DB 不可达时 save_release_history 绝不能写 JSON——否则会产生 ghost release"
    )
