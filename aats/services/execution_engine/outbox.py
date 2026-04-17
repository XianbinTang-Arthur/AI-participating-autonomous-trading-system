from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.orm.exc import StaleDataError

from aats.bootstrap.logging import get_logger, log_event
from aats.bus.base import EventBus
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.common import EventEnvelope
from aats.schemas.execution import FillEvent, OrderIntent, OrderObligation, OrderState
from aats.schemas.operator import ExecutionErrorSummary
from aats.services.execution_control.order_service import ExecutionOrderService
from aats.services.execution_control.shadow import Phase1ExecutionShadowService
from aats.storage.event_store_postgres import PostgresEventStore
from aats.storage.base import ExecutionRepository
from aats.storage.execution_command_repo_postgres import PostgresExecutionCommandRepository
from aats.storage.execution_fill_repo_v2_postgres import PostgresExecutionFillRepositoryV2
from aats.storage.execution_order_repo_postgres import (
    PostgresExecutionOrderHistoryRepository,
    PostgresExecutionOrderRepository,
)
from aats.storage.obligation_repo_postgres import PostgresExecutionObligationRepository
from aats.storage.outbox_repo_postgres import PostgresOutboxRepository

if TYPE_CHECKING:
    from aats.services.execution_engine.obligation_cache import ObligationHotStateCache
    from aats.services.execution_engine.order_state_cache import OrderStateHotCache
    from aats.services.execution_engine.fill_event_cache import FillEventHotCache


