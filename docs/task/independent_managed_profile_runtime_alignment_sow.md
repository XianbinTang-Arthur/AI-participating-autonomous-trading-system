# Independent 合约实盘托管配置对齐修复 SOW

## 背景

本轮针对 Independent 合约实盘链路做前后端与运行时复审时，`tests/integration/test_strategy_runtime_integration.py` 中
`test_managed_derivatives_live_profile_selects_independent_family_for_overlay`
失败，原因是测试仍在断言旧版 `derivatives_live` 托管配置：

- `strategy_hedge_independent_long_entry_threshold == 0.30`
- `strategy_hedge_independent_long_scale_in_threshold == 0.40`

但当前仓库受控契约已经由：

- `configs/strategy_profiles/derivatives_live.yaml`
- `aats/bootstrap/managed_profiles.py`
- `tests/unit/test_env_profiles.py`

共同固定为：

- `long_entry_threshold == 0.22`
- `short_entry_threshold == 0.30`
- `long_scale_in_threshold == 0.34`
- `short_scale_in_threshold == 0.40`
- `independent_rollout_stage == live`
- `min_confirm_ticks == 1`
- `min_score_drawdown_bps == 6.0`

## 目标

1. 保持 Independent 合约实盘运行时代码不被过期测试误判。
2. 让 runtime 集成测试与当前 managed profile 契约一致。
3. 明确 `derivatives_live` 是“Independent 主策略 pinned profile”，而不是旧版对称 0.30/0.40 阈值。

## 边界

- 不调整生产 `derivatives_live` 配置。
- 不更改 Independent 引擎、allocator、recovery 或 operator 查询逻辑。
- 仅修正错误的集成测试断言，并补充当前托管契约的关键断言。

## 方案

1. 更新 `test_managed_derivatives_live_profile_selects_independent_family_for_overlay`：
   - 改为断言当前 `derivatives_live` managed profile 的真实参数；
   - 补充 `strategy_hedge_independent_rollout_stage == "live"`；
   - 补充 `min_confirm_ticks == 1` 与 `min_score_drawdown_bps == 6.0`。
2. 运行受影响的最窄集成测试与全量 unit 测试，确认没有引入回归。

## 验收标准

1. `tests/integration/test_strategy_runtime_integration.py -k independent` 全部通过。
2. WSL2 中 Independent 相关 runtime 集成测试通过。
3. unit 全量回归通过。
