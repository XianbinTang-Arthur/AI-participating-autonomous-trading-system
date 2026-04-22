# Round 3 · Paper Trading 使用指南

> **状态**：Phase 1 基建已完成并部署（默认 OFF）
> **更新**：2026-04-22 autonomous session
> **前置**：`docs/task/round3_paper_trading_design.md`（设计文档）

---

## 这是什么

Paper trading shadow 让您**用真实实盘流量**同步跑一个或多个"候选配置"的策略，
记录"如果采用该配置会做什么决策"，对比 live baseline 的决策分歧 —— 而
**完全不触发任何真实下单**。

## 如何启用

改 `configs/strategy_profiles/derivatives_live.yaml`，加两个字段：

```yaml
# 例 · 测试"把 entry_threshold 从 0.25 降到 0.15 会触发多少新交易"
paper_trading_shadow_enabled: true
paper_trading_shadow_candidates:
  - candidate_id: "independent_threshold_0.15"
    family: "independent"
    overrides:
      strategy_hedge_independent_long_entry_threshold: 0.15
      strategy_hedge_independent_short_entry_threshold: 0.15
```

然后 `bash scripts/deploy.sh --skip-commit` —— decision 进程重启后 shadow 开始跑。

## 如何验证 shadow 在工作

```bash
# 最近 5 分钟 shadow 决策数
wsl -d Ubuntu -- docker exec aats-postgres psql -U admin -d aats_live_derivatives \
  -tAc "SELECT COUNT(*) FROM event_store WHERE event_type='StrategyFamilyShadowDecision'
        AND event_timestamp > NOW() - INTERVAL '5 min';"
# 应该 > 0（enabled 后）

# 看最新一条 shadow 决策的原始 payload
wsl -d Ubuntu -- docker exec aats-postgres psql -U admin -d aats_live_derivatives \
  -c "SELECT payload FROM event_store WHERE event_type='StrategyFamilyShadowDecision'
      ORDER BY event_timestamp DESC LIMIT 1;"

# 日志里看 shadow 运行事件
wsl -d Ubuntu -- docker logs aats-decision --since 10m 2>&1 | grep "paper_trading"
```

## Shadow 决策字段（StrategyFamilyShadowDecision）

| 字段 | 含义 |
|------|------|
| `shadow_decision_id` | 本条 shadow 的唯一 ID |
| `decision_id` | 对应的 live decision id（join key） |
| `candidate_id` | 人类可读候选名（您在 yaml 里设的） |
| `candidate_config_version` | overrides 的 sha256[:16]（跨周期聚合 key） |
| `baseline_target_qty` | Live baseline 的目标仓位 |
| `shadow_target_qty` | Candidate 会开的目标仓位 |
| `would_override_baseline` | bool：shadow 和 baseline 决策是否不同 |
| `shadow_action_type` | 5 分类：same_as_baseline / hold_instead / entry_override / exit_override / reverse_override |

## 关键分析 query（SQL）

### 1. 候选总览（最近 24h）

```sql
SELECT
  payload->>'candidate_id' AS candidate_id,
  COUNT(*) AS total_shadow_decisions,
  COUNT(*) FILTER (WHERE (payload->>'would_override_baseline')::bool) AS would_override,
  ROUND(100.0 * COUNT(*) FILTER (WHERE (payload->>'would_override_baseline')::bool) /
        NULLIF(COUNT(*), 0), 1) AS override_rate_pct
FROM event_store
WHERE event_type = 'StrategyFamilyShadowDecision'
  AND event_timestamp > NOW() - INTERVAL '24 hours'
GROUP BY 1
ORDER BY total_shadow_decisions DESC;
```

### 2. 按 shadow_action_type 分布

```sql
SELECT
  payload->>'candidate_id' AS candidate_id,
  payload->>'shadow_action_type' AS action_type,
  COUNT(*)
FROM event_store
WHERE event_type = 'StrategyFamilyShadowDecision'
  AND event_timestamp > NOW() - INTERVAL '24 hours'
GROUP BY 1, 2
ORDER BY 1, 3 DESC;
```

