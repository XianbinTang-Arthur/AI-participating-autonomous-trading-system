"""RDP 进程访问 live DB(aats_live_derivatives)的统一 session 入口。

设计动机(R1-09 / R1-10 / R2-05):
  - Phase 2 cost calibration 要读 live DB 的 execution_fills / execution_orders
  - Phase 1 apply saga 要写 live DB 的 strategy_profile_activation
  - RDP daemon 默认只连 research DB(RDP_DATABASE_URL),访问不到 live DB
  - 旧方案:ETL 把 fills 镜像到 research bronze(工作量大)
  - v3 方案:RDP daemon 多开一个 live DB pool,read-only 的场景单独 engine

实现要点(R2-05):
  - eager init:daemon 启动时一次性 ping,失败则进程 refuse to start(fail-fast)
  - pool_pre_ping=True:避免 Postgres 清理 idle connection 导致的 stale connection 错误
  - pool_recycle=300:主动 recycle,和默认连接 TTL 同步
  - pool_timeout=30:saga 里拿不到连接应快 fail(不卡住上游 API)

环境变量:
  AATS_LIVE_DB_URL_RDP  —  必须设置;RDP 专用 live DB DSN
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from aats.storage.connection_budget import (
    RDP_LIVE_SESSION_RO_POOL,
    RDP_LIVE_SESSION_RW_POOL,
)

log = logging.getLogger(__name__)

_LIVE_DB_URL_ENV = "AATS_LIVE_DB_URL_RDP"

_engine_rw: Engine | None = None
_engine_ro: Engine | None = None
_session_factory_rw: Any = None
_session_factory_ro: Any = None
_init_lock = threading.Lock()
_initialized = False


class LiveDbNotConfiguredError(RuntimeError):
    """AATS_LIVE_DB_URL_RDP 未配置 — 生产路径必须有。"""


class LiveDbNotInitializedError(RuntimeError):
    """调用方在 init_live_engines() 之前就请求 session。"""


def _build_ro_connect_args() -> dict:
    """RO engine 的连接参数 — 通过 options 设置默认 read-only 事务。"""
    return {"options": "-c default_transaction_read_only=on"}


def init_live_engines(*, allow_missing_url: bool = False) -> bool:
    """初始化 live DB engines(RW + RO)。进程启动时 eager 调用。

    Returns True on success, False if url missing and allow_missing_url=True.
    Raises LiveDbNotConfiguredError if url missing and not allowed.
    """
    global _engine_rw, _engine_ro, _session_factory_rw, _session_factory_ro, _initialized

    with _init_lock:
        if _initialized:
            return True

        url = os.environ.get(_LIVE_DB_URL_ENV)
        if not url or not url.strip():
            if allow_missing_url:
                log.warning(
                    "%s 未配置 — RDP 将不能访问 live DB;apply saga / cost "
                    "calibration 会返回 503。适用于仅跑 research 的测试环境。",
                    _LIVE_DB_URL_ENV,
                )
                return False
            raise LiveDbNotConfiguredError(
                f"{_LIVE_DB_URL_ENV} 未配置 — RDP daemon 生产环境必须注入此值"
            )

        _engine_rw = create_engine(
            url.strip(),
            pool_size=RDP_LIVE_SESSION_RW_POOL.pool_size,
            max_overflow=RDP_LIVE_SESSION_RW_POOL.max_overflow,
            pool_recycle=300,
            pool_pre_ping=True,
            pool_timeout=30,
        )
        _engine_ro = create_engine(
            url.strip(),
            pool_size=RDP_LIVE_SESSION_RO_POOL.pool_size,
            max_overflow=RDP_LIVE_SESSION_RO_POOL.max_overflow,
            pool_recycle=300,
            pool_pre_ping=True,
            pool_timeout=30,
            connect_args=_build_ro_connect_args(),
        )

        # Eager ping — fail fast if either engine is unreachable
        try:
            with _engine_rw.connect() as c:
                c.execute(text("SELECT 1"))
            with _engine_ro.connect() as c:
                c.execute(text("SELECT 1"))
        except Exception:
            _engine_rw.dispose() if _engine_rw else None
            _engine_ro.dispose() if _engine_ro else None
            _engine_rw = _engine_ro = None
            raise

        _session_factory_rw = sessionmaker(bind=_engine_rw, expire_on_commit=False)
        _session_factory_ro = sessionmaker(bind=_engine_ro, expire_on_commit=False)
        _initialized = True
        log.info("live DB engines initialized (RW + RO pools)")
        return True


def get_live_session(mode: str = "rw") -> Session:
    """返回一个新的 Session。调用方负责 close/commit/rollback。

    mode='rw' - apply saga 写路径
    mode='ro' - cost calibration / sleeve advice 读路径
    """
    if not _initialized:
        raise LiveDbNotInitializedError(
            "call init_live_engines() before get_live_session()"
        )
    if mode == "rw":
        if _session_factory_rw is None:
            raise LiveDbNotInitializedError("RW factory unavailable")
        return _session_factory_rw()
    elif mode == "ro":
        if _session_factory_ro is None:
            raise LiveDbNotInitializedError("RO factory unavailable")
        return _session_factory_ro()
    else:
        raise ValueError(f"mode must be 'rw' or 'ro', got {mode!r}")


def is_initialized() -> bool:
    """给 health check / API 用,判断 live pool 是否已初始化。"""
    return _initialized


def _reset_for_tests() -> None:
    """测试用:清掉 engines,下一次 init 重建。生产不应调。"""
    global _engine_rw, _engine_ro, _session_factory_rw, _session_factory_ro, _initialized
    with _init_lock:
        for engine in (_engine_rw, _engine_ro):
            if engine is not None:
                try:
                    engine.dispose()
                except Exception:  # pragma: no cover
                    pass
        _engine_rw = _engine_ro = None
        _session_factory_rw = _session_factory_ro = None
        _initialized = False
