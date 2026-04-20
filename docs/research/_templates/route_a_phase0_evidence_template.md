# 路线 A Phase 0 · Evidence 提案 · `<SIGNAL_NAME>`  @ `<HORIZON>`

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../../docs/project_positioning.md)。

> 本模板是 `docs/governance/alpha_evidence_gate.md` v0.1 §6.1 的 "必备 8 字段" 填写骨架。
> 路线 A = **microstructure directional alpha** (见 alpha_evidence_gate §8.1).
> Phase 0 = 第一轮研究, 每个 `(feature × horizon)` 组合**独立**过 gate, 不打包.

---

## 元数据 (提案头)

| 字段 | 值 |
|---|---|
| 提案 ID | `route-a-phase0-<feature>-<horizon>-<YYYYMMDD>` |
| 提案日期 | `<YYYY-MM-DD UTC>` |
| 提案人 | `<name>` |
| Scope: feature | `<OFI / TFI / queue_imbalance / top_of_book_depth_ratio / microprice_deviation / ...>` |
| Scope: horizon | `<5s / 30s / 5min / 15min>` |
| Scope: symbol | `<BTC-USDT-SWAP>` (初期建议只跑 BTC, 过 gate 后再加 ETH) |
| Scope: time range | `<train_start>/<test_start>` UTC, train `<N>` days, test `<N>` days |
| Scope: cost model | `<fee_resolver commit hash, slip/exec buffer>` (必须用当前冻结值) |
| 本次目标 | Go / Conditional revisit / Archive |

---

## §1 数据源

**表 + 时间范围 + 样本 N + 过滤条件**:

- Silver: `silver.market_<orderbook_metrics / trade_flow>_15m` (若 horizon > 15min)
- Bronze: `bronze.market_<orderbook_bbo / orderbook_books5 / trades>` (若 horizon < 15min)
- 时间范围: `<from_ts>` 到 `<to_ts>` UTC (**必须在观察窗结束后**, i.e. ≥ 2026-04-27 14:15 UTC 起)
- 总样本 N: `<count>`
- 过滤条件:
  - symbol='BTC-USDT-SWAP'
  - quality_flags **不含** `funding_no_data` / `partial_baseline` (视指标而定)
  - <其他>

**数据快照 commit hash**: `<aats_research schema snapshot ref, 或 Silver catch-up 运行日期>`

**Gap / missing 处理**: `<丢弃 / 插值 / ffill / 直接报错>`

---

## §2 特征定义

**Python 代码 OR 等价 SQL**, 可复现:

```python
# features.py 提交到 scripts/research/route_a_phase0/
def compute_<feature_name>(df):
    """
    定义: <严格文字描述 + 公式>
    单位: <bps / ratio / z-score>
    归一化: <none / z-score / minmax>
    窗口: <lookback bars / seconds>
    """
    ...
```

**特征统计表** (填在提案里):

| 统计 | train | test |
|---|---|---|
| mean | | |
| std | | |
| min / max | | |
| p1 / p99 | | |
| zero rate | | |
| NaN rate | | |

**特征稳定性 check**: mean/std 在 train 和 test 之间不应差 > 50%. 若差 → regime shift, scope 不成立。

---

## §3 模型定义

**先**尝试线性 (OLS / ridge); 过 gate 失败后才考虑非线性。

**默认超参**:
- 模型: `<OLS / Ridge(alpha=X) / Lasso / GBDT(depth=N, n=M)>`
- Target: `<realized_edge_<horizon>_bps>` 定义 (如 `(close_t+h - close_t) / close_t * 10000`)
- 训练方法: `<fit() on train / CV in train only>`

**若做了 hyperparameter sweep**: 明示 sweep 范围 + 结果表, **且必须在 train 内做**, test 永远 hold-out.

---

## §4 Train / Test 分割

**时间边界**:
- train: `[<train_start>, <train_end>)` UTC
- test:  `[<test_start>, <test_end>)` UTC

**分割理由**: `<why this boundary? e.g. "test_start 在 fed rate decision 之前, 避开 regime shift">`

**边界之前是否穿过已知 regime 切换**: `<yes/no + 证据>`

