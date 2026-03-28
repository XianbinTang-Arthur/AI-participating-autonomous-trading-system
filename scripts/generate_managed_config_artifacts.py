from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent

from aats.bootstrap.managed_profiles import (
    MANAGED_PROFILE_DEFINITIONS,
    MANAGED_PROFILE_DERIVED_ENV_KEYS,
    ManagedEnvProfile,
)


@dataclass(frozen=True, slots=True)
class EnvFieldSpec:
    key: str
    tag: str
    comment: str
    allowed_values: str
    example_value: str


@dataclass(frozen=True, slots=True)
class EnvSectionSpec:
    title: str
    intro: str
    fields: tuple[EnvFieldSpec, ...]


def env_field(
    key: str,
    *,
    tag: str,
    comment: str,
    allowed_values: str,
    recommended_value: str,
) -> EnvFieldSpec:
    return EnvFieldSpec(
        key=key,
        tag=tag,
        comment=comment,
        allowed_values=allowed_values,
        example_value=recommended_value,
    )


COMMON_RUNTIME_FIELDS: tuple[EnvSectionSpec, ...] = (
    EnvSectionSpec(
        title="交易标的与账户规模",
        intro="这里放你最常改、且和账户资金/交易标的直接相关的 override。",
        fields=(
            env_field(
                "AATS_DEFAULT_SYMBOL",
                tag="运行必改",
                comment="默认交易标的。改标的先改这里。",
                allowed_values="交易所支持的现货或合约符号",
                recommended_value="BTC-USDT",
            ),
            env_field(
                "AATS_ALLOWED_SYMBOLS",
                tag="常用可调",
                comment="允许交易的标的列表。单标的运行建议只保留一个。",
                allowed_values="JSON 数组；推荐单标的只保留一个",
                recommended_value="[\"BTC-USDT\"]",
            ),
            env_field(
                "AATS_INITIAL_USDT_BALANCE",
                tag="常用可调",
                comment="本地组合初始 USDT 口径。实盘建议填你准备给这套系统使用的资金规模。",
                allowed_values="正数",
                recommended_value="100.0",
            ),
        ),
    ),
    EnvSectionSpec(
        title="数据库与运行实例",
        intro="这里放每个运行实例都不同的基础设施参数。",
        fields=(
            env_field(
                "AATS_DATABASE_URL",
                tag="运行必改",
                comment="PostgreSQL 连接串。现货/合约建议分库。",
                allowed_values="合法的 postgresql+psycopg 连接串",
                recommended_value="postgresql+psycopg://postgres:123456@localhost:5432/aats_example",
            ),
            env_field(
                "AATS_DATABASE_RUNTIME_LOCK_KEY",
                tag="运行必改",
                comment="数据库单实例锁键。现货/合约请保持不同。",
                allowed_values="正整数",
                recommended_value="42420001",
            ),
            env_field(
                "AATS_API_PORT",
                tag="运行必改",
                comment="API/UI 端口。并行跑多个实例时必须不同。",
                allowed_values="未占用端口",
                recommended_value="8000",
            ),
            env_field(
                "AATS_LOG_DIR",
                tag="运行必改",
                comment="日志目录。建议按 profile 分开。",
                allowed_values="相对或绝对路径",
                recommended_value="logs/example",
            ),
        ),
    ),
    EnvSectionSpec(
        title="交易所与凭证",
        intro="这里只放密钥与会话密钥，不放策略调参。",
        fields=(
            env_field(
                "AATS_OPENAI_API_KEY",
                tag="按需填写",
                comment="OpenAI 密钥。只有 strategy profile 里 ai_provider=openai 时才会实际使用。",
                allowed_values="有效的 OpenAI API Key；若 AI provider=disabled 可留占位值",
                recommended_value="REPLACE_WITH_OPENAI_API_KEY",
            ),
            env_field(
                "AATS_OKX_API_KEY",
                tag="运行必改",
                comment="OKX API Key。",
                allowed_values="与当前交易环境匹配的真实或模拟盘 Key",
                recommended_value="REPLACE_WITH_REAL_OKX_API_KEY",
            ),
            env_field(
                "AATS_OKX_API_SECRET",
                tag="运行必改",
                comment="OKX API Secret。",
                allowed_values="与当前 OKX API Key 配套的 Secret",
                recommended_value="REPLACE_WITH_REAL_OKX_API_SECRET",
            ),
            env_field(
                "AATS_OKX_API_PASSPHRASE",
                tag="运行必改",
                comment="OKX API Passphrase。",
                allowed_values="与当前 OKX API Key 配套的 Passphrase",
                recommended_value="REPLACE_WITH_REAL_OKX_API_PASSPHRASE",
            ),
            env_field(
                "AATS_OPERATOR_SESSION_SECRET",
                tag="运行必改",
                comment="Operator 会话密钥。请换成足够长的随机串。",
                allowed_values="足够长的随机字符串",
                recommended_value="REPLACE_WITH_LONG_RANDOM_OPERATOR_SESSION_SECRET",
            ),
            env_field(
                "AATS_OPERATOR_SESSION_COOKIE_NAME",
                tag="常用可调",
                comment="Operator cookie 名称。并行跑多个实例时建议不同。",
                allowed_values="自定义字符串",
                recommended_value="aats_operator_session_example",
            ),
        ),
    ),
)


