"""Shared governance DB connection pool for RDP API routes.

Fix P1: rdp_routes.py 和 rdp_control_summary.py 各自维护独立的
_governance_engine_cache 和 sessionmaker，导致同一 DB URL 创建两套
连接池。统一到本模块，Engine 和 sessionmaker 均按 URL 缓存。
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

_governance_engine_cache: dict[str, Engine] = {}
_governance_factory_cache: dict[str, sessionmaker[Session]] = {}


def governance_db_url() -> str | None:
    """获取 governance schema 所在数据库的连接串.

    优先 AATS_ACTIVE_PARAMETER_DB_URL（gateway/compose 注入），
    其次 RDP_DATABASE_URL（.env.research / 本地脚本）。
    """
    url = os.environ.get("AATS_ACTIVE_PARAMETER_DB_URL")
    if url:
        return url
    try:
        from aats.data_platform.config import get_settings as get_rdp_settings

        return get_rdp_settings().database_url
    except Exception:
        return None


def get_governance_engine(url: str) -> Engine:
    """返回 URL 对应的缓存 Engine，避免每次请求重建连接池."""
    cached = _governance_engine_cache.get(url)
    if cached is not None:
        return cached
    engine = create_engine(url, pool_pre_ping=True, pool_size=2, max_overflow=1)
    _governance_engine_cache[url] = engine
    return engine


def _get_governance_factory(url: str) -> sessionmaker[Session]:
    """返回 URL 对应的缓存 sessionmaker，避免每次 session 调用重建."""
    cached = _governance_factory_cache.get(url)
    if cached is not None:
        return cached
    engine = get_governance_engine(url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    _governance_factory_cache[url] = factory
    return factory


@contextlib.contextmanager
def governance_session() -> Iterator[Session]:
    """创建一个连接 governance schema 的轻量 session.

    gateway 容器通过 AATS_ACTIVE_PARAMETER_DB_URL 连接 aats_research，
    本地开发通过 RDP_DATABASE_URL (.env.research) 连接。
    Engine 和 sessionmaker 均按 URL 缓存。
    """
    url = governance_db_url()
    if not url:
        raise RuntimeError(
            "No governance DB URL available "
            "(AATS_ACTIVE_PARAMETER_DB_URL / RDP_DATABASE_URL)",
        )
    factory = _get_governance_factory(url)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
