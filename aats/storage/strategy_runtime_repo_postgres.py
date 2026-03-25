from __future__ import annotations

from sqlalchemy import asc, desc, select
from sqlalchemy.orm import Session, sessionmaker

from aats.schemas.common import dump_payload_exact
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
from aats.storage.sqlalchemy_models import (
    AllocatorBudgetSnapshotModel,
    AllocatorConflictResolutionModel,
    AllocatorNettingDecisionModel,
    PortfolioAllocationDecisionModel,
    SleeveBudgetAssignmentModel,
    SleeveBudgetProfileModel,
    StrategyExecutionBundleModel,
    StrategySleeveIntentModel,
)


class PostgresStrategyRuntimeRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def save_budget_profile(self, profile: SleeveBudgetProfile) -> SleeveBudgetProfile:
        payload = dump_payload_exact(profile)
        with self.session_factory() as session:
            row = session.get(SleeveBudgetProfileModel, profile.budget_profile_id)
            if row is None:
                row = SleeveBudgetProfileModel(
                    budget_profile_id=profile.budget_profile_id,
                    family=profile.family,
                    product_type=profile.product_type,
                    margin_mode=profile.margin_mode,
                    symbol_scope_json=list(profile.symbol_scope),
                    quote_budget_limit=profile.quote_budget_limit,
                    margin_budget_limit=profile.margin_budget_limit,
                    notional_cap=profile.notional_cap,
                    max_symbol_notional=profile.max_symbol_notional,
                    max_drawdown_usdt=profile.max_drawdown_usdt,
                    allocator_base_weight=profile.allocator_base_weight,
                    hedge_priority_class=profile.hedge_priority_class,
                    created_at=profile.created_at,
                    updated_at=profile.updated_at,
                    payload=payload,
                )
                session.add(row)
            else:
                row.family = profile.family
                row.product_type = profile.product_type
                row.margin_mode = profile.margin_mode
                row.symbol_scope_json = list(profile.symbol_scope)
                row.quote_budget_limit = profile.quote_budget_limit
                row.margin_budget_limit = profile.margin_budget_limit
                row.notional_cap = profile.notional_cap
                row.max_symbol_notional = profile.max_symbol_notional
                row.max_drawdown_usdt = profile.max_drawdown_usdt
                row.allocator_base_weight = profile.allocator_base_weight
                row.hedge_priority_class = profile.hedge_priority_class
                row.created_at = profile.created_at
                row.updated_at = profile.updated_at
                row.payload = payload
            session.commit()
        return profile

    def list_budget_profiles(
        self,
        *,
        product_type: str | None = None,
        margin_mode: str | None = None,
        family: str | None = None,
    ) -> list[SleeveBudgetProfile]:
        with self.session_factory() as session:
            stmt = select(SleeveBudgetProfileModel)
            if product_type is not None:
                stmt = stmt.where(SleeveBudgetProfileModel.product_type == product_type)
            if margin_mode is not None:
                stmt = stmt.where(SleeveBudgetProfileModel.margin_mode == margin_mode)
            if family is not None:
                stmt = stmt.where(SleeveBudgetProfileModel.family == family)
            stmt = stmt.order_by(desc(SleeveBudgetProfileModel.updated_at), desc(SleeveBudgetProfileModel.budget_profile_id))
            rows = session.scalars(stmt).all()
        return [SleeveBudgetProfile.model_validate(row.payload) for row in rows]

    def save_budget_assignment(self, assignment: SleeveBudgetAssignment) -> SleeveBudgetAssignment:
        payload = dump_payload_exact(assignment)
        with self.session_factory() as session:
            row = session.get(SleeveBudgetAssignmentModel, assignment.assignment_id)
            if row is None:
                row = SleeveBudgetAssignmentModel(
                    assignment_id=assignment.assignment_id,
                    budget_profile_id=assignment.budget_profile_id,
                    strategy_sleeve_id=assignment.strategy_sleeve_id,
                    family=assignment.family,
                    symbol=assignment.symbol,
                    product_type=assignment.product_type,
                    margin_mode=assignment.margin_mode,
                    active_budget_multiplier=assignment.active_budget_multiplier,
                    allocator_base_weight=assignment.allocator_base_weight,
                    effective_quote_budget_limit=assignment.effective_quote_budget_limit,
                    effective_margin_budget_limit=assignment.effective_margin_budget_limit,
                    effective_notional_cap=assignment.effective_notional_cap,
                    effective_max_symbol_notional=assignment.effective_max_symbol_notional,
                    hedge_priority_class=assignment.hedge_priority_class,
                    created_at=assignment.created_at,
                    updated_at=assignment.updated_at,
                    payload=payload,
                )
                session.add(row)
            else:
                row.budget_profile_id = assignment.budget_profile_id
                row.strategy_sleeve_id = assignment.strategy_sleeve_id
                row.family = assignment.family
                row.symbol = assignment.symbol
                row.product_type = assignment.product_type
                row.margin_mode = assignment.margin_mode
                row.active_budget_multiplier = assignment.active_budget_multiplier
                row.allocator_base_weight = assignment.allocator_base_weight
                row.effective_quote_budget_limit = assignment.effective_quote_budget_limit
                row.effective_margin_budget_limit = assignment.effective_margin_budget_limit
                row.effective_notional_cap = assignment.effective_notional_cap
                row.effective_max_symbol_notional = assignment.effective_max_symbol_notional
                row.hedge_priority_class = assignment.hedge_priority_class
                row.created_at = assignment.created_at
                row.updated_at = assignment.updated_at
                row.payload = payload
            session.commit()
        return assignment

    def list_budget_assignments(
        self,
        *,
        product_type: str | None = None,
        margin_mode: str | None = None,
        symbol: str | None = None,
        strategy_sleeve_id: str | None = None,
    ) -> list[SleeveBudgetAssignment]:
        with self.session_factory() as session:
            stmt = select(SleeveBudgetAssignmentModel)
            if product_type is not None:
                stmt = stmt.where(SleeveBudgetAssignmentModel.product_type == product_type)
            if margin_mode is not None:
                stmt = stmt.where(SleeveBudgetAssignmentModel.margin_mode == margin_mode)
            if symbol is not None:
                stmt = stmt.where(SleeveBudgetAssignmentModel.symbol == symbol)
            if strategy_sleeve_id is not None:
                stmt = stmt.where(SleeveBudgetAssignmentModel.strategy_sleeve_id == strategy_sleeve_id)
            stmt = stmt.order_by(
                desc(SleeveBudgetAssignmentModel.updated_at),
                desc(SleeveBudgetAssignmentModel.assignment_id),
            )
            rows = session.scalars(stmt).all()
        return [SleeveBudgetAssignment.model_validate(row.payload) for row in rows]

    def save_sleeve_intent(self, intent: StrategySleeveIntent) -> StrategySleeveIntent:
        payload = dump_payload_exact(intent)
        with self.session_factory() as session:
            row = session.get(StrategySleeveIntentModel, intent.sleeve_intent_id)
            if row is None:
                row = StrategySleeveIntentModel(
                    sleeve_intent_id=intent.sleeve_intent_id,
                    decision_id=intent.decision_id,
                    family=intent.family,
                    strategy_sleeve_id=intent.strategy_sleeve_id,
                    state=intent.state,
                    symbol=intent.symbol,
                    product_type=intent.product_type,
                    margin_mode=intent.margin_mode,
                    inventory_policy=intent.inventory_policy,
                    route_action=intent.route_action,
                    allocation_id=intent.allocation_id,
                    automatic_enabled=intent.automatic_enabled,
                    budget_multiplier=intent.budget_multiplier,
                    allocator_weight=intent.allocator_weight,
                    created_at=intent.created_at,
                    payload=payload,
                )
                session.add(row)
            else:
                row.decision_id = intent.decision_id
                row.family = intent.family
                row.strategy_sleeve_id = intent.strategy_sleeve_id
                row.state = intent.state
                row.symbol = intent.symbol
                row.product_type = intent.product_type
                row.margin_mode = intent.margin_mode
                row.inventory_policy = intent.inventory_policy
                row.route_action = intent.route_action
                row.allocation_id = intent.allocation_id
                row.automatic_enabled = intent.automatic_enabled
                row.budget_multiplier = intent.budget_multiplier
                row.allocator_weight = intent.allocator_weight
                row.created_at = intent.created_at
                row.payload = payload
            session.commit()
        return intent

    def list_sleeve_intents(
        self,
        *,
        product_type: str | None = None,
        margin_mode: str | None = None,
        symbol: str | None = None,
        limit: int | None = None,
    ) -> list[StrategySleeveIntent]:
        with self.session_factory() as session:
            stmt = select(StrategySleeveIntentModel)
            if product_type is not None:
                stmt = stmt.where(StrategySleeveIntentModel.product_type == product_type)
            if margin_mode is not None:
                stmt = stmt.where(StrategySleeveIntentModel.margin_mode == margin_mode)
            if symbol is not None:
                stmt = stmt.where(StrategySleeveIntentModel.symbol == symbol)
            stmt = stmt.order_by(desc(StrategySleeveIntentModel.created_at), desc(StrategySleeveIntentModel.sleeve_intent_id))
            if limit is not None:
                stmt = stmt.limit(limit)
            rows = session.scalars(stmt).all()
        return [StrategySleeveIntent.model_validate(row.payload) for row in rows]

    def save_allocation_decision(self, decision: PortfolioAllocationDecision) -> PortfolioAllocationDecision:
        payload = dump_payload_exact(decision)
        with self.session_factory() as session:
            row = session.get(PortfolioAllocationDecisionModel, decision.allocation_id)
            if row is None:
                row = PortfolioAllocationDecisionModel(
                    allocation_id=decision.allocation_id,
                    decision_id=decision.decision_id,
                    symbol=decision.symbol,
                    product_type=decision.product_type,
                    margin_mode=decision.margin_mode,
                    allocator_version=decision.allocator_version,
                    automatic_enabled=decision.automatic_enabled,
                    route_action=decision.route_action,
                    primary_family=decision.primary_family,
                    primary_strategy_sleeve_id=decision.primary_strategy_sleeve_id,
                    created_at=decision.created_at,
                    payload=payload,
                )
                session.add(row)
            else:
                row.decision_id = decision.decision_id
                row.symbol = decision.symbol
                row.product_type = decision.product_type
                row.margin_mode = decision.margin_mode
                row.allocator_version = decision.allocator_version
                row.automatic_enabled = decision.automatic_enabled
                row.route_action = decision.route_action
                row.primary_family = decision.primary_family
                row.primary_strategy_sleeve_id = decision.primary_strategy_sleeve_id
                row.created_at = decision.created_at
                row.payload = payload
            self._sync_budget_snapshots(session, decision.budget_snapshots)
            self._sync_conflict_resolutions(session, decision.conflict_resolutions)
            self._sync_netting_decisions(session, decision.netting_decisions)
            session.commit()
        return decision

    def latest_allocation_decision(
        self,
        *,
        product_type: str | None = None,
        margin_mode: str | None = None,
        symbol: str | None = None,
    ) -> PortfolioAllocationDecision | None:
        with self.session_factory() as session:
            stmt = select(PortfolioAllocationDecisionModel)
            if product_type is not None:
                stmt = stmt.where(PortfolioAllocationDecisionModel.product_type == product_type)
            if margin_mode is not None:
                stmt = stmt.where(PortfolioAllocationDecisionModel.margin_mode == margin_mode)
            if symbol is not None:
                stmt = stmt.where(PortfolioAllocationDecisionModel.symbol == symbol)
            stmt = stmt.order_by(
                desc(PortfolioAllocationDecisionModel.created_at),
                desc(PortfolioAllocationDecisionModel.allocation_id),
            ).limit(1)
            row = session.scalar(stmt)
        return None if row is None else PortfolioAllocationDecision.model_validate(row.payload)

    def save_execution_bundle(self, bundle: StrategyExecutionBundle) -> StrategyExecutionBundle:
        payload = dump_payload_exact(bundle)
        with self.session_factory() as session:
            row = session.get(StrategyExecutionBundleModel, bundle.bundle_id)
            if row is None:
                row = StrategyExecutionBundleModel(
                    bundle_id=bundle.bundle_id,
                    decision_id=bundle.decision_id,
                    family=bundle.family,
                    strategy_sleeve_id=bundle.strategy_sleeve_id,
                    allocation_id=bundle.allocation_id,
                    product_type=bundle.product_type,
                    margin_mode=bundle.margin_mode,
                    route_action=bundle.route_action,
                    status=bundle.status,
                    selected_symbol=bundle.selected_symbol,
                    created_at=bundle.created_at,
                    payload=payload,
                )
                session.add(row)
            else:
                row.decision_id = bundle.decision_id
                row.family = bundle.family
                row.strategy_sleeve_id = bundle.strategy_sleeve_id
                row.allocation_id = bundle.allocation_id
                row.product_type = bundle.product_type
                row.margin_mode = bundle.margin_mode
                row.route_action = bundle.route_action
                row.status = bundle.status
                row.selected_symbol = bundle.selected_symbol
                row.created_at = bundle.created_at
                row.payload = payload
            session.commit()
        return bundle

    def recent_execution_bundles(
        self,
        *,
        product_type: str | None = None,
        margin_mode: str | None = None,
        symbol: str | None = None,
        limit: int | None = None,
    ) -> list[StrategyExecutionBundle]:
        with self.session_factory() as session:
            stmt = select(StrategyExecutionBundleModel)
            if product_type is not None:
                stmt = stmt.where(StrategyExecutionBundleModel.product_type == product_type)
            if margin_mode is not None:
                stmt = stmt.where(StrategyExecutionBundleModel.margin_mode == margin_mode)
            if symbol is not None:
                stmt = stmt.where(StrategyExecutionBundleModel.selected_symbol == symbol)
            stmt = stmt.order_by(
                desc(StrategyExecutionBundleModel.created_at),
                asc(StrategyExecutionBundleModel.bundle_id),
            )
            if limit is not None:
                stmt = stmt.limit(limit)
            rows = session.scalars(stmt).all()
        return [StrategyExecutionBundle.model_validate(row.payload) for row in rows]

    def list_budget_snapshots(
        self,
        *,
        allocation_id: str | None = None,
        strategy_sleeve_id: str | None = None,
    ) -> list[AllocatorBudgetSnapshot]:
        with self.session_factory() as session:
            stmt = select(AllocatorBudgetSnapshotModel)
            if allocation_id is not None:
                stmt = stmt.where(AllocatorBudgetSnapshotModel.allocation_id == allocation_id)
            if strategy_sleeve_id is not None:
                stmt = stmt.where(AllocatorBudgetSnapshotModel.strategy_sleeve_id == strategy_sleeve_id)
            stmt = stmt.order_by(
                desc(AllocatorBudgetSnapshotModel.created_at),
                desc(AllocatorBudgetSnapshotModel.budget_snapshot_id),
            )
            rows = session.scalars(stmt).all()
        return [AllocatorBudgetSnapshot.model_validate(row.payload) for row in rows]

    def list_conflict_resolutions(
        self,
        *,
        allocation_id: str | None = None,
        symbol: str | None = None,
    ) -> list[AllocatorConflictResolution]:
        with self.session_factory() as session:
            stmt = select(AllocatorConflictResolutionModel)
            if allocation_id is not None:
                stmt = stmt.where(AllocatorConflictResolutionModel.allocation_id == allocation_id)
            if symbol is not None:
                stmt = stmt.where(AllocatorConflictResolutionModel.symbol == symbol)
            stmt = stmt.order_by(
                desc(AllocatorConflictResolutionModel.created_at),
                desc(AllocatorConflictResolutionModel.conflict_resolution_id),
            )
            rows = session.scalars(stmt).all()
        return [AllocatorConflictResolution.model_validate(row.payload) for row in rows]

    def list_netting_decisions(
        self,
        *,
        allocation_id: str | None = None,
        symbol: str | None = None,
    ) -> list[AllocatorNettingDecision]:
        with self.session_factory() as session:
            stmt = select(AllocatorNettingDecisionModel)
            if allocation_id is not None:
                stmt = stmt.where(AllocatorNettingDecisionModel.allocation_id == allocation_id)
            if symbol is not None:
                stmt = stmt.where(AllocatorNettingDecisionModel.symbol == symbol)
            stmt = stmt.order_by(
                desc(AllocatorNettingDecisionModel.created_at),
                desc(AllocatorNettingDecisionModel.netting_decision_id),
            )
            rows = session.scalars(stmt).all()
        return [AllocatorNettingDecision.model_validate(row.payload) for row in rows]

    @staticmethod
    def _sync_budget_snapshots(session: Session, items: list[AllocatorBudgetSnapshot]) -> None:
        for item in items:
            payload = dump_payload_exact(item)
            row = session.get(AllocatorBudgetSnapshotModel, item.budget_snapshot_id)
            if row is None:
                row = AllocatorBudgetSnapshotModel(
                    budget_snapshot_id=item.budget_snapshot_id,
                    allocation_id=item.allocation_id,
                    strategy_sleeve_id=item.strategy_sleeve_id,
                    family=item.family,
                    symbol=item.symbol,
                    product_type=item.product_type,
                    margin_mode=item.margin_mode,
                    requested_notional=item.requested_notional,
                    approved_notional=item.approved_notional,
                    requested_delta_qty=item.requested_delta_qty,
                    approved_delta_qty=item.approved_delta_qty,
                    budget_multiplier=item.budget_multiplier,
                    allocator_weight=item.allocator_weight,
                    quote_budget_limit=item.quote_budget_limit,
                    margin_budget_limit=item.margin_budget_limit,
                    notional_cap=item.notional_cap,
                    max_symbol_notional=item.max_symbol_notional,
                    hedge_priority_class=item.hedge_priority_class,
                    clamped=item.clamped,
                    created_at=item.created_at,
                    payload=payload,
                )
                session.add(row)
                continue
            row.allocation_id = item.allocation_id
            row.strategy_sleeve_id = item.strategy_sleeve_id
            row.family = item.family
            row.symbol = item.symbol
            row.product_type = item.product_type
            row.margin_mode = item.margin_mode
            row.requested_notional = item.requested_notional
            row.approved_notional = item.approved_notional
            row.requested_delta_qty = item.requested_delta_qty
            row.approved_delta_qty = item.approved_delta_qty
            row.budget_multiplier = item.budget_multiplier
            row.allocator_weight = item.allocator_weight
            row.quote_budget_limit = item.quote_budget_limit
            row.margin_budget_limit = item.margin_budget_limit
            row.notional_cap = item.notional_cap
            row.max_symbol_notional = item.max_symbol_notional
            row.hedge_priority_class = item.hedge_priority_class
            row.clamped = item.clamped
            row.created_at = item.created_at
            row.payload = payload

    @staticmethod
    def _sync_conflict_resolutions(session: Session, items: list[AllocatorConflictResolution]) -> None:
        for item in items:
            payload = dump_payload_exact(item)
            row = session.get(AllocatorConflictResolutionModel, item.conflict_resolution_id)
            if row is None:
                row = AllocatorConflictResolutionModel(
                    conflict_resolution_id=item.conflict_resolution_id,
                    allocation_id=item.allocation_id,
                    symbol=item.symbol,
                    product_type=item.product_type,
                    margin_mode=item.margin_mode,
                    conflict_type=item.conflict_type,
                    resolution_action=item.resolution_action,
                    gross_requested_qty=item.gross_requested_qty,
                    net_approved_qty=item.net_approved_qty,
                    blocked_qty=item.blocked_qty,
                    protected_notional=item.protected_notional,
                    reduced_notional=item.reduced_notional,
                    created_at=item.created_at,
                    payload=payload,
                )
                session.add(row)
                continue
            row.allocation_id = item.allocation_id
            row.symbol = item.symbol
            row.product_type = item.product_type
            row.margin_mode = item.margin_mode
            row.conflict_type = item.conflict_type
            row.resolution_action = item.resolution_action
            row.gross_requested_qty = item.gross_requested_qty
            row.net_approved_qty = item.net_approved_qty
            row.blocked_qty = item.blocked_qty
            row.protected_notional = item.protected_notional
            row.reduced_notional = item.reduced_notional
            row.created_at = item.created_at
            row.payload = payload

    @staticmethod
    def _sync_netting_decisions(session: Session, items: list[AllocatorNettingDecision]) -> None:
        for item in items:
            payload = dump_payload_exact(item)
            row = session.get(AllocatorNettingDecisionModel, item.netting_decision_id)
            if row is None:
                row = AllocatorNettingDecisionModel(
                    netting_decision_id=item.netting_decision_id,
                    allocation_id=item.allocation_id,
                    symbol=item.symbol,
                    product_type=item.product_type,
                    margin_mode=item.margin_mode,
                    gross_buy_qty=item.gross_buy_qty,
                    gross_sell_qty=item.gross_sell_qty,
                    net_approved_qty=item.net_approved_qty,
                    created_at=item.created_at,
                    payload=payload,
                )
                session.add(row)
                continue
            row.allocation_id = item.allocation_id
            row.symbol = item.symbol
            row.product_type = item.product_type
            row.margin_mode = item.margin_mode
            row.gross_buy_qty = item.gross_buy_qty
            row.gross_sell_qty = item.gross_sell_qty
            row.net_approved_qty = item.net_approved_qty
            row.created_at = item.created_at
            row.payload = payload
