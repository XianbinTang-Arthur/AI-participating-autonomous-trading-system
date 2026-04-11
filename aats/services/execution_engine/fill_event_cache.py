"""P1-2 热路径优化：FillEvent 跨进程缓存。

核心问题
========
决策进程在每次 ``context_builder.build()`` 都要打 Postgres
``execution_repo.fills_for_scope()``。本模块在该读路径上插一层共享缓存：
local dict + Redis 持久化 + NATS 实时广播，消除每决策周期的 PG SELECT 查询。

与 OrderStateHotCache 的区别：
- FillEvent 是 append-only / immutable，无状态机更新
- 去重使用 fill_id 唯一性，无需 ts 比较
- 需要 FIFO 容量管理（fills 可能很多）

数据通路：
    NATS ``execution.fill_events`` —— 已存在，由 outbox publisher 广播
"""
from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any

from aats.bootstrap.logging import log_event
from aats.bus.base import EventBus
from aats.events import topics
from aats.events.envelopes import parse_envelope
from aats.schemas.execution import FillEvent
from aats.services.fill_ordering import fill_processing_sort_key
from aats.services.runtime_scope import (
    RuntimeStateScope,
    filter_fills,
)
from aats.storage.hot_state_store import HotStateStore, make_key

_NS_FILL = "fill_event"

FILL_INDEX_KEY = make_key(_NS_FILL, "index")

# 最大缓存条数。超出后淘汰最老的 fill。
# 决策周期只需要 scope 内的最近 fills 用于 strategy_health / leg_lifecycle。
_MAX_CACHED_FILLS = 2000


def _fill_key(fill_id: str) -> str:
    return make_key(_NS_FILL, "by_fid", fill_id)


