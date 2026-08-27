from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from scripts.rdp_deep_backfill_api import _parse_api_candle, deep_backfill_one


START = datetime(2026, 5, 16, tzinfo=UTC)
END = datetime(2026, 5, 28, tzinfo=UTC)


@pytest.mark.parametrize("symbol", ("DOGE-USDT-SWAP", "BTC-USDT-FUTURES"))
def test_unproven_candle_scope_fails_before_settings_database_or_network(
    symbol: str,
) -> None:
    with patch(
        "aats.data_platform.config.get_settings",
        side_effect=AssertionError("settings must not load"),
    ), patch(
        "scripts.rdp_deep_backfill_api._fetch_candles_page",
        side_effect=AssertionError("network must not run"),
    ), pytest.raises(
        ValueError,
        match="instrument_scope_unsupported_or_unproven",
    ):
        deep_backfill_one(
            symbol,
            "15m",
            START,
            dry_run=True,
        )


def test_refresh_existing_requires_bounded_aware_window() -> None:
    with patch("aats.data_platform.config.get_settings", return_value=MagicMock()):
        with pytest.raises(ValueError, match="refresh_end is required"):
            deep_backfill_one(
                "BTC-USDT-SWAP",
                "15m",
                START,
                refresh_existing=True,
                dry_run=True,
            )
        with pytest.raises(ValueError, match="after target_start"):
            deep_backfill_one(
                "BTC-USDT-SWAP",
                "15m",
                START,
                refresh_existing=True,
                refresh_end=START,
                dry_run=True,
            )


def test_refresh_existing_fetches_and_filters_exact_window() -> None:
    inside = int(datetime(2026, 5, 27, 23, 45, tzinfo=UTC).timestamp() * 1000)
    boundary_old = int(START.timestamp() * 1000)
    outside_old = int(datetime(2026, 5, 15, 23, 45, tzinfo=UTC).timestamp() * 1000)
    page = [
        [str(inside), "100", "101", "99", "100", "1", "1", "100", "1"],
        [str(boundary_old), "100", "101", "99", "100", "1", "1", "100", "1"],
        [str(outside_old), "100", "101", "99", "100", "1", "1", "100", "1"],
    ]
    session_context = MagicMock()
    session_context.__enter__.return_value = MagicMock()
    session_context.__exit__.return_value = False

    with patch("aats.data_platform.config.get_settings", return_value=MagicMock()), patch(
        "aats.data_platform.db.get_session",
        return_value=session_context,
    ), patch(
        "scripts.rdp_deep_backfill_api._query_existing_range",
        return_value=(START, END),
    ), patch(
        "scripts.rdp_deep_backfill_api._fetch_candles_page",
        return_value=page,
    ):
        result = deep_backfill_one(
            "BTC-USDT-SWAP",
            "15m",
            START,
            refresh_existing=True,
            refresh_end=END,
            dry_run=True,
            rate_limit_sleep=0.0,
        )

    assert result["mode"] == "refresh_existing"
    assert result["rows_fetched"] == 2
    assert result["new_min_ts"] == START.isoformat()
    assert result["new_max_ts"] == datetime(2026, 5, 27, 23, 45, tzinfo=UTC).isoformat()


@pytest.mark.parametrize(
    "row",
    [
        ["1", "100", "99", "98", "100", "1", "1", "1", "1"],
        ["1", "100", "101", "99", "100", "-1", "1", "1", "1"],
        ["1", "100", "101", "99", "100", "1", "1", "1", "unknown"],
        ["1", "NaN", "101", "99", "100", "1", "1", "1", "1"],
    ],
)
def test_parse_api_candle_rejects_financially_invalid_rows(row: list[str]) -> None:
    assert _parse_api_candle(row, "BTC-USDT-SWAP") is None


def test_dry_run_fails_closed_on_invalid_source_row() -> None:
    page = [["bad-ts", "100", "101", "99", "100", "1", "1", "100", "1"]]
    session_context = MagicMock()
    session_context.__enter__.return_value = MagicMock()
    session_context.__exit__.return_value = False

    with patch("aats.data_platform.config.get_settings", return_value=MagicMock()), patch(
        "aats.data_platform.db.get_session",
        return_value=session_context,
    ), patch(
        "scripts.rdp_deep_backfill_api._query_existing_range",
        return_value=(END, END),
    ), patch(
        "scripts.rdp_deep_backfill_api._fetch_candles_page",
        return_value=page,
    ), pytest.raises(RuntimeError, match="candle_backfill_dry_run_failed"):
        deep_backfill_one(
            "BTC-USDT-SWAP",
            "15m",
            START,
            dry_run=True,
            rate_limit_sleep=0.0,
        )


def test_dry_run_fails_closed_when_page_limit_is_reached() -> None:
    stamp = int(datetime(2026, 5, 27, 23, 45, tzinfo=UTC).timestamp() * 1000)
    page = [
        [str(stamp - index), "100", "101", "99", "100", "1", "1", "100", "1"]
        for index in range(100)
    ]
    session_context = MagicMock()
    session_context.__enter__.return_value = MagicMock()
    session_context.__exit__.return_value = False

    with patch("aats.data_platform.config.get_settings", return_value=MagicMock()), patch(
        "aats.data_platform.db.get_session",
        return_value=session_context,
    ), patch(
        "scripts.rdp_deep_backfill_api._query_existing_range",
        return_value=(END, END),
    ), patch(
        "scripts.rdp_deep_backfill_api._fetch_candles_page",
        return_value=page,
    ), patch(
        "scripts.rdp_deep_backfill_api.MAX_PAGES_HARD_LIMIT",
        1,
    ), pytest.raises(RuntimeError, match="candle_backfill_dry_run_failed"):
        deep_backfill_one(
            "BTC-USDT-SWAP",
            "15m",
            START,
            dry_run=True,
            rate_limit_sleep=0.0,
        )
