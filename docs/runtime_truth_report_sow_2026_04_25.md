# Runtime Truth Report SOW - 2026-04-25

## Business Objectives And Boundaries

Provide a one-command, read-only runtime truth report for the AATS derivatives-live stack. The goal is to stop spending heartbeat rounds manually reconstructing basic state and to make git/deploy alignment, health, runtime auth status, and live decision/fill counts explicit.

This task does not change strategy logic, risk gates, execution behavior, AI provider behavior, symbols, venues, strategy families, release, promotion, tuning, schema, or live order behavior.

## Module Responsibilities And Domain Model

`scripts/runtime_truth_report.py` is an operational diagnostics entrypoint. It reports:

- Windows repository HEAD, branch state, dirty state, and origin divergence.
- WSL deployed repository HEAD and branch state.
- Whether deployed HEAD matches Windows HEAD.
- Gateway `/healthz` result and required app container health.
- Protected dashboard bundle auth/runtime availability.
- Live decision/fill aggregate counts using the running gateway container environment.
- Static dashboard truth-surface markers for the current pre-order feasibility UI linkage.

## Input/Output Interfaces

Input is CLI flags:

- `--repo-root`
- `--api-base`
- `--wsl-distro`
- `--wsl-project`
- `--gateway-container`
- `--pretty`

Output is JSON on stdout. The command returns `0` when it can generate the report. Runtime blockers are represented in `blocking_findings` instead of making local development runs unusable.

## Database Schema / Tables / Indexes / Constraints

No schema changes. The report reads aggregate facts from:

- `portfolio_allocation_decisions`
- `execution_fills`

The script never prints database URLs, usernames, passwords, tokens, API keys, or full connection strings.

## Transactions, Consistency, Concurrency

The database probe is a read-only aggregate query executed through SQLAlchemy inside the already-running gateway container. No writes, locks, migrations, or transactional state changes are introduced.

## Authorization, Authentication, Data Security

The dashboard bundle probe is intentionally unauthenticated. If protected panels return `operator_auth_required`, the report records that exact auth blocker and does not infer effective runtime state from configuration targets.

The database probe uses the gateway container's existing environment implicitly. It prints only aggregate counts and non-sensitive decision identifiers. Secret-like stderr/stdout fragments are redacted defensively.

## Error Handling And Idempotency

The report is idempotent and read-only. Failed probes return structured `ok=false` sections with sanitized errors. Report generation remains usable even when a specific probe is blocked by auth or local worktree state.

## State Transition And Lifecycle

No runtime state transitions are introduced. The script only observes current state.

## Caching And Performance

The script performs a bounded set of subprocess and HTTPS requests with short timeouts. It does not cache results. It is intended for heartbeat and operator diagnostics, not per-request runtime usage.

## Logging, Monitoring, Auditing

The JSON output is the audit artifact. It can be saved by automation if needed. The script itself does not write logs.

## Testing Strategy

Unit tests cover:

- Secret redaction.
- Git divergence/status parsing.
- Docker health parsing.
- Auth-required dashboard bundle classification.
- Database probe command safety.
- Separation between report generation and runtime blockers.

Manual validation runs the script against the live local derivatives stack.

## Migration, Rollback, Compatibility

No migration is required. Rollback is removing `scripts/runtime_truth_report.py`, its tests, and this SOW if it misreports runtime facts.

## Configuration And Environment Isolation

The script defaults to the current derivatives-live local stack but accepts flags for API base, WSL distro, WSL path, and gateway container. It does not import application settings or read secret files.

## Code Organization And Dependencies

The script uses Python standard library plus SQLAlchemy inside the gateway container's existing runtime. The local script itself has no new package dependency.

## Documentation And Operations Manual

Example:

```powershell
.venv\Scripts\python.exe scripts\runtime_truth_report.py --pretty
```

Use `blocking_findings` for deploy blockers. Use `runtime.dashboard_bundle.status` to distinguish verified runtime facts from auth-required unknowns.

## Deployment And Acceptance Criteria

Acceptance:

- Runtime truth report runs with one command.
- Report includes git/deploy alignment and divergence.
- Report includes effective runtime facts or exact auth blocker.
- Report includes live decision/fill counts without printing DB URL.
- Report includes deployment health.
- No credentials or DB URLs are printed.

Deployment is not required for live service behavior because this is read-only operational tooling. It can be deployed through `scripts/deploy.sh` if operators need it present in the WSL checkout.
