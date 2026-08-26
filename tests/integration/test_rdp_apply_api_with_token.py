"""A-0.5 · 集成测试：apply API 必须携带合法 HMAC apply-token.

设计来源：``docs/task/rdp_hardening_batch_a_detailed_design.md §7.4``。

覆盖 4 个强制 case：

1. ``test_apply_without_token_returns_403`` — 不带 ``X-Rdp-Apply-Token`` 直接返回 403
2. ``test_apply_with_expired_token_returns_403`` — token 过期 → 403 ``invalid_apply_token`` + reason=expired
3. ``test_apply_with_token_of_different_actor_returns_403`` — token.actor ≠ session.identity 且 auth_enabled=True → 403 ``actor_mismatch``
4. ``test_apply_with_valid_token_returns_200`` — 合法 token + actor 一致 → 200 ok=True

额外附送：

- ``test_rollback_without_token_returns_403`` — rollback 路径也必须带 token
- ``test_emit_operator_token_returns_valid_token`` — ``POST /rdp/operator-tokens`` 签发链路自洽

本测试套不走 Postgres，但依赖真实的 apply_token 模块签发 / 校验，测完整的
FastAPI 依赖栈行为。业务层调用（``apply_approved_recommendation``）被 patch
成 fake，测试只关心 token gate 本身。
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aats.api.rdp_apply_token import emit_token
from aats.api.rdp_routes import rdp_router


_TEST_SECRET = "integration-secret-for-apply-token"


@pytest.fixture(autouse=True)
def _set_token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RDP_APPLY_TOKEN_SECRET", _TEST_SECRET)
    # 默认 300s；个别测试会覆盖
    monkeypatch.delenv("RDP_APPLY_TOKEN_TTL_SECONDS", raising=False)


def _build_runtime(*, auth_enabled: bool = False) -> SimpleNamespace:
    """构造 RDP 路由依赖的 app.state.runtime.

    - ``auth_enabled=False`` + ``operator_unsafe_write_without_auth=True`` →
      本地 dev 的宽松写模式；token gate 仍然生效，但 actor-match 不强制
    - ``auth_enabled=True`` → actor-match 生效
    """

    return SimpleNamespace(
        settings=SimpleNamespace(
            operator_auth_enabled=auth_enabled,
            operator_control_plane_execution_ledger_enabled=False,
            operator_unsafe_write_without_auth=not auth_enabled,
            storage_mode="memory",
        ),
        environment_capabilities=SimpleNamespace(local_only=True),
    )


@contextmanager
def _patched_apply_success():
    """把真正的 apply 实现替换成 fake，让 token gate 独立于业务逻辑受测。"""

    def _fake_apply(*_args, **_kwargs):
        return {
            "ok": True,
            "message": "apply success",
            "operation_type": "apply",
        }

    with (
        patch(
            "aats.data_platform.decision_system.active_parameter_apply.apply_approved_recommendation",
            _fake_apply,
        ),
        patch(
            "aats.api.rdp_routes._step2_integrity_blocking_reason",
            return_value=None,
        ),
    ):
        yield


@contextmanager
def _patched_rollback_success():
    def _fake_rollback(*_args, **_kwargs):
        return {
            "ok": True,
            "message": "rollback success",
        }

    with patch(
        "aats.data_platform.decision_system.active_parameter_apply.rollback_active_parameter_set",
        _fake_rollback,
    ):
        yield


def _build_app(*, auth_enabled: bool = False) -> FastAPI:
    app = FastAPI()
    app.include_router(rdp_router)
    app.state.runtime = _build_runtime(auth_enabled=auth_enabled)
    return app


# ── Case 1 ─────────────────────────────────────────────────────────────────


def test_apply_without_token_returns_403() -> None:
    """不带 ``X-Rdp-Apply-Token`` → 403 + code=missing_apply_token。"""
    app = _build_app(auth_enabled=False)

    with _patched_apply_success():
        client = TestClient(app)
        response = client.post(
            "/rdp/parameters/apply",
            json={
                "recommendation_id": "rec_missing_token",
                "actor": "operator",
            },
        )

    assert response.status_code == 403
    detail = response.json()["detail"]
    assert detail["code"] == "missing_apply_token"
    assert detail["action"] == "apply"


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (
            "/rdp/releases/create",
            {"recommendation_id": "rec_release_missing_token", "actor": "operator"},
        ),
        (
            "/rdp/recommendations/rec_combined_missing_token/approve-and-release",
            {"actor": "operator"},
        ),
    ],
)
def test_composite_apply_endpoints_without_token_return_403(
    path: str,
    payload: dict[str, str],
) -> None:
    """所有可能写入 active parameter set 的 API 都必须走 apply token。"""
    client = TestClient(_build_app(auth_enabled=False))
    response = client.post(path, json=payload)

    assert response.status_code == 403
    assert response.json()["detail"] == {
        "code": "missing_apply_token",
        "action": "apply",
    }


def test_composite_release_without_apply_does_not_require_token() -> None:
    """skip_apply=True 只建治理记录，不应要求参数应用令牌。"""
    app = _build_app(auth_enabled=False)
    with patch(
        "aats.api.rdp_routes._step2_integrity_blocking_reason",
        return_value="test integrity stop",
    ):
        response = TestClient(app).post(
            "/rdp/releases/create",
            json={
                "recommendation_id": "rec_skip_apply_without_token",
                "actor": "operator",
                "skip_apply": True,
            },
        )

    assert response.status_code == 200
    assert response.json()["integrity_blocked"] is True


# ── Case 2 ─────────────────────────────────────────────────────────────────


def test_apply_with_expired_token_returns_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """过期 token → 403 + code=invalid_apply_token + reason=expired。"""
    monkeypatch.setenv("RDP_APPLY_TOKEN_TTL_SECONDS", "60")
    # 在签发瞬间把时间定格在 past，然后在请求时恢复 now，让 TTL=60 已失效。
    # 更直接的做法是 emit 时写一个手工构造的过期 token，但 emit_token 会用
    # 真实 time.time；这里选 patch 的方式最贴近生产。
    frozen_past = time.time() - 9999
    with patch("aats.api.rdp_apply_token.time.time", return_value=frozen_past):
        expired_token = emit_token(actor="operator", action="apply")

    app = _build_app(auth_enabled=False)
    with _patched_apply_success():
        client = TestClient(app)
        response = client.post(
            "/rdp/parameters/apply",
            json={
                "recommendation_id": "rec_expired_token",
                "actor": "operator",
            },
            headers={"X-Rdp-Apply-Token": expired_token},
        )

    assert response.status_code == 403
    detail = response.json()["detail"]
    assert detail["code"] == "invalid_apply_token"
    assert detail["reason"] == "expired"
    assert detail["action"] == "apply"


# ── Case 3 ─────────────────────────────────────────────────────────────────


def test_apply_with_token_of_different_actor_returns_403() -> None:
    """auth_enabled=True 时，token.actor ≠ session.identity → 403 actor_mismatch。"""
    app = _build_app(auth_enabled=True)

    # 模拟一个已登录 session：principal.identity = "alice"
    from aats.api.auth import OperatorPrincipal, require_write_access

    def _fake_alice() -> OperatorPrincipal:
        return OperatorPrincipal(
            identity="alice",
            role="operator",
            auth_enabled=True,
            auth_source="session",
        )

    app.dependency_overrides[require_write_access] = _fake_alice

    # 但 token 是 bob 签的
    bob_token = emit_token(actor="bob", action="apply")

    with _patched_apply_success():
        client = TestClient(app)
        response = client.post(
            "/rdp/parameters/apply",
            json={
                "recommendation_id": "rec_actor_mismatch",
                "actor": "alice",
            },
            headers={"X-Rdp-Apply-Token": bob_token},
        )

    assert response.status_code == 403
    detail = response.json()["detail"]
    assert detail["code"] == "actor_mismatch"
    assert detail["session_actor"] == "alice"
    assert detail["token_actor"] == "bob"
    assert detail["action"] == "apply"


# ── Case 4 ─────────────────────────────────────────────────────────────────


def test_apply_with_valid_token_returns_200() -> None:
    """合法 token + 本地 dev 宽松模式 → 200 ok=True。"""
    app = _build_app(auth_enabled=False)
    token = emit_token(actor="operator", action="apply")

    with _patched_apply_success():
        client = TestClient(app)
        response = client.post(
            "/rdp/parameters/apply",
            json={
                "recommendation_id": "rec_valid_token",
                "actor": "operator",
            },
            headers={"X-Rdp-Apply-Token": token},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["operation_type"] == "apply"


# ── 额外：rollback 也必须带 token ───────────────────────────────────────────


def test_rollback_without_token_returns_403() -> None:
    app = _build_app(auth_enabled=False)

    with _patched_rollback_success():
        client = TestClient(app)
        response = client.post(
            "/rdp/parameters/rollback",
            json={
                "family": "independent",
                "timeframe": "15m",
                "actor": "operator",
            },
        )

    assert response.status_code == 403
    detail = response.json()["detail"]
    assert detail["code"] == "missing_apply_token"
    assert detail["action"] == "rollback"


def test_rollback_with_valid_token_returns_200() -> None:
    app = _build_app(auth_enabled=False)
    token = emit_token(actor="operator", action="rollback")

    with _patched_rollback_success():
        client = TestClient(app)
        response = client.post(
            "/rdp/parameters/rollback",
            json={
                "family": "independent",
                "timeframe": "15m",
                "actor": "operator",
            },
            headers={"X-Rdp-Apply-Token": token},
        )

    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_rollback_with_apply_token_returns_403_action_mismatch() -> None:
    """apply token 不能复用到 rollback 路由（action_mismatch）。"""
    app = _build_app(auth_enabled=False)
    apply_token = emit_token(actor="operator", action="apply")

    with _patched_rollback_success():
        client = TestClient(app)
        response = client.post(
            "/rdp/parameters/rollback",
            json={
                "family": "independent",
                "timeframe": "15m",
                "actor": "operator",
            },
            headers={"X-Rdp-Apply-Token": apply_token},
        )

    assert response.status_code == 403
    detail = response.json()["detail"]
    assert detail["code"] == "invalid_apply_token"
    assert detail["reason"].startswith("action_mismatch")


# ── 额外:/rdp/operator-tokens 自洽性 ────────────────────────────────────────


def test_emit_operator_token_endpoint_returns_valid_token() -> None:
    """``POST /rdp/operator-tokens`` 签发的 token 能被 apply 路由验证通过。"""
    app = _build_app(auth_enabled=False)

    client = TestClient(app)
    emit_response = client.post(
        "/rdp/operator-tokens",
        json={"action": "apply"},
    )
    assert emit_response.status_code == 200
    emit_payload = emit_response.json()
    assert emit_payload["action"] == "apply"
    assert emit_payload["ttl_seconds"] >= 60
    assert emit_payload["ttl_seconds"] <= 900
    token = emit_payload["token"]
    assert isinstance(token, str) and token

    with _patched_apply_success():
        apply_response = client.post(
            "/rdp/parameters/apply",
            json={
                "recommendation_id": "rec_emit_self_test",
                "actor": "operator",
            },
            headers={"X-Rdp-Apply-Token": token},
        )

    assert apply_response.status_code == 200
    assert apply_response.json()["ok"] is True


def test_emit_operator_token_rejects_unknown_action() -> None:
    app = _build_app(auth_enabled=False)
    client = TestClient(app)
    response = client.post(
        "/rdp/operator-tokens",
        json={"action": "delete_everything"},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "invalid_action"
    assert "apply" in detail["allowed"]


def test_apply_with_malformed_token_returns_403() -> None:
    app = _build_app(auth_enabled=False)

    with _patched_apply_success():
        client = TestClient(app)
        response = client.post(
            "/rdp/parameters/apply",
            json={
                "recommendation_id": "rec_malformed",
                "actor": "operator",
            },
            headers={"X-Rdp-Apply-Token": "not@a@valid@token!!!"},
        )

    assert response.status_code == 403
    detail = response.json()["detail"]
    assert detail["code"] == "invalid_apply_token"
    assert detail["reason"] == "malformed"
