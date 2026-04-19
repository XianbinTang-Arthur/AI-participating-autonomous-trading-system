# Task 87 - Smart Arbitrage Funding 事件边界估算修复

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 背景

此前 `smart_arbitrage` 的 funding 事件数使用“预计持有时长 / funding 间隔”机械向上取整，最少记 1 次。这样会把“持有窗口还没跨过下一次 funding 结算边界”的场景估得过重，而且无法跟随 OKX 动态 funding 周期。

## 本次修复

- 将 funding 事件估算改为：
  - 优先保留显式配置的 `smart_arbitrage_expected_funding_events`
  - 如果账户服务拿到了 OKX public funding-rate 的 `fundingTime/nextFundingTime`，优先按交易所真实 schedule 估算
  - 否则才回退到本地按 `smart_arbitrage_funding_interval_hours` 投影下一次 funding 边界
  - 只有持有窗口跨过该边界时才计入 funding 事件
- 当持有窗口内没有 projected funding 结算事件时，funding 成本直接记为 `0`
- 将 funding proxy 读取和 fee schedule 读取解耦，避免 `funding_source_mode=account_proxy` 时被 `fee_source_mode` 误伤
- 保留原有 fallback 语义：
  - 如果没有可用的 funding 间隔或持有时长信息，仍允许沿用 legacy total funding 成本口径

## 影响

- `smart_arbitrage` 在短持有窗口场景下，不会再因为 funding 成本被机械计入而把净边际压低
- `smart_arbitrage_cost_summary.predicted.expected_funding_events` 与 `funding_cost_bps` 会更贴近当前持有窗口
- `cost_source_flags` 会区分 `funding_schedule_exchange_actual` 和 `funding_schedule_projected_from_config`

## 已知限制

- 如果账户服务当前没有拿到 funding-rate 数据，仍会回退到本地投影逻辑
- 这次实现走的是 OKX public REST funding-rate，不是 public websocket funding-rate 频道
