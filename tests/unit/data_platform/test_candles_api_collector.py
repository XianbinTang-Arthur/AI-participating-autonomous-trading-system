from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from aats.data_platform.collectors.rolling.candles_api_collector import (
    collect_candles_incremental,
)


def _api_row(ts_ms: int, *, confirmed: bool) -> list[str]:
    return [
        str(ts_ms),
        "100",
        "101",
        "99",
        "100.5",
        "10",
        "1",
        "1000",
        "1" if confirmed else "0",
    ]


def test_unconfirmed_latest_candle_does_not_advance_checkpoint() -> None:
    session = MagicMock()
    settings = MagicMock()
    settings.okx_rest_url = "https://example.invalid"
    settings.okx_timeout_seconds = 1.0
    settings.okx_rate_limit_sleep = 0.0
    closed_ts = datetime(2026, 8, 25, 20, 0, tzinfo=UTC)
    open_ts = datetime(2026, 8, 25, 20, 15, tzinfo=UTC)
    response = [
        _api_row(int(open_ts.timestamp() * 1000), confirmed=False),
        _api_row(int(closed_ts.timestamp() * 1000), confirmed=True),
    ]

    with patch(
        "aats.data_platform.collectors.rolling.candles_api_collector.get_checkpoint",
        return_value=None,
    ), patch(
        "aats.data_platform.collectors.rolling.candles_api_collector.create_ingest_run",
        return_value="run-1",
    ), patch(
        "aats.data_platform.collectors.rolling.candles_api_collector.create_run_item",
        return_value="item-1",
    ), patch(
        "aats.data_platform.collectors.rolling.candles_api_collector._fetch_candles",
        return_value=response,
    ), patch(
        "aats.data_platform.collectors.rolling.candles_api_collector._write_staging",
        return_value=2,
    ), patch(
        "aats.data_platform.collectors.rolling.candles_api_collector.upsert_checkpoint"
    ) as checkpoint:
        collect_candles_incremental(
            session,
            settings,
            symbol="BTC-USDT-SWAP",
            timeframe="15m",
            max_pages=1,
        )

    assert checkpoint.call_args.kwargs["last_successful_ts"] == closed_ts
    assert checkpoint.call_args.kwargs["next_expected_ts"] == open_ts


def test_checkpoint_overlap_refetches_same_timestamp_for_confirmation_healing() -> None:
    session = MagicMock()
    settings = MagicMock()
    settings.okx_rest_url = "https://example.invalid"
    settings.okx_timeout_seconds = 1.0
    settings.okx_rate_limit_sleep = 0.0
    checkpoint_ts = datetime(2026, 8, 25, 20, 15, tzinfo=UTC)
    response = [_api_row(int(checkpoint_ts.timestamp() * 1000), confirmed=True)]

    with patch(
        "aats.data_platform.collectors.rolling.candles_api_collector.get_checkpoint",
        return_value={"last_successful_ts": checkpoint_ts},
    ), patch(
        "aats.data_platform.collectors.rolling.candles_api_collector.create_ingest_run",
        return_value="run-1",
    ), patch(
        "aats.data_platform.collectors.rolling.candles_api_collector.create_run_item",
        return_value="item-1",
    ), patch(
        "aats.data_platform.collectors.rolling.candles_api_collector._fetch_candles",
        return_value=response,
    ), patch(
        "aats.data_platform.collectors.rolling.candles_api_collector._write_staging",
        return_value=1,
    ) as write_staging, patch(
        "aats.data_platform.collectors.rolling.candles_api_collector.upsert_checkpoint"
    ) as checkpoint:
        collect_candles_incremental(
            session,
            settings,
            symbol="BTC-USDT-SWAP",
            timeframe="15m",
            max_pages=1,
        )

    written_rows = write_staging.call_args.args[2]
    assert [row.ts for row in written_rows] == [checkpoint_ts]
    assert written_rows[0].confirm is True
    assert checkpoint.call_args.kwargs["last_successful_ts"] == checkpoint_ts
