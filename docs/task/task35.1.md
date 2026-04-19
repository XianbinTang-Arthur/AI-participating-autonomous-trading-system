# task35.1 阻断控制后端模块

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 目标

把当前分散在恢复状态、健康检查、AI 工作台、风险页里的阻断信息统一收口为一个独立后端模块，输出统一的 blocker control 快照，供风险页、AI 工作台和后续动作接口共同消费。

## 范围

- 新增后端模块：`aats/services/blocker_control/`
- 新增结构化 schema：`aats/schemas/blocker_control.py`
- 在 operator query 层新增 blocker control 聚合输出
- 不在本任务里直接处理具体恢复动作的业务逻辑
- 不在本任务里修改执行、订单、对账核心链路

## 目标结果

后端能够生成统一的 `BlockerControlSnapshot`，至少包含：

- `panel_version`
- `halted`
- `review_required`
- `resume_eligible`
- `safe_to_trade`
- `primary_blocker`
- `secondary_blockers`
- `blockers`
- `next_step_summary`

每条 blocker 至少包含：

- `blocker`
- `category`
- `subsystem`
- `priority`
- `resolution_mode`
- `title`
- `description`
- `impact`
- `recommended_next_step`
- `root_cause`
- `derived_from`
- `affects_execution`
- `submit_only`
- `actions`

## 模块设计

### 建议目录

- `aats/services/blocker_control/service.py`
- `aats/services/blocker_control/priority.py`
- `aats/services/blocker_control/actions.py`
- `aats/services/blocker_control/__init__.py`

### 职责划分

#### `service.py`

负责：

- 聚合原始 blocker 来源
- 计算主阻断和次级阻断
- 计算 blocker 优先级
- 生成统一 blocker copy
- 生成前端动作定义

不负责：

- 直接执行业务动作

#### `priority.py`

负责：

- blocker 优先级规则
- 根因 blocker 与表象 blocker 的排序策略

#### `actions.py`

负责：

- blocker action 的统一执行入口
- 动作前置校验
- 动作执行结果封装

## blocker 来源

本模块要统一消费的后端状态源：

- `health_service.snapshot()`
- `system_mode()`
- `recovery_view()`
- `kill_switch.halted`
- `latest_reconciliation`
- `ai_service.status()`

## blocker 分类

### `system_execution`

影响系统能否恢复自动运行。

示例：

- `reconciliation_halt_required`
- `operator_rebaseline_required`
- `ai_degraded_requires_manual_review`
- `account_snapshot_missing`
- `market_data_stale`

### `submission_mode`

系统可运行，但不会真实提交交易所。

示例：

- `guarded_execution_dry_run`
- `local_demo_no_exchange_submission`
- `real_market_paper_uses_local_paper_execution`

### `ai_decision`

只影响 AI 决策链，不一定影响系统整体运行。

### `profile_control`

只影响自动切档，不应抢占系统主阻断。

## 优先级原则

默认优先级从高到低：

1. `reconciliation_halt_required`
2. `operator_rebaseline_required`
3. `ai_degraded_requires_manual_review`
4. `account_snapshot_missing`
5. `account_state_stale`
6. `market_connection_down`
7. `market_data_stale`
8. `rebaseline_in_progress`
9. `kill_switch_active`
10. 其他 blocker

### 特别规则

- `kill_switch_active` 允许存在，但默认不作为主根因
- 当存在更高优先级 blocker 时，应将 `kill_switch_active` 视作派生状态
- `submit_only` blocker 不应覆盖真正的系统运行阻断

## 数据模型要求

### `BlockerActionDefinition`

至少包含：

- `action_id`
- `label`
- `kind`
- `method`
- `endpoint`
- `client_action`
- `value`
- `tone`
- `requires_confirmation`
- `confirmation_title`
- `confirmation_copy`
- `expected_effect`

### `BlockerControlItem`

除 blocker 信息外，还必须携带：

- `actions`
- `root_cause`
- `derived_from`

### `BlockerControlSnapshot`

必须可直接作为前端主数据源使用，不需要前端二次推断。

## 接口定义

### `GET /system/blocker-control`

返回统一 blocker control 快照。

### Query facade

新增：

- `OperatorQueryService.blocker_control()`
- `RuntimeQueryFacade.blocker_control()`

## 验收标准

- 风险页和 AI 工作台能消费同一份 blocker control 数据
- `primary_blocker` 不再默认等于 `kill_switch_active`
- blocker 数据可以直接驱动动作按钮和状态说明
- 主阻断、次级阻断、下一步动作都由后端直接定义

## 测试要求

- 返回结构测试
- 优先级测试
- 根因 blocker 测试
- `kill_switch_active` 降级为表象 blocker 测试
- `submit_only` blocker 不抢占主阻断测试
