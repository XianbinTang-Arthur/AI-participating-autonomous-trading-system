# Task 139 - Parent Exit Timeline 排序修复与本地筛选

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 目标

- 修复 parent-exit operator action 时间线按事件追加顺序展示的问题，改为按动作时间倒序展示。
- 给风险页的 `退出任务处理时间线` 增加本地筛选，支持按动作类型、父任务 ID、操作人快速收敛查看范围。

## 范围

- `aats/services/operator/query_service.py`
  - 统一 parent-exit action history / recent action 的排序语义
  - `created_at` 缺失时回退到 event envelope timestamp
- `aats/api/static/modules/store.js`
  - 增加风险页时间线筛选 UI state
- `aats/api/static/app.js`
  - 接入时间线筛选事件处理
- `aats/api/static/modules/views/risk-view.js`
  - 渲染筛选控件
  - 始终渲染全量条目，再按筛选条件隐藏/显示
- 测试
  - `tests/integration/test_operator_api.py`
  - `tests/integration/test_dashboard_ui.py`

## 不做

- 不新增后端筛选 API
- 不改 operator action 持久化 schema
- 不改 parent-exit review / reconciliation 语义

## 验收

- `exit_execution_action_history` 与 per-parent `latest/recent_operator_action` 都按动作时间倒序。
- 风险页时间线可以按 `动作 / 父任务 / 操作人` 筛选。
- 放宽筛选条件后，不需要额外请求也能重新看到之前被隐藏的条目。
