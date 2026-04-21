# post_only_with_timeout_fallback Execution Mode — 设计提案 & §3 解冻申请

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../project_positioning.md)。

> **分类**: 执行路径增强 (execution-layer), **不是** alpha 提案.
> **Governance 触面**: `frozen_parameters.md` §2.3 (fee/cost 模型核心) — 走 §3 解冻流程.

---

## 元数据

| 字段 | 值 |
|---|---|
| 提案 ID | `execution-post_only_exit-2026_04_21` |
| 提案日期 | 2026-04-21 UTC |
| 提案人 | Claude Opus 4.7 |
| Scope | 仅 `close_stale_execution_mode`(非紧急退出); `close_failed_thesis` / `de_risk(execution_health_degraded)` 不改 |
| Symbol | BTC-USDT-SWAP (其他 symbol 同步生效) |
| Governance 触面 | `frozen_parameters.md` §2.3 — **确实触及**, 需 §3 解冻 |
| Alpha gate 是否适用 | **不适用** (理由见 §5.3) |
| 本次目标 | 用户批准后实施；批准前仅停在设计+Evidence plan |

---

## §1 背景与动机

### 1.1 触发背景

2026-04-19 起 30+ 小时调查得到结论 (见 `docs/review/independent_cost_model_audit_2026_04_19.md`):

- Independent family `book_action=close_stale_thesis` 路径，当前配置 `close_stale_execution_mode=bounded_limit` (`configs/strategy_profiles/derivatives_live.yaml:463`)
- **表面**: limit order
- **实际 OKX**: `ordType=ioc` (Immediate-Or-Cancel) → **永远 taker 撮合或取消**，绝不挂单成为 maker
- **成本**: taker 5 bps/side，退出一次独立名义 5 bps
- **H2 修复 (P1-B step 2, 2026-04-19)** 已把 `bounded_limit_ioc → taker` 冻结在 `fee_resolver.py:179-182`，`frozen_parameters.md` §2.3

### 1.2 提案动机

- **非 alpha 动机**: 不是为了"让信号过阈值"。阈值 (`entry_threshold=0.25`, `max_acceptable_cost_bps=7.5`, `min_safe_net_edge_bps=2.0`) 全部不动。
- **执行层动机**: OKX 原生支持 `ordType=post_only` (真 maker, 仅挂单，若跨价立刻被 OKX 拒绝)。**当前代码没有出站路径使用它**，仅入站 fill parser 认识。
- **经济收益**: 退出场景（非紧急）允许 3s 挂单窗口 → 真 maker 撮合 → fee 2 bps (vs taker 5 bps)，理论 3 bps/side 节约。
- **底线约束**: 保守 `fill_rate=0.3` (见 §4.3)，真实数据反驳则走 §8 回退。

### 1.3 不做什么 (红线)

- ❌ **不改**任何 alpha / 决策阈值 (§2.1)
- ❌ **不改** scoring 权重 (§2.2)
- ❌ **不反转** H2 `bounded_limit_ioc → taker` 分类
- ❌ **不适用于** `close_failed_thesis` 和 `de_risk(execution_health_degraded)` — 后者是紧急出口，3s 挂单窗口违背 urgency=high 语义
- ❌ **不假设** post_only 填充率 > 0.3 (未有实盘数据前)

---

## §2 关键事实 (OKX 外部真相 + 代码现状)

### 2.1 OKX post_only 语义

**引用**: OKX V5 API 文档 `POST /api/v5/trade/order` 的 `ordType` 参数说明
- `post_only`: 只做 maker。若订单 submit 瞬间会 match（跨价或在对手挂单内），OKX 立刻拒绝，返回 `sCode≠0`，不进簿。
- 要求 `px` 必须填，`tdMode` 必须填，`tif` 对 post_only 无效（永远按 limit 挂，直到撮合或被取消）。
- 永续合约 maker fee 档位（VIP0）: -0.02% = -2 bps（实际为 rebate 或正付费，取决于账户等级；项目当前 `trade_cost_derivatives_maker_fee_bps=2.0`）。

