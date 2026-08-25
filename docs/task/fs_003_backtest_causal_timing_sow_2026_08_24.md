# FS-003 回测因果时间契约修复设计与实施范围

> 文档状态：Phase 3C 实施任务 / 设计冻结  
> 最后核对：2026-08-24（起始代码基线 `00b6df0f8a8d2665d6cae3e88996843767cd1f56`）  
> 当前工作区：`codex/fs-002-kill-switch-p0`，包含尚未提交的 Phase 3A/3B 变更  
> 核对范围：当前 replay/backtest 代码、Gold 时间标准、Phase 2 审计证据、隔离替身复现  
> 运行时边界：未读取 `.env.*`，未连接真实账户/交易所/数据库，未部署  
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 业务目标与边界

本阶段关闭 `FS-003` 已证明的同 K 线前视偏差：策略可以使用一根完整 K 线的 open/high/low/close/volume 形成动作，但该动作不得再以同一根 K 线的 close 成交。默认 IOC 与 bounded-limit 只能在下一根可用 K 线的 open 事件执行；post-only 需要观察下一根完整 K 线的成交量，故只能在下一根 K 线关闭事件解析结果。

本阶段只修复离线 replay/backtest harness，不修改 live execution，不声称当前 OHLCV 成交模型等价于真实订单簿。旧时间模型产生的全部绩效结果失效，必须使用新模型重跑后才能作为研究证据。

## 2. 当前行为与根因

修复前的确定性隔离复现：单根 `open=100/close=110` 的已闭合 K 线生成开仓动作后，harness 立即把同一根 K 线的 `close=110` 传给 `FillSimulator`，并在该根 K 线内产生 1 笔成交：

```text
bar_start=2026-03-01T00:00:00+00:00
bar_open=100
bar_close=110
simulator_calls=[(submitted_at=bar_start, price=110)]
fills_count=1
```

根因是 harness 在同一个循环体内依次执行 `evaluate_bar(current_bar)` 与 `simulate(... current_bar.close ...)`；`ReplayDecision.ts` 又沿用 bar start，没有 observation complete、decision、submit、eligible fill 与 actual fill 的独立时间语义。Gold 标准明确 `ts` 是 bar open timestamp，因此完整 K 线直到 `ts + timeframe` 才可观察。

## 3. 模块职责与领域模型

| 模块 | 本阶段职责 |
|---|---|
| `replay/backtest/harness.py` | 验证输入时间契约；将交易动作排队；在下一可交易事件解析；维护成交与策略状态的一致性；输出时间线 |
| replay adapter | 只在已闭合 K 线上评估；可继续使用当前闭合 K 线的完整 OHLCV |
| `FillSimulator` | 保持纯成交计算器；由 harness 传入因果正确的下一事件价格/流动性 |
| `PositionTracker` / `EquityBuilder` | 按实际 fill 时间更新，按 bar close 时间估值 |
| 审计证据 | 标记旧结果失效，记录新时间契约、测试与剩余模型限制 |

新增不可变时间线记录，每个决策明确 observation bar start、observation complete/decision、submit、next tradable event、fill 及结果状态。交易决策只有在模拟成交成功后才提交 adapter 提议的持仓状态；no-fill 或末端无下一事件时不得让策略状态假装已经成交。

## 4. 输入/输出接口

`run_backtest` 的参数保持兼容。`BacktestConfig` 新增只读模型标识 `execution_model_version="next_bar_event_v2"`；不提供恢复有缺陷 same-bar 模式的开关。

`BacktestResult` 增加 `execution_timeline`。每条记录至少包含：

- `observation_bar_start_ts`；
- `observation_completed_at_ts` 与 `decision_ts`；
- 有订单时的 `submitted_at_ts`；
- `next_tradable_event_ts`、`fill_ts`；
- `status`：`no_order/filled/partial_fill/no_fill/expired_no_next_event`；
- `price_source`：`next_bar_open`、`next_bar_close` 或空。

保留 `ReplayDecision.ts=bar.ts` 作为既有 bar identity，不再把它解释成可成交时间。

## 5. 数据库 schema、表、索引与约束

无 schema、table、index、constraint 或 migration 变更。Gold 表按现有 `(symbol, ts)` 与 `is_closed` 读取；harness 在内存中验证顺序和闭合状态。

## 6. 事务、一致性与并发

纯离线单线程计算，无数据库写事务。每次最多有一个上一根 K 线产生的 pending order：先在当前 K 线对应事件解析上一订单，再用当前闭合 K 线形成下一决策。adapter 的提议状态使用副本评估，只有 fill 成功后才成为下一轮权威状态，防止 post-only no-fill 后研究状态虚假持仓。

## 7. 授权、认证与数据安全