PROFILE_SPECIFIC_FIELDS: dict[ManagedEnvProfile, tuple[EnvSectionSpec, ...]] = {
    "spot": (
        EnvSectionSpec(
            title="现货仓位与下单上限",
            intro="现货 cash 模式固定为 1x；杠杆不在这里暴露。",
            fields=(
                env_field("AATS_DEFAULT_ORDER_QTY", tag="常用可调", comment="默认单笔下单数量。", allowed_values="正数", recommended_value="0.0005"),
                env_field("AATS_MAX_ABS_POSITION_QTY", tag="常用可调", comment="单标的最大绝对持仓数量。", allowed_values="正数", recommended_value="0.003"),
                env_field("AATS_MAX_NOTIONAL_PER_SYMBOL", tag="常用可调", comment="单标的最大名义金额。", allowed_values="正数", recommended_value="100000"),
                env_field("AATS_MAX_OPEN_ORDERS", tag="常用可调", comment="最多同时挂单数。", allowed_values="1 ~ 20", recommended_value="2"),
            ),
        ),
    ),
    "spot_live": (
        EnvSectionSpec(
            title="现货实盘仓位与下单上限",
            intro="现货 cash 模式固定为 1x；若想调整风格，请改 strategy_profiles/spot_live.yaml。",
            fields=(
                env_field("AATS_DEFAULT_ORDER_QTY", tag="常用可调", comment="默认单笔下单数量。", allowed_values="正数", recommended_value="0.01"),
                env_field("AATS_MAX_ABS_POSITION_QTY", tag="常用可调", comment="单标的最大绝对持仓数量。", allowed_values="正数", recommended_value="0.02"),
                env_field("AATS_MAX_NOTIONAL_PER_SYMBOL", tag="常用可调", comment="单标的最大名义金额。", allowed_values="正数", recommended_value="1000"),
                env_field("AATS_MAX_OPEN_ORDERS", tag="常用可调", comment="最多同时挂单数。", allowed_values="1 ~ 20", recommended_value="5"),
            ),
        ),
    ),
    "derivatives": (
        EnvSectionSpec(
            title="合约仓位、杠杆与风控上限",
            intro="这里只放账户级风险边界；进出场阈值和自动换档去 strategy profile 文件改。",
            fields=(
                env_field("AATS_DEFAULT_ORDER_QTY", tag="常用可调", comment="默认单笔下单数量。", allowed_values="正数", recommended_value="0.01"),
                env_field("AATS_MAX_ABS_POSITION_QTY", tag="常用可调", comment="单标的最大绝对持仓数量。", allowed_values="正数", recommended_value="0.08"),
                env_field("AATS_MAX_NOTIONAL_PER_SYMBOL", tag="常用可调", comment="单标的最大名义金额。", allowed_values="正数", recommended_value="100000"),
                env_field("AATS_MAX_OPEN_ORDERS", tag="常用可调", comment="最多同时挂单数。", allowed_values="1 ~ 20", recommended_value="3"),
                env_field("AATS_MAX_TARGET_LEVERAGE", tag="常用可调", comment="最大目标杠杆。", allowed_values="1.0 ~ 125.0（受交易所和代码约束）", recommended_value="8"),
                env_field("AATS_DEFAULT_TARGET_LEVERAGE", tag="常用可调", comment="默认目标杠杆。", allowed_values="1.0 ~ AATS_MAX_TARGET_LEVERAGE", recommended_value="3"),
                env_field("AATS_DERIVATIVES_ONLY_REDUCE_TRIGGER_MARGIN_FRACTION", tag="常用可调", comment="达到该保证金占用比例后只允许减仓。", allowed_values="0.0 ~ 1.0", recommended_value="0.70"),
                env_field("AATS_DERIVATIVES_AUTO_HALT_MARGIN_USAGE_FRACTION", tag="常用可调", comment="达到该保证金占用比例后自动暂停。", allowed_values="0.0 ~ 1.0", recommended_value="0.85"),
                env_field("AATS_DERIVATIVES_AUTO_HALT_LIQUIDATION_GAP_FRACTION", tag="常用可调", comment="距强平过近时自动暂停。", allowed_values="0.0 ~ 1.0", recommended_value="0.08"),
                env_field("AATS_MAX_MARGIN_USAGE_FRACTION", tag="常用可调", comment="预估保证金占用上限。", allowed_values="0.0 ~ 1.0", recommended_value="0.85"),
                env_field("AATS_LIQUIDATION_BUFFER_FRACTION", tag="常用可调", comment="强平缓冲比例。", allowed_values="0.0 ~ 1.0", recommended_value="0.15"),
            ),
        ),
    ),
    "derivatives_live": (
        EnvSectionSpec(
            title="合约实盘仓位、杠杆与风控上限",
            intro="这里只放账户级风险边界；进出场阈值和自动换档去 strategy_profiles/derivatives_live.yaml 改。",
            fields=(
                env_field("AATS_DEFAULT_ORDER_QTY", tag="常用可调", comment="默认单笔下单数量。", allowed_values="正数", recommended_value="0.01"),
                env_field("AATS_MAX_ABS_POSITION_QTY", tag="常用可调", comment="单标的最大绝对持仓数量。", allowed_values="正数", recommended_value="0.02"),
                env_field("AATS_MAX_NOTIONAL_PER_SYMBOL", tag="常用可调", comment="单标的最大名义金额。", allowed_values="正数", recommended_value="1000"),
                env_field("AATS_MAX_OPEN_ORDERS", tag="常用可调", comment="最多同时挂单数。", allowed_values="1 ~ 20", recommended_value="5"),
                env_field("AATS_MAX_TARGET_LEVERAGE", tag="常用可调", comment="最大目标杠杆。", allowed_values="1.0 ~ 125.0（受交易所和代码约束）", recommended_value="8"),
                env_field("AATS_DEFAULT_TARGET_LEVERAGE", tag="常用可调", comment="默认目标杠杆。", allowed_values="1.0 ~ AATS_MAX_TARGET_LEVERAGE", recommended_value="3"),
                env_field("AATS_DERIVATIVES_ONLY_REDUCE_TRIGGER_MARGIN_FRACTION", tag="常用可调", comment="达到该保证金占用比例后只允许减仓。", allowed_values="0.0 ~ 1.0", recommended_value="0.65"),
                env_field("AATS_DERIVATIVES_AUTO_HALT_MARGIN_USAGE_FRACTION", tag="常用可调", comment="达到该保证金占用比例后自动暂停。", allowed_values="0.0 ~ 1.0", recommended_value="0.75"),
                env_field("AATS_DERIVATIVES_AUTO_HALT_LIQUIDATION_GAP_FRACTION", tag="常用可调", comment="距强平过近时自动暂停。", allowed_values="0.0 ~ 1.0", recommended_value="0.10"),
                env_field("AATS_MAX_MARGIN_USAGE_FRACTION", tag="常用可调", comment="预估保证金占用上限。", allowed_values="0.0 ~ 1.0", recommended_value="0.75"),
                env_field("AATS_LIQUIDATION_BUFFER_FRACTION", tag="常用可调", comment="强平缓冲比例。", allowed_values="0.0 ~ 1.0", recommended_value="0.20"),
            ),
        ),
    ),
}


