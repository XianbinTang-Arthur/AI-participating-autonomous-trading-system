# Directional Terminal Order No-Fill Explanation Truth Surface SOW

## Scope

- Task type: `truth-chain-implementation`.
- Target: read-only runtime truth surface for OKX `BTC-USDT-SWAP` directional decisions.
- Change: when a directional decision has order surfaces but every visible order is already terminal without fill, expose why fill/lifecycle evidence is not expected.
- Non-goals: no strategy logic, risk gate, execution behavior, provider path, symbol, venue, schema, release, promotion, tuning, or timeframe plumbing changes.

## Current Behavior

`scripts/runtime_truth_report.py` already classifies terminal no-fill order surfaces as `verified_terminal_order_no_fill_expected`, but the operator-facing projection only exposes the status and expected booleans. It does not expose the terminal state class, reason, source system, execution style, or position intent that explains why the decision is considered closed without fill.

## Acceptance Criteria

- Runtime truth includes a structured `terminal_no_fill_explanation` only when all visible order surfaces are terminal and no fill surface exists.
- The explanation includes classification, reason code, terminal states/statuses, source systems, execution styles, position intents, order counts, terminal counts, and fill-surface presence.
- `project_live_runtime_facts` projects the explanation for both latest decision and latest executable directional decision.
- Existing terminal no-fill behavior remains unchanged: order expected, fill not expected, lifecycle not expected, no missing field.
- Unit tests cover the new explanation and live-facts projection.

## Validation

- Run focused unit tests for `tests/unit/scripts/test_runtime_truth_report.py`.
- Run repository validation required for safe-readonly script/test/doc changes.
- Generate a fresh runtime truth report after deploy and confirm the new fields appear without active blockers.

## Rollback

Revert the script/test/doc commit. This removes only the read-only explanation fields and restores the previous truth surface; it does not affect live order behavior.
