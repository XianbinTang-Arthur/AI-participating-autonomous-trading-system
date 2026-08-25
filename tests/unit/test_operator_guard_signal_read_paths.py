from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from aats.services.operator.query_service import OperatorQueryService


class _SnapshotProvider:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def snapshot(self) -> dict:
        return dict(self.payload)


def _partial_query_service(runtime: SimpleNamespace) -> OperatorQueryService:
    service = OperatorQueryService.__new__(OperatorQueryService)
    service.runtime = runtime
    service._cached_ttl = lambda _key, _ttl, loader: loader()
    service._scope_cache_fragment = lambda: "test"
    return service


def test_net_short_liquidation_gap_uses_negative_quantity_direction() -> None:
    service = OperatorQueryService.__new__(OperatorQueryService)
    position = SimpleNamespace(
        side="net",
        quantity=Decimal("-0.1"),
        mark_price=Decimal("100"),
        liquidation_price=Decimal("150"),
    )

    assert service._position_liquidation_gap_ratio(position) == Decimal("0.5")


def test_net_long_liquidation_gap_uses_positive_quantity_direction() -> None:
    service = OperatorQueryService.__new__(OperatorQueryService)
    position = SimpleNamespace(
        side="net",
        quantity=Decimal("0.1"),
        mark_price=Decimal("100"),
        liquidation_price=Decimal("60"),
    )

    assert service._position_liquidation_gap_ratio(position) == Decimal("0.4")


def test_gateway_trial_guard_reads_cross_process_cache() -> None:
    expected = {
        "enabled": True,
        "enabled_for_runtime": True,
        "status": "monitoring",
        "summary": "试盘守护正在运行。",
    }
    runtime = SimpleNamespace(
        trial_guard_service=None,
        guard_signal_caches={"trial": _SnapshotProvider(expected)},
    )
    service = _partial_query_service(runtime)

    assert service.trial_guard() == expected


def test_gateway_derivatives_guard_reads_cross_process_cache() -> None:
    expected = {
        "enabled": True,
        "status": "monitoring",
        "only_reduce_required": False,
        "auto_halt_required": False,
    }
    runtime = SimpleNamespace(
        derivatives_live_guard_service=None,
        guard_signal_caches={"derivatives_live": _SnapshotProvider(expected)},
    )
    service = _partial_query_service(runtime)

    assert service.derivatives_live_guard() == expected