DEPRECATED_FIELD_ROWS = (
    (
        "AATS_CONFIG_PROFILE",
        "managed profile 启动时不再建议写进 `.env`；由代码按 profile 自动派生。",
    ),
    (
        "AATS_MARKET_DATA_BACKEND / AATS_EXECUTION_BACKEND / AATS_ACCOUNT_BACKEND",
        "managed profile 启动时由代码自动派生，不建议继续在 `.env` 里覆盖。",
    ),
    (
        "AATS_TRADING_PRODUCT_TYPE / AATS_MARGIN_MODE / AATS_OKX_SIMULATED_TRADING",
        "managed profile 启动时由代码自动派生，不建议继续在 `.env` 里覆盖。",
    ),
    (
        "AATS_PRIMARY_TIMEFRAME / AATS_SECONDARY_TIMEFRAME",
        "当前实现固定为 15m + 1h；保留字段仅为兼容旧配置，不建议继续写入 `.env`。",
    ),
)


STRATEGY_FIELD_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "AI/自动换档",
        (
            "ai_operating_mode",
            "ai_provider",
            "ai_model_name",
            "ai_timeout_seconds",
            "ai_degrade_after_failures",
            "ai_recovery_probe_interval_seconds",
            "ai_decision_min_confidence",
            "ai_decision_max_uncertainty",
            "ai_decision_min_directional_edge",
            "ai_shadow_mode_enabled",
            "ai_shadow_evaluation_window",
            "ai_outcome_review_bad_window_threshold",
            "ai_outcome_max_fee_ratio_delta",
            "ai_outcome_max_churn_ratio_delta",
            "ai_execution_suggestion_mode",
            "strategy_profile_auto_control_enabled",
            "strategy_profile_auto_rollback_enabled",
            "strategy_profile_emergency_safety_fast_track_enabled",
            "strategy_profile_emergency_safety_confidence_min",
        ),
    ),
    (
        "多策略与 sleeve 自动控制",
        (
            "strategy_family_active",
            "strategy_family_auto_selection_enabled",
            "strategy_sleeve_auto_parallel_enabled",
            "strategy_sleeve_auto_min_budget_multiplier",
            "strategy_sleeve_auto_reconciliation_contraction_multiplier",
            "strategy_sleeve_auto_soft_loss_usdt",
            "strategy_sleeve_auto_hard_loss_usdt",
            "strategy_sleeve_auto_volatility_cap_enabled",
            "smart_arbitrage_enabled",
            "spot_grid_enabled",
            "dca_enabled",
        ),
    ),
    (
        "directional 决策阈值",
        (
            "max_decisions_per_minute",
            "decision_min_interval_seconds_15m",
            "decision_min_interval_seconds_1h",
            "decision_min_price_move_bps",
            "decision_min_momentum_delta",
            "strategy_short_bias_enabled",
            "strategy_dynamic_leverage_enabled",
            "strategy_flat_signal_hold_enabled",
            "strategy_flat_exit_microstructure_threshold",
            "strategy_flat_exit_factor_threshold",
            "strategy_flat_exit_ai_edge_threshold",
            "strategy_expected_slippage_bps_fraction",
            "strategy_edge_noise_buffer_bps",
            "strategy_min_net_edge_bps",
            "strategy_entry_allowed_regimes",
            "strategy_entry_min_signal_edge_bps",
            "strategy_entry_alpha_min",
            "strategy_entry_confidence_min",
            "strategy_short_entry_allowed_regimes",
            "strategy_short_entry_min_signal_edge_bps",
            "strategy_short_entry_alpha_min",
            "strategy_short_entry_confidence_min",
            "strategy_scale_in_min_signal_edge_bps",
            "strategy_scale_in_alpha_min",
            "strategy_scale_in_confidence_min",
            "strategy_short_scale_in_min_signal_edge_bps",
            "strategy_short_scale_in_alpha_min",
            "strategy_short_scale_in_confidence_min",
            "strategy_reversal_min_signal_edge_bps",
            "strategy_reversal_alpha_min",
            "strategy_reversal_confidence_min",
            "strategy_short_reversal_min_signal_edge_bps",
            "strategy_short_reversal_alpha_min",
            "strategy_short_reversal_confidence_min",
            "strategy_min_hold_seconds",
            "strategy_post_close_cooldown_seconds",
            "strategy_max_fee_drag_ratio",
            "strategy_max_churn_ratio",
            "strategy_low_edge_threshold_bps",
            "strategy_low_edge_streak_limit",
            "strategy_low_edge_cooldown_seconds",
            "strategy_transient_close_retry_cooldown_seconds",
        ),
    ),
    (
        "试盘守护",
        (
            "trial_guard_enabled",
            "trial_guard_poll_interval_seconds",
            "trial_guard_lookback_fills",
            "trial_guard_min_closed_fills",
            "trial_guard_max_daily_loss_usdt",
            "trial_guard_max_consecutive_losses",
            "trial_guard_max_fee_to_notional_ratio",
            "trial_guard_max_high_slippage_ratio",
            "trial_guard_max_slow_submit_to_fill_ratio",
        ),
    ),
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate managed config templates and docs.")
    parser.add_argument(
        "--sync-local",
        action="store_true",
        help="同步覆盖本地四个 .env 模板（会保留已有键值）。",
    )
    return parser.parse_args()


