# Runtime Trading Mode 语义 (2026-04-20, 2026-04-23 勘误)

> **本文件为 governance 文档, 不是实施指引.**
> 作用: 让 operator / reviewer 精确理解 `ai_operating_mode` 的真实语义, 不要再把 "不下单" 误当作模式层面的硬约束.

**创建时间**: 2026-04-20
**2026-04-23 勘误**: 原版本错误地把 `baseline_only` 描述成 "按设计完全不下单 / 100% 不下单 / final_target_qty=0". 经全量代码审阅 (见 §8 修订记录), 这个描述与代码实际行为**不符**. 真实语义是: **`baseline_only` 只是不调用 AI 参与决策, 系统仍走完整的 baseline → target → 下单流程**. 是否下单由 baseline 信号驱动, 不是由模式强制 hold. 下文均已按真实语义改写.

---

## 1. 硬事实 (2026-04-23 校对后)

```
AATSSettings.ai_operating_mode = "baseline_only"           ← aats/bootstrap/settings.py
→ DecisionOutcome.decision_authority = "reference_only"     ← target_position.py:1936-1940 (authority_map)
→ target_qty 由 _target_quantity_baseline_only() 计算        ← target_position.py:724-755
→ final_action 由 baseline 派生的 position_intent 派生         ← target_position.py:1971-1983 action_map
→ final_target_qty = target_qty (不被 mode 改写为 0)         ← target_position.py:2016
→ 进入执行链 (order_manager / okx_adapter) 正常下单           ← execution_engine 不检查 decision_authority
```

关键点:
- **`decision_authority="reference_only"` 是一个标签字段**, 用于下游审计/展示, **不是执行拦截关卡**. 执行引擎 (`aats/services/execution_engine/`, `aats/services/execution_control/`) **不存在**任何对 `reference_only` 或 `baseline_only` 的分支判断.
- `baseline_only` 下是否下单, 完全由 baseline 的 `composite_alpha_score` / `direction_bias` / `position_intent` 等决定. 若 baseline 有信号且过风控, **系统会下单**; 若 baseline 是 flat 或被 guardrail 拦截, **系统不下单** —— 但这和 `ai_assisted` / `ai_decision_maker` 模式下 baseline fallback 的行为是一致的.
- `ai_operating_mode` 唯一明确拦的是 "是否调用 AI" (见 `build_ai_decision_intent` target_position.py:406) 以及 "target_qty 计算分支选哪一个" (baseline_only / ai_assisted / ai_decision_maker).

"不下单" 本身在 `baseline_only` 下 **既可能是正常的 (baseline 无信号)**, **也可能意味着 alpha/cost 问题** —— 需要看 baseline 层诊断, 不能归因到模式.

---

## 2. 四档运行模式 vs 实盘含义

### 2.1 Canonical mapping (决策权限)

`aats/services/decision_engine/target_position.py:1926-1930` 的映射:

```python
authority_map = {
    "baseline_only":     "reference_only",   # 纯 baseline 决策, 不调用 AI; 是否下单由 baseline 信号决定
    "ai_assisted":       "advisory",          # AI 弱参与, 默认按 baseline 下单
    "ai_decision_maker": "final_decision",   # 完全按 AI (+baseline fallback) 下单
}
```

注意: `authority_map` 的值 (`reference_only` / `advisory` / `final_decision`) 仅作为 **标签** 参与 `DecisionOutcome.decision_authority` 字段, 用于审计/展示. 执行引擎不会根据它决定是否下单.

历史 alias (见 `aats/schemas/decision.py` 中的 `AI_OPERATING_MODE_CANONICAL_MAP`):
- `ai_advisory / ai_blended` → 折叠到 `ai_assisted`
- `ai_primary` → 折叠到 `ai_decision_maker`