### 2.2 代码现状 (grep 实证)

| 位置 | 现状 | 与 post_only 的关系 |
|---|---|---|
| `okx_adapter.py:1976` | 注释 `ordType: market/limit/ioc/post_only` | 仅读取路径认识字符串 |
| `okx_adapter.py:2178` | `order_type="limit" if order_type in {"limit", "ioc", "fok", "post_only"} else "market"` | 反序列化时认识；内部表示归一为 "limit" |
| `okx_adapter.py:138-146` `_order_type()` | 只会 emit `market / limit / ioc / fok` | **出站路径缺失 post_only** — 即使 intent 标记了也不会发出 |
| `execution_policy.py:21-56` | mode → execution_style 仅支持 `bounded_limit_ioc / bounded_taker_cap / aggressive_bounded_taker_cap / taker` | 没有 `post_only` 选项 |
| `planner.py:1021-1023` `_bounded_live_limit_price` | buy: ref×(1+offset); sell: ref×(1-offset) | **跨价方向**，与 post_only 所需（挂在己方最优队列）方向相反 |
| `order_manager.py:1215` `cancel_order` | 基于 client_order_id 主动取消 | 基础设施已就位 |
| 所有文件 | **无任何**"N 秒未成交自动取消+重下" 的 orchestration | **新 orchestration 层需要建** |
| `fee_resolver.py:179-182` | `bounded_limit_ioc` → taker (H2) | §2.3 冻结，不动 |

### 2.3 Fee 档位 (perpetual, VIP0)

```
trade_cost_derivatives_maker_fee_bps = 2.0  (configs/strategy_profiles/derivatives_live.yaml:108)
trade_cost_derivatives_taker_fee_bps = 5.0  (configs/strategy_profiles/derivatives_live.yaml:109)
```

单笔退出节约上限: 5.0 − 2.0 = 3.0 bps/side（理想情形，实际受 fill_rate 折扣，见 §4）

---

## §3 设计提案

### 3.1 新增 execution_mode 枚举值

**文件**: `aats/bootstrap/settings.py` — `IndependentExecutionPolicyMode` Literal

```python
IndependentExecutionPolicyMode = Literal[
    "adaptive",
    "passive_first",
    "bounded_limit",
    "bounded_taker",
    "aggressive_bounded_taker",
    "post_only_with_timeout_fallback",  # 新增
]
```

**新增配置项**:
```python
strategy_hedge_independent_post_only_timeout_ms: float = 3000.0
strategy_hedge_independent_post_only_fallback_mode: str = "bounded_taker"  # 回退模式
strategy_hedge_independent_post_only_expected_fill_rate: float = 0.3  # 保守估计, §4.3
```

### 3.2 execution_policy.py 新分支

在 `resolve_execution_policy_from_mode` 增加 post_only 分支：

```python
if mode == "post_only_with_timeout_fallback":
    return IndependentExecutionPolicy(
        edge_strength=edge_strength,
        urgency=urgency,
        execution_style_preference="post_only",  # 新 style
        order_type_preference="post_only",        # 新 order_type
        time_in_force_preference="GTC",
        limit_offset_bps_preference=limit_offset_bps,  # 同方向（己方挂单）
        max_acceptable_cost_bps=max_acceptable_cost_bps,
        policy_reason=policy_reason,
        mode=mode,
        price_style="post_only",
        passive_first=True,
        bounded_limit_ioc=False,
        post_only=True,         # 新字段
        bounded_taker=False,
        reason=policy_reason,
    )
```

### 3.3 planner.py 新定价逻辑

新方法 `_post_only_limit_price`：
- **buy**: `price = min(reference × (1 − safety_offset_fraction), best_bid)` — 挂在买一或更低
- **sell**: `price = max(reference × (1 + safety_offset_fraction), best_ask)` — 挂在卖一或更高
- 若计算出的价格会跨价（e.g., buy price ≥ best_ask）→ **放弃 post_only，直接退化为 fallback_mode**（避免 OKX 拒绝后再补 orchestration）

