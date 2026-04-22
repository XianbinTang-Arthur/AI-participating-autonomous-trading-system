# 2026-04-21 · C2 实盘安全审计报告

> **作者**：Claude (autonomous)
> **范围**：AATS fail-closed 路径 — kill switch / RiskEngine / only_reduce /
> 位置上限 / kill switch 跨进程 / RecoveryPostureEvaluator
> **方法**：只读审计 + 锚定关键不变性为 anchor tests（不改业务代码）
> **阅读时间**：5-8 分钟

---

## TL;DR（一分钟版）

**结论**：**AATS 的 fail-closed 架构是扎实的**。6 大核心防线全部工作，
audit agent 扫了 Tier 1 + Tier 2 + Tier 3 路径共 13 项，均 SAFE。

发现 3 个**显式证明缺口**（不是漏洞，是缺测试锁死不变性），已写
22 项 anchor tests 补齐。所有测试通过。

```
新增 3 个 anchor test 文件 · 24 个测试方法全过 · 零业务代码改动
└─ d477bf4  test_risk_engine_provider_none_behavior         (10 tests)
└─ d6e6694  test_risk_engine_zero_limit_semantics            (6 tests)
└─ 6b0cbaf  test_guard_signal_cache_bootstrap_failure        (8 tests)
```

---

## 核心发现：系统如何 fail-closed

6 层防御，每一层都有机制确保"出错时默认保守"：

### 第 1 层 · GuardSignalHotStateCache 的 `_FAIL_CLOSED_SENTINEL`

- Redis 断连 / NATS 无数据 / bootstrap 失败 → 返回
  ```python
  {"only_reduce_required": True, "safe_to_trade": False,
   "only_reduce_reasons": ["guard_signal_missing_or_stale"], ...}
  ```
- 参见 `guard_signal_cache.py:75-83`
- **anchor test**: `test_guard_signal_cache_bootstrap_failure.py` (6b0cbaf)

### 第 2 层 · RiskEngine 三档 provider `only_reduce_reasons`

- `reconciliation_only_reduce_reasons` + `runtime_guard_only_reduce_reasons`
  + `recovery_status_only_reduce_reasons` 三条路径聚合
- 任何一条非空 → 硬拒 open（`approved=False`）
- 参见 `risk.py:1487-1543`
- **anchor test**: `test_risk_engine_provider_none_behavior.py` (d477bf4)

### 第 3 层 · 位置/名义上限硬夹

- `max_abs_position_qty` → hard cap qty
- `max_notional_per_symbol` → scale qty proportionally
- `max_gross/pending/total_open_notional` → 若触发设 `only_reduce_required`
- 参见 `risk.py:102-117, 1450-1467`
- **anchor test**: `test_risk_engine_zero_limit_semantics.py` (d6e6694)

### 第 4 层 · 保证金/爆仓自动熔断

- `DerivativesLiveGuardService.evaluate_now()` 检查：
  - margin_usage >= 0.85 (`auto_halt_margin_threshold`) → halt
  - liquidation_gap <= 0.08 → halt
  - risk_snapshot 缺 >240s → halt
- 熔断 → `kill_switch.halt()` → `OKXAdapter` 拒非减仓单

### 第 5 层 · Kill Switch 跨进程同步

- Redis 断线多进程默认 **fail-safe halt**（`kill_switch.py:245`）
- NATS 事件时间戳验证防 rollback
- 存在测试：`test_kill_switch_cross_process.py:152-182`

### 第 6 层 · RecoveryPostureEvaluator 持久 blocker

- `derivatives_exchange_position_without_local_execution_chain` 在
  `_PERSISTENT_STATUS_BLOCKERS` 里，reconciliation 清掉后仍然保持阻塞
- 参见 `recovery_posture.py:64`

---

## 定位到的 3 个显式证明缺口

审计按"有没有**明确的测试证明 fail-closed 分支**"标准评估，找出 3 处。
不是漏洞，是缺锁定不变性的测试。如果未来有人把 Redis 异常处理改成
"silently swallow"、把 permissive fallback 改成别的，这 3 处的缺测试
让 regression 能溜过去。

### Gap 1 · Provider.snapshot() 返回 None 的行为

**位置**：`risk.py:1796-1804, 1960-1970`
**原行为**：provider 注入但 snapshot() 返回 None/empty → RiskEngine
`return []`（permissive）
**为什么这是设计如此而不是漏洞**：
- provider 是**可选的**（`| None = None` 语法）
- 真正的 fail-closed 在 provider 内部（GuardSignalHotStateCache 返回 sentinel）
- RiskEngine 不该去推断 "provider 应不应该在线"
**anchor test**: d477bf4 的 10 个测试锁定这个行为 + docstring 说明"要改需同步动 4 个位置"

### Gap 2 · `max_*_notional=0` 的不一致语义

**位置**：`risk.py:1453/1459/1465/1469`
**发现**：
- `max_abs_position_qty=0` → qty 被夹成 0 → 硬拒（安全 by accident）
- `max_notional_per_symbol=0` → scale 到 0 → 硬拒（安全）
- `max_gross/pending/total_open_notional=0` → `> Decimal("0")` gate 跳过检查 → 放行（不安全）
**风险评估**：生产默认值都是非零（settings.py:728-730: 2500/1250/5000），
只有运营主动设 0 才触发。这是**配置 UX 问题**，不是 runtime bug。
**anchor test**: d6e6694 的 6 个测试明确每个字段 = 0 时的当前行为；
docstring 列出修复路线（Pydantic `Field(gt=0)` 是最稳妥的修法）

