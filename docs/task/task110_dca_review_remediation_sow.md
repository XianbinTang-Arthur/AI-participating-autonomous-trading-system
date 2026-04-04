# Task110 定投主线 DCA 审查与修复 SOW

## 1. 业务目标与边界
- 目标：针对 `dca` 主线做定向 code review，修复已确认且可复现的系统级缺陷，避免定投在 `pullback-only` 模式下因缺少锚点历史而误触发买入。
- 本次覆盖：
  - `aats/services/strategy_engines/dca.py`
  - 与其直接相关的 unit / integration tests
  - 运行时摘要链路对新增原因码的兼容性验证
- 非目标：
  - 不重构 allocator、coordinator、execution engine
  - 不修改数据库 schema、表结构或索引
  - 不改变公开 API 结构

## 2. 当前行为摘要
- `dca` 仅在 `spot` runtime 下启用。
- 引擎先检查价格可用性、仓位上限、定投时间间隔，再按预算换算本轮 tranche 数量。
- 若 `dca_pullback_only_enabled=true`，当前实现会尝试用最近市场快照均值作为 anchor，再判断是否达到回撤阈值。
- 目标仓位以 sleeve 维度计算，再增量叠加到账户级目标仓位。

## 3. 已确认问题
- 缺陷 A：当启用 `pullback-only` 但系统尚无任何可用锚点历史时，当前实现直接跳过回撤判断并返回 `ready`，与“只在回撤时买入”的业务语义相反。
- 缺陷 B：`dca` 读取 sleeve 库存和最近一次定投时间窗时，scope 使用了 `settings.margin_mode`，而 coordinator / target 持久化使用的是 `base_target.margin_mode`。一旦运行配置的全局 `margin_mode` 与该次 spot target 的 `margin_mode` 不一致，就会查错 sleeve。
- 待一并核对：锚点历史里若混入无效价格，是否也应按“历史不足”处理，而不是参与均值计算。

## 4. 模块职责与领域模型
- `DcaStrategyEngine`
  - 根据行情、时间间隔、回撤条件和 sleeve 库存，生成 `StrategyCandidate`
  - 输出账户级目标仓位、sleeve 级目标仓位、成本估计及原因码
- `StrategyCoordinatorService`
  - 汇总 `dca` 候选并转换为 `StrategySleeveIntent`
- `OperatorQueryService`
  - 将最新 `dca` 快照和参数暴露到运行时查询接口

## 5. 输入 / 输出接口
- 输入：
  - `AATSSettings.dca_*`
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
- 要保证同一次 `dca` 评估在候选结果、运行时摘要和测试断言中的口径一致。

## 8. 认证、授权与数据安全
- 不涉及鉴权模型或凭证处理。
- 不新增外部 I/O，不引入新的敏感数据暴露面。

## 9. 错误处理与幂等
- 当 `pullback-only` 缺少可用锚点历史时，应明确返回 `dca_pullback_anchor_history_insufficient`，而不是静默给出可执行目标。
- 锚点历史中的无效价格应被忽略；若全部无效，则同样 fail-closed。
- sleeve inventory 与 interval history 的查询 scope 必须与最终持久化的 `strategy_sleeve_id` 构造口径一致。
- 保持评估函数幂等；相同输入必须返回相同候选结果。

## 10. 状态流转与生命周期
- 修复前：
  - `pullback-only` 冷启动时可能直接返回 `ready`
  - `margin_mode` 口径不一致时，可能把已有 `dca` sleeve 误判为零库存，或漏掉最近一次定投记录
- 修复后：
  - 缺少可用锚点历史时返回 `inactive`
  - 有效锚点存在时才进入回撤判定
  - sleeve inventory / interval history 统一按 `directional_target.margin_mode` 查找
  - 其余间隔、预算和仓位上限语义保持不变

## 11. 缓存与性能
- 不新增缓存。
- 仅增加常量级价格过滤与历史校验，对性能影响可忽略。

## 12. 日志、监控与审计
- 不新增日志字段。
- 通过 `reason_codes` 和 `metrics` 提升 operator/runtime 的可解释性。

## 13. 测试策略
- 单元测试：
  - `pullback-only` 且没有锚点历史时，候选必须 `inactive`
  - 候选需暴露新的原因码与历史可用数指标
  - `settings.margin_mode != directional_target.margin_mode` 时，`dca` 仍需命中正确 sleeve 库存与最近一次定投记录
- 最小集成测试：
  - `tests/integration/test_strategy_runtime_integration.py`
  - 确认 runtime 接口在冷启动场景下不再把 `dca` 暴露为可执行目标

## 14. 迁移、回滚与兼容性
- 不需要 migration。
- 回滚仅需回退 Python 代码与测试。
- 保持 API 字段结构不变，只修正内部状态语义和原因码。

## 15. 配置与环境隔离
- 继续使用现有 `configs/strategy_profiles/*.yaml` 与 `AATSSettings`。
- 不改 `.env`，不改 profile 选择逻辑。

## 16. 代码组织与依赖
- 变更范围限制在 `dca` 引擎、原因码文案与相关测试。
- 不新增第三方依赖。

## 17. 文档与运维手册
- 本文档记录审查边界、修复目标和验收标准。
- 最终交付中同步说明 bug 细节、修复行为和剩余风险。

## 18. 部署与验收标准
- `dca_pullback_only_enabled=true` 且缺少可用锚点历史时，不得返回可执行定投目标。
- runtime/operator 需要能看到新的原因码。
- 相关 lint、unit、最小 integration 测试完成并报告结果。
