# RDP 手动入口与审批后续动作收口 SOW

## Business objectives and boundaries
- 只保留两个手动入口：
  - `刷新数据`
  - `运行完整 RDP`
- 不新增 Step2/Phase3/Phase4/Phase5/Phase6 的单独按钮。
- 解决“审批后不知道下一步”的断点：
  - `parameter_upgrade` 批准后，页面必须出现可执行的 `运行 Gate / 创建发布`
  - `keep_active / lower_priority / pause / require_review` 必须明确说明“不会创建新发布”
- 收紧首页中文文案，移除原始技术串和过度解释。

## Module responsibilities and domain model
- `aats/api/rdp_control_summary.py`
  - 输出两个手动 workflow 动作
  - 输出待处理组合卡片
  - 新增“待发布候选”数据模型
  - 输出更短、更面向中文用户的摘要字段
- `aats/api/static/modules/views/rdp-control-panel.js`
  - 渲染两个手动按钮
  - 渲染待处理组合 + 待发布候选
  - 中文化证据明细中的阶段、状态、指标和来源轮次
- `aats/api/static/modules/actions/rdp-actions.js`
  - 手动 workflow 触发提示改成中文
  - 审批成功后的提示明确说明下一步去哪里操作

## Input/output interfaces
- 继续复用：
  - `POST /rdp/tasks/trigger`
  - `GET /rdp/workbench/overview`
  - `GET /rdp/workbench/items`
  - `POST /rdp/gates/run`
  - `POST /rdp/releases/create`
- 不新增新的写接口。

## Database schema / tables / indexes / constraints
- 无 schema 变更。

## Transactions, consistency, concurrency
- 继续复用现有 `rdp_task_queue` 与 daemon 串行消费语义。
- 同一 workflow 已有 `running/pending` 时，手动触发按钮必须禁用。

## Authorization, Authentication, Data Security
- 所有手动入口继续要求 `require_write_access`。
- 不读取、不输出任何凭证。

## Error Handling and Idempotency
- 手动 workflow 按钮必须提供禁用原因。
- `parameter_upgrade` 批准后，如果页面刷新成功，必须能看到“待发布候选”区块。
- 证据不完整时，审批继续 fail-closed。

## State Transition and Lifecycle
- `parameter_upgrade`
  - 批准后进入“待发布候选”
  - 下一步是 `运行 Gate` 或 `创建发布`
- `keep_active / lower_priority / pause / require_review`
  - 批准后只记录治理结论
  - 不进入发布链

## Caching and Performance
- 继续复用 request 级 `build_rdp_control_summary()` 缓存。
- 不新增额外数据库写入。

## Logging, Monitoring, Auditing
- 继续复用现有审批、Gate、发布、任务触发日志链。

## Testing Strategy
- 更新 `tests/unit/test_rdp_control_summary.py`
  - 两个手动入口
  - 待发布候选
  - 更明确的审批按钮文案
- 更新 `tests/integration/test_dashboard_ui.py`
  - 首页只显示两个手动入口
  - 待发布候选区块可见
  - 证据详情中文化

## Migration, Rollback, Compatibility
- 向后兼容现有写接口。
- 旧 UI 若还依赖原文案，需要同步更新 fixture/assertions。

## Configuration and Environment Isolation
- 无新增配置。

## Code Organization and Dependencies
- 只改 RDP workbench 读模型、前端渲染和动作提示。
- 不做无关 refactor。

## Deployment and Acceptance Criteria
- 首页出现两个明确按钮：
  - `刷新数据`
  - `运行完整 RDP`
- `parameter_upgrade` 批准后，页面出现“待发布候选”
- 待发布候选卡片带：
  - `运行 Gate`
  - `创建发布`
- `keep_active` 类按钮文案改成明确动作，如 `确认保持当前`
- 首页主卡和证据详情不再直接展示原始技术串
