from __future__ import annotations

import asyncio
import dataclasses
from collections import OrderedDict
from collections.abc import Callable
from typing import Generic, TypeVar

from aats.bootstrap.logging import get_logger, log_event
from aats.bootstrap.metrics import MetricsRegistry
from aats.events.envelopes import parse_envelope
from aats.schemas.common import EventEnvelope, utc_now
from aats.schemas.features import FeatureSnapshot
from aats.schemas.market import MarketSnapshot
from aats.services.decision_engine.trigger_policy import DecisionTriggerPolicy
from aats.services.decision_engine.orchestrator import DecisionOrchestrator
from aats.services.market_gateway.gateway import MarketDataGateway

CanTriggerCheck = Callable[..., tuple[bool, str]]

# LF-019：_enqueue_trigger 覆盖旧 pending 时递增此 counter，用于长期观测
# 队列饱和率（单次 run_cycle 毛刺越严重 → latest-wins 丢弃越多）。
METRIC_DROPPED_TRIGGERS = "decision_cycle_dropped_triggers_total"

# LF-007：_timeframe_locks / _consecutive_failures 原先是普通 dict，
# 以 (symbol, timeframe) 为 key。长期跑下来 delisted symbol / 已下线品种
# 的 entry 永远留着，内存无上限。用 OrderedDict + maxsize 的 LRU 约束。
#
# maxsize=256 经验值：symbol 组合×timeframe 上限目前几十级；预留 ~10×
# 余量既能容纳所有当前 (symbol,timeframe)、也不至于评估运维 delisted
# 后进入 legacy 旁路的锁也被误 evict（legacy 已默认关，production 下
# 这两个 dict 在 queue 路径基本只 read consecutive_failures）。
_MAX_TIMEFRAME_LOCK_ENTRIES = 256
_MAX_CONSECUTIVE_FAILURE_ENTRIES = 256

_K = TypeVar("_K")
_V = TypeVar("_V")


class _BoundedLRUDict(Generic[_K, _V]):
    """轻量 FIFO/LRU 边界 dict：insert 超过 maxsize 时淘汰最旧 entry。

    不依赖 cachetools（不在项目 deps 里），复用项目现有的 ``OrderedDict +
    popitem(last=False)`` 模式（参见 fill_event_cache.py）。

    语义约定：
    - ``setdefault`` / ``__setitem__`` 在新增 entry 时触发淘汰；
    - ``get`` 读路径 **不** move_to_end（避免给低价值查询带写锁语义）；
    - ``pop`` / ``__delitem__`` 简单透传，和 dict 一致；
    - 这个结构不是线程安全的，复用 trigger 原 dict 的语义（asyncio 单线程
      内使用）。
    """

    def __init__(self, maxsize: int) -> None:
        if maxsize <= 0:
            raise ValueError(f"maxsize must be > 0, got {maxsize}")
        self._maxsize = maxsize
        self._data: OrderedDict[_K, _V] = OrderedDict()

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def __getitem__(self, key: _K) -> _V:
        return self._data[key]

    def __setitem__(self, key: _K, value: _V) -> None:
        self._data[key] = value
        self._evict_if_over()

    def __delitem__(self, key: _K) -> None:
        del self._data[key]

    def get(self, key: _K, default: _V | None = None) -> _V | None:
        return self._data.get(key, default)

    def setdefault(self, key: _K, default: _V) -> _V:
        if key in self._data:
            return self._data[key]
        self._data[key] = default
        self._evict_if_over()
        return default

    def pop(self, key: _K, default: _V | None = None) -> _V | None:
        return self._data.pop(key, default)

    def _evict_if_over(self) -> None:
        while len(self._data) > self._maxsize:
            self._data.popitem(last=False)


@dataclasses.dataclass(frozen=True)
class _PendingTrigger:
    """单次 run_cycle 触发信号。

    由 handler 的快路径产生（命中 should_trigger=True 时），通过
    ``_trigger_queue`` 交给后台 dispatcher task 消费。带上
    ``feature_envelope`` 是为了让 run_cycle 读到的就是 trigger
    评估时那一条（R3-P1-U-A 的 ``feature_snapshot_ref`` 不漂移契约，
    见 ``context_builder.py:128`` 的 hint 优先路径）。
    """

    feature_envelope: EventEnvelope
    snapshot: FeatureSnapshot
    timeframe: str
    market_snapshot: MarketSnapshot


