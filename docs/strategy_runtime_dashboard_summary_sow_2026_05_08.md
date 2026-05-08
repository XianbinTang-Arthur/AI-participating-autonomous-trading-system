# strategyRuntime dashboard summary read path SOW

## 背景

`strategyRuntime` 已进入 dashboard snapshot plane，但线上仍出现冷刷新超过 5s 的情况。当前 SQL 层 recent/limit 查询本身很快，主要风险来自 snapshot 冷建时仍在组装完整策略运行时 payload：最近多条 coordinator snapshot、预算快照、冲突/netting 明细、sleeve 库存、independent expected-vs-realized 全局诊断、smart-arbitrage realized cost calibration。

前端总览页和策略页实际只需要：

- `summary`
- `entry_execution_guard`
- `family_enablement`
- 少量 `configured_parameters`
- `latest_snapshot.automation_decisions`
- `latest_bundle`
- `latest_applied_target`

完整 `/strategy/runtime` 仍应保留重型诊断字段，供人工下钻和集成测试使用。

## 范围

本次只新增 dashboard summary 读路径：

1. `OperatorQueryService.strategy_runtime_dashboard()`。
2. `StrategyQueryFacade.strategy_runtime_dashboard()`。
3. `dashboard/bundle` request fallback 和 snapshot-plane loader 的 `strategyRuntime` panel 改走 dashboard summary。
4. `_build_strategy_runtime(..., dashboard_summary_only=True)` 跳过 dashboard 不消费的重型 section。

不改变 `/strategy/runtime` 的完整响应语义。

## 验收

1. dashboard snapshot 刷新不再触发完整 `query.strategy_runtime()`。
2. dashboard summary payload 保留策略页和总览页所需字段。
3. 完整 `/strategy/runtime` 仍包含预算快照、归因诊断和 smart-arbitrage cost summary。
4. 增加单元测试覆盖 facade 与 bundle routing。
