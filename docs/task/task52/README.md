# Task52 - Phase 2 执行命令流落地

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../../docs/project_positioning.md)。


## 目标
本任务将 Phase 2 的最小可运行主路径正式落地：

- `submit / cancel` 从同步过程调用切为持久化命令
- runtime 增加后台命令 worker
- 旧 `execution_repo / obligation / portfolio` 主真相暂不切换
- Phase 1 影子层继续保留并与新命令流并存

## 本次交付范围

### 新增模块
- `aats/services/execution_control/order_service.py`
  - 负责提交/撤单命令入队
- `aats/services/execution_control/command_service.py`
  - 负责后台消费 `execution_commands`
- `aats/services/execution_control/order_state_machine.py`
  - 将现有订单状态机迁移到 execution_control 命名空间

### 运行时接线
- `AATSSettings` 新增：
  - `execution_command_flow_enabled`
  - `execution_command_poll_interval_seconds`
- `build_runtime()` 在配置开启且存在持久化命令 repo 时会装配：
  - `ExecutionOrderService`
  - `ExecutionCommandProcessor`
- `ApplicationRuntime.start_background_tasks()` 会启动：
  - `aats_execution_command_flow`

### OrderManager 行为变化
- 当 `execution_command_flow_enabled = true`：
  - `handle_order_intent()` 只会先落 `CREATED`，再持久化 `submit` 命令
  - 实际 adapter 提交由后台 worker 调用 `process_submit_command()` 完成
  - `cancel_order()` 会先落 `CANCEL_PENDING`，再持久化 `cancel` 命令
  - 实际 adapter 撤单由后台 worker 调用 `process_cancel_command()` 完成
- 当开关关闭时：
  - 保持原同步执行路径不变

## 为什么默认关闭
Phase 2 改变了提交语义：

- 同步路径：`handle_order_intent -> adapter.submit -> final state`
- Phase 2 路径：`handle_order_intent -> enqueue submit command -> worker 执行`

这属于生产语义变化，而不是透明重构，所以先以显式开关上线更安全。

## 当前验收结果
- 单元测试：
  - `tests/unit/test_task52_execution_command_flow.py`
- 运行时集成测试：
  - `tests/integration/test_phase2_command_flow_runtime.py`
- 回归验证：
  - `72 passed, 17 skipped`
  - `5 passed`

## 当前边界
本次 Phase 2 仍然没有完成以下切换：

- 新 `execution_orders / execution_fills / reservations / ledger_*` 还不是主真相
- `command_outbox / external_event_inbox` 还没有进入主消费闭环
- operator API 还没有专门展示 Phase 2 command processor 视图

## 下一阶段建议
Phase 3 应开始把：

- `fill -> settlement -> ledger journal`
- `portfolio snapshot -> projection only`
- `reservation -> ledger-backed source of truth`

逐步切到新的账本主链。
