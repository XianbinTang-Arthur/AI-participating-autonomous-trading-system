# Decision Truth Chain Pre-order Feasibility Linkage SOW

## Business objectives and boundaries

目标是在策略判断页的本轮摘要与最近决策记录中暴露已存在的 `no_trade_classification.pre_order_feasibility` 证据，使操作者不用只依赖详情抽屉也能追踪无交易结论对应的信号阈值、净边际、成本、盘口、流动性、策略门禁与风控门禁维度。

边界：只读展示，不改变策略、风控、执行、AI provider、symbol、venue、strategy family、release、promotion 或 tuning 行为。

## Module responsibilities and domain model

- `aats/api/static/modules/no-trade-display.js`：维护无交易分类和 pre-order feasibility 的展示语义。
- `aats/api/static/modules/views/strategy-view.js`：在策略页摘要和历史卡片中复用同一 evidence rows。
- `tests/integration/test_dashboard_ui.py`：验证 UI 输出由 payload 驱动，不生成推断。

核心领域对象为 `no_trade_classification.pre_order_feasibility`，其维度来自后端 read model，不在前端重新计算。

## Input/output interfaces

输入：`latestDecision` 与 `recentDecisions.decisions[]` 中现有的 `no_trade_classification.pre_order_feasibility` payload。

输出：策略页本轮工作区与历史 mobile card 中的只读中文行，包括执行可行性总览与已有维度。

## Database schema / tables / indexes / constraints

不涉及数据库 schema、索引或约束变更。

## Transactions, Consistency, Concurrency

不涉及交易或并发写入。前端只消费 dashboard bundle/API payload。

## Authorization, Authentication, Data Security

不新增 API，不降低认证要求。页面仍由原 dashboard/session 机制控制。不得输出凭证、token、数据库连接串或环境文件内容。

## Error Handling and Idempotency

若 payload 缺失 `pre_order_feasibility`，摘要/历史路径不渲染该证据行。若 payload 存在但部分维度缺失，由共享 helper 标注证据缺失或仅渲染已存在维度，避免前端推断。

## State Transition and Lifecycle

无状态迁移。该改动只扩展已部署 truth surface 的展示生命周期。

## Caching and Performance

只增加少量字符串渲染和数组映射，复用现有 dashboard bundle，无额外网络请求。

## Logging, Monitoring, Auditing

不新增日志。审计意义来自 UI 可见的 truth-chain 展示和自动化状态记录。

## Testing Strategy

扩展 dashboard UI 集成测试，覆盖：
- 本轮摘要展示 pre-order feasibility 总览和维度。
- 最近决策历史卡片展示同一 evidence rows。
- 历史叙事不回退到 AI timeout 或泛化 copy。

## Migration, Rollback, Compatibility

向后兼容。旧 payload 不带 `pre_order_feasibility` 时不新增展示。回滚方式为 revert 本次只读 UI commit。

## Configuration and Environment Isolation

不新增配置，不读取 `.env`。适用于当前 derivatives-live dashboard 静态模块。

## Code Organization and Dependencies

不新增依赖。共享 helper 放在 `no-trade-display.js`，策略页只 import helper，避免复制语义。

## Documentation and Operations Manual

本 SOW 记录任务边界和验收。部署仍必须使用 `scripts/deploy.sh`，本任务本轮不执行部署。

## Deployment and Acceptance Criteria

本轮 acceptance：
- 摘要/历史路径只在 payload 提供 evidence 时显示 pre-order feasibility。
- 显示内容复用同一维度 helper，不手写第二套语义。
- focused UI test 通过。
- 无 live order 行为变化。
- 不打印敏感信息。
