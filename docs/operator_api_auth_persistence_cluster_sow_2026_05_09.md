# Operator API Auth Persistence Cluster SOW 2026-05-09

## Business objectives and boundaries
- Clear the current `operator_api` deploy gate blocker for the Postgres-backed operator account persistence test.
- Keep scope limited to operator API test reliability for session-auth transport compatibility.
- Do not change live trading strategy, venue, symbol, risk engine, execution gates, truth chain, release gates, provider routing, or runtime timeframe plumbing.

## Module responsibilities and domain model
- `tests/integration/test_operator_api.py` owns integration coverage for operator API auth behavior.
- `aats.api.auth_routes` owns `/auth/login` transport and session checks.
- `aats.storage.operator_repo_postgres` owns persisted operator user lookup and mutation.
- `OperatorUserRecord` remains the persisted operator account domain object.

## Input/output interfaces
- Input: `POST /auth/login` with username and password against a `TestClient`.
- Output: HTTP 200 plus authenticated identity when transport is session-compatible and credentials match.
- Failure output remains the existing HTTP error details when auth is disabled, transport is incompatible, or credentials are invalid.

## Database schema / tables / indexes / constraints
- No schema, table, index, or constraint changes.
- The test continues using `temporary_postgres_url()` and the existing operator user table through repository APIs.

## Transactions, Consistency, Concurrency
- No transaction behavior changes.
- Test setup still commits the seed operator user through `runtime.operator_repo.save_user()`.
- Existing repository commit and runtime disposal behavior is preserved.

## Authorization, Authentication, Data Security
- No credentials, tokens, API keys, or connection strings are printed.
- The test exercises session auth over a session-compatible HTTPS test transport.
- Password material remains test-only and is hashed before persistence.

## Error Handling and Idempotency
- Existing API error behavior remains unchanged.
- The test should distinguish transport compatibility failures from Postgres persistence failures by asserting the provider transport state before login.

## State Transition and Lifecycle
- User lifecycle remains: seed disabled-auth runtime -> persist operator user -> dispose DB runtime -> rebuild auth-enabled runtime -> login.
- Session state is created only through `/auth/login`.

## Caching and Performance
- No cache behavior changes.
- No performance-sensitive runtime path changes.

## Logging, Monitoring, Auditing
- Existing operator login audit path remains covered by successful login.
- No logging changes.

## Testing Strategy
- Re-run the focused failing WSL2 integration test.
- Re-run full `tests/integration/test_operator_api.py` in WSL2 to confirm the gate shrinks.
- Run required `ruff check aats/ --fix` and Windows unit tests.

## Migration, Rollback, Compatibility
- No migration required.
- Rollback is deleting this SOW and reverting the single test-client transport change.
- Public API compatibility is preserved.

## Configuration and Environment Isolation
- No production configuration changes.
- Test transport is isolated to the integration test via `TestClient(..., base_url="https://testserver")`.

## Code Organization and Dependencies
- No new dependencies.
- No production module reorganization.

## Documentation and Operations Manual
- This document records the bounded task and rollback surface.
- Runtime/deploy state remains tracked in `artifacts/automation`.

## Deployment and Acceptance Criteria
- Deployment is not attempted unless the operator API gate passes.
- Acceptance is binary:
  - Focused auth persistence test passes in WSL2.
  - Full operator API integration suite has one fewer failure, with any remaining failures explicitly named.
  - Required lint/unit validation is run and reported.