如过 regime 切换 → 调整边界或说明这是 intentional cross-regime test.

---

## §5 Cost Model (**不可改, 只能引用**)

```
fee_resolver commit hash  : <abc1234>
bounded_limit_ioc classify: taker (P1-B H2 frozen)
taker fee                 : <X bps>
maker fee                 : <Y bps>
slippage_buffer_bps       : <governance 当前值, 从 .env.derivatives.live 读>
execution_buffer_bps      : <governance 当前值>
safe_edge_bps             : <sum, 固定>
```

**若想"下调 cost 让 edge 变正"** → **直接 Archive**. 这是反模式 §7 #2.

---

## §6 四条硬指标实际数据

### 6.1 OOS (§3.1)

| 指标 | train | test | 结论 |
|---|---|---|---|
| IR (annualized) | | | |
| Sharpe | | | |
| Hit rate | | | |
| Max drawdown | | | |
| Sample N | | | |

**判据**:
- test IR / train IR ≥ 0.5 (或提案方定, 必须预注册)
- train 正 → test 正 (同号)
- test 内部无 ≥ 50% 样本时段 flat / 反向

**图**: `docs/research/route_a_phase0/<proposal_id>/oos_cumulative_returns.png`

**结论**: `[PASS / FAIL + reason]`

### 6.2 Cross-window (§3.2)

**切片**: test 集按时间切 **≥ 3** 个**不相邻**时间片。

| Slice | 时间范围 | IR | Hit rate | Max DD | Sample N |
|---|---|---|---|---|---|
| S1 | | | | | |
| S2 | | | | | |
| S3 | | | | | |
| (S4) | | | | | |

**判据**:
- 所有 slice IR 同号
- std(slice IR) ≤ 2 × mean(slice IR) (或提案方定)

**图**: slice 对比柱状图 + 时序 cumulative returns 分段染色

**结论**: `[PASS / FAIL + reason]`

### 6.3 Cost-adjusted (§3.3)

| | train | test |
|---|---|---|
| Gross edge (bps) | | |
| Fee | | |
| Slippage buffer | | |
| Execution buffer | | |
| **Net edge** | | |

**判据**:
- test 集 net_edge > 0 (零也算边缘过, 需说明 robustness)
- Sensitivity: fee 上调 20% 或 slip +0.5 bps, net edge 仍 > 0 ?

**结论**: `[PASS / FAIL + reason]`

### 6.4 Regime-slice (§3.4)

**切片维度** (至少 1, 建议 2):

Option 1: 波动率 × funding 方向 (2×2)

| | low_vol | high_vol |
|---|---|---|
| funding ≥ 0 | IR = `<>`, N = `<>` | IR = `<>`, N = `<>` |
| funding < 0 | IR = `<>`, N = `<>` | IR = `<>`, N = `<>` |

**判据**:
- 每 cell IR 同号 (不要求等量)
- 最狭窄 cell 样本 N ≥ 50 (否则样本太少无法断言)

**图**: heatmap

**结论**: `[PASS / FAIL + reason]`

---

## §7 加分项 (可选, 但加强说服力)

- [ ] Physical plausibility: `<经济机制解释, < 100 字>`
- [ ] Cross-symbol: 在 ETH-USDT-SWAP 上也同号 (Yes / No / 未测)
- [ ] Timeframe neighborhood: horizon × 0.67 和 × 1.5 也同号 (Yes / No / 未测)
- [ ] Replay audit: 在 Gold replay bars 上 / 或 tick replay 上跑无 look-ahead bias (Yes / No / 未测)
- [ ] Independent re-run: 非提案作者 fresh session 跑得一致 (Yes / No / N/A)

---

## §8 反模式 Red Flag Check (见 alpha_evidence_gate §7)

在提交前, 逐条自查:

