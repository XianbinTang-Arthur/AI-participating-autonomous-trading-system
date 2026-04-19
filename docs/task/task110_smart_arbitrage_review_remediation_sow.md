# Task110 智能套利主线代码审查与预算口径修复 SOW

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 业务目标与边界
- 目标：修复 `smart_arbitrage` 在 allocator 阶段的预算/名义额口径错误，避免真实可开套利尺寸被错误缩小。
- 边界：仅修改 `smart_arbitrage -> coordinator -> allocator` 相关预算计算与测试，不改 public API 字段名，不改执行链路协议，不做无关重构。

## 模块职责与领域模型
- `smart_arbitrage/engine.py`：生成单 pair 或多 pair 候选、状态、腿计划与成本说明。
- `coordinator.py`：把候选转成 `StrategySleeveIntent`，并交给 allocator。
- `allocator.py`：按 sleeve 预算、组合预算和冲突规则产出最终执行腿。
- 核心领域对象：`StrategyCandidate`、`StrategySleeveIntent`、`AllocatorBudgetSnapshot`、`StrategyLegIntent`。

## 输入输出接口
- 输入：
  - `StrategySleeveIntent.target_notional`
  - `StrategySleeveIntent.legs[*].delta_position_qty/reference_price/pair_id/role`
  - `smart_arbitrage_quote_budget_per_trade`
  - `smart_arbitrage_max_pair_notional`
  - 聚合候选中的 `pair_count_selected`
- 输出：
  - `AllocatorBudgetSnapshot.requested_notional/approved_notional`
  - 缩放后的 `StrategySleeveIntent`
  - 最终 `StrategyLegIntent` 列表

## 数据库 Schema / 表 / 索引 / 约束
- 本次不新增表、不改索引、不改迁移。
- 变更仅影响运行时内存对象与持久化 payload 的数值口径。

## 事务、一致性与并发
- 变更发生在单次决策计算内，无额外事务边界。
- 要求同一轮 `candidate -> sleeve intent -> allocator budget snapshot -> execution legs` 口径一致。

## 鉴权、认证与数据安全
- 无新增鉴权面。
- 无新增敏感数据采集与存储。

## 错误处理与幂等
- 若 `smart_arbitrage` 腿信息缺失，应回退到现有通用 notional 计算，不引入硬失败。
- 相同输入重复运行必须得到相同预算结果。

## 状态流转与生命周期
- 不改变 `opening / active / recovery / unwinding / blocked` 语义。
- 仅修正这些状态进入 allocator 后的预算缩放结果。

## 缓存与性能
- 仅增加小规模腿分组与聚合，复杂度保持线性。
- 不新增外部 I/O。

## 日志、监控、审计
- 不新增日志字段。
- 通过现有 `AllocatorBudgetSnapshot` 与 runtime payload 观察修复结果。

## 测试策略
- 新增/更新单元测试覆盖：
  - 单 pair 智能套利请求名义额不再把双腿名义额直接相加。
  - 多 pair 聚合时预算上限按 pair 数量扩展，而不是错误复用单 pair 上限。
- 运行既有 `smart_arbitrage` 组件单测、`strategy_coordinator` 单测，以及窄集成回归。

## 迁移、回滚、兼容性
- 无数据库迁移。
- 兼容既有配置；回滚仅需回退本次代码改动。

## 配置与环境隔离
- 继续使用现有 `.venv\Scripts\python.exe`。
- 不读取或修改 `.env.derivatives.live` 中数据库配置。

## 代码组织与依赖
- 仅修改：
  - `aats/services/strategy_engines/allocator.py`
  - 相关测试文件
- 不新增第三方依赖。

## 文档与运维手册
- 本文件作为本次审查与修复的范围说明。
- 最终交付中补充 bug 说明、修复方式、测试结果与剩余风险。

## 部署与验收标准
- 单 pair `smart_arbitrage` 不再被 allocator 以双倍名义额错误裁小。
- 多 pair 聚合在 `max_concurrent_pairs > 1` 时，预算上限按 pair 数量生效。
- 相关 lint、单测、窄集成测试通过。