def _read_existing_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return {
        key: value
        for key, value in dotenv_values(path).items()
        if key is not None and value is not None
    }


def _render_env(profile: ManagedEnvProfile, values: dict[str, str]) -> str:
    definition = MANAGED_PROFILE_DEFINITIONS[profile]
    lines: list[str] = [
        f"# AATS {profile} 最小 override 模板",
        "# 这个文件只放用户/账户私有 override，不再复制整份 settings。",
        f"# 运行时基线由代码内 managed profile 自动派生：{definition.runtime_defaults['config_profile']} / {definition.runtime_defaults['mode']}",
        f"# 策略调参请改：{definition.strategy_tuning_relative_path}",
        "# 下面没有写到的字段，会回落到 settings.py 默认值或该 profile 的代码基线。",
        "",
    ]
    for section in COMMON_RUNTIME_FIELDS + PROFILE_SPECIFIC_FIELDS[profile]:
        lines.append(f"# {section.title}")
        lines.append(f"# {section.intro}")
        for field in section.fields:
            lines.append(
                f"# [{field.tag}] {field.key}：{field.comment}可选值：{field.allowed_values}；推荐值：{field.example_value}。"
            )
            lines.append(f"{field.key}={values.get(field.key, field.example_value)}")
        lines.append("")
    lines.append("# 提示")
    lines.append("# 1. 不要再把 AATS_CONFIG_PROFILE、AATS_TRADING_PRODUCT_TYPE、AATS_MARGIN_MODE 写回这个文件。")
    lines.append("# 2. 若要调节 AI、自动换档、directional / smart_arbitrage / spot_grid / dca，请去对应 strategy_profiles/*.yaml。")
    lines.append("")
    return "\n".join(lines)


