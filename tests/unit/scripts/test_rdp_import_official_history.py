from __future__ import annotations

from pathlib import Path

from scripts.rdp_import_official_history import main


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
