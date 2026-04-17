"""Governance DB 共享工具函数.

提供 DB 连通性检查、日期解析、JSON 序列化等跨模块复用的辅助函数。
所有 governance 子模块统一从此处导入，避免重复定义。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)

# Advisory lock keys 集中注册表。PostgreSQL pg_try_advisory_lock 的 bigint key
# 在多个调度器 / release_cycle 路径里复用；集中声明避免各模块硬编码 magic
# number 后撞上同一把锁却不自知。新增锁时在这里加条目，并在 value 里写清楚
# 谁持有、预期持锁时长、解锁条件。
ADVISORY_LOCK_KEYS: dict[str, int] = {
    # pg_try_advisory_lock/pg_advisory_unlock：整体 governance scheduler 单例锁，
    # 保证同一时刻只有一个进程跑 release_cycle 推进，避免竞态双写 release_history。
    "governance_scheduler_singleton": 0x41415353,  # "AASS"
    # release_cycle 内部的 release-id 级锁，配合 pg_try_advisory_xact_lock 使用。
    "release_cycle_per_release": 0x41415243,  # "AARC"
}


# 进程级共享 governance engine（仅供热点路径读用，禁止 dispose）。
# 与 `try_governance_db` 的一次性 engine 并行存在：
#   * try_governance_db → 每次 create+dispose，适合短路径 / 脚本 / 测试
#   * get_cached_governance_engine → 全进程复用，适合 per-tick overrides / 决策
#     循环里的高频读，避免 create_engine+SELECT 1+dispose 三件套的握手成本。
_CACHED_GOVERNANCE_ENGINE: "Engine | None" = None
_CACHED_GOVERNANCE_LOCK = threading.Lock()

# 合法的 parameter_set 状态
VALID_PS_STATUSES = frozenset({"draft", "candidate", "frozen", "deprecated"})

# 合法的 recommendation 状态
VALID_REC_STATUSES = frozenset({"draft", "approved", "rejected", "superseded"})

# 合法的 recommendation 类型
VALID_REC_TYPES = frozenset({
    "parameter_upgrade", "keep_active", "lower_priority",
    "pause", "require_review",
})


def resolve_governance_db_url() -> str | None:
    """Resolve the governance DB URL using the same fallback chain as RDP.

    Precedence:
      1. ``AATS_ACTIVE_PARAMETER_DB_URL``
      2. ``RDP_DATABASE_URL``
      3. ``get_settings().database_url``
    """
    direct_url = os.environ.get("AATS_ACTIVE_PARAMETER_DB_URL")
    if direct_url and direct_url.strip():
        return direct_url.strip()

    rdp_url = os.environ.get("RDP_DATABASE_URL")
    if rdp_url and rdp_url.strip():
        return rdp_url.strip()

    try:
        from aats.data_platform.config import get_settings

        settings_url = str(get_settings().database_url).strip()
        return settings_url or None
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("failed to resolve governance DB URL from RDP settings: %s", exc)
        return None


def get_cached_governance_engine() -> "Engine | None":
    """返回进程级共享的 governance engine；首次调用时惰性创建。

    调用约定：
      * 返回的 engine 生命周期归本模块所有，**caller 不得 dispose()**。
      * 连接池开启 ``pool_pre_ping``；陈旧连接会被 SQLAlchemy 透明回收。
      * 若 URL 解析失败 / create_engine 抛异常 → 返回 ``None``；caller 应当
        走降级路径（比如 last-known cache）。

    用途：hot path（每 tick 读 overrides 等）复用连接，避免重复握手。
    对于一次性脚本、测试、手工工具链，继续用 :func:`try_governance_db`。
    """
    global _CACHED_GOVERNANCE_ENGINE
    if _CACHED_GOVERNANCE_ENGINE is not None:
        return _CACHED_GOVERNANCE_ENGINE
    with _CACHED_GOVERNANCE_LOCK:
        if _CACHED_GOVERNANCE_ENGINE is not None:
            return _CACHED_GOVERNANCE_ENGINE
        url = resolve_governance_db_url()
        if not url:
            return None
        try:
            from sqlalchemy import create_engine

            _CACHED_GOVERNANCE_ENGINE = create_engine(
                url,
                pool_pre_ping=True,
                pool_size=2,
                max_overflow=3,
            )
            return _CACHED_GOVERNANCE_ENGINE
        except Exception as exc:
            log.debug("governance DB engine 初始化失败 (%s)", exc)
            return None


def reset_cached_governance_engine_for_tests() -> None:
    """测试钩子：释放 cached engine，下一次 get_cached_governance_engine 会重建。

    模块私有约定，生产路径不应调用。
    """
    global _CACHED_GOVERNANCE_ENGINE
    with _CACHED_GOVERNANCE_LOCK:
        engine = _CACHED_GOVERNANCE_ENGINE
        _CACHED_GOVERNANCE_ENGINE = None
    if engine is not None:
        try:
            engine.dispose()
        except Exception:  # pragma: no cover - defensive
            pass


def try_governance_db():
    """尝试获取 governance DB 连接。

    Returns
    -------
    (engine, True) 或 (None, False)

    使用方需在 finally 中调用 engine.dispose() 释放资源。

    DB URL 优先级:
      1. AATS_ACTIVE_PARAMETER_DB_URL — 专用 governance DB URL
      2. RDP_DATABASE_URL — RDP 统一 DB URL（与 aats.data_platform.db.get_engine 一致）
    历史上两个 URL 分别被不同入口引用（脚本 vs 核心模块），导致批量写入入口
    通过 AATS_ACTIVE_PARAMETER_DB_URL 写入但 bootstrap 通过 RDP_DATABASE_URL
    读取，active_parameter_sets 表始终为空。现在统一 fallback 链。
    """
    url = resolve_governance_db_url()
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
    """将 ISO 字符串解析为 datetime，None 或非法格式返回 None.

    DB 层通用工具：批量反序列化场景中单条非法记录不应导致整批失败，因此
    此处维持 None-on-illegal 的软失败契约。底层统一委托给
    :func:`parse_iso_datetime_utc` 保证时区处理一致。
    """
    from aats.data_platform.governance._time_util import parse_iso_datetime_utc

    if val is None:
        return None
    try:
        return parse_iso_datetime_utc(val, context="governance._db_util.parse_dt")
    except ValueError:
        return None


def json_dumps(obj: Any) -> str:
    """序列化为 JSON 字符串（给 JSONB 参数用）."""
    return json.dumps(obj, ensure_ascii=False, default=str)
