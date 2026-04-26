# Task256: No-Trade Multi-Factor Attribution Refinement

## Business Objectives and Boundaries
- Objective: make runtime truth explain current no-trade decisions as a chain of final blockers and contributing factors.
- Boundary: read-only reporting only. No strategy, risk, execution, provider, symbol, venue, family, release, promotion, tuning, schema, or live order behavior changes.
- Fixed scope remains OKX + BTC-USDT-SWAP; independent remains live carrier only, not final alpha.

## Module Responsibilities and Domain Model
- `scripts/runtime_truth_report.py` owns no-secret runtime fact projection.
- `latest_decision.no_trade_attribution.primary_blocker` remains a backward-compatible scalar summary.
- New attribution fields distinguish:
  - `final_blockers`: direct reasons the latest decision emitted no execution.
  - `contributing_factors`: contextual soft gates or non-final conditions.
  - `blocker_chain`: compact ordered evidence for allocation and execution path state.

## Input / Output Interfaces
- Input: latest `portfolio_allocation_decisions.payload`, latest audit refs, and per-decision order/fill counts read inside the running gateway container.
- Output: sanitized JSON report with no raw payload body and no secrets.
- Compatibility: existing keys remain present; consumers can continue reading `primary_blocker`.

## Database Schema / Tables / Indexes / Constraints
- No schema, table, index, or constraint change.
- Read-only access touches existing allocation, audit, order, state, and fill tables.

## Transactions, Consistency, Concurrency
- No writes and no transaction lifecycle changes.
- Runtime truth is a point-in-time read projection; live facts remain authoritative over artifacts.

## Authorization, Authentication, Data Security
- The DB probe continues to run inside the gateway container using existing environment.
- The script must not print credentials, URLs, tokens, passwords, or raw payloads.
- Dashboard auth-required panels are kept as unknown effective runtime, not inferred truth.

## Error Handling and Idempotency
- Existing command failure and redaction behavior remains.
- Re-running the report is idempotent and only emits current read-only facts.

## State Transition and Lifecycle
- No trading state transition change.
- Attribution lifecycle: classify current decision as execution-present or current no-trade, then summarize final blockers and contributing factors.

## Caching and Performance
- No cache changes.
- The added classification uses bounded local lists already loaded in memory.

## Logging, Monitoring, Auditing
- Runtime truth output becomes more auditable by separating soft contraction from terminal no-execution causes.
- No additional logging sink is added.

## Testing Strategy
- Update focused unit tests for existing attribution output.
- Add a regression test for fresh reconciliation soft contraction plus independent inactive/execution-incompatible/advisory-only no-trade.
- Run focused script tests and required broader validation.

## Migration, Rollback, Compatibility
- No migration required.
- Rollback: revert this read-only reporting commit and redeploy with `scripts/deploy.sh`.

## Configuration and Environment Isolation
- No configuration or environment variable change.
- No provider selector, AI mode, profile control, or runtime timeframe change.

## Code Organization and Dependencies
- Keep changes inside the existing runtime truth script and its focused unit tests.
- No new dependency.

## Documentation and Operations Manual
- This SOW documents the intent and safety boundary.
- Operators should treat `contributing_factors` as context, not as the final no-order cause when `final_blockers` exists.

## Deployment and Acceptance Criteria
- Acceptance:
  1. Runtime truth distinguishes final no-trade blockers from contributing soft contractions.
  2. Latest independent inactive/execution-incompatible/advisory-only state is visible in sanitized output.
  3. `primary_blocker` no longer collapses the latest no-trade to `reconciliation_contraction_active` when direct final blockers exist.
  4. No live order behavior changes.
- Deployment: after tests and commit, deploy only through `scripts/deploy.sh`.
