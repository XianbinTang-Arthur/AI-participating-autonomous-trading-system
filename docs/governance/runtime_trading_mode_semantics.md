# Runtime Trading Mode 语义 (2026-04-20)

> **本文件为 governance 文档, 不是实施指引.**
> 作用: 让 operator / reviewer 一眼看出 "系统在不下单是按设计行为, 不是 bug".

**创建时间**: 2026-04-20
**创建背景**: 2026-04-19 用户问 "1.2% 阳线为何不下单", 诊断链条最终追到 `decision_authority = "reference_only"`. 调查发现 **这不是 bug, 是当前 `ai_operating_mode = "baseline_only"` 的按设计行为**. 但之前缺少文档把这件事说清楚, 导致 30+ 小时 alpha 挖掘工作建立在"不下单 = bug"的错误前提上.

---

## 1. 硬事实 (2026-04-20 WSL2 live)

```
AATSSettings.ai_operating_mode = "baseline_only"           ← aats/bootstrap/settings.py:245
→ DecisionOutcome.decision_authority = "reference_only"     ← target_position.py:1927, 1990
→ DecisionOutcome.final_action = "hold"                     ← 无视 book_runtime_states.score
→ final_target_qty = 0                                      ← 系统不下单, 按设计
```

即使:
- Baseline 有 composite_alpha_score (例 -0.21, short 倾向)
- Book runtime state 有 score (例 long=0.14)
- Effective entry threshold 合理 (例 0.25)
- Expected net edge 计算完整 (例 -6.33 bps)

**只要 `ai_operating_mode = "baseline_only"`, 系统 100% 不下单**, 无论上述其他字段什么值.

---

## 2. 四档运行模式 vs 实盘含义

### 2.1 Canonical mapping (决策权限)

`aats/services/decision_engine/target_position.py:1926-1930` 的映射:

```python
authority_map = {
    "baseline_only":     "reference_only",   # 不下单
    "ai_assisted":       "advisory",          # AI 弱参与, 默认按 baseline 下单
    "ai_decision_maker": "final_decision",   # 完全按 AI (+baseline fallback) 下单
}
```

历史 alias (见 `aats/schemas/decision.py:48-58`):
- `ai_advisory / ai_blended` → 折叠到 `ai_assisted`
- `ai_primary / ai_decision_maker_with_profile_control` → 折叠到 `ai_decision_maker`

### 2.2 四档语义表

| canonical mode | decision_authority | 实盘行为 | 风险级别 | 适用阶段 |
|---|---|---|---|---|
| `baseline_only` | `reference_only` | **完全不下单**, 全程 hold/flat, 只产 reference decision 用于观察 | 零资金风险 | 开发 / 调试 / 压测 / alpha 探索 / 当前生产 |
| `ai_assisted` | `advisory` | Baseline 决定, AI 仅咨询; final_action 跟随 baseline; 真下单 | 中 (baseline quality 决定) | Baseline alpha 已 validated, AI 开始接入但不主导 |
| `ai_decision_maker` | `final_decision` | AI 主导, baseline 作为 fallback 源; final_action 跟随 AI 或 baseline; 真下单 | 高 (AI quality + fallback 双重决定) | AI 已 validated, 准备放给 AI 决策 |

### 2.3 本次 session 发现的共识误区

**误区**: "不下单 → 系统坏了 / 模型不够好 / 门槛太严 / 数据不够"

**真相**: 在 `baseline_only` 模式下, **上述任何原因都不是不下单的原因**. 系统在 `decision_authority="reference_only"` 硬拦截下**按设计 hold**.

**直接的、避免未来重犯的 check**:
> 任何 "为什么系统不下单" 的讨论, **第一步必须确认 `ai_operating_mode` 是什么**. 若是 `baseline_only`, 答案已结束, 没有 bug 要修.

---

## 3. 切换到实盘模式的前置条件 (checklist)

**禁止** operator 未经以下检查直接切 `ai_operating_mode` 到 `ai_assisted` 或 `ai_decision_maker`:

### 3.1 Alpha 证据门槛 (未来 P1 evidence gate 成文后接入)

- 至少有**一条 alpha 路径**在 out-of-sample + 成本后净收益正 + cross-window 稳定
- Gate 决议文档签字在案

### 3.2 基础设施可信 checklist

- [ ] Silver ETL 持续产出, `SELECT MAX(ts) FROM silver.market_*_15m` 在最近 15 min 内
- [ ] Task queue 状态和实际数据状态一致 (不存在"假成功")
- [ ] `meta.ingest_runs.status` 语义真实反映 success/fail
- [ ] Dashboard panel 显示 freshness 告警, operator 不依赖人工查表

### 3.3 Execution economics checklist

- [ ] 真实成本 (fee + slip) 监控有 alerting
- [ ] `expected_net_edge_bps` 计算正确 (cross-check with fee_resolver)
- [ ] Order throttle / churn 限制已设

