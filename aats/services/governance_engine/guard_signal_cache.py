"""Finding 3: decision 侧 guard signal 跨进程缓存。

四进程架构下 DerivativesLiveGuardService / ForwardTrialGuardService /
RecoveryPostureEvaluator 只在 execution 进程运行（_slice_active("startup_recovery")
门禁），而 RiskEngine 在 decision 进程需要这些信号来做风控决策。

本模块提供 ``GuardSignalHotStateCache``：
  - **Execution 侧（publisher）**：guard service evaluate_now() 后调用
    ``publish(snapshot)``，写 local dict + Redis (best-effort) + NATS (best-effort)
  - **Decision 侧（reader）**：bootstrap 时从 Redis 恢复快照，订阅 NATS 实时
    更新到 local dict；RiskEngine 同步调用 ``snapshot()`` 读 local dict

设计原则（与 ObligationHotStateCache 一致）：
  - Local dict 是同步读源（RiskEngine.evaluate 是同步调用）
  - Redis 用于持久化 + 跨进程共享
  - NATS 用于亚秒级实时广播
  - **Fail-closed**：快照过期 → 返回空 dict → RiskEngine 视为无 provider
    → 回退到保守模式（不开新仓、只允许减仓）

Redis key 格式：``aats:hot:system:guard_signal:<signal_name>``
NATS topic：``system.guard_signal_updates``，key 为 signal_name

signal_name 约定：
  - ``derivatives_live`` — DerivativesLiveGuardService.snapshot()
  - ``trial`` — ForwardTrialGuardService.snapshot()
  - ``recovery`` — RecoveryPostureEvaluator.finalize_status()
"""
from __future__ import annotations

import time
from typing import Any

from aats.bootstrap.logging import log_event
from aats.events import topics
from aats.storage.hot_state_store import NS_SYSTEM, HotStateStore, make_key


# ─────────────────────────────────────────────────────────────────────
# 常量
# ─────────────────────────────────────────────────────────────────────

# 默认 stale 阈值 120 秒。guard 评估周期约 5-15 秒；120 秒足以覆盖
# 短暂网络抖动和进程重启，又不会让 decision 侧长时间持有过期信号。
_DEFAULT_STALE_THRESHOLD_SECONDS = 120.0

# Redis TTL = stale_threshold * 3，确保 Redis 数据在 decision 侧
# stale check 失败后仍可用于进程重启恢复。
_REDIS_TTL_MULTIPLIER = 3.0


