from __future__ import annotations

from sqlalchemy import desc, select
from sqlalchemy.orm import Session, sessionmaker

from aats.schemas.common import dump_payload_exact
from aats.schemas.portfolio import PortfolioSnapshot
from aats.services.runtime_scope import RuntimeStateScope
from aats.storage.scope_metadata import portfolio_scope_metadata
from aats.storage.sqlalchemy_models import PortfolioSnapshotModel


class PostgresPortfolioRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def save_snapshot(self, snapshot: PortfolioSnapshot) -> None:
        scope = portfolio_scope_metadata(snapshot)
        with self.session_factory() as session:
            session.add(
                PortfolioSnapshotModel(
                    snapshot_ts=snapshot.snapshot_ts,
                    created_at=snapshot.created_at,
                    total_equity=snapshot.total_equity,
                    realized_pnl=snapshot.realized_pnl,
                    unrealized_pnl=snapshot.unrealized_pnl,
                    product_type=scope["product_type"] or snapshot.product_type,
                    margin_mode=scope["margin_mode"] or snapshot.margin_mode,
                    primary_symbol=scope["primary_symbol"],
                    payload=dump_payload_exact(snapshot),
                )
            )
            session.commit()

    def latest(self) -> PortfolioSnapshot | None:
        with self.session_factory() as session:
            row = session.scalar(
                select(PortfolioSnapshotModel).order_by(desc(PortfolioSnapshotModel.sequence_id)).limit(1)
            )
        return self._to_snapshot(row) if row is not None else None

    def history(self) -> list[PortfolioSnapshot]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(PortfolioSnapshotModel).order_by(PortfolioSnapshotModel.sequence_id)
            ).all()
        return [self._to_snapshot(row) for row in rows]

    def recent_history(self, *, limit: int) -> list[PortfolioSnapshot]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(PortfolioSnapshotModel)
                .order_by(desc(PortfolioSnapshotModel.sequence_id))
                .limit(limit)
            ).all()
        return [self._to_snapshot(row) for row in reversed(rows)]

    def history_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
        limit: int | None = None,
    ) -> list[PortfolioSnapshot]:
        query = (
            select(PortfolioSnapshotModel)
            .where(PortfolioSnapshotModel.product_type == scope.product_type)
            .where(PortfolioSnapshotModel.margin_mode == scope.margin_mode)
            .order_by(PortfolioSnapshotModel.sequence_id)
        )
        if limit is not None:
            query = query.limit(limit)
        with self.session_factory() as session:
            rows = session.scalars(query).all()
        return [self._to_snapshot(row) for row in rows]

    def latest_for_scope(self, *, scope: RuntimeStateScope) -> PortfolioSnapshot | None:
        with self.session_factory() as session:
            row = session.scalar(
                select(PortfolioSnapshotModel)
                .where(PortfolioSnapshotModel.product_type == scope.product_type)
                .where(PortfolioSnapshotModel.margin_mode == scope.margin_mode)
                .order_by(desc(PortfolioSnapshotModel.sequence_id))
                .limit(1)
            )
        return self._to_snapshot(row) if row is not None else None

    @staticmethod
    def _to_snapshot(row: PortfolioSnapshotModel) -> PortfolioSnapshot:
        payload = dict(row.payload)
        payload.setdefault("snapshot_ts", row.snapshot_ts)
        payload.setdefault("created_at", row.created_at)
        payload.setdefault("balances", payload.get("balances") or {})
        payload.setdefault("realized_pnl", row.realized_pnl)
        payload.setdefault("unrealized_pnl", row.unrealized_pnl)
        payload.setdefault("total_equity", row.total_equity)
        payload.setdefault("gross_exposure", payload.get("gross_exposure") or 0)
        payload.setdefault("net_exposure", payload.get("net_exposure") or 0)
        payload.setdefault("product_type", row.product_type or payload.get("product_type") or "spot")
        payload.setdefault("margin_mode", row.margin_mode or payload.get("margin_mode") or "cash")
        payload.setdefault("cost_basis", payload.get("cost_basis") or {})
        payload.setdefault("risk_budget_usage", payload.get("risk_budget_usage") or {})
        payload.setdefault("margin_usage", payload.get("margin_usage") or 0)
        payload.setdefault("leverage_profile", payload.get("leverage_profile") or {})
        payload.setdefault("cash_equity", payload.get("cash_equity") or 0)
        payload.setdefault("spot_asset_equity", payload.get("spot_asset_equity") or 0)
        payload.setdefault("off_position_asset_equity", payload.get("off_position_asset_equity") or 0)
        payload.setdefault("derivatives_unrealized_pnl", payload.get("derivatives_unrealized_pnl") or 0)
        payload.setdefault("collateral_value", payload.get("collateral_value") or 0)
        positions = []
        for position in payload.get("positions") or []:
            item = dict(position)
            item.setdefault("symbol", item.get("symbol") or row.primary_symbol or "legacy_unknown")
            item.setdefault("position_qty", 0)
            item.setdefault("position_notional", 0)
            item.setdefault("avg_entry_price", 0)
            item.setdefault("unrealized_pnl", 0)
            item.setdefault("product_type", payload["product_type"])
            item.setdefault("exposure_side", "flat")
            item.setdefault("target_leverage", 1.0)
            item.setdefault("margin_mode", payload["margin_mode"])
            item.setdefault("margin_allocated", 0)
            item.setdefault("maintenance_margin", 0)
            item.setdefault("liquidation_price", None)
            positions.append(item)
        payload["positions"] = positions
        return PortfolioSnapshot.model_validate(payload)
