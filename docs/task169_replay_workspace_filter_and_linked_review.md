# Task 169 - Replay 工作区、筛选折叠与联动对读收口

## Business objectives and boundaries
- 在仓库内把 replay 从风险页的一张摘要卡，升级成独立工作区。
- 让 replay 父腿历史支持筛选与折叠，便于 operator 只看 `inventory_only / target_only / target_and_inventory` 三类父腿阶段。
- 把 replay 父腿复盘历史与最新腿级对账异常放在同一页联读，减少 residual inventory、target 切换和执行链残留的解释成本。
- 不新造后端领域模型，不改 replay 校验主逻辑；优先复用现有：
  - `/replay/status`
  - `/replay/recent-validations`
  - `/reconciliation/latest`

## Current behavior summary
- 当前风险页已经有：
  - `回放父腿复盘`
  - `回放父腿历史`
- 但还没有：
  - 独立 replay 页面
  - 历史筛选/折叠
  - 与腿级 reconciliation 的专门联读区域

## Module responsibilities and domain model
- `aats/api/static/modules/views/replay-view.js`
  - 专门渲染 replay 工作区
  - 承担筛选/折叠和 replay x reconciliation 联读
- `aats/api/static/modules/store.js`
  - 管理 replay 页的数据面和分页上限
- `aats/api/static/app.js`
  - 管理 replay 页面路由、节点、动作分发和刷新状态
- `aats/api/auth_routes.py`
  - 把 replay 最近历史接进 dashboard bundle，避免页面单独追加请求
- `aats/api/ui.py`
  - 注册 `/ui/replay`

## Input/output interfaces
- 输入：
  - `replayStatus.last_validation`
  - `replayRecentValidations.validations`
  - `reconciliationLatest.mismatch_summary.leg_mismatch_summary`
- 输出：
  - `Replay 工作区`
  - `父腿复盘与腿级对账联读`
  - `回放父腿历史` 筛选/折叠表

## Database schema / tables / indexes / constraints
- 本轮不改数据库。

## Transactions, Consistency, Concurrency
- 全部是只读 UI 和 bundle 聚合，不引入新事务语义。
- 筛选在前端完成；折叠只调整 limit，不改变回放记录本身。

## Authorization, Authentication, Data Security
- 继续复用现有 dashboard shell 鉴权。
- `/ui/replay` 权限与其它工作区一致。

## Error Handling and Idempotency
- replay 历史为空时，页面显示空态，不伪造复盘数据。
- reconciliation 缺失时，联读区域退回解释性文案。
- filter 只接受白名单值：`all / inventory_only / target_only / target_and_inventory`。

## State Transition and Lifecycle
- 风险页保留摘要卡。
- Replay 工作区承接详细对读和历史筛选。
- operator 可以在 Replay 工作区与风险页之间来回切换，不引入新工作流状态。

## Caching and Performance
- 通过 dashboard bundle 一次拉取 replayStatus、replayRecentValidations、reconciliationLatest。
- 不增加额外页面内串行请求。

## Logging, Monitoring, Auditing
- 不改回放审计写入。
- 这轮只增强 operator 对 replay/postmortem 的读路径。

## Testing Strategy
- `tests/integration/test_dashboard_ui.py`
  - 验证 `/ui/replay` 和 `replay-view.js` 可访问
  - 验证 Replay 工作区展示筛选/折叠动作
  - 验证 Replay 工作区联读腿级对账摘要

## Migration, Rollback, Compatibility
- 无 migration。
- 回滚时删除 replay 工作区和相关前端路由即可，不影响已有 replay API。

## Configuration and Environment Isolation
- 无新增配置。

## Code Organization and Dependencies
- 复用现有 helper：
  - `readableOverlayParentSignalSummary(...)`
  - `readableOverlayParentPostmortemMeta(...)`
  - `readableOverlayParentLegQuantitySummary(...)`
- 风险页继续保留摘要，不和 Replay 工作区重复承担完整职责。

## Documentation and Operations Manual
- 本文档记录这轮 replay 工作区收口内容。

## Deployment and Acceptance Criteria
- lint 通过
- 相关 unit / integration tests 通过
- 满足以下可视化结果：
  - 顶部导航可进入 `回放与复盘`
  - Replay 工作区可筛选 `仅库存活跃 / 仅目标活跃 / 目标与库存`
  - Replay 工作区可 `查看更多 / 收起历史`
  - Replay 工作区可直接联读最新 replay 父腿复盘与腿级 reconciliation 异常
