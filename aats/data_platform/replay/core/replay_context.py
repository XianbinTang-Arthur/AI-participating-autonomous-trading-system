"""Replay context: bar-level data model for historical replay.

Phase 2 replay 以 Gold replay bars 为输入，逐 bar 构建上下文供策略 adapter 评估。
本模块定义 replay 流程中所有共享的数据结构。

Edge Contract（P0-3 统一语义）：
    所有 family adapter 必须按以下 5 层分解输出 edge：
    - signal_edge_proxy_bps:   来自策略信号（score / momentum / trend / alpha）的机会代理
    - funding_adjustment_bps:  来自 funding rate 的附加调整
    - cost_bps:               成本总计（blended fee + slippage，费率按执行策略混合 maker/taker）
    - noise_buffer_bps:       信号噪声缓冲（对齐生产端 strategy_edge_noise_buffer_bps）
    - expected_net_edge_bps:  = signal_edge_proxy_bps + funding_adjustment_bps - cost_bps - noise_buffer_bps

    内部估算方式可以不同，但输出语义必须统一。
"""

from __future__ import annotations

import copy
import dataclasses as dc
import math
import re
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar, Literal


REPLAY_EXECUTION_STYLES = frozenset(
    {
        "bounded_limit_ioc",
        "maker",
        "passive",
        "passive_first",
        "bounded_limit",
        "taker",
        "bounded_taker_cap",
        "exchange",
        "market",
    }
)


# ---------------------------------------------------------------------------
# 输入：Gold replay bar 行
# ---------------------------------------------------------------------------

@dc.dataclass(frozen=True)
class ReplayBar:
    """从 gold.market_*_replay_bars_* 读取的一行。"""
    symbol: str
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None
    quote_volume: Decimal | None
    is_closed: bool
    aligned_funding_rate: Decimal | None
    funding_source_ts: datetime | None


def canonicalize_replay_timeframe(timeframe: str) -> str:
    """Return the stable spelling of one fixed replay timeframe."""
    if not isinstance(timeframe, str):
        raise ValueError(
            "Unsupported replay timeframe for causal timing: "
            f"{timeframe!r}; expected <positive integer>[m|h|d]"
        )
    match = re.fullmatch(r"([1-9][0-9]*)([mhd])", timeframe.strip().lower())
    if match is None:
        raise ValueError(
            "Unsupported replay timeframe for causal timing: "
            f"{timeframe!r}; expected <positive integer>[m|h|d]"
        )
    return f"{int(match.group(1))}{match.group(2)}"


def parse_replay_timeframe(timeframe: str) -> timedelta:
    """Parse a fixed replay timeframe without accepting calendar periods."""
    canonical = canonicalize_replay_timeframe(timeframe)
    amount = int(canonical[:-1])
    unit = canonical[-1]
    if unit == "m":
        return timedelta(minutes=amount)
    if unit == "h":
        return timedelta(hours=amount)
    return timedelta(days=amount)


# ---------------------------------------------------------------------------
# Replay 成本配置
# ---------------------------------------------------------------------------

