# RDP 点击路径全链路审查与修复 SOW

## 背景

本轮任务不是只看某一个前端组件，而是从用户点击 RDP 面板按钮开始，沿着：

- 前端事件分发
- action handler
- API route
- 后端治理 / 发布 / 观察 / 回滚服务
- control-summary 回流

做一遍完整审查，并手工跑通关键链路。

## 审查范围

### 前端入口

- `aats/api/static/modules/views/rdp-control-panel.js`
- `aats/api/static/modules/actions/rdp-actions.js`
- `aats/api/static/app.js`

### 后端入口

- `aats/api/rdp_routes.py`
- `aats/api/rdp_control_summary.py`

### 关键后端服务

- `aats/data_platform/decision_system/recommendation_registry.py`
- `aats/data_platform/decision_system/active_parameter_apply.py`
- `aats/data_platform/production_workflow/release_registry.py`
- `aats/data_platform/production_workflow/observation_window.py`
- `aats/data_platform/governance/rdp_task_db.py`

## 手工链路

按以下顺序做隔离验证：

1. `GET /rdp/control-summary`
2. `POST /rdp/tasks/trigger`
3. `POST /rdp/recommendations/{id}/approve`
4. `POST /rdp/releases/create`
5. `POST /rdp/observations/run`
6. `POST /rdp/parameters/rollback`
7. 再次 `GET /rdp/control-summary`

## 本轮发现的问题

### 1. 已应用 recommendation 仍被算作“待发布”

- release 创建成功并 apply 后，`control-summary.operations_summary.approved_release_candidate_count` 仍然保留旧值。
- 根因：`pending_recommendations` 和 `approved_release_candidate_count` 只按 recommendation status 判断，没有排除已经进入 `active_parameter_sets` 的 approved recommendation。

### 2. 已回滚的旧 release 仍然留在 observation queue

- rollback 完成后，旧 release 已经不是当前 active 参数版本，但仍会继续出现在 `observation_queue`，导致 UI 继续把它显示为“当前待处理”。
- 根因：`_build_observation_queue()` 只看 release 的 `observation_status`，没有结合 `is_current_active_release` 过滤掉已经失效的 release。

## 修复目标

1. `pending_recommendations` 只保留真正待处理的 recommendation：
   - draft recommendation
   - approved 但尚未成为当前 active set 的 parameter_upgrade
2. `approved_release_candidate_count` 与 UI 队列口径一致。
3. `observation_queue` 只保留当前仍有操作意义的 release，不再把已回滚/已失效的旧 release 当成当前队列。

## 验证

- 单测：
  - `test_rdp_control_summary.py`
  - 覆盖已应用 approved recommendation 的过滤
  - 覆盖非当前 active release 的 observation queue 过滤
- 集成：
  - `test_rdp_production_workflow_api.py`
  - 用隔离临时项目根目录跑一遍 approve → release → observation → rollback → control-summary
