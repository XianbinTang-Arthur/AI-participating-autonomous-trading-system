# Task 140 - External Book Expectancy Summary Migration Surface

## Summary

To give external consumers a stable migration path away from old single-value summaries,
`book_expectancy_summary` is now exposed at the top level of the main operator decision payloads.

## What changed

- `PositionTarget` now carries top-level `book_expectancy_summary`
- `DecisionOutcome` now carries top-level `book_expectancy_summary`
- `StrategyCoordinatorService.apply_selected_target()` copies the selected family book expectancy summary to both objects
- `OperatorQueryService` backfills top-level `book_expectancy_summary` from nested `family_execution_summary`
  for older payloads
- `/decision/latest`
- `/decision/{decision_id}`
- `/decision/recent`

## Migration guidance

External consumers should prefer the new top-level field:

- `position_target.book_expectancy_summary`
- `decision_outcome.book_expectancy_summary`
- `recent_decisions[].book_expectancy_summary`

The older nested location remains available for compatibility:

- `family_execution_summary.book_expectancy_summary`