### 3. Override 窗口（shadow 和 baseline 具体多大差异）

```sql
SELECT
  payload->>'candidate_id' AS candidate_id,
  (payload->>'baseline_target_qty')::numeric AS baseline_qty,
  (payload->>'shadow_target_qty')::numeric AS shadow_qty,
  ABS((payload->>'shadow_target_qty')::numeric - (payload->>'baseline_target_qty')::numeric) AS abs_diff,
  event_timestamp
FROM event_store
WHERE event_type = 'StrategyFamilyShadowDecision'
  AND (payload->>'would_override_baseline')::bool = true
  AND event_timestamp > NOW() - INTERVAL '24 hours'
ORDER BY event_timestamp DESC
LIMIT 20;
```

## 候选示例库

### 候选 A：降 entry_threshold

```yaml
- candidate_id: "independent_threshold_0.15"
  family: "independent"
  overrides:
    strategy_hedge_independent_long_entry_threshold: 0.15
    strategy_hedge_independent_short_entry_threshold: 0.15
```

**验证什么**：信号能过门槛吗？目前 score ≈ 0.02 连 0.15 也过不了 — shadow 应该和 baseline 高度一致。这是**确认问题不在 threshold** 的有力数据。

### 候选 B：降 noise_buffer

```yaml
- candidate_id: "independent_low_buffer"
  family: "independent"
  overrides:
    strategy_edge_noise_buffer_bps: 1.0  # 从 4.0 降
```

**验证什么**：buffer 松一点是否让 net_edge gate 放行更多决策？

### 候选 C：调低 slippage fraction

```yaml
- candidate_id: "independent_lower_slippage"
  family: "independent"
  overrides:
    strategy_expected_slippage_bps_fraction: 0.15  # 从 0.28 降
```

**验证什么**：更准确的成本估算是否改变决策？

### 候选 D：多候选并行

```yaml
paper_trading_shadow_candidates:
  - candidate_id: "threshold_0.15"
    family: "independent"
    overrides:
      strategy_hedge_independent_long_entry_threshold: 0.15
  - candidate_id: "buffer_1.0"
    family: "independent"
    overrides:
      strategy_edge_noise_buffer_bps: 1.0
  - candidate_id: "combined"
    family: "independent"
    overrides:
      strategy_hedge_independent_long_entry_threshold: 0.15
      strategy_edge_noise_buffer_bps: 1.0
```

每个 candidate 每个决策周期都产出一条 shadow 决策；跑 1-2 天数据后用上面
SQL 对比。

## Phase 1 已知局限（Phase 2 / 3 会解决）

1. **没 PnL 数据**：shadow 只记决策分歧，不算"会赚/亏多少钱"。Phase 2 接
   cheap PnL model（`mid + slippage at t+15s`）。
2. **没窗口聚合报告**：每条 shadow 单独存，看趋势要手写 SQL。Phase 2 引入
   `StrategyFamilyShadowEvaluation` 自动按 50 个决策窗口聚合。
3. **没 Grafana 面板**：Phase 3 加专门 dashboard。当前只能用 SQL 查。
4. **热切换需要重启**：改 yaml + `deploy.sh`，decision 进程重启（~10s）。
   Phase 3 可选加 operator API 热切。

## 安全保证（已测试）

本场 5 个 anchor tests 锁定:
- `paper_trading_shadow_service=None` → 零开销 skip
- `enabled()=False` → skip, 不调 evaluate_candidates
- evaluate_candidates 抛异常 → swallow, 不影响 live run_cycle
- 单 publish 失败 → 继续下一个 candidate
- 正确 publish 到 `strategy.family_shadow_decision`

---

## 一句话结论

**现在不用碰这玩意**。Phase 1 是基建，您决定 "要测哪个参数" 时改 yaml 重启
即可。您可以在我继续做别的时直接用上面的候选 A/B/C 样例开始收数据，
我不需要您额外指令。