@dc.dataclass(frozen=True)
class ReplayCostConfig:
    """可配置的交易成本模型。

    默认值对齐生产 derivatives_live 配置（maker/taker 混合费率 + 合理滑点）。
    所有值单位为 bps（1 bps = 0.01%）。

    OKX 费率参考（2024/2025）：
    - Swap taker fee:  0.05% = 5 bps（普通用户），VIP 可低至 2-3 bps
    - Swap maker fee:  0.02% = 2 bps（普通用户）
    - 滑点:           OKX BTC-USDT-SWAP 实际 <1 bps（derivatives_live.yaml 注释）

    费率混合公式（完整对齐 fee_resolver.py:175-182）：
    当 execution_style 为 limit 类型时，
    maker_bias  = clamp(-maker_taker_bias, 0, 1)
    maker_weight = clamp(0.15 + passive_bias * 0.45 + maker_bias * 0.20, 0, 0.80)
    blended_fee  = taker * (1 - maker_weight) + maker * maker_weight

    execution_style 名称映射：
    - 生产 fee_resolver 原生识别: bounded_limit_ioc / maker / passive
    - 策略层别名（执行层映射后走同一路径）: passive_first / bounded_limit
    - taker 类: taker / bounded_taker_cap / exchange / market → 不混合
    """
    taker_fee_bps: float = 5.0       # OKX swap taker 0.05% = 5 bps
    slippage_bps: float = 1.0        # 对齐生产 configured_slippage ≈ max_tolerance(20)×fraction(0.05)=1.0
    maker_fee_bps: float = 2.0       # OKX swap maker 0.02% = 2 bps
    execution_style: str = "passive_first"   # 对齐 strategy_hedge_independent_entry_execution_mode
    passive_bias: float = 0.7        # passive_first 默认 0.7，bounded_limit 默认 0.5
    maker_taker_bias: float = 0.0    # 生产默认 0；非零时偏移 maker_weight（fee_resolver.py:177）

    # fee_resolver.py:175 原生识别的 limit 类 style + 策略层别名
    _BLENDED_STYLES: ClassVar[frozenset[str]] = frozenset({
        "bounded_limit_ioc", "maker", "passive",   # fee_resolver 原生
        "passive_first", "bounded_limit",           # 策略层别名
    })

    def __post_init__(self) -> None:
        resolved: dict[str, float] = {}
        for name in (
            "taker_fee_bps",
            "slippage_bps",
            "maker_fee_bps",
            "passive_bias",
            "maker_taker_bias",
        ):
            value = getattr(self, name)
            if type(value) is bool:
                raise ValueError(f"{name} must be numeric, not boolean")
            try:
                finite = math.isfinite(float(value))
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"{name} must be numeric") from exc
            if not finite:
                raise ValueError(f"{name} must be finite")
            canonical = float(value)
            if canonical == 0.0:
                canonical = 0.0
            resolved[name] = canonical
            object.__setattr__(self, name, resolved[name])
        if not isinstance(self.execution_style, str):
            raise ValueError("execution_style must be a string")
        canonical_style = self.execution_style.strip().lower()
        if not canonical_style:
            raise ValueError("execution_style must be non-empty")
        object.__setattr__(self, "execution_style", canonical_style)
        if not -10_000.0 < resolved["maker_fee_bps"] < 10_000.0:
            raise ValueError("replay_maker_fee_bps_out_of_range")
        if not 0.0 <= resolved["taker_fee_bps"] < 10_000.0:
            raise ValueError("replay_taker_fee_bps_out_of_range")
        if not 0.0 <= resolved["slippage_bps"] < 10_000.0:
            raise ValueError("replay_slippage_bps_out_of_range")
        if not 0.0 <= resolved["passive_bias"] <= 1.0:
            raise ValueError("replay_passive_bias_out_of_range")
        if not -1.0 <= resolved["maker_taker_bias"] <= 1.0:
            raise ValueError("replay_maker_taker_bias_out_of_range")
        if canonical_style not in REPLAY_EXECUTION_STYLES:
            raise ValueError("replay_execution_style_unsupported")

    @property
    def blended_fee_bps(self) -> float:
        """按执行策略计算的混合费率（bps）。

        完整对齐 fee_resolver.py:175-182，包含 maker_taker_bias 项。
        """
        style = self.execution_style.lower()
        if style in self._BLENDED_STYLES:
            passive = min(max(self.passive_bias, 0.0), 1.0)
            maker_bias = min(max(-self.maker_taker_bias, 0.0), 1.0)
            maker_weight = min(max(0.15 + passive * 0.45 + maker_bias * 0.20, 0.0), 0.80)
            return self.taker_fee_bps * (1.0 - maker_weight) + self.maker_fee_bps * maker_weight
        return self.taker_fee_bps

    @property
    def total_cost_bps(self) -> float:
        """单次开平仓的单边成本（bps）= blended_fee + slippage。"""
        return self.blended_fee_bps + self.slippage_bps

    def to_dict(self) -> dict[str, Any]:
        return {
            "taker_fee_bps": self.taker_fee_bps,
            "slippage_bps": self.slippage_bps,
            "maker_fee_bps": self.maker_fee_bps,
            "execution_style": self.execution_style,
            "passive_bias": self.passive_bias,
            "maker_taker_bias": self.maker_taker_bias,
            # 以下为只读计算属性（from_dict 不消费，仅供可观测性）
            "blended_fee_bps": self.blended_fee_bps,
            "total_cost_bps": self.total_cost_bps,
        }

    @classmethod
    def from_dict(
        cls,
        d: dict[str, Any],
        *,
        base: ReplayCostConfig | None = None,
    ) -> ReplayCostConfig:
        if not isinstance(d, dict) or any(not isinstance(key, str) for key in d):
            raise ValueError("cost_config must be a string-keyed mapping")
        writable_keys = {
            "taker_fee_bps",
            "slippage_bps",
            "maker_fee_bps",
            "execution_style",
            "passive_bias",
            "maker_taker_bias",
        }
        read_only_keys = {"blended_fee_bps", "total_cost_bps"}
        unknown = set(d) - writable_keys - read_only_keys
        if unknown:
            raise ValueError(
                "unknown_cost_config_keys:" + ",".join(sorted(unknown))
            )
        defaults = base or cls()

        def _number(key: str, default: float) -> float:
            if key not in d:
                return float(default)
            value = d[key]
            if value is None:
                raise ValueError(f"{key} must not be null")
            if type(value) is bool:
                raise ValueError(f"{key} must be numeric, not boolean")
            try:
                return float(value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"{key} must be numeric") from exc

        execution_style = d.get("execution_style", defaults.execution_style)
        if "execution_style" in d and execution_style is None:
            raise ValueError("execution_style must not be null")
        if not isinstance(execution_style, str):
            raise ValueError("execution_style must be a string")
        resolved = cls(
            taker_fee_bps=_number("taker_fee_bps", defaults.taker_fee_bps),
            slippage_bps=_number("slippage_bps", defaults.slippage_bps),
            maker_fee_bps=_number("maker_fee_bps", defaults.maker_fee_bps),
            execution_style=execution_style,
            passive_bias=_number("passive_bias", defaults.passive_bias),
            maker_taker_bias=_number(
                "maker_taker_bias",
                defaults.maker_taker_bias,
            ),
        )
        for key in read_only_keys & set(d):
            provided = d[key]
            if provided is None or type(provided) is bool:
                raise ValueError(f"{key} must be numeric")
            try:
                matches = float(provided) == float(getattr(resolved, key))
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"{key} must be numeric") from exc
            if not matches:
                raise ValueError(f"{key} does not match resolved cost config")
        return resolved


# ---------------------------------------------------------------------------
# Replay 参数覆盖
# ---------------------------------------------------------------------------