def _write_env_file(path: Path, content: str) -> None:
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


def _example_values_for_profile(profile: ManagedEnvProfile) -> dict[str, str]:
    examples: dict[str, str] = {}
    if profile == "spot":
        examples.update(
            {
                "AATS_DEFAULT_SYMBOL": "BTC-USDT",
                "AATS_ALLOWED_SYMBOLS": "[\"BTC-USDT\"]",
                "AATS_INITIAL_USDT_BALANCE": "750000",
                "AATS_DATABASE_URL": "postgresql+psycopg://postgres:123456@localhost:5432/aats_spot",
                "AATS_DATABASE_RUNTIME_LOCK_KEY": "42420011",
                "AATS_API_PORT": "8000",
                "AATS_LOG_DIR": "logs/spot",
                "AATS_DEFAULT_ORDER_QTY": "0.001",
                "AATS_MAX_ABS_POSITION_QTY": "0.003",
                "AATS_MAX_NOTIONAL_PER_SYMBOL": "100000",
                "AATS_MAX_OPEN_ORDERS": "2",
                "AATS_OPERATOR_SESSION_COOKIE_NAME": "aats_operator_session_spot",
            }
        )
    elif profile == "spot_live":
        examples.update(
            {
                "AATS_DEFAULT_SYMBOL": "BTC-USDT",
                "AATS_ALLOWED_SYMBOLS": "[\"BTC-USDT\"]",
                "AATS_INITIAL_USDT_BALANCE": "100",
                "AATS_DATABASE_URL": "postgresql+psycopg://postgres:123456@localhost:5432/aats_live_spot",
                "AATS_DATABASE_RUNTIME_LOCK_KEY": "42420011",
                "AATS_API_PORT": "8010",
                "AATS_LOG_DIR": "logs/live_spot",
                "AATS_DEFAULT_ORDER_QTY": "0.01",
                "AATS_MAX_ABS_POSITION_QTY": "0.02",
                "AATS_MAX_NOTIONAL_PER_SYMBOL": "1000",
                "AATS_MAX_OPEN_ORDERS": "5",
                "AATS_OPERATOR_SESSION_COOKIE_NAME": "aats_operator_session_spot_live",
            }
        )
    elif profile == "derivatives":
        examples.update(
            {
                "AATS_DEFAULT_SYMBOL": "BTC-USDT-SWAP",
                "AATS_ALLOWED_SYMBOLS": "[\"BTC-USDT-SWAP\"]",
                "AATS_INITIAL_USDT_BALANCE": "750000",
                "AATS_DATABASE_URL": "postgresql+psycopg://postgres:123456@localhost:5432/aats_derivatives",
                "AATS_DATABASE_RUNTIME_LOCK_KEY": "42420021",
                "AATS_API_PORT": "8001",
                "AATS_LOG_DIR": "logs/derivatives",
                "AATS_DEFAULT_ORDER_QTY": "0.01",
                "AATS_MAX_ABS_POSITION_QTY": "0.08",
                "AATS_MAX_NOTIONAL_PER_SYMBOL": "100000",
                "AATS_MAX_OPEN_ORDERS": "3",
                "AATS_MAX_TARGET_LEVERAGE": "8",
                "AATS_DEFAULT_TARGET_LEVERAGE": "3",
                "AATS_DERIVATIVES_ONLY_REDUCE_TRIGGER_MARGIN_FRACTION": "0.70",
                "AATS_DERIVATIVES_AUTO_HALT_MARGIN_USAGE_FRACTION": "0.85",
                "AATS_DERIVATIVES_AUTO_HALT_LIQUIDATION_GAP_FRACTION": "0.08",
                "AATS_MAX_MARGIN_USAGE_FRACTION": "0.85",
                "AATS_LIQUIDATION_BUFFER_FRACTION": "0.15",
                "AATS_OPERATOR_SESSION_COOKIE_NAME": "aats_operator_session_derivatives",
            }
        )
    else:
        examples.update(
            {
                "AATS_DEFAULT_SYMBOL": "BTC-USDT-SWAP",
                "AATS_ALLOWED_SYMBOLS": "[\"BTC-USDT-SWAP\"]",
                "AATS_INITIAL_USDT_BALANCE": "100",
                "AATS_DATABASE_URL": "postgresql+psycopg://postgres:123456@localhost:5432/aats_live_derivatives",
                "AATS_DATABASE_RUNTIME_LOCK_KEY": "42420021",
                "AATS_API_PORT": "8011",
                "AATS_LOG_DIR": "logs/live_derivatives",
                "AATS_DEFAULT_ORDER_QTY": "0.01",
                "AATS_MAX_ABS_POSITION_QTY": "0.02",
                "AATS_MAX_NOTIONAL_PER_SYMBOL": "1000",
                "AATS_MAX_OPEN_ORDERS": "5",
                "AATS_MAX_TARGET_LEVERAGE": "8",
                "AATS_DEFAULT_TARGET_LEVERAGE": "3",
                "AATS_DERIVATIVES_ONLY_REDUCE_TRIGGER_MARGIN_FRACTION": "0.65",
                "AATS_DERIVATIVES_AUTO_HALT_MARGIN_USAGE_FRACTION": "0.75",
                "AATS_DERIVATIVES_AUTO_HALT_LIQUIDATION_GAP_FRACTION": "0.10",
                "AATS_MAX_MARGIN_USAGE_FRACTION": "0.75",
                "AATS_LIQUIDATION_BUFFER_FRACTION": "0.20",
                "AATS_OPERATOR_SESSION_COOKIE_NAME": "aats_operator_session_derivatives_live",
            }
        )
    examples.setdefault("AATS_OPENAI_API_KEY", "REPLACE_WITH_OPENAI_API_KEY")
    examples.setdefault("AATS_OKX_API_KEY", "REPLACE_WITH_REAL_OKX_API_KEY")
    examples.setdefault("AATS_OKX_API_SECRET", "REPLACE_WITH_REAL_OKX_API_SECRET")
    examples.setdefault("AATS_OKX_API_PASSPHRASE", "REPLACE_WITH_REAL_OKX_API_PASSPHRASE")
    examples.setdefault("AATS_OPERATOR_SESSION_SECRET", "REPLACE_WITH_LONG_RANDOM_OPERATOR_SESSION_SECRET")
    return examples


