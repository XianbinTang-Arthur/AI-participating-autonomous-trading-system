# Task 108：independent 严格审查后的修复说明

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 1. 业务目标与边界

本任务只修复 `independent` 在严格 code review 中确认的 4 类问题：

1. 同一 bundle 的双腿风险没有按合并后的暴露重新校验
2. independent 的按腿独立只停留在决策层，执行边界仍是全有或全无
3. recovery / review 摘要丢失 hedge 关键腿信息
4. 重启后 fill 历史与当前仓位不一致时，腿级持有期与冷却锚点可能被意外清零

本任务不新增新模式，不改 public API 的主语义，不做新的策略能力扩展。

## 2. 模块职责与领域模型

- `aats/services/governance_engine/risk.py`
  - 负责 independent 双腿的 bundle 级合并风险校验
- `aats/bootstrap/config.py`
  - 负责把 independent 的“按腿独立执行”真正落到主执行总线
- `aats/services/execution_engine/bundle_recovery.py`
  - 负责恢复摘要保留腿级 hedge 识别信息
- `aats/services/decision_engine/context_builder.py`
  - 负责在 fill 历史不完整时保守重建腿级生命周期锚点

## 3. 输入 / 输出接口

输入：

- `PositionTarget.strategy_execution_legs`
- `LegOrderIntent`
- `StrategyExecutionBundle`
- `OrderState`
- `FillEvent`
- `PortfolioSnapshot`

输出：

- independent bundle 的实际可执行腿集合
- bundle 级 aggregate 风控结果
- recovery bundle 腿摘要中的 `pos_side / leg_action / strategy_execution_mode`
- 保守但不丢失的腿级 lifecycle anchor

## 4. 事务、一致性、并发

- independent 在同一轮决策里允许“部分腿执行”，但只允许：
  - 单腿自身通过 policy / risk
  - 且通过 bundle 级合并风险校验
- 若 bundle 合并风险失败，则整组腿都不执行，避免出现“各腿单看合法，但合并后超限”
- final decision outcome 必须反映“实际继续执行后的净目标”，不能继续沿用原始 target

## 5. 错误处理与幂等

- bundle 级风险拒绝需要落成明确 rejection reason，不能静默降级
- independent 的非执行腿不能继续生成 order intent
- recovery 摘要需要保留足够字段，保证人工 review 能区分“开多 / 平空 / 开空 / 平多”

## 6. 生命周期与状态顺序

修复后应保证：

1. 决策层按腿产出 intent
2. 先做单腿 policy / risk
3. 再做 independent bundle 的合并风险校验
4. 只对最终可执行腿发 plan / intent
5. finalized outcome 反映实际继续执行的 target
6. 恢复摘要与运行时 bundle 对同一组腿保持一致解释

## 7. 测试策略

需要补的回归：

- independent 一条腿被风险拦住时，另一条腿仍可继续执行
- 两条腿单看合法，但合并后 gross 风险超限时，bundle 必须被拦
- recovery view / bundle summary 必须保留 `pos_side / leg_action / strategy_execution_mode`
- fill 历史与当前仓位不一致时，腿级 opened_at / latest_fill_timestamp 不能被错误清空

## 8. 回滚与兼容

- 所有新增字段保持 optional，旧 payload 仍可读取
- 若修复引发异常，可先回滚 `config.py` 的 partial independent execution 分支
- 其余 schema 字段新增不影响旧记录读取

## 9. 验收标准

- independent 的部分执行语义在主链集成测试中可见
- bundle 级 gross 风险超限有明确失败测试
- recovery bundle summary 能携带 hedge 腿识别字段
- 重启 / fill mismatch 下，腿级 min-hold / cooldown 锚点不再直接丢失
