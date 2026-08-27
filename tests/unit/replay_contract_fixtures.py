from __future__ import annotations

from decimal import Decimal

from aats.domain.instrument_contract import InstrumentContract


LINEAR_SWAP_CONTRACT = InstrumentContract(
    symbol="BTC-USDT-SWAP",
    instrument_type="SWAP",
    contract_type="linear",
    base_currency="BTC",
    quote_currency="USDT",
    settle_currency="USDT",
    contract_value=Decimal("0.01"),
    contract_multiplier=Decimal("1"),
    contract_value_currency="BTC",
    lot_size=Decimal("0.01"),
    min_size=Decimal("0.01"),
    tick_size=Decimal("0.1"),
)

INVERSE_SWAP_CONTRACT = InstrumentContract(
    symbol="BTC-USD-SWAP",
    instrument_type="SWAP",
    contract_type="inverse",
    base_currency="BTC",
    quote_currency="USD",
    settle_currency="BTC",
    contract_value=Decimal("100"),
    contract_multiplier=Decimal("1"),
    contract_value_currency="USD",
    lot_size=Decimal("1"),
    min_size=Decimal("1"),
    tick_size=Decimal("0.1"),
)

SPOT_CONTRACT = InstrumentContract(
    symbol="BTC-USDT",
    instrument_type="SPOT",
    contract_type="spot",
    base_currency="BTC",
    quote_currency="USDT",
    settle_currency="USDT",
    contract_value=Decimal("1"),
    contract_multiplier=Decimal("1"),
    contract_value_currency="BTC",
    lot_size=Decimal("0.0001"),
    min_size=Decimal("0.0001"),
    tick_size=Decimal("0.1"),
)


__all__ = [
    "INVERSE_SWAP_CONTRACT",
    "LINEAR_SWAP_CONTRACT",
    "SPOT_CONTRACT",
]
