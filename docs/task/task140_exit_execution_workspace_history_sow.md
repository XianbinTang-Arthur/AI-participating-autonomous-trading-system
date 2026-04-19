# Task 140 - Exit Execution Operator 工作区列表

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 目标

- 把 parent-exit action history 从风险页局部卡片推进成独立的 operator 工作区列表。
- 支持按 `parent_intent_id / action / actor / 时间窗口` 独立筛选。
- 支持分页查看更长的 parent-exit history。
- 让卡片和工作区共用同一套筛选状态，避免“卡片只做局部过滤，工作区条件不同步”。

## 范围

- `aats/services/operator/query_service.py`
  - 抽出通用的 parent-exit action history 查询
  - 新增过滤与分页能力
- `aats/api/routes.py`
  - 新增 `GET /system/exit-execution/action-history`
- `aats/api/static/modules/store.js`
  - 风险页增加 exit-execution workspace 筛选/分页 UI state
  - risk bundle 增加独立 history panel
- `aats/api/static/app.js`
  - 筛选状态跨卡片/工作区同步
  - 应用筛选、重置筛选、上一页/下一页动作
- `aats/api/static/modules/views/risk-view.js`
  - 新增 `退出任务工作区` section
  - 保留卡片级最近历史，同时新增独立长历史列表
- 测试
  - `tests/integration/test_operator_api.py`
  - `tests/integration/test_dashboard_ui.py`

## 不做

- 不新增单独的前端 route/view
- 不新增新的持久化表
- 不改 parent-exit action event schema

## 验收

- `/system/exit-execution/action-history` 支持过滤和分页。
- 风险页出现独立的 `退出任务工作区` section。
- 卡片和工作区使用同一套筛选条件。
- 工作区能翻页看更长的 parent-exit 历史。
