# Operator Truth Cockpit SOW

## Business Objectives And Boundaries

在总览页提供第一个只读的运行真相驾驶舱，把 OKX BTC-USDT-SWAP directional 实盘显微镜需要的运行态、策略载体、门禁、决策、执行、成交和阻断事实放到同一个入口。范围只包含前端展示与现有只读 panel 取数，不改变策略、风控、执行、AI provider、schema、symbol、venue、strategy family、release、promotion 或 live order behavior。

## Module Responsibilities And Domain Model

`aats/api/static/modules/store.js` 负责让 overview bundle 拉取现有 `/strategy/runtime` panel。`aats/api/static/modules/views/overview-view.js` 负责把 `aiRuntime`、`strategyRuntime`、`latestDecision`、`executionLatest`、`blockers`、`metrics` 聚合为用户可读状态。`terms.js` 只补充运行态来源文案。

## Input/Output Interfaces

输入来自已有 dashboard bundle panel：`/ai/runtime`、`/strategy/runtime`、`/decision/latest`、`/execution/latest`、`/system/blockers`、`/system/metrics`。输出为总览页的中文 HTML 片段和既有 `navigate-view` 下钻按钮。

## Database Schema / Tables / Indexes / Constraints

不新增、不修改数据库 schema、表、索引或约束。

## Transactions, Consistency, Concurrency

本任务只读现有 API payload，不引入事务、不写数据库、不改变并发决策或订单状态机。显示一致性沿用 dashboard bundle 的 panel cache 与刷新机制。

## Authorization, Authentication, Data Security

沿用现有 operator dashboard 认证与 bundle auth 处理。前端不显示凭证、token、API key、数据库密码或完整连接串。

## Error Handling And Idempotency

缺失 panel 字段时使用 “待确认/未验证/暂无” 文案降级。重复刷新只重绘 UI，不产生副作用。

## State Transition And Lifecycle

不新增运行状态，不改变订单、fill、position lifecycle。驾驶舱只是把现有状态的最新快照投影到总览页。

## Caching And Performance

新增 overview 对 `/strategy/runtime` 的一次只读 panel 拉取。该 panel 已存在于策略页并走现有 bundle 机制；本任务不新增后端查询路径。

## Logging, Monitoring, Auditing

不新增日志与审计事件。验收通过运行静态回归测试和部署后 runtime truth smoke。

## Testing Strategy

新增 `tests/unit/test_operator_truth_cockpit_wiring.py`，静态验证 overview 拉取 `strategyRuntime`、驾驶舱文案/字段/下钻入口存在，并验证新增运行态来源文案本地化。

## Migration, Rollback, Compatibility

无需迁移。回滚方式是恢复 `store.js`、`overview-view.js`、`terms.js` 与新增测试/文档。该变更不修改公开 API，兼容已有 dashboard bundle。

## Configuration And Environment Isolation

不新增配置项，不改变 `.env`、profile、provider、symbol、venue 或 strategy family。

## Code Organization And Dependencies

复用现有 `surfaceCard`、`summaryStrip`、`timeline`、`actionButton`、`readableState` 等前端组件；不新增 npm/Python 依赖。

## Documentation And Operations Manual

本 SOW 记录变更边界与验收标准。运行入口在总览页，点击“策略/执行/风控/AI分析”进入对应已有页面。

## Deployment And Acceptance Criteria

验收标准：总览页首屏显示运行真相驾驶舱；能同时看到有效运行态、AI 服务、策略载体、准入门禁、决策到执行、成交证据、阻断队列；下钻按钮指向已有页面；`directional_1h` 仍显示为未验证；lint、聚焦测试和完整单元测试通过；部署只能使用 `scripts/deploy.sh`。
