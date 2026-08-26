from __future__ import annotations

from pathlib import Path

from scripts.rdp_import_official_history import _safe_error_code, main


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
