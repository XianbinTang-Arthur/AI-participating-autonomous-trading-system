# Operator Terminal No-Fill Explanation UI Surface SOW

## Task

- task_type: `truth-chain-implementation`
- input: `task_registry.next_recommended_task` = `operator_terminal_no_fill_explanation_ui_surface`
- output: `/execution/latest` exposes a read-only `terminal_no_fill_explanation`, and the existing operator strategy / overview pages render it in Chinese.
- scope: API truth projection, frontend truth surface, focused tests, task documentation.
- out of scope: strategy logic, risk gates, execution state transitions, provider behavior, symbol/venue/profile changes, schema changes, deployment choreography beyond the approved deploy script.

## Current Behavior

Runtime truth can now classify the latest executable directional decision as `verified_terminal_order_no_fill_expected`, but the operator UI still shows only a latest order status. A user has to infer why no fill is expected from backend artifacts instead of seeing the reason at the decision surface.

## Acceptance Criteria

- `/execution/latest` returns `terminal_no_fill_explanation` when the latest visible decision order group has only terminal no-fill order states and no matching fills.
- The strategy page shows a clear "why no fill" callout for terminal no-fill cases.
- The overview truth cockpit and timeline show the same terminal no-fill explanation.
- Filled decisions or non-terminal orders do not get mislabeled as terminal no-fill.
- No live order behavior, risk behavior, strategy selection, or schema is changed.

## Validation

- Focused unit tests for the `/execution/latest` projection.
- Static wiring test for cockpit source markers.
- Dashboard render test for strategy and overview pages.
- Standard repo validation: ruff and unit test suite.

## Rollback

Revert the API projection helper and the corresponding UI/test/doc changes. Since the change is read-only, rollback does not require order recovery or data migration.