### 3.4 okx_adapter.py `_order_type` 新分支

```python
@staticmethod
def _order_type(intent: OrderIntent) -> str:
    if intent.order_type == "post_only":
        return "post_only"
    if intent.order_type != "limit":
        return intent.order_type
    tif = str(intent.time_in_force or "IOC").upper()
    if tif == "IOC":
        return "ioc"
    if tif == "FOK":
        return "fok"
    return "limit"
```

### 3.5 order_manager.py 新 orchestration 层

新的 timeout-driven fallback orchestration（细节在实施阶段敲定）:

1. Submit post_only → 记录 `post_only_deadline_ts = now + timeout_ms`
2. 在 order_manager 的主循环/event bus 订阅点定期检查：对标记为 post_only 的未成交订单，若 `now > deadline`:
   a. 若 sCode≠0（post_only 被 OKX 拒绝）→ 立即 fallback（不等超时）
   b. 若部分成交 → cancel 剩余、按 fallback_mode 重下 remaining_qty
   c. 若完全未成交 → cancel、按 fallback_mode 重下 full_qty
3. Fallback 订单走现有 `bounded_taker` 路径，成本按 **taker** 计费（fee_resolver 不给 maker 折扣）
4. 所有 fallback 事件记入 `event_store` 作为 post-deploy evidence 收集源

### 3.6 fee_resolver.py 新分支 (§2.3 触及项)

```python
# post_only_with_timeout_fallback 的 cost 估计：
# maker × fill_rate + taker × (1 − fill_rate)
# fill_rate 由 settings.strategy_hedge_independent_post_only_expected_fill_rate 配置
# H2 不受影响：bounded_limit_ioc 仍归 taker，这是新增分支不是回退
if normalized_style == "post_only" or normalized_order_type == "post_only":
    fill_rate = to_decimal(self.settings.strategy_hedge_independent_post_only_expected_fill_rate)
    fill_rate = min(max(fill_rate, Decimal("0")), Decimal("1"))
    return (maker * fill_rate) + (taker * (Decimal("1") - fill_rate))
```

**验证测试必写**:
- `bounded_limit_ioc` 仍返 taker (H2 保持)
- `post_only` 在 fill_rate=0.3 返 2.0×0.3 + 5.0×0.7 = 4.1 bps
- `post_only` 在 fill_rate=0.0 返 5.0 bps (退化为 taker)
- `post_only` 在 fill_rate=1.0 返 2.0 bps (全 maker)

---

## §4 成本模型修正 (§2.3 触及项的完整论证)

### 4.1 触面

`fee_resolver.estimated_execution_fee_bps_decimal` 新增一个分支。原 H2 分支 (`bounded_limit_ioc → taker`) 保持。

### 4.2 为什么这不是 H2 回退

| H2 要防止的 | 本提案 |
|---|---|
| 把 IOC 当 maker 折扣 → 低估 fee → 让 gate 过线 → 实盘亏费 | ✅ **没有**。bounded_limit_ioc 仍算 taker |
| 在同一个 order type 上把 classification 改宽 | ✅ **没有**。新增 `post_only` 是**物理上不同**的 order type (OKX 拒绝跨价, 实际挂单) |
| 低估成本让信号过关 | ⚠️ 风险源: 见 §4.3 保守 fill_rate 防御 |

### 4.3 保守 fill_rate 选择

**决策**: 默认 `fill_rate=0.3`（30%）

**根据**:
- **理论上限**: 若 post_only 从不被拒绝、从不超时 → fill_rate=1.0 → fee=2.0 bps
- **悲观底**: 若 post_only 全部超时或拒绝 → fill_rate=0 → fee=5.0 bps (退回现状)
- **无实盘数据**: 目前**零**样本。用 0.3 = "悲观偏下的合理猜测"
  - 30% 成交 → 70% fallback = taker
  - 节约 = (5.0 − 2.0) × 0.3 = **0.9 bps/side**
  - 远小于 3.0 bps/side 的理论上限 → 防止"假设过好"的反模式

