# Task112 七条主线公式复审补丁 SOW

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 目标
- 再次复审七条主线后，修掉这轮确认可复现的三类明显口径错误：
  - `dca` 最近一次建仓时间取错，导致间隔判断可能放松。
  - `directional + hedge mode` 下，符号级执行健康把对冲开仓误当成平仓。
  - `smart_arbitrage` 多对聚合时，把不同名义规模的 pair 指标做了简单平均。

## 边界
- 不重构策略协调器、执行引擎或账本架构。
- 不改 public API 字段名。
- 不引入新的费率或外部数据源。

## 涉及主线
- 方向性交易主线：受 hedge-mode 执行健康口径影响。
- 智能套利主线 `smart_arbitrage`：受多对聚合加权口径影响。
- 定投主线 `dca`：受最近一次目标时间窗口影响。
- 现货网格 `spot_grid`、保护性对冲 `protective`、机会型对冲 `opportunistic`、独立双账本 `independent` 本轮复审未发现新的明确公式错误；其中后三条会间接受益于 hedge-mode 健康口径修正。

## 修复点
- `aats/services/strategy_engines/dca.py`
  - `_last_dca_target_at(...)` 改为返回最新匹配时间，而不是第一条匹配历史。
- `aats/services/strategy_execution_health.py`
  - `compute_strategy_execution_health(...)` 新增 hedge-mode 分支，按 long/short 两条腿分别回放，再合并关闭事件与生命周期。
  - 避免把 `short open sell` 误记成 net long 的平仓。
- `aats/services/decision_engine/context_builder.py`
  - 调用执行健康时补传当前 long/short 数量。
- `aats/services/operator/query_service.py`
  - operator 的 `strategy_execution_health` 也补传当前 long/short 数量，保持展示口径一致。
- `aats/services/strategy_engines/smart_arbitrage/engine.py`
  - 多 pair 聚合指标改为按 pair 请求名义规模加权。

## 测试策略
- 单测覆盖：
  - DCA 多历史目标时仍按最新一次间隔判断。
  - hedge-mode 下对冲开仓不会制造假的 closed trade / fee drag。
  - smart arbitrage 多对聚合边际与成本按名义规模加权。
- 集成验证：
  - 运行最窄 operator / strategy runtime 现有集成，确认 API 链路未被破坏。
