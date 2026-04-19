# task118 target_position as-of 时钟一致性修复

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 业务目标与边界
- 修复 `aats/services/decision_engine/target_position.py` 中策略时间守卫混用 `utc_now()` 的确定性问题。
- 统一将 `min_hold`、`post-close cooldown`、`low-edge cooldown`、overlay `rebalance cooldown`、independent 腿级冷却的时钟基准收敛到 `DecisionContext.as_of_ts`。
- 不改变策略公式、阈值、路由优先级、风控限额和执行接口。

## 当前行为摘要
- 当前 `target_position.py` 的多个时间守卫直接读取墙钟时间。
- 同一份历史输入在 replay / postmortem / simulation 中，若运行当天的墙钟不同，决策结果会漂移。
- 该问题同时影响 `directional`、`protective`、`opportunistic`、`independent` 四条主线。

## 模块职责与领域模型
- `DecisionContext.as_of_ts`：决策输入的统一时间锚点。
- `TargetPositionEngine`：只能消费 `context.as_of_ts` 做策略判断，不能再自行读取墙钟时间。
- `PositionTarget.decision_expiry_ts`：应以 `context.as_of_ts` 为基准生成，保证回放一致性。

## 输入 / 输出接口
- 输入不变：`TargetPositionEngine.build(context, baseline, ai_assessment, ...)`
- 输出不变：`PositionTarget`
- 行为变化：
  - 所有时间守卫改为基于 `context.as_of_ts`
  - `decision_expiry_ts` 改为 `context.as_of_ts + 15m`

## 数据库 / 一致性 / 并发
- 不涉及 schema 变更。
- 不涉及事务语义变更。
- 修复后 replay 与 live 的同一历史输入可获得一致决策，提升状态一致性。

## 安全 / 幂等 / 生命周期
- 不新增权限、认证或密钥读写。
- 不改变订单生命周期，仅修复“是否允许当前动作”的时间判断基准。
- 幂等性提升：同一历史上下文重复回放结果一致。

## 错误处理
- 不新增错误码。
- 若 `context.as_of_ts` 缺失，仍由现有 schema 校验阻止构造非法上下文。

## 日志 / 监控 / 审计
- 不新增日志字段。
- 通过测试确保 `guardrail_flags`、`hedge_overlay_decision.blocked_reasons` 在固定历史 `as_of_ts` 下稳定。

## 测试策略
- 新增 / 更新最窄单测，覆盖：
  - directional `post_close_cooldown`
  - protective `rebalance cooldown`
  - opportunistic `min_hold`
  - independent `post_close_cooldown`
- 使用固定历史 `as_of_ts`，确保测试不依赖运行当天墙钟。

## 迁移 / 回滚 / 兼容性
- 无数据迁移。
- 回滚方式：恢复 `target_position.py` 的时间基准实现。
- 对外接口保持兼容，仅决策一致性修复。

## 配置与环境隔离
- 不新增配置项。
- 不依赖环境变量变化。

## 文档 / 运维 / 验收
- 验收标准：
  - `target_position.py` 中策略时间守卫不再直接使用 `utc_now()`
  - 固定历史 `as_of_ts` 的单测全部通过
  - 相关 lint、unit、最窄 integration 通过
