# 合约运行模式链路复核与最小修复 SOW

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 业务目标与边界
- 目标：复核当前系统在 `derivatives` 运行模式下的全链路业务逻辑，定位一个明确且可复现的不合理点，并以最小改动修复。
- 边界：不改动合约 pre-trade 风控公式、不改动执行适配器下单语义、不调整公开 API，不做无关重构。

## 当前行为摘要
- 合约对账已经能把“交易所存在仓位，但本地没有可回放执行链”归类为 `only_reduce_required=true`，并给出 `go_close_position_on_exchange` 建议动作。
- 合约风控也会读取 reconciliation / recovery 的 `only_reduce_required`，阻止继续新增暴露。
- 不合理点在恢复层：启动恢复与运行态恢复评估仍可能把 `only_reduce` 对外标成 `safe_to_trade=true`，导致控制面、策略档位治理和部分下游判断把“只允许减仓”误读为“可以继续自动交易”。

## 模块职责与领域模型
- `ExecutionRecoveryService`：生成启动恢复后的基础 `RecoveryStatus`。
- `RecoveryPostureEvaluator`：按当前 reconciliation、health、kill switch、bundle recovery 重新归一化恢复状态。
- `RiskEngine`：消费恢复态与 only-reduce 约束，决定是否允许继续新增暴露。
- 领域状态重点：`recovery_state`、`safe_to_trade`、`resume_eligible`、`only_reduce_required`。

## 输入 / 输出接口
- 输入：`ReconciliationReport`、`RecoveryStatus`、runtime health / kill switch / bundle recovery 状态。
- 输出：归一化后的 `RecoveryStatus` 与 operator recovery view。
- 本次不新增字段，只修正既有字段语义：`only_reduce` 不再被同时视为 `safe_to_trade`。

## 数据库 Schema / 表 / 索引 / 约束
- 不变更 schema。
- 现有 `ReconciliationStateSnapshot.safe_to_trade` 会随本次语义修正持久化更准确的恢复状态。

## 事务、一致性与并发
- 不引入新事务边界。
- 修复仅改变恢复状态计算条件，不改变仓位、委托、成交持久化顺序。

## 鉴权、认证与数据安全
- 不涉及鉴权、认证或敏感数据读写变更。

## 错误处理与幂等
- 保持现有恢复流程与幂等路径不变。
- 如果 reconciliation 继续要求 `only_reduce`，重复归一化仍会稳定输出相同恢复语义。

## 状态流转与生命周期
- 修复前：`only_reduce` 可能同时出现 `safe_to_trade=true`。
- 修复后：`only_reduce` 仍保留为可运行的恢复态，但统一标记为 `safe_to_trade=false`。
- `resume_eligible` 暂保持现有含义，避免扩大行为面。

## 缓存与性能
- 仅增加常量级条件判断，无新增 I/O、缓存或循环。

## 日志、监控与审计
- 不新增日志字段。
- operator recovery view 与持久化 state snapshot 会反映更准确的“不可继续自动交易”语义。

## 测试策略
- 更新启动恢复单测，覆盖合约 `only_reduce` 时 `safe_to_trade=false`。
- 更新恢复姿态单测，覆盖运行态归一化后 `only_reduce` 仍 `resume_eligible=true` 但 `safe_to_trade=false`。
- 更新最小恢复集成测试，覆盖 recovery view 对外语义。

## 迁移、回滚与兼容性
- 无数据迁移。
- 回滚仅需回退本次代码修改。
- 保持 API 结构兼容，只修正字段语义。

## 配置与环境隔离
- 不新增配置项。
- 修复同时适用于内存模式和持久化模式的合约运行域。

## 代码组织与依赖
- 仅修改恢复相关模块与对应测试。
- 不新增第三方依赖。

## 文档与运维手册
- 本文档记录本次链路复核结论与修复边界，供后续合约恢复专项治理参考。

## 部署与验收标准
- 合约 `only_reduce` 恢复态在 runtime / operator recovery view 中显示为 `safe_to_trade=false`。
- 系统仍不会因为该状态自动进入 halt，且 `only_reduce_required` 继续向下游风控传播。
- 单测、最小恢复集成测试通过；lint 若环境缺依赖，需要明确报告失败原因。
