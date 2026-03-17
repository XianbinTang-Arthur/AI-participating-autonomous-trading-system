# Reservation and Outbox Design

## Scope
This document defines the smallest safe redesign for:
- local reservation/obligation tracking
- atomic persistence of execution state plus event publication

It is intentionally limited to the execution path first:
- `ORDER_INTENTS`
- `ORDER_UPDATES`
- `FILL_EVENTS`
- portfolio snapshot side effects driven by fills

It does not yet redesign:
- deposits / withdrawals
- full double-entry ledger
- external message broker integration

## Current Problems

### 1. No local obligation model
Current runtime safety depends on exchange snapshots and open-order counts. The system has no durable local record of:
- reserved quote balance for pending spot buys
- reserved base balance for pending spot sells
- reserved margin budget for derivatives
- how much obligation remains after partial fills
- how much to release on cancel / reject / fail

This makes the following invariants unenforceable:
- one unit of available balance cannot be committed twice locally
- open obligations reconcile with open orders
- restart can reconstruct pending obligations exactly

### 2. Persistence is split across independent commits
The current path is:
1. save order state
2. publish event
3. subscriber writes derived state

In Postgres, `execution_repo` and `event_store` commit independently. A crash between them can leave:
- order state present without `ORDER_UPDATES`
- fill present without `FILL_EVENTS`
- event present while downstream projection did not run

## Design Goals
- Preserve current domain objects where possible.
- Add the minimum new persisted state required to track obligations.
- Make execution-side writes atomic before any asynchronous fan-out.
- Keep replay and recovery deterministic.
- Avoid a broad ledger rewrite in the first phase.

## Phase 1: Reservation / Obligation Model

### New Domain Object
Add a durable `OrderObligation` model keyed by `client_order_id`.

Proposed fields:
- `obligation_id`
- `client_order_id`
- `decision_id`
- `intent_id`
- `symbol`
- `product_type`
- `margin_mode`
- `side`
- `status`
- `reserve_currency`
- `reserved_amount`
- `released_amount`
- `consumed_amount`
- `created_at`
- `last_update_ts`
- `payload`

### Obligation Statuses
- `ACTIVE`
- `PARTIALLY_CONSUMED`
- `RELEASED`
- `CANCELED`
- `FAILED`

### Reservation Rules

#### Spot buy
- reserve quote currency
- preferred amount:
  - market order: `quantity * latest_price * safety_buffer`
  - limit order: `quantity * limit_price`
- consumed amount increases from fills
- remaining reserved amount is released on terminal order close

#### Spot sell
- reserve base currency equal to requested base quantity
- consumed amount increases from fills in base quantity
- remainder released on terminal order close

#### Derivatives
First phase uses conservative notional-based margin reservation, not full exchange margin emulation.
- reserve quote currency budget based on:
  - `abs(quantity * reference_price) / max(target_leverage, 1.0)`
  - plus configured fee buffer
- consumed amount tracks filled notional budget
- released when the order terminates

This is intentionally conservative. It is a local safety gate, not a perfect exchange margin engine.

### New Repository
Add `ExecutionObligationRepository` with:
- `save_obligation(...)`
- `get_obligation(client_order_id)`
- `active_obligations_for_scope(...)`
- `release_obligation(...)`
- `consume_obligation(...)`

### Runtime Behavior

#### On `OrderIntent`
Before adapter submit:
1. compute obligation from intent and current pricing context
2. validate local available balance minus active obligations
3. persist:
   - `OrderState(CREATED/SUBMITTING)`
   - `OrderObligation(ACTIVE)`

#### On `FillEvent`
For each new fill:
1. update obligation consumption
2. if terminal or fully consumed, release remainder
3. then apply portfolio effects

#### On terminal order update
If state becomes one of:
- `CANCELED`
- `REJECTED`
- `FAILED`
- `EXPIRED`
- `DRY_RUN`
- `BLOCKED`

Release any remaining active obligation.

