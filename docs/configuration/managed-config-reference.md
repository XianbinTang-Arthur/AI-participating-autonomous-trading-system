# Managed Profile 配置说明

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 生效顺序

1. `settings.py` 默认值
2. managed profile 代码基线（运行时语义，不建议在 `.env` 重复）
3. `configs/strategy_profiles/<profile>.yaml` 策略调参
4. 对应 `.env` 里的最小 override

> live profile 还会受到 startup hardening 约束。即使配置文件能被解析，exchange-coupled runtime 也必须满足 Postgres、OKX account/execution、Operator auth、single runtime guard 等硬条件才允许启动。

## 四个托管 profile

### `spot`

- 运行时基线：`guarded_spot_enabled` / `guarded_live`
- 策略调参文件：`configs/strategy_profiles/spot.yaml`
- 默认产品类型：`spot`
- 默认保证金模式：`cash`
- 默认 OKX 模式：`模拟盘`

### `spot_live`

- 运行时基线：`guarded_spot_enabled` / `guarded_live`
- 策略调参文件：`configs/strategy_profiles/spot_live.yaml`
- 默认产品类型：`spot`
- 默认保证金模式：`cash`
- 默认 OKX 模式：`实盘`

### `derivatives`

- 运行时基线：`guarded_derivatives_enabled` / `guarded_live`
- 策略调参文件：`configs/strategy_profiles/derivatives.yaml`
- 默认产品类型：`derivatives`
- 默认保证金模式：`cross`
- 默认 OKX 模式：`模拟盘`

### `derivatives_live`

- 运行时基线：`guarded_derivatives_enabled` / `guarded_live`
- 策略调参文件：`configs/strategy_profiles/derivatives_live.yaml`
- 默认产品类型：`derivatives`
- 默认保证金模式：`cross`
- 默认 OKX 模式：`实盘`

## `.env` 里应该保留什么

- 标的与资金规模
- 数据库、端口、日志目录
- 交易所与 OpenAI 凭证
- 账户级仓位/杠杆/风控上限

## live profile 安全必填项

| 字段 | 要求 |
|------|------|
| `AATS_STORAGE_MODE` | `postgres` |
| `AATS_DATABASE_URL` | 指向对应 live DB |
| `AATS_DATABASE_SINGLE_RUNTIME_GUARD_ENABLED` | `true` |
| `AATS_EXECUTION_BACKEND` | `okx` |
| `AATS_ACCOUNT_BACKEND` | `okx` |
| `AATS_ACCOUNT_READ_ENABLED` | `true` |
| `AATS_OPERATOR_AUTH_ENABLED` | `true` |
| `AATS_OPERATOR_UNSAFE_WRITE_WITHOUT_AUTH` | `false` |
| `AATS_OPERATOR_SESSION_SECRET` | 长随机 secret，不能提交 |
| `AATS_OPERATOR_SESSION_COOKIE_SECURE` | live 环境为 `true` |

live 环境还应确认 active parameter version、gate history、reconciliation 状态和 recovery status。

## 按字段分组的修改指南

### 想改数据库去哪

- 改根目录对应 profile 的 `.env.*` 文件。
- 主要字段：
  - `AATS_DATABASE_URL`
  - `AATS_DATABASE_RUNTIME_LOCK_KEY`
- 现货和合约建议分库；并行运行时 lock key 也要不同。

### 想改端口 / 日志 / 实例隔离去哪

- 改根目录对应 profile 的 `.env.*` 文件。
- 主要字段：
  - `AATS_API_PORT`
  - `AATS_LOG_DIR`
  - `AATS_OPERATOR_SESSION_COOKIE_NAME`

### 想改交易所凭证和会话密钥去哪

- 改根目录对应 profile 的 `.env.*` 文件。
- 主要字段：
  - `AATS_OKX_API_KEY`
  - `AATS_OKX_API_SECRET`
  - `AATS_OKX_API_PASSPHRASE`
  - `AATS_OPERATOR_SESSION_SECRET`
  - `AATS_OPENAI_API_KEY`

### 想改仓位 / 杠杆 / 名义金额上限去哪

- 改根目录对应 profile 的 `.env.*` 文件。
- 现货常改：
  - `AATS_DEFAULT_ORDER_QTY`
  - `AATS_MAX_ABS_POSITION_QTY`
  - `AATS_MAX_NOTIONAL_PER_SYMBOL`
  - `AATS_MAX_OPEN_ORDERS`
- 合约额外常改：
  - `AATS_MAX_TARGET_LEVERAGE`
  - `AATS_DEFAULT_TARGET_LEVERAGE`
  - `AATS_DERIVATIVES_ONLY_REDUCE_TRIGGER_MARGIN_FRACTION`
  - `AATS_DERIVATIVES_AUTO_HALT_MARGIN_USAGE_FRACTION`
  - `AATS_DERIVATIVES_AUTO_HALT_LIQUIDATION_GAP_FRACTION`
  - `AATS_MAX_MARGIN_USAGE_FRACTION`
  - `AATS_LIQUIDATION_BUFFER_FRACTION`

