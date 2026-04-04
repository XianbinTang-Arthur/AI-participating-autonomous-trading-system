## Task 192: Query-Layer Legacy Runtime-State Normalization

### Business objectives and boundaries
- Ensure operator/query surfaces no longer leak legacy independent payloads where `book_state` still carries guard semantics.
- Keep scope limited to shared normalization plus the query/replay read paths that expose `book_runtime_states` and `*_replay_snapshot`.
- Do not change allocator, decision generation, or frontend rendering logic in this task.

### Module responsibilities and domain model
- `aats/services/strategy_engines/independent/payload_normalization.py`
  - Centralizes legacy independent payload normalization for runtime states and replay snapshots.
- `aats/services/strategy_engines/independent/replay.py`
  - Uses the shared normalizer for recovery/replay snapshot construction.
- `aats/services/operator/query_service.py`
  - Uses the shared normalizer before exposing position-target / decision-outcome / audit payloads.

### Input/output interfaces
- Input:
  - Raw dict payloads from event store / audit refs / stored strategy runtime JSON.
  - Persisted legacy runtime-state objects where `book_state in {"cooldown", "suspended"}`.
- Output:
  - Normalized `book_runtime_states`
  - Normalized nested `family_execution_summary.book_runtime_states`
  - Normalized `long_replay_snapshot` / `short_replay_snapshot`

### Database schema / tables / indexes / constraints
- No schema changes.
- Existing persisted JSON payloads remain untouched; normalization is read-time only.

### Transactions, Consistency, Concurrency
- No new transactions.
- Normalization must be deterministic and side-effect free.

### Authorization, Authentication, Data Security
- No auth or access-control changes.

### Error Handling and Idempotency
- Invalid runtime-state dicts should still be skipped rather than failing the whole query path.
- Running normalization multiple times must produce the same result.

### State Transition and Lifecycle
- Lifecycle stays in `book_state`.
- Guard semantics stay in `guard_state`.
- Legacy prior-state values must normalize consistently with the same state-machine rules already used by recovery/replay.

### Caching and Performance
- Keep normalization in-memory and per-payload.
- Avoid extra repository or event-store lookups.

### Logging, Monitoring, Auditing
- No logging changes.
- Acceptance is driven by unit and integration tests.

### Testing Strategy
- Add operator API integration coverage for legacy payloads.
- Keep replay/recovery tests passing while moving them onto the shared normalizer.

### Migration, Rollback, Compatibility
- No migration required.
- Backward compatibility is preserved through read-time normalization.
- Rollback is code-only.

### Configuration and Environment Isolation
- Use existing test runtime settings and temporary Postgres helpers.
- No environment or profile changes.

### Code Organization and Dependencies
- Introduce one shared normalizer module under `independent/`.
- Reuse existing `state_machine` semantics; do not fork transition logic.

### Documentation and Operations Manual
- This SOW documents the query-layer compatibility boundary added after Task 191 exposed the remaining leak path.

### Deployment and Acceptance Criteria
- Query payloads no longer surface legacy guard-tagged `book_state` values.
- Nested `family_execution_summary` and `*_replay_snapshot` payloads are normalized too.
- Replay/recovery and operator API tests pass.
