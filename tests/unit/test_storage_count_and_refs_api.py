"""S1 新增 API 的契约测试。

测试 gateway_slow_query_systematic_fix_sow.md §S1 引入的两个新 API：

1. ``EventStore.count_by_topic_scoped`` —— 纯 count，不反序列化 payload
2. ``ReconciliationRepository.portfolio_snapshot_refs_for_scope`` —— 直接返回
   去重后的 portfolio_snapshot_ref 集合

核心契约：count / refs 的返回必须与"走老路径（by_topic_scoped / history_for_scope）
再在 Python 里 len() / set-comp"完全一致——否则 _build_metrics 的 dashboard
面板数字会静默飘移。
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from aats.schemas.audit import DecisionAuditRecord
from aats.schemas.common import EventEnvelope, utc_now
from aats.schemas.execution import FillEvent, OrderState
from aats.schemas.portfolio import PortfolioSnapshot
from aats.schemas.reconciliation import ReconciliationReport
from aats.services.runtime_scope import RuntimeStateScope
from aats.storage.audit_repo import InMemoryAuditRepository
from aats.storage.audit_repo_postgres import PostgresAuditRepository
from aats.storage.event_store import InMemoryEventStore
from aats.storage.execution_fill_repo_v2_postgres import PostgresExecutionFillRepositoryV2
from aats.storage.execution_repo_postgres import PostgresExecutionRepository
from aats.storage.portfolio_repo_postgres import PostgresPortfolioRepository
from aats.storage.reconciliation_repo import InMemoryReconciliationRepository
from aats.storage.reconciliation_repo_postgres import PostgresReconciliationRepository
from aats.storage.sqlalchemy_models import Base


def _spot_scope() -> RuntimeStateScope:
    return RuntimeStateScope(
        product_type="spot",
        margin_mode="cash",
        default_symbol="BTC-USDT",
        allowed_symbols=("BTC-USDT",),
        smart_arbitrage_spot_margin_modes=(),
    )


def _derivatives_scope() -> RuntimeStateScope:
    return RuntimeStateScope(
        product_type="derivatives",
        margin_mode="cross",
        default_symbol="BTC-USDT-SWAP",
        allowed_symbols=("BTC-USDT-SWAP",),
        smart_arbitrage_spot_margin_modes=(),
    )


def _event(*, topic: str, symbol: str, product_type: str, margin_mode: str) -> EventEnvelope:
    return EventEnvelope(
        event_type="test.event",
        event_timestamp=utc_now(),
        source_component="test",
        topic=topic,
        key=symbol,
        payload={
            "symbol": symbol,
            "product_type": product_type,
            "margin_mode": margin_mode,
        },
    )


def _reconciliation_report(
    *,
    reconciliation_id: str,
    product_type: str,
    margin_mode: str,
    portfolio_snapshot_ref: str | None,
    as_of_ts: datetime | None = None,
) -> ReconciliationReport:
    return ReconciliationReport(
        reconciliation_id=reconciliation_id,
        portfolio_snapshot_ref=portfolio_snapshot_ref,
        as_of_ts=as_of_ts or datetime.now(timezone.utc),
        product_type=product_type,
        margin_mode=margin_mode,
        allowed_symbols=["BTC-USDT" if product_type == "spot" else "BTC-USDT-SWAP"],
        order_diff={},
        fill_diff={},
        balance_diff={},
        position_diff={},
        severity="OK",
        halt_required=False,
    )


def _session_factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def _portfolio_snapshot(
    *,
    decision_id: str,
    snapshot_ts: datetime,
    product_type: str = "spot",
    margin_mode: str = "cash",
) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        decision_id=decision_id,
        snapshot_ts=snapshot_ts,
        balances={"USDT": Decimal("100")},
        positions=[],
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        total_equity=Decimal("100"),
        gross_exposure=Decimal("0"),
        net_exposure=Decimal("0"),
        product_type=product_type,  # type: ignore[arg-type]
        margin_mode=margin_mode,  # type: ignore[arg-type]
    )


def _fill_event(
    *,
    fill_id: str,
    fill_ts: datetime,
    product_type: str = "spot",
    margin_mode: str = "cash",
    symbol: str = "BTC-USDT",
) -> FillEvent:
    return FillEvent(
        fill_id=fill_id,
        decision_id=f"decision_{fill_id}",
        intent_id=f"intent_{fill_id}",
        client_order_id=f"client_{fill_id}",
        exchange_order_id=f"exchange_{fill_id}",
        symbol=symbol,
        side="buy",
        fill_qty=Decimal("0.01"),
        fill_price=Decimal("100"),
        fee_amount=Decimal("0.01"),
        liquidity_role="taker",
        exchange_timestamp=fill_ts,
        ingestion_timestamp=fill_ts,
        product_type=product_type,  # type: ignore[arg-type]
        margin_mode=margin_mode,  # type: ignore[arg-type]
    )


def _order_state(
    *,
    client_order_id: str,
    created_at: datetime,
    last_update_ts: datetime | None = None,
    product_type: str = "spot",
    margin_mode: str = "cash",
    symbol: str = "BTC-USDT",
) -> OrderState:
    return OrderState(
        decision_id=f"decision_{client_order_id}",
        intent_id=f"intent_{client_order_id}",
        symbol=symbol,
        client_order_id=client_order_id,
        status="SUBMITTED",
        created_at=created_at,
        submitted_ts=created_at,
        last_update_ts=last_update_ts,
        requested_qty=Decimal("0.01"),
        filled_qty=Decimal("0"),
        remaining_qty=Decimal("0.01"),
        product_type=product_type,  # type: ignore[arg-type]
        margin_mode=margin_mode,  # type: ignore[arg-type]
    )


def _audit_record(*, decision_id: str, decision_context_ref: str) -> DecisionAuditRecord:
    return DecisionAuditRecord(
        decision_id=decision_id,
        decision_context_ref=decision_context_ref,
    )


class TestEventStoreCountByTopicScoped(unittest.TestCase):
    def test_empty_store_returns_zero(self) -> None:
        store = InMemoryEventStore()
        scope = _spot_scope()
        self.assertEqual(store.count_by_topic_scoped("strategy.decision_context", scope=scope), 0)

    def test_count_equals_len_of_by_topic_scoped(self) -> None:
        store = InMemoryEventStore()
        scope = _spot_scope()
        for _ in range(5):
            store.append(_event(
                topic="strategy.decision_context",
                symbol="BTC-USDT",
                product_type="spot",
                margin_mode="cash",
            ))
        count = store.count_by_topic_scoped("strategy.decision_context", scope=scope)
        listed = list(store.by_topic_scoped("strategy.decision_context", scope=scope))
        self.assertEqual(count, len(listed))
        self.assertEqual(count, 5)

    def test_count_filters_topic(self) -> None:
        store = InMemoryEventStore()
        scope = _spot_scope()
        store.append(_event(
            topic="strategy.decision_context",
            symbol="BTC-USDT",
            product_type="spot",
            margin_mode="cash",
        ))
        store.append(_event(
            topic="execution.order_intents",
            symbol="BTC-USDT",
            product_type="spot",
            margin_mode="cash",
        ))
        self.assertEqual(store.count_by_topic_scoped("strategy.decision_context", scope=scope), 1)
        self.assertEqual(store.count_by_topic_scoped("execution.order_intents", scope=scope), 1)
        self.assertEqual(store.count_by_topic_scoped("unknown.topic", scope=scope), 0)

    def test_count_filters_scope(self) -> None:
        store = InMemoryEventStore()
        # 一条 spot 事件 + 一条 derivatives 事件
        store.append(_event(
            topic="strategy.decision_context",
            symbol="BTC-USDT",
            product_type="spot",
            margin_mode="cash",
        ))
        store.append(_event(
            topic="strategy.decision_context",
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
        ))
        self.assertEqual(
            store.count_by_topic_scoped("strategy.decision_context", scope=_spot_scope()),
            1,
        )
        self.assertEqual(
            store.count_by_topic_scoped("strategy.decision_context", scope=_derivatives_scope()),
            1,
        )


class TestReconciliationRepoPortfolioSnapshotRefs(unittest.TestCase):
    def test_empty_repo_returns_empty_set(self) -> None:
        repo = InMemoryReconciliationRepository()
        self.assertEqual(
            repo.portfolio_snapshot_refs_for_scope(scope=_spot_scope()),
            set(),
        )

    def test_returns_dedup_set_of_refs(self) -> None:
        repo = InMemoryReconciliationRepository()
        repo.save_report(_reconciliation_report(
            reconciliation_id="recon_1",
            product_type="spot",
            margin_mode="cash",
            portfolio_snapshot_ref="snap_1",
        ))
        repo.save_report(_reconciliation_report(
            reconciliation_id="recon_2",
            product_type="spot",
            margin_mode="cash",
            portfolio_snapshot_ref="snap_2",
        ))
        repo.save_report(_reconciliation_report(
            reconciliation_id="recon_3",
            product_type="spot",
            margin_mode="cash",
            portfolio_snapshot_ref="snap_1",  # dup
        ))
        refs = repo.portfolio_snapshot_refs_for_scope(scope=_spot_scope())
        self.assertEqual(refs, {"snap_1", "snap_2"})

    def test_filters_out_null_refs(self) -> None:
        repo = InMemoryReconciliationRepository()
        repo.save_report(_reconciliation_report(
            reconciliation_id="recon_1",
            product_type="spot",
            margin_mode="cash",
            portfolio_snapshot_ref="snap_1",
        ))
        repo.save_report(_reconciliation_report(
            reconciliation_id="recon_2",
            product_type="spot",
            margin_mode="cash",
            portfolio_snapshot_ref=None,  # 业务上的"未参与对账"报告
        ))
        refs = repo.portfolio_snapshot_refs_for_scope(scope=_spot_scope())
        self.assertEqual(refs, {"snap_1"})

    def test_filters_by_scope(self) -> None:
        repo = InMemoryReconciliationRepository()
        repo.save_report(_reconciliation_report(
            reconciliation_id="recon_spot",
            product_type="spot",
            margin_mode="cash",
            portfolio_snapshot_ref="snap_spot",
        ))
        repo.save_report(_reconciliation_report(
            reconciliation_id="recon_deriv",
            product_type="derivatives",
            margin_mode="cross",
            portfolio_snapshot_ref="snap_deriv",
        ))
        self.assertEqual(
            repo.portfolio_snapshot_refs_for_scope(scope=_spot_scope()),
            {"snap_spot"},
        )
        self.assertEqual(
            repo.portfolio_snapshot_refs_for_scope(scope=_derivatives_scope()),
            {"snap_deriv"},
        )

    def test_result_equals_legacy_comprehension(self) -> None:
        """与老路径 ``{r.portfolio_snapshot_ref for r in history_for_scope}`` 等价。

        ``_build_metrics`` 迁移前用的是这个 set-comp，迁移后用新 API。两条路径
        必须完全等价，否则 dashboard 的 ``snapshot_without_reconciliation_count``
        会静默飘移。
        """
        repo = InMemoryReconciliationRepository()
        for i, ref in enumerate(["r1", "r2", "r1", None, "r3"], start=1):
            repo.save_report(_reconciliation_report(
                reconciliation_id=f"recon_{i}",
                product_type="spot",
                margin_mode="cash",
                portfolio_snapshot_ref=ref,
            ))
        new_api = repo.portfolio_snapshot_refs_for_scope(scope=_spot_scope())
        legacy_comprehension = {
            r.portfolio_snapshot_ref
            for r in repo.history_for_scope(scope=_spot_scope())
            if r.portfolio_snapshot_ref
        }
        self.assertEqual(new_api, legacy_comprehension)

    def test_limit_returns_refs_from_latest_reports_only(self) -> None:
        repo = InMemoryReconciliationRepository()
        for i, ref in enumerate(["old_1", "old_2", "new_1", "new_2"], start=1):
            repo.save_report(_reconciliation_report(
                reconciliation_id=f"recon_{i}",
                product_type="spot",
                margin_mode="cash",
                portfolio_snapshot_ref=ref,
            ))

        refs = repo.portfolio_snapshot_refs_for_scope(scope=_spot_scope(), limit=2)

        self.assertEqual(refs, {"new_1", "new_2"})


class TestPostgresScopedLimitSemantics(unittest.TestCase):
    def test_portfolio_history_for_scope_limit_returns_latest_rows_in_chronological_order(self) -> None:
        repo = PostgresPortfolioRepository(_session_factory())
        base_ts = datetime(2026, 5, 7, tzinfo=timezone.utc)
        for index in range(4):
            repo.save_snapshot(_portfolio_snapshot(
                decision_id=f"decision_{index + 1}",
                snapshot_ts=base_ts + timedelta(minutes=index),
            ))

        rows = repo.history_for_scope(scope=_spot_scope(), limit=2)

        self.assertEqual([row.decision_id for row in rows], ["decision_3", "decision_4"])

    def test_reconciliation_history_for_scope_limit_returns_latest_rows_in_chronological_order(self) -> None:
        repo = PostgresReconciliationRepository(_session_factory())
        base_ts = datetime(2026, 5, 7, tzinfo=timezone.utc)
        for index in range(4):
            repo.save_report(_reconciliation_report(
                reconciliation_id=f"recon_{index + 1}",
                product_type="spot",
                margin_mode="cash",
                portfolio_snapshot_ref=f"snap_{index + 1}",
                as_of_ts=base_ts + timedelta(minutes=index),
            ))

        rows = repo.history_for_scope(scope=_spot_scope(), limit=2)

        self.assertEqual([row.reconciliation_id for row in rows], ["recon_3", "recon_4"])

    def test_legacy_execution_fills_for_scope_limit_returns_latest_rows_in_chronological_order(self) -> None:
        repo = PostgresExecutionRepository(_session_factory())
        base_ts = datetime(2026, 5, 7, tzinfo=timezone.utc)
        for index in range(4):
            repo.save_fill(_fill_event(
                fill_id=f"fill_{index + 1}",
                fill_ts=base_ts + timedelta(minutes=index),
            ))

        rows = repo.fills_for_scope(scope=_spot_scope(), limit=2)

        self.assertEqual([row.fill_id for row in rows], ["fill_3", "fill_4"])

    def test_legacy_execution_order_states_for_scope_limit_uses_update_or_created_time(self) -> None:
        repo = PostgresExecutionRepository(_session_factory())
        base_ts = datetime(2026, 5, 7, tzinfo=timezone.utc)
        repo.save_order_state(_order_state(client_order_id="order_1", created_at=base_ts))
        repo.save_order_state(_order_state(client_order_id="order_2", created_at=base_ts + timedelta(minutes=1)))
        repo.save_order_state(_order_state(
            client_order_id="order_3",
            created_at=base_ts + timedelta(minutes=2),
            last_update_ts=base_ts + timedelta(minutes=4),
        ))
        repo.save_order_state(_order_state(client_order_id="order_4", created_at=base_ts + timedelta(minutes=3)))

        rows = repo.order_states_for_scope(scope=_spot_scope(), limit=2)

        self.assertEqual([row.client_order_id for row in rows], ["order_4", "order_3"])

    def test_execution_fill_v2_fills_since_limit_returns_latest_rows_in_chronological_order(self) -> None:
        repo = PostgresExecutionFillRepositoryV2(_session_factory())
        base_ts = datetime(2026, 5, 7, tzinfo=timezone.utc)
        for index in range(4):
            fill = _fill_event(
                fill_id=f"fill_v2_{index + 1}",
                fill_ts=base_ts + timedelta(minutes=index),
            )
            repo.save_fill(
                fill=fill,
                order_id=f"order_{index + 1}",
                source="test",
                raw_payload={"fill_id": fill.fill_id},
            )

        rows = repo.fills_since(limit=2)

        self.assertEqual([row["fill_id"] for row in rows], ["fill_v2_3", "fill_v2_4"])


class TestAuditRepoBatchLatestLookup(unittest.TestCase):
    def test_inmemory_get_many_latest_returns_requested_latest_records(self) -> None:
        repo = InMemoryAuditRepository()
        repo.upsert(_audit_record(decision_id="decision_a", decision_context_ref="ctx_a_old"))
        repo.upsert(_audit_record(decision_id="decision_b", decision_context_ref="ctx_b"))
        repo.upsert(_audit_record(decision_id="decision_a", decision_context_ref="ctx_a_new"))

        rows = repo.get_many_latest(["decision_a", "missing", "decision_b"])

        by_id = {row.decision_id: row for row in rows}
        self.assertEqual(set(by_id), {"decision_a", "decision_b"})
        self.assertEqual(by_id["decision_a"].decision_context_ref, "ctx_a_new")

    def test_postgres_get_many_latest_returns_one_latest_revision_per_decision(self) -> None:
        repo = PostgresAuditRepository(_session_factory())
        repo.upsert(_audit_record(decision_id="decision_a", decision_context_ref="ctx_a_old"))
        repo.upsert(_audit_record(decision_id="decision_b", decision_context_ref="ctx_b"))
        repo.upsert(_audit_record(decision_id="decision_a", decision_context_ref="ctx_a_new"))

        rows = repo.get_many_latest(["decision_a", "missing", "decision_b"])

        by_id = {row.decision_id: row for row in rows}
        self.assertEqual(set(by_id), {"decision_a", "decision_b"})
        self.assertEqual(by_id["decision_a"].decision_context_ref, "ctx_a_new")


if __name__ == "__main__":
    unittest.main()