**已删除**（2026-04-24）：`ai_decision_maker_with_profile_control` 曾经把"AI 决策者"和"自动换档"
两个模块捆在一个枚举值里，违反正交性原则，已彻底移除。运行模式（`ai_operating_mode`）和
档位自动换档（`strategy_profile_auto_control_enabled`）现在完全独立——前者是 AI 在单次决策里
扮演什么角色，后者是 6 个策略档位由谁选，两个开关互不影响。老事件 payload 若仍含该值，
`normalize_ai_operating_mode` 会 fallback 到 `baseline_only`（安全兜底）。

### 2.2 四档语义表

| canonical mode | decision_authority | 实盘行为 | 风险级别 | 适用阶段 |
|---|---|---|---|---|
| `baseline_only` | `reference_only` | **不调用 AI**, 决策链 = pure baseline. 是否下单由 baseline 信号决定 (有信号会真下单) | 等于 baseline 策略自身的风险 | Baseline 为唯一 alpha 来源时 / AI 未 validated 时 / 当前生产 |
| `ai_assisted` | `advisory` | Baseline 决定, AI 仅咨询; final_action 跟随 baseline; 真下单 | 中 (baseline quality 决定) | Baseline alpha 已 validated, AI 开始接入但不主导 |
| `ai_decision_maker` | `final_decision` | AI 主导, baseline 作为 fallback 源; final_action 跟随 AI 或 baseline; 真下单 | 高 (AI quality + fallback 双重决定) | AI 已 validated, 准备放给 AI 决策 |

### 2.3 勘误: 把 "不下单" 误当模式硬约束

**曾经的误区** (2026-04-20 起至 2026-04-23 勘误前):
> "`baseline_only` = 按设计不下单, 所以诊断链遇到 `decision_authority=reference_only` 就可以停手, 不是 bug 要修."

**真相**: 代码里**没有**任何基于 mode 的下单硬拦截. `baseline_only` 下不下单的唯一原因是 **baseline 层没产出可下单的信号** (direction=flat / 被 guardrail 拦 / target_qty 等于当前持仓 等). 这和 alpha 层诊断**完全同一条路径**.

**正确的诊断顺序** (2026-04-23 起):
1. **先查 baseline 层**: `baseline.direction_bias` / `composite_alpha_score` / `position_intent` / `guardrail_flags`. 是 flat? 被 guardrail 拦? target 等于当前仓?
2. **再查风控/成本层**: `expected_net_edge_bps`, `max_acceptable_cost_bps`, `strategy_edge_noise_buffer_bps`, cooldown 等.
3. **最后才看 mode**: `ai_operating_mode` 只影响 "是否调 AI" 和 "target_qty 算法选哪条", 不影响 "下不下单" 的布尔判断.

**注意**: 2026-04-19 至 2026-04-20 session 里 "1.2% 阳线为何不下单" 的归因结论 (归咎 `ai_operating_mode=baseline_only`) **是错的**. 真实原因应回到 baseline 层 (`composite_alpha_score` / `position_intent`) 重新诊断. 5 份 NO-GO 证据的**数据层发现仍然有效** (15m OHLC 线性 alpha 不足), 但"所以 mode 挡住不下单"的解释**不成立**.

---

## 3. 切换到实盘模式的前置条件 (checklist)

**禁止** operator 未经以下检查直接切 `ai_operating_mode` 到 `ai_assisted` 或 `ai_decision_maker`:

### 3.1 Alpha 证据门槛 (已接入 `alpha_evidence_gate.md` v0.1)

- 至少有**一条 alpha 路径** 通过 [`alpha_evidence_gate.md`](alpha_evidence_gate.md) §3 四条硬指标 (OOS / cross-window / cost-adjusted / regime-slice)
- 提案文档需过 §7 八条反模式 red flag 自查
- Go 决策记入提案文档末尾 + `[evidence: docs/research/...]` commit 前缀
- §5 cost model 用 governance 当前值, 禁止"调低 cost 让 net edge 变正"反模式

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

### 3.5 持久化切换流程 (推荐)

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

### 3.6 临时 override 机制 (UI admin 通道)

