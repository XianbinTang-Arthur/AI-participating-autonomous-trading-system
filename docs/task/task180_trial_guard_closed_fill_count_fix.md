# Task180 试盘守护 closed fill 计数口径修复 SOW

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 业务目标与边界

- 目标：让 `trial_guard_min_closed_fills` 按“发生过平仓/减仓实现”的成交样本生效，不再把纯开仓成交计入最小样本量。
- 边界：只修正 operator 查询层和试盘守护样本口径，不改变风控阈值、不改变撮合/持仓会计逻辑、不改公开 API 字段名。

## 模块职责与领域模型

- `aats/services/operator/query_service.py`
  - 负责定义 fill outcome 是否属于 closed fill 的判定规则。
  - 负责为报表和试盘守护提供统一口径的 closed fill 样本。
- `aats/services/operator/report_queries.py`
  - 负责生成 profitability overview，确保 `closed_fill_count`、`recent_closed_fills`、费率和异常比例基于同一批 closed fill。
- `aats/services/governance_engine/trial_guard.py`
  - 继续只负责 hard stop 判定。
  - 消费统一口径的 closed fill 样本，并避免异常计数与 closed fill 计数口径不一致。

## 输入输出接口

- 输入：
  - `FillOutcomeRecord.starting_position_qty`
  - `FillOutcomeRecord.ending_position_qty`
  - `FillOutcomeRecord.realized_pnl_delta`
  - `FillOutcomeRecord.execution_action`
  - `FillOutcomeRecord.position_intent`
- 输出：
  - `profitability_overview.summary.closed_fill_count`
  - `profitability_overview.recent_closed_fills`
  - `forward_validation.periods[*].closed_fill_count`
  - `trial_guard.fill_count`

## 数据库 / 表 / 索引 / 约束

- 本次不新增、不修改数据库表结构。
- 复用既有 `fill_outcome_repo` 与 funding fee 数据。

## 事务、一致性、并发

- 全部为只读查询与内存判定逻辑调整。
- 不引入新的事务边界或并发写入路径。

## 鉴权、认证、数据安全

- 不修改认证与授权逻辑。
- 不扩大任何接口返回的敏感数据范围。

## 错误处理与幂等

- 对缺失 `starting_position_qty/ending_position_qty` 的旧数据保留兼容兜底：
  - 优先用 `realized_pnl_delta`
  - 再回退到 `execution_action/position_intent` 语义判断

## 状态迁移与生命周期

- 纯开仓 / 同向加仓：不计入 closed fill。
- 减仓 / 平仓 / 反手：计入 closed fill。
- `trial_guard` 仅在 closed fill 数达到阈值后进入正式监控。

## 缓存与性能

- 在 `OperatorQueryService` 现有 cache 中增加 scoped closed fill outcomes 缓存。
- 仅做一次过滤复用，避免多处重复扫描 fill outcomes。

## 日志、监控、审计

- 不新增日志事件。
- 保持现有 `trial_guard` halt 审计记录不变。

## 测试策略

- 单元测试：
  - 验证 `trial_guard` 在 anomaly summary 与 closed fill summary 口径不一致时优先使用 closed fill 口径。
- 集成测试：
  - 验证 profitability overview / forward validation / trial_guard 都忽略纯开仓 fill outcome 的 closed fill 计数。

## 迁移、回滚、兼容性

- 无 schema migration。
- 回滚方式：回退本次代码修改即可。
- API 字段保持兼容，仅语义修正为更符合配置命名。

## 配置与环境隔离

- 不新增配置项。
- 继续复用现有 `trial_guard_*` 配置。

## 代码组织与依赖

- 仅修改：
  - `aats/services/operator/query_service.py`
  - `aats/services/operator/report_queries.py`
  - `aats/services/governance_engine/trial_guard.py`
  - 相关测试

## 文档与运维手册

- 本文件记录本次修复的目标、边界和验收口径。

## 部署与验收标准

- `trial_guard_min_closed_fills=5` 时，纯开仓成交不会推进 closed fill 样本计数。
- profitability / forward validation / trial_guard 的 closed fill 计数一致。
- lint、单测、相关集成测试通过。
