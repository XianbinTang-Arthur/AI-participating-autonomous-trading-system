from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from aats.schemas.audit import DecisionAuditRecord
from aats.schemas.common import EventEnvelope
from aats.schemas.execution import FillEvent, OrderObligation, OrderState
from aats.schemas.exit_execution import ChildExitOrderRef, ExitExecutionIntent
from aats.schemas.operator import OperatorUserRecord
from aats.schemas.portfolio import FillOutcomeRecord, FundingFeeRecord, PortfolioSnapshot, SleevePnLRecord
from aats.schemas.reconciliation import (
    BaselineGenerationRecord,
    ExchangeAckWatermark,
    ReconciliationFinding,
    ReconciliationReport,
    ReconciliationStateSnapshot,
    ReplayProjectionOffset,
)
from aats.schemas.strategy_profiles import (
    StrategyProfileActivationRecord,
    StrategyProfileActivationState,
    StrategyProfileEvaluationRecord,
    StrategyProfileRecommendation,
    StrategyProfileRejectionRecord,
    StrategyProfileRevision,
)
from aats.schemas.strategy_runtime import (
    AllocatorBudgetSnapshot,
    AllocatorConflictResolution,
    AllocatorNettingDecision,
    PortfolioAllocationDecision,
    SleeveBudgetAssignment,
    SleeveBudgetProfile,
    StrategyExecutionBundle,
    StrategySleeveIntent,
    StrategySleeveRecord,
)
from aats.services.runtime_scope import RuntimeStateScope


# ─────────────────────────────────────────────────────────────────────
# Stage 5: 乐观并发控制（Optimistic Concurrency Control）
# ─────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class StrategyExecutionBundleSaveResult:
    """save_execution_bundle 的返回结果。

    bundle: 写入后的 bundle（content 与传入相同）。
    row_version: 该 bundle 持久化后的最新版本号。下一次基于此 bundle 做
        增量修改的写入应当传入这个 row_version 作为 expected_row_version。
    created: True 表示这是一次插入，False 表示更新。
    """

    bundle: "StrategyExecutionBundle"
    row_version: int
    created: bool


class OptimisticLockError(Exception):
    """save_execution_bundle 检测到 row_version 不匹配时抛出。

    多进程下两个写者同时尝试更新同一 bundle，先到的成功并把 row_version
    从 N 升到 N+1，后到的写者带的 expected_row_version 仍是 N，CAS 失败。
    捕获方应当：
        1) 重新 get_execution_bundle(bundle_id) 拿到最新 bundle 与 row_version
        2) 把自己的修改 merge 到最新 bundle
        3) 用最新的 row_version 重试 save_execution_bundle

    expected_row_version 语义：
        - int N (≥ 1)：caller 期望库内已经存在 row_version=N 的 bundle，
          CAS UPDATE 应当成功
        - None：caller 期望库内**不**存在该 bundle（首次插入路径），
          但实际上已经存在
    """

    def __init__(
        self,
        bundle_id: str,
        *,
        expected: int | None,
        actual: int | None,
    ) -> None:
        self.bundle_id = bundle_id
        self.expected_row_version = expected
        self.actual_row_version = actual
        super().__init__(
            f"optimistic_lock_conflict bundle_id={bundle_id} "
            f"expected_row_version={expected} actual_row_version={actual}"
        )