@dc.dataclass(frozen=True)
class ReplayParameterOverrides:
    """可在 replay 实验中覆盖的策略参数。

    Phase 2 冻结参数（均可通过 CLI --param key=value 覆盖）：

    策略门槛参数：
    - min_confirm_ticks           信号确认强度
    - score_stability_threshold   强信号是否被过度拦截
    - min_safe_net_edge_bps       边缘机会放行下限

    Signal edge 校准参数：
    - signal_edge_scale_bps       score -> bps 的缩放系数（影响 signal proxy 绝对值）
    - directional_trend_weight    directional 的趋势/return 混合权重（0~1）
    - directional_return_clamp_bps  directional bar return 限幅（bps）

    Phase 1 扩展参数（与 production settings 直接语义对齐）：
    - entry_threshold             开仓评分阈值（long）
    - close_threshold             平仓评分阈值（long）
    - scale_in_threshold          加仓评分阈值（long）
    - short_entry_threshold       开仓评分阈值（short，None = 同 entry_threshold）
    - short_close_threshold       平仓评分阈值（short，None = 同 close_threshold）
    - strategy_short_bias_enabled 是否允许 independent replay 选择 short leg
    - min_hold_seconds            最小持仓秒数
    - rebalance_cooldown_seconds  平仓后冷却秒数
    - max_thesis_age_seconds      thesis 最长存活秒数
    - de_risk_net_edge_bps        降风险触发阈值（bps）
    - failed_thesis_net_edge_bps  thesis 失效阈值（bps）
    - expected_slippage_buffer_bps 滑点缓冲（bps）
    - expected_execution_buffer_bps 执行缓冲（bps）
    - max_acceptable_cost_bps     最大允许单边成本（bps）
    - min_score_drawdown_bps      最大评分回撤容忍（bps）
    - min_liquidity_quality       最低流动性质量分
    - limit_offset_bps_entry      限价偏移（bps）

    成本模型（也可通过 --param taker_fee_bps=5 直接覆盖）：
    - cost_config                 交易成本配置（taker_fee_bps + slippage_bps）
    """
    min_confirm_ticks: int = 2
    score_stability_threshold: float = 5.0
    min_safe_net_edge_bps: float = 2.0

    # Signal edge 校准参数（收口在这里，不锁死在 adapter 内部常量）
    signal_edge_scale_bps: float = 12.0
    """score -> bps 的缩放系数。score=0.6 * 12 = 7.2 bps 信号代理。
    与 _PARAM_DEFAULTS (rdp_run_step3_research.py) 对齐，两端唯一真相源。"""

    directional_trend_weight: float = 0.7
    """directional adapter 里 趋势强度 vs bar return 的混合权重。
    signal = weight * trend_signal + (1-weight) * clamped_return。"""

    directional_return_clamp_bps: float = 20.0
    """directional adapter 里 bar return 的限幅（bps）。防止单根极端 bar 主导 signal。"""

    # ── Phase 1 扩展：进出场阈值 ──────────────────────────────────
    # 默认值对齐 derivatives_live.yaml 实盘钉住值
    entry_threshold: float = 0.30
    """long book 开仓评分门槛。生产端映射: strategy_hedge_independent_long_entry_threshold
    对齐 derivatives_live.yaml:342 实盘钉住值 0.30。"""

    close_threshold: float = 0.15
    """long book 平仓评分门槛。生产端映射: strategy_hedge_independent_long_close_threshold"""

    scale_in_threshold: float = 0.40
    """long book 加仓评分门槛。生产端映射: strategy_hedge_independent_long_scale_in_threshold
    对齐 derivatives_live.yaml:353 实盘钉住值 0.40。
    ⚠️ REPLAY 未模拟: 当前 replay 只有 open/hold/close 三态，没有 scale-in（加仓）逻辑。
    该参数仅做透传映射到生产端，replay 回测不验证其效果。"""

    short_entry_threshold: float | None = None
    """short book 开仓阈值。None 时使用 entry_threshold（对称模式）。"""

    short_close_threshold: float | None = None
    """short book 平仓阈值。None 时使用 close_threshold（对称模式）。"""

    strategy_short_bias_enabled: bool = True
    """是否允许 independent replay 计算并选择 short leg。

    字段名与生产 ``AATSSettings.strategy_short_bias_enabled`` 完全一致。默认 ``True``
    用于兼容当前 derivatives replay 与 tracked derivatives profiles；面向指定 profile
    生成正式证据时，调用方必须显式传入该 profile 解析后的实际值。
    """

    # ── Phase 1 扩展：持仓时间管理 ────────────────────────────────
    min_hold_seconds: float = 300.0
    """最小持仓秒数，防止过频交易。生产端映射: strategy_hedge_independent_long_min_hold_seconds"""

    rebalance_cooldown_seconds: float = 120.0
    """平仓后冷却秒数。生产端映射: strategy_hedge_independent_rebalance_cooldown_seconds"""

    max_thesis_age_seconds: float = 1800.0
    """thesis 最长存活秒数。生产端映射: strategy_hedge_independent_max_thesis_age_seconds"""

    # ── Phase 1 扩展：风险管理阈值 ────────────────────────────────
    de_risk_net_edge_bps: float = 2.0
    """净边际变薄时触发降风险的阈值（bps）。生产端映射: strategy_hedge_independent_de_risk_net_edge_bps"""

    failed_thesis_net_edge_bps: float = -1.0
    """净边际低于此值视为 thesis 失效并退出（bps）。
    约束: 必须 <= de_risk_net_edge_bps。
    生产端映射: strategy_hedge_independent_failed_thesis_net_edge_bps"""

    catastrophic_failed_thesis_buffer_bps: float = 3.0
    """灾难性 failed_thesis 缓冲（bps），whipsaw 防护阈值。
    仅当 expected_net_edge_bps <= failed_thesis_net_edge_bps - 此缓冲 时，
    判定为灾难性 thesis 失效，允许豁免 min_hold 立即出场。
    默认 3.0 bps：覆盖 BTC-USDT-SWAP 正常噪声带（~1-2 bps），
    确保只有真实深度亏损才触发紧急止损。
    生产端映射: strategy_hedge_independent_catastrophic_failed_thesis_buffer_bps"""

    # ── Phase 1 扩展：成本缓冲 ────────────────────────────────────
    expected_slippage_buffer_bps: float = 0.5
    """开仓预期滑点缓冲（bps）。叠加到 cost 计算中。
    生产端映射: strategy_hedge_independent_expected_slippage_buffer_bps"""

    expected_execution_buffer_bps: float = 0.5
    """开仓执行缓冲（bps）。叠加到 cost 计算中。
    生产端映射: strategy_hedge_independent_expected_execution_buffer_bps"""

    max_acceptable_cost_bps: float = 7.5
    """最大允许的单边预期成本（bps）。超出则阻断。
    生产端映射: strategy_hedge_independent_max_acceptable_cost_bps"""

    # ── Phase 1 扩展：评分质量 ────────────────────────────────────
    min_score_drawdown_bps: float | None = 6.0
    """评分最大回撤容忍度（bps）。None 时仅使用 score_stability_threshold。
    对齐 derivatives_live.yaml:389 实盘值 6.0。
    生产端映射: strategy_hedge_independent_min_score_drawdown_bps"""

    min_liquidity_quality: float = 0.55
    """最低流动性质量分。replay 默认 liq=1.0，此参数做灵敏度分析。
    生产端映射: strategy_hedge_independent_min_liquidity_quality"""

    # ── Phase 1 扩展：执行策略 ────────────────────────────────────
    limit_offset_bps_entry: float = 1.5
    """开仓限价偏移（bps）。影响成交率与滑点。
    生产端映射: strategy_hedge_independent_limit_offset_bps_entry
    ⚠️ REPLAY 未模拟: causal harness 已禁止同 bar 成交，但仍没有按该 offset
    做订单簿限价匹配。该参数仅做透传映射到生产端，replay 回测不验证其效果。"""

    # ── 信号噪声缓冲 ─────────────────────────────────────────
    noise_buffer_bps: float = 2.0
    """信号噪声缓冲（bps），从 net_edge 中扣除。
    对齐生产端 strategy_edge_noise_buffer_bps（derivatives_live.yaml = 2.0）。
    ⚠️ 此参数为 YAML-only，不在 RDP active_parameters 映射中，
    但校准时必须纳入以匹配生产门控行为。"""

    # 成本配置
    cost_config: ReplayCostConfig = dc.field(default_factory=ReplayCostConfig)

    # 可扩展的额外参数
    extra: dict[str, Any] = dc.field(default_factory=dict)

    def __post_init__(self) -> None:
        """校验并规范化会进入 artifact/fingerprint 的策略参数。"""
        if type(self.extra) is not dict:
            raise ValueError("extra must be a dict")
        if any(type(key) is not str or not key for key in self.extra):
            raise ValueError("extra keys must be non-empty strings")
        # ``extra`` remains an opaque compatibility payload for legacy replay
        # callers.  Detach nested values from the caller so later mutation of
        # the source mapping cannot silently alter this frozen dataclass.  The
        # versioned backtest artifact contract separately requires this mapping
        # to be empty, so opaque values never become part of that schema.
        try:
            frozen_extra = copy.deepcopy(self.extra)
        except Exception as exc:
            raise ValueError("extra values must support defensive copying") from exc
        object.__setattr__(self, "extra", frozen_extra)
        if type(self.min_confirm_ticks) is not int or self.min_confirm_ticks < 1:
            raise ValueError("min_confirm_ticks must be a positive integer")
        if type(self.strategy_short_bias_enabled) is not bool:
            raise ValueError(
                "strategy_short_bias_enabled 必须是 bool，"
                f"实际为 {type(self.strategy_short_bias_enabled).__name__}"
            )
        optional_numeric_fields = {
            "short_entry_threshold",
            "short_close_threshold",
            "min_score_drawdown_bps",
        }
        numeric_fields = {
            field.name: getattr(self, field.name)
            for field in dc.fields(self)
            if field.name
            not in {
                "min_confirm_ticks",
                "strategy_short_bias_enabled",
                "cost_config",
                "extra",
            }
        }
        for name, value in numeric_fields.items():
            if value is None:
                if name not in optional_numeric_fields:
                    raise ValueError(f"{name} must not be null")
                continue
            if type(value) is bool:
                raise ValueError(f"{name} must be numeric, not boolean")
            if type(value) not in {int, float, Decimal}:
                raise ValueError(f"{name} must be numeric")
            try:
                canonical = float(value)
            except OverflowError as exc:
                raise ValueError(f"{name} must be finite") from exc
            if not math.isfinite(canonical):
                raise ValueError(f"{name} must be finite")
            # JSON distinguishes -0.0 lexically even though the economics do
            # not.  Normalize before dataclass serialization/fingerprinting.
            if canonical == 0.0:
                canonical = 0.0
            object.__setattr__(self, name, canonical)
        unit_interval_fields = {
            "entry_threshold": self.entry_threshold,
            "close_threshold": self.close_threshold,
            "scale_in_threshold": self.scale_in_threshold,
            "directional_trend_weight": self.directional_trend_weight,
            "min_liquidity_quality": self.min_liquidity_quality,
        }
        if self.short_entry_threshold is not None:
            unit_interval_fields["short_entry_threshold"] = (
                self.short_entry_threshold
            )
        if self.short_close_threshold is not None:
            unit_interval_fields["short_close_threshold"] = (
                self.short_close_threshold
            )
        for name, value in unit_interval_fields.items():
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0, 1]")
        non_negative_fields = {
            "score_stability_threshold": self.score_stability_threshold,
            "min_safe_net_edge_bps": self.min_safe_net_edge_bps,
            "min_hold_seconds": self.min_hold_seconds,
            "rebalance_cooldown_seconds": self.rebalance_cooldown_seconds,
            "max_thesis_age_seconds": self.max_thesis_age_seconds,
            "expected_slippage_buffer_bps": (
                self.expected_slippage_buffer_bps
            ),
            "expected_execution_buffer_bps": (
                self.expected_execution_buffer_bps
            ),
            "max_acceptable_cost_bps": self.max_acceptable_cost_bps,
            "limit_offset_bps_entry": self.limit_offset_bps_entry,
            "noise_buffer_bps": self.noise_buffer_bps,
        }
        if self.min_score_drawdown_bps is not None:
            non_negative_fields["min_score_drawdown_bps"] = (
                self.min_score_drawdown_bps
            )
        for name, value in non_negative_fields.items():
            if value < 0.0:
                raise ValueError(f"{name} must be non-negative")
        if self.signal_edge_scale_bps <= 0.0:
            raise ValueError("signal_edge_scale_bps must be positive")
        if self.directional_return_clamp_bps <= 0.0:
            raise ValueError("directional_return_clamp_bps must be positive")
        if self.failed_thesis_net_edge_bps > self.de_risk_net_edge_bps:
            raise ValueError(
                f"约束违反: failed_thesis_net_edge_bps ({self.failed_thesis_net_edge_bps}) "
                f"必须 <= de_risk_net_edge_bps ({self.de_risk_net_edge_bps})"
            )
        if self.catastrophic_failed_thesis_buffer_bps < 0.0:
            raise ValueError(
                f"约束违反: catastrophic_failed_thesis_buffer_bps "
                f"({self.catastrophic_failed_thesis_buffer_bps}) 必须 >= 0"
            )
        if self.close_threshold > self.entry_threshold:
            raise ValueError(
                f"约束违反: close_threshold ({self.close_threshold}) "
                f"应当 <= entry_threshold ({self.entry_threshold})"
            )
        if self.scale_in_threshold < self.entry_threshold:
            raise ValueError(
                f"约束违反: scale_in_threshold ({self.scale_in_threshold}) "
                f"应当 >= entry_threshold ({self.entry_threshold})"
            )
        if self.short_entry_threshold is not None and self.short_close_threshold is not None:
            if self.short_close_threshold > self.short_entry_threshold:
                raise ValueError(
                    f"约束违反: short_close_threshold ({self.short_close_threshold}) "
                    f"应当 <= short_entry_threshold ({self.short_entry_threshold})"
                )
        safe_edge = (
            self.min_safe_net_edge_bps
            + self.expected_slippage_buffer_bps
            + self.expected_execution_buffer_bps
        )
        # 要求 safe_edge >= de_risk + 1.0 bps 最小间距
        # 目的: 持仓区间 [safe_edge, ∞) 与 de_risk 区间 (-∞, de_risk] 之间
        # 至少有 1 bps hysteresis 带，避免边际信号反复翻转 entry/de_risk
        if safe_edge < self.de_risk_net_edge_bps + 1.0:
            raise ValueError(
                f"约束违反: safe_edge ({self.min_safe_net_edge_bps} + "
                f"{self.expected_slippage_buffer_bps} + "
                f"{self.expected_execution_buffer_bps} = {safe_edge}) "
                f"必须 >= de_risk_net_edge_bps ({self.de_risk_net_edge_bps}) + 1.0 bps，"
                f"否则持仓 hysteresis 带过窄会导致边际信号反复翻转"
            )
        if self.min_hold_seconds > self.max_thesis_age_seconds:
            raise ValueError(
                f"约束违反: min_hold_seconds ({self.min_hold_seconds}) "
                f"必须 <= max_thesis_age_seconds ({self.max_thesis_age_seconds})，"
                f"否则 min_hold 锁定期间 stale_thesis 无法触发正常退出"
            )

    # ── 工厂方法：按 family 获取合理默认参数 ─────────────────────
    @classmethod
    def for_family(cls, family: str = "independent") -> "ReplayParameterOverrides":
        """获取指定 family 的默认参数。

        directional 家族原始硬编码阈值与 independent 不同：
          - directional: entry=0.45, close=0.20
          - independent: entry=0.30, close=0.15
        使用本方法可避免共享默认值导致的静默行为变更。
        """
        if family == "directional":
            return cls(
                entry_threshold=0.45,
                close_threshold=0.20,
                scale_in_threshold=0.55,
            )
        if family == "independent":
            return cls()
        raise ValueError(f"unsupported_replay_family:{family}")

    # ── 辅助方法：获取方向特定阈值 ──────────────────────────────
    def get_entry_threshold(self, leg: str = "long") -> float:
        """获取指定方向的开仓阈值。"""
        if leg == "short" and self.short_entry_threshold is not None:
            return self.short_entry_threshold
        return self.entry_threshold

    def get_close_threshold(self, leg: str = "long") -> float:
        """获取指定方向的平仓阈值。"""
        if leg == "short" and self.short_close_threshold is not None:
            return self.short_close_threshold
        return self.close_threshold

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "min_confirm_ticks": self.min_confirm_ticks,
            "score_stability_threshold": self.score_stability_threshold,
            "min_safe_net_edge_bps": self.min_safe_net_edge_bps,
            "signal_edge_scale_bps": self.signal_edge_scale_bps,
            "directional_trend_weight": self.directional_trend_weight,
            "directional_return_clamp_bps": self.directional_return_clamp_bps,
            # Phase 1 扩展
            "entry_threshold": self.entry_threshold,
            "close_threshold": self.close_threshold,
            "scale_in_threshold": self.scale_in_threshold,
            "short_entry_threshold": self.short_entry_threshold,
            "short_close_threshold": self.short_close_threshold,
            "strategy_short_bias_enabled": self.strategy_short_bias_enabled,
            "min_hold_seconds": self.min_hold_seconds,
            "rebalance_cooldown_seconds": self.rebalance_cooldown_seconds,
            "max_thesis_age_seconds": self.max_thesis_age_seconds,
            "de_risk_net_edge_bps": self.de_risk_net_edge_bps,
            "failed_thesis_net_edge_bps": self.failed_thesis_net_edge_bps,
            "catastrophic_failed_thesis_buffer_bps": self.catastrophic_failed_thesis_buffer_bps,
            "expected_slippage_buffer_bps": self.expected_slippage_buffer_bps,
            "expected_execution_buffer_bps": self.expected_execution_buffer_bps,
            "max_acceptable_cost_bps": self.max_acceptable_cost_bps,
            "min_score_drawdown_bps": self.min_score_drawdown_bps,
            "min_liquidity_quality": self.min_liquidity_quality,
            "limit_offset_bps_entry": self.limit_offset_bps_entry,
            "noise_buffer_bps": self.noise_buffer_bps,
            "cost_config": self.cost_config.to_dict(),
        }
        if self.extra:
            d.update(self.extra)
        return d

    @classmethod
    def from_dict(
        cls,
        d: dict[str, Any],
        *,
        base: ReplayParameterOverrides | None = None,
    ) -> ReplayParameterOverrides:
        """从字典反序列化。

        支持两种成本传入方式：
        1. 嵌套: {"cost_config": {"taker_fee_bps": 5, "slippage_bps": 2}}
        2. 平铺（CLI 友好）: {"taker_fee_bps": 5, "slippage_bps": 2}
        平铺与嵌套成本来源不得并存；并存时失败关闭，避免静默覆盖。
        """
        if not isinstance(d, dict) or any(not isinstance(key, str) for key in d):
            raise ValueError("replay parameters must be a string-keyed mapping")
        known = {
            "min_confirm_ticks", "score_stability_threshold",
            "min_safe_net_edge_bps", "signal_edge_scale_bps",
            "directional_trend_weight", "directional_return_clamp_bps",
            "cost_config",
            # 平铺 cost keys（from_dict 时消费，不进 extra）
            "taker_fee_bps", "slippage_bps",
            "maker_fee_bps", "execution_style", "passive_bias", "maker_taker_bias",
            # Phase 1 扩展参数
            "entry_threshold", "close_threshold", "scale_in_threshold",
            "short_entry_threshold", "short_close_threshold",
            "strategy_short_bias_enabled",
            "min_hold_seconds", "rebalance_cooldown_seconds",
            "max_thesis_age_seconds",
            "de_risk_net_edge_bps", "failed_thesis_net_edge_bps",
            "catastrophic_failed_thesis_buffer_bps",
            "expected_slippage_buffer_bps", "expected_execution_buffer_bps",
            "max_acceptable_cost_bps",
            "min_score_drawdown_bps", "min_liquidity_quality",
            "limit_offset_bps_entry",
            "noise_buffer_bps",
        }

        defaults = base or cls()

        # 缺失字段继承 family baseline；显式 JSON null 必须失败，避免把
        # 错拼/空覆盖静默解释成另一组默认值。
        def _v(key: str, default: float) -> float:
            if key not in d:
                return float(default)
            val = d[key]
            if val is None:
                raise ValueError(f"{key} must not be null")
            if type(val) is bool:
                raise ValueError(f"{key} must be numeric, not boolean")
            if type(val) not in {int, float, Decimal}:
                raise ValueError(f"{key} must be numeric")
            try:
                return float(val)
            except OverflowError as exc:
                raise ValueError(f"{key} must be finite") from exc

        # 真正的可选数值仍允许显式 null（当前仅用于 min_score_drawdown_bps）。
        def _v_opt(key: str) -> float | None:
            val = d.get(key)
            if val is None:
                return None
            if type(val) is bool:
                raise ValueError(f"{key} must be numeric, not boolean")
            if type(val) not in {int, float, Decimal}:
                raise ValueError(f"{key} must be numeric")
            try:
                return float(val)
            except OverflowError as exc:
                raise ValueError(f"{key} must be finite") from exc

        def _v_bool(key: str, default: bool) -> bool:
            if key not in d:
                return default
            val = d[key]
            if val is None:
                raise ValueError(f"{key} must not be null")
            if type(val) is not bool:
                raise ValueError(
                    f"{key} 必须是 JSON boolean，实际为 {type(val).__name__}"
                )
            return val

        # 成本配置：优先从平铺 keys 组装，其次从嵌套 cost_config。
        # 任一平铺成本字段都必须触发该分支，否则 execution_style/
        # bias 类单项覆盖会被静默丢弃。未指定字段继承 family
        # baseline 的完整 ReplayCostConfig，不得漂移回另一组常量。
        flat_cost_keys = {
            "taker_fee_bps",
            "slippage_bps",
            "maker_fee_bps",
            "execution_style",
            "passive_bias",
            "maker_taker_bias",
        }
        has_flat_cost = any(key in d for key in flat_cost_keys)
        if "cost_config" in d:
            cost_raw = d["cost_config"]
            if not isinstance(cost_raw, dict) or any(
                not isinstance(key, str) for key in cost_raw
            ):
                raise ValueError("cost_config must be a string-keyed mapping")
            if has_flat_cost:
                raise ValueError(
                    "flat cost parameters conflict with nested cost_config"
                )
        if has_flat_cost:
            flat_cost = {key: d[key] for key in flat_cost_keys if key in d}
            cost = ReplayCostConfig.from_dict(
                flat_cost,
                base=defaults.cost_config,
            )
        else:
            cost_raw = d.get("cost_config")
            cost = (
                ReplayCostConfig.from_dict(
                    cost_raw,
                    base=defaults.cost_config,
                )
                if "cost_config" in d
                else defaults.cost_config
            )

        confirm_raw = d.get("min_confirm_ticks")
        if "min_confirm_ticks" not in d:
            confirm = defaults.min_confirm_ticks
        else:
            if confirm_raw is None:
                raise ValueError("min_confirm_ticks must not be null")
            if type(confirm_raw) is not int:
                if type(confirm_raw) is bool:
                    raise ValueError(
                        "min_confirm_ticks must be an integer, not boolean"
                    )
                raise ValueError("min_confirm_ticks must be an integer")
            confirm = confirm_raw

        return cls(
            min_confirm_ticks=confirm,
            score_stability_threshold=_v(
                "score_stability_threshold",
                defaults.score_stability_threshold,
            ),
            min_safe_net_edge_bps=_v(
                "min_safe_net_edge_bps",
                defaults.min_safe_net_edge_bps,
            ),
            signal_edge_scale_bps=_v(
                "signal_edge_scale_bps",
                defaults.signal_edge_scale_bps,
            ),
            directional_trend_weight=_v(
                "directional_trend_weight",
                defaults.directional_trend_weight,
            ),
            directional_return_clamp_bps=_v(
                "directional_return_clamp_bps",
                defaults.directional_return_clamp_bps,
            ),
            # Phase 1 扩展
            entry_threshold=_v("entry_threshold", defaults.entry_threshold),
            close_threshold=_v("close_threshold", defaults.close_threshold),
            scale_in_threshold=_v(
                "scale_in_threshold",
                defaults.scale_in_threshold,
            ),
            short_entry_threshold=(
                _v_opt("short_entry_threshold")
                if "short_entry_threshold" in d
                else defaults.short_entry_threshold
            ),
            short_close_threshold=(
                _v_opt("short_close_threshold")
                if "short_close_threshold" in d
                else defaults.short_close_threshold
            ),
            strategy_short_bias_enabled=_v_bool(
                "strategy_short_bias_enabled",
                defaults.strategy_short_bias_enabled,
            ),
            min_hold_seconds=_v(
                "min_hold_seconds",
                defaults.min_hold_seconds,
            ),
            rebalance_cooldown_seconds=_v(
                "rebalance_cooldown_seconds",
                defaults.rebalance_cooldown_seconds,
            ),
            max_thesis_age_seconds=_v(
                "max_thesis_age_seconds",
                defaults.max_thesis_age_seconds,
            ),
            de_risk_net_edge_bps=_v(
                "de_risk_net_edge_bps",
                defaults.de_risk_net_edge_bps,
            ),
            failed_thesis_net_edge_bps=_v(
                "failed_thesis_net_edge_bps",
                defaults.failed_thesis_net_edge_bps,
            ),
            catastrophic_failed_thesis_buffer_bps=_v(
                "catastrophic_failed_thesis_buffer_bps",
                defaults.catastrophic_failed_thesis_buffer_bps,
            ),
            expected_slippage_buffer_bps=_v(
                "expected_slippage_buffer_bps",
                defaults.expected_slippage_buffer_bps,
            ),
            expected_execution_buffer_bps=_v(
                "expected_execution_buffer_bps",
                defaults.expected_execution_buffer_bps,
            ),
            max_acceptable_cost_bps=_v(
                "max_acceptable_cost_bps",
                defaults.max_acceptable_cost_bps,
            ),
            # min_score_drawdown_bps: key 缺失→6.0（新默认）; 显式 null→None（禁用 drawdown 检查）
            min_score_drawdown_bps=(
                _v_opt("min_score_drawdown_bps")
                if d.get("min_score_drawdown_bps") is not None
                else (
                    None
                    if "min_score_drawdown_bps" in d
                    else defaults.min_score_drawdown_bps
                )
            ),
            min_liquidity_quality=_v(
                "min_liquidity_quality",
                defaults.min_liquidity_quality,
            ),
            limit_offset_bps_entry=_v(
                "limit_offset_bps_entry",
                defaults.limit_offset_bps_entry,
            ),
            noise_buffer_bps=_v("noise_buffer_bps", defaults.noise_buffer_bps),
            cost_config=cost,
            extra={k: v for k, v in d.items() if k not in known},
        )


