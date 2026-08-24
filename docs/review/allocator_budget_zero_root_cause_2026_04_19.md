# Allocator `budgeted_notional=0` 根因调研报告

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


- 调研时间：2026-04-19 15:45 UTC+8
- 环境：derivatives-live（`aats-live-derivatives`，BTC-USDT-SWAP）
- 分支：`claude/hopeful-nightingale-8e49de` worktree（从 main@`09530b9` 切出）
- 调研代理：hopeful-nightingale-8e49de subagent

## 一句话结论

**两个串联门控同时阻塞**下单：(1) DecisionEngine 的 `_apply_trade_qualification_gate` 把 `baseline.confidence=0.51` 判为低于 `strategy_short_entry_confidence_min=0.55`，把 `target_qty` 强制降为 `current_position_qty=0`；(2) Allocator 侧 `independent` family 的 `long_leg_score=0.235 / short_leg_score=0.268` 双双低于 `strategy_hedge_independent_{long,short}_entry_threshold=0.30`，即使 DecisionEngine 通过也会再度清零。**根因分类：配置门槛偏高相对当前 baseline 输出分布**（calibration 后未同步下调 confidence/score gates），**不是代码 bug，也不是 allocator 白名单缺失**。

---

## 一、数据通路定位

### 1. 事件发射点

`decision_target_sizing_resolved` 唯一源头：[aats/services/decision_engine/target_position.py:250](../../aats/services/decision_engine/target_position.py:250) `log_position_sizing_breakdown`。两处调用：

- [target_position.py:536](../../aats/services/decision_engine/target_position.py:536) — DecisionEngine 内部 build 路径（首次发射，一般不在主线日志看到）
- [bootstrap/config.py:1798](../../aats/bootstrap/config.py:1798) — `_publish_finalized_decision_outcome` 的最终发射（这是用户看到的日志）

### 2. budgeted_notional / resolved_reference_qty 强制置零逻辑

[target_position.py:187-241](../../aats/services/decision_engine/target_position.py:187) `finalize_position_sizing_breakdown`：

```python
if abs(normalized_target_qty) <= EPSILON_DECIMAL_12:   # L209
    resolved_reference_qty = Decimal("0")
    budgeted_notional = Decimal("0")
    if abs(normalized_balance_reference_qty) > EPSILON_DECIMAL_12 or sizing_breakdown.sizing_mode == "balance_aware":
        normalized_balance_reference_qty = Decimal("0")   # L216
```

**这解释了日志上看到的现象**：`target_qty=0` 时 `resolved_reference_qty` 和 `budgeted_notional` 被显式清零，而 `legacy_reference_qty` 按 `signed_default_qty * scale` 的原始计算保留（所以会出现 `-0.000237...` 这样的残值）。

> 此处代码**语义正确**：`target_qty` 下游才是唯一权威，上游的 legacy/balance 引用只是审计追溯。问题不在 `finalize`，而在上游 `target_qty` 为何是 0。

### 3. target_qty 的上游管线

[target_position.py:503](../../aats/services/decision_engine/target_position.py:503) `target_qty = self._target_quantity(...)` → [L639 `_target_quantity`](../../aats/services/decision_engine/target_position.py:639) → 按 `ai_operating_mode` dispatch：

- `baseline_only` → [`_target_quantity_baseline_only`](../../aats/services/decision_engine/target_position.py:714)
- `ai_assisted` → `_target_quantity_ai_assisted`
- `ai_decision_maker` → `_target_quantity_ai_decision_maker`

三个分支前都统一做 [L660 `_apply_entry_edge_gate`](../../aats/services/decision_engine/target_position.py:1140)（内含 `_apply_trade_qualification_gate`）和 [L669 `_apply_strategy_execution_guards`](../../aats/services/decision_engine/target_position.py:1235)。任一门禁返回 `current_position_qty`，后续就是 `managed=0 → target=0`。

### 4. Allocator / 家族层的并行管线

Allocator 在 DecisionEngine 之后跑，见 [`strategy_engines/families/independent_family.py:218`](../../aats/services/strategy_engines/families/independent_family.py:218) `evaluate_independent_books`，其入口质量门槛 [`strategy_engines/independent/gates.py:148`](../../aats/services/strategy_engines/independent/gates.py:148) `evaluate_entry_quality_gate`：

```python
if score + 1e-9 < entry_threshold:
    blocked_reasons.append(f"independent_{side}_book_signal_below_entry_threshold")
```

