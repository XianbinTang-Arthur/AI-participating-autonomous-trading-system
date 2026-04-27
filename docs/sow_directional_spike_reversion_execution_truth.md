# SOW: Directional Spike/Reversion Execution Truth

## Business Objectives and Boundaries
Add read-only execution-science evidence for recent directional filled episodes, focused on spike/reversion context around fills. The purpose is to make trading-quality diagnosis auditable before any strategy/risk/order behavior change.

Boundaries: OKX, BTC-USDT-SWAP, directional live carrier. No strategy tuning, no risk gate changes, no order/execution behavior changes, no provider changes, no schema changes, no symbol/venue/family expansion.

## Module Responsibilities and Domain Model
`scripts/runtime_truth_report.py` owns aggregation and classification. It reuses existing directional episode attribution and RDP silver orderbook/trade-flow context.

Domain model: directional decision, latest fill, decision-time mid, fill-time mid, post-fill mid, signed fill adverse distance, signed post-fill reversion, silver trade-flow imbalance.

## Input/Output Interfaces
Input: existing `directional_episode_attribution_truth.recent_decisions` with microstructure enrichment.

Output: additive `directional_spike_reversion_truth` and selected `runtime.live_runtime_facts` fields.

## Database Schema / Tables / Indexes / Constraints
No schema, table, index, or constraint changes.

## Transactions, Consistency, Concurrency
No writes and no trading transaction participation. The report is idempotent and read-only.

## Authorization, Authentication, Data Security
No secrets are read or printed. Existing no-secret runtime truth probing remains unchanged.

## Error Handling and Idempotency
Missing microstructure or fill price evidence is reported as a smallest missing field. Re-running the report produces the same facts for the same data snapshot.

## State Transition and Lifecycle
No order, fill, lot, position, or provider state transition changes.

## Caching and Performance
The new summary reuses existing in-memory report data and adds no database query.

## Logging, Monitoring, Auditing
The new truth surface makes spike/reversion context visible in the runtime report and live runtime facts, supporting operator review of directional losses.

## Testing Strategy
Unit tests cover adverse fill plus post-fill reversion classification and live runtime projection fields.

## Migration, Rollback, Compatibility
Rollback is reverting the doc/script/test changes. JSON changes are additive and backward compatible.

## Configuration and Environment Isolation
No configuration changes.

## Code Organization and Dependencies
No new dependency. Script logic stays in `scripts/runtime_truth_report.py`; unit tests stay in `tests/unit/scripts/test_runtime_truth_report.py`.

## Documentation and Operations Manual
This SOW is the documentation artifact. Operators should treat the new fields as evidence, not as an automatic trading gate.

## Deployment and Acceptance Criteria
Acceptance criteria:
- Directional filled episodes with decision/fill microstructure produce `verified_directional_spike_reversion_execution_context_present`.
- Latest context reports signed adverse fill bps and post-fill mid move bps when available.
- Runtime live facts expose the latest classification and sample counts.
- Focused tests, lint, full unit tests, deploy, and post-deploy smoke pass.
