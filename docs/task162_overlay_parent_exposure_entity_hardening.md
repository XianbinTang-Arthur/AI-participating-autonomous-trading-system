# Task 162 - Overlay Parent Exposure Entity Hardening

## Business objectives and boundaries
- 继续把 `protective / opportunistic` 的 parent exposure 从“共享信号对象”收紧成更像业务实体的一等对象。
- 不改 allocator / apply / execution 主链，不把 overlay family 改造成独立 alpha family。
- 保持现有 family helper 兼容，仅把 direct-args fallback 统一上收到共享模块。

## Current behavior summary
- 当前仓库已经有共享 `OverlayParentExposureLifecycle`，并由 coordinator 下发给 overlay families。
- 但 `evaluate_protective_overlay_decision(...)` 和 `evaluate_opportunistic_overlay_decision(...)` 仍各自内联 direct-args fallback。
- 生命周期对象里也还缺少更适合业务解释的聚合字段，例如：
  - signed `target_qty / current_qty / effective_qty`
  - `source_of_truth`

## Implementation plan
1. 在 `overlay_parent_exposure.py` 增加：
   - `source_of_truth`
   - `target_qty / current_qty / effective_qty`
   - 共享 direct-args resolver
2. 让 `protective / opportunistic` fallback 统一调用共享 resolver，而不是各自内联拼装 parent exposure。
3. 把这些更像业务实体的字段补进 candidate metrics，便于后续 runtime/operator 摘要继续上收。

## Testing strategy
- unit:
  - `tests/unit/test_protective_family.py`
  - `tests/unit/test_opportunistic_family.py`
- integration:
  - `tests/integration/test_mainline_chain.py`
  - `tests/integration/test_strategy_runtime_integration.py`

## Acceptance criteria
- lint 通过
- 相关 unit tests 通过
- 最窄 overlay integration tests 通过
- overlay family 的 direct-args fallback 不再在 family 内联派生 parent exposure
