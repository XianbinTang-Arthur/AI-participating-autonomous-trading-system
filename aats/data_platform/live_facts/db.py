"""Live DB 只读连接管理.

与 aats.data_platform.db (research DB) 完全独立，
专门管理对 production DB 的只读连接。

设计原则:
  1. 只读 — session 结束时 rollback，不 commit
  2. 独立引擎 — 不与 research DB 共用连接池
  3. 安全 — 不暴露写入方法
  4. 容错 — 连接失败不阻断 RDP 主流程
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from aats.data_platform.config import ResearchPlatformSettings, get_settings
from aats.storage.connection_budget import RDP_LIVE_FACTS_POOL

log = logging.getLogger(__name__)

# ── 引擎缓存 ───────────────────────────────────────────────────────

_live_engine: Engine | None = None
_live_session_factory: sessionmaker[Session] | None = None


# ── 引擎初始化 ─────────────────────────────────────────────────────


def get_live_engine(settings: ResearchPlatformSettings | None = None) -> Engine:
    """获取或创建 live DB 只读引擎.

    Raises
    ------
    RuntimeError  如果 RDP_LIVE_DATABASE_URL 未配置
    """
    global _live_engine
    if _live_engine is not None:
        return _live_engine

    settings = settings or get_settings()
    if not settings.live_database_url:
        raise RuntimeError(
            "RDP_LIVE_DATABASE_URL 未配置。"
            "请在 .env.research 中设置 production DB 只读连接。"
        )

    connect_args: dict[str, Any] = {}
    if settings.live_db_connect_timeout_seconds > 0:
        connect_args["connect_timeout"] = settings.live_db_connect_timeout_seconds

    _live_engine = create_engine(
        settings.live_database_url,
        pool_size=RDP_LIVE_FACTS_POOL.pool_size,
        max_overflow=RDP_LIVE_FACTS_POOL.max_overflow,
        pool_pre_ping=True,
        connect_args=connect_args,
        echo=False,
    )
    # 隐藏密码的安全日志
    safe_url = settings.live_database_url.split("@")[-1] if "@" in settings.live_database_url else "***"
    log.info("已创建 live DB 只读引擎: %s (schema=%s)", safe_url, settings.live_db_schema or "public")
    return _live_engine


def get_live_session_factory(
    settings: ResearchPlatformSettings | None = None,
) -> sessionmaker[Session]:
    """获取 live DB session 工厂."""
    global _live_session_factory
    if _live_session_factory is None:
        engine = get_live_engine(settings)
        _live_session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    return _live_session_factory


@contextlib.contextmanager
def get_live_session(
    settings: ResearchPlatformSettings | None = None,
) -> Iterator[Session]:
    """获取 live DB 只读 session.

    安全保证: 结束时强制 rollback，不 commit。

    用法::

        with get_live_session() as session:
            rows = query_adapter.fetch_execution_orders(session, symbol="BTC-USDT-SWAP")
    """
    factory = get_live_session_factory(settings)
    session = factory()
    try:
        yield session
        # 只读: 强制 rollback
        session.rollback()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_live_engine() -> None:
    """重置缓存引擎和 session 工厂（用于测试）."""
    global _live_engine, _live_session_factory
    if _live_engine is not None:
        _live_engine.dispose()
    _live_engine = None
    _live_session_factory = None


def test_connection(settings: ResearchPlatformSettings | None = None) -> bool:
    """快速测试 live DB 连接是否可用."""
    try:
        engine = get_live_engine(settings)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        log.warning("live DB 连接测试失败: %s", exc)
        return False
