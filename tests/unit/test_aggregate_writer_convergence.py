from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from aats.bootstrap.settings import AATSSettings
from aats.events import topics
from aats.schemas.common import utc_now
from aats.schemas.exit_execution import ChildExitOrderRef, ExitExecutionIntent
from aats.schemas.execution import OrderIntent, OrderObligation
from aats.services.execution_engine.exit_execution_writer import ExitExecutionWriter
from aats.services.execution_engine.obligations import ExecutionObligationService
from aats.services.execution_engine.obligation_cache import OBLIGATION_INDEX_KEY
from aats.services.execution_engine.obligation_writer import ObligationWriterRequiredError
from aats.services.operator.query_service import OperatorQueryService
from aats.storage.exit_execution_repo import InMemoryExitExecutionRepository
from aats.storage.obligation_repo_postgres import PostgresExecutionObligationRepository


def _obligation(*, client_order_id: str, status: str = "ACTIVE") -> OrderObligation:
    return OrderObligation(
        client_order_id=client_order_id,
        decision_id=f"decision_{client_order_id}",
        intent_id=f"intent_{client_order_id}",
        symbol="BTC-USDT-SWAP",
        side="buy",
        reserve_currency="USDT",
        reserved_amount=Decimal("10"),
        status=status,
        product_type="derivatives",
        margin_mode="cross",
        last_update_ts=utc_now(),
    )


class _RecordingObligationCache:
    def __init__(self) -> None:
        self._latest = {"stale_order": _obligation(client_order_id="stale_order")}
        self.published: list[OrderObligation] = []
        self.replaced_source_component: str | None = None

    async def publish(self, obligation: OrderObligation) -> None:
        self._latest[obligation.client_order_id] = obligation
        self.published.append(obligation)

    async def replace_all_from_source(
        self,
        obligations: list[OrderObligation],
        *,
        source_component: str,
    ) -> dict[str, int]:
        replacement = {obligation.client_order_id: obligation for obligation in obligations}
        removed = set(self._latest) - set(replacement)
        self._latest = replacement
        self.replaced_source_component = source_component
        return {
            "cached_count": len(replacement),
            "active_count": sum(
                1
                for obligation in replacement.values()
                if obligation.status in {"ACTIVE", "PARTIALLY_CONSUMED"}
            ),
            "removed_count": len(removed),
        }


class _FailingReplaceObligationCache(_RecordingObligationCache):
    async def replace_all_from_source(
        self,
        obligations: list[OrderObligation],
        *,
        source_component: str,
    ) -> dict[str, int]:
        _ = (obligations, source_component)
        raise RuntimeError("redis_index_write_failed")


class _RecordingHotStateStore:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def get(self, key: str):
        assert key == OBLIGATION_INDEX_KEY
        return {"all_coids": ["stale_order"]}

    async def delete(self, key: str) -> None:
        self.deleted.append(key)


class _ObligationRepo:
    def __init__(self, obligations: list[OrderObligation]) -> None:
        self._obligations = obligations

    def all_obligations(self) -> list[OrderObligation]:
        return list(self._obligations)


@pytest.mark.asyncio
async def test_clear_obligation_cache_rebuilds_active_state_from_db() -> None:
    active = _obligation(client_order_id="active_order")
    cache = _RecordingObligationCache()
    store = _RecordingHotStateStore()
    service = OperatorQueryService.__new__(OperatorQueryService)
    service.runtime = SimpleNamespace(
        obligation_hot_state_cache=cache,
        hot_state_store=store,
        obligation_repo=_ObligationRepo([active]),
    )
    service.recovery_view = lambda: {"recovery_state": "running"}
    service._append_event = lambda **kwargs: SimpleNamespace(  # noqa: E731
        event_id="evt_clear_obligation_cache",
        topic=kwargs["topic"],
    )

    payload = await service.clear_obligation_cache(
        reason="test_rebuild",
        actor_role="admin",
    )

    assert store.deleted == []
    assert list(cache._latest) == ["active_order"]
    assert cache.replaced_source_component == "operator_clear_obligation_cache"
    assert payload["_topic"] == topics.OPERATOR_ACTIONS
    assert payload["details"]["rebuilt_obligation_count"] == 1
    assert payload["details"]["active_rebuilt_obligation_count"] == 1
    assert payload["details"]["removed_local_obligation_count"] == 1
    assert payload["details"]["rebuild_failed_count"] == 0