# ---------------------------------------------------------------------------
# Replay 上下文：传递给策略 adapter 的逐 bar 状态
# ---------------------------------------------------------------------------

@dc.dataclass
class ReplayState:
    """在 replay 过程中跨 bar 累积的可变状态。

    字段生命周期：
      - position_qty/side/entry_price/entry_ts: 由 adapter._advance_state 维护
        不变量: position_side=="flat" 时 entry_price 和 entry_ts 必须为 None
      - last_close_ts: 由 adapter._advance_state 在 close 时设置
      - score_history: 由 replay_runner 外部 append（adapter 内部用独立 deque）
      - bar_index: 由 replay_runner 外部设置（adapter 不读写）
    """
    position_qty: Decimal = Decimal("0")        # 当前持仓
    position_side: Literal["flat", "long", "short"] = "flat"
    entry_price: Decimal | None = None
    entry_ts: datetime | None = None
    score_history: list[float] = dc.field(default_factory=list)   # runner 维护，adapter 不使用
    bar_index: int = 0                                            # runner 维护，adapter 不使用
    last_close_ts: datetime | None = None       # 上次平仓时间（冷却用）


@dc.dataclass(frozen=True)
class ReplayBarContext:
    """区分 observation identity 与因果决策时间的单根 bar 上下文。

    ``bar.ts`` 是 Gold bar 起点和 :class:`ReplayDecision` 的稳定身份。
    已闭合 bar 直到 ``observation_completed_at_ts`` 才可观测；所有
    持仓、thesis 和冷却生命周期计算必须使用 ``decision_ts``。
    """
    bar: ReplayBar
    bar_index: int
    state: ReplayState
    params: ReplayParameterOverrides
    family: str
    symbol: str
    timeframe: str
    dataset_version: str
    observation_completed_at_ts: datetime
    decision_ts: datetime

    def __post_init__(self) -> None:
        canonical_timeframe = canonicalize_replay_timeframe(self.timeframe)
        object.__setattr__(self, "timeframe", canonical_timeframe)
        timestamps = {
            "bar.ts": self.bar.ts,
            "observation_completed_at_ts": self.observation_completed_at_ts,
            "decision_ts": self.decision_ts,
        }
        for name, value in timestamps.items():
            if not isinstance(value, datetime):
                raise ValueError(f"{name} must be a datetime")
            if value.tzinfo is None or value.utcoffset() != timedelta(0):
                raise ValueError(f"{name} must be timezone-aware UTC")
        expected_completion = self.bar.ts + parse_replay_timeframe(canonical_timeframe)
        if self.observation_completed_at_ts != expected_completion:
            raise ValueError(
                "observation completion must equal bar start plus timeframe"
            )
        if self.decision_ts < self.observation_completed_at_ts:
            raise ValueError("decision cannot precede observation completion")