无新增认证面、权限或凭证。不得读取 `.env.*` 或访问 live account。时间线只包含研究时间、动作结果和模型来源，不包含密钥、token 或账户标识。

## 8. 错误处理与幂等

- 任一 `is_closed=false`：在 adapter 评估前 `ValueError` 失败关闭；
- timeframe 无法解析：`ValueError`；
- bar timestamp 重复、倒序或区间重叠：`ValueError`；
- post-only 下一 bar 缺 volume/volume<=0：`no_fill`，不使用虚构 fallback 流动性；
- 最后一根 K 线形成订单但没有下一事件：`expired_no_next_event`，零成交；
- 相同 bars/config/adapter 脚本重复运行应产生相同时间线和结果。

## 9. 状态转换与生命周期

```text
closed bar observed
  -> decision at bar_end
  -> no delta: no_order
  -> trade delta: submitted/pending
       -> next bar open (IOC/bounded): filled / partial_fill / no_fill
       -> next bar close (post_only): filled / partial_fill / no_fill
       -> no next bar: expired_no_next_event
```

`filled/partial_fill` 才提交并按 PositionTracker 实际净仓校正 adapter 提议状态；`no_fill/expired` 保留成交前状态。

## 10. 缓存与性能

无缓存。每根 K 线增加一个 `ReplayState` 小对象副本和一条时间线记录，复杂度仍为 O(n)，内存从 equity/decision 规模增加同阶 O(n)。不引入额外 I/O。

## 11. 日志、监控与审计

现有 loaded-bar 日志保留，并增加 execution model version。结果时间线作为审计主证据。失败信息可包含 bar index/timeframe/timestamp，但不得包含数据库连接信息。旧产物没有 `next_bar_event_v2` 标识，必须视为 stale/invalid，不得与新结果拼接比较。

## 12. 测试策略

新增/更新 golden 与 adversarial 单测覆盖：

1. 单根 K 线开仓决策不能成交；
2. 当前 K 线 close 决策只能按下一 bar open 成交；
3. next bar 跳空时成交基准必须是 gap 后 open；
4. 时间线满足 observation complete <= submit <= fill，且 fill 不落在 observation bar 内；
5. 未闭合 K 线在 adapter 调用前失败；
6. 重复/倒序/重叠时间失败；
7. 最后订单明确 expired；
8. post-only 使用下一 bar close/volume，缺失或零流动性 no-fill；
9. no-fill 不提交 adapter 提议状态；
10. entry/hold/exit 序列在新增下一事件后仍完成两次成交；
11. bar-close MtM timestamp 使用 bar end 而非 bar start；
12. 相关 CLI/scorecard 结构兼容。

随后运行 focused tests、replay/backtest/CLI 相关单测、Ruff 与全量 unit suite。真实数据库 integration 不属于本修复正确性的必要条件。

## 13. 迁移、回滚与兼容

无 DB migration。行为兼容性有意收紧：同样的输入会产生不同 fill 数、价格、PnL 与时间戳。禁止在生产研究流程中回滚到 same-bar 模型。调用方若依赖旧绩效，必须重跑并显式记录 `next_bar_event_v2`；旧结果只能保留为历史反例。

## 14. 配置与环境隔离

无新环境变量或 feature flag。模型版本是固定安全默认，不允许配置为 same-bar。测试完全使用内存 bars、fake adapter 和 mock session。

## 15. 代码组织与依赖

预计修改：

- `aats/data_platform/replay/backtest/harness.py`；
- `aats/data_platform/replay/backtest/__init__.py`（公开时间线 DTO）；
- `tests/unit/test_backtest_harness.py` 与新的 FS-003 对抗测试；
- `docs/code_review/README.md` 和 `audit/full_system_2026_08_24` 当前状态。

不修改 live service，不新增三方依赖，不把 adapter 的策略计算与成交模拟混在一起。

## 16. 文档、运维手册与验收标准

本阶段验收标准：

- 修复前单 K 线 same-close 利用链不再产生 fill；
- 默认成交价格只来自下一 bar open，post-only 的完整 bar 流动性只在该 bar close 后使用；
- observation/decision/submit/fill 时间线可机器断言；
- 未闭合/乱序/重叠数据失败关闭；
- no-fill/terminal expiry 不产生虚假策略持仓；
- focused、相关、全量 unit 与 Ruff 通过；
- 审计将 FS-003 至多更新为 `CODE REMEDIATED / INDEPENDENT REVIEW OPEN`，而不是直接宣告生产放行；
- 所有旧回测结果明确失效并要求重跑。

即使本阶段通过，OHLCV 模型仍不证明真实盘口排队、部分成交、队列优先级或实际延迟；这些属于独立 execution-realism 校准与 OOS 复核，不能用单元测试替代。
