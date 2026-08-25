# 34 FS-014 OHLCV 成交现实性风险收敛记录

> 文档状态：现行整改证据  
> 阶段：Phase 3N  
> 核对日期：2026-08-24  
> 起始基线：`00b6df0f8a8d2665d6cae3e88996843767cd1f56`  
> 工作分支：`codex/fs-002-kill-switch-p0`，变更尚未提交  
> 验证边界：纯内存 OHLCV、fake adapter/session 与单元测试；未读取 `.env.*`，未连接数据库、Redis、NATS、交易所或账户，未启动容器，未部署  
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 结论

Phase 3N 消除了 FS-014 中四个可由现有 OHLCV 输入确定性修正的乐观错误：

1. IOC 和 bounded-limit 不再忽略成交量并无条件全量成交；
2. post-only 概率命中后不再绕过容量约束；
3. bounded-limit 不再使用未经撮合证据支持的 maker/taker 混合费率和零滑点；
4. cost-adjusted edge 不再漏记 FillSimulator 已经施加到成交价的固定滑点。

现行模型固定为 `ohlcv_participation_cap_v2`。三种订单都要求正成交量并受默认
1% bar-volume participation cap；超出部分产生 partial fill。IOC/bounded 在下一 bar
open 解析时只使用下单前已闭合 observation bar 的 volume，避免读取下一根 bar 的
未来完整成交量；post-only 在下一 bar close 解析，可使用该完整 bar volume。

这只是明确标注的 OHLCV 代理，不是 L2 撮合模型。FS-014 因此更新为：

**PARTIALLY REMEDIATED / OHLCV CONTAINED / L2 CALIBRATION OPEN**。

## 2. 原始缺陷与因果链

整改前的 `FillSimulator` 对 IOC 直接返回 `target_qty`，bounded-limit 直接全量并按
固定混合费率计价；harness 对两者传入零 volume 仍得到成交。post-only 虽按
`target_qty / bar_volume` 决定概率，命中后仍全量成交。与此同时，IOC fill price
已经包含方向性固定滑点，但 CostValidator 只接收 `fee_bps`，使成本诊断与 PnL 的
口径不一致。

可信后果是：成交率、容量和净 edge 同时被高估；边际较薄的策略可能在 artifact
中保持正值，实际考虑容量与成本后却翻负。由于该路径是离线证据生成链，不需要用
真实资金复现。

## 3. 实施内容

### 3.1 FillSimulator

- 固定 `FILL_MODEL_VERSION = "ohlcv_participation_cap_v2"`；
- 新增 `max_volume_participation`，默认 `Decimal("0.01")`；
- 所有订单类型在 `bar_volume <= 0` 时统一 no-fill；
- IOC、bounded-limit 和 post-only 命中结果都取
  `min(target_qty, bar_volume × participation cap)`；
- IOC/bounded 统一按 taker fee 和方向性 fixed slippage 计价；
- `FillResult` 显式保存 `slippage_bps`；
- notes 保存 full/partial、实际/目标数量和 participation，不记录秘密。

### 3.2 Harness 与因果流动性

- `BacktestConfig` 固定 fill-model version，并增加 participation 配置；
- CLI 用 Decimal `--max-volume-participation` 暴露同一 cap，并随 config 写入 artifact；
- 不支持的 model version 或不在 `(0, 1]` 的 cap 在加载 Gold bars 前失败；
- pending order 保存产生订单时已经闭合的 observation volume；
- IOC/bounded 的 next-open fill 只消费该 observation volume；
- post-only 继续在 next-close 消费 execution bar volume；
- timeline 新增 `liquidity_source`，逐笔区分
  `observation_bar_volume` 与 `next_bar_volume`；
- partial fill 继续只提交实际数量到 PositionTracker 与 ReplayState。

### 3.3 成本与证据 artifact

- `actual_cost_bps = actual_fee_bps + actual_slippage_bps`；
- CostDiagnostic 增加可选 fee/slippage 分项，旧构造方保持可读；
- scorecard 优先聚合实际分项，只有旧 diagnostic 缺字段时才使用兼容回退；
- scorecard meta 新增 fill model、`market_data_granularity=ohlcv` 和固定限制清单：
  无 L2 depth、无 spread/queue position、无 market-impact 校准、固定滑点、仅
  volume-participation proxy。