### 想改 AI / 自动换档去哪

- 改 `configs/strategy_profiles/<profile>.yaml`。
- 主要字段：
  - `ai_operating_mode`
  - `ai_provider`
  - `ai_model_name`
  - `ai_timeout_seconds`
  - `ai_degrade_after_failures`
  - `ai_recovery_probe_interval_seconds`
  - `ai_decision_min_confidence`
  - `ai_decision_max_uncertainty`
  - `ai_decision_min_directional_edge`
  - `ai_shadow_mode_enabled`
  - `ai_execution_suggestion_mode`
  - `strategy_profile_auto_control_enabled`
  - `strategy_profile_auto_rollback_enabled`
  - `strategy_profile_emergency_safety_fast_track_enabled`

### 想改 directional 去哪

- 改 `configs/strategy_profiles/<profile>.yaml`。
- 主要字段：
  - `max_decisions_per_minute`
  - `decision_min_interval_seconds_15m`
  - `decision_min_interval_seconds_1h`
  - `decision_min_price_move_bps`
  - `decision_min_momentum_delta`
  - `strategy_short_bias_enabled`
  - `strategy_dynamic_leverage_enabled`
  - `strategy_entry_*`
  - `strategy_short_entry_*`
  - `strategy_scale_in_*`
  - `strategy_short_scale_in_*`
  - `strategy_reversal_*`
  - `strategy_short_reversal_*`
  - `strategy_min_hold_seconds`
  - `strategy_post_close_cooldown_seconds`
  - `strategy_max_fee_drag_ratio`
  - `strategy_max_churn_ratio`

### 想改智能套利（smart_arbitrage）去哪

- 改 `configs/strategy_profiles/<profile>.yaml`。
- 主要字段：
  - `smart_arbitrage_enabled`
  - `smart_arbitrage_basis_entry_bps`
  - `smart_arbitrage_basis_exit_bps`
  - `smart_arbitrage_estimated_cost_bps`
  - `smart_arbitrage_quote_budget_per_trade`
  - `smart_arbitrage_max_pair_notional`
  - `smart_arbitrage_hedge_target_leverage`

### 想改现货网格（spot_grid）去哪

- 改 `configs/strategy_profiles/<profile>.yaml`。
- 主要字段：
  - `spot_grid_enabled`
  - `spot_grid_anchor_lookback_snapshots`
  - `spot_grid_band_bps`
  - `spot_grid_inventory_floor_fraction`
  - `spot_grid_inventory_ceiling_fraction`
  - `spot_grid_rebalance_min_fraction_of_max_qty`
  - `spot_grid_breakout_guard_enabled`

### 想改定投（dca）去哪

- 改 `configs/strategy_profiles/<profile>.yaml`。
- 主要字段：
  - `dca_enabled`
  - `dca_interval_seconds`
  - `dca_quote_budget_per_cycle`
  - `dca_max_position_fraction_of_limit`
  - `dca_pullback_only_enabled`
  - `dca_pullback_entry_bps`

### 想改多策略自动并行 / sleeve 预算去哪

- 改 `configs/strategy_profiles/<profile>.yaml`。
- 主要字段：
  - `strategy_family_active`
  - `strategy_family_auto_selection_enabled`
  - `strategy_sleeve_auto_execution_enabled`
  - `strategy_sleeve_auto_min_budget_multiplier`
  - `strategy_sleeve_auto_reconciliation_contraction_multiplier`
  - `strategy_sleeve_auto_soft_loss_usdt`
  - `strategy_sleeve_auto_hard_loss_usdt`
  - `strategy_sleeve_auto_volatility_cap_enabled`

### 想改试盘守护去哪

- 改 `configs/strategy_profiles/<profile>.yaml`。
- 主要字段：
  - `trial_guard_enabled`
  - `trial_guard_poll_interval_seconds`
  - `trial_guard_lookback_fills`
  - `trial_guard_min_closed_fills`
  - `trial_guard_max_daily_loss_usdt`
  - `trial_guard_max_consecutive_losses`
  - `trial_guard_max_fee_to_notional_ratio`
  - `trial_guard_max_high_slippage_ratio`
  - `trial_guard_max_slow_submit_to_fill_ratio`

## 策略调参应该放哪里

### AI/自动换档

