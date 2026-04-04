# task35.3 风险页阻断控制面板 UI

## 目标

把“风险与恢复”页从固定按钮区改造成真正的 blocker control 面板，让用户按照优先级逐条处理阻断，而不是自己猜下一步操作。

## 设计目标

- 顶部只显示真实主阻断
- 操作建议区只处理当前第一优先级 blocker
- 次级阻断按优先级列表展示
- 每条 blocker 都带清晰动作
- 所有新增前端文案必须使用干净 UTF-8 中文

## 页面定位

### 风险与恢复页

是 blocker 的主处理入口。

### AI 工作台

只展示 AI blocker 的详情和联动操作，不应成为主恢复入口。

## 页面结构

### 1. 顶部状态条

展示：

- 当前是否已暂停
- 当前是否允许恢复
- 当前是否安全可交易
- 当前主阻断标题

### 2. 第一优先级阻断处置

这个区域替代旧的“操作建议”固定按钮区。

显示内容：

- blocker 标题
- blocker 描述
- 对系统的影响
- 推荐下一步
- 主动作按钮组

处理规则：

- 总是只显示当前第一优先级 blocker
- 当第一条 blocker 解决后，自动切换为下一条 blocker

### 3. 次级阻断列表

每条 blocker 显示：

- 标题
- 分类
- 影响说明
- 推荐动作
- 可执行按钮

### 4. AI 复核联动卡片

在 AI 工作台中展示：

- 为什么需要 AI 复核
- 当前 review 对系统的影响
- `确认恢复 AI 决策`
- `改为仅基础策略继续运行`

## 前端数据源

### 风险页

必须使用：

- `blockerControl`

不再自行拼接：

- `resume_blocked_reasons`
- 固定 reconciliation 按钮逻辑

### AI 工作台

也应使用同一份：

- `blockerControl`

## 动作交互

### client 动作

例如：

- 查看最新对账
- 打开 AI 工作台
- 刷新页面

### API 动作

统一调用：

- `POST /system/blocker-actions/{action_id}`

前端传入：

- `panel_version`
- `blocker`

## 文案要求

必须遵循：

- `docs/frontend_copy_guidelines.md`

### 强制要求

- 不允许新增乱码中文
- 不允许新增旧 takeover 术语
- 文案必须说明“原因 + 影响 + 下一步动作”

### 推荐标题

- `阻断控制面板`
- `第一优先级阻断处置`
- `次级阻断`
- `AI 复核处置`

## 禁止继续保留的旧设计

- 风险页固定对账按钮区
- 只显示表层 blocker，不显示根因
- 没有动作的 blocker 提示
- 需要人工复核但没有复核入口

## 前端状态命名建议

- `blockerControl`
- `primaryBlocker`
- `secondaryBlockers`

避免继续使用：

- 隐式拼装的 `riskActions`
- 与 blocker 无关的固定按钮集合

## 错误与冲突处理

### 动作执行成功

- 提示成功文案
- 自动刷新 blocker control

### `409 conflict`

表示：

- 状态已变化
- 当前 blocker 已被其他动作解决或替换

前端应：

- 提示“状态已刷新，请按最新阻断顺序处理”
- 自动刷新页面数据

## 验收标准

- 风险页“操作建议”区域已被第一优先级 blocker 面板替代
- AI review blocker 能在风险页和 AI 工作台都被处理
- 页面上不再出现旧 recommendation/rollback 控制面
- 页面文案为真实、准确、干净 UTF-8 中文

## 测试要求

- 风险页源码中存在：
  - `第一优先级阻断处置`
  - `阻断控制面板`
- AI 工作台源码中存在：
  - `AI 复核处置`
- 不再出现旧按钮文案：
  - `立即评估并生成建议`
  - `评估并允许自动切换`
  - `回滚到上一稳定策略档位`
- blocker action 点击后能携带 `panel_version` 发起请求
