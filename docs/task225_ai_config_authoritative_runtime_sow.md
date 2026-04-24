# Task 225 - AI Config Authoritative Runtime SOW

## Business Objectives And Boundaries
- Objective: make the AI config UI report the live AI operating mode and strategy profile auto-control state from the decision process, not the gateway-local AI stub.
- Boundary: read-model fix only. Do not change trading decisions, execution gates, risk rules, profile activation policy, or provider behavior.

## Module Responsibilities And Domain Model
- `aats/services/operator/query_service.py`: builds operator read payloads for AI overview and AI config summary.
- `aats/services/operator/runtime_queries.py`: owns local and authoritative AI runtime resolution.
- `aats/api/routes.py`: exposes direct HTTP AI read endpoints.
- `aats/api/auth_routes.py`: builds dashboard bundle panels consumed by the browser shell.
- Domain rule: in multi-process mode, gateway has no local `ai_service`; public UI reads must bridge to decision for authoritative AI status.

## Input/Output Interfaces
- Inputs: existing `/ai/runtime`, `/ai/overview`, `/ai-config/summary`, and `/dashboard/bundle` requests.
- Outputs: unchanged JSON schemas, but AI mode fields come from authoritative decision runtime where available.

## Database Schema / Tables / Indexes / Constraints
- No schema changes.

## Transactions, Consistency, Concurrency
- No writes. Dashboard bundle should reuse a single authoritative AI runtime fetch per request when multiple AI panels are requested.

## Authorization, Authentication, Data Security
- Preserve existing read/admin access checks.
- Do not expose secrets or environment values.

## Error Handling And Idempotency
- If the decision bridge is unavailable, existing `/ai/runtime` error behavior remains authoritative for direct routes.
- Dashboard bundle keeps panel-level error isolation.

## State Transition And Lifecycle
- No state transitions. This is a read-path correction.

## Caching And Performance
- Preserve dashboard bundle TTL/in-flight cache.
- Avoid duplicate AI command bridge calls within the same dashboard bundle request.

## Logging, Monitoring, Auditing
- No new logs required; existing endpoint timing remains available in bundle payload.

## Testing Strategy
- Add regression coverage that a gateway-like runtime without local `ai_service` still returns decision-process AI mode and auto-control state through direct summary and dashboard bundle panels.
- Run targeted tests plus standard lint/unit validation as time allows.

## Migration, Rollback, Compatibility
- Backward compatible JSON fields.
- Rollback by reverting the read-path changes.

## Configuration And Environment Isolation
- No config changes. Honors existing `ai_operating_mode` and `strategy_profile_auto_control_enabled`.

## Code Organization And Dependencies
- No new dependency.
- Keep helper methods close to existing `ai_overview` / `ai_config_summary` read model.

## Documentation And Operations Manual
- This SOW documents the operator UI mismatch and intended correction.

## Deployment And Acceptance Criteria
- Acceptance:
  - `/ai/runtime`, `/ai-config/summary`, and `/dashboard/bundle?panel=aiRuntime&panel=aiConfigModel` all agree on `ai_decision_maker` and auto-control enabled when the decision runtime reports them.
  - Browser AI Config page no longer falls back to baseline/manual because of gateway-local `ai_service_not_loaded`.
