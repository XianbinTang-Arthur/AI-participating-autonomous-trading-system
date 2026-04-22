# Round 3 · Paper Trading for Non-AI Strategies · 设计文档

> **生成**：2026-04-22 Plan agent design + PM 决策
> **状态**：设计定稿，进入 Phase 1 实现
> **前置**：RDP 审计 (`2026_04_22_rdp_infrastructure_audit.md`) 里的 Gap #1

---

## 0. 问题陈述

- `ai_shadow_mode_enabled` 工作得很好：AI 决策的 shadow path 完整（`AIShadowDecision` + `AI_SHADOW_DECISIONS` topic + `Phase1ExecutionShadowService`）
- `strategy_family_independent_shadow_mode_enabled` **只是一个显示用的 metric**，没有任何代码根据它跑 shadow
- 用户想在调 `independent` 策略（entry_threshold、cost model）**之前**，用真实实盘流量**并行**跑候选版本，观察"如果当时决策不同会怎么走"

---

## 1. 架构决策（PM 已拍板）

### 1.1 Schema：新 `StrategyFamilyShadowDecision`
`AIShadowDecision` 字段紧耦 AI 场景（baseline_* / ai_shadow_* / ai_assessment_ref），扩展会扭曲语义。新 schema 干净，共享 `ShadowActionType` Literal。

### 1.2 Service：新 `StrategyFamilyShadowService`
`Phase1ExecutionShadowService` 是**执行层**镜像，不评估策略。Paper trading 是**决策层**，mirror `TargetPositionEngine.build_shadow` 风格。

### 1.3 Hook 点：`orchestrator._run_cycle_body` 里，live target 生成后 & AI shadow 前
✅ PM 决策（Q1 答案）：**Inside run_cycle**。
- 理由：cycle 预算 30s（LF-003 保证），shadow 加 5-50ms 可忽略
- 同 decision_id / span / context，Jaeger 关联清晰
- fire-and-forget 会引入 correlation 复杂度，回报不值

### 1.4 候选配置方式：Option 1 · 新 `paper_trading_shadow_candidates` 设置
✅ PM 决策（Q2 答案）：**Option 1**，不复用 `StrategyProfile`。
- Option 1 purpose-built for paper trading，快速落地
- Option 2 reuse profile 基建是 "order of magnitude more work"（Plan agent 原话）
- 如果将来要 profile 集成，schema 设计允许 clean migration（candidate_id + overrides + config_version_hash 都在）

### 1.5 PnL：Phase 1 纯决策，不算模拟 fill
✅ PM 决策（Q3 答案）：**不算 PnL**。
- 决策层分歧率（agreement / override / reverse）本身就能告诉我们 "candidate 有没有意义"
- 决策层如果和 baseline 完全一致，PnL 模拟是白跑
- fill 模型选择（mid / mid+slippage / VWAP）应该独立思考，不 freeze 在 Phase 1
- Phase 2 或 RDP Gap #2 再接 PnL

### 1.6 热切换：Phase 1 接受重启
✅ PM 决策（Q4 答案）：**接受 process restart 切 shadow**。
- Paper trading 是非关键路径，restart 短暂 1-2 min 可接受
- 用户迭代候选的 velocity 没快到需要秒级热切换
- 节省 Phase 1 的 operator_actions 扩展工作

---

## 2. 数据模型

### 新增 `aats/schemas/strategy_shadow.py`

```python
class StrategyFamilyShadowDecision(SchemaBase):
    shadow_decision_id: str = Field(default_factory=lambda: new_id("strat_shadow"))
    decision_id: str
    symbol: str
    timeframe: str
    # Candidate identity
    candidate_id: str                          # "independent_low_threshold"
    candidate_family: StrategyFamily
    candidate_overrides: dict                  # for traceability
    candidate_config_version: str              # sha256 of overrides
    # Live side
    baseline_family: StrategyFamily
    baseline_target_qty: Decimal
    baseline_action: str
    # Shadow side
    shadow_target_qty: Decimal
    shadow_action: str
    # Divergence
    would_override_baseline: bool
    shadow_action_type: ShadowActionType       # reuse from ai_shadow
    reason_codes: list[str] = Field(default_factory=list)
    # For Phase 2+ PnL replay
    reference_price: Decimal | None = None
    reference_spread_bps: float | None = None
    market_snapshot_ref: str | None = None
    created_at: datetime
```

