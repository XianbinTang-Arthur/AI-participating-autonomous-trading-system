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


def _finding(
    *,
    scope_kind: str,
    scope_ref: str | None,
    finding_type: str,
    severity_class: str,
    reason_code: str,
    review_required: bool = False,
    halt_required: bool = False,
    blocks_resume: bool = False,
) -> dict:
    return {
        "reconciliation_id": "recon_stuck_submission_scope",
        "scope_kind": scope_kind,
        "scope_ref": scope_ref,
        "product_type": "derivatives",
        "margin_mode": "cross",
        "primary_symbol": "BTC-USDT-SWAP",
        "layer": "structural",
        "finding_type": finding_type,
        "severity_class": severity_class,
        "structural": True,
        "financial": False,
        "observational": False,
        "review_required": review_required,
        "only_reduce_required": False,
        "halt_required": halt_required,
        "blocks_resume": blocks_resume,
        "reason_code": reason_code,
        "details_json": {},
    }


def test_stuck_submission_allows_self_blocking_reconciliation() -> None:
    report = _report()

    assert OperatorQueryService._latest_reconciliation_allows_stuck_submission_resolution(
        report,
        client_order_id="cl_target",
    )


def test_stuck_submission_allows_self_blocking_order_findings() -> None:
    report = _report(
        findings=[
            _finding(
                scope_kind="order",
                scope_ref="cl_target",
                finding_type="exchange_open_order_missing",
                severity_class="review",
                reason_code="local_open_orders_diverge_from_exchange_open_orders",
                review_required=True,
                blocks_resume=True,
            ),
            _finding(
                scope_kind="order",
                scope_ref="cl_target",
                finding_type="order_state_unknown_on_exchange",
                severity_class="halt",
                reason_code="order_state_unknown_on_exchange",
                review_required=True,
                halt_required=True,
                blocks_resume=True,
            ),
            _finding(
                scope_kind="account",
                scope_ref=None,
                finding_type="derivatives_order_state_unknown_on_exchange",
                severity_class="halt",
                reason_code="derivatives_order_state_unknown_on_exchange",
                review_required=True,
                halt_required=True,
                blocks_resume=True,
            ),
        ],
    )

    assert OperatorQueryService._latest_reconciliation_allows_stuck_submission_resolution(
        report,
        client_order_id="cl_target",
    )


def test_stuck_submission_allows_self_blocking_structural_review_with_allowed_findings() -> None:
    report = _report(
        structural_review_required=True,
        mismatch_categories=[
            "local_open_order_divergence",
            "derivatives_order_state_unknown_on_exchange",
        ],
        mismatch_reasons=[
            "local_open_orders_diverge_from_exchange_open_orders",
            "derivatives_local_order_missing_from_exchange_open_order_view",
        ],
        findings=[
            _finding(
                scope_kind="account",
                scope_ref=None,
                finding_type="derivatives_order_state_unknown_on_exchange",
                severity_class="halt",
                reason_code="derivatives_order_state_unknown_on_exchange",
                review_required=True,
                halt_required=True,
                blocks_resume=True,
            ),
            _finding(
                scope_kind="order",
                scope_ref="cl_target",
                finding_type="exchange_open_order_missing",
                severity_class="review",
                reason_code="local_open_orders_diverge_from_exchange_open_orders",
                review_required=True,
                blocks_resume=True,
            ),
            _finding(
                scope_kind="order",
                scope_ref="cl_target",
                finding_type="order_state_unknown_on_exchange",
                severity_class="halt",
                reason_code="order_state_unknown_on_exchange",
                review_required=True,
                halt_required=True,
                blocks_resume=True,
            ),
        ],
    )

    assert OperatorQueryService._latest_reconciliation_allows_stuck_submission_resolution(
        report,
        client_order_id="cl_target",
    )


def test_stuck_submission_allows_nonblocking_historical_fill_lookback_gap() -> None:
    report = _report(
        fill_diff={
            "replayed": {},
            "exchange": {
                "missing_on_exchange": ["old_fill"],
                "unexpected_on_exchange": [],
            },
        },
        mismatch_reasons=[
            "local_open_orders_diverge_from_exchange_open_orders",
            "derivatives_local_order_missing_from_exchange_open_order_view",
            "local_exchange_fill_set_diverges_from_exchange_fill_set",
        ],
        findings=[
            _finding(
                scope_kind="fill",
                scope_ref="old_fill",
                finding_type="historic_orphan_fill",
                severity_class="info",
                reason_code="local_fill_older_than_exchange_lookback_window",
            ),
        ],
    )

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


def test_stuck_submission_rejects_allowed_finding_scoped_to_other_order() -> None:
    report = _report(
        structural_review_required=True,
        findings=[
            _finding(
                scope_kind="order",
                scope_ref="cl_other",
                finding_type="exchange_open_order_missing",
                severity_class="review",
                reason_code="local_open_orders_diverge_from_exchange_open_orders",
                review_required=True,
                blocks_resume=True,
            )
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


def test_stuck_submission_rejects_reconciliation_with_balance_diff() -> None:
    report = _report(
        balance_diff={
            "exchange": {
                "USDT": {"local": "100", "exchange": "90"},
            },
        },
    )

    assert not OperatorQueryService._latest_reconciliation_allows_stuck_submission_resolution(
        report,
        client_order_id="cl_target",
    )


def test_stuck_submission_rejects_reconciliation_with_structural_review_required() -> None:
    report = _report(structural_review_required=True)

    assert not OperatorQueryService._latest_reconciliation_allows_stuck_submission_resolution(
        report,
        client_order_id="cl_target",
    )


def test_stuck_submission_rejects_reconciliation_with_unrelated_category() -> None:
    report = _report(
        mismatch_categories=[
            "local_open_order_divergence",
            "local_position_divergence",
        ],
        mismatch_reasons=[
            "local_open_orders_diverge_from_exchange_open_orders",
            "local_position_differs_from_exchange_position",
        ],
    )

    assert not OperatorQueryService._latest_reconciliation_allows_stuck_submission_resolution(
        report,
        client_order_id="cl_target",
    )
