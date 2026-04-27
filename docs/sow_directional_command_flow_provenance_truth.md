# SOW: Directional Command-Flow Provenance Truth

## Business Objectives and Boundaries
Expose a read-only runtime truth surface that separates current directional submit-command fills from historical no-submit-command fills. The objective is evidence density and operator diagnosis, not strategy tuning or execution behavior changes.

Boundaries: OKX, BTC-USDT-SWAP, existing directional live carrier. No order behavior, risk gate, provider, schema, symbol, venue, or strategy-family changes.

## Module Responsibilities and Domain Model
`scripts/runtime_truth_report.py` owns aggregation and classification. It consumes existing slippage coverage rows and projects a compact directional command-flow provenance summary into `runtime.live_runtime_facts`.

Domain entities: execution fill, execution order, submit command, pre-trade reference price, directional strategy family, historical no-submit-command path.

## Input/Output Interfaces
Input: existing live DB aggregate `slippage_cost_calibration.by_reference_coverage_path`.

Output: `directional_command_flow_provenance_truth` plus selected `runtime.live_runtime_facts` fields for status, current submit-command fill counts, historical no-submit fill counts, and active reference gaps.

## Database Schema / Tables / Indexes / Constraints
No schema, table, index, or constraint changes. The report remains read-only.

## Transactions, Consistency, Concurrency
The runtime report reads aggregate facts only. It does not mutate database state and does not participate in trading transactions.

## Authorization, Authentication, Data Security
No new credentials are read or printed. Database access continues to use the existing container runtime environment and only aggregate non-secret facts are emitted.

## Error Handling and Idempotency
If slippage coverage evidence is unavailable, the new truth surface reports the missing upstream field. Re-running the report is idempotent.

## State Transition and Lifecycle
No order, position, command, or fill state transition changes.

## Caching and Performance
The new summarizer reuses existing in-memory aggregate rows. It adds no database query.

## Logging, Monitoring, Auditing
The new runtime facts distinguish current command-flow reference gaps from historical no-submit legacy gaps, reducing false active-blocker classification.

## Testing Strategy
Unit tests cover verified current directional command-flow provenance, current command-flow reference gaps, and live runtime projection fields.

## Migration, Rollback, Compatibility
Rollback is reverting the script/test/doc changes. Existing JSON fields remain backward compatible; new fields are additive.

## Configuration and Environment Isolation
No configuration changes. Works in the existing Windows unit-test environment and WSL2 deployed runtime.

## Code Organization and Dependencies
No new dependencies. Code stays in `scripts/runtime_truth_report.py`; tests stay in `tests/unit/scripts/test_runtime_truth_report.py`.

## Documentation and Operations Manual
This SOW is the documentation artifact. Operators should use the new runtime facts to decide whether missing slippage references are a current command-path issue or historical no-submit-command evidence debt.

## Deployment and Acceptance Criteria
Acceptance criteria:
- Current directional submit-command fills with command references report `verified_current_directional_command_flow_fill_provenance_present`.
- Directional submit-command fills missing reference prices report `current_directional_command_flow_reference_gap`.
- Historical no-submit-command directional fills are counted separately and do not become an active current command-path gap.
- Focused tests, lint, and unit tests pass before deploy.
