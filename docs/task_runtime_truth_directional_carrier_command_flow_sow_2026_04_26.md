# Runtime Truth Directional Carrier and Command Flow SOW - 2026-04-26

## Business objectives and boundaries
- Objective: make the runtime truth report project the current live carrier and execution command-flow state from live facts instead of stale or partial static assumptions.
- Boundary: read-only truth surface only; no strategy, risk, provider, schema, venue, symbol, execution, or live order behavior changes.

## Module responsibilities and domain model
- `scripts/runtime_truth_report.py` owns the no-secret runtime truth projection used by automation and PM loops.
- `database_truth.runtime_config` is the source for command-flow enablement because it is read inside the running gateway container.
- `database_truth.latest_decision.primary_family` is the best available live carrier evidence; latest executable directional decision is only a fallback.

## Input/output interfaces
- The runtime truth JSON gains stable live facts: `active_live_carrier`, `execution_command_flow_enabled`, and `execution_command_flow_flag_present`.
- `scope.live_carrier` is updated from live facts during report construction.
- Existing fields remain backward compatible.

## Database schema / tables / indexes / constraints
- No database schema, table, index, or constraint changes.

## Transactions, Consistency, Concurrency
- No transactions are introduced.
- The report remains read-only and aggregates already committed database/runtime facts.

## Authorization, Authentication, Data Security
- No credentials are read or printed.
- Existing gateway-container environment loading remains encapsulated inside the running process context.

## Error Handling and Idempotency
- Missing database facts degrade to `None` or `unknown_pending_database_truth`.
- Report generation remains idempotent for the same runtime state.

## State Transition and Lifecycle
- No state transition changes.
- The report only classifies live facts more accurately.

## Caching and Performance
- No new hot-path runtime cache or polling loop.
- The projection uses data already fetched by the existing database probe.

## Logging, Monitoring, Auditing
- Automation can now distinguish configured/static carrier text from effective live carrier evidence.
- Command-flow enablement is visible in the authoritative live runtime facts projection.

## Testing Strategy
- Unit tests cover command-flow projection, carrier inference, and scope update behavior.
- Runtime truth smoke verifies deployed output.

## Migration, Rollback, Compatibility
- No migration required.
- Rollback is a normal code revert and redeploy through `scripts/deploy.sh --skip-commit`.

## Configuration and Environment Isolation
- No config changes.
- No new symbol, venue, family, provider, release, promotion, tuning, or timeframe path is enabled.

## Code Organization and Dependencies
- Changes stay in `scripts/runtime_truth_report.py` and `tests/unit/scripts/test_runtime_truth_report.py`.
- No new dependency.

## Documentation and Operations Manual
- This SOW documents that live carrier must be derived from live facts, not heartbeat/static stale text.

## Deployment and Acceptance Criteria
- Acceptance: focused runtime truth tests pass, `ruff check aats/ --fix` passes, full unit tests pass, deployment via standard script succeeds, and post-deploy runtime truth reports `scope.live_carrier=directional` plus `live_runtime_facts.execution_command_flow_enabled=true`.
