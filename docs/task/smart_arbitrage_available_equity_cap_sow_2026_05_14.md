# Smart Arbitrage Available Equity Cap SOW - 2026-05-14

## Current behavior

Smart arbitrage now reads available trading equity from the live decision context. A remaining edge case existed when multiple opening pairs were selected in the same cycle: each pair was capped independently by the same realtime available equity, then the aggregate candidate summed those per-pair requests.

## Scope

- Keep the realtime account balance as the source of truth.
- Apply a second aggregate cap across newly opening smart-arbitrage pairs before the candidate is handed to allocation.
- Leave active, recovery, and unwinding pair management outside this new-entry cap so existing exposure repair and exits are not starved by quote balance sizing.
- Add a regression test proving that a multi-pair opening request cannot exceed the realtime available trading equity.

## Validation target

- Ruff on `aats/`.
- Unit test coverage for smart-arbitrage multi-pair capping.
- Full unit suite if the targeted test is clean.
