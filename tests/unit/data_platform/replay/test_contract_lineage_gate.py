from __future__ import annotations

from datetime import datetime, timezone

import pytest

from aats.data_platform.replay.core.replay_runner import load_gold_bars


UTC = timezone.utc
START = datetime(2026, 8, 1, tzinfo=UTC)
END = datetime(2026, 8, 2, tzinfo=UTC)


class _Session:
    def __init__(self, rows: list[tuple] | None = None) -> None:
        self.rows = rows or []
        self.execute_calls = 0

    def execute(self, statement, params):
        self.execute_calls += 1
        return _Rows(self.rows)


class _Rows:
    def __init__(self, rows: list[tuple]) -> None:
        self.rows = rows

    def fetchall(self) -> list[tuple]:
        return self.rows


def test_legacy_derivative_replay_is_blocked_before_querying_unsealed_gold() -> None:
    session = _Session()

    with pytest.raises(
        ValueError,
        match="legacy_derivative_replay_contract_lineage_required",
    ):
        load_gold_bars(
            session,
            symbol="BTC-USDT-SWAP",
            timeframe="1h",
            start_ts=START,
            end_ts=END,
            dataset_version="v1.0",
        )

    assert session.execute_calls == 0


@pytest.mark.parametrize(
    "symbol",
    ("DOGE-USDT", "DOGE-USDT-SWAP", "BTC-USDT-240927"),
)
def test_unknown_instrument_scope_is_blocked_before_gold_query(symbol: str) -> None:
    session = _Session()

    with pytest.raises(
        ValueError,
        match="instrument_scope_unsupported_or_unproven",
    ):
        load_gold_bars(
            session,
            symbol=symbol,
            timeframe="1h",
            start_ts=START,
            end_ts=END,
            dataset_version="v1.0",
        )

    assert session.execute_calls == 0


def test_spot_replay_keeps_existing_read_path() -> None:
    session = _Session(
        [
            (
                "BTC-USDT",
                START,
                "100",
                "101",
                "99",
                "100.5",
                "2",
                "201",
                True,
                None,
                None,
            )
        ]
    )

    bars = load_gold_bars(
        session,
        symbol="BTC-USDT",
        timeframe="1h",
        start_ts=START,
        end_ts=END,
        dataset_version="v1.0",
    )

    assert session.execute_calls == 1
    assert len(bars) == 1
    assert bars[0].symbol == "BTC-USDT"
