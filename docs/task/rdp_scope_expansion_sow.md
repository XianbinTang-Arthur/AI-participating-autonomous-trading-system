# RDP 能力扩展 SOW

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


> 起草时间: 2026-04-18
> 起因: 实盘连续 19h 不下单的追因显示,RDP 能研究+发布参数,但其管辖范围**不包括**真正卡住下单的那一层(profile-level 门槛)。与此同时 `release_cycle` 周期任务连续 8h exit=1——两个问题串在一起,暴露了 RDP 当前的能力边界与自动化链路健壮性都需要整改。
>
> 本 SOW 只做规划,不做实现。Phase 1 落地前需先独立评审并获得批准。

---

## 1. 现状边界 (Facts)

### 1.1 RDP 当前管辖范围

`governance.active_parameter_sets` 每个 combo 包含的 22 个字段(以 directional/1h 为代表):

| 类别 | 字段 | 作用 |
|------|------|------|
| 信号阈值 | `entry_threshold`, `close_threshold`, `scale_in_threshold` | combo 内部信号判定 |
| 单合约净边际 | `min_safe_net_edge_bps`, `de_risk_net_edge_bps`, `failed_thesis_net_edge_bps` | combo 级成本门槛 |
| 成本估算 | `taker_fee_bps`, `slippage_bps`, `expected_execution_buffer_bps`, `expected_slippage_buffer_bps` | 决策层成本估算输入 |
| 风控边界 | `max_acceptable_cost_bps`, `catastrophic_failed_thesis_buffer_bps`, `directional_return_clamp_bps` | combo 层 clamp |
| 稳定性 | `min_confirm_ticks`, `min_hold_seconds`, `score_stability_threshold`, `rebalance_cooldown_seconds` | 抗噪声 |
| 流动性 | `min_liquidity_quality`, `max_thesis_age_seconds` | 订单前置条件 |
| 其他 | `directional_trend_weight`, `limit_offset_bps_entry`, `signal_edge_scale_bps` | combo-specific |

### 1.2 RDP 不管的层次

| 层次 | 字段举例 | 当前谁管 | 影响 |
|------|---------|---------|------|
| **Strategy Profile 门槛** | `strategy_entry_min_signal_edge_bps=13.0`, `strategy_entry_alpha_min`, `strategy_min_net_edge_bps` | `strategy_profile_seed.py` 硬编码 clamp | **直接决定能否开单** — 当前实盘卡单的根因在这 |
| **Profile 切换** | `active_profile_id` (balanced/trend_normal/trend_high/breakout/...) | operator 手动 + 稀疏的 workflow | 各 profile 门槛差异可达 2 倍 |
| **Regime 识别阈值** | `regime_detector` 的斜率/带宽/波动分档 | 常量 | 决定 trend 切换节奏 |
| **Cost Model 校准** | fee/slippage 静态值 | 常量 (12.6 bps 合计) | 偏乐观 → net_edge 被高估 → gate 假阳性 |
| **Sleeve Budget** | 每个 strategy 的资金配额 | `sleeve_budget_profiles` 静态表 | 组合层风险与 alpha 权衡 |
| **Risk Guardrail** | max_leverage, max_gross_exposure, per-symbol_cap | 配置文件常量 | 黑天鹅承受力 |

### 1.3 实盘数据佐证

- `strategy_profile_activation` 自 `2026-04-16 20:47:34 UTC` 初始化以来**从未更新**,actor=`system_seed`,reason=`initial_seed`。
- `parameter_releases` 10 条全部 `apply_result=success`,最新 `2026-04-18 04:09:53 UTC`——**combo 层**参数在被持续更新。
- 即 RDP 在它管辖的范围内正常工作,但这个范围与"能不能下单"几乎脱钩。

---

## 2. 扩张方向 (Scope)

### 方向 1:Profile-level 参数纳管 ⭐ 最优先

**目标:** 让 `strategy_entry_min_signal_edge_bps` 这种"决定能不能下单"的门槛,可以被 RDP 基于历史回测数据建议调整。

**技术改动:**

- 新增 `recommendation_type = "profile_upgrade"`
- 扩展 `parameter_sets` schema 引入 `scope`:
  - `scope=combo` (现有,默认)
  - `scope=profile` (新增,key = profile_id,如 `trend_normal`)
