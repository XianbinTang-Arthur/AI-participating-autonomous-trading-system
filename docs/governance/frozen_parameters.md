# Frozen Parameters — 冻结列表 (2026-04-20)

> **Governance 纪律**: 本列表中的参数/路径 **不允许** 在没有明确解冻流程的情况下改动.
> 作用: 防止团队陷入 "拧旋钮" 的假进展.

**创建时间**: 2026-04-20
**创建背景**: 2026-04-19 起 30+ 小时工作中, 多次出现 "为了让系统下单而下调 threshold" 的冲动. 用户 2026-04-20 战略 directive 明确要求: **停止在错误方向上试错**. 本文档把这件事变成工程纪律.

---

## 1. 为什么需要冻结

**反模式** (过去 30 小时里出现过或差点出现):

1. 1.2% 阳线没下单 → 降 `entry_threshold` 让系统能下单
2. Short slope 负 → 改 short scoring 权重强行翻正
3. OI delta R² 负 → 换更细 horizon / 不同 smoothing 让 R² 变正
4. 成本太高 → 降 `max_acceptable_cost_bps` 假装 edge 够用

**核心识别特征**: 改参数的 **动机** 是 "让当前数据看起来满足门槛", 而不是 "客观证据显示门槛定错".

**准则**:
> 参数改动的正确触发是**新证据**, 不是**旧证据看起来不够用**. 若你发现自己在想 "调一下这个就好了", 极大概率这是反模式.

---

## 2. 冻结清单

### 2.1 Strategy profile 参数 (15m OHLC family)

**当前值**（`configs/active_parameter_sets/derivatives_live/independent_15m.json` 或等价）:

| 参数 | 当前值 | 冻结理由 |
|---|---|---|
| `entry_threshold` | 0.25 | 5 份 NO-GO 证据证明 15m OHLC alpha 不存在, 调低阈值只会入**噪声 trade** |
| `scale_in_threshold` | 0.25 | 同上 |
| `close_threshold` | 0.15 | 同上 |
| `min_confirm_ticks` | (当前值) | 1.2% 阳线场景下已验证"不应追" |
| `score_stability_threshold` | (当前值) | 同上 |
| `signal_edge_scale_bps` | 12 / 20 | P1-B calibration 已做过, 证据不足以再调 |
| `max_acceptable_cost_bps` | 7.5 | **不得下调去凑 "正 net edge"**; 上调需真实成本数据支持 |
| `min_safe_net_edge_bps` | 2.0 | 与 cost model 耦合, 同上 |
| `strategy_edge_noise_buffer_bps` | 2.0 | 设计为对冲信号噪声, 不是 calibration 旋钮 |

### 2.2 Independent family scoring 权重 (Mode A / B / C)

**文件**: `aats/services/strategy_engines/independent/scoring.py`

- `_MODE_A_W_ALPHA` / `_MODE_A_W_MOMENTUM` / `_MODE_A_W_TREND` / `_MODE_A_W_MICRO` / `_MODE_A_W_CONFIDENCE`
- `_MODE_B_W_*` / `_MODE_C_W_*`
- Mode A/B/C bonus 系数 `_MODE_A_BONUS_*` etc.

**冻结理由**: H4 修复后经 5 份 NO-GO 证据交叉验证, 任何权重微调都是在已证伪假设上的无效 tuning.

**例外**: 若 P1-D microstructure 引入**新特征列**(如 orderbook imbalance / trade flow), 新权重是**新设计**不是"调整", 不在本冻结范围. 但加新权重需走完整 alpha evidence gate 流程.

### 2.3 Fee / cost 模型核心

**文件**: `aats/services/fee_resolver.py`

- `estimated_execution_fee_bps_decimal` 逻辑
- `bounded_limit_ioc → taker` 分类 (P1-B step 2 H2 修复已上线, 不得回退)

**冻结理由**: H2 修复经 Path C 成本对账验证 (25 笔 fill 全 5 bps 零方差), 符合 OKX 实际档位.

### 2.4 Runtime Gates

**文件**: `aats/services/decision_engine/target_position.py` 的 `authority_map`

- `baseline_only → reference_only`
- `ai_assisted → advisory`
- `ai_decision_maker → final_decision`

**冻结理由**: 见 `docs/governance/runtime_trading_mode_semantics.md`. 改动此 map **等于**改变实盘授权路径, 不得为 "debug 让系统下单" 而触碰.

### 2.5 已归档路径 (更强的冻结)

以下假设**永久归档**, 不只是"暂停":

- **P1-A 双通道 CHASE**: `docs/design/archived/p1a_dual_channel_chase_failed_path_2026_04_19.md`
- **Kline + funding 线性 alpha**: `docs/design/archived/kline_funding_no_alpha_2026_04_20.md`
- **所有 OHLC 派生 fast_impulse 候选**: 见 fast_impulse_candidate_selection 报告

**禁止**: 重新提出已归档路径除非有**方法论级突破** (如换 horizon 层 + 非线性模型), 且提案必须先过 alpha evidence gate.

---

## 3. 解冻流程

任何冻结参数的改动 **必须**:

1. **新证据**: 写在 `docs/research/` 下的回归/分析报告, 含 out-of-sample + cross-window + cost-adjusted
2. **通过 alpha evidence gate** (待 P1 成文)
3. **独立 PR** (不能与无关改动捆绑)
4. **双人批准**: 代码层面 review + 策略层面 signoff (现阶段 = 用户最终决定)
5. **Deploy audit trail**: commit message 明确引用证据文档
6. **回退预案**: 解冻后 24-48h 内数据异常必须能快速 revert

---

## 4. 现状快照 (2026-04-20 WSL2)

| 参数 | 值 | 冻结 since |
|---|---|---|
| `ai_operating_mode` | `baseline_only` | **permanently frozen 到 alpha evidence gate 通过** |
| `decision_authority` (derived) | `reference_only` | 同上 |
| `entry_threshold` (independent_15m) | 0.25 | 2026-04-20 战略 directive |
| `max_acceptable_cost_bps` | 7.5 | 2026-04-20 战略 directive |
| `min_safe_net_edge_bps` | 2.0 | 同上 |

---

## 5. 不在冻结范围 (可改, 但不 encourage)

以下属于**运维参数** , 不是 alpha/trading 决策参数:

- Rate limit (OKX REST / WS)
- Buffer size / flush interval (microstructure collector)
- Retention 期 (event_store 归档)
- Log level
- Container resource limit

这些参数改动不需要 alpha evidence, 但仍需:
- Deploy audit trail
- Unit test (若涉及行为变化)

---

## 6. 签署

- 起草: Claude Opus 4.7 · 2026-04-20
- 基于: 用户 2026-04-20 战略 framework directive "停止在错误方向上试错"
- 批准状态: 待用户确认
- 下次修订: alpha evidence gate 成文后补充量化标准 (§3)
- 文档所有权: governance layer