- [ ] **动机反模式**: 本提案的动机是 "新证据 showing old assumption wrong", **不是** "让旧数据看起来 work"
- [ ] **Cost 造假**: 没自行 assume 低 fee / 低 slip; 用的是 fee_resolver + governance 当前值
- [ ] **Degenerate cross-window**: 3 个时间片不存在 "2 正 1 显著负 合起来似正" 的情况
- [ ] **Single-point win**: 不依赖 boundary case (fee 涨 0.5bps 就 fail 的不算)
- [ ] **Hyperparameter overfit**: test 集 IR 不是在 test 上 sweep 出来的, 是 genuine hold-out
- [ ] **Missing replay**: 若做了 §7 replay audit 为 Yes
- [ ] **Unfalsifiable**: 已预注册 "什么证据出现我放弃" 的 commitment
- [ ] **Rule change mid-flight**: 没在结果出来后改判据定义

任一条不过 → **直接 Archive**, 不进判定矩阵.

---

## §9 结论 + 提案

**判定**: 参考 alpha_evidence_gate §5 判定矩阵

- [ ] **Go**: 四条硬指标全过 + 反模式 check 全清
  - 下一步: `<具体下一步, e.g. "把 feature 落到 research notebook 标准化, 准备 phase 1 打包 (feature + horizon 组合≥N 独立过 gate)">`
  - 解冻参数 (若需): `<列出要解冻的 frozen 参数 + 为什么>`
- [ ] **Conditional revisit**: 三条过 + 一条 fail
  - 缺的是: `<哪条硬指标>`
  - 需要什么新证据才能重审: `<明确补数据 / 新观察周期 / etc>`
- [ ] **Archive**: ≤ 2 条过, 或反模式红旗
  - 归档路径: `docs/design/archived/route_a_<feature>_<horizon>_<YYYYMMDD>.md`
  - 归档理由: `<>`

---

## §10 回退预案 (仅 Go 时必填)

若本提案判 Go 并进入实施:

**实施后 24-48h 监控指标**:

| 指标 | 正常范围 | 触发 revert 阈值 |
|---|---|---|
| Realized net edge (test 复现) | > <X> bps | < 0 |
| Sample coverage | > 80% of train 分布 | < 50% |
| Drawdown | < <Y>% | > 2X |
| ... | | |

**revert 操作**: `<具体 commit 的 git revert 命令 / feature flag 关闭 / config 回改>`

**revert 决策人**: 用户 (最终 sign-off)

---

## §11 可复现性证据

- 代码 commit: `<hash>`
- 依赖: `<requirements.txt commit 或 poetry.lock>`
- 随机种子 (若有): `<seed value + 所有 stochastic 过程说明>`
- 数据 snapshot: `<aats_research schema dump ref>`
- 跑完整实验的 shell 命令:
  ```bash
  python scripts/research/route_a_phase0/<proposal_id>/run.py --config config.yaml
  ```

---

## §12 Cross-check

- [ ] 代码已 review (作者 / 非作者 / N/A 单人团队)
- [ ] fresh session 重跑结果一致 (数值 ±1% 可接受)
- [ ] 任何数值不一致已排查 (非随机性, 真实归因)

---

## §13 决策 Audit

- 提案时间: `<UTC>`
- 决策时间: `<UTC>`
- 决策人: `<>`
- 决策结果: Go / Conditional revisit / Archive
- 理由摘要 (<100 字): `<>`

实施 commit message 格式 (解冻参数时):
```
[evidence: docs/research/route_a_phase0/<proposal_id>/<filename>.md]

<正文>
```

---

## §14 附录

### A. 原始数据查询 SQL
```sql
-- ...
```

### B. 图表源数据 CSV 路径
```
docs/research/route_a_phase0/<proposal_id>/
  ├── oos_cumulative_returns.png
  ├── oos_data.csv
  ├── cross_window_slices.csv
  ├── regime_heatmap.csv
  └── ...
```

### C. 相关 governance doc 引用
- `docs/governance/alpha_evidence_gate.md` v0.1
- `docs/governance/frozen_parameters.md` (若涉及解冻)
- `docs/governance/runtime_trading_mode_semantics.md` (不涉及, runtime mode 永冻)

---

## §15 签署

- 起草模板: Claude Opus 4.7 · 2026-04-20
- 模板所属: `alpha_evidence_gate.md` v0.1 §8.1 路线 A phase 0 配套
- 填写人每次: `<proposer name + signature>`
- 审批人: `<user>`
- 模板版本: v0.1 (首次使用后按实际经验反馈到 v0.2)
