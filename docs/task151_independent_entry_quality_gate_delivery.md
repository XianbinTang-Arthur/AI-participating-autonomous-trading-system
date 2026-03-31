# Task 151 - Independent Entry Quality Gate Delivery

## 本次范围

本批次只实现 `independent family optimization README` 中建议的 **Deliverable 1**：

- entry quality gate
- score stability metrics
- liquidity quality score
- settings / managed profile 显式配置
- runtime / operator 配置暴露
- unit / integration tests

本次 **没有** 同步实现：

- thesis-aware exit state machine
- close reason / de-risk 拆分
- execution policy matrix
- book-native runtime state objects
- replay / realized diagnostics

## 核心改动

### 1. independent 开仓前新增 quality gate

`aats/services/strategy_engines/families/independent_family.py`

- 新增 `_independent_entry_quality_gate(...)`
- 在 `_evaluate_independent_book(...)` 的 opening / scale-in 路径中调用

当前 quality gate 会额外检查：

- 最低流动性质量
- score 确认次数
- score 稳定性
- execution health 是否允许继续加风险

### 2. 新增 score stability metrics

新增 `ScoreStabilityMetrics`，支持两种来源：

- `recent_target_history`
- `current_signal_confirmation`

如果存在 recent independent targets，会优先使用最近历史分数；
如果当前运行时还拿不到稳定的 recent score history，则退回到当前一轮的多因子确认数，不会因为“没有历史分数持久化”就把系统直接打死。

### 3. 新增 liquidity quality score

新增 `_compute_liquidity_quality_score(...)`，当前基于以下可得输入合成：

- `baseline.factor_scores.liquidity_scale`
- 同方向 microstructure 对齐程度
- 预估滑点占容忍阈值的比例
- 按腿 recent fee drag / churn

### 4. 新增 execution health state

新增 `_independent_execution_health_state(...)`，当前输出：

- `ok`
- `degraded`
- `blocked`

当前主要依据：

- trial guard
- fee drag / churn
- recent low-edge streak

## 新增配置

`aats/bootstrap/settings.py`

- `strategy_hedge_independent_min_confirm_ticks`
- `strategy_hedge_independent_min_score_stability_bps`
- `strategy_hedge_independent_min_liquidity_quality`
- `strategy_hedge_independent_require_execution_health_ok`

`configs/strategy_profiles/derivatives.yaml`

`configs/strategy_profiles/derivatives_live.yaml`

两份 managed profile 都已显式写出这组值，避免出现“代码支持了但 profile 没启用”的问题。

## 运行时暴露

`aats/services/operator/query_service.py`

独立双书的这组新配置已进入 runtime/operator 配置摘要：

- `hedge_independent_min_confirm_ticks`
- `hedge_independent_min_score_stability_bps`
- `hedge_independent_min_liquidity_quality`
- `hedge_independent_require_execution_health_ok`

`aats/api/static/modules/views/strategy-view.js`

策略页独立双书配置卡已新增对应中文说明。

## 当前限制

本次实现仍有明确边界：

1. `score stability` 的主输入还不是专门的多 tick score 序列存储。
   当前优先读 recent targets；没有 recent history 时，退回到当前一轮的多因子确认数。

2. 这次只提高了 **entry / scale-in 的质量门槛**，没有重写 close / de-risk 状态机。

3. 这次没有把 liquidity / stability / execution health 继续抬成新的公共 schema 字段；目前先发布在 candidate metrics 和阻断原因里。

## 验证重点

新增/更新测试覆盖了：

- 流动性不足时阻断开仓
- recent score support 不足时阻断开仓
- execution health degraded 时阻断开仓
- 所有 gate 通过时允许开仓
- managed profiles 显式携带新配置
- runtime / strategy view 能看到新配置
