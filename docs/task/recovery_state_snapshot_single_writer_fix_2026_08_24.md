# 恢复状态快照单写者修复工作说明

> 文档状态：历史任务与交付记录
> 最后核对：2026-08-24（代码基线 `2465520d355c64540811d9191c2d85c1e95df385`）
> 核对范围：当前静态代码、受控 derivatives 模拟盘运行时、最小回归测试
> 当前替代：运行状态始终以受控 API、交易所只读查询和标准健康检查为准

## 业务目标与边界

在四进程拓扑中，确保 reconciliation recovery state snapshot 只由 execution 进程持久化，避免 gateway 在 kill switch 跨进程传播窗口内把已恢复的 `normal_operation` 覆盖为 `manually_halted`。不修改交易策略、下单逻辑、风险阈值、账户配置或公开 API。

## 当前行为与根因证据

derivatives 模拟盘完成清仓、operator rebaseline 和自动 resume 后，execution 已写入 `normal_operation / safe_to_trade=true` 快照；随后 gateway 因本地 kill switch 副本尚未收敛，又写入时间更晚的 `manually_halted / safe_to_trade=false` 快照。`/system/health` 同时显示 `halted=false`、无 blocker、提交链路可用，证明问题是跨进程恢复快照的单写者边界缺失，而不是交易所或执行层仍被暂停。

## 模块职责与领域模型

- `RecoveryPostureEvaluator`：计算恢复姿态；只有 execution 或 monolith 可以把 `ReconciliationStateSnapshot` 写入 repository。
- gateway、market、decision：允许计算本地展示状态，但不得持久化 execution-owned recovery truth。
- `ReconciliationStateSnapshot`：保持现有字段、ID 和数据库模型不变。

## 输入输出接口

输入仍为 `RecoveryStatus` 与 `ReconciliationReport`；输出仍为 repository 中的新状态快照。HTTP API、NATS operator command schema 和数据库表结构均不变化。

## 数据库、事务、一致性与并发

不新增表、列、索引或约束。现有 repository 的幂等写入与事务边界保持不变。修复通过进程角色门控消除 gateway 与 execution 对同一 scoped recovery truth 的并发写竞争。

## 鉴权、认证与数据安全

不改变 operator session、角色要求或 API key 兼容行为；文档和测试不记录密钥、密码、token 或账户标识。

## 错误处理、幂等与生命周期

非权威进程的持久化调用直接安全返回；execution/monolith 保持原有“状态变化时写快照”的幂等逻辑。状态生命周期仍为 halt/rebaseline/resume，修复后 resume 成功的 `normal_operation` 不会被非权威副本回退。

## 缓存、性能、日志、监控与审计

不新增缓存和网络调用；减少非权威数据库写入。operator halt、rebaseline、resume 审计事件与 blocker snapshot 不变。验收时同时核对 `/system/health`、`/system/recovery`、最新 reconciliation 和数据库快照。

## 测试策略

- 单元测试：gateway 不得写 recovery state snapshot；execution、monolith 和兼容的 `None` 角色仍可写。
- 回归测试：运行 recovery posture 相关测试、全量 unit tests 和最窄 operator API 集成测试。
- 部署后：验证 derivatives 模拟盘仓位 0、挂单 0、reconciliation `CLEAN`、最新恢复快照 `normal_operation`、所有必需容器 healthy。

## 迁移、回滚、兼容性与环境隔离

无数据库迁移。回滚可恢复单个代码提交；旧快照作为历史审计保留，新一轮受控 resume/rebaseline 会写入正确快照。修复仅按 runtime `process_role` 生效，保持单进程兼容。

## 代码组织、依赖、文档与运维

仅修改 recovery posture evaluator、对应单元测试和本任务记录；不新增依赖。部署继续使用唯一入口 `scripts/deploy.sh --profile derivatives --skip-commit`，不得手工调用 Compose 或使用 rsync。

## 部署与验收标准

1. Ruff 与单元测试通过；最窄 WSL2 集成测试通过。
2. 提交后的 Windows HEAD 与 WSL2 HEAD 一致。
3. gateway、market、decision、execution、rdp-daemon 全部 healthy。
4. `/system/health` 为 healthy、无 blocker；`/system/recovery` 与最新 state snapshot 显示 `normal_operation / safe_to_trade=true`。
5. OKX 模拟盘保持零仓位、零活动委托，实盘账户不在操作范围内。
