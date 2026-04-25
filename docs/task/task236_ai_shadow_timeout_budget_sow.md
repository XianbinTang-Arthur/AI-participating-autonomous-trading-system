# Task236 - AI Shadow Timeout Budget

## Business Objectives And Boundaries

Prevent auxiliary AI shadow assessment from consuming the live decision-cycle budget. The live AI decision path may still use the configured `ai_timeout_seconds`; shadow assessment is audit support and must fail soft if it cannot return quickly.

Out of scope: strategy thresholds, symbol, venue, strategy family, risk gates, kill switch, execution gates, promotion/release gates, and runtime timeframe plumbing.

## Module Responsibilities And Domain Model

`AIInferenceService.assess` owns the primary live AI assessment. `_maybe_record_shadow_assessment` owns the optional AI shadow assessment. The shadow result supports comparison and audit only; it must not block the main decision lifecycle.

## Input/Output Interfaces

Input is the existing `DecisionContext`, `BaselineAssessment`, AI prompt, and provider response. Output remains an `AIMarketAssessment` for live assessment and, when enabled, an optional shadow `AIMarketAssessment`. Shadow timeout produces a fallback shadow assessment with `fallback_reason=ai_shadow_timeout`.

## Database Schema / Tables / Indexes / Constraints

No schema, table, index, or constraint changes.

## Transactions, Consistency, Concurrency

No transaction boundary changes. The shadow provider call remains awaited inside the AI service, but with a short independent timeout so it cannot consume the full decision-cycle budget after the primary AI call.

## Authorization, Authentication, Data Security

No credential, authentication, authorization, or secret handling change. Validation must not print API keys or DB URLs.

## Error Handling And Idempotency

Shadow timeout is handled as `ai_shadow_timeout`. Other shadow failures remain `ai_shadow_fallback`. Both are idempotent fail-soft outcomes and do not affect the primary AI assessment or trading gates.

## State Transition And Lifecycle

No order, fill, portfolio, strategy profile, or deployment lifecycle state transition changes.

## Caching And Performance

The shadow call gets a fixed internal cap below the 30 second run-cycle timeout. This preserves budget for target construction, publish operations, and audit persistence.

## Logging, Monitoring, Auditing

The shadow fallback reason is machine-readable and distinguishes timeout from generic shadow fallback. Existing event publication carries the shadow assessment when available.

## Testing Strategy

Add a unit test proving a slow shadow provider call records `ai_shadow_timeout` while the primary assessment remains valid and non-fallback.

## Migration, Rollback, Compatibility

No migration. Rollback is reverting the code and test changes. Existing settings and profile files remain compatible.

## Configuration And Environment Isolation

No new environment variable or active parameter is introduced. The cap is internal to the AI service to avoid expanding the live parameter surface.

## Code Organization And Dependencies

Only `aats/services/ai_service/inference.py` and focused AI inference tests should change.

## Documentation And Operations Manual

This SOW is the operational record. Post-deploy review should check whether `decision_cycle_timeout` caused by shadow-path latency stops recurring.

## Deployment And Acceptance Criteria

Acceptance:
- A slow shadow assessment records `fallback_reason=ai_shadow_timeout`.
- Primary AI assessment still uses configured provider semantics.
- Decision cycles are not expected to fail solely because the shadow provider call is slow.
- No risk, execution, promotion, release, symbol, venue, or strategy family behavior changes.
