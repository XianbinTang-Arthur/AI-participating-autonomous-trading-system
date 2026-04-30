# AI Runtime Effective Mode Truth Bridge SOW

## Business Objectives and Boundaries

Build a read-only runtime truth surface that separates configured AI targets from effective runtime mode, manual override state, provider configured/active state, shadow enablement, and strategy profile auto-control. The surface must explicitly classify auth-gated runtime evidence instead of treating missing authenticated data as baseline mode or active AI timeout.

This task does not change provider behavior, AI decisions, strategy logic, risk gates, execution paths, symbols, venues, timeframes, release gates, or tuning.

## Module Responsibilities and Domain Model

- `scripts/runtime_truth_report.py` owns the no-secret runtime truth projection.
- Gateway `/ai/runtime` and `/dashboard/bundle` are read-only evidence sources.
- `ai_runtime_effective_mode_truth` is a derived truth object, not an authority that mutates live state.

## Input/Output Interfaces

Inputs:
- Gateway health and AI runtime read endpoints.
- Existing dashboard bundle probe.
- Existing runtime truth report object.

Outputs:
- A top-level `ai_runtime_effective_mode_truth` object.
- Projected `runtime.live_runtime_facts` keys for AI runtime truth status, auth gate, configured/effective mode, manual override, provider, shadow, profile auto-control, and timeout blocker classification.

## Database Schema / Tables / Indexes / Constraints

No schema, table, index, or constraint changes.

## Transactions, Consistency, Concurrency

No transactions are opened by this change. The report consumes point-in-time HTTP evidence and existing runtime truth inputs.

## Authorization, Authentication, Data Security

The bridge must not bypass authentication. If AI runtime endpoints are auth-gated, it records `operator_auth_required` as evidence and marks effective runtime facts as unknown. It must not print credentials, tokens, API keys, database passwords, or full connection strings.

## Error Handling and Idempotency

HTTP failures are classified as missing runtime evidence. Auth-required responses are classified separately from transport failures. The report is idempotent and read-only.

## State Transition and Lifecycle

No runtime lifecycle state is changed. This task only improves truth-chain semantics around AI runtime evidence.

## Caching and Performance

Adds one bounded read-only probe to `/ai/runtime` using the same short timeout pattern as existing gateway probes.

## Logging, Monitoring, Auditing

The new truth object becomes an audit surface for future Navigator and PM Loop decisions. It distinguishes:
- configured target
- effective runtime mode
- manual override
- provider configured
- provider active
- shadow enabled
- profile auto-control effective
- AI timeout blocker classification

## Testing Strategy

Add unit tests for:
- auth-gated AI runtime evidence
- verified AI runtime evidence
- live runtime fact projection

## Migration, Rollback, Compatibility

No migration is required. Rollback is a normal git revert plus deployment through `scripts/deploy.sh --profile derivatives-live --skip-commit`.

## Configuration and Environment Isolation

No new environment variables are added. The live environment remains OKX + BTC-USDT-SWAP with `none_verified` shadow benchmark until independently verified.

## Code Organization and Dependencies

Keep implementation in `scripts/runtime_truth_report.py`; no new dependencies.

## Documentation and Operations Manual

Operators should treat auth-gated AI runtime truth as unknown, not as baseline-only and not as active provider failure. AI timeout is an active blocker only when a runtime provider path is verified active and timeout evidence exists.

## Deployment and Acceptance Criteria

Acceptance criteria:
- Runtime truth includes `ai_runtime_effective_mode_truth`.
- Auth-gated endpoints produce explicit auth-gated status and `operator_authenticated_runtime_read_access` as the smallest missing field.
- Verified endpoint payloads project configured/effective/manual/provider/shadow/profile fields.
- Tests pass.
- Deployment, if performed, uses only `scripts/deploy.sh --profile derivatives-live --skip-commit`.
