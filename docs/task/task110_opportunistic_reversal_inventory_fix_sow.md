# Task 110：Opportunistic 反转库存归因修复 SOW

## 业务目标与边界
- 目标：修复 `opportunistic` 在方向性主腿翻向时错误接管旧方向库存的问题，避免把旧主腿残留仓位误判为机会腿。
- 边界：仅修复 `aats/services/decision_engine/target_position.py` 中的 opportunistic 决策归因，不改 public API、不改执行器协议、不改 operator 展示结构。

## 模块职责与领域模型
- `TargetPositionEngine` 负责把方向性净仓目标拆成 `hedge mode` 双腿目标。
- `HedgeOverlayDecision` 负责描述 overlay 当前是否激活、为何激活、目标腿数量和阻断原因。
- 本次修复要求：只有当主腿当前库存真实存在时，`opportunistic` 才能把对侧库存解释为机会腿。

## 输入输出接口
- 输入：`DecisionContext.current_long_position_qty`、`DecisionContext.current_short_position_qty`、`directional_target_qty`、`strategy_hedge_opportunistic_*` 配置。
- 输出：`PositionTarget.hedge_overlay_decision` 与 `PositionTarget.strategy_execution_legs`。
- 兼容性：保持 schema 与字段名不变，只修正字段语义。

## 数据库 Schema / 表 / 索引 / 约束
- 无数据库 schema 变更。

## 事务、一致性、并发
- 本次仅影响纯决策逻辑，无额外事务。
- 一致性目标：主腿翻向时，旧方向残留仓位必须继续按方向性主线收口，不能被 overlay 语义抢占。

## 授权、认证、数据安全
- 不涉及认证授权变更。
- 不新增凭据、不读取新的敏感配置。

## 错误处理与幂等
- 修复后在“主腿目标已翻向、但主腿当前库存尚未建立”的场景下，overlay 应稳定返回 inactive，不因为缺失开仓时间戳而误触发 min-hold。
- 同一输入重复计算应得到相同结果。

## 状态迁移与生命周期
- 旧行为：opportunistic 会把对侧残留库存直接视为机会腿，可能进入 `holding` / `closing` / `blocked`。
- 新行为：若主腿当前库存不存在，则 opportunistic 保持 `inactive`，等待主腿库存建立后再评估机会腿。

## 缓存与性能
- 无缓存变更。
- 仅增加一次轻量条件判断，无可见性能影响。

## 日志、监控、审计
- 不新增日志字段。
- 间接受益：执行腿归因更准确，后续审计与健康归因不再把主线反转关闭误记为机会腿。

## 测试策略
- 新增单测覆盖：
  - 主腿翻向且旧方向库存仍存在时，机会型 overlay 不得抢占旧仓位语义。
  - 缺少对侧 leg opened timestamp 时，也不得因为误判机会腿而阻断反转。
- 回归运行 opportunistic 相关 unit / integration 测试。

## 迁移、回滚、兼容性
- 无数据迁移。
- 如需回滚，只需回退本次逻辑补丁和新增测试。

## 配置与环境隔离
- 不新增配置项。
- 验证继续使用仓库既有 `.venv\Scripts\python.exe`。

## 代码组织与依赖
- 修改文件限定在决策引擎与测试，不新增第三方依赖。

## 文档与运维手册
- 本 SOW 即本次 review/fix 的交付边界说明。

## 部署与验收标准
- 验收标准：
  - opportunistic 反转场景下不再错误产生 `role="hedge"` 的旧方向关闭腿；
  - 缺少 opened_at 时不会被误判的 `min_hold` 卡住；
  - opportunistic 现有开腿、费耗、最小持有、rollout 用例继续通过。
