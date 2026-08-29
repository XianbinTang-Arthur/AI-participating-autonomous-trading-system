"""Contract tests for the exact derivatives split-runtime NATS topology."""

from __future__ import annotations

import pytest

from aats.bootstrap.config import build_runtime
from aats.bootstrap.settings import AATSSettings
from aats.bus.consumer_ownership import (
    SPLIT_RUNTIME_CONSUMER_TOPICS_BY_ROLE,
)
from aats.bus.memory_bus import InMemoryEventBus


def _derivatives_memory_settings() -> AATSSettings:
    return AATSSettings.model_validate(
        {
            "mode": "paper_live",
            "market_data_backend": "demo",
            "execution_backend": "paper",
            "account_backend": "disabled",
            "account_read_enabled": False,
            "storage_mode": "memory",
            "event_bus_backend": "in_memory",
            "hot_state_backend": "memory",
            "event_persistence_mode": "strict",
            "trading_product_type": "derivatives",
            "margin_mode": "cross",
            "derivatives_position_mode": "hedge",
            "default_symbol": "BTC-USDT-SWAP",
            "allowed_symbols": ("BTC-USDT-SWAP",),
        }
    )


@pytest.mark.parametrize(
    "role",
    ("gateway", "market", "decision", "execution"),
)
@pytest.mark.asyncio
async def test_manifest_exactly_matches_runtime_subscription_wiring(role: str) -> None:
    runtime = await build_runtime(
        _derivatives_memory_settings(),
        process_role=role,
    )
    try:
        assert isinstance(runtime.bus, InMemoryEventBus)
        # Publishing through InMemoryEventBus touches defaultdict entries even
        # when no handler exists.  Only non-empty handler lists are subscriptions.
        observed = frozenset(
            topic for topic, handlers in runtime.bus._subs.items() if handlers
        )
        assert observed == SPLIT_RUNTIME_CONSUMER_TOPICS_BY_ROLE[role]
    finally:
        await runtime.stop_background_tasks()
