from __future__ import annotations

import logging
from collections.abc import Callable

from sqlalchemy import desc, or_, select
from sqlalchemy.orm import Session, sessionmaker

from aats.schemas.common import dump_payload_exact
from aats.schemas.portfolio import BASELINE_SNAPSHOT_ORIGINS, TRUSTED_BASELINE_SNAPSHOT_ORIGINS, PortfolioSnapshot
from aats.services.runtime_scope import RuntimeStateScope
from aats.storage.scope_metadata import portfolio_scope_metadata
from aats.storage.sqlalchemy_models import PortfolioSnapshotModel

_logger = logging.getLogger("aats.storage.portfolio_repo_postgres")


class PostgresPortfolioRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory
        # Stage 6 Slice 6.3 hot-fix：portfolio_repo → snapshot_cache 同进程 listener。
        # 详见 docs/task/stage_6_slice_6_3_cache_listener_fix_design.md。
        # listener 只在 save_snapshot (非 in_session 版本) commit 成功后触发。
        # outbox publisher 走 save_snapshot_in_session + 外部 _publish_to_cache，
        # 不经过 listener 钩子，行为不变。
        self._snapshot_listener: Callable[[PortfolioSnapshot], None] | None = None

    def attach_snapshot_listener(
        self, listener: Callable[[PortfolioSnapshot], None]
    ) -> None:
        """注入 snapshot listener，每次 save_snapshot(commit) 后同步调用。

        listener 抛异常会被捕获并 log warning，不会拖垮 save_snapshot 的主路径。
        """
        self._snapshot_listener = listener

    def save_snapshot(self, snapshot: PortfolioSnapshot) -> None:
        with self.session_factory() as session:
            self.save_snapshot_in_session(session, snapshot)
            session.commit()
        # commit 成功后才通知 listener，避免 listener 看到未 commit 数据
        self._notify_listener(snapshot)

    def _notify_listener(self, snapshot: PortfolioSnapshot) -> None:
        listener = self._snapshot_listener
        if listener is None:
            return
        try:
            listener(snapshot)
        except Exception as exc:  # pragma: no cover - best-effort
            _logger.warning(
                "portfolio_repo_snapshot_listener_failed error_type=%s error=%s",
                type(exc).__name__,
                exc,
            )

    def save_snapshot_in_session(self, session: Session, snapshot: PortfolioSnapshot) -> None:
        scope = portfolio_scope_metadata(snapshot)
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
            if limit <= 0:
                return []
            query = query.order_by(None).order_by(desc(PortfolioSnapshotModel.sequence_id)).limit(limit)
        with self.session_factory() as session:
            rows = session.scalars(query).all()
        if limit is not None:
            rows = list(reversed(rows))
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

    def latest_baseline_for_scope(self, *, scope: RuntimeStateScope) -> PortfolioSnapshot | None:
        """Find the latest baseline snapshot via a single DB query.

        Matches snapshots whose ``snapshot_origin`` is a known baseline origin,
        OR legacy baselines where both ``source_fill_id`` and
        ``source_intent_id`` are null/absent (``is_legacy_baseline_snapshot``).
        """
        baseline_origins = list(BASELINE_SNAPSHOT_ORIGINS)
        origin_col = PortfolioSnapshotModel.payload["snapshot_origin"].as_string()
        # Legacy baseline: both source_fill_id and source_intent_id are
        # JSON-null or absent.  PostgreSQL ``->>`` returns SQL NULL for both
        # cases, so ``IS NULL`` covers them.
        src_fill = PortfolioSnapshotModel.payload["source_fill_id"].as_string()
        src_intent = PortfolioSnapshotModel.payload["source_intent_id"].as_string()
        with self.session_factory() as session:
            row = session.scalar(
                select(PortfolioSnapshotModel)
                .where(PortfolioSnapshotModel.product_type == scope.product_type)
                .where(PortfolioSnapshotModel.margin_mode == scope.margin_mode)
                .where(
                    or_(
                        origin_col.in_(baseline_origins),
                        (src_fill.is_(None) & src_intent.is_(None)),
                    )
                )
                .order_by(
                    desc(PortfolioSnapshotModel.snapshot_ts),
                    desc(PortfolioSnapshotModel.sequence_id),
                )
                .limit(1)
            )
        return self._to_snapshot(row) if row is not None else None

    def latest_trusted_baseline_for_scope(self, *, scope: RuntimeStateScope) -> PortfolioSnapshot | None:
        """Find the latest exchange/operator imported baseline via a single DB query."""
        trusted_origins = list(TRUSTED_BASELINE_SNAPSHOT_ORIGINS)
        origin_col = PortfolioSnapshotModel.payload["snapshot_origin"].as_string()
        with self.session_factory() as session:
            row = session.scalar(
                select(PortfolioSnapshotModel)
                .where(PortfolioSnapshotModel.product_type == scope.product_type)
                .where(PortfolioSnapshotModel.margin_mode == scope.margin_mode)
                .where(origin_col.in_(trusted_origins))
                .order_by(
                    desc(PortfolioSnapshotModel.snapshot_ts),
                    desc(PortfolioSnapshotModel.sequence_id),
                )
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
            item.setdefault("position_key", item.get("position_key"))
            item.setdefault("position_qty", 0)
            item.setdefault("position_notional", 0)
            item.setdefault("avg_entry_price", 0)
            item.setdefault("unrealized_pnl", 0)
            item.setdefault("product_type", payload["product_type"])
            item.setdefault("exposure_side", "flat")
            item.setdefault("target_leverage", 1.0)
            item.setdefault("margin_mode", payload["margin_mode"])
            item.setdefault("position_mode", None)
            item.setdefault("pos_side", None)
            item.setdefault("instrument_family", None)
            item.setdefault("settle_currency", None)
            item.setdefault("margin_allocated", 0)
            item.setdefault("maintenance_margin", 0)
            item.setdefault("margin_ratio", None)
            item.setdefault("liquidation_price", None)
            item.setdefault("margin_source", "estimated")
            positions.append(item)
        payload["positions"] = positions
        return PortfolioSnapshot.model_validate(payload)
