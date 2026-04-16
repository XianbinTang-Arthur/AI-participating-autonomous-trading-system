# Independent 合约实盘 rollout guard 修复

## 背景

Independent 合约实盘链路已经把 family 级执行模式统一到 `independent_books`，而单腿执行会继续下沉为
`independent_long_book` / `independent_short_book`。执行引擎的 live rollout 守卫需要同时识别这两层语义，
否则 legacy / 降级 / 归一化路径里的订单可能绕过 independent overlay 的灰度阶段限制。

## 发现的问题

`aats/services/strategy_overlay_rollout.py` 的 `overlay_mode_from_execution_mode()` 只识别：

- `protective_overlay`
- `opportunistic_overlay`
- `independent_long_book`
- `independent_short_book`

没有识别 family 级 `independent_books`。  
`OrderManager._leg_overlay_rollout_blockers()` 依赖这个 helper 决定是否执行 overlay rollout guard，因此当
`strategy_execution_mode=independent_books` 透传到订单 intent 时，会直接跳过 independent rollout 阻断。

## 修复目标

1. 让共享 helper 正确识别 `independent_books -> independent`
2. 用单测覆盖 direct leg submit 和 normalized order-intent 两条路径
3. 用 runtime 集成测试验证真实 Independent 合约链路在 `dry_run` 阶段会阻断这类订单

## 实施方案

1. 最小修改 `overlay_mode_from_execution_mode()`，补上 `independent_books`
2. 在 `tests/unit/test_order_manager_errors.py` 增加两条回归测试
3. 新增 `tests/integration/test_independent_live_rollout_guard.py`，通过 `build_runtime()` 走真实 execution guard

## 验收标准

- `strategy_execution_mode="independent_books"` 的 derivatives long/short leg 在 `strategy_hedge_independent_rollout_stage="dry_run"` 时必须落成：
  - `status="BLOCKED"`
  - `submission_mode="leg_overlay_rollout_blocked"`
  - `execution_error` 包含 `independent_overlay_rollout_stage_blocks_live_runtime`
- 现有 `independent_long_book` / `independent_short_book` 行为不回归