class EventStore(Protocol):
    def append(self, envelope: EventEnvelope) -> None:
        ...

    def all(self) -> list[EventEnvelope]:
        ...

    def count(self, *, topic: str | None = None, decision_id: str | None = None) -> int:
        ...

    def get(self, event_id: str) -> EventEnvelope | None:
        ...

    def latest(self, topic: str, key: str | None = None) -> EventEnvelope | None:
        ...

    def by_topic(self, topic: str) -> list[EventEnvelope]:
        ...

    def recent_by_topic(self, topic: str, *, limit: int) -> list[EventEnvelope]:
        ...

    def recent_by_topic_and_key(self, topic: str, *, key: str, limit: int) -> list[EventEnvelope]:
        ...

    def by_topic_scoped(
        self,
        topic: str,
        *,
        scope: RuntimeStateScope,
        limit: int | None = None,
    ) -> list[EventEnvelope]:
        ...

    def latest_by_topic_scoped(
        self,
        topic: str,
        *,
        scope: RuntimeStateScope,
        key: str | None = None,
    ) -> EventEnvelope | None:
        ...

    def count_by_topic_scoped(
        self,
        topic: str,
        *,
        scope: RuntimeStateScope,
    ) -> int:
        """返回指定 topic + scope 的事件数。

        Metrics / dashboard 聚合类查询只需要 ``len(events)``，却经由
        ``by_topic_scoped`` 拉整张结果集再 ``len()``——对 ``event_store`` 热表
        （当前 ~545K 行 / 6.2 GB）而言单次 SELECT 就能吃掉十秒级 Python
        反序列化 + jsonb 解码。本方法直接 ``SELECT count(*)``，不解码
        payload，作为 gateway_slow_query_systematic_fix_sow.md §S1 的一部分
        引入。
        """
        ...

    def by_decision(self, decision_id: str) -> list[EventEnvelope]:
        ...

    def between(
        self,
        *,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        topic: str | None = None,
        decision_id: str | None = None,
    ) -> list[EventEnvelope]:
        ...

    def archive_before(self, *, before_ts: datetime) -> dict[str, int]:
        ...

    def archive_summary(self) -> dict[str, object]:
        ...

    def save_replay_offset(self, offset: ReplayProjectionOffset) -> ReplayProjectionOffset:
        ...

    def latest_replay_offset(
        self,
        *,
        projection_key: str,
        scope: RuntimeStateScope,
    ) -> ReplayProjectionOffset | None:
        ...


class ExecutionRepository(Protocol):
    def save_order_state(self, state: OrderState) -> OrderState:
        ...

    def has_intent(self, intent_id: str) -> bool:
        ...

    def save_fill(self, fill: FillEvent) -> bool:
        ...

    def order_states(self) -> list[OrderState]:
        ...

    def get_order_state(self, client_order_id: str) -> OrderState | None:
        ...

    def recent_order_states(
        self,
        *,
        limit: int = 20,
        statuses: tuple[str, ...] | None = None,
    ) -> list[OrderState]:
        ...

    def order_states_for_decision(self, decision_id: str) -> list[OrderState]:
        """Decision-side filter for order truth-chain lookups."""
        ...

    def open_order_states(self) -> list[OrderState]:
        ...

    def fills(self) -> list[FillEvent]:
        ...

    def fills_since(
        self,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[FillEvent]:
        ...

    def fills_for_order(self, client_order_id: str) -> list[FillEvent]:
        ...

    def fills_for_decisions(self, decision_ids: list[str]) -> list[FillEvent]:
        """SQL-side 过滤：根据 decision_id 批量查 fills。

        2026-04-21 新增：替代 `_decision_fills()` 的 ``[f for f in repo.fills()
        if f.decision_id in allowed]`` 载入全表 + Python 过滤反模式。
        ``FillEventModel.decision_id`` 有 index，SQL 侧 IN/ANY 极快。
        fills 表是无界增长，这条路径在 AI shadow evaluation 里会走，早修更稳。
        """
        ...

    def order_states_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
        statuses: tuple[str, ...] | None = None,
        limit: int | None = None,
        open_only: bool = False,
    ) -> list[OrderState]:
        ...

    def fills_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[FillEvent]:
        ...


class ExecutionObligationRepository(Protocol):
    def save_obligation(self, obligation: OrderObligation) -> OrderObligation:
        ...

    def get_obligation(self, client_order_id: str) -> OrderObligation | None:
        ...

    def active_obligations(self) -> list[OrderObligation]:
        ...

    def all_obligations(self) -> list[OrderObligation]:
        ...

    def reserve_obligation_transactional(
        self,
        obligation: OrderObligation,
        snapshot_available_balance: Decimal,
        epsilon: Decimal,
    ) -> OrderObligation:
        ...


