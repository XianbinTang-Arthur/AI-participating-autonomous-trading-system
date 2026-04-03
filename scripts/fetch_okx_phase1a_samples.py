from __future__ import annotations

import argparse
import csv
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests


BASE_URL = "https://www.okx.com"
CANDLES_ENDPOINT = "/api/v5/market/history-candles"
FUNDING_ENDPOINT = "/api/v5/public/funding-rate-history"

DEFAULT_CANDLE_INSTRUMENTS = [
    "BTC-USDT",
    "ETH-USDT",
    "BTC-USDT-SWAP",
    "ETH-USDT-SWAP",
]

DEFAULT_FUNDING_INSTRUMENTS = [
    "BTC-USDT-SWAP",
    "ETH-USDT-SWAP",
]

DEFAULT_BARS = ["1m", "5m", "15m", "1H"]


@dataclass(frozen=True)
class FetchResult:
    endpoint: str
    inst_id: str
    bar: str | None
    raw_path: Path
    preview_path: Path
    count: int


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, headers: list[str], rows: Iterable[list[Any]]) -> None:
    ensure_dir(path.parent)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for row in rows:
            writer.writerow(row)


def get_json(session: requests.Session, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
    url = f"{BASE_URL}{endpoint}"
    resp = session.get(url, params=params, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("code") != "0":
        raise RuntimeError(f"OKX API returned error for {url} params={params}: {payload}")
    return payload


def fetch_candles(
    session: requests.Session,
    output_root: Path,
    inst_id: str,
    bar: str,
    limit: int,
) -> FetchResult:
    params = {
        "instId": inst_id,
        "bar": bar,
        "limit": str(limit),
    }
    payload = get_json(session, CANDLES_ENDPOINT, params)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stem = f"{inst_id}-{bar}-{timestamp}"

    raw_path = output_root / "candles" / f"{stem}.json"
    preview_path = output_root / "candles" / f"{stem}.csv"

    wrapped = {
        "fetched_at_utc": utc_now_iso(),
        "endpoint": CANDLES_ENDPOINT,
        "params": params,
        "response": payload,
    }
    write_json(raw_path, wrapped)

    # OKX docs: [ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm]
    rows = payload.get("data", [])
    csv_rows: list[list[Any]] = []
    for item in rows:
        csv_rows.append([
            item[0],  # ts
            item[1],  # open
            item[2],  # high
            item[3],  # low
            item[4],  # close
            item[5],  # vol
            item[6],  # volCcy
            item[7],  # volCcyQuote
            item[8],  # confirm
        ])

    write_csv(
        preview_path,
        headers=[
            "ts",
            "open",
            "high",
            "low",
            "close",
            "vol",
            "volCcy",
            "volCcyQuote",
            "confirm",
        ],
        rows=csv_rows,
    )

    return FetchResult(
        endpoint=CANDLES_ENDPOINT,
        inst_id=inst_id,
        bar=bar,
        raw_path=raw_path,
        preview_path=preview_path,
        count=len(rows),
    )


def fetch_funding(
    session: requests.Session,
    output_root: Path,
    inst_id: str,
    limit: int,
) -> FetchResult:
    params = {
        "instId": inst_id,
        "limit": str(limit),
    }
    payload = get_json(session, FUNDING_ENDPOINT, params)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stem = f"{inst_id}-funding-{timestamp}"

    raw_path = output_root / "funding" / f"{stem}.json"
    preview_path = output_root / "funding" / f"{stem}.csv"

    wrapped = {
        "fetched_at_utc": utc_now_iso(),
        "endpoint": FUNDING_ENDPOINT,
        "params": params,
        "response": payload,
    }
    write_json(raw_path, wrapped)

    rows = payload.get("data", [])
    # funding history can evolve; keep a flexible preview
    preferred_order = [
        "instId",
        "fundingRate",
        "fundingTime",
        "formulaType",
        "method",
        "interestRate",
        "impactValue",
        "nextFundingRate",
        "nextFundingTime",
        "settState",
        "settFundingRate",
    ]

    all_keys: list[str] = []
    seen = set()
    for key in preferred_order:
        seen.add(key)
        all_keys.append(key)
    for row in rows:
        for key in row.keys():
            if key not in seen:
                seen.add(key)
                all_keys.append(key)

    csv_rows: list[list[Any]] = []
    for row in rows:
        csv_rows.append([row.get(k) for k in all_keys])

    write_csv(preview_path, headers=all_keys, rows=csv_rows)

    return FetchResult(
        endpoint=FUNDING_ENDPOINT,
        inst_id=inst_id,
        bar=None,
        raw_path=raw_path,
        preview_path=preview_path,
        count=len(rows),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch Phase 1-A OKX API sample payloads for candles and funding."
    )
    parser.add_argument(
        "--output-root",
        default="artifacts/okx_api_samples",
        help="Directory to save raw JSON and preview CSV files.",
    )
    parser.add_argument(
        "--candle-limit",
        type=int,
        default=200,
        help="Number of rows to request per candle sample.",
    )
    parser.add_argument(
        "--funding-limit",
        type=int,
        default=50,
        help="Number of rows to request per funding sample.",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=0.2,
        help="Sleep between requests to stay comfortably under rate limits.",
    )
    parser.add_argument(
        "--candle-instruments",
        nargs="*",
        default=DEFAULT_CANDLE_INSTRUMENTS,
        help="Candles instrument list.",
    )
    parser.add_argument(
        "--funding-instruments",
        nargs="*",
        default=DEFAULT_FUNDING_INSTRUMENTS,
        help="Funding instrument list.",
    )
    parser.add_argument(
        "--bars",
        nargs="*",
        default=DEFAULT_BARS,
        help="Candles bar sizes, e.g. 1m 5m 15m 1H",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_root = Path(args.output_root)
    ensure_dir(output_root / "candles")
    ensure_dir(output_root / "funding")

    session = requests.Session()
    session.headers.update({
        "User-Agent": "aats-phase1a-sample-fetcher/1.0"
    })

    results: list[FetchResult] = []

    # Candles
    for inst_id in args.candle_instruments:
        for bar in args.bars:
            result = fetch_candles(
                session=session,
                output_root=output_root,
                inst_id=inst_id,
                bar=bar,
                limit=args.candle_limit,
            )
            results.append(result)
            print(f"[candles] {inst_id} {bar}: {result.count} rows -> {result.raw_path}")
            time.sleep(args.sleep_seconds)

    # Funding
    for inst_id in args.funding_instruments:
        result = fetch_funding(
            session=session,
            output_root=output_root,
            inst_id=inst_id,
            limit=args.funding_limit,
        )
        results.append(result)
        print(f"[funding] {inst_id}: {result.count} rows -> {result.raw_path}")
        time.sleep(args.sleep_seconds)

    # Summary
    summary_path = output_root / "fetch_summary.json"
    summary = {
        "fetched_at_utc": utc_now_iso(),
        "base_url": BASE_URL,
        "results": [
            {
                "endpoint": r.endpoint,
                "inst_id": r.inst_id,
                "bar": r.bar,
                "raw_path": str(r.raw_path),
                "preview_path": str(r.preview_path),
                "count": r.count,
            }
            for r in results
        ],
    }
    write_json(summary_path, summary)
    print(f"\nSummary written to: {summary_path}")


if __name__ == "__main__":
    main()