`strategy_hedge_independent_long_entry_threshold=0.30` / `strategy_hedge_independent_short_entry_threshold=0.30`（derivatives_live.yaml:362/364）。

---

## 二、配置快照

### derivatives_live.yaml 关键字段（当前生效）

| 字段 | 值 | 来源行 |
|------|-----|--------|
| `ai_operating_mode` | `baseline_only` | :20 |
| `strategy_flat_signal_hold_enabled` | `true` | :225 |
| `strategy_dynamic_leverage_enabled` | `true` | :208 |
| `strategy_edge_noise_buffer_bps` | `2.0` | :241 |
| `strategy_min_net_edge_bps` | `2.0` | :245 |
| `strategy_entry_min_signal_edge_bps` | `5.0` | :264 |
| `strategy_entry_alpha_min` | `0.10` | :266 |
| **`strategy_entry_confidence_min`** | **`0.55`** | :268 |
| `strategy_short_entry_min_signal_edge_bps` | `4.0` | :271 |
| `strategy_short_entry_alpha_min` | `0.15` | :273 |
| **`strategy_short_entry_confidence_min`** | **`0.55`** | :275 |
| `strategy_short_entry_allowed_regimes` | `trend / breakout / range / uncertain` | :255-259 |
| **`strategy_hedge_independent_long_entry_threshold`** | **`0.30`** | :362 |
| **`strategy_hedge_independent_short_entry_threshold`** | **`0.30`** | :364 |
| `strategy_post_close_cooldown_seconds` | `300.0` | :475 |
| `strategy_low_edge_cooldown_seconds` | `900.0` | :487 |
| `strategy_low_edge_streak_limit` | `4` | :485 |

### settings.py 默认（未被 profile 覆盖，实际生效）

| 字段 | 默认值 | 行 |
|------|--------|-----|
| `default_order_qty` | `0.001` | :223 |
| `default_target_leverage` | `1.0` | :440 |
| `max_margin_usage_fraction` | `0.85`（日志看到 0.75，疑 env 覆盖） | :724 |
| `strategy_alpha_edge_bps_scale` | `100.0` | :559 |
| `strategy_cost_guard_enabled` | `True` | :558 |

---

## 三、运行时状态（2026-04-19 15:40 UTC+8）

### 1. 持仓 / fill

- `BTC-USDT-SWAP` 仓位 = 0
- 过去 24h `execution.fill` 事件数 = **0**（确认用户报告的零 fill）

### 2. decision_outcome 分布（过去 1 小时）

```
baseline_dir | final_dir | action | auth           | count
-------------+-----------+--------+----------------+------
flat         | flat      | hold   | reference_only | 111
short        | flat      | hold   | reference_only |   5
```

**116 条决策中 5 条 baseline 判 short，全部被下游降为 flat**。`decision_authority=reference_only` 仅是 `baseline_only` 模式的固定 label（见 `authority_map` [L1926-1930](../../aats/services/decision_engine/target_position.py:1926)），**不参与阻塞**。

### 3. 典型 short-bias 决策的 decision_blocker_chain

`decision_7a264b... / decision_ddb03d... / decision_c28b5f...`（5 条短路径样本一致）：

```json
[
  {"stage": "baseline", "blocked": true,
   "reasons": [
     "baseline_regime_uncertain_threshold_crossed",
     "baseline_target_not_promoted_to_actionable_target"
   ],
   "direction_bias": "short",
   "direction_rule": "baseline_regime_uncertain_threshold_crossed",
   "direction_threshold": 0.15},
  {"stage": "target_gate", "blocked": true,
   "reasons": ["short_entry_confidence_below_threshold"]},
  {"stage": "ai_gate", "blocked": false, "reasons": []}
]
```

**target_gate 阻塞理由 = `short_entry_confidence_below_threshold`**。`baseline_target_not_promoted_to_actionable_target` 不是独立阻塞，而是 target_gate 把 qty 拉回 0 的派生标签（见 [L2060-2065](../../aats/services/decision_engine/target_position.py:2060)）。

### 4. baseline_reference 数值（5 条 short 样本完全一致，说明来自同一特征快照）

| 字段 | 值 |
|------|-----|
| `direction_bias` | `short` |
| `confidence` | **`0.5065197662976615`** |
| `composite_alpha_score` | `-0.15299972028611097`（abs=0.153 > 0.15 阈值，刚过） |
| `regime` | `uncertain` |
| `suggested_position_scale` | ~0.20 |
| `signal_edge_bps` | 未入库（此字段不落 baseline_reference，见 L1994-2004；但由 `_signal_edge_bps = \|alpha\| * 100 = 15.3 bps`，远超 `short_entry_min_signal_edge_bps=4.0`） |

