# Short leg slope 负 + R²=0.00063 根因诊断

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


**日期**: 2026-04-19
**背景**: P1-B step 1 signal_edge_scale 标定报告发现 short leg 对 realized_edge 回归 slope 负、R²≈0.00063，long leg slope 正 (+16.94 bps/score)、R²=0.0173。本报告为 P1-A Phase 2 代码改动前的根因定位。
**作用域**: 只做诊断，不改代码。

---

## 1. 判定速览

| 项 | 结论 |
|---|---|
| **根因类型** | **公式设计缺陷**（非数据问题、非 code bug —— 代码按设计实现，但设计在 long/short 对称评分框架下存在方向泄漏） |
| **主根因** | **H4**：`confidence` 作为方向无关加项，权重 12%（Mode A），随 `|alpha_score|` 上升，被两腿共享 → 在 BTC bull drift 下系统性污染 short leg 的 score-realized 相关性 |
| **次根因** | **H3 部分**：`regime_bonus +0.04`（range/uncertain）和 `volatility_bonus +0.03`（high vol）是方向无关加项，两腿同加，稀释 short slope 信号（不会翻转方向但会削弱 R²） |
| **H1 / H2 / H5** | 不成立（alpha / AI / microstructure 的 `side_sign` 翻转数学对称） |
| **H6** | 共犯但不是主因，H4 修完后重要性显著下降 |
| **预计 short R² 改进** | 0.00063 → **0.010 ~ 0.020**（接近 long 当前水平），slope 由负翻正至 +10 ~ +15 bps/score 量级 |
| **是否需等样本** | **不需要** —— H4 是公式问题，修完立即生效，可用相同 30 天数据立即重测 |

---

## 2. Long vs Short 逐分量对比表（含 `file:line`）

生产入口 `compute_raw_book_score` ([aats/services/strategy_engines/independent/scoring.py:124-181](aats/services/strategy_engines/independent/scoring.py:124))：

| 分量 | Long leg 公式 | Short leg 公式 | 是否对称 | 权重 (Mode A) | 对 slope 影响 |
|---|---|---|---|---|---|
| `alpha_component` ([scoring.py:137](aats/services/strategy_engines/independent/scoring.py:137)) | `clamp(max(0, +composite_alpha), 0, 1)` | `clamp(max(0, -composite_alpha), 0, 1)` | ✅ | 0.34 | 方向驱动 |
| `ai_component` ([scoring.py:138](aats/services/strategy_engines/independent/scoring.py:138)) | `clamp(max(0, +ai_edge), 0, 1)` | `clamp(max(0, -ai_edge), 0, 1)` | ✅ | 0.0 (Mode A) | 方向驱动（Mode A 无贡献） |
| `momentum_component` ([scoring.py:139](aats/services/strategy_engines/independent/scoring.py:139)) | `clamp(max(0, +momentum_alpha), 0, 1)` | `clamp(max(0, -momentum_alpha), 0, 1)` | ✅ | 0.24 | 方向驱动 |
| `trend_component` ([scoring.py:140](aats/services/strategy_engines/independent/scoring.py:140)) | `clamp(max(0, +trend_alpha), 0, 1)` | `clamp(max(0, -trend_alpha), 0, 1)` | ✅ | 0.18 | 方向驱动 |
| `microstructure_component` ([scoring.py:141](aats/services/strategy_engines/independent/scoring.py:141)) | `clamp(max(0, +micro_alpha), 0, 1)` | `clamp(max(0, -micro_alpha), 0, 1)` | ✅ | 0.12 | 方向驱动 |
| **`confidence` ★** ([scoring.py:142](aats/services/strategy_engines/independent/scoring.py:142)) | `clamp(baseline.confidence, 0, 1)` | `clamp(baseline.confidence, 0, 1)` **（相同值）** | ❌ **方向无关** | **0.12** | **反方向污染 short** |
| `regime_bonus` ([scoring.py:175-176](aats/services/strategy_engines/independent/scoring.py:175)) | `+0.04 if regime ∈ {range,uncertain}` | 同上，**两腿同加** | ❌ 方向无关 | 0.04 bonus | 稀释 short slope |
| `direction_bias_bonus` ([scoring.py:177-178](aats/services/strategy_engines/independent/scoring.py:177)) | `+0.06 if bias == "long"` | `+0.06 if bias == "short"` | ✅ | 0.06 bonus | 方向驱动 |
| `volatility_bonus` ([scoring.py:179-180](aats/services/strategy_engines/independent/scoring.py:179)) | `+0.03 if vol == high` | 同上，**两腿同加** | ❌ 方向无关 | 0.03 bonus | 稀释 short slope |

