# Managed Profile 配置说明

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


> 最后核对：2026-08-25（起始 HEAD `00b6df0f8a8d2665d6cae3e88996843767cd1f56`；Phase 3A–3W 整改提交候选）。本文只描述 managed profile 当前路径；legacy `config_profile` YAML 是 deprecated 兼容路径，不应新增依赖。

## 生效顺序

1. `settings.py` 默认值
2. managed profile 代码基线（运行时语义，不建议在 `.env` 重复）
3. `configs/strategy_profiles/<profile>.yaml` 策略调参；文件必须是 mapping，且每个 key 必须属于 `AATSSettings`，否则启动失败关闭
4. 对应 `.env` 里允许覆盖的最小环境字段
5. `build_runtime()` 从 Postgres `governance.active_parameter_sets` 注入的 active parameters

managed profile 派生字段（环境、mode、storage/backend、产品/保证金/持仓模式、模拟/实盘标识、Operator auth/secure cookie、主副 timeframe 等）即使写入 `.env` 也会被忽略并记录日志。`runtime_profile_resolution()` 当前是 `env_only`，不会从旧管理控制面再次覆盖。

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
- 默认持仓模式：`net`
- 默认 OKX 模式：`模拟盘`

### `derivatives_live`

- 运行时基线：`guarded_derivatives_enabled` / `guarded_live`
- 策略调参文件：`configs/strategy_profiles/derivatives_live.yaml`
- 默认产品类型：`derivatives`
- 默认保证金模式：`cross`
- 默认持仓模式：`hedge`
- 默认 OKX 模式：`实盘`

## `.env` 里应该保留什么

- 标的与本地 paper/demo 规模；exchange-coupled 可用余额必须来自账户快照
- 数据库、端口、日志目录
- 交易所与 OpenAI 凭证
- 账户级仓位/杠杆/风控上限

## live profile 有效安全约束

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

这些是 runtime 必须满足的有效状态，其中 managed 派生字段不应重新写入 `.env`。live 环境还应确认 active parameter version、gate history、reconciliation 状态和 recovery status。

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
- 这些值是风险上限或本地规模种子，不能代替 OKX account snapshot；active parameter 映射字段若在数据库中有 active set，最终以数据库注入值为准。
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
  - `strategy_profile_emergency_safety_fast_track_enabled`

`strategy_profile_auto_control_enabled` 在全部托管 profile 中默认是 `false`，即手动切档。
它是自动换档硬门禁，不是可由页面覆盖的普通默认值：配置关闭时，页面和 API 均不得恢复
自动控制；改为 `true` 后需要按标准部署流程重启，之后操作员才能在允许范围内暂停或恢复。

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
  - `smart_arbitrage_quote_budget_per_trade`（配置上限；实际开仓还会受实时可用权益约束）
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
- `strategy_profile_emergency_safety_fast_track_enabled`
- `strategy_profile_emergency_safety_confidence_min`

上述自动换档主开关默认关闭。页面上的手动/自动选择只管理配置允许范围内的运行态，不能
绕过 `strategy_profile_auto_control_enabled: false`。

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

## managed `.env` 中会被忽略的派生字段

| 字段 | 说明 |
| --- | --- |
| `AATS_CONFIG_PROFILE / AATS_ENVIRONMENT / AATS_STARTUP_PROFILE / AATS_MODE` | 由 managed profile 自动派生；环境 override 被忽略。 |
| `AATS_STORAGE_MODE / AATS_MARKET_DATA_BACKEND / AATS_EXECUTION_BACKEND / AATS_ACCOUNT_BACKEND / AATS_ACCOUNT_READ_ENABLED` | 由 managed profile 自动派生；环境 override 被忽略。 |
| `AATS_LIVE_SUBMIT_ENABLED / AATS_GUARDED_EXECUTION_DRY_RUN / AATS_BOOTSTRAP_PORTFOLIO_FROM_EXCHANGE` | 由 managed profile 自动派生；环境 override 被忽略。 |
| `AATS_TRADING_PRODUCT_TYPE / AATS_MARGIN_MODE / AATS_DERIVATIVES_POSITION_MODE / AATS_DERIVATIVES_HEDGE_TRANSITION_MODE / AATS_DERIVATIVES_REQUIRE_EXCHANGE_POS_MODE_MATCH` | 产品身份由 managed profile 自动派生；环境 override 被忽略。 |
| `AATS_OKX_SIMULATED_TRADING / AATS_OPERATOR_AUTH_ENABLED / AATS_OPERATOR_SESSION_COOKIE_SECURE` | 模拟/实盘与认证身份由 managed profile 自动派生；环境 override 被忽略。 |
| `AATS_PRIMARY_TIMEFRAME / AATS_SECONDARY_TIMEFRAME` | 当前实现固定为 15m + 1h；环境 override 被忽略。 |

## legacy `configs/*.yaml` 当前职责

- 仍保留给非托管/manual `config_profile` 路径与测试使用
- 托管 profile（`spot/derivatives/spot_live/derivatives_live`）不再叠加这些 YAML
- 新的策略调参统一走 `configs/strategy_profiles/*.yaml`

## Active parameter 真源与故障语义

- 主交易 runtime 只读取 Postgres `governance.active_parameter_sets`。
- `active_parameter_registry_path` 和 `configs/active_parameter_sets/*.json` 只保留兼容 API/审计用途，加载路径不再使用文件 fallback。
- 数据库 URL 已配置但加载失败时，loader 返回带 `db_load_failed` 的空 registry 并记录 error；系统退化到 managed/profile 参数。Operator 必须把这种退化当作配置漂移处理。
- active parameters 在普通环境 override 之后注入，因此映射字段的最终值以 active set 为准。