@dataclass(slots=True)
class PostgresExecutionOutboxPublisher:
    _MAX_PUBLISH_ATTEMPTS: ClassVar[int] = 3
    # Stage 5：OrderStateModel 的 row_version OCC 在 commit 时如果检测到
    # 别的 worker 已经推进了 row_version，会抛 StaleDataError。我们重读
    # 最新行后让 OrderStateMachine.merge 重新合并自己的 incoming state，
    # 再 commit 一次。3 次仍然失败说明竞争异常激烈，让 caller 看到错误。
    _MAX_PERSIST_ATTEMPTS: ClassVar[int] = 3
    # P1-16：单条 envelope 发 NATS 的硬超时。publish_envelope 底层的 NATS
    # connection drain / ACK 链路若在网络分区时挂起，会把整条 flush_pending
    # 协程一起拖住（下一周期的 execution loop 跟着卡住），订单队列堆积。
    # 加 wait_for 上限让 flush 能快速失败并把事件推进到 record_failure，避免
    # 一条坏事件拖垮整条 pipeline。
    # 声明为 ClassVar 是为了继续作为类级常量存在（与 _MAX_PUBLISH_ATTEMPTS 一致），
    # 不进入 dataclass field 顺序，避免 Python 3.14 的 "non-default follows default"
    # 校验把后续无默认值的字段判为违法。
    _PUBLISH_TIMEOUT_SECONDS: ClassVar[float] = 5.0
    session_factory: sessionmaker[Session]
    event_store: PostgresEventStore
    execution_repo: ExecutionRepository
    obligation_repo: PostgresExecutionObligationRepository
    outbox_repo: PostgresOutboxRepository
    bus: EventBus
    execution_command_repo: PostgresExecutionCommandRepository | None = None
    execution_order_repo: PostgresExecutionOrderRepository | None = None
    execution_order_history_repo: PostgresExecutionOrderHistoryRepository | None = None
    execution_fill_repo: PostgresExecutionFillRepositoryV2 | None = None
    # Stage 6 Slice 6.5：跨进程 obligation 缓存。persist_order_state /
    # persist_order_state_and_command / persist_fill 这三个 async entry 在
    # 事务 commit 成功之后会调 cache.fire_and_forget_publish(obligation)，把
    # 最新的 obligation 快照推到 cache 本地 dict + Redis + NATS 广播。None =
    # 未接线（legacy test / recovery-only path），行为与 6.5 之前完全一样。
    # 设计文档：docs/task/stage_6_slice_6_5_obligation_hot_state_design.md §10
    obligation_cache: "ObligationHotStateCache | None" = None
    # P1-1：跨进程 OrderState 缓存。事务 commit 成功后 best-effort 推送
    # 最新 order_state 到 cache 本地 dict + Redis。NATS 广播已由 flush_pending
    # 完成（通过 outbox → ORDER_UPDATES topic），cache 通过订阅接收。
    order_state_cache: "OrderStateHotCache | None" = None
    # P1-2：跨进程 FillEvent 缓存。事务 commit 成功后 best-effort 推送
    # fill 到 cache。append-only，dedup by fill_id。
    fill_event_cache: "FillEventHotCache | None" = None
    logger: Any = field(init=False)

    def __post_init__(self) -> None:
        self.logger = get_logger("aats.execution_outbox")

    async def persist_order_state(
        self,
        *,
        order_state: OrderState,
        key: str,
        obligation: OrderObligation | None = None,
    ) -> OrderState:
        # 原来 with self.session_factory() as session: 开启的是 SQLAlchemy 同步
        # session——BEGIN / UPDATE / INSERT / COMMIT 全跑在 event loop 主线程上。
        # 每次下单 / 撤单 / 状态转移都至少走一次，直接影响 HTTP handler 响应速度。
        # 全部丢到 asyncio.to_thread，让 await 成为真正的 yield 点。flush_pending
        # 留在主协程里——它内部已经走 to_thread + 异步 publish_envelope。
        persisted = await asyncio.to_thread(
            self._persist_order_state_with_retry,
            order_state=order_state,
            key=key,
            obligation=obligation,
        )
        await self.flush_pending()
        # Stage 6 Slice 6.5：事务已 commit，obligation 广播到 cache。必须放在
        # flush_pending 之后，这样 order_update envelope 已经通过 NATS 出去了；
        # cache 的 OBLIGATION_UPDATES 广播独立一条 topic，不受 flush_pending
        # 影响。
        self._publish_obligation_to_cache(obligation)
        # P1-1：事务已 commit，order_state 推到跨进程 cache。
        self._publish_order_state_to_cache(persisted)
        return persisted

    def _persist_order_state_with_retry(
        self,
        *,
        order_state: OrderState,
        key: str,
        obligation: OrderObligation | None,
    ) -> OrderState:
        # Stage 5：OCC retry 包装。每次失败重新打开 session，让
        # _persist_order_state_sync 内部重新 SELECT 最新 row，
        # 让 OrderStateMachine.merge 重新合并 incoming state，再 commit。
        last_exc: StaleDataError | None = None
        for attempt in range(self._MAX_PERSIST_ATTEMPTS):
            try:
                return self._persist_order_state_sync(
                    order_state=order_state,
                    key=key,
                    obligation=obligation,
                )
            except StaleDataError as exc:
                last_exc = exc
                log_event(
                    self.logger,
                    "execution_outbox_order_state_stale_retry",
                    level="warning",
                    client_order_id=order_state.client_order_id,
                    intent_id=order_state.intent_id,
                    attempt=attempt + 1,
                    max_attempts=self._MAX_PERSIST_ATTEMPTS,
                )
        assert last_exc is not None
        log_event(
            self.logger,
            "execution_outbox_order_state_stale_exhausted",
            level="error",
            client_order_id=order_state.client_order_id,
            intent_id=order_state.intent_id,
            max_attempts=self._MAX_PERSIST_ATTEMPTS,
        )
        raise last_exc

    def _persist_order_state_sync(
        self,
        *,
        order_state: OrderState,
        key: str,
        obligation: OrderObligation | None,
    ) -> OrderState:
        with self.session_factory() as session:
            persisted, previous = self.execution_repo.save_order_state_in_session(session, order_state)  # type: ignore[attr-defined]
            self._ensure_execution_order_row(session, order_state=persisted)
            if obligation is not None:
                self.obligation_repo.save_obligation_in_session(session, obligation)
            envelopes = [self._order_update_envelope(key=key, persisted=persisted)]
            summary = self._execution_error_summary(previous=previous, persisted=persisted)
            if summary is not None:
                envelopes.append(summary)
            for envelope in envelopes:
                self.event_store.append_in_session(session, envelope)
                self.outbox_repo.enqueue_in_session(session, envelope)
            session.commit()
        return persisted

    async def persist_order_state_and_command(
        self,
        *,
        order_state: OrderState,
        key: str,
        command_id: str,
        command_type: str,
        command_idempotency_key: str,
        command_payload: dict[str, Any],
        command_created_at,
        obligation: OrderObligation | None = None,
    ) -> OrderState:
        if self.execution_command_repo is None:
            return await self.persist_order_state(order_state=order_state, key=key, obligation=obligation)
        # 同 persist_order_state，丢线程池；此版本额外写一条 execution_command
        # 行，所以下面 helper 需要独立的参数签名。
        persisted = await asyncio.to_thread(
            self._persist_order_state_and_command_with_retry,
            order_state=order_state,
            key=key,
            command_id=command_id,
            command_type=command_type,
            command_idempotency_key=command_idempotency_key,
            command_payload=command_payload,
            command_created_at=command_created_at,
            obligation=obligation,
        )
        await self.flush_pending()
        # Stage 6 Slice 6.5：事务已 commit，obligation 广播到 cache。
        self._publish_obligation_to_cache(obligation)
        # P1-1：事务已 commit，order_state 推到跨进程 cache。
        self._publish_order_state_to_cache(persisted)
        return persisted

    def _persist_order_state_and_command_with_retry(
        self,
        *,
        order_state: OrderState,
        key: str,
        command_id: str,
        command_type: str,
        command_idempotency_key: str,
        command_payload: dict[str, Any],
        command_created_at,
        obligation: OrderObligation | None,
    ) -> OrderState:
        # Stage 5：与 _persist_order_state_with_retry 相同的 OCC retry 模式，
        # 包住带 command 写的版本。每次重试都会重新 SELECT order_states
        # 最新行 + 重新 merge incoming state，然后整事务一次 commit。
        # 注意 execution_command 行用 idempotency_key 唯一性约束保证幂等，
        # 重试时同 command_id 会被 enqueue_command_in_session 内部识别为重复。
        last_exc: StaleDataError | None = None
        for attempt in range(self._MAX_PERSIST_ATTEMPTS):
            try:
                return self._persist_order_state_and_command_sync(
                    order_state=order_state,
                    key=key,
                    command_id=command_id,
                    command_type=command_type,
                    command_idempotency_key=command_idempotency_key,
                    command_payload=command_payload,
                    command_created_at=command_created_at,
                    obligation=obligation,
                )
            except StaleDataError as exc:
                last_exc = exc
                log_event(
                    self.logger,
                    "execution_outbox_order_state_command_stale_retry",
                    level="warning",
                    client_order_id=order_state.client_order_id,
                    intent_id=order_state.intent_id,
                    command_id=command_id,
                    attempt=attempt + 1,
                    max_attempts=self._MAX_PERSIST_ATTEMPTS,
                )
        assert last_exc is not None
        log_event(
            self.logger,
            "execution_outbox_order_state_command_stale_exhausted",
            level="error",
            client_order_id=order_state.client_order_id,
            intent_id=order_state.intent_id,
            command_id=command_id,
            max_attempts=self._MAX_PERSIST_ATTEMPTS,
        )
        raise last_exc

    def _persist_order_state_and_command_sync(
        self,
        *,
        order_state: OrderState,
        key: str,
        command_id: str,
        command_type: str,
        command_idempotency_key: str,
        command_payload: dict[str, Any],
        command_created_at,
        obligation: OrderObligation | None,
    ) -> OrderState:
        with self.session_factory() as session:
            persisted, previous = self.execution_repo.save_order_state_in_session(session, order_state)  # type: ignore[attr-defined]
            self._ensure_execution_order_row(
                session,
                order_state=persisted,
                command_type=command_type,
                command_payload=command_payload,
            )
            if obligation is not None:
                self.obligation_repo.save_obligation_in_session(session, obligation)
            envelopes = [self._order_update_envelope(key=key, persisted=persisted)]
            summary = self._execution_error_summary(previous=previous, persisted=persisted)
            if summary is not None:
                envelopes.append(summary)
            for envelope in envelopes:
                self.event_store.append_in_session(session, envelope)
                self.outbox_repo.enqueue_in_session(session, envelope)
            assert self.execution_command_repo is not None  # 上游已判空
            self.execution_command_repo.enqueue_command_in_session(
                session,
                command_id=command_id,
                order_id=persisted.client_order_id,
                command_type=command_type,
                idempotency_key=command_idempotency_key,
                payload=command_payload,
                created_at=command_created_at,
            )
            session.commit()
        return persisted

    def _ensure_execution_order_row(
        self,
        session: Session,
        *,
        order_state: OrderState,
        command_type: str | None = None,
        command_payload: dict[str, Any] | None = None,
    ) -> None:
        if self.execution_order_repo is None:
            return
        existing = self.execution_order_repo.get_order_by_client_order_id_in_session(
            session,
            order_state.client_order_id,
            for_update=True,
        )
        if existing is not None:
            return
        intent = self._seed_intent_from_command_payload(command_type=command_type, command_payload=command_payload)
        if intent is None:
            intent = ExecutionOrderService._intent_from_order_state(order_state)
        created_at = order_state.created_at
        self.execution_order_repo.create_order_in_session(
            session,
            order_id=order_state.client_order_id,
            intent=intent,
            initial_state=order_state.status,
            created_at=created_at,
            raw_payload={
                "client_order_id": order_state.client_order_id,
                "venue_order_id": order_state.exchange_order_id,
                "source_system": order_state.submission_mode or "execution_outbox_publisher",
                "intent": intent.model_dump(mode="python"),
                "order_state": order_state.model_dump(mode="python"),
            },
        )
        if self.execution_order_history_repo is not None:
            self.execution_order_history_repo.append_transition_in_session(
                session,
                order_id=order_state.client_order_id,
                from_state=None,
                to_state=order_state.status,
                reason_code="execution_outbox_seed",
                source="execution_outbox",
                source_message_id=order_state.intent_id,
                payload=order_state.model_dump(mode="python"),
                created_at=created_at,
            )

    def _ensure_execution_fill_row(
        self,
        session: Session,
        *,
        fill: FillEvent,
    ) -> None:
        if self.execution_fill_repo is None:
            return
        order_id = fill.client_order_id
        if self.execution_order_repo is not None:
            existing = self.execution_order_repo.get_order_by_client_order_id_in_session(
                session,
                fill.client_order_id,
                for_update=True,
            )
            if existing is None:
                intent = Phase1ExecutionShadowService.intent_from_fill(fill)
                self.execution_order_repo.create_order_in_session(
                    session,
                    order_id=fill.client_order_id,
                    intent=intent,
                    initial_state=fill.order_status_after_fill or "FILLED",
                    created_at=fill.created_at,
                    raw_payload={
                        "client_order_id": fill.client_order_id,
                        "venue_order_id": fill.exchange_order_id,
                        "source_system": "execution_outbox_fill_backfill",
                        "fill_event": fill.model_dump(mode="python"),
                    },
                )
            else:
                order_id = str(existing["order_id"])
        self.execution_fill_repo.save_fill_in_session(
            session,
            fill=fill,
            order_id=order_id,
            source=fill.venue.lower(),
            raw_payload={
                "venue_fill_id": fill.fill_id,
                "fill_event": fill.model_dump(mode="python"),
            },
        )

    async def persist_fill(
        self,
        *,
        fill: FillEvent,
        obligation: OrderObligation | None = None,
    ) -> bool:
        # 同 persist_order_state 的改造路径：把整个 sync session 块扔到线程池。
        # 成交写入是热路径里更频繁的那条——每笔交易至少一次，不释放 event loop
        # 的话 UI 仪表盘在忙时会看到明显的卡顿。
        saved = await asyncio.to_thread(
            self._persist_fill_sync,
            fill=fill,
            obligation=obligation,
        )
        if not saved:
            return False
        await self.flush_pending()
        latest_order_state = await asyncio.to_thread(
            self.execution_repo.get_order_state,
            fill.client_order_id,
        )
        # Stage 6 Slice 6.5：事务已 commit，obligation 广播到 cache。仅在
        # saved=True 时调：saved=False 说明 fill 被 dedup 了（重复 fill_id），
        # obligation 本身未被 persist，此时广播会把一份可能未写入 PG 的版本
        # 推到 cache，破坏 cache ↔ PG 最终一致性。
        self._publish_obligation_to_cache(obligation)
        self._publish_order_state_to_cache(latest_order_state)
        # P1-2：事务已 commit，fill 推到跨进程 cache。同 obligation，仅 saved=True
        # 时调：dedup 的 fill 不应该再推一遍。
        self._publish_fill_to_cache(fill)
        return True

    def _persist_fill_sync(
        self,
        *,
        fill: FillEvent,
        obligation: OrderObligation | None,
    ) -> bool:
        with self.session_factory() as session:
            saved = self.execution_repo.save_fill_in_session(session, fill)  # type: ignore[attr-defined]
            if not saved:
                session.rollback()
                return False
            self._ensure_execution_fill_row(session, fill=fill)
            if obligation is not None:
                self.obligation_repo.save_obligation_in_session(session, obligation)
            envelope = build_envelope(
                topic=topics.FILL_EVENTS,
                key=fill.symbol,
                payload_model=fill,
                source_component="execution_engine",
            )
            self.event_store.append_in_session(session, envelope)
            self.outbox_repo.enqueue_in_session(session, envelope)
            session.commit()
        return True

    # ── Fix P1-3：order_state + fills + obligations 原子持久化 ───────
    async def persist_order_state_with_fills(
        self,
        *,
        order_state: OrderState,
        key: str,
        fills: list[FillEvent],
        obligations_per_fill: list[OrderObligation | None],
        final_obligation: OrderObligation | None,
    ) -> OrderState:
        """当 adapter 同步返回 fills 时，在单事务内持久化 order_state + 所有
        fills + 链式 obligation 更新 + 终态 obligation finalization。消除
        多步 commit 之间的崩溃窗口。
        """
        persisted = await asyncio.to_thread(
            self._persist_order_state_with_fills_sync,
            order_state=order_state,
            key=key,
            fills=fills,
            obligations_per_fill=obligations_per_fill,
            final_obligation=final_obligation,
        )
        await self.flush_pending()
        # 广播 caches
        if final_obligation is not None:
            self._publish_obligation_to_cache(final_obligation)
        elif obligations_per_fill:
            last_obl = next((o for o in reversed(obligations_per_fill) if o is not None), None)
            self._publish_obligation_to_cache(last_obl)
        self._publish_order_state_to_cache(persisted)
        for fill in fills:
            self._publish_fill_to_cache(fill)
        return persisted

    def _persist_order_state_with_fills_sync(
        self,
        *,
        order_state: OrderState,
        key: str,
        fills: list[FillEvent],
        obligations_per_fill: list[OrderObligation | None],
        final_obligation: OrderObligation | None,
    ) -> OrderState:
        with self.session_factory() as session:
            persisted, previous = self.execution_repo.save_order_state_in_session(session, order_state)  # type: ignore[attr-defined]
            self._ensure_execution_order_row(session, order_state=persisted)
            envelopes: list[EventEnvelope] = [self._order_update_envelope(key=key, persisted=persisted)]
            summary = self._execution_error_summary(previous=previous, persisted=persisted)
            if summary is not None:
                envelopes.append(summary)
            # 逐 fill 写入同一 session
            for fill, obl in zip(fills, obligations_per_fill):
                saved = self.execution_repo.save_fill_in_session(session, fill)  # type: ignore[attr-defined]
                if saved:
                    self._ensure_execution_fill_row(session, fill=fill)
                    if obl is not None:
                        self.obligation_repo.save_obligation_in_session(session, obl)
                    envelopes.append(
                        build_envelope(
                            topic=topics.FILL_EVENTS,
                            key=fill.symbol,
                            payload_model=fill,
                            source_component="execution_engine",
                        )
                    )
            # 终态 obligation finalization
            if final_obligation is not None:
                self.obligation_repo.save_obligation_in_session(session, final_obligation)
            for envelope in envelopes:
                self.event_store.append_in_session(session, envelope)
                self.outbox_repo.enqueue_in_session(session, envelope)
            session.commit()
        return persisted

    def _publish_obligation_to_cache(self, obligation: OrderObligation | None) -> None:
        """Stage 6 Slice 6.5：事务 commit 成功后广播 obligation 到跨进程 cache。

        - ``obligation is None`` → noop（caller 本次没有 obligation 要写）
        - ``self.obligation_cache is None`` → noop（未接线，legacy path）
        - 异常 → log warning 不抛（I1 fail-soft）

        调用时机：**必须**在 session.commit() 成功之后。flush_pending() 之后 /
        之前都可以，只是 flush_pending 本身走 outbox NATS 广播的 order_update /
        fill envelope，与 OBLIGATION_UPDATES 是两条不同 topic，互不干扰。
        """
        if obligation is None or self.obligation_cache is None:
            return
        try:
            self.obligation_cache.fire_and_forget_publish(obligation)
        except Exception as exc:  # pragma: no cover
            log_event(
                self.logger,
                "execution_outbox_obligation_cache_publish_failed",
                level="warning",
                client_order_id=obligation.client_order_id,
                error_type=type(exc).__name__,
                error=str(exc),
            )

    def _publish_order_state_to_cache(self, order: OrderState | None) -> None:
        """P1-1：事务 commit 成功后推 order_state 到跨进程 cache。

        模板同 ``_publish_obligation_to_cache``：I1 fail-soft，异常只 warning。
        """
        if order is None or self.order_state_cache is None:
            return
        try:
            self.order_state_cache.fire_and_forget_publish(order)
        except Exception as exc:  # pragma: no cover
            log_event(
                self.logger,
                "execution_outbox_order_state_cache_publish_failed",
                level="warning",
                client_order_id=order.client_order_id,
                error_type=type(exc).__name__,
                error=str(exc),
            )

    def _publish_fill_to_cache(self, fill: FillEvent) -> None:
        """P1-2：事务 commit 成功后推 fill 到跨进程 cache。

        模板同 ``_publish_obligation_to_cache``：I1 fail-soft，异常只 warning。
        """
        if self.fill_event_cache is None:
            return
        try:
            self.fill_event_cache.fire_and_forget_publish(fill)
        except Exception as exc:  # pragma: no cover
            log_event(
                self.logger,
                "execution_outbox_fill_cache_publish_failed",
                level="warning",
                fill_id=fill.fill_id,
                error_type=type(exc).__name__,
                error=str(exc),
            )

    async def flush_pending(self, *, limit: int = 100) -> None:
        pending = await asyncio.to_thread(self.outbox_repo.pending, limit=limit)
        published_ids: list[str] = []
        for envelope in pending:
            try:
                # P1-16：硬超时保护。没有 wait_for 包裹时，NATS 连接 drain 挂起会把
                # 整条 flush 协程一起卡住。超时视为 publish 失败，让 envelope 走
                # record_failure_with_threshold，attempt_count 计满后进 FAILED(DLQ)。
                await asyncio.wait_for(
                    self.bus.publish_envelope(envelope, persist=False),
                    timeout=self._PUBLISH_TIMEOUT_SECONDS,
                )
                published_ids.append(envelope.event_id)
            except Exception as exc:
                if published_ids:
                    await asyncio.to_thread(self.outbox_repo.mark_published_batch, published_ids)
                    published_ids = []
                is_timeout = isinstance(exc, asyncio.TimeoutError)
                # R2-P2-E3：timeout 分支保留异常类型前缀，方便 grep/aggregation；
                # 与非 timeout 的 str(exc) 行为一致（后者通常也包含类型信息）。
                error_str = (
                    f"TimeoutError: publish_timeout_seconds={self._PUBLISH_TIMEOUT_SECONDS}"
                    if is_timeout
                    else str(exc)
                )
                status = await asyncio.to_thread(
                    self.outbox_repo.record_failure_with_threshold,
                    envelope.event_id,
                    error_str,
                    max_attempts=self._MAX_PUBLISH_ATTEMPTS,
                )
                # P1-16：FAILED 状态是 DLQ 的入口（pending() 不再重放），必须以
                # critical 级别告警让 ops 第一时间感知。非 FAILED 状态保持 error。
                failed_level = "critical" if status == "FAILED" else "error"
                log_event(
                    self.logger,
                    "execution_outbox_publish_failed",
                    level=failed_level,
                    topic=envelope.topic,
                    key=envelope.key,
                    event_id=envelope.event_id,
                    outbox_status=status,
                    is_timeout=is_timeout,
                    max_attempts=self._MAX_PUBLISH_ATTEMPTS,
                    error_type=type(exc).__name__,
                    error=error_str,
                )
                if status != "FAILED":
                    break  # FIFO: retriable failure blocks subsequent messages until resolved
        if published_ids:
            await asyncio.to_thread(self.outbox_repo.mark_published_batch, published_ids)

    @staticmethod
    def _seed_intent_from_command_payload(
        *,
        command_type: str | None,
        command_payload: dict[str, Any] | None,
    ) -> OrderIntent | None:
        if str(command_type or "").lower() != "submit":
            return None
        payload = dict(command_payload or {})
        intent_payload = payload.get("intent")
        if not isinstance(intent_payload, dict):
            return None
        try:
            return OrderIntent.model_validate(intent_payload)
        except Exception:
            return None

    @staticmethod
    def _order_update_envelope(*, key: str, persisted: OrderState) -> EventEnvelope:
        return build_envelope(
            topic=topics.ORDER_UPDATES,
            key=key,
            payload_model=persisted,
            source_component="execution_engine",
        )

    @staticmethod
    def _execution_error_summary(
        *,
        previous: OrderState | None,
        persisted: OrderState,
    ) -> EventEnvelope | None:
        if persisted.status not in {"FAILED", "REJECTED", "BLOCKED"}:
            return None
        if previous is not None and previous.status == persisted.status and previous.execution_error == persisted.execution_error:
            return None
        summary = ExecutionErrorSummary(
            subsystem="execution_engine",
            severity="error" if persisted.status == "FAILED" else "warning",
            message=persisted.execution_error or persisted.cancel_reason or persisted.status,
            decision_id=persisted.decision_id,
            intent_id=persisted.intent_id,
            order_id=persisted.client_order_id,
            status=persisted.status,
            observed_at=persisted.last_update_ts or persisted.created_at,
        )
        return build_envelope(
            topic=topics.EXECUTION_ERROR_SUMMARIES,
            key=persisted.symbol,
            payload_model=summary,
            source_component="execution_engine",
        )
