# Orderbook payload depth truth SOW

## Business objectives and boundaries

Expose read-only execution-science evidence showing whether local orderbook sidecar payloads can support depth-aware fill-feasibility work for OKX `BTC-USDT-SWAP`. This does not change strategy logic, risk gates, AI provider behavior, symbols, venues, schemas, order submission, or live execution behavior.

## Module responsibilities and domain model

`scripts/runtime_truth_report.py` owns the runtime truth aggregation. The domain model links RDP bronze orderbook sidecar payload metadata, collector sequence continuity, and silver books5 orderbook coverage.

## Input/output interfaces

Input is the existing RDP database probe plus one new non-sensitive read projection from `bronze.market_orderbook_payloads`. Output is `orderbook_payload_depth_truth` and selected `live_runtime_facts` keys. Raw public orderbook payload bodies are not exposed.

## Database schema / tables / indexes / constraints

No schema, table, index, or constraint changes. The read surface uses existing `bronze.market_orderbook_payloads`, `bronze.market_orderbook_books5`, `bronze.market_orderbook_bbo`, and `silver.market_orderbook_metrics_15m`.

## Transactions, consistency, concurrency

No transaction or write path is added. The report reads a point-in-time snapshot and remains safe under concurrent collector writes.

## Authorization, authentication, data security

No credentials or secrets are printed. The sidecar projection exposes booleans for hash/checksum/sequence presence, not raw payloads or full hashes.

## Error handling and idempotency

Missing sidecar rows, hashes, checksums, books5 sequence, or silver books5 samples are surfaced through explicit status and `smallest_missing_field`. Re-running the report is idempotent.

## State transition and lifecycle

No order, fill, position, lifecycle, recovery, or portfolio state transition occurs.

## Caching and performance

The new RDP read uses a bounded latest-row query by channel and existing recent sequence aggregation. No broad raw payload scan is introduced.

## Logging, monitoring, auditing

The audit output is the runtime truth artifact and `live_runtime_facts` projection. Operators can verify whether books5 depth evidence is sidecar-backed before deeper fill feasibility work.

## Testing strategy

Unit tests cover successful books5 sidecar depth readiness and live fact projection.

## Migration, rollback, compatibility

No migration. Rollback is `git revert` of the read-only report commit and redeploy through `scripts/deploy.sh`.

## Configuration and environment isolation

No new configuration. Scope remains derivatives-live OKX `BTC-USDT-SWAP`.

## Code organization and dependencies

Implementation stays in `scripts/runtime_truth_report.py` and `tests/unit/scripts/test_runtime_truth_report.py`. No new dependency.

## Documentation and operations manual

Operators should treat `orderbook_payload_depth_truth.status=verified_books5_payload_depth_evidence_present` as proof that sidecar hash/checksum/sequence and silver books5 evidence exist. It is not alpha, profitability, promotion, or scale-up evidence.

## Deployment and acceptance criteria

Acceptance passes when focused tests and full unit tests pass, runtime truth emits `orderbook_payload_depth_truth`, deployed head matches Windows head, and post-deploy runtime truth has no blocking findings.
