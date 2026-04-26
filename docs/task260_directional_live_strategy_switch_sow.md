# Task260: Directional Live Strategy Switch

## Business Objectives and Boundaries
- Objective: switch the derivatives live strategy family from independent to directional per operator instruction.
- Boundary: configuration-only runtime behavior change. No strategy algorithm, risk engine, execution gate, kill switch, schema, provider, symbol, venue, release, promotion, or tuning logic changes.
- Fixed trading scope remains OKX + BTC-USDT-SWAP.

## Module Responsibilities and Domain Model
- `configs/strategy_profiles/derivatives_live.yaml` owns the derivatives live strategy profile.
- `strategy_family_active=directional` fixes the primary strategy family.
- `strategy_family_auto_selection_enabled=false` keeps automatic family switching disabled.
- Independent family and independent overlay live cutover are disabled so they cannot shadow or override the fixed directional selection.

## Input / Output Interfaces
- Input: managed derivatives live profile loaded through existing config loader.
- Output: runtime settings select directional as the configured active family.
- Public APIs and payload schemas remain unchanged.

## Database Schema / Tables / Indexes / Constraints
- No schema, table, index, or constraint changes.
- No migration is required.

## Transactions, Consistency, Concurrency
- No transaction behavior changes.
- Runtime consistency depends on standard deploy restart loading the updated profile.

## Authorization, Authentication, Data Security
- No credential, token, database URL, or API key changes.
- Deployment continues through `scripts/deploy.sh`.

## Error Handling and Idempotency
- Re-loading the profile is idempotent.
- If deploy fails, existing container state should be handled by the standard deployment rollback/retry path.

## State Transition and Lifecycle
- This changes live strategy selection semantics from independent carrier to fixed directional.
- It does not bypass risk engine, execution gates, kill switch, truth chain, or reconciliation controls.

## Caching and Performance
- No cache design changes.
- Restarted services reload the profile through the normal bootstrap path.

## Logging, Monitoring, Auditing
- Existing runtime truth and operator surfaces should show configured active family as directional after deploy.
- Audit trail is the git commit plus deploy smoke.

## Testing Strategy
- Update managed profile tests to assert derivatives live is directional.
- Update the narrow runtime integration profile test to assert a managed derivatives live cycle remains directional.
- Run focused tests, required `aats/` lint, full unit suite, and post-deploy runtime truth smoke.

## Migration, Rollback, Compatibility
- Rollback: revert this commit and redeploy through `scripts/deploy.sh`.
- Backward compatibility: config keys remain present; independent-specific parameters remain in the file but disabled.

## Configuration and Environment Isolation
- Only derivatives live profile is changed.
- Spot and non-live derivatives profiles are not changed.

## Code Organization and Dependencies
- No new dependencies.
- No production Python code changes are required.

## Documentation and Operations Manual
- Operators should use `directional`, not `direction`, because `directional` is the project enum value.
- Do not re-enable independent live cutover unless explicitly reverting this switch.

## Deployment and Acceptance Criteria
- Acceptance:
  1. `derivatives_live` managed profile loads with `strategy_family_active=directional`.
  2. Family auto-selection remains disabled.
  3. Independent family/live execution and independent hedge overlay are disabled.
  4. Focused tests and deployment smoke pass.
  5. Post-deploy runtime truth has no deployment/git blockers.
- Deployment: commit, push, deploy via `scripts/deploy.sh --profile derivatives-live --skip-commit`.