**对比灵敏度**:

| fill_rate | 估计 fee | 节约 | 距离 `max_acceptable_cost_bps=7.5` 的 gate |
|---|---|---|---|
| 0.0 | 5.00 | 0 | 无变化（退回 bounded_limit_ioc 基线）|
| 0.2 | 4.40 | 0.6 | 单边多 0.6 bps 空间 |
| **0.3 (默认)** | **4.10** | **0.9** | **单边多 0.9 bps 空间** |
| 0.5 | 3.50 | 1.5 | 单边多 1.5 bps |
| 0.8 | 2.60 | 2.4 | 单边多 2.4 bps |
| 1.0 | 2.00 | 3.0 | 单边多 3.0 bps（**不得假设**）|

### 4.4 Gate 通过率的影响分析

**当前 baseline**:
- `expected_lifecycle_cost_bps` ≈ entry_taker + exit_taker + 双边 slip ≈ 5 + 5 + 2×slip_bps
- 假设 slip_bps=0.5/side → 总 cost ≈ 11.0 bps（已超 `max_acceptable_cost_bps=7.5`）
- **结论**: 当前配置下几乎**所有**信号都在 cost gate 外

**应用本提案后（仅 exit）**:
- exit_taker 5.0 → exit_blended 4.1（fill_rate=0.3）
- 总 cost ≈ 5 + 4.1 + 2×slip = 10.1 bps（依然 > 7.5）
- **结论**: 本提案单独**不足以**让信号过 cost gate。这证明本提案**不是**"调松门槛"（反模式 #4）。

**若结合 entry post_only（本提案暂不做）**:
- 将是未来独立提案，需要重新 §3 解冻审批

### 4.5 Evidence 回路 (post-deploy)

实施 24h 后必须收集并汇报以下指标（见 §7.2）:
- 真实 fill_rate = post_only 完全成交笔数 / post_only 总笔数
- 真实 partial_rate = 部分成交笔数 / 总笔数
- 真实 reject_rate = OKX 拒绝笔数 / 总笔数

**Go 后阈值**: 若 24-48h 内 fill_rate 实际 < 0.3 → 更新 config 降低或完全回退（见 §8）。

---

## §5 Governance Audit

### 5.1 `frozen_parameters.md` §2 逐条

| 条款 | 是否触及 | 说明 |
|---|---|---|
| 2.1 Strategy profile 参数 | ❌ 不触及 | `entry_threshold` / `scale_in_threshold` / `close_threshold` / `signal_edge_scale_bps` / `max_acceptable_cost_bps` / `min_safe_net_edge_bps` 等全部不动 |
| 2.2 Scoring 权重 | ❌ 不触及 | `scoring.py` 未改 |
| **2.3 Fee / cost 模型核心** | ⚠️ **触及** | `estimated_execution_fee_bps_decimal` 新增 post_only 分支; bounded_limit_ioc → taker 不动 |
| 2.4 Runtime gates (authority_map) | ❌ 不触及 | `target_position.py` 未改 |
| 2.5 已归档路径 | ❌ 不触及 | post_only 是新设计, 不是复活 archived |
| 2.6 Governance 文档自身 | ❌ 不触及 | 本次不改 frozen_parameters.md / alpha_evidence_gate.md |

### 5.2 §3 解冻流程对照

| §3 要求 | 本提案对应 |
|---|---|
| 1. 新证据（回归/分析报告）| **本文件 + OKX 官方 API 文档**。执行层变更的证据形式是"OKX 外部语义 + 代码现状对比"，不是统计回归 (理由见 §5.3) |
| 2. 通过 alpha evidence gate | **不适用**，见 §5.3 |
| 3. 独立 PR | ✅ 承诺（实施阶段单独 PR, 不打包）|
| 4. 双人批准 | ✅ 用户 sign-off on §9 |
| 5. Deploy audit trail | ✅ commit message 格式: `[evidence: docs/design/post_only_maker_exit_mode_2026_04_21.md]` |
| 6. 回退预案 | ✅ 见 §8 |

