# Claimed Submit Operator Handoff SOW

## Business Objectives And Boundaries
Provide a no-secret operator handoff artifact after confirmation-packet validation so a human can verify the OKX absence checks without rereading multiple runtime artifacts. The handoff is read-only and must not mutate order state, exchange state, Redis, Postgres, strategy configuration, risk gates, provider settings, symbols, venues, or strategy families.

## Module Responsibilities And Domain Model
`scripts/verify_operator_confirmation_packet.py` remains the validator for the claimed-submit stuck order. The added handoff output packages the validation result, exact confirmation string, OKX checks, protected endpoint metadata, forbidden actions, and rollback language.

## Input/Output Interfaces
Inputs remain `--packet`, `--runtime-truth`, and optional `--operator-confirmation`. New optional output is `--handoff-output <path>`, which writes a JSON artifact while preserving the existing stdout validation result and exit-code behavior.

## Database Schema / Tables / Indexes / Constraints
No schema, table, index, or constraint changes. The feature consumes local JSON artifacts only.

## Transactions, Consistency, Concurrency
No transactions. Consistency is inherited from the verifier: packet evidence hash and current runtime truth comparisons must pass before the handoff is considered valid.

## Authorization, Authentication, Data Security
No live API, database, env file, token, or credential is read. The handoff contains operational identifiers and confirmation text only.

## Error Handling And Idempotency
The verifier keeps returning exit code `0` for valid evidence and `2` for invalid evidence. Handoff generation is idempotent for the same inputs and overwrites only the requested output path.

## State Transition And Lifecycle
No lifecycle transition is introduced. A handoff with `awaiting_external_operator_confirmation` is not permission to recover. Recovery remains gated by exact external confirmation and the existing protected writer path.

## Caching And Performance
The feature reads two small JSON files and writes one small JSON file. No caching is needed.

## Logging, Monitoring, Auditing
The generated handoff JSON is the audit artifact. It is intended to be stored next to runtime truth and verifier result artifacts.

## Testing Strategy
Unit tests cover handoff status while awaiting external confirmation and when exact confirmation makes the packet ready for protected recovery.

## Migration, Rollback, Compatibility
No migration. Existing verifier arguments remain compatible. Rollback is reverting the script, tests, and this SOW document.

## Configuration And Environment Isolation
No runtime configuration is required. The script runs in the repository Python environment and writes only to the requested artifact path.

## Code Organization And Dependencies
The change stays inside the existing verifier script and unit test file. It uses only Python standard library modules.

## Documentation And Operations Manual
Operators should use the handoff as a checklist: first verify the listed OKX absence conditions, then rerun the verifier with the exact confirmation string before invoking the protected recovery endpoint.

## Deployment And Acceptance Criteria
Acceptance requires focused tests, ruff for changed files, and a real-artifact smoke that writes a valid handoff JSON. Deployment is safe because the script is read-only.