- Research 侧:增加 profile-level 研究 job,对每个 profile 的关键门槛做 grid search + OOS 验证
- Gate 侧:profile_upgrade 的 Gate 规则应比 combo 严格:
  - 至少 30 天 OOS 回测
  - Sharpe 不下降 + MaxDD 不扩大
  - 活跃度(年化成交次数)不能骤降 50% 以上
- Apply 侧:复用既有 apply_token + CAS + 观察窗;**但 `approve-and-release` 一键按钮对 profile_upgrade 禁用**,强制分步

**风险等级:** 🔴 高 —— 一次 profile 切换可能让系统从"不下单"变"疯狂下单"或反之

**工作量估算:** 3-5 天(含 schema 迁移、research job、UI 分流、测试)

---

### 方向 2:Cost Model 校准(daily/weekly reconciliation)⭐ 次优先

**目标:** 用真实 fills 数据反推 `taker_fee_bps + slippage_bps`,防止 cost 估算与实盘偏差累积导致"净边际"系统性偏差。

**为什么优先级仅次于方向 1:**
- cost model 偏乐观 → gate 假阳性 → 实盘亏损累积
- cost model 偏保守 → gate 假阴性 → 错过机会(现状的一部分可能是这个)
- RDP 已经有 fills 数据,是成本最低的扩张

**技术改动:**

- 新 research job:`cost_calibration_cycle`(每日一次)
  - 输入:最近 7 天 `execution_fills` + 对应 `execution_orders` 的成交价、下单价、amount
  - 输出:`effective_taker_fee_bps` / `effective_slippage_bps` per symbol per timeframe
- drift 检测:实测 vs 当前 `taker_fee_bps` 配置,|Δ| > 阈值触发 recommendation
- recommendation_type 复用 `parameter_upgrade`(scope=combo, 只改成本字段)

**风险等级:** 🟡 中 —— 只改估算,不直接改实盘行为;但 drift 大时说明现有 gate 一直在误判

**工作量估算:** 2-3 天

---

### 方向 3:Sleeve Budget 建议(observation-only)

**目标:** RDP 根据各 strategy 历史 edge/Sharpe/drawdown 周期性 recommend sleeve budget 调整。

**为什么不像前两个优先:**
- 资金分配改动的二阶效应难量化
- 新 strategy (冷启动 < 60 天) 的数据噪声大,容易过拟合
- 单个 sleeve 的历史 edge 可能由少数大单主导,波动大

**技术改动:**

- 新 recommendation_type `sleeve_budget_adjust`(纯 observation,**不带 auto-apply**)
- 输出 "本周 advice: strategy_X 建议从 30% → 25%" 级别的建议到 UI
- operator 可以采纳后手动编辑 `sleeve_budget_profiles`

**风险等级:** 🟢 低(observation-only)

**工作量估算:** 2 天(大多是 UI 和文案)

---

### 方向 4:Risk Guardrail 动态化

**目标:** max_leverage / max_gross_exposure 根据 portfolio realized vol / drawdown 动态收紧/放松。

**为什么放最后:**
- 风控参数的"过紧"会直接让系统停转,一个 bug 成本极高
- 比 profile 切换还高一个 blast radius
- 需要非常慢的 rollout(shadow-mode → small-delta 建议 → operator 手动)

**技术改动:** 暂不展开,需要独立 SOW

**风险等级:** 🔴 高(同方向 1)

---

## 3. 阶段化路线图

| Phase | 目标 | 周期 | 前置 |
|-------|------|------|------|
| **Phase 0** | 修 `release_cycle` 死循环(见 §5) | 当天 | — |
| **Phase 1** | 方向 1 — Profile-level 参数纳管 | 3-5 天 | Phase 0 |
| **Phase 2** | 方向 2 — Cost Model 校准 | 2-3 天 | Phase 1(复用 recommendation 流程) |
| **Phase 3** | 方向 3 — Sleeve Budget advice | 2 天 | Phase 2 |
| **Phase 4** | 方向 4 — Risk Guardrail 动态化 | 独立 SOW,至少 1 周 | Phase 3 + 独立评审 |

---

## 4. 共用 Guardrail(所有 Phase 强制)

每个新的 scope 都必须满足:

1. **独立 Gate 阈值**:不能复用 combo 层的 Gate(profile 层需要更严的 OOS 覆盖)
2. **强制 dry-run 观察窗**:新 recommendation_type 的默认 `observation_window_hours` 至少是 combo 的 3 倍(72h 起步)
3. **Operator 二次确认**:对 profile/sleeve/risk scope,"批准并发布"按钮**禁用**,必须 approve → Gate → 人工看 Gate 结果 → release 分步完成
4. **回滚路径**:每个 scope 都要有 `POST /rdp/<scope>/rollback` 端点
5. **审计**:所有 apply/rollback 记录必须进 `parameter_apply_history`(新增 scope 字段区分)
6. **Shadow 模式**:每个新 scope 在上线头 2 周只产出 recommendation 不允许 auto-apply,等 recommendation 质量被 operator 手工验证达标

---

## 5. Phase 0 预置:修复 release_cycle 死循环

### 问题重述
- `aats-rdp-daemon` 每小时入队 `release_cycle` workflow
- scheduler 根据 `configs/rdp_workflows/release_cycle.json` 执行 `scripts/rdp_run_release_cycle.py`
- 该脚本在"批次 A 硬化"中被 stub 为 `sys.exit(2)`,理由是"防止 CLI 绕过 API token"
- **结果**:daemon 自己在调用自己已 stub 的子脚本 → 每小时 exit=1,8h+ 未报警未修复

### 方案对比

| 方案 | 改动面 | 保留批次 A 硬化意图 | 推荐度 |
|------|-------|-------------------|-------|
| **A. 在 stub 脚本加 daemon-internal env gate** | 最小 | ⚠ 增加"暗门" | 不推荐 |
| **B. daemon 直接 in-process 调 `run_release_cycle`**(跳过 scheduler + subprocess) | daemon 特判 release_cycle | ✅ 保留 | ⭐ 推荐 |
| **C. 从 daemon workflow 列表移除 release_cycle**,只保留手动触发 | 最简单,去功能 | ✅ 保留 | 兜底 |

**推荐方案 B** 的理由:
- 符合"CLI 对外禁用,daemon 可用"的设计意图(daemon 是 trusted context)
- 去掉不必要的 subprocess fork
- 其他 workflow(research_cycle / governance_cycle / data_maintenance)保持 subprocess 模式,不改通用 dispatcher
- 改动收敛在 `scripts/rdp_task_daemon.py` 一个文件

### Phase 0 验收
- [ ] daemon 下一个 release_cycle tick 不再 exit=1
- [ ] `rdp_task_queue` 出现至少 1 条 `release_cycle status=done`
- [ ] 如果有 pending-approved recommendations,能自动 release 并写 `parameter_releases`
- [ ] 如果没有 pending-approved,日志打 `"no eligible recommendations, skipped"` 但 exit=0

---

## 6. 关键决定(Phase 1 开工前的前置设定)

> 2026-04-18 决定:运营方把这 5 个问题全权委托给工程侧,以下是本次 SOW 的定稿选择;若 Phase 1 实施中发现决策有误再单独发修订。

### 6.1 Profile-level 研究的 OOS 窗口 → **90 天**

理由:
- 与 combo-level research 的窗口对齐(同一套数据基础设施),减少维护负担。
- 90 天覆盖 ≥ 3 个完整周线、12+ 日线周期,足以识别周期性 regime 变化而不被单月极端行情主导。
- 180 天长度在加密市场已经跨越多个行情切换,用来校准 profile 门槛反而会拉平特征——profile 的意义就是适配当前 regime,90 天更贴近"当下最优"。

### 6.2 Profile 切换的 Gate 规则 → **三指标联审 + 活跃度下限**

不允许单一指标决策。目标 profile 在 90 天 OOS 上必须**同时满足**:

| 指标 | 阈值 | 目的 |
|------|------|------|
| Sharpe | ≥ 当前 profile 的 **95%** | 不能牺牲风险调整后收益 |
| MaxDD | ≤ 当前 profile 的 **105%**(即恶化不超过 5%) | 尾部风险守住 |
| 年化成交笔数(活跃度) | ≥ 当前 profile 的 **50%** | 防止变成"门槛更高 = 不下单 = 假 Sharpe"(本次事故就是这个模式) |
| 命中率 | 不做硬阈值,只作 observational | 命中率会被 Sharpe + 活跃度间接约束 |

**本次追因直接驱动的设计**:活跃度下限这条必须有。trend_normal 现在就处在"数值很漂亮因为没开仓"的状态,这种"profile 漂亮但系统不工作"绝不能被 Gate 当成 upgrade candidate 放过。

### 6.3 Shadow 期 → **4 周,且至少 5 条 profile_upgrade recommendation 产生过**

两个条件联合判定 Shadow 期结束(任一未达即延长):

