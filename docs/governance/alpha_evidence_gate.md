# AATS Alpha Evidence Gate — v0.1 (2026-04-20)

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。

> **Governance 纪律**: 任何"继续投入资源研究某条 alpha 路线"或"解冻 `docs/governance/frozen_parameters.md` 中参数"的提案, **必须**先通过本 gate.

---

## 1. 本文件的定位

### 1.1 是什么

- **研究决策协议**: 在"继续深挖"和"归档"之间做**二元**判定
- **Minimum bar**: 通不过 = 不继续; 通过 ≠ "这条路线一定盈利", 只意味着值得继续投资源
- **反反模式机制**: 防止"为了让旧 signal 看起来能用而调参"进入代码层

### 1.2 不是什么

- 不是 backtest 实现标准 (具体算法每路线自定)
- 不是绩效目标 (本 v0.1 不设 Sharpe / IR 最低值)
- 不是全自动 CI gate (必须人类 review + decision audit)
- 不是永久死刑 (归档后有"方法论级突破"可重申请)

### 1.3 为什么现在需要 (背景)

2026-04-19 ~ 2026-04-20 战略 directive 明确: **停止在错误方向上试错**。

摘自 `frozen_parameters.md` §1 — 过去 30 小时出现过或差点出现的反模式:

1. 1.2% 阳线没下单 → 降 `entry_threshold` 让系统能下单
2. Short slope 负 → 改 short scoring 权重强行翻正
3. OI delta R² 负 → 换更细 horizon / 不同 smoothing 让 R² 变正
4. 成本太高 → 降 `max_acceptable_cost_bps` 假装 edge 够用

**核心识别特征**: 改参数 / 引入新 feature 的**动机**是"让当前数据看起来满足门槛", 而不是"客观证据显示门槛定错或原假设错了".

本 gate 的作用: 在这种冲动即将成为代码/配置改动之前, 强制一次 structural check.

---

## 2. 核心原则 (先说边界)

1. **Evidence 驱动, 不是 hope 驱动**. 提案必须基于**新证据**, 不是基于"旧证据看起来还有希望".
2. **Evidence 的 scope 必须明示**: specific signal × specific timeframe × specific cost model × specific regime. 通过 = 仅在该 scope 内通过, 不默许泛化.
3. **Minimum bar, not target**. 过 gate 不代表该路线值得上实盘, 只代表**值得继续花时间研究**.
4. **人类最终拍板**. 本 gate 是辅助 think framework, 不自动放行.
5. **拒绝 sunk-cost 驱动**. 已经在某路线投入了 X 人天, 不构成"继续"的理由. 唯一合理的理由是 evidence.

---

## 3. 四条硬指标 (全部必须过)

每条都是**结构性 properties**, 不是单点 IR 阈值. 具体数值阈值由提案方在文档里明示 + 用户批准 (v0.1 不预设数值, 让第一个具体应用倒推出合理起点).

### 3.1 Out-of-sample stability (OOS)

**判据**:
- 必须做 **时间顺序** train / test split (不是随机 KFold, 不是 shuffled split)
- Test 集 IR 同号于 train 集 (train 正 test 负 = 失败)
- Test 集 IR 不能显著劣于 train (提案方定义"显著"阈值, 常见: test IR ≥ train IR × 50%)
- Test 集内部没有 ≥ 50% 样本时段 flat 或反向

**失败模式**:
- Hyperparameter 在 train 上 sweep 到最优, test 未 hold-out → overfit
- train 和 test 边界穿过 regime 切换 → train 是一种 market, test 是另一种, 看起来劣化其实是结构变化

**建议提供证据**:
- Train/test IR 表格 + 时序图
- 边界时间点的说明: "我在 2024-12-31T23:59:00Z 切, 为什么不在 2024-09-01?"

### 3.2 Cross-window stability

**判据**:
- Test 集内部按时间切 **≥ 3 个不相邻** 时间片 (例如 Q1 / Q3 / Q4)
- 每片 IR **同号**
- 每片的统计量 (IR / hit rate / max drawdown) 标准差不应超过均值的某倍 (提案方定 具体 N, 常见: σ ≤ 2 × μ)

**失败模式 — degenerate**: 3 个时间片, 2 片 IR 正 1 片显著负, 平均下来正 → 看起来过了 OOS, 但 cross-window 揭示结构性不稳定

