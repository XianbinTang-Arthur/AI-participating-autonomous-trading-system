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
  - **Fail-closed**：快照缺失或过期 → 返回 _FAIL_CLOSED_SENTINEL
    （only_reduce_required=True, safe_to_trade=False）→ RiskEngine 落入
    只减仓模式（不开新仓、不放行新 intent）

Redis key 格式：``aats:hot:system:guard_signal:<signal_name>``
NATS topic：``system.guard_signal_updates``，key 为 signal_name

signal_name 约定：
  - ``derivatives_live`` — DerivativesLiveGuardService.snapshot()
  - ``trial`` — ForwardTrialGuardService.snapshot()
  - ``recovery`` — RecoveryPostureEvaluator.finalize_status()
"""
from __future__ import annotations

import hashlib
import json
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

# Fail-closed sentinel：guard 快照缺失或过期时返回此 dict，
# 让 RiskEngine 落入保守模式（只减仓、不允许开新仓）。
#
# RiskEngine 有两条拒绝路径：
#   A) 硬拒绝：_runtime_guard_only_reduce_reasons() / _recovery_status_only_reduce_reasons()
#      返回非空 reasons → _evaluate_derivatives_pretrade 第 1526 行 if 成立 → 拒绝开仓
#   B) 软约束：_adaptive_control_states() 读 only_reduce_required → 压缩 multiplier
#
# 必须同时覆盖两条路径。only_reduce_required=True 走路径 B，
# only_reduce_reasons 非空走路径 A（硬拒绝）。
#
# 字段清单：
#   - only_reduce_required=True     → 路径 B 软约束（multiplier 压缩）
#   - only_reduce_reasons=[...]     → 路径 A 硬拒绝（开仓 intent 被 reject）
#   - auto_halt_required=False      → 不触发自动停机（仅限制开仓，不暴力停车）
#   - safe_to_trade=False           → recovery 层视为不安全
#   - review_required=True          → 要求 operator 人工审核
#   - status="stale"                → 不匹配 "breached"（trial guard）但语义明确
#
# 注意：之前返回空 dict {}，RiskEngine 对空 dict 的所有 .get() 默认值
# 都是 permissive（only_reduce=False, safe_to_trade=True, breached=False），
# 导致实际 fail-open。本 sentinel 修复了该安全缺陷。
_FAIL_CLOSED_SENTINEL: dict[str, Any] = {
    "only_reduce_required": True,
    "only_reduce_reasons": ["guard_signal_missing_or_stale"],
    "auto_halt_required": False,
    "safe_to_trade": False,
    "review_required": True,
    "status": "stale",
    "_stale": True,
}


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
        # 2026-04-21：publish 时对业务 payload 做内容哈希，与上次持久化过的
        # 对比；若相同则走 persist=False 的 publish_envelope 路径 —— 仍广播
        # NATS（reader `_handle_remote_update` 心跳更新，避免 120s 后 fail-closed）
        # 但跳过 event_store.append（3.3 GB 重复记录消失）。
        self._last_published_hash: str | None = None

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

        # 3. best-effort NATS 广播（+ event_store 持久化分流）
        #
        # 2026-04-21 dedup：在同 signal_name 连续 publish 时，如果业务 payload
        # 与上次完全相同，跳过 event_store.append（避免 recovery 信号 709 KB ×
        # 13s 的 98.5% 重复持久化），但**仍然** publish 到 NATS ——
        #
        # 为什么 NATS 必须保留：reader 侧（decision 进程）的
        # `_handle_remote_update` 更新 `_last_updated_at`；``snapshot()`` 有
        # `age > stale_threshold (120s) → fail-closed sentinel` 的硬约束。
        # 如果 NATS 被 dedup 跳过，超过 120s 就会让 RiskEngine 误入 only-reduce
        # 模式。NATS 传输是纯内存/临时的，重复消息零成本；只有 PG event_store
        # 是昂贵的部分，所以只 dedup event_store。
        #
        # dedup hash 基于 business `snapshot`（不包括 `_cached_at`/`_writer_role`
        # 等 metadata），保证同一业务状态连续 publish 识别为重复。
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

                # 计算业务 payload hash（排除 metadata）
                try:
                    payload_signature = json.dumps(
                        snapshot, sort_keys=True, default=str
                    )
                    payload_hash = hashlib.sha256(
                        payload_signature.encode("utf-8")
                    ).hexdigest()
                except Exception:
                    # json 化失败（unlikely，snapshot 已经走过 dump_payload_exact）
                    # → 保守起见按"不同"处理，持久化
                    payload_hash = None

                is_duplicate = (
                    payload_hash is not None
                    and payload_hash == self._last_published_hash
                )

                # 优先走 publish_envelope(persist=...)；如果 bus 不支持此接口
                # （如 KafkaEventBus 只有 base 的 publish(...)），fallback 到
                # 原 publish 路径，persist 由 bus 默认决定（= True）。
                publish_envelope = getattr(self._bus, "publish_envelope", None)
                if callable(publish_envelope):
                    await publish_envelope(envelope, persist=not is_duplicate)
                else:
                    # fallback: 老 bus 接口，不能控制 persist 粒度
                    await self._bus.publish(
                        topic=topics.GUARD_SIGNAL_UPDATES,
                        key=self._signal_name,
                        payload=envelope.model_dump(mode="json"),
                    )

                # publish 成功后才更新 hash —— 失败时下次必须走 persist=True
                # 以保证数据落盘
                if payload_hash is not None:
                    self._last_published_hash = payload_hash
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

        **Fail-closed**：无数据或过期 → 返回 ``_FAIL_CLOSED_SENTINEL``
        （only_reduce_required=True, safe_to_trade=False），确保 RiskEngine
        在 execution 尚未发布、Redis/NATS 断链或缓存过期时落入只减仓模式，
        而不是默认无约束放行开仓。
        """
        if not self._latest:
            log_event(
                self._logger,
                "guard_signal_cache_fail_closed",
                level="warning",
                signal_name=self._signal_name,
                reason="no_data",
            )
            return dict(_FAIL_CLOSED_SENTINEL)
        age = time.time() - self._last_updated_at
        if age > self._stale_threshold:
            log_event(
                self._logger,
                "guard_signal_cache_fail_closed",
                level="warning",
                signal_name=self._signal_name,
                reason="stale",
                age_seconds=round(age, 1),
                threshold_seconds=self._stale_threshold,
            )
            return dict(_FAIL_CLOSED_SENTINEL)
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
