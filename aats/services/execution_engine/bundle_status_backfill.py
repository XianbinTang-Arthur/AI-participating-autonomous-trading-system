from __future__ import annotations

from typing import Any

from sqlalchemy import Select, inspect, select
from sqlalchemy.orm import Session

from aats.schemas.common import dump_payload_exact
from aats.schemas.execution import OrderState
from aats.schemas.strategy_runtime import StrategyExecutionBundle
from aats.services.execution_engine.bundle_status import (
    apply_strategy_bundle_status_reason_codes,
    derive_strategy_bundle_status,
)
from aats.storage.execution_repo_converged_postgres import (
    ConvergedPostgresExecutionRepository,
    _order_model_to_dict,
)
from aats.storage.execution_repo_postgres import PostgresExecutionRepository
from aats.storage.sqlalchemy_models import (
    ExecutionOrderModel,
    OrderStateModel,
    StrategyExecutionBundleModel,
)


def _hydrate_order_state(row: OrderStateModel) -> OrderState:
    return PostgresExecutionRepository._to_order_state(row)


def _hydrate_bundle(row: StrategyExecutionBundleModel) -> StrategyExecutionBundle:
    payload = dict(row.payload or {})
    payload.setdefault("bundle_id", row.bundle_id)
    payload.setdefault("decision_id", row.decision_id)
    payload.setdefault("family", row.family)
    payload.setdefault("strategy_sleeve_id", row.strategy_sleeve_id)
    payload.setdefault("allocation_id", row.allocation_id)
    payload.setdefault("product_type", row.product_type)
    payload.setdefault("margin_mode", row.margin_mode)
    payload.setdefault("route_action", row.route_action)
    payload.setdefault("bundle_type", row.bundle_type)
    payload.setdefault("bundle_priority", row.bundle_priority)
    payload.setdefault("status", row.status)
    payload.setdefault("selected_symbol", row.selected_symbol)
    payload.setdefault("gross_requested_exposure", row.gross_requested_exposure)
    payload.setdefault("net_approved_exposure", row.net_approved_exposure)
    payload.setdefault("expected_cost_bps", row.expected_cost_bps)
    payload.setdefault("expected_edge_bps", row.expected_edge_bps)
    payload.setdefault("portfolio_risk_budget_state", row.portfolio_risk_budget_state)
    payload.setdefault("created_at", row.created_at)
    return StrategyExecutionBundle.model_validate(payload)


def _resolve_order_storage_source(session: Session) -> str | None:
    inspector = inspect(session.bind)
    if not inspector.has_table("strategy_execution_bundles"):
        return None
    if inspector.has_table("order_states"):
        return "legacy"
    if inspector.has_table("execution_orders"):
        return "converged"
    return None


def _load_order_states_for_bundle(
    session: Session,
    *,
    bundle_id: str,
    storage_source: str,
) -> list[OrderState]:
    if storage_source == "legacy":
        order_rows = session.scalars(
            select(OrderStateModel)
            .where(OrderStateModel.strategy_bundle_id == bundle_id)
            .order_by(OrderStateModel.created_at.asc(), OrderStateModel.client_order_id.asc())
        ).all()
        return [_hydrate_order_state(order_row) for order_row in order_rows]
    if storage_source == "converged":
        order_rows = session.scalars(
            select(ExecutionOrderModel)
            .where(ExecutionOrderModel.strategy_bundle_id == bundle_id)
            .order_by(ExecutionOrderModel.created_at.asc(), ExecutionOrderModel.order_id.asc())
        ).all()
        return [
            ConvergedPostgresExecutionRepository._hydrate_order_state(_order_model_to_dict(order_row))
            for order_row in order_rows
        ]
    raise ValueError(f"unsupported_order_storage_source:{storage_source}")


def derive_backfilled_independent_bundle(
    bundle: StrategyExecutionBundle,
    order_states: list[OrderState],
) -> StrategyExecutionBundle | None:
    derived_status = derive_strategy_bundle_status(
        order_states=order_states,
        previous_status=bundle.status,
    )
    if derived_status != "blocked" or bundle.status == "blocked":
        return None
    return bundle.model_copy(
        update={
            "status": "blocked",
            "reason_codes": apply_strategy_bundle_status_reason_codes(
                reason_codes=list(bundle.reason_codes),
                status="blocked",
            ),
        },
    )


def _candidate_bundle_stmt(
    *,
    bundle_ids: list[str] | None = None,
    limit: int | None = None,
) -> Select[tuple[StrategyExecutionBundleModel]]:
    stmt = (
        select(StrategyExecutionBundleModel)
        .where(StrategyExecutionBundleModel.family == "independent")
        .where(StrategyExecutionBundleModel.product_type == "derivatives")
        .where(StrategyExecutionBundleModel.status == "review_required")
        .order_by(StrategyExecutionBundleModel.created_at.asc(), StrategyExecutionBundleModel.bundle_id.asc())
    )
    if bundle_ids:
        stmt = stmt.where(StrategyExecutionBundleModel.bundle_id.in_(bundle_ids))
    if limit is not None:
        stmt = stmt.limit(limit)
    return stmt


def backfill_independent_blocked_bundles(
    session: Session,
    *,
    bundle_ids: list[str] | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    storage_source = _resolve_order_storage_source(session)
    if storage_source is None:
        return {
            "ok": False,
            "scanned": 0,
            "updated": 0,
            "skipped_no_orders": 0,
            "skipped_not_blocked": 0,
            "dry_run": dry_run,
            "results": [],
            "skipped_reason": "required_tables_missing",
        }

    scanned = 0
    updated = 0
    skipped_no_orders = 0
    skipped_not_blocked = 0
    results: list[dict[str, Any]] = []

    rows = session.scalars(_candidate_bundle_stmt(bundle_ids=bundle_ids, limit=limit)).all()
    for row in rows:
        scanned += 1
        bundle = _hydrate_bundle(row)
        order_states = _load_order_states_for_bundle(
            session,
            bundle_id=row.bundle_id,
            storage_source=storage_source,
        )
        if not order_states:
            skipped_no_orders += 1
            results.append({
                "bundle_id": row.bundle_id,
                "ok": False,
                "reason": "no_order_states",
            })
            continue
        updated_bundle = derive_backfilled_independent_bundle(bundle, order_states)
        if updated_bundle is None:
            skipped_not_blocked += 1
            results.append({
                "bundle_id": row.bundle_id,
                "ok": True,
                "updated": False,
                "status": bundle.status,
            })
            continue

        if not dry_run:
            row.status = updated_bundle.status
            row.payload = dump_payload_exact(updated_bundle)
            row.row_version = int(row.row_version or 0) + 1
        updated += 1
        results.append({
            "bundle_id": row.bundle_id,
            "ok": True,
            "updated": True,
            "old_status": bundle.status,
            "new_status": updated_bundle.status,
        })

    return {
        "ok": True,
        "storage_source": storage_source,
        "scanned": scanned,
        "updated": updated,
        "skipped_no_orders": skipped_no_orders,
        "skipped_not_blocked": skipped_not_blocked,
        "dry_run": dry_run,
        "results": results,
    }
