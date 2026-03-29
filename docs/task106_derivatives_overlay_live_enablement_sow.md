# Task 106：合约 Overlay 全量实盘放开前置能力补齐 SOW

## 1. 任务背景

当前合约 `hedge mode` 相关能力的真实状态如下：

- `protective` 已经具备可实盘的主路径能力。
- `opportunistic` 已具备配置、决策、执行语义与控制面展示，但仍缺少实盘级闭环验证。
- `independent` 已具备双书决策、腿级执行、腿级风控、腿级对账和 operator 可见性，但当前仍被配置层明确禁止直接放开到 `live`。

本任务的目标不是直接删限制，而是补齐“放开到实盘前必须具备的能力与验证链”，并按安全顺序推进，最终才允许移除 `independent live` 的硬限制并更新 live profile。

## 2. 当前行为摘要

当前系统对 overlay 的处理边界如下：

- `protective`
  - 已可在 `derivatives + hedge` 运行域真实生成腿级订单并进入实盘执行链。
- `opportunistic`
  - 已有独立配置、独立阈值、独立决策输出、独立 UI / audit 暴露。
  - 当前 `derivatives_live` profile 中 rollout stage 已允许到 `live`，但默认仍保持 `strategy_hedge_opportunistic_enabled=false`。
- `independent`
  - 已有 long book / short book 的独立阈值、独立冷却、独立 trial guard 语义。
  - 当前仍由 `settings` 校验明确禁止 `strategy_hedge_independent_rollout_stage=live`。

当前缺口不是“多配几个参数”，而是还缺实盘前必须有的：

- 主链端到端验证
- overlay bundle 的部分成功 / 部分失败恢复闭环
- persistence / replay / recovery / reconciliation 的 overlay 专项测试
- 放开 live 之前的验收门槛与回滚策略

## 3. 业务目标与边界

### 3.1 业务目标

本任务必须按顺序完成以下 4 个目标：

1. 补 protective / opportunistic / independent 的主链端到端集成测试
2. 补 overlay bundle 的部分成功 / 部分失败恢复语义
3. 补 persistence / replay / recovery / reconciliation 的 overlay 专项测试
4. 最后才移除 `settings.py` 中对 `independent live` 的硬限制，并更新 live profile

### 3.2 非目标

本任务明确不做以下事项：

- 不扩展新的 overlay 类型
- 不把 spot runtime 引入同样的 overlay live 放开任务
- 不改 public API 语义
- 不在缺少测试与恢复闭环的情况下提前打开 `independent live`
- 不将“删掉限制”视为任务完成

## 4. 模块职责与领域模型

### 4.1 决策与主链执行

- `aats/services/decision_engine/target_position.py`
  - 负责生成 `protective / opportunistic / independent` 的 `strategy_execution_legs`
- `aats/bootstrap/config.py`
  - 负责 `POSITION_TARGETS -> policy -> risk -> execution plan -> order intent -> strategy bundle`
  - 本任务中必须被主链测试直接覆盖

### 4.2 执行与恢复

- `aats/services/execution_engine/order_manager.py`
  - 负责腿级 intent 的最终提交与本地阻断
- `aats/services/execution_engine/bundle_recovery.py`
  - 负责 bundle 级恢复评估
- `aats/services/execution_engine/recovery.py`
  - 负责恢复状态与 bundle recovery 状态暴露

### 4.3 对账与回放

- `aats/services/reconciliation_service/comparator.py`
  - 负责对账 finding 归因
- `aats/services/reconciliation_service/replay.py`
  - 负责决策链 / 执行链 / audit 链回放验证

### 4.4 Operator 与控制面

- `aats/services/operator/query_service.py`
- `aats/api/static/modules/views/strategy-view.js`
- `aats/api/static/modules/detail-drawers.js`

本任务不要求新增新页面，但要求现有页面和 audit 输出在 overlay live 放开前能正确解释：

- 哪条腿来自哪种 overlay
- 哪一条腿失败
- 当前 bundle 是否进入恢复
- 是否允许 resume / only-reduce / manual review

## 5. 输入输出接口

### 5.1 输入

- `PositionTarget.strategy_execution_legs`
- `StrategyExecutionBundle`
- `LegOrderIntent`
- `OrderState`
- `FillEvent`
- `ReconciliationReport`
- `RecoveryStatusSnapshot`

### 5.2 输出

本任务完成后，系统必须能稳定输出：

- overlay bundle 的明确状态
  - `submitted`
  - `partial_fill_recovery`
  - `recovered`
  - `review_required`
- overlay 专项 recovery / reconciliation 摘要
- overlay live 准入是否满足验收条件

## 6. 数据库 / 表 / 索引 / 兼容性

本任务优先目标不是改 schema，而是补齐现有 schema 的行为闭环。

仅在以下前提下才允许新增字段：

- 无法表达 overlay bundle 的恢复状态
- 无法表达 overlay 腿来源
- 无法支持 replay / recovery 的正确归因

如需新增字段，必须满足：

