from __future__ import annotations

import asyncio
from decimal import Decimal
from datetime import timedelta, timezone
from typing import Callable

from aats.bootstrap.settings import AATSSettings
from aats.bootstrap.logging import correlation_fields, get_logger, log_event
from aats.bootstrap.telemetry import start_span
from aats.bus.base import EventBus
from aats.events import topics
from aats.events.envelopes import parse_payload, publish_model
from aats.schemas.common import new_id, utc_now
from aats.schemas.execution import (
    FillEvent,
    LegOrderIntent,
    OrderIntent,
    OrderObligation,
    OrderState,
    effective_close_only_for_intent,
    effective_reduce_only_for_intent,
    execution_attempt_id_from_components,
    leg_intent_from_order_intent,
    order_intent_from_leg_order_intent,
)
from aats.schemas.operator import ExecutionErrorSummary
from aats.services.execution_control.order_service import ExecutionOrderService
from aats.services.execution_control.order_state_machine import OrderStateMachine
from aats.services.execution_control.shadow import Phase1ExecutionShadowService
from aats.services.execution_engine.bundle_status import (
    apply_strategy_bundle_status_reason_codes,
    derive_strategy_bundle_status,
)
from aats.services.execution_engine.exchange_adapter import ExchangeAdapter
from aats.services.execution_engine.exit_intent_aggregator import (
    child_exit_order_ref_from_order_state,
    clear_resume_issue,
    create_exit_execution_intent_from_order_intent,
    create_exit_execution_intent_from_order_state,
    dispatch_template_from_parent,
    record_resume_issue,
    refresh_exit_execution_intents,
    recompute_exit_execution_intent,
    resume_block_reason,
    request_cancel_exit_execution_intent,
)
from aats.services.execution_engine.exit_execution_writer import ExitExecutionWriter
from aats.services.execution_engine.obligations import ExecutionObligationService, ExecutionReservationError
from aats.services.execution_engine.order_truth import (
    blocks_new_risk_actions,
    is_risk_reducing_order_intent,
    is_risk_reducing_order_state,
    is_unknown_write_state,
    unknown_write_operation,
)
from aats.services.execution_engine.outbox import PostgresExecutionOutboxPublisher
from aats.services.execution_engine.state_writer import (
    save_fill_direct_legacy_only,
    save_order_state_direct_legacy_only,
)
from aats.services.governance_engine.kill_switch import KillSwitch
from aats.services.ledger.posting import Phase1LedgerMirrorService
from aats.services.strategy_overlay_rollout import overlay_mode_from_execution_mode, overlay_rollout_status
from aats.storage.base import ExecutionRepository, ExitExecutionRepository
from aats.storage.execution_fill_repo_v2 import ExecutionFillRepositoryV2
from aats.storage.execution_order_repo import ExecutionOrderHistoryRepository, ExecutionOrderRepository


