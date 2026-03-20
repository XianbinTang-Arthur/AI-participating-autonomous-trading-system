# task35.2 第一批 blocker 动作接口

## 目标

为第一批系统级 blocker 补齐可执行的后端动作接口，完成“看得到阻断，也能处理阻断”的闭环。

## 范围

第一批只覆盖对恢复最关键的动作：

- 重新对账
- 接受当前状态为新基线
- 恢复自动运行
- 保持暂停
- 确认恢复 AI 决策
- 改为仅基础策略继续运行

## 设计原则

### 1. 动作必须幂等

重复点击不应破坏状态。

### 2. 动作执行前必须重新校验

校验项：

- `panel_version`
- 目标 blocker 是否仍处于 active
- 动作前置条件是否仍成立

### 3. 动作执行后必须返回最新 blocker control 快照

避免前端继续使用过期状态。

## 接口定义

### `POST /system/blocker-actions/{action_id}`

请求体：

- `panel_version`
- `blocker`
- `reason`

返回：

- `action_id`
- `status`
- `message`
- `blocker_control`

### 专用动作接口

同时保留显式接口，供其他页面或后续联动调用：

- `POST /system/rebaseline`
- `POST /system/resume`
- `POST /system/halt`
- `POST /system/ai-review/restore`
- `POST /system/ai-review/degrade-to-baseline`

## 动作清单

### `reconcile-now`

作用：

- 立即执行一次对账验证

结果：

- 刷新最新对账
- 重新计算恢复状态

### `accept-rebaseline`

作用：

- 接受当前状态为新基线

结果：

- 写入新的 baseline
- 刷新恢复资格

### `resume-system`

作用：

- 请求恢复自动运行

结果：

- 重新校验 blocker
- 如通过则清除 kill switch
- 如不通过则返回最新 blocker control

### `halt-system`

作用：

- 明确保持暂停

结果：

- 维持 kill switch
- 记录 operator action

### `ai-review-restore`

作用：

- 人工确认本次 AI review 已审阅，并允许恢复 AI 决策链路

结果：

- 清除 `outcome_review_required`
- 保留当前 AI mode
- 不直接恢复下单
- 只恢复 AI 决策资格

### `ai-review-degrade-to-baseline`

作用：

- 人工否决当前 AI 主导，改为仅基础策略继续运行

结果：

- 清除 `outcome_review_required`
- 设置 manual override mode = `baseline_only`
- 解除当前 AI review 阻断
- 系统后续可在 baseline_only 下继续运行

## 与现有后端函数的联动

可直接复用：

- `validate_reconciliation(...)`
- `rebaseline(...)`
- `resume(...)`
- `halt(...)`

需要新增：

- `ai_review_restore(...)`
- `ai_review_degrade_to_baseline(...)`
- blocker action dispatcher

## 新增运行时语义

AI review 不是单纯解锁按钮，必须明确区分两条路径：

### 恢复 AI 决策

- 后续继续允许 AI 主导

### 降为 baseline_only

- 系统继续运行
- 但 AI 不再主导决策

## 状态一致性要求

- AI review 动作不能直接产生订单
- AI review 动作不能直接改仓
- AI review 动作不能默认强制切档
- 只改变后续决策权和恢复资格

## 错误处理

### `409 conflict`

适用于：

- `panel_version` 过期
- blocker 已不再 active

### `400 bad request`

适用于：

- action 不支持
- 当前状态不满足前置条件

## 测试要求

- `GET /system/blocker-control` 返回正确动作列表
- `POST /system/blocker-actions/{action_id}` 成功路径
- `panel_version` 过期返回 409
- blocker 已消失返回 409
- `ai-review-restore` 清除 review 阻断
- `ai-review-degrade-to-baseline` 将 manual override 切到 `baseline_only`
- `resume-system` 在仅剩 `kill_switch_active` 时可恢复
- `resume-system` 在真实 blocker 仍存在时继续被阻断
