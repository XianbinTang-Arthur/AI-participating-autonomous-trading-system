# AI Runtime Operator Authenticated Read Access SOW

## Business Objectives and Boundaries

Provide a safe, read-only path for runtime truth reports to use an operator read credential when one is explicitly available to the process. The goal is to verify effective AI runtime facts without bypassing authentication or treating configured targets as effective runtime state.

This task does not change provider behavior, AI decisions, strategy logic, risk gates, execution paths, symbols, venues, timeframes, release gates, tuning, or live order behavior.

## Module Responsibilities and Domain Model

- `scripts/runtime_truth_report.py` owns no-secret runtime truth projection.
- Gateway `/ai/runtime` and `/dashboard/bundle` remain the runtime evidence sources.
- Operator read credentials are optional probe inputs. They are not persisted and must never appear in report output.

## Input/Output Interfaces

Inputs:
- Existing gateway API base URL.
- Optional environment variable name passed by `--operator-read-api-key-env`.

Outputs:
- Sanitized operator read-auth attempt metadata.
- Authenticated `/ai/runtime` facts when a valid read credential is available.
- Distinct auth failure classification when a provided credential is rejected.

## Database Schema / Tables / Indexes / Constraints

No schema, table, index, or constraint changes.

## Transactions, Consistency, Concurrency

No transactions are opened. The report performs bounded HTTP reads and existing runtime truth probes.

## Authorization, Authentication, Data Security

The report must not bypass operator auth. It may supply `X-AATS-API-Key` only from an explicit environment variable. The credential value is not printed, persisted, logged, or copied into artifacts.

## Error Handling and Idempotency

Missing credential, auth-required response, invalid credential, and transport failure are classified separately. The report is idempotent and read-only.

## State Transition and Lifecycle

No live runtime state is changed.

## Caching and Performance

The existing short HTTP timeout pattern is preserved. No cache is added.

## Logging, Monitoring, Auditing

Runtime truth records whether an auth credential source was configured, whether a credential was present, and whether the authenticated probe succeeded, without exposing the credential.

## Testing Strategy

Add unit tests for:
- environment-sourced read auth metadata without secret exposure
- authenticated `/ai/runtime` probe using the header
- rejected credential classified as auth failure and not AI timeout

## Migration, Rollback, Compatibility

No migration is required. Rollback is a normal git revert plus deployment through `scripts/deploy.sh --profile derivatives-live --skip-commit`.

## Configuration and Environment Isolation

Default optional env var name is `AATS_RUNTIME_TRUTH_OPERATOR_READ_API_KEY`. If it is absent, runtime truth remains auth-gated and explicitly reports the missing credential source.

## Code Organization and Dependencies

Keep implementation in `scripts/runtime_truth_report.py`; no new dependencies.

## Documentation and Operations Manual

Operators can run the report with a process-local read credential environment variable to verify effective AI runtime mode. Absence or rejection of the credential must never be interpreted as baseline-only mode or active AI provider timeout.

## Deployment and Acceptance Criteria

Acceptance criteria:
- Runtime truth accepts optional operator read API key from env without printing it.
- `/ai/runtime` and dashboard bundle probes can use the read header.
- Invalid credential is classified separately from missing credential and from AI timeout.
- Tests pass.
- Deployment, if performed, uses only `scripts/deploy.sh --profile derivatives-live --skip-commit`.
