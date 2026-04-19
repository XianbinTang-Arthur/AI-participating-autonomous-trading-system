# P1-A 双通道 CHASE 方案失败路径归档 (2026-04-19)

**状态**: ❌ 永久归档 — 证伪
**证伪方**: 两份独立任务报告 + H4 修复后的经验验证
**替代方向**: P1-C FADE 策略调研（已立项）

---

## 1. 背景 — 为什么启动 P1-A

2026-04-19 用户观察到 BTC-USDT-SWAP 从 75,300 反弹到 76,200（+1.2% 15m-45m 窗口），
AATS 系统全程**零下单**。用户提问："为什么这么好的趋势系统一单都不下吗？"

诊断显示多个门槛串联阻断：
- DecisionEngine target_gate confidence_min=0.55 阻断
- Allocator independent entry_threshold=0.30 阻断  
- `expected_net_edge = signal_edge - cost` 在成本 ≈ 6 bps 下**结构性为负**

由此提出 **P1-A 设想**:
> 重建 trend + momentum 双通道特征，让系统能更敏感地识别 1.2% 级别的急拉/急跌行情

核心假设（CHASE 前提）:
- 现有 `momentum_score = ROC(5)` 太慢
- 换更快的 fast_impulse 公式（如 `(close-open)/open`、EMA3 slope、breakout）能抓住 15m 急拉
- 若 fast_impulse 和 trend_alpha 各有独立 edge，双通道权重优化可放大系统信号

预期交付: 在 Mode A 下提高 short leg 表现，让系统能对称追涨追跌。

---

## 2. 证伪过程

### 2.1 报告一：fast_impulse 候选选型回归（`docs/review/fast_impulse_candidate_selection_2026_04_19.md`）

在 33 天 BTC-USDT-SWAP 15m Gold 数据上，测试 5 个 fast_impulse 候选 + baseline ROC(5) 对下一根 15m realized_return 的预测力：

| Formula | long R²(test) | short R²(test) | 对称性 |
|---|---|---|---|
| f1 (close-open)/open | **-0.0164** | **-0.0210** | ✓ 同号 |
| f2 (close-close_{-1}) | **-0.0164** | **-0.0213** | ✓ 同号 |
| f3 EMA3.slope | **-0.0224** | **-0.0158** | ✓ 同号 |
| f4 breakout dist | **-0.0038** | **-0.0077** | ✓ 同号 |
| f5 accel | **-0.0127** | **-0.0228** | ✓ 同号 |
| ROC(5) baseline | **-0.0041** | **-0.0113** | ✓ 同号 |

**核心结论**:
1. **全部 6 个候选，long + short 两端，test R² 全为负，slope 全为负**
2. 急拉场景 (|ROC(5)|<0.002 + |X|≥80%ile) win rate 0.378-0.508 — 统计上无 edge
3. **15min horizon 本质是均值回归，不是动量**；short 方向持续反动量

### 2.2 报告二：Short leg asymmetry 根因（`docs/review/short_leg_asymmetry_root_cause_2026_04_19.md`）

独立分析 baseline.py 的 confidence 公式, 确认：
- `confidence = 0.35 + 0.35·|α| + ...` 方向无关
- 在对称 long/short 评分框架中，weight 0.12 被两腿共享
- 在多头 drift 中污染 short leg 的 score-realized 相关性

### 2.3 H4 修复后的经验验证（`docs/review/h4_fix_validation_2026_04_19.md`）

30 天 BTC-USDT-SWAP bar-level 聚合 (n=234), 对比 H4 修复前后：

| 指标 | OLD | NEW |
|---|---|---|
| long 15m R² | 0.00984 | **0.01377** (+40%) |
| long 15m slope | +14.91 | +13.96 |
| short 15m R² | 0.00015 | **0.00329** (**×22**) |
| short 15m slope | -3.45 | **-14.55** (负向放大) |
| short 30m R² | 0.00193 | **0.00820** (**×4.2**) |
| short 30m slope | -17.67 | **-32.81** (负向放大) |

H4 修复**正确地隔离了短腿信号**（R² 放大 22×），但信号的**真实方向是 FADE 而不是 CHASE**。

---

## 3. 为什么 CHASE 假设不成立

