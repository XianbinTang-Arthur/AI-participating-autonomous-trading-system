# Operator No-Trade Cause Surface SoW - 2026-04-25

## Business Objectives And Boundaries
Surface the deployed `no_trade_classification` field in operator-facing strategy and decision truth-chain views. The goal is to make no-order/no-fill decisions explain themselves with the machine-readable blocker instead of ambiguous narrative text.

Out of scope: strategy logic, risk gates, execution gates, AI provider/model behavior, symbol/venue/family expansion, tuning, promotion, release, schema changes, or runtime timeframe plumbing.

## Module Responsibilities And Domain Model
- `aats/api/static/modules/no-trade-display.js` owns frontend localization and compact rendering helpers for `no_trade_classification`.
- `aats/api/static/modules/detail-drawers.js` owns the decision detail drawer and adds a dedicated no-trade cause card.
- `aats/api/static/modules/views/strategy-view.js` owns the strategy page hero, workbench, and decision history summaries.

Domain model: `no_trade_classification` is a read-only operator truth payload emitted by the backend. It contains `classification`, `scope`, `is_no_trade`, order/fill counts, policy/risk blocker status, reason codes, and optional book runtime state evidence.

## Input/Output Interfaces
Input is the existing operator payload from `/decision/latest`, `/decision/recent`, or `/decision/{decision_id}`. Output is rendered HTML only. API payload shape is unchanged.

## Database Schema / Tables / Indexes / Constraints
No database schema, table, index, or constraint changes.

## Transactions, Consistency, Concurrency
No transaction or concurrency behavior changes. Rendering is deterministic from the already-loaded payload.

## Authorization, Authentication, Data Security
No auth behavior changes. The UI consumes already-authorized operator API payloads. No secrets, credentials, tokens, or DB URLs are read or displayed.

## Error Handling And Idempotency
Missing or partial `no_trade_classification` falls back to existing strategy copy. Unknown classification keys render as localized state/fallback text rather than raw broken UI.

## State Transition And Lifecycle
No trading lifecycle state transition changes. The UI distinguishes policy/risk blockers, runtime execution gaps, intended strategy signal/net-edge no-trade, and unknown sparse no-trade.

## Caching And Performance
No new network requests. Helper functions are pure string formatting and bounded to short arrays.

## Logging, Monitoring, Auditing
No logging changes. The rendered card exposes audit-relevant classifier facts already present in the payload.

## Testing Strategy
Add focused Node-render tests through `tests/integration/test_dashboard_ui.py` for the decision drawer and strategy view.

## Migration, Rollback, Compatibility
Backward compatible: if the field is absent, existing UI behavior remains. Rollback is reverting this UI helper and its consumers.

## Configuration And Environment Isolation
No configuration changes. Runtime AI configuration remains interpreted by `/ai/runtime`; this change does not re-enable provider paths.

## Code Organization And Dependencies
Add one small shared static module and import it from the two existing view modules. No external dependencies.

## Documentation And Operations Manual
Operators should treat AI timeout as an active blocker only when effective runtime evidence shows the AI/provider path is actually active.

## Deployment And Acceptance Criteria
Acceptance: operator strategy view and decision drawer show the no-trade classifier for no-trade samples; AI/provider timeout is not presented as current live blocker under `baseline_only` or manual-effective runtime; focused validation passes; no secrets are printed.
