# Task 137 - Family Cutover 多腿摘要清理

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 目标

- 让 `query_service` 在已有 `decision_outcome` 时直接复用 `final_action / final_direction`
- 给 family cutover 增加结构化多腿摘要，避免强迫 `position_intent / final_direction` 用单值表达多腿动作
- 让策略页、首页、概览页、决策抽屉和应用入口统一读取新的 family/legs 摘要

## 边界

- 不改 allocator 选主逻辑
- 不改真实撮合、风控或执行器下单方向
- 不删除现有 `position_intent / final_direction` 字段，保持兼容

## 变更点

- 在 `DecisionOutcome / PositionTarget` 增加 `family_execution_summary`
- `StrategyCoordinatorService.apply_selected_target()` 在 family cutover 下生成结构化多腿摘要
- `OperatorQueryService` 的 `decision_outcome / ai_decision_audit` 优先复用原生 `decision_outcome`
- `strategy-view.js / app.js / home-view.js / overview-view.js / detail-drawers.js` 改读 family/legs 摘要

## 数据模型

- `summary_mode`
  - `single_leg`
  - `multi_leg`
- `family`
- `route_action`
- `family_action`
- `leg_count`
- `position_intents`
- `directions`
- `leg_actions`
- `execution_modes`

## 验收

- protective / opportunistic 单腿 cutover 时，UI 和 operator 摘要优先显示真实腿语义
- independent 双腿 cutover 时，UI 可以显示“多腿联动 / 双向”，而不是被压成单一净仓位
- `decision_outcome` 已存在时，不再被 `position_target` 的旧净仓位字段覆盖
- lint、unit tests、最窄 integration tests 通过
