# Operator API Blocker Governance Replay Cluster SOW

## Business Objectives And Boundaries
Clear the current operator API blocker governance and replay validation reliability cluster without changing live trading behavior, strategy selection, venue, symbol, risk controls, execution gates, or runtime timeframe plumbing. Scope remains the integration-test gate that blocks push/deploy follow-through for OKX `BTC-USDT-SWAP` derivatives live readiness.

## Module Responsibilities And Domain Model
The affected surface is `tests/integration/test_operator_api.py`. The tests validate operator action audit persistence, blocker history surfacing, and replay validation history persistence. Production operator APIs remain unchanged.

## Input/Output Interfaces
Inputs are in-memory test runtime records: an `OrderState` for cancel audit coverage and a replayable decision chain made of `DecisionContext`, `PositionTarget`, and `DecisionAuditRecord`. Outputs are `/orders/latest`, `/orders/{id}/cancel`, `/system/blockers`, `/replay/validate/{decision_id}`, and `/replay/recent-validations`.

## Database Schema / Tables / Indexes / Constraints
No database schema, migration, table, index, or constraint changes.

## Transactions, Consistency, Concurrency
The tests stop the background decision trigger before seeding deterministic records so assertions do not depend on incidental loop timing. No production transaction semantics are changed.

## Authorization, Authentication, Data Security
No credentials are read or printed. The tests preserve the existing anonymous unsafe-write path for local in-memory integration coverage and do not alter auth enforcement.

## Error Handling And Idempotency
The seeded records use unique ids, making repeated runs deterministic and idempotent inside each isolated test runtime.

## State Transition And Lifecycle
The cancel audit test seeds a `SUBMITTED` local paper order so the normal cancel lifecycle can emit an `OPERATOR_ACTIONS` audit record. The replay test seeds the minimal complete decision chain required by the replay validator.

## Caching And Performance
No production cache behavior changes.

## Logging, Monitoring, Auditing
Validation artifacts and runtime truth reports are recorded under `artifacts/automation/`.

## Testing Strategy
Run ruff, the focused WSL blocker governance replay cluster, full `tests/integration/test_operator_api.py`, and Windows unit tests. Deployment remains blocked unless the full operator API gate passes.

## Migration, Rollback, Compatibility
Rollback is a test/doc revert plus restoring automation state artifacts. No public API or runtime compatibility impact.

## Configuration And Environment Isolation
No configuration changes. WSL validation uses the existing `~/aats-venv`; Windows validation uses `.venv\Scripts\python.exe`.

## Code Organization And Dependencies
No dependency changes. The patch remains in the existing integration test module.

## Documentation And Operations Manual
This SOW records the bounded task. The deployment rule remains `scripts/deploy.sh` only.

## Deployment And Acceptance Criteria
Acceptance is binary: focused blocker governance replay cluster passes, artifacts are updated, and any remaining full operator API failures are captured as the next blocker. No deployment is attempted while full gate failures remain.
