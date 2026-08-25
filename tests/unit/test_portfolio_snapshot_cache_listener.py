"""Stage 6 Slice 6.3 hot-fix 单元测试：portfolio_repo → snapshot_cache listener。

设计文档
========
docs/task/stage_6_slice_6_3_cache_listener_fix_design.md §D7

覆盖范围
========
1. listener 未 attach：save_snapshot 正常写 repo，cache 仍为空
2. listener attached：InMemoryPortfolioRepository.save_snapshot → cache.get_sync 立即命中
3. listener attached：PostgresPortfolioRepository.save_snapshot → 同上（走 SQLite in-mem 作替身）
4. idempotent：同 ts 重复 save_snapshot 只更新一次（_apply_locally noop 规则）
5. listener 抛异常：save_snapshot 仍完成、repo.history() 有新 entry
6. 多 scope 隔离：不同 product_type/margin_mode 的 snapshot 互不污染
"""
from __future__ import annotations

import asyncio
import logging
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from aats.bus.memory_bus import InMemoryEventBus
from aats.schemas.portfolio import PortfolioSnapshot, Position
from aats.services.portfolio_service.snapshot_cache import (
    PORTFOLIO_SNAPSHOT_KEY_LATEST,
    PortfolioSnapshotCache,
)
from aats.services.runtime_scope import RuntimeStateScope
from aats.storage.hot_state_store import InMemoryHotStateStore, make_key
from aats.storage.portfolio_repo import InMemoryPortfolioRepository
from aats.storage.portfolio_repo_postgres import PostgresPortfolioRepository
from aats.storage.sqlalchemy_models import Base


def _make_logger() -> logging.Logger:
    logger = logging.getLogger("test.portfolio_snapshot_cache_listener")
    logger.setLevel(logging.DEBUG)
    return logger


def _make_scope(
    *,
    product_type: str = "spot",
    margin_mode: str = "cash",
    default_symbol: str = "BTC-USDT",
) -> RuntimeStateScope:
    return RuntimeStateScope(
        product_type=product_type,  # type: ignore[arg-type]
        margin_mode=margin_mode,  # type: ignore[arg-type]
        allowed_symbols=(default_symbol,),
        default_symbol=default_symbol,
    )


def _make_snapshot(
    *,
    snapshot_ts: datetime | None = None,
    product_type: str = "spot",
    margin_mode: str = "cash",
    decision_id: str | None = "decision-listener-001",
    total_equity: str = "100.00",
    positions: list[Position] | None = None,
    snapshot_origin: str = "fill_derived",
) -> PortfolioSnapshot:
    if snapshot_ts is None:
        snapshot_ts = datetime(2026, 4, 8, 12, 0, 0, tzinfo=timezone.utc)
    return PortfolioSnapshot(
        decision_id=decision_id,
        snapshot_origin=snapshot_origin,  # type: ignore[arg-type]
        snapshot_ts=snapshot_ts,
        balances={"USDT": Decimal(total_equity)},
        positions=list(positions or []),
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        total_equity=Decimal(total_equity),
        gross_exposure=Decimal("0"),
        net_exposure=Decimal("0"),
        product_type=product_type,  # type: ignore[arg-type]
        margin_mode=margin_mode,  # type: ignore[arg-type]
    )


def _make_derivatives_position() -> Position:
    return Position(
        symbol="BTC-USDT-SWAP",
        position_key="BTC-USDT-SWAP:long",
        position_qty=Decimal("0.0015"),
        position_notional=Decimal("117.0838"),
        avg_entry_price=Decimal("78055.8667"),
        unrealized_pnl=Decimal("-1.136"),
        product_type="derivatives",  # type: ignore[arg-type]
        exposure_side="long",
        target_leverage=10.91,
        margin_mode="cross",  # type: ignore[arg-type]
        position_mode="long_short_mode",
        pos_side="long",
        instrument_family="BTC-USDT",
        settle_currency="USDT",
    )


def _make_cache() -> PortfolioSnapshotCache:
    return PortfolioSnapshotCache(
        hot_state_store=InMemoryHotStateStore(),
        bus=InMemoryEventBus(),
        process_role="monolith",
        logger=_make_logger(),
    )


