#!/usr/bin/env python3
"""Build a fail-closed research-eligibility report for one Silver 15m window.

The command reads only ``RDP_DATABASE_URL`` from the process environment.  It
does not load dotenv files and it never prints a connection string.  Collector
freshness must come from the deployment evidence packet produced by the
standard deployment workflow.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import stat
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from sqlalchemy import create_engine, text

_SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from aats.data_platform.quality.microstructure_eligibility import (  # noqa: E402
    MicrostructureEligibilityPolicy,
    MicrostructureWindowObservation,
    evaluate_microstructure_window,
)
from aats.data_platform.data_governance.continuity import (  # noqa: E402
    classify_continuity_window,
)


_UTC = timezone.utc
_QUERY = text(
    """
    SELECT
        ob.symbol,
        ob.ts,
        ob.bbo_samples_n,
        ob.books5_samples_n,
        ob.dataset_version AS orderbook_dataset_version,
        ob.ingest_run_id::text AS orderbook_ingest_run_id,
        ob.quality_flags AS orderbook_quality_flags,
        tf.trade_count,
        tf.dataset_version AS trades_dataset_version,
        tf.ingest_run_id::text AS trades_ingest_run_id,
        tf.quality_flags AS trades_quality_flags,
        oi.oi_samples_n,
        oi.funding_rate_current,
        oi.mark_price,
        oi.dataset_version AS oi_funding_dataset_version,
        oi.ingest_run_id::text AS oi_funding_ingest_run_id,
        oi.quality_flags AS oi_funding_quality_flags,
        COALESCE(liq.long_liq_count, 0) + COALESCE(liq.short_liq_count, 0)
            AS liquidation_event_count,
        liq.dataset_version AS liquidations_dataset_version,
        liq.ingest_run_id::text AS liquidations_ingest_run_id,
        liq.quality_flags AS liquidations_quality_flags
    FROM silver.market_orderbook_metrics_15m AS ob
    LEFT JOIN silver.market_trade_flow_15m AS tf
        ON tf.symbol = ob.symbol AND tf.ts = ob.ts
    LEFT JOIN silver.market_oi_funding_metrics_15m AS oi
        ON oi.symbol = ob.symbol AND oi.ts = ob.ts
    LEFT JOIN silver.market_liquidation_metrics_15m AS liq
        ON liq.symbol = ob.symbol AND liq.ts = ob.ts
    WHERE ob.symbol = :symbol
      AND (
          CAST(:window_start AS TIMESTAMPTZ) IS NULL
          OR ob.ts = :window_start
      )
    ORDER BY ob.ts DESC
    LIMIT 1
    """
)


def _parse_window_start(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("window_start_must_be_timezone_aware")
    return parsed.astimezone(_UTC)


def _load_collector_freshness(
    path: pathlib.Path,
    *,
    evaluated_at: datetime | None = None,
    max_age_seconds: float = 60.0,
) -> dict[str, bool]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    rows = payload.get("collector_freshness")
    if not isinstance(rows, list):
        raise ValueError("collector_evidence_missing_collector_freshness")
    now = (evaluated_at or datetime.now(_UTC)).astimezone(_UTC)
    result: dict[str, bool] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("collector_evidence_invalid_row")
        name = str(row.get("name", ""))
        heartbeat_raw = row.get("heartbeat_at")
        if not heartbeat_raw:
            result[name] = False
            continue
        heartbeat_at = datetime.fromisoformat(str(heartbeat_raw).replace("Z", "+00:00"))
        if heartbeat_at.tzinfo is None or heartbeat_at.utcoffset() is None:
            raise ValueError("collector_heartbeat_at_must_be_timezone_aware")
        age_seconds = (now - heartbeat_at.astimezone(_UTC)).total_seconds()
        result[name] = (
            row.get("fresh") is True
            and -5.0 <= age_seconds < max_age_seconds
        )
    return result


def _row_to_observation(
    row: Mapping[str, Any],
    *,
    collector_freshness: Mapping[str, bool],
    continuity: Mapping[str, Mapping[str, Any]],
) -> MicrostructureWindowObservation:
    start = row["ts"]
    return MicrostructureWindowObservation(
        symbol=str(row["symbol"]),
        window_start=start,
        window_end=start + timedelta(minutes=15),
        bbo_samples_n=int(row["bbo_samples_n"] or 0),
        books5_samples_n=int(row["books5_samples_n"] or 0),
        trade_count=int(row["trade_count"] or 0),
        oi_samples_n=int(row["oi_samples_n"] or 0),
        funding_rate_present=row["funding_rate_current"] is not None,
        mark_price_present=row["mark_price"] is not None,
        liquidation_event_count=int(row["liquidation_event_count"] or 0),
        microstructure_collector_fresh=collector_freshness.get(
            "aats-microstructure-collector", False
        ),
        liquidations_collector_fresh=collector_freshness.get(
            "aats-liquidations-daemon", False
        ),
        continuity_statuses={
            name: str(value["status"])
            for name, value in continuity.items()
        },
        connection_generations={
            name: value["connection_generation"]
            for name, value in continuity.items()
        },
        continuity_drop_counts={
            name: int(value["drop_count"])
            for name, value in continuity.items()
        },
        continuity_fingerprints={
            name: str(value["fingerprint"])
            for name, value in continuity.items()
        },
        dataset_versions={
            "orderbook": row["orderbook_dataset_version"],
            "trades": row["trades_dataset_version"],
            "oi_funding": row["oi_funding_dataset_version"],
            "liquidations": row["liquidations_dataset_version"],
        },
        ingest_run_ids={
            "orderbook": row["orderbook_ingest_run_id"],
            "trades": row["trades_ingest_run_id"],
            "oi_funding": row["oi_funding_ingest_run_id"],
            "liquidations": row["liquidations_ingest_run_id"],
        },
        quality_flags={
            "orderbook": row["orderbook_quality_flags"] or (),
            "trades": row["trades_quality_flags"] or (),
            "oi_funding": row["oi_funding_quality_flags"] or (),
            "liquidations": row["liquidations_quality_flags"] or (),
        },
    )


def _load_continuity(connection, row: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    start = row["ts"]
    end = start + timedelta(minutes=15)
    symbol = str(row["symbol"])
    channels = {
        "orderbook": (
            ("aats-microstructure-collector", "bbo-tbt"),
            ("aats-microstructure-collector", "books5"),
        ),
        "trades": (("aats-microstructure-collector", "trades"),),
        "oi_funding": (
            ("aats-microstructure-collector", "open-interest"),
            ("aats-microstructure-collector", "funding-rate"),
            ("aats-microstructure-collector", "mark-price"),
        ),
        "liquidations": (
            ("aats-liquidations-daemon", "liquidation-orders"),
        ),
    }
    output: dict[str, dict[str, Any]] = {}
    for dataset, bindings in channels.items():
        reports = [
            classify_continuity_window(
                connection,
                collector=collector,
                channel=channel,
                symbol=symbol,
                window_start=start,
                window_end=end,
                require_flush=(dataset != "liquidations"),
            )
            for collector, channel in bindings
        ]
        statuses = {report.status for report in reports}
        status = (
            "complete"
            if statuses == {"complete"}
            else ("known_gap" if "known_gap" in statuses else "unknown")
        )
        generations = {
            generation
            for report in reports
            for generation in report.generations
        }
        fingerprints = sorted(report.fingerprint for report in reports)
        output[dataset] = {
            "status": status,
            "connection_generation": (
                next(iter(generations)) if len(generations) == 1 else None
            ),
            "drop_count": sum(
                report.event_counts.get("DROP", 0) for report in reports
            ),
            "fingerprint": hashlib.sha256(
                "|".join(fingerprints).encode()
            ).hexdigest(),
        }
    return output


def _write_new_report(path: pathlib.Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(encoded)
    path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True, help="例如 BTC-USDT-SWAP")
    parser.add_argument(
        "--window-start",
        help="ISO-8601 UTC 起点；省略时选择该 symbol 最新的 Silver 窗口",
    )
    parser.add_argument("--collector-evidence", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--min-bbo-samples", type=int, default=720)
    parser.add_argument("--min-books5-samples", type=int, default=720)
    parser.add_argument("--min-trade-count", type=int, default=1)
    parser.add_argument("--min-oi-samples", type=int, default=1)
    parser.add_argument(
        "--max-latest-window-age-seconds",
        type=int,
        default=1_800,
        help="省略 --window-start 时，最新 Silver 窗口结束时间允许的最大年龄",
    )
    args = parser.parse_args()
    if args.max_latest_window_age_seconds <= 0:
        parser.error("--max-latest-window-age-seconds must be positive")

    database_url = os.environ.get("RDP_DATABASE_URL", "").strip()
    if not database_url:
        print("ERROR: RDP_DATABASE_URL is required", file=sys.stderr)
        return 2
    try:
        evaluated_at = datetime.now(_UTC)
        window_start = _parse_window_start(args.window_start)
        collector_freshness = _load_collector_freshness(
            args.collector_evidence,
            evaluated_at=evaluated_at,
        )
        engine = create_engine(database_url, pool_pre_ping=True)
        try:
            with engine.connect() as connection:
                row = connection.execute(
                    _QUERY,
                    {
                        "symbol": args.symbol.strip().upper(),
                        "window_start": window_start,
                    },
                ).mappings().first()
                continuity = (
                    _load_continuity(connection, row)
                    if row is not None
                    else None
                )
        finally:
            engine.dispose()
        if row is None:
            raise LookupError("microstructure_window_not_found")
        observation = _row_to_observation(
            row,
            collector_freshness=collector_freshness,
            continuity=continuity or {},
        )
        policy = MicrostructureEligibilityPolicy(
            min_bbo_samples=args.min_bbo_samples,
            min_books5_samples=args.min_books5_samples,
            min_trade_count=args.min_trade_count,
            min_oi_samples=args.min_oi_samples,
            max_window_age_seconds=(
                args.max_latest_window_age_seconds
                if window_start is None
                else None
            ),
        )
        report = evaluate_microstructure_window(
            observation,
            policy=policy,
            evaluated_at=evaluated_at,
        )
        _write_new_report(args.output, report.to_dict())
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "eligible_for_research": report.eligible_for_research,
                "evidence_fingerprint": report.evidence_fingerprint,
                "output": str(args.output),
                "reason_codes": list(report.reason_codes),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report.eligible_for_research else 1


if __name__ == "__main__":
    raise SystemExit(main())