**典型反例** (15m OHLC 被归档的原因): alpha 在某段时间存在, 在另一段时间消失, 合起来统计看整体似有, 实际是 regime-sensitive fragment

**建议提供证据**:
- 每 slice 的 IR + 回撤 + hit rate
- 一张时序 cumulative returns 图, 分段用不同颜色

### 3.3 Cost-adjusted net edge

**判据**:
- Fee model 用**当前** `aats/services/fee_resolver.py` (不允许自行 assume 更低 fee)
- `bounded_limit_ioc → taker` 分类保持不变 (见 `frozen_parameters.md` §2.3 H2 修复)
- Slippage buffer + execution buffer 使用 governance 当前值, 不下调
- `realized_edge − (fee + slip + exec)` 在 test 集上 > 0 (零也算边缘过, 建议提案方说明 robustness)

**失败模式**:
- 为了让 net edge 变正, 自行 assume "我下单会用更好的 fee tier" → **直接拒**
- 为了让 net edge 变正, 下调 `max_acceptable_cost_bps` → **直接拒** (反模式 #4)
- Fee 用的是 historical 某时段档位, 但当前档位更高 → **拒**, 必须用当前

**建议提供证据**:
- 成本明细表: 单笔 fill 的 fee / slip / exec buffer 分解
- 对比: 若 fee 再上调 20% / slip 再加 0.5 bps, net edge 是否仍 > 0 (sensitivity)

### 3.4 Regime-slice stability

**判据**:
- 按 **≥ 1 个** regime 维度切片, 至少 2 个 bucket/维
- 每个 bucket 内 IR **同号** (不要求等量, 只要求不反号)
- 推荐维度:
  - 波动率 (realized vol 中位数切 2 bucket)
  - Funding rate 方向 (funding > 0 / < 0)
  - Trend 方向 (长期 MA slope up / down)
  - Session (亚洲时段 / 欧美时段)

**失败模式 — narrow scope**:
- 只在"funding > 0 × upper trend × 高波"一个 cell 里有 alpha → 可遇不可求, scope 太窄
- Alpha 在牛市有, 熊市反向 → 本质是 regime-contingent, 不能直接跑实盘

**建议提供证据**:
- 2 × 2 或 2 × 3 的 regime × performance heatmap
- 最狭窄 cell 的样本数 N (样本太少 < 50 可能是 noise)

---

## 4. 加分项 (不 required, 但加强说服力)

| 项 | 说明 |
|---|---|
| **Physical plausibility** | 有 OKX 微观结构 / 行为金融 / 套利机制解释, 不是纯统计 artifact |
| **Cross-symbol stability** | BTC 和 ETH 上同号 (单 symbol evidence 也可接受, 但双 symbol 说服力更强) |
| **Timeframe neighborhood** | 15m 周围的 10m / 20m 也同号 (排除 horizon 选择 artifact) |
| **Replay audit** | 在 Gold replay bars 上独立跑一遍, 验证无 look-ahead bias |
| **Independent re-run** | 非提案作者在 fresh session 跑脚本得到相同数值 |

---

## 5. 判定矩阵

| OOS (§3.1) | Cross-window (§3.2) | Cost-adjusted (§3.3) | Regime-slice (§3.4) | 结论 |
|:---:|:---:|:---:|:---:|---|
| ✓ | ✓ | ✓ | ✓ | **Go** — 值得进下一阶段 (可实施代码 / 可解冻 frozen 参数) |
| ✓✓✓ 只差 1 条 | | | | **Conditional revisit** — 记录缺的那条 + 需要什么新证据才能重审 |
| ≤ 2 ✓ | | | | **Archive** — 归到 `docs/design/archived/`, 本路线暂停 |

**Archive** 语义:
- 不等于**证伪**, 等于**当前证据不足以继续**
- 不等于**永久死刑**, 但要"方法论级突破"才能重申请 (新 horizon 层 / 非线性模型 / 新特征列 / 新数据源)
- 重申请需**全量重过 gate**, 不是 "补一条之前缺的"

---

## 6. 过程纪律

### 6.1 Evidence 文档必备字段

一份提案 (通常放 `docs/research/<topic>_<date>.md`) 必须含:

1. **数据源**: 表名 / 时间范围 / 样本 N / 过滤条件
2. **特征定义**: Python 代码或 SQL, 可 reproducible
3. **模型定义**: 线性 / GBDT / NN, 默认 hyperparam (若有 sweep 明示 sweep 范围 + 结果)
4. **Train / test 分割规则**: 边界时间明确到秒, 说明选 boundary 的理由
5. **Cost model 用的具体 fee / buffer 数值**: 引用 fee_resolver commit hash
6. **4 条硬指标的实际数据**: 表格 + 图
7. **结论 + 提案**: Go ✓ / Conditional revisit / Archive
8. **回退预案 (仅 Go 时必填)**: 若决策是 Go, 落地后 24-48h 内什么数据变化会触发 revert

### 6.2 可复现性

- 代码 commit 到 `scripts/research/` (或等价路径)
- 依赖在 `requirements.txt` 或 `pyproject.toml` 声明, 不允许"我本地装了某包"
- 随机种子 (如有) 显式 seed, 所有 report 数值可跑出
- 数据快照 commit hash 必须引用 (如 `aats_research.silver.* @ 2026-04-25T00:00:00Z`)

### 6.3 Cross-check (轻量版)

- **理想**: 一个非提案作者跑一遍脚本, 核对 key 数值 (IR / edge / hit rate)
- **现状 (单人团队)**: 同一作者在 **fresh session** 跑一遍, 避免 cache / 上下文污染
- 若两次数值不一致, 必须追溯到 source 再提案 (不接受"可能是随机性")

### 6.4 Decision audit

- Go / Conditional / Archive 决策写入提案文档末尾, 附 UTC 时间 + 批准者
- 实施 commit message 必须引用 evidence doc 路径: 例如 `[evidence: docs/research/mstr_tfi_5s_2026_04_25.md]`
- 本 gate 本身也纳入 `frozen_parameters.md` 的"不得绕过"范围 — 改 gate 必须走 gate 同款纪律

---

## 7. 反模式清单 (这些情况**直接拒**, 不进入判定矩阵)

无论 §3 四条过了几条, 以下任一红旗出现即 **Archive 决策**:

1. **动机反模式**: 提案明示或暗示动机是"让现有数据看起来满足原假设"
2. **Cost 造假**: 为让 net edge 正而 assume 更低 fee / 更低 slippage / 不调用实际 fee_resolver
3. **Degenerate cross-window**: 3 时间片中 2 片 IR > 0 但 1 片显著负, 整体看起来正
4. **Single-point win**: 仅 Cost-adjusted 边缘过, 且依赖 boundary case (fee 再涨 0.5 bps 就 fail)
5. **Hyperparameter overfit**: test 的 IR 实际是在 test 上 sweep 得到, 不是 genuine hold-out
6. **Missing replay**: 声称有 alpha, 但拒绝在 Gold replay bars / tick replay 上跑一遍
7. **Unfalsifiable**: 每次"证据不足"就换 horizon / 换 symbol / 换 slicing 再试, 没有明确的"什么证据出现我放弃"承诺
8. **Rule change mid-flight**: 原定 gate 判据, 在结果出来后改判据定义以匹配结果

---

## 8. 应用 scope: 当前两条路线

### 8.1 路线 A: microstructure directional

**Scope 建议** (仅起点, 提案方可调整):
- 数据: `silver.market_orderbook_metrics_15m`, `silver.market_trade_flow_15m`, `bronze.market_orderbook_bbo`, `bronze.market_trades`
- **前置**: Silver microstructure 连续稳定产出 ≥ **7 天** (从 2026-04-20 P0-a 修复起点计)
- 特征候选: OFI, TFI, queue imbalance, top-of-book depth ratio, microprice deviation
- Horizon: 5s / 30s / 5min / 15min (多 horizon 扫描, 不预设)
- Model: 先线性, 过 gate 失败后可考虑 shallow tree (需说明为何非线性)
- Regime slice: 高/低波 × funding 方向 (2×2)

**Gate 应用**:
- 每个 `(feature × horizon)` 组合**独立**过 gate, 不打包提案
- 通过一个组合不等于通过整条路线, 需累积 ≥ N 个独立组合过 gate 才开讨论路线级实施 (具体 N 由第一个组合通过后定)

### 8.2 路线 B: gamma carry / basis / funding

**Scope 建议**:
- 数据: funding rate history (Bronze 已有) + perpetual basis (**需新 collector, 工程量 M**) + mark/spot price history
- **前置**: basis collector 落地 (独立任务)
- 策略类型: **非方向** carry (不赌 price direction)
- Horizon: 8h funding cycle (OKX 固定结算节律)
- Regime slice: funding 符号 × basis 分位数

**Gate 应用**:
- 同路线 A 结构
- 但 Cost-adjusted §3.3 要额外包含 **hedge cost** (现货对冲 perp 的双边 fee + spread)
- Funding cycle 是外生固定节律, cross-window §3.2 建议按月切, 不按 15m

### 8.3 未来新路线

- 套同一 gate 结构, 不为新路线放宽
- 新数据源接入前, 必须有"若数据来了假设是什么"的 pre-registration, 避免看到数据后事后 rationalize

---

## 9. 版本迭代

### 9.1 当前版本

- **v0.1 (本版, 2026-04-20)**: 定义框架 + 判定矩阵 + 反模式. 硬指标数值阈值 **TBD**, 由第一个具体 application 倒推出合理起点.

### 9.2 下次迭代触发

- 路线 A phase 0 出第一份 evidence doc 时, 根据实际需要把"显著劣化阈值" (§3.1) / "标准差倍率" (§3.2) / "最小样本 N" (§3.4) 等具象化
- 或: 发现本 gate 有反模式未覆盖, 补充到 §7

### 9.3 CHANGELOG

| 版本 | 日期 | 变更 |
|---|---|---|
| v0.1 | 2026-04-20 | 初版起草 (用户战略 directive "守门"层) |

---

## 10. 与其他 governance doc 的关系

| 文档 | 关系 |
|---|---|
| `frozen_parameters.md` | §3 "解冻流程" 第 2 步明示: 必须"通过 alpha evidence gate" → 本 gate 就是该条件 |
| `runtime_trading_mode_semantics.md` | Runtime mode 切换**永久冻结**, 不走本 gate (独立 governance, 见该文 §) |
| `p0b_observability_implementation_spec_2026_04_20.md` | P0-b 前端/Grafana/alert 不涉及 alpha 决策, 不走本 gate |
| `docs/design/archived/*` | 通过本 gate 被 Archive 的路线沉淀到这里, 新申请时重指向 |

---

## 11. 不在本 gate 范围的

- 实盘执行层故障处理 (见 runbook / safe_shutdown)
- 实时监控告警 (见 p0b observability spec)
- Runtime trading mode 切换 (永久冻结, 见 runtime_trading_mode_semantics.md)
- 数据层 / 基础设施改动 (如 P0-a Silver ETL 修复 / P0-c candles rolling) — 这些是**真相层**不是**alpha 层**, 不走本 gate

---

## 12. 轻量程序化守门 (最小版)

用户 2026-04-20 战略 directive 对 P3 "程序化守门"的要求: **只做 CI/check, 不急着上复杂 hook 体系**.

**本 gate v0.1 附带的最小守门** (推荐本周末后独立任务落地, 不在本文件实施):

1. **Pre-commit check** (建议, 非强制):
   - 若 commit 修改了 `configs/active_parameter_sets/**` 或 `configs/strategy_profiles/**`, 要求 commit message 含 `[evidence: docs/research/...]` 前缀
   - 若 commit 修改了 `docs/governance/frozen_parameters.md` 的冻结清单, 要求引用 evidence doc + 本 gate 决策记录

2. **CI check** (建议, 非强制):
   - PR 触发时, 如有冻结路径改动, 校验 commit message 引用的 evidence doc 路径**存在且含 Go 决策**

不做:
- 复杂的"自动跑 gate 判定"系统 (gate 本来就要人类 review)
- 拦截 local commit (只在 PR/push 层检查, local 保持低摩擦)

---

## 13. 签署

- 起草: Claude Opus 4.7 · 2026-04-20
- 触发: 用户 2026-04-20 战略 directive "先修真相 → 收敛战略 → 加守门" 的 **守门** 层
- 批准状态: **待用户批准 v0.1**
- 下次 revise 触发: 路线 A phase 0 的第一份 evidence doc 产出时
- 文档所有权: governance layer
