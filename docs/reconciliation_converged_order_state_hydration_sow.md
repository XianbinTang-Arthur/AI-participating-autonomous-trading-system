# Reconciliation Converged OrderState Hydration SOW

## Business Objectives And Boundaries

Fix a live derivatives reconciliation false hard mismatch where converged execution replay hydrates historical `execution_orders.raw_payload` rows as zero-filled order states. Scope is limited to read compatibility for existing top-level OrderState payloads and the regression test that proves filled quantities survive hydration.

## Module Responsibilities And Domain Model

`ConvergedPostgresExecutionRepository` remains the execution truth adapter for converged `execution_orders` / `execution_fills`. `StateComparator` continues comparing hydrated `OrderState.filled_qty` against replayed fill quantities.

## Input/Output Interfaces

Input: `ExecutionOrderModel` rows whose `raw_payload` may either contain nested `order_state` or a legacy top-level OrderState-shaped payload. Output: a validated `OrderState` preserving status, fill quantities, fee fields, exchange ids, margin metadata, and snapshot refs.

## Database Schema / Tables / Indexes / Constraints

No schema, index, or constraint changes. Existing `execution_orders.raw_payload` variants remain valid.

## Transactions, Consistency, Concurrency

Read-only hydration change only. No transaction behavior changes and no direct repair of live rows.

## Authorization, Authentication, Data Security

No API or credential changes. Investigation used live DB only through existing local container access and did not print secrets.

## Error Handling And Idempotency

Nested `order_state` remains preferred. Top-level payloads are only parsed when required OrderState keys are present, so generic raw payload fallback behavior is preserved.

## State Transition And Lifecycle

No state transition rules change. The fix prevents terminal filled states from being rehydrated as zero-filled synthetic fallbacks.

## Caching And Performance

Constant-time payload shape check per order row. No new queries or cache behavior.

## Logging, Monitoring, Auditing

No logging changes. Acceptance is visible through subsequent reconciliation reports dropping `local_order_state_differs_from_fill_reconstruction` for the affected rows.

## Testing Strategy

Add a unit regression that recreates a top-level filled OrderState payload and verifies hydration preserves `FILLED`, `filled_qty`, `remaining_qty`, average price, fees, exchange id, and margin metadata.

## Migration, Rollback, Compatibility

Backward compatible with nested payloads and older top-level payloads. Rollback is code-only.

## Configuration And Environment Isolation

No config or environment changes.

## Code Organization And Dependencies

Change stays inside the existing converged execution repository and existing task58 test module. No new dependencies.

## Documentation And Operations Manual

This SOW records the incident scope and acceptance criteria. Operational action after deploy is to let or trigger reconciliation validation and confirm report severity clears or changes to a non-blocking class.

## Deployment And Acceptance Criteria

After tests pass, commit and deploy through `scripts/deploy.sh --skip-commit` for `derivatives-live`. Acceptance: required containers healthy, gateway `/healthz` OK, and a fresh reconciliation no longer reports 65 local execution reconstruction mismatches caused by zero-filled hydration.
