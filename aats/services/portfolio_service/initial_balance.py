from __future__ import annotations

from decimal import Decimal
from typing import Any


def exchange_account_balance_required(settings: Any) -> bool:
    return (
        str(getattr(settings, "account_backend", "") or "").lower() == "okx"
        and bool(getattr(settings, "account_read_enabled", False))
    )


def effective_portfolio_initial_usdt_balance(
    settings: Any,
    *,
    exchange_coupled: bool | None = None,
) -> Decimal:
    if exchange_coupled is None:
        exchange_coupled = bool(getattr(settings, "bootstrap_portfolio_from_exchange", False))
    if exchange_account_balance_required(settings) and exchange_coupled:
        return Decimal("0")
    return Decimal(str(getattr(settings, "initial_usdt_balance", 0) or 0))
