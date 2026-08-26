from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from scripts.rdp_deep_backfill_funding import _parse_funding, deep_backfill_funding


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