class FillEventHotCache:
    """Sidecar cache for ``FillEvent`` across 4 processes.

    Follows the same template as ``ObligationHotStateCache`` (Stage 6 Slice 6.5).
    FillEvent is append-only: dedup by fill_id, no ts comparison needed.
    """

    def __init__(self, *, logger: logging.Logger, max_capacity: int = _MAX_CACHED_FILLS) -> None:
        self._logger = logger
        self._max_capacity = max_capacity
        # OrderedDict 保持插入顺序，用于 FIFO 淘汰
        self._fills: OrderedDict[str, FillEvent] = OrderedDict()
        self._hot_state_store: HotStateStore | None = None
        self._bus: EventBus | None = None
        self._process_role: str = "monolith"
        self._bootstrapped: bool = False
        self._subscribed: bool = False
        self._index_version: int = 0

    # ──────────────────────────────────────────────────────────────────
    # 启动 / 关闭
    # ──────────────────────────────────────────────────────────────────

    async def bootstrap(
        self,
        *,
        hot_state_store: HotStateStore,
        bus: EventBus,
        process_role: str,
        subscribe: bool = True,
    ) -> None:
        self._hot_state_store = hot_state_store
        self._bus = bus
        self._process_role = process_role

        try:
            index = await hot_state_store.get(FILL_INDEX_KEY)
        except Exception as exc:
            log_event(self._logger, "fill_event_cache_bootstrap_index_failed",
                      level="warning", process_role=self._process_role,
                      error_type=type(exc).__name__, error=str(exc))
            index = None

        if isinstance(index, dict):
            try:
                self._index_version = int(index.get("version") or 0)
            except Exception:
                self._index_version = 0
            all_fids_raw = index.get("all_fill_ids") or []
            if isinstance(all_fids_raw, list) and all_fids_raw:
                # 只取最后 max_capacity 个
                recent_fids = all_fids_raw[-self._max_capacity:]
                keys = [_fill_key(str(fid)) for fid in recent_fids]
                try:
                    raws = await hot_state_store.get_many(keys)
                except Exception as exc:
                    log_event(self._logger, "fill_event_cache_bootstrap_get_many_failed",
                              level="warning", process_role=self._process_role,
                              count=len(keys), error_type=type(exc).__name__, error=str(exc))
                    raws = {}
                for _key, raw in raws.items():
                    if not isinstance(raw, dict):
                        continue
                    try:
                        fill = FillEvent.model_validate(raw)
                        self._fills[fill.fill_id] = fill
                    except Exception as exc:
                        log_event(self._logger, "fill_event_cache_bootstrap_parse_failed",
                                  level="warning", process_role=self._process_role,
                                  error_type=type(exc).__name__, error=str(exc))
                log_event(self._logger, "fill_event_cache_bootstrap_hydrated",
                          process_role=self._process_role,
                          cached_count=len(self._fills), index_version=self._index_version)
            else:
                log_event(self._logger, "fill_event_cache_bootstrap_empty_index",
                          process_role=self._process_role, index_version=self._index_version)
        else:
            log_event(self._logger, "fill_event_cache_bootstrap_no_index",
                      process_role=self._process_role)

        self._bootstrapped = True
        if subscribe:
            await self.register_remote_subscription(bus)

    async def register_remote_subscription(self, bus: EventBus) -> None:
        try:
            await bus.subscribe(topics.FILL_EVENTS, self._handle_remote_event)
            self._subscribed = True
            log_event(self._logger, "fill_event_cache_subscribed",
                      process_role=self._process_role, topic=topics.FILL_EVENTS)
        except Exception as exc:
            log_event(self._logger, "fill_event_cache_subscribe_failed",
                      level="warning", process_role=self._process_role,
                      error_type=type(exc).__name__, error=str(exc))

    async def stop(self) -> None:
        log_event(self._logger, "fill_event_cache_stopped",
                  process_role=self._process_role, subscribed=self._subscribed,
                  cached_count=len(self._fills))

    # ──────────────────────────────────────────────────────────────────
    # 写路径
    # ──────────────────────────────────────────────────────────────────

    async def publish(self, fill: FillEvent, *, skip_local: bool = False) -> None:
        if not skip_local:
            applied = self._apply_locally(fill)
            if not applied:
                return

        await self._best_effort_redis_set(fill)
        await self._best_effort_redis_index_update()

    def fire_and_forget_publish(self, fill: FillEvent | None) -> None:
        if fill is None:
            return
        applied = self._apply_locally(fill)
        if not applied:
            return
        if self._hot_state_store is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if loop.is_closed() or not loop.is_running():
            return
        try:
            loop.create_task(
                self.publish(fill, skip_local=True),
                name="fill_event_cache_publish",
            )
        except Exception:
            pass

    # ──────────────────────────────────────────────────────────────────
    # 读路径（context_builder 调）
    # ──────────────────────────────────────────────────────────────────

    def fills_for_scope_sync(
        self,
        scope: RuntimeStateScope,
        *,
        since: datetime | None = None,
    ) -> list[FillEvent] | None:
        """返回 scope 内的 fills（排序后）。未 bootstrap 返回 None（I5）。"""
        if not self._bootstrapped:
            return None
        all_fills = list(self._fills.values())
        scoped = filter_fills(all_fills, scope)
        if since is not None:
            scoped = [f for f in scoped if f.ingestion_timestamp >= since]
        scoped.sort(key=fill_processing_sort_key)
        return scoped

    # ──────────────────────────────────────────────────────────────────
    # NATS 远端事件接收
    # ──────────────────────────────────────────────────────────────────

    async def _handle_remote_event(self, message: dict[str, Any]) -> None:
        try:
            envelope = parse_envelope(message)
            fill = FillEvent.model_validate(envelope.payload)
        except Exception as exc:
            log_event(self._logger, "fill_event_cache_remote_parse_failed",
                      level="warning", process_role=self._process_role,
                      error_type=type(exc).__name__, error=str(exc))
            return
        self._apply_locally(fill)

    # ──────────────────────────────────────────────────────────────────
    # 内部 helpers
    # ──────────────────────────────────────────────────────────────────

    def _apply_locally(self, fill: FillEvent) -> bool:
        """Append-only dedup by fill_id. 已存在的 fill_id 被跳过。"""
        fid = fill.fill_id
        if fid in self._fills:
            return False
        self._fills[fid] = fill
        # FIFO 淘汰
        while len(self._fills) > self._max_capacity:
            self._fills.popitem(last=False)
        return True

    async def _best_effort_redis_set(self, fill: FillEvent) -> None:
        if self._hot_state_store is None:
            return
        try:
            await self._hot_state_store.set(
                _fill_key(fill.fill_id),
                fill.model_dump(mode="json"),
            )
        except Exception as exc:
            log_event(self._logger, "fill_event_cache_redis_set_failed",
                      level="warning", process_role=self._process_role,
                      fill_id=fill.fill_id,
                      error_type=type(exc).__name__, error=str(exc))

    async def _best_effort_redis_index_update(self) -> None:
        if self._hot_state_store is None:
            return
        self._index_version += 1
        index_payload = {
            "all_fill_ids": list(self._fills.keys()),
            "count": len(self._fills),
            "version": self._index_version,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "writer_role": self._process_role,
        }
        try:
            await self._hot_state_store.set(FILL_INDEX_KEY, index_payload)
        except Exception as exc:
            log_event(self._logger, "fill_event_cache_redis_index_failed",
                      level="warning", process_role=self._process_role,
                      error_type=type(exc).__name__, error=str(exc))

    # ──────────────────────────────────────────────────────────────────
    # 诊断
    # ──────────────────────────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        return {
            "process_role": self._process_role,
            "bootstrapped": self._bootstrapped,
            "subscribed": self._subscribed,
            "cached_count": len(self._fills),
            "max_capacity": self._max_capacity,
            "index_version": self._index_version,
        }
