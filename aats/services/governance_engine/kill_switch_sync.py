"""Stage 6 Slice 6.2：跨进程 kill_switch 同步服务。

设计文档
========
docs/task/stage_6_slice_6_2_kill_switch_design.md

核心问题
========
4 进程拓扑下，每个进程在 ``_build_shared_slice`` 阶段独立构造一个 ``KillSwitch``
实例，纯内存。任意进程的 halt 都不会传播到其他进程；崩溃 + restart 后状态丢失。
这是真实的资金安全缺陷，必须在实盘前修掉。

三层架构
========
``KillSwitch``（已存在，sync）：
    所有 ~30 个 sync 读路径（订单 pre-submit / health check / blocker 渲染等）
    直接打这里。零网络、零阻塞、永远是本进程的"快路径真相"。

``KillSwitchSyncService``（本模块，新增）：
    持有 ``KillSwitch`` + ``HotStateStore`` + ``EventBus`` + ``process_role``。
    把 4 个进程的本地真相收敛到同一个 Redis 状态机的"边车"。

源真相：
    Redis ``aats:hot:system:kill_switch`` —— 持久化跨重启
    NATS ``system.kill_switch_state`` —— 跨进程实时广播

不变量
======
I1 任何 halt/resume 都立即生效在本地 (sync read 永不落后于本进程的 sync write)
I2 ≤1s 内被另外 3 进程的本地 cache 看到（NATS 广播）
I3 进程崩溃 + restart 之后能恢复上一次 halt 状态（bootstrap 从 Redis 读）
I4 Redis 不可达不影响本进程的 halt 生效（best-effort 写）
I5 NATS 不可达不影响本进程的 halt 生效（best-effort 写）
I6 乱序的 NATS 事件不会让本地 cache 退到旧状态（set_at_ts 排序）
I7 测试调 ``kill_switch.halt()`` 直接路径不破（KillSwitch API 不变）
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
from aats.services.governance_engine.kill_switch import KillSwitch
from aats.storage.hot_state_store import HotStateStore, NS_SYSTEM, make_key

KILL_SWITCH_REDIS_KEY = make_key(NS_SYSTEM, "kill_switch")
"""Redis key for the kill_switch state. ``aats:hot:system:kill_switch``."""

KILL_SWITCH_EVENT_TYPE = "KillSwitchStateChanged"
"""Event envelope ``event_type`` field for kill_switch state broadcasts."""

KILL_SWITCH_SOURCE_COMPONENT = "aats.governance.kill_switch_sync"
"""Event envelope ``source_component`` for kill_switch state broadcasts."""


class KillSwitchSyncService:
    """Sidecar that synchronises a local ``KillSwitch`` to Redis + NATS.

    Sync 写路径（``halt_threadsafe`` / ``resume_threadsafe``）从 worker thread 调，
    用 ``asyncio.run_coroutine_threadsafe`` 投递到主 loop。本地 cache 永远是第一步，
    Redis / NATS 是 best-effort 步骤——业务安全永不被写失败破坏。
    """

    def __init__(
        self,
        *,
        kill_switch: KillSwitch,
        hot_state_store: HotStateStore,
        bus: EventBus,
        process_role: str,
        logger: logging.Logger,
    ) -> None:
        self._kill_switch = kill_switch
        self._hot_state_store = hot_state_store
        self._bus = bus
        self._process_role = process_role
        self._logger = logger
        # 主 loop 引用，bootstrap 时缓存。worker thread 用 run_coroutine_threadsafe 投递
        self._loop: asyncio.AbstractEventLoop | None = None
        # 本地"已经应用过的最大 set_at_ts"。乱序 NATS 事件用这个去重 + 拒绝退化
        self._last_applied_ts: float = 0.0
        # 写入去重：同一 (halted, reason) 不重复广播
        self._last_published_state: tuple[bool, str | None] | None = None
        # bootstrap 是否已经成功跑过
        self._bootstrapped: bool = False
        # 订阅 handler 引用（unsubscribe 路径用，部分 bus 实现不支持 unsubscribe，
        # 我们记下来便于诊断）
        self._subscribed: bool = False

    # ──────────────────────────────────────────────────────────────────
    # 启动 / 关闭
    # ──────────────────────────────────────────────────────────────────

    async def bootstrap(self) -> None:
        """启动期 hydration：

        1. 从 Redis 读 ``aats:hot:system:kill_switch``
        2. 如果存在且 ``halted=True``，调本地 ``self._kill_switch.halt(...)``
        3. 如果不存在，**不动**本地 cache（避免冷启动两个进程互相覆盖）
        4. 订阅 NATS ``system.kill_switch_state`` topic

        ⚠️ 任何步骤的失败都不能阻止 build_runtime 完成。
        """
        self._loop = asyncio.get_running_loop()

        # Step 1：从 Redis 读
        try:
            stored: Any = await self._hot_state_store.get(KILL_SWITCH_REDIS_KEY)
        except Exception as exc:
            log_event(
                self._logger,
                "kill_switch_sync_bootstrap_redis_failed",
                level="warning",
                process_role=self._process_role,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            stored = None

        if isinstance(stored, dict):
            # Step 2：hydrate 本地
            try:
                halted = bool(stored.get("halted", False))
                reason = stored.get("reason")
                set_at_ts = float(stored.get("set_at_ts", 0.0))
                source_role = stored.get("source_role")
                if halted:
                    self._kill_switch.halt(reason=str(reason or "bootstrap_from_redis"))
                self._last_applied_ts = set_at_ts
                log_event(
                    self._logger,
                    "kill_switch_sync_bootstrap_hydrated",
                    process_role=self._process_role,
                    halted=halted,
                    reason=reason,
                    set_at_ts=set_at_ts,
                    source_role=source_role,
                )
            except Exception as exc:
                log_event(
                    self._logger,
                    "kill_switch_sync_bootstrap_parse_failed",
                    level="warning",
                    process_role=self._process_role,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
        else:
            log_event(
                self._logger,
                "kill_switch_sync_bootstrap_empty",
                process_role=self._process_role,
            )

        # Step 4：订阅 NATS（即便上面失败也要订阅，订阅失败也不抛）
        try:
            await self._bus.subscribe(topics.KILL_SWITCH_STATE, self._handle_remote_event)
            self._subscribed = True
            log_event(
                self._logger,
                "kill_switch_sync_subscribed",
                process_role=self._process_role,
                topic=topics.KILL_SWITCH_STATE,
            )
        except Exception as exc:
            log_event(
                self._logger,
                "kill_switch_sync_subscribe_failed",
                level="warning",
                process_role=self._process_role,
                error_type=type(exc).__name__,
                error=str(exc),
            )

        self._bootstrapped = True

    async def stop(self) -> None:
        """关闭期清理：当前 EventBus 抽象不支持 unsubscribe，所以我们只标记状态。

        ⚠️ 不要在 stop 里写 Redis：关闭不代表 resume，下次启动应该读到上一次的 halt
        状态。
        """
        log_event(
            self._logger,
            "kill_switch_sync_stopped",
            process_role=self._process_role,
            subscribed=self._subscribed,
            last_applied_ts=self._last_applied_ts,
        )
        self._loop = None

    # ──────────────────────────────────────────────────────────────────
    # async 写路径（FastAPI handler 链路用）
    # ──────────────────────────────────────────────────────────────────

    async def halt(self, reason: str) -> None:
        """async halt：本地 → Redis → NATS。

        失败语义：
        - 本地 cache 永不失败（步骤 1 是 sync 赋值）
        - Redis 写失败 → warning，继续 NATS（其他进程仍能收到）
        - NATS 写失败 → warning，结束（其他进程要等到自己下次 bootstrap 才看到）

        去重：当前 (halted, reason) 与上次广播相同时跳过 Redis + NATS（trial_guard
        抖动场景下避免广播 storm）。本地 ``halt()`` 仍然每次都跑（赋值是 idempotent
        + cheap）。
        """
        set_at_ts = time.time()
        # Step 1：本地 sync（永不失败）
        self._kill_switch.halt(reason=reason)
        new_state = (True, reason)
        # 写入去重
        if self._last_published_state == new_state:
            log_event(
                self._logger,
                "kill_switch_sync_halt_skipped_dedup",
                process_role=self._process_role,
                reason=reason,
            )
            return
        self._last_published_state = new_state
        # 本地 apply ts 推进，避免随后收到自己广播的事件被错认为"更新"
        self._last_applied_ts = max(self._last_applied_ts, set_at_ts)
        payload = {
            "halted": True,
            "reason": reason,
            "set_at_ts": set_at_ts,
            "source_role": self._process_role,
        }
        await self._best_effort_redis_set(payload)
        await self._best_effort_nats_broadcast(payload)
        log_event(
            self._logger,
            "kill_switch_sync_halt_applied",
            process_role=self._process_role,
            reason=reason,
            set_at_ts=set_at_ts,
        )

    async def resume(self) -> None:
        """async resume：与 halt 对称。同样去重 + best-effort 写。"""
        set_at_ts = time.time()
        self._kill_switch.resume()
        new_state: tuple[bool, str | None] = (False, None)
        if self._last_published_state == new_state:
            log_event(
                self._logger,
                "kill_switch_sync_resume_skipped_dedup",
                process_role=self._process_role,
            )
            return
        self._last_published_state = new_state
        self._last_applied_ts = max(self._last_applied_ts, set_at_ts)
        payload = {
            "halted": False,
            "reason": None,
            "set_at_ts": set_at_ts,
            "source_role": self._process_role,
        }
        await self._best_effort_redis_set(payload)
        await self._best_effort_nats_broadcast(payload)
        log_event(
            self._logger,
            "kill_switch_sync_resume_applied",
            process_role=self._process_role,
            set_at_ts=set_at_ts,
        )

    # ──────────────────────────────────────────────────────────────────
    # sync 写路径（worker thread / 启动期 sync 调用用）
    # ──────────────────────────────────────────────────────────────────

    def halt_threadsafe(self, reason: str, *, timeout: float = 2.0) -> None:
        """从非 asyncio 上下文（worker thread / 启动期 sync 调用）调 halt。

        - 主 loop 可用 → ``run_coroutine_threadsafe`` 投递到主 loop，等 ``timeout`` 秒
        - 主 loop 不可用（测试 / bootstrap 之前）→ 直接调 ``self._kill_switch.halt()``
          fall back，**保证本地 cache 立即更新**
        - 投递后超时或异常 → log warning，本地 cache 仍然 halt（步骤 1 已完成）

        ⚠️ 永不抛异常：worker thread 上游可能误把 halt 失败当成"halt 没生效，继续下单"。
        本地 cache always wins。
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            # 测试 / bootstrap 之前：退化到 sync local-only
            self._kill_switch.halt(reason=reason)
            log_event(
                self._logger,
                "kill_switch_sync_halt_threadsafe_local_only",
                level="warning",
                process_role=self._process_role,
                reason=reason,
            )
            return
        # 即便 run_coroutine_threadsafe 投递成功，本地也已经在 self.halt() 里第一步
        # 更新过；这里多一次本地 sync 是为了在投递排队期间也立刻可见
        self._kill_switch.halt(reason=reason)
        try:
            future = asyncio.run_coroutine_threadsafe(self.halt(reason=reason), loop)
        except RuntimeError as exc:
            # loop 在投递瞬间被关闭
            log_event(
                self._logger,
                "kill_switch_sync_halt_threadsafe_submit_failed",
                level="warning",
                process_role=self._process_role,
                reason=reason,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return
        try:
            future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            log_event(
                self._logger,
                "kill_switch_sync_halt_threadsafe_timeout",
                level="warning",
                process_role=self._process_role,
                reason=reason,
                timeout=timeout,
            )
        except Exception as exc:
            log_event(
                self._logger,
                "kill_switch_sync_halt_threadsafe_partial",
                level="warning",
                process_role=self._process_role,
                reason=reason,
                error_type=type(exc).__name__,
                error=str(exc),
            )

    def resume_threadsafe(self, *, timeout: float = 2.0) -> None:
        """``halt_threadsafe`` 的对称版本。"""
        loop = self._loop
        if loop is None or loop.is_closed():
            self._kill_switch.resume()
            log_event(
                self._logger,
                "kill_switch_sync_resume_threadsafe_local_only",
                level="warning",
                process_role=self._process_role,
            )
            return
        self._kill_switch.resume()
        try:
            future = asyncio.run_coroutine_threadsafe(self.resume(), loop)
        except RuntimeError as exc:
            log_event(
                self._logger,
                "kill_switch_sync_resume_threadsafe_submit_failed",
                level="warning",
                process_role=self._process_role,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return
        try:
            future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            log_event(
                self._logger,
                "kill_switch_sync_resume_threadsafe_timeout",
                level="warning",
                process_role=self._process_role,
                timeout=timeout,
            )
        except Exception as exc:
            log_event(
                self._logger,
                "kill_switch_sync_resume_threadsafe_partial",
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
            log_event(
                self._logger,
                "kill_switch_sync_remote_parse_failed",
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
            log_event(
                self._logger,
                "kill_switch_sync_remote_skipped_stale",
                process_role=self._process_role,
                set_at_ts=set_at_ts,
                last_applied_ts=self._last_applied_ts,
                source_role=source_role,
            )
            return

        # apply 到本地
        if halted:
            self._kill_switch.halt(reason=str(reason or "remote_halt"))
        else:
            self._kill_switch.resume()
        self._last_applied_ts = set_at_ts
        # 同步去重 marker：远端最新状态等同于本地最近一次广播状态，避免下次本进程
        # 写时被错误去重，要把 marker 重置为新的状态
        self._last_published_state = (halted, reason if halted else None)

        log_event(
            self._logger,
            "kill_switch_sync_remote_applied",
            process_role=self._process_role,
            halted=halted,
            reason=reason,
            set_at_ts=set_at_ts,
            source_role=source_role,
        )

    # ──────────────────────────────────────────────────────────────────
    # 内部 best-effort I/O
    # ──────────────────────────────────────────────────────────────────

    async def _best_effort_redis_set(self, payload: dict[str, Any]) -> None:
        try:
            await self._hot_state_store.set(KILL_SWITCH_REDIS_KEY, payload)
        except Exception as exc:
            log_event(
                self._logger,
                "kill_switch_sync_redis_set_failed",
                level="warning",
                process_role=self._process_role,
                error_type=type(exc).__name__,
                error=str(exc),
            )

    async def _best_effort_nats_broadcast(self, payload: dict[str, Any]) -> None:
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
            log_event(
                self._logger,
                "kill_switch_sync_nats_publish_failed",
                level="warning",
                process_role=self._process_role,
                error_type=type(exc).__name__,
                error=str(exc),
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
            "last_applied_ts": self._last_applied_ts,
            "kill_switch": self._kill_switch.status(),
        }
