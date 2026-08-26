from __future__ import annotations

import csv
import io
from decimal import Decimal

from aats.data_platform.collectors.backfill.file_parser import parse_funding_csv_rows


def test_official_funding_file_is_recorded_as_settled_without_interval_assumption() -> None:
    reader = csv.reader(
        io.StringIO(
            "instrument_name,funding_rate,funding_time\n"
            "BTC-USDT-SWAP,0.0001,1787695200000\n"
            "BTC-USDT-SWAP,0.0002,1787702400000\n"
            "BTC-USDT-SWAP,0.0003,1787716800000\n"
        )
    )

    rows = parse_funding_csv_rows(reader, "BTC-USDT-SWAP")

    assert len(rows) == 3
    assert [int((rows[index].ts - rows[index - 1].ts).total_seconds()) for index in (1, 2)] == [
        7200,
        14400,
    ]
    assert all(row.method == "settled_file" for row in rows)
    assert [row.realized_rate for row in rows] == [
        Decimal("0.0001"),
        Decimal("0.0002"),
        Decimal("0.0003"),
    ]
