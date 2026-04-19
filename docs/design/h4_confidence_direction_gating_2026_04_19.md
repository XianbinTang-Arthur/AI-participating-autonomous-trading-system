# H4 方向门控修复设计 (Independent scoring surgical fix)

**日期**: 2026-04-19
**范围**: `aats/services/strategy_engines/independent/scoring.py` + `aats/data_platform/replay/adapters/independent_adapter.py` + 锁定测试
**依据**: `docs/review/short_leg_asymmetry_root_cause_2026_04_19.md` §5 方案 A.1
**审批状态**: 等待批准

---

## 1. 背景与动机

P1-B step 1 standalone-edge calibration 输出报告发现：

| Leg | slope (bps/score) | R² |
|---|---|---|
| long | +16.94 | 0.0173 |
| **short** | **负** | **0.00063** |

短腿的 score 与 realized_edge 几乎不相关，而且趋势**反向**。若不修复此缺陷：

- P1-A 的任何 composite 权重调整都在错误的 baseline 上优化（反指标校准）
- 系统永远只能在多头市场里跑，对称 short 永远无法产生稳定 edge
- signal_edge_scale_bps 的 RDP 推荐对 short 永远偏低

`short_leg_asymmetry_root_cause_2026_04_19.md` 的根因定位：`confidence`（scoring.py:142）作为方向无关加项，权重 0.12，随 `|alpha_score|` 单调上升 → 在多头 drift 中，`short_score` 被 confidence 系统性抬高，但 `realized_edge(short)` 随 α↑ 下降，形成负 slope。

次根因：`regime_bonus`（scoring.py:175）和 `volatility_bonus`（scoring.py:179）两腿同加，稀释短腿的方向信号。

---

## 2. 修复方案

**核心思想**：方向无关加项只在该 leg 与 `baseline.direction_bias` 对齐时计入。

### 2.1 scoring.py 改动（3 处）

**L142 — confidence 方向门控**:
```python
# 旧
confidence = _clamp(float(baseline.confidence), 0.0, 1.0)

# 新
leg_aligned = baseline.direction_bias == leg
confidence = _clamp(
    float(baseline.confidence) if leg_aligned else 0.0,
    0.0, 1.0,
)
```

**L175-176 — regime_bonus 方向门控**:
```python
# 旧
if baseline.regime in {"range", "uncertain"}:
    score += 0.04

# 新
if baseline.regime in {"range", "uncertain"} and leg_aligned:
    score += 0.04
```

**L179-180 — volatility_bonus 方向门控**:
```python
# 旧
if baseline.volatility_state == "high":
    score += 0.03

# 新
if baseline.volatility_state == "high" and leg_aligned:
    score += 0.03
```

`direction_bias_bonus`（L177-178 `+0.06 if direction_bias == leg`）**保持不变** —— 它本身已经是方向对齐项，逻辑自然正确。

### 2.2 independent_adapter.py 改动（1 处）

Replay adapter 的 `confidence_raw` 来自 volume_ratio（非 `baseline.confidence`），但同样方向无关。

**L215-251 — confidence 方向门控**（在 `_compute_book_score` 末段加入）:

```python
# 旧
confidence_raw = min(_sigmoid((vol_ratio - 1.0) * 200), 1.0)
score = (
    _W_ALPHA * alpha_raw
    + _W_MOMENTUM * momentum_raw
    + _W_TREND * trend_raw
    + _W_MICRO * micro_raw
    + _W_CONFIDENCE * confidence_raw
)

# 新
confidence_raw = min(_sigmoid((vol_ratio - 1.0) * 200), 1.0)
# H4 对齐生产：confidence 只在该 bar 的 trend 方向与 leg 一致时计入
# bar_return 和 momentum_return 在 leg=="short" 时已被翻转（line 217/228），
# 在两腿自身坐标系下均以 "正 = 对齐方向" 判定
leg_aligned_trend = trend_dir > 0  # trend_dir 在短腿时已翻转
if not leg_aligned_trend:
    confidence_raw = 0.0
score = (
    _W_ALPHA * alpha_raw
    + _W_MOMENTUM * momentum_raw
    + _W_TREND * trend_raw
    + _W_MICRO * micro_raw
    + _W_CONFIDENCE * confidence_raw
)
```

**说明**：replay 没有 `direction_bias` 语义（没有 baseline 对象），只能用"本 bar 的 trend 方向"代理，取 `trend_dir > 0`（翻转后坐标系下为正表对齐）作为门控信号。与生产端的门控机制不完全等价但同向，足以让 replay 的 short leg score distribution 与生产对齐，避免生产 ↔ 回测 drift。

---

## 3. 锁定测试（新增）

在 `tests/unit/test_independent_scoring_ai_fallback.py` 新增一个 `TestH4DirectionGating` 类，覆盖：

1. `test_long_leg_direction_bias_short_zeros_confidence_and_bonuses`
   - `direction_bias="short", leg="long"` → leg_aligned=False
   - 断言 score 中 confidence contribution = 0，regime_bonus/volatility_bonus = 0
2. `test_short_leg_direction_bias_long_zeros_confidence_and_bonuses`
   - `direction_bias="long", leg="short"`（打开 short_bias）→ leg_aligned=False
   - 断言同上
3. `test_aligned_leg_still_gets_full_confidence_and_bonuses`
   - `direction_bias="long", leg="long"`, regime="range", vol="high"
   - 断言 confidence + 0.04 + 0.06 + 0.03 全部加上