def _make_postgres_repo_session_factory(
    owner: unittest.TestCase,
) -> sessionmaker[Session]:
    """用 SQLite in-memory 搭 PostgresPortfolioRepository 的替身。

    PostgresPortfolioRepository 只依赖 sessionmaker + PortfolioSnapshotModel，
    ORM mapping 在 SQLite 上也能跑。
    """
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    owner.addCleanup(engine.dispose)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


# ─────────────────────────────────────────────────────────────────────
# Case 1：listener 未 attach → cache miss
# ─────────────────────────────────────────────────────────────────────


class TestListenerNotAttached(unittest.TestCase):
    def test_inmemory_repo_save_snapshot_without_listener_leaves_cache_empty(self) -> None:
        repo = InMemoryPortfolioRepository()
        cache = _make_cache()
        scope = _make_scope()
        snapshot = _make_snapshot()

        # 不 attach listener，直接 save_snapshot
        repo.save_snapshot(snapshot)

        self.assertEqual(len(repo.history()), 1)
        self.assertIsNone(cache.get_sync(scope))

    def test_postgres_repo_save_snapshot_without_listener_leaves_cache_empty(self) -> None:
        session_factory = _make_postgres_repo_session_factory(self)
        repo = PostgresPortfolioRepository(session_factory)
        cache = _make_cache()
        scope = _make_scope()
        snapshot = _make_snapshot()

        repo.save_snapshot(snapshot)

        self.assertEqual(len(repo.history()), 1)
        self.assertIsNone(cache.get_sync(scope))


# ─────────────────────────────────────────────────────────────────────
# Case 2：InMemoryPortfolioRepository + listener → cache hit
# ─────────────────────────────────────────────────────────────────────


class TestListenerAttachedInMemory(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = InMemoryPortfolioRepository()
        self.cache = _make_cache()
        self.repo.attach_snapshot_listener(self.cache.apply_sync)
        self.scope = _make_scope()

    def test_save_snapshot_updates_cache_synchronously(self) -> None:
        snapshot = _make_snapshot()
        self.repo.save_snapshot(snapshot)

        cached = self.cache.get_sync(self.scope)
        self.assertIsNotNone(cached)
        assert cached is not None  # for mypy
        self.assertEqual(cached.decision_id, snapshot.decision_id)
        self.assertEqual(cached.snapshot_ts, snapshot.snapshot_ts)
        self.assertEqual(cached.total_equity, snapshot.total_equity)
        # repo 也有
        self.assertEqual(len(self.repo.history()), 1)

    def test_save_newer_snapshot_overwrites_cache(self) -> None:
        base_ts = datetime(2026, 4, 8, 12, 0, 0, tzinfo=timezone.utc)
        first = _make_snapshot(
            snapshot_ts=base_ts,
            decision_id="d-first",
            total_equity="100.00",
        )
        second = _make_snapshot(
            snapshot_ts=base_ts + timedelta(seconds=30),
            decision_id="d-second",
            total_equity="200.00",
        )

        self.repo.save_snapshot(first)
        self.repo.save_snapshot(second)

        cached = self.cache.get_sync(self.scope)
        assert cached is not None
        self.assertEqual(cached.decision_id, "d-second")
        self.assertEqual(cached.total_equity, Decimal("200.00"))
        self.assertEqual(len(self.repo.history()), 2)

    def test_save_snapshot_with_older_ts_does_not_regress_cache(self) -> None:
        """D6 的 idempotent 规则：新 ts 更小时 cache 不退化。

        注意：repo.history() 还是会有两条，因为 InMemoryPortfolioRepository
        没有去重，listener 只保证 cache._apply_locally 的 ts 比较 noop。
        """
        base_ts = datetime(2026, 4, 8, 12, 0, 0, tzinfo=timezone.utc)
        newer = _make_snapshot(
            snapshot_ts=base_ts + timedelta(seconds=30),
            decision_id="d-newer",
            total_equity="999.00",
        )
        older = _make_snapshot(
            snapshot_ts=base_ts,
            decision_id="d-older",
            total_equity="111.00",
        )

        self.repo.save_snapshot(newer)
        self.repo.save_snapshot(older)

        cached = self.cache.get_sync(self.scope)
        assert cached is not None
        # cache 应该继续持有 newer（D6 规则）
        self.assertEqual(cached.decision_id, "d-newer")
        self.assertEqual(cached.total_equity, Decimal("999.00"))
        # 但 repo 会有两条，因为 listener 只影响 cache
        self.assertEqual(len(self.repo.history()), 2)

    def test_same_ts_snapshot_is_noop_on_cache(self) -> None:
        """重复 save 同 ts 的 snapshot，cache 走 noop 路径但不抛。"""
        snapshot = _make_snapshot(decision_id="d-fixed")
        self.repo.save_snapshot(snapshot)
        self.repo.save_snapshot(snapshot)

        cached = self.cache.get_sync(self.scope)
        assert cached is not None
        self.assertEqual(cached.decision_id, "d-fixed")
        # repo 有两条，cache 本地 dict 只更新过一次（第二次 noop）
        self.assertEqual(len(self.repo.history()), 2)


# ─────────────────────────────────────────────────────────────────────
# Case 3：PostgresPortfolioRepository + listener → cache hit
# ─────────────────────────────────────────────────────────────────────


class TestListenerAttachedPostgres(unittest.TestCase):
    def setUp(self) -> None:
        self.session_factory = _make_postgres_repo_session_factory(self)
        self.repo = PostgresPortfolioRepository(self.session_factory)
        self.cache = _make_cache()
        self.repo.attach_snapshot_listener(self.cache.apply_sync)
        self.scope = _make_scope()

    def test_save_snapshot_updates_cache_after_commit(self) -> None:
        snapshot = _make_snapshot(decision_id="d-pg-001")
        self.repo.save_snapshot(snapshot)

        cached = self.cache.get_sync(self.scope)
        assert cached is not None
        self.assertEqual(cached.decision_id, "d-pg-001")
        self.assertEqual(cached.total_equity, snapshot.total_equity)

        # repo 也有
        history = self.repo.history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].decision_id, "d-pg-001")

    def test_save_snapshot_in_session_does_not_trigger_listener(self) -> None:
        """D6 决策：save_snapshot_in_session 不通知 listener，outbox publisher
        路径不变。
        """
        snapshot = _make_snapshot(decision_id="d-pg-in-session")
        with self.session_factory() as session:
            self.repo.save_snapshot_in_session(session, snapshot)
            session.commit()

        # repo 有
        self.assertEqual(len(self.repo.history()), 1)
        # cache 没有（因为 save_snapshot_in_session 不走 listener）
        self.assertIsNone(self.cache.get_sync(self.scope))