class PortfolioRepository(Protocol):
    def save_snapshot(self, snapshot: PortfolioSnapshot) -> None:
        ...

    def latest(self) -> PortfolioSnapshot | None:
        ...

    def history(self) -> list[PortfolioSnapshot]:
        ...

    def recent_history(self, *, limit: int) -> list[PortfolioSnapshot]:
        ...

    def history_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
        limit: int | None = None,
    ) -> list[PortfolioSnapshot]:
        ...

    def latest_for_scope(self, *, scope: RuntimeStateScope) -> PortfolioSnapshot | None:
        ...

    def latest_baseline_for_scope(self, *, scope: RuntimeStateScope) -> PortfolioSnapshot | None:
        ...


class FillOutcomeRepository(Protocol):
    def save_outcome(self, outcome: FillOutcomeRecord) -> FillOutcomeRecord:
        ...

    def get_outcome(self, fill_id: str) -> FillOutcomeRecord | None:
        ...

    def outcomes(self) -> list[FillOutcomeRecord]:
        ...

    def outcomes_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[FillOutcomeRecord]:
        ...


class FundingFeeRepository(Protocol):
    def save_record(self, record: FundingFeeRecord) -> FundingFeeRecord:
        ...

    def get_record(self, bill_id: str) -> FundingFeeRecord | None:
        ...

    def records(self) -> list[FundingFeeRecord]:
        ...

    def records_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[FundingFeeRecord]:
        ...


class SleevePnLRepository(Protocol):
    def save_record(self, record: SleevePnLRecord) -> SleevePnLRecord:
        ...

    def get_record(self, record_id: str) -> SleevePnLRecord | None:
        ...

    def records(self) -> list[SleevePnLRecord]:
        ...

    def records_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[SleevePnLRecord]:
        ...

    def replace_scope(
        self,
        *,
        scope: RuntimeStateScope,
        records: list[SleevePnLRecord],
    ) -> None:
        ...


class ReconciliationRepository(Protocol):
    def save_report(self, report: ReconciliationReport) -> None:
        ...

    def save_findings(self, findings: list[ReconciliationFinding]) -> None:
        ...

    def findings_for_reconciliation(self, *, reconciliation_id: str) -> list[ReconciliationFinding]:
        ...

    def save_state_snapshot(self, snapshot: ReconciliationStateSnapshot) -> None:
        ...

    def latest_state_snapshot_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
    ) -> ReconciliationStateSnapshot | None:
        ...

    def save_baseline_generation(self, generation: BaselineGenerationRecord) -> None:
        ...

    def latest_baseline_generation_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
    ) -> BaselineGenerationRecord | None:
        ...

    def save_exchange_ack_watermark(self, watermark: ExchangeAckWatermark) -> None:
        ...

    def latest_exchange_ack_watermark_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
    ) -> ExchangeAckWatermark | None:
        ...

    def latest(self) -> ReconciliationReport | None:
        ...

    def history(self) -> list[ReconciliationReport]:
        ...

    def recent_history(self, *, limit: int) -> list[ReconciliationReport]:
        ...

    def history_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
        limit: int | None = None,
    ) -> list[ReconciliationReport]:
        ...

    def latest_for_scope(self, *, scope: RuntimeStateScope) -> ReconciliationReport | None:
        ...

    def portfolio_snapshot_refs_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
    ) -> set[str]:
        """返回指定 scope 下所有对账报告引用的 portfolio_snapshot_ref 去重集合。

        dashboard metrics 只需要 ``{r.portfolio_snapshot_ref for r in history}``
        这种 set，但 ``history_for_scope(limit=None)`` 会把完整 report 行
        （含 payload JSON）全量拉到 Python 再做 set-comp。本方法改为
        ``SELECT DISTINCT portfolio_snapshot_ref``，只返回 str 集合，
        略过 payload 反序列化。
        """
        ...