1. **时间条件**:至少 4 周(4 次 weekly research_cycle)
2. **样本条件**:此期间至少产出 5 条 profile_upgrade recommendation,且其中至少 3 条获 operator 人工评分(分数不作硬阈值,但必须有记录)

为什么不是 2 周:一次 research_cycle 是 weekly,2 周只有 2 个数据点,根本看不出 recommendation 的稳定性;4 周能观察到"同 regime 下 RDP 是否反复建议同样的值"这个一致性信号。

为什么需要样本条件:如果 Shadow 期碰上市况单调,可能 4 周一条 recommendation 都没出,这种 fallthrough 不能当作"Shadow 通过"。

### 6.4 profile_upgrade 与旧 `strategy_profile_recommendations` 表 → **短期并存,长期合流**

短期(Phase 1 - Phase 3):
- **新 recommendation 全部走 RDP 的 `governance.recommendations` 表**(`recommendation_type=profile_upgrade`)
- **RDP apply 成功后,同步写一条 audit 记录进 live DB 的 `strategy_profile_recommendations`**,作为 live 侧审计 sink
- 旧表的直接写入路径(非 RDP 来源)保留,让手动紧急 profile 切换还能走
- 两侧 recommendation_id 用独立前缀区分:RDP 产出 `prof_rec_*`,手动走 `manual_prof_*`

长期(Phase 4+):
- 根据 Phase 1-3 运营数据判断是否还需要"手动直接写 live 表"的 escape hatch
- 如果 90 天内没有真实使用,在 Phase 4 做 schema 合流 SOW;否则保留两条路径

### 6.5 `strategy_profile_seed.py` 的 clamp 范围 → **不放宽,超界触发 `profile_type_review`**

现状:`trend_normal` 的 `strategy_entry_min_signal_edge_bps` clamp 在 `[13.0, 16.0]`。RDP 若基于 90 天 OOS 算出最优 = 10 bps,有两种可能含义:

1. Seed 定的下限过保守——应该放宽
2. 当前市况就不适合 trend_normal——应该切换到另一个 profile(比如 balanced)

两种可能性本质上不同,clamp 不能自动放。规则:

- **RDP 建议值超出 clamp 时 → recommendation 状态直接 `rejected_by_clamp`,不进 draft**
- **连续 3 轮 research_cycle 同一个 profile 的建议都超出同一个方向的 clamp 时,产生 `profile_type_review` 特殊 recommendation**
  - 不 auto-apply,只标红给 operator
  - 内容:"`trend_normal.strategy_entry_min_signal_edge_bps` 近 3 轮建议分别是 10.2 / 9.8 / 11.1,均低于 clamp 下限 13.0;建议人工评估:(a) 切换到 balanced profile (b) 放宽 clamp 下限 (c) 用新 90 天数据重新 seed"
- 同时暴露"seed vs research diverge"度量到 Grafana,作为"profile taxonomy 需要重建"的长期信号

理由:clamp 是 profile **语义定义的一部分**,而不是临时数值约束。放宽 clamp = 改变 profile 的身份,这应该是人类决策,不是 RDP 自动化能决定的。但系统必须有能力识别 clamp 与数据长期脱节并升级到人类审查——这条路径就是 `profile_type_review`。

---

## 7. 不在范围

- **订单执行层的参数**(limit vs market、slicing、TWAP 窗口) —— 这些归 execution layer,不走 RDP 治理
- **Feature 工程 / alpha model 重训** —— 属于 research 独立轨道,已有 `research_cycle` 覆盖
- **硬件/基础设施相关的常量**(DB 连接池、Redis TTL) —— 非交易参数

---

## 附录 A:决策层读参数路径图

```
DecisionEngine
  ├─ StrategyProfile (读 strategy_profile_activation.payload.active_profile_id)
  │    └─ clamp from strategy_profile_seed.py  ← 当前 RDP 未纳管
  ├─ ComboParameters (读 governance.active_parameter_sets)
  │    └─ ✅ 当前 RDP 已纳管
  └─ CostModel (读 combo parameters + sleeve budget)
       └─ 部分纳管(combo-level fee/slip) / 部分未纳管(sleeve budget)
```

## 附录 B:可观测性补齐

建议在 Phase 1 同步补齐:
- RDP daemon 任务成功率(按 workflow 分) 的 Grafana panel
- `parameter_apply_history` 的每日聚合(按 scope/actor 分)
- profile_activation 最后更新时长 SLI(告警:> 7 天未更新 = profile 可能已僵化)
