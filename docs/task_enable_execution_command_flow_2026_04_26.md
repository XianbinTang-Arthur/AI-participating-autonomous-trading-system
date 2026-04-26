# Enable Execution Command Flow SOW - 2026-04-26

## Business objectives and boundaries

Enable the live derivatives runtime command-flow switch so new executable orders persist submit commands before venue submission. This addresses the current directional pre-submit reliability gap without changing strategy, risk limits, symbol, venue, provider, promotion, release, or tuning behavior.

## Module responsibilities and domain model

`deploy/wsl2-dev/docker-compose.aats.derivatives-live.yml` owns live compose-level non-secret runtime toggles. `OrderManager` creates local `CREATED` order state. `ExecutionOrderService` creates submit commands. `ExecutionCommandProcessor` drains persisted commands and calls the existing guarded submit executor.

## Input/output interfaces

Input is the derivatives-live compose overlay and existing environment layering. Output is an explicit `AATS_EXECUTION_COMMAND_FLOW_ENABLED=true` environment value for the five main AATS application containers.

## Database schema / tables / indexes / constraints

No schema, index, migration, or table change. Existing `execution_commands`, `execution_orders`, and `order_states` tables are used.

## Transactions, Consistency, Concurrency

Command-flow uses the existing transactional order-state-and-command path when the outbox command repo is available. This reduces the interruption window where an order can exist as `CREATED` without submit-command truth.

## Authorization, Authentication, Data Security

No credentials, tokens, API keys, passwords, or connection strings are changed or exposed. The new flag is non-secret.

## Error Handling and Idempotency

Existing command idempotency keys and processor retry semantics remain unchanged. Recovery/operator/financial convergence toggles remain off in this task, so existing stranded orders are not auto-reconciled or force-upgraded into an operator-control workflow.

## State Transition and Lifecycle

Future executable orders should move through persisted submit-command truth before venue submission. The already-stranded `CREATED` directional order remains a separate recovery/reconciliation item.

## Caching and Performance

Adds the existing command processor loop to the execution runtime when enabled. No cache schema or polling interval change.

## Logging, Monitoring, Auditing

Runtime truth should show command-flow as enabled after deploy. If a future order is still missing a command, it should be classified as a command persistence defect rather than a disabled-flow configuration issue.

## Testing Strategy

Add a unit guard proving derivatives-live compose explicitly enables command-flow. Re-run focused command-flow runtime tests, lint, full unit tests, and a WSL integration command-flow test.

## Migration, Rollback, Compatibility

Rollback is reverting this compose/test/doc change and redeploying with `scripts/deploy.sh`. Existing database rows remain compatible.

## Configuration and Environment Isolation

The switch is set in the tracked derivatives-live compose overlay, not in untracked secret env files. Recovery ledger, operator ledger, and financial convergence remain disabled.

## Code Organization and Dependencies

No new dependencies. Changes are limited to compose overlay, tests, and this SOW.

## Documentation and Operations Manual

Operators should treat the existing stranded order as unresolved recovery work. This task only makes new order submission truth deterministic.

## Deployment and Acceptance Criteria

Acceptance criteria:

1. derivatives-live overlay contains `AATS_EXECUTION_COMMAND_FLOW_ENABLED: "true"`.
2. Focused command-flow tests pass.
3. Post-deploy runtime truth shows command-flow enabled and no longer reports `execution_command_flow_disabled_direct_submit_interruption_window`.
