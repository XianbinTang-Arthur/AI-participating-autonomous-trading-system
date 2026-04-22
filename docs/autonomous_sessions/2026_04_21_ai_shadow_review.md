# 2026-04-21 · Phase 1 · AI Shadow 数据回顾

> **结论先行（30 秒）**：**没有 AI 历史数据可以分析**。系统当前
> `ai_operating_mode: baseline_only`，这个模式下 AI 路径**完全 short-circuit**
> （`should_attempt_assessment()` line 695 立刻返回 False），连 shadow 都不跑。
>
> 您的成本纪律是彻底的 —— 不是"以为关了其实还在烧钱"。
>
> 本文档改为提供 **"如果要重启 AI，成本-收益决策框架"**。

---

## 为什么没有 AI 数据

代码路径（`aats/services/ai_service/inference.py:694-697`）：

```python
def should_attempt_assessment(self) -> bool:
    if self.settings.ai_operating_mode == "baseline_only":
        return False   # ← 立刻返回，不进 provider call
    return self.effective_operating_mode() != "baseline_only"
```

`orchestrator.run_cycle()` 里：
```python
if self.ai_service.should_attempt_assessment():
    ai_assessment = await self.ai_service.assess(...)   # ← 不执行
```

Shadow assessment (`_maybe_record_shadow_assessment()` line 505) 只在 AI
assessment 流程**内部**被调用。主路径跳过 → shadow 也跳过。

`event_store` 里 37 种 event type，**没有一个以 AI/Shadow 开头**。确认过。

---

## 如果要打开 AI，您要看到什么

### 成本现实（基于当前流量）

| 指标 | 当前观测值 |
|------|-----------|
| 决策频率 | **2.46 次/分钟**（最近 24h 共 3538 个 DecisionContext） |
| 折合每天 | ~3545 次决策 |
| 折合每月 | ~106k 次决策 |

**当前设置** (`aats/bootstrap/settings.py:254-258`)：
```python
ai_provider = "disabled"        # ← 当前值
ai_model_name = "gpt-4o-mini"   # 默认
ai_timeout_seconds = 5.0
```

### OpenAI 成本估算（粗糙）

gpt-4o-mini 的公开价格：
- input: $0.15 / 1M tokens
- output: $0.60 / 1M tokens

每次 AI 决策 assessment + shadow 估算用量（需实测，这里假设）：
- prompt ≈ 2000 tokens，response ≈ 500 tokens（主 assessment）
- shadow 再来一次（相当于 × 2 如果 `ai_shadow_mode_enabled=True`）

**单次决策**：
- input: 2000 × 2 = 4000 tokens → $0.0006
- output: 500 × 2 = 1000 tokens → $0.0006
- **合计 ~$0.0012 / 决策**

**每月成本**：
- 106k × $0.0012 = **$127/月**

**账户权益 $393.73** → 每月 AI 成本相当于**账户的 32%**。

> 粗算不严谨：实际 prompt size 我没测；`ai_timeout_seconds=5s` 意味着
> 超时后不计费；gpt-4o-mini 比 gpt-4o 便宜 20 倍。如果换 gpt-4o 成本
> 再乘 20 → $2540/月 = 账户 645%。

### 盈亏平衡：AI 要多"聪明"才值回来？

设 AI 使每次决策的 expected edge 提高 Δ bps。当前观测每次决策名义值
约 $300（0.001 BTC × ~$300k）。

**每月 AI 收益 = 交易频率 × 名义 × Δ bps**

即使 AI 让每次决策平均多赚 1 bp ($0.03/次)：
- 106k 次 × $0.03 = $3180/月 ← 看起来能覆盖成本
- 但实际：**大部分决策 hold 不交易**；baseline 4 天成交 25 次
- 4 天 25 次 → **每月 ~190 次成交**
- 190 × $0.03 = **$5.7/月** 无法覆盖 $127 AI 成本

**真实 break-even**：AI 要让**实际成交**每笔多赚 $0.67（≈ 22 bps / 0.22%），
才能给 106k 次决策的 AI 成本买单。这意味着 AI 得在 30%+ 的决策里**比 baseline
明显更准**。

### 建议的重启 AI 实验路径（当您决定做时）

**Stage 0 · 测实际成本**（低成本实验）
1. 小窗口（1 天）打开 `ai_operating_mode: ai_assisted` + `ai_shadow_mode_enabled=true`
2. 只交易 1 个 symbol，流量保持当前
3. 跑完关掉，看 OpenAI 账单实际花多少

**Stage 1 · 收 shadow 数据（只观察不下单）**（中等成本）
1. 开 `ai_assisted` 让 AI 跑假设决策但 baseline 仍做主
2. 积累 1 周 shadow_evaluation 数据
3. 用 `AIShadowEvaluation` schema 里的 `shadow_outperformed` / `net_pnl`
   对比 baseline
4. **这才是"AI 能不能赚"的第一手数据**（当前都没有）

**Stage 2 · 让 AI 真决策小额**
1. 只在 AI confidence > X 时让 AI 覆盖 baseline
2. 位置规模是 baseline 的一半
3. 观察 P&L

**Stage 3 · 全量**

每一步都有 rollback（改回 `baseline_only` 就行），每一步产生可审计数据。

---

## 这份文档的 takeaway（给您 3 句）

1. **AI 彻底关着** — 没有数据、没有烧钱、没有 shadow。您做对了。
2. **以 $393.73 账户规模 + 当前 2.5 次/分钟 决策频率**，gpt-4o-mini 即使
   只做 shadow 也要吃掉账户 ~32%/月。**当前账户规模不适合长期开 AI**。
3. **要证明 AI 值得开**，最小成本实验是 Stage 1：短窗口（1 天）开 shadow
   抓数据，关掉看账单，再决定是否扩大。

---

## 下一步

Phase 2 立刻启动：分析 baseline 信号为何 `expected_net_edge = -7 bps`。
这是**更直接的盈利杠杆** —— 不花 OpenAI 钱、就能看清"是信号弱还是成本
估得太保守"。
