from __future__ import annotations

from aats.schemas.common import utc_now
from aats.schemas.reconciliation import ReconciliationReport
from aats.services.operator.query_service import OperatorQueryService


def _report(**overrides) -> ReconciliationReport:
    payload = {
        "reconciliation_id": "recon_stuck_submission_scope",
        "as_of_ts": utc_now(),
        "product_type": "derivatives",
        "margin_mode": "cross",
        "allowed_symbols": ["BTC-USDT-SWAP"],
        "exchange_comparison_enabled": True,
        "order_diff": {
            "reconstructed": {},
            "exchange": {
                "missing_on_exchange": ["cl_target"],
                "unexpected_on_exchange": [],
                "status_mismatches": {},
            },
        },
        "fill_diff": {"replayed": {}, "exchange": {}},
        "balance_diff": {},
        "position_diff": {},
        "unknown_state_details": [
            {
                "kind": "order_state_unknown_on_exchange",
                "symbol": "BTC-USDT-SWAP",
                "order_key": "cl_target",
            }
        ],
        "severity": "HARD_MISMATCH",
        "review_required": True,
        "halt_required": True,
    }
    payload.update(overrides)
    return ReconciliationReport(**payload)


def test_stuck_submission_allows_self_blocking_reconciliation() -> None:
    report = _report()

    assert OperatorQueryService._latest_reconciliation_allows_stuck_submission_resolution(
        report,
        client_order_id="cl_target",
    )


def test_stuck_submission_rejects_reconciliation_with_unexpected_exchange_order() -> None:
    report = _report(
        order_diff={
            "reconstructed": {},
            "exchange": {
                "missing_on_exchange": ["cl_target"],
                "unexpected_on_exchange": ["cl_other"],
                "status_mismatches": {},
            },
        },
    )

    assert not OperatorQueryService._latest_reconciliation_allows_stuck_submission_resolution(
        report,
        client_order_id="cl_target",
    )


def test_stuck_submission_rejects_reconciliation_with_other_unknown_order() -> None:
    report = _report(
        unknown_state_details=[
            {
                "kind": "order_state_unknown_on_exchange",
                "symbol": "BTC-USDT-SWAP",
                "order_key": "cl_target",
            },
            {
                "kind": "order_state_unknown_on_exchange",
                "symbol": "BTC-USDT-SWAP",
                "order_key": "cl_other",
            },
        ],
    )

    assert not OperatorQueryService._latest_reconciliation_allows_stuck_submission_resolution(
        report,
        client_order_id="cl_target",
    )


def test_stuck_submission_rejects_reconciliation_with_unbooked_exchange_fill() -> None:
    report = _report(
        fill_diff={
            "replayed": {},
            "exchange": {
                "unexpected_on_exchange": ["fill_unknown"],
            },
        },
    )

    assert not OperatorQueryService._latest_reconciliation_allows_stuck_submission_resolution(
        report,
        client_order_id="cl_target",
    )