- `ai_operating_mode`
- `ai_provider`
- `ai_model_name`
- `ai_timeout_seconds`
- `ai_degrade_after_failures`
- `ai_recovery_probe_interval_seconds`
- `ai_decision_min_confidence`
- `ai_decision_max_uncertainty`
- `ai_decision_min_directional_edge`
- `ai_shadow_mode_enabled`
- `ai_shadow_evaluation_window`
- `ai_outcome_review_bad_window_threshold`
- `ai_outcome_max_fee_ratio_delta`
- `ai_outcome_max_churn_ratio_delta`
- `ai_execution_suggestion_mode`
- `strategy_profile_auto_control_enabled`
- `strategy_profile_auto_rollback_enabled`
- `strategy_profile_emergency_safety_fast_track_enabled`
- `strategy_profile_emergency_safety_confidence_min`

### 多策略与 sleeve 自动控制

- `strategy_family_active`
- `strategy_family_auto_selection_enabled`
- `strategy_sleeve_auto_execution_enabled`
- `strategy_sleeve_auto_min_budget_multiplier`
- `strategy_sleeve_auto_reconciliation_contraction_multiplier`
- `strategy_sleeve_auto_soft_loss_usdt`
- `strategy_sleeve_auto_hard_loss_usdt`
- `strategy_sleeve_auto_volatility_cap_enabled`
- `smart_arbitrage_enabled`
- `spot_grid_enabled`
- `dca_enabled`

### directional 决策阈值

- `max_decisions_per_minute`
- `decision_min_interval_seconds_15m`
- `decision_min_interval_seconds_1h`
- `decision_min_price_move_bps`
- `decision_min_momentum_delta`
- `strategy_short_bias_enabled`
- `strategy_dynamic_leverage_enabled`
- `strategy_flat_signal_hold_enabled`
- `strategy_flat_exit_microstructure_threshold`
- `strategy_flat_exit_factor_threshold`
- `strategy_flat_exit_ai_edge_threshold`
- `strategy_expected_slippage_bps_fraction`
- `strategy_edge_noise_buffer_bps`
- `strategy_min_net_edge_bps`
- `strategy_entry_allowed_regimes`
- `strategy_entry_min_signal_edge_bps`
- `strategy_entry_alpha_min`
- `strategy_entry_confidence_min`
- `strategy_short_entry_allowed_regimes`
- `strategy_short_entry_min_signal_edge_bps`
- `strategy_short_entry_alpha_min`
- `strategy_short_entry_confidence_min`
- `strategy_scale_in_min_signal_edge_bps`
- `strategy_scale_in_alpha_min`
- `strategy_scale_in_confidence_min`
- `strategy_short_scale_in_min_signal_edge_bps`
- `strategy_short_scale_in_alpha_min`
- `strategy_short_scale_in_confidence_min`
- `strategy_reversal_min_signal_edge_bps`
- `strategy_reversal_alpha_min`
- `strategy_reversal_confidence_min`
- `strategy_short_reversal_min_signal_edge_bps`
- `strategy_short_reversal_alpha_min`
- `strategy_short_reversal_confidence_min`
- `strategy_min_hold_seconds`
- `strategy_post_close_cooldown_seconds`
- `strategy_max_fee_drag_ratio`
- `strategy_max_churn_ratio`
- `strategy_low_edge_threshold_bps`
- `strategy_low_edge_streak_limit`
- `strategy_low_edge_cooldown_seconds`
- `strategy_transient_close_retry_cooldown_seconds`

### 试盘守护

- `trial_guard_enabled`
- `trial_guard_poll_interval_seconds`
- `trial_guard_lookback_fills`
- `trial_guard_min_closed_fills`
- `trial_guard_max_daily_loss_usdt`
- `trial_guard_max_consecutive_losses`
- `trial_guard_max_fee_to_notional_ratio`
- `trial_guard_max_high_slippage_ratio`
- `trial_guard_max_slow_submit_to_fill_ratio`

## 已标记为 deprecated / 不建议继续写入 managed `.env` 的字段

| 字段 | 说明 |
| --- | --- |
| `AATS_CONFIG_PROFILE` | managed profile 启动时不再建议写进 `.env`；由代码按 profile 自动派生。 |
| `AATS_MARKET_DATA_BACKEND / AATS_EXECUTION_BACKEND / AATS_ACCOUNT_BACKEND` | managed profile 启动时由代码自动派生，不建议继续在 `.env` 里覆盖。 |
| `AATS_TRADING_PRODUCT_TYPE / AATS_MARGIN_MODE / AATS_OKX_SIMULATED_TRADING` | managed profile 启动时由代码自动派生，不建议继续在 `.env` 里覆盖。 |
| `AATS_PRIMARY_TIMEFRAME / AATS_SECONDARY_TIMEFRAME` | 当前实现固定为 15m + 1h；保留字段仅为兼容旧配置，不建议继续写入 `.env`。 |

## legacy `configs/*.yaml` 当前职责

- 仍保留给非托管/manual `config_profile` 路径与测试使用
- 托管 profile（`spot/derivatives/spot_live/derivatives_live`）不再叠加这些 YAML
- 新的策略调参统一走 `configs/strategy_profiles/*.yaml`
