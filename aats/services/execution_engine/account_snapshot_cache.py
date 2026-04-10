"""跨进程 account snapshot 缓存边车。

核心问题
========
Stage 3 多进程切片化把 account WS/REST refresh loop 收敛到 execution role，
但 gateway/decision/market 进程的 ``SystemHealthService.status()``、
``OperatorQueryService`` dashboard、以及所有通过 ``account_service.latest_snapshot()``
获取账户状态的路径仍然读取本地 ``OKXAccountService._latest_snapshot``——在非
execution 角色下该字段永远为 None，导致 dashboard 报 ``account_snapshot_missing``
blocker 且 health 服务误判。

解决方案
========
与 Stage 6 Slice 6.3 PortfolioSnapshotCache / Slice 6.5 ObligationHotStateCache
同 sidecar 模板：

- execution role 在 ``_refresh_account_loop`` 每次成功 refresh 后调
  ``cache.publish(snapshot)`` 广播；
- 所有角色的 cache 订阅 NATS ``account.snapshots``，收到后用
  ``fetched_at`` idempotent 规则更新本地 snapshot；
- 启动时从 Redis ``aats:hot:account:latest_snapshot`` hydrate；
- miss 不致命：非 execution role 的 ``account_service._latest_snapshot`` 会停在
  上一次 bootstrap 到的 stale 值，health_service 照常按 stale_after_seconds
  标记 ``account_state_stale`` blocker。

关键决策
========
D1  cache 放 execution_engine 子树，与 obligation_cache 同一级
D2  本地 ``Optional[ExchangeAccountSnapshot]`` 为 cached truth
D3  publish 由 config._refresh_account_loop 触发（不改 account_service 内部）
D4  复用已有 topic ``account.snapshots``
D5  publish 三步：local set -> best-effort Redis set -> best-effort NATS publish
D6  idempotent：``fetched_at <= local`` noop（与 PortfolioSnapshotCache 相同）
D7  broadcast payload 白名单裁剪 ``raw`` 字段：仅保留
    ``funding_rate_by_symbol``（非 execution 角色的
    ``OKXAccountService.funding_schedule()`` 依赖），其余大体积
    原始响应（``balance`` 等）剥离，以控制 NATS/Redis payload 大小
D8  所有 process_role 对称装载，cache 类内部无 role 分支

不变量
========
I1  fail-soft：Redis/NATS 任何失败都不阻塞 account_service 主路径
I2  cross-process <= 1 个 refresh interval：NATS 实时广播
I3  restart-safe：bootstrap 从 Redis 读 hydrate
I4  idempotent：乱序事件按 fetched_at 判断
I5  miss 不破坏读：cache miss -> _latest_snapshot 保持 None/stale ->
    health_service 标记 stale blocker（已有逻辑）
"""
from __future__ import annotations

import logging
from typing import Any

from aats.bootstrap.logging import log_event
from aats.bus.base import EventBus
from aats.events import topics
from aats.schemas.common import EventEnvelope
from aats.schemas.exchange import ExchangeAccountSnapshot
from aats.storage.hot_state_store import HotStateStore, make_key

# Redis namespace — 与 hot_state_store.py 现有 NS_ACCOUNT 对齐
_NS_ACCOUNT = "account"

ACCOUNT_SNAPSHOT_KEY_LATEST = "latest_snapshot"
"""Redis key 第二段：``aats:hot:account:latest_snapshot``。"""

ACCOUNT_SNAPSHOT_EVENT_TYPE = "ExchangeAccountSnapshotPublished"
"""Event envelope ``event_type``。"""

ACCOUNT_SNAPSHOT_SOURCE_COMPONENT = "aats.execution_engine.account_snapshot_cache"
"""Event envelope ``source_component``。"""


def _redis_key() -> str:
    return make_key(_NS_ACCOUNT, ACCOUNT_SNAPSHOT_KEY_LATEST)


