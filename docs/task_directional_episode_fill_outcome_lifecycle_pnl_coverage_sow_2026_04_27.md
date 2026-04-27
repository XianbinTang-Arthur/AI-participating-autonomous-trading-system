# Directional Episode Fill Outcome Lifecycle PnL Coverage SOW

## Business Objectives And Boundaries
- Objective: explain `realized_pnl_usdt=null` on the latest filled directional episode by distinguishing open-position unrealized PnL from missing `fill_outcomes` lifecycle linkage.
- Boundary: read-only runtime truth only. No order-state mutation, strategy tuning, risk change, execution behavior change, schema migration, provider change, symbol/venue/family expansion, or timeframe plumbing change.

## Module Responsibilities And Domain Model
- `scripts/runtime_truth_report.py`: enrich recent directional episode attribution with PnL lifecycle coverage from `fill_outcomes`, `position_lots`, and `lot_events`.
- `tests/unit/scripts/test_runtime_truth_report.py`: verify open-position and missing-closed-outcome classifications.
- Domain model: a filled episode can be realized, partially covered, open/unrealized, or missing lifecycle PnL linkage.

## Input/Output Interfaces
- Input: `execution_fills`, `execution_orders`, `fill_outcomes`, `position_lots`, `lot_events`, and existing decision allocation payloads.
- Output: `directional_episode_attribution_truth.recent_decisions[].pnl_lifecycle` plus projected live runtime facts for latest episode lifecycle PnL status and coverage.

## Database Schema / Tables / Indexes / Constraints
- No schema change.
- Uses existing indexed fields: `fill_outcomes.fill_id`, `position_lots.source_fill_id`, and `lot_events.fill_id`.

## Transactions, Consistency, Concurrency
- Read-only probes against the running database.
- No transaction or trading-state mutation is introduced.
- The report reflects a point-in-time consistency snapshot.

## Authorization, Authentication, Data Security
- Uses the running gateway container environment implicitly.
- Does not print credentials, tokens, API keys, passwords, or connection strings.

## Error Handling And Idempotency
- Missing tables or missing evidence are surfaced as explicit `smallest_missing_field` values.
- Re-running the report is idempotent for a fixed DB state.

## State Transition And Lifecycle
- No lifecycle state is changed.
- Lifecycle evidence is classified:
  - realized PnL outcome complete;
  - partial outcome coverage;
  - open-position PnL not yet realized;
  - closed-lifecycle PnL outcome missing;
  - missing open-lot evidence.

## Caching And Performance
- Adds bounded aggregates to the existing 24-decision probe.
- No cache layer is added.

## Logging, Monitoring, Auditing
- Runtime truth now reports exact PnL lifecycle coverage status for the latest filled directional episode.
- This improves operator audit without manual SQL joins.

## Testing Strategy
- Focused unit tests for open-position classification and missing closed-lifecycle outcome classification.
- Full runtime truth unit test and full unit suite must pass.

## Migration, Rollback, Compatibility
- No migration.
- Backward compatible: existing fields remain unchanged; new `pnl_lifecycle` fields are additive.
- Rollback: revert the commit and redeploy with `bash scripts/deploy.sh --skip-commit`.

## Configuration And Environment Isolation
- No new configuration.
- Scope remains OKX + BTC-USDT-SWAP directional live canary.

## Code Organization And Dependencies
- No new dependencies.
- Changes stay in runtime truth script, tests, and this SOW.

## Documentation And Operations Manual
- Operators should inspect `latest_directional_episode_pnl_lifecycle_status` before interpreting null realized PnL as missing data or trading loss.

## Deployment And Acceptance Criteria
- Acceptance:
  - latest filled directional episode no longer presents null realized PnL without lifecycle explanation;
  - open-position unrealized PnL and missing closed-fill outcomes are distinct;
  - focused tests, ruff, full unit tests, deploy, and post-deploy runtime truth smoke pass;
  - post-deploy runtime truth reports no blocking findings.
