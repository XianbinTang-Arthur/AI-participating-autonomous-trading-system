## Task 194: Independent Score Drawdown Replay Backfill and Sweep Support

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


### Business objectives and boundaries
- Close the remaining phase-two compatibility gap where legacy `threshold_snapshot` payloads do not expose the new drawdown-threshold fields during replay/recovery reads.
- Add a minimal in-repo sweep utility so score-drawdown threshold tuning can be driven by code and tests instead of ad-hoc manual inspection.
- Do not change live profile values in this task.

### Module responsibilities and domain model
- `aats/services/strategy_engines/independent/replay.py`
  - Backfill additive `score_drawdown_bps` and `effective_score_drawdown_bps` when replay/recovery reads an older threshold snapshot.
- `aats/services/strategy_engines/independent/tuning.py`
  - Provide sweep-ready sample extraction and threshold summary utilities for independent replay snapshots.
- `aats/services/strategy_engines/independent/__init__.py`
  - Re-export the new tuning helpers for internal reuse.

### Input/output interfaces
- Inputs:
  - legacy `long_threshold_snapshot` / `short_threshold_snapshot`
  - legacy `min_score_stability_bps`
  - additive `min_score_drawdown_bps` / `effective_score_drawdown_threshold_bps`
  - `IndependentDecisionSnapshot`
- Outputs:
  - normalized replay/recovery threshold snapshots with drawdown fields
  - sweep summaries by threshold bucket

### Database schema / tables / indexes / constraints
- No database schema changes.
- Backfill happens at read time only; stored historical payloads remain untouched.

### Transactions, Consistency, Concurrency
- No new transactions.
- Replay/recovery should deterministically surface the same additive threshold fields for the same stored payload.

### Authorization, Authentication, Data Security
- No auth or data-security changes.

### Error Handling and Idempotency
- Backfill is additive and idempotent.
- If a snapshot already contains the new drawdown fields, preserve them.

### State Transition and Lifecycle
- No state machine changes.
- This task only affects replay/recovery diagnostics and offline tuning support.

### Caching and Performance
- Only lightweight in-memory normalization and aggregation.
- No new I/O paths.

### Logging, Monitoring, Auditing
- Replay/recovery diagnostics become more complete for old payloads.
- Sweep summaries provide repeatable evidence for later threshold tuning.

### Testing Strategy
- Unit test replay threshold backfill from legacy metrics.
- Unit test sweep sample extraction and threshold summary behavior.
- Narrow integration test recovery path with a legacy threshold snapshot fixture.

### Migration, Rollback, Compatibility
- No migration.
- Fully backward compatible; older payloads gain additive fields at read time.

### Configuration and Environment Isolation
- Keep using `.venv\\Scripts\\python.exe`.
- Do not change `.env` or managed profile values in this task.

### Code Organization and Dependencies
- Keep replay normalization local to `independent/replay.py`.
- Keep threshold sweep support isolated in a small utility module under `independent/`.

### Documentation and Operations Manual
- This SOW documents the remaining phase-two closure and the first code artifact for tuning.

### Deployment and Acceptance Criteria
- Legacy replay/recovery threshold snapshots expose drawdown threshold fields when the source metrics provide enough data.
- Sweep utility returns deterministic threshold summaries for replay decision snapshots.
- Lint, unit tests, and the narrowest affected integration tests pass.