@pytest.mark.asyncio
async def test_clear_obligation_cache_failure_preserves_existing_authoritative_cache() -> None:
    active = _obligation(client_order_id="active_order")
    cache = _FailingReplaceObligationCache()
    service = OperatorQueryService.__new__(OperatorQueryService)
    recorded_actions = []
    service.runtime = SimpleNamespace(
        obligation_hot_state_cache=cache,
        hot_state_store=_RecordingHotStateStore(),
        obligation_repo=_ObligationRepo([active]),
    )
    service.recovery_view = lambda: {"recovery_state": "running"}

    def _append_event(**kwargs):
        recorded_actions.append(kwargs["payload_model"])
        return SimpleNamespace(event_id="evt_clear_obligation_cache_failed", topic=kwargs["topic"])

    service._append_event = _append_event

    with pytest.raises(ValueError, match="clear_obligation_cache_rebuild_failed"):
        await service.clear_obligation_cache(
            reason="test_rebuild_failure",
            actor_role="admin",
        )

    assert list(cache._latest) == ["stale_order"]
    assert recorded_actions[-1].status == "failed"
    assert recorded_actions[-1].details["cleared_local"] is False
    assert recorded_actions[-1].details["cleared_redis"] is False
    assert recorded_actions[-1].details["rebuild_failed_count"] == 1


def test_postgres_obligation_direct_service_write_requires_writer() -> None:
    settings = AATSSettings.model_validate(
        {
            "storage_mode": "postgres",
            "market_data_backend": "demo",
            "execution_backend": "paper",
            "account_backend": "disabled",
            "account_read_enabled": False,
        }
    )
    service = ExecutionObligationService(
        settings=settings,
        obligation_repo=PostgresExecutionObligationRepository(session_factory=None),  # type: ignore[arg-type]
    )

    with pytest.raises(ObligationWriterRequiredError):
        service.persist_previewed_obligation(_obligation(client_order_id="postgres_direct"))


def test_exit_execution_writer_saves_child_and_recomputes_parent_together() -> None:
    repo = InMemoryExitExecutionRepository()
    writer = ExitExecutionWriter(repo)
    parent = ExitExecutionIntent(
        parent_intent_id="parent_exit_writer",
        execution_chain_id="chain_exit_writer",
        symbol="BTC-USDT-SWAP",
        side="sell",
        intent_kind="close",
        target_exit_quantity=Decimal("0.01"),
    )
    repo.save_exit_execution_intent(parent)
    child_ref = ChildExitOrderRef(
        parent_intent_id=parent.parent_intent_id,
        child_order_id="child_exit_writer",
        client_order_id="child_exit_writer",
        execution_chain_id=parent.execution_chain_id,
        symbol=parent.symbol,
        planned_quantity=Decimal("0.01"),
        child_status="SUBMITTED",
        aggregate_category="WORKING",
    )

    _saved_child, saved_parent = writer.save_child_ref_and_recompute_parent(
        parent_intent=parent,
        child_ref=child_ref,
        recompute_parent=lambda parent_intent, child_refs: parent_intent.model_copy(
            update={
                "child_order_ids": [ref.client_order_id for ref in child_refs],
                "aggregate_version": int(parent_intent.aggregate_version) + 1,
            }
        ),
        source_component="test",
        reason_code="child_ref_recompute",
    )

    assert saved_parent.child_order_ids == ["child_exit_writer"]
    assert saved_parent.aggregate_version == 1
    assert repo.child_refs_for_parent(parent_intent_id=parent.parent_intent_id) == [child_ref]