**存在的事实**: Admin UI 有 `/ai/operating-mode/select` 路径 (`aats/api/auth_routes.py`),
调用 `set_manual_operating_mode_override`. 这是**设计上的 escape hatch**, 不是漏洞, 但有严格约束:

| 属性 | 约束 |
|---|---|
| 授权 | 必须 `require_admin_access` (operator role=admin) |
| 持久性 | **临时**: `ai_manual_operating_mode_override_freeze_seconds` 后自动 expire 回配置值 |
| Audit | 调用时记录 `actor_role` / `actor_identity` / `auth_source` / `reason` |
| 持久化 | **不改 .env.\*.live**; 重启服务或 expire 后回 baseline |
| 门槛 | `AATS_ALLOW_UI_OPERATING_MODE_OVERRIDE` env var, **默认 false** (2026-04-20 起, 见下文) |

**何时允许用 UI override** (代替 §3.5 持久化流程):
- 短时紧急 (< 24h) 实盘切换
- 且 §3.2-3.4 checklist 也已满足
- 且 operator 主动承诺"事后补 §3.5 持久化流程或显式 kill switch"
- 且 alpha_evidence_gate 已有至少一条 Go 决策

**何时不允许用 UI override**:
- alpha_evidence_gate 尚未有任何 Go 决策 (2026-04-27 观察窗结束前即是此情况)
- `AATS_ALLOW_UI_OPERATING_MODE_OVERRIDE=false` (默认值) — 代码层直接拒, 前端按钮虽显示但后端返回 403
- 任何"为了让系统立刻下单"的即兴动机

**严禁**:
- 跳过 checklist 直接改 env
- 绕过 deploy.sh 手动 `docker exec` 改 env
- 为了"让系统下单"而切模式但不先看 alpha 证据
- 用 UI override 绕过 §3.5 持久化纪律 (override 必须 post-hoc 补 §3.5)

---

## 4. Observability 要求 (P0-b 修复范围)

本 governance doc 本身不足以消除未来误读. 必须配套 observability:

### 4.1 Operator UI 明示

- 主页面**顶栏永久可见**显示 "当前运行模式: **baseline_only** (reference_only / 仅 baseline, 不使用 AI)"
- 若模式 = `ai_assisted` / `ai_decision_maker`, 颜色变红, 提醒 AI 已接入实盘
- 点击标签可跳到本文档

### 4.2 Grafana panel

- 新增 panel `Runtime Trading Mode` 在主 dashboard 醒目位置
- 显示:
  - 当前 canonical mode (从 `strategy.decision_outcome.ai_operating_mode` 最新值取)
  - decision_authority (derived)
  - 最近 1h / 24h 下单数 (用于观察, **不绑定 mode 的应然值**: 任何 mode 都可能 0 或非零)
- 告警规则 (2026-04-23 校正):
  - ~~"baseline_only 模式下过去 24h 有 order submitted → 警报"~~ 已废弃. 理由: baseline_only 下下单是合法行为, 该规则会产生持续误报. 见 §8 修订记录.
  - 若 `ai_decision_maker` 模式下过去 24h 0 order → 告警 (可能 alpha/cost 问题) — 保留

### 4.3 Decision outcome event

- `strategy.decision_outcome` payload 已有 `ai_operating_mode` + `decision_authority` 字段
- **下游 consumer** (Loki 索引 / Prometheus metric / Operator UI) 都必须显示这两个字段
- 不允许只展示 `final_action`/`final_target_qty` 而**隐藏**模式上下文

---

## 5. 回到本 session 的 30 小时工作

按本文档的事实 (2026-04-23 勘误后重新校对):

