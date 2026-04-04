# Task 155 - Independent 预期 vs 已实现诊断交付

## 本轮交付范围

本轮完成 Deliverable 5 的最小可交付闭环，重点是把 `independent` 的 `expected vs realized` 诊断从内部计算结果抬升为 runtime / operator / replay / dashboard 都可见的正式摘要。

## 已完成内容

1. 新增独立双书 expected-vs-realized summary schema
   - `StrategyExpectedVsRealizedSummary`
   - `StrategyExpectedVsRealizedBookDiagnostics`

2. 新增显式配置开关并写入 managed profiles
   - `strategy_hedge_independent_emit_book_level_metrics`
   - `strategy_hedge_independent_emit_expected_vs_realized_metrics`
   - `strategy_hedge_independent_emit_close_reason_metrics`
   - `strategy_hedge_independent_emit_execution_policy_metrics`

3. operator/runtime 已接通
   - `/strategy/runtime` 顶层返回 `independent_expected_vs_realized_summary`
   - `summary.latest_independent_expected_vs_realized_*` 会给出最新样本摘要
   - `latest_applied_target` 会附带当前 decision 级诊断
   - `/decision/latest`
   - `/decision/{id}`
   - `/decision/recent`
   - `ai_decision_audit`
   - replay validation summary

4. dashboard 已接通
   - 策略页显示独立双书 `预期 vs 已实现`
   - 决策抽屉显示 decision 级 `预期 vs 已实现`
   - 独立双书配置卡显示 4 个 diagnostics emit 开关

## 当前摘要字段

当前对外摘要至少包括：

- 样本数
- 开仓 / 加仓 / 收口 / 降风险次数
- 平均预期净边际
- 平均已实现毛收益 / 手续费 / 滑点 / 净收益
- 费用拖累占比
- churn ratio
- passive-first usage ratio
- expected vs realized 净收益偏差
- 相关性
- 退出原因分布
- long / short book breakdown

## 本轮不包含

以下内容仍不在 Deliverable 5 的本轮范围内：

- 更深的 realized diagnostics 生产归因看板
- 仓库外真实消费者迁移
- 新的指标落到外部 metrics backend
- long / short 原生 realized attribution drill-down

## 验收关注点

建议优先核对：

1. `/strategy/runtime` 是否返回 `independent_expected_vs_realized_summary`
2. `/decision/latest|recent|{id}` 是否返回同名顶层字段
3. replay validation summary 是否附带同名字段
4. 策略页 / 决策抽屉文案是否为 clean UTF-8 中文
