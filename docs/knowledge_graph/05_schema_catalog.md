# 05 · Schema 目录（系统的"名词"）

> **历史快照**：2026-04-21 版本；当前 schema 清单以 `aats/schemas/` 和完整代码审查说明为准。

> **生成于 HEAD=待更新** · 2026-04-21
> **内容**：`aats/schemas/` 下的核心 pydantic 模型，按领域分组

---

## TL;DR

AATS 的数据契约基本都在 `aats/schemas/` 下。所有模型继承 `SchemaBase`（含
`schema_version` + `created_at`）。跨进程的 event 都装进 `EventEnvelope` 里。

---

## 按领域索引

### 决策域（`decision.py`）

| 模型 | 含义 | 流向 |
|------|------|------|
| **DecisionContext** | 一次决策的完整输入快照（54 字段：市场 / 组合 / feature / 健康 / blockers） | → DECISION_CONTEXTS |
| **BaselineAssessment** | 规则算法的评估结果（regime / direction_bias / composite_alpha_score） | → BASELINE_ASSESSMENTS |
| **AIMarketAssessment** | AI 评估（不在当前 baseline_only 模式下产生） | → AI_ASSESSMENTS |
| **AIDecisionIntent** | AI 建议的动作 | → 进入 DecisionOutcome |
| **DecisionOutcome** | 最终决策（含 decision_source / final_action / risk_capped） | → DECISION_OUTCOMES（⚠️ 实际 never 发布，见 LF-006） |
| **PositionTarget** | 目标仓位 + 紧迫度 + 策略元数据（48 字段） | → POSITION_TARGETS |

---

### 执行域（`execution.py`）

| 模型 | 含义 | 流向 |
|------|------|------|
| **OrderIntent** | 单个下单意图（含 reduce_only 语义） | → ORDER_INTENTS |
| **LegOrderIntent** | hedge 模式下的多腿分解 | OrderIntent 的子组件 |
| **ExecutionPlan** | 预下单的执行策略（style / urgency / slippage 容忍） | 执行进程内 |
| **OrderState** | 订单生命周期快照（见 [04_state_machines.md](04_state_machines.md)） | Postgres + Redis + ORDER_UPDATES |
| **FillEvent** | 单笔成交 | → FILL_EVENTS（critical topic） |
| **OrderObligation** | 订单占用的资金/保证金 | → OBLIGATION_UPDATES |

**⚠️ 三路 reduce_only 语义**（`reduce_only` flag + `leg_action` + `position_intent`）有已知复杂度，见 `execution.py:157-175` + 注释 "P1-13"。

---

### 组合与 PnL 域（`portfolio.py`, `fill_outcome.py`）

| 模型 | 含义 |
|------|------|
| **PortfolioSnapshot** | 账户持仓快照（balances / positions / realized_pnl / equity） |
| **Position** | 单品种仓位 |
| **InstrumentPositionState** | 多腿聚合视图（net + long + short） |
| **FillOutcomeRecord** | 一笔 fill 对仓位 / PnL 的影响 |
| **PortfolioBalanceDelta** | 单笔余额变动审计 |
| **FundingFeeRecord** | 衍生品资金费 |

---

### 对账域（`reconciliation.py`）

| 模型 | 含义 |
|------|------|
| **ReconciliationReport** | 交易所 vs 本地状态对比总报告 |
| **ReconciliationFinding** | 单条发现（含 severity_class / halt_required / only_reduce_required） |
| **BaselineGenerationRecord** | 启动时账户 baseline 快照 |
| **ExchangeAckWatermark** | HLC 水位标记（replay 恢复用） |

---

### 治理域（`governance.py`）

| 模型 | 含义 |
|------|------|
| **PolicyDecision** | 策略层过滤（symbol 白名单 / execution style 允许） |
| **RiskDecision** | 风险决策（approved / capped_qty / only_reduce_required） |
| **DerivativesExposureMetrics** | 长短名义、净杠杆、维持保证金 |
| **LegRiskConstraint** | 每腿独立的只减仓约束 |

