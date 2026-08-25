# FS-014 OHLCV 成交现实性风险收敛设计与实施范围

> 文档状态：Phase 3N 已实施 / L2 校准仍开放  
> 最后核对：2026-08-24（起始代码基线 `00b6df0f8a8d2665d6cae3e88996843767cd1f56`）  
> 当前工作区：`codex/fs-002-kill-switch-p0`，包含尚未提交的 Phase 3A–3N 整改  
> 核对范围：当前 FillSimulator、backtest harness、CostValidator、evidence scorecard、Phase 3C 时间契约与相关单测  
> 运行时边界：未读取 `.env.*`，未连接数据库、交易所或账户，未部署  
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 业务目标与边界

本阶段收敛 `FS-014` 中可由当前 OHLCV 输入确定性修复的乐观偏差：IOC/bounded 无流动性约束仍全量成交、bounded 固定混合费率且零滑点、post-only 命中后总是全量、实际成本诊断漏记模拟滑点，以及 artifact 不声明微观结构局限。

本阶段不伪造 L2 depth、spread、queue position、cancel latency、market impact 或交易所撮合。只实现明确标注的 `OHLCV participation-cap proxy`，并把 `FS-014` 状态更新为 `PARTIALLY REMEDIATED / L2 CALIBRATION OPEN`，不能标为 CLOSED，也不能据此外推 live 容量或收益。

## 2. 整改前行为与根因

本阶段开始时 `FillSimulator` 的代码事实如下；这些条目是修复输入，不再描述
Phase 3N 工作区的现行行为：

1. IOC 永远 `filled_qty=target_qty`，不读取 `bar_volume`；
2. bounded-limit 永远全量、使用 `0.5 × (maker+taker)` 费率且不加滑点；
3. post-only 只用 `target_qty/bar_volume` 决定命中概率，一旦命中即全量成交；
4. harness 对 IOC/bounded 明确传 `bar_volume=0`，却仍得到成交；
5. harness 的 PnL 使用已含 IOC 滑点的 fill price，但 CostValidator 只记录 `fee_bps`，导致 cost-adjusted edge 漏记滑点；
6. evidence scorecard 没有独立 fill-model version、输入粒度和 L2/queue/impact 限制字段。

Phase 3C 已解决同 bar 成交前视，但没有解决上述容量、部分成交和成本口径问题。

## 3. 模块职责与领域模型

| 模块 | 本阶段职责 |
|---|---|
| `fill_simulator.py` | 统一成交量存在性、1% participation cap、partial fill、滑点与费率口径 |
| `harness.py` | IOC/bounded 使用 observation bar 已知 volume；post-only 使用下一完整 bar volume；固定 fill-model version |
| `cost_validator.py` | 保存实际 fee/slippage 分解并以总成本计算 edge |
| `evidence_scorecard.py` | 输出 fill-model、OHLCV 粒度与现实性限制；正确聚合动态 fee/slippage |
| 单元测试 | 对抗性证明无量 no-fill、cap partial、无未来 volume、成本不漏记与 artifact 诚实性 |

`FillResult.filled_qty` 继续表示实际成交量，允许 `0 < filled_qty < target_qty`；`slippage_bps` 新增为该 fill price 已计入的方向性滑点绝对 bps。`CostDiagnostic.actual_cost_bps = actual_fee_bps + actual_slippage_bps`。

## 4. 输入/输出接口

`FillSimulator.simulate(request, reference_price, bar_volume)` 签名保持兼容，但所有 order type 都消费 `bar_volume`。无效价格、数量或 volume 返回 `no_fill`，不抛出业务异常。

新增/扩展字段：

- `FillResult.slippage_bps: float = 0.0`；
- `BacktestConfig.fill_model_version = "ohlcv_participation_cap_v2"`；
- `BacktestConfig.max_volume_participation = Decimal("0.01")`；
- CLI `--max-volume-participation` 以 Decimal 暴露同一配置；
- `CostDiagnostic.actual_fee_bps/actual_slippage_bps` 可选兼容字段；
- scorecard `meta` 增加 fill model、`market_data_granularity="ohlcv"` 和限制清单。

旧的 fill-model version 不提供兼容开关；显式错误版本在读取 bars/评估策略前失败。

## 5. 数据库 schema、表、索引与约束

无数据库 schema、migration、table、index 或 constraint 变更。全部变更位于纯内存 replay/artifact 层，不写数据库。

历史 artifact 不自动改写或删除。缺少新 fill-model version/限制字段的旧结果继续视为不能支撑当前 live-capital 结论。

## 6. 事务、一致性与并发

本模块是纯函数/单次 replay 聚合，没有事务或共享并发状态。partial fill 必须原子地同步 PositionTracker、ReplayState、equity、timeline 和 cost diagnostics，不能只改其中一层。

deterministic post-only 抽样保持同 seed 同结果；participation cap 不引入随机性。

## 7. 授权、认证与数据安全

无认证/授权变更。本阶段不读取环境凭证、账户、订单、仓位或交易所数据，不导入 live execution 模块。artifact 不包含秘密。

`FS-014` 的验证不得通过真实资金小单完成；后续校准应使用隔离历史 L2/成交数据或 exchange stub。

## 8. 错误处理与幂等

- `target_qty <= 0`：no-fill；
- `reference_price <= 0`：no-fill；
- `bar_volume <= 0`：所有 order type 均 no-fill；
- `max_volume_participation <= 0` 或 `> 1`：backtest 在策略评估前拒绝配置；
- 未支持的 fill-model version：在加载/评估前拒绝；
- simulator 返回 `filled_qty > target_qty`：harness 继续 fail-fast；
- 重复相同输入：结果完全一致。