### 5. baseline.confidence 24h 分布

```
min    = 0.4380
max    = 0.6502
median = 0.5244
p95    = 0.5783
```

**`strategy_short_entry_confidence_min=0.55` 仅被 ~10–15% 的决策触及**；5 条 short 样本 `confidence=0.5065` 在 25 分位附近，**结构性无法过门槛**。

### 6. 并行 Allocator 决策（最近样本）

```
symbol         | target_qty | delta_qty | target_notional | route         | primary_family
---------------+------------+-----------+-----------------+---------------+---------------
BTC-USDT-SWAP  | 0          | 0         | 0               | advisory_only | independent
```

Reason codes：
- `allocator_v2_phase2_applied`
- `allocator_primary_family_independent`
- `legacy_configured_strategy_family_independent_unavailable`
- **`independent_long_book_signal_below_entry_threshold`**
- **`independent_short_book_signal_below_entry_threshold`**
- `independent_family_candidate_inactive`
- `legacy_configured_strategy_family_independent_hold_only`

对应的 sleeve_intents 指标：
- `long_leg_score = 0.2353` （`entry_threshold=0.30` → 差 0.065）
- `short_leg_score = 0.2677` （`entry_threshold=0.30` → 差 0.032）

**即使 DecisionEngine 的 target_gate 放行，Allocator 的独立双书门槛也会挡住（independent 是当前 primary_family，directional 被 `legacy_configured_strategy_family_independent_unavailable` 移除）**。

---

## 四、根因假设验证矩阵

| 假设 | 验证方法 | 结果 |
|------|----------|------|
| H1：Allocator 缺 symbol 白名单 / notional quota | 查 `portfolio_allocation_decisions` payload | ❌ 假。`target_notional=0` 来自 `target_position_qty=0`，不是白名单缺失。`primary_family=independent` 正常路由到。 |
| H2：AI 决策门禁（`baseline_only` 模式 allocator 不给预算） | 代码读 `_decision_outcome` 的 `authority_map` | ❌ 假。`reference_only` 只是 label，不参与阻塞。ai_gate 实际是 `blocked:false`。 |
| H3：Conviction / confidence 门槛 | 对比 `baseline.confidence=0.5065` vs `strategy_short_entry_confidence_min=0.55` | ✅ **真**。target_gate 返回 `short_entry_confidence_below_threshold`。 |
| H4：Independent family 未启用 | 查 `strategy_hedge_overlay_enabled / mode` | ❌ 假。`enabled=true`，`mode=independent`，family 已跑到 `evaluate_independent_books`。 |
| H5：Cost gate（expected_net_edge 负） | 查 `_apply_entry_edge_gate` cost_guard 是否触发 | ❌ **部分假**。5 条 short 样本在 `_apply_trade_qualification_gate` 就已返回 0，`cost_guard` 在 desired=0 时直接放行；实际 `signal_edge_bps ≈ 15.3`，扣 cost 后 net ≈ 15.3 - 6 - 2 - 2 = 5.3 bps > 0，cost_guard 不会触发。之前用户看到 "expected_net_edge 负" 的诊断可能是 baseline 未达 short bias 的 flat 场景（signal_edge 接近 0）。 |
| H6：fixed_order_qty 被硬编码为 0 | 查 `default_order_qty` 配置 | ❌ 假。`default_order_qty=0.001`（日志中 `legacy_reference_qty=-0.000237 = -0.001 × 0.2375 scale`，与配置一致）。 |
| H7（新发现）：Allocator 独立双书门槛结构性未达 | 查 `long_leg_score` / `short_leg_score` vs `strategy_hedge_independent_{long,short}_entry_threshold=0.30` | ✅ **真**。`long_leg=0.235 / short_leg=0.268`，均 <0.30。即使 H3 修复也会被此层挡住。 |

---

## 五、最终判定

**根因：calibration 层刚下调 composite_alpha 阈值到 ±0.15（commit 6344f00）后，DecisionEngine 和 Allocator 两层的"下游门槛" 没同步下调**，导致：

1. baseline 能产出 `direction_bias=short` 了（`|alpha|=0.153 > 0.15`），但
2. `confidence`（另一套独立计算，[baseline.py:145](../../aats/services/decision_engine/baseline.py:145) `0.35 + |alpha|*0.35 + regime_conf*0.2 + pos_scale*0.1`）在 uncertain regime + 刚过阈值的 alpha 下**结构性在 0.50 附近**，触不到 DecisionEngine 的 `confidence_min=0.55`；
3. Allocator 侧 `independent` family 的 `leg_score`（聚合 alpha/momentum/trend/microstructure 的独立评分）在当前弱信号下**结构性在 0.24–0.27**，触不到 `entry_threshold=0.30`。