class ExitExecutionRepository(Protocol):
    def save_exit_execution_intent(self, intent: ExitExecutionIntent) -> ExitExecutionIntent:
        ...

    def get_exit_execution_intent(self, parent_intent_id: str) -> ExitExecutionIntent | None:
        ...

    def get_exit_execution_intent_by_execution_chain(
        self,
        execution_chain_id: str,
    ) -> ExitExecutionIntent | None:
        ...

    def list_exit_execution_intents(self) -> list[ExitExecutionIntent]:
        ...

    def save_child_exit_order_ref(self, child_ref: ChildExitOrderRef) -> ChildExitOrderRef:
        ...

    def child_refs_for_parent(self, *, parent_intent_id: str) -> list[ChildExitOrderRef]:
        ...

    def parent_intent_id_for_child(self, *, client_order_id: str) -> str | None:
        ...


class AuditRepository(Protocol):
    def upsert(self, record: DecisionAuditRecord) -> None:
        ...

    def get(self, decision_id: str) -> DecisionAuditRecord | None:
        ...

    def latest(self) -> DecisionAuditRecord | None:
        ...

    def recent(self, *, limit: int) -> list[DecisionAuditRecord]:
        ...

    def all(self) -> list[DecisionAuditRecord]:
        ...

    def history(self, decision_id: str) -> list[DecisionAuditRecord]:
        ...

    def count(self) -> int:
        ...


class OperatorUserRepository(Protocol):
    def save_user(self, user: OperatorUserRecord) -> OperatorUserRecord:
        ...

    def get_by_username(self, username: str) -> OperatorUserRecord | None:
        ...

    def all_users(self) -> list[OperatorUserRecord]:
        ...

    def count(self, *, enabled_only: bool = False) -> int:
        ...

    def record_login(self, username: str, logged_in_at: datetime) -> None:
        ...

    def delete_user(self, username: str) -> bool:
        ...


class StrategyProfileRepository(Protocol):
    def save_revision(self, revision: StrategyProfileRevision) -> StrategyProfileRevision:
        ...

    def get_revision(self, revision_id: str) -> StrategyProfileRevision | None:
        ...

    def list_revisions(
        self,
        *,
        product_type: str | None = None,
        margin_mode: str | None = None,
        profile_id: str | None = None,
        status: str | None = None,
    ) -> list[StrategyProfileRevision]:
        ...

    def activation_state(
        self,
        *,
        product_type: str,
        margin_mode: str,
        allowed_symbols: tuple[str, ...],
    ) -> StrategyProfileActivationState:
        ...

    def save_activation_state(self, state: StrategyProfileActivationState) -> StrategyProfileActivationState:
        ...


class StrategySleeveRepository(Protocol):
    def save_sleeve(self, sleeve: StrategySleeveRecord) -> StrategySleeveRecord:
        ...

    def get_sleeve(self, sleeve_id: str) -> StrategySleeveRecord | None:
        ...

    def list_sleeves(self) -> list[StrategySleeveRecord]:
        ...

    def save_recommendation(self, recommendation: StrategyProfileRecommendation) -> StrategyProfileRecommendation:
        ...

    def get_recommendation(self, recommendation_id: str) -> StrategyProfileRecommendation | None:
        ...

    def latest_recommendation(
        self,
        *,
        product_type: str,
        margin_mode: str,
        allowed_symbols: tuple[str, ...],
    ) -> StrategyProfileRecommendation | None:
        ...

    def list_recommendations(
        self,
        *,
        product_type: str | None = None,
        margin_mode: str | None = None,
        decision_status: str | None = None,
    ) -> list[StrategyProfileRecommendation]:
        ...

    def save_activation_record(self, record: StrategyProfileActivationRecord) -> StrategyProfileActivationRecord:
        ...

    def list_activation_history(
        self,
        *,
        product_type: str | None = None,
        margin_mode: str | None = None,
    ) -> list[StrategyProfileActivationRecord]:
        ...

    def save_rejection(self, record: StrategyProfileRejectionRecord) -> StrategyProfileRejectionRecord:
        ...

    def list_rejections(
        self,
        *,
        product_type: str | None = None,
        margin_mode: str | None = None,
    ) -> list[StrategyProfileRejectionRecord]:
        ...

    def save_evaluation(self, record: StrategyProfileEvaluationRecord) -> StrategyProfileEvaluationRecord:
        ...

    def list_evaluations(
        self,
        *,
        product_type: str | None = None,
        margin_mode: str | None = None,
    ) -> list[StrategyProfileEvaluationRecord]:
        ...