class AccountSnapshotCache:
    """Sidecar cache for cross-process ``ExchangeAccountSnapshot`` sharing.

    See module docstring for full design rationale.

    Lifecycle:
        1. ``__init__`` — zero-argument, lightweight
        2. ``bootstrap(hot_state_store, bus, process_role)`` — Redis hydrate + optional subscribe
        3. ``register_remote_subscription(bus)`` — NATS subscribe via ``_CollectingBus``
        4. ``publish(snapshot)`` — execution role calls after each refresh
        5. ``stop()`` — cleanup logging

    Readers access ``get_sync()`` or ``latest`` property.
    """

    _DEFAULT_REDIS_TTL_SECONDS: int = 1800

    def __init__(
        self,
        *,
        logger: logging.Logger,
        redis_ttl_seconds: int | None = None,
    ) -> None:
        self._logger = logger
        self._hot_state_store: HotStateStore | None = None
        self._bus: EventBus | None = None
        self._process_role: str = "unknown"
        self._latest: ExchangeAccountSnapshot | None = None
        self._latest_recent_bills: list[dict[str, Any]] = []
        self._bootstrapped: bool = False
        self._subscribed: bool = False
        self._redis_ttl_seconds: int = (
            redis_ttl_seconds
            if redis_ttl_seconds is not None
            else self._DEFAULT_REDIS_TTL_SECONDS
        )

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
        """启动期 hydration。

        1. 从 Redis 读 ``aats:hot:account:latest_snapshot``
        2. 如果存在且能 parse -> 写本地 ``_latest``
        3. (可选) 订阅 NATS ``account.snapshots`` topic

        ``subscribe=False`` 时由 ``_wire_event_subscriptions`` 经 ``_CollectingBus``
        调 ``register_remote_subscription`` 完成。
        """
        self._hot_state_store = hot_state_store
        self._bus = bus
        self._process_role = process_role

        # Step 1: Redis hydrate
        key = _redis_key()
        try:
            stored: Any = await hot_state_store.get(key)
        except Exception as exc:
            log_event(
                self._logger,
                "account_snapshot_cache_bootstrap_redis_failed",
                level="warning",
                process_role=process_role,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            stored = None

        if isinstance(stored, dict):
            try:
                snapshot_data = stored.get("snapshot", stored)
                snapshot = ExchangeAccountSnapshot.model_validate(snapshot_data)
                self._latest = snapshot
                bills = stored.get("recent_bills")
                if isinstance(bills, list):
                    self._latest_recent_bills = [
                        dict(row) for row in bills if isinstance(row, dict)
                    ]
                log_event(
                    self._logger,
                    "account_snapshot_cache_bootstrap_hydrated",
                    process_role=process_role,
                    fetched_at=snapshot.fetched_at.isoformat(),
                    account_source=snapshot.account_source,
                    recent_bills_count=len(self._latest_recent_bills),
                )
            except Exception as exc:
                log_event(
                    self._logger,
                    "account_snapshot_cache_bootstrap_parse_failed",
                    level="warning",
                    process_role=process_role,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
        else:
            log_event(
                self._logger,
                "account_snapshot_cache_bootstrap_empty",
                process_role=process_role,
            )

        self._bootstrapped = True

        # Step 2: NATS subscribe
        if subscribe:
            await self.register_remote_subscription(bus)

    async def register_remote_subscription(self, bus: EventBus) -> None:
        """订阅 ``account.snapshots`` 远端事件。

        production 路径的入口：``_wire_event_subscriptions`` 在 ``_CollectingBus``
        上调本方法，把 cache 的 handler 和其它 ``account.snapshots`` 订阅者聚合到
        同一个 NATS JetStream durable consumer。
        """
        try:
            await bus.subscribe(topics.ACCOUNT_SNAPSHOTS, self._handle_remote_event)
            self._subscribed = True
            log_event(
                self._logger,
                "account_snapshot_cache_subscribed",
                process_role=self._process_role,
                topic=topics.ACCOUNT_SNAPSHOTS,
            )
        except Exception as exc:
            log_event(
                self._logger,
                "account_snapshot_cache_subscribe_failed",
                level="warning",
                process_role=self._process_role,
                error_type=type(exc).__name__,
                error=str(exc),
            )

    async def stop(self) -> None:
        """关闭期清理。仅做日志记录。"""
        log_event(
            self._logger,
            "account_snapshot_cache_stopped",
            process_role=self._process_role,
            subscribed=self._subscribed,
            has_snapshot=self._latest is not None,
        )

    # ──────────────────────────────────────────────────────────────────
    # 写路径（execution role 的 _refresh_account_loop 调）
    # ──────────────────────────────────────────────────────────────────

    async def publish(
        self,
        snapshot: ExchangeAccountSnapshot,
        *,
        recent_bills: list[dict[str, Any]] | None = None,
    ) -> None:
        """execution role 刷新成功后调用。

        步骤 (D5):
        1. 同步更新本地 ``_latest`` + ``_latest_recent_bills``
        2. best-effort 写 Redis（raw 白名单裁剪，见 D7）
        3. best-effort 广播 NATS（同上）

        idempotent (D6): ``fetched_at <= local`` 则 noop。

        P2 修复：``recent_bills`` 一并广播，让非 execution 角色的
        ``recent_funding_fee_summary()`` / ``recent_bills_summary()`` 不再永远为空。
        """
        if not self._apply_locally(snapshot):
            return
        if recent_bills is not None:
            self._latest_recent_bills = list(recent_bills)
        await self._best_effort_redis_set(snapshot)
        await self._best_effort_nats_broadcast(snapshot)
        log_event(
            self._logger,
            "account_snapshot_cache_publish_applied",
            process_role=self._process_role,
            fetched_at=snapshot.fetched_at.isoformat(),
            recent_bills_count=len(self._latest_recent_bills),
        )

    # ──────────────────────────────────────────────────────────────────
    # 读路径
    # ──────────────────────────────────────────────────────────────────

    @property
    def latest(self) -> ExchangeAccountSnapshot | None:
        """当前缓存的最新 snapshot（可能为 None）。"""
        return self._latest

    def get_sync(self) -> ExchangeAccountSnapshot | None:
        """sync 读取接口。返回 None 表示 cache miss / 未 bootstrap。"""
        return self._latest

    @property
    def recent_bills(self) -> list[dict[str, Any]]:
        """当前缓存的 recent_bills（可能为空 list）。"""
        return list(self._latest_recent_bills)

    def snapshot(self) -> dict[str, Any]:
        """诊断用快照。"""
        return {
            "bootstrapped": self._bootstrapped,
            "subscribed": self._subscribed,
            "has_snapshot": self._latest is not None,
            "fetched_at": self._latest.fetched_at.isoformat() if self._latest else None,
            "recent_bills_count": len(self._latest_recent_bills),
            "process_role": self._process_role,
        }

    # ──────────────────────────────────────────────────────────────────
    # 内部方法
    # ──────────────────────────────────────────────────────────────────

    def _apply_locally(self, snapshot: ExchangeAccountSnapshot) -> bool:
        """idempotent 本地更新。返回 True 表示已更新，False 表示 stale/noop。"""
        if self._latest is not None and snapshot.fetched_at <= self._latest.fetched_at:
            return False
        self._latest = snapshot
        return True

    def _build_broadcast_payload(self, snapshot: ExchangeAccountSnapshot) -> dict[str, Any]:
        """构造同时包含 snapshot 和 recent_bills 的广播负载。

        格式::

            {
                "snapshot": { ... },   # ExchangeAccountSnapshot dump, raw 白名单裁剪
                "recent_bills": [ ... ],  # list[dict]
            }
        """
        snapshot_data = snapshot.model_dump(mode="json")
        # P2 fix: 不再整体剥离 raw，而是白名单保留 funding_rate_by_symbol，
        # 因为非 execution 角色的 OKXAccountService.funding_schedule() 依赖此字段。
        raw = snapshot_data.pop("raw", None)
        if isinstance(raw, dict):
            whitelisted_raw: dict[str, Any] = {}
            funding_rate = raw.get("funding_rate_by_symbol")
            if funding_rate is not None:
                whitelisted_raw["funding_rate_by_symbol"] = funding_rate
            if whitelisted_raw:
                snapshot_data["raw"] = whitelisted_raw
        return {
            "snapshot": snapshot_data,
            "recent_bills": list(self._latest_recent_bills),
        }

    async def _best_effort_redis_set(self, snapshot: ExchangeAccountSnapshot) -> None:
        """best-effort 写 Redis。raw 白名单裁剪 (D7)。"""
        if self._hot_state_store is None:
            return
        try:
            payload = self._build_broadcast_payload(snapshot)
            await self._hot_state_store.set(_redis_key(), payload, ttl_seconds=self._redis_ttl_seconds)
        except Exception as exc:
            log_event(
                self._logger,
                "account_snapshot_cache_redis_set_failed",
                level="warning",
                process_role=self._process_role,
                error_type=type(exc).__name__,
                error=str(exc),
            )

    async def _best_effort_nats_broadcast(self, snapshot: ExchangeAccountSnapshot) -> None:
        """best-effort NATS 广播。payload 包含 snapshot + recent_bills (D7)。"""
        if self._bus is None:
            return
        try:
            broadcast_data = self._build_broadcast_payload(snapshot)
            envelope = EventEnvelope(
                event_type=ACCOUNT_SNAPSHOT_EVENT_TYPE,
                source_component=ACCOUNT_SNAPSHOT_SOURCE_COMPONENT,
                topic=topics.ACCOUNT_SNAPSHOTS,
                key="latest",
                payload=broadcast_data,
            )
            await self._bus.publish(
                topic=topics.ACCOUNT_SNAPSHOTS,
                key="latest",
                payload=envelope.model_dump(mode="json"),
            )
        except Exception as exc:
            log_event(
                self._logger,
                "account_snapshot_cache_nats_broadcast_failed",
                level="warning",
                process_role=self._process_role,
                error_type=type(exc).__name__,
                error=str(exc),
            )

    async def _handle_remote_event(self, message: dict[str, Any]) -> None:
        """NATS 远端事件处理器。

        解析 envelope -> 提取 ExchangeAccountSnapshot + recent_bills
        -> idempotent 更新本地。
        """
        try:
            envelope = EventEnvelope.model_validate(message["payload"])
            payload_data = envelope.payload
            # 新格式：payload = {"snapshot": {...}, "recent_bills": [...]}
            # 兼容旧格式：payload 直接就是 ExchangeAccountSnapshot dump
            if "snapshot" in payload_data:
                snapshot_data = payload_data["snapshot"]
                bills = payload_data.get("recent_bills", [])
            else:
                snapshot_data = payload_data
                bills = []
            snapshot = ExchangeAccountSnapshot.model_validate(snapshot_data)
            remote_bills = [dict(row) for row in bills if isinstance(row, dict)]
        except Exception as exc:
            log_event(
                self._logger,
                "account_snapshot_cache_remote_event_parse_failed",
                level="warning",
                process_role=self._process_role,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return

        applied = self._apply_locally(snapshot)
        if applied:
            self._latest_recent_bills = remote_bills
            log_event(
                self._logger,
                "account_snapshot_cache_remote_event_applied",
                process_role=self._process_role,
                fetched_at=snapshot.fetched_at.isoformat(),
                recent_bills_count=len(remote_bills),
            )
            # 通知外部 listener（用于将 snapshot+bills 写回 account_service）
            if self._on_state_updated is not None:
                try:
                    self._on_state_updated(snapshot, remote_bills)
                except Exception as exc:
                    log_event(
                        self._logger,
                        "account_snapshot_cache_listener_failed",
                        level="warning",
                        process_role=self._process_role,
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
        else:
            log_event(
                self._logger,
                "account_snapshot_cache_remote_event_stale",
                level="debug",
                process_role=self._process_role,
                fetched_at=snapshot.fetched_at.isoformat(),
                local_fetched_at=self._latest.fetched_at.isoformat() if self._latest else None,
            )

    # ──────────────────────────────────────────────────────────────────
    # listener 注册
    # ──────────────────────────────────────────────────────────────────

    _on_state_updated: Any = None

    def set_on_state_updated(self, callback: Any) -> None:
        """注册一个回调：每当 cache 从远端事件更新了 snapshot + bills 时调用。

        回调签名::

            def callback(
                snapshot: ExchangeAccountSnapshot,
                recent_bills: list[dict[str, Any]],
            ) -> None

        典型用法：非 execution 角色把回调设为更新
        ``account_service._latest_snapshot`` + ``account_service._latest_recent_bills``，
        让 account_service 的所有读路径自动获取到跨进程同步的状态。
        """
        self._on_state_updated = callback

    # 向后兼容别名
    def set_on_snapshot_updated(self, callback: Any) -> None:
        """向后兼容：包装为 (snapshot, bills) 签名的回调。"""
        def _wrapped(snapshot: ExchangeAccountSnapshot, _bills: list[dict[str, Any]]) -> None:
            callback(snapshot)
        self._on_state_updated = _wrapped
