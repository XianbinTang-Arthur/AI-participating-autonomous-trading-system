# Task37 五阶段施工顺序总览

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../../docs/project_positioning.md)。


## 1. 总体目标

本施工计划的最终目标是把当前仓库重构为：

- 可直接管理真实资金
- 可长期无人值守运行
- 订单、成交、资金、恢复、对账均可证明一致
- 在外部系统异常、重启、乱序、重复回报场景下仍能恢复到唯一正确状态

目标系统分为五层：

1. `Market / Feature / Decision Layer`
2. `Execution Control Layer`
3. `Ledger Layer`
4. `Projection Layer`
5. `Recovery / Reconciliation / Operator Control Layer`

其中：

- `Ledger Layer` 是唯一资金真相源。
- `Execution Control Layer` 是唯一订单推进真相源。
- `Projection Layer` 只承担展示和查询。
- `Event Store` 降级为审计与回放辅助，不再承担资金真相职责。

## 2. Phase 1：新内核骨架与双写入口

### 目标

建立新执行内核和新资金内核的最小骨架，但不切断当前主业务链路。

### 核心任务

- 新增执行控制表。
- 新增账本表。
- 新增 reservation / settlement 表。
- 新增 inbox / outbox 边界表。
- 新增 repo 协议。
- 在现有下单和 fill 处理链路中接入 shadow write。

### 主要改动对象

- `aats/storage/sqlalchemy_models.py`
- `aats/storage/base.py`
- `aats/bootstrap/config.py`
- `aats/services/execution_engine/order_manager.py`
- 新增 `aats/storage/*_repo.py`

### 阶段验收

- 新表完成落库。
- 新 repo 可读写。
- 下单与 fill 至少可双写到新结构。
- 不破坏现有主路径测试。

## 3. Phase 2：执行状态机切换

### 目标

把执行链路从过程式函数调用改造成持久化状态机。

### 核心任务

- 引入 `execution_orders`。
- 引入 `execution_order_state_history`。
- 引入 `execution_commands`。
- 把 submit、cancel、sync 改造成命令状态机。
- 把 OKX REST / WS 回报统一接入 inbox 去重入口。

### 主要改动对象

- 新建 `aats/services/execution_control/order_service.py`
- 新建 `aats/services/execution_control/command_service.py`
- 新建 `aats/services/execution_control/fill_ingestion.py`
- 新建 `aats/services/execution_control/order_state_machine.py`
- `aats/services/execution_engine/okx_adapter.py`
- `aats/services/execution_engine/outbox.py`

### 阶段验收

- 每个外部命令可重试。
- 每个外部事件可幂等消费。
- 每次订单状态变更可追溯。
- 同一 venue fill 不会重复生效。

## 4. Phase 3：资金真相切到账本

### 目标

把资金真相从 `portfolio state / snapshot` 切换到账本。

### 核心任务

- 引入 `ledger_accounts`。
- 引入 `ledger_journals`。
- 引入 `ledger_entries`。
- 引入 `reservations`。
- 引入 `settlements`。
- 把 fill 结算逻辑迁移到账本 posting。
- 把保留金逻辑迁移到账本 reservation posting。
- 把持仓、权益、PnL 改造成账本和执行状态投影。

### 主要改动对象

- 新建 `aats/services/ledger/posting.py`
- 新建 `aats/services/ledger/reservation_posting.py`
- 新建 `aats/services/ledger/settlement_posting.py`
- `aats/services/accounting.py`
- `aats/services/execution_engine/obligations.py`
- `aats/services/portfolio_service/*`

### 阶段验收

- 所有余额变化都能由 ledger journal 解释。
- fill 和 settlement 显式分离。
- `portfolio_snapshot` 不再承担资金真相职责。
- projection 可由 ledger + execution 全量重建。

## 5. Phase 4：恢复与对账重写

### 目标

把恢复与对账从“快照修补”改成“确定性恢复 + 差异分类”。

### 核心任务

- 重写启动恢复逻辑。
- 重写 resume gate。
- 把 reconciliation 从 repair-oriented 改为 classify-oriented。
- 明确四类恢复结论：
  - `safe_to_resume`
  - `requires_manual_review`
  - `force_rebuild_projection`
  - `halt_and_de_risk`

### 主要改动对象

- `aats/services/execution_engine/recovery.py`
- `aats/services/reconciliation_service/repair.py`
- `aats/services/governance_engine/recovery_posture.py`
- 新建 `aats/services/recovery_control/*`

### 阶段验收

- 重启恢复以 execution + ledger + authoritative exchange state 为基础。
- 不再依赖隐式 snapshot 修补。
- 对账结论可直接驱动 operator 动作。
- 不确定状态默认停机。

## 6. Phase 5：控制面切换与旧内核下线

### 目标

完成 operator control plane、查询层和旧内核的最终切换。

### 核心任务

- operator query 切到新 projection。
- 强化鉴权、会话撤销、审批与审计能力。
- 下线旧 execution repo / portfolio repo / obligation repo。
- 把 event store 降级为审计与回放辅助。

### 主要改动对象

- `aats/services/operator/query_service.py`
- `aats/api/auth.py`
- `aats/api/auth_routes.py`
- `aats/api/session_auth.py`
- 旧 `execution_repo* / portfolio_repo* / obligation_repo*`

### 阶段验收

- API 读写路径都不再依赖旧资金真相模型。
- operator 仅能基于新状态机和新账本操作。
- 旧投影路径仅保留历史兼容用途。

## 7. 施工顺序约束

施工顺序必须严格遵守：

1. 先建表和 repo 骨架。
2. 再切执行状态机。
3. 再切资金真相。
4. 再切恢复与对账。
5. 最后切控制面与旧模块下线。

禁止跳过顺序直接切资金层或恢复层，否则会导致：

- 执行与资金缺少共同主键和幂等边界。
- 恢复无法基于新状态完整重建。
- operator 与对账继续依赖旧 projection。

## 8. 每阶段后的保留策略

- Phase 1 后：旧 execution / obligation / portfolio 路径继续承担生产兼容职责。
- Phase 2 后：旧订单推进逻辑逐步只读，新的 execution state machine 承担主职责。
- Phase 3 后：旧 portfolio snapshot 仅保留为 projection 和历史兼容层。
- Phase 4 后：旧 recovery / repair 逻辑降级为诊断辅助。
- Phase 5 后：旧资金真相路径全部退出生产主链路。
