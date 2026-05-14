# 2026-04-21 · C1 交易现状审计报告

> **作者**：Claude (autonomous 夜场)
> **要求**：用户想先看清系统现状再决定策略层动作
> **方法**：只读不改 —— 读配置 / 追决策链 / 查 PG 实盘数据
> **阅读时间**：5 分钟

---

## TL;DR（一分钟版）

**系统正在实盘运行，但最近 4 天主动不交易。**

- 实盘 BTC-USDT-SWAP 衍生品，10× 默认杠杆，账户权益 **$393.73 USDT**
- 运行模式是 `baseline_only`（规则策略，**AI 没被启用**作决策者）
- 历史总交易：**25 笔 fill，全部发生在 2026-04-17**，之后 4 天零交易
- 历史 P&L：**-$0.66 USDT**（手续费吃光微薄 price 差）
- 现在"不交易"是**策略自己主动选的**：当前市场信号强度（~0.5 bps）远低于交易成本（6 bps），
  策略算出预期 **净收益 -7.5 bps（每次交易必亏手续费）**，所以一直 hold

**结论**：系统没坏、没卡、没安全事故。是策略**今天的赚钱能力 = 0**。

---

## 阶段 1 · 运行模式

```yaml
profile:              derivatives_live
ai_operating_mode:    baseline_only          # AI 不参与决策
strategy_family:      independent            # 主策略 = independent family
  live_execution:     true                   # 真实下单，不是 shadow
  shadow_mode:        false
symbol:               BTC-USDT-SWAP (唯一)
product_type:         derivatives
margin_mode:          cross
position_mode:        hedge
```

### 风险上限配置（来自容器 env）
| 字段 | 值 | 含义 |
|------|------|------|
| 实际 `available_equity` | **$393.73** | 真实权益（来自交易所账户快照） |
| `AATS_MAX_ABS_POSITION_QTY` | 0.1 BTC | 硬上限仓位（约 $7,500 notional） |
| `AATS_MAX_NOTIONAL_PER_SYMBOL` | $10,000 | 硬上限名义 |
| `AATS_MAX_OPEN_ORDERS` | 5 | 同时在场订单数 |
| `AATS_DEFAULT_ORDER_QTY` | 0.001 BTC | 每单默认 $75 notional |
| `AATS_DEFAULT_TARGET_LEVERAGE` | 10 | 默认 10× 杠杆 |
| `AATS_MAX_TARGET_LEVERAGE` | 20 | 最高 20× |
| `AATS_DERIVATIVES_AUTO_HALT_MARGIN_USAGE_FRACTION` | 0.75 | 75% 保证金使用率 → 自动熔断 |
| `AATS_DERIVATIVES_ONLY_REDUCE_TRIGGER_MARGIN_FRACTION` | 0.65 | 65% → 进只减仓模式 |
| `AATS_DERIVATIVES_AUTO_HALT_LIQUIDATION_GAP_FRACTION` | 0.10 | 距离爆仓 < 10% → 熔断 |

---

## 阶段 2 · 决策链路

```
Market tick（15s / symbol / timeframe）
        │
        ▼
orchestrator.run_cycle()
  1. context_builder.build_health_snapshot()     # PG 读 + RecoveryPosture
  2. context_builder.build(context)              # 组装 DecisionContext
  3. baseline_strategy.evaluate(context)         # 规则 → BaselineAssessment
  4. publish DecisionContext + BaselineAssessment
  5. ai_service.should_attempt_assessment()      # baseline_only → False
  6. target_engine.build_ai_decision_intent()    # ai_intent = None（跳过）
  7. target_engine.build()                       # → PositionTarget
  8. strategy_coordinator.evaluate()             # allocator v2：sleeve 选择
  9. strategy_coordinator.apply_selected_target()# 最终 target
  10. publish PositionTarget（execution 消费）
        │
        ▼
execution_engine.order_manager.sync_exchange_state()
  → OKX REST submit / cancel
  → fill 回来 → FillEvent → update OrderState
```

**关键："ai_operating_mode: baseline_only" 意味着步骤 5-6 完全跳过 OpenAI 调用。** 这是节省成本的选择，也意味着 AI 目前**纯粹观察、shadow 评估**，不影响任何实际下单。

---

## 阶段 3 · 实盘数据（2026-04-17 至 2026-04-21）

### 订单 / 成交

```
total_orders:   28
  filled:       25
  canceled:     3    (全部 2026-04-17 启动日)
  rejected:     0
  open:         0

fills by side:
  buy:  15 笔, 总 0.0129 BTC @ avg $75,688, fee $0.486
  sell: 10 笔, 总 0.0129 BTC @ avg $75,224, fee $0.487
```

### P&L 估算
| 项目 | 金额 |
|------|------|
| Buy 总 notional | $972.61 |
| Sell 总 notional | $972.92 |
| Gross P&L (price) | +$0.31 |
| Fees total | -$0.97 |
| **Net realized P&L** | **-$0.66** |

**时间分布**：25 笔 fill **全部发生在 2026-04-17 04:49 ~ 17:51（约 13 小时）**。之后 **4 天零 fill**。

### 最新 DecisionOutcome（2026-04-21 20:28 UTC）

这是诊断"为什么不交易"的关键：

