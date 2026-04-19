# Task 205 - Legacy Key Scan and Retirement Order

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Scan scope
- auto-parallel / sleeve auto execution runtime payloads
- operator/runtime summaries
- strategy dashboard bundle
- candidate / intent metrics mirrors
- config compatibility keys

## Retirement order
1. `auto_automation_state`
   - Scope: candidate / intent metrics only
   - Reason: no primary UI or runtime logic depends on it anymore
   - Action in this task: replace with `auto_legacy_automation_state`

2. `summary.auto_parallel_enabled`
   - Scope: strategy dashboard frontend fallback only
   - Reason: runtime summary already moved to `entry_auto_execution_enabled`
   - Action in this task: remove fallback usage from strategy view

3. `entry_execution_guard.configured_auto_parallel_enabled`
   - Scope: guard payload field naming only
   - Reason: the name still encodes the deprecated model even though the guard now represents auto execution semantics
   - Action in this task: rename to `configured_auto_execution_enabled`

4. `StrategySleeveAutomationDecision.automation_state`
   - Scope: per-decision compatibility field
   - Reason: still useful for historical consumers, but it is now only a coarse projection
   - Action in this task: keep, compatibility-only

5. `summary.compatibility.legacy_automation_state_counts`
   - Scope: runtime compatibility summary
   - Reason: still useful for migration diagnostics and historical comparisons
   - Action in this task: keep, compatibility-only

6. `strategy_sleeve_auto_parallel_enabled`
   - Scope: settings/config compatibility key
   - Reason: highest migration cost at the time of the scan
   - Action after follow-up tasks: removed from active settings/env inputs; retained only as a historical compatibility name

## Acceptance criteria
- New payloads stop emitting `auto_automation_state` and use `auto_legacy_automation_state` instead.
- Strategy view no longer falls back to `summary.auto_parallel_enabled`.
- Entry-execution guard payload uses `configured_auto_execution_enabled`.
- Remaining legacy keys are documented as compatibility-only and ordered for future removal.