- H4 方向门控修复: ✅ 真 bug 修复, 数学正确, 保留
- fast_impulse NO-GO: ✅ 真发现 (15m OHLC linear 无 alpha), 结论有效
- FADE raw-level NO-GO: ✅ 真发现, 结论有效
- kline+funding preview NO-GO: ✅ 真发现, 结论有效
- OI delta NO-GO: ✅ 真发现, 结论有效
- True basis × OI NO-GO: ✅ 真发现 (agent 第 5 份), 结论有效
- "等 30 天 microstructure 后系统会下单" → 推论**条件正确**: 只要 baseline 层有 validated alpha, `baseline_only` 下系统就会下单. 之前版本的反驳 ("mode 不改永远不下单") **已撤回**.
- "为 1.2% 阳线调 entry_threshold / min_confirm_ticks" → 仍然是 **❌ 绕过 alpha_evidence_gate 的反模式** (见 §3 + `frozen_parameters.md`).

5 份 NO-GO 的**数据层面发现**仍然有效. 但**运维层面的归因**需要倒回去: 系统不下单的第一原因是 **baseline 层无可下单信号 (alpha/cost 不足)**, 不是 `baseline_only` 模式. 模式只决定是否调 AI, 不决定是否下单.

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
- 批准状态: 用户 2026-04-20 初稿批准; 2026-04-23 勘误修订版已批准 (见 §8)
- 下次修订条件:
  - ✅ alpha evidence gate 已在 v0.1 接入 (2026-04-20 d2d1c35), §3.1 已具体化
  - 任一 mode 切换发生后补 audit trail
  - alpha_evidence_gate v0.2+ 引入新硬指标时同步 §3.1
- **文档所有权**: governance layer, 改动需符合 "重大改动前必须备份+设计+获批准" 纪律 (CLAUDE.md §7)

---

## 8. 修订记录

### 2026-04-23 — 语义勘误

**触发**: 用户指出原文档 `baseline_only = reference_only = 完全不下单` 的核心断言与代码实际行为不符.

**验证**: 全量审阅:
- `aats/services/decision_engine/target_position.py:688-755` — `_target_quantity_baseline_only()` 基于 baseline 计算 target_qty, 不强制返回 0
- `aats/services/decision_engine/target_position.py:2007-2016` — `_decision_outcome` 中 `final_target_qty=target_qty` 直接传入, 不因 mode 改写
- `aats/services/execution_engine/` + `aats/services/execution_control/` — **零处**检查 `reference_only` / `baseline_only`, 不存在执行层的 mode 硬拦截

**结论**: `baseline_only` 的真实语义 = 不调用 AI 参与决策, 仅基于 baseline 走完整决策 + 完整下单流程. `decision_authority="reference_only"` 是标签字段, 不是执行拦截关卡.

**改动范围**:
- 本文档: §1 "硬事实", §2.1 authority_map 注释, §2.2 表格 `baseline_only` 行, §2.3 误区→勘误改写, §4.1 UI 文案, §4.2 Grafana 告警描述, §5 session 结论校对
- `docs/governance/p0b_observability_implementation_spec_2026_04_20.md` — §1.2, §2.1, §2.2, §2.3 的 "baseline_only = 不下单" 描述
- `docs/governance/frozen_parameters.md` — §4 快照冻结理由措辞
- `aats/api/static/dashboard-shell.html` / `shell-renderer.js` / `app.css` — UI 顶栏 badge 与 modal 文案
- `deploy/wsl2-dev/grafana/provisioning/alerting/rules.yml` — 删除 `sev2-runtime-baseline-has-orders` 告警 (误报源)
- `tests/unit/test_grafana_runtime_mode_observability.py` — 对应测试删除
- `tests/unit/test_orders_submitted_mode_label.py` — docstring 调整 (metric label 本身仍保留, 供 sev3 告警使用)

**不改动的部分** (代码本身是对的):
- `target_position.py` 的 `_target_quantity_baseline_only()` / `_decision_outcome()` / `authority_map`
- 执行引擎 (本就不拦 mode, 符合真实语义)
- `aats_orders_submitted_total{mode=...}` metric (label 本身有用, sev3 告警需要)

**已批准**: 用户 2026-04-23