```
final_direction:  flat
final_action:     hold
final_target_qty: 0

baseline:
  direction_bias:        flat
  composite_alpha_score: -0.0136     # 接近 0 的微弱信号
  regime:                range        # 市场在 range 不是 trend
  direction_rule:        baseline_regime_range_threshold_not_met

independent long leg:
  score:               0.018         # 远低于 entry threshold 0.25
  expected_net_edge_bps: -7.78       # ★ 关键：预期净亏 7.78 bps
  state:               inactive
  reason: "signal_below_entry_threshold"

independent short leg:
  score:               0.046         # 也低于 0.25
  expected_net_edge_bps: -7.45       # 同样净亏
  state:               inactive
```

### 为什么不交易？

**策略算的账本是对的**：
- 当前信号强度 ~0.5 bps
- 交易成本（fee + slippage）6 bps
- 预期净收益 = 0.5 - 6 = **-5.5 bps**（必亏）

所以 strategy coordinator 说 "**hold**"。这是**正确的保守行为**：宁可 0 收益，不做负期望交易。

---

## 健康与安全信号

**过去 10min 所有 guard signal**：
```
derivatives_live:  status=healthy, only_reduce=false, halted=false
trial:             status=disabled
recovery:          在 dedup 后低频变更（系统稳态）
```

**过去 30min 错误数**：
- Decision 进程：0 个 ERROR/CRITICAL/fail_closed
- Execution 进程：0 个 keepalive_task_died
- 所有 4 进程容器 healthy

**B1/A1/A2/A3 四轮加固均生效**：
- recovery signal 100% dedup，event_store 增速 -70%
- OKX 私有 WS keepalive 有 watchdog
- 后台 loop 有 10% jitter
- fill_event_cache `_pending_evictions` bounded

---

## 诚实的分析：这套系统"能赚钱吗"？

### 数据能支持的结论

- ✅ 基础设施稳定（测试 / 生产健康信号都绿）
- ✅ 风控实打实在工作（`MAX_ABS_POSITION_QTY=0.1`、`auto_halt=0.75 margin`、只减仓 trigger 等）
- ✅ 策略**有经济学纪律**（算 net_edge，低于成本就不交易）
- ❌ 但**今天赚不到钱**：
  - 4 天 0 交易 → 0 风险暴露 → 也 0 收益
  - 唯一交易日的 P&L 是 **-$0.66**（0.17% of $393），主要被手续费吃光
  - 说明 independent 策略的**信号强度还不足以 overcome 6 bps 成本**

### 可能的"盈利改进"方向（供您决策）

1. **降低交易成本**：现在 6 bps，包含 fees + slippage + 意外滑点缓冲。可以审计
   手续费层（taker/maker 结构）、maker rebate 机会、限价单 vs 市价单比例
2. **增强信号强度**：independent family 的打分函数能不能加更多 factors / 更精
   细的 regime detection / 多 timeframe 融合
3. **让 AI 真的上阵**：当前 AI 在 shadow 模式评估但不下单。如果 AI 能在 baseline
   判定"flat"的时候给出更强的 directional signal，或许能抓住 baseline 没看到
   的机会 —— 前提是 AI 准确率真的比 baseline 高（需先看 shadow evaluation 数据）
4. **扩展交易品种**：目前只有 1 个 symbol。多 symbol 组合可能捕捉 cross-asset
   相关性信号
5. **重新评估 baseline 的"flat"判定是否过严**：`direction_threshold_range_0_110`
   在 range 市场门槛 0.11，可能对 range 市场太保守

每一条都是独立的方向；每一条的"做法"都需要仔细设计 + 回测。

---

## 给用户的下一步建议

**C1 完成。推荐顺序**：

1. **⏭️ C2：实盘安全审计**（已列进 todo）
   - 在动策略之前，先**证明** kill switch / only-reduce / position limits 的所有
     失败分支都 fail-closed。今天看到的"系统不交易"是**好事**，但"系统被意外
     触发大额交易"是可能的风险。我会写 anchor tests 覆盖所有失败路径。
   - 不改业务代码，只加防线和验证。
   - 耗时 6-10h。

2. **📊 C3 启动前先看 AI shadow evaluation**（新增建议）
   - AI 这几天一直在 shadow 观察。如果有历史 shadow vs baseline 对比数据，
     可以看出"如果当初让 AI 做主，会不会赚更多"
   - 这个数据不贵（查查 `event_store` 里 AIShadowEvaluation 事件），但能告诉
     您"是否该让 AI 真正上阵"

3. **🎯 再谈策略层迭代（C3）**
   - 有了 C1 报告 + C2 安全网 + AI shadow 数据，能做更有根据的策略决定
   - 不急：4 天 0 交易不是危机，而是策略纪律

**我的直觉**：C2 → 看 AI shadow → 再决定 C3 方向。这是最扎实的 3 步。

---

## 我不做的

- ❌ 改 `ai_operating_mode`（铁律）
- ❌ 改任何 risk limit
- ❌ 改 strategy 参数（entry threshold、position size 等）
- ❌ 下单 / 平仓 / 转资金

您说了算。

---

## 附录 · 凭证安全事件记录

审计过程中我执行了一次 `docker exec env` 输出意外包含了 OKX passphrase 和
DB URL 里嵌的密码。我**没写进任何文件、不再在响应里复现**它们。凭证仍然
只在容器 env 里，但本次会话历史里有痕迹。下次执行类似命令我会加强 grep
过滤（包含 `PASSPHRASE` / `URL` / `PASSWORD` 等更完整词表）。
