# Task 207 - Remove Deprecated Sleeve Auto Parallel Key

## Business objective and boundaries
- Remove `strategy_sleeve_auto_parallel_enabled` from the active `AATSSettings` input contract.
- Keep `strategy_sleeve_auto_execution_enabled` as the only supported config key for non-protective sleeve auto execution.
- Preserve runtime/operator compatibility metadata only as read-only historical context.
- Do not change allocator or execution behavior semantics in this task.

## Module responsibilities and domain model
- `aats.bootstrap.settings.AATSSettings`
  - Accept only `strategy_sleeve_auto_execution_enabled`.
  - Reject `strategy_sleeve_auto_parallel_enabled` from explicit dict/YAML validation inputs.
- `aats.bootstrap.config.load_settings`
  - Reject deprecated env input `AATS_STRATEGY_SLEEVE_AUTO_PARALLEL_ENABLED`.
- Operator/runtime payloads
  - Continue exposing the deprecated key name as historical metadata only.
  - Stop implying the deprecated key can still be used as an active config source.

## Input/output interfaces
- Supported input:
  - `strategy_sleeve_auto_execution_enabled`
- Removed input:
  - `strategy_sleeve_auto_parallel_enabled`
  - `AATS_STRATEGY_SLEEVE_AUTO_PARALLEL_ENABLED`
- Runtime output:
  - `strategy_sleeve_auto_execution_config_source` always resolves to the new key.
  - `strategy_sleeve_auto_execution_uses_deprecated_key` is always false.
  - compatibility metadata may still mention the removed key name, but not an active value.

## Database schema / tables / indexes / constraints
- No database changes.

## Transactions, consistency, concurrency
- No transaction or concurrency changes.

## Authorization, authentication, data security
- No auth changes.

## Error handling and idempotency
- Removed key usage must fail explicitly with a stable error code:
  - `strategy_sleeve_auto_parallel_enabled_has_been_removed_use_strategy_sleeve_auto_execution_enabled`
- This avoids silent ignore after removing the field from `AATSSettings`.

## State transition and lifecycle
- No state-machine changes.

## Caching and performance
- No meaningful runtime performance impact.

## Logging, monitoring, auditing
- Remove startup behavior that warned about deprecated-key usage, because that path is no longer valid.
- Runtime summaries continue to expose read-only compatibility metadata.

## Testing strategy
- Update unit tests that still pass the removed key.
- Add settings coverage that explicit dict/env usage of the removed key now fails.
- Update the narrow strategy runtime/dashboard integration expectations to the new primary key only.

## Migration, rollback, compatibility
- Breaking change by request.
- Compatibility window is now read-only metadata, not active input.
- Rollback path is to restore the old field and dual-read validator if needed.

## Configuration and environment isolation
- Repo-managed YAML already uses `strategy_sleeve_auto_execution_enabled`.
- Repo `.env*` files are scanned to confirm they do not still use the removed key.

## Code organization and dependencies
- Keep changes localized to settings/config/runtime payload/tests/docs.
- Avoid allocator or execution refactors.

## Documentation and operations manual
- Update generated managed config reference and historical task docs to reflect removal.

## Deployment and acceptance criteria
- `AATSSettings` no longer accepts `strategy_sleeve_auto_parallel_enabled`.
- `load_settings()` rejects `AATS_STRATEGY_SLEEVE_AUTO_PARALLEL_ENABLED`.
- All repo tests and docs use `strategy_sleeve_auto_execution_enabled`.