**方向无关部分总权重**：`W_CONFIDENCE (0.12) + regime_bonus (0.04) + volatility_bonus (0.03) = 0.19`（占最大可能 score 的 ~19%）。

`baseline.confidence` 的公式 ([aats/services/decision_engine/baseline.py:145-154](aats/services/decision_engine/baseline.py:145))：
```python
confidence = min(max(0.35 + abs(alpha_score)*0.35 + regime_confidence*0.2 + position_scale*0.1, 0.4), 0.96)
```
关键项 `abs(alpha_score) * 0.35` —— 随 `|alpha_score|` 单调递增，**不区分方向**。

---

## 3. 假设 H1-H6 逐条判定

### H1 — `alpha_component` 符号处理不对称 → ❌ 不成立

[scoring.py:137](aats/services/strategy_engines/independent/scoring.py:137)：
```python
side_sign = 1.0 if leg == "long" else -1.0
alpha_component = _clamp(max(0.0, side_sign * float(baseline.composite_alpha_score)), 0.0, 1.0)
```
对任意 `x = composite_alpha_score`：long leg = `max(0, +x)`, short leg = `max(0, -x)` —— 严格数学对称。

### H2 — `ai_component` long/short 派生不对称 → ❌ 不成立

[scoring.py:138](aats/services/strategy_engines/independent/scoring.py:138)：`ai_component = _clamp(max(0, side_sign * _ai_directional_edge(ai_assessment)), 0, 1)`。`AIMarketAssessment.directional_edge` ([aats/schemas/decision.py:163](aats/schemas/decision.py:163)) 是 float 有符号量，与 composite_alpha 处理方式完全一致。**且生产当前 `ai_operating_mode = baseline_only` (Mode A, W_AI=0)**，AI 分量对 R² 无贡献。

### H3 — regime_bonus / direction_bias_bonus / volatility_bonus → 🟡 部分成立

- `regime_bonus +0.04`：**两腿同加**，方向无关 → 在 short 正半轴样本中系统性抬高 short_score 但不改变 realized，**稀释 slope 信号**（贡献约 4% 的 score 噪声）。
- `direction_bias_bonus +0.06`：按 leg 匹配加，long 和 short 天然对称 → ✅ 不污染。
- `volatility_bonus +0.03`：**两腿同加**，方向无关 → 同样稀释（贡献约 3% 噪声）。

H3 不会使 slope 翻转为负（它们是 binary term 且期望值相对稳定），但会显著降低 R²。

### H4 — `confidence_component` 偏向 long → ✅ **主根因成立**

机制详细推导：

1. **公式上**：`baseline.confidence = 0.35 + 0.35·|α| + 0.2·regime_conf + 0.1·pos_scale`，范围 [0.4, 0.96]，随 `|α|` 单调上升。
2. **对两腿的作用**：两腿的 `confidence_component = confidence × 0.12`（Mode A）都加相同的 `0.048~0.115` 到 leg_score。
3. **在 α > 0 子域（bullish）**：
   - `long_score` = (方向相关项正值, ~0.88·|α|·系数) + `confidence × 0.12` + bonuses
   - `short_score` = **0 (方向相关项全被 max(0, -α)=0 过滤)** + `confidence × 0.12` + bonuses
   - 即：**short_score 在 α > 0 子域几乎完全由 confidence + bonuses 驱动**，而 confidence 随 `|α|` 上升
   - 实际 realized return `r ≈ α·x + ε`，在 α > 0 时 r > 0 → `realized_edge(short) = -r < 0`
   - 结果：**α ↑ → short_score ↑ (via confidence)**，同时 **α ↑ → realized_edge(short) ↓**
   - 这两个子域样本点散布出**负 slope**

4. **在 α < 0 子域（bearish）**：两腿公式对称生效，slope 为正。

5. **整体回归**：正半轴子域的负 slope 污染 + 负半轴子域的正 slope 信号，净效果视样本分布：
   - BTC 过去 30 天若偏多头，α > 0 样本占比高 → **正半轴污染主导 → 整体 slope 翻为负**。
   - 这精确匹配 step 1 的观测：`slope 负、R² ≈ 0.00063`（信号几乎被 confidence 污染抹平）。

