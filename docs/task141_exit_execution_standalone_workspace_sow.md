# Task 141 - Exit Execution 独立前端工作台

## 业务目标与边界

- 把 parent-exit operator 工作区从风险页内的局部 section 提升为独立前端工作台。
- 支持按 `parent_intent_id / action / actor / 时间窗口` 独立筛选，并允许通过 URL 直接分享或恢复当前筛选状态。
- 支持更长历史分页，不再只依赖风险页内的小窗口时间线。
- 保留风险页内的摘要卡片和最近处理时间线，不移除现有 risk 页面入口。

## 模块职责与领域模型

- `aats/api/ui.py`
  - 新增 `/ui/exit-execution` 页面入口。
- `aats/api/static/dashboard-shell.html`
  - 新增独立导航项与 `data-view="exitExecution"` 的内容容器。
- `aats/api/static/modules/store.js`
  - 区分风险页与独立工作台各自的分页状态。
  - 共享 parent-exit 历史筛选字段，避免两套筛选语义漂移。
- `aats/api/static/app.js`
  - 新增独立 `exitExecution` 视图渲染。
  - 将共享筛选状态与 `/ui/exit-execution` 的查询参数双向同步。
  - 风险页跳转到独立工作台时保留当前筛选语义。
- `aats/api/static/modules/views/risk-view.js`
  - 保留风险页中的最近处理时间线和工作区摘要。
  - 增加“进入独立工作台”入口。
- `aats/api/static/modules/views/exit-execution-view.js`
  - 新增独立工作台视图，承载长历史分页与筛选结果。

## 输入 / 输出接口

- 前端页面路由
  - `GET /ui/exit-execution`
- 已有读接口复用
  - `GET /system/exit-execution/action-history`
- URL 查询参数
  - `parent_intent_id`
  - `action`
  - `actor`
  - `window_hours`
  - `offset`
  - `limit`

## 数据库 / 事务 / 一致性

- 本任务不新增数据库表，不修改持久化事务。
- URL 恢复仅影响前端 UI 状态，不改变后端执行状态。

## 认证、授权与安全

- 沿用现有 dashboard 登录控制与 operator action 权限控制。
- 独立工作台是读视图；写动作仍通过既有 operator API 执行。

## 错误处理与幂等

- 查询参数非法时回退到安全默认值，不抛前端异常。
- 筛选条件同步只更新本地 UI 状态，不产生额外写入副作用。

## 状态迁移与生命周期

- 风险页筛选变更后，同步写入独立工作台的共享筛选字段。
- 独立工作台载入时，从 URL 恢复筛选与分页状态。
- 独立工作台中的筛选、分页变更即时回写到 URL，便于刷新和分享。

## 性能与缓存

- 独立工作台继续复用现有 `/system/exit-execution/action-history` 分页接口。
- 风险页保持当前的较小窗口；独立工作台使用更长历史分页。

## 日志、监控、审计

- 本任务不新增后端审计模型。
- operator 动作时间线仍复用已有 action event 数据。

## 测试策略

- `tests/integration/test_dashboard_ui.py`
  - 新增 `/ui/exit-execution` 路由与独立视图断言。
  - 校验 app.js 已接入独立 view、URL 状态恢复和工作区导航。
- `tests/integration/test_operator_api.py`
  - 继续覆盖 action-history 过滤与分页接口。

## 迁移、回滚、兼容性

- 无数据库迁移。
- 风险页原有 parent-exit 工作区与时间线保持兼容。

## 配置与环境隔离

- 无新增环境变量。

## 代码组织与依赖

- 新增独立 `exit-execution-view.js`，尽量复用 risk-view 里已稳定的 parent-exit 渲染函数。
- 不引入新的第三方依赖。

## 文档与运维

- 本 SOW 作为当前阶段的设计记录。

## 部署与验收标准

- `/ui/exit-execution` 可访问并渲染独立工作台。
- 支持按 `parent_intent_id / action / actor / 时间窗口` 筛选，并通过 URL 恢复。
- 风险页可直接跳转到独立工作台，且筛选条件同步。
- lint、单测和最窄 UI/接口集成测试通过。
