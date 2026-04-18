"""v2 apply token unit tests — scope + recommendation_id binding (R2-04).

Test matrix:
  1. v2 round-trip (scope='profile') succeeds.
  2. v1 token rejected when caller demands v2 (v2_required).
  3. v2 token with wrong scope rejected (scope_mismatch).
  4. v2 token with wrong rec_id rejected (rec_id_mismatch).
  5. emit_token: scope/rec_id 必须成对提供.
  6. v1 兼容: combo path 不传 required_scope → v1 token 通过.
"""

from __future__ import annotations

import pytest

from aats.api.rdp_apply_token import (
    InvalidTokenError,
    emit_token,
    verify_token,
)


@pytest.fixture(autouse=True)
def _set_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RDP_APPLY_TOKEN_SECRET", "test-secret-v2")
    monkeypatch.delenv("RDP_APPLY_TOKEN_TTL_SECONDS", raising=False)


def test_v2_round_trip_profile() -> None:
    token = emit_token(
        "alice", "apply", scope="profile", recommendation_id="rec-profile-123",
    )
    actor, _exp = verify_token(
        token, "apply",
        required_scope="profile",
        required_recommendation_id="rec-profile-123",
    )
    assert actor == "alice"


def test_v1_token_rejected_when_v2_required() -> None:
    # v1 token (no scope/rec_id)
    token = emit_token("alice", "apply")
    with pytest.raises(InvalidTokenError) as exc:
        verify_token(
            token, "apply",
            required_scope="profile",
            required_recommendation_id="rec-profile-1",
        )
    assert "v2_required" in str(exc.value)


def test_scope_mismatch_rejected() -> None:
    token = emit_token(
        "alice", "apply", scope="combo", recommendation_id="rec-combo-1",
    )
    with pytest.raises(InvalidTokenError) as exc:
        verify_token(
            token, "apply",
            required_scope="profile",
            required_recommendation_id="rec-combo-1",
        )
    assert "scope_mismatch" in str(exc.value)


def test_rec_id_mismatch_rejected() -> None:
    token = emit_token(
        "alice", "apply", scope="profile", recommendation_id="rec-A",
    )
    with pytest.raises(InvalidTokenError) as exc:
        verify_token(
            token, "apply",
            required_scope="profile",
            required_recommendation_id="rec-B",
        )
    assert "rec_id_mismatch" in str(exc.value)


def test_emit_requires_pair() -> None:
    # scope 传了 rec_id 没传 → ValueError
    with pytest.raises(ValueError):
        emit_token("alice", "apply", scope="profile")


def test_verify_requires_pair() -> None:
    token = emit_token(
        "alice", "apply", scope="profile", recommendation_id="rec-X",
    )
    with pytest.raises(ValueError):
        verify_token(
            token, "apply", required_scope="profile",
            # 缺少 required_recommendation_id
        )


def test_v1_backward_compat_combo() -> None:
    """combo path 不传 required_scope → v1 token 仍然通过(向后兼容)。"""
    token = emit_token("alice", "apply")  # v1
    actor, _exp = verify_token(token, "apply")  # 不传 scope
    assert actor == "alice"
