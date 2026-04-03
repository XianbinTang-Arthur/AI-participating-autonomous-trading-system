"""Raw source file discovery and registration.

Scans historical download directories for OKX candle/funding ZIP files,
registers them in meta.raw_source_files, and prevents duplicate ingestion.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from aats.data_platform.models import utc_now

# ---------------------------------------------------------------------------
# File name patterns — matched against real OKX historical download filenames
# ---------------------------------------------------------------------------
# Real examples:
#   BTC-USDT-candlesticks-2026-04-01.zip          (spot candles, day)
#   ETH-USDT-SWAP-candlesticks-2026-03.zip        (swap candles, month)
#   BTC-USDT-SWAP-fundingrates-2026-04-01.zip     (funding, day)
#   ETH-USDT-SWAP-fundingrates-2026-03.zip        (funding, month)
#
# NOTE: OKX candle filenames do NOT carry timeframe — timeframe_hint will
# be None for candles discovered from files.  The caller (backfill script)
# must determine the timeframe from directory structure or user input.

_CANDLE_DAY_RE = re.compile(
    r"^(?P<symbol>[A-Z0-9]+-[A-Z0-9]+(?:-SWAP)?)-candlesticks-(?P<date>\d{4}-\d{2}-\d{2})\.zip$",
    re.IGNORECASE,
)
_CANDLE_MONTH_RE = re.compile(
    r"^(?P<symbol>[A-Z0-9]+-[A-Z0-9]+(?:-SWAP)?)-candlesticks-(?P<month>\d{4}-\d{2})\.zip$",
    re.IGNORECASE,
)
_FUNDING_DAY_RE = re.compile(
    r"^(?P<symbol>[A-Z0-9]+-[A-Z0-9]+(?:-SWAP)?)-fundingrates-(?P<date>\d{4}-\d{2}-\d{2})\.zip$",
    re.IGNORECASE,
)
_FUNDING_MONTH_RE = re.compile(
    r"^(?P<symbol>[A-Z0-9]+-[A-Z0-9]+(?:-SWAP)?)-fundingrates-(?P<month>\d{4}-\d{2})\.zip$",
    re.IGNORECASE,
)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _infer_instrument_type(symbol: str) -> str | None:
    return "swap" if symbol.upper().endswith("SWAP") else "spot"


def _parse_filename(name: str) -> dict[str, Any] | None:
    """Extract metadata from a filename.  Returns None if unrecognised.

    NOTE: OKX candle filenames do not carry timeframe, so ``timeframe_hint``
    will be ``None`` for candle files.
    """
    for pat, domain, gran in [
        (_CANDLE_DAY_RE, "candles", "day"),
        (_CANDLE_MONTH_RE, "candles", "month"),
        (_FUNDING_DAY_RE, "funding", "day"),
        (_FUNDING_MONTH_RE, "funding", "month"),
    ]:
        m = pat.match(name)
        if m:
            gd = m.groupdict()
            return dict(
                dataset_domain=domain,
                symbol_hint=gd["symbol"].upper(),
                timeframe_hint=None,  # OKX filenames do not carry timeframe
                source_granularity=gran,
                date_hint=gd.get("date") or gd.get("month"),
            )
    return None


def discover_files(root_dir: str | Path) -> list[dict[str, Any]]:
    """Walk *root_dir* and return metadata dicts for every recognisable ZIP."""
    root = Path(root_dir)
    results: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.zip")):
        info = _parse_filename(path.name)
        if info is None:
            continue
        info["source_path"] = str(path.resolve())
        info["file_size_bytes"] = path.stat().st_size
        results.append(info)
    return results


def register_source_file(
    session: Session,
    *,
    source_path: str,
    dataset_domain: str,
    symbol_hint: str,
    timeframe_hint: str | None,
    source_granularity: str | None,
    file_size_bytes: int | None = None,
    compute_checksum: bool = True,
) -> str | None:
    """Register a file in meta.raw_source_files. Returns file_id or None if duplicate."""
    # Check for duplicate path
    existing = session.execute(
        text("SELECT source_file_id FROM meta.raw_source_files WHERE source_path = :p"),
        dict(p=source_path),
    ).scalar()
    if existing:
        return None

    checksum = _sha256(Path(source_path)) if compute_checksum else None
    if checksum:
        dup = session.execute(
            text("SELECT source_file_id FROM meta.raw_source_files WHERE checksum = :c"),
            dict(c=checksum),
        ).scalar()
        if dup:
            return None

    file_id = str(uuid.uuid4())
    now = utc_now()
    session.execute(
        text("""
            INSERT INTO meta.raw_source_files
                (source_file_id, source_type, dataset_domain, instrument_type,
                 symbol_hint, timeframe_hint, source_granularity,
                 source_path, checksum, file_size_bytes, discovered_at,
                 parse_status, ingested_status, created_at, updated_at)
            VALUES
                (:fid, 'historical_file', :domain, :inst,
                 :sym, :tf, :gran,
                 :path, :cksum, :size, :now,
                 'pending', 'pending', :now, :now)
        """),
        dict(
            fid=file_id, domain=dataset_domain,
            inst=_infer_instrument_type(symbol_hint),
            sym=symbol_hint, tf=timeframe_hint, gran=source_granularity,
            path=source_path, cksum=checksum, size=file_size_bytes, now=now,
        ),
    )
    return file_id


def discover_and_register(session: Session, root_dir: str | Path) -> list[str]:
    """Discover all files under *root_dir* and register new ones. Returns list of new file_ids."""
    new_ids: list[str] = []
    for info in discover_files(root_dir):
        fid = register_source_file(
            session,
            source_path=info["source_path"],
            dataset_domain=info["dataset_domain"],
            symbol_hint=info["symbol_hint"],
            timeframe_hint=info.get("timeframe_hint"),
            source_granularity=info.get("source_granularity"),
            file_size_bytes=info.get("file_size_bytes"),
        )
        if fid:
            new_ids.append(fid)
    return new_ids