class DecisionCycleTrigger:
    # 连续失败后退避，避免堵死 asyncio 事件循环（冷启动时 feature store 为空）
    _BACKOFF_INITIAL_S = 2.0
    _BACKOFF_MAX_S = 30.0
    # LF-010：feature snapshot 从进 handler 到被消费之间的最大允许年龄。
    # market 进程重启 / NATS 积压 replay 时可能让 decision 收到很旧的 feature，
    # 用 snapshot_ts 对比 utc_now() 兜底；超过阈值直接丢弃并 warn。
    # 30s 经验值：trigger_policy 用的 market_data_stale_after_seconds=45s，
    # 这里取更紧的 30s 因为 feature 是从 market 衍生出来、正常新鲜的 feature
    # 一般在 1-2s 内到达 decision，30s 已经极宽。
    _MAX_FEATURE_SNAPSHOT_AGE_S = 30.0

    def __init__(
        self,
        *,
        orchestrator: DecisionOrchestrator,
        market_gateway: MarketDataGateway,
        policy: DecisionTriggerPolicy,
        can_trigger: CanTriggerCheck | None = None,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        self.orchestrator = orchestrator
        self.market_gateway = market_gateway
        self.policy = policy
        self.can_trigger = can_trigger
        self.metrics = metrics
        self.logger = get_logger("aats.decision_trigger")
        # legacy handler 路径沿用的 per-(symbol, timeframe) 锁；S3 清理
        # 时会一起删掉。queue 路径不再需要这把锁，因为单 dispatcher
        # task 天然串行。
        #
        # LF-007：两个 dict 都用 _BoundedLRUDict 封顶，避免 delisted
        # symbol / 下线品种的 entry 永驻内存。Type annotation 保留
        # dict 形式以最小化 call-site 修改（_BoundedLRUDict 实现了
        # dict 需要的 setdefault/get/pop/in 操作）。
        self._timeframe_locks: _BoundedLRUDict[
            tuple[str, str], asyncio.Lock
        ] = _BoundedLRUDict(maxsize=_MAX_TIMEFRAME_LOCK_ENTRIES)
        self._consecutive_failures: _BoundedLRUDict[
            tuple[str, str], int
        ] = _BoundedLRUDict(maxsize=_MAX_CONSECUTIVE_FAILURE_ENTRIES)

        # ──────────────────────────────────────────────────────────────
        # Queue dispatcher 基础设施（docs/task/
        # decision_features_handler_queue_decoupling_sow.md §3.S2）。
        #
        # 目的：把 run_cycle 从 NATS 订阅回调里搬出来。原设计里 handler
        # 直接 ``async with lock: await run_cycle(...)``，run_cycle 毛刺
        # 22s 时 32 个 in-flight handler 全堵在锁上、event loop 被
        # sync I/O 冲击 → NATS publish 超时 → decision_cycle_failed
        # 级联（见 SOW §1.2 根因链）。
        #
        # 当前路径：handler 只做 parse + should_trigger 判断，命中的
        # trigger 塞进 ``_trigger_queue`` 立即返回让 NATS ack；后台
        # ``_dispatcher_loop``（由 ``start()`` 拉起）单协程消费 queue 跑
        # run_cycle。``stop()`` 通过 cancel dispatcher task + 清空
        # queue 的方式收敛。
        #
        # 当前进度：S2 已上线（flag 默认 True）。S3 待删 legacy 路径
        # + flag 本身（legacy 的 ``_timeframe_locks`` 锁也随之回收）。
        # ──────────────────────────────────────────────────────────────
        self._trigger_queue: asyncio.Queue[_PendingTrigger] | None = None
        self._dispatcher_task: asyncio.Task[None] | None = None
        self._dispatcher_shutdown = asyncio.Event()
        # Feature flag：S2 已把默认切到 True；S3 会连同 flag + legacy 路径一起删。
        self._use_queue_dispatcher: bool = True

    # ──────────────────────────────────────────────────────────────
    # 生命周期：start() / stop()
    # bootstrap/config.py 的 _subscribe_critical_handlers 在 subscribe
    # 之前调 start()，stop_background_tasks 里镜像调 stop()。和
    # abort_hook_service 同模式。
    # ──────────────────────────────────────────────────────────────

    @property
    def background_task(self) -> asyncio.Task[None] | None:
        """返回 service-owned dispatcher task，供进程生命周期只读监督。"""
        return self._dispatcher_task

    async def start(self) -> None:
        """初始化 queue 并拉起后台 dispatcher task。在 bus.subscribe 之前调。

        具体动作：
        - 新建 ``_trigger_queue`` (``asyncio.Queue(maxsize=1)``，latest-wins
          语义见 ``_enqueue_trigger``)；
        - 清空 ``_dispatcher_shutdown``（支持 start→stop→start 的重启场景）；
        - ``asyncio.create_task(_dispatcher_loop(), name="features_snapshot_dispatcher")``
          并记录在 ``self._dispatcher_task`` 里，由 ``stop()`` 负责 cancel。

        幂等：已 start 过（``_dispatcher_task is not None``）时直接返回。
        """
        if self._dispatcher_task is not None:
            return
        self._trigger_queue = asyncio.Queue(maxsize=1)
        self._dispatcher_shutdown.clear()
        self._dispatcher_task = asyncio.create_task(
            self._dispatcher_loop(),
            name="features_snapshot_dispatcher",
        )

    async def stop(self) -> None:
        """通知 dispatcher 退出并等待收敛。在 stop_background_tasks 里调。

        process_lifecycle.py:274-287 的 ``finally: await
        runtime.stop_background_tasks()`` 是保证路径，drain 语义由该
        路径承担。queue 里若有 pending，dispatcher cancel 会丢 1 条；
        features_snapshots 每秒 30+ 条，下一条 ms 级内会再触发——
        业务上可接受（SOW §7 已论证为非风险）。
        """
        self._dispatcher_shutdown.set()
        if self._dispatcher_task is not None:
            self._dispatcher_task.cancel()
            try:
                await self._dispatcher_task
            except asyncio.CancelledError:
                pass
            self._dispatcher_task = None
        self._trigger_queue = None

    # ──────────────────────────────────────────────────────────────
    # Dispatcher loop
    # ──────────────────────────────────────────────────────────────

    async def _dispatcher_loop(self) -> None:
        """后台 task：从 queue 消费 trigger 跑 run_cycle。

        单协程串行——相当于把原 handler 里的 ``async with lock`` 语义
        移到这里，但**不再阻塞 NATS 订阅循环**。
        """
        assert self._trigger_queue is not None, "dispatcher_loop requires started queue"
        while not self._dispatcher_shutdown.is_set():
            try:
                pending = await self._trigger_queue.get()
            except asyncio.CancelledError:
                return
            try:
                await self._run_cycle_with_backoff(pending)
            except asyncio.CancelledError:
                # stop() 过程中的 cancel 传下来——让外层循环的
                # shutdown.is_set() 分支退出。
                raise
            except Exception as exc:  # noqa: BLE001
                # 和 legacy handler 等价的错误处理：记日志 + 计连续失败
                # + 退避。异常绝不能让 dispatcher 退出，否则 features_snapshots
                # 就永远消费不了。
                fail_key = (pending.snapshot.symbol, pending.timeframe)
                n = self._consecutive_failures.get(fail_key, 0) + 1
                self._consecutive_failures[fail_key] = n
                backoff = min(self._BACKOFF_INITIAL_S * n, self._BACKOFF_MAX_S)
                log_event(
                    self.logger,
                    "decision_cycle_failed",
                    level="warning" if n > 1 else "error",
                    symbol=pending.snapshot.symbol,
                    timeframe=pending.timeframe,
                    error_type=type(exc).__name__,
                    error=str(exc),
                    consecutive_failures=n,
                    backoff_s=backoff,
                )
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    raise
            finally:
                if self._trigger_queue is not None:
                    self._trigger_queue.task_done()

    async def _run_cycle_with_backoff(self, pending: _PendingTrigger) -> None:
        """跑 run_cycle 并在成功后 record_trigger。

        失败抛到 _dispatcher_loop 里处理 backoff。调用语义等价于 legacy
        handler 的 ``try: run_cycle else: record_trigger``。
        """
        should_run, drop_reason = self._pending_still_triggerable(pending)
        if not should_run:
            log_event(
                self.logger,
                "features_snapshot_trigger_dropped_stale_pending",
                level="info",
                symbol=pending.snapshot.symbol,
                timeframe=pending.timeframe,
                reason=drop_reason,
                feature_event_id=getattr(pending.feature_envelope, "event_id", None),
            )
            return
        await self.orchestrator.run_cycle(
            symbol=pending.snapshot.symbol,
            timeframe=pending.timeframe,
            feature_snapshot_hint=pending.feature_envelope,
            # 2026-04-23 P1-a：market snapshot 也透传，消除 trigger 评估 vs
            # build 读取之间 new market snapshot 抢跑的 ref 漂移。pending 已经
            # capture trigger 瞬间的 market_snapshot（trigger.py L482 抓取），
            # 这里只需把它送进决策路径；context_builder 会优先用此 hint。
            market_snapshot_hint=pending.market_snapshot,
        )
        fail_key = (pending.snapshot.symbol, pending.timeframe)
        self._consecutive_failures.pop(fail_key, None)
        self.policy.record_trigger(
            feature_snapshot=pending.snapshot,
            market_snapshot=pending.market_snapshot,
            timeframe=pending.timeframe,
        )

    def _pending_still_triggerable(self, pending: _PendingTrigger) -> tuple[bool, str]:
        """Re-check a queued trigger immediately before running a decision cycle.

        Queue admission and queue consumption can be separated by a full
        ``run_cycle`` duration. A previous cycle may record a trigger while a
        newer pending item is waiting in the queue, so the waiting item must go
        through the same policy gate again before it can consume another stale
        portfolio snapshot.
        """
        if self.can_trigger is not None:
            allowed, reason = self.can_trigger(symbol=pending.snapshot.symbol)
            if not allowed:
                return False, reason or "can_trigger_rejected_at_dispatch"
        should_trigger_fn = getattr(self.policy, "should_trigger", None)
        if not callable(should_trigger_fn):
            return True, "policy_revalidation_unavailable"
        should_trigger, reason = should_trigger_fn(
            feature_snapshot=pending.snapshot,
            market_snapshot=pending.market_snapshot,
            timeframe=pending.timeframe,
        )
        if not should_trigger:
            return False, reason or "policy_rejected_at_dispatch"
        return True, reason or "policy_revalidated"

    async def _enqueue_trigger(self, pending: _PendingTrigger) -> None:
        """覆盖式入队：queue maxsize=1 + latest-wins。

        语义：如果 queue 里已有一个 pending（dispatcher 还在跑上一个
        run_cycle），**直接替换成最新的**。被覆盖的那个 trigger 不
        执行——按 SOW §7 源码论证，不破坏决策语义：
        - should_trigger / record_trigger 状态机只在 run_cycle 完成
          后更新（trigger_policy.py:91-108），所以替换期间新 snapshot
          自然还是 should_trigger=True；
        - context_builder.py:128-136 优先用 ``feature_snapshot_hint``，
          每个 pending 自带 envelope，dispatcher 跑哪个就 hint 哪个，
          R3-P1-U-A ref 不漂移约束不受影响。
        """
        if self._trigger_queue is None:
            # start() 还没跑，不该到这里。保险：把 trigger 丢弃并记日志。
            log_event(
                self.logger,
                "features_snapshot_trigger_dropped_no_queue",
                level="warning",
                symbol=pending.snapshot.symbol,
                timeframe=pending.timeframe,
            )
            return

        # 非阻塞 drain 旧 pending（maxsize=1 最多一条）
        try:
            _stale = self._trigger_queue.get_nowait()
            # get_nowait 取出后必须 task_done 配对，否则 queue 的
            # unfinished_tasks 计数永远不归零，影响未来 join()。
            self._trigger_queue.task_done()
            # LF-019：被新 pending 覆盖的旧 trigger 没被 run_cycle 消费，
            # 递增 counter 供 Prometheus 长期观测队列饱和趋势。
            if self.metrics is not None:
                self.metrics.increment(METRIC_DROPPED_TRIGGERS)
        except asyncio.QueueEmpty:
            pass

        try:
            self._trigger_queue.put_nowait(pending)
        except asyncio.QueueFull:
            # 极罕见竞态：两个 handler 同时走到 drain-then-put 之间，
            # 其中一个在我们 drain 之后刚放进去。此时直接丢当前 pending
            # 也对——queue 里已有一个同等级或更新的 trigger。
            log_event(
                self.logger,
                "features_snapshot_trigger_dropped_race",
                level="debug",
                symbol=pending.snapshot.symbol,
                timeframe=pending.timeframe,
            )
            # LF-019：竞态路径丢的是当前 pending（不是旧的），同样计数
            if self.metrics is not None:
                self.metrics.increment(METRIC_DROPPED_TRIGGERS)

    # ──────────────────────────────────────────────────────────────
    # NATS handler 入口（按 flag 分流，S2 会把 flag 切到 True）
    # ──────────────────────────────────────────────────────────────

    def _is_feature_snapshot_fresh(self, snapshot: FeatureSnapshot) -> bool:
        """LF-010：feature snapshot 的 wall-clock 年龄校验。

        snapshot_ts 是 market 侧给 feature 盖的时间戳（exchange truth），
        如果 market 进程崩/JetStream 背压导致老消息 replay 进来，可能是
        几分钟前的。这里用 utc_now() - snapshot_ts 跟 _MAX_FEATURE_SNAPSHOT_AGE_S
        对比；超过就拒绝并 log warning，避免下游 orchestrator 拿陈旧
        feature 跑 run_cycle。

        返回 True 表示可以继续处理。False 分支内已 log。
        """
        age_s = (utc_now() - snapshot.snapshot_ts).total_seconds()
        if age_s > self._MAX_FEATURE_SNAPSHOT_AGE_S:
            log_event(
                self.logger,
                "features_snapshot_rejected_stale",
                level="warning",
                symbol=snapshot.symbol,
                snapshot_ts=snapshot.snapshot_ts.isoformat(),
                age_s=age_s,
                max_age_s=self._MAX_FEATURE_SNAPSHOT_AGE_S,
            )
            return False
        return True

    async def handle_feature_snapshot(self, message: dict) -> None:
        if self._use_queue_dispatcher:
            await self._handle_feature_snapshot_via_queue(message)
        else:
            await self._handle_feature_snapshot_legacy(message)

    async def _handle_feature_snapshot_legacy(self, message: dict) -> None:
        # R3-P1-U-A：同时保留触发本次 cycle 的 feature envelope（parse_envelope 得到
        # 完整 EventEnvelope，含 event_id / event_timestamp），向下游 run_cycle 透传。
        # 保证 DecisionContext.feature_snapshot_ref = 本 envelope.event_id，与
        # trigger_policy 评估依据的 snapshot 严格一致，消除触发与构建之间新
        # snapshot 抢跑导致的 ref 漂移。
        feature_envelope = parse_envelope(message)
        snapshot = FeatureSnapshot.model_validate(feature_envelope.payload)
        # LF-010：拒绝陈旧的 feature snapshot（market 重启 / NATS replay 保护）
        if not self._is_feature_snapshot_fresh(snapshot):
            return
        if self.can_trigger is not None:
            allowed, _reason = self.can_trigger(symbol=snapshot.symbol)
            if not allowed:
                return
        for timeframe in self.policy.enabled_timeframes():
            lock = self._timeframe_locks.setdefault((snapshot.symbol, timeframe), asyncio.Lock())
            async with lock:
                if self.can_trigger is not None:
                    allowed, _reason = self.can_trigger(symbol=snapshot.symbol)
                    if not allowed:
                        continue
                current_market_snapshot = self.market_gateway.latest_snapshot(snapshot.symbol)
                should_trigger, _reason = self.policy.should_trigger(
                    feature_snapshot=snapshot,
                    market_snapshot=current_market_snapshot,
                    timeframe=timeframe,
                )
                if not should_trigger or current_market_snapshot is None:
                    continue
                fail_key = (snapshot.symbol, timeframe)
                try:
                    await self.orchestrator.run_cycle(
                        symbol=snapshot.symbol,
                        timeframe=timeframe,
                        feature_snapshot_hint=feature_envelope,
                        # 2026-04-23 P1-a：对等于 feature hint，透传 market
                        # snapshot。legacy inline 路径也修一遍，语义一致。
                        market_snapshot_hint=current_market_snapshot,
                    )
                except Exception as exc:
                    n = self._consecutive_failures.get(fail_key, 0) + 1
                    self._consecutive_failures[fail_key] = n
                    backoff = min(self._BACKOFF_INITIAL_S * n, self._BACKOFF_MAX_S)
                    log_event(
                        self.logger,
                        "decision_cycle_failed",
                        level="warning" if n > 1 else "error",
                        symbol=snapshot.symbol,
                        timeframe=timeframe,
                        error_type=type(exc).__name__,
                        error=str(exc),
                        consecutive_failures=n,
                        backoff_s=backoff,
                    )
                    await asyncio.sleep(backoff)
                    continue
                # 成功后重置退避计数
                self._consecutive_failures.pop(fail_key, None)
                self.policy.record_trigger(
                    feature_snapshot=snapshot,
                    market_snapshot=current_market_snapshot,
                    timeframe=timeframe,
                )

    async def _handle_feature_snapshot_via_queue(self, message: dict) -> None:
        """快路径（S2 实施）：parse + should_trigger 判断，命中就 enqueue
        立即 return 让 NATS ack。**不** 在这条路径上 await run_cycle。

        语义对齐 legacy 的几处关键：
        - R3-P1-U-A：feature_envelope 作为 feature_snapshot_hint 透传给
          run_cycle（放进 _PendingTrigger），context_builder.py:128 的
          hint 优先路径保证 ref 不漂移。
        - can_trigger 门控：handler 入口做一次 + 每个 timeframe 前重做
          一次，维持 legacy 的双重检查语义（legacy 在锁内重检，本路径
          无锁，把第二次检查保留为"入队前最后一次兜底"）。
        - policy.should_trigger / record_trigger：record_trigger 由
          dispatcher 在 run_cycle 成功后调，handler 只 read should_trigger。

        和 legacy 的差异：
        - 没有 asyncio.Lock。单 dispatcher task 天然串行；多个 handler
          可能并发命中 should_trigger=True 时通过 _enqueue_trigger 的
          latest-wins 排重（SOW §7 已源码论证不破坏决策语义）。
        """
        feature_envelope = parse_envelope(message)
        snapshot = FeatureSnapshot.model_validate(feature_envelope.payload)
        # LF-010：拒绝陈旧的 feature snapshot（market 重启 / NATS replay 保护）
        if not self._is_feature_snapshot_fresh(snapshot):
            return
        if self.can_trigger is not None:
            allowed, _reason = self.can_trigger(symbol=snapshot.symbol)
            if not allowed:
                return
        for timeframe in self.policy.enabled_timeframes():
            # 入队前的 can_trigger 二次检查：对齐 legacy 锁内那次 (trigger.py
            # 原 line 52-55)，处理的是"第一次 can_trigger check 之后、此 for 循环
            # 执行期间 halted/mode_control 状态刚好切换"的极窄竞态。legacy
            # 路径里是抓到 lock 再 check，本路径无锁，把这次 check 放在
            # should_trigger 之前（策略计算之前）效果等价。
            if self.can_trigger is not None:
                allowed, _reason = self.can_trigger(symbol=snapshot.symbol)
                if not allowed:
                    continue
            current_market_snapshot = self.market_gateway.latest_snapshot(snapshot.symbol)
            should_trigger, _reason = self.policy.should_trigger(
                feature_snapshot=snapshot,
                market_snapshot=current_market_snapshot,
                timeframe=timeframe,
            )
            if not should_trigger or current_market_snapshot is None:
                continue
            await self._enqueue_trigger(
                _PendingTrigger(
                    feature_envelope=feature_envelope,
                    snapshot=snapshot,
                    timeframe=timeframe,
                    market_snapshot=current_market_snapshot,
                )
            )
