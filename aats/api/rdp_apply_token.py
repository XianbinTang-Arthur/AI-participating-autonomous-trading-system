"""RDP apply/rollback/freeze 动作的 short-lived HMAC token.

批次 A · A-0.5 收口：废弃旧的生产写闸 env flag——那是"一个公开环境变量控制
所有写动作"的单点屏障，容易被 CI 镜像层泄露。改用签发给指定操作员的
TTL-bounded HMAC token，每次 apply/rollback 都必须带一个新鲜的 token 才能落库。

Token 载荷（``actor|action|exp_ts``）用服务端 secret 做 HMAC-SHA256，外层
再 base64-urlsafe。TTL 默认 300s、下限 60s、上限 900s，``RDP_APPLY_TOKEN_TTL_SECONDS``
环境变量可覆盖（超界会被 clamp）。

参考：``docs/task/rdp_hardening_batch_a_detailed_design.md §7.3.1``。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from typing import Tuple

# 合法 action 列表——防止误用其他字符串导致 HMAC 被绕过
_ALLOWED_ACTIONS: frozenset[str] = frozenset({"apply", "rollback", "freeze"})

_TTL_ENV_VAR = "RDP_APPLY_TOKEN_TTL_SECONDS"
_SECRET_ENV_VAR = "RDP_APPLY_TOKEN_SECRET"

_DEFAULT_TTL_SECONDS = 300
_TTL_MIN_SECONDS = 60
_TTL_MAX_SECONDS = 900


class InvalidTokenError(Exception):
    """Token 校验失败。``reason`` 用英文短码方便路由映射 HTTP 响应。"""


def _secret() -> bytes:
    secret = os.environ.get(_SECRET_ENV_VAR)
    if not secret:
        raise RuntimeError(
            f"{_SECRET_ENV_VAR} 未配置——生产环境必须在部署脚本里注入此值，"
            "不得提交到 git。"
        )
    return secret.encode("utf-8")


def ttl_seconds() -> int:
    """返回当前 TTL，已 clamp 到 [60, 900] 秒区间。"""
    raw = os.environ.get(_TTL_ENV_VAR)
    if raw is None:
        return _DEFAULT_TTL_SECONDS
    try:
        parsed = int(raw)
    except ValueError:
        return _DEFAULT_TTL_SECONDS
    return max(_TTL_MIN_SECONDS, min(parsed, _TTL_MAX_SECONDS))


def emit_token(actor: str, action: str) -> str:
    """签发一枚 ``actor`` 对 ``action`` 的 TTL-bounded HMAC token。"""
    if action not in _ALLOWED_ACTIONS:
        raise ValueError(
            f"action 必须是 {sorted(_ALLOWED_ACTIONS)} 之一，收到: {action!r}"
        )
    if not actor or "|" in actor:
        # '|' 是载荷分隔符，actor 名必须干净
        raise ValueError(f"actor 非法或含分隔符: {actor!r}")

    exp_ts = int(time.time()) + ttl_seconds()
    payload = f"{actor}|{action}|{exp_ts}"
    sig = hmac.new(
        _secret(),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    raw = f"{payload}|{sig}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def verify_token(token: str, required_action: str) -> Tuple[str, int]:
    """校验 ``token`` 是否为合法的 ``required_action`` 凭证。

    通过返回 ``(actor, exp_ts)``；任一环节失败抛 :class:`InvalidTokenError`，
    ``args[0]`` 是短码（``malformed`` / ``action_mismatch`` / ``expired`` /
    ``bad_sig`` 等），便于调用方把错误透给 HTTP 层。
    """
    if required_action not in _ALLOWED_ACTIONS:
        raise ValueError(
            f"required_action 非法: {required_action!r}"
        )

    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
    except Exception as exc:
        raise InvalidTokenError("malformed") from exc

    parts = raw.split("|")
    if len(parts) != 4:
        raise InvalidTokenError("malformed")
    actor, action, exp_ts_str, sig = parts

    if action not in _ALLOWED_ACTIONS:
        raise InvalidTokenError(f"action_unknown:{action}")
    if action != required_action:
        raise InvalidTokenError(
            f"action_mismatch:expected={required_action}:got={action}"
        )

    try:
        exp_ts = int(exp_ts_str)
    except ValueError as exc:
        raise InvalidTokenError("invalid_exp") from exc
    if exp_ts < int(time.time()):
        raise InvalidTokenError("expired")

    expected_sig = hmac.new(
        _secret(),
        f"{actor}|{action}|{exp_ts}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        raise InvalidTokenError("bad_sig")

    return actor, exp_ts


__all__ = [
    "InvalidTokenError",
    "emit_token",
    "ttl_seconds",
    "verify_token",
]
