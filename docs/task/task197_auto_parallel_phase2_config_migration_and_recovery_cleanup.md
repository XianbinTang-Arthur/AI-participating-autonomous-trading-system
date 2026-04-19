# Task 197 - Auto Parallel Phase 2 Config Migration And Recovery Cleanup

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Business objectives and boundaries
- Introduce the new config key `strategy_sleeve_auto_execution_enabled` while the old compatibility key still existed during that phase. The old key has since been removed.
- Emit explicit deprecation warnings when startup still relies on the old key.
- Preserve phase 1 permission/budget/composition semantics without changing allocator route actions.
- Fix the two repository-wide failing unit tests that are unrelated to phase 1 logic but currently keep the full unit suite from going green.

## Module responsibilities and domain model
- `AATSSettings`: resolve the effective sleeve auto execution switch from the new key first, then fall back to the deprecated key.
- `config.py`: emit startup warnings for deprecated config usage.
- `query_service.py` / `auth_routes.py` / `strategy-view.js`: expose the new key while preserving the old key for compatibility.
- `recovery_posture.py`: clear stale review / only-reduce state after a clean reconciliation.
- `query_service.py`: preserve raw invalid transition details in operator exception summaries.

## Input/output interfaces
- Input:
  - YAML / env settings with `strategy_sleeve_auto_execution_enabled`
  - existing recovery / operator payloads
- Output:
  - effective runtime setting value
  - startup deprecation warning when old key is used alone
  - runtime payloads exposing both old and new keys during migration
  - corrected operator transition summary ordering/details
  - corrected recovery posture cleanup after clean reconciliation

## Database schema / tables / indexes / constraints
- No schema changes.

## Transactions, Consistency, Concurrency
- No transactional behavior changes.
- Config normalization remains deterministic during settings validation.

## Authorization, Authentication, Data Security
- No auth changes.

## Error Handling and Idempotency
- Old/new config keys resolve to one effective boolean.
- Clean reconciliation should deterministically clear stale review-only leftovers.

## State Transition and Lifecycle
- Runtime continues to support old config while preferring new config.
- Operator transition exception summary must keep the invalid transition’s raw prior/current state details.

## Caching and Performance
- No meaningful performance impact.

## Logging, Monitoring, Auditing
- Startup now warns when the deprecated key is the effective source.
- Runtime configured parameters include both old and new keys during migration.

## Testing Strategy
- Add settings/startup tests for new key precedence and deprecated-key warnings.
- Keep phase 1 control tests intact.
- Re-run the full unit suite and targeted integration tests.

## Migration, Rollback, Compatibility
- Phase 2 is dual-read compatible.
- Existing configs keep working.
- Rollback is straightforward: remove the new key and deprecation warning, keep the old key only.

## Configuration and Environment Isolation
- Managed profile YAMLs move to the new key.
- Manual overrides using the old key still work but emit a startup warning.

## Code Organization and Dependencies
- No new external dependencies.

## Documentation and Operations Manual
- Operators should understand that the old `strategy_sleeve_auto_parallel_enabled` path has been retired and only `strategy_sleeve_auto_execution_enabled` remains valid.

## Deployment and Acceptance Criteria
- New key works and takes precedence over old key.
- Startup warns when old key is used alone.
- Full unit suite returns to the existing repo baseline or better.
