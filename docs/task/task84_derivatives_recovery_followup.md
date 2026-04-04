# 合约恢复语义补丁（二次修复）SOW

## 业务目标与边界
- 目标：修复合约运行模式下两处恢复语义问题。
- 问题 1：交易所存在本地执行链无法解释的合约仓位时，系统仍把它降级成 non-blocking `only_reduce`。
- 问题 2：`only_reduce` 恢复态仍给出 `resume_eligible=true`，导致 UI、恢复状态和 `resume` API 语义不一致。
- 边界：不改动合约下单适配器、保证金公式、风控限额计算，不修改公开 API 结构。

## 当前行为摘要
- 对账层已经能识别 `derivatives_exchange_position_without_local_execution_chain`。
- 风控层也会消费 `only_reduce_required`，阻止新增暴露。
- 但恢复层此前仍有两个问题：
  - 未知仓位来源没有提升到 `review_required` / `resume_blocked` 语义。
  - 纯 `only_reduce` 恢复态虽然 `safe_to_trade=false`，但仍可能被视为可恢复运行。

## 模块职责与领域模型
- `StateComparator`：识别账实差异并产出 findings / severity。
- `RecoveryReconciliationClassifier`：把对账结果映射为恢复策略分类。
- `ExecutionRecoveryService` 与 `ExecutionLedgerRecoveryService`：生成启动恢复状态。
- `RecoveryPostureEvaluator`：按最新证据归一化运行态恢复状态。
- 关键领域字段：`review_required`、`only_reduce_required`、`resume_blocking`、`resume_eligible`、`safe_to_trade`。

## 输入 / 输出接口
- 输入：`ReconciliationReport`、恢复基线状态、kill switch、bundle recovery、health 状态。
- 输出：启动恢复状态、runtime recovery view、resume 可用性判断。
- 本次不新增字段，仅修正既有字段取值与优先级。

## 数据库 Schema / 表 / 索引 / 约束
- 不变更 schema。
- 现有 `reconciliation_reports` / `reconciliation_state_snapshots` 会持久化更准确的 `review_required`、`resume_eligible` 和 `safe_to_trade`。

## 事务、一致性与并发
- 不新增事务边界。
- 修复只改变恢复状态判定，不改变订单、成交、仓位持久化流程。

## 鉴权、认证与数据安全
- 不涉及认证、授权和敏感数据处理变更。

## 错误处理与幂等
- `resume` 在 `only_reduce` 恢复态下应稳定返回 blocked，而不是表面可恢复后再重新落回受限态。
- 对同一份 reconciliation 重复归一化时，应输出同样的恢复结论。

## 状态流转与生命周期
- 未知合约仓位来源：
  - 修复前：`only_reduce`
  - 修复后：`review_required`，同时保留 `only_reduce_required`
- 纯 `only_reduce` 恢复态：
  - 修复前：`resume_eligible=true`
  - 修复后：`resume_eligible=false`

## 缓存与性能
- 仅增加常量级分支判断和理由去重，不引入新 I/O。

## 日志、监控与审计
- 不新增日志字段。
- operator recovery view 与状态快照会更准确反映“待人工确认”和“不可恢复自动运行”。

## 测试策略
- 更新对账单测，覆盖未知仓位来源升级为 `REVIEW_REQUIRED`。
- 更新 classifier / recovery posture / execution recovery 单测。
- 更新恢复集成测试，覆盖：
  - 未知仓位来源 -> `review_required`
  - 纯 `only_reduce` -> `resume` 被阻断

## 迁移、回滚与兼容性
- 无数据迁移。
- 回滚仅需回退本次代码修改。
- 保持接口结构兼容，变化仅体现在恢复状态语义更严格。

## 配置与环境隔离
- 不新增配置项。
- 适用于当前合约 `paper_live` / `guarded_live` 相关恢复链路。

## 代码组织与依赖
- 仅修改对账、恢复判定及相关测试。
- 不新增第三方依赖。

## 文档与运维手册
- 本文档记录第二轮合约恢复补丁的作用范围和验收标准。

## 部署与验收标准
- `derivatives_exchange_position_without_local_execution_chain` 必须进入 `review_required`，不能再被当作 non-blocking only-reduce。
- `only_reduce` 恢复态必须显示为不可 `resume`，且 `resume` API 返回 blocked。
- 单测与最小恢复集成测试通过；lint 若环境缺依赖，需要明确报告失败原因。
