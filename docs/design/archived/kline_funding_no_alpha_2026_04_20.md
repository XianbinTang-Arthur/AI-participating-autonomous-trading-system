# Kline + Funding 衍生特征 NO-GO 归档 (2026-04-20)

**状态**: ❌ 永久归档 — 证伪
**证伪依据**: 3 份独立研究报告
**下一步替代**: 继续 P1-D Microstructure（orderbook/trade flow/OI delta 等**非 kline-funding 派生**的特征）

---

## 1. 归档范围

本归档覆盖**所有基于 15m kline OHLC + funding rate 的派生特征**在 BTC-USDT-SWAP 15m horizon 上的**directional alpha 探索**:

- OHLC 派生: close-open / close-close[-1] / EMA3 slope / breakout / momentum (P1-A CHASE)
- confidence-gated direction-relative scoring (H4)
- 短腿 FADE score（P1-C）
- **basis (swap - spot) / basis_z** (今次新增)
- **funding_rate / funding_anomaly / funding_z** (今次新增)
- **minutes_to_next_funding** (今次新增，见保留项 §5)

---

## 2. 证伪证据链

### 2.1 P1-A CHASE 证伪 (2026-04-19)

**报告**: `docs/review/fast_impulse_candidate_selection_2026_04_19.md`

5 个 fast_impulse 候选 + baseline ROC(5), 在 33 天 BTC-USDT-SWAP 15m 上:
- **long + short 两端 test R² 全为负**
- slope 全为负（均值回归，不是动量）
- 急拉场景 win rate 0.378-0.508（coin flip）

### 2.2 P1-C FADE 证伪 (2026-04-19)

**报告**: `docs/research/fade_strategy_investigation_proposal_2026_04_19.md`

H4 修复后的 raw-level 回归（n=8194）:
- short 15m FADE slope +0.50, R² = 0.00000
- 扣 6 bps 成本后 mean_net 全为负
- 仅 q95 n=10-12 擦过 0 轴（无统计显著性，Bonferroni 矫正 p=0.24）

### 2.3 Kline + Funding 预览回归 (2026-04-20)

**报告**: `docs/research/p1d_preview_regression_funding_basis_2026_04_20.md`

6 个特征 × 4 个 horizon 回归，全部未达标:

| 特征 | 15m test R² | 判定 |
|---|---|---|
| basis | -0.0021 | 无 edge |
| basis_z | -0.0010 | 无 edge |
| funding_rate | -0.0028 | 无 edge |
| funding_anomaly | 类似量级 | 无 edge |
| funding_z | -0.0008 | 无 edge |
| minutes_to_next_funding | -0.0007 | 无 edge (但见 §5) |

扣 6 bps 成本后 q80/q90 mean_net **全在 -5 到 -7 bps**，CHASE 和 FADE 双向都亏。

---

## 3. 为什么这些 kline+funding 单独特征都不 work

1. **BTC 15m 本质是均值回归 + noise**（P1-A 独立证据 + fast_impulse 独立佐证）
2. **OHLC bar 把 bar 内 microstructure 信号平均掉**，保留的是"粗粒度" direction 信号
3. **Funding 是 8h 粒度事件**，15m 尺度上对应稀疏信号，不是 persistent
4. **Basis 是期限结构均值回归**，在 4h-日线有 slope 但被 15m 短期噪声淹没
5. **Cost 阈值硬门槛**: 6 bps cost 要求 feature R² ≥ 0.01 才能 gross > net. 上述所有特征 R² < 0.005

---

## 4. 逻辑总结

> **15m BTC 上，基于 OHLC + funding 公开公共数据的任何线性特征，扣 6 bps 成本后都不赚钱。**

这是三份独立报告用不同方法交叉验证得出的**结构性结论**，不是单一窗口的偶然。

---

## 5. **保留项** — `minutes_to_next_funding ≤ 60` bucket 作为 event-window 候选

预览回归发现一个值得 **independent 立项** 的现象:

- 全样本 `minutes_to_next_funding` 回归 R² = -0.0007（无）
- 但 **bucket `≤ 60 min` 的 subset**: Pearson = **-0.19** (绝对值最大)
- 不够统计显著（n 太少, test R²=-0.044 过拟合）
- **但信号方向明确**: 接近 funding settlement 时 price 和 funding 方向反向

**不归档**: 这是 event-window-specific 信号，不属于"kline+funding 单独持续特征"否决范围。
**立项**: 见 `docs/design/p1d_phase2b_funding_window_event_research_proposal_2026_04_20.md`

---

## 6. 遗产

保留工件:
1. **预览回归脚本** (`scripts/research/p1d_preview_regression_funding_basis.py`): 可重用的回归/cross-window/bucket slice 框架
2. **fast_impulse 选型脚本** (`scripts/calibration/fast_impulse_selection_regression.py`): 公式对比回归骨架
3. **FADE 调研脚本** (`scripts/research/fade_strategy_investigation.py`): 反向假设验证 pattern

**不保留的假设**:
- ❌ "换个 OHLC 公式能在 15m 找到 edge"
- ❌ "funding/basis 作为 persistent 线性特征能 exploit"
- ❌ "在 kline/funding 公开数据里还有未被试过的线性 alpha"

---

## 7. 检查清单（避免重犯）

任何新提出的"基于 OHLC+funding 的 15m BTC 线性特征"**都必须**先过这 3 道测试才能立项:

- [ ] **新** — 不在上述 5-6 个已证伪特征族内
- [ ] **预期 R² ≥ 0.01** 有文献 / 理论支撑
- [ ] **不受本 meta-conclusion 覆盖**（即：不是 OHLC+funding 单独衍生的 persistent 信号）

符合全部 3 条才能 spawn 验证。

---

## 8. 下一阶段主线

**P1-D Microstructure** 继续推进：
- ✅ Stage 1-4 已上线（WS 实时采集中）
- 🔄 **Stage 5（今次新增）**：OKX REST 批量下 OI/mark/LS 历史数据 + OI delta 回归（详见 `docs/design/p1d_okx_historical_backfill_plan_2026_04_20.md`）
- ⏳ Phase 2A W3-W4: 3 核心特征真实数据回归 + W4 hard gate
- ⏳ Phase 2B: event-window 子策略（保留项 §5） + 其他 explorations

---

## 9. 签署

- 归档决策: 2026-04-20 (用户 implicit 批准通过推进 P1-D Stage 5)
- 归档位置: `docs/design/archived/`
- 相关活跃文档:
  - `docs/research/p1d_preview_regression_funding_basis_2026_04_20.md` (今次证据)
  - `docs/design/p1d_phase2b_funding_window_event_research_proposal_2026_04_20.md` (保留项立项)
  - `docs/design/p1d_okx_historical_backfill_plan_2026_04_20.md` (Stage 5 主线)
