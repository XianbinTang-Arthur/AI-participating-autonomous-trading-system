from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

from aats.bootstrap.settings import AATSSettings
from aats.schemas.operator import OperatorRole


class SessionAuthError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SessionIdentity:
    identity: str
    role: OperatorRole
    issued_at: int
    expires_at: int
    session_version: int


def issue_session_token(
    *,
    settings: AATSSettings,
    identity: str,
    role: OperatorRole,
    session_version: int,
) -> str:
    if not settings.operator_session_secret:
        raise SessionAuthError("operator_session_secret_missing")
    issued_at = int(time.time())
    expires_at = issued_at + settings.operator_session_max_age_seconds
    payload = {
        "sub": identity,
        "role": role,
        "iat": issued_at,
        "exp": expires_at,
        "ver": session_version,
    }
    encoded_payload = _urlsafe_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = _sign(settings.operator_session_secret, encoded_payload)
    return f"{encoded_payload}.{signature}"


def verify_session_token(*, settings: AATSSettings, token: str | None) -> SessionIdentity | None:
    if not token or not settings.operator_session_secret:
        return None
    try:
        encoded_payload, encoded_signature = token.split(".", 1)
    except ValueError:
        return None
    expected_signature = _sign(settings.operator_session_secret, encoded_payload)
    if not hmac.compare_digest(encoded_signature, expected_signature):
        return None
    try:
        payload = json.loads(_urlsafe_decode(encoded_payload))
    except (json.JSONDecodeError, ValueError):
        return None
    expires_at = int(payload.get("exp", 0) or 0)
    if expires_at <= int(time.time()):
        return None
    role = str(payload.get("role") or "")
    identity = str(payload.get("sub") or "")
    if role not in {"viewer", "operator", "admin"} or not identity:
        return None
    return SessionIdentity(
        identity=identity,
        role=role,  # type: ignore[arg-type]
        issued_at=int(payload.get("iat", 0) or 0),
        expires_at=expires_at,
        session_version=int(payload.get("ver", 1) or 1),
    )


def _sign(secret: str, encoded_payload: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), encoded_payload.encode("ascii"), hashlib.sha256).digest()
    return _urlsafe_encode(digest)


def _urlsafe_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _urlsafe_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))