**类型：配置（不是代码 bug，不是设计意图）**。设计意图是信号强的时候才下单，当前问题是"信号强度的度量尺"（confidence 0.5 vs 0.55、leg_score 0.27 vs 0.30）和"信号强度的定义"（composite_alpha 0.15 阈值）**不同步**。

---

## 六、修复方案

### 方案 A（推荐）：两层门槛同步下调，与新 calibration 对齐

在 `configs/strategy_profiles/derivatives_live.yaml`：

```yaml
# 从 0.55 下调到 0.50
# 理由：24h baseline.confidence 分布 median=0.52，p95=0.58。
# 0.55 阈值只被 ~15% 决策触及，且与 composite_alpha=0.15 的新 calibration 不同步。
# 0.50 对应约 40 分位，允许"刚过 alpha 阈值 + uncertain regime"通过，
# 由 target_gate 的 alpha / signal_edge / cost_guard 继续保底。
strategy_entry_confidence_min: 0.50
strategy_short_entry_confidence_min: 0.50

# 从 0.30 下调到 0.25
# 理由：独立双书当前 long=0.24 / short=0.27，0.25 让"strong enough to enter"门槛
# 与 DecisionEngine 同级（≈直接对应 confidence 0.50）。
# 真正的防乱下单由 liquidity_quality / min_confirm_ticks / score_stability 继续保底（gates.py:163-169）。
strategy_hedge_independent_long_entry_threshold: 0.25
strategy_hedge_independent_short_entry_threshold: 0.25
```

**风险评估**：
- ↑ 下单频次：估算从 0/24h → 5–10/24h（基于短偏离决策 ~5/小时 → 约每 6 小时一次可入场，再经 cost_guard/cooldown 过滤）。
- ↑ 误判风险：可控。三层保底仍在：(1) DecisionEngine 侧 `alpha_min=0.15`、`signal_edge_min=4.0`、`cost_guard`、`post_close/low_edge cooldown`；(2) Allocator 侧 `liquidity_quality`、`min_confirm_ticks`、`score_stability`；(3) RiskEngine 的 position / leverage cap。
- 单次亏损边界：`budgeted_notional ≈ $393 × 0.75 × 1.035 × 0.20 ≈ $61`，最大 leverage 1.035 下 ≈ 0.0008 BTC 持仓，满仓滑点 + 费率损失上限 ≈ $0.10–0.20。

### 方案 B（保守）：只下调 DecisionEngine 层，让 allocator 继续挡

只改 `strategy_entry_confidence_min: 0.50`，保留 `independent_{long,short}_entry_threshold=0.30`。

效果：DecisionEngine 开始产生非零 target_qty（日志里 `final_target_qty` 从 0 变成实际值），但 allocator 会把 route 从 `advisory_only` 保持不变，实际仓位仍 0。用户能在日志上看到 "direction_bias=short → final_direction=short → allocator_blocked" 的链条，**便于继续诊断 allocator 层**，但仍不下单。

**用途**：想先把"baseline 能产出短信号"完整链条打通到日志可视，再单独观察 allocator。

### 方案 C（激进）：直接降 composite_alpha 阈值让信号更强

不改 confidence / leg_score，下调 `direction_threshold` 到 0.10。
**不推荐**：用户明确要求"不要改 calibration 阈值（刚 deploy，等观察 24h）"。

### 方案 D（设计接受）：不改，维持"弱信号不下单"

**不推荐**：用户目标是"真金白银动起来实盘"，24h 零 fill 已经触发运营信号，需要至少有 actionable flow。

### 建议：先 A，24h 观察 fill 分布和 PnL；若仍过少再单独微调 alpha_min 或 signal_edge_min。不建议一次改太多。

---

## 七、配置补丁（未 apply）

以下是推荐的最小修改 diff（仅展示，**调研 subagent 不执行**，留给主任务决定是否落地）：