# ---------------------------------------------------------------------------
# 输出：逐 bar 决策记录
# ---------------------------------------------------------------------------

@dc.dataclass(frozen=True)
class ReplayDecision:
    """策略 adapter 对单根 bar 的评估结果。

    字段对齐 Phase 2 设计决策文档 §8.3 + P0-3 统一 edge contract。

    Edge 分解（所有 family 统一语义）：
    - signal_edge_proxy_bps:   策略信号派生的机会代理值
    - funding_adjustment_bps:  funding rate 附加调整
    - cost_bps:               交易成本（blended fee + slippage）
    - noise_buffer_bps:       信号噪声缓冲
    - expected_net_edge_bps:  = signal + funding - cost - noise_buffer（最终净 edge）
    """
    ts: datetime
    family: str
    symbol: str
    timeframe: str
    state: str                              # flat / probing / holding / ...
    selectable: bool                        # 是否可选中
    execution_compatible: bool              # 是否可执行
    long_score: float
    short_score: float
    blocking_reasons: list[str]
    expected_net_edge_bps: float
    target_position_qty: Decimal
    delta_position_qty: Decimal

    # Edge 分解字段（P0-3 统一 contract）
    signal_edge_proxy_bps: float = 0.0      # 来自策略信号的机会代理
    funding_adjustment_bps: float = 0.0     # 来自 funding rate 的附加调整
    cost_bps: float = 0.0                   # 交易成本
    noise_buffer_bps: float = 0.0           # 信号噪声缓冲

    # 扩展字段
    action: str = "hold"                    # open / hold / close / blocked
    close_reason: str = ""                  # thesis_failed / de_risk / score_below_close / direction_reversal / thesis_stale
    score_stable: bool = False
    funding_rate: float | None = None
    close_price: float | None = None
    bar_index: int = 0
    # 新增字段置于旧可选位置参数之后，避免 legacy 位置构造静默错位。
    # False 仅保留 legacy 反序列化兼容；versioned harness 会失败关闭。
    cost_bps_is_explicit: bool = False

    def __post_init__(self) -> None:
        if type(self.cost_bps_is_explicit) is not bool:
            raise ValueError("cost_bps_is_explicit must be boolean")

    def to_flat_dict(self) -> dict[str, Any]:
        """序列化为平坦字典（写 CSV / parquet 用）。"""
        return {
            "ts": self.ts.isoformat(),
            "family": self.family,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "state": self.state,
            "selectable": self.selectable,
            "execution_compatible": self.execution_compatible,
            "long_score": self.long_score,
            "short_score": self.short_score,
            "blocking_reasons": "|".join(self.blocking_reasons) if self.blocking_reasons else "",
            "signal_edge_proxy_bps": self.signal_edge_proxy_bps,
            "funding_adjustment_bps": self.funding_adjustment_bps,
            "cost_bps": self.cost_bps,
            "noise_buffer_bps": self.noise_buffer_bps,
            "expected_net_edge_bps": self.expected_net_edge_bps,
            "target_position_qty": str(self.target_position_qty),
            "delta_position_qty": str(self.delta_position_qty),
            "action": self.action,
            "close_reason": self.close_reason,
            "score_stable": self.score_stable,
            "funding_rate": self.funding_rate,
            "close_price": self.close_price,
            "bar_index": self.bar_index,
        }
