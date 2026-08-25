# 23 FS-003 回测因果时间契约修复证据

> 日期：2026-08-24  
> 起始基线：`00b6df0f8a8d2665d6cae3e88996843767cd1f56`  
> 工作分支：`codex/fs-002-kill-switch-p0`，未提交工作区同时包含 Phase 3A/3B 变更  
> 验证类型：静态代码追踪 + 隔离确定性复现 + 单元/对抗回归  
> 未执行：真实账户、交易所、live DB、部署、历史策略全量重跑、独立人工复核  
> 当前裁定：**CODE REMEDIATED / REVIEW & EVIDENCE RE-RUN OPEN**  
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 原始故障与修复前复现

Gold 时间标准明确 candle `ts` 是 bar open timestamp。原 harness 在同一循环中先把当前 bar 的完整 OHLCV 交给 adapter，再立即把同一个 `bar.close` 传给 `FillSimulator`。完整 K 线只可能在 `ts + timeframe` 观察完毕，原路径却让由该完整 K 线产生的订单在 `ts` 以同一 close 成交。

修改代码前的单 K 线确定性复现：

```text
{
  bar_start: 2026-03-01T00:00:00+00:00,
  bar_open: 100,
  bar_close: 110,
  simulator_calls: [(1772323200000, 110)],
  fills_count: 1
}
```

这不是“滑点参数偏小”，而是 observation、decision、submit 与 fill 的事件顺序错误；增加固定 bps 不能消除前视偏差。

## 2. 冻结的因果时间契约

实现 SOW 见 [fs_003_backtest_causal_timing_sow_2026_08_24.md](../../docs/task/fs_003_backtest_causal_timing_sow_2026_08_24.md)。当前固定模型为 `next_bar_event_v2`，不提供恢复 same-bar 的配置开关：

```text
observation bar start = bar.ts
observation complete = decision = submit = bar.ts + timeframe

IOC / bounded-limit:
  next tradable event = next available bar.ts
  reference price = next available bar.open
  fill resolution = next available bar.ts

post-only:
  next tradable event = next available bar.ts
  full volume becomes observable = next available bar.ts + timeframe
  reference price / resolution = next available bar.close
```

存在 gap 时，订单只能在 gap 后第一条实际可用 bar 事件解析，不能在缺失区间内插值成交。末端没有下一事件时状态为 `expired_no_next_event`，不得回到 observation close 补成交。

## 3. 代码修复

### 3.1 输入数据失败关闭

`run_backtest` 在任何 adapter 观察数据前完成：

- `is_closed=true` 校验；
- timeframe 固定周期解析；
- timestamp 严格递增；
- 重复、倒序、时区不一致或小于 timeframe 的重叠拒绝；
- `ReplayDecision.ts` 必须保持 observation bar identity；
- `execution_model_version` 必须是 `next_bar_event_v2`。

未闭合 K 线的最终 close/high/low/volume 不会进入策略计算。

### 3.2 pending order 与成交状态一致性

adapter 在 `ReplayState` 副本上形成提议状态。有交易 delta 时，harness 只排队订单；下一事件真正 filled 后才提交提议状态。`no_fill` 或末端 expiry 会丢弃提议状态，避免 post-only 没成交但下一根 K 线已经假装持仓。

部分成交也被防御性处理：PositionTracker 的实际 `net_qty/avg_entry_price` 会反向校正 ReplayState，状态记为 `partial_fill`，不会把 0.4 张误写成完整 1 张 open/close。模拟器若返回 `filled_qty > target_qty` 则立即失败。

### 3.3 时间与研究 artifact

`ExecutionTimingRecord` 为每个决策保存：

- observation bar start / complete；
- decision / submit；
- next tradable event / resolution / fill；
- action、status 与 price source。

bar close 估值 timestamp 也从原来的 bar start 修正为 bar end。CLI 的 `summary.json` 与 scorecard meta 都包含 `execution_model_version`，并新增 `execution_timeline.json`。没有该模型标识的旧回测/scorecard 不能继续作为现行证据。

### 3.4 流动性失败语义

post-only 只在下一根完整 bar 关闭后使用其 volume；volume 缺失或非正时 `no_fill`，删除原来虚构 `1000` 成交量的 fallback。IOC/bounded 当前 bar-proxy 模型不声称使用 bar volume 推导开盘流动性。

## 4. 修复后原利用链复测

使用与修复前相同的单 K 线、相同 fake adapter 和相同 FillSimulator spy：