## 4. 防御性验证

新增 `tests/unit/test_fs014_ohlcv_fill_realism.py`，并扩展 FS-003 因果测试与既有
fill/harness/scorecard/CLI 回归。关键对抗断言包括：

1. IOC、post-only、bounded-limit 在零 volume 时全部 no-fill；
2. 目标数量 5、bar volume 100、cap 1% 时只成交 1；
3. bounded sell 同时承受不利方向滑点和 taker fee；
4. post-only 概率命中仍只能 partial fill；
5. observation volume 为 50、下一 bar volume 为 1,000,000 时，IOC 仍只按 50
   计算 cap，不读取未来量；
6. 旧 fill-model version、零 cap 和大于 100% 的 cap 都在 market-data loader 前失败；
7. fee/slippage 分项能独立保留，scorecard 不被错误的全局滑点默认值覆盖；
8. meta 明示模型版本、数据粒度和五项现实性限制。

## 5. 测试记录

### 5.1 定向与扩大相关回归

```text
106 passed, 1 warning in 0.55s
119 passed, 1 warning in 0.47s
```

警告均为现有 `.pytest_cache` Windows 创建告警，不是测试断言失败。

### 5.2 仓库规定的原样全量命令

```text
.venv\Scripts\python.exe -m pytest tests/unit/ -x -q
87 passed, 1 error in 3.54s
```

唯一 error 发生在 `tmp_path` fixture setup：Windows 系统临时目录
`C:\Users\...\AppData\Local\Temp\pytest-of-...` 返回 `PermissionError [WinError 5]`。
在错误发生前没有 test assertion failure。

### 5.3 仓库内全新 basetemp 复跑

```text
4329 passed, 30 skipped, 1666 warnings, 85 subtests passed in 98.83s
```

30 项 skip 未计作覆盖。1666 条 warning 主要来自既存 SQLite datetime adapter
deprecation 和 long-short poller AsyncMock 未 await；它们继续由 FS-021 测试治理风险
承接，不能写成已修复。

### 5.4 Lint

```text
.venv\Scripts\python.exe -m ruff check aats/ --fix
All checks passed!
```

针对本阶段修改代码与测试的 Ruff 也通过。

## 6. 未执行验证

本阶段没有需要真实依赖的最窄 integration suite；未启动 WSL2、Docker 或任何
runtime，也未连接 Gold 数据库。没有使用真实 L2、历史 trades、订单、成交或账户
数据做校准。没有重跑仓库外历史策略 artifact，也没有完成独立 reviewer 复核。

因此以上结果只证明代码契约和隔离行为，不证明实际成交率、容量、收益或生产环境
状态。

## 7. 剩余关闭条件

真正关闭 FS-014 至少还需要：

1. 带时间戳的历史 L2/orderbook/trades 与真实 fill 数据；
2. 订单到达延迟、spread、queue position、limit touch、cancel/replace、partial fill
   和 market impact 模型；
3. 按 symbol、size、时段与 volatility regime 校准，并做严格 out-of-sample 验证；
4. 容量曲线、误差带和保守置信区间；
5. 盘点并失效旧 fill-model artifact，按 `next_bar_event_v2` +
   `ohlcv_participation_cap_v2` 或更高证据等级重跑；
6. 独立人工复核，并保持 G3 在所有证据完成前未放行。

任何上述缺口都不能通过调高 participation cap、恢复全量成交、用真实资金小单或把
warning/skip 忽略为“通过”来绕过。

## 8. 当前裁定

已收敛：无量全成、bar proxy 无容量上限、bounded 乐观混合费率/零滑点、成本诊断
漏记固定滑点、artifact 不声明现实性边界。

未收敛：L2/queue/spread/impact/latency 校准、历史证据重跑、容量置信区间、目标环境
与独立复核。

**FS-014：PARTIALLY REMEDIATED / OHLCV CONTAINED / L2 CALIBRATION OPEN。**  
**G3：PARTIAL / 未放行。**  
**REAL-MONEY PRODUCTION：NO-GO。**
