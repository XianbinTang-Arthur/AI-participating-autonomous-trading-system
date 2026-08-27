from __future__ import annotations

import pytest

from aats.data_platform.models import instrument_type_for_symbol
from aats.domain.instrument_scope import (
    INSTRUMENT_SCOPE_UNSUPPORTED_REASON,
    classify_instrument_scope,
)


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("BTC-USDT", "spot"),
        (" eth-usdt ", "spot"),
        ("BTC-USDT-SWAP", "swap"),
        (" eth-usdt-swap ", "swap"),
    ],
)
def test_explicitly_supported_instruments_keep_their_scope(
    symbol: str,
    expected: str,
) -> None:
    assert classify_instrument_scope(symbol) == expected
    assert instrument_type_for_symbol(symbol) == expected


@pytest.mark.parametrize(
    "symbol",
    [
        None,
        "",
        "DOGE-USDT",
        "DOGE-USDT-SWAP",
        "BTC-USDT-240927",
        "BTC-USDT-FUTURES",
    ],
)
def test_unknown_shape_or_suffix_never_proves_instrument_scope(
    symbol: str | None,
) -> None:
    assert classify_instrument_scope(symbol) == "unsupported"
    with pytest.raises(ValueError, match=INSTRUMENT_SCOPE_UNSUPPORTED_REASON):
        instrument_type_for_symbol(symbol)  # type: ignore[arg-type]