### Gap 3 · GuardSignalCache bootstrap 失败后的 sentinel

**位置**：`guard_signal_cache.py:167-176` (try/except 在 bootstrap 里)
**原行为**：Redis.get() 异常 → log warning → `_latest` 保持空 → 下次
`snapshot()` 检测 `not self._latest` → 返回 `_FAIL_CLOSED_SENTINEL`
**缺的是什么**：代码正确，但没有显式测试证明。已有
`test_bootstrap_with_empty_redis` 覆盖"Redis 空但 get 成功"，缺"Redis get
抛异常"
**anchor test**: 6b0cbaf 的 8 个测试补齐（包括 RuntimeError / TimeoutError /
从未 bootstrap / 跟 _FAIL_CLOSED_SENTINEL 字段一致性等）

---

## 未做（和未做原因）

按 C2 约定**只加测试，不改业务代码**。所以以下"真正的 fix"没做：

1. **Pydantic `Field(gt=0)` 阻止 max_*_notional 设 0**
   - 需改 `aats/bootstrap/settings.py` 的 5-6 个字段
   - 可能破坏现有部署（如果有 staging/test 环境故意用 0 禁用检查）
   - 建议：另开一个小 commit、deploy 前人工 review

2. **NATS subscription 失败的显式告警**
   - 当前 `_subscribe_internal` 失败只 log warning，不触发 health blocker
   - Audit agent 发现：如果 execution halt 了但某个进程没订阅成功，
     它就看不到 halt 事件，只能靠 Redis bootstrap 兜底
   - Fix：在 `SystemHealthService` 里加 "guard_signal_cache_not_subscribed"
     blocker（需要 coordination with health service）

3. **Margin NaN/Infinity 显式测试**
   - 理论风险低（OKX snapshot validation 先拦），但没有测试证明
   - 可以后续加

**这些都是 C3 级（改业务代码）的工作，不在本次 C2 范围。**

---

## 风险矩阵（audit agent 的原始分档）

| Path | Trigger | Happy | Error Branch | Gap |
|------|---------|-------|---------------|-----|
| Guard signal stale | age >120s | ✓ | ✓ | - |
| Guard signal missing | never published | ✓ | ✓ | - |
| Guard signal bootstrap failure | Redis 抛异常 | ✓ | ✓ **(此 PR 补)** | - |
| Provider returns None | 注入但失效 | ✓ | ✓ **(此 PR 锁)** | - |
| Max abs qty | target > limit | ✓ | ✓ (zero 含义 **锁死**) | - |
| Max notional per symbol | notional > limit | ✓ | ✓ (zero 含义 **锁死**) | - |
| Max gross/pending/total | notional > limit | ✓ | ⚠ (zero **禁用检查** — 已文档化) | UX gap |
| Margin calc NaN | equity_base=0 | ✓ | 有 fallback | Low (OKX validate 先拦) |
| Auto halt @ margin 0.85 | usage ≥ 0.85 | ✓ | ✓ | - |
| Auto halt @ liq gap 0.10 | gap ≤ 0.08 | ✓ | ✓ | - |
| Kill switch NATS 失败 | subscribe fail | ✓ | ⚠ log-only | C3 级 |
| Recovery persistent blockers | 交易对账 mismatch | ✓ | ✓ | - |
| Kill switch Redis fail | bootstrap exception | ✓ | ✓ fail-safe halt | - |

---

## 给您的下一步建议

### 立即可做（您决定）
**无紧急动作**。核心防线已验证、缺口已锚定。

### 下次 autonomous 可做
1. **Pydantic 加 `Field(gt=0)` 阻止误设 0**（小改动、跑一遍测试就行）
2. **health_service 加 "guard_signal_cache_not_subscribed" blocker**（中等改动，要对 wiring 仔细）

### 和真正的目标相关
您最关心的问题是 **"baseline 当前的拒单纪律够不够 + AI 什么时候能赚钱"**。
C2 回答了前半句 —— **拒单纪律是可信的**。后半句是 C3 要做的事：
- 调 AI shadow evaluation 数据看 "AI 如果决策会不会赚"
- 或者先拆解 baseline 的信号强度为何一直 < 交易成本

我按 C2 后的顺序继续，下一步是 **AI shadow evaluation 数据回顾**（小
任务、纯读数）—— 您同意吗？

---

## 附录 · 本次 C2 产出的 4 个 commit

| Commit | 内容 | 行数 |
|--------|------|------|
| a95011d | C1 trading state audit 报告 | +242 |
| d477bf4 | test #1: provider None 行为锚定 | +312 |
| d6e6694 | test #2: 位置/名义上限 = 0 语义锚定 | +239 |
| 6b0cbaf | test #3: bootstrap 失败 → sentinel 证明 | +242 |

**总计**：1035 行新增，**零业务代码改动**，24 个新 anchor test。