# ─────────────────────────────────────────────────────────────────────
# Case 4：listener 抛异常不拖垮 save_snapshot
# ─────────────────────────────────────────────────────────────────────


class _BoomListener:
    def __init__(self) -> None:
        self.call_count = 0

    def __call__(self, snapshot: PortfolioSnapshot) -> None:
        self.call_count += 1
        raise RuntimeError("listener_boom")


class TestListenerExceptionIsolated(unittest.TestCase):
    def test_inmemory_repo_survives_listener_exception(self) -> None:
        repo = InMemoryPortfolioRepository()
        boom = _BoomListener()
        repo.attach_snapshot_listener(boom)
        snapshot = _make_snapshot()

        # 不应抛
        repo.save_snapshot(snapshot)

        self.assertEqual(boom.call_count, 1)
        # repo 的写路径完成
        self.assertEqual(len(repo.history()), 1)
        self.assertEqual(repo.history()[0].decision_id, snapshot.decision_id)

    def test_postgres_repo_survives_listener_exception(self) -> None:
        session_factory = _make_postgres_repo_session_factory(self)
        repo = PostgresPortfolioRepository(session_factory)
        boom = _BoomListener()
        repo.attach_snapshot_listener(boom)
        snapshot = _make_snapshot(decision_id="d-pg-boom")

        repo.save_snapshot(snapshot)

        self.assertEqual(boom.call_count, 1)
        # commit 已经成功
        self.assertEqual(len(repo.history()), 1)
        self.assertEqual(repo.history()[0].decision_id, "d-pg-boom")


# ─────────────────────────────────────────────────────────────────────
# Case 5：多 scope 隔离
# ─────────────────────────────────────────────────────────────────────