### 3.4 Runtime 安全 checklist

- [ ] `max_notional_per_symbol` 上限合理
- [ ] Kill switch 测试过 (手动触发可用)
- [ ] Reconciliation 对账流程跑过一次端到端

### 3.5 切换流程

```
1. 阅读本文档 + 最新 alpha evidence gate 决议
2. 填写 3.2-3.4 checklist, 全 ✓ 才继续
3. 修改 .env.derivatives.live 的 AATS_AI_OPERATING_MODE=<new_mode>
4. git commit + deploy (有 audit trail)
5. 观察第一个 15 min tick 的 decision_outcome:
   - decision_authority 必须是预期值
   - 若有实际下单, confirm order + position 符合预期
6. 如任何异常, 立即 kill switch + 回 baseline_only
```

**严禁**:
- 跳过 checklist 直接改 env
- 绕过 deploy.sh 手动 `docker exec` 改 env
- 为了"让系统下单"而切模式但不先看 alpha 证据

---

## 4. Observability 要求 (P0-b 修复范围)

本 governance doc 本身不足以消除未来误读. 必须配套 observability:

### 4.1 Operator UI 明示

- 主页面**顶栏永久可见**显示 "当前运行模式: **baseline_only** (reference_only / 按设计不下单)"
- 若模式 = `ai_assisted` / `ai_decision_maker`, 颜色变红, 提醒实盘中
- 点击标签可跳到本文档

### 4.2 Grafana panel

- 新增 panel `Runtime Trading Mode` 在主 dashboard 醒目位置
- 显示:
  - 当前 canonical mode (从 `strategy.decision_outcome.ai_operating_mode` 最新值取)
  - decision_authority (derived)
  - 最近 1h / 24h 下单数 (应与 mode 一致: baseline_only = 0)
- 告警规则:
  - 若 baseline_only 模式下过去 24h 有 order submitted → 警报 (不可能情况, 防御性监控)
  - 若 ai_decision_maker 模式下过去 24h 0 order → 告警 (可能 alpha/cost 问题)

### 4.3 Decision outcome event

- `strategy.decision_outcome` payload 已有 `ai_operating_mode` + `decision_authority` 字段
- **下游 consumer** (Loki 索引 / Prometheus metric / Operator UI) 都必须显示这两个字段
- 不允许只展示 `final_action`/`final_target_qty` 而**隐藏**模式上下文

---

## 5. 回到本 session 的 30 小时工作

按本文档的事实:

- H4 方向门控修复: ✅ 真 bug 修复, 数学正确, 保留
- fast_impulse NO-GO: ✅ 真发现 (15m OHLC linear 无 alpha), 结论有效
- FADE raw-level NO-GO: ✅ 真发现, 结论有效
- kline+funding preview NO-GO: ✅ 真发现, 结论有效
- OI delta NO-GO: ✅ 真发现, 结论有效
- True basis × OI NO-GO: ✅ 真发现 (agent 第 5 份), 结论有效
- "等 30 天 microstructure 后系统会下单" → **❌ 伪推论**, 本文档第 1 节否决: `ai_operating_mode` 不改, 永远不下单.
- "为 1.2% 阳线调 entry_threshold / min_confirm_ticks" → **❌ 按本文档第 3 节, 是绕过门槛的不当做法**.

5 份 NO-GO 的**数据层面发现**仍然有效(它们回答"15m OHLC 线性特征是否有可 exploit alpha"), 但**运维层面的 "所以系统才不下单" 解释错了** —— 系统不下单的第一原因始终是 `baseline_only` 模式, 不是 alpha 缺失.

---

## 6. P1 后续工作关联

本 governance doc 约束:

| 后续文档 | 关联 |
|---|---|
| `docs/governance/alpha_evidence_gate.md` (P1) | 3.1 的 "alpha 证据门槛" 具体量化 |
| `docs/research/dual_track_minimum_loop.md` (P1) | microstructure 3 个 + gamma carry 3 个最小闭环 |
| `docs/governance/frozen_parameters.md` (P1) | 15m entry_threshold / min_confirm_ticks / edge_scale 冻结列表 |
| `docs/review/p0a_silver_etl_truth_layer_fix_2026_04_20.md` | 基础设施真相层修复完工 |
| `docs/review/p0b_runtime_mode_observability_fix_2026_04_20.md` | 本 doc 对应的 observability 代码改动 (dashboard + UI label) |

---

## 7. 签署

- 起草: Claude Opus 4.7 · 2026-04-20
- 触发: 用户 2026-04-19 "为何不下单" + 2026-04-20 战略 framework directive
- 批准状态: 待用户确认
- 下次修订条件:
  - alpha evidence gate 成文后补 §3.1 具体化
  - 任一 mode 切换发生后补 audit trail
- **文档所有权**: governance layer, 改动需符合 "重大改动前必须备份+设计+获批准" 纪律 (CLAUDE.md §7)
