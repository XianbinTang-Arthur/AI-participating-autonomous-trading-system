# Independent Blocked Bundle Backfill Runtime Alignment

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 背景

上一轮审查已经确认，Independent 历史 `blocked` bundle 回填链路存在两个和实盘部署环境不一致的点：

1. `scripts/rdp_backfill_independent_blocked_bundles.py` 连接的是 RDP/research 配置路径，而不是 Independent 实盘 runtime 实际使用的交易数据库。
2. `aats/services/execution_engine/bundle_status_backfill.py` 只支持 legacy `order_states`，没有覆盖 converged 执行存储的 `execution_orders`。

这会导致脚本在真实部署环境里经常直接返回 `required_tables_missing`，从而让历史 `review_required` 污染数据无法被回填。

## 目标

1. 让 backfill 脚本和实盘 runtime 走同一套 profile / managed config / database_url 解析链。
2. 让 backfill 同时支持 legacy `order_states` 和 converged `execution_orders`。
3. 用最小回归测试锁定：
   - runtime profile 解析路径
   - converged 表结构下的 `review_required -> blocked` 回填

## 实施方案

### 1. 脚本改成 runtime-first

- 新增 `--profile` 参数，支持 `spot / derivatives / spot_live / derivatives_live`
- 解析顺序与其它 runtime 运维脚本一致：
  - 显式 `--profile`
  - 否则沿用容器内 `AATS_PROFILE`，并补齐 `AATS_STARTUP_PROFILE` / `AATS_ENV_TEMPLATE_PROFILE`
  - 最后调用 `aats.bootstrap.config.load_settings()`
- 使用 `settings.database_url`，不再读取 `aats.data_platform.config`

### 2. 回填服务支持 converged storage

- 保留 legacy `order_states` 路径
- 新增 converged `execution_orders` 路径
- 通过 `strategy_bundle_id` 回查订单并复用 converged repo 的 `OrderState` hydrate 逻辑

### 3. 测试

- unit:
  - legacy schema 保持可用
  - converged schema 可把 historical `review_required` bundle 重新归类为 `blocked`
  - runtime DB 解析 helper 会正确走 profile / `AATS_PROFILE` shim
- integration:
  - 继续复用 `tests/integration/test_independent_bundle_recovery.py`，确认 recovery 端对历史 blocked bundle 的展示语义不回退

## 验收标准

1. `rdp_backfill_independent_blocked_bundles.py` 在 runtime profile 下连接的是交易 runtime DB，而不是 research DB。
2. 在只存在 `execution_orders` 的 schema 下，回填仍能把全 blocked Independent bundle 改成 `blocked`。
3. recovery/operator 侧集成测试继续通过，历史 blocked bundle 不重新进入 recovery 报表。