不使用虚构默认 volume，不把缺数据当成全量成交。

## 9. 状态转换与生命周期

成交状态：

```text
missing/zero liquidity -> no_fill
positive liquidity + cap >= target -> filled
positive liquidity + 0 < cap < target -> partial_fill
post_only probability miss -> no_fill
post_only probability hit -> filled or partial_fill after cap
```

IOC/bounded 的 volume 真源是产生订单的 observation bar volume，因为该值在 submit 时已知；不得使用下一根 bar 的最终 volume 决定 next-open fill。post-only 仍在下一 bar close 解析，可使用该完整 bar volume。

## 10. 缓存与性能

无缓存。每笔订单只增加常数次 Decimal 比较/乘法，复杂度仍为 O(order count)，对 replay 性能影响可忽略。

不得为性能把 Decimal participation 计算改回 float 中间量。

## 11. 日志、监控与审计

FillResult notes 记录 order type、实际/目标数量、volume participation、cap 和 fee/slippage 口径，不记录秘密。Backtest log 增加 fill-model version。

scorecard 限制字段必须明确表达：无 L2 depth、无 queue position、无 spread/impact 校准、固定滑点、OHLCV volume proxy。该字段是证据范围，不是 PASS/FAIL verdict。

## 12. 测试策略

新增 FS-014 对抗测试覆盖：

1. IOC/bounded 缺失或零 volume 必须 no-fill；
2. IOC 超过 1% cap 只部分成交；
3. bounded 超 cap 只部分成交，按 taker fee 且计固定滑点；
4. post-only 命中后仍受 cap，产生 partial fill；
5. IOC/bounded 使用 observation volume，不使用下一 bar 最终 volume；
6. partial fill 只提交实际 position quantity；
7. CostValidator 总成本等于 fee+slippage，scorecard 不双计/漏计；
8. scorecard meta 输出 fill-model/OHLCV/限制；
9. 旧 fill model 不能重启；
10. 非法 participation 配置在 adapter 运行前失败。

更新既有 fill/harness/scorecard 断言，运行 backtest focused/related、FS-003 回归、全量 unit 与 Ruff。

## 13. 迁移、回滚与兼容

这是有意收紧的模型变更：相同策略/数据可能得到更少成交、更高成本和不同 PnL。不得提供恢复“无量全成/成本漏记”的兼容开关。依赖旧精确数值的测试和 artifact 必须更新/失效，而不是钉回乐观结果。

API 层保持 named-argument 向后兼容；`FillResult.slippage_bps` 和 CostDiagnostic 分解字段提供默认值，使独立构造方可渐进迁移。旧 scorecard reader 应忽略新增 meta 字段。

## 14. 配置与环境隔离

不新增环境变量。1% participation cap 固化在 `BacktestConfig` 默认值，可由隔离研究显式下调；上调仍受 `(0,1]` 校验，但任何值都不构成 live 容量校准证据。

命令行研究入口使用 `--max-volume-participation`，默认 `0.01`；该参数会写入
`summary.json` config，便于 artifact 追踪。fill-model version 不提供命令行降级开关。

测试仅使用内存 bars、fake adapter/session，不访问 `.env.*`、数据库、Redis/NATS、Docker/WSL2 或交易所。

## 15. 代码组织与依赖

预计修改：

- `aats/data_platform/replay/backtest/fill_simulator.py`；
- `aats/data_platform/replay/backtest/harness.py`；
- `aats/data_platform/replay/backtest/cost_validator.py`；
- `aats/data_platform/replay/backtest/evidence_scorecard.py`；
- 相关既有测试与新增 `tests/unit/test_fs014_ohlcv_fill_realism.py`；
- 审计、code review、研究/测试现行文档。

不新增第三方依赖，不改 live execution、策略逻辑、数据库或 deployment。

## 16. 文档、运维手册与验收标准

现行文档必须区分：`next_bar_event_v2` 解决因果时间；`ohlcv_participation_cap_v2` 只收敛 OHLCV 代理的全成/成本错误；真正盘口现实性仍需 L2/历史 fill 校准。不得把“partial fill 单测通过”写成“live 容量已验证”。

本阶段验收：

- IOC/bounded 不再在 volume=0 时全成；
- 三种 order type 均受 1% cap，harness 正确处理 partial；
- next-open 流动性代理不读取未来 bar volume；
- cost diagnostics 与 scorecard 包含实际 fee+slippage 且不重复计算；
- artifact 明示 OHLCV 和未覆盖的 L2/queue/impact 边界；
- focused、related、full unit、Ruff、文档链接和 diff check 通过，或准确披露环境阻塞；
- `FS-014` 只更新为部分收敛，`FS-003/G3` 的旧证据重跑和现实性门禁继续 OPEN；
- 真实资金生产继续 NO-GO。

真正关闭 FS-014 仍需：带时间戳的历史 L2/orderbook/trades/真实 fill 数据；订单到达延迟、spread、queue position、limit touch、cancel/replace、partial fill、market impact、波动/时段状态模型；按 symbol/size/regime 校准与 out-of-sample 验证；容量曲线和置信区间；旧策略重跑；独立复核。

实施与验证结果见
[`34-fs-014-ohlcv-fill-realism-containment.md`](../../audit/full_system_2026_08_24/34-fs-014-ohlcv-fill-realism-containment.md)。