class TestListenerScopeIsolation(unittest.TestCase):
    def test_different_scopes_do_not_contaminate_each_other(self) -> None:
        repo = InMemoryPortfolioRepository()
        cache = _make_cache()
        repo.attach_snapshot_listener(cache.apply_sync)

        spot_scope = _make_scope(product_type="spot", margin_mode="cash")
        derivatives_scope = _make_scope(
            product_type="derivatives", margin_mode="cross"
        )

        spot_snapshot = _make_snapshot(
            product_type="spot",
            margin_mode="cash",
            decision_id="d-spot",
            total_equity="100.00",
        )
        deriv_snapshot = _make_snapshot(
            product_type="derivatives",
            margin_mode="cross",
            decision_id="d-deriv",
            total_equity="500.00",
        )

        repo.save_snapshot(spot_snapshot)
        repo.save_snapshot(deriv_snapshot)

        spot_cached = cache.get_sync(spot_scope)
        deriv_cached = cache.get_sync(derivatives_scope)

        assert spot_cached is not None
        assert deriv_cached is not None
        self.assertEqual(spot_cached.decision_id, "d-spot")
        self.assertEqual(deriv_cached.decision_id, "d-deriv")

        # 保证没有互相污染
        self.assertEqual(spot_cached.total_equity, Decimal("100.00"))
        self.assertEqual(deriv_cached.total_equity, Decimal("500.00"))


class TestRecoveryDirectSaveCrossProcessSync(unittest.IsolatedAsyncioTestCase):
    async def test_recovery_auto_healed_empty_snapshot_clears_gateway_stale_position(
        self,
    ) -> None:
        repo = InMemoryPortfolioRepository()
        hot_state_store = InMemoryHotStateStore()
        bus = InMemoryEventBus()
        writer_cache = PortfolioSnapshotCache(
            hot_state_store=hot_state_store,
            bus=bus,
            process_role="execution",
            logger=_make_logger(),
        )
        gateway_cache = PortfolioSnapshotCache(
            hot_state_store=hot_state_store,
            bus=bus,
            process_role="gateway",
            logger=_make_logger(),
        )
        scope_fingerprint = "derivatives:cross"
        await writer_cache.bootstrap(scope_fingerprint=scope_fingerprint)
        await gateway_cache.bootstrap(scope_fingerprint=scope_fingerprint)
        repo.attach_snapshot_listener(writer_cache.fire_and_forget_publish)
        scope = _make_scope(
            product_type="derivatives",
            margin_mode="cross",
            default_symbol="BTC-USDT-SWAP",
        )

        base_ts = datetime(2026, 4, 26, 22, 34, 18, tzinfo=timezone.utc)
        stale_position_snapshot = _make_snapshot(
            snapshot_ts=base_ts,
            product_type="derivatives",
            margin_mode="cross",
            decision_id="fill-derived-old-position",
            total_equity="117.0838",
            positions=[_make_derivatives_position()],
            snapshot_origin="fill_derived",
        )
        healed_empty_snapshot = _make_snapshot(
            snapshot_ts=base_ts + timedelta(minutes=2),
            product_type="derivatives",
            margin_mode="cross",
            decision_id="recovery-healed-empty-position",
            total_equity="118.2198",
            positions=[],
            snapshot_origin="recovery_auto_healed",
        )

        repo.save_snapshot(stale_position_snapshot)
        await asyncio.sleep(0.05)
        gateway_stale = gateway_cache.get_sync(scope)
        assert gateway_stale is not None
        self.assertEqual(len(gateway_stale.positions), 1)

        repo.save_snapshot(healed_empty_snapshot)
        await asyncio.sleep(0.05)

        writer_cached = writer_cache.get_sync(scope)
        gateway_cached = gateway_cache.get_sync(scope)
        assert writer_cached is not None
        assert gateway_cached is not None
        self.assertEqual(writer_cached.decision_id, "recovery-healed-empty-position")
        self.assertEqual(gateway_cached.decision_id, "recovery-healed-empty-position")
        self.assertEqual(writer_cached.positions, [])
        self.assertEqual(gateway_cached.positions, [])

        redis_payload = await hot_state_store.get(
            make_key("portfolio", PORTFOLIO_SNAPSHOT_KEY_LATEST, scope_fingerprint)
        )
        self.assertIsInstance(redis_payload, dict)
        assert isinstance(redis_payload, dict)
        self.assertEqual(
            redis_payload["decision_id"], "recovery-healed-empty-position"
        )
        self.assertEqual(redis_payload["positions"], [])


if __name__ == "__main__":
    unittest.main()
