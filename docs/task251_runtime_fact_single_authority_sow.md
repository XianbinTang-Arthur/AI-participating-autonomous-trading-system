# Task251 Runtime Fact Single Authority SOW

## Business Objective

Make automation and operator diagnostics deterministic by projecting live runtime facts as the only authoritative source, while keeping artifact facts as non-authoritative last-known context.

## Scope

In scope:

- `scripts/runtime_truth_report.py` read-only output structure.
- Unit tests for stale artifact handling and runtime fact authority.
- Documentation of the runtime fact authority policy.

Out of scope:

- Strategy, risk, execution, provider, symbol, venue, family, release, promotion, tuning, schema, timeframe plumbing, and live order behavior changes.
- Any direct credential handling or secret-file output.

## Design

The runtime truth report now keeps the existing `runtime.artifact_last_known` compatibility field, but adds:

- `runtime.live_runtime_facts`: live facts derived from DB probes, dashboard runtime probes, git/deploy probes, and health probes.
- `runtime.artifact_last_known_status`: artifact freshness and mismatch metadata, with `may_override_live=false`.
- `runtime.fact_authority`: machine-readable policy that `runtime.live_runtime_facts` is authoritative and artifact facts cannot override live facts.

Artifacts are compared against live facts for key runtime fields such as latest decision, decision/fill counts, shadow benchmark, and AI timeout blocker classification. Mismatched or aged artifacts are marked stale, but stale artifacts are not runtime blockers by themselves.

## Security

No credentials, tokens, API keys, database passwords, or full connection strings are read or printed. The existing DB probe still runs inside the gateway container using its already-loaded environment and emits aggregate non-sensitive facts only.

## Acceptance Criteria

- Runtime truth report clearly separates live facts from artifact last-known facts.
- Stale artifact facts cannot override live runtime facts.
- The change is read-only and introduces no live order behavior change.

## Validation

- Focused unit tests for `tests/unit/scripts/test_runtime_truth_report.py`.
- Full unit test suite.
- Runtime truth report smoke after deploy.

## Rollback

Revert the Task251 commit and redeploy with `scripts/deploy.sh` if the new fact-authority projection misreports runtime state.
