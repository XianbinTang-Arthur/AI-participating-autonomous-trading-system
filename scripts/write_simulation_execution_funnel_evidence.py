#!/usr/bin/env python3
"""Write immutable, read-only derivatives simulation execution-funnel evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sys
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

_SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from aats.data_platform.governance._atomic_io import immutable_json_write  # noqa: E402
from aats.data_platform.operations.execution_funnel import (  # noqa: E402
    EXECUTION_FUNNEL_TOPICS,
    evaluate_simulation_execution_funnel,
    parse_simulation_deployment_identity,
)


_EVENT_ROWS = text(
    """
    SELECT sequence_id, event_id, created_at, topic, decision_id, symbol,
           product_type, margin_mode, payload
    FROM event_store
    WHERE created_at >= :window_start
      AND created_at < :window_end
      AND topic = ANY(:topics)
      AND symbol = :symbol
    ORDER BY sequence_id
    LIMIT :row_limit
    """
)
_ORDER_ROWS = text(
    """
    SELECT order_id, decision_id, state, source_system, created_at, updated_at
    FROM execution_orders
    WHERE created_at >= :window_start
      AND created_at < :window_end
      AND symbol = :symbol
    ORDER BY created_at, order_id
    """
)
_FILL_ROWS = text(
    """
    SELECT fill_id, order_id, decision_id, source_system, created_at,
           ingestion_ts
    FROM execution_fills
    WHERE created_at >= :window_start
      AND created_at < :window_end
      AND symbol = :symbol
    ORDER BY created_at, fill_id
    """
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment-evidence", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--window-end", type=str)
    parser.add_argument("--symbol", default="BTC-USDT-SWAP")
    parser.add_argument("--max-new-risk-notional", required=True)
    parser.add_argument("--min-nonzero-targets", type=int, default=100)
    parser.add_argument("--settle-delay-seconds", type=int, default=30)
    parser.add_argument("--max-events", type=int, default=100_000)
    parser.add_argument("--database-url-env", default="AATS_DATABASE_URL")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_events <= 0:
        print("ERROR: max_events_must_be_positive", file=sys.stderr)
        return 2
    database_url = os.environ.get(args.database_url_env, "").strip()
    if not database_url:
        print("ERROR: database_url_environment_variable_missing", file=sys.stderr)
        return 2
    try:
        deployment_bytes = args.deployment_evidence.read_bytes()
        deployment_payload = json.loads(deployment_bytes)
        if not isinstance(deployment_payload, Mapping):
            raise ValueError("deployment_evidence_must_be_mapping")
        deployment = parse_simulation_deployment_identity(
            deployment_payload,
            evidence_fingerprint=hashlib.sha256(deployment_bytes).hexdigest(),
        )
        window_end = (
            _parse_datetime(args.window_end)
            if args.window_end
            else datetime.now(UTC)
        )
        max_new_risk_notional = _parse_decimal(args.max_new_risk_notional)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    params = {
        "window_start": deployment.generated_at,
        "window_end": window_end,
        "topics": list(EXECUTION_FUNNEL_TOPICS),
        "symbol": args.symbol,
        "row_limit": args.max_events + 1,
    }
    try:
        engine = create_engine(
            database_url,
            pool_pre_ping=True,
            connect_args={"options": "-c default_transaction_read_only=on"},
        )
    except (SQLAlchemyError, ValueError):
        print("ERROR: database_engine_initialization_failed", file=sys.stderr)
        return 2
    try:
        with engine.connect() as connection:
            event_rows = connection.execute(_EVENT_ROWS, params).mappings().all()
            if len(event_rows) > args.max_events:
                raise ValueError("execution_funnel_event_limit_exceeded")
            order_rows = connection.execute(_ORDER_ROWS, params).mappings().all()
            fill_rows = connection.execute(_FILL_ROWS, params).mappings().all()
    except SQLAlchemyError:
        print("ERROR: database_query_failed", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    finally:
        engine.dispose()

    try:
        evidence = evaluate_simulation_execution_funnel(
            deployment=deployment,
            window_end=window_end,
            symbol=args.symbol,
            max_new_risk_notional=max_new_risk_notional,
            min_nonzero_targets=args.min_nonzero_targets,
            settle_delay_seconds=args.settle_delay_seconds,
            event_rows=event_rows,
            order_rows=order_rows,
            fill_rows=fill_rows,
        )
        digest = immutable_json_write(evidence.to_dict(), args.output)
    except (FileExistsError, OSError, ValueError) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "output": args.output.as_posix(),
                "sha256": digest,
                "status": evidence.status,
                "passed": evidence.passed,
                "mature_nonzero_target_count": (
                    evidence.mature_nonzero_target_count
                ),
                "order_count": evidence.order_count,
                "fill_count": evidence.fill_count,
                "reason_codes": list(evidence.reason_codes),
                "evidence_fingerprint": evidence.evidence_fingerprint,
                "production_ready": evidence.production_ready,
                "trading_ready": evidence.trading_ready,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return {"PASS": 0, "FAIL": 1, "UNKNOWN": 2}[evidence.status]


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("window_end_must_be_timezone_aware")
    return parsed.astimezone(UTC)


def _parse_decimal(value: Any) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("max_new_risk_notional_invalid") from exc
    if not parsed.is_finite():
        raise ValueError("max_new_risk_notional_must_be_finite")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
