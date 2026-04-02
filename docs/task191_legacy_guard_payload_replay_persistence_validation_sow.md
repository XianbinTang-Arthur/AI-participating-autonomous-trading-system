## Task 191: Legacy Guard Payload Replay/Persistence Validation

### Business objectives and boundaries
- Confirm that persisted legacy independent runtime payloads using `book_state="cooldown"` or `book_state="suspended"` remain readable after the `book_state` / `guard_state` split.
- Limit scope to replay/recovery read paths that consume stored `book_runtime_states` and `*_replay_snapshot` payloads.
- Do not refactor the independent engine, allocator, or operator screens in this task.

### Module responsibilities and domain model
- `aats/storage/strategy_runtime_repo_postgres.py` persists allocation decisions and sleeve metrics as raw JSON payloads.
- `aats/services/strategy_engines/independent/replay.py` reconstructs recovery and decision snapshots from persisted runtime payloads.
- Legacy payloads may encode guard semantics inside `book_state`; new contracts require lifecycle in `book_state` and blocking semantics in `guard_state`.

### Input/output interfaces
- Input:
  - Persisted `PortfolioAllocationDecision` rows with `StrategySleeveIntent.metrics["book_runtime_states"]`.
  - Persisted `long_replay_snapshot` / `short_replay_snapshot` dictionaries inside sleeve metrics.
- Output:
  - `IndependentRecoverySnapshot`
  - nested `IndependentDecisionSnapshot`
  - nested replay snapshot payloads exposed from recovery views

### Database schema / tables / indexes / constraints
- No schema changes.
- Existing `portfolio_allocation_decisions.payload` JSON continues to store both old and new payload shapes.

### Transactions, Consistency, Concurrency
- No transaction behavior changes.
- Validation must prove that a fresh repository instance can read previously persisted legacy payloads without manual migration.

### Authorization, Authentication, Data Security
- No auth changes.
- Tests use temporary Postgres schemas only.

### Error Handling and Idempotency
- Legacy payload normalization must be read-only and idempotent.
- Re-reading the same payload should yield the same normalized recovery/replay view.

### State Transition and Lifecycle
- Legacy guard-tagged `book_state` values must no longer leak into lifecycle output fields.
- Active guards should surface through `guard_state`.
- Stale pseudo-guards should normalize to `guard_state=null` while preserving lifecycle state.

### Caching and Performance
- Keep normalization local to replay/recovery read paths.
- Avoid extra database round-trips.

### Logging, Monitoring, Auditing
- No logging changes.
- Validation relies on unit and Postgres integration coverage.

### Testing Strategy
- Add unit coverage around recovery snapshot construction from legacy runtime payloads.
- Add Postgres integration coverage that persists legacy payloads, reopens storage, and verifies normalized replay/recovery output.

### Migration, Rollback, Compatibility
- No migration required.
- Backward compatibility is achieved by normalizing old payloads during read.
- Rollback is code-only.

### Configuration and Environment Isolation
- Use the existing temporary Postgres test helper and `.venv\\Scripts\\python.exe`.
- No config profile changes.

### Code Organization and Dependencies
- Keep changes scoped to `aats/services/strategy_engines/independent/replay.py` and targeted tests/docs.
- Reuse existing state-machine helpers instead of introducing a parallel normalization model.

### Documentation and Operations Manual
- This SOW documents the compatibility boundary and expected normalized output semantics.

### Deployment and Acceptance Criteria
- Legacy persisted `book_state="cooldown"/"suspended"` payloads can be replayed after a storage round-trip.
- Recovery/replay outputs expose lifecycle `book_state` plus separated `guard_state`.
- Lint, unit tests, and the narrowest affected Postgres integration pass.
