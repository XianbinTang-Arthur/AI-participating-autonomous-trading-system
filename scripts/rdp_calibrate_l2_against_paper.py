#!/usr/bin/env python3
"""Calibrate an L2 replay artifact against read-only local paper lifecycle facts."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from collections import defaultdict
from decimal import Decimal
from typing import Any, Mapping

from sqlalchemy import create_engine, text

_SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from aats.data_platform.execution_realism.simulation_calibration import (  # noqa: E402
    ExecutionCalibrationPolicy,
    ObservedCommand,
    ObservedFill,
    ObservedPaperOrder,
    ObservedStateTransition,
    PredictedExecution,
    calibrate_l2_against_paper_lifecycle,
)


_ORDERS = text(
    """
    SELECT order_id, symbol, side, requested_qty, state, source_system,
           created_at, updated_at
    FROM execution_orders
    WHERE order_id = ANY(:order_ids)
      AND source_system = :source_system
    ORDER BY created_at, order_id
    """
)
_TRANSITIONS = text(
    """
    SELECT order_id, from_state, to_state, created_at
    FROM execution_order_state_history
    WHERE order_id = ANY(:order_ids)
    ORDER BY order_id, id
    """
)
_COMMANDS = text(
    """
    SELECT order_id, command_type, state, created_at, updated_at
    FROM execution_commands
    WHERE order_id = ANY(:order_ids)
    ORDER BY order_id, created_at, command_id
    """
)
_FILLS = text(
    """
    SELECT fill_id, order_id, fill_qty, fill_price, fee_amount,
           exchange_ts, ingestion_ts
    FROM execution_fills
    WHERE order_id = ANY(:order_ids)
    ORDER BY order_id, ingestion_ts, fill_id
    """
)


def _load_l2_predictions(path: pathlib.Path) -> tuple[str, tuple[PredictedExecution, ...]]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, Mapping):
        raise ValueError("l2_evidence_must_be_mapping")
    fingerprint = str(payload.get("evidence_fingerprint", "")).strip()
    results = payload.get("results")
    if not fingerprint.startswith("l2_") or not isinstance(results, list):
        raise ValueError("l2_evidence_identity_or_results_invalid")
    predictions: list[PredictedExecution] = []
    for row in results:
        if not isinstance(row, Mapping):
            raise ValueError("l2_evidence_result_invalid")
        predictions.append(
            PredictedExecution(
                order_id=str(row.get("order_id", "")),
                target_quantity=Decimal(str(row.get("target_quantity", ""))),
                filled_quantity=Decimal(str(row.get("filled_quantity", ""))),
                average_fill_price=(
                    Decimal(str(row["average_fill_price"]))
                    if row.get("average_fill_price") is not None
                    else None
                ),
                fee_bps_weighted=(
                    float(row["fee_bps_weighted"])
                    if row.get("fee_bps_weighted") is not None
                    else None
                ),
            )
        )
    if not predictions:
        raise ValueError("l2_evidence_results_empty")
    return fingerprint, tuple(predictions)


def _group(rows: list[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["order_id"])].append(row)
    return grouped


def _build_observed_orders(
    order_rows: list[Mapping[str, Any]],
    transition_rows: list[Mapping[str, Any]],
    command_rows: list[Mapping[str, Any]],
    fill_rows: list[Mapping[str, Any]],
) -> tuple[ObservedPaperOrder, ...]:
    transitions = _group(transition_rows)
    commands = _group(command_rows)
    fills = _group(fill_rows)
    return tuple(
        ObservedPaperOrder(
            order_id=str(row["order_id"]),
            symbol=str(row["symbol"]),
            side=str(row["side"]),
            requested_quantity=row["requested_qty"],
            state=str(row["state"]),
            source_system=str(row["source_system"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            transitions=tuple(
                ObservedStateTransition(
                    from_state=item["from_state"],
                    to_state=str(item["to_state"]),
                    created_at=item["created_at"],
                )
                for item in transitions[str(row["order_id"])]
            ),
            commands=tuple(
                ObservedCommand(
                    command_type=str(item["command_type"]),
                    state=str(item["state"]),
                    created_at=item["created_at"],
                    updated_at=item["updated_at"],
                )
                for item in commands[str(row["order_id"])]
            ),
            fills=tuple(
                ObservedFill(
                    fill_id=str(item["fill_id"]),
                    quantity=item["fill_qty"],
                    price=item["fill_price"],
                    fee_amount=item["fee_amount"],
                    exchange_ts=item["exchange_ts"],
                    ingestion_ts=item["ingestion_ts"],
                )
                for item in fills[str(row["order_id"])]
            ),
        )
        for row in order_rows
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--l2-evidence", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--expected-source-system", default="paper_local")
    parser.add_argument("--min-matched-orders", type=int, default=20)
    parser.add_argument("--max-fill-ratio-mae", type=float, default=0.20)
    parser.add_argument("--max-price-error-bps-mean", type=float, default=10.0)
    parser.add_argument("--max-fee-error-bps-mean", type=float, default=1.0)
    parser.add_argument("--max-command-to-terminal-p95-ms", type=float, default=5_000.0)
    args = parser.parse_args(argv)

    database_url = os.environ.get("AATS_LIVE_DB_URL_RDP", "").strip()
    if not database_url:
        print("ERROR: AATS_LIVE_DB_URL_RDP is required", file=sys.stderr)
        return 2
    try:
        l2_fingerprint, predictions = _load_l2_predictions(args.l2_evidence)
        order_ids = [prediction.order_id for prediction in predictions]
        engine = create_engine(
            database_url,
            pool_pre_ping=True,
            connect_args={"options": "-c default_transaction_read_only=on"},
        )
        try:
            with engine.connect() as connection:
                params = {
                    "order_ids": order_ids,
                    "source_system": args.expected_source_system,
                }
                order_rows = connection.execute(_ORDERS, params).mappings().all()
                transition_rows = connection.execute(
                    _TRANSITIONS, {"order_ids": order_ids}
                ).mappings().all()
                command_rows = connection.execute(
                    _COMMANDS, {"order_ids": order_ids}
                ).mappings().all()
                fill_rows = connection.execute(
                    _FILLS, {"order_ids": order_ids}
                ).mappings().all()
        finally:
            engine.dispose()
        observed = _build_observed_orders(
            order_rows,
            transition_rows,
            command_rows,
            fill_rows,
        )
        report = calibrate_l2_against_paper_lifecycle(
            observed_orders=observed,
            predicted_executions=predictions,
            l2_execution_evidence_fingerprint=l2_fingerprint,
            policy=ExecutionCalibrationPolicy(
                expected_source_system=args.expected_source_system,
                min_matched_orders=args.min_matched_orders,
                max_fill_ratio_mae=args.max_fill_ratio_mae,
                max_price_error_bps_mean=args.max_price_error_bps_mean,
                max_fee_error_bps_mean=args.max_fee_error_bps_mean,
                max_command_to_terminal_p95_ms=args.max_command_to_terminal_p95_ms,
            ),
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(report.to_dict(), handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "passed": report.passed,
                "matched_order_count": report.matched_order_count,
                "evidence_fingerprint": report.evidence_fingerprint,
                "reason_codes": list(report.reason_codes),
                "output": args.output.as_posix(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
