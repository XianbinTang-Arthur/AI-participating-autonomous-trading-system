# Task 215: release_cycle shared queue-boundary freeze

## Business objectives and boundaries

Close the P0 golden-path conflict where `release_cycle` can still be manually/API queued and then executed in-process by `rdp_task_daemon` with `dry_run=False`. This task blocks `release_cycle` at the shared task-queue boundary and at daemon execution time for any pre-patch residual tasks. It does not change strategies, recommendations, release gates, live config, or AI autonomy.

## Module responsibilities and domain model

- `aats.data_platform.governance.rdp_task_db`: owns task queue creation contracts and must be the shared boundary for scheduler/API/retry helper callers.
- `aats.api.rdp_routes`: maps queue-boundary rejections to stable operator-facing API responses.
- `scripts.rdp_task_daemon`: refuses to execute blocked workflows if a residual pending task already exists.
- Existing `release_cycle` definitions remain queryable and auditable; they are only blocked from new enqueue while the golden-path freeze is active.

## Input/output interfaces

Input: `workflow` passed to `db_create_task` / `db_create_task_if_idle`.

Output: non-release workflows keep existing return contracts. `release_cycle` enqueue raises a deterministic blocked exception; API returns `{ok: false, task_id: null, workflow: "release_cycle", message: ...}`. Daemon execution of a residual `release_cycle` task returns a deterministic failure without calling release/apply logic.

## Database schema / tables / indexes / constraints

No schema, index, migration, or table change. The existing `governance.rdp_task_queue` partial unique index remains unchanged.

## Transactions, consistency, concurrency

The guard runs before SQL execution, so no transaction writes, WAL churn, or partial queue rows are produced for blocked `release_cycle` attempts.

## Authorization, authentication, data security

No credential access. Existing `require_write_access` remains unchanged. The API response uses a fixed safe message and does not expose SQL or secrets.

## Error handling and idempotency

`release_cycle` enqueue is rejected before DB access in both create helpers. Repeated attempts produce the same deterministic blocked error.

## State transition and lifecycle

Blocked `release_cycle` attempts do not create pending tasks. If a pre-patch `release_cycle` task already exists, daemon execution fails closed before release/apply lifecycle and does not auto-retry.

## Caching and performance

No cache changes. The guard is an in-memory set membership check.

## Logging, monitoring, auditing

The deterministic exception makes blocked enqueue attempts distinguishable from invalid workflow and DB failure paths. Existing server logging remains the audit path.

## Testing strategy

Unit tests cover:
- `db_create_task_if_idle("release_cycle")` blocked before SQL.
- `db_create_task("release_cycle")` blocked before SQL.
- non-release workflow still inserts through the helper.
- `execute_workflow("release_cycle")` fails closed without in-process or subprocess execution.

API regression covers `/rdp/tasks/trigger` returning a stable blocked response for `release_cycle`.

## Migration, rollback, compatibility

Rollback: remove the guard, custom exception, API catch branch, daemon execution guard, and tests. `VALID_WORKFLOWS` still includes `release_cycle` for config/query compatibility.

## Configuration and environment isolation

No runtime configuration changes. The freeze is code-level for this golden-path phase.

## Code organization and dependencies

No new dependency. Changes stay within the existing RDP task DB, API route, and daemon modules.

## Documentation and operations manual

This SOW is the operator-facing task note. Existing release scripts and workflow configs are not edited in this task.

## Deployment and acceptance criteria

Acceptance:
- New `release_cycle` tasks cannot be enqueued via shared DB helpers.
- API trigger cannot create a `release_cycle` pending task.
- Existing residual `release_cycle` tasks cannot execute release/apply through daemon.
- Safe non-release workflows remain unaffected.
- Tests pass without running `release_cycle`.
