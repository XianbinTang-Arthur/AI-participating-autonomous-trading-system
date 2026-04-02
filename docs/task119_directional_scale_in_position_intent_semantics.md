# task119 directional scale-in position_intent 语义修复

## 业务目标与边界
- 修复 `directional` 同向加仓仍被标记为 `open_long/open_short` 的语义问题。
- 让 `PositionTarget.position_intent` 能区分“首次开仓”和“同向加仓”。
- 让 `DecisionOutcome.final_action`、operator/query、审计和依赖 `position_intent` 推导方向的 helper 与新语义保持一致。
- 不改变实际撮合 side、reduce-only、close-only 行为，不重写执行 planner 的抽象动作模型。

## 当前行为摘要
- `aats/services/decision_engine/target_position.py` 的 `_position_intent()` 对同向扩仓返回 `open_long/open_short`。
- `_decision_outcome()` 随后把这两类统一映射成 `final_action="enter"`。
- 结果会把“首次开仓”和“同向加仓”混为一类，污染决策审计、dashboard 和事后归因。

## 模块职责
- `TargetPositionEngine`：输出更精确的 `position_intent`
- `DecisionOutcome`：输出与 `position_intent` 一致的 `final_action`
- `execution schema helpers`：保持 `position_intent -> action / pos_side / order side` 的兼容推导
- `operator/query`：展示正确的行为语义

## 输入 / 输出接口
- 保持现有接口结构不变。
- `position_intent` 新增两个合法值：
  - `scale_in_long`
  - `scale_in_short`

## 一致性 / 幂等 / 生命周期
- 不改变 target qty 计算，只修复语义标签。
- 不改变 order side 和 reduce-only 语义。
- 审计和执行记录在相同输入下可稳定区分 entry 与 scale-in。

## 测试策略
- 单测覆盖：
  - `TargetPositionEngine` 同向加仓返回 `scale_in_long/scale_in_short`
  - `DecisionOutcome.final_action == "scale_in"`
  - `ExecutionPlanner` 抽象动作仍为 `scale_in`
  - 方向推导 helper 对 `scale_in_short` 仍给出 `sell`
- 最窄集成测试覆盖：
  - dashboard / operator 读取 `scale_in_long` 时，动作语义仍显示为 `scale_in`

## 兼容性
- 旧数据里的 `open_long/open_short` 仍然可读。
- 新数据会开始写入 `scale_in_long/scale_in_short`。