---

### 系统域（`system.py`）

| 模型 | 含义 |
|------|------|
| **HealthSnapshot** | 组件健康快照（含 mode / operating_state / blockers） |
| **ComponentHealth** | 单子系统状态 |
| **RuntimeModeState** | 运行模式全景（profile / environment / policy） |
| **RecoveryStatus** | 系统恢复状态（safe_to_trade / resume_eligible 等） |
| **RecoveryBundleSummary** | 策略 bundle 层面的孤儿执行恢复 |
| **IndependentRecoverySnapshot** | 单腿（long/short）独立的 recovery 状态机 |

---

### 策略运行域（`strategy_runtime.py`）

| 模型 | 含义 |
|------|------|
| **StrategyLegIntent** | sleeve 要求的仓位增量 |
| **StrategySleeveIntent** | sleeve 是否 ready / active / blocked |
| **StrategySleeveAutomationDecision** | 自动化开关 + budget + scale |
| **SleeveBudgetProfile** | 单个 sleeve 的资金预算上限 |
| **StrategyBookRuntimeState** | 腿的运行时诊断（expected vs actual fill） |
| **StrategyCandidate** | 可选 sleeve 选项（baseline 推荐 vs AI 推荐） |
| **StrategyCoordinatorSnapshot** | 多 sleeve 分配 + 冲突解决 |

---

### 交易所域（`exchange.py`）

| 模型 | 含义 |
|------|------|
| **ExchangeAccountSnapshot** | 从交易所 API 读的完整账户（balances / positions / orders / fills / instruments） |
| **ExchangeBalance** | 单币种余额 |
| **ExchangePosition** | 交易所侧报告的仓位 |
| **ExchangeFill** | 交易所成交记录 |
| **InstrumentMetadata** | 品种元数据（tick_size / contract_value / max_leverage） |
| **ExchangeAccountRiskSnapshot** | 保证金 / 爆仓状态 |

---

### AI Shadow 域（`ai_shadow.py`）—— 当前关闭

| 模型 | 含义 |
|------|------|
| **AIShadowDecision** | 如果让 AI 决策会怎样（当前 baseline_only → 不生成） |
| **AIShadowEvaluation** | Shadow vs baseline 窗口对比（无数据） |
| **AIDegradationEvent** | AI provider 失败 + auto downgrade |

---

### 通用域（`common.py`）

| 模型 | 含义 |
|------|------|
| **EventEnvelope** | 所有 NATS event 的外层壳（event_id / event_type / topic / key / payload / trace_context） |
| **SchemaBase** | 所有 model 的基类（schema_version / created_at） |

---

## 可疑模式（已抄到 [10_latent_findings.md](10_latent_findings.md)）

1. **372 个 Decimal 字段没有 `Field(ge=0)` 验证** — 允许负的 position_qty / price / balance 悄悄存下来
2. **`DECISION_OUTCOMES` topic 声明但从不发布** — 已在 LF-006 记录
3. **Legacy 字段未清理** — `legacy_reference_qty`、`automation_state` 带 deprecated 注释但仍然活跃
4. **`reduce_only` 三路语义复杂** — OKX 视角、obligation 视角、legacy 视角可能不一致（P1-13）
5. **`dict[str, Any]` catch-all** — `order_diff` / `fill_diff` / `balance_diff` / `risk_budget_state` / `threshold_snapshot` 都是无 shape 的字典
6. **过大的 monolithic 模型** — DecisionContext（54 字段）、PositionTarget（48）、OrderState 等

---

## 使用本目录

- 新 event type 加进 NATS：找领域 → 在对应 schema 文件加模型 → 继承 `SchemaBase`
- 看不懂某个 NATS 消息：查 [02_data_flow.md](02_data_flow.md) 的 Topic 表找 payload model → 回这里看字段
- 改现有模型：先看 `schema_version`，考虑是否要 bump；replay 兼容性要考虑
