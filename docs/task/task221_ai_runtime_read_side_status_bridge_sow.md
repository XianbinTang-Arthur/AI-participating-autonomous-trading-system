# Task221 - AI runtime read-side status bridge

## Business objectives and boundaries
- Objective: make authenticated `GET /ai/runtime` on the gateway report the authoritative AI runtime state from the decision process when AI is enabled.
- Boundary: read-side status only. No strategy, symbol, venue, tuning, promotion, release, risk gate, kill switch, or execution behavior changes.

## Module responsibilities and domain model
- `aats.schemas.operator_command`: add a read-only AI command name for runtime status.
- `aats.bootstrap.config`: register the decision-role handler that returns `OperatorQueryService.ai_runtime()` locally.
- `aats.services.operator.runtime_queries` / `query_service`: expose an async authoritative read path that uses the existing AI command bridge when local `ai_service` is absent.
- `aats.api.routes`: make `/ai/runtime` use the async authoritative path.

## Input/output interfaces
- Input: authenticated gateway HTTP `GET /ai/runtime`.
- Output: same runtime status shape as `ai_service.status()` plus existing normalized fields; no secrets.
- Internal bridge command: `ai_runtime_status` with an empty payload.

## Database schema / tables / indexes / constraints
- No database schema changes.

## Transactions, consistency, concurrency
- The read command is side-effect free and uses the existing request/response bridge correlation id.
- Worker remains serial under the existing command bridge lock; acceptable because status calls are lightweight.

## Authorization, authentication, data security
- Existing route auth remains unchanged.
- Response must not include provider API keys, tokens, passwords, or env values.
- Bridge payload is empty and does not carry operator credentials.

## Error handling and idempotency
- If local `ai_service` exists, return local status.
- If gateway has no bridge client, preserve the existing stable stub.
- Bridge timeout/remote errors are surfaced by the API route as existing operator command errors.
- Read command is idempotent.

## State transition and lifecycle
- No runtime state transition.
- Does not alter manual override, degradation, recovery, profile activation, or risk state.

## Caching and performance
- `/ai/runtime` uses an uncached async authoritative read to avoid stale `provider=not_loaded` from gateway cache.
- Sync callers keep existing cached stub path.

## Logging, monitoring, auditing
- Existing `ai_command_*` bridge logs apply.
- No audit event is appended for read-only status.

## Testing Strategy
- Unit test bridge dispatch for `ai_runtime_status`.
- Unit test gateway async fallback uses `ai_command_client` and does not expose secrets.
- Run targeted tests, ruff, full unit suite if time allows.

## Migration, Rollback, Compatibility
- Backward compatible API shape.
- Rollback: revert this task commit and redeploy through `scripts/deploy.sh`.

## Configuration and Environment Isolation
- No new env vars.
- Works with current `AI_SELECTOR=DEEPSEEK|OPENAI|DISABLED` because it reads status only.

## Code Organization and Dependencies
- Reuse existing command bridge; no new dependency.

## Documentation and Operations Manual
- SOW records intent and acceptance criteria.

## Deployment and Acceptance Criteria
- `GET /ai/runtime` no longer reports `provider=not_loaded` when decision AI service is configured and reachable.
- Gateway health endpoints remain stable.
- Tests pass and no secrets are printed.