```diff
--- a/configs/strategy_profiles/derivatives_live.yaml
+++ b/configs/strategy_profiles/derivatives_live.yaml
@@ -265,7 +265,11 @@
 # [常用可调] strategy_entry_alpha_min：开仓最低 alpha。可选值：0.0 ~ 1.0；推荐值：0.18。
 strategy_entry_alpha_min: 0.10
 # [常用可调] strategy_entry_confidence_min：开仓最低置信度。可选值：0.0 ~ 1.0；推荐值：0.67。
-strategy_entry_confidence_min: 0.55
+# 2026-04-19 calibration 后下调：baseline.confidence 24h median=0.52，p95=0.58，
+# 0.55 只被 ~15% 决策触及；下调到 0.50（~40 分位）与新 composite_alpha 阈值 0.15 对齐。
+strategy_entry_confidence_min: 0.50
@@ -272,7 +276,10 @@
 # [进阶可调] strategy_short_entry_alpha_min：新开空最低 alpha。当前只在合约运行域生效。可选值：0.0 ~ 1.0；推荐值：0.15。
 strategy_short_entry_alpha_min: 0.15
 # [进阶可调] strategy_short_entry_confidence_min：新开空最低置信度。当前只在合约运行域生效。可选值：0.0 ~ 1.0；推荐值：0.55。
-strategy_short_entry_confidence_min: 0.55
+# 2026-04-19 同步下调，理由同 long。
+strategy_short_entry_confidence_min: 0.50
@@ -360,10 +367,12 @@
 # [进阶可调] strategy_hedge_independent_long_entry_threshold：独立 long book 的开仓阈值。可选值：0.0 ~ 1.0；当前实盘钉住值：0.30。
-strategy_hedge_independent_long_entry_threshold: 0.30
+# 2026-04-19 下调到 0.25，与 DecisionEngine confidence_min=0.50 同级，真正防乱单由 liquidity_quality + min_confirm_ticks + score_stability 保底。
+strategy_hedge_independent_long_entry_threshold: 0.25
 # [进阶可调] strategy_hedge_independent_short_entry_threshold：独立 short book 的开仓阈值。可选值：0.0 ~ 1.0；当前实盘钉住值：0.30。
-strategy_hedge_independent_short_entry_threshold: 0.30
+# 2026-04-19 同步下调。
+strategy_hedge_independent_short_entry_threshold: 0.25
```

建议 commit message（若落地）：

```
fix(allocator): 下调 entry confidence / independent leg 门槛与新 calibration 对齐

04-19 baseline composite_alpha 阈值下调到 ±0.15 后，两层下游门槛未同步：
- DecisionEngine: confidence_min=0.55 vs baseline 24h median=0.52（结构性触不到）
- Allocator independent: entry_threshold=0.30 vs leg_score 0.24-0.27（结构性触不到）

导致 24h 零 fill，5 条 short-bias 决策全部被 target_gate (short_entry_confidence_below_threshold)
和 allocator (independent_{long,short}_book_signal_below_entry_threshold) 阻塞。

- confidence_min: 0.55 → 0.50 (long + short)
- hedge_independent_{long,short}_entry_threshold: 0.30 → 0.25

三层保底仍在：alpha_min / signal_edge_min / cost_guard（DecisionEngine）、
liquidity_quality / min_confirm_ticks / score_stability（Allocator）、
position/leverage cap（RiskEngine）。

docs/review/allocator_budget_zero_root_cause_2026_04_19.md
```

---

## 八、报告给主任务的关键信息

1. **根因类型**：配置（不是代码 bug、不是 allocator 白名单缺失、不是 AI 决策门禁）。
2. **阻塞在两层**：DecisionEngine target_gate (`confidence_min=0.55`) + Allocator independent family (`leg_entry_threshold=0.30`)，两者独立触发，都需要调整。
3. **需要主任务决定**：
   - 是否采纳**方案 A**（同步下调两层门槛，一次性修复）；
   - 或采纳**方案 B**（只改 DecisionEngine，先看 allocator 链路日志再定）。
4. **没做的事**（按约束）：
   - 未推 GitHub、未 deploy；
   - 未改任何 baseline / calibration 阈值；
   - 未卸载 feature flag（basis/funding/oi/ls 保持 enabled）；
   - 未读取凭证文件（`.env.derivatives.live` 未 Read，`max_margin_usage_fraction=0.75` 的 env 覆盖点未进一步追踪，非根因）。
5. **建议主任务下一步**：
   - 不需要再 spawn 子任务；直接在主线 commit + deploy `derivatives_live.yaml` 的 4 行修改即可；
   - 部署后 1–2h 内应能看到第一个非零 `final_target_qty`，24h 内预计 5–10 次 fill；
   - 若 24h 后 fill 分布仍偏少，再微调 `alpha_min` 或 `entry_min_signal_edge_bps`。
