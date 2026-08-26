from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from aats.data_platform.data_governance.archive import (
    ArchiveScope,
    _write_parquet_immutable,
)
from scripts.rdp_verify_archive_restore import main


def test_archive_restore_cli_dry_run_has_no_database_side_effect(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    scope = ArchiveScope(
        source_id="00000000-0000-0000-0000-000000000001",
        dataset_name="bronze.market_trades",
        table="bronze.market_trades",
        symbol="BTC-USDT-SWAP",
        coverage_start=start,
        coverage_end=start + timedelta(days=1),
    )
    parquet = tmp_path / "part.parquet"
    _write_parquet_immutable(
        iter([[{"symbol": scope.symbol, "ts": start, "trade_id": "1"}]]),
        parquet,
        parquet.with_suffix(".manifest.json"),
        scope,
    )

    assert main(["--parquet", str(parquet)]) == 2


def test_archive_restore_cli_rejects_relative_or_missing_file() -> None:
    assert main(["--parquet", "relative.parquet"]) == 4