```text
{
  bar_start: 2026-03-01T00:00:00+00:00,
  bar_open: 100,
  bar_close: 110,
  simulator_calls: [],
  fills_count: 0,
  timeline_status: expired_no_next_event,
  decision_ts: 2026-03-01T01:00:00+00:00,
  fill_ts: null
}
```

裁定：**原 same-bar-close 利用链已被代码阻断**。

## 5. 对抗与回归验证

新增 `tests/unit/test_fs003_backtest_causal_timing.py`，10 个直接用例覆盖：

1. 完整 bar 决策只能在 gap 后下一 bar open 成交；
2. 单根末端 trade 过期且零 fill；
3. 未闭合 bar 在 adapter 调用前拒绝；
4. duplicate timestamp 拒绝；
5. overlapping timestamp 拒绝；
6. post-only 缺流动性 no-fill，且提议状态不提交；
7. 对抗 partial fill 只提交实际数量；
8. bar-close MtM 使用 bar end；
9. 非固定/无效 timeframe 拒绝；
10. 旧 same-bar model 不能重新启用。

最终验证：

| 范围 | 结果 |
|---|---|
| FS-003 + harness + CLI focused | `26 passed in 0.26s` |
| replay/backtest/scorecard/data-platform related | `182 passed, 5 subtests passed in 1.33s` |
| 全量 unit | `4161 passed, 30 skipped, 1665 warnings, 85 subtests passed in 94.69s` |
| Ruff `aats/ --fix` | `All checks passed` |
| 受影响代码与测试 targeted Ruff | `All checks passed` |

全量 warning 与 Phase 1/2 已知 sqlite datetime/AsyncMock warning 相同，本阶段没有把 warning 解释为测试失败，也没有宣称已治理 `FS-021`。

没有与 data-platform backtest harness 直接对应、且无需外部数据库的 integration suite；本修复的动态证据由纯内存 Gold bars、fake adapter 和真实 FillSimulator/PositionTracker 组合完成。未执行真实 DB/交易所测试，不影响 same-bar 因果反例的确定性，但仍属于运行边界。

## 6. 旧结果处理

本修复不会静默改写或删除历史 artifact。任何由旧 harness 生成、缺少 `execution_model_version=next_bar_event_v2` 或缺少因果时间线的收益、回撤、Sharpe、fill/cost 结果，必须标记 stale/invalid 并重跑。尚未完成仓库外 artifact 清单、受影响策略 lineage 追踪和正式重跑，因此“绩效证据已恢复可信”仍为 **UNKNOWN**。

## 7. 剩余风险与 FS-014 边界

`FS-003` 的时间前视错误已在代码和隔离回归层修复，但以下不是本阶段已经证明的事实：

- IOC/bounded 的开盘全量成交没有 L2 depth、队列或真实 latency 校准；
- post-only 仍是 bar-volume 分段概率代理，不是订单触达/排队重放；
- 没有真实历史 fill 回灌、容量曲线或 conservative confidence interval；
- 未对全部旧策略/参数跑新模型，也未封存独立 OOS；
- 尚无独立 reviewer 对实现和新结果签署。

这些限制由 `FS-014` 和研究治理 gate 继续承接，不能因为 FS-003 单元测试通过就外推 live 收益。

## 8. 当前裁定

- 静态修复：**PASS**；
- 确定性利用链复测：**PASS**；
- focused/related/full unit：**PASS**；
- 旧 artifact 失效标记规则：**DEFINED**；
- 旧 artifact 清单与策略重跑：**OPEN**；
- 独立人工复核：**OPEN**；
- 真实盘口执行现实性：**OPEN（FS-014）**；
- FS-003：**CODE REMEDIATED / REVIEW & EVIDENCE RE-RUN OPEN**；
- G3：**PARTIAL / 未放行**；
- 真实资金生产：**NO-GO**。

## 9. Phase 3N 后续边界更新

本文件冻结 Phase 3C 的时间因果修复证据。Phase 3N 已进一步用
`ohlcv_participation_cap_v2` 替换 IOC/bounded 无量全成：三类订单均受正 volume
和默认 1% participation cap，允许 partial；bounded 计 taker fee + fixed slippage，
成本 artifact 明示 fee/slippage 与 OHLCV/L2 限制。

因此上文“开盘全量成交”是 Phase 3C 完成时的残余风险快照，不再是 Phase 3N 工作区
现行行为。真实 L2 depth、spread、queue、impact、latency、旧证据重跑和独立复核
仍 OPEN；当前权威 FS-014 状态见
[34-fs-014-ohlcv-fill-realism-containment.md](34-fs-014-ohlcv-fill-realism-containment.md)。