def _render_reference() -> str:
    lines = [
        "# Managed Profile 配置说明",
        "",
        "## 生效顺序",
        "",
        "1. `settings.py` 默认值",
        "2. managed profile 代码基线（运行时语义，不建议在 `.env` 重复）",
        "3. `configs/strategy_profiles/<profile>.yaml` 策略调参",
        "4. 对应 `.env` 里的最小 override",
        "",
        "## 四个托管 profile",
        "",
    ]
    for profile, definition in MANAGED_PROFILE_DEFINITIONS.items():
        lines.append(f"### `{profile}`")
        lines.append("")
        lines.append(f"- 运行时基线：`{definition.runtime_defaults['config_profile']}` / `{definition.runtime_defaults['mode']}`")
        lines.append(f"- 策略调参文件：`{definition.strategy_tuning_relative_path}`")
        lines.append(f"- 默认产品类型：`{definition.runtime_defaults['trading_product_type']}`")
        lines.append(f"- 默认保证金模式：`{definition.runtime_defaults['margin_mode']}`")
        lines.append(f"- 默认 OKX 模式：`{'模拟盘' if definition.runtime_defaults['okx_simulated_trading'] else '实盘'}`")
        lines.append("")
    lines.extend(
        [
        "## `.env` 里应该保留什么",
        "",
        "- 标的与资金规模",
        "- 数据库、端口、日志目录",
        "- 交易所与 OpenAI 凭证",
        "- 账户级仓位/杠杆/风控上限",
        "",
        "## 按字段分组的修改指南",
        "",
        "### 想改数据库去哪",
        "",
        "- 改根目录对应 profile 的 `.env.*` 文件。",
        "- 主要字段：",
        "  - `AATS_DATABASE_URL`",
        "  - `AATS_DATABASE_RUNTIME_LOCK_KEY`",
        "- 现货和合约建议分库；并行运行时 lock key 也要不同。",
        "",
        "### 想改端口 / 日志 / 实例隔离去哪",
        "",
        "- 改根目录对应 profile 的 `.env.*` 文件。",
        "- 主要字段：",
        "  - `AATS_API_PORT`",
        "  - `AATS_LOG_DIR`",
        "  - `AATS_OPERATOR_SESSION_COOKIE_NAME`",
        "",
        "### 想改交易所凭证和会话密钥去哪",
        "",
        "- 改根目录对应 profile 的 `.env.*` 文件。",
        "- 主要字段：",
        "  - `AATS_OKX_API_KEY`",
        "  - `AATS_OKX_API_SECRET`",
        "  - `AATS_OKX_API_PASSPHRASE`",
        "  - `AATS_OPERATOR_SESSION_SECRET`",
        "  - `AATS_OPENAI_API_KEY`",
        "",
        "### 想改仓位 / 杠杆 / 名义金额上限去哪",
        "",
        "- 改根目录对应 profile 的 `.env.*` 文件。",
        "- 现货常改：",
        "  - `AATS_DEFAULT_ORDER_QTY`",
        "  - `AATS_MAX_ABS_POSITION_QTY`",
        "  - `AATS_MAX_NOTIONAL_PER_SYMBOL`",
        "  - `AATS_MAX_OPEN_ORDERS`",
        "- 合约额外常改：",
        "  - `AATS_MAX_TARGET_LEVERAGE`",
        "  - `AATS_DEFAULT_TARGET_LEVERAGE`",
        "  - `AATS_DERIVATIVES_ONLY_REDUCE_TRIGGER_MARGIN_FRACTION`",
        "  - `AATS_DERIVATIVES_AUTO_HALT_MARGIN_USAGE_FRACTION`",
        "  - `AATS_DERIVATIVES_AUTO_HALT_LIQUIDATION_GAP_FRACTION`",
        "  - `AATS_MAX_MARGIN_USAGE_FRACTION`",
        "  - `AATS_LIQUIDATION_BUFFER_FRACTION`",
        "",
        "### 想改 AI / 自动换档去哪",
        "",
        "- 改 `configs/strategy_profiles/<profile>.yaml`。",
        "- 主要字段：",
        "  - `ai_operating_mode`",
        "  - `ai_provider`",
        "  - `ai_model_name`",
        "  - `ai_timeout_seconds`",
        "  - `ai_degrade_after_failures`",
        "  - `ai_recovery_probe_interval_seconds`",
        "  - `ai_decision_min_confidence`",
        "  - `ai_decision_max_uncertainty`",
        "  - `ai_decision_min_directional_edge`",
        "  - `ai_shadow_mode_enabled`",
        "  - `ai_execution_suggestion_mode`",
        "  - `strategy_profile_auto_control_enabled`",
        "  - `strategy_profile_auto_rollback_enabled`",
        "  - `strategy_profile_emergency_safety_fast_track_enabled`",
        "",
        "### 想改 directional 去哪",
        "",
        "- 改 `configs/strategy_profiles/<profile>.yaml`。",
        "- 主要字段：",
        "  - `max_decisions_per_minute`",
        "  - `decision_min_interval_seconds_15m`",
        "  - `decision_min_interval_seconds_1h`",
        "  - `decision_min_price_move_bps`",
        "  - `decision_min_momentum_delta`",
        "  - `strategy_short_bias_enabled`",
        "  - `strategy_dynamic_leverage_enabled`",
        "  - `strategy_entry_*`",
        "  - `strategy_short_entry_*`",
        "  - `strategy_scale_in_*`",
        "  - `strategy_short_scale_in_*`",
        "  - `strategy_reversal_*`",
        "  - `strategy_short_reversal_*`",
        "  - `strategy_min_hold_seconds`",
        "  - `strategy_post_close_cooldown_seconds`",
        "  - `strategy_max_fee_drag_ratio`",
        "  - `strategy_max_churn_ratio`",
        "",
        "### 想改智能套利（smart_arbitrage）去哪",
        "",
        "- 改 `configs/strategy_profiles/<profile>.yaml`。",
        "- 主要字段：",
        "  - `smart_arbitrage_enabled`",
        "  - `smart_arbitrage_basis_entry_bps`",
        "  - `smart_arbitrage_basis_exit_bps`",
        "  - `smart_arbitrage_estimated_cost_bps`",
        "  - `smart_arbitrage_quote_budget_per_trade`",
        "  - `smart_arbitrage_max_pair_notional`",
        "  - `smart_arbitrage_hedge_target_leverage`",
        "",
        "### 想改现货网格（spot_grid）去哪",
        "",
        "- 改 `configs/strategy_profiles/<profile>.yaml`。",
        "- 主要字段：",
        "  - `spot_grid_enabled`",
        "  - `spot_grid_anchor_lookback_snapshots`",
        "  - `spot_grid_band_bps`",
        "  - `spot_grid_inventory_floor_fraction`",
        "  - `spot_grid_inventory_ceiling_fraction`",
        "  - `spot_grid_rebalance_min_fraction_of_max_qty`",
        "  - `spot_grid_breakout_guard_enabled`",
        "",
        "### 想改定投（dca）去哪",
        "",
        "- 改 `configs/strategy_profiles/<profile>.yaml`。",
        "- 主要字段：",
        "  - `dca_enabled`",
        "  - `dca_interval_seconds`",
        "  - `dca_quote_budget_per_cycle`",
        "  - `dca_max_position_fraction_of_limit`",
        "  - `dca_pullback_only_enabled`",
        "  - `dca_pullback_entry_bps`",
        "",
        "### 想改多策略自动并行 / sleeve 预算去哪",
        "",
        "- 改 `configs/strategy_profiles/<profile>.yaml`。",
        "- 主要字段：",
        "  - `strategy_family_active`",
        "  - `strategy_family_auto_selection_enabled`",
        "  - `strategy_sleeve_auto_parallel_enabled`",
        "  - `strategy_sleeve_auto_min_budget_multiplier`",
        "  - `strategy_sleeve_auto_reconciliation_contraction_multiplier`",
        "  - `strategy_sleeve_auto_soft_loss_usdt`",
        "  - `strategy_sleeve_auto_hard_loss_usdt`",
        "  - `strategy_sleeve_auto_volatility_cap_enabled`",
        "",
        "### 想改试盘守护去哪",
        "",
        "- 改 `configs/strategy_profiles/<profile>.yaml`。",
        "- 主要字段：",
        "  - `trial_guard_enabled`",
        "  - `trial_guard_poll_interval_seconds`",
        "  - `trial_guard_lookback_fills`",
        "  - `trial_guard_min_closed_fills`",
        "  - `trial_guard_max_daily_loss_usdt`",
        "  - `trial_guard_max_consecutive_losses`",
        "  - `trial_guard_max_fee_to_notional_ratio`",
        "  - `trial_guard_max_high_slippage_ratio`",
        "  - `trial_guard_max_slow_submit_to_fill_ratio`",
        "",
        "## 策略调参应该放哪里",
        "",
    ]
    )
    for group_name, fields in STRATEGY_FIELD_GROUPS:
        lines.append(f"### {group_name}")
        lines.append("")
        for field in fields:
            lines.append(f"- `{field}`")
        lines.append("")
    lines.extend(
        [
            "## 已标记为 deprecated / 不建议继续写入 managed `.env` 的字段",
            "",
            "| 字段 | 说明 |",
            "| --- | --- |",
        ]
    )
    for field, note in DEPRECATED_FIELD_ROWS:
        lines.append(f"| `{field}` | {note} |")
    lines.extend(
        [
            "",
            "## legacy `configs/*.yaml` 当前职责",
            "",
            "- 仍保留给非托管/manual `config_profile` 路径与测试使用",
            "- 托管 profile（`spot/derivatives/spot_live/derivatives_live`）不再叠加这些 YAML",
            "- 新的策略调参统一走 `configs/strategy_profiles/*.yaml`",
            "",
        ]
    )
    return "\n".join(lines)