### 5.3 为什么 alpha_evidence_gate 4 硬指标不适用

`alpha_evidence_gate.md` §3 四条硬指标（OOS / cross-window / cost-adjusted / regime-slice）是为 **alpha 信号提案**设计的。本提案性质不同:

| alpha_evidence_gate §3 | 本提案的处理 |
|---|---|
| §3.1 OOS stability | **N/A** — 不是信号预测模型。"post_only 是否被 OKX 接受" 是外部协议事实，不是统计稳定性问题 |
| §3.2 Cross-window | **N/A** — OKX post_only 协议不随时间 regime 变化；fill_rate 随市场状态变化会通过 §7.2 post-deploy 观察收集 |
| §3.3 Cost-adjusted | **反向适用** — 本提案就是为了让 cost 估计**更真实**。保守 fill_rate=0.3 满足 §3.3 "不允许 assume 更低 fee" 的精神：我们不是 assume 更低 fee，而是 assume 一个部分 maker 部分 taker 的**加权估计**，且加权系数保守 |
| §3.4 Regime-slice | **Deferred to post-deploy** — 若实盘后发现高波/低波下 fill_rate 差异大，再独立追加 regime-conditional fill_rate |

### 5.4 反模式 §7 逐条自检

| 反模式 | 自检 |
|---|---|
| #1 动机反模式（让旧数据过阈值）| ✅ 清。阈值不动；§4.4 证明本提案单独不足以让信号过 gate |
| #2 Cost 造假 | ✅ 清。用 OKX 真 maker fee 配置值，没自行假设更低；保守 fill_rate |
| #3 Degenerate cross-window | N/A（不是统计提案）|
| #4 Single-point win | ✅ 清。§4.3 灵敏度表显示各 fill_rate 档位的影响 |
| #5 Hyperparameter overfit | N/A（不是模型拟合）|
| #6 Missing replay | ⚠️ 注意：本提案无法在 replay 上完全验证（post_only 行为依赖 live book），但可在 WSL2 paper / staging 上做**端到端连通性**测试（见 §7.1） |
| #7 Unfalsifiable | ✅ 清。§8 预注册"24-48h fill_rate < X 触发 revert" |
| #8 Rule change mid-flight | ✅ 清。fill_rate 阈值 0.3 **在提案时固定**，不在看到 post-deploy 数据后回调 |

---

## §6 风险分析

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| fill_rate 真实 < 0.3 | 中 | cost 估计过于乐观 → 更多信号过 gate 但实盘 fee 超预期 | §4.3 保守 + §7.2 24h 观察 + §8 自动 revert |
| OKX 拒绝率高（e.g. 价格计算跨价频繁）| 低-中 | post_only 实际覆盖率低，等同于 fallback_mode=bounded_taker | §3.3 planner 预检查价格是否跨价，避免 OKX 拒绝浪费一次 RTT |
| 部分成交后残量 fallback 的 state machine 冲突 | 中 | 状态机 bug 可能导致残量漏单 | 实施阶段必写集成测试（WSL2 testcontainers + paper adapter 覆盖 partial → fallback 流程）|
| 3s 持仓暴露在 close_stale 场景不可接受 | 低 | close_stale 本身 urgency=medium，3s 可接受 | 不应用于 close_failed_thesis / 紧急 de_risk（scope 限定 §1.3）|
| Timeout orchestration 新引入 race condition | 中 | 可能 cancel 成功后 fallback 失败，留下裸 position | 实施时必跑现有 recovery 路径测试；event_store 保留完整 chain |
| 若实盘 24h 观察 revert 时已有 in-flight post_only 订单 | 低 | 过渡期部分订单走新、部分走旧路径 | revert 操作 = config flip + deploy；既存 order 按原 path 走完终态 |
| fee_resolver 分支逻辑错误导致 H2 回退 | 低 | 违反 §2.3 红线 | 单元测试**必**覆盖 bounded_limit_ioc 仍返 taker |

---

## §7 实施与 Evidence 收集计划