4. `test_misaligned_short_leg_no_drift_across_modes`
   - 三档 mode × `direction_bias="long", leg="short"` → 方向无关加项全部为 0，score 只剩方向相关项
5. `test_replay_adapter_short_leg_no_confidence_when_trend_up` (新增到 `tests/unit/test_independent_replay_adapter.py` 或同一文件)
   - Mock 一组 bars，trend_dir 为正（多头）时 short leg confidence_raw 应为 0

---

## 4. 现有测试影响分析

已扫描 `tests/unit/test_independent_scoring.py` 和 `test_independent_scoring_ai_fallback.py`：

| 测试 | direction_bias | leg | 是否受影响 |
|---|---|---|---|
| `test_compute_raw_book_score_matches_legacy_wrapper_and_fixture` | long | long | 否（leg_aligned=True） |
| `test_mode_a_baseline_only_uses_fallback_weights` | long | long | 否 |
| `test_mode_c_matches_original_formula` | long | long | 否 |
| `test_three_modes_produce_different_scores` | long | long | 否 |
| `test_mode_b_ai_assisted_uses_low_weight_formula` | long | long | 否 |
| `test_baseline_only_strong_signals_score_above_entry_threshold` | long | long, regime=trend, vol=high | 否（aligned，vol_bonus 依旧加） |
| `test_short_bias_disabled_returns_zero_regardless_of_mode` | short | short | 否（早退路径不变） |
| `test_short_mode_a_uses_fallback_weights` | short | short | 否 |
| `test_short_three_modes_produce_different_scores` | short | short | 否 |
| `test_short_signal_confirmation_count_three_tier` | short | short | 否（confirmation 与 bonus 无关） |

**结论**：所有现有测试都是"aligned"场景（leg == direction_bias），修复不会破坏任何既有断言。新增 5 个 misaligned 场景覆盖即可。

---

## 5. 预期指标变化（引自 short_leg §6）

| | 修复前 | 修复后预估 |
|---|---|---|
| long R² | 0.0173 | 0.012 ~ 0.018 |
| long slope | +16.94 | +14 ~ +17 |
| **short R²** | **0.00063** | **0.010 ~ 0.020** |
| **short slope** | **负** | **+10 ~ +15** |

short R² 提升 ~20 倍，slope 由负翻正至与 long 对称量级。long 小幅下降属于正常（去掉了~0.048 的 confidence 底盘，score 标度收缩）。

---

## 6. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| entry_threshold 需要重新标定 | 短腿 score 下降 ~0.08，可能导致入场数↑↓ | 先 replay 重跑观察分布；必要时在 RDP calibration 中调低 entry_threshold 0.05 |
| RDP signal_edge_scale_bps 推荐值失效 | scale_bps 是基于旧 score 分布校准的 | 先保持现有 scale=20 运行 3-5 天观察 PnL；RDP 下一轮 calibration 自动收敛到新分布 |
| replay ↔ 生产 drift | 两侧 leg_aligned 定义不完全等价 | 跑 end-to-end replay validation（见 §8），对比 long/short score 分布 |
| direction_bias 未被 baseline 输出 | 理论上可能 None/flat | 已核对 `BaselineAssessment.direction_bias: Literal["long", "short", "flat"]`，flat 时两腿 `leg_aligned=False`，confidence/bonus 都不加 —— 这是**期望行为**（flat regime 不应给任一腿加分） |

---

## 7. 回退方案

若修复后观察到：
- short R² 没有翻正（说明除 H4 外还有其他隐藏缺陷）
- 或 live 上 fill 数量断崖式下跌（entry_threshold 问题）

直接 git revert 这 3 个 commit 即可（原文件已备份到 `docs/design/backups/2026_04_19_h4/`）。替代路径：把 H4 修复改为"加权折扣"而非"硬归零"（`confidence *= 0.5 if not leg_aligned`）作为 softer fix。

---

## 8. 验收标准（顺序执行）

1. **单元测试全绿**：
   ```
   .venv\Scripts\python.exe -m pytest tests/unit/test_independent_scoring.py tests/unit/test_independent_scoring_ai_fallback.py -x -q
   ```
2. **Mode A 权重 Σ = 1 锁定测试仍通过**（`TestCompositeWeightsSum`, `TestCompositeModeAReplaySync`）
3. **Replay 回测 33 天 BTC-USDT-SWAP**：重跑 standalone-edge calibration，产出新的 long/short R² + slope 报告
4. **验收门槛**（硬性）：
   - short R² ≥ 0.01
   - short slope > 0
   - long R² ≥ 0.012（不严重退化）
   - long slope ≥ +12
5. **条件不满足时**：写 followup 报告分析，按 §7 回退或改 softer fix

---

## 9. 实施顺序

1. 备份 `scoring.py` 和 `independent_adapter.py` 到 `docs/design/backups/2026_04_19_h4/`
2. 改 `scoring.py`（3 处）
3. 改 `independent_adapter.py`（1 处）
4. 新增 5 个锁定测试
5. 跑单元测试
6. 跑 replay 重校准并对比 R² / slope
7. 落地 commit（语义化前缀 `fix:`）

---

## 10. 与后续工作的关系

**不解耦**：此修复是 P1-A Phase 2 的强前置。  
**解耦**：event_store retention（Path B）、cost audit 线上对账（Path C）可并行独立推进。

修复上线并验收后，才能进入下一阶段决策（FADE 调研 vs microstructure 扩展）。
