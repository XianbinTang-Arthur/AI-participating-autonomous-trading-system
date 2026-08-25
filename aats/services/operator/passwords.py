from __future__ import annotations

import base64
import hashlib
import hmac
import os


_SCHEME = "pbkdf2_sha256"
_ITERATIONS = 390_000
_MAX_VERIFY_ITERATIONS = 1_000_000
_DUMMY_SALT = b"AATS-login-dummy"
_DUMMY_EXPECTED_DIGEST = b"\x00" * 32


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return "$".join(
        (
            _SCHEME,
            str(_ITERATIONS),
            base64.urlsafe_b64encode(salt).decode("ascii").rstrip("="),
            base64.urlsafe_b64encode(derived).decode("ascii").rstrip("="),
        )
    )


def verify_password(password: str, encoded_hash: str) -> bool:
    try:
        scheme, iterations, encoded_salt, encoded_digest = encoded_hash.split("$", 3)
        iteration_count = int(iterations)
        if scheme != _SCHEME or not 1 <= iteration_count <= _MAX_VERIFY_ITERATIONS:
            raise ValueError("unsupported_password_hash")
        salt = _decode(encoded_salt)
        expected = _decode(encoded_digest)
        if not salt or not expected:
            raise ValueError("invalid_password_hash")
    except Exception:
        return consume_dummy_password_verification(password)
    try:
        candidate = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            iteration_count,
        )
    except Exception:
        return consume_dummy_password_verification(password)
    return hmac.compare_digest(candidate, expected)


def consume_dummy_password_verification(password: str) -> bool:
    """Consume the normal KDF class without authenticating any identity."""

    candidate = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        _DUMMY_SALT,
        _ITERATIONS,
    )
    hmac.compare_digest(candidate, _DUMMY_EXPECTED_DIGEST)
    return False


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))
