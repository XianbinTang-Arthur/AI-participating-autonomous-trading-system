from __future__ import annotations

from aats.services.execution_control.monitor import Phase1ShadowMonitor
from aats.services.runtime_scope import RuntimeStateScope


class _LegacyExecutionRepo:
    def order_states_for_scope(self, *, scope, statuses=None, limit=None, open_only=False):
        return [object(), object()]

    def fills_for_scope(self, *, scope, since=None, limit=None):
        return [object()]


class _ScopedOrderRepo:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def count_orders_for_scope(
        self,
        *,
        product_type: str,
        margin_mode: str,
        symbols: tuple[str, ...] = (),
        open_only: bool = False,
    ) -> int:
        self.calls.append(
            {
                "product_type": product_type,
                "margin_mode": margin_mode,
                "symbols": symbols,
                "open_only": open_only,
            }
        )
        return 2

    def count_orders(self):
        raise AssertionError("phase1 shadow monitor must not use unscoped order count")


class _ScopedFillRepo:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def count_fills_for_scope(
        self,
        *,
        product_type: str,
        margin_mode: str,
        symbols: tuple[str, ...] = (),
    ) -> int:
        self.calls.append(
            {
                "product_type": product_type,
                "margin_mode": margin_mode,
                "symbols": symbols,
            }
        )
        return 1

    def count_fills(self):
        raise AssertionError("phase1 shadow monitor must not use unscoped fill count")


class _ObligationRepo:
    def all_obligations(self):
        return []


class _ReservationRepo:
    def count_reservations(self):
        return 0


def test_phase1_shadow_monitor_uses_scoped_execution_truth_counts_for_backlog() -> None:
    scope = RuntimeStateScope(
        product_type="derivatives",
        margin_mode="cross",
        allowed_symbols=("BTC-USDT-SWAP",),
        default_symbol="BTC-USDT-SWAP",
    )
    order_repo = _ScopedOrderRepo()
    fill_repo = _ScopedFillRepo()
    monitor = Phase1ShadowMonitor(
        execution_repo=_LegacyExecutionRepo(),
        obligation_repo=_ObligationRepo(),
        state_scope=scope,
        execution_order_repo=order_repo,
        execution_fill_repo=fill_repo,
        reservation_repo=_ReservationRepo(),
    )

    snapshot = monitor.snapshot()

    assert snapshot["lag"]["order_backlog"] == 0
    assert snapshot["lag"]["fill_backlog"] == 0
    assert order_repo.calls == [
        {
            "product_type": "derivatives",
            "margin_mode": "cross",
            "symbols": ("BTC-USDT-SWAP",),
            "open_only": False,
        }
    ]
    assert fill_repo.calls == [
        {
            "product_type": "derivatives",
            "margin_mode": "cross",
            "symbols": ("BTC-USDT-SWAP",),
        }
    ]