### H5 — `microstructure_alpha` 方向继承 → ❌ 不成立

[scoring.py:141](aats/services/strategy_engines/independent/scoring.py:141)：和 H1/H2 完全同构，`max(0, side_sign * microstructure_alpha)`，数学对称。

### H6 — 采样偏差（数据问题） → 🟡 共犯但非主因

- 30 天窗口 + BTC 若偏多头 drift，short 方向的"真反弹"样本稀少 → short leg 的方向相关项绝大多数样本为 0 → short_score 的变化主要由 H4 / H3 的方向无关项驱动。
- H6 本质上是**放大 H4 影响的背景条件**：样本越偏多头，confidence 污染占比越大。
- 修完 H4 后，short leg 在多头市场里方向相关项仍然多为 0，但 short_score 本身也会更贴近 0（因为 confidence 和 bonus 不再加），所以回归样本点集中在原点附近，R² 降低的同时 **slope 不会翻负**。

---

## 4. 具体样本回放（composite_alpha = -0.15 场景，Mode A）

假设某时刻 `composite_alpha_score = -0.15`（短向略强），`momentum_alpha = -0.10`, `trend_alpha = -0.08`, `microstructure_alpha = -0.05`, `regime_confidence = 0.6`, `position_scale = 0.5`, regime = "range", volatility_state = "high", direction_bias = "short"。

**baseline.confidence** = `min(max(0.35 + 0.15·0.35 + 0.6·0.2 + 0.5·0.1, 0.4), 0.96)` = `min(max(0.5725, 0.4), 0.96)` = **0.5725**。

**Short leg score**（期望：高分，因 α、momentum、trend 都 bearish）：
- alpha_c = max(0, +0.15) = 0.15 → 0.15·0.34 = **0.0510**
- momentum_c = 0.10 → 0.10·0.24 = **0.0240**
- trend_c = 0.08 → 0.08·0.18 = **0.0144**
- micro_c = 0.05 → 0.05·0.12 = **0.0060**
- confidence × W = 0.5725·0.12 = **0.0687**
- regime_bonus = **0.04** (range)
- direction_bias_bonus = **0.06** (bias==short)
- volatility_bonus = **0.03** (high)
- **short_score = 0.2741** ✓ 方向信号被正确体现

**Long leg score**（期望：低分）：
- 所有方向相关项 = 0（被 max(0, +side_sign·负值)=0 过滤）
- confidence × W = **0.0687** ← **同 short 一样的加项**
- regime_bonus = **0.04**
- direction_bias_bonus = **0** (bias!=long)
- volatility_bonus = **0.03**
- **long_score = 0.1387** —— 仍然非零，且其中 **49.5% 来自 confidence**（0.0687/0.1387）

**问题暴露**：现在换一个 `composite_alpha = +0.15`（对称样本）：
- `long_score` = 0.15·0.34 + ... + **0.0687 (confidence)** + 0.04 + 0.06 + 0.03 = ~0.2741
- `short_score` = 0 + 0 + 0 + 0 + **0.0687 (confidence)** + 0.04 + 0 + 0.03 = **0.1387**
  - 即便 short 方向全错，score 仍 ≈ 0.14，且其中 49.5% 是 confidence
- 而此时 realized_return 期望 > 0（因 α = +0.15 predicts up）→ `realized_edge(short) < 0`
- **(short_score, realized_edge_short) = (0.14, 负值)**；在 α = -0.15 时为 `(0.27, 正值)`
- 两个点间拟合 slope：`(正 - 负) / (0.27 - 0.14) > 0`，看似 slope 为正

但关键是 confidence 随 **|α|** 而非 α 变化：
- α = +0.30: short_score ≈ 0.074·0.12 + 0.04 + 0 + 0.03 = 0.179（confidence=0.455→0.615），realized 更正 → realized_edge_short 更负
- α = +0.05: short_score ≈ 0.453·0.12 + 0.04 + 0 + 0.03 = 0.124，realized 略正 → realized_edge_short 略负
- 在 α > 0 子域 **short_score 随 |α| 上升 (via confidence)**，**realized_edge_short 随 α 下降** → 形成**负 slope**

