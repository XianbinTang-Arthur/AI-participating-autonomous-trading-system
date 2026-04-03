"""Historical ZIP/CSV file parsers for OKX candles and funding data."""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from aats.data_platform.models import CandleRow, FundingRow


def _ts_from_ms(raw: str) -> datetime:
    """Convert millisecond epoch string to UTC datetime."""
    return datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc)


def _dec(val: str) -> Decimal:
    return Decimal(val.strip()) if val.strip() else Decimal(0)


def _dec_or_none(val: str) -> Decimal | None:
    v = val.strip()
    if not v:
        return None
    try:
        return Decimal(v)
    except InvalidOperation:
        return None


def _bool_confirm(val: str) -> bool:
    v = val.strip().lower()
    return v in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# Candle file parser
# ---------------------------------------------------------------------------

def parse_candle_csv_rows(reader: csv.reader, symbol_hint: str) -> list[CandleRow]:
    """Parse rows from a candle CSV (no header expected from OKX files)."""
    rows: list[CandleRow] = []
    for line in reader:
        if len(line) < 9:
            continue
        # OKX candle CSV columns (no header):
        # ts, open, high, low, close, vol, volCcy, volCcyQuote, confirm
        raw_ts = line[0].strip()
        try:
            ts = _ts_from_ms(raw_ts)
        except (ValueError, OSError):
            continue
        rows.append(CandleRow(
            symbol=symbol_hint.upper(),
            ts=ts,
            open=_dec(line[1]),
            high=_dec(line[2]),
            low=_dec(line[3]),
            close=_dec(line[4]),
            vol=_dec_or_none(line[5]),
            vol_ccy=_dec_or_none(line[6]),
            vol_quote=_dec_or_none(line[7]),
            confirm=_bool_confirm(line[8]) if len(line) > 8 else True,
            raw_symbol=symbol_hint,
            raw_ts=raw_ts,
        ))
    return rows


def parse_candle_zip(zip_path: str | Path, symbol_hint: str) -> list[CandleRow]:
    """Open a candle ZIP, parse every CSV inside, return all CandleRows."""
    all_rows: list[CandleRow] = []
    with zipfile.ZipFile(zip_path, "r") as zf:
        for name in sorted(zf.namelist()):
            if not name.lower().endswith(".csv"):
                continue
            with zf.open(name) as f:
                text_stream = io.TextIOWrapper(f, encoding="utf-8", errors="replace")
                reader = csv.reader(text_stream)
                all_rows.extend(parse_candle_csv_rows(reader, symbol_hint))
    return all_rows


# ---------------------------------------------------------------------------
# Funding file parser
# ---------------------------------------------------------------------------

def parse_funding_csv_rows(reader: csv.reader, symbol_hint: str) -> list[FundingRow]:
    """Parse rows from a funding CSV."""
    rows: list[FundingRow] = []
    for line in reader:
        if len(line) < 3:
            continue
        # OKX funding CSV: symbol, fundingRate, fundingTime
        raw_sym = line[0].strip()
        raw_rate = line[1].strip()
        raw_ts = line[2].strip()
        try:
            ts = _ts_from_ms(raw_ts)
        except (ValueError, OSError):
            continue
        try:
            rate = Decimal(raw_rate)
        except InvalidOperation:
            continue
        rows.append(FundingRow(
            symbol=symbol_hint.upper(),
            ts=ts,
            funding_rate=rate,
            raw_symbol=raw_sym or symbol_hint,
            raw_ts=raw_ts,
        ))
    return rows


def parse_funding_zip(zip_path: str | Path, symbol_hint: str) -> list[FundingRow]:
    """Open a funding ZIP, parse every CSV inside, return all FundingRows."""
    all_rows: list[FundingRow] = []
    with zipfile.ZipFile(zip_path, "r") as zf:
        for name in sorted(zf.namelist()):
            if not name.lower().endswith(".csv"):
                continue
            with zf.open(name) as f:
                text_stream = io.TextIOWrapper(f, encoding="utf-8", errors="replace")
                reader = csv.reader(text_stream)
                all_rows.extend(parse_funding_csv_rows(reader, symbol_hint))
    return all_rows
