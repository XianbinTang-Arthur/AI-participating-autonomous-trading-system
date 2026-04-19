# Task 146 SOW - Independent Recovery Version Visibility And Compat Test Cleanup

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Business objectives and boundaries

- Reduce residual reliance on `max_drawdown_bps` in unit and analysis-style tests.
- Keep only a small explicit compatibility test surface for the legacy alias.
- Expose independent `state_version` and score-stability `semantics_version` in recovery/operator read paths and dashboard diagnostics.
- Do not change scoring semantics, public settings keys, or execution behavior.

## Module responsibilities and domain model

- `independent` scoring/model tests should treat `upward_excursion_bps` / `downward_drawdown_bps` as primary semantics.
- `operator` / `recovery` read-side should surface independent recovery snapshot generation metadata explicitly.
- dashboard risk view should render those versions as operator-facing diagnostics.

## Input/output interfaces

- `GET /system/recovery` continues to return `independent_recovery_snapshots`, now enriched with `score_stability_semantics_version`.
- risk dashboard consumes the existing recovery payload and renders an additional diagnostics card.

## Database schema / tables / indexes / constraints

- No schema changes.

## Transactions, Consistency, Concurrency

- Read-side only. No new transactional behavior.

## Authorization, Authentication, Data Security

- No auth model changes. Reuses existing operator/recovery visibility.

## Error Handling and Idempotency

- Missing version fields remain readable and degrade to `待确认` on the dashboard.

## State Transition and Lifecycle

- No state machine behavior changes.
- Only visibility of current independent recovery generation metadata is improved.

## Caching and Performance

- Enrichment happens inside existing cached recovery view construction.
- No new polling or write paths.

## Logging, Monitoring, Auditing

- No new log channels.
- Recovery/operator diagnostics become easier to interpret because version fields are explicit.

## Testing Strategy

- Update unit tests to stop using `max_drawdown_bps` as a primary assertion path.
- Keep one small compatibility-focused test group.
- Add operator API coverage for enriched recovery snapshots.
- Add dashboard UI coverage for independent recovery version diagnostics.

## Migration, Rollback, Compatibility

- Fully backward compatible at API/schema level.
- Rollback is code-only.

## Configuration and Environment Isolation

- No `.env.*` changes.

## Code Organization and Dependencies

- Minimal edits in existing operator/recovery/dashboard files.
- No new external dependencies.

## Documentation and Operations Manual

- This SOW is the operator/read-side documentation update for the change.

## Deployment and Acceptance Criteria

- `max_drawdown_bps` is no longer used as the normal assertion path outside dedicated compatibility tests.
- `system/recovery` exposes independent `state_version` and `score_stability_semantics_version`.
- risk dashboard clearly displays those versions.
