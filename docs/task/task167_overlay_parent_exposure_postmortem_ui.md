# Task 167 - Overlay Parent Exposure Postmortem UI

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Business objectives and boundaries
- 把 `overlay_parent_exposure_summary` 从 API 字段升级成可直接阅读的前端 postmortem 区域。
- 覆盖两个仓库内展示面：
  - decision drawer
  - 风险页里的 replay 区域
- 不改交易逻辑，不新增外部消费者假设。

## Current behavior summary
- `ai_decision_audit` 与 replay validation 已经携带 `overlay_parent_exposure_summary`
- 但前端之前没有单独的可视化区域，仍然需要从原始 JSON 或通用摘要里间接读取

## Module responsibilities and domain model
- `aats/api/static/modules/terms.js`
  - 新增父腿暴露 postmortem 辅助文案函数
- `aats/api/static/modules/detail-drawers.js`
  - 为 decision drawer 新增“父腿暴露复盘”卡片
- `aats/api/static/modules/views/risk-view.js`
  - 为 replay 区域新增“回放父腿复盘”卡片

## Input/output interfaces
- 输入：
  - `ai_decision_audit.overlay_parent_exposure_summary`
  - `replayStatus.last_validation.overlay_parent_exposure_summary`
- 输出：
  - decision drawer 独立复盘卡
  - risk view 独立 replay 复盘卡

## Database schema / tables / indexes / constraints
- 本轮不改数据库。

## Transactions, Consistency, Concurrency
- 前端直接消费已经持久化的稳定摘要对象，不新增并发语义。

## Authorization, Authentication, Data Security
- 不改权限模型。
- 仅展示已有策略审计字段。

## Error Handling and Idempotency
- 缺少摘要时不渲染独立卡片。
- 旧数据仍可通过现有 backfill helper 得到兼容结果。

## State Transition and Lifecycle
- 卡片重点展示：
  - 父腿阶段
  - 父腿契约上下文
  - 多空数量拆解

## Caching and Performance
- 仅新增前端字符串拼装，无额外接口请求。

## Logging, Monitoring, Auditing
- operator 可以直接从 UI 看见 residual inventory / inventory-only / target-and-inventory 等阶段信息。

## Testing Strategy
- `tests/integration/test_dashboard_ui.py`

## Migration, Rollback, Compatibility
- 无 migration。
- 回滚时仅需移除新卡片与 helper，不影响已有 API。

## Configuration and Environment Isolation
- 无新增配置。

## Code Organization and Dependencies
- 复用现有 `readableOverlayParentSignalSummary(...)` 归一化能力，避免重复拼装。

## Documentation and Operations Manual
- 本文档即本轮交付说明。

## Deployment and Acceptance Criteria
- lint 通过
- 相关 unit tests 通过
- 最窄 dashboard integration test 通过
- decision drawer 与 replay 风险页都能看到独立的父腿暴露复盘区域
