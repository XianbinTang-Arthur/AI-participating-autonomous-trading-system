from __future__ import annotations

from sqlalchemy import asc, desc, select
from sqlalchemy.orm import Session, sessionmaker

from aats.schemas.common import dump_payload_exact
from aats.schemas.strategy_runtime import (
    PortfolioAllocationDecision,
    StrategyExecutionBundle,
    StrategySleeveIntent,
)
from aats.storage.sqlalchemy_models import (
    PortfolioAllocationDecisionModel,
    StrategyExecutionBundleModel,
    StrategySleeveIntentModel,
)


class PostgresStrategyRuntimeRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

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