class GuardSignalHotStateCache:
    """Cross-process guard signal snapshot cache.

    提供两种接口满足 RiskEngine 注入需求：
      - ``snapshot()`` → dict — live_runtime_guard_provider / trial_guard_provider
      - ``__call__()`` → dict — recovery_status_provider (Callable)

    构造后必须调用 ``bootstrap()``；未 bootstrap 时 ``snapshot()`` 返回空 dict。
    """

    def __init__(
        self,
        *,
        signal_name: str,
        logger: Any,
        stale_threshold_seconds: float = _DEFAULT_STALE_THRESHOLD_SECONDS,
    ) -> None:
        self._signal_name = signal_name
        self._logger = logger
        self._stale_threshold = stale_threshold_seconds
        self._hot_state_store: HotStateStore | None = None
        self._bus: Any | None = None  # EventBus
        self._process_role: str = "monolith"
        self._latest: dict[str, Any] = {}
        self._last_updated_at: float = 0.0
        self._bootstrapped: bool = False
        self._subscribed: bool = False

    @property
    def redis_key(self) -> str:
        return make_key(NS_SYSTEM, "guard_signal", self._signal_name)

    @property
    def signal_name(self) -> str:
        return self._signal_name

    @property
    def bootstrapped(self) -> bool:
        return self._bootstrapped

    # ── 生命周期 ──

    async def bootstrap(
        self,
        *,
        hot_state_store: HotStateStore | None = None,
        bus: Any | None = None,
        process_role: str = "monolith",
        subscribe: bool = False,
    ) -> None:
        """初始化缓存。

        Args:
            hot_state_store: Redis 后端（None 则退化为纯内存）
            bus: EventBus 用于 NATS 广播/订阅
            process_role: 当前进程角色（日志用）
            subscribe: True 时立即订阅 NATS topic（decision 侧传 True）
        """
        self._hot_state_store = hot_state_store
        self._bus = bus
        self._process_role = process_role

        # 从 Redis 恢复初始快照
        if hot_state_store is not None:
            try:
                raw = await hot_state_store.get(self.redis_key)
                if isinstance(raw, dict) and raw:
                    self._latest = raw
                    self._last_updated_at = float(raw.get("_cached_at", 0.0))
                    log_event(
                        self._logger,
                        "guard_signal_cache_bootstrap_restored",
                        signal_name=self._signal_name,
                        process_role=process_role,
                        cached_at=self._last_updated_at,
                    )
            except Exception as exc:
                log_event(
                    self._logger,
                    "guard_signal_cache_bootstrap_redis_failed",
                    level="warning",
                    signal_name=self._signal_name,
                    process_role=process_role,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )

        self._bootstrapped = True

        if subscribe and bus is not None:
            await self._subscribe_internal()

        log_event(
            self._logger,
            "guard_signal_cache_bootstrapped",
            signal_name=self._signal_name,
            process_role=process_role,
            has_initial_data=bool(self._latest),
            subscribed=self._subscribed,
        )

    async def subscribe_deferred(self, *, bus: Any) -> None:
        """推迟订阅（用于 _CollectingBus 模式）。"""
        self._bus = bus
        await self._subscribe_internal()

    async def _subscribe_internal(self) -> None:
        if self._subscribed:
            return
        try:
            await self._bus.subscribe(
                topics.GUARD_SIGNAL_UPDATES,
                self._handle_remote_update,
            )
            self._subscribed = True
        except Exception as exc:
            log_event(
                self._logger,
                "guard_signal_cache_subscribe_failed",
                level="warning",
                signal_name=self._signal_name,
                process_role=self._process_role,
                error_type=type(exc).__name__,
                error=str(exc),
            )

    # ── 写路径 (execution 侧) ──

    async def publish(self, snapshot: dict[str, Any]) -> None:
        """发布 guard 快照到 local + Redis + NATS。

        三层写路径（与 ObligationHotStateCache 一致）：
          1. 同步写 local dict（立即可见）
          2. best-effort 写 Redis（跨进程持久化）
          3. best-effort NATS 广播（亚秒级通知 decision 侧）
        """
        now = time.time()
        enriched = {
            **snapshot,
            "_cached_at": now,
            "_signal_name": self._signal_name,
            "_writer_role": self._process_role,
        }

        # 1. 本地写
        self._latest = enriched
        self._last_updated_at = now

        # 2. best-effort Redis
        if self._hot_state_store is not None:
            try:
                await self._hot_state_store.set(
                    self.redis_key,
                    enriched,
                    ttl_seconds=self._stale_threshold * _REDIS_TTL_MULTIPLIER,
                )
            except Exception as exc:
                log_event(
                    self._logger,
                    "guard_signal_cache_redis_write_failed",
                    level="warning",
                    signal_name=self._signal_name,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )

        # 3. best-effort NATS 广播
        if self._bus is not None:
            try:
                from aats.schemas.common import EventEnvelope, dump_payload_exact

                envelope = EventEnvelope(
                    event_type="GuardSignalUpdate",
                    source_component="aats.governance.guard_signal_cache",
                    topic=topics.GUARD_SIGNAL_UPDATES,
                    key=self._signal_name,
                    payload=dump_payload_exact(enriched),
                )
                await self._bus.publish(
                    topic=topics.GUARD_SIGNAL_UPDATES,
                    key=self._signal_name,
                    payload=envelope.model_dump(mode="json"),
                )
            except Exception as exc:
                log_event(
                    self._logger,
                    "guard_signal_cache_nats_broadcast_failed",
                    level="warning",
                    signal_name=self._signal_name,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )

    # ── 读路径 (decision 侧) ──

    def snapshot(self) -> dict[str, Any]:
        """同步读取缓存的 guard 快照。

        RiskEngine 的 ``_runtime_guard_state()`` / ``_trial_guard_state()``
        会调用 ``provider.snapshot()``，所以必须是同步方法。

        **Fail-closed**：无数据或过期 → 返回空 dict →
        RiskEngine 视为 provider=None 的等效行为（不开新仓、只允许减仓）。
        """
        if not self._latest:
            return {}
        age = time.time() - self._last_updated_at
        if age > self._stale_threshold:
            return {}
        # 剥离内部 metadata，只返回业务字段
        return {k: v for k, v in self._latest.items() if not k.startswith("_")}

    def __call__(self) -> dict[str, Any]:
        """Callable 接口，用于 ``recovery_status_provider``。"""
        return self.snapshot()

    # ── NATS 回调 ──

    async def _handle_remote_update(self, message: dict[str, Any]) -> None:
        """NATS 订阅回调：收到远端广播的 guard 快照。

        只接受与自己 signal_name 匹配且时间戳更新的消息。
        """
        try:
            from aats.schemas.common import EventEnvelope

            envelope = EventEnvelope.model_validate(message["payload"])
            payload = envelope.payload
            if not isinstance(payload, dict):
                return
            # 只接受同 signal_name 的更新
            if payload.get("_signal_name") != self._signal_name:
                return
            cached_at = float(payload.get("_cached_at", 0.0))
            # 幂等：时间戳 <= 本地则忽略（乱序/重投/回环）
            if cached_at <= self._last_updated_at:
                return
            self._latest = payload
            self._last_updated_at = cached_at
        except Exception:
            pass  # fail-soft：NATS 回调不能抛异常

    # ── 诊断 ──

    def diagnostic(self) -> dict[str, Any]:
        """运维诊断信息。"""
        age = time.time() - self._last_updated_at if self._last_updated_at > 0 else None
        return {
            "signal_name": self._signal_name,
            "bootstrapped": self._bootstrapped,
            "subscribed": self._subscribed,
            "has_data": bool(self._latest),
            "last_updated_at": self._last_updated_at,
            "age_seconds": round(age, 1) if age is not None else None,
            "stale": age is not None and age > self._stale_threshold,
            "stale_threshold_seconds": self._stale_threshold,
            "process_role": self._process_role,
            "redis_key": self.redis_key,
        }
