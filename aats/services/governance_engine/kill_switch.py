"""Stage 6 Slice 6.4：二合一的 KillSwitch + 跨进程同步边车。

设计文档
========
docs/task/stage_6_slice_6_4_kill_switch_unification_design.md

历史
====
Stage 6 Slice 6.2 引入了 ``KillSwitchSyncService`` 边车作为 ``KillSwitch`` 的
"跨进程同步层"。代价是 5 个写入点 (W1-W5) 都要 ``if/else`` fallback：

    if self.kill_switch_sync is not None:
        self.kill_switch_sync.halt_threadsafe(reason)
    else:
        self.kill_switch.halt(reason=reason)

fallback 路径**静默绕过跨进程同步** —— guarded_live 下任意进程的 trial_guard
或 derivatives_live_guard 触发 halt 时如果 sync_service 注入失败，其他 3 进程
会继续接单直到下一次 bootstrap 才从 Redis 看到 halt 状态。这是 W1-W5 共有的
资金安全 bug 类。

Stage 6 Slice 6.4 把两个类合并成一个：本地 cache + Redis + NATS 三层一体，
sync 写入路径自动调度到主 loop 完成跨进程广播，写入点不再需要 if/else fallback。

API 设计
========
零参构造
    ``KillSwitch()`` —— 纯本地数据持有者，halt/resume 立即生效，不写 Redis、
    不发 NATS。测试与启动期早于 bus connect 的代码路径走这条。

配线 sidecar
    ``await ks.bootstrap(hot_state_store=..., bus=..., process_role=..., logger=...)``
    一次性挂上 Redis + NATS + 主 loop。bootstrap 会从 Redis 拉一次最新 halt 状态
    并订阅 ``system.kill_switch_state``。

sync 写入（向后兼容老 API）
    ``ks.halt(reason)`` / ``ks.resume()`` —— 本地立即生效；如果已 bootstrap，
    自动 fire-and-forget 一个跨进程广播任务（主 loop 线程内）或 ``run_coroutine
    _threadsafe`` 投递（worker thread 内，等待 ≤ 2s）。**永不抛异常**。

async 写入（FastAPI handler 路径）
    ``await ks.halt_async(reason)`` / ``await ks.resume_async()`` —— 本地立即
    生效，await 完成后 Redis + NATS 都已尝试写入（best-effort）。

读路径不变
    ``ks.halted`` / ``ks.status()`` —— 与 Stage 6 Slice 6.2 之前的 ``KillSwitch``
    完全等价，~50 个 sync 读路径无需修改。

不变量
======
I1 任何 halt/resume 都立即生效在本地（sync read 永不落后于本进程的 sync write）
I2 ≤1s 内被另外 3 进程的本地 cache 看到（NATS 广播）
I3 进程崩溃 + restart 之后能恢复上一次 halt 状态（bootstrap 从 Redis 读）
I4 Redis 不可达不影响本进程的 halt 生效（best-effort 写）
I5 NATS 不可达不影响本进程的 halt 生效（best-effort 写）
I6 乱序的 NATS 事件不会让本地 cache 退到旧状态（set_at_ts 排序）
I7 W1-W5 写入点不再需要 if/else fallback：``ks.halt(reason)`` 自动处理 sidecar
   未配线 / 已配线 两种状态
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import time
from typing import Any

from aats.bootstrap.logging import log_event
from aats.bus.base import EventBus
from aats.events import topics
from aats.events.envelopes import parse_envelope
from aats.schemas.common import EventEnvelope, dump_payload_exact
from aats.storage.hot_state_store import HotStateStore, NS_SYSTEM, make_key

KILL_SWITCH_REDIS_KEY = make_key(NS_SYSTEM, "kill_switch")
"""Redis key for the kill_switch state. ``aats:hot:system:kill_switch``."""

# Redis TTL（秒）。kill_switch 状态需跨重启持久化（I3），但不应永驻 Redis：
# 如果系统停止运行超过 30 天，旧 halt 状态应过期，重启时走 fail-safe halt
# 路径（Redis 空 + 多进程模式 → 保守 halt → 等 NATS 或人工恢复）。
# 30 天远长于任何正常维护窗口，足够安全。
_KILL_SWITCH_REDIS_TTL_SECONDS: int = 30 * 24 * 3600  # 30 days

# bootstrap 时 Redis 数据新鲜度阈值（秒）。超过此阈值的 halt 状态仍会被
# 恢复（保守策略），但会 log warning 提醒运维检查。
_KILL_SWITCH_STALENESS_THRESHOLD_SECONDS: float = 48 * 3600  # 48 hours

KILL_SWITCH_EVENT_TYPE = "KillSwitchStateChanged"
"""Event envelope ``event_type`` field for kill_switch state broadcasts."""

KILL_SWITCH_SOURCE_COMPONENT = "aats.governance.kill_switch"
"""Event envelope ``source_component`` for kill_switch state broadcasts."""


class KillSwitch:
    """Thread-safe halt/resume switch + 跨进程同步边车（合并版）。

    Uses a single tuple assignment for atomicity — Python guarantees that
    binding a name to a new object is atomic at the bytecode level, so a
    concurrent reader of ``status()`` / ``halted`` will always see a consistent
    (halted, reason) pair.

    sidecar 字段（``_hot_state_store`` / ``_bus`` / ``_loop`` 等）只在 ``bootstrap``
    时被设置一次，之后只读；不需要锁。``_state`` 是唯一被频繁更新的字段，靠 tuple
    赋值原子性保证读侧一致性。
    """

    def __init__(self) -> None:
        # === 本地 state（始终可用，无需 bootstrap）===
        self._state: tuple[bool, str | None] = (False, None)

        # === sidecar 配线状态（bootstrap 后才有效）===
        self._hot_state_store: HotStateStore | None = None
        self._bus: EventBus | None = None
        self._process_role: str = "monolith"
        self._logger: logging.Logger | None = None
        # 主 loop 引用，bootstrap 时缓存。worker thread 用 run_coroutine_threadsafe 投递
        self._loop: asyncio.AbstractEventLoop | None = None
        # 本地"已经应用过的最大 set_at_ts"。乱序 NATS 事件用这个去重 + 拒绝退化
        self._last_applied_ts: float = 0.0
        # 写入去重：同一 (halted, reason) 不重复广播
        self._last_published_state: tuple[bool, str | None] | None = None
        # bootstrap 是否已经成功跑过
        self._bootstrapped: bool = False
        # NATS 订阅是否成功
        self._subscribed: bool = False

    # ──────────────────────────────────────────────────────────────────
    # 本地读路径（永远可用，与 Stage 6 Slice 6.2 之前完全兼容）
    # ──────────────────────────────────────────────────────────────────

    def status(self) -> dict[str, str | bool | None]:
        halted, reason = self._state
        return {"halted": halted, "reason": reason}

    @property
    def halted(self) -> bool:
        return self._state[0]

    # ──────────────────────────────────────────────────────────────────
    # sync 写路径（向后兼容老 API + 自动跨进程同步）
    # ──────────────────────────────────────────────────────────────────

    def halt(self, reason: str = "manual_halt") -> None:
        """Sync halt：本地立即生效 + 自动 best-effort 跨进程广播。

        语义：
        - 本地 cache **永远** 立即更新（步骤 1）
        - 如果未 bootstrap（``_bus`` / ``_loop`` is None） → 步骤 2 跳过，纯本地模式
        - 如果 bootstrap 已完成且当前在主 loop 线程 → ``loop.create_task`` 异步广播
        - 如果 bootstrap 已完成但当前在 worker thread → ``run_coroutine_threadsafe``
          投递并等待 ≤ 2s（与 Stage 6 Slice 6.2 ``halt_threadsafe`` 行为对齐）
        - 如果 bootstrap 完成但 loop 已停止 → 步骤 2 跳过，本地仍然 halt

        ⚠️ 永不抛异常：worker thread 上游可能误把 halt 失败当成"halt 没生效，继续下单"。
        本地 cache always wins。
        """
        # Step 1: 本地立即生效（无锁，tuple 赋值原子）
        self._state = (True, reason)
        # Step 2: 跨进程广播（best-effort）
        self._dispatch_async_publish(halted=True, reason=reason)

    def resume(self) -> None:
        """Sync resume：与 ``halt`` 对称。"""
        self._state = (False, None)
        self._dispatch_async_publish(halted=False, reason=None)

    # ──────────────────────────────────────────────────────────────────
    # async 写路径（FastAPI handler / 主 loop 内 await 用）
    # ──────────────────────────────────────────────────────────────────

    async def halt_async(self, reason: str = "manual_halt") -> None:
        """Async halt：本地立即生效，await 完成后 Redis + NATS 已尝试写入。

        与 ``halt()`` 的区别：调用方可以 ``await`` 等到广播完成（或 best-effort 失败
        被记录），适合 FastAPI handler 等需要确定写入时序的路径。
        """
        self._state = (True, reason)
        if self._bus is None:
            return
        set_at_ts = time.time()
        self._last_applied_ts = max(self._last_applied_ts, set_at_ts)
        await self._publish(halted=True, reason=reason, set_at_ts=set_at_ts)

    async def resume_async(self) -> None:
        """Async resume：与 ``halt_async`` 对称。"""
        self._state = (False, None)
        if self._bus is None:
            return
        set_at_ts = time.time()
        self._last_applied_ts = max(self._last_applied_ts, set_at_ts)
        await self._publish(halted=False, reason=None, set_at_ts=set_at_ts)

    # ──────────────────────────────────────────────────────────────────
    # 启动 / 关闭
    # ──────────────────────────────────────────────────────────────────

    async def bootstrap(
        self,
        *,
        hot_state_store: HotStateStore,
        bus: EventBus,
        process_role: str,
        logger: logging.Logger,
    ) -> None:
        """启动期 hydration：

        1. 缓存 sidecar deps（``_hot_state_store`` / ``_bus`` / ``_process_role`` /
           ``_logger`` / ``_loop``）
        2. 从 Redis 读 ``aats:hot:system:kill_switch``
        3. 如果存在且 ``halted=True``，更新本地 ``_state``
        4. 订阅 NATS ``system.kill_switch_state`` topic

        ⚠️ 任何步骤的失败都不能阻止 build_runtime 完成。
        """
        self._hot_state_store = hot_state_store
        self._bus = bus
        self._process_role = process_role
        self._logger = logger
        self._loop = asyncio.get_running_loop()

        # Step 2：从 Redis 读
        _redis_read_failed = False
        try:
            stored: Any = await hot_state_store.get(KILL_SWITCH_REDIS_KEY)
        except Exception as exc:
            log_event(
                logger,
                "kill_switch_bootstrap_redis_failed",
                level="warning",
                process_role=process_role,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            stored = None
            _redis_read_failed = True

        # ── Fail-safe：Redis 不可用时在多进程模式下默认 halt ──────────
        # 4 进程拓扑下 kill_switch 的跨进程同步依赖 Redis。如果 bootstrap
        # 时 Redis 不可达，我们无法确定系统是否已被 halt——此时继续交易可能
        # 造成资金损失。保守策略：默认 halt，等 NATS 订阅（step 4）收到
        # 最新事件后自动修正（NATS DeliverLast 会推送最后一条状态变更）。
        #
        # monolith 模式无需跨进程同步，不受此影响。
        if _redis_read_failed and process_role not in (None, "monolith"):
            self._state = (True, "redis_unavailable_fail_safe")
            log_event(
                logger,
                "kill_switch_bootstrap_fail_safe_halt",
                level="error",
                process_role=process_role,
                reason="redis_unavailable_fail_safe",
                hint="系统将保持 halt 直到 Redis 恢复或 NATS 推送最新状态",
            )

        if isinstance(stored, dict):
            try:
                halted = bool(stored.get("halted", False))
                reason = stored.get("reason")
                set_at_ts = float(stored.get("set_at_ts", 0.0))
                source_role = stored.get("source_role")
                if halted:
                    self._state = (True, str(reason or "bootstrap_from_redis"))
                self._last_applied_ts = set_at_ts

                # 新鲜度检查：数据超过阈值时 log warning 提醒运维。
                # 仍然正常 hydrate（保守：宁可被旧 halt 卡住也不漏放），
                # 但运维应检查是否需要手动 resume。
                if set_at_ts > 0:
                    age_seconds = time.time() - set_at_ts
                    if age_seconds > _KILL_SWITCH_STALENESS_THRESHOLD_SECONDS:
                        log_event(
                            logger,
                            "kill_switch_bootstrap_stale_state",
                            level="warning",
                            process_role=process_role,
                            halted=halted,
                            reason=reason,
                            set_at_ts=set_at_ts,
                            age_hours=round(age_seconds / 3600, 1),
                            threshold_hours=round(
                                _KILL_SWITCH_STALENESS_THRESHOLD_SECONDS / 3600, 1,
                            ),
                            hint="Redis 中的 kill_switch 状态超过新鲜度阈值，请检查是否需要手动 resume",
                        )

                log_event(
                    logger,
                    "kill_switch_bootstrap_hydrated",
                    process_role=process_role,
                    halted=halted,
                    reason=reason,
                    set_at_ts=set_at_ts,
                    source_role=source_role,
                )
            except Exception as exc:
                log_event(
                    logger,
                    "kill_switch_bootstrap_parse_failed",
                    level="warning",
                    process_role=process_role,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
        else:
            log_event(
                logger,
                "kill_switch_bootstrap_empty",
                process_role=process_role,
            )

        # Step 4：订阅 NATS（即便上面失败也要订阅，订阅失败也不抛）
        try:
            await bus.subscribe(topics.KILL_SWITCH_STATE, self._handle_remote_event)
            self._subscribed = True
            log_event(
                logger,
                "kill_switch_subscribed",
                process_role=process_role,
                topic=topics.KILL_SWITCH_STATE,
            )
        except Exception as exc:
            log_event(
                logger,
                "kill_switch_subscribe_failed",
                level="warning",
                process_role=process_role,
                error_type=type(exc).__name__,
                error=str(exc),
            )

        self._bootstrapped = True

    async def stop(self) -> None:
        """关闭期清理：当前 EventBus 抽象不支持 unsubscribe，所以我们只标记状态。

        ⚠️ 不要在 stop 里写 Redis：关闭不代表 resume，下次启动应该读到上一次的 halt
        状态。
        """
        if self._logger is not None:
            log_event(
                self._logger,
                "kill_switch_stopped",
                process_role=self._process_role,
                subscribed=self._subscribed,
                last_applied_ts=self._last_applied_ts,
            )
        self._loop = None

    # ──────────────────────────────────────────────────────────────────
    # 内部：sync 写路径的异步分发
    # ──────────────────────────────────────────────────────────────────

    def _dispatch_async_publish(self, *, halted: bool, reason: str | None) -> None:
        """从 sync 路径调度跨进程广播。

        三种执行路径：
        1. 未 bootstrap（``_bus`` / ``_loop`` is None）→ 直接 return（纯本地模式）
        2. 在主 loop 线程 → ``loop.create_task`` fire-and-forget
        3. 在 worker thread → ``run_coroutine_threadsafe`` 投递并等待 ≤ 2s

        永不抛异常。
        """
        bus = self._bus
        loop = self._loop
        if bus is None or loop is None or loop.is_closed():
            return
        if not loop.is_running():
            return

        set_at_ts = time.time()
        # 推进 last_applied_ts，避免随后收到自己广播的事件被错认为"更新"
        self._last_applied_ts = max(self._last_applied_ts, set_at_ts)

        coro = self._publish(halted=halted, reason=reason, set_at_ts=set_at_ts)

        # 判断当前线程是否为主 loop 线程
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        if running is loop:
            # 主 loop 线程内：fire-and-forget
            try:
                loop.create_task(coro, name="kill_switch_publish")
            except Exception as exc:  # pragma: no cover
                if self._logger is not None:
                    log_event(
                        self._logger,
                        "kill_switch_dispatch_create_task_failed",
                        level="warning",
                        process_role=self._process_role,
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
                coro.close()
            return

        # 非 loop 线程：投递到主 loop 并等待
        try:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError as exc:
            if self._logger is not None:
                log_event(
                    self._logger,
                    "kill_switch_dispatch_submit_failed",
                    level="warning",
                    process_role=self._process_role,
                    halted=halted,
                    reason=reason,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
            coro.close()
            return
        try:
            future.result(timeout=2.0)
        except concurrent.futures.TimeoutError:
            if self._logger is not None:
                log_event(
                    self._logger,
                    "kill_switch_dispatch_timeout",
                    level="warning",
                    process_role=self._process_role,
                    halted=halted,
                    reason=reason,
                    timeout=2.0,
                )
        except Exception as exc:
            if self._logger is not None:
                log_event(
                    self._logger,
                    "kill_switch_dispatch_partial",
                    level="warning",
                    process_role=self._process_role,
                    halted=halted,
                    reason=reason,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )

    # ──────────────────────────────────────────────────────────────────
    # 内部：跨进程广播主体（async）
    # ──────────────────────────────────────────────────────────────────

    async def _publish(
        self,
        *,
        halted: bool,
        reason: str | None,
        set_at_ts: float,
    ) -> None:
        """Best-effort Redis SET + NATS publish。

        去重：相同 (halted, reason) 与上次广播相同时跳过 Redis + NATS（trial_guard
        抖动场景下避免广播 storm）。本地 ``_state`` 已经在 caller 里更新过，dedup
        只影响是否再发一次广播。
        """
        new_state: tuple[bool, str | None] = (halted, reason if halted else None)
        if self._last_published_state == new_state:
            if self._logger is not None:
                log_event(
                    self._logger,
                    "kill_switch_publish_skipped_dedup",
                    process_role=self._process_role,
                    halted=halted,
                    reason=reason,
                )
            return
        self._last_published_state = new_state

        payload: dict[str, Any] = {
            "halted": halted,
            "reason": reason if halted else None,
            "set_at_ts": set_at_ts,
            "source_role": self._process_role,
        }

        await self._best_effort_redis_set(payload)
        await self._best_effort_nats_broadcast(payload)

        if self._logger is not None:
            log_event(
                self._logger,
                "kill_switch_published",
                process_role=self._process_role,
                halted=halted,
                reason=reason,
                set_at_ts=set_at_ts,
            )

    async def _best_effort_redis_set(self, payload: dict[str, Any]) -> None:
        if self._hot_state_store is None:
            return
        try:
            await self._hot_state_store.set(
                KILL_SWITCH_REDIS_KEY,
                payload,
                ttl_seconds=_KILL_SWITCH_REDIS_TTL_SECONDS,
            )
        except Exception as exc:
            if self._logger is not None:
                log_event(
                    self._logger,
                    "kill_switch_redis_set_failed",
                    level="warning",
                    process_role=self._process_role,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )

    async def _best_effort_nats_broadcast(self, payload: dict[str, Any]) -> None:
        if self._bus is None:
            return
        try:
            envelope = EventEnvelope(
                event_type=KILL_SWITCH_EVENT_TYPE,
                source_component=KILL_SWITCH_SOURCE_COMPONENT,
                topic=topics.KILL_SWITCH_STATE,
                key=self._process_role,
                payload=dump_payload_exact(payload),
            )
            await self._bus.publish(
                topic=topics.KILL_SWITCH_STATE,
                key=self._process_role,
                payload=envelope.model_dump(mode="json"),
            )
        except Exception as exc:
            if self._logger is not None:
                log_event(
                    self._logger,
                    "kill_switch_nats_publish_failed",
                    level="warning",
                    process_role=self._process_role,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )

    # ──────────────────────────────────────────────────────────────────
    # NATS 远端事件接收
    # ──────────────────────────────────────────────────────────────────

    async def _handle_remote_event(self, message: dict[str, Any]) -> None:
        """订阅 ``system.kill_switch_state`` 后的回调。

        - 校验 ``set_at_ts > self._last_applied_ts``，旧事件忽略（I6）
        - 同一 set_at_ts 去重（idempotent）
        - 来自自己进程的事件忽略（避免回环改本地 cache）
        - apply 失败不抛（订阅 handler 异常会让 NATS 客户端 nak / log）
        """
        try:
            envelope = parse_envelope(message)
            payload = envelope.payload or {}
            set_at_ts = float(payload.get("set_at_ts", 0.0))
            halted = bool(payload.get("halted", False))
            reason = payload.get("reason")
            source_role = payload.get("source_role")
        except Exception as exc:
            if self._logger is not None:
                log_event(
                    self._logger,
                    "kill_switch_remote_parse_failed",
                    level="warning",
                    process_role=self._process_role,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
            return

        # 自己广播的回环事件：本地早已应用，跳过
        if source_role == self._process_role:
            return

        # set_at_ts 单调性：旧事件不允许退化本地 cache
        if set_at_ts <= self._last_applied_ts:
            if self._logger is not None:
                log_event(
                    self._logger,
                    "kill_switch_remote_skipped_stale",
                    process_role=self._process_role,
                    set_at_ts=set_at_ts,
                    last_applied_ts=self._last_applied_ts,
                    source_role=source_role,
                )
            return

        # apply 到本地
        if halted:
            self._state = (True, str(reason or "remote_halt"))
        else:
            self._state = (False, None)
        self._last_applied_ts = set_at_ts
        # 同步去重 marker：远端最新状态等同于本地最近一次广播状态，避免下次本进程
        # 写时被错误去重
        self._last_published_state = (halted, reason if halted else None)

        if self._logger is not None:
            log_event(
                self._logger,
                "kill_switch_remote_applied",
                process_role=self._process_role,
                halted=halted,
                reason=reason,
                set_at_ts=set_at_ts,
                source_role=source_role,
            )

    # ──────────────────────────────────────────────────────────────────
    # 诊断 / 内省
    # ──────────────────────────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        """启动日志 / dashboard 用的内省 dict。"""
        halted, reason = self._state
        return {
            "process_role": self._process_role,
            "bootstrapped": self._bootstrapped,
            "subscribed": self._subscribed,
            "last_applied_ts": self._last_applied_ts,
            "kill_switch": {"halted": halted, "reason": reason},
        }
