"""Stage 6 Slice 6.5：obligation 跨进程缓存。

设计文档
========
docs/task/stage_6_slice_6_5_obligation_hot_state_design.md

核心问题
========
4 进程拓扑下，decision 进程在每次 risk pre-check 都要打 Postgres
``obligation_repo.active_obligations()``，跨 symbol 扩张后这一路 QPS 会线性增长。
gateway 进程的 dashboard polling 也频繁查 ``all_obligations()``。本 slice 在
这些读路径上插一层共享缓存：local dict + Redis 持久化 + NATS 实时广播。

三层架构（与 6.3 PortfolioSnapshotCache 同 sidecar 模板）
========
``ExecutionObligationService``（写方）：
    每次 ``save_obligation`` 返回之后 best-effort fire-and-forget
    ``cache.publish(obligation)``。本地 dict 立即更新，Redis 和 NATS 都是
    best-effort 广播。

``ObligationHotStateCache``（本模块，新增）：
    持有 ``HotStateStore`` (Redis) + ``EventBus`` (NATS) + ``process_role``。
    把 4 个进程的 obligation 视图收敛到同一份"最新 dict"。

``risk.py::RiskEngine._active_obligations`` / ``recovery_posture`` /
``query_service`` 等读方：
    sync 签名保持不变。先调 ``cache.active_sync()`` 或 ``all_sync()``，返回
    None 表示 cache 未 bootstrap 或缓存无数据（无法区分，后者也走 fallback），
    此时调用方 fallback ``obligation_repo`` 原路径。

两条数据通路：
    Redis ``aats:hot:obligation:by_coid:<client_order_id>`` —— per-coid KV
    Redis ``aats:hot:obligation:index`` —— 存 all_coids/active_coids/version
    NATS ``execution.obligation_updates`` —— 单条 OrderObligation envelope

关键决策（详见设计文档 §2）
========
D1  cache 类名 ``ObligationHotStateCache``，放 execution_engine 子树
D2  本地 ``dict[client_order_id, OrderObligation]`` 作为 source of cached truth
D3  publish 由 service 层触发，**不**改 obligation_repo session 事务边界
D4  新 topic ``execution.obligation_updates``，单条 envelope 即 full payload
D5  publish 三步：local set → best-effort Redis set → best-effort NATS publish
D6  Redis key = ``aats:hot:obligation:by_coid:<coid>`` + index key
D8  bootstrap: read index → get_many coids → hydrate local dict → subscribe NATS
D9  ``_handle_remote_event`` 用 ``last_update_ts <= local`` idempotent 判断
D12 零参构造允许；未 bootstrap 时 publish 退到 local-only 不抛
D13 ``get_sync`` / ``active_sync`` / ``all_sync`` 在未 bootstrap 时返回 None
D15 4 进程对称装载，cache 类内部没有 process_role 分支

不变量 I1-I5
========
I1 fail-soft：Redis/NATS 任何失败都不阻塞 obligation_repo 主路径
I2 cross-process ≤1s：NATS 实时广播；local dict publish 立即可见
I3 restart-safe：bootstrap 从 Redis 读 index + get_many 恢复
I4 idempotent：同 coid 乱序事件按 last_update_ts 判断，退化则 noop
I5 miss 不破坏读：cache 未 bootstrap / Redis 挂 / NATS 挂 → sync 返回 None
   → caller fallback obligation_repo（PG 作为 source of truth）
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
from aats.schemas.common import EventEnvelope, dump_payload_exact
from aats.schemas.execution import OrderObligation
from aats.storage.hot_state_store import HotStateStore, make_key

# 本 slice 用的 hot_state_store namespace。与 hot_state_store.py 现有 NS_MARKET /
# NS_ACCOUNT / NS_SYSTEM 同一级。留在本文件而不外提，因为只有 obligation cache
# 自己用。
# Redis key TTL（秒）。避免已终结的 obligation（FILLED / CANCELED 等）永驻
# Redis 造成内存泄漏。7 天与 NATS JetStream 流保留期对齐。bootstrap 时
# 如果 per-coid key 已过期，get_many 返回空，不影响正确性（只是少了历史
# 数据，系统从 PG 重建）。
_REDIS_TTL_SECONDS: int = 7 * 24 * 3600  # 7 days

_NS_OBLIGATION = "obligation"

OBLIGATION_INDEX_KEY = make_key(_NS_OBLIGATION, "index")
"""Redis key：``aats:hot:obligation:index``，存 active_coids / all_coids / version。"""

OBLIGATION_EVENT_TYPE = "OrderObligationUpdated"
"""Event envelope ``event_type`` field for obligation broadcasts."""

OBLIGATION_SOURCE_COMPONENT = "aats.execution_engine.obligation_cache"
"""Event envelope ``source_component`` for obligation broadcasts."""


def _obligation_key(client_order_id: str) -> str:
    """生成 per-coid Redis key：``aats:hot:obligation:by_coid:<coid>``。"""
    return make_key(_NS_OBLIGATION, "by_coid", client_order_id)


def _compare_ts(obligation: OrderObligation) -> datetime:
    """幂等 / 排序用的 timestamp 字段。

    OrderObligation.last_update_ts 可能为 None（刚被 reserve 未被修改过），
    此时退回 created_at / datetime.min。
    """
    ts = obligation.last_update_ts
    if ts is None:
        # 新建 obligation 没 last_update_ts，用一个确定的下界
        return datetime.min.replace(tzinfo=timezone.utc)
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


class ObligationHotStateCache:
    """Sidecar cache for ``OrderObligation`` across 4 processes.

    See design doc §2/§3 for full architecture. Behavior summary:

    - Local in-memory ``dict[client_order_id, OrderObligation]`` is the
      source of cached truth for sync readers.
    - ``publish(obligation)`` updates the local dict synchronously and
      best-effort writes Redis + broadcasts NATS (D5).
    - ``_handle_remote_event()`` applies remote NATS events using the
      ``last_update_ts <= local`` rule (D9).
    - ``get_sync(coid)`` / ``active_sync()`` / ``all_sync()`` return ``None``
      when cache is not bootstrapped; readers fall back to
      ``obligation_repo`` on ``None`` (I5).
    - All four process_roles install the cache identically; no role branch
      inside this class (D15).
    """

    def __init__(self, *, logger: logging.Logger) -> None:
        self._logger = logger
        # 主数据：client_order_id → 最新 obligation
        self._latest: dict[str, OrderObligation] = {}
        # sidecar 配线状态（bootstrap 后才有效）
        self._hot_state_store: HotStateStore | None = None
        self._bus: EventBus | None = None
        self._process_role: str = "monolith"
        # bootstrap 是否成功跑过
        self._bootstrapped: bool = False
        # NATS 订阅是否成功
        self._subscribed: bool = False
        # index version，每次 publish 成功递增
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
        """启动期 hydration：

        1. 从 Redis 读 ``aats:hot:obligation:index``
        2. 如果存在 all_coids 列表，``get_many`` 所有 per-coid keys
        3. parse 成功的 entry 写本地 dict
        4. （可选）订阅 NATS OBLIGATION_UPDATES topic

        ``subscribe`` 参数允许 caller 把订阅步骤推迟到外层 wiring。**production
        路径**（``build_runtime`` → ``_wire_event_subscriptions``）必须传
        ``subscribe=False``，让 cache 的远端订阅通过同一个 ``_CollectingBus`` 被
        聚合到 NATS JetStream durable consumer 上，避开 "consumer is already
        bound to a subscription" 错误。与 Slice 6.3 PortfolioSnapshotCache 同
        处理模式。

        ⚠️ 任何步骤的失败都不能阻止 build_runtime 完成（与 6.2/6.3 同语义）。
        """
        self._hot_state_store = hot_state_store
        self._bus = bus
        self._process_role = process_role

        # Step 1: 读 index
        try:
            index = await hot_state_store.get(OBLIGATION_INDEX_KEY)
        except Exception as exc:
            log_event(
                self._logger,
                "obligation_cache_bootstrap_index_failed",
                level="warning",
                process_role=self._process_role,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            index = None

        if isinstance(index, dict):
            try:
                self._index_version = int(index.get("version") or 0)
            except Exception:
                self._index_version = 0
            all_coids_raw = index.get("all_coids") or []
            if isinstance(all_coids_raw, list) and all_coids_raw:
                keys = [_obligation_key(str(coid)) for coid in all_coids_raw]
                try:
                    raws = await hot_state_store.get_many(keys)
                except Exception as exc:
                    log_event(
                        self._logger,
                        "obligation_cache_bootstrap_get_many_failed",
                        level="warning",
                        process_role=self._process_role,
                        count=len(keys),
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
                    raws = {}
                for _key, raw in raws.items():
                    if not isinstance(raw, dict):
                        continue
                    try:
                        obligation = OrderObligation.model_validate(raw)
                        self._latest[obligation.client_order_id] = obligation
                    except Exception as exc:
                        log_event(
                            self._logger,
                            "obligation_cache_bootstrap_parse_failed",
                            level="warning",
                            process_role=self._process_role,
                            error_type=type(exc).__name__,
                            error=str(exc),
                        )
                log_event(
                    self._logger,
                    "obligation_cache_bootstrap_hydrated",
                    process_role=self._process_role,
                    cached_count=len(self._latest),
                    index_version=self._index_version,
                )
            else:
                log_event(
                    self._logger,
                    "obligation_cache_bootstrap_empty_index",
                    process_role=self._process_role,
                    index_version=self._index_version,
                )
        else:
            log_event(
                self._logger,
                "obligation_cache_bootstrap_no_index",
                process_role=self._process_role,
            )

        self._bootstrapped = True

        # Step 2: NATS subscribe
        if subscribe:
            await self.register_remote_subscription(bus)

    async def register_remote_subscription(self, bus: EventBus) -> None:
        """订阅 ``execution.obligation_updates`` 远端事件。

        production 路径的入口：``_wire_event_subscriptions`` 在 ``_CollectingBus``
        上调本方法，把 cache 的 ``_handle_remote_event`` 与其它订阅者共聚合到同
        一个 NATS JetStream durable consumer。失败 log warning 不抛（best-effort）。
        """
        try:
            await bus.subscribe(topics.OBLIGATION_UPDATES, self._handle_remote_event)
            self._subscribed = True
            log_event(
                self._logger,
                "obligation_cache_subscribed",
                process_role=self._process_role,
                topic=topics.OBLIGATION_UPDATES,
            )
        except Exception as exc:
            log_event(
                self._logger,
                "obligation_cache_subscribe_failed",
                level="warning",
                process_role=self._process_role,
                error_type=type(exc).__name__,
                error=str(exc),
            )

    async def stop(self) -> None:
        """关闭期清理。EventBus 抽象不支持 unsubscribe，仅做日志记录。

        ⚠️ 不要在 stop 里写 / 删除 Redis：cache 状态是"最近一份 obligation 集"，
        关闭不代表数据失效，下次启动应该 hydrate 回来。
        """
        log_event(
            self._logger,
            "obligation_cache_stopped",
            process_role=self._process_role,
            subscribed=self._subscribed,
            cached_count=len(self._latest),
        )

    # ──────────────────────────────────────────────────────────────────
    # 写路径（ExecutionObligationService.save_obligation 之后调）
    # ──────────────────────────────────────────────────────────────────

    async def publish(
        self,
        obligation: OrderObligation,
        *,
        skip_local: bool = False,
    ) -> None:
        """由 ExecutionObligationService 在 save_obligation 之后调用。

        步骤（D5）：
        1. **同步**更新本地 in-memory dict
        2. **best-effort** 写 Redis per-coid key
        3. **best-effort** 写 Redis index key（递增 version）
        4. **best-effort** 广播 NATS OBLIGATION_UPDATES

        idempotent 保证（D9）：如果 obligation.last_update_ts <= 本地同 coid 的
        ts，视为重复或乱序，**完全 noop**（不写 Redis、不发 NATS）。

        ``skip_local``：``True`` 时跳过 step 1，直接进入远端 best-effort 步骤。
        由 ``fire_and_forget_publish`` 内部使用：sync caller 已经预先 eager apply
        了本地 dict 以保证 read-after-write 立即可见，随后 schedule 的 task 只
        做远端 Redis + NATS。**禁止** 外部 caller 直接传 True，否则 D9 退化
        保护会被绕过。
        """
        if not skip_local:
            applied = self._apply_locally(obligation)
            if not applied:
                log_event(
                    self._logger,
                    "obligation_cache_publish_noop_stale",
                    process_role=self._process_role,
                    client_order_id=obligation.client_order_id,
                    status=obligation.status,
                )
                return

        await self._best_effort_redis_set(obligation)
        await self._best_effort_redis_index_update()
        await self._best_effort_nats_broadcast(obligation)

        log_event(
            self._logger,
            "obligation_cache_publish_applied",
            process_role=self._process_role,
            client_order_id=obligation.client_order_id,
            status=obligation.status,
            skip_local=skip_local,
        )

    def apply_sync(self, obligation: OrderObligation) -> None:
        """sync 版本的 publish，只同步本地 dict。

        用在无 event loop 的 sync context（譬如某些 test helper）。不写 Redis、
        不发 NATS。publish() 的 async 路径会在同一个事件循环里重复覆盖，D9 的
        idempotent 规则保证两边最终一致。
        """
        self._apply_locally(obligation)

    def fire_and_forget_publish(self, obligation: OrderObligation | None) -> None:
        """Sync-friendly fire-and-forget wrapper for ``publish()``.

        主要 caller 是 ``ExecutionObligationService`` 的 3 个 sync save_obligation
        路径（persist_previewed_obligation / consume_for_fill /
        finalize_for_order_state）。async caller（reserve_for_intent）应直接
        ``await cache.publish(obligation)``，不走本方法。

        语义：
        1. ``obligation is None`` → noop（caller 已决定没有要写的 entity）
        2. **eager** ``_apply_locally(obligation)`` → 立即更新本地 dict，保证
           caller 在同一个 sync stack 内后续 ``get_sync`` / ``active_sync`` 读
           到最新值；D9 退化检查也在这一步兜底，重复或乱序直接 noop
        3. 未 bootstrap（``_bus`` / ``_hot_state_store`` 为 None）→ 本地已更新，
           无 sidecar 要调度，return
        4. 找不到 running event loop（极罕见，仅纯 sync 测试路径）→ local-only
           return
        5. 主 loop 内 → ``loop.create_task(publish(obligation, skip_local=True))``
           fire-and-forget，远端 Redis + NATS 由 task 异步完成

        永不抛异常（I1 fail-soft）。
        """
        if obligation is None:
            return

        # Step 1: eager local apply。无论是否 bootstrapped 都先 touch 本地 dict，
        # 这样 caller 在同一个 call stack 上后续读 dict 立即可见。D9 idempotent
        # 检查在 _apply_locally 里兜底：如果 obligation 是 stale / 重复，直接
        # noop，不会继续走远端。
        applied = self._apply_locally(obligation)
        if not applied:
            log_event(
                self._logger,
                "obligation_cache_fire_and_forget_noop_stale",
                process_role=self._process_role,
                client_order_id=obligation.client_order_id,
                status=obligation.status,
            )
            return

        # Step 2: 未 bootstrapped → 只做了本地 apply，不调度远端
        if self._bus is None and self._hot_state_store is None:
            return

        # Step 3: 找 running loop
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 纯 sync 路径（比如某些 test helper）。local dict 已更新，无远端
            # 机会；log 一条 debug 然后退出。
            log_event(
                self._logger,
                "obligation_cache_fire_and_forget_no_loop",
                level="debug",
                process_role=self._process_role,
                client_order_id=obligation.client_order_id,
            )
            return

        if loop.is_closed() or not loop.is_running():
            return

        # Step 4: 调度远端 best-effort task。skip_local=True 因为 step 1 已经
        # 预先 apply 过了，再走一次会因 D9 等 ts 判断 noop 掉整个远端步骤（Bug）。
        try:
            loop.create_task(
                self.publish(obligation, skip_local=True),
                name="obligation_cache_publish",
            )
        except Exception as exc:  # pragma: no cover
            log_event(
                self._logger,
                "obligation_cache_fire_and_forget_schedule_failed",
                level="warning",
                process_role=self._process_role,
                client_order_id=obligation.client_order_id,
                error_type=type(exc).__name__,
                error=str(exc),
            )

    # ──────────────────────────────────────────────────────────────────
    # 读路径（risk.py / query_service.py / recovery_posture.py 调）
    # ──────────────────────────────────────────────────────────────────

    def get_sync(self, client_order_id: str) -> OrderObligation | None:
        """sync 路径：按 coid 读本地 dict。

        - cache 未 bootstrap → 返回 None（调用方 fallback obligation_repo）
        - bootstrap 后找不到 coid → 返回 None（调用方 fallback）
        """
        if not self._bootstrapped:
            return None
        return self._latest.get(client_order_id)

    def active_sync(self) -> list[OrderObligation] | None:
        """sync 路径：返回 status in ACTIVE/PARTIALLY_CONSUMED 的 obligation list。

        - cache 未 bootstrap → 返回 None（调用方 fallback obligation_repo）
        - bootstrap 后无 active → 返回 [] 空列表（**不 fallback**，空确实是结果）
        """
        if not self._bootstrapped:
            return None
        return [
            o
            for o in self._latest.values()
            if o.status in {"ACTIVE", "PARTIALLY_CONSUMED"}
        ]

    def all_sync(self) -> list[OrderObligation] | None:
        """sync 路径：返回本地 dict 所有 obligation。

        - cache 未 bootstrap → 返回 None
        - bootstrap 后无数据 → 返回 []
        """
        if not self._bootstrapped:
            return None
        return list(self._latest.values())

    # ──────────────────────────────────────────────────────────────────
    # NATS 远端事件接收
    # ──────────────────────────────────────────────────────────────────

    async def _handle_remote_event(self, message: dict[str, Any]) -> None:
        """订阅 ``execution.obligation_updates`` 后的回调。

        D9 的 idempotent 规则：远端 ``last_update_ts <= 本地同 coid 的 ts`` →
        noop。这一条规则同时覆盖：

        - execution 自己回环（ts 必然相等）
        - 乱序 / 重投事件（ts 更小）
        - 同毫秒 corner case（ts 相等，无害 noop）
        """
        try:
            envelope = parse_envelope(message)
            obligation = OrderObligation.model_validate(envelope.payload)
        except Exception as exc:
            log_event(
                self._logger,
                "obligation_cache_remote_parse_failed",
                level="warning",
                process_role=self._process_role,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return

        applied = self._apply_locally(obligation)
        if not applied:
            log_event(
                self._logger,
                "obligation_cache_remote_skipped_stale",
                process_role=self._process_role,
                client_order_id=obligation.client_order_id,
            )
            return

        log_event(
            self._logger,
            "obligation_cache_remote_applied",
            process_role=self._process_role,
            client_order_id=obligation.client_order_id,
            status=obligation.status,
        )

    # ──────────────────────────────────────────────────────────────────
    # 内部 helpers
    # ──────────────────────────────────────────────────────────────────

    def _apply_locally(self, obligation: OrderObligation) -> bool:
        """idempotent local apply（D9）。

        Returns ``True`` 表示新 obligation 被应用了，``False`` 表示因 ts 退化或
        重复被 noop 跳过。caller 用返回值决定是否后续 best-effort 写 Redis + 发
        应用日志。
        """
        coid = obligation.client_order_id
        existing = self._latest.get(coid)
        if existing is not None:
            if _compare_ts(obligation) <= _compare_ts(existing):
                return False
        self._latest[coid] = obligation
        return True

    async def _best_effort_redis_set(self, obligation: OrderObligation) -> None:
        """best-effort 写 per-coid key（带 TTL）。失败 log warning 不抛。"""
        if self._hot_state_store is None:
            return
        try:
            await self._hot_state_store.set(
                _obligation_key(obligation.client_order_id),
                obligation.model_dump(mode="json"),
                ttl_seconds=_REDIS_TTL_SECONDS,
            )
        except Exception as exc:
            log_event(
                self._logger,
                "obligation_cache_redis_set_failed",
                level="warning",
                process_role=self._process_role,
                client_order_id=obligation.client_order_id,
                error_type=type(exc).__name__,
                error=str(exc),
            )

    async def _best_effort_redis_index_update(self) -> None:
        """best-effort 写 index key。

        每次 publish 成功后被调用。index 记录 all_coids + active_coids + version。
        重写整个 index 是 O(N)，当 N 在千级以内可接受；真实线上单 account 通常
        <100 个 obligation 同时 active。
        """
        if self._hot_state_store is None:
            return
        self._index_version += 1
        index_payload = {
            "all_coids": list(self._latest.keys()),
            "active_coids": [
                coid
                for coid, o in self._latest.items()
                if o.status in {"ACTIVE", "PARTIALLY_CONSUMED"}
            ],
            "version": self._index_version,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "writer_role": self._process_role,
        }
        try:
            await self._hot_state_store.set(
                OBLIGATION_INDEX_KEY, index_payload, ttl_seconds=_REDIS_TTL_SECONDS,
            )
        except Exception as exc:
            log_event(
                self._logger,
                "obligation_cache_redis_index_failed",
                level="warning",
                process_role=self._process_role,
                error_type=type(exc).__name__,
                error=str(exc),
            )

    async def _best_effort_nats_broadcast(self, obligation: OrderObligation) -> None:
        """best-effort 广播 NATS OBLIGATION_UPDATES。失败 log warning 不抛。"""
        if self._bus is None:
            return
        try:
            envelope = EventEnvelope(
                event_type=OBLIGATION_EVENT_TYPE,
                source_component=OBLIGATION_SOURCE_COMPONENT,
                topic=topics.OBLIGATION_UPDATES,
                key=obligation.client_order_id,
                payload=dump_payload_exact(obligation.model_dump(mode="json")),
            )
            await self._bus.publish(
                topic=topics.OBLIGATION_UPDATES,
                key=obligation.client_order_id,
                payload=envelope.model_dump(mode="json"),
            )
        except Exception as exc:
            log_event(
                self._logger,
                "obligation_cache_nats_publish_failed",
                level="warning",
                process_role=self._process_role,
                client_order_id=obligation.client_order_id,
                error_type=type(exc).__name__,
                error=str(exc),
            )

    # ──────────────────────────────────────────────────────────────────
    # 诊断 / 内省
    # ──────────────────────────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        """启动日志 / dashboard 用的内省 dict。"""
        active_count = sum(
            1
            for o in self._latest.values()
            if o.status in {"ACTIVE", "PARTIALLY_CONSUMED"}
        )
        return {
            "process_role": self._process_role,
            "bootstrapped": self._bootstrapped,
            "subscribed": self._subscribed,
            "cached_count": len(self._latest),
            "active_count": active_count,
            "index_version": self._index_version,
        }