def _render_configs_readme() -> str:
    return "\n".join(
        [
            "# configs 目录职责",
            "",
            "## 当前推荐路径",
            "",
            "- `spot / derivatives / spot_live / derivatives_live` 四个托管 profile：",
            "  - 运行时语义来自代码里的 managed profile 基线",
            "  - 最小 override 来自项目根目录四个 `.env.*` 文件",
            "  - 策略调参来自 `configs/strategy_profiles/*.yaml`",
            "",
            "## legacy `configs/*.yaml` 的职责",
            "",
            "- 只保留给非托管/manual `config_profile` 路径与测试使用",
            "- 不再作为四个托管 profile 的主配置来源",
            "- `base.yaml` 主要是本地演示/开发默认值说明，不是当前实盘推荐配置",
            "",
            "## 目录说明",
            "",
            "- `strategy_profiles/`：托管 profile 使用的策略调参文件",
            "- `templates/`：自动生成的最小 `.env` 示例模板",
            "- 其余 YAML：legacy/manual `config_profile` 路径或测试兼容",
            "",
            "## 维护规则",
            "",
            "- 账户、数据库、端口、日志、凭证类 override 改根目录 `.env.*`",
            "- AI、自动换档、directional / smart_arbitrage / spot_grid / dca 调参改 `strategy_profiles/*.yaml`",
            "- 若新增设置字段，优先更新 `aats/bootstrap/settings.py`，再决定它应归属 `.env` 还是 `strategy_profiles/*.yaml`",
            "",
        ]
    )


