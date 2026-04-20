from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Callable

from aats.bootstrap.logging import get_logger, log_event
from aats.events.envelopes import parse_envelope, parse_payload
from aats.schemas.common import EventEnvelope
from aats.schemas.features import FeatureSnapshot
from aats.schemas.market import MarketSnapshot
from aats.services.decision_engine.trigger_policy import DecisionTriggerPolicy
from aats.services.decision_engine.orchestrator import DecisionOrchestrator
from aats.services.market_gateway.gateway import MarketDataGateway

CanTriggerCheck = Callable[..., tuple[bool, str]]


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

    def __init__(
        self,
        *,
        orchestrator: DecisionOrchestrator,
        market_gateway: MarketDataGateway,
        policy: DecisionTriggerPolicy,
        can_trigger: CanTriggerCheck | None = None,
    ) -> None:
        self.orchestrator = orchestrator
        self.market_gateway = market_gateway
        self.policy = policy
        self.can_trigger = can_trigger
        self.logger = get_logger("aats.decision_trigger")
        # legacy handler 路径沿用的 per-(symbol, timeframe) 锁；S3 清理
        # 时会一起删掉。queue 路径不再需要这把锁，因为单 dispatcher
        # task 天然串行。
        self._timeframe_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._consecutive_failures: dict[tuple[str, str], int] = {}

        # ──────────────────────────────────────────────────────────────
        # Queue dispatcher 基础设施（docs/task/
        # decision_features_handler_queue_decoupling_sow.md §3.S1）。
        #
        # 目的：把 run_cycle 从 NATS 订阅回调里搬出来。原设计里 handler
        # 直接 ``async with lock: await run_cycle(...)``，run_cycle 毛刺
        # 22s 时 32 个 in-flight handler 全堵在锁上、event loop 被
        # sync I/O 冲击 → NATS publish 超时 → decision_cycle_failed
        # 级联（见 SOW §1.2 根因链）。
        #
        # 新路径：handler 只做 parse + should_trigger 判断，命中的
        # trigger 塞进 ``_trigger_queue`` 立即返回让 NATS ack；后台
        # ``_dispatcher_loop`` 单协程消费 queue 跑 run_cycle。
        #
        # S1 先建这个骨架，但 flag 默认 False，生产行为**完全不变**。
        # S2 切 flag=True 启用；S3 彻底删 legacy。
        # ──────────────────────────────────────────────────────────────
        self._trigger_queue: asyncio.Queue[_PendingTrigger] | None = None
        self._dispatcher_task: asyncio.Task[None] | None = None
        self._dispatcher_shutdown = asyncio.Event()
        # Feature flag：S2 改成 True；S3 连同 flag 一起删。
        self._use_queue_dispatcher: bool = False

    # ──────────────────────────────────────────────────────────────
    # 生命周期：start() / stop()
    # bootstrap/config.py 的 _subscribe_critical_handlers 在 subscribe
    # 之前调 start()，stop_background_tasks 里镜像调 stop()。和
    # abort_hook_service 同模式。
    # ──────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """初始化 queue + 起 dispatcher task。在 bus.subscribe 之前调。

        幂等：多次调用只起一个 task。
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
        await self.orchestrator.run_cycle(
            symbol=pending.snapshot.symbol,
            timeframe=pending.timeframe,
            feature_snapshot_hint=pending.feature_envelope,
        )
        fail_key = (pending.snapshot.symbol, pending.timeframe)
        self._consecutive_failures.pop(fail_key, None)
        self.policy.record_trigger(
            feature_snapshot=pending.snapshot,
            market_snapshot=pending.market_snapshot,
            timeframe=pending.timeframe,
        )

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

    # ──────────────────────────────────────────────────────────────
    # NATS handler 入口（按 flag 分流，S2 会把 flag 切到 True）
    # ──────────────────────────────────────────────────────────────

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
        """S2 实现快路径（本 S1 stage 不启用）。

        S1 先写空壳避免 flag=True 时炸。S2 commit 会把这里填实，
        flag 同步改 True。
        """
        # S1 占位：flag 为 False 时此分支不会被调用（handle_feature_snapshot
        # 已经 route 到 legacy）。万一有人误开 flag 至少不崩溃。
        log_event(
            self.logger,
            "features_snapshot_via_queue_not_implemented",
            level="warning",
            note="S2 未合入时 flag 不应为 True；回退到 legacy",
        )
        await self._handle_feature_snapshot_legacy(message)