class StrategyRuntimeRepository(Protocol):
    def save_budget_profile(self, profile: SleeveBudgetProfile) -> SleeveBudgetProfile:
        ...

    def list_budget_profiles(
        self,
        *,
        product_type: str | None = None,
        margin_mode: str | None = None,
        family: str | None = None,
        limit: int | None = None,
    ) -> list[SleeveBudgetProfile]:
        ...

    def save_budget_assignment(self, assignment: SleeveBudgetAssignment) -> SleeveBudgetAssignment:
        ...

    def list_budget_assignments(
        self,
        *,
        product_type: str | None = None,
        margin_mode: str | None = None,
        symbol: str | None = None,
        strategy_sleeve_id: str | None = None,
        limit: int | None = None,
    ) -> list[SleeveBudgetAssignment]:
        ...

    def save_sleeve_intent(self, intent: StrategySleeveIntent) -> StrategySleeveIntent:
        ...

    def list_sleeve_intents(
        self,
        *,
        product_type: str | None = None,
        margin_mode: str | None = None,
        symbol: str | None = None,
        limit: int | None = None,
    ) -> list[StrategySleeveIntent]:
        ...

    def save_allocation_decision(self, decision: PortfolioAllocationDecision) -> PortfolioAllocationDecision:
        ...

    def latest_allocation_decision(
        self,
        *,
        product_type: str | None = None,
        margin_mode: str | None = None,
        symbol: str | None = None,
    ) -> PortfolioAllocationDecision | None:
        ...

    def get_allocation_decision(self, allocation_id: str) -> PortfolioAllocationDecision | None:
        ...

    def save_execution_bundle(self, bundle: StrategyExecutionBundle) -> StrategyExecutionBundle:
        """无版本检查的写入。

        ⚠️ 历史接口；多进程下写竞争会导致丢失更新。新代码应优先调用
        save_execution_bundle_versioned。本方法在底层会做"读 → 写"且不做 CAS，
        所以如果两个进程并发调它，最后写的会覆盖前一个。
        """
        ...

    def save_execution_bundle_versioned(
        self,
        bundle: StrategyExecutionBundle,
        *,
        expected_row_version: int | None,
    ) -> "StrategyExecutionBundleSaveResult":
        """带乐观并发控制的写入（Stage 5）。

        - expected_row_version=None：表示这是该 bundle 的首次插入。如果库内
          已经存在同 ID 的 bundle，会抛 OptimisticLockError。
        - expected_row_version=N：UPDATE 时附加 WHERE row_version=N，rowcount
          必须为 1，否则抛 OptimisticLockError。
        - 成功后返回的 row_version 是写入完成后的新值（旧值+1，或首次插入的 1）。
        """
        ...

    def get_execution_bundle_with_version(
        self,
        bundle_id: str,
    ) -> tuple["StrategyExecutionBundle", int] | None:
        """读取 bundle 同时返回当前 row_version。

        多进程下做"读 → 修改 → 写"循环时，必须用这个方法拿到 row_version，
        否则就退化成 last-writer-wins。
        """
        ...

    def get_execution_bundle(self, bundle_id: str) -> StrategyExecutionBundle | None:
        ...

    def recent_execution_bundles(
        self,
        *,
        product_type: str | None = None,
        margin_mode: str | None = None,
        symbol: str | None = None,
        limit: int | None = None,
    ) -> list[StrategyExecutionBundle]:
        ...

    def list_budget_snapshots(
        self,
        *,
        allocation_id: str | None = None,
        strategy_sleeve_id: str | None = None,
        limit: int | None = None,
    ) -> list[AllocatorBudgetSnapshot]:
        ...

    def list_conflict_resolutions(
        self,
        *,
        allocation_id: str | None = None,
        symbol: str | None = None,
        limit: int | None = None,
    ) -> list[AllocatorConflictResolution]:
        ...

    def list_netting_decisions(
        self,
        *,
        allocation_id: str | None = None,
        symbol: str | None = None,
        limit: int | None = None,
    ) -> list[AllocatorNettingDecision]:
        ...
