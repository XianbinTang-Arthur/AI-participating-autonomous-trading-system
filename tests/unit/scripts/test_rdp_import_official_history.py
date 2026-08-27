from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from scripts.rdp_import_official_history import (
    _deduplicate_gaps,
    _is_one_second_sample,
    _l2_samples_are_causal,
    _safe_error_code,
    main,
)


def test_official_import_error_code_keeps_safe_actionable_reason() -> None:
    assert (
        _safe_error_code(RuntimeError("trade_history_max_pages_exceeded"))
        == "trade_history_max_pages_exceeded"
    )
    assert (
        _safe_error_code(
            RuntimeError("okx_retry_exhausted:http=500:code=50011")
        )
        == "okx_retry_exhausted:http=500:code=50011"
    )


def test_official_import_error_code_redacts_unstructured_detail() -> None:
    assert _safe_error_code(RuntimeError("failed at /secret/path")) == "RuntimeError"
    assert _safe_error_code(Exception("postgresql://user:pw@host/db")) == "Exception"


def test_official_import_dry_run_validates_window_before_reporting_plan(
    tmp_path: Path,
) -> None:
    raw = (tmp_path / "raw").resolve()

    result = main(
        [
            "trade-rest",
            "--symbol",
            "BTC-USDT-SWAP",
            "--start",
            "2026-08-02T00:00:00Z",
            "--end",
            "2026-08-01T00:00:00Z",
            "--raw-archive-dir",
            str(raw),
        ]
    )

    assert result == 4


def test_official_import_valid_dry_run_has_no_database_or_network_side_effect(
    tmp_path: Path,
) -> None:
    raw = (tmp_path / "raw").resolve()

    result = main(
        [
            "trade-rest",
            "--symbol",
            "BTC-USDT-SWAP",
            "--start",
            "2026-08-01T00:00:00Z",
            "--end",
            "2026-08-02T00:00:00Z",
            "--raw-archive-dir",
            str(raw),
        ]
    )

    assert result == 2
    assert not raw.exists()


def test_official_trade_file_dry_run_rejects_missing_primary_input(
    tmp_path: Path,
) -> None:
    result = main(
        [
            "trade-file",
            "--symbol",
            "BTC-USDT-SWAP",
            "--start",
            "2026-08-01T00:00:00Z",
            "--end",
            "2026-08-02T00:00:00Z",
            "--input",
            str((tmp_path / "missing.zip").resolve()),
            "--raw-archive-dir",
            str((tmp_path / "raw").resolve()),
        ]
    )

    assert result == 4


def test_l2_half_second_samples_downsample_exactly_from_source_aligned_start() -> None:
    start = _safe_time("2026-08-20T00:00:00.003+00:00")

    assert _is_one_second_sample(start, start)
    assert not _is_one_second_sample(
        _safe_time("2026-08-20T00:00:00.503+00:00"), start
    )
    assert _is_one_second_sample(
        _safe_time("2026-08-20T00:00:01.003+00:00"), start
    )


def test_l2_causal_check_is_independent_from_classified_coverage_gap() -> None:
    sample = datetime(2026, 8, 20, tzinfo=timezone.utc)
    rows = [
        SimpleNamespace(
            ts=sample,
            source_state_ts=sample - timedelta(milliseconds=125),
            staleness_ms=125,
        )
    ]

    assert _l2_samples_are_causal(rows, source_rows_read=1)
    rows[0].source_state_ts = sample + timedelta(milliseconds=1)
    rows[0].staleness_ms = -1
    assert not _l2_samples_are_causal(rows, source_rows_read=1)


def test_l2_gap_provenance_deduplicates_derived_bbo_subset() -> None:
    gap = {
        "sample_ts": "2026-08-20T00:00:00+00:00",
        "gap_start": "2026-08-20T00:00:00+00:00",
        "gap_end": "2026-08-20T00:00:00.500000+00:00",
        "reason": "state_unavailable",
        "missing_samples": 1,
    }

    assert _deduplicate_gaps((gap,), (dict(gap),)) == (gap,)


def _safe_time(value: str):
    from datetime import datetime

    return datetime.fromisoformat(value)
