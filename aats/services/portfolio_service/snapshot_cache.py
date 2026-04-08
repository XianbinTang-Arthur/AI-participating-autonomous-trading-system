"""Stage 6 Slice 6.3：portfolio_snapshot 跨进程缓存。

设计文档
========
docs/task/stage_6_slice_6_3_portfolio_snapshot_design.md

核心问题
========
4 进程拓扑下，gateway 进程的 dashboard 反复 polling 5+ 个 endpoint，每次都
funnel 到 ``OperatorQueryService._latest_scoped_snapshot`` → 直接打 portfolio_repo
→ Postgres SELECT。symbols 扩张后这一路 QPS 会线性增长。本 slice 在 query
路径上插一层共享缓存，把热的 latest snapshot 在所有进程间用 NATS 实时同步、
用 Redis 持久化兜底重启 hydrate。

三层架构（与 6.2 KillSwitchSyncService 同 sidecar 模板）
========
``OperatorQueryService._latest_scoped_snapshot``（query_service.py:957，已存在）：
    sync 路径，10 处 caller 全部位于 dashboard / operator API 子树。本 slice
    只在这一处加 cache 优先 + portfolio_repo fallback，sync 签名保持不变。

``PortfolioSnapshotCache``（本模块，新增）：
    持有 ``HotStateStore`` (Redis) + ``EventBus`` (NATS) + ``process_role``。
    把 4 个进程的 dashboard 视图收敛到同一份"最新 snapshot 视图"。

两条数据通路：
    Redis ``aats:hot:portfolio:latest:<scope_fingerprint>`` —— 持久化跨重启
    NATS ``portfolio.snapshots`` —— 跨进程实时广播（由 outbox publisher 发，
    cache 只订阅，不再额外广播）

关键决策（详见设计文档 §4.2）
========
D2  cache 注入是 query_service 的私有字段，sync caller API 不动
D5  ``cache.publish(snapshot)`` = 同步更新本地 dict + best-effort 写 Redis；
    **不广播 NATS**，NATS 由 outbox publisher 现有的 flush_pending 流程驱动
D6  ``_handle_remote_event`` 用 ``snapshot.snapshot_ts <= 本地`` idempotent
    比较，同时承担"自回环跳过"+"防退化"两个职责，**不依赖 source_role 字段**
D8  4 个 process_role 都装 cache，统一行为，cache 类没有 process_role 分支
D9  cache 注入点严格限定在 ``query_service._latest_scoped_snapshot``，
    **不 wrap PortfolioRepository、不修改 latest_snapshot_for_scope helper**。
    所有 production 路径（context_builder / coordinator / recovery /
    reconciliation / startup_recovery）直接打 PG，**完全绕过 cache**

不变量 I1-I9
========
I1 execution 进程内的写入对本地 portfolio_repo 立即可见 — outbox publisher
   现有事务不变
I2 跨 4 进程 ≤1s 同步 — NATS 广播延迟 < 50ms（已在 critical 路径）
I3 进程崩溃 + restart 之后 cache 恢复最近一份 snapshot — bootstrap 从 Redis 读
I4 Redis 不可达：cache 读 fallback Postgres，cache 写 best-effort 跳过
I5 NATS 不可达：cache subscriber 收不到广播；下次 sync caller miss → fallback
I6 cache miss 不破坏读：所有 sync caller fallback 到 portfolio_repo
I7 8 处 sync caller API / 签名不变
I8 乱序 / 重投的 NATS 事件：D6 的 ``snapshot_ts <= 本地`` noop 规则
I9 scope 隔离：不同 product_type/margin_mode 的 snapshot 互不污染
"""
from __future__ import annotations

import logging
from typing import Any

from aats.bootstrap.logging import log_event
from aats.bus.base import EventBus
from aats.events import topics
from aats.events.envelopes import parse_envelope
from aats.schemas.portfolio import PortfolioSnapshot
from aats.services.runtime_scope import RuntimeStateScope
from aats.storage.hot_state_store import HotStateStore, make_key

