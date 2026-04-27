# Directional Target Atomic Convergence Guard SOW

## Scope
- Task type: `runtime-reliability-fix`.
- Input: live directional evidence of duplicate target bundles opening from stale flat state while prior orders were still unresolved.
- Output: target qualification guard that freezes exposure-increasing derivatives target changes when unresolved open orders are present.

## Current Behavior
- `DecisionContext.current_open_orders` is populated from cache/repository truth.
- The AI gate can block on open orders, but baseline/target candidates can still emit entry, scale-in, or reversal targets while prior target orders have not converged.
- Reduce/close behavior must remain available because it is the safety path for cutting exposure.

## Acceptance Criteria
- A derivatives entry target with `current_open_orders` returns hold and records `target_convergence_open_orders_block_exposure_increase`.
- A derivatives reduce target with `current_open_orders` can still emit `reduce_long`/`reduce_short`.
- Focused target-position tests pass.
- Full `tests/unit/` suite passes before commit.

## Rollback
- Revert the implementation commit and redeploy the previous clean head.
- The change is isolated to target-position qualification and unit tests.
