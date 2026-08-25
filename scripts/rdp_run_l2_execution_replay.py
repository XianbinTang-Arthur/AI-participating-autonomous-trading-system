#!/usr/bin/env python3
"""Run causal Top-5/event execution replay from eligible RDP microstructure data."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Mapping

from sqlalchemy import create_engine, text

_SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from aats.data_platform.execution_realism.l2_event_replay import (  # noqa: E402
    L2OrderBookSnapshot,
    L2OrderRequest,
    L2ReplayPolicy,
    L2TradeEvent,
    OrderBookLevel,
    replay_l2_orders,
)


_BOOK_QUERY = text(
    """
    SELECT
        book.*,
        payload.collector_sequence,
        payload.payload_hash,
        payload.capture_status
    FROM bronze.market_orderbook_books5 AS book
    JOIN bronze.market_orderbook_payloads AS payload
      ON payload.snapshot_table = 'bronze.market_orderbook_books5'
     AND payload.symbol = book.symbol
     AND payload.ts = book.ts
    WHERE book.symbol = :symbol
      AND book.ts >= :window_start
      AND book.ts <= :window_end
      AND payload.capture_status = 'diff_payload_persisted'
      AND payload.payload_hash IS NOT NULL
    ORDER BY book.ts, payload.collector_sequence
    """
)
_TRADE_QUERY = text(
    """
    SELECT symbol, ts, trade_id, px, sz, side
    FROM bronze.market_trades
    WHERE symbol = :symbol
      AND ts >= :window_start
      AND ts <= :window_end
    ORDER BY ts, trade_id
    """
)


def _parse_datetime(raw: str, *, field_name: str) -> datetime:
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name}_must_be_timezone_aware")
    return parsed.astimezone(UTC)


def _load_requests(path: pathlib.Path) -> tuple[dict[str, Any], tuple[L2OrderRequest, ...]]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, Mapping) or not isinstance(payload.get("requests"), list):
        raise ValueError("request_manifest_invalid")
    symbol = str(payload.get("symbol", "")).strip().upper()
    requests: list[L2OrderRequest] = []
    for row in payload["requests"]:
        if not isinstance(row, Mapping):
            raise ValueError("request_manifest_row_invalid")
        requests.append(
            L2OrderRequest(
                order_id=str(row.get("order_id", "")),
                symbol=symbol,
                submitted_at=_parse_datetime(
                    str(row.get("submitted_at", "")),
                    field_name="submitted_at",
                ),
                side=str(row.get("side", "")),  # type: ignore[arg-type]
                order_kind=str(row.get("order_kind", "")),  # type: ignore[arg-type]
                target_quantity=Decimal(str(row.get("target_quantity", ""))),
                expected_edge_bps=float(row.get("expected_edge_bps", 0.0)),
                limit_price=(
                    Decimal(str(row["limit_price"]))
                    if row.get("limit_price") is not None
                    else None
                ),
                max_wait_ms=int(row.get("max_wait_ms", 2_000)),
            )
        )
    if not requests:
        raise ValueError("request_manifest_empty")
    return dict(payload), tuple(requests)


def _request_evidence_context(manifest: Mapping[str, Any]) -> tuple[str, str, str]:
    plan_id = str(manifest.get("plan_id", "")).strip()
    timeframe = str(manifest.get("timeframe", "")).strip()
    benchmark_segment = str(manifest.get("benchmark_segment", "")).strip()
    dataset_fingerprint = str(manifest.get("dataset_fingerprint", "")).strip()
    if not plan_id:
        raise ValueError("request_manifest_plan_id_required")
    if not timeframe:
        raise ValueError("request_manifest_timeframe_required")
    if benchmark_segment != "valid":
        raise ValueError("request_manifest_benchmark_segment_must_be_valid")
    if (
        not dataset_fingerprint.startswith("rfds_")
        or len(dataset_fingerprint) != 69
        or not all(
            character in "0123456789abcdef"
            for character in dataset_fingerprint[5:]
        )
    ):
        raise ValueError("request_manifest_dataset_fingerprint_invalid")
    return plan_id, timeframe, dataset_fingerprint


def _floor_15m(value: datetime) -> datetime:
    return value.replace(minute=(value.minute // 15) * 15, second=0, microsecond=0)


def _eligibility_manifest_fingerprint(
    path: pathlib.Path,
    *,
    symbol: str,
    window_start: datetime,
    window_end: datetime,
) -> str:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    reports = payload.get("reports") if isinstance(payload, Mapping) else None
    if not isinstance(reports, list) or not reports:
        raise ValueError("eligibility_manifest_reports_required")
    evidence_by_start: dict[datetime, str] = {}
    for report in reports:
        if not isinstance(report, Mapping) or report.get("eligible_for_research") is not True:
            raise ValueError("eligibility_manifest_contains_ineligible_window")
        observation = report.get("observation")
        if not isinstance(observation, Mapping):
            raise ValueError("eligibility_manifest_observation_missing")
        if str(observation.get("symbol", "")).strip().upper() != symbol:
            raise ValueError("eligibility_manifest_symbol_mismatch")
        start = _parse_datetime(str(observation.get("window_start", "")), field_name="window_start")
        fingerprint = str(report.get("evidence_fingerprint", "")).strip()
        if len(fingerprint) != 64:
            raise ValueError("eligibility_evidence_fingerprint_invalid")
        evidence_by_start[start] = fingerprint
    expected: list[datetime] = []
    current = _floor_15m(window_start)
    final = _floor_15m(window_end)
    while current <= final:
        expected.append(current)
        current += timedelta(minutes=15)
    missing = [item.isoformat() for item in expected if item not in evidence_by_start]
    if missing:
        raise ValueError(f"eligibility_manifest_window_gap:{missing}")
    encoded = json.dumps(
        [(item.isoformat(), evidence_by_start[item]) for item in expected],
        separators=(",", ":"),
    ).encode("utf-8")
    return "micro_" + hashlib.sha256(encoded).hexdigest()


def _book_row_to_snapshot(row: Mapping[str, Any]) -> L2OrderBookSnapshot:
    bids = tuple(
        OrderBookLevel(row[f"bid_px_{index}"], row[f"bid_sz_{index}"])
        for index in range(1, 6)
        if row[f"bid_px_{index}"] is not None and row[f"bid_sz_{index}"] is not None
    )
    asks = tuple(
        OrderBookLevel(row[f"ask_px_{index}"], row[f"ask_sz_{index}"])
        for index in range(1, 6)
        if row[f"ask_px_{index}"] is not None and row[f"ask_sz_{index}"] is not None
    )
    return L2OrderBookSnapshot(
        symbol=str(row["symbol"]),
        ts=row["ts"],
        collector_sequence=int(row["collector_sequence"]),
        bids=bids,
        asks=asks,
        payload_hash=str(row["payload_hash"]),
    )


def _trade_row_to_event(row: Mapping[str, Any]) -> L2TradeEvent:
    return L2TradeEvent(
        symbol=str(row["symbol"]),
        ts=row["ts"],
        trade_id=str(row["trade_id"]),
        price=row["px"],
        quantity=row["sz"],
        aggressor_side=str(row["side"]),  # type: ignore[arg-type]
    )


def _write_json_once(path: pathlib.Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-manifest", type=pathlib.Path, required=True)
    parser.add_argument("--eligibility-manifest", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--cost-summary-output", type=pathlib.Path, required=True)
    parser.add_argument("--taker-fee-bps", type=float, default=5.0)
    parser.add_argument("--maker-fee-bps", type=float, default=2.0)
    parser.add_argument("--queue-ahead-multiplier", default="1.0")
    args = parser.parse_args(argv)

    database_url = os.environ.get("RDP_DATABASE_URL", "").strip()
    if not database_url:
        print("ERROR: RDP_DATABASE_URL is required", file=sys.stderr)
        return 2
    try:
        manifest, requests = _load_requests(args.request_manifest)
        plan_id, timeframe, dataset_fingerprint = _request_evidence_context(manifest)
        symbol = requests[0].symbol
        start = min(request.submitted_at for request in requests)
        end = max(
            request.submitted_at + timedelta(milliseconds=request.max_wait_ms)
            for request in requests
        )
        eligibility_fingerprint = _eligibility_manifest_fingerprint(
            args.eligibility_manifest,
            symbol=symbol,
            window_start=start,
            window_end=end,
        )
        engine = create_engine(database_url, pool_pre_ping=True)
        try:
            with engine.connect() as connection:
                params = {"symbol": symbol, "window_start": start, "window_end": end}
                book_rows = connection.execute(_BOOK_QUERY, params).mappings().all()
                trade_rows = connection.execute(_TRADE_QUERY, params).mappings().all()
        finally:
            engine.dispose()
        evidence = replay_l2_orders(
            snapshots=tuple(_book_row_to_snapshot(row) for row in book_rows),
            trades=tuple(_trade_row_to_event(row) for row in trade_rows),
            requests=requests,
            microstructure_eligibility_fingerprint=eligibility_fingerprint,
            policy=L2ReplayPolicy(
                taker_fee_bps=args.taker_fee_bps,
                maker_fee_bps=args.maker_fee_bps,
                queue_ahead_multiplier=Decimal(args.queue_ahead_multiplier),
            ),
        )
        _write_json_once(args.output, evidence.to_dict())
        _write_json_once(
            args.cost_summary_output,
            evidence.execution_cost_summary(
                plan_id=plan_id,
                timeframe=timeframe,
                benchmark_segment="valid",
                dataset_fingerprint=dataset_fingerprint,
            ),
        )
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "model_version": evidence.model_version,
                "request_count": evidence.request_count,
                "evidence_fingerprint": evidence.evidence_fingerprint,
                "output": args.output.as_posix(),
                "cost_summary_output": args.cost_summary_output.as_posix(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
