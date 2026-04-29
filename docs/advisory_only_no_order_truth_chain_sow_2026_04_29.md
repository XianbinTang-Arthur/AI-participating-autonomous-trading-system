# Advisory-only No-order Truth Chain SOW - 2026-04-29

## Goal

Classify the latest approved, execution-compatible `directional` primary candidate that becomes `advisory_only` with `suppressed_after_approval` as a verified no-order expectation in the runtime truth report.

## Boundaries

- Read-only truth surface only: `scripts/runtime_truth_report.py`, focused tests, and automation artifacts.
- No strategy, risk, execution, provider, symbol, venue, schema, timeframe, release, promotion, tuning, or live order behavior changes.
- No database mutation and no OKX action.
- Do not treat configured AI target as effective runtime mode without runtime evidence.

## Inputs

- `artifacts/automation/runtime_truth_2026_04_29T02_55_49Z_pre.json`
- Latest decision primary candidate facts:
  - route action: `advisory_only`
  - execution behavior: `suppressed_after_approval`
  - approved and execution-compatible: true
  - requested delta non-zero, composed delta zero
  - current smallest missing field: `primary_candidate_order_expectation_classification`

## Output

Runtime truth should expose:

- `latest_decision_primary_candidate_order_expected=false`
- `latest_decision_primary_candidate_no_order_root_cause=primary_candidate_advisory_only_suppressed_after_approval`
- no `primary_candidate_order_expectation_classification` missing field for this condition

## Acceptance Criteria

- PASS: focused unit tests cover the advisory-only suppressed-after-approval classification.
- PASS: runtime truth post-check reports the new no-order root cause for the latest decision.
- PASS: no live order behavior or runtime-affecting path is changed.
- PASS: rollback is a simple revert of the documentation, report code, and tests.