---

## 3. NATS Topics

```python
# aats/events/topics.py
STRATEGY_FAMILY_SHADOW_DECISIONS = "strategy.family_shadow_decision"
STRATEGY_FAMILY_SHADOW_EVALUATIONS = "strategy.family_shadow_evaluation"
```

- event_store 持久化：**是**（和 `AI_SHADOW_DECISIONS` 对齐）
- 分区 key：`symbol`
- audit 集成：`DecisionAuditRecord` 加 `strategy_family_shadow_decision_refs` 字段

---

## 4. Rollout Plan（我执行）

### Phase 1.1 · Schema + Topics + Config（1 commit）
- 新文件 `aats/schemas/strategy_shadow.py`
- 加 2 个 topic 常量
- `settings.py` 加 `paper_trading_shadow_enabled: bool = False` + `paper_trading_shadow_candidates: tuple[dict, ...] = ()`

### Phase 1.2 · Service 实现（1 commit）
- 新文件 `aats/services/strategy_engines/paper_trading_shadow.py`
- 核心方法 `build_shadow(context, baseline, target, candidate_overrides) -> StrategyFamilyShadowDecision`
- 内部：`settings.model_copy(update=overrides)` → 调 `IndependentFamilyEngine.evaluate` 或其他 family evaluator → 包成 shadow decision
- **异常隔离**：整段 try/except，失败 log warning 但不抛（绝不影响 live）

### Phase 1.3 · Hook in run_cycle + audit（1 commit）
- `orchestrator._run_cycle_body` 加 shadow block（live target 之后、AI shadow 之前）
- guard by `settings.paper_trading_shadow_enabled and candidates非空`
- 遍历 candidates，每个 `publish_model(STRATEGY_FAMILY_SHADOW_DECISIONS)`
- `audit.py` handler 扩 `strategy_family_shadow_decision_refs`

每 phase 独立 commit + 独立回归测试。

### Phase 2 · Tracker + Evaluator（下次 session）
- `StrategyFamilyShadowTracker` ring buffer
- `STRATEGY_FAMILY_SHADOW_EVALUATIONS` 窗口聚合
- （Phase 2 再考虑 cheap PnL）

### Phase 3 · UI / Grafana（更下次）
- `OperatorQueryService` 查询端点
- Grafana dashboard panel

---

## 5. 风险 & Mitigation

| 风险 | 缓解 |
|------|------|
| Shadow 异常打死 live path | 整个 shadow block 包 try/except，任何异常→ warning log + metric，绝不 re-raise。顺便 back-port 同样保护给 AI shadow 路径（单独 commit） |
| Shadow 运行超时影响 cycle 预算 | 30s cycle 预算（LF-003），shadow 典型 5-50ms。实测超 100ms → 降级到 fire-and-forget（但不在 Phase 1） |
| Candidate overrides 泄露进 live settings | 强制用 `settings.model_copy(update=...)`（Pydantic 不可变拷贝），anchor test 验证 live settings hash 不变 |
| 多 symbol 并发 | Phase 1 串行在 `run_cycle` 里，`_timeframe_locks` 串行化，无并发问题 |
| Shadow config 写错导致 candidate 永远拒绝下单 | 不影响 live，只是 shadow 数据无意义；`would_override_baseline=False` 会持续显示 candidate 不动 |

---

## 6. 非目标（NOT 做）

- ❌ 模拟真实下单 / 提交到 OKX
- ❌ AI shadow 的功能变更（那条路独立，本工作不动）
- ❌ 历史 backtest replay（RDP Gap #2，独立 scope）
- ❌ Shadow 胜出后自动切 live（需要 `strategy_profile_auto_control_enabled` 的治理流程，out of scope）
- ❌ 跨 family shadow（Plan agent 里 Pattern B，等有 v2 family 再说）

---

## 下一步

立刻开始 **Phase 1.1**（schema + topics + config）。每步独立 commit，方便您 review + 随时 rollback。
