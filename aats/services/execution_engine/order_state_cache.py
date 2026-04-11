"""P1-1 热路径优化：OrderState 跨进程缓存。

核心问题
========
决策进程在每次 ``context_builder.build()`` 都要打 Postgres
``execution_repo.order_states_for_scope(open_only=True)``。本模块在该读路径上
插一层共享缓存：local dict + Redis 持久化 + NATS 实时广播，消除每决策周期的
PG SELECT 查询。

三层架构（与 6.5 ObligationHotStateCache 同 sidecar 模板）
========
L1 进程内 dict：client_order_id → OrderState，零延迟
L2 Redis：per-coid KV + index key，跨重启 hydrate
L3 Postgres：source of truth，cache miss 时 fallback

数据通路：
    NATS ``execution.order_updates`` —— 已存在，由 outbox publisher 广播

不变量
========
I1 fail-soft：Redis/NATS 任何失败都不阻塞 execution_repo 主路径
I5 miss 不破坏读：cache 未 bootstrap / miss → 返回 None → caller fallback PG
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from aats.bootstrap.logging import log_event
from aats.bus.base import EventBus
from aats.events import topics
from aats.events.envelopes import parse_envelope
from aats.schemas.execution import OrderState
from aats.services.runtime_scope import (
    RuntimeStateScope,
    filter_order_states,
)
from aats.storage.hot_state_store import HotStateStore, make_key

_NS_ORDER_STATE = "order_state"

ORDER_STATE_INDEX_KEY = make_key(_NS_ORDER_STATE, "index")

_TERMINAL_STATUSES = frozenset({
    "FILLED", "CANCELED", "REJECTED", "BLOCKED", "DRY_RUN", "FAILED", "EXPIRED",
})


def _order_state_key(client_order_id: str) -> str:
    return make_key(_NS_ORDER_STATE, "by_coid", client_order_id)


def _compare_ts(order: OrderState) -> datetime:
    ts = order.last_update_ts
    if ts is None:
        ts = order.submitted_ts
    if ts is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


class OrderStateHotCache:
    """Sidecar cache for ``OrderState`` across 4 processes.

    Follows the same template as ``ObligationHotStateCache`` (Stage 6 Slice 6.5).
    """

    def __init__(self, *, logger: logging.Logger) -> None:
        self._logger = logger
        self._latest: dict[str, OrderState] = {}
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
            index = await hot_state_store.get(ORDER_STATE_INDEX_KEY)
        except Exception as exc:
            log_event(self._logger, "order_state_cache_bootstrap_index_failed",
                      level="warning", process_role=self._process_role,
                      error_type=type(exc).__name__, error=str(exc))
            index = None

        if isinstance(index, dict):
            try:
                self._index_version = int(index.get("version") or 0)
            except Exception:
                self._index_version = 0
            all_coids_raw = index.get("all_coids") or []
            if isinstance(all_coids_raw, list) and all_coids_raw:
                keys = [_order_state_key(str(coid)) for coid in all_coids_raw]
                try:
                    raws = await hot_state_store.get_many(keys)
                except Exception as exc:
                    log_event(self._logger, "order_state_cache_bootstrap_get_many_failed",
                              level="warning", process_role=self._process_role,
                              count=len(keys), error_type=type(exc).__name__, error=str(exc))
                    raws = {}
                for _key, raw in raws.items():
                    if not isinstance(raw, dict):
                        continue
                    try:
                        order = OrderState.model_validate(raw)
                        self._latest[order.client_order_id] = order
                    except Exception as exc:
                        log_event(self._logger, "order_state_cache_bootstrap_parse_failed",
                                  level="warning", process_role=self._process_role,
                                  error_type=type(exc).__name__, error=str(exc))
                log_event(self._logger, "order_state_cache_bootstrap_hydrated",
                          process_role=self._process_role,
                          cached_count=len(self._latest), index_version=self._index_version)
            else:
                log_event(self._logger, "order_state_cache_bootstrap_empty_index",
                          process_role=self._process_role, index_version=self._index_version)
        else:
            log_event(self._logger, "order_state_cache_bootstrap_no_index",
                      process_role=self._process_role)

        self._bootstrapped = True
        if subscribe:
            await self.register_remote_subscription(bus)

    async def register_remote_subscription(self, bus: EventBus) -> None:
        try:
            await bus.subscribe(topics.ORDER_UPDATES, self._handle_remote_event)
            self._subscribed = True
            log_event(self._logger, "order_state_cache_subscribed",
                      process_role=self._process_role, topic=topics.ORDER_UPDATES)
        except Exception as exc:
            log_event(self._logger, "order_state_cache_subscribe_failed",
                      level="warning", process_role=self._process_role,
                      error_type=type(exc).__name__, error=str(exc))

    async def stop(self) -> None:
        log_event(self._logger, "order_state_cache_stopped",
                  process_role=self._process_role, subscribed=self._subscribed,
                  cached_count=len(self._latest))

    # ──────────────────────────────────────────────────────────────────
    # 写路径
    # ──────────────────────────────────────────────────────────────────

    async def publish(self, order: OrderState, *, skip_local: bool = False) -> None:
        if not skip_local:
            applied = self._apply_locally(order)
            if not applied:
                return

        await self._best_effort_redis_set(order)
        await self._best_effort_redis_index_update()
        # 注意：不调 _best_effort_nats_broadcast。与 ObligationHotStateCache 不同，
        # order state 变更已由 outbox publisher 的 flush_pending 通过
        # execution.order_updates topic 广播，cache 通过 register_remote_subscription
        # 订阅该 topic 接收。重复广播只会产生无用的第二条 NATS 消息。

    def fire_and_forget_publish(self, order: OrderState | None) -> None:
        if order is None:
            return
        applied = self._apply_locally(order)
        if not applied:
            return
        if self._bus is None and self._hot_state_store is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if loop.is_closed() or not loop.is_running():
            return
        try:
            loop.create_task(
                self.publish(order, skip_local=True),
                name="order_state_cache_publish",
            )
        except Exception:
            pass

    # ──────────────────────────────────────────────────────────────────
    # 读路径（context_builder 调）
    # ──────────────────────────────────────────────────────────────────

    def open_orders_for_scope_sync(
        self,
        scope: RuntimeStateScope,
    ) -> list[OrderState] | None:
        """返回 scope 内的 open orders。未 bootstrap 返回 None（I5）。"""
        if not self._bootstrapped:
            return None
        all_orders = list(self._latest.values())
        scoped = filter_order_states(all_orders, scope)
        return [o for o in scoped if o.status not in _TERMINAL_STATUSES]

    # ──────────────────────────────────────────────────────────────────
    # NATS 远端事件接收
    # ──────────────────────────────────────────────────────────────────

    async def _handle_remote_event(self, message: dict[str, Any]) -> None:
        try:
            envelope = parse_envelope(message)
            order = OrderState.model_validate(envelope.payload)
        except Exception as exc:
            log_event(self._logger, "order_state_cache_remote_parse_failed",
                      level="warning", process_role=self._process_role,
                      error_type=type(exc).__name__, error=str(exc))
            return
        self._apply_locally(order)

    # ──────────────────────────────────────────────────────────────────
    # 内部 helpers
    # ──────────────────────────────────────────────────────────────────

    def _apply_locally(self, order: OrderState) -> bool:
        coid = order.client_order_id
        existing = self._latest.get(coid)
        if existing is not None:
            if _compare_ts(order) <= _compare_ts(existing):
                return False
        self._latest[coid] = order
        return True

    async def _best_effort_redis_set(self, order: OrderState) -> None:
        if self._hot_state_store is None:
            return
        try:
            await self._hot_state_store.set(
                _order_state_key(order.client_order_id),
                order.model_dump(mode="json"),
            )
        except Exception as exc:
            log_event(self._logger, "order_state_cache_redis_set_failed",
                      level="warning", process_role=self._process_role,
                      client_order_id=order.client_order_id,
                      error_type=type(exc).__name__, error=str(exc))

    async def _best_effort_redis_index_update(self) -> None:
        if self._hot_state_store is None:
            return
        self._index_version += 1
        index_payload = {
            "all_coids": list(self._latest.keys()),
            "open_coids": [
                coid for coid, o in self._latest.items()
                if o.status not in _TERMINAL_STATUSES
            ],
            "version": self._index_version,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "writer_role": self._process_role,
        }
        try:
            await self._hot_state_store.set(ORDER_STATE_INDEX_KEY, index_payload)
        except Exception as exc:
            log_event(self._logger, "order_state_cache_redis_index_failed",
                      level="warning", process_role=self._process_role,
                      error_type=type(exc).__name__, error=str(exc))

    # ──────────────────────────────────────────────────────────────────
    # 诊断
    # ──────────────────────────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        open_count = sum(1 for o in self._latest.values() if o.status not in _TERMINAL_STATUSES)
        return {
            "process_role": self._process_role,
            "bootstrapped": self._bootstrapped,
            "subscribed": self._subscribed,
            "cached_count": len(self._latest),
            "open_count": open_count,
            "index_version": self._index_version,
        }
