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
import logging
import os
import time
from typing import Tuple

logger = logging.getLogger(__name__)

# 合法 action 列表——防止误用其他字符串导致 HMAC 被绕过。
#
# 注：``freeze`` 为预留动作。批次 A 签发端（``POST /rdp/operator-tokens``）
# 与 CLI 均接受 ``action=freeze``，但当前 API 层**没有**对应的消费端点；
# freeze / 候选导入 / parameter 废弃 的 API 化排期在后续批次。签出的 freeze
# token 在现有路由上无法落地，仅为不破坏已发文档契约而保留。
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


def emit_token(
    actor: str,
    action: str,
    *,
    scope: str | None = None,
    recommendation_id: str | None = None,
) -> str:
    """签发一枚 ``actor`` 对 ``action`` 的 TTL-bounded HMAC token。

    **v1 格式**(向后兼容):``actor|action|exp_ts|sig``
        仅 ``scope=None and recommendation_id=None`` 时发 v1。
        v1 token 只能用于 scope='combo' 的 rec(服务端校验)。

    **v2 格式**(rdp_scope_expansion_v3 §0.4):
        ``actor|action|scope|recommendation_id|exp_ts|sig``
        profile / sleeve scope 的 apply 必须使用 v2(服务端强制要求)。
    """
    if action not in _ALLOWED_ACTIONS:
        raise ValueError(
            f"action 必须是 {sorted(_ALLOWED_ACTIONS)} 之一，收到: {action!r}"
        )
    if not actor or "|" in actor:
        raise ValueError(f"actor 非法或含分隔符: {actor!r}")

    # v2 模式:scope + recommendation_id 必须都传或都不传
    if (scope is None) != (recommendation_id is None):
        raise ValueError(
            "scope 与 recommendation_id 必须成对提供,或都不提供(v1 兼容模式)"
        )

    exp_ts = int(time.time()) + ttl_seconds()

    if scope is None and recommendation_id is None:
        # v1 格式
        payload = f"{actor}|{action}|{exp_ts}"
    else:
        # v2 格式
        if "|" in scope or "|" in recommendation_id:
            raise ValueError("scope / recommendation_id 含分隔符")
        payload = f"{actor}|{action}|{scope}|{recommendation_id}|{exp_ts}"

    sig = hmac.new(
        _secret(),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    raw = f"{payload}|{sig}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def verify_token(
    token: str,
    required_action: str,
    *,
    required_scope: str | None = None,
    required_recommendation_id: str | None = None,
) -> Tuple[str, int]:
    """校验 ``token`` 是否为合法的 ``required_action`` 凭证。

    **向后兼容**:如果 ``required_scope=None and required_recommendation_id=None``,
    则只做 v1 三段格式校验(现有 combo apply/rollback 路径不受影响)。

    **v2 校验(Phase 1+)**:传入 ``required_scope`` + ``required_recommendation_id``,
    token 必须是 5 段 v2 格式,且 token 的 scope/rec_id 与 required 严格匹配。
    这防止 combo token 被拿去 apply profile rec(R2-04)。

    返回 ``(actor, exp_ts)``;任一环节失败抛 :class:`InvalidTokenError`。
    """
    if required_action not in _ALLOWED_ACTIONS:
        raise ValueError(f"required_action 非法: {required_action!r}")

    # 两个必须成对
    if (required_scope is None) != (required_recommendation_id is None):
        raise ValueError(
            "required_scope 与 required_recommendation_id 必须成对提供"
        )

    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
    except Exception as exc:
        raise InvalidTokenError("malformed") from exc

    parts = raw.split("|")
    # v1: actor|action|exp_ts|sig        (4 parts)
    # v2: actor|action|scope|rec_id|exp_ts|sig  (6 parts)
    if len(parts) == 4:
        actor, action, exp_ts_str, sig = parts
        tok_scope: str | None = None
        tok_rec_id: str | None = None
        payload = f"{actor}|{action}|{exp_ts_str}"
    elif len(parts) == 6:
        actor, action, tok_scope, tok_rec_id, exp_ts_str, sig = parts
        payload = f"{actor}|{action}|{tok_scope}|{tok_rec_id}|{exp_ts_str}"
    else:
        raise InvalidTokenError("malformed")

    if action not in _ALLOWED_ACTIONS:
        logger.warning("verify_token action_unknown: payload_action=%r", action)
        raise InvalidTokenError("action_unknown")
    if action != required_action:
        logger.warning(
            "verify_token action_mismatch: expected=%r got=%r",
            required_action, action,
        )
        raise InvalidTokenError("action_mismatch")

    # Scope / rec_id binding check (R2-04)
    if required_scope is not None:
        if tok_scope is None:
            # caller 要求 v2 但 token 是 v1 → 拒绝
            raise InvalidTokenError("v2_required")
        if tok_scope != required_scope:
            logger.warning(
                "verify_token scope_mismatch: expected=%r got=%r",
                required_scope, tok_scope,
            )
            raise InvalidTokenError("scope_mismatch")
        if tok_rec_id != required_recommendation_id:
            logger.warning(
                "verify_token rec_id_mismatch: expected=%r got=%r",
                required_recommendation_id, tok_rec_id,
            )
            raise InvalidTokenError("rec_id_mismatch")

    try:
        exp_ts = int(exp_ts_str)
    except ValueError as exc:
        raise InvalidTokenError("invalid_exp") from exc
    if exp_ts < int(time.time()):
        raise InvalidTokenError("expired")

    expected_sig = hmac.new(
        _secret(),
        payload.encode("utf-8"),
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
