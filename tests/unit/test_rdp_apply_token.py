"""A-0.5 · unit coverage for the HMAC apply-token module.

Covers the 6 cases from ``docs/task/rdp_hardening_batch_a_detailed_design.md §7.4``:

1. emit + verify round-trip on the happy path
2. expired token → ``expired``
3. wrong action → ``action_mismatch``
4. tampered signature → ``bad_sig``
5. malformed token bytes → ``malformed``
6. TTL is clamped to the [60, 900] seconds range
"""

from __future__ import annotations

import base64
import time
from unittest.mock import patch

import pytest

from aats.api.rdp_apply_token import (
    InvalidTokenError,
    emit_token,
    ttl_seconds,
    verify_token,
)


_FAKE_SECRET = "test-secret-unit"


@pytest.fixture(autouse=True)
def _set_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RDP_APPLY_TOKEN_SECRET", _FAKE_SECRET)
    # Default TTL is intentionally left unset so each test controls it
    monkeypatch.delenv("RDP_APPLY_TOKEN_TTL_SECONDS", raising=False)


def test_emit_and_verify_ok() -> None:
    token = emit_token("alice", "apply")
    actor, exp_ts = verify_token(token, "apply")
    assert actor == "alice"
    assert exp_ts > int(time.time())


def test_expired_token_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # TTL=60s, then fast-forward past expiry
    monkeypatch.setenv("RDP_APPLY_TOKEN_TTL_SECONDS", "60")
    token = emit_token("alice", "apply")
    future = time.time() + 61
    with patch("aats.api.rdp_apply_token.time.time", return_value=future):
        with pytest.raises(InvalidTokenError) as exc_info:
            verify_token(token, "apply")
    assert "expired" in str(exc_info.value)


def test_wrong_action_rejected() -> None:
    token = emit_token("alice", "apply")
    with pytest.raises(InvalidTokenError) as exc_info:
        verify_token(token, "rollback")
    assert "action_mismatch" in str(exc_info.value)


def test_tampered_sig_rejected() -> None:
    token = emit_token("alice", "apply")
    raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
    actor, action, exp_ts, sig = raw.split("|")
    # Flip the last byte of the signature
    tampered_sig = sig[:-1] + ("0" if sig[-1] != "0" else "1")
    tampered_raw = f"{actor}|{action}|{exp_ts}|{tampered_sig}"
    tampered = base64.urlsafe_b64encode(
        tampered_raw.encode("utf-8")
    ).decode("ascii")
    with pytest.raises(InvalidTokenError) as exc_info:
        verify_token(tampered, "apply")
    assert str(exc_info.value) == "bad_sig"


def test_malformed_token_rejected() -> None:
    # Not base64 at all
    with pytest.raises(InvalidTokenError) as exc_info:
        verify_token("not@valid@base64!!!", "apply")
    assert str(exc_info.value) == "malformed"

    # Base64 but wrong number of fields
    weird = base64.urlsafe_b64encode(b"a|b|c").decode("ascii")
    with pytest.raises(InvalidTokenError) as exc_info:
        verify_token(weird, "apply")
    assert str(exc_info.value) == "malformed"


def test_ttl_clamped_to_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    # Below lower bound → clamp to 60
    monkeypatch.setenv("RDP_APPLY_TOKEN_TTL_SECONDS", "5")
    assert ttl_seconds() == 60

    # Above upper bound → clamp to 900
    monkeypatch.setenv("RDP_APPLY_TOKEN_TTL_SECONDS", "10000")
    assert ttl_seconds() == 900

    # Non-integer → fall back to default
    monkeypatch.setenv("RDP_APPLY_TOKEN_TTL_SECONDS", "not_a_number")
    assert ttl_seconds() == 300


def test_emit_rejects_unknown_action() -> None:
    with pytest.raises(ValueError):
        emit_token("alice", "delete_everything")


def test_emit_rejects_actor_with_separator() -> None:
    with pytest.raises(ValueError):
        emit_token("bad|actor", "apply")


def test_verify_without_secret_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = emit_token("alice", "apply")
    monkeypatch.delenv("RDP_APPLY_TOKEN_SECRET", raising=False)
    with pytest.raises(RuntimeError):
        verify_token(token, "apply")


def test_cross_secret_token_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Token minted under secret A must not verify under secret B."""
    token = emit_token("alice", "apply")
    monkeypatch.setenv("RDP_APPLY_TOKEN_SECRET", "different-secret")
    with pytest.raises(InvalidTokenError) as exc_info:
        verify_token(token, "apply")
    assert str(exc_info.value) == "bad_sig"
