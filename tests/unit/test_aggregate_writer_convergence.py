from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from aats.bootstrap.settings import AATSSettings
from aats.events import topics
from aats.schemas.common import utc_now
from aats.schemas.execution import OrderObligation
from aats.services.execution_engine.obligations import ExecutionObligationService
from aats.services.execution_engine.obligation_cache import OBLIGATION_INDEX_KEY
from aats.services.execution_engine.obligation_writer import ObligationWriterRequiredError
from aats.services.operator.query_service import OperatorQueryService
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

    async def publish(self, obligation: OrderObligation) -> None:
        self._latest[obligation.client_order_id] = obligation
        self.published.append(obligation)


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

    assert store.deleted == [
        OBLIGATION_INDEX_KEY,
        "aats:hot:obligation:by_coid:stale_order",
    ]
    assert list(cache._latest) == ["active_order"]
    assert [item.client_order_id for item in cache.published] == ["active_order"]
    assert payload["_topic"] == topics.OPERATOR_ACTIONS
    assert payload["details"]["rebuilt_obligation_count"] == 1
    assert payload["details"]["rebuild_failed_count"] == 0


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


def test_services_keep_aggregate_writes_behind_writer_services() -> None:
    allowed_by_needle = {
        ".save_obligation(": {
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
    for path in Path("aats/services").rglob("*.py"):
        normalized = Path(path.as_posix())
        text = path.read_text(encoding="utf-8")
        for needle, allowed in allowed_by_needle.items():
            if normalized in allowed:
                continue
            if needle in text:
                violations.append(f"{path}:{needle}")

    assert not violations