### 7.1 实施前 (dev + WSL2 集成测试)

**单元测试必过**:
- `fee_resolver.py`: post_only → blended, bounded_limit_ioc → taker (H2 regression guard)
- `execution_policy.py`: mode=post_only_with_timeout_fallback → correct IndependentExecutionPolicy
- `planner.py`: _post_only_limit_price 各种 book 形态下不跨价
- `okx_adapter._order_type`: intent.order_type="post_only" → "post_only"

**集成测试 (WSL2 testcontainers)**:
- Paper adapter + 模拟 order book → post_only 正常成交 path
- Paper adapter + 模拟 OKX 拒绝 → 立即 fallback
- Paper adapter + 超时 → cancel + fallback (full qty)
- Paper adapter + 部分成交 + 超时 → cancel + fallback (remainder)

**端到端 staging**:
- 连 OKX demo account，发 1 单 post_only（最小 min_size），确认 ordType=post_only 到达 exchange，确认 fill 或拒绝回报能被正确解析

### 7.2 实施后 (24-48h post-deploy)

**Observability 指标 (新加 Grafana panel)**:
- `exec.post_only.submitted_total`
- `exec.post_only.filled_full_total` / `.filled_partial_total` / `.rejected_total` / `.timeout_total`
- `exec.post_only.fallback_submitted_total`
- `exec.post_only.observed_fill_rate_24h` = filled_full / submitted
- `exec.post_only.observed_partial_rate_24h`
- `exec.post_only.observed_fee_per_fill_bps` (从 OKX fill feeRate 字段解析)

**SQL 查询模板** (放 `scripts/research/post_only_exit_fill_stats.sql`):
```sql
-- 实施后 24-48h 跑, 收集实盘 fill_rate
select
    count(*) filter (where status = 'FILLED' and fill_qty = requested_qty) as filled_full,
    count(*) filter (where status = 'FILLED' and fill_qty < requested_qty) as filled_partial,
    count(*) filter (where status = 'CANCELED' and cancel_reason like '%post_only_timeout%') as timeout_cancel,
    count(*) filter (where status = 'REJECTED' and execution_error like '%post_only_rejected%') as okx_reject,
    count(*) as total
from order_states
where created_ts >= '<deploy_ts>'
  and payload ->> 'execution_mode' = 'post_only_with_timeout_fallback';
```

### 7.3 Go 后 7 天回顾

- 观察 24-48h 数据 → 更新 `strategy_hedge_independent_post_only_expected_fill_rate` 为观察值
- 若观察值 < 0.3 → 说明保守估计仍乐观 → 考虑彻底 revert
- 若观察值 > 0.5 → 可考虑适度上调（独立提案走完整流程，**不**当场 commit）

---

## §8 回退预案 (§3 第 6 项必填)

### 8.1 自动回退触发条件 (24-48h post-deploy)

| 指标 | 触发 revert 阈值 |
|---|---|
| observed_fill_rate_24h | < 0.15 (目标 0.3 的一半，说明保守估计都乐观) |
| okx_reject rate | > 0.4 (说明 planner 跨价预检查失败率高) |
| fallback_mode 使用率 + timeout 率 | > 0.85 (说明 post_only 几乎白搭，不如直接 bounded_taker) |
| 任何 state_machine exception 涉及 post_only chain | ≥ 3 次在 24h 内 |

### 8.2 Revert 操作

**Level 1 (软 revert, 1 行 config 改动)**:
```yaml
# configs/strategy_profiles/derivatives_live.yaml:463
strategy_hedge_independent_close_stale_execution_mode: bounded_limit   # 从 post_only_with_timeout_fallback 改回
```
deploy.sh --skip-commit → 立即生效，新 exit 全走旧路径

**Level 2 (完整 revert, 代码回滚)**:
```bash
git revert <实施 PR 的 merge commit>
bash scripts/deploy.sh --commit "revert: post_only exit mode — observed fill_rate below threshold"
```

### 8.3 Revert 决策权

