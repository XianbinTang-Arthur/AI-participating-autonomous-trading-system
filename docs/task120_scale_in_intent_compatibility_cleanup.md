# task120 scale_in 意图兼容点清理

## 业务目标与边界
- 收干仓库内剩余的 `scale_in_long / scale_in_short` 兼容点。
- 保证同向加仓语义从 `directional` / hedge 腿级计划一直传到 `ORDER_INTENTS`、storage 和前端展示。
- 不改撮合方向、风险逻辑和真实下单参数，只修语义传递与消费兼容。

## 当前行为摘要
- `directional` 主路径已经能生成 `scale_in_*`。
- 但仍有三类残留：
  - `LegOrderIntent -> OrderIntent` 会把同向加仓重新降成 `open_*`
  - converged execution repo 里 `scale_in_short` 仍被当成 `buy`
  - 前端术语表缺少 `scale_in_*` 中文映射

## 模块职责
- `aats/bootstrap/config.py`
  - 识别策略腿当前/目标仓位，给腿级 planner 传递正确的 `position_intent`
- `aats/services/execution_engine/planner.py`
  - 保留腿级 `position_intent`，不再在 build leg intent 时丢失
- `aats/schemas/execution.py`
  - 允许 `LegOrderIntent` 携带 `scale_in_*`
  - `order_intent_from_leg_order_intent()` 优先保留显式语义
- `aats/storage/execution_repo_converged_postgres.py`
  - 正确把 `scale_in_short` 识别为 `sell`
- `aats/api/static/modules/terms.js`
  - 正确展示 `加多 / 加空`

## 输入 / 输出接口
- 不新增公开 API。
- `LegOrderIntent` 仅新增可选 `position_intent` 字段，保持向后兼容。

## 一致性 / 幂等 / 生命周期
- 同一份腿级计划在总线、命令流、落库和 UI 中保持同一 `position_intent`。
- 不改变既有 idempotency key、bundle id 和 order id 生成方式。

## 测试策略
- 单测覆盖：
  - 腿级 planner / intent 转换保留 `scale_in_*`
  - converged repo side 推导正确识别 `scale_in_short`
- 集成测试覆盖：
  - independent 主链同向扩仓时，落到 `ORDER_INTENTS` 的仍是 `scale_in_long`
  - 前端术语表能把 `scale_in_long / scale_in_short` 本地化成中文

## 兼容性
- 旧数据里的 `open_* / reduce_* / close_*` 不变。
- 没有显式 `position_intent` 的旧 `LegOrderIntent` 仍按原规则推导。