### 3.1 市场结构证据

两份独立报告（fast_impulse 选型 + H4 后经验验证）从不同数据、不同方法论同时得到同一结论：

- **BTC 15m 存在显著的均值回归**
- **short 端在所有 horizon 都是反动量**（slope 全部为负）
- **仅 long 端、且 horizon ≥ 60m 才表现出 CHASE 特性**

### 3.2 可能的原因

- BTC 天然长期向上 drift（spot 持仓者居多）→ long 信号=CHASE 合理
- Short signal 往往在局部顶部触发，随后反弹概率高 → FADE 更贴合结构
- 15-30m 尺度上，大单冲击后的反转占主导，而不是动量延续

### 3.3 P1-A 继续的代价

若强行在 CHASE 框架下调整双通道权重:
- **对 long 端**: 最多小幅改善（已接近最优）
- **对 short 端**: 任何权重调整都是在**负 slope 的反指标**上做优化 — **越调越错**
- **对系统整体**: 可能偶尔赌中 1-2 次 CHASE 成功，长期期望负（均值回归占主导）

---

## 4. 放弃原因总结

| 维度 | 证据 |
|---|---|
| 统计显著性 | 33 天 3000+ 样本 + 30 天 234 bar 聚合，R² 全部为负，效应稳定 |
| 方法论独立性 | 两份报告用完全不同方法（公式选型回归 vs confidence 污染根因）得出同一结论 |
| 经验验证 | H4 修复后短腿 R² 放大 22×，揭示的是**FADE 信号**，不是可追的 CHASE |
| 市场结构 | BTC 长期向上 drift + 15-30m 反转 — 天然不支持对称 CHASE 框架 |

---

## 5. 遗产与借鉴

P1-A 过程中产出的有用工件（**保留**）:

1. **H4 方向门控修复** — 不论 CHASE 或 FADE，都是正确的评分对称性修复
2. **fast_impulse 选型回归脚本** (`scripts/calibration/fast_impulse_selection_regression.py`) — 可复用于其他公式评估
3. **H4 验证脚本** (`scripts/calibration/validate_h4_short_leg_fix.py`) — OLD vs NEW 算法对比框架
4. **baseline-level vs bar-level 方法学对比** — 未来回归分析需谨慎区分样本粒度

P1-A 过程中的假设**不保留**:
- ❌ "fast_impulse 切换能让系统追 1.2% 急拉"
- ❌ "双通道 momentum + trend 可在 15m 产生稳定 CHASE edge"
- ❌ "短腿 slope 应该翻正（对称 CHASE）"

---

## 6. 替代路径 — P1-C FADE 策略调研

H4 修复后的数据揭示 short 端是 FADE 信号。下一阶段主线改为：

1. **P1-C 立项** — 独立 spawn 调研"FADE 策略可执行性"
2. 用已清洗过的 baseline（post-H4）跑回归：`y = realized_bps, X = -short_score`
3. 若 R² ≥ 0.01 且 slope 正，证明 FADE 有 exploitable edge
4. 再评估：是作为新策略上线，还是作为 regime filter 嵌入现有独立家族

详见 `docs/research/fade_strategy_investigation_proposal_2026_04_19.md`（与 P1-C spawn 任务同步产出）。

---

## 7. 检查清单 — 如何避免重犯

- [ ] 新策略假设**必须**先过统计检验（R²、slope、win rate）再动代码
- [ ] 任何"某次大行情应该能抓到"的 anecdote **不构成** pattern 证据
- [ ] 多 horizon 扫描是必备步骤 — 15m/30m/1h/2h 至少四档
- [ ] long/short 对称性假设必须**经验验证**，不能直接 assume
- [ ] 多样本窗口（震荡 vs 趋势）至少覆盖一次，避免过拟合
- [ ] 双方法论交叉验证（如 fast_impulse 独立 + H4 后 empirical）

---

## 8. 签署

- 归档决策：用户批准（2026-04-19）
- 证伪依据：两份独立报告 + 1 份 H4 验收
- 归档位置：`docs/design/archived/`
- 相关活跃文档：`docs/review/h4_fix_validation_2026_04_19.md`（证据）、`docs/research/fade_strategy_investigation_proposal_2026_04_19.md`（替代路径）
