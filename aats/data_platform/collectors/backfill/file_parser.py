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
# Real OKX historical candle CSV header:
#   instrument_name,open,high,low,close,vol,vol_ccy,vol_quote,open_time,confirm
#
# Key facts:
#   - First row IS a header
#   - First column is instrument_name, NOT timestamp
#   - Timestamp column is open_time (ms epoch), at index 8 (0-based)

# Mapping from header column names to our internal names.
# Handles minor OKX header variations with a normalised lookup.
_CANDLE_HEADER_MAP: dict[str, str] = {
    "instrument_name": "instrument_name",
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "vol": "vol",
    "vol_ccy": "vol_ccy",
    "vol_quote": "vol_quote",
    "open_time": "open_time",
    "confirm": "confirm",
    # fallbacks for possible API-style headers
    "ts": "open_time",
    "volccy": "vol_ccy",
    "volccyquote": "vol_quote",
    "volquote": "vol_quote",
}


def parse_candle_csv_rows(reader: csv.reader, symbol_hint: str) -> list[CandleRow]:
    """Parse rows from an OKX candle CSV **with header**.

    Reads the first row as column headers, maps by name, and extracts
    ``open_time`` as the bar-open timestamp.  Falls back to positional
    parsing only when the first row looks like data (all-numeric).
    """
    rows: list[CandleRow] = []
    header: list[str] | None = None
    col: dict[str, int] = {}

    for line in reader:
        if len(line) < 9:
            continue

        # Detect header row on the first valid line
        if header is None:
            # If the first cell is NOT a pure integer (ms epoch), treat as header
            if not line[0].strip().isdigit():
                header = [c.strip().lower().replace(" ", "_") for c in line]
                for i, name in enumerate(header):
                    mapped = _CANDLE_HEADER_MAP.get(name)
                    if mapped:
                        col[mapped] = i
                continue
            else:
                # No header — assume legacy positional order:
                # ts, open, high, low, close, vol, volCcy, volQuote, confirm
                header = []
                col = {
                    "open_time": 0, "open": 1, "high": 2, "low": 3, "close": 4,
                    "vol": 5, "vol_ccy": 6, "vol_quote": 7, "confirm": 8,
                }

        # Extract values by column name
        def _get(key: str) -> str:
            idx = col.get(key)
            if idx is not None and idx < len(line):
                return line[idx].strip()
            return ""

        raw_ts = _get("open_time")
        if not raw_ts:
            continue
        try:
            ts = _ts_from_ms(raw_ts)
        except (ValueError, OSError):
            continue

        # instrument_name -> raw_symbol; fall back to symbol_hint
        raw_sym = _get("instrument_name") or symbol_hint

        rows.append(CandleRow(
            symbol=symbol_hint.upper(),
            ts=ts,
            open=_dec(_get("open")),
            high=_dec(_get("high")),
            low=_dec(_get("low")),
            close=_dec(_get("close")),
            vol=_dec_or_none(_get("vol")),
            vol_ccy=_dec_or_none(_get("vol_ccy")),
            vol_quote=_dec_or_none(_get("vol_quote")),
            confirm=_bool_confirm(_get("confirm")) if _get("confirm") else True,
            raw_symbol=raw_sym,
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
    """Parse rows from an OKX funding CSV.

    Real OKX header: ``instrument_name,funding_rate,funding_time``
    Skips the header row automatically.
    """
    rows: list[FundingRow] = []
    header_skipped = False
    for line in reader:
        if len(line) < 3:
            continue
        # Skip header: first cell is non-numeric (e.g. "instrument_name")
        if not header_skipped:
            if not line[2].strip().isdigit():
                header_skipped = True
                continue
            header_skipped = True

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
