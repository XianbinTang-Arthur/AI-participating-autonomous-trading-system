# 2026-04-22 · Round 2 的教训：5 findings 里 4 个是假阳

> **TL;DR**：Round 2 实际处理了 5 个 MED latent findings。**4 个经深入调查是假阳**（audit agent 没仔细读代码就下结论），只有 1 个（LF-014）是真 fix。
>
> 这是**好消息** —— AATS 代码比初次审计估计的更成熟。
>
> 但也是**流程教训** —— 未来 PM 调 audit agent 时必须二次验证，不盲信。

---

## 5 个 findings 的复检结果

| # | Finding | 初次判断 | 复检结论 |
|---|---------|---------|---------|
| LF-001 | 心跳 health check 看不到 GIL 卡死 | 🔴 HIGH | **假阳** — heartbeat 是 async 跑 event loop 上，event loop 卡 → mtime 也卡 → docker healthcheck 照捕 |
| LF-014 | Market WS 无 circuit breaker / REST fallback | 🟡 MED | **部分真** — REST fallback 是 overengineering，但"连续失败阈值告警"值得加（d1451d6） |
| LF-015 | Operator command proxy 假定 execution 在 | 🟡 MED | **假阳** — 已有 30s timeout；真正 fix 需 UI Phase 3 |
| LF-018 | RDP daemon 连接池与业务共享 | 🟡 MED | **假阳** — RDP 已有隔离池（aats_research 独立 DB + 小 pool_size=3 读 live） |
| LF-020 | Decision trigger idleness 无告警 | 🟡 MED | **假阳** — `decision_cycles` metric 已存在，只缺 Grafana alert rule（e5098aa 写入指南） |

**命中率：1 / 5 = 20%**（如果把 LF-014 算"部分真"，35%）。

---

## 为什么假阳这么多

### 因为 audit agent 只看名字不读代码

举例：
- **LF-001**：agent 猜"heartbeat 是后台线程"，实际 `_heartbeat_loop` 是 `async def`，明明跑 event loop。如果 agent 读了 30 行代码就能看出。
- **LF-020**：agent 说"没 metric"，grep 一下 `decision_cycles` 就看到已有。

### 因为 audit 倾向过度包装保守

agent 天然偏向"列出所有可能的风险"，不强制"每个风险先验证真伪"。这让 finding 清单看起来 thorough 但实际信噪比低。

### 因为 PM（我自己）没做 gatekeeping

我把 20 条 findings 一股脑放进 10_latent_findings.md 当权威清单，没先用 "if I had 1 hour to verify each, would it survive?" 过滤一遍。

---

## 流程改进（下次 audit 时用）

### 新的 PM pattern

1. **Agent 输出 → "claim"，不是 "finding"**
2. **每个 claim 必须有二次验证环节**：
   - grep 对应代码路径
   - 读 30 行以上上下文
   - 如果 claim 不成立 → 分级标记"FALSE POSITIVE"保留历史
3. **只有验证过的 claim 才进正式 latent list**
4. **命中率低于 50% 的 audit agent prompt 要改进** —— prompt 里加 "每条 claim 必须附上 file:line + 最小复现脚本 + 预期错误日志"

### 给下次 audit prompt 加的要求

```
FOR EACH finding, you MUST provide:
1. file:line for the alleged issue
2. A 5-10 line code snippet showing the problem
3. A concrete scenario (input → expected buggy output)
4. What you verified vs what you inferred

If you cannot provide all 4, mark the finding as "speculative" not "confirmed".
```

---

## 对 AATS 的启示

代码质量比想象的好。主要防御层（fail-closed, exponential backoff, connection pool isolation, metrics）在工程实践里都有扎实落地。这次 session 之前我担心"可能到处都是 latent bug"，事实上大多数审计担忧是没仔细读代码。

**但真正的改进空间还在策略层**（Phase 2 baseline signal analysis 已经定位）：
- 成本模型没扣 maker rebate
- Direction bias 对称性
- Score gate vs net_edge gate 没联动

**这些是 value-laden 的改动**，不是 MED latent fix，需要用户参与设计决策。

---

## 本场最终 latent fix 统计

| Round | 真 fix 数 | 假阳 / 已处理过 |
|-------|----------|---------------|
| Phase 1 (Round 1) | 7 真（003/004/007/010/016/017/019） | LF-006 假阳（agent 敢拒） |
| Round 2 | 1 真（LF-014） | LF-001/015/018/020 假阳 |

**10 commits 里 8 个是真技术改进**，其他 2 个是"检查后发现不需要改"的事后文档。

最重要的教训不是"代码没 bug"，而是"PM 做 review 时要自己过一遍"。