- **Level 1 触发**: Claude 自主执行 (config-only，无代码回滚)
- **Level 2 触发**: 必须用户 sign-off (涉及代码回滚)

### 8.4 In-flight order 处理

Revert 时若有 in-flight post_only 订单:
- 不主动 cancel（让其自然走完 timeout + fallback）
- 新 deploy 后的订单全走旧 `bounded_limit` 路径
- 过渡期 < timeout_ms (3s) + OKX round-trip

---

## §9 Go / No-Go 决策 + 签署

### 9.1 提案方自评 (Claude Opus 4.7)

- `frozen_parameters.md` §3 解冻流程: **6 项全部对应完毕**
- 反模式自检 (alpha_evidence_gate §7): **8 项全清 / N/A**
- 保守 fill_rate 选择论证: **充分** (见 §4.3)
- 回退预案: **完整** (§8 三层: 指标 / 操作 / 决策权)
- Scope 限定: **仅 close_stale**，不扩散到紧急出口
- 文档完整度: **符合 §3 要求**

**提案方建议**: Go

### 9.2 用户决策

- 决策时间: 2026-04-21 UTC
- 决策人: 用户 (excellentang@gmail.com)
- 决策结果: [x] **Go** / [ ] Conditional revisit / [ ] Archive
- 批准语境: 选项二 (exit_mode real maker path) 完整 §3 解冻流程；选项一 (Task 177) 已落地 (a9be30b)，选项五 (P1-D microstructure alpha) 属长期规划独立走 gate
- 实施约束:
  1. 独立 PR，不与无关改动捆绑
  2. commit message 必须引用 `[evidence: docs/design/post_only_maker_exit_mode_2026_04_21.md]`
  3. 单测 + WSL2 集成测试全过再 merge
  4. 实施后 24-48h 按 §7.2 观察，按 §8 阈值自动或人工 revert

### 9.3 实施前依赖 (Go 后)

1. 本文件 commit 到 main 分支 (作为 evidence trail)
2. 实施 PR 单独开，commit message 引用本文件路径
3. 单元测试 + 集成测试全过
4. WSL2 staging 跑至少 1 个端到端 post_only flow
5. 实施 PR merge 前 Grafana panel + SQL 脚本就绪（§7.2）

---

## §10 可复现性

### 10.1 本文件基于的代码快照

- commit: `cb9ebde` (main, 2026-04-21)
- 关键 grep 实证命令:
  ```bash
  grep -n "post_only" aats/services/execution_engine/okx_adapter.py
  grep -n "bounded_limit_ioc" aats/services/fee_resolver.py
  grep -n "_bounded_live_limit_price" aats/services/execution_engine/planner.py
  ```

### 10.2 OKX 文档引用

- V5 API `POST /api/v5/trade/order`: https://www.okx.com/docs-v5/en/ (`ordType` 字段说明)
- Perpetual swap fee schedule: 账户 VIP 等级页面实时查

---

## §11 相关 governance doc 引用

- `docs/governance/frozen_parameters.md` — §2.3 + §3 解冻流程
- `docs/governance/alpha_evidence_gate.md` — §3 / §7 (部分适用，详见 §5.3)
- `docs/governance/runtime_trading_mode_semantics.md` — **不涉及** (本提案不改 runtime mode)
- `docs/review/independent_cost_model_audit_2026_04_19.md` — H2 修复来源
- `docs/review/cost_audit_live_reconciliation_2026_04_19.md` — Path C 成本对账证据
- `CLAUDE.md` — 操作手册 (本次实施遵守 §OrderState 持久化 三重同步纪律)

---

## §12 签署

- 起草: Claude Opus 4.7 · 2026-04-21
- 基于: 2026-04-21 会话中用户选定的"选项一 → 选项二 → 选项五"优先顺序，在完成选项一（Task 177）后进入选项二的研究阶段
- 审批人: 用户 (决策栏 §9.2)
- 文档所有权: execution layer (执行引擎 + 成本模型交叉)
- 版本: v0.1 (Evidence plan 阶段，实施阶段可能产生 v0.2 微调)