class OrderManager:
    _OBLIGATION_ATOMIC_FINALIZE_EPSILON = Decimal("1e-12")
    _FILL_BACKFILL_RECENT_LIMIT = 100
    _FILL_BACKFILL_TERMINAL_STATUSES = ("FILLED", "CANCELED", "EXPIRED")
    _SEMANTIC_DUPLICATE_RECENT_LIMIT = 100
    _RISK_INCREASE_CONVERGENCE_RECENT_LIMIT = 100
    _RISK_INCREASE_CONVERGENCE_RECENT_SECONDS = 300
    _RISK_INCREASE_CONVERGENCE_LANES = {"long_increase", "short_increase"}
    _SEMANTIC_DUPLICATE_NO_EFFECT_STATUSES = {
        "BLOCKED",
        "CANCELED",
        "CANCELLED",
        "DRY_RUN",
        "EXPIRED",
        "FAILED",
        "REJECTED",
    }
    _TRANSIENT_RETRY_PATTERNS = ("50013", "systems are busy", "service busy", "temporarily unavailable")
    _EXIT_SPLIT_MAX_CHILDREN = 32

    def __init__(
        self,
        *,
        settings: AATSSettings,
        bus: EventBus,
        adapter: ExchangeAdapter,
        execution_repo: ExecutionRepository,
        exit_execution_repo: ExitExecutionRepository | None = None,
        exit_execution_writer: ExitExecutionWriter | None = None,
        obligation_service: ExecutionObligationService | None = None,
        execution_outbox_publisher: PostgresExecutionOutboxPublisher | None = None,
        persistent_order_service: ExecutionOrderService | None = None,
        shadow_execution_service: Phase1ExecutionShadowService | None = None,
        shadow_execution_order_repo: ExecutionOrderRepository | None = None,
        shadow_execution_order_history_repo: ExecutionOrderHistoryRepository | None = None,
        shadow_execution_fill_repo: ExecutionFillRepositoryV2 | None = None,
        shadow_ledger_mirror_service: Phase1LedgerMirrorService | None = None,
        leg_risk_evaluator: Callable[[LegOrderIntent], object] | None = None,
        strategy_runtime_repo=None,
        kill_switch: KillSwitch,
    ) -> None:
        self.settings = settings
        self.bus = bus
        self.adapter = adapter
        self.execution_repo = execution_repo
        self.exit_execution_repo = exit_execution_repo
        self.exit_execution_writer = (
            exit_execution_writer
            if exit_execution_writer is not None
            else ExitExecutionWriter(exit_execution_repo)
            if exit_execution_repo is not None
            else None
        )
        self.obligation_service = obligation_service
        self.execution_outbox_publisher = execution_outbox_publisher
        self.persistent_order_service = persistent_order_service
        self.shadow_execution_service = shadow_execution_service or self._build_legacy_shadow_execution_service(
            shadow_execution_order_repo=shadow_execution_order_repo,
            shadow_execution_order_history_repo=shadow_execution_order_history_repo,
            shadow_execution_fill_repo=shadow_execution_fill_repo,
        )
        self.shadow_ledger_mirror_service = shadow_ledger_mirror_service
        self.leg_risk_evaluator = leg_risk_evaluator
        self.strategy_runtime_repo = strategy_runtime_repo
        self.kill_switch = kill_switch
        self.order_state_machine = OrderStateMachine()
        # Serialize reservation preview/persist so concurrent intents cannot over-reserve the same balance window.
        self._reservation_lock = asyncio.Lock()
        # Per-symbol lock to prevent concurrent submissions for the same trading pair.
        self._symbol_locks: dict[str, asyncio.Lock] = {}
        self.logger = get_logger("aats.execution_engine")

    async def handle_order_intent(self, message: dict) -> None:
        intent = parse_payload(message, OrderIntent)
        # Stage 8：execution engine 的入口 span。父 span 由 NatsEventBus._on_msg
        # 通过 envelope.trace_context 提取的 decision_engine.run_cycle 提供；
        # Jaeger 里会看到 decision → execution 的跨进程 trace chain。
        # 设计文档：docs/task/stage_8_otel_integration_design.md §D5
        with start_span(
            "execution_engine.handle_order_intent",
            attributes={
                "aats.intent_id": intent.intent_id,
                "aats.decision_id": intent.decision_id,
                "aats.symbol": intent.symbol,
                "aats.side": str(intent.side),
                "aats.quantity": str(intent.quantity),
            },
        ):
            await self._handle_normalized_order_intent(
                intent=intent,
                leg_intent=leg_intent_from_order_intent(intent),
            )

    async def handle_leg_order_intent(self, message: dict) -> None:
        leg_intent = parse_payload(message, LegOrderIntent)
        with start_span(
            "execution_engine.handle_leg_order_intent",
            attributes={
                "aats.leg_intent_id": getattr(leg_intent, "leg_intent_id", ""),
                "aats.symbol": getattr(leg_intent, "symbol", ""),
            },
        ):
            await self.submit_leg_order(leg_intent=leg_intent)

    async def submit_leg_order(self, *, leg_intent: LegOrderIntent) -> None:
        await self._handle_normalized_order_intent(
            intent=order_intent_from_leg_order_intent(leg_intent),
            leg_intent=leg_intent,
        )

    async def _handle_normalized_order_intent(
        self,
        *,
        intent: OrderIntent,
        leg_intent: LegOrderIntent | None = None,
    ) -> None:
        # P0-4：真金白银幂等防线。idempotency_key 驱动 clOrdId 的 SHA256 摘要，
        # 任何空值意味着"随机 clOrdId 会被生成",OKX 会把重试视为新订单 → 重复下单。
        # 在唯一入口处拒单而非后续 fallback 到 new_id("clord")，让问题在源头暴露。
        if not intent.idempotency_key or not str(intent.idempotency_key).strip():
            log_event(
                self.logger,
                "order_intent_missing_idempotency_key",
                level="critical",
                **correlation_fields(
                    decision_id=intent.decision_id,
                    intent_id=intent.intent_id,
                    symbol=intent.symbol,
                ),
            )
            blocked_state = self._blocked_order_state_from_intent(
                intent=intent,
                client_order_id=intent.intent_id or new_id("clord"),
                submission_mode="blocked_missing_idempotency_key",
                execution_error="missing_idempotency_key",
            )
            await self._persist_order_state(
                order_state=blocked_state, key=intent.symbol, intent=intent,
            )
            return
        # 去重检查优先于 kill_switch：已入库的 intent（无论之前是 BLOCKED/FILLED/…）
        # 不需要再次写 DB，避免 halted 期间重复 upsert。
        if self.execution_repo.has_intent(intent.intent_id):
            log_event(
                self.logger,
                "duplicate_order_intent_ignored",
                level="warning",
                **correlation_fields(
                    decision_id=intent.decision_id,
                    intent_id=intent.intent_id,
                    symbol=intent.symbol,
                ),
            )
            return
        if self.kill_switch.halted:
            # Fix P2-6：持久化 BLOCKED 状态，保留审计记录和幂等保护。
            blocked_state = self._blocked_order_state_from_intent(
                intent=intent,
                client_order_id=intent.idempotency_key or new_id("clord"),
                submission_mode="kill_switch_blocked",
                execution_error="kill_switch_active",
            )
            await self._persist_order_state(
                order_state=blocked_state, key=intent.symbol, intent=intent,
            )
            log_event(
                self.logger,
                "order_intent_blocked",
                level="warning",
                **correlation_fields(
                    decision_id=intent.decision_id,
                    intent_id=intent.intent_id,
                    symbol=intent.symbol,
                    reason="kill_switch_active",
                ),
            )
            return
        cooldown_state = self._transient_close_retry_cooldown_state(intent=intent)
        if cooldown_state is not None:
            await self._persist_order_state(order_state=cooldown_state, key=intent.symbol)
            return

        log_event(
            self.logger,
            "order_intent_received",
            **correlation_fields(
                decision_id=intent.decision_id,
                intent_id=intent.intent_id,
                symbol=intent.symbol,
                side=intent.side,
                quantity=intent.quantity,
            ),
        )
        symbol_lock = self._symbol_locks.setdefault(intent.symbol, asyncio.Lock())
        async with symbol_lock:
            await self._execute_guarded_order_intent(intent=intent, leg_intent=leg_intent)

    async def _execute_guarded_order_intent(
        self,
        *,
        intent: OrderIntent,
        leg_intent: LegOrderIntent | None = None,
    ) -> None:
        preview_client_order_id_fn = getattr(self.adapter, "preview_client_order_id", None)
        preview_client_order_id = (
            preview_client_order_id_fn(intent)
            if callable(preview_client_order_id_fn)
            else None
        ) or intent.idempotency_key or new_id("clord")
        intent, leg_intent = self._apply_execution_attempt_id(
            intent=intent,
            client_order_id=preview_client_order_id,
            leg_intent=leg_intent,
        )
        initial_obligation = None
        async with self._reservation_lock:
            if self.execution_repo.has_intent(intent.intent_id):
                log_event(
                    self.logger,
                    "duplicate_order_intent_ignored",
                    level="warning",
                    **correlation_fields(
                        decision_id=intent.decision_id,
                        intent_id=intent.intent_id,
                        symbol=intent.symbol,
                    ),
                )
                return
            semantic_duplicate_block = self._semantic_duplicate_snapshot_submit_block(
                intent=intent,
                client_order_id=preview_client_order_id,
            )
            if semantic_duplicate_block is not None:
                await self._persist_order_state(
                    order_state=semantic_duplicate_block,
                    key=intent.symbol,
                    intent=intent,
                )
                return
            convergence_block = self._risk_increase_convergence_submit_block(
                intent=intent,
                client_order_id=preview_client_order_id,
            )
            if convergence_block is not None:
                await self._persist_order_state(
                    order_state=convergence_block,
                    key=intent.symbol,
                    intent=intent,
                )
                return
            intent, leg_intent, blocked_state = self._apply_leg_submit_guards(
                intent=intent,
                client_order_id=preview_client_order_id,
                leg_intent=leg_intent,
            )
            if blocked_state is not None:
                await self._persist_order_state(order_state=blocked_state, key=intent.symbol)
                return
            unknown_write_block = self._unknown_write_submit_block(
                intent=intent,
                client_order_id=preview_client_order_id,
            )
            if unknown_write_block is not None:
                await self._persist_order_state(order_state=unknown_write_block, key=intent.symbol)
                return
            try:
                if self.obligation_service is not None:
                    initial_obligation = await self.obligation_service.preview_reservation_for_intent(
                        intent=intent,
                        client_order_id=preview_client_order_id,
                    )
            except ExecutionReservationError as exc:
                blocked_state = OrderState(
                    decision_id=intent.decision_id,
                    execution_chain_id=intent.execution_chain_id,
                    execution_attempt_id=intent.execution_attempt_id,
                    intent_id=intent.intent_id,
                    symbol=intent.symbol,
                    client_order_id=preview_client_order_id,
                    venue="OKX" if self.adapter.readiness().get("backend") == "okx" else "PAPER",
                    exchange_order_id=None,
                    status="BLOCKED",
                    submission_mode="local_order_manager",
                    submitted_ts=None,
                    last_update_ts=utc_now(),
                    requested_qty=intent.quantity,
                    filled_qty=Decimal("0"),
                    remaining_qty=intent.quantity,
                    average_fill_price=None,
                    fees=Decimal("0"),
                    reduce_only=effective_reduce_only_for_intent(intent),
                    close_only=effective_close_only_for_intent(intent),
                    td_mode=intent.td_mode,
                    position_mode=intent.position_mode,
                    pos_side=intent.pos_side,
                    reduce_only_reason=intent.reduce_only_reason,
                    close_only_reason=intent.close_only_reason,
                    instrument_family=intent.instrument_family,
                    settle_currency=intent.settle_currency,
                    product_type=intent.product_type,
                    target_leverage=intent.target_leverage,
                    margin_mode=intent.margin_mode,
                    exposure_side=intent.exposure_side,
                    execution_action=intent.execution_action,
                    leg_action=intent.leg_action,
                    position_intent=intent.position_intent,
                    leg_intent_id=intent.leg_intent_id,
                    strategy_family=intent.strategy_family,
                    strategy_sleeve_id=intent.strategy_sleeve_id,
                    allocation_id=intent.allocation_id,
                    strategy_bundle_id=intent.strategy_bundle_id,
                    strategy_leg_role=intent.strategy_leg_role,
                    strategy_pair_id=intent.strategy_pair_id,
                    strategy_opportunity_kind=intent.strategy_opportunity_kind,
                    strategy_execution_mode=intent.strategy_execution_mode,
                    strategy_state_phase=intent.strategy_state_phase,
                    market_snapshot_ref=intent.market_snapshot_ref,
                    feature_snapshot_ref=intent.feature_snapshot_ref,
                    portfolio_snapshot_ref=intent.portfolio_snapshot_ref,
                    health_snapshot_ref=intent.health_snapshot_ref,
                    execution_error=str(exc),
                    submission_payload={},
                )
                await self._persist_order_state(order_state=blocked_state, key=intent.symbol)
                return
            created_state = OrderState(
                decision_id=intent.decision_id,
                execution_chain_id=intent.execution_chain_id,
                execution_attempt_id=intent.execution_attempt_id,
                intent_id=intent.intent_id,
                symbol=intent.symbol,
                client_order_id=preview_client_order_id,
                venue="OKX" if self.adapter.readiness().get("backend") == "okx" else "PAPER",
                exchange_order_id=None,
                status="CREATED",
                submission_mode="local_order_manager",
                submitted_ts=None,
                last_update_ts=utc_now(),
                requested_qty=intent.quantity,
                filled_qty=Decimal("0"),
                remaining_qty=intent.quantity,
                average_fill_price=None,
                fees=Decimal("0"),
                reduce_only=effective_reduce_only_for_intent(intent),
                close_only=effective_close_only_for_intent(intent),
                td_mode=intent.td_mode,
                position_mode=intent.position_mode,
                pos_side=intent.pos_side,
                reduce_only_reason=intent.reduce_only_reason,
                close_only_reason=intent.close_only_reason,
                instrument_family=intent.instrument_family,
                settle_currency=intent.settle_currency,
                product_type=intent.product_type,
                target_leverage=intent.target_leverage,
                margin_mode=intent.margin_mode,
                exposure_side=intent.exposure_side,
                execution_action=intent.execution_action,
                leg_action=intent.leg_action,
                position_intent=intent.position_intent,
                leg_intent_id=intent.leg_intent_id,
                strategy_family=intent.strategy_family,
                strategy_sleeve_id=intent.strategy_sleeve_id,
                allocation_id=intent.allocation_id,
                strategy_bundle_id=intent.strategy_bundle_id,
                strategy_leg_role=intent.strategy_leg_role,
                strategy_pair_id=intent.strategy_pair_id,
                strategy_opportunity_kind=intent.strategy_opportunity_kind,
                strategy_execution_mode=intent.strategy_execution_mode,
                strategy_state_phase=intent.strategy_state_phase,
                market_snapshot_ref=intent.market_snapshot_ref,
                feature_snapshot_ref=intent.feature_snapshot_ref,
                portfolio_snapshot_ref=intent.portfolio_snapshot_ref,
                health_snapshot_ref=intent.health_snapshot_ref,
                submission_payload={},
            )
            submit_command = None
            if self.persistent_order_service is not None:
                submit_command = {
                    "command_id": new_id("cmd"),
                    "command_type": "submit",
                    "idempotency_key": self.persistent_order_service.submit_command_idempotency_key(
                        created_state.client_order_id
                    ),
                    "payload": self.persistent_order_service.submit_command_payload(
                        intent=intent,
                        client_order_id=created_state.client_order_id,
                    ),
                    "created_at": utc_now(),
                }
            created_state = await self._persist_order_state(
                order_state=created_state,
                key=intent.symbol,
                obligation=initial_obligation,
                intent=intent,
                command=submit_command,
            )
            if (
                self.obligation_service is not None
                and self.execution_outbox_publisher is None
                and initial_obligation is not None
            ):
                self.obligation_service.persist_previewed_obligation(initial_obligation)
        self._shadow_sync_obligation(initial_obligation, reason="reservation_hold", related_fill=None)
        if self.persistent_order_service is not None:
            if not self._submit_command_persisted_transactionally():
                try:
                    self.persistent_order_service.enqueue_submit(
                        intent=intent,
                        client_order_id=created_state.client_order_id,
                    )
                except Exception as exc:
                    failed_state = created_state.model_copy(
                        update={
                            "status": "FAILED",
                            "submission_mode": "phase2_enqueue_failed",
                            "submitted_ts": utc_now(),
                            "last_update_ts": utc_now(),
                            "cancel_reason": str(exc),
                            "execution_error": str(exc),
                        }
                    )
                    failed_state = await self._persist_order_state(
                        order_state=failed_state,
                        key=intent.symbol,
                        obligation=self._terminal_outbox_obligation(order_state=failed_state, fills=[]),
                    )
                    self._finalize_obligation(order_state=failed_state)
                    raise
            return
        await self.process_submit_command(
            intent=intent,
            client_order_id=created_state.client_order_id,
            leg_intent=leg_intent,
        )

    async def process_submit_command(
        self,
        *,
        intent: OrderIntent,
        client_order_id: str | None = None,
        leg_intent: LegOrderIntent | None = None,
    ) -> OrderState:
        # P0-4：命令流入口也需校验 idempotency_key，否则 recovery/retry 路径
        # 可能绕过 _handle_normalized_order_intent 的校验直接走到 submit。
        idempotency_key_missing = (
            not intent.idempotency_key or not str(intent.idempotency_key).strip()
        )
        if client_order_id is None and idempotency_key_missing:
            log_event(
                self.logger,
                "process_submit_command_missing_idempotency_key",
                level="critical",
                **correlation_fields(
                    decision_id=intent.decision_id,
                    intent_id=intent.intent_id,
                    symbol=intent.symbol,
                ),
            )
            blocked_state = self._blocked_order_state_from_intent(
                intent=intent,
                client_order_id=intent.intent_id or new_id("clord"),
                submission_mode="blocked_missing_idempotency_key",
                execution_error="missing_idempotency_key",
            )
            return await self._persist_order_state(order_state=blocked_state, key=intent.symbol)
        if client_order_id is not None:
            current = self.execution_repo.get_order_state(client_order_id)
            if current is not None and current.status not in {"CREATED", "SUBMITTING"}:
                return current
        resolved_client_order_id = client_order_id or intent.idempotency_key or new_id("clord")
        intent, leg_intent = self._apply_execution_attempt_id(
            intent=intent,
            client_order_id=resolved_client_order_id,
            leg_intent=leg_intent,
        )
        semantic_duplicate_block = self._semantic_duplicate_snapshot_submit_block(
            intent=intent,
            client_order_id=resolved_client_order_id,
        )
        if semantic_duplicate_block is not None:
            return await self._persist_order_state(
                order_state=semantic_duplicate_block,
                key=intent.symbol,
                intent=intent,
            )
        convergence_block = self._risk_increase_convergence_submit_block(
            intent=intent,
            client_order_id=resolved_client_order_id,
        )
        if convergence_block is not None:
            return await self._persist_order_state(
                order_state=convergence_block,
                key=intent.symbol,
                intent=intent,
            )
        intent, leg_intent, blocked_state = self._apply_leg_submit_guards(
            intent=intent,
            client_order_id=resolved_client_order_id,
            leg_intent=leg_intent,
        )
        if blocked_state is not None:
            return await self._persist_order_state(order_state=blocked_state, key=intent.symbol)
        unknown_write_block = self._unknown_write_submit_block(
            intent=intent,
            client_order_id=resolved_client_order_id,
        )
        if unknown_write_block is not None:
            return await self._persist_order_state(order_state=unknown_write_block, key=intent.symbol)
        return await self._execute_submit_intent(
            intent=intent,
            client_order_id=resolved_client_order_id,
            leg_intent=leg_intent,
        )

    async def process_cancel_command(self, *, client_order_id: str) -> OrderState:
        current = self.resolve_order_state_for_control(client_order_id)
        if current is None:
            raise KeyError(f"order_state_not_found client_order_id={client_order_id}")
        if self.order_state_machine.is_terminal(current.status):
            return current
        return await self._execute_cancel_from_state(current)

    async def _execute_submit_intent(
        self,
        *,
        intent: OrderIntent,
        client_order_id: str | None = None,
        leg_intent: LegOrderIntent | None = None,
    ) -> OrderState:
        resolved_client_order_id = client_order_id or intent.idempotency_key or new_id("clord")
        intent, leg_intent = self._apply_execution_attempt_id(
            intent=intent,
            client_order_id=resolved_client_order_id,
            leg_intent=leg_intent,
        )
        await self._persist_submitting_state_for_intent(
            intent=intent,
            client_order_id=resolved_client_order_id,
        )
        # post_only_with_timeout_fallback (Layer 4, 2026-04-21):
        # execution_style="post_only" 是来自 planner 的信号 (见 §3.4).
        # post_only 自带 orchestration: submit → 等 timeout → 若仍未成交则
        # cancel + fallback 重下 remaining_qty (走 bounded_taker 路径).
        # 优先于 serial_exit_split: 如果 fallback intent 触发 split, 由 fallback
        # 的 _execute_submit_intent 递归处理.
        if self._intent_signals_post_only(intent):
            return await self._execute_post_only_with_timeout_fallback(
                intent=intent,
                client_order_id=resolved_client_order_id,
                leg_intent=leg_intent,
            )
        split_limit = await self._serial_exit_split_limit(intent=intent)
        if split_limit is not None:
            # Task 142：split 统一返回 OrderState（或 raise
            # serial_exit_split_missing_anchor_state），caller 不再做 3 段式
            # workaround。anchor state 由上面的 _persist_submitting_state_for_intent
            # 保证存在，split 内部 early-return 会用 fallback helper 兜底。
            return await self._execute_serial_exit_split(
                intent=intent,
                client_order_id=resolved_client_order_id,
                leg_intent=leg_intent,
                split_limit=split_limit,
            )
        return await self._submit_single_order_intent(
            intent=intent,
            client_order_id=resolved_client_order_id,
            leg_intent=leg_intent,
        )

    @staticmethod
    def _intent_signals_post_only(intent: OrderIntent) -> bool:
        """post_only_with_timeout_fallback (2026-04-21): execution_style 是
        post_only 的唯一意图信号 (OrderIntent.order_type Literal 不含 post_only).
        见 docs/design/post_only_maker_exit_mode_2026_04_21.md §3.4
        """
        return str(getattr(intent, "execution_style", "") or "").strip().lower() == "post_only"

    async def _execute_post_only_with_timeout_fallback(
        self,
        *,
        intent: OrderIntent,
        client_order_id: str,
        leg_intent: LegOrderIntent | None,
    ) -> OrderState:
        """post_only orchestration (Layer 4, 2026-04-21).

        Sequence (见 docs/design/post_only_maker_exit_mode_2026_04_21.md §3.5):
          1. _submit_single_order_intent(post_only) → OrderState
          2. 若立即终态 (FILLED / FAILED / REJECTED / CANCELED / EXPIRED) → return
             - sCode!=0 (OKX 拒绝, 通常 51005 will fill immediately) → 立即 fallback
          3. asyncio.sleep(timeout_ms)
          4. 重新读 order_state. 若已终态 → return
          5. 若 remaining_qty <= epsilon → return (state machine 即将终结)
          6. cancel_order(client_order_id) → 等取消完成
          7. 用 remaining_qty 构造 fallback intent (bounded_taker), 递归 _execute_submit_intent

        Fallback 走现有 bounded_taker 路径, 计费按 taker (fee_resolver 不给 maker 折扣).
        所有 fallback 事件记 log_event 作为 post-deploy evidence.
        """
        post_only_state = await self._submit_single_order_intent(
            intent=intent,
            client_order_id=client_order_id,
            leg_intent=leg_intent,
        )
        # 立即终态 (含 OKX REJECTED) → 触发 fallback 或直接返回
        if self.order_state_machine.is_terminal(post_only_state.status):
            if post_only_state.status in {"REJECTED", "FAILED"}:
                # post_only 被 OKX 拒绝 (e.g., sCode 51116 跨价) → 立即 fallback
                log_event(
                    self.logger,
                    "post_only_rejected_immediate_fallback",
                    **correlation_fields(
                        decision_id=intent.decision_id,
                        intent_id=intent.intent_id,
                        symbol=intent.symbol,
                        client_order_id=client_order_id,
                        rejection_status=post_only_state.status,
                        rejection_reason=post_only_state.cancel_reason or post_only_state.execution_error,
                    ),
                )
                return await self._submit_post_only_fallback(
                    original_intent=intent,
                    original_leg_intent=leg_intent,
                    remaining_qty=post_only_state.remaining_qty or intent.quantity,
                    trigger="rejected",
                )
            return post_only_state

        # 等待 timeout
        timeout_ms = self._post_only_timeout_ms()
        await asyncio.sleep(max(timeout_ms, 0.0) / 1000.0)

        # 重读最新 state (sync_exchange_state polling loop 已可能更新)
        refreshed = self.execution_repo.get_order_state(client_order_id) or post_only_state
        if self.order_state_machine.is_terminal(refreshed.status):
            log_event(
                self.logger,
                "post_only_terminal_within_timeout",
                **correlation_fields(
                    decision_id=intent.decision_id,
                    intent_id=intent.intent_id,
                    symbol=intent.symbol,
                    client_order_id=client_order_id,
                    final_status=refreshed.status,
                    filled_qty=str(refreshed.filled_qty),
                ),
            )
            return refreshed

        remaining_qty = max(
            Decimal(refreshed.remaining_qty or Decimal("0")),
            Decimal("0"),
        )
        if remaining_qty <= self._OBLIGATION_ATOMIC_FINALIZE_EPSILON:
            # 状态机将自然终结, 无需 fallback
            return refreshed

        # 超时未成交 → cancel post_only
        log_event(
            self.logger,
            "post_only_timeout_cancel",
            **correlation_fields(
                decision_id=intent.decision_id,
                intent_id=intent.intent_id,
                symbol=intent.symbol,
                client_order_id=client_order_id,
                pre_cancel_status=refreshed.status,
                pre_cancel_filled_qty=str(refreshed.filled_qty),
                pre_cancel_remaining_qty=str(remaining_qty),
                timeout_ms=timeout_ms,
            ),
        )
        try:
            canceled = await self.cancel_order(client_order_id)
        except Exception as exc:
            log_event(
                self.logger,
                "post_only_cancel_failed",
                level="error",
                **correlation_fields(
                    decision_id=intent.decision_id,
                    intent_id=intent.intent_id,
                    symbol=intent.symbol,
                    client_order_id=client_order_id,
                    error=str(exc),
                ),
            )
            # cancel 失败: 返回原 state, 不触发 fallback (避免 double-spend remaining)
            return refreshed

        # 重新计算 remaining (cancel 可能在 race 中带回新的 fill)
        post_cancel_remaining = max(
            Decimal(canceled.remaining_qty or Decimal("0")),
            Decimal("0"),
        )
        if post_cancel_remaining <= self._OBLIGATION_ATOMIC_FINALIZE_EPSILON:
            log_event(
                self.logger,
                "post_only_filled_during_cancel_race",
                **correlation_fields(
                    decision_id=intent.decision_id,
                    intent_id=intent.intent_id,
                    symbol=intent.symbol,
                    client_order_id=client_order_id,
                    cancel_status=canceled.status,
                    filled_qty=str(canceled.filled_qty),
                ),
            )
            return canceled

        return await self._submit_post_only_fallback(
            original_intent=intent,
            original_leg_intent=leg_intent,
            remaining_qty=post_cancel_remaining,
            trigger="timeout",
        )

    def _post_only_timeout_ms(self) -> float:
        raw = getattr(
            self.settings,
            "strategy_hedge_independent_post_only_timeout_ms",
            3000.0,
        )
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 3000.0

    def _post_only_fallback_mode(self) -> str:
        raw = getattr(
            self.settings,
            "strategy_hedge_independent_post_only_fallback_mode",
            "bounded_taker",
        )
        return str(raw).strip().lower() or "bounded_taker"

    async def _submit_post_only_fallback(
        self,
        *,
        original_intent: OrderIntent,
        original_leg_intent: LegOrderIntent | None,
        remaining_qty: Decimal,
        trigger: str,
    ) -> OrderState:
        """构造 + 派发 fallback intent. 走 _execute_submit_intent 递归 (允许 split)."""
        fallback_intent, fallback_leg_intent = self._build_post_only_fallback_intent(
            original_intent=original_intent,
            original_leg_intent=original_leg_intent,
            remaining_qty=remaining_qty,
        )
        fallback_client_order_id = self._derived_post_only_fallback_client_order_id(fallback_intent)
        log_event(
            self.logger,
            "post_only_fallback_dispatch",
            **correlation_fields(
                decision_id=fallback_intent.decision_id,
                intent_id=fallback_intent.intent_id,
                symbol=fallback_intent.symbol,
                trigger=trigger,
                fallback_mode=self._post_only_fallback_mode(),
                fallback_quantity=str(remaining_qty),
                original_intent_id=original_intent.intent_id,
            ),
        )
        return await self._execute_submit_intent(
            intent=fallback_intent,
            client_order_id=fallback_client_order_id,
            leg_intent=fallback_leg_intent,
        )

    def _build_post_only_fallback_intent(
        self,
        *,
        original_intent: OrderIntent,
        original_leg_intent: LegOrderIntent | None,
        remaining_qty: Decimal,
    ) -> tuple[OrderIntent, LegOrderIntent | None]:
        """构造 fallback intent (Layer 4, evidence doc §3.5).

        当前只支持 bounded_taker 一种 fallback (default).
        其他 fallback_mode 在 Layer 5 配置审计阶段拒绝.
        """
        fallback_mode = self._post_only_fallback_mode()
        if fallback_mode != "bounded_taker":
            raise RuntimeError(
                f"post_only_unsupported_fallback_mode:{fallback_mode}"
            )
        suffix = ":pof"  # post_only_fallback
        update_fields = {
            "intent_id": f"{original_intent.intent_id}{suffix}",
            "quantity": remaining_qty,
            "execution_style": "bounded_taker_cap",
            "order_type": "market",
            "time_in_force": "IOC",
            "limit_price": None,
            "idempotency_key": f"{original_intent.idempotency_key}{suffix}",
            "execution_attempt_id": None,
        }
        fallback_intent = original_intent.model_copy(update=update_fields)
        fallback_leg_intent: LegOrderIntent | None = None
        if original_leg_intent is not None:
            leg_update = {
                **update_fields,
                "leg_intent_id": f"{original_leg_intent.leg_intent_id}{suffix}",
                "idempotency_key": f"{original_leg_intent.idempotency_key}{suffix}",
            }
            # OrderIntent has intent_id but LegOrderIntent has leg_intent_id; pop wrong key.
            leg_update.pop("intent_id", None)
            fallback_leg_intent = original_leg_intent.model_copy(update=leg_update)
        return fallback_intent, fallback_leg_intent

    def _derived_post_only_fallback_client_order_id(self, intent: OrderIntent) -> str:
        preview_client_order_id_fn = getattr(self.adapter, "preview_client_order_id", None)
        return (
            preview_client_order_id_fn(intent)
            if callable(preview_client_order_id_fn)
            else None
        ) or intent.idempotency_key or new_id("clord")

    async def _persist_submitting_state_for_intent(
        self,
        *,
        intent: OrderIntent,
        client_order_id: str,
    ) -> None:
        current = self.execution_repo.get_order_state(client_order_id)
        if current is None:
            current = OrderState(
                decision_id=intent.decision_id,
                execution_chain_id=intent.execution_chain_id,
                execution_attempt_id=intent.execution_attempt_id,
                intent_id=intent.intent_id,
                symbol=intent.symbol,
                client_order_id=client_order_id,
                venue="OKX" if self.adapter.readiness().get("backend") == "okx" else "PAPER",
                exchange_order_id=None,
                status="CREATED",
                submission_mode="local_order_manager",
                submitted_ts=None,
                last_update_ts=utc_now(),
                requested_qty=intent.quantity,
                filled_qty=Decimal("0"),
                remaining_qty=intent.quantity,
                average_fill_price=None,
                fees=Decimal("0"),
                reduce_only=effective_reduce_only_for_intent(intent),
                close_only=effective_close_only_for_intent(intent),
                td_mode=intent.td_mode,
                position_mode=intent.position_mode,
                pos_side=intent.pos_side,
                reduce_only_reason=intent.reduce_only_reason,
                close_only_reason=intent.close_only_reason,
                instrument_family=intent.instrument_family,
                settle_currency=intent.settle_currency,
                product_type=intent.product_type,
                target_leverage=intent.target_leverage,
                margin_mode=intent.margin_mode,
                exposure_side=intent.exposure_side,
                execution_action=intent.execution_action,
                leg_action=intent.leg_action,
                position_intent=intent.position_intent,
                leg_intent_id=intent.leg_intent_id,
                strategy_family=intent.strategy_family,
                strategy_sleeve_id=intent.strategy_sleeve_id,
                allocation_id=intent.allocation_id,
                strategy_bundle_id=intent.strategy_bundle_id,
                strategy_leg_role=intent.strategy_leg_role,
                strategy_pair_id=intent.strategy_pair_id,
                strategy_opportunity_kind=intent.strategy_opportunity_kind,
                strategy_execution_mode=intent.strategy_execution_mode,
                strategy_state_phase=intent.strategy_state_phase,
                market_snapshot_ref=intent.market_snapshot_ref,
                feature_snapshot_ref=intent.feature_snapshot_ref,
                portfolio_snapshot_ref=intent.portfolio_snapshot_ref,
                health_snapshot_ref=intent.health_snapshot_ref,
                submission_payload={},
            )
        submitting_state = current.model_copy(
            update={
                "decision_id": intent.decision_id,
                "execution_chain_id": intent.execution_chain_id,
                "execution_attempt_id": intent.execution_attempt_id,
                "intent_id": intent.intent_id,
                "symbol": intent.symbol,
                "client_order_id": client_order_id,
                "status": "SUBMITTING",
                "last_update_ts": utc_now(),
                "requested_qty": intent.quantity,
                "remaining_qty": intent.quantity,
                "reduce_only": effective_reduce_only_for_intent(intent),
                "close_only": effective_close_only_for_intent(intent),
                "td_mode": intent.td_mode,
                "position_mode": intent.position_mode,
                "pos_side": intent.pos_side,
                "reduce_only_reason": intent.reduce_only_reason,
                "close_only_reason": intent.close_only_reason,
                "instrument_family": intent.instrument_family,
                "settle_currency": intent.settle_currency,
                "product_type": intent.product_type,
                "target_leverage": intent.target_leverage,
                "margin_mode": intent.margin_mode,
                "exposure_side": intent.exposure_side,
                "execution_action": intent.execution_action,
                "leg_action": intent.leg_action,
                "position_intent": intent.position_intent,
                "leg_intent_id": intent.leg_intent_id,
                "strategy_family": intent.strategy_family,
                "strategy_sleeve_id": intent.strategy_sleeve_id,
                "allocation_id": intent.allocation_id,
                "strategy_bundle_id": intent.strategy_bundle_id,
                "strategy_leg_role": intent.strategy_leg_role,
                "strategy_pair_id": intent.strategy_pair_id,
                "strategy_opportunity_kind": intent.strategy_opportunity_kind,
                "strategy_execution_mode": intent.strategy_execution_mode,
                "strategy_state_phase": intent.strategy_state_phase,
                "market_snapshot_ref": intent.market_snapshot_ref,
                "feature_snapshot_ref": intent.feature_snapshot_ref,
                "portfolio_snapshot_ref": intent.portfolio_snapshot_ref,
                "health_snapshot_ref": intent.health_snapshot_ref,
            }
        )
        await self._persist_order_state(order_state=submitting_state, key=intent.symbol)

    async def _submit_single_order_intent(
        self,
        *,
        intent: OrderIntent,
        client_order_id: str,
        leg_intent: LegOrderIntent | None = None,
    ) -> OrderState:
        try:
            if leg_intent is not None:
                submit_leg_order = getattr(self.adapter, "submit_leg_order", None)
                if callable(submit_leg_order):
                    order_state, fills = await submit_leg_order(leg_intent)
                else:
                    order_state, fills = await self.adapter.submit(intent)
            else:
                order_state, fills = await self.adapter.submit(intent)
        except Exception as exc:
            order_state = OrderState(
                decision_id=intent.decision_id,
                execution_chain_id=intent.execution_chain_id,
                execution_attempt_id=intent.execution_attempt_id,
                intent_id=intent.intent_id,
                symbol=intent.symbol,
                client_order_id=client_order_id,
                venue="OKX" if self.adapter.readiness().get("backend") == "okx" else "PAPER",
                exchange_order_id=None,
                status="FAILED",
                submission_mode="adapter_exception",
                submitted_ts=utc_now(),
                last_update_ts=utc_now(),
                requested_qty=intent.quantity,
                filled_qty=Decimal("0"),
                remaining_qty=intent.quantity,
                average_fill_price=None,
                fees=Decimal("0"),
                reduce_only=effective_reduce_only_for_intent(intent),
                close_only=effective_close_only_for_intent(intent),
                td_mode=intent.td_mode,
                position_mode=intent.position_mode,
                pos_side=intent.pos_side,
                reduce_only_reason=intent.reduce_only_reason,
                close_only_reason=intent.close_only_reason,
                instrument_family=intent.instrument_family,
                settle_currency=intent.settle_currency,
                product_type=intent.product_type,
                target_leverage=intent.target_leverage,
                margin_mode=intent.margin_mode,
                exposure_side=intent.exposure_side,
                execution_action=intent.execution_action,
                leg_action=intent.leg_action,
                position_intent=intent.position_intent,
                leg_intent_id=intent.leg_intent_id,
                strategy_family=intent.strategy_family,
                strategy_sleeve_id=intent.strategy_sleeve_id,
                allocation_id=intent.allocation_id,
                strategy_bundle_id=intent.strategy_bundle_id,
                strategy_leg_role=intent.strategy_leg_role,
                strategy_pair_id=intent.strategy_pair_id,
                strategy_opportunity_kind=intent.strategy_opportunity_kind,
                strategy_execution_mode=intent.strategy_execution_mode,
                strategy_state_phase=intent.strategy_state_phase,
                market_snapshot_ref=intent.market_snapshot_ref,
                feature_snapshot_ref=intent.feature_snapshot_ref,
                portfolio_snapshot_ref=intent.portfolio_snapshot_ref,
                health_snapshot_ref=intent.health_snapshot_ref,
                cancel_reason=str(exc),
                execution_error=str(exc),
                submission_payload={},
            )
            fills = []
            log_event(
                self.logger,
                "order_submit_failed",
                level="error",
                **correlation_fields(
                    decision_id=intent.decision_id,
                    intent_id=intent.intent_id,
                    symbol=intent.symbol,
                    error=str(exc),
                ),
            )
        order_state = self._order_state_with_intent_context(
            order_state=order_state,
            intent=intent,
        )
        fills = [
            self._fill_with_intent_context(fill=fill, intent=intent)
            for fill in fills
        ]

        # ── Fix P1-3：当有 fills 且有 outbox publisher 时，走单事务原子路径 ──
        if fills and self.execution_outbox_publisher is not None and self.obligation_service is not None:
            # 先规范化 fill client_order_id
            normalized_fills = []
            for fill in fills:
                if fill.client_order_id != order_state.client_order_id:
                    fill = fill.model_copy(
                        update={
                            "client_order_id": order_state.client_order_id,
                            "execution_attempt_id": (
                                order_state.execution_attempt_id
                                or execution_attempt_id_from_components(
                                    client_order_id=order_state.client_order_id,
                                    execution_chain_id=order_state.execution_chain_id,
                                    intent_id=order_state.intent_id,
                                )
                            ),
                        }
                    )
                normalized_fills.append(fill)
            # 链式计算所有 obligation 更新 + 终态 finalization
            per_fill_obligations, final_obligation = (
                self.obligation_service.preview_chained_fill_obligations_and_finalize(
                    fills=normalized_fills,
                    order_state=order_state,
                )
            )
            persisted_order_state = await self.execution_outbox_publisher.persist_order_state_with_fills(
                order_state=order_state,
                key=intent.symbol,
                fills=normalized_fills,
                obligations_per_fill=per_fill_obligations,
                final_obligation=final_obligation,
            )
            log_event(
                self.logger,
                "order_state_persisted",
                **correlation_fields(
                    decision_id=persisted_order_state.decision_id,
                    intent_id=persisted_order_state.intent_id,
                    order_id=persisted_order_state.client_order_id,
                    status=persisted_order_state.status,
                    venue=persisted_order_state.venue,
                    submission_mode=persisted_order_state.submission_mode,
                    fill_count=len(normalized_fills),
                ),
            )
            self._shadow_write_order_state(order_state=persisted_order_state, intent=intent)
            self._sync_strategy_bundle_status(order_state=persisted_order_state)
            self._sync_exit_execution_intent(order_state=persisted_order_state, intent=intent)
            for fill in normalized_fills:
                self._shadow_write_fill(fill)
            mirrored_obl = final_obligation or (per_fill_obligations[-1] if per_fill_obligations else None)
            self._shadow_sync_obligation(mirrored_obl, reason="atomic_settlement", related_fill=normalized_fills[-1] if normalized_fills else None)
            return persisted_order_state

        # ── Legacy 路径：无 outbox 或无 fills ──
        persisted_order_state = await self._persist_order_state(
            order_state=order_state,
            key=intent.symbol,
            intent=intent,
            obligation=self._terminal_outbox_obligation(order_state=order_state, fills=fills),
        )

        for fill in fills:
            if fill.client_order_id != persisted_order_state.client_order_id:
                fill = fill.model_copy(
                    update={
                        "client_order_id": persisted_order_state.client_order_id,
                        "execution_attempt_id": (
                            persisted_order_state.execution_attempt_id
                            or execution_attempt_id_from_components(
                                client_order_id=persisted_order_state.client_order_id,
                                execution_chain_id=persisted_order_state.execution_chain_id,
                                intent_id=persisted_order_state.intent_id,
                            )
                        ),
                    }
                )
            await self._persist_fill(fill)
        self._finalize_obligation(order_state=persisted_order_state)
        return persisted_order_state

    async def _risk_reducing_max_order_quantity_limit(self, *, intent: OrderIntent) -> Decimal | None:
        if not is_risk_reducing_order_intent(intent):
            return None
        limit_provider = getattr(self.adapter, "risk_reducing_max_order_quantity_limit", None)
        if not callable(limit_provider):
            return None
        try:
            limit = limit_provider(intent=intent)
            if asyncio.iscoroutine(limit):
                limit = await limit
        except Exception as exc:
            log_event(
                self.logger,
                "exit_split_limit_lookup_failed",
                level="warning",
                **correlation_fields(
                    decision_id=intent.decision_id,
                    intent_id=intent.intent_id,
                    symbol=intent.symbol,
                    error=str(exc),
                ),
            )
            return None
        if limit is None:
            return None
        return max(Decimal(limit), Decimal("0"))

    async def _serial_exit_split_limit(self, *, intent: OrderIntent) -> Decimal | None:
        normalized_limit = await self._risk_reducing_max_order_quantity_limit(intent=intent)
        if normalized_limit is None:
            return None
        if intent.quantity - normalized_limit <= self._OBLIGATION_ATOMIC_FINALIZE_EPSILON:
            return None
        return normalized_limit

    def _fallback_serial_exit_split_anchor_state(self, *, client_order_id: str) -> OrderState:
        """Task 142：当 `_execute_serial_exit_split` 要在未派发任何 slice 的情况下提前
        退出时，必须返回 anchor OrderState（通常是 caller 先 persist 的 SUBMITTING
        状态）。anchor 不存在是基础设施级别 race / DB 写失败 —— raise 而非静默返回 None，
        让上层看到明确故障而不是隐式签名不匹配。"""
        anchor = self.execution_repo.get_order_state(client_order_id)
        if anchor is None:
            raise RuntimeError("serial_exit_split_missing_anchor_state")
        return anchor

    async def _execute_serial_exit_split(
        self,
        *,
        intent: OrderIntent,
        client_order_id: str,
        leg_intent: LegOrderIntent | None,
        split_limit: Decimal,
        start_slice_index: int = 1,
    ) -> OrderState:
        # Task 142：返回契约收口 —— 必定返回 OrderState。原签名 `OrderState | None`
        # 把"没派发任何 slice 就退出"的语义与"anchor state 缺失"的基础设施错误混在
        # 一起，caller `_execute_submit_intent` 不得不三段式 workaround（split→None→
        # fallback→raise）。新契约下，所有 early-return 路径统一经
        # `_fallback_serial_exit_split_anchor_state` 返回 anchor 或 raise。
        last_state: OrderState | None = self.execution_repo.get_order_state(client_order_id)
        if split_limit <= self._OBLIGATION_ATOMIC_FINALIZE_EPSILON:
            return last_state if last_state is not None else self._fallback_serial_exit_split_anchor_state(
                client_order_id=client_order_id
            )
        for slice_index in range(start_slice_index, self._EXIT_SPLIT_MAX_CHILDREN + 1):
            parent = self._parent_exit_execution_intent(intent=intent)
            if parent is not None and (
                parent.operator_review_required
                or parent.aggregate_status in {"CANCEL_PENDING", "COMPLETED", "CANCELED", "FAILED_SAFE", "REVIEW_REQUIRED"}
            ):
                return last_state if last_state is not None else self._fallback_serial_exit_split_anchor_state(
                    client_order_id=client_order_id
                )
            remaining_quantity = (
                max(Decimal(intent.quantity), Decimal("0"))
                if slice_index == start_slice_index and start_slice_index <= 1
                else (
                    parent.remaining_dispatchable_quantity
                    if parent is not None
                    else max(Decimal(intent.quantity), Decimal("0"))
                )
            )
            if remaining_quantity <= self._OBLIGATION_ATOMIC_FINALIZE_EPSILON:
                return last_state if last_state is not None else self._fallback_serial_exit_split_anchor_state(
                    client_order_id=client_order_id
                )
            child_quantity = min(split_limit, remaining_quantity)
            if child_quantity <= self._OBLIGATION_ATOMIC_FINALIZE_EPSILON:
                return last_state if last_state is not None else self._fallback_serial_exit_split_anchor_state(
                    client_order_id=client_order_id
                )
            child_intent, child_leg_intent = self._split_child_intent(
                intent=intent,
                leg_intent=leg_intent,
                quantity=child_quantity,
                slice_index=slice_index,
            )
            child_client_order_id = (
                client_order_id
                if slice_index == start_slice_index and start_slice_index <= 1
                else self._derived_child_client_order_id(child_intent)
            )
            log_event(
                self.logger,
                "serial_exit_split_dispatch",
                **correlation_fields(
                    decision_id=child_intent.decision_id,
                    intent_id=child_intent.intent_id,
                    symbol=child_intent.symbol,
                    execution_chain_id=child_intent.execution_chain_id,
                    parent_intent_id=None if parent is None else parent.parent_intent_id,
                    slice_index=slice_index,
                    child_quantity=child_quantity,
                    remaining_dispatchable_quantity=remaining_quantity,
                    max_size_limit=split_limit,
                ),
            )
            child_intent, child_leg_intent = self._apply_execution_attempt_id(
                intent=child_intent,
                client_order_id=child_client_order_id,
                leg_intent=child_leg_intent,
            )
            if slice_index > start_slice_index or start_slice_index > 1:
                await self._persist_submitting_state_for_intent(
                    intent=child_intent,
                    client_order_id=child_client_order_id,
                )
            last_state = await self._submit_single_order_intent(
                intent=child_intent,
                client_order_id=child_client_order_id,
                leg_intent=child_leg_intent,
            )
            parent = self._parent_exit_execution_intent(intent=intent)
            if not self._should_continue_serial_exit_split(
                child_state=last_state,
                parent=parent,
                slice_index=slice_index,
            ):
                return last_state
        return last_state

    def _should_continue_serial_exit_split(
        self,
        *,
        child_state: OrderState,
        parent,
        slice_index: int,
    ) -> bool:
        if slice_index >= self._EXIT_SPLIT_MAX_CHILDREN:
            return False
        if not self.order_state_machine.is_terminal(child_state.status):
            return False
        if child_state.status in {"FAILED", "REJECTED", "BLOCKED"}:
            return False
        if is_unknown_write_state(child_state):
            return False
        if parent is None:
            return False
        if parent.operator_review_required or parent.aggregate_status in {"REVIEW_REQUIRED", "CANCEL_PENDING", "FAILED_SAFE"}:
            return False
        if parent.remaining_dispatchable_quantity <= self._OBLIGATION_ATOMIC_FINALIZE_EPSILON:
            return False
        return child_state.filled_qty > self._OBLIGATION_ATOMIC_FINALIZE_EPSILON or child_state.status == "FILLED"

    def _split_child_intent(
        self,
        *,
        intent: OrderIntent,
        leg_intent: LegOrderIntent | None,
        quantity: Decimal,
        slice_index: int,
    ) -> tuple[OrderIntent, LegOrderIntent | None]:
        if slice_index <= 1:
            return (
                intent.model_copy(update={"quantity": quantity}),
                None if leg_intent is None else leg_intent.model_copy(update={"quantity": quantity}),
            )
        suffix = f":slice:{slice_index}"
        child_intent = intent.model_copy(
            update={
                "intent_id": f"{intent.intent_id}{suffix}",
                "quantity": quantity,
                "idempotency_key": f"{intent.idempotency_key}{suffix}",
                "execution_attempt_id": None,
            }
        )
        child_leg_intent = None
        if leg_intent is not None:
            child_leg_intent = leg_intent.model_copy(
                update={
                    "leg_intent_id": f"{leg_intent.leg_intent_id}{suffix}",
                    "quantity": quantity,
                    "idempotency_key": f"{leg_intent.idempotency_key}{suffix}",
                    "execution_attempt_id": None,
                }
            )
        return child_intent, child_leg_intent

    def _derived_child_client_order_id(self, intent: OrderIntent) -> str:
        preview_client_order_id_fn = getattr(self.adapter, "preview_client_order_id", None)
        return (
            preview_client_order_id_fn(intent)
            if callable(preview_client_order_id_fn)
            else None
        ) or intent.idempotency_key or new_id("clord")

    def _parent_exit_execution_intent(self, *, intent: OrderIntent):
        if self.exit_execution_repo is None:
            return None
        execution_chain_id = str(intent.execution_chain_id or intent.intent_id).strip()
        if not execution_chain_id:
            return None
        return self.exit_execution_repo.get_exit_execution_intent_by_execution_chain(execution_chain_id)

    async def sync_exchange_state(self) -> None:
        candidates = await asyncio.to_thread(self._sync_candidates)
        order_states, fills = await self.adapter.sync(candidates)
        persisted_states: list[OrderState] = []
        atomically_settled_order_ids: set[str] = set()
        fills_by_order: dict[str, list[FillEvent]] = {}
        for fill in fills:
            fills_by_order.setdefault(fill.client_order_id, []).append(fill)
        for order_state in order_states:
            order_fills = fills_by_order.pop(order_state.client_order_id, [])
            atomic_persisted = await self._persist_order_state_with_fills_atomic(
                order_state=order_state,
                key=order_state.symbol,
                fills=order_fills,
            )
            if atomic_persisted is not None:
                persisted_states.append(atomic_persisted)
                atomically_settled_order_ids.add(atomic_persisted.client_order_id)
                continue
            persisted = await self._persist_order_state(
                order_state=order_state,
                key=order_state.symbol,
                obligation=self._terminal_outbox_obligation(order_state=order_state, fills=order_fills),
            )
            persisted_states.append(persisted)
            for fill in order_fills:
                await self._persist_fill(fill)
        for order_fills in fills_by_order.values():
            for fill in order_fills:
                await self._persist_fill(fill)
        for order_state in persisted_states:
            if order_state.client_order_id in atomically_settled_order_ids:
                continue
            self._finalize_obligation(order_state=order_state)
        self._refresh_exit_execution_intents()
        await self._resume_exit_execution_after_sync()

    async def _persist_order_state_with_fills_atomic(
        self,
        *,
        order_state: OrderState,
        key: str,
        fills: list[FillEvent],
        intent: OrderIntent | None = None,
    ) -> OrderState | None:
        if not fills or self.execution_outbox_publisher is None or self.obligation_service is None:
            return None
        normalized_fills: list[FillEvent] = []
        for fill in fills:
            if fill.client_order_id != order_state.client_order_id:
                fill = fill.model_copy(
                    update={
                        "client_order_id": order_state.client_order_id,
                        "execution_attempt_id": (
                            order_state.execution_attempt_id
                            or execution_attempt_id_from_components(
                                client_order_id=order_state.client_order_id,
                                execution_chain_id=order_state.execution_chain_id,
                                intent_id=order_state.intent_id,
                            )
                        ),
                    }
                )
            normalized_fills.append(fill)
        per_fill_obligations, final_obligation = (
            self.obligation_service.preview_chained_fill_obligations_and_finalize(
                fills=normalized_fills,
                order_state=order_state,
            )
        )
        persisted_order_state = await self.execution_outbox_publisher.persist_order_state_with_fills(
            order_state=order_state,
            key=key,
            fills=normalized_fills,
            obligations_per_fill=per_fill_obligations,
            final_obligation=final_obligation,
        )
        log_event(
            self.logger,
            "order_state_persisted",
            **correlation_fields(
                decision_id=persisted_order_state.decision_id,
                intent_id=persisted_order_state.intent_id,
                order_id=persisted_order_state.client_order_id,
                status=persisted_order_state.status,
                venue=persisted_order_state.venue,
                submission_mode=persisted_order_state.submission_mode,
                fill_count=len(normalized_fills),
            ),
        )
        self._shadow_write_order_state(order_state=persisted_order_state, intent=intent)
        self._sync_strategy_bundle_status(order_state=persisted_order_state)
        self._sync_exit_execution_intent(order_state=persisted_order_state, intent=intent)
        for fill in normalized_fills:
            self._shadow_write_fill(fill)
        mirrored_obligation = final_obligation or next(
            (obligation for obligation in reversed(per_fill_obligations) if obligation is not None),
            None,
        )
        self._shadow_sync_obligation(
            mirrored_obligation,
            reason="atomic_settlement",
            related_fill=normalized_fills[-1] if normalized_fills else None,
        )
        return persisted_order_state

    def _sync_candidates(self) -> list[OrderState]:
        open_states = self.execution_repo.open_order_states()
        prioritized_open_states = [
            *[state for state in open_states if self._is_unknown_write_state(state)],
            *[state for state in open_states if not self._is_unknown_write_state(state)],
        ]
        candidates: dict[str, OrderState] = {
            state.client_order_id: state
            for state in prioritized_open_states
        }
        for state in self.execution_repo.recent_order_states(
            limit=self._FILL_BACKFILL_RECENT_LIMIT,
            statuses=self._FILL_BACKFILL_TERMINAL_STATUSES,
        ):
            if state.filled_qty <= self._OBLIGATION_ATOMIC_FINALIZE_EPSILON:
                continue
            if self.execution_repo.fills_for_order(state.client_order_id):
                continue
            candidates.setdefault(state.client_order_id, state)
        return list(candidates.values())

    @staticmethod
    def _is_unknown_write_state(state: OrderState) -> bool:
        return is_unknown_write_state(state)

    def _refresh_exit_execution_intents(self) -> None:
        if self.exit_execution_repo is None:
            return
        refresh_exit_execution_intents(
            execution_repo=self.execution_repo,
            exit_execution_repo=self.exit_execution_repo,
            settings=self.settings,
            exit_execution_writer=self.exit_execution_writer,
        )

    async def _resume_exit_execution_after_sync(self) -> None:
        if self.exit_execution_repo is None:
            return
        for parent in sorted(
            self.exit_execution_repo.list_exit_execution_intents(),
            key=lambda item: (item.updated_at, item.parent_intent_id),
        ):
            if resume_block_reason(parent) is not None:
                continue
            await self._resume_exit_execution_parent(parent=parent)

    async def _resume_exit_execution_parent(self, *, parent) -> OrderState | None:
        template = dispatch_template_from_parent(parent)
        if template is None:
            return None
        parent = self.exit_execution_repo.get_exit_execution_intent(parent.parent_intent_id) or parent
        if resume_block_reason(parent) is not None:
            return None
        child_refs = self.exit_execution_repo.child_refs_for_parent(parent_intent_id=parent.parent_intent_id)
        next_slice_index = len(child_refs) + 1
        if next_slice_index > self._EXIT_SPLIT_MAX_CHILDREN:
            return None
        intent = OrderIntent.model_validate(
            {
                **template,
                "quantity": str(parent.remaining_dispatchable_quantity),
            }
        )
        split_limit = await self._risk_reducing_max_order_quantity_limit(intent=intent)
        if split_limit is None or split_limit <= self._OBLIGATION_ATOMIC_FINALIZE_EPSILON:
            self._save_exit_execution_parent(
                record_resume_issue(
                    parent,
                    kind="resume_limit_lookup_failed",
                    error="max_size_limit_unavailable",
                )
            )
            log_event(
                self.logger,
                "serial_exit_split_resume_skipped",
                level="warning",
                **correlation_fields(
                    decision_id=intent.decision_id,
                    intent_id=intent.intent_id,
                    symbol=intent.symbol,
                    execution_chain_id=intent.execution_chain_id,
                    parent_intent_id=parent.parent_intent_id,
                    reason="missing_or_invalid_max_size_limit",
                ),
            )
            return None
        parent = self.exit_execution_writer.recompute_parent(
            parent_intent_id=parent.parent_intent_id,
            transform_parent=clear_resume_issue,
            recompute_parent=lambda parent_intent, child_refs: recompute_exit_execution_intent(
                parent_intent=parent_intent,
                child_refs=child_refs,
            ),
            source_component="order_manager",
            reason_code="retry_limit_lookup_clear_resume_issue",
        )
        log_event(
            self.logger,
            "serial_exit_split_resume",
            **correlation_fields(
                decision_id=intent.decision_id,
                intent_id=intent.intent_id,
                symbol=intent.symbol,
                execution_chain_id=intent.execution_chain_id,
                parent_intent_id=parent.parent_intent_id,
                next_slice_index=next_slice_index,
                remaining_dispatchable_quantity=parent.remaining_dispatchable_quantity,
                max_size_limit=split_limit,
            ),
        )
        return await self._execute_serial_exit_split(
            intent=intent,
            client_order_id=intent.idempotency_key,
            leg_intent=None,
            split_limit=split_limit,
            start_slice_index=next_slice_index,
        )

    def _semantic_duplicate_snapshot_submit_block(
        self,
        *,
        intent: OrderIntent,
        client_order_id: str,
    ) -> OrderState | None:
        blocker = self._semantic_duplicate_snapshot_state(
            intent=intent,
            client_order_id=client_order_id,
        )
        if blocker is None:
            return None
        log_event(
            self.logger,
            "semantic_duplicate_order_intent_blocked",
            level="critical",
            **correlation_fields(
                decision_id=intent.decision_id,
                intent_id=intent.intent_id,
                symbol=intent.symbol,
                client_order_id=client_order_id,
                portfolio_snapshot_ref=intent.portfolio_snapshot_ref,
                blocking_client_order_id=blocker.client_order_id,
                blocking_intent_id=blocker.intent_id,
                blocking_status=blocker.status,
                blocking_position_intent=blocker.position_intent,
            ),
        )
        return self._blocked_order_state_from_intent(
            intent=intent,
            client_order_id=client_order_id,
            submission_mode="semantic_duplicate_snapshot_blocked",
            execution_error=f"semantic_duplicate_snapshot_order:{blocker.client_order_id}",
        )

    def _semantic_duplicate_snapshot_state(
        self,
        *,
        intent: OrderIntent,
        client_order_id: str,
    ) -> OrderState | None:
        snapshot_ref = self._normalized_snapshot_ref(intent.portfolio_snapshot_ref)
        if not snapshot_ref:
            return None
        intent_lanes = self._semantic_duplicate_lanes_from_intent(intent)
        if not intent_lanes:
            return None
        for state in self.execution_repo.recent_order_states(
            limit=self._SEMANTIC_DUPLICATE_RECENT_LIMIT,
        ):
            if state.client_order_id == client_order_id or state.intent_id == intent.intent_id:
                continue
            if (
                state.execution_chain_id
                and intent.execution_chain_id
                and state.execution_chain_id == intent.execution_chain_id
            ):
                continue
            if self._normalized_snapshot_ref(state.portfolio_snapshot_ref) != snapshot_ref:
                continue
            if state.symbol != intent.symbol:
                continue
            if self._semantic_duplicate_product_mismatch(state=state, intent=intent):
                continue
            if not self._semantic_duplicate_state_can_block(state):
                continue
            state_lanes = self._semantic_duplicate_lanes_from_state(state)
            if not state_lanes:
                continue
            if intent_lanes.isdisjoint(state_lanes):
                if self._semantic_duplicate_disjoint_lanes_allowed(
                    intent=intent,
                    state=state,
                    intent_lanes=intent_lanes,
                    state_lanes=state_lanes,
                ):
                    continue
                return state
            return state
        return None

    def _risk_increase_convergence_submit_block(
        self,
        *,
        intent: OrderIntent,
        client_order_id: str,
    ) -> OrderState | None:
        blocker = self._risk_increase_convergence_state(
            intent=intent,
            client_order_id=client_order_id,
        )
        if blocker is None:
            return None
        log_event(
            self.logger,
            "directional_risk_increase_convergence_blocked",
            level="critical",
            **correlation_fields(
                decision_id=intent.decision_id,
                intent_id=intent.intent_id,
                symbol=intent.symbol,
                client_order_id=client_order_id,
                portfolio_snapshot_ref=intent.portfolio_snapshot_ref,
                blocking_client_order_id=blocker.client_order_id,
                blocking_intent_id=blocker.intent_id,
                blocking_status=blocker.status,
                blocking_position_intent=blocker.position_intent,
                blocking_filled_qty=blocker.filled_qty,
            ),
        )
        return self._blocked_order_state_from_intent(
            intent=intent,
            client_order_id=client_order_id,
            submission_mode="risk_increase_convergence_blocked",
            execution_error=f"risk_increase_convergence_order:{blocker.client_order_id}",
        )

    def _risk_increase_convergence_state(
        self,
        *,
        intent: OrderIntent,
        client_order_id: str,
    ) -> OrderState | None:
        intent_lanes = self._semantic_duplicate_lanes_from_intent(intent)
        if not self._directional_risk_increase_context(intent):
            return None
        if not (intent_lanes & self._RISK_INCREASE_CONVERGENCE_LANES):
            return None
        for state in self.execution_repo.recent_order_states(
            limit=self._RISK_INCREASE_CONVERGENCE_RECENT_LIMIT,
        ):
            if state.client_order_id == client_order_id or state.intent_id == intent.intent_id:
                continue
            if state.symbol != intent.symbol:
                continue
            if self._semantic_duplicate_product_mismatch(state=state, intent=intent):
                continue
            if not self._directional_risk_increase_context(state):
                continue
            state_lanes = self._semantic_duplicate_lanes_from_state(state)
            state_increase_lanes = state_lanes & self._RISK_INCREASE_CONVERGENCE_LANES
            if not state_increase_lanes:
                continue
            if self._risk_increase_state_is_inflight(state):
                return state
            if not self._risk_increase_state_is_recently_filled(state):
                continue
            for lane in state_increase_lanes:
                exposure_side = "long" if lane == "long_increase" else "short"
                current_qty = self._intent_current_exposure_qty(intent=intent, side=exposure_side)
                if (
                    current_qty is None
                    or current_qty <= self._OBLIGATION_ATOMIC_FINALIZE_EPSILON
                ):
                    return state
        return None

    @classmethod
    def _risk_increase_state_is_inflight(cls, state: OrderState) -> bool:
        status = str(state.status or "").upper()
        if status in cls._SEMANTIC_DUPLICATE_NO_EFFECT_STATUSES:
            return False
        if status == "FILLED":
            return False
        return status not in {"CANCELED", "CANCELLED", "EXPIRED", "REJECTED", "FAILED"}

    def _risk_increase_state_is_recently_filled(self, state: OrderState) -> bool:
        if str(state.status or "").upper() != "FILLED":
            return False
        filled_qty = self._decimal_or_zero(state.filled_qty)
        if filled_qty <= self._OBLIGATION_ATOMIC_FINALIZE_EPSILON:
            return False
        state_ts = state.last_update_ts or state.submitted_ts or state.created_at
        if state_ts is None:
            return True
        now = utc_now()
        if state_ts.tzinfo is None:
            state_ts = state_ts.replace(tzinfo=timezone.utc)
        return now - state_ts <= timedelta(seconds=self._RISK_INCREASE_CONVERGENCE_RECENT_SECONDS)

    @staticmethod
    def _directional_risk_increase_context(value: object) -> bool:
        for field_name in (
            "strategy_family",
            "strategy_execution_mode",
            "strategy_bundle_id",
            "execution_chain_id",
        ):
            normalized = str(getattr(value, field_name, "") or "").strip().lower()
            if normalized == "directional" or normalized.startswith(("directional:", "directional_")):
                return True
        return False

    def _intent_current_exposure_qty(self, *, intent: OrderIntent, side: str) -> Decimal | None:
        risk_budget_state = intent.risk_budget_state if isinstance(intent.risk_budget_state, dict) else {}
        convergence = risk_budget_state.get("execution_convergence")
        if not isinstance(convergence, dict):
            convergence = {}
        for key in (
            f"current_{side}_position_qty",
            f"{side}_position_qty",
        ):
            if key in convergence:
                return self._decimal_or_zero(convergence.get(key))
            if key in risk_budget_state:
                return self._decimal_or_zero(risk_budget_state.get(key))
        return None

    def _semantic_duplicate_disjoint_lanes_allowed(
        self,
        *,
        intent: OrderIntent,
        state: OrderState,
        intent_lanes: set[str],
        state_lanes: set[str],
    ) -> bool:
        if not (
            self._semantic_duplicate_independent_book_context(intent)
            and self._semantic_duplicate_independent_book_context(state)
        ):
            return False
        intent_books = self._semantic_duplicate_lane_books(intent_lanes)
        state_books = self._semantic_duplicate_lane_books(state_lanes)
        return bool(intent_books and state_books and intent_books.isdisjoint(state_books))

    @staticmethod
    def _semantic_duplicate_independent_book_context(value: object) -> bool:
        for field_name in (
            "strategy_family",
            "strategy_execution_mode",
            "strategy_bundle_id",
            "execution_chain_id",
        ):
            normalized = str(getattr(value, field_name, "") or "").strip().lower()
            if normalized == "independent" or normalized.startswith(("independent:", "independent_")):
                return True
        return False

    @staticmethod
    def _semantic_duplicate_lane_books(lanes: set[str]) -> set[str]:
        books: set[str] = set()
        for lane in lanes:
            normalized = str(lane or "").strip().lower()
            if normalized.startswith("long_"):
                books.add("long")
                continue
            if normalized.startswith("short_"):
                books.add("short")
                continue
            return set()
        return books

    def _semantic_duplicate_state_can_block(self, state: OrderState) -> bool:
        filled_qty = self._decimal_or_zero(state.filled_qty)
        if filled_qty > self._OBLIGATION_ATOMIC_FINALIZE_EPSILON:
            return True
        status = str(state.status or "").upper()
        if status in self._SEMANTIC_DUPLICATE_NO_EFFECT_STATUSES:
            return False
        if self.order_state_machine.is_terminal(status):
            return False
        return True

    @staticmethod
    def _semantic_duplicate_product_mismatch(
        *,
        state: OrderState,
        intent: OrderIntent,
    ) -> bool:
        for field_name in ("product_type", "margin_mode"):
            state_value = getattr(state, field_name, None)
            intent_value = getattr(intent, field_name, None)
            if state_value is None or intent_value is None:
                continue
            if str(state_value).strip().lower() != str(intent_value).strip().lower():
                return True
        return False

    def _semantic_duplicate_lanes_from_intent(self, intent: OrderIntent) -> set[str]:
        return self._semantic_duplicate_lanes(
            position_intent=intent.position_intent,
            side=intent.side,
            pos_side=intent.pos_side,
            exposure_side=intent.exposure_side,
            reduce_only=effective_reduce_only_for_intent(intent),
            close_only=effective_close_only_for_intent(intent),
        )

    def _semantic_duplicate_lanes_from_state(self, state: OrderState) -> set[str]:
        return self._semantic_duplicate_lanes(
            position_intent=state.position_intent,
            side=getattr(state, "side", None),
            pos_side=state.pos_side,
            exposure_side=state.exposure_side,
            reduce_only=state.reduce_only,
            close_only=state.close_only,
        )

    @staticmethod
    def _semantic_duplicate_lanes(
        *,
        position_intent: str | None,
        side: str | None,
        pos_side: str | None,
        exposure_side: str | None,
        reduce_only: bool | None,
        close_only: bool | None,
    ) -> set[str]:
        mapped = {
            "open_long": {"long_increase"},
            "scale_in_long": {"long_increase"},
            "reduce_long": {"long_reduce"},
            "close_long": {"long_reduce"},
            "open_short": {"short_increase"},
            "scale_in_short": {"short_increase"},
            "reduce_short": {"short_reduce"},
            "close_short": {"short_reduce"},
            "reverse_to_long": {"short_reduce", "long_increase"},
            "reverse_to_short": {"long_reduce", "short_increase"},
        }.get(str(position_intent or "").strip().lower())
        if mapped is not None:
            return set(mapped)
        exposure = str(pos_side or exposure_side or "").strip().lower()
        if bool(reduce_only) or bool(close_only):
            if exposure in {"long", "short"}:
                return {f"{exposure}_reduce"}
            return set()
        side_value = str(side or "").strip().lower()
        if side_value == "buy":
            return {"long_increase"}
        if side_value == "sell":
            return {"short_increase"}
        return set()

    @staticmethod
    def _normalized_snapshot_ref(value: object) -> str:
        return str(value or "").strip()

    @staticmethod
    def _decimal_or_zero(value: object) -> Decimal:
        if value is None:
            return Decimal("0")
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))

    @staticmethod
    def _order_state_with_intent_context(
        *,
        order_state: OrderState,
        intent: OrderIntent,
    ) -> OrderState:
        return order_state.model_copy(
            update={
                "execution_chain_id": order_state.execution_chain_id or intent.execution_chain_id,
                "execution_attempt_id": order_state.execution_attempt_id or intent.execution_attempt_id,
                "leg_intent_id": order_state.leg_intent_id or intent.leg_intent_id,
                "reduce_only": effective_reduce_only_for_intent(intent),
                "close_only": effective_close_only_for_intent(intent),
                "td_mode": intent.td_mode,
                "position_mode": intent.position_mode,
                "pos_side": intent.pos_side,
                "reduce_only_reason": intent.reduce_only_reason,
                "close_only_reason": intent.close_only_reason,
                "instrument_family": intent.instrument_family,
                "settle_currency": intent.settle_currency,
                "strategy_family": intent.strategy_family,
                "strategy_sleeve_id": intent.strategy_sleeve_id,
                "allocation_id": intent.allocation_id,
                "strategy_bundle_id": intent.strategy_bundle_id,
                "strategy_leg_role": intent.strategy_leg_role,
                "strategy_pair_id": intent.strategy_pair_id,
                "strategy_opportunity_kind": intent.strategy_opportunity_kind,
                "strategy_execution_mode": intent.strategy_execution_mode,
                "strategy_state_phase": intent.strategy_state_phase,
                "product_type": intent.product_type,
                "target_leverage": intent.target_leverage,
                "margin_mode": intent.margin_mode,
                "exposure_side": intent.exposure_side,
                "execution_action": intent.execution_action,
                "leg_action": intent.leg_action,
                "position_intent": intent.position_intent,
                "market_snapshot_ref": intent.market_snapshot_ref,
                "feature_snapshot_ref": intent.feature_snapshot_ref,
                "portfolio_snapshot_ref": intent.portfolio_snapshot_ref,
                "health_snapshot_ref": intent.health_snapshot_ref,
            }
        )

    @staticmethod
    def _fill_with_intent_context(
        *,
        fill: FillEvent,
        intent: OrderIntent,
    ) -> FillEvent:
        return fill.model_copy(
            update={
                "execution_chain_id": fill.execution_chain_id or intent.execution_chain_id,
                "execution_attempt_id": fill.execution_attempt_id or intent.execution_attempt_id,
                "leg_intent_id": fill.leg_intent_id or intent.leg_intent_id,
                "reduce_only": effective_reduce_only_for_intent(intent),
                "close_only": effective_close_only_for_intent(intent),
                "td_mode": intent.td_mode,
                "position_mode": intent.position_mode,
                "pos_side": intent.pos_side,
                "reduce_only_reason": intent.reduce_only_reason,
                "close_only_reason": intent.close_only_reason,
                "instrument_family": intent.instrument_family,
                "settle_currency": intent.settle_currency,
                "strategy_family": intent.strategy_family,
                "strategy_sleeve_id": intent.strategy_sleeve_id,
                "allocation_id": intent.allocation_id,
                "strategy_bundle_id": intent.strategy_bundle_id,
                "strategy_leg_role": intent.strategy_leg_role,
                "strategy_pair_id": intent.strategy_pair_id,
                "strategy_opportunity_kind": intent.strategy_opportunity_kind,
                "strategy_execution_mode": intent.strategy_execution_mode,
                "strategy_state_phase": intent.strategy_state_phase,
                "product_type": intent.product_type,
                "target_leverage": intent.target_leverage,
                "margin_mode": intent.margin_mode,
                "exposure_side": intent.exposure_side,
                "execution_action": intent.execution_action,
                "leg_action": intent.leg_action,
                "position_intent": intent.position_intent,
                "market_snapshot_ref": intent.market_snapshot_ref,
                "feature_snapshot_ref": intent.feature_snapshot_ref,
                "portfolio_snapshot_ref": intent.portfolio_snapshot_ref,
                "health_snapshot_ref": intent.health_snapshot_ref,
            }
        )

    def _unknown_write_submit_block(
        self,
        *,
        intent: OrderIntent,
        client_order_id: str,
    ) -> OrderState | None:
        blocker = self._blocking_unknown_write_state(intent=intent, client_order_id=client_order_id)
        if blocker is None:
            return None
        operation = unknown_write_operation(blocker) or "submit"
        if blocker.execution_chain_id and intent.execution_chain_id and blocker.execution_chain_id == intent.execution_chain_id:
            execution_error = (
                f"unknown_{operation}_requires_reconciliation_for_execution_chain:{blocker.client_order_id}"
            )
            submission_mode = "unknown_write_duplicate_submit_blocked"
        elif blocker.intent_id == intent.intent_id:
            execution_error = f"unknown_{operation}_requires_reconciliation_for_intent:{blocker.client_order_id}"
            submission_mode = "unknown_write_duplicate_submit_blocked"
        elif blocker.client_order_id == client_order_id:
            execution_error = (
                f"unknown_{operation}_requires_reconciliation_for_client_order_id:{blocker.client_order_id}"
            )
            submission_mode = "unknown_write_duplicate_submit_blocked"
        else:
            execution_error = f"unknown_{operation}_blocks_new_risk_actions_for_symbol:{blocker.client_order_id}"
            submission_mode = "unknown_write_symbol_risk_blocked"
        return self._blocked_order_state_from_intent(
            intent=intent,
            client_order_id=client_order_id,
            submission_mode=submission_mode,
            execution_error=execution_error,
        )

    def _blocking_unknown_write_state(
        self,
        *,
        intent: OrderIntent,
        client_order_id: str,
    ) -> OrderState | None:
        non_terminal_query = getattr(self.execution_repo, "non_terminal_order_states", None)
        candidate_states = (
            non_terminal_query()
            if callable(non_terminal_query)
            else self.execution_repo.order_states()
        )
        unknown_states = [
            state
            for state in candidate_states
            if self._is_unknown_write_state(state) and not self.order_state_machine.is_terminal(state.status)
        ]
        if not unknown_states:
            return None
        for state in unknown_states:
            if state.execution_chain_id and intent.execution_chain_id and state.execution_chain_id == intent.execution_chain_id:
                return state
            if state.intent_id == intent.intent_id:
                return state
            if state.client_order_id == client_order_id:
                return state
        if is_risk_reducing_order_intent(intent):
            return None
        for state in unknown_states:
            if state.symbol == intent.symbol and blocks_new_risk_actions(state):
                return state
        return None

    async def cancel_order(self, client_order_id: str) -> OrderState:
        current = self.resolve_order_state_for_control(client_order_id)
        if current is None:
            raise KeyError(f"order_state_not_found client_order_id={client_order_id}")
        if self.order_state_machine.is_terminal(current.status):
            return current
        pre_submit_canceled = await self._cancel_pending_submit_before_exchange_ack(current)
        if pre_submit_canceled is not None:
            return pre_submit_canceled
        cancel_pending = current.model_copy(
            update={
                "status": "CANCEL_PENDING",
                "cancellation_requested_ts": utc_now(),
                "last_update_ts": utc_now(),
            }
        )
        persisted_pending = await self._persist_order_state(order_state=cancel_pending, key=current.symbol)
        if self.persistent_order_service is not None:
            self.persistent_order_service.enqueue_cancel(
                order_state=persisted_pending,
                reason="operator_requested_cancel",
            )
            return persisted_pending
        return await self._execute_cancel_from_state(persisted_pending)

    def request_cancel_exit_intent(self, parent_intent_id: str):
        if self.exit_execution_repo is None:
            raise KeyError("exit_execution_repo_not_configured")
        return self.exit_execution_writer.recompute_parent(
            parent_intent_id=parent_intent_id,
            transform_parent=request_cancel_exit_execution_intent,
            recompute_parent=lambda parent, child_refs: recompute_exit_execution_intent(
                parent_intent=parent,
                child_refs=child_refs,
            ),
            source_component="order_manager",
            reason_code="operator_request_cancel_parent",
        )

    async def retry_exit_execution_limit_lookup(self, parent_intent_id: str):
        if self.exit_execution_repo is None:
            raise KeyError("exit_execution_repo_not_configured")
        self._refresh_exit_execution_intents()
        parent = self.exit_execution_repo.get_exit_execution_intent(parent_intent_id)
        if parent is None:
            raise KeyError(f"exit_execution_intent_not_found parent_intent_id={parent_intent_id}")
        block_reason = resume_block_reason(parent)
        if block_reason is not None:
            raise ValueError(f"exit_execution_resume_blocked:{block_reason}")
        dispatched_state = await self._resume_exit_execution_parent(parent=parent)
        refreshed_parent = self.exit_execution_repo.get_exit_execution_intent(parent_intent_id) or parent
        return refreshed_parent, dispatched_state

    async def safe_cancel_exit_intent(self, parent_intent_id: str):
        if self.exit_execution_repo is None:
            raise KeyError("exit_execution_repo_not_configured")
        self._refresh_exit_execution_intents()
        parent = self.request_cancel_exit_intent(parent_intent_id)
        child_results: list[OrderState] = []
        skipped_children: list[dict[str, str]] = []
        seen_child_ids: set[str] = set()
        for child_ref in self.exit_execution_repo.child_refs_for_parent(parent_intent_id=parent_intent_id):
            client_order_id = str(child_ref.client_order_id or "").strip()
            if not client_order_id or client_order_id in seen_child_ids:
                continue
            seen_child_ids.add(client_order_id)
            current = self.resolve_order_state_for_control(client_order_id)
            if current is None:
                skipped_children.append(
                    {
                        "client_order_id": client_order_id,
                        "reason": "order_state_not_found",
                    }
                )
                continue
            if self.order_state_machine.is_terminal(current.status):
                skipped_children.append(
                    {
                        "client_order_id": client_order_id,
                        "reason": "already_terminal",
                        "status": current.status,
                    }
                )
                continue
            child_results.append(await self.cancel_order(client_order_id))
        refreshed_parent = self.exit_execution_repo.get_exit_execution_intent(parent_intent_id) or parent
        return refreshed_parent, child_results, skipped_children

    async def _execute_cancel_from_state(self, order_state: OrderState) -> OrderState:
        current = order_state
        pre_submit_canceled = await self._cancel_pending_submit_before_exchange_ack(current)
        if pre_submit_canceled is not None:
            return pre_submit_canceled
        if current.status != "CANCEL_PENDING":
            current = current.model_copy(
                update={
                    "status": "CANCEL_PENDING",
                    "cancellation_requested_ts": utc_now(),
                    "last_update_ts": utc_now(),
                }
            )
            current = await self._persist_order_state(order_state=current, key=current.symbol)
        pre_submit_canceled = await self._cancel_pending_submit_before_exchange_ack(current)
        if pre_submit_canceled is not None:
            return pre_submit_canceled
        state, fills = await self.adapter.cancel(current)
        atomic_persisted = await self._persist_order_state_with_fills_atomic(
            order_state=state,
            key=current.symbol,
            fills=fills,
        )
        if atomic_persisted is not None:
            return atomic_persisted
        persisted = await self._persist_order_state(
            order_state=state,
            key=current.symbol,
            obligation=self._terminal_outbox_obligation(order_state=state, fills=fills),
        )
        for fill in fills:
            await self._persist_fill(fill)
        self._finalize_obligation(order_state=persisted)
        return persisted

    def resolve_order_state_for_control(self, client_order_id: str) -> OrderState | None:
        current = self.execution_repo.get_order_state(client_order_id)
        if current is not None:
            return current
        row = self._phase2_execution_order_row(client_order_id)
        if row is None:
            return None
        return self._hydrate_order_state_from_execution_row(row)

    async def _persist_order_state(
        self,
        *,
        order_state: OrderState,
        key: str,
        obligation=None,
        intent: OrderIntent | None = None,
        command: dict | None = None,
    ) -> OrderState:
        if self.execution_outbox_publisher is not None:
            if command is not None and self._submit_command_persisted_transactionally():
                persisted = await self.execution_outbox_publisher.persist_order_state_and_command(
                    order_state=order_state,
                    key=key,
                    obligation=obligation,
                    command_id=str(command["command_id"]),
                    command_type=str(command["command_type"]),
                    command_idempotency_key=str(command["idempotency_key"]),
                    command_payload=dict(command["payload"]),
                    command_created_at=command["created_at"],
                )
            else:
                persisted = await self.execution_outbox_publisher.persist_order_state(
                    order_state=order_state,
                    key=key,
                    obligation=obligation,
                )
            log_event(
                self.logger,
                "order_state_persisted",
                **correlation_fields(
                    decision_id=persisted.decision_id,
                    intent_id=persisted.intent_id,
                    order_id=persisted.client_order_id,
                    status=persisted.status,
                    venue=persisted.venue,
                    submission_mode=persisted.submission_mode,
                    execution_error=persisted.execution_error,
                ),
            )
            self._shadow_write_order_state(order_state=persisted, intent=intent)
            self._sync_strategy_bundle_status(order_state=persisted)
            self._sync_exit_execution_intent(order_state=persisted, intent=intent)
            return persisted
        previous = self.execution_repo.get_order_state(order_state.client_order_id)
        persisted = save_order_state_direct_legacy_only(
            execution_repo=self.execution_repo,
            order_state=order_state,
            source_component="execution_engine",
            logger=self.logger,
        )
        log_event(
            self.logger,
            "order_state_persisted",
            **correlation_fields(
                decision_id=persisted.decision_id,
                intent_id=persisted.intent_id,
                order_id=persisted.client_order_id,
                status=persisted.status,
                venue=persisted.venue,
                submission_mode=persisted.submission_mode,
                execution_error=persisted.execution_error,
            ),
        )
        await publish_model(
            bus=self.bus,
            topic=topics.ORDER_UPDATES,
            key=key,
            payload_model=persisted,
            source_component="execution_engine",
        )
        await self._publish_execution_error_summary(previous=previous, persisted=persisted)
        self._shadow_write_order_state(order_state=persisted, intent=intent)
        self._sync_strategy_bundle_status(order_state=persisted)
        self._sync_exit_execution_intent(order_state=persisted, intent=intent)
        return persisted

    def _sync_exit_execution_intent(
        self,
        *,
        order_state: OrderState,
        intent: OrderIntent | None = None,
    ) -> None:
        if self.exit_execution_repo is None:
            return
        parent = self._ensure_exit_execution_intent(order_state=order_state, intent=intent)
        if parent is None:
            return
        child_ref = child_exit_order_ref_from_order_state(
            parent_intent_id=parent.parent_intent_id,
            order_state=order_state,
            settings=self.settings,
        )
        self.exit_execution_writer.save_child_ref_and_recompute_parent(
            parent_intent=parent,
            child_ref=child_ref,
            recompute_parent=lambda parent_intent, child_refs: recompute_exit_execution_intent(
                parent_intent=parent_intent,
                child_refs=child_refs,
            ),
            source_component="order_manager",
            reason_code="sync_child_ref_from_order_state",
        )

    def _ensure_exit_execution_intent(
        self,
        *,
        order_state: OrderState,
        intent: OrderIntent | None = None,
    ):
        if self.exit_execution_repo is None:
            return None
        existing_parent_id = self.exit_execution_repo.parent_intent_id_for_child(
            client_order_id=order_state.client_order_id,
        )
        if existing_parent_id is not None:
            return self.exit_execution_repo.get_exit_execution_intent(existing_parent_id)
        execution_chain_id = str(
            order_state.execution_chain_id
            or (intent.execution_chain_id if intent is not None else "")
            or order_state.intent_id
        )
        if execution_chain_id:
            existing = self.exit_execution_repo.get_exit_execution_intent_by_execution_chain(execution_chain_id)
            if existing is not None:
                if intent is not None and dispatch_template_from_parent(existing) is None:
                    return self._save_exit_execution_intent_with_template(parent=existing, intent=intent)
                return existing
        if intent is not None and is_risk_reducing_order_intent(intent):
            parent = create_exit_execution_intent_from_order_intent(intent)
            return self._save_exit_execution_intent_with_template(parent=parent, intent=intent)
        if is_risk_reducing_order_state(order_state):
            parent = create_exit_execution_intent_from_order_state(order_state)
            return self._save_exit_execution_parent(parent)
        return None

    def _save_exit_execution_intent_with_template(self, *, parent, intent: OrderIntent):
        if self.exit_execution_repo is None:
            return parent
        if ":slice:" in str(intent.intent_id or ""):
            return self._save_exit_execution_parent(parent)
        metadata = dict(parent.metadata)
        metadata["dispatch_template"] = intent.model_dump(mode="json")
        metadata["dispatch_template_version"] = 1
        saved_parent = parent.model_copy(update={"metadata": metadata})
        return self._save_exit_execution_parent(saved_parent)

    def _save_exit_execution_parent(self, parent):
        if self.exit_execution_repo is None:
            return parent
        return self.exit_execution_writer.save_exit_execution_intent(
            parent,
            source_component="order_manager",
            reason_code="save_exit_execution_parent",
        )

    def _sync_strategy_bundle_status(self, *, order_state: OrderState) -> None:
        if self.strategy_runtime_repo is None:
            return
        bundle_id = str(order_state.strategy_bundle_id or "").strip()
        if not bundle_id:
            return
        bundle = self.strategy_runtime_repo.get_execution_bundle(bundle_id)
        if bundle is None:
            return
        indexed_query = getattr(self.execution_repo, "order_states_by_bundle_id", None)
        bundle_order_states = (
            indexed_query(bundle_id)
            if callable(indexed_query)
            else [
                state
                for state in self.execution_repo.order_states()
                if str(state.strategy_bundle_id or "").strip() == bundle_id
            ]
        )
        derived_status = derive_strategy_bundle_status(
            order_states=bundle_order_states,
            previous_status=bundle.status,
        )
        if derived_status == bundle.status:
            return
        self.strategy_runtime_repo.save_execution_bundle(
            bundle.model_copy(
                update={
                    "status": derived_status,
                    "reason_codes": apply_strategy_bundle_status_reason_codes(
                        reason_codes=list(bundle.reason_codes),
                        status=derived_status,
                    ),
                }
            )
        )

    def _submit_command_persisted_transactionally(self) -> bool:
        if self.execution_outbox_publisher is None:
            return False
        return getattr(self.execution_outbox_publisher, "execution_command_repo", None) is not None

    async def _persist_fill(self, fill: FillEvent) -> None:
        obligation = None
        mirrored_obligation = None
        if self.obligation_service is not None and self.execution_outbox_publisher is not None:
            obligation = self.obligation_service.preview_obligation_for_fill(fill)
            mirrored_obligation = obligation
        if self.execution_outbox_publisher is not None:
            saved = await self.execution_outbox_publisher.persist_fill(fill=fill, obligation=obligation)
            if not saved:
                return
            self._shadow_write_fill(fill)
            self._shadow_sync_obligation(mirrored_obligation, reason="fill_settlement", related_fill=fill)
            return
        elif not save_fill_direct_legacy_only(
            execution_repo=self.execution_repo,
            fill=fill,
            source_component="execution_engine",
            logger=self.logger,
        ):
            return
        log_event(
            self.logger,
            "fill_event_created",
            **correlation_fields(
                decision_id=fill.decision_id,
                intent_id=fill.intent_id,
                order_id=fill.client_order_id,
                fill_id=fill.fill_id,
                symbol=fill.symbol,
                fill_qty=fill.fill_qty,
                fill_price=fill.fill_price,
                venue=fill.venue,
            ),
        )
        await publish_model(
            bus=self.bus,
            topic=topics.FILL_EVENTS,
            key=fill.symbol,
            payload_model=fill,
            source_component="execution_engine",
        )
        self._shadow_write_fill(fill)
        if self.obligation_service is not None:
            mirrored_obligation = self.obligation_service.consume_for_fill(fill)
        self._shadow_sync_obligation(mirrored_obligation, reason="fill_settlement", related_fill=fill)

    async def _publish_execution_error_summary(
        self,
        *,
        previous: OrderState | None,
        persisted: OrderState,
    ) -> None:
        if persisted.status not in {"FAILED", "REJECTED", "BLOCKED"}:
            return
        if previous is not None and previous.status == persisted.status and previous.execution_error == persisted.execution_error:
            return
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
        await publish_model(
            bus=self.bus,
            topic=topics.EXECUTION_ERROR_SUMMARIES,
            key=persisted.symbol,
            payload_model=summary,
            source_component="execution_engine",
        )

    def _finalize_obligation(self, *, order_state: OrderState) -> None:
        if self.obligation_service is None:
            return
        if self.execution_outbox_publisher is not None:
            current = self.obligation_service.obligation_repo.get_obligation(order_state.client_order_id)
            obligation = self.obligation_service.preview_obligation_for_order_state(order_state)
            if (
                obligation is not None
                and (
                    current is None
                    or current.model_dump(mode="json") != obligation.model_dump(mode="json")
                )
            ):
                obligation = self.execution_outbox_publisher.persist_obligation_sync(
                    obligation=obligation,
                    source_component="execution_engine",
                    reason_code="finalize_for_order_state",
                )
            self._shadow_sync_obligation(obligation, reason="reservation_release", related_fill=None)
            return
        obligation = self.obligation_service.finalize_for_order_state(order_state)
        self._shadow_sync_obligation(obligation, reason="reservation_release", related_fill=None)

    def _terminal_outbox_obligation(self, *, order_state: OrderState, fills: list) -> object | None:
        if self.execution_outbox_publisher is None or self.obligation_service is None:
            return None
        if fills:
            return None
        if order_state.status not in {"CANCELED", "REJECTED", "FAILED", "BLOCKED", "DRY_RUN", "EXPIRED"}:
            return None
        if abs(order_state.filled_qty) > self._OBLIGATION_ATOMIC_FINALIZE_EPSILON:
            return None
        return self.obligation_service.preview_obligation_for_order_state(order_state)

    def _transient_close_retry_cooldown_state(self, *, intent: OrderIntent) -> OrderState | None:
        cooldown_seconds = max(self.settings.strategy_transient_close_retry_cooldown_seconds, 0.0)
        if cooldown_seconds <= 0.0:
            return None
        if intent.position_intent not in {"close_long", "close_short"}:
            return None
        if intent.urgency == "high":
            return None
        cutoff = utc_now() - timedelta(seconds=cooldown_seconds)
        for state in self.execution_repo.recent_order_states(limit=25, statuses=("FAILED", "BLOCKED")):
            if state.symbol != intent.symbol or state.position_intent != intent.position_intent:
                continue
            observed_at = state.last_update_ts or state.created_at
            if observed_at < cutoff:
                continue
            if abs(state.requested_qty - intent.quantity) > max(intent.quantity * Decimal("0.2"), Decimal("1e-8")):
                continue
            error_text = f"{state.execution_error or ''} {state.cancel_reason or ''}".lower()
            if not any(pattern in error_text for pattern in self._TRANSIENT_RETRY_PATTERNS):
                continue
            return OrderState(
                decision_id=intent.decision_id,
                execution_chain_id=intent.execution_chain_id,
                execution_attempt_id=(
                    intent.execution_attempt_id
                    or execution_attempt_id_from_components(
                        client_order_id=intent.idempotency_key,
                        execution_chain_id=intent.execution_chain_id,
                        intent_id=intent.intent_id,
                    )
                ),
                intent_id=intent.intent_id,
                symbol=intent.symbol,
                client_order_id=intent.idempotency_key or new_id("clord"),
                venue="OKX" if self.adapter.readiness().get("backend") == "okx" else "PAPER",
                exchange_order_id=None,
                status="BLOCKED",
                submission_mode="local_retry_cooldown",
                submitted_ts=None,
                last_update_ts=utc_now(),
                requested_qty=intent.quantity,
                filled_qty=Decimal("0"),
                remaining_qty=intent.quantity,
                average_fill_price=None,
                fees=Decimal("0"),
                reduce_only=effective_reduce_only_for_intent(intent),
                close_only=effective_close_only_for_intent(intent),
                td_mode=intent.td_mode,
                position_mode=intent.position_mode,
                pos_side=intent.pos_side,
                reduce_only_reason=intent.reduce_only_reason,
                close_only_reason=intent.close_only_reason,
                instrument_family=intent.instrument_family,
                settle_currency=intent.settle_currency,
                product_type=intent.product_type,
                target_leverage=intent.target_leverage,
                margin_mode=intent.margin_mode,
                exposure_side=intent.exposure_side,
                execution_action=intent.execution_action,
                leg_action=intent.leg_action,
                position_intent=intent.position_intent,
                leg_intent_id=intent.leg_intent_id,
                strategy_family=intent.strategy_family,
                strategy_sleeve_id=intent.strategy_sleeve_id,
                allocation_id=intent.allocation_id,
                strategy_bundle_id=intent.strategy_bundle_id,
                strategy_leg_role=intent.strategy_leg_role,
                strategy_pair_id=intent.strategy_pair_id,
                strategy_opportunity_kind=intent.strategy_opportunity_kind,
                strategy_execution_mode=intent.strategy_execution_mode,
                strategy_state_phase=intent.strategy_state_phase,
                market_snapshot_ref=intent.market_snapshot_ref,
                feature_snapshot_ref=intent.feature_snapshot_ref,
                portfolio_snapshot_ref=intent.portfolio_snapshot_ref,
                health_snapshot_ref=intent.health_snapshot_ref,
                execution_error=f"transient_close_retry_cooldown_active:{state.execution_error or state.cancel_reason or 'transient_exchange_failure'}",
                submission_payload={},
            )
        return None

    @staticmethod
    def _apply_leg_risk_context(
        *,
        intent: OrderIntent,
        leg_intent: LegOrderIntent,
        risk_decision: object,
    ) -> tuple[OrderIntent, LegOrderIntent]:
        update_payload = {
            "required_initial_margin": getattr(risk_decision, "required_initial_margin", None),
            "projected_margin_usage": getattr(risk_decision, "projected_margin_usage", None),
            "projected_notional": getattr(risk_decision, "projected_notional", None),
            "risk_budget_multiplier": getattr(risk_decision, "risk_budget_multiplier", None),
            "risk_budget_state": dict(getattr(risk_decision, "risk_budget_state", {}) or {}),
            "execution_aggressiveness_multiplier": getattr(
                risk_decision,
                "execution_aggressiveness_multiplier",
                None,
            ),
            "execution_aggressiveness_state": dict(
                getattr(risk_decision, "execution_aggressiveness_state", {}) or {}
            ),
            "only_reduce_required": bool(getattr(risk_decision, "only_reduce_required", False)),
            "risk_limit_breached": bool(getattr(risk_decision, "risk_limit_breached", False)),
            "liquidation_buffer_remaining": getattr(
                risk_decision,
                "liquidation_buffer_remaining",
                None,
            ),
        }
        return (
            intent.model_copy(update=update_payload),
            leg_intent.model_copy(update=update_payload),
        )

    @staticmethod
    def _leg_risk_blocked_error(risk_decision: object) -> str:
        reasons = [
            str(item)
            for item in (getattr(risk_decision, "rejection_reasons", []) or [])
            if str(item).strip()
        ]
        if not reasons:
            reasons = ["leg_only_reduce_mode_active"]
        return f"leg_risk_blocked:{','.join(dict.fromkeys(reasons))}"

    def _leg_overlay_rollout_blockers(self, *, leg_intent: LegOrderIntent) -> list[str]:
        if (
            str(leg_intent.product_type or "") != "derivatives"
            or str(leg_intent.position_mode or "") != "long_short_mode"
        ):
            return []
        overlay_mode = overlay_mode_from_execution_mode(leg_intent.strategy_execution_mode)
        if overlay_mode is None:
            return []
        blockers: list[str] = []
        if not self.settings.strategy_hedge_overlay_enabled:
            blockers.append("strategy_hedge_overlay_disabled")
        if overlay_mode in {"protective", "opportunistic"}:
            blockers.append(f"{overlay_mode}_overlay_retired")
        if overlay_mode == "independent" and not self.settings.strategy_hedge_independent_enabled:
            blockers.append("strategy_hedge_independent_disabled")
        if blockers:
            return list(dict.fromkeys(blockers))
        rollout = overlay_rollout_status(self.settings, mode=overlay_mode)
        return [str(item) for item in (rollout.get("blocking_reasons") or []) if str(item).strip()]

    @staticmethod
    def _leg_overlay_rollout_blocked_error(reasons: list[str]) -> str:
        cleaned = [str(item) for item in reasons if str(item).strip()]
        if not cleaned:
            cleaned = ["overlay_rollout_blocked"]
        return f"leg_overlay_rollout_blocked:{','.join(dict.fromkeys(cleaned))}"

    def _apply_leg_submit_guards(
        self,
        *,
        intent: OrderIntent,
        client_order_id: str,
        leg_intent: LegOrderIntent | None = None,
    ) -> tuple[OrderIntent, LegOrderIntent | None, OrderState | None]:
        normalized_leg_intent = leg_intent or leg_intent_from_order_intent(intent)
        if normalized_leg_intent is None:
            return intent, None, None
        guarded_intent = intent
        guarded_leg_intent = normalized_leg_intent
        if self.leg_risk_evaluator is not None:
            risk_decision = self.leg_risk_evaluator(guarded_leg_intent)
            guarded_intent, guarded_leg_intent = self._apply_leg_risk_context(
                intent=guarded_intent,
                leg_intent=guarded_leg_intent,
                risk_decision=risk_decision,
            )
            if not bool(getattr(risk_decision, "approved", False)):
                return (
                    guarded_intent,
                    guarded_leg_intent,
                    self._blocked_order_state_from_intent(
                        intent=guarded_intent,
                        client_order_id=client_order_id,
                        submission_mode="leg_risk_blocked",
                        execution_error=self._leg_risk_blocked_error(risk_decision),
                    ),
                )
        rollout_blockers = self._leg_overlay_rollout_blockers(leg_intent=guarded_leg_intent)
        if rollout_blockers:
            return (
                guarded_intent,
                guarded_leg_intent,
                self._blocked_order_state_from_intent(
                    intent=guarded_intent,
                    client_order_id=client_order_id,
                    submission_mode="leg_overlay_rollout_blocked",
                    execution_error=self._leg_overlay_rollout_blocked_error(rollout_blockers),
                ),
            )
        return guarded_intent, guarded_leg_intent, None

    def _blocked_order_state_from_intent(
        self,
        *,
        intent: OrderIntent,
        client_order_id: str,
        submission_mode: str,
        execution_error: str,
    ) -> OrderState:
        return OrderState(
            decision_id=intent.decision_id,
            execution_chain_id=intent.execution_chain_id,
            execution_attempt_id=intent.execution_attempt_id,
            intent_id=intent.intent_id,
            symbol=intent.symbol,
            client_order_id=client_order_id,
            venue="OKX" if self.adapter.readiness().get("backend") == "okx" else "PAPER",
            exchange_order_id=None,
            status="BLOCKED",
            submission_mode=submission_mode,
            submitted_ts=None,
            last_update_ts=utc_now(),
            requested_qty=intent.quantity,
            filled_qty=Decimal("0"),
            remaining_qty=intent.quantity,
            average_fill_price=None,
            fees=Decimal("0"),
            reduce_only=effective_reduce_only_for_intent(intent),
            close_only=effective_close_only_for_intent(intent),
            td_mode=intent.td_mode,
            position_mode=intent.position_mode,
            pos_side=intent.pos_side,
            reduce_only_reason=intent.reduce_only_reason,
            close_only_reason=intent.close_only_reason,
            instrument_family=intent.instrument_family,
            settle_currency=intent.settle_currency,
            product_type=intent.product_type,
            target_leverage=intent.target_leverage,
            margin_mode=intent.margin_mode,
            exposure_side=intent.exposure_side,
            execution_action=intent.execution_action,
            leg_action=intent.leg_action,
            position_intent=intent.position_intent,
            leg_intent_id=intent.leg_intent_id,
            strategy_family=intent.strategy_family,
            strategy_sleeve_id=intent.strategy_sleeve_id,
            allocation_id=intent.allocation_id,
            strategy_bundle_id=intent.strategy_bundle_id,
            strategy_leg_role=intent.strategy_leg_role,
            strategy_pair_id=intent.strategy_pair_id,
            strategy_opportunity_kind=intent.strategy_opportunity_kind,
            strategy_execution_mode=intent.strategy_execution_mode,
            strategy_state_phase=intent.strategy_state_phase,
            market_snapshot_ref=intent.market_snapshot_ref,
            feature_snapshot_ref=intent.feature_snapshot_ref,
            portfolio_snapshot_ref=intent.portfolio_snapshot_ref,
            health_snapshot_ref=intent.health_snapshot_ref,
            execution_error=execution_error,
            submission_payload={},
        )

    async def _cancel_pending_submit_before_exchange_ack(self, order_state: OrderState) -> OrderState | None:
        if self.persistent_order_service is None:
            return None
        if order_state.exchange_order_id is not None:
            return None
        if order_state.status not in {"CREATED", "SUBMITTING", "CANCEL_PENDING"}:
            return None
        submit_command = self._lookup_submit_command(
            client_order_id=order_state.client_order_id,
            intent_id=order_state.intent_id,
        )
        command_state = str(submit_command.get("state") or "").upper() if submit_command is not None else None
        if submit_command is not None and command_state not in {"PENDING", "ACKED", "FAILED"}:
            return None
        now = utc_now()
        if submit_command is not None and command_state == "PENDING":
            self.persistent_order_service.execution_command_repo.mark_abandoned(
                str(submit_command["command_id"]),
                reason="operator_cancel_before_submit",
                updated_at=now,
            )
        canceled = order_state.model_copy(
            update={
                "status": "CANCELED",
                "canceled_ts": now,
                "last_update_ts": now,
                "cancel_reason": order_state.cancel_reason or "operator_cancel_before_submit",
                "execution_error": None,
            }
        )
        persisted = await self._persist_order_state(
            order_state=canceled,
            key=order_state.symbol,
            obligation=self._terminal_outbox_obligation(order_state=canceled, fills=[]),
        )
        self._finalize_obligation(order_state=persisted)
        return persisted

    def _lookup_submit_command(
        self,
        *,
        client_order_id: str | None,
        intent_id: str | None,
    ) -> dict | None:
        if self.persistent_order_service is None:
            return None
        repo = self.persistent_order_service.execution_command_repo
        for key in self.persistent_order_service.submit_command_lookup_keys(
            client_order_id=client_order_id,
            intent_id=intent_id,
        ):
            command = repo.get_by_idempotency_key(key)
            if command is not None:
                return command
        return None

    @staticmethod
    def _apply_execution_attempt_id(
        *,
        intent: OrderIntent,
        client_order_id: str,
        leg_intent: LegOrderIntent | None = None,
    ) -> tuple[OrderIntent, LegOrderIntent | None]:
        attempt_id = execution_attempt_id_from_components(
            execution_attempt_id=intent.execution_attempt_id,
            client_order_id=client_order_id,
            execution_chain_id=intent.execution_chain_id,
            intent_id=intent.intent_id,
        )
        updated_intent = (
            intent
            if attempt_id == intent.execution_attempt_id
            else intent.model_copy(update={"execution_attempt_id": attempt_id})
        )
        if leg_intent is None:
            return updated_intent, None
        updated_leg_intent = (
            leg_intent
            if attempt_id == leg_intent.execution_attempt_id
            else leg_intent.model_copy(update={"execution_attempt_id": attempt_id})
        )
        return updated_intent, updated_leg_intent

    def _phase2_execution_order_row(self, client_order_id: str) -> dict | None:
        execution_order_repo = getattr(self.persistent_order_service, "execution_order_repo", None)
        if execution_order_repo is not None:
            row = execution_order_repo.get_order_by_client_order_id(client_order_id)
            if row is not None:
                return row
        execution_shadow_repo = getattr(self.shadow_execution_service, "execution_order_repo", None)
        if execution_shadow_repo is not None:
            return execution_shadow_repo.get_order_by_client_order_id(client_order_id)
        return None

    @staticmethod
    def _is_missing_payload_value(value) -> bool:
        return value is None or value == ""

    @staticmethod
    def _looks_like_order_state_payload(payload: dict) -> bool:
        required_keys = {
            "decision_id",
            "intent_id",
            "symbol",
            "client_order_id",
            "status",
            "requested_qty",
            "remaining_qty",
        }
        return required_keys.issubset(payload.keys()) and (
            "filled_qty" in payload or "exchange_status" in payload
        )

    def _hydrate_order_state_from_execution_row(self, row: dict) -> OrderState:
        def _aware(value):
            if value is None:
                return None
            if getattr(value, "tzinfo", None) is None:
                return value.replace(tzinfo=timezone.utc)
            return value

        def _set_missing(payload: dict, key: str, value) -> None:
            if self._is_missing_payload_value(payload.get(key)) and not self._is_missing_payload_value(value):
                payload[key] = value

        raw_payload = dict(row.get("raw_payload") or {})
        order_payload = raw_payload.get("order_state")
        if isinstance(order_payload, dict):
            payload = dict(order_payload)
            submission_payload = payload.get("submission_payload")
            if not isinstance(submission_payload, dict):
                submission_payload = {}
            payload.setdefault("decision_id", row.get("decision_id"))
            payload.setdefault(
                "execution_chain_id",
                raw_payload.get("execution_chain_id") or submission_payload.get("executionChainId"),
            )
            payload.setdefault(
                "execution_attempt_id",
                raw_payload.get("execution_attempt_id") or submission_payload.get("executionAttemptId"),
            )
            payload.setdefault("intent_id", row.get("intent_id"))
            payload.setdefault("symbol", row.get("symbol"))
            payload.setdefault("client_order_id", row.get("client_order_id") or row.get("order_id"))
            payload.setdefault("exchange_order_id", row.get("venue_order_id"))
            payload.setdefault("status", row.get("state"))
            payload.setdefault("requested_qty", row.get("requested_qty"))
            payload.setdefault("remaining_qty", row.get("requested_qty"))
            payload.setdefault("product_type", row.get("product_type"))
            payload.setdefault("margin_mode", row.get("margin_mode"))
            payload.setdefault("target_leverage", row.get("raw_payload", {}).get("target_leverage", 1.0))
            payload.setdefault("reduce_only", row.get("reduce_only", False))
            payload.setdefault("close_only", row.get("close_only", False))
            payload.setdefault(
                "td_mode",
                row.get("td_mode") or submission_payload.get("tdMode") or row.get("margin_mode"),
            )
            payload.setdefault("position_mode", row.get("position_mode"))
            payload.setdefault("pos_side", row.get("pos_side") or submission_payload.get("posSide"))
            payload.setdefault("reduce_only_reason", row.get("reduce_only_reason"))
            payload.setdefault("close_only_reason", row.get("close_only_reason"))
            payload.setdefault("instrument_family", row.get("instrument_family"))
            payload.setdefault("settle_currency", row.get("settle_currency"))
            payload.setdefault("position_intent", row.get("position_intent") or "open_long")
            payload.setdefault("execution_action", row.get("execution_action"))
            payload.setdefault("strategy_family", raw_payload.get("strategy_family"))
            payload.setdefault("strategy_sleeve_id", raw_payload.get("strategy_sleeve_id") or row.get("strategy_sleeve_id"))
            payload.setdefault("allocation_id", raw_payload.get("allocation_id") or row.get("allocation_id"))
            payload.setdefault("strategy_bundle_id", raw_payload.get("strategy_bundle_id"))
            payload.setdefault("strategy_leg_role", raw_payload.get("strategy_leg_role"))
            # Execution truth snapshot refs 优先取已持久化的 order_state payload 值，
            # 缺失则回退到 raw_payload 顶层锚点（outbox/order_service 在落库时写入）。
            payload.setdefault("market_snapshot_ref", raw_payload.get("market_snapshot_ref"))
            payload.setdefault("feature_snapshot_ref", raw_payload.get("feature_snapshot_ref"))
            payload.setdefault("portfolio_snapshot_ref", raw_payload.get("portfolio_snapshot_ref"))
            payload.setdefault("health_snapshot_ref", raw_payload.get("health_snapshot_ref"))
            payload.setdefault("submission_payload", submission_payload)
            if payload.get("pos_side") in {"", None}:
                payload["pos_side"] = row.get("pos_side") or submission_payload.get("posSide") or None
            return OrderState.model_validate(payload)
        if self._looks_like_order_state_payload(raw_payload):
            payload = dict(raw_payload)
            submission_payload = payload.get("submission_payload")
            if not isinstance(submission_payload, dict):
                submission_payload = {}
            row_state = str(row.get("state") or payload.get("status") or "CREATED")
            payload["status"] = row_state
            _set_missing(payload, "decision_id", row.get("decision_id"))
            _set_missing(
                payload,
                "execution_chain_id",
                raw_payload.get("execution_chain_id") or submission_payload.get("executionChainId"),
            )
            _set_missing(
                payload,
                "execution_attempt_id",
                raw_payload.get("execution_attempt_id")
                or row.get("execution_attempt_id")
                or submission_payload.get("executionAttemptId"),
            )
            _set_missing(payload, "intent_id", row.get("intent_id"))
            _set_missing(payload, "symbol", row.get("symbol"))
            _set_missing(payload, "client_order_id", row.get("client_order_id") or row.get("order_id"))
            if not self._is_missing_payload_value(row.get("venue_order_id")):
                payload["exchange_order_id"] = row.get("venue_order_id")
            _set_missing(payload, "venue", "OKX" if self.adapter.readiness().get("backend") == "okx" else "PAPER")
            _set_missing(payload, "submission_mode", raw_payload.get("source_system") or "phase2_execution_order_repo")
            _set_missing(payload, "requested_qty", row.get("requested_qty"))
            _set_missing(payload, "remaining_qty", row.get("requested_qty"))
            _set_missing(payload, "filled_qty", Decimal("0"))
            _set_missing(payload, "fees", Decimal("0"))
            created_at = _aware(row.get("created_at")) or utc_now()
            updated_at = _aware(row.get("updated_at")) or created_at
            _set_missing(payload, "submitted_ts", created_at if row_state != "CREATED" else None)
            _set_missing(payload, "last_update_ts", updated_at)
            _set_missing(payload, "last_exchange_update_ts", _aware(row.get("last_exchange_ts")))
            _set_missing(payload, "product_type", row.get("product_type"))
            _set_missing(payload, "margin_mode", row.get("margin_mode"))
            _set_missing(payload, "target_leverage", raw_payload.get("target_leverage", 1.0))
            _set_missing(payload, "reduce_only", row.get("reduce_only", False))
            _set_missing(payload, "close_only", row.get("close_only", False))
            _set_missing(payload, "td_mode", row.get("td_mode") or submission_payload.get("tdMode") or row.get("margin_mode"))
            _set_missing(payload, "position_mode", row.get("position_mode"))
            _set_missing(payload, "pos_side", row.get("pos_side") or submission_payload.get("posSide"))
            _set_missing(payload, "reduce_only_reason", row.get("reduce_only_reason"))
            _set_missing(payload, "close_only_reason", row.get("close_only_reason"))
            _set_missing(payload, "instrument_family", row.get("instrument_family"))
            _set_missing(payload, "settle_currency", row.get("settle_currency"))
            _set_missing(payload, "position_intent", row.get("position_intent") or "open_long")
            _set_missing(payload, "execution_action", row.get("execution_action"))
            _set_missing(payload, "strategy_family", row.get("strategy_family"))
            _set_missing(payload, "strategy_sleeve_id", row.get("strategy_sleeve_id"))
            _set_missing(payload, "allocation_id", row.get("allocation_id"))
            _set_missing(payload, "strategy_bundle_id", row.get("strategy_bundle_id"))
            _set_missing(payload, "strategy_leg_role", row.get("strategy_leg_role"))
            _set_missing(payload, "market_snapshot_ref", raw_payload.get("market_snapshot_ref"))
            _set_missing(payload, "feature_snapshot_ref", raw_payload.get("feature_snapshot_ref"))
            _set_missing(payload, "portfolio_snapshot_ref", raw_payload.get("portfolio_snapshot_ref"))
            _set_missing(payload, "health_snapshot_ref", raw_payload.get("health_snapshot_ref"))
            payload["submission_payload"] = submission_payload
            if payload.get("pos_side") in {"", None}:
                payload["pos_side"] = row.get("pos_side") or submission_payload.get("posSide") or None
            return OrderState.model_validate(payload)
        submission_mode = str(raw_payload.get("source_system") or "phase2_execution_order_repo")
        venue = str(raw_payload.get("venue") or ("OKX" if self.adapter.readiness().get("backend") == "okx" else "PAPER"))
        created_at = _aware(row.get("created_at")) or utc_now()
        updated_at = _aware(row.get("updated_at")) or created_at
        requested_qty = row.get("requested_qty")
        return OrderState(
            decision_id=str(row.get("decision_id") or ""),
            execution_chain_id=raw_payload.get("execution_chain_id"),
            execution_attempt_id=(
                raw_payload.get("execution_attempt_id")
                or execution_attempt_id_from_components(
                    client_order_id=str(row.get("client_order_id") or row.get("order_id") or ""),
                    execution_chain_id=raw_payload.get("execution_chain_id"),
                    intent_id=str(row.get("intent_id") or ""),
                )
            ),
            intent_id=str(row.get("intent_id") or ""),
            symbol=str(row.get("symbol") or self.settings.default_symbol),
            client_order_id=str(row.get("client_order_id") or row.get("order_id") or ""),
            venue=venue,
            exchange_order_id=row.get("venue_order_id"),
            status=str(row.get("state") or "CREATED"),
            submission_mode=submission_mode,
            submitted_ts=created_at if str(row.get("state") or "CREATED") != "CREATED" else None,
            last_update_ts=updated_at,
            last_exchange_update_ts=_aware(row.get("last_exchange_ts")),
            requested_qty=requested_qty,
            filled_qty=Decimal("0"),
            remaining_qty=requested_qty,
            average_fill_price=None,
            fees=Decimal("0"),
            reduce_only=bool(raw_payload.get("reduce_only", False)),
            close_only=bool(raw_payload.get("close_only", False)),
            td_mode=str(
                raw_payload.get("td_mode")
                or raw_payload.get("submission_payload", {}).get("tdMode")
                or row.get("margin_mode")
                or "cash"
            ),
            position_mode=raw_payload.get("position_mode"),
            pos_side=raw_payload.get("pos_side") or raw_payload.get("submission_payload", {}).get("posSide") or None,
            reduce_only_reason=raw_payload.get("reduce_only_reason"),
            close_only_reason=raw_payload.get("close_only_reason"),
            instrument_family=raw_payload.get("instrument_family"),
            settle_currency=raw_payload.get("settle_currency"),
            product_type=row.get("product_type") or "spot",
            target_leverage=float(raw_payload.get("target_leverage") or 1.0),
            margin_mode=row.get("margin_mode") or "cash",
            exposure_side=str(raw_payload.get("exposure_side") or "flat"),
            execution_action=row.get("execution_action"),
            position_intent=str(row.get("position_intent") or "open_long"),
            strategy_family=raw_payload.get("strategy_family"),
            strategy_sleeve_id=raw_payload.get("strategy_sleeve_id") or row.get("strategy_sleeve_id"),
            allocation_id=raw_payload.get("allocation_id") or row.get("allocation_id"),
            strategy_bundle_id=raw_payload.get("strategy_bundle_id"),
            strategy_leg_role=raw_payload.get("strategy_leg_role"),
            market_snapshot_ref=raw_payload.get("market_snapshot_ref"),
            feature_snapshot_ref=raw_payload.get("feature_snapshot_ref"),
            portfolio_snapshot_ref=raw_payload.get("portfolio_snapshot_ref"),
            health_snapshot_ref=raw_payload.get("health_snapshot_ref"),
            submission_payload={},
        )

    def _shadow_write_order_state(self, *, order_state: OrderState, intent: OrderIntent | None = None) -> None:
        if self.shadow_execution_service is None:
            return
        try:
            self.shadow_execution_service.shadow_order_state(order_state=order_state, intent=intent)
        except Exception as exc:
            log_event(
                self.logger,
                "shadow_order_state_write_failed",
                level="warning",
                **correlation_fields(
                    decision_id=order_state.decision_id,
                    intent_id=order_state.intent_id,
                    order_id=order_state.client_order_id,
                    status=order_state.status,
                    error=str(exc),
                ),
            )

    def _shadow_write_fill(self, fill: FillEvent) -> None:
        if self.shadow_execution_service is None:
            return
        try:
            self.shadow_execution_service.shadow_fill(fill)
        except Exception as exc:
            log_event(
                self.logger,
                "shadow_fill_write_failed",
                level="warning",
                **correlation_fields(
                    decision_id=fill.decision_id,
                    intent_id=fill.intent_id,
                    order_id=fill.client_order_id,
                    fill_id=fill.fill_id,
                    error=str(exc),
                ),
            )

    @staticmethod
    def _build_legacy_shadow_execution_service(
        *,
        shadow_execution_order_repo: ExecutionOrderRepository | None,
        shadow_execution_order_history_repo: ExecutionOrderHistoryRepository | None,
        shadow_execution_fill_repo: ExecutionFillRepositoryV2 | None,
    ) -> Phase1ExecutionShadowService | None:
        if shadow_execution_order_repo is None:
            return None
        return Phase1ExecutionShadowService(
            execution_order_repo=shadow_execution_order_repo,
            execution_order_history_repo=shadow_execution_order_history_repo,
            execution_fill_repo=shadow_execution_fill_repo,
        )

    def _shadow_sync_obligation(
        self,
        obligation: OrderObligation | None,
        *,
        reason: str,
        related_fill: FillEvent | None,
    ) -> None:
        if obligation is None or self.shadow_ledger_mirror_service is None:
            return
        try:
            self.shadow_ledger_mirror_service.sync_obligation(
                obligation,
                reason=reason,
                related_fill=related_fill,
            )
        except Exception as exc:
            log_event(
                self.logger,
                "shadow_obligation_write_failed",
                level="warning",
                **correlation_fields(
                    decision_id=obligation.decision_id,
                    intent_id=obligation.intent_id,
                    order_id=obligation.client_order_id,
                    reason=reason,
                    error=str(exc),
                ),
            )
