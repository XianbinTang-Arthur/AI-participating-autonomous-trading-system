"""Governance DB 共享工具函数.

提供 DB 连通性检查、日期解析、JSON 序列化等跨模块复用的辅助函数。
所有 governance 子模块统一从此处导入，避免重复定义。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

# 合法的 parameter_set 状态
VALID_PS_STATUSES = frozenset({"draft", "candidate", "frozen", "deprecated"})

# 合法的 recommendation 状态
VALID_REC_STATUSES = frozenset({"draft", "approved", "rejected", "superseded"})

# 合法的 recommendation 类型
VALID_REC_TYPES = frozenset({
    "parameter_upgrade", "keep_active", "lower_priority",
    "pause", "require_review",
})


def try_governance_db():
    """尝试获取 governance DB 连接。

    Returns
    -------
    (engine, True) 或 (None, False)

    使用方需在 finally 中调用 engine.dispose() 释放资源。

    DB URL 优先级:
      1. AATS_ACTIVE_PARAMETER_DB_URL — 专用 governance DB URL
      2. RDP_DATABASE_URL — RDP 统一 DB URL（与 aats.data_platform.db.get_engine 一致）
    历史上两个 URL 分别被不同入口引用（脚本 vs 核心模块），导致 apply-frozen
    通过 AATS_ACTIVE_PARAMETER_DB_URL 写入但 bootstrap 通过 RDP_DATABASE_URL
    读取，active_parameter_sets 表始终为空。现在统一 fallback 链。
    """
    url = os.environ.get("AATS_ACTIVE_PARAMETER_DB_URL")
    if not url:
        url = os.environ.get("RDP_DATABASE_URL")
    if not url:
        return None, False
    try:
        from sqlalchemy import create_engine, text as sa_text

        engine = create_engine(url, pool_pre_ping=True, pool_size=1, max_overflow=0)
        with engine.connect() as conn:
            conn.execute(sa_text("SELECT 1"))
        return engine, True
    except Exception as exc:
        log.debug("governance DB 不可用 (%s)，使用文件模式", exc)
        return None, False


def parse_dt(val: str | None) -> datetime | None:
    """将 ISO 字符串解析为 datetime，None 原样返回."""
    if val is None:
        return None
    try:
        return datetime.fromisoformat(val)
    except (ValueError, TypeError):
        return None


def json_dumps(obj: Any) -> str:
    """序列化为 JSON 字符串（给 JSONB 参数用）."""
    return json.dumps(obj, ensure_ascii=False, default=str)
