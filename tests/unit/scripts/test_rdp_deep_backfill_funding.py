from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from scripts.rdp_deep_backfill_funding import (
    _parse_funding,
    deep_backfill_funding,
    main,
)


def test_dry_run_uses_observed_settlement_times_without_fixed_cadence() -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    end = datetime(2026, 8, 2, tzinfo=UTC)
    stamps = [
        int(datetime(2026, 8, 1, 20, tzinfo=UTC).timestamp() * 1000),
        int(datetime(2026, 8, 1, 12, tzinfo=UTC).timestamp() * 1000),
        int(start.timestamp() * 1000),
    ]
    page = [
        {
            "instId": "BTC-USDT-SWAP",
            "fundingTime": str(stamp),
            "fundingRate": "0.0001",
        }
        for stamp in stamps
    ]
    session_context = MagicMock()
    session_context.__enter__.return_value = MagicMock()
    session_context.__exit__.return_value = False

    with patch("aats.data_platform.config.get_settings", return_value=MagicMock()), patch(
        "aats.data_platform.db.get_session",
        return_value=session_context,
    ), patch(
        "scripts.rdp_deep_backfill_funding._query_existing_range",
        return_value=(end, end),
    ), patch(
        "scripts.rdp_deep_backfill_funding._fetch_funding_page",
        return_value=page,
    ):
        result = deep_backfill_funding(
            "BTC-USDT-SWAP",
            start,
            dry_run=True,
            rate_limit_sleep=0.0,
        )

    assert result["rows_fetched"] == 3
    assert result["coverage_ratio"] == 1.0
    assert result["gaps"] == []
    assert result["observed_settlement_intervals_seconds"] == [28_800, 43_200]


def test_dry_run_reports_requested_window_shortfall() -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    end = datetime(2026, 8, 2, tzinfo=UTC)
    first = datetime(2026, 8, 1, 12, tzinfo=UTC)
    page = [
        {
            "instId": "BTC-USDT-SWAP",
            "fundingTime": str(int(first.timestamp() * 1000)),
            "fundingRate": "0.0001",
        }
    ]
    session_context = MagicMock()
    session_context.__enter__.return_value = MagicMock()
    session_context.__exit__.return_value = False

    with patch("aats.data_platform.config.get_settings", return_value=MagicMock()), patch(
        "aats.data_platform.db.get_session",
        return_value=session_context,
    ), patch(
        "scripts.rdp_deep_backfill_funding._query_existing_range",
        return_value=(end, end),
    ), patch(
        "scripts.rdp_deep_backfill_funding._fetch_funding_page",
        return_value=page,
    ):
        result = deep_backfill_funding(
            "BTC-USDT-SWAP",
            start,
            dry_run=True,
            rate_limit_sleep=0.0,
        )

    assert result["coverage_ratio"] == 0.0
    assert result["gaps"] == [
        {
            "gap_start": start.isoformat(),
            "gap_end": first.isoformat(),
            "reason": "official_funding_history_coverage_shortfall",
        }
    ]


def test_dry_run_retries_empty_page_then_reports_api_exhaustion() -> None:
    start = datetime(2026, 1, 14, tzinfo=UTC)
    end = datetime(2026, 1, 15, tzinfo=UTC)
    session_context = MagicMock()
    session_context.__enter__.return_value = MagicMock()
    session_context.__exit__.return_value = False

    with patch("aats.data_platform.config.get_settings", return_value=MagicMock()), patch(
        "aats.data_platform.db.get_session",
        return_value=session_context,
    ), patch(
        "scripts.rdp_deep_backfill_funding._query_existing_range",
        return_value=(end, end),
    ), patch(
        "scripts.rdp_deep_backfill_funding._fetch_funding_page",
        return_value=[],
    ) as fetch:
        result = deep_backfill_funding(
            "BTC-USDT-SWAP",
            start,
            dry_run=True,
            rate_limit_sleep=0.0,
        )

    assert fetch.call_count == 3
    assert result["pages_fetched"] == 3
    assert result["api_exhausted"] is True
    assert result["coverage_ratio"] == 0.0


def test_refresh_existing_uses_explicit_half_open_window() -> None:
    start = datetime(2026, 8, 25, tzinfo=UTC)
    end = datetime(2026, 8, 26, tzinfo=UTC)
    page = [
        {
            "instId": "BTC-USDT-SWAP",
            "fundingTime": str(int(stamp.timestamp() * 1000)),
            "fundingRate": "0.0001",
        }
        for stamp in (
            datetime(2026, 8, 25, 16, tzinfo=UTC),
            datetime(2026, 8, 25, 8, tzinfo=UTC),
            start,
        )
    ]
    session_context = MagicMock()
    session_context.__enter__.return_value = MagicMock()
    session_context.__exit__.return_value = False

    with patch("aats.data_platform.config.get_settings", return_value=MagicMock()), patch(
        "aats.data_platform.db.get_session",
        return_value=session_context,
    ), patch(
        "scripts.rdp_deep_backfill_funding._query_existing_range",
        return_value=(datetime(2026, 1, 1, tzinfo=UTC), end),
    ), patch(
        "scripts.rdp_deep_backfill_funding._fetch_funding_page",
        return_value=page,
    ) as fetch:
        result = deep_backfill_funding(
            "BTC-USDT-SWAP",
            start,
            dry_run=True,
            rate_limit_sleep=0.0,
            refresh_existing=True,
            refresh_end=end,
        )

    assert fetch.call_args.kwargs["after_ms"] == int(end.timestamp() * 1000)
    assert result["rows_fetched"] == 3
    assert result["coverage_ratio"] == 1.0
    assert result["mode"] == "refresh_existing"


def test_parse_funding_rejects_wrong_instrument_and_non_finite_rate() -> None:
    base = {
        "instId": "ETH-USDT-SWAP",
        "fundingTime": "1",
        "fundingRate": "0.0001",
    }
    assert _parse_funding(base, "BTC-USDT-SWAP") is None
    assert _parse_funding(
        {**base, "instId": "BTC-USDT-SWAP", "fundingRate": "NaN"},
        "BTC-USDT-SWAP",
    ) is None


def test_dry_run_fails_closed_on_invalid_funding_row() -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    end = datetime(2026, 8, 2, tzinfo=UTC)
    page = [
        {
            "instId": "ETH-USDT-SWAP",
            "fundingTime": str(int(start.timestamp() * 1000)),
            "fundingRate": "0.0001",
        }
    ]
    session_context = MagicMock()
    session_context.__enter__.return_value = MagicMock()
    session_context.__exit__.return_value = False

    with patch("aats.data_platform.config.get_settings", return_value=MagicMock()), patch(
        "aats.data_platform.db.get_session",
        return_value=session_context,
    ), patch(
        "scripts.rdp_deep_backfill_funding._query_existing_range",
        return_value=(end, end),
    ), patch(
        "scripts.rdp_deep_backfill_funding._fetch_funding_page",
        return_value=page,
    ), pytest.raises(RuntimeError, match="funding_backfill_dry_run_failed"):
        deep_backfill_funding(
            "BTC-USDT-SWAP",
            start,
            dry_run=True,
            rate_limit_sleep=0.0,
        )


def test_cli_returns_nonzero_when_any_symbol_fails() -> None:
    with patch(
        "scripts.rdp_deep_backfill_funding.deep_backfill_funding",
        side_effect=RuntimeError("funding_backfill_failed"),
    ):
        result = main(
            [
                "--symbols",
                "BTC-USDT-SWAP",
                "--target-start",
                "2026-08-25",
                "--dry-run",
            ]
        )

    assert result == 1
