# Task 168 - Replay Overlay Parent History Closure

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Business objectives and boundaries
- 把 `overlay_parent_exposure_summary` 从“最新一条 replay 摘要卡”继续接到 replay 历史表，便于横向比较最近几次校验。
- 不改后端接口契约，不新增页面，只收口现有风险页里的 replay 区域。

## Current behavior summary
- 当前已经有：
  - decision drawer 的 `父腿暴露复盘`
  - 风险页的 `回放父腿复盘`
- 但 replay 历史仍然缺少细分表格，最近几次 validation 的父腿阶段无法并排比较。

## Module responsibilities and domain model
- `aats/api/static/modules/views/risk-view.js`
  - 新增 replay 历史表格渲染
  - 把 `recent_validations[].overlay_parent_exposure_summary` 转成结构化表格与移动卡片
- `aats/api/static/modules/terms.js`
  - 复用已有父腿阶段、契约口径、数量拆解 helper

## Input/output interfaces
- 输入：
  - `replayStatus.recent_validations[].overlay_parent_exposure_summary`
- 输出：
  - 风险页 `回放父腿历史` 细分表格

## Database schema / tables / indexes / constraints
- 本轮不改数据库。

## Transactions, Consistency, Concurrency
- 前端只消费现有 replay 状态载荷，不新增一致性语义。

## Authorization, Authentication, Data Security
- 不改权限模型。
- 仅展示已有 replay 审计信息。

## Error Handling and Idempotency
- `recent_validations` 为空时不渲染历史卡片。
- 某条 validation 缺少 `overlay_parent_exposure_summary` 时，表格回退到占位文案，不伪造对象。

## State Transition and Lifecycle
- 历史表重点展示：
  - 回放时间 / 决策
  - 父腿阶段
  - 契约口径
  - 双腿数量拆解

## Caching and Performance
- 仅做前端字符串拼装和表格渲染，无额外接口请求。

## Logging, Monitoring, Auditing
- 风险页现在可以直接对比：
  - inventory_only
  - target_and_inventory
  - mixed source
  等父腿阶段变化。

## Testing Strategy
- `tests/integration/test_dashboard_ui.py`

## Migration, Rollback, Compatibility
- 无 migration。
- 回滚时只需移除 replay 历史卡片，不影响已有 API 与摘要卡。

## Configuration and Environment Isolation
- 无新增配置。

## Code Organization and Dependencies
- 复用 `readableOverlayParentSignalSummary(...)`
- 复用 `readableOverlayParentPostmortemMeta(...)`
- 复用 `readableOverlayParentLegQuantitySummary(...)`

## Documentation and Operations Manual
- 本文档同时记录本轮收口后的剩余工作清单。

## Deployment and Acceptance Criteria
- lint 通过
- 相关 unit tests 通过
- 最窄 dashboard integration test 通过
- 风险页同时具备：
  - 最新一条 replay 的父腿复盘卡
  - 最近多条 replay 的父腿历史表

## Remaining work
- 仓库内这条 `overlay_parent_exposure_summary` 展示链已基本收口，没有必须继续做的阻塞项。
- 可选优化：
  - 给 replay 历史表增加筛选/折叠
  - 给 replay 详情单独做专门页面，而不只放在风险页
  - 把父腿复盘历史和 overlay leg reconciliation 做联动对读
