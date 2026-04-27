# SOW: OKX hedge scale-in intent truth surface

## Business objectives and boundaries

Expose whether the OKX hedge-mode `scale_in_long` / `scale_in_short` semantic mismatch is an active runtime blocker or a historical residue. Scope is fixed to OKX + BTC-USDT-SWAP directional live carrier. This task must not change strategy, risk, execution submission behavior, provider behavior, symbols, venues, strategy families, release gates, promotion gates, or timeframe plumbing.

## Module responsibilities and domain model

- `scripts/runtime_truth_report.py`: read-only runtime truth aggregation and live fact projection.
- `execution_orders`, `order_states`, `execution_order_state_history`: historical/runtime evidence sources for `okx_leg_action_mismatch_with_position_intent`.
- `aats.schemas.execution.position_intent_matches_leg_intent`: canonical compatibility model that treats `scale_in_*` as lifecycle-specific open-leg intent in hedge mode.
- `aats.services.execution_engine.okx_adapter`: submission semantic gate that must use the canonical compatibility model.

## Input/output interfaces

Input is existing runtime code plus aggregate database facts from the gateway container environment. Output is a new `okx_hedge_scale_in_intent_truth` object and live facts such as active/historical mismatch counts and code-marker presence. No raw payload, database URL, token, API key, password, or full connection string is emitted.

## Database schema / tables / indexes / constraints

No schema change. Read-only aggregate queries touch `execution_orders`, `order_states`, and `execution_order_state_history`. Existing indexes on state, decision, symbol, and created/update timestamps remain unchanged.

## Transactions, consistency, concurrency

All database access is read-only and runs in one connection from the running gateway container. The report is a point-in-time truth sample and does not mutate trading state.

## Authorization, authentication, data security

The probe uses the gateway container's existing environment and never prints connection settings. Output is restricted to counts, timestamps, order ids, semantic fields, and statuses needed for diagnosis.

## Error handling and idempotency

Missing code markers, missing database truth, deployment mismatch, active recent mismatch, historical-only mismatch, and no-sample states are explicit statuses. Re-running the report is idempotent.

## State transition and lifecycle

This truth surface does not create or transition orders. It classifies whether prior `BLOCKED` records represent an active semantic failure or historical rows after code alignment.

## Caching and performance

Queries are bounded aggregate counts plus five latest sanitized rows. No caching changes are introduced.

## Logging, monitoring, auditing

The runtime truth output becomes the audit artifact for future PM loops. It separates current active blockers from historical evidence.

## Testing Strategy

Unit tests cover DB probe string inclusion, historical-only mismatch classification, active mismatch classification, and live fact projection. Existing OKX adapter/planner tests remain the behavioral source for submission compatibility.

## Migration, rollback, compatibility

No migration. Rollback is reverting the runtime truth report and tests. Historical payload rows remain untouched.

## Configuration and environment isolation

No new environment variables. The report uses the existing gateway container environment and fixed BTC-USDT-SWAP symbol default.

## Code Organization and Dependencies

Changes stay inside `scripts/runtime_truth_report.py` and unit tests. No new dependencies are added.

## Documentation and Operations Manual

Operators should treat `active_scale_in_intent_mismatch_after_alignment` as a current P0 execution semantic blocker. `historical_scale_in_intent_mismatch_no_recent_hits` means the mismatch exists in history but has not appeared recently after current code alignment.

## Deployment and Acceptance Criteria

Acceptance requires focused tests, `ruff check aats/ --fix`, full unit tests, clean git state before commit, and no live-order behavior change. Because this is read-only truth surface work, it is safe-readonly eligible for deploy through `scripts/deploy.sh --skip-commit` if committed and pushed.