# 本 slice 用的 hot_state_store namespace。和 hot_state_store.py 现有 NS_MARKET /
# NS_ACCOUNT 同一级，避免和其它子系统的 key 撞。命名留在本文件而不外提，因为只
# 有 cache 自己用。如果未来其它 portfolio 相关的 hot state 也需要存 Redis，可以
# 把这个常量提到 hot_state_store.py。
_NS_PORTFOLIO = "portfolio"

PORTFOLIO_SNAPSHOT_KEY_LATEST = "latest"
"""Redis key 第二段，区分 'latest snapshot' 和未来可能的其它 view。"""

PORTFOLIO_SNAPSHOT_EVENT_TYPE = "PortfolioSnapshotPublished"
"""outbox publisher 发的 envelope event_type。cache 不强依赖这个字段（远端事件
解析只看 envelope.payload），但记录在此供诊断 / 文档用途。"""


class PortfolioSnapshotCache:
    """Sidecar cache for the latest ``PortfolioSnapshot`` per scope.

    See design doc §4 for the full architecture. Behavior summary:

    - Local in-memory ``dict[scope_fingerprint, PortfolioSnapshot]`` is the
      source of cached truth for sync readers (D2, D9: only
      ``OperatorQueryService._latest_scoped_snapshot`` reads it).
    - ``publish(snapshot)`` updates the local dict synchronously and
      best-effort writes Redis. It does NOT broadcast NATS — the outbox
      publisher's existing ``flush_pending()`` flow drives the NATS path
      (D5).
    - ``_handle_remote_event()`` applies remote NATS events using the
      ``snapshot_ts <= local`` rule, which simultaneously handles self-loop
      noop and stale-event rejection (D6).
    - ``get_sync(scope)`` returns the local dict entry or ``None``;
      readers fall back to ``portfolio_repo`` on miss (I6).
    - All four process_roles install the cache identically (D8).
    """

    def __init__(
        self,
        *,
        hot_state_store: HotStateStore,
        bus: EventBus,
        process_role: str,
        logger: logging.Logger,
    ) -> None:
        self._hot_state_store = hot_state_store
        self._bus = bus
        self._process_role = process_role
        self._logger = logger
        # 主数据：scope_fingerprint → 最新 snapshot
        self._latest: dict[str, PortfolioSnapshot] = {}
        # bootstrap 是否成功跑过
        self._bootstrapped: bool = False
        # NATS 订阅是否成功
        self._subscribed: bool = False
        # bootstrap 时记下的 fingerprints（诊断用）
        self._bootstrapped_scopes: list[str] = []

    # ──────────────────────────────────────────────────────────────────
    # 启动 / 关闭
    # ──────────────────────────────────────────────────────────────────

    async def bootstrap(
        self,
        *,
        scope_fingerprint: str,
        subscribe: bool = True,
    ) -> None:
        """启动期 hydration：

        1. 从 Redis 读 ``aats:hot:portfolio:latest:<scope_fingerprint>``
        2. 如果存在且能 parse → 写本地 dict
        3. （可选）订阅 NATS PORTFOLIO_SNAPSHOTS topic

        ``subscribe`` 参数允许 caller 把订阅步骤推迟到外层 wiring。**production
        路径**（``build_runtime`` → ``_wire_event_subscriptions``）必须传
        ``subscribe=False``，让 cache 的远端订阅通过同一个 ``_CollectingBus``
        被聚合到 audit / reconciliation 等已有的 portfolio.snapshots 订阅上，
        共用一个 NATS JetStream durable consumer，避开 "consumer is already
        bound to a subscription" 错误。Stage 7 修复 ``_CollectingBus`` 时
        已经踩过同样的坑（POSITION_TARGETS / PORTFOLIO_SNAPSHOTS /
        RECONCILIATION_REPORTS），见 ``_wire_event_subscriptions`` docstring。
        deferred subscribe 之后必须 explicit 调用 ``register_remote_subscription``。

        默认 ``subscribe=True`` 保留单元测试与 in-memory 模拟下的"一次到位"
        语义；InMemoryEventBus 没有 durable name 冲突，所以走默认路径无害。

        ⚠️ 任何步骤的失败都不能阻止 build_runtime 完成（与 6.2 同语义）。
        """
        # Step 1: Redis hydrate
        key = self._key_for(scope_fingerprint)
        try:
            stored: Any = await self._hot_state_store.get(key)
        except Exception as exc:
            log_event(
                self._logger,
                "portfolio_snapshot_cache_bootstrap_redis_failed",
                level="warning",
                process_role=self._process_role,
                scope_fingerprint=scope_fingerprint,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            stored = None

        if isinstance(stored, dict):
            try:
                snapshot = PortfolioSnapshot.model_validate(stored)
                self._latest[scope_fingerprint] = snapshot
                log_event(
                    self._logger,
                    "portfolio_snapshot_cache_bootstrap_hydrated",
                    process_role=self._process_role,
                    scope_fingerprint=scope_fingerprint,
                    snapshot_ts=snapshot.snapshot_ts.isoformat(),
                    decision_id=snapshot.decision_id,
                )
            except Exception as exc:
                log_event(
                    self._logger,
                    "portfolio_snapshot_cache_bootstrap_parse_failed",
                    level="warning",
                    process_role=self._process_role,
                    scope_fingerprint=scope_fingerprint,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
        else:
            log_event(
                self._logger,
                "portfolio_snapshot_cache_bootstrap_empty",
                process_role=self._process_role,
                scope_fingerprint=scope_fingerprint,
            )

        self._bootstrapped = True
        if scope_fingerprint not in self._bootstrapped_scopes:
            self._bootstrapped_scopes.append(scope_fingerprint)

        # Step 2: NATS subscribe（订阅失败也不抛）。
        # subscribe=False 时由 _wire_event_subscriptions 经 _CollectingBus 路径
        # 调 register_remote_subscription 完成。
        if subscribe:
            await self.register_remote_subscription(self._bus)

    async def register_remote_subscription(self, bus: EventBus) -> None:
        """订阅 ``portfolio.snapshots`` 远端事件。

        production 路径的入口：``_wire_event_subscriptions`` 在 ``_CollectingBus``
        上调本方法，把 cache 的 ``_handle_remote_event`` 与 audit /
        reconciliation 等其它 portfolio.snapshots 订阅者共聚合到同一个 fan-out
        handler，最终落在同一个 NATS JetStream durable consumer 上（每个
        process_role + topic 在 NATS 里只能有一个 durable binding）。

        允许 ``bus`` 是 ``_CollectingBus`` 或真实的 ``EventBus``。两种情况
        下行为都是 best-effort：失败 log warning 不抛。
        """
        try:
            await bus.subscribe(topics.PORTFOLIO_SNAPSHOTS, self._handle_remote_event)
            self._subscribed = True
            log_event(
                self._logger,
                "portfolio_snapshot_cache_subscribed",
                process_role=self._process_role,
                topic=topics.PORTFOLIO_SNAPSHOTS,
            )
        except Exception as exc:
            log_event(
                self._logger,
                "portfolio_snapshot_cache_subscribe_failed",
                level="warning",
                process_role=self._process_role,
                error_type=type(exc).__name__,
                error=str(exc),
            )

    async def stop(self) -> None:
        """关闭期清理。EventBus 抽象不支持 unsubscribe，仅做日志记录。

        ⚠️ 不要在 stop 里写 / 删除 Redis：cache 状态是"最近一份 snapshot"，
        关闭不代表数据失效，下次启动应该读到最近一次的内容。
        """
        log_event(
            self._logger,
            "portfolio_snapshot_cache_stopped",
            process_role=self._process_role,
            subscribed=self._subscribed,
            cached_scopes=list(self._latest.keys()),
        )

    # ──────────────────────────────────────────────────────────────────
    # 写路径（execution outbox commit hook 调）
    # ──────────────────────────────────────────────────────────────────

    async def publish(self, snapshot: PortfolioSnapshot) -> None:
        """outbox publisher commit 之后的 hook。

        步骤（D5）：
        1. **同步**更新本地 in-memory dict（execution 进程自己的 dashboard 立
           即受益，不必 fallback PG）
        2. **best-effort** 写 Redis（其他进程 bootstrap 时 hydrate 兜底）
        3. **不广播 NATS** — outbox publisher 的 ``flush_pending`` 已经发了

        idempotent 保证（D6）：如果 snapshot.snapshot_ts <= 本地同 scope 的
        ts，视为重复或乱序，noop（不抛、不写 Redis）。
        """
        scope_fingerprint = self._scope_fingerprint_from_snapshot(snapshot)
        applied = self._apply_locally(scope_fingerprint, snapshot)
        if not applied:
            log_event(
                self._logger,
                "portfolio_snapshot_cache_publish_noop_stale",
                process_role=self._process_role,
                scope_fingerprint=scope_fingerprint,
                snapshot_ts=snapshot.snapshot_ts.isoformat(),
            )
            return
        await self._best_effort_redis_set(scope_fingerprint, snapshot)
        log_event(
            self._logger,
            "portfolio_snapshot_cache_publish_applied",
            process_role=self._process_role,
            scope_fingerprint=scope_fingerprint,
            snapshot_ts=snapshot.snapshot_ts.isoformat(),
            decision_id=snapshot.decision_id,
        )

    def apply_sync(self, snapshot: PortfolioSnapshot) -> None:
        """Stage 6 Slice 6.3 hot-fix：sync 版本的 publish，只同步本地 dict。

        用作 ``portfolio_repo.save_snapshot`` 的 listener 钩子。所有绕过
        outbox publisher 直接写 repo 的路径（recovery / repair / projections
        / positions / tests）通过这个钩子把 snapshot 同步到 cache 本地 dict，
        修复 operator UI 读到 stale bootstrap snapshot 的 bug。

        - 不写 Redis（sync 路径不能 await；Redis 端由 outbox publisher 路径
          覆盖）
        - 不发 NATS（跨进程传播由 outbox publisher 现有路径负责）
        - 复用 ``_apply_locally`` 的 ``snapshot_ts <= existing`` idempotent
          规则：outbox publisher 路径会再次调 ``publish()``，那次会被 noop

        详见 docs/task/stage_6_slice_6_3_cache_listener_fix_design.md。
        """
        try:
            scope_fingerprint = self._scope_fingerprint_from_snapshot(snapshot)
        except Exception as exc:
            log_event(
                self._logger,
                "portfolio_snapshot_cache_apply_sync_parse_failed",
                level="warning",
                process_role=self._process_role,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return
        applied = self._apply_locally(scope_fingerprint, snapshot)
        if applied:
            log_event(
                self._logger,
                "portfolio_snapshot_cache_apply_sync_applied",
                process_role=self._process_role,
                scope_fingerprint=scope_fingerprint,
                snapshot_ts=snapshot.snapshot_ts.isoformat(),
                decision_id=snapshot.decision_id,
            )

    # ──────────────────────────────────────────────────────────────────
    # 读路径（query_service._latest_scoped_snapshot 调）
    # ──────────────────────────────────────────────────────────────────

    def get_sync(self, scope: RuntimeStateScope) -> PortfolioSnapshot | None:
        """sync 路径：读本地 in-memory dict。

        miss 时返回 ``None``，caller 应该 fallback 到 ``portfolio_repo`` (I6)。
        """
        scope_fingerprint = self._scope_fingerprint(scope)
        return self._latest.get(scope_fingerprint)

    # ──────────────────────────────────────────────────────────────────
    # NATS 远端事件接收
    # ──────────────────────────────────────────────────────────────────

    async def _handle_remote_event(self, message: dict[str, Any]) -> None:
        """订阅 ``portfolio.snapshots`` 后的回调。

        D6 的 idempotent 规则：远端 ``snapshot_ts <= 本地同 scope 的 ts`` →
        noop。这一条规则同时覆盖三个场景：

        - execution 自己回环（ts 必然相等）
        - 乱序 / 重投事件（ts 更小）
        - 同毫秒 corner case（ts 相等，无害 noop）
        """
        try:
            envelope = parse_envelope(message)
            snapshot = PortfolioSnapshot.model_validate(envelope.payload)
        except Exception as exc:
            log_event(
                self._logger,
                "portfolio_snapshot_cache_remote_parse_failed",
                level="warning",
                process_role=self._process_role,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return

        scope_fingerprint = self._scope_fingerprint_from_snapshot(snapshot)
        applied = self._apply_locally(scope_fingerprint, snapshot)
        if not applied:
            log_event(
                self._logger,
                "portfolio_snapshot_cache_remote_skipped_stale",
                process_role=self._process_role,
                scope_fingerprint=scope_fingerprint,
                snapshot_ts=snapshot.snapshot_ts.isoformat(),
            )
            return

        log_event(
            self._logger,
            "portfolio_snapshot_cache_remote_applied",
            process_role=self._process_role,
            scope_fingerprint=scope_fingerprint,
            snapshot_ts=snapshot.snapshot_ts.isoformat(),
            decision_id=snapshot.decision_id,
        )

    # ──────────────────────────────────────────────────────────────────
    # 内部 helpers
    # ──────────────────────────────────────────────────────────────────

    def _apply_locally(
        self,
        scope_fingerprint: str,
        snapshot: PortfolioSnapshot,
    ) -> bool:
        """idempotent local apply（D6）。

        Returns ``True`` 表示新 snapshot 被应用了，``False`` 表示因 ts 退化或
        重复被 noop 跳过。caller 用返回值决定是否后续 best-effort 写 Redis +
        发应用日志。
        """
        existing = self._latest.get(scope_fingerprint)
        if existing is not None and snapshot.snapshot_ts <= existing.snapshot_ts:
            return False
        self._latest[scope_fingerprint] = snapshot
        return True

    async def _best_effort_redis_set(
        self,
        scope_fingerprint: str,
        snapshot: PortfolioSnapshot,
    ) -> None:
        """best-effort 写 Redis。失败 log warning 不抛。"""
        try:
            await self._hot_state_store.set(
                self._key_for(scope_fingerprint),
                snapshot.model_dump(mode="json"),
            )
        except Exception as exc:
            log_event(
                self._logger,
                "portfolio_snapshot_cache_redis_set_failed",
                level="warning",
                process_role=self._process_role,
                scope_fingerprint=scope_fingerprint,
                error_type=type(exc).__name__,
                error=str(exc),
            )

    @staticmethod
    def _scope_fingerprint(scope: RuntimeStateScope) -> str:
        """``RuntimeStateScope`` → ``'product_type:margin_mode'``。

        与 ``portfolio_repo.latest_for_scope`` 的 WHERE 子句保持一致：
        Postgres 表 ``portfolio_snapshots`` 的 scope 列是 product_type +
        margin_mode 两维（不带 symbol，因为 portfolio snapshot 是 account-wide）。
        ``snapshot.product_type`` 和 ``snapshot.margin_mode`` 字段也是这两维，
        所以 publish() 端和 get_sync() 端通过这个 fingerprint 自然对齐。
        """
        return f"{scope.product_type}:{scope.margin_mode}"

    @staticmethod
    def _scope_fingerprint_from_snapshot(snapshot: PortfolioSnapshot) -> str:
        """从 snapshot 字段反推 fingerprint，与 ``_scope_fingerprint(scope)`` 一致。"""
        return f"{snapshot.product_type}:{snapshot.margin_mode}"

    @staticmethod
    def _key_for(scope_fingerprint: str) -> str:
        """生成 Redis key：``aats:hot:portfolio:latest:<scope_fingerprint>``。"""
        return make_key(
            _NS_PORTFOLIO,
            PORTFOLIO_SNAPSHOT_KEY_LATEST,
            scope_fingerprint,
        )

    # ──────────────────────────────────────────────────────────────────
    # 诊断 / 内省
    # ──────────────────────────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        """启动日志 / dashboard 用的内省 dict。"""
        return {
            "process_role": self._process_role,
            "bootstrapped": self._bootstrapped,
            "subscribed": self._subscribed,
            "cached_scopes": list(self._latest.keys()),
            "bootstrapped_scopes": list(self._bootstrapped_scopes),
        }
