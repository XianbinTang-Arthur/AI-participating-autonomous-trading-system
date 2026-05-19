# Research Factory Operationalization And Verdict Board SOW

Date: 2026-05-19

## Business objectives and boundaries

Shift Research Factory from expanding governance objects to operationalizing the
existing evidence workflow and producing candidate verdicts.

This work adds:

- a stable governance workflow CLI alias
- a candidate verdict board
- operator-facing summary/checklist improvements

It does not add a new package, review, approval, safety policy, execution layer,
dry-run executor, apply bridge, runtime write path, active parameter mutation, or
OKX write path.

## Module responsibilities and domain model

New verdict module:

- `CandidateVerdict`
- `build_candidate_verdict_from_payloads()`
- `build_candidate_verdict_from_workflow()`
- `update_candidate_verdict_board()`

Allowed verdicts:

- `reject`
- `keep_observing`
- `positive_executable_edge`

New CLI entry points:

- `scripts/rdp_run_research_governance_workflow.py`
- `scripts/rdp_update_candidate_verdict_board.py`

## Input/output interfaces

Governance CLI inputs:

- Gold replay experiment inputs
- factor expression or proposal artifact
- execution cost summary artifact
- observation summary artifact
- explicit research profile
- DB URL environment variable name

Verdict inputs:

- `workflow_summary.json`
- `operator_review_checklist.json`
- referenced research artifacts when available

Verdict outputs:

- `artifacts/research/research_factory/verdicts/candidate_verdict_board.jsonl`
- `artifacts/research/research_factory/verdicts/candidate_verdict_board.md`

## Database schema / tables / indexes / constraints

No database schema changes.

## Transactions, consistency, concurrency

The verdict board is rewritten atomically after merging verdict entries by
`workflow_id`. CLI failures emit JSON and do not write runtime state.

## Authorization, authentication, data security

The governance CLI reads only the database URL from the environment variable
specified by `--database-url-env`. It does not read `.env` files.

All new outputs stay under `artifacts/research`. Verdicts and operator surfaces
must reject runtime/apply language in executable next-action fields.

## Error handling and idempotency

CLI parse and runtime failures print JSON. Verdict board updates replace an
existing entry for the same workflow instead of appending duplicates.

## State transition and lifecycle

Verdict rules V1:

- evidence, candidate, observation, or reference-integrity failure can reject
- insufficient observation can keep observing
- execution compatibility or weak evidence keeps observing
- all required gates passing with positive executable edge can produce
  `positive_executable_edge`

No verdict authorizes apply or runtime mutation.

## Caching and performance

No caching changes. Verdict board updates read small JSON artifacts and rewrite
small board files.

## Logging, monitoring, auditing

Auditability is provided by workflow summary, checklist, verdict JSONL, and
Markdown board.

## Testing strategy

Add tests for:

- governance CLI JSON failures and success with injected fake workflow
- no `.env` loading in the CLI
- negative edge verdict rejection
- insufficient observation keep-observing verdict
- all gates passing positive executable edge verdict
- reference integrity failure rejection
- runtime/apply next-action rejection
- verdict board JSONL/Markdown writes

## Migration, rollback, compatibility

No migrations. Existing workflow APIs are preserved. The old governance review
script remains available; the new workflow script is the operator-facing alias.

## Configuration and environment isolation

No new environment variables are required. The CLI accepts the DB URL environment
variable name explicitly.

## Code organization and dependencies

No new third-party dependencies.

## Documentation and operations manual

This SOW defines the current two-week freeze: bug fixes, CLI, verdict board,
operator summary/checklist, artifact reference fixes, tests, and real-candidate
run fixes are allowed. New governance package/review/approval layers are out of
scope.

## Deployment and acceptance criteria

Acceptance requires lint, focused tests, Research Factory unit tests, full unit
tests, and WSL2 RDP workflow sanity. No deployment is required.
