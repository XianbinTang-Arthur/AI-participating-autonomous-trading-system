from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from aats.data_platform.gold import replay_bar_builder


UTC = timezone.utc
START = datetime(2026, 5, 1, tzinfo=UTC)


class _Result:
    def __init__(self, *, rows: list[tuple] | None = None, row: tuple | None = None) -> None:
        self._rows = rows or []
        self._row = row

    def fetchall(self) -> list[tuple]:
        return self._rows

    def fetchone(self) -> tuple | None:
        return self._row


class _GoldBuildSession:
    def __init__(
        self,
        *,
        candles: list[tuple],
        pre_funding: tuple | None,
        window_funding: list[tuple],
    ) -> None:
        self.candles = candles
        self.pre_funding = pre_funding
        self.window_funding = window_funding
        self.inserted_batches: list[list[dict]] = []

    def execute(self, statement, params=None):  # noqa: ANN001, ANN201 - SQLAlchemy test double
        sql = str(statement)
        if "FROM silver.market_swap_candles_1h" in sql:
            return _Result(rows=self.candles)
        if "FROM silver.market_swap_funding" in sql and "ts < :start" in sql:
            return _Result(row=self.pre_funding)
        if "FROM silver.market_swap_funding" in sql and "ts >= :start" in sql:
            return _Result(rows=self.window_funding)
        if "INSERT INTO gold.market_swap_replay_bars_1h" in sql:
            self.inserted_batches.append(list(params or []))
            return SimpleNamespace(rowcount=len(params or []))
        return _Result()


def _patch_run_registry(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(replay_bar_builder, "create_ingest_run", lambda *args, **kwargs: "gold-run-1")
    monkeypatch.setattr(replay_bar_builder, "create_run_item", lambda *args, **kwargs: "item-1")
    monkeypatch.setattr(replay_bar_builder, "finish_run_item", lambda *args, **kwargs: None)
    monkeypatch.setattr(replay_bar_builder, "finish_ingest_run", lambda *args, **kwargs: None)


def _candle(ts: datetime, close: str) -> tuple:
    return (
        "BTC-USDT-SWAP",
        ts,
        Decimal("100"),
        Decimal("101"),
        Decimal("99"),
        Decimal(close),
        Decimal("10"),
        Decimal("1000"),
        True,
    )


def test_gold_builder_propagates_silver_funding_dataset_version(monkeypatch) -> None:  # noqa: ANN001
    _patch_run_registry(monkeypatch)
    session = _GoldBuildSession(
        candles=[
            _candle(START + timedelta(hours=1), "100.5"),
            _candle(START + timedelta(hours=2), "101.5"),
        ],
        pre_funding=(START, Decimal("0.0001"), "funding_v1"),
        window_funding=[],
    )

    replay_bar_builder.build_gold_replay_bars(
        session,  # type: ignore[arg-type]
        symbol="BTC-USDT-SWAP",
        timeframe="1h",
        window_start=START,
        window_end=START + timedelta(hours=2),
    )

    inserted = session.inserted_batches[0]
    assert [row["aligned_funding_rate"] for row in inserted] == [Decimal("0.0001"), Decimal("0.0001")]
    assert [row["source_funding_dataset_version"] for row in inserted] == ["funding_v1", "funding_v1"]


def test_gold_builder_fills_unique_funding_dataset_version_when_bar_has_no_rate(monkeypatch) -> None:  # noqa: ANN001
    _patch_run_registry(monkeypatch)
    session = _GoldBuildSession(
        candles=[
            _candle(START, "100.5"),
            _candle(START + timedelta(hours=1), "101.5"),
        ],
        pre_funding=None,
        window_funding=[(START + timedelta(hours=1), Decimal("0.0002"), "funding_v1")],
    )

    replay_bar_builder.build_gold_replay_bars(
        session,  # type: ignore[arg-type]
        symbol="BTC-USDT-SWAP",
        timeframe="1h",
        window_start=START,
        window_end=START + timedelta(hours=2),
    )

    inserted = session.inserted_batches[0]
    assert inserted[0]["aligned_funding_rate"] is None
    assert inserted[0]["source_funding_dataset_version"] == "funding_v1"
    assert inserted[1]["aligned_funding_rate"] == Decimal("0.0002")
    assert inserted[1]["source_funding_dataset_version"] == "funding_v1"