#### On restart / recovery
Recovery must rebuild active obligations from persisted obligation rows, not infer them only from order states.

### Portfolio View Change
Do not overload `PortfolioState.balances` with reserved balances.
Instead, expose a derived view:
- total balance
- reserved amount per currency
- available-after-obligations per currency

This keeps current snapshot semantics stable while making operator/risk views explicit.

## Phase 2: Atomic Execution Persistence and Outbox

### Principle
Execution source-of-truth writes must be committed together:
- order state mutation
- fill insertion
- obligation mutation
- outbox event insertion

Publication to the in-process bus happens after commit via an outbox relay.

### New Table
Add `outbox_events`:
- `outbox_id`
- `event_id`
- `topic`
- `event_key`
- `source_component`
- `payload`
- `decision_id`
- `status` (`PENDING`, `PUBLISHED`, `FAILED`)
- `attempt_count`
- `created_at`
- `published_at`
- `last_error`

### New Storage Boundary
Introduce an execution transaction coordinator for Postgres:
- one SQLAlchemy session
- save order/fill/obligation rows
- save corresponding outbox rows
- commit once

For memory mode:
- apply all mutations in memory first
- append corresponding envelopes to an in-memory outbox list
- relay publishes after the mutation batch completes

### Outbox Flow
1. command handler writes business records + outbox rows atomically
2. relay scans pending outbox rows in order
3. relay publishes envelopes to current bus
4. on success mark row `PUBLISHED`
5. on failure keep `PENDING` or mark `FAILED` with retry metadata

### Topics Covered First
First outbox phase should only cover:
- `ORDER_UPDATES`
- `FILL_EVENTS`
- `EXECUTION_ERROR_SUMMARIES`

Do not move all topics at once. Portfolio and reconciliation can remain subscriber-driven projections.

## Replay and Recovery Changes

### Replay
Replay engine must additionally validate:
- each open order with active nonzero remaining quantity has an active obligation unless explicitly exempt
- each released terminal order has no remaining active obligation
- fill consumption never exceeds reserved amount without explicit overspend marker

### Recovery
Recovery startup must:
- load persisted obligations
- reconcile them against open order states
- halt if mismatch exists:
  - obligation without order
  - open order without obligation
  - negative remaining reserved amount

## Migration Plan

### Migration 1
Add tables:
- `order_obligations`
- `outbox_events`

No existing table rewrite in the first migration.

### Migration 2
Backfill best-effort obligations for currently open orders:
- spot buy from requested qty and stored price context if available
- spot sell from remaining qty
- derivatives from remaining notional and leverage

If required data is missing:
- create obligation row marked with `payload.backfill_uncertain = true`
- halt runtime until operator review

## Implementation Sequence

### Step 1
Add schema and storage types:
- obligation schema
- obligation repo
- SQLAlchemy model
- migration

### Step 2
Integrate local reservation into `OrderManager.handle_order_intent`
- compute obligation
- reject when local available-after-obligation is insufficient
- persist state + obligation together

### Step 3
Integrate obligation release/consume
- on fill ingestion
- on terminal order updates
- on cancel flow
- on operator forced resolution

### Step 4
Introduce outbox table and relay
- atomic write for order/fill/obligation + outbox rows
- relay publishes only after commit

### Step 5
Update replay and recovery invariants

### Step 6
Add focused tests
- duplicate order intent cannot double-reserve
- partial fill consumes only partial obligation
- cancel releases remainder
- restart restores active obligations
- crash after DB commit but before publish still emits via outbox relay
- duplicate relay publish is idempotent

## Explicit Non-Goals for First Pass
- perfect exchange-equivalent derivatives margin
- full accounting ledger replacement
- cross-service distributed transaction protocol
- Kafka / Redis / external queue introduction

## Merge Strategy
This redesign should not land as one patch.
Recommended PR sequence:
1. schema + repo + migration only
2. reservation enforcement in execution path
3. recovery/replay support
4. outbox and atomic execution persistence
5. operator and observability surfacing
