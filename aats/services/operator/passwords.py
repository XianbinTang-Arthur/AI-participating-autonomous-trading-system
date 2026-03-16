from __future__ import annotations

import base64
import hashlib
import hmac
import os


_SCHEME = "pbkdf2_sha256"
_ITERATIONS = 390_000


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
    except ValueError:
        return False
    if scheme != _SCHEME:
        return False
    salt = _decode(encoded_salt)
    expected = _decode(encoded_digest)
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
    return hmac.compare_digest(candidate, expected)


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))
