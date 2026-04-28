# Aggregate writer convergence follow-up SOW (2026-04-28)

## 1. Business objectives / boundaries

Close the remaining single-writer gaps found in review:

- execution outbox transactions that write `OrderObligation` must enqueue a durable `execution.obligation_updates` event in the same commit.
- operator obligation cache replacement must remove stale local obligations across already-bootstrapped processes.
- `ExitExecutionWriter` must not let stale parent saves regress terminal, review-required, or resume-issue state.
- `refresh_exit_execution_intents` must save refreshed child refs and the recomputed parent through one writer/session boundary.

No schema migration, no API shape change, and no changes to exchange-facing order semantics are in scope.

## 2. Module responsibilities / domain model

- `PostgresExecutionOutboxPublisher` remains the single production writer for execution order/fill/obligation persistence and durable events.
- `ObligationHotStateCache` owns local/Redis/NATS hot-state repair, including replace-index notifications for stale-key removal.
- `ExitExecutionWriter` owns parent/child `exit_execution` writes and stale-parent protection.
- `exit_intent_aggregator` computes refreshed refs and delegates persistence to the writer.

## 3. Input / output interfaces

Existing inputs and outputs are preserved:

- `OrderState`, `FillEvent`, `OrderObligation`
- `ExitExecutionIntent`, `ChildExitOrderRef`
- `OperatorQueryService.clear_obligation_cache()` response details

The only new wire payload is an internal `execution.obligation_updates` cache-replacement envelope consumed by `ObligationHotStateCache`.

## 4. DB schema / tables / indexes / constraints

No schema changes. Existing writes touch:

- order/fill/obligation tables
- event store and outbox tables
- exit execution parent/child tables

## 5. Transactions, consistency, concurrency

- Any transaction that persists an obligation row also appends and enqueues its `OBLIGATION_UPDATES` envelope before commit.
- Existing post-commit cache publish remains a low-latency best-effort hint; durable outbox is the recovery path.
- Exit parent saves/recomputes merge against the locked/current row, preserve terminal snapshots from regression, and preserve review/resume state when the incoming aggregate version is stale.
- Refresh writes all child refs and the recomputed parent under one writer call; Postgres locks the parent row before saving refs and recomputing.

## 6. Authorization, authentication, data security

No new operator permission or credential access. Do not read or print live env secrets.

## 7. Error handling and idempotency

- Obligation event replay remains idempotent by `last_update_ts`.
- Cache replace events only remove coids absent from the rebuilt DB set; they do not mark an unbootstrapped cache authoritative.
- Stale parent saves do not clear current resume issues or review-required status.

## 8. State transition and lifecycle

- Obligation lifecycle visibility is now tied to the same transaction as the row update.
- Cache replacement can tombstone local stale active obligations on other processes.
- Exit execution terminal/review lifecycle cannot be regressed by an older refresh write.

## 9. Caching and performance

One extra outbox envelope is added only when an obligation row is written. Cache replacement broadcasts one compact replace-index envelope plus existing per-obligation updates.

## 10. Logging, monitoring, auditing

Existing outbox publish failure logging remains. Cache replace events log applied removal counts.

## 11. Testing strategy

- Unit tests for stale exit parent preservation and refresh atomic writer routing.
- Unit tests for cache replace-index remote removal.
- Integration tests for durable obligation events on order/fill outbox paths.
- Existing writer convergence scan remains in place.

## 12. Migration, rollback, compatibility

Code-only rollback. Existing rows and events remain compatible.

## 13. Configuration and environment isolation

No new config. Behavior is enabled by existing Postgres outbox and hot cache wiring.

## 14. Code organization and dependencies

No new dependency. Changes stay in existing execution services and tests.

## 15. Documentation and operations manual

This SOW records the follow-up repair scope. Operator behavior is unchanged except cache repair now propagates stale-key removals.

## 16. Deployment and acceptance criteria

Acceptance:

- no reviewed direct-writer/cache-regression findings remain open;
- targeted unit and integration tests pass;
- repo lint/unit commands pass or any failure is explicitly reported.