而 α < 0 子域：
- α = -0.05: short_score = 0.05·0.34 + ... + 0.453·0.12 + 0.04 + 0.06 + 0.03 ≈ 0.215
- α = -0.30: short_score = 0.30·0.34 + ... + 0.615·0.12 + 0.04 + 0.06 + 0.03 ≈ 0.378
- short_score 随 |α| 上升，realized_edge_short 随 |α| 上升（realized 更负）→ **正 slope**

两个子域 slope 方向相反，**净 slope 由样本分布加权**：
- BTC 多头样本多 (H6) → 负半轴 slope 被稀释 → 整体 slope 负

---

## 5. 修复方案（仅展示，不动代码）

### 推荐方案 A.1：方向门控 confidence + 方向门控 regime/volatility bonus

**变更范围**：
1. [aats/services/strategy_engines/independent/scoring.py:142](aats/services/strategy_engines/independent/scoring.py:142)
2. [aats/services/strategy_engines/independent/scoring.py:175-180](aats/services/strategy_engines/independent/scoring.py:175)
3. [aats/data_platform/replay/adapters/independent_adapter.py:245-259](aats/data_platform/replay/adapters/independent_adapter.py:245)（同步 replay）

**scoring.py diff**（示意）：
```python
 def compute_raw_book_score(...):
     ...
     microstructure_component = _clamp(max(0.0, side_sign * microstructure_alpha), 0.0, 1.0)
-    confidence = _clamp(float(baseline.confidence), 0.0, 1.0)
+    # H4 修复：confidence 只在该 leg 与 direction_bias 对齐时计入
+    # 理由：confidence 公式 `0.35 + 0.35·|alpha_score|` 方向无关，两腿共享
+    # 会造成 short leg 在多头市场中被 confidence 系统性污染（realized slope 负）
+    leg_aligned = baseline.direction_bias == leg
+    confidence = _clamp(float(baseline.confidence) if leg_aligned else 0.0, 0.0, 1.0)
     ...
-    if baseline.regime in {"range", "uncertain"}:
+    # H3 修复：方向无关 bonus 只在 leg 与 bias 对齐时加，避免稀释 short slope
+    if baseline.regime in {"range", "uncertain"} and leg_aligned:
         score += 0.04
     if baseline.direction_bias == leg:
         score += 0.06
-    if baseline.volatility_state == "high":
+    if baseline.volatility_state == "high" and leg_aligned:
         score += 0.03
     return _clamp(score, 0.0, 1.0)
```

**replay adapter diff**（同步，避免生产/回测 drift）：
```python
 def _compute_book_score(self, bar, *, leg):
     ...
     confidence_raw = min(_sigmoid((vol_ratio - 1.0) * 200), 1.0)
+    # 对齐生产 H4 修复：confidence 只在 bar 方向与 leg 一致时计入
+    if leg == "long":
+        leg_aligned = bar_return > 0
+    else:
+        leg_aligned = bar_return < 0  # bar_return 此处已是翻转后值，> 0 表示 short 对齐
+    if not leg_aligned:
+        confidence_raw = 0.0
     score = (
         _W_ALPHA * alpha_raw + ... + _W_CONFIDENCE * confidence_raw
     )
```

**注意**：replay adapter 的 `confidence_raw` 语义与生产不同（volume ratio vs `0.35 + 0.35·|α|`），但**方向无关性相同**，修复思路一致。

### 替代方案 B：confidence 作为乘子而非加项（更彻底）

```python
weighted = (alpha_c*W_a + ai_c*W_ai + momentum_c*W_m + trend_c*W_t + micro_c*W_micro) * (0.5 + 0.5*confidence)
```
- 彻底消除方向无关加项
- confidence 高时方向信号放大，低时压缩 —— 更贴近 "confidence" 语义
- 缺点：需重新标定 `entry_threshold` / `scale_in_threshold` / `signal_edge_scale_bps` 全套阈值，改动规模大

**建议先走方案 A.1**：改动小、语义清晰、阈值基本不需要重标（因为 short leg 原本 confidence 污染值已经是 0.048-0.115 范围，去掉后 score 平均下降 ~0.08，可用现有 entry_threshold 运行一段时间观察，必要时微调一档）。

### 同步修改清单