def test_exit_execution_writer_preserves_sticky_cancel_on_stale_parent_save() -> None:
    repo = InMemoryExitExecutionRepository()
    writer = ExitExecutionWriter(repo)
    current = ExitExecutionIntent(
        parent_intent_id="parent_exit_cancel_merge",
        execution_chain_id="chain_exit_cancel_merge",
        symbol="BTC-USDT-SWAP",
        side="sell",
        intent_kind="close",
        target_exit_quantity=Decimal("0.01"),
        aggregate_status="CANCEL_PENDING",
        cancel_requested=True,
        cancel_requested_ts=utc_now(),
        aggregate_version=5,
    )
    repo.save_exit_execution_intent(current)
    stale_refresh = current.model_copy(
        update={
            "aggregate_status": "WORKING",
            "cancel_requested": False,
            "cancel_requested_ts": None,
            "aggregate_version": 4,
        }
    )

    saved = writer.save_exit_execution_intent(
        stale_refresh,
        source_component="test",
        reason_code="stale_refresh",
    )

    assert saved.cancel_requested is True
    assert saved.cancel_requested_ts == current.cancel_requested_ts
    assert saved.aggregate_status == "CANCEL_PENDING"
    assert saved.aggregate_version == 6


@pytest.mark.asyncio
async def test_postgres_obligation_direct_reserve_requires_writer() -> None:
    settings = AATSSettings.model_validate(
        {
            "storage_mode": "postgres",
            "market_data_backend": "demo",
            "execution_backend": "paper",
            "account_backend": "disabled",
            "account_read_enabled": False,
        }
    )
    service = ExecutionObligationService(
        settings=settings,
        obligation_repo=PostgresExecutionObligationRepository(session_factory=None),  # type: ignore[arg-type]
    )

    async def _fake_build_reservation_for_intent(*, intent, client_order_id):
        _ = (intent, client_order_id)
        return _obligation(client_order_id="postgres_reserve_direct"), Decimal("100")

    service._build_reservation_for_intent = _fake_build_reservation_for_intent  # type: ignore[method-assign]

    with pytest.raises(ObligationWriterRequiredError):
        await service.reserve_for_intent(
            intent=OrderIntent(
                intent_id="intent_postgres_reserve_direct",
                decision_id="decision_postgres_reserve_direct",
                symbol="BTC-USDT-SWAP",
                side="buy",
                quantity=Decimal("0.001"),
                execution_style="exchange",
                order_type="market",
                urgency="medium",
                time_in_force="IOC",
                idempotency_key="postgres_reserve_direct",
            ),
            client_order_id="postgres_reserve_direct",
        )


def test_services_keep_aggregate_writes_behind_writer_services() -> None:
    allowed_by_needle = {
        ".save_obligation(": {
            Path("aats/services/execution_engine/obligation_writer.py"),
            Path("aats/storage/obligation_repo.py"),
        },
        ".reserve_obligation_transactional(": {
            Path("aats/services/execution_engine/obligation_writer.py"),
        },
        "fill_outcome_repo.save_outcome(": {
            Path("aats/services/portfolio_service/fill_projection_writer.py"),
        },
        "exit_execution_repo.save_exit_execution_intent(": {
            Path("aats/services/execution_engine/exit_execution_writer.py"),
        },
        "exit_execution_repo.save_child_exit_order_ref(": {
            Path("aats/services/execution_engine/exit_execution_writer.py"),
        },
    }
    violations: list[str] = []
    for path in Path("aats").rglob("*.py"):
        normalized = Path(path.as_posix())
        text = path.read_text(encoding="utf-8")
        for needle, allowed in allowed_by_needle.items():
            if normalized in allowed:
                continue
            if needle in text:
                violations.append(f"{path}:{needle}")

    assert not violations