- 向后兼容旧记录读取
- 默认值明确
- 不要求一次性回填全部历史数据

## 7. 事务、一致性、并发

本任务需要保住以下不变量：

- 同 symbol 的 `long/short` 腿必须保持独立账本
- bundle 内任一腿失败时，系统必须进入确定性的恢复状态，而不是默默留下一边成功一边失败的实盘暴露
- 重启后恢复视图必须与运行中视图一致
- replay 不能因为 overlay 而出现顺序依赖或状态丢失

## 8. 错误处理、幂等与重试

必须补齐以下行为：

- overlay bundle 中部分腿成功、部分腿失败时的分类
  - 哪些属于可恢复
  - 哪些属于必须人工确认
- 重试同一 overlay bundle 时，不能重复扩大腿暴露
- replay 同一决策链时，不能因为 overlay 导致 fill / order / bundle 对不上

## 9. 生命周期与状态顺序

overlay live 放开前，必须明确并测试以下生命周期顺序：

1. 决策层生成 overlay 腿
2. 主链发布 execution plans / order intents
3. 腿级订单执行
4. bundle 状态更新
5. 若存在部分成功 / 部分失败，进入 bundle recovery
6. 若恢复失败或状态不可信，进入 review_required / only_reduce / resume_blocked
7. 恢复完成后，bundle 状态回到可解释的终态

## 10. 日志、监控、审计

必须保证以下审计问题可以直接回答：

- 当前 live 运行线是否启用了哪种 overlay
- 当前 overlay bundle 里哪些腿已提交、已成交、失败、待恢复
- 当前恢复是因为哪条腿导致
- 当前是系统自动恢复还是人工复核必需

## 11. 测试策略

### 11.1 Phase A：主链端到端集成测试

必须新增或补齐：

- `protective` 主链端到端测试
- `opportunistic` 主链端到端测试
- `independent` 主链端到端测试

覆盖链路必须包含：

- `run_cycle()`
- `POSITION_TARGETS`
- `handle_position_target()`
- `StrategyExecutionBundle`
- `ORDER_INTENTS`
- `execution_repo`

### 11.2 Phase B：overlay bundle 恢复语义

必须补齐：

- 一腿成功、一腿失败时的 bundle 状态定义
- 对应 recovery / reconciliation / operator 行为
- 至少一组集成测试覆盖 overlay bundle 部分失败

### 11.3 Phase C：持久化 / 回放 / 恢复 / 对账专项测试

必须补齐：

- `persistence_and_replay`
  - overlay bundle 回放一致性
- `recovery`
  - overlay bundle 重启恢复
- `reconciliation`
  - overlay 腿对账与异常归因

### 11.4 Phase D：放开 independent live

只有当前三阶段全部通过，才允许：

- 移除 `settings.py` 中 `strategy_hedge_independent_rollout_stage_live_not_allowed_in_phase_d`
- 更新 `derivatives_live.yaml`
- 新增 live profile 配置测试

## 12. 迁移、回滚、兼容

### 12.1 迁移顺序

1. 先补测试
2. 再补 overlay bundle 恢复闭环
3. 再补 replay / recovery / reconciliation 专项验证
4. 最后才移除 independent live 限制并更新 live profile

### 12.2 回滚策略

一旦 overlay live 放开后发现问题，回滚顺序必须是：

1. 先关 `strategy_hedge_independent_enabled`
2. 再把 `strategy_hedge_independent_rollout_stage` 回退到 `dry_run`
3. 如有必要，再关闭 `strategy_hedge_opportunistic_enabled`
4. `protective` 保持作为最后兜底路径

## 13. 配置与环境隔离

配置目标状态建议为：

- `derivatives_live`
  - `protective`: enabled
  - `opportunistic`: enabled only after专项测试通过
  - `independent`: enabled only after专项测试和恢复闭环全部通过

不允许：

- 在没有专项集成测试之前直接把 `independent` 放到 live
- 在没有 bundle recovery 语义前直接把所有 overlay 同时实盘化

## 14. 代码组织与依赖

推荐拆成 4 个开发阶段：

- `Task106-A`
  - 主链端到端 overlay 集成测试
- `Task106-B`
  - overlay bundle 部分成功 / 部分失败恢复语义
- `Task106-C`
  - persistence / replay / recovery / reconciliation 专项测试
- `Task106-D`
  - 移除 independent live 硬限制并更新 live profile

## 15. 部署与验收标准

以下条件全部满足前，不得声称“protective / opportunistic / independent 都可实盘”：

- 三类 overlay 都有主链端到端集成测试
- overlay bundle 部分成功 / 部分失败有确定性恢复语义
- replay / recovery / reconciliation 都有 overlay 专项测试
- `independent live` 限制移除前，所有相关测试通过
- live profile 更新后，配置测试和最窄 live submit 路径测试通过

## 16. 最终一句话目标

本任务的完成标准不是“把开关打开”，而是：

**让 `protective / opportunistic / independent` 在合约 hedge live 下具备可验证、可恢复、可回放、可对账、可回滚的完整实盘闭环。**
