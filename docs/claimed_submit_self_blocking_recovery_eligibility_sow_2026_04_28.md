# CLAIMED submit self-blocking recovery eligibility SOW (2026-04-28)

## Task

Allow the protected stuck-submission recovery gate to recognize a reconciliation report as self-blocking when every active structural finding is scoped to the target local order being absent from OKX, plus tolerated historical fills that are outside the OKX fill lookback window.

## Scope

This affects only the existing protected `resolve_stuck_submission` eligibility check. It does not change strategy logic, order submission, exchange adapters, risk gates, provider behavior, schemas, symbols, venues, or direct database writes.

## Input

The current claimed-submit blocker has an OKX absence operator confirmation, no local fills, no exchange order id, and no OKX open order, but the protected writer rejects it with `latest_reconciliation_not_clean` because the latest reconciliation sets `structural_review_required=true`.

## Output

`OperatorQueryService._latest_reconciliation_allows_stuck_submission_resolution` now treats `structural_review_required=true` as acceptable only when the findings already pass the existing self-blocking checks. A structural review flag with no allowed findings remains rejected.

## Validation

- Focused unit tests for the self-blocking reconciliation helper.
- Focused operator API integration test for resolving a stuck submission when the latest reconciliation is self-blocking and structural review is required.
- Full `ruff check aats/ --fix`.
- Full `pytest tests/unit/ -x -q`.

## Rollback

Revert the production-code change in `aats/services/operator/query_service.py` and the corresponding tests. This restores the previous conservative behavior where any `structural_review_required=true` reconciliation blocks protected stuck-submission recovery.
