# Task 212: Review Follow-up for Risk View Removal and Operator Cache Robustness

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Business objectives and boundaries
- Review the recent `风险恢复` UI removal changes.
- Fix any concrete correctness or robustness issues introduced or exposed by the change.
- Keep the risk page behavior unchanged: the `独立双书恢复快照` card remains removed.

## Module responsibilities and domain model
- `aats/api/static/modules/views/risk-view.js`
  - Risk page presentation only; no recovery-domain behavior changes in this follow-up.
- `aats/services/operator/query_service.py`
  - Query-side cache helpers must remain safe even for partially constructed instances used by tests and read-only utility paths.
- `tests/unit/test_operator_position_states.py`
  - Locks the partially initialized `OperatorQueryService` cache lifecycle behavior.

## Input/output interfaces
- No API shape changes.
- No dashboard rendering changes beyond the already completed card removal.

## Database schema / tables / indexes / constraints
- No database changes.

## Transactions, Consistency, Concurrency
- Cache helper initialization must be consistent across `_cached`, `_cached_ttl`, and `_invalidate_cache`.
- No transactional semantics change.

## Authorization, Authentication, Data Security
- No auth or security changes.

## Error Handling and Idempotency
- `_invalidate_cache()` should be safe to call on partially constructed `OperatorQueryService` instances.
- Repeated invalidation remains idempotent.

## State Transition and Lifecycle
- No recovery lifecycle changes.
- Only cache lifecycle robustness is improved.

## Caching and Performance
- Negligible overhead from centralized lazy cache-state initialization.
- No change to cache keys or retention semantics.

## Logging, Monitoring, Auditing
- No logging or audit changes.

## Testing Strategy
- Add/update unit coverage for partially constructed query-service cache invalidation.
- Run lint, full unit suite, and the narrowest affected integration test.

## Migration, Rollback, Compatibility
- Fully backward compatible.
- Rollback is a small revert if necessary.

## Configuration and Environment Isolation
- No config changes.

## Code Organization and Dependencies
- Keep the fix local to `query_service.py` and the corresponding unit test.

## Documentation and Operations Manual
- This SOW documents the review follow-up scope.

## Deployment and Acceptance Criteria
- `风险恢复`页继续不显示 `独立双书恢复快照`。
- `OperatorQueryService` cache helpers remain safe under `__new__()` test construction paths.
- Validation commands pass.