def _sync_local_env_files() -> None:
    profile_to_filename: dict[ManagedEnvProfile, str] = {
        "spot": ".env.spot",
        "spot_live": ".env.spot.live",
        "derivatives": ".env.derivatives",
        "derivatives_live": ".env.derivatives.live",
    }
    for profile, filename in profile_to_filename.items():
        path = ROOT / filename
        existing = _read_existing_env(path)
        values = _example_values_for_profile(profile)
        for key, value in existing.items():
            if key in MANAGED_PROFILE_DERIVED_ENV_KEYS:
                continue
            values[key] = value
        _write_env_file(path, _render_env(profile, values))


def _write_example_env_files() -> None:
    templates_dir = ROOT / "configs" / "templates"
    templates_dir.mkdir(parents=True, exist_ok=True)
    for profile in MANAGED_PROFILE_DEFINITIONS:
        path = templates_dir / f".env.{profile}.example"
        _write_env_file(path, _render_env(profile, _example_values_for_profile(profile)))


def _write_reference_doc() -> None:
    docs_dir = ROOT / "docs" / "configuration"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "managed-config-reference.md").write_text(_render_reference().rstrip() + "\n", encoding="utf-8")


def _write_configs_readme() -> None:
    (ROOT / "configs" / "README.md").write_text(_render_configs_readme().rstrip() + "\n", encoding="utf-8")


def main() -> None:
    args = _parse_args()
    _write_example_env_files()
    _write_reference_doc()
    _write_configs_readme()
    if args.sync_local:
        _sync_local_env_files()
    print("managed config artifacts generated")


if __name__ == "__main__":
    main()
