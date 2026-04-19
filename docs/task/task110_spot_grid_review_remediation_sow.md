# Task110 现货网格主线 Spot Grid 审查与修复 SOW

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 1. 业务目标与边界
- 目标：针对 `spot_grid` 主线做定向 code review，修复已确认且可复现的系统级缺陷，避免现货网格在冷启动或配置口径失配时给出错误库存目标。
- 本次覆盖：
  - `aats/services/strategy_engines/spot_grid.py`
  - 与其直接相关的 unit / integration tests
  - 运行时摘要链路对 `spot_grid` 指标的兼容性验证
- 非目标：
  - 不重构 allocator、coordinator、execution engine
  - 不修改数据库 schema、表结构或索引
  - 不改变公开 API 结构

## 2. 当前行为摘要
- `spot_grid` 仅在 `spot` runtime 下启用。
- 引擎读取最近市场快照，按均值计算 anchor，并根据带宽把当前价格映射为目标库存分数。
- 目标库存以 sleeve 维度计算，再增量叠加到账户级目标仓位。
- 当 sleeve delta 小于最小再平衡阈值时，候选状态退回 `hold_current`。

## 3. 已确认问题
- 缺陷 A：`spot_grid_anchor_lookback_snapshots` 只用于截取最多多少条快照，但评估时没有要求“至少拥有这么多条快照”。结果是冷启动阶段只有 1 条快照时也会出交易目标。
- 缺陷 B：`spot_grid_inventory_ceiling_fraction` 在配置注释中允许到 `1.5`，但核心实现把该值硬截断到 `1.0`，导致高于 100% 的库存上限配置被静默吞掉。
- 待一并核对：带宽非法值是否应 fail-closed，而不是被静默修正为最小正数。

## 4. 模块职责与领域模型
- `SpotGridStrategyEngine`
  - 根据行情、baseline 和当前库存，生成 `StrategyCandidate`
  - 输出账户级目标仓位、sleeve 级目标仓位、成本估计及原因码
- `StrategyCoordinatorService`
  - 汇总 `spot_grid` 候选并转换为 `StrategySleeveIntent`
- `OperatorQueryService`
  - 将最新 `spot_grid` 快照和参数暴露到运行时查询接口

## 5. 输入 / 输出接口
- 输入：
  - `AATSSettings.spot_grid_*`
  - `StrategyEngineInput.latest_market_snapshot`
  - `StrategyEngineInput.recent_market_snapshots`
  - `DecisionContext.current_position_qty`
  - sleeve inventory truth
- 输出：
  - `StrategyCandidate.state / selectable / route_action`
  - `target_position_qty`
  - `delta_position_qty`
  - `metrics.anchor_price / target_sleeve_position_qty / target_account_position_qty`
  - `reason_codes`

## 6. 数据库 / 表 / 索引 / 约束
- 本次不改数据库 schema。
- 仅依赖现有事件与运行时持久化链路，不新增表、索引、约束。

## 7. 事务、一致性与并发
- 修复点位于纯计算逻辑，不引入新的事务边界。
- 要保证同一次 `spot_grid` 评估在候选结果、运行时摘要和测试断言中的口径一致。

## 8. 认证、授权与数据安全
- 不涉及鉴权模型或凭证处理。
- 不新增外部 I/O，不引入新的敏感数据暴露面。

## 9. 错误处理与幂等
- 快照不足时，应明确返回 `spot_grid_anchor_history_insufficient`，而不是静默给出可执行目标。
- 非法配置值应优先 fail-closed，避免把错误配置转换成可交易状态。
- 保持评估函数幂等；相同输入必须返回相同候选结果。

## 10. 状态流转与生命周期
- 修复前：
  - 冷启动时快照数不足仍可能返回 `ready`
  - `inventory_ceiling_fraction > 1.0` 会被静默截断
- 修复后：
  - 未达到最小锚点快照要求时返回 `inactive`
  - `inventory_ceiling_fraction` 按文档口径生效
  - 非法带宽配置不再被隐式修正为可交易状态

## 11. 缓存与性能
- 不新增缓存。
- 仅增加常量级校验逻辑，对性能影响可忽略。

## 12. 日志、监控与审计
- 不新增日志字段。
- 通过 `reason_codes` 和 `metrics` 提升 operator/runtime 的可解释性。

## 13. 测试策略
- 单元测试：
  - 快照数小于 `spot_grid_anchor_lookback_snapshots` 时，候选必须 `inactive`
  - `inventory_ceiling_fraction=1.5` 时，目标库存上限必须允许超过 `max_abs_position_qty`
  - 如实现调整带宽校验，则补充非法带宽 fail-closed 测试
- 最小集成测试：
  - `tests/integration/test_strategy_runtime_integration.py`
  - 确认 runtime 接口仍能暴露 `spot_grid` 候选与参数摘要

## 14. 迁移、回滚与兼容性
- 不需要 migration。
- 回滚仅需回退 Python 代码与测试。
- 保持 API 字段结构不变，只修正内部数值和状态语义。

## 15. 配置与环境隔离
- 继续使用现有 `configs/strategy_profiles/*.yaml` 与 `AATSSettings`。
- 不改 `.env`，不改 profile 选择逻辑。

## 16. 代码组织与依赖
- 变更范围限制在 `spot_grid` 引擎与相关测试。
- 不新增第三方依赖。

## 17. 文档与运维手册
- 本文档记录审查边界、修复目标和验收标准。
- 最终交付中同步说明 bug 细节、修复行为和剩余风险。

## 18. 部署与验收标准
- `spot_grid` 在锚点历史不足时不得返回可执行库存目标。
- `spot_grid_inventory_ceiling_fraction` 的高于 `1.0` 配置不得被静默吞掉。
- 相关 lint、unit、最小 integration 测试完成并报告结果。
