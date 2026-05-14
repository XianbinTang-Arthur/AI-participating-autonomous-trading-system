from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from aats.bootstrap.logging import correlation_fields, get_logger, log_event
from aats.bootstrap.metrics import MetricsRegistry
from aats.bootstrap.telemetry import start_span
from aats.bus.base import EventBus
from aats.events import topics
from aats.events.envelopes import publish_model
from aats.schemas.common import EventEnvelope, new_id
from aats.schemas.decision import PositionTarget
from aats.schemas.market import MarketSnapshot
from aats.schemas.operator import ProcessingFailureRecord
from aats.services.ai_service.inference import AIInferenceService
from aats.services.decision_engine.baseline import BaselineStrategy
from aats.services.decision_engine.context_builder import DecisionContextBuilder
from aats.services.decision_engine.target_position import TargetPositionEngine
from aats.services.strategy_engines.overlay_parent_exposure import overlay_parent_exposure_record

if TYPE_CHECKING:
    from aats.services.operator.strategy_profiles import StrategyProfileControlService
    from aats.services.strategy_engines.coordinator import StrategyCoordinatorService
    from aats.services.strategy_engines.paper_trading_shadow import (
        PaperTradingShadowService,
    )


class DecisionOrchestrator:
    def __init__(
        self,
        *,
        bus: EventBus,
        context_builder: DecisionContextBuilder,
        baseline_strategy: BaselineStrategy,
        ai_service: AIInferenceService,
        target_engine: TargetPositionEngine,
        strategy_profile_service: StrategyProfileControlService | None = None,
        strategy_coordinator: StrategyCoordinatorService | None = None,
        paper_trading_shadow_service: PaperTradingShadowService | None = None,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        self.bus = bus
        self.context_builder = context_builder
        self.baseline_strategy = baseline_strategy
        self.ai_service = ai_service
        self.target_engine = target_engine
        self.strategy_profile_service = strategy_profile_service
        self.strategy_coordinator = strategy_coordinator
        # Round 3 · 2026-04-22 · Non-AI paper trading shadow (独立于 AI shadow).
        # 默认 None → 不跑 shadow code path，零开销。
        # build_runtime() 在 settings.paper_trading_shadow_enabled=True 时才实例化。
        self.paper_trading_shadow_service = paper_trading_shadow_service
        self.metrics = metrics
        self.logger = get_logger("aats.decision_engine")

    # LF-20260421-003 fix · 2026-04-22
    # run_cycle 全局 timeout（默认 30s）。
    #
    # 背景：run_cycle 里有多个 `publish_model` 和 `asyncio.to_thread` 调用，
    # NATS 背压、JetStream 同步写慢、PG 锁等待都可能让某次 cycle 挂几十秒。
    # trigger.py 的 `_timeframe_locks` 把同 (symbol, timeframe) 串行化，一个
    # 周期挂住 = 该 symbol+timeframe 后续决策全部卡队。
    #
    # 30s 的选择：
    # - 远高于正常 cycle 时间（~100ms）
    # - 略低于前端 DEFAULT_TIMEOUT_MS（30s）+ 1 次 retry buffer
    # - 触发超时时外层 try/except 走 `_publish_failure_best_effort` 把孤儿
    #   事件标记成 processing_failure，trigger backoff 接管重试
    _RUN_CYCLE_TIMEOUT_SECONDS = 30.0

    async def run_cycle(
        self,
        symbol: str,
        timeframe: str,
        *,
        feature_snapshot_hint: EventEnvelope | None = None,
        market_snapshot_hint: MarketSnapshot | None = None,
    ) -> PositionTarget:
        # 关键背景：本函数原本几乎所有的工作都跑在 event loop 主线程上 ——
        # context_builder.build / baseline_strategy.evaluate /
        # target_engine.build* / strategy_coordinator.evaluate 都是纯 sync
        # 的 CPU + 同步 DB 读，把它们直接 await 进来（其实只是同步执行）会
        # 让 event loop 在每个 decision 周期里阻塞 15-30s，直接结果是同进程
        # 的 FastAPI handler（dashboard bundle、UI 静态资源、favicon）全部
        # 拿不到调度，前端骨架卡死、人工排障时观察到 favicon 都要等 8 秒以上。
        #
        # 这里把每个明显是"纯输入→纯输出 + 没有 async 依赖"的 sync 调用都
        # 丢到 asyncio.to_thread。这些方法本来就是同步 Python，原地放进
        # thread pool 不改变算法行为；同时由于 trigger.py 的
        # `_timeframe_locks` 已经把同 (symbol, timeframe) 串行化，单个周期内
        # 只会有一个线程在跑这些方法，避免了潜在的多线程竞态。
        # 收益：每次 to_thread 都是一次 yield 点，event loop 可以在线程跑
        # 计算的同时调度 HTTP handler，dashboard 不再被决策周期卡死。
        decision_id = new_id("decision")
        # Stage 8：整个 decision cycle 作为一个顶层 span，内部所有 publish_model
        # → NatsEventBus.publish_envelope 发出的 NATS 事件会自动作为子 span
        # 挂在它下面，形成 Jaeger 里的 "decision_engine.run_cycle →
        # nats.publish.decision_contexts / baseline_assessments / ..." 链路。
        # 设计文档：docs/task/stage_8_otel_integration_design.md §D5
        with start_span(
            "decision_engine.run_cycle",
            attributes={
                "aats.decision_id": decision_id,
                "aats.symbol": symbol,
                "aats.timeframe": timeframe,
            },
        ):
            try:
                # LF-003: 全局 timeout 防止 NATS 背压 / PG 锁等待让 cycle 挂死
                return await asyncio.wait_for(
                    self._run_cycle_body(
                        symbol=symbol,
                        timeframe=timeframe,
                        decision_id=decision_id,
                        feature_snapshot_hint=feature_snapshot_hint,
                        market_snapshot_hint=market_snapshot_hint,
                    ),
                    timeout=self._RUN_CYCLE_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError as exc:
                # 超时 → 记 PROCESSING_FAILURES 标记孤儿 → 上抛让 trigger backoff 处理
                log_event(
                    self.logger,
                    "decision_cycle_timeout",
                    level="error",
                    **correlation_fields(
                        decision_id=decision_id,
                        symbol=symbol,
                        timeframe=timeframe,
                        timeout_seconds=self._RUN_CYCLE_TIMEOUT_SECONDS,
                    ),
                )
                await self._publish_failure_best_effort(
                    decision_id=decision_id,
                    symbol=symbol,
                    timeframe=timeframe,
                    exc=exc,
                )
                raise
            except Exception as exc:
                # P1-11：若 run_cycle 在 position_target 发出之前崩溃，之前已经发过的
                # decision_context / baseline / ai_assessment / shadow 等事件会在
                # event stream 上成为"孤儿"（不会有对应的 target / outcome）。下游
                # 的 reconciliation_service.handle_processing_failure 订阅
                # PROCESSING_FAILURES 并按 decision_id 关联，能够把这些孤儿标记为
                # 未完成，避免重放 / 审计时把它们当作已决策结果。
                await self._publish_failure_best_effort(
                    decision_id=decision_id,
                    symbol=symbol,
                    timeframe=timeframe,
                    exc=exc,
                )
                raise

    async def _run_cycle_body(
        self,
        *,
        symbol: str,
        timeframe: str,
        decision_id: str,
        feature_snapshot_hint: EventEnvelope | None = None,
        market_snapshot_hint: MarketSnapshot | None = None,
    ) -> PositionTarget:
        await self._refresh_account_state_for_decision()
        health_snapshot = await asyncio.to_thread(
            self.context_builder.build_health_snapshot,
            decision_id=decision_id,
        )
        health_envelope = await publish_model(
            bus=self.bus,
            topic=topics.HEALTH_SNAPSHOTS,
            key=symbol,
            payload_model=health_snapshot,
            source_component="governance_engine",
        )
        context = await asyncio.to_thread(
            self.context_builder.build,
            symbol=symbol,
            timeframe=timeframe,
            decision_id=decision_id,
            health_snapshot_ref=health_envelope.event_id,
            feature_snapshot_hint=feature_snapshot_hint,
            market_snapshot_hint=market_snapshot_hint,
        )
        log_event(
            self.logger,
            "decision_cycle_started",
            **correlation_fields(
                decision_id=context.decision_id,
                symbol=symbol,
                timeframe=timeframe,
            ),
        )
        baseline = await asyncio.to_thread(self.baseline_strategy.evaluate, context)
        await publish_model(
            bus=self.bus,
            topic=topics.DECISION_CONTEXTS,
            key=symbol,
            payload_model=context,
            source_component="decision_engine",
        )
        await publish_model(
            bus=self.bus,
            topic=topics.BASELINE_ASSESSMENTS,
            key=symbol,
            payload_model=baseline,
            source_component="decision_engine",
        )
        operating_mode = self.ai_service.effective_operating_mode()
        profile_control_decision = None
        ai_assessment = None
        if self.ai_service.should_attempt_assessment():
            ai_assessment = await self.ai_service.assess(context=context, baseline=baseline)
            operating_mode = self.ai_service.effective_operating_mode()
        # Task P3-1：删除 `canonical_mode = self.ai_service.canonical_effective_operating_mode()`
        # —— 赋值但未使用（F841），下游不消费。
        # strategy_profile_auto_control_enabled 与 ai_operating_mode 完全正交:
        # False → 连 evaluate_mainline_profile_control 都不调,杜绝 OpenAI 账单泄漏。
        # 手动激活走独立 admin API(profiles/{id}/activate 等),不经此路径,不受影响。
        # UI "当前档位"展示读 runtime_profile_snapshot,不依赖此处的 decision。
        if (
            self.strategy_profile_service is not None
            and self.strategy_profile_service.settings.strategy_profile_auto_control_enabled
        ):
            profile_control_decision = await self.strategy_profile_service.evaluate_mainline_profile_control(
                decision_id=context.decision_id,
            )
        ai_decision_intent = await asyncio.to_thread(
            self.target_engine.build_ai_decision_intent,
            context=context,
            baseline=baseline,
            ai_assessment=ai_assessment,
            operating_mode=operating_mode,
        )
        if ai_decision_intent is not None and profile_control_decision is not None:
            ai_decision_intent = ai_decision_intent.model_copy(
                update={
                    "requested_profile_id": profile_control_decision.requested_profile_id,
                    "requested_profile_reason_codes": list(profile_control_decision.decision_reason_codes),
                }
            )
        target = await asyncio.to_thread(
            self.target_engine.build,
            context,
            baseline,
            ai_assessment,
            ai_decision_intent,
            profile_control_decision=profile_control_decision,
            operating_mode=operating_mode,
        )
        if self.strategy_coordinator is not None:
            strategy_snapshot = await asyncio.to_thread(
                self.strategy_coordinator.evaluate,
                context=context,
                baseline=baseline,
                directional_target=target,
                ai_assessment=ai_assessment,
            )
            strategy_envelope = await publish_model(
                bus=self.bus,
                topic=topics.STRATEGY_COORDINATOR_SNAPSHOTS,
                key=symbol,
                payload_model=strategy_snapshot,
                source_component="decision_engine",
            )
            for sleeve_intent in strategy_snapshot.sleeve_intents:
                await publish_model(
                    bus=self.bus,
                    topic=topics.STRATEGY_SLEEVE_INTENTS,
                    key=symbol,
                    payload_model=sleeve_intent,
                    source_component="decision_engine",
                )
            if strategy_snapshot.allocation_decision is not None:
                await publish_model(
                    bus=self.bus,
                    topic=topics.PORTFOLIO_ALLOCATION_DECISIONS,
                    key=symbol,
                    payload_model=strategy_snapshot.allocation_decision,
                    source_component="decision_engine",
                )
            target = self.strategy_coordinator.apply_selected_target(
                base_target=target,
                snapshot=strategy_snapshot,
                snapshot_ref=strategy_envelope.event_id,
            )
        # ── LF-Round3-P1.3 · 2026-04-22 · Paper trading shadow hook ──
        # 在 live target 最终版本生成后（含 strategy_coordinator.apply_selected_target
        # 之后）、AI shadow 之前评估候选 strategy 参数。
        #
        # 安全不变量（见 docs/task/round3_paper_trading_design.md §7）：
        # - 整段 try/except 包，任何异常绝不 propagate 进 live run_cycle
        # - service 默认 None（未注入）→ skip
        # - service enabled=False 或 candidates 空 → skip
        await self._maybe_record_paper_trading_shadows(
            context=context,
            baseline=baseline,
            live_target=target,
            symbol=symbol,
        )
        brief = None if ai_assessment is None else self.ai_service.latest_brief(context.decision_id)
        shadow_assessment = None if ai_assessment is None else self.ai_service.latest_shadow_assessment(context.decision_id)
        if brief is not None:
            await publish_model(
                bus=self.bus,
                topic=topics.AI_DECISION_BRIEFS,
                key=symbol,
                payload_model=brief,
                source_component="ai_service",
            )
        if ai_assessment is not None:
            await publish_model(
                bus=self.bus,
                topic=topics.AI_ASSESSMENTS,
                key=symbol,
                payload_model=ai_assessment,
                source_component="ai_service",
            )
        if shadow_assessment is not None:
            # LF-Round3-backport · 2026-04-22
            # AI shadow 也要加异常保护（之前没有）。shadow 路径是辅助数据，
            # 任何失败绝不应该 kill live decision cycle。同 paper trading
            # shadow 的 pattern。
            try:
                shadow_decision = await asyncio.to_thread(
                    self.target_engine.build_shadow,
                    context=context,
                    baseline=baseline,
                    ai_assessment=shadow_assessment,
                    actual_target=target,
                    operating_mode=operating_mode,
                )
                self.ai_service.record_shadow_decision(shadow_decision)
                await publish_model(
                    bus=self.bus,
                    topic=topics.AI_SHADOW_DECISIONS,
                    key=symbol,
                    payload_model=shadow_decision,
                    source_component="decision_engine",
                )
            except Exception as exc:
                log_event(
                    self.logger,
                    "ai_shadow_decision_build_or_publish_failed",
                    level="warning",
                    decision_id=context.decision_id,
                    symbol=symbol,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
        position_target_envelope = await publish_model(
            bus=self.bus,
            topic=topics.POSITION_TARGETS,
            key=symbol,
            payload_model=target,
            source_component="decision_engine",
        )
        overlay_parent_record = overlay_parent_exposure_record(
            decision_id=target.decision_id,
            product_type=target.product_type,
            strategy_family=target.strategy_family,
            strategy_sleeve_id=target.strategy_sleeve_id,
            allocation_id=target.allocation_id,
            source_stage="position_target",
            source_ref=position_target_envelope.event_id,
            parent_exposure=target.overlay_parent_exposure,
        )
        if overlay_parent_record is not None:
            await publish_model(
                bus=self.bus,
                topic=topics.OVERLAY_PARENT_EXPOSURES,
                key=symbol,
                payload_model=overlay_parent_record,
                source_component="decision_engine",
            )
        if shadow_assessment is not None:
            await self._publish_shadow_evaluation_best_effort(
                decision_id=context.decision_id,
                symbol=symbol,
            )
        if self.metrics is not None:
            self.metrics.increment("decision_cycles")
        log_event(
            self.logger,
            "decision_cycle_completed",
            **correlation_fields(
                decision_id=context.decision_id,
                symbol=symbol,
                target_position_qty=target.target_position_qty,
                delta_position_qty=target.delta_position_qty,
            ),
        )
        return target

    async def _refresh_account_state_for_decision(self) -> None:
        settings = self.context_builder.settings
        capabilities = getattr(self.context_builder.mode_controller, "environment_capabilities", None)
        if not bool(getattr(capabilities, "exchange_coupled", False)):
            return
        if settings.account_backend != "okx" or not settings.account_read_enabled:
            return
        refresh = getattr(self.context_builder.account_service, "refresh", None)
        if not callable(refresh):
            return
        try:
            await refresh(force_account_state=True)
        except TypeError as exc:
            if "force_account_state" not in str(exc):
                raise
            await refresh()

    # R2-P0-D1：publish 硬超时。若 NATS 背压或 bus 内部死锁，publish_model 可能
    # 永不返回。_publish_failure_best_effort 在 raise 原始业务异常之前 await 这个
    # publish——没有超时等于让整个 run_cycle 永久挂起，trigger.py backoff/重试都
    # 永远等不到协程 return。用 wait_for 明确上限，超时走 warning 分支，业务异常
    # 仍按原样 raise 出去。
    _FAILURE_PUBLISH_TIMEOUT_SECONDS: float = 5.0

    async def _publish_failure_best_effort(
        self,
        *,
        decision_id: str,
        symbol: str,
        timeframe: str,
        exc: BaseException,
    ) -> None:
        """Best-effort publish a ProcessingFailureRecord when run_cycle raises.

        任何在 publish_model 自身的异常只做 warning log，不再 raise，否则会
        覆盖原始业务异常让 trigger.py 的 backoff 逻辑读到错误的 error_type。
        """
        try:
            await asyncio.wait_for(
                publish_model(
                    bus=self.bus,
                    topic=topics.PROCESSING_FAILURES,
                    key="decision_engine",
                    payload_model=ProcessingFailureRecord(
                        subsystem="decision_engine",
                        stage="run_cycle",
                        severity="error",
                        message=f"decision_cycle_failed: {type(exc).__name__}: {exc}",
                        decision_id=decision_id,
                        symbol=symbol,
                        retriable=True,
                        observed_at=datetime.now(timezone.utc),
                        details={
                            "timeframe": timeframe,
                            "error_type": type(exc).__name__,
                        },
                    ),
                    source_component="decision_engine",
                ),
                timeout=self._FAILURE_PUBLISH_TIMEOUT_SECONDS,
            )
        except Exception as publish_exc:
            is_timeout = isinstance(publish_exc, asyncio.TimeoutError)
            log_event(
                self.logger,
                "decision_cycle_failure_publish_failed",
                level="warning",
                **correlation_fields(
                    decision_id=decision_id,
                    symbol=symbol,
                    timeframe=timeframe,
                    error=(
                        f"publish_timeout_seconds={self._FAILURE_PUBLISH_TIMEOUT_SECONDS}"
                        if is_timeout
                        else str(publish_exc)
                    ),
                    error_type=type(publish_exc).__name__,
                    is_timeout=is_timeout,
                ),
            )

    async def _publish_shadow_evaluation_best_effort(
        self,
        *,
        decision_id: str,
        symbol: str,
    ) -> None:
        try:
            shadow_evaluation, created = self.ai_service.evaluate_shadow_window(
                limit=self.ai_service.settings.ai_shadow_evaluation_window
            )
            if not created or shadow_evaluation is None:
                return
            envelope = await publish_model(
                bus=self.bus,
                topic=topics.AI_SHADOW_EVALUATIONS,
                key=symbol,
                payload_model=shadow_evaluation,
                source_component="decision_engine",
            )
            self.ai_service.publish_shadow_performance_report(
                evaluation=shadow_evaluation,
                latest_evaluation_ref=envelope.event_id,
            )
        except Exception as exc:
            log_event(
                self.logger,
                "shadow_evaluation_failed",
                level="warning",
                **correlation_fields(
                    decision_id=decision_id,
                    symbol=symbol,
                    error=str(exc),
                ),
            )

    async def _maybe_record_paper_trading_shadows(
        self,
        *,
        context,
        baseline,
        live_target,
        symbol: str,
    ) -> list[str]:
        """Round 3 · 评估 non-AI paper trading 候选并 publish。

        **安全不变量**：本方法内部对任何异常 swallow + warning log，永远不
        让 shadow 评估破坏 live run_cycle。返回被 publish 的 shadow
        decision event_id 列表（audit 层可 append）。

        服务未注入或未 enabled 时直接返回空列表，零开销。
        """
        if self.paper_trading_shadow_service is None:
            return []
        if not self.paper_trading_shadow_service.enabled():
            return []
        try:
            shadow_decisions = self.paper_trading_shadow_service.evaluate_candidates(
                context=context,
                baseline=baseline,
                live_target=live_target,
            )
        except Exception as exc:
            log_event(
                self.logger,
                "paper_trading_shadow_evaluate_failed",
                level="warning",
                decision_id=live_target.decision_id,
                symbol=symbol,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return []

        event_ids: list[str] = []
        for decision in shadow_decisions:
            try:
                envelope = await publish_model(
                    bus=self.bus,
                    topic=topics.STRATEGY_FAMILY_SHADOW_DECISIONS,
                    key=symbol,
                    payload_model=decision,
                    source_component="paper_trading_shadow",
                )
                event_ids.append(envelope.event_id)
            except Exception as exc:
                log_event(
                    self.logger,
                    "paper_trading_shadow_publish_failed",
                    level="warning",
                    decision_id=live_target.decision_id,
                    symbol=symbol,
                    candidate_id=decision.candidate_id,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )

        # Phase 2 · 窗口达到阈值时 publish evaluation
        await self._maybe_publish_paper_trading_evaluations(symbol=symbol)
        return event_ids

    async def _maybe_publish_paper_trading_evaluations(self, *, symbol: str) -> None:
        """Round 3 Phase 2 · 每次记 shadow 后检查是否有窗口满了，满了就 publish 聚合报告。

        service.evaluate_windows() 安全幂等，返回已满窗口的 evaluations（可能空）。
        任何 publish 失败 swallow，不影响 live。
        """
        if self.paper_trading_shadow_service is None:
            return
        try:
            evaluations = self.paper_trading_shadow_service.evaluate_windows()
        except Exception as exc:
            log_event(
                self.logger,
                "paper_trading_shadow_evaluation_loop_failed",
                level="warning",
                symbol=symbol,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return

        for evaluation in evaluations:
            try:
                await publish_model(
                    bus=self.bus,
                    topic=topics.STRATEGY_FAMILY_SHADOW_EVALUATIONS,
                    key=symbol,
                    payload_model=evaluation,
                    source_component="paper_trading_shadow",
                )
            except Exception as exc:
                log_event(
                    self.logger,
                    "paper_trading_shadow_evaluation_publish_failed",
                    level="warning",
                    symbol=symbol,
                    candidate_id=evaluation.candidate_id,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
