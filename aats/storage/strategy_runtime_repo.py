from __future__ import annotations

from aats.schemas.strategy_runtime import (
    AllocatorBudgetSnapshot,
    AllocatorConflictResolution,
    AllocatorNettingDecision,
    PortfolioAllocationDecision,
    SleeveBudgetAssignment,
    SleeveBudgetProfile,
    StrategyExecutionBundle,
    StrategySleeveIntent,
)
from aats.storage.base import (
    OptimisticLockError,
    StrategyExecutionBundleSaveResult,
)


class InMemoryStrategyRuntimeRepository:
    def __init__(self) -> None:
        self._budget_profiles: dict[str, SleeveBudgetProfile] = {}
        self._budget_assignments: dict[str, SleeveBudgetAssignment] = {}
        self._sleeve_intents: dict[str, StrategySleeveIntent] = {}
        self._allocations: dict[str, PortfolioAllocationDecision] = {}
        self._bundles: dict[str, StrategyExecutionBundle] = {}
        # Stage 5: 每个 bundle 的当前 row_version，与 _bundles 平行维护
        self._bundle_versions: dict[str, int] = {}
        self._budget_snapshots: dict[str, AllocatorBudgetSnapshot] = {}
        self._conflict_resolutions: dict[str, AllocatorConflictResolution] = {}
        self._netting_decisions: dict[str, AllocatorNettingDecision] = {}

    def save_budget_profile(self, profile: SleeveBudgetProfile) -> SleeveBudgetProfile:
        self._budget_profiles[profile.budget_profile_id] = profile
        return profile

    def list_budget_profiles(
        self,
        *,
        product_type: str | None = None,
        margin_mode: str | None = None,
        family: str | None = None,
        limit: int | None = None,
    ) -> list[SleeveBudgetProfile]:
        rows = sorted(self._budget_profiles.values(), key=lambda item: item.updated_at, reverse=True)
        result = [
            item
            for item in rows
            if (product_type is None or item.product_type == product_type)
            and (margin_mode is None or item.margin_mode == margin_mode)
            and (family is None or item.family == family)
        ]
        return result if limit is None else result[:limit]

    def save_budget_assignment(self, assignment: SleeveBudgetAssignment) -> SleeveBudgetAssignment:
        self._budget_assignments[assignment.assignment_id] = assignment
        return assignment

    def list_budget_assignments(
        self,
        *,
        product_type: str | None = None,
        margin_mode: str | None = None,
        symbol: str | None = None,
        strategy_sleeve_id: str | None = None,
        limit: int | None = None,
    ) -> list[SleeveBudgetAssignment]:
        rows = sorted(self._budget_assignments.values(), key=lambda item: item.updated_at, reverse=True)
        result = [
            item
            for item in rows
            if (product_type is None or item.product_type == product_type)
            and (margin_mode is None or item.margin_mode == margin_mode)
            and (symbol is None or item.symbol == symbol)
            and (strategy_sleeve_id is None or item.strategy_sleeve_id == strategy_sleeve_id)
        ]
        return result if limit is None else result[:limit]

    def save_sleeve_intent(self, intent: StrategySleeveIntent) -> StrategySleeveIntent:
        self._sleeve_intents[intent.sleeve_intent_id] = intent
        return intent

    def list_sleeve_intents(
        self,
        *,
        product_type: str | None = None,
        margin_mode: str | None = None,
        symbol: str | None = None,
        limit: int | None = None,
    ) -> list[StrategySleeveIntent]:
        rows = sorted(self._sleeve_intents.values(), key=lambda item: item.created_at, reverse=True)
        rows = [
            item
            for item in rows
            if (product_type is None or item.product_type == product_type)
            and (margin_mode is None or item.margin_mode == margin_mode)
            and (symbol is None or item.symbol == symbol)
        ]
        return rows if limit is None else rows[:limit]

    def save_allocation_decision(self, decision: PortfolioAllocationDecision) -> PortfolioAllocationDecision:
        self._allocations[decision.allocation_id] = decision
        for snapshot in decision.budget_snapshots:
            self._budget_snapshots[snapshot.budget_snapshot_id] = snapshot
        for item in decision.conflict_resolutions:
            self._conflict_resolutions[item.conflict_resolution_id] = item
        for item in decision.netting_decisions:
            self._netting_decisions[item.netting_decision_id] = item
        return decision

    def latest_allocation_decision(
        self,
        *,
        product_type: str | None = None,
        margin_mode: str | None = None,
        symbol: str | None = None,
    ) -> PortfolioAllocationDecision | None:
        rows = sorted(self._allocations.values(), key=lambda item: item.created_at, reverse=True)
        for item in rows:
            if product_type is not None and item.product_type != product_type:
                continue
            if margin_mode is not None and item.margin_mode != margin_mode:
                continue
            if symbol is not None and item.symbol != symbol:
                continue
            return item
        return None

    def get_allocation_decision(self, allocation_id: str) -> PortfolioAllocationDecision | None:
        return self._allocations.get(allocation_id)

    def save_execution_bundle(self, bundle: StrategyExecutionBundle) -> StrategyExecutionBundle:
        """无版本检查的写入。

        ⚠️ 历史接口；多进程下并发写会丢失更新。新代码应优先使用
        save_execution_bundle_versioned。这里仍然 bump row_version，
        以保证两个接口对版本号的视图一致。
        """
        self._bundles[bundle.bundle_id] = bundle
        self._bundle_versions[bundle.bundle_id] = self._bundle_versions.get(bundle.bundle_id, 0) + 1
        return bundle

    def save_execution_bundle_versioned(
        self,
        bundle: StrategyExecutionBundle,
        *,
        expected_row_version: int | None,
    ) -> StrategyExecutionBundleSaveResult:
        """带 CAS 的写入（Stage 5）。

        语义和 Postgres 实现完全一致：
        - expected_row_version=None：要求库内不存在同 ID 的 bundle，否则抛错
        - expected_row_version=N：库内必须有同 ID 的 bundle 且 row_version==N
          才允许更新；写入完成后 row_version 升级为 N+1
        """
        actual_version = self._bundle_versions.get(bundle.bundle_id)
        if expected_row_version is None:
            if actual_version is not None:
                # 首次插入路径冲突：caller 期望 row 不存在，但库内已经有了。
                # expected=None 与 base.py OptimisticLockError 协议一致，
                # 表示"期望首次插入"语义，而不是"期望版本号 0"。
                raise OptimisticLockError(
                    bundle.bundle_id,
                    expected=None,
                    actual=actual_version,
                )
            new_version = 1
            created = True
        else:
            if actual_version != expected_row_version:
                raise OptimisticLockError(
                    bundle.bundle_id,
                    expected=expected_row_version,
                    actual=actual_version,
                )
            new_version = expected_row_version + 1
            created = False

        self._bundles[bundle.bundle_id] = bundle
        self._bundle_versions[bundle.bundle_id] = new_version
        return StrategyExecutionBundleSaveResult(
            bundle=bundle,
            row_version=new_version,
            created=created,
        )

    def get_execution_bundle(self, bundle_id: str) -> StrategyExecutionBundle | None:
        return self._bundles.get(bundle_id)

    def get_execution_bundle_with_version(
        self,
        bundle_id: str,
    ) -> tuple[StrategyExecutionBundle, int] | None:
        bundle = self._bundles.get(bundle_id)
        version = self._bundle_versions.get(bundle_id)
        if bundle is None or version is None:
            return None
        return bundle, version

    def recent_execution_bundles(
        self,
        *,
        product_type: str | None = None,
        margin_mode: str | None = None,
        symbol: str | None = None,
        limit: int | None = None,
    ) -> list[StrategyExecutionBundle]:
        rows = sorted(self._bundles.values(), key=lambda item: item.created_at, reverse=True)
        rows = [
            item
            for item in rows
            if (product_type is None or item.product_type == product_type)
            and (margin_mode is None or item.margin_mode == margin_mode)
            and (symbol is None or item.selected_symbol == symbol)
        ]
        return rows if limit is None else rows[:limit]

    def list_budget_snapshots(
        self,
        *,
        allocation_id: str | None = None,
        strategy_sleeve_id: str | None = None,
        limit: int | None = None,
    ) -> list[AllocatorBudgetSnapshot]:
        rows = sorted(self._budget_snapshots.values(), key=lambda item: item.created_at, reverse=True)
        result = [
            item
            for item in rows
            if (allocation_id is None or item.allocation_id == allocation_id)
            and (strategy_sleeve_id is None or item.strategy_sleeve_id == strategy_sleeve_id)
        ]
        return result if limit is None else result[:limit]

    def list_conflict_resolutions(
        self,
        *,
        allocation_id: str | None = None,
        symbol: str | None = None,
        limit: int | None = None,
    ) -> list[AllocatorConflictResolution]:
        rows = sorted(self._conflict_resolutions.values(), key=lambda item: item.created_at, reverse=True)
        result = [
            item
            for item in rows
            if (allocation_id is None or item.allocation_id == allocation_id)
            and (symbol is None or item.symbol == symbol)
        ]
        return result if limit is None else result[:limit]

    def list_netting_decisions(
        self,
        *,
        allocation_id: str | None = None,
        symbol: str | None = None,
        limit: int | None = None,
    ) -> list[AllocatorNettingDecision]:
        rows = sorted(self._netting_decisions.values(), key=lambda item: item.created_at, reverse=True)
        result = [
            item
            for item in rows
            if (allocation_id is None or item.allocation_id == allocation_id)
            and (symbol is None or item.symbol == symbol)
        ]
        return result if limit is None else result[:limit]