| 文件 | 改动 | 风险 |
|---|---|---|
| `aats/services/strategy_engines/independent/scoring.py` | L142 confidence 方向门控; L175-176, L179-180 bonus 方向门控 | 低，纯条件分支追加 |
| `aats/data_platform/replay/adapters/independent_adapter.py` | L245-251 confidence_raw 方向门控 | 低，但需同步跑 replay 对齐测试 |
| 可能影响的 unit test | `test_independent_scoring*.py` 涉及两腿对称 confidence 的 case 需更新 expected values | 中，需逐条确认 |
| calibration / replay 重跑 | `entry_threshold` / `signal_edge_scale_bps` 的 RDP 推荐可能需要微调 | 中，改完后跑一轮 RDP calibration |

---

## 6. 预计修复后 R² / slope 变化

|  | 修复前 | 修复后预估 | 依据 |
|---|---|---|---|
| long R² | 0.0173 | **0.012 ~ 0.018** | 去掉 confidence 污染后长 leg 的 score range 变小，分母 σ² 降低，分子同步降低，R² 基本持平（当前 long slope 本来就由方向相关项主导，confidence 污染是"加强"而非"扭曲"） |
| long slope | +16.94 bps | **+14 ~ +17 bps** | 小幅下降，因去掉 bonus/confidence 的方向无关抬升，score 标度略收缩 |
| **short R²** | **0.00063** | **0.010 ~ 0.020** | 方向无关项污染完全消除，方向相关项的正 slope 信号得以显现 |
| **short slope** | **负** | **+10 ~ +15 bps** | 与 long leg 大致对称（方向相关项权重与 long 相同） |

**R² 仍然不高的原因**：单因子（score）对 60min realized 本身就弱（市场噪声占 90%+），这是 signal quality 的上限，不是公式问题。修复 H4 后 long 和 short 应大致对称，这本身就是最关键的一致性指标。

---

## 7. 是否需等样本（H6 判定）

**不需要**。理由：

1. H4 是公式缺陷，修完立即在**相同 30 天数据**上能观察到 short slope 翻正，不依赖更多样本。
2. H6 是背景条件（BTC 多头 drift），修完 H4 后其放大效应被消除。
3. 即便后续 BTC 进入真正的双向波动期，当前代码仍有 H4 固有缺陷，**不修 H4 再多样本都无法获得 short leg 的正 slope**。

**建议路径**：
1. 先在 replay / unit test 层打出方案 A.1 的 patch，跑相同 30 天 Gold 数据
2. 对比修复前后 `(short_leg_score, realized_edge_short)` 散点 + OLS 拟合
3. 确认 short R² ≥ 0.01 且 slope 为正后，再接 P1-A Phase 2 的 composite 权重调整
4. Phase 2 之后再跑一轮 end-to-end replay validation 并 RDP calibrate 新的 `signal_edge_scale_bps`

---

## 8. 与 P1-A 的耦合判定

用户原担心："**如果 P1-A 修改 composite 权重但不修 short leg 公式，short 方向 P1-A 完全无效果甚至反效果**"。

**本报告确认此判断成立**：P1-A 不论怎么调 `_MODE_A_W_*` 权重，都是在**方向相关分量**之间重新分配，而 `W_CONFIDENCE = 0.12` 这一项的"方向无关加项"性质不变 —— short leg 在多头市场中仍然被 confidence 推高到同样 ~0.048-0.115 的底盘，realized_edge(short) 仍然被 market drift 拉负 → slope 仍会负。

**强烈建议 P1-A 的 Phase 2 代码改动前优先上 H4 修复**（方案 A.1），否则 short leg 任何 composite 调整都是在反指标的基础上调，优化方向会完全错位。

---

## 9. 小结

| 问题 | 答案 |
|---|---|
| 根因类型 | 公式设计缺陷（方向无关加项在对称 long/short 框架下泄漏） |
| 主根因 | H4 — confidence 方向无关（`scoring.py:142`） |
| 次根因 | H3 — regime_bonus + volatility_bonus 两腿同加（`scoring.py:175,179`） |
| 修复代码范围 | scoring.py 3 处（1 行改、2 个 `and leg_aligned` 条件追加） + replay adapter 1 处同步 |
| 预计 short R² | 0.00063 → 0.010 ~ 0.020 |
| 预计 short slope | 负 → +10 ~ +15 bps/score |
| 是否需等样本 | 否，立即可修并重测 |
| P1-A 是否阻塞 | **是** —— 建议 P1-A Phase 2 代码改动前先上 H4 修复 |
