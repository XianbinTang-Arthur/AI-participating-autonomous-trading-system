"""DecisionAuditService —— 决策审计事件聚合。

P2-1 优化
=========
原实现：每条审计事件触发一次 ``asyncio.to_thread(audit_repo.upsert, record)``，
在高频决策周期中（19 个 handler × 每周期），产生大量独立 DB write。

优化方案：
1. **本地读缓存** ``_record_cache``：同一 decision_id 的后续 handler 命中缓存，
   消除重复 ``audit_repo.get()`` 的 DB SELECT。
2. **写缓冲 + 后台 flush** ``_write_buffer``：多条 upsert 合并为一次
   ``asyncio.to_thread`` 批量写。flush 间隔可配，默认 0.5s / 50 条上限。
3. 未调 ``start_batch_writer()`` 时退化为逐条写，兼容单元测试。
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict

from aats.bootstrap.logging import correlation_fields, get_logger, log_event
from aats.bus.base import EventBus
from aats.events import topics
from aats.events.envelopes import parse_envelope, publish_model
from aats.schemas.audit import DecisionAuditRecord
from aats.storage.base import AuditRepository

# 读缓存最大容量。超出后淘汰最老的 decision_id。
_MAX_RECORD_CACHE = 500


class DecisionAuditService:
    def __init__(
        self,
        *,
        bus: EventBus,
        audit_repo: AuditRepository,
        batch_flush_interval: float = 0.5,
        batch_max_size: int = 50,
    ) -> None:
        self.bus = bus
        self.audit_repo = audit_repo
        self.logger = get_logger("aats.audit")
        # P2-1: 本地读缓存——同一 decision_id 的后续 handler 命中缓存，
        # 避免重复 audit_repo.get() 的 DB 往返。
        self._record_cache: OrderedDict[str, DecisionAuditRecord] = OrderedDict()
        # P2-1: 写缓冲——多条 upsert 积攒后在一次 to_thread 内批量执行。
        self._write_buffer: list[DecisionAuditRecord] = []
        self._batch_flush_interval = batch_flush_interval
        self._batch_max_size = batch_max_size
        self._flush_task: asyncio.Task | None = None

    # ──────────────────────────────────────────────────────────────────
    # 生命周期
    # ──────────────────────────────────────────────────────────────────

    async def start_batch_writer(self) -> None:
        """启动后台 flush 任务。未调用时 _publish_record 退化为逐条写。"""
        if self._flush_task is None:
            self._flush_task = asyncio.create_task(
                self._flush_loop(), name="audit_batch_flush",
            )
            log_event(
                self.logger,
                "audit_batch_writer_started",
                flush_interval=self._batch_flush_interval,
                max_size=self._batch_max_size,
            )

    async def stop_batch_writer(self) -> None:
        """停止 flush 任务并刷入所有剩余缓冲。"""
        if self._flush_task is not None:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None
        # 最终 flush：确保所有缓冲中的 records 落盘
        await self._flush_once()
        log_event(
            self.logger,
            "audit_batch_writer_stopped",
            remaining_buffer=len(self._write_buffer),
            cache_size=len(self._record_cache),
        )

    # ──────────────────────────────────────────────────────────────────
    # 批量 flush 内部逻辑
    # ──────────────────────────────────────────────────────────────────

    async def _flush_loop(self) -> None:
        """后台定时 flush 任务。"""
        try:
            while True:
                await asyncio.sleep(self._batch_flush_interval)
                await self._flush_once()
        except asyncio.CancelledError:
            pass

    async def _flush_once(self) -> None:
        """将 _write_buffer 中积攒的 records 一次性写入 DB。"""
        if not self._write_buffer:
            return
        batch = self._write_buffer[:]
        self._write_buffer.clear()
        try:
            await asyncio.to_thread(self._write_batch_sync, batch)
        except Exception as exc:
            log_event(
                self.logger,
                "audit_batch_flush_failed",
                level="error",
                batch_size=len(batch),
                error_type=type(exc).__name__,
                error=str(exc),
            )
            # 失败的批次放回队首，下次 flush 重试
            self._write_buffer = batch + self._write_buffer

    def _write_batch_sync(self, batch: list[DecisionAuditRecord]) -> None:
        """在线程池中执行：将一批 records 写入 DB。

        尝试使用 ``upsert_batch``（单 session / 单 commit），否则逐条 fallback。
        """
        upsert_batch = getattr(self.audit_repo, "upsert_batch", None)
        if callable(upsert_batch):
            upsert_batch(batch)
        else:
            for record in batch:
                self.audit_repo.upsert(record)

    # ──────────────────────────────────────────────────────────────────
    # handle_* 事件 handlers（与原实现完全一致，只有底层 _fetch / _publish 变了）
    # ──────────────────────────────────────────────────────────────────

    async def handle_decision_context(self, message: dict) -> None:
        envelope = parse_envelope(message)
        decision_id = str(envelope.payload["decision_id"])
        record = DecisionAuditRecord(
            decision_id=decision_id,
            decision_context_ref=envelope.event_id,
        )
        await self._publish_record(record)

    async def handle_baseline_assessment(self, message: dict) -> None:
        await self._update_decision_record(
            message=message,
            ref_field="baseline_assessment_ref",
        )

    async def handle_ai_assessment(self, message: dict) -> None:
        await self._update_decision_record(
            message=message,
            ref_field="ai_market_assessment_ref",
        )

    async def handle_ai_decision_brief(self, message: dict) -> None:
        await self._update_decision_record(
            message=message,
            ref_field="ai_decision_brief_ref",
        )

    async def handle_ai_shadow_decision(self, message: dict) -> None:
        envelope = parse_envelope(message)
        decision_id = str(envelope.payload["decision_id"])
        record = await self._fetch_existing_record(decision_id)
        if envelope.event_id in record.ai_shadow_decision_refs:
            return
        updated = record.model_copy(
            update={"ai_shadow_decision_refs": [*record.ai_shadow_decision_refs, envelope.event_id]}
        )
        await self._publish_record(updated)

    async def handle_ai_shadow_evaluation(self, message: dict) -> None:
        envelope = parse_envelope(message)
        decision_ids = envelope.payload.get("decision_ids")
        if not isinstance(decision_ids, list):
            return
        for decision_id in decision_ids:
            if not isinstance(decision_id, str):
                continue
            record = await self._fetch_existing_record(decision_id)
            if envelope.event_id in record.ai_shadow_evaluation_refs:
                continue
            updated = record.model_copy(
                update={"ai_shadow_evaluation_refs": [*record.ai_shadow_evaluation_refs, envelope.event_id]}
            )
            await self._publish_record(updated)

    async def handle_strategy_coordinator_snapshot(self, message: dict) -> None:
        envelope = parse_envelope(message)
        decision_id = str(envelope.payload["decision_id"])
        record = await self._fetch_existing_record(decision_id)
        updated = record.model_copy(
            update={"strategy_coordinator_snapshot_ref": envelope.event_id}
        )
        await self._publish_record(updated)

    async def handle_strategy_sleeve_intent(self, message: dict) -> None:
        envelope = parse_envelope(message)
        decision_id = str(envelope.payload["decision_id"])
        record = await self._fetch_existing_record(decision_id)
        if envelope.event_id in record.strategy_sleeve_intent_refs:
            return
        updated = record.model_copy(
            update={"strategy_sleeve_intent_refs": [*record.strategy_sleeve_intent_refs, envelope.event_id]}
        )
        await self._publish_record(updated)

    async def handle_portfolio_allocation_decision(self, message: dict) -> None:
        envelope = parse_envelope(message)
        decision_id = str(envelope.payload["decision_id"])
        record = await self._fetch_existing_record(decision_id)
        updated = record.model_copy(
            update={
                "portfolio_allocation_decision_ref": envelope.event_id,
                "selected_strategy_sleeve_id": envelope.payload.get("primary_strategy_sleeve_id")
                or record.selected_strategy_sleeve_id,
                "allocation_id": envelope.payload.get("allocation_id") or record.allocation_id,
            }
        )
        await self._publish_record(updated)

    async def handle_position_target(self, message: dict) -> None:
        envelope = parse_envelope(message)
        decision_id = str(envelope.payload["decision_id"])
        record = await self._fetch_existing_record(decision_id)
        updated = record.model_copy(
            update={
                "position_target_ref": envelope.event_id,
                "selected_strategy_sleeve_id": envelope.payload.get("strategy_sleeve_id") or record.selected_strategy_sleeve_id,
                "allocation_id": envelope.payload.get("allocation_id") or record.allocation_id,
            }
        )
        await self._publish_record(updated)

    async def handle_decision_outcome(self, message: dict) -> None:
        envelope = parse_envelope(message)
        decision_id = str(envelope.payload["decision_id"])
        record = await self._fetch_existing_record(decision_id)
        updated = record.model_copy(
            update={
                "decision_outcome_ref": envelope.event_id,
                "selected_strategy_sleeve_id": envelope.payload.get("selected_strategy_sleeve_id") or record.selected_strategy_sleeve_id,
                "allocation_id": envelope.payload.get("allocation_id") or record.allocation_id,
            }
        )
        await self._publish_record(updated, flush_immediate=True)

    async def handle_policy_decision(self, message: dict) -> None:
        await self._update_decision_record(
            message=message,
            ref_field="policy_decision_ref",
        )

    async def handle_risk_decision(self, message: dict) -> None:
        await self._update_decision_record(
            message=message,
            ref_field="risk_decision_ref",
        )

    async def handle_execution_plan(self, message: dict) -> None:
        envelope = parse_envelope(message)
        decision_id = str(envelope.payload["decision_id"])
        record = await self._fetch_existing_record(decision_id)
        updates: dict[str, object] = {"execution_plan_ref": envelope.event_id}
        if envelope.event_id not in record.execution_plan_refs:
            updates["execution_plan_refs"] = [*record.execution_plan_refs, envelope.event_id]
        await self._publish_record(record.model_copy(update=updates))

    async def handle_strategy_execution_bundle(self, message: dict) -> None:
        envelope = parse_envelope(message)
        decision_id = str(envelope.payload["decision_id"])
        record = await self._fetch_existing_record(decision_id)
        updated = record.model_copy(
            update={
                "strategy_execution_bundle_ref": envelope.event_id,
                "selected_strategy_sleeve_id": envelope.payload.get("strategy_sleeve_id") or record.selected_strategy_sleeve_id,
                "allocation_id": envelope.payload.get("allocation_id") or record.allocation_id,
            }
        )
        await self._publish_record(updated, flush_immediate=True)

    async def handle_order_intent(self, message: dict) -> None:
        envelope = parse_envelope(message)
        decision_id = str(envelope.payload["decision_id"])
        record = await self._fetch_existing_record(decision_id)
        if envelope.event_id not in record.order_intent_refs:
            record = record.model_copy(
                update={"order_intent_refs": [*record.order_intent_refs, envelope.event_id]},
            )
            await self._publish_record(record, flush_immediate=True)

    async def handle_order_update(self, message: dict) -> None:
        envelope = parse_envelope(message)
        decision_id = str(envelope.payload["decision_id"])
        record = await self._fetch_existing_record(decision_id)
        if envelope.event_id in record.order_state_refs:
            return
        record = record.model_copy(
            update={"order_state_refs": [*record.order_state_refs, envelope.event_id]},
        )
        await self._publish_record(record)

    async def handle_fill_event(self, message: dict) -> None:
        envelope = parse_envelope(message)
        decision_id = str(envelope.payload["decision_id"])
        record = await self._fetch_existing_record(decision_id)
        if envelope.event_id not in record.fill_event_refs:
            record = record.model_copy(
                update={"fill_event_refs": [*record.fill_event_refs, envelope.event_id]},
            )
            await self._publish_record(record)

    async def handle_portfolio_snapshot(self, message: dict) -> None:
        envelope = parse_envelope(message)
        decision_id = envelope.payload.get("decision_id")
        if not isinstance(decision_id, str):
            return
        record = await self._fetch_existing_record(decision_id)
        if record.portfolio_delta_ref == envelope.event_id and envelope.event_id in record.portfolio_delta_refs:
            return
        portfolio_delta_refs = list(record.portfolio_delta_refs)
        if envelope.event_id not in portfolio_delta_refs:
            portfolio_delta_refs.append(envelope.event_id)
        updated = record.model_copy(
            update={
                "portfolio_delta_ref": envelope.event_id,
                "portfolio_delta_refs": portfolio_delta_refs,
            }
        )
        await self._publish_record(updated)

    async def handle_reconciliation_report(self, message: dict) -> None:
        envelope = parse_envelope(message)
        decision_id = envelope.payload.get("decision_id")
        if not isinstance(decision_id, str):
            return
        record = await self._fetch_existing_record(decision_id)
        report_snapshot_ref = envelope.payload.get("portfolio_snapshot_ref")
        valid_snapshot_refs = set(record.portfolio_delta_refs)
        if record.portfolio_delta_ref is not None:
            valid_snapshot_refs.add(record.portfolio_delta_ref)
        if (
            isinstance(report_snapshot_ref, str)
            and valid_snapshot_refs
            and report_snapshot_ref not in valid_snapshot_refs
        ):
            raise RuntimeError(
                "Reconciliation report snapshot reference does not match audit-linked portfolio snapshot "
                f"for decision_id={decision_id}"
            )
        if envelope.event_id in record.reconciliation_refs:
            return
        updated = record.model_copy(
            update={"reconciliation_refs": [*record.reconciliation_refs, envelope.event_id]},
        )
        await self._publish_record(updated)

    # ──────────────────────────────────────────────────────────────────
    # 内部读写路径
    # ──────────────────────────────────────────────────────────────────

    async def _fetch_existing_record(self, decision_id: str) -> DecisionAuditRecord:
        # P2-1: 先查本地缓存——同一决策周期内多次 handle_* 只命中一次 DB
        cached = self._record_cache.get(decision_id)
        if cached is not None:
            return cached
        # 缓存未命中，走 DB。_existing_record 既要 DB get 又可能写一次
        # synthetic seed，作为一个原子单元丢到线程池。
        record = await asyncio.to_thread(self._existing_record, decision_id)
        self._cache_record(record)
        return record

    def _existing_record(self, decision_id: str) -> DecisionAuditRecord:
        record = self.audit_repo.get(decision_id)
        if record is None:
            record = DecisionAuditRecord(
                decision_id=decision_id,
                decision_context_ref=f"synthetic_execution_seed:{decision_id}",
            )
            self.audit_repo.upsert(record)
            log_event(
                self.logger,
                "decision_audit_synthetic_seeded",
                level="warning",
                **correlation_fields(
                    decision_id=decision_id,
                    reason="missing_audit_record_seeded_from_execution_flow",
                ),
            )
        return record

    async def _update_decision_record(self, *, message: dict, ref_field: str) -> None:
        envelope = parse_envelope(message)
        decision_id = str(envelope.payload["decision_id"])
        record = await self._fetch_existing_record(decision_id)
        updated = record.model_copy(update={ref_field: envelope.event_id})
        await self._publish_record(updated)

    async def _publish_record(
        self, record: DecisionAuditRecord, *, flush_immediate: bool = False,
    ) -> None:
        # P2-1: 更新本地读缓存
        self._cache_record(record)
        if self._flush_task is not None:
            # 批量模式：入队缓冲，到达上限时立即 flush
            self._write_buffer.append(record)
            # P2-2 fix: 关键决策边界事件（execution bundle / decision outcome /
            # order intent）设 flush_immediate=True，立即将整个 buffer flush 落盘，
            # 消除 operator 查询在 0.5s flush 窗口内读到 stale 数据的风险。
            if flush_immediate or len(self._write_buffer) >= self._batch_max_size:
                await self._flush_once()
        else:
            # 非批量模式（测试 / 未调 start_batch_writer）：立即写，兼容原行为
            await asyncio.to_thread(self.audit_repo.upsert, record)
        log_event(
            self.logger,
            "decision_audit_updated",
            level="debug",
            **correlation_fields(
                decision_id=record.decision_id,
                execution_plan_ref=record.execution_plan_ref,
                execution_plan_ref_count=len(record.execution_plan_refs),
                strategy_execution_bundle_ref=record.strategy_execution_bundle_ref,
                strategy_coordinator_snapshot_ref=record.strategy_coordinator_snapshot_ref,
                strategy_sleeve_intent_ref_count=len(record.strategy_sleeve_intent_refs),
                portfolio_allocation_decision_ref=record.portfolio_allocation_decision_ref,
                portfolio_delta_ref_count=len(record.portfolio_delta_refs),
                order_intent_ref_count=len(record.order_intent_refs),
                order_state_ref_count=len(record.order_state_refs),
                fill_event_ref_count=len(record.fill_event_refs),
                ai_shadow_decision_ref_count=len(record.ai_shadow_decision_refs),
                ai_shadow_evaluation_ref_count=len(record.ai_shadow_evaluation_refs),
                reconciliation_ref_count=len(record.reconciliation_refs),
            ),
        )
        await publish_model(
            bus=self.bus,
            topic=topics.AUDIT_RECORDS,
            key=record.decision_id,
            payload_model=record,
            source_component="audit_service",
        )

    def _cache_record(self, record: DecisionAuditRecord) -> None:
        """更新本地读缓存，FIFO 淘汰超出容量的旧条目。"""
        did = record.decision_id
        if did in self._record_cache:
            self._record_cache.move_to_end(did)
        self._record_cache[did] = record
        while len(self._record_cache) > _MAX_RECORD_CACHE:
            self._record_cache.popitem(last=False)